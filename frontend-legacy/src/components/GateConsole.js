/**
 * GateConsole — HITL 门控控制台。
 * 左栏：等待中的门控（5s 轮询 /api/hitl/waiting）；右栏：门控详情与操作；
 * 下方：最近 run 的干预历史（只读）。
 */
const GateConsole = {
  _pollTimer: null,
  _waiting: [],
  _selectedRunId: null,
  _selectedGate: null,
  _files: [],            // [{name, open, loading, content, truncated, error}]
  _interventions: null,  // null = loading
  _injectOpen: false,
  _editMode: false,
  _rollbackOpen: false,
  _selectToken: 0,
  _recent: [],
  _historyRunId: null,
  /* Paper Review 区状态 */
  _paperRuns: [],
  _paperRunId: null,
  _paperLang: 'zh',
  _paperBusy: null,           // 'pull' | 'push' | 'revise'
  _paperNotice: null,         // {msg, isErr}
  _paperAnnotations: undefined,  // undefined=未加载 null=加载中 object=已加载
  _paperAnnErr: null,
  _paperReviseOpen: false,
  _paperReviseResult: null,   // 改稿响应或 {error}
  _paperVersions: null,       // null=加载中
  _paperVerErr: null,
  _paperToken: 0,

  /* ---------- 生命周期 ---------- */

  render(container) {
    container.innerHTML = `
      <div id="gate-root">
        <div class="gate-layout">
          <div class="card" style="margin-bottom:0">
            <h2>Waiting Gates</h2>
            <div id="gate-list"><p style="color:var(--text-muted)">Loading...</p></div>
          </div>
          <div id="gate-detail"></div>
        </div>
        <div class="card" id="gate-paper" style="margin-top:16px">
          <h2>Paper Review</h2>
          <div id="gate-paper-body"><p style="color:var(--text-muted)">Loading...</p></div>
        </div>
        <div class="card" style="margin-top:16px">
          <h2>Recent Runs</h2>
          <div id="gate-recent"><p style="color:var(--text-muted)">Loading...</p></div>
          <div id="gate-history"></div>
        </div>
      </div>`;
    this._renderDetailEmpty();
    this._startPoll();
    this._refreshWaiting();
    this._loadRecent();
    this._loadPaper();
  },

  destroy() { this._clearPoll(); },

  onEvent(event) {
    if (['stage_complete', 'stage_start', 'pipeline_started', 'pipeline_completed',
         'run_status_changed'].includes(event.type)) {
      this._refreshWaiting();
    }
  },

  _startPoll() {
    this._clearPoll();
    this._pollTimer = setInterval(() => {
      // 视图已被卸载（无 destroy 钩子时兜底）
      if (!document.getElementById('gate-root')) { this._clearPoll(); return; }
      this._refreshWaiting();
    }, 5000);
  },

  _clearPoll() {
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
  },

  /* ---------- 左栏：等待中的门控 ---------- */

  async _refreshWaiting() {
    if (!document.getElementById('gate-list')) return;  // 视图已卸载
    let runs;
    try {
      const w = await API.hitlWaiting();
      runs = (w && w.waiting_runs) || [];
    } catch (e) {
      console.warn('GateConsole waiting poll failed:', e);
      return;  // 保留旧列表
    }
    if (!document.getElementById('gate-list')) return;
    this._waiting = runs;
    this._renderList();
    // 选中 run 已不在等待 → 在详情区提示（不清空，保留上下文供阅读）
    if (this._selectedRunId && !runs.some(r => r.run_id === this._selectedRunId)) {
      const hint = document.getElementById('gate-stale-hint');
      if (hint) hint.style.display = 'block';
    }
  },

  _renderList() {
    const el = document.getElementById('gate-list');
    if (!el) return;
    if (!this._waiting.length) {
      el.innerHTML = '<p style="color:var(--text-muted)">No gates waiting.</p>';
      return;
    }
    el.innerHTML = this._waiting.map(g => `
      <div class="gate-card ${g.run_id === this._selectedRunId ? 'selected' : ''}"
           onclick="GateConsole._select('${this._escAttr(g.run_id)}')">
        <div class="gate-card-id">${this._esc(g.run_id)}</div>
        <div style="font-size:13px;margin-top:4px">
          <b>Stage ${g.stage ?? '?'} — ${this._esc(g.stage_name || '')}</b>
        </div>
        <div style="font-size:12px;color:var(--text-secondary);margin-top:4px">${this._esc(g.reason || '')}</div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:4px">${this._esc(this._ago(g.since))}</div>
      </div>`).join('');
  },

  /* ---------- 右栏：门控详情 ---------- */

  _renderDetailEmpty() {
    const el = document.getElementById('gate-detail');
    if (!el) return;
    el.innerHTML = `
      <div class="card" style="margin-bottom:0">
        <p style="color:var(--text-muted)">Select a gate from the left to view details.</p>
      </div>`;
  },

  async _select(runId) {
    this._selectedRunId = runId;
    this._injectOpen = false;
    this._editMode = false;
    this._rollbackOpen = false;
    const gate = this._waiting.find(g => g.run_id === runId) || null;
    this._selectedGate = gate;  // 快照：轮询把 run 移出等待列表后详情仍可阅读
    const token = ++this._selectToken;
    this._files = ((gate && gate.output_files) || []).map(name => ({
      name, open: false, loading: true, content: '', truncated: false, error: null,
    }));
    this._interventions = null;
    this._renderList();
    this._renderDetail();

    const stage = gate ? gate.stage : null;
    const nn = String(stage == null ? 0 : stage).padStart(2, '0');
    const fileFetches = this._files.map(async f => {
      try {
        const r = await API.hitlFile(runId, `stage-${nn}/${f.name}`);
        f.content = r.content || '';
        f.truncated = !!r.truncated;
      } catch (e) {
        f.error = String(e.message || e);
      }
      f.loading = false;
    });
    const ivFetch = (async () => {
      try {
        const r = await API.hitlInterventions(runId);
        this._interventions = (r && r.interventions) || [];
      } catch (e) {
        this._interventions = [];
      }
    })();
    await Promise.all([...fileFetches, ivFetch]);
    if (token !== this._selectToken) return;  // 已切换到其它门控
    this._renderDetail();
  },

  _renderDetail() {
    const el = document.getElementById('gate-detail');
    if (!el) return;
    const gate = this._selectedGate;
    if (!gate || gate.run_id !== this._selectedRunId) { this._renderDetailEmpty(); return; }
    el.innerHTML = `
      <div class="card" style="margin-bottom:0">
        <div id="gate-notice" style="display:none;margin-bottom:10px;font-size:13px"></div>
        <div id="gate-stale-hint" style="display:none;margin-bottom:10px;font-size:13px;color:var(--warning)">This gate is no longer waiting.</div>
        <h2 style="font-family:var(--font-mono);font-size:15px;word-break:break-all">${this._esc(gate.run_id)}</h2>
        <div style="font-size:13px;color:var(--text-secondary);margin-bottom:4px">
          <b>Stage ${gate.stage ?? '?'} — ${this._esc(gate.stage_name || '')}</b>
          · ${this._esc(gate.reason || '')} · ${this._esc(this._ago(gate.since))}
        </div>
        <h3 class="gate-h3">Context Summary</h3>
        <pre class="gate-pre">${this._esc(gate.context_summary || '—')}</pre>
        <h3 class="gate-h3">Output Files</h3>
        <div id="gate-files">${this._filesHtml()}</div>
        <h3 class="gate-h3">Intervention History</h3>
        <div id="gate-iv">${this._timelineHtml(this._interventions)}</div>
        <h3 class="gate-h3">Actions</h3>
        <div id="gate-actions">${this._actionsHtml(gate)}</div>
      </div>`;
  },

  /* ---------- Output Files ---------- */

  _filesHtml() {
    if (!this._files.length) {
      return '<p style="color:var(--text-muted);font-size:13px">No output files.</p>';
    }
    return this._files.map((f, i) => {
      let body = '';
      if (f.loading) {
        body = '<p style="color:var(--text-muted);font-size:12px;padding:6px 10px">Loading...</p>';
      } else if (f.error) {
        body = `<p style="color:var(--error);font-size:12px;padding:6px 10px">${this._esc(f.error)}</p>`;
      } else if (this._editMode) {
        body = `<textarea class="gate-textarea" data-file="${this._esc(f.name)}" rows="12">${this._esc(f.content)}</textarea>`;
      } else if (f.open) {
        body = `<pre class="gate-pre">${this._esc(f.content)}</pre>`;
      }
      return `
        <div class="gate-file">
          <div class="gate-file-head" onclick="GateConsole._toggleFile(${i})">
            <span style="font-family:var(--font-mono);font-size:12px;flex:1">${this._esc(f.name)}</span>
            ${f.truncated ? '<span class="gate-badge">truncated</span>' : ''}
            ${this._editMode ? '' : `<span style="color:var(--text-muted);font-size:12px">${f.open ? '▾' : '▸'}</span>`}
          </div>
          ${body}
        </div>`;
    }).join('');
  },

  _toggleFile(i) {
    if (this._editMode) return;  // 编辑模式下始终展开
    const f = this._files[i];
    if (!f || f.loading || f.error) return;
    f.open = !f.open;
    this._updateFiles();
  },

  _updateFiles() {
    const el = document.getElementById('gate-files');
    if (el) el.innerHTML = this._filesHtml();
  },

  /* ---------- Intervention History ---------- */

  _timelineHtml(items) {
    if (items === null) return '<p style="color:var(--text-muted);font-size:13px">Loading...</p>';
    if (!items || !items.length) {
      return '<p style="color:var(--text-muted);font-size:13px">No interventions recorded.</p>';
    }
    return '<div class="gate-tl">' + items.map(iv => {
      const msg = String(iv.guidance || iv.message || '');
      const short = msg.length > 200 ? msg.slice(0, 200) + '…' : msg;
      return `
        <div class="gate-tl-item">
          <div style="font-size:12px;color:var(--text-muted)">${this._esc(iv.timestamp || '')}</div>
          <div style="font-size:13px">
            <span class="status-badge running">${this._esc(iv.action || '?')}</span>
            ${iv.stage != null ? `<span style="margin-left:6px;color:var(--text-secondary)">Stage ${this._esc(iv.stage)}</span>` : ''}
          </div>
          ${short ? `<div style="font-size:12px;color:var(--text-secondary);margin-top:2px">${this._esc(short)}</div>` : ''}
        </div>`;
    }).join('') + '</div>';
  },

  /* ---------- Actions ---------- */

  _actionsHtml(gate) {
    const acts = (Array.isArray(gate.available_actions) && gate.available_actions.length)
      ? gate.available_actions : ['approve', 'reject'];
    const labels = {
      approve: 'Approve', reject: 'Reject', skip: 'Skip', abort: 'Abort',
      inject: 'Inject', edit: 'Edit', rollback: 'Rollback',
      collaborate: 'Collaborate', take_over: 'Take Over', resume: 'Resume',
    };
    const danger = ['reject', 'abort', 'rollback'];
    let html = '<div style="display:flex;gap:8px;flex-wrap:wrap">';
    for (const a of acts) {
      const cls = a === 'approve' ? 'gate-btn primary'
        : danger.includes(a) ? 'gate-btn danger' : 'gate-btn';
      html += `<button class="${cls}" onclick="GateConsole._act('${this._escAttr(a)}')">${labels[a] || this._esc(a)}</button>`;
    }
    html += '</div>';

    if (this._injectOpen) {
      html += `
        <div style="margin-top:10px">
          <textarea id="gate-inject-text" class="gate-textarea" rows="4" placeholder="Guidance to inject (optional)"></textarea>
          <div style="margin-top:6px;display:flex;gap:8px">
            <button class="gate-btn primary" onclick="GateConsole._submitInject()">Submit</button>
            <button class="gate-btn" onclick="GateConsole._act('inject')">Cancel</button>
          </div>
        </div>`;
    }
    if (this._rollbackOpen) {
      html += `
        <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input id="gate-rollback-stage" class="gate-input" type="number" min="1" max="23"
                 placeholder="Target stage" style="width:140px" />
          <button class="gate-btn danger" onclick="GateConsole._submitRollback()">Submit</button>
          <button class="gate-btn" onclick="GateConsole._act('rollback')">Cancel</button>
        </div>`;
    }
    if (this._editMode) {
      html += `
        <div style="margin-top:10px">
          <p style="font-size:12px;color:var(--text-muted);margin-bottom:6px">Edit output files, then submit.</p>
          <div style="display:flex;gap:8px">
            <button class="gate-btn primary" onclick="GateConsole._submitEdit()">Submit</button>
            <button class="gate-btn" onclick="GateConsole._act('edit')">Cancel</button>
          </div>
        </div>`;
    }
    return html;
  },

  _updateActions() {
    const el = document.getElementById('gate-actions');
    const gate = this._selectedGate;
    if (el && gate && gate.run_id === this._selectedRunId) el.innerHTML = this._actionsHtml(gate);
  },

  async _act(action) {
    if (action === 'inject') {
      this._injectOpen = !this._injectOpen;
      this._updateActions();
      return;
    }
    if (action === 'rollback') {
      this._rollbackOpen = !this._rollbackOpen;
      this._updateActions();
      return;
    }
    if (action === 'edit') {
      this._editMode = !this._editMode;
      if (this._editMode) this._files.forEach(f => { f.open = true; });
      this._updateFiles();
      this._updateActions();
      return;
    }
    // 直接发送类动作
    if (['reject', 'abort'].includes(action)) {
      if (!confirm(`${I18N.t('Are you sure?')} (${action})`)) return;
    }
    await this._respond({ action, message: '', guidance: '' });
  },

  async _submitInject() {
    const t = document.getElementById('gate-inject-text');
    const guidance = t ? t.value.trim() : '';
    const ok = await this._respond({ action: 'inject', guidance, message: guidance });
    if (ok) { this._injectOpen = false; this._updateActions(); }
  },

  async _submitEdit() {
    const edited_files = {};
    document.querySelectorAll('#gate-files textarea[data-file]').forEach(ta => {
      edited_files[ta.dataset.file] = ta.value;  // key 用原始文件名（不带 stage-NN 前缀）
    });
    const ok = await this._respond({ action: 'edit', edited_files, message: '' });
    if (ok) { this._editMode = false; this._updateFiles(); this._updateActions(); }
  },

  async _submitRollback() {
    const inp = document.getElementById('gate-rollback-stage');
    const n = parseInt(inp && inp.value, 10);
    if (!Number.isInteger(n) || n < 1) { alert(I18N.t('Invalid stage number.')); return; }
    if (!confirm(`${I18N.t('Are you sure?')} (rollback → stage ${n})`)) return;
    const ok = await this._respond({ action: 'rollback', rollback_to_stage: n, message: '' });
    if (ok) { this._rollbackOpen = false; this._updateActions(); }
  },

  /**
   * 并发保护：respond 前重新查该 run 是否仍在门控等待；
   * 已解决则提示并刷新列表，不发 respond。
   * 返回是否发送成功。
   */
  async _respond(body) {
    const runId = this._selectedRunId;
    if (!runId) return false;
    let w = null;
    try { w = await API.hitlRunWaiting(runId); } catch (e) { w = null; }
    if (!w || !w.waiting) {
      alert(I18N.t('Gate already resolved'));
      await this._refreshWaiting();
      return false;
    }
    try {
      await API.hitlRespond(runId, body);
      this._notice(I18N.t('Response sent.'), false);
      await this._refreshWaiting();
      return true;
    } catch (e) {
      alert(I18N.t('Respond failed') + ': ' + (e.message || e));
      return false;
    }
  },

  _notice(msg, isErr) {
    const el = document.getElementById('gate-notice');
    if (!el) return;
    el.textContent = msg;
    el.style.color = isErr ? 'var(--error)' : 'var(--success)';
    el.style.display = 'block';
    setTimeout(() => { if (el) el.style.display = 'none'; }, 4000);
  },

  /* ---------- 下方：最近 run 的干预历史（只读） ---------- */

  async _loadRecent() {
    const el = document.getElementById('gate-recent');
    if (!el) return;
    try {
      const r = await API.listRuns();
      this._recent = (r.runs || []).slice(0, 10);
    } catch (e) {
      this._recent = [];
    }
    const el2 = document.getElementById('gate-recent');
    if (!el2) return;
    if (!this._recent.length) {
      el2.innerHTML = '<p style="color:var(--text-muted)">No runs found.</p>';
      return;
    }
    el2.innerHTML = this._recent.map(r => `
      <div class="gate-run-row" onclick="GateConsole._viewHistory('${this._escAttr(r.run_id)}')">
        <span style="font-family:var(--font-mono);font-size:13px;color:var(--accent)">${this._esc(r.run_id)}</span>
        <span class="status-badge ${r.checkpoint?.status || 'idle'}">${this._esc(r.checkpoint?.status || 'unknown')}</span>
        <span style="color:var(--text-muted);font-size:12px">${this._esc(r.checkpoint?.stage_name || '')}</span>
      </div>`).join('');
  },

  async _viewHistory(runId) {
    this._historyRunId = runId;
    const el = document.getElementById('gate-history');
    if (!el) return;
    const head = `<h3 class="gate-h3" style="margin-top:14px"><span>Intervention History</span> — <span style="font-family:var(--font-mono)">${this._esc(runId)}</span></h3>`;
    el.innerHTML = head + '<p style="color:var(--text-muted);font-size:13px">Loading...</p>';
    let items = [];
    try {
      const r = await API.hitlInterventions(runId);
      items = (r && r.interventions) || [];
    } catch (e) { /* 空时间线 */ }
    if (this._historyRunId !== runId) return;
    const el2 = document.getElementById('gate-history');
    if (el2) el2.innerHTML = head + this._timelineHtml(items);
  },

  /* ---------- Paper Review：Overleaf 批注改稿 ---------- */

  async _loadPaper() {
    if (!document.getElementById('gate-paper-body')) return;
    try {
      const r = await API.listRuns();
      this._paperRuns = (r.runs || []).slice(0, 20);
    } catch (e) {
      this._paperRuns = [];
    }
    if (!this._paperRunId || !this._paperRuns.some(r => r.run_id === this._paperRunId)) {
      this._paperRunId = this._paperRuns.length ? this._paperRuns[0].run_id : null;
    }
    this._updatePaper();
    if (this._paperRunId) this._paperFetchVersions();
  },

  _updatePaper() {
    const el = document.getElementById('gate-paper-body');
    if (el) el.innerHTML = this._paperBodyHtml();
  },

  _paperBodyHtml() {
    if (!this._paperRuns.length) {
      return '<p style="color:var(--text-muted)">No runs found.</p>';
    }
    const busy = !!this._paperBusy;
    const opts = this._paperRuns.map(r =>
      `<option value="${this._esc(r.run_id)}"${r.run_id === this._paperRunId ? ' selected' : ''}>${this._esc(r.run_id)}</option>`
    ).join('');
    const langBtn = l =>
      `<button class="gate-btn gate-toggle${this._paperLang === l ? ' active' : ''}"${busy ? ' disabled' : ''} onclick="GateConsole._paperSetLang('${l}')">${l}</button>`;
    const btn = (kind, label, cls) => {
      const loading = this._paperBusy === kind;
      const text = loading && kind === 'revise'
        ? I18N.t('Revising… this may take a few minutes')
        : I18N.t(label) + (loading ? ' …' : '');
      return `<button class="${cls}"${busy ? ' disabled' : ''} onclick="GateConsole._paperAct('${kind}')">${this._esc(text)}</button>`;
    };
    const notice = this._paperNotice
      ? `<div class="gate-notice" style="color:${this._paperNotice.isErr ? 'var(--error)' : 'var(--success)'}">${this._esc(this._paperNotice.msg)}</div>`
      : '';
    let revisePanel = '';
    if (this._paperReviseOpen && !busy) {
      revisePanel = `
        <div style="margin-top:10px">
          <textarea id="gate-paper-instruction" class="gate-textarea" rows="3"
                    placeholder="Optional extra instruction, e.g. 把讨论部分压缩一半"></textarea>
          <div style="margin-top:6px;display:flex;gap:8px">
            <button class="gate-btn primary" onclick="GateConsole._paperSubmitRevise()">${I18N.t('Confirm Revise')}</button>
            <button class="gate-btn" onclick="GateConsole._paperToggleRevise()">${I18N.t('Cancel')}</button>
          </div>
        </div>`;
    }
    if (this._paperBusy === 'revise') {
      revisePanel = `<p style="font-size:13px;color:var(--warning);margin-top:10px">${this._esc(I18N.t('Revising… this may take a few minutes'))}</p>`;
    }
    if (this._paperBusy === 'translate') {
      revisePanel = `<p style="font-size:13px;color:var(--warning);margin-top:10px">${this._esc(I18N.t('Translating… this may take a few minutes'))}</p>`;
    }
    return `
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span style="font-size:13px;color:var(--text-secondary)">${I18N.t('Run:')}</span>
        <select class="gate-input" style="font-family:var(--font-mono);font-size:12px;max-width:320px"
                onchange="GateConsole._paperSelectRun(this.value)"${busy ? ' disabled' : ''}>${opts}</select>
        <span style="font-size:13px;color:var(--text-secondary);margin-left:8px">${I18N.t('Language')}</span>
        ${langBtn('zh')}${langBtn('en')}
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
        ${btn('pull', 'Pull from Overleaf', 'gate-btn')}
        ${btn('push', 'Push to Overleaf', 'gate-btn')}
        ${btn('annotate', 'View Annotations', 'gate-btn')}
        ${btn('revise', 'Revise by Annotations', 'gate-btn primary')}
        ${btn('translate', 'Translate to English', 'gate-btn')}
      </div>
      ${notice}
      ${this._paperReviseResultHtml()}
      ${revisePanel}
      <h3 class="gate-h3">${I18N.t('Annotations')}</h3>
      <div id="gate-paper-annotations">${this._paperAnnHtml()}</div>
      <h3 class="gate-h3">${I18N.t('Versions')}</h3>
      <div id="gate-paper-versions">${this._paperVersionsHtml()}</div>`;
  },

  _paperAnnHtml() {
    if (this._paperAnnErr) {
      return `<p style="color:var(--error);font-size:13px">${this._esc(this._paperAnnErr)}</p>`;
    }
    const a = this._paperAnnotations;
    if (a === null) return '<p style="color:var(--text-muted);font-size:13px">Loading...</p>';
    if (a === undefined) {
      return '<p style="color:var(--text-muted);font-size:13px">Click "View Annotations" or pull from Overleaf to load the annotation report.</p>';
    }
    if (!a.has_user_version) {
      return '<p style="color:var(--warning);font-size:13px">No pulled user version yet — pull from Overleaf first.</p>';
    }
    const comments = Array.isArray(a.comments) ? a.comments : [];
    const edits = Array.isArray(a.edits) ? a.edits : [];
    let html = `<h4 style="font-size:13px;margin:8px 0 6px">${I18N.t('Comments')} (${comments.length})</h4>`;
    if (!comments.length) {
      html += '<p style="color:var(--text-muted);font-size:13px">No comments.</p>';
    } else {
      html += comments.map(c => `
        <div class="gate-anno-item">
          <div style="font-size:13px">
            ${c.section ? `<span class="gate-badge" style="margin-right:6px">${this._esc(c.section)}</span>` : ''}
            ${this._esc(c.text || '')}
          </div>
          ${c.context ? `<div style="font-size:12px;color:var(--text-muted);margin-top:2px">${this._esc(this._trunc(c.context, 150))}</div>` : ''}
        </div>`).join('');
    }
    html += `<h4 style="font-size:13px;margin:12px 0 6px">${I18N.t('Edits')} (${edits.length})</h4>`;
    if (!edits.length) {
      html += '<p style="color:var(--text-muted);font-size:13px">No direct edits.</p>';
    } else {
      html += edits.map(e => `
        <div class="gate-anno-item">
          ${e.section ? `<span class="gate-badge">${this._esc(e.section)}</span>` : ''}
          <details style="margin-top:4px">
            <summary style="cursor:pointer;font-size:12px;color:var(--text-secondary)">${I18N.t('Before')} → ${I18N.t('After')}</summary>
            <pre class="gate-pre gate-edit-before">${this._esc(this._trunc(e.before, 300))}</pre>
            <pre class="gate-pre gate-edit-after">${this._esc(this._trunc(e.after, 300))}</pre>
          </details>
        </div>`).join('');
    }
    return html;
  },

  _paperVersionsHtml() {
    if (this._paperVerErr) {
      return `<p style="color:var(--error);font-size:13px">${this._esc(this._paperVerErr)}</p>`;
    }
    const v = this._paperVersions;
    if (v === null) return '<p style="color:var(--text-muted);font-size:13px">Loading...</p>';
    if (!v.length) return '<p style="color:var(--text-muted);font-size:13px">No versions yet.</p>';
    return v.map(item => `
      <div class="gate-version-row">
        <span style="font-family:var(--font-mono);font-size:12px;flex:1;word-break:break-all">${this._esc(item.name)}</span>
        <span style="color:var(--text-muted);font-size:12px">${this._esc(item.time)}</span>
      </div>`).join('');
  },

  _paperReviseResultHtml() {
    const r = this._paperReviseResult;
    if (!r) return '';
    if (r.error) {
      return `<div class="gate-notice" style="color:var(--error)">${this._esc(r.error)}</div>`;
    }
    const pushedCount = Array.isArray(r.pushed) ? r.pushed.length : (r.pushed ? 1 : 0);
    const warn = (Array.isArray(r.reverted_numbers) && r.reverted_numbers.length)
      ? `<div style="color:var(--warning);margin-top:4px">${this._esc(I18N.t(`${r.reverted_numbers.length} numbers reverted to verified values`))}</div>`
      : '';
    const numWarn = (arr, labelKey) => (Array.isArray(arr) && arr.length)
      ? `<div style="color:var(--warning);margin-top:4px">${arr.length} ${this._esc(I18N.t(labelKey))}: ${this._esc(this._trunc(arr.join(', '), 120))}</div>`
      : '';
    return `
      <div class="gate-notice" style="color:var(--success)">
        ${this._esc(I18N.t('Revise complete'))}: version ${this._esc(r.version != null ? r.version : '—')}
        · ${this._esc(r.applied_comments != null ? r.applied_comments : 0)} ${this._esc(I18N.t('applied comments'))}
        · ${this._esc(r.applied_edits != null ? r.applied_edits : 0)} ${this._esc(I18N.t('applied edits'))}
        ${r.folder ? ` · ${this._esc(I18N.t('pushed to'))} ${this._esc(r.folder)} (${pushedCount})` : ''}
        ${r.reason ? `<div style="margin-top:4px;color:var(--text-secondary)">${this._esc(r.reason)}</div>` : ''}
        ${warn}
        ${numWarn(r.added_numbers, 'numbers added by translation')}
        ${numWarn(r.dropped_numbers, 'numbers dropped by translation')}
      </div>`;
  },

  _paperResetRunState() {
    this._paperNotice = null;
    this._paperAnnotations = undefined;
    this._paperAnnErr = null;
    this._paperReviseOpen = false;
    this._paperReviseResult = null;
    this._paperVersions = null;
    this._paperVerErr = null;
  },

  _paperSelectRun(runId) {
    if (this._paperBusy || runId === this._paperRunId) return;
    ++this._paperToken;
    this._paperRunId = runId;
    this._paperResetRunState();
    this._updatePaper();
    this._paperFetchVersions();
  },

  _paperSetLang(l) {
    if (this._paperBusy || this._paperLang === l) return;
    ++this._paperToken;
    this._paperLang = l;
    this._paperResetRunState();
    this._updatePaper();
    this._paperFetchVersions();
  },

  async _paperAct(kind) {
    const runId = this._paperRunId;
    if (!runId || this._paperBusy) return;
    if (kind === 'revise') { this._paperToggleRevise(); return; }
    if (kind === 'annotate') { this._paperFetchAnnotations(); return; }
    if (kind === 'translate') { this._paperDoTranslate(); return; }
    const token = ++this._paperToken;
    this._paperBusy = kind;
    this._paperNotice = null;
    this._updatePaper();
    try {
      if (kind === 'pull') {
        const r = await API.paperPull(runId, this._paperLang);
        if (token !== this._paperToken) return;
        const ch = (r.changed_remote || []).length;
        const cp = (r.copied || []).length;
        this._paperNotice = {
          msg: `${I18N.t('Pulled from Overleaf')}: ${ch} ${I18N.t('changed')}, ${cp} ${I18N.t('copied')}`,
          isErr: false,
        };
      } else if (kind === 'push') {
        const r = await API.paperPush(runId, this._paperLang);
        if (token !== this._paperToken) return;
        const n = Array.isArray(r.pushed) ? r.pushed.length : (r.pushed || 0);
        this._paperNotice = {
          msg: `${I18N.t('Pushed to Overleaf')}: ${r.folder || ''} (${n})`,
          isErr: false,
        };
      }
    } catch (e) {
      if (token !== this._paperToken) return;
      this._paperNotice = { msg: this._errDetail(e), isErr: true };
    }
    this._paperBusy = null;
    this._updatePaper();
    if (kind === 'pull' && this._paperNotice && !this._paperNotice.isErr) {
      this._paperFetchAnnotations();  // Pull 成功后自动刷新批注报告
    }
    this._paperFetchVersions();
  },

  async _paperDoTranslate() {
    const runId = this._paperRunId;
    if (!runId || this._paperBusy) return;
    if (!confirm(I18N.t('Translate the finalized Chinese paper to English (IEEE format) and push to Overleaf en/?'))) return;
    const token = ++this._paperToken;
    this._paperBusy = 'translate';
    this._paperNotice = null;
    this._paperReviseResult = null;
    this._updatePaper();
    try {
      // LLM 翻译可能 2-5 分钟：不设短超时，期间按钮禁用并显示加载态
      const r = await API.paperTranslate(runId);
      if (token !== this._paperToken) return;
      this._paperReviseResult = r;
    } catch (e) {
      if (token !== this._paperToken) return;
      this._paperReviseResult = { error: this._errDetail(e) };
    }
    this._paperBusy = null;
    this._updatePaper();
    this._paperFetchVersions();
  },

  _paperToggleRevise() {
    if (this._paperBusy) return;
    this._paperReviseOpen = !this._paperReviseOpen;
    this._updatePaper();
  },

  async _paperSubmitRevise() {
    const runId = this._paperRunId;
    if (!runId || this._paperBusy) return;
    const ta = document.getElementById('gate-paper-instruction');
    const instruction = ta ? ta.value.trim() : '';
    const token = ++this._paperToken;
    this._paperBusy = 'revise';
    this._paperNotice = null;
    this._paperReviseResult = null;
    this._updatePaper();
    try {
      // LLM 调用可能 1-3 分钟：不设短超时，期间按钮禁用并显示加载态
      const r = await API.paperRevise(runId, this._paperLang, instruction);
      if (token !== this._paperToken) return;
      this._paperReviseResult = r;
      this._paperReviseOpen = false;
    } catch (e) {
      if (token !== this._paperToken) return;
      this._paperReviseResult = { error: this._errDetail(e) };
    }
    this._paperBusy = null;
    this._updatePaper();
    this._paperFetchVersions();
  },

  async _paperFetchAnnotations() {
    const runId = this._paperRunId;
    if (!runId) return;
    const token = this._paperToken;
    this._paperAnnotations = null;
    this._paperAnnErr = null;
    this._updatePaper();
    try {
      const r = await API.paperAnnotations(runId, this._paperLang);
      if (token !== this._paperToken) return;
      this._paperAnnotations = r;
    } catch (e) {
      if (token !== this._paperToken) return;
      this._paperAnnotations = undefined;
      this._paperAnnErr = this._errDetail(e);
    }
    this._updatePaper();
  },

  async _paperFetchVersions() {
    const runId = this._paperRunId;
    if (!runId) return;
    const token = this._paperToken;
    this._paperVersions = null;
    this._paperVerErr = null;
    this._updatePaper();
    try {
      const r = await API.paperVersions(runId, this._paperLang);
      if (token !== this._paperToken) return;
      this._paperVersions = this._paperNormVersions(r);
    } catch (e) {
      if (token !== this._paperToken) return;
      this._paperVersions = [];
      this._paperVerErr = this._errDetail(e);
    }
    this._updatePaper();
  },

  // 版本列表响应字段以后端为准，做容错归一化
  _paperNormVersions(raw) {
    const arr = Array.isArray(raw) ? raw
      : (raw && (raw.versions || raw.snapshots || raw.files)) || [];
    return arr.map(v => {
      if (typeof v === 'string') return { name: v, time: '' };
      if (!v || typeof v !== 'object') return { name: String(v), time: '' };
      const name = v.name || v.filename || v.file || v.path || JSON.stringify(v);
      let time = v.mtime || v.modified || v.time || v.timestamp || v.created || '';
      if (typeof time === 'number') {
        time = new Date(time > 1e12 ? time : time * 1000).toLocaleString();
      }
      return { name: String(name), time: String(time) };
    });
  },

  _trunc(s, n) {
    const str = String(s == null ? '' : s);
    return str.length > n ? str.slice(0, n) + '…' : str;
  },

  // API 错误信息里尽量提取 FastAPI 的 detail 字段
  _errDetail(e) {
    let m = String((e && e.message) || e);
    const i = m.indexOf('{');
    if (i >= 0) {
      try {
        const d = JSON.parse(m.slice(i)).detail;
        if (d) m = typeof d === 'string' ? d : JSON.stringify(d);
      } catch (_) { /* 保留原始信息 */ }
    }
    return m;
  },

  /* ---------- 工具 ---------- */

  _ago(ts) {
    const t = Date.parse(ts);
    if (isNaN(t)) return String(ts || '');
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return I18N.t('just now');
    if (s < 3600) return I18N.t(`${Math.floor(s / 60)}m ago`);
    if (s < 86400) return I18N.t(`${Math.floor(s / 3600)}h ago`);
    return I18N.t(`${Math.floor(s / 86400)}d ago`);
  },

  _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  },

  _escAttr(s) {
    return String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  },
};
