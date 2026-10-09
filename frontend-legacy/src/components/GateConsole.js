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
