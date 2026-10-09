/**
 * ProjectHome — Codex 式项目主页（概念图 A）。
 * 头部（题目/状态/大进度条）+ 三 Tab：Overview（23 阶段时间线/指标/论文状态）、
 * Interact（对话流 + 门控卡片 + 论文批注改稿）、Manage（产物/Overleaf/干预历史/危险区）。
 * runId 为 null 时是「未绑定」新研究模式：只有对话，不挂 run。
 */
const ProjectHome = {
  runId: null,
  tab: 'overview',
  project: null,            // /api/projects 里的项目信息（topic/status/current_stage/...）
  _lastRunId: undefined,

  /* 跨 tab 持久的状态 */
  _chatCache: {},           // runId|'__new__' -> [{role, content}]
  _stagesMeta: null,        // /api/pipeline/stages（静态，缓存一次）
  _run: undefined,          // /api/runs/{id}

  /* 门控状态 */
  _gate: null,
  _gateKey: '',
  _gateFiles: [],           // [{name, open, loading, content, truncated, error}]
  _injectOpen: false,
  _editMode: false,
  _rollbackOpen: false,

  /* Paper Review 状态（迁移自 GateConsole，runId 固定） */
  _paperLang: 'zh',
  _paperBusy: null,         // 'pull'|'push'|'revise'|'translate'
  _paperNotice: null,       // {msg, isErr}
  _paperAnnotations: undefined,  // undefined=未加载 null=加载中 object=已加载
  _paperAnnErr: null,
  _paperReviseOpen: false,
  _paperReviseResult: null,
  _paperVersions: null,
  _paperVerErr: null,
  _paperToken: 0,

  /* Manage 状态 */
  _ivData: undefined,       // undefined=未加载 null=加载中 array=已加载
  _artifactCache: {},       // path -> {content, truncated} | {error}
  _filesList: null,         // /api/runs/{id}/files 的全量文件列表（null=未加载）
  _expandedDirs: {},        // 顶层目录 -> 展开状态（跨重渲染保留用户展开态）

  /* 定时器 / WS */
  _waitingTimer: null,
  _runTimer: null,
  _onChatResp: null,
  _onChatErr: null,

  // 历史版本目录（收进折叠区）：stage-XX_vN / stage-XX_repair_vN / archived_* / *_vN
  _histDirRe: /(?:^archived_|_v\d+$)/,

  /* ================= 生命周期 ================= */

  render(container, opts) {
    opts = opts || {};
    const runId = opts.runId || null;
    const runChanged = runId !== this._lastRunId;
    this._lastRunId = runId;
    this.runId = runId;
    this.tab = opts.tab || 'overview';
    this.project = opts.project || null;   // null 时头部回退 runId，待 _refreshRun 同步
    if (runChanged) this._resetRunState();

    if (!runId) {
      container.innerHTML = `
        <div id="proj-root">
          <div class="proj-head">
            <div class="proj-head-title">New Research</div>
            <div class="proj-head-sub">
              <span>New research — describe your topic or idea to start</span>
            </div>
          </div>
          <div id="proj-body"></div>
        </div>`;
      this._renderInteract();
      this._attachChat();
      return;
    }

    container.innerHTML = `
      <div id="proj-root">
        <div class="proj-head" id="proj-head">${this._headHtml()}</div>
        <div class="proj-tabs" id="proj-tabs">${this._tabsHtml()}</div>
        <div id="proj-body"></div>
      </div>`;
    this._renderBody();
    this._attachChat();
    this._startTimers();
    this._refreshWaiting();
    this._refreshRun();
  },

  destroy() {
    this._clearTimers();
    this._detachChat();
  },

  onEvent(event) {
    if (!event || !event.type) return;
    if (['stage_complete', 'stage_start', 'run_status_changed', 'pipeline_started',
         'pipeline_completed', 'stage_fail', 'paper_ready', 'run_discovered'].includes(event.type)) {
      this._refreshRun();
      this._refreshWaiting();
    }
  },

  _resetRunState() {
    this._clearTimers();
    this._run = undefined;
    this._gate = null;
    this._gateKey = '';
    this._gateFiles = [];
    this._injectOpen = false;
    this._editMode = false;
    this._rollbackOpen = false;
    this._paperLang = 'zh';
    this._paperResetRunState();
    this._ivData = undefined;
    this._artifactCache = {};
    this._filesList = null;
    this._expandedDirs = {};
    this._artsKey = '';
  },

  _startTimers() {
    this._clearTimers();
    if (!this.runId) return;
    this._waitingTimer = setInterval(() => this._refreshWaiting(), 5000);
    this._runTimer = setInterval(() => this._refreshRun(), 15000);
  },

  _clearTimers() {
    if (this._waitingTimer) { clearInterval(this._waitingTimer); this._waitingTimer = null; }
    if (this._runTimer) { clearInterval(this._runTimer); this._runTimer = null; }
  },

  /* ================= 头部 / Tab 栏 ================= */

  _headHtml() {
    const p = this.project || {};
    const ckpt = (this._run && this._run.checkpoint) || {};
    const total = (window.App && App.totalStages) || 23;
    const stage = p.current_stage || ckpt.last_completed_stage || 0;
    const pct = Math.max(0, Math.min(100, Math.round(100 * stage / total)));
    const title = p.topic || this.runId || '';
    let status = p.status || ckpt.status || 'unknown';
    if ((status === 'unknown' || status === 'no_checkpoint') && stage >= total) status = 'completed';
    const badge = this._gate
      ? `<span class="proj-badge waiting">● ${I18N.t('Waiting')}</span>`
      : status === 'completed'
        ? `<span class="proj-badge done">● ${I18N.t('Done')}</span>`
        : status === 'running'
          ? `<span class="proj-badge running">● ${I18N.t('Running')}</span>`
          : `<span class="proj-badge">● ${this._esc(I18N.t(status))}</span>`;
    const barCls = this._gate ? 'waiting' : (status === 'completed' ? 'done' : '');
    const updated = p.updated_at ? `${I18N.t('Updated')} ${this._esc(this._ago(p.updated_at * 1000))}` : '';
    return `
      <div class="proj-head-title">${this._esc(title)}</div>
      <div class="proj-head-sub">
        <span class="mono">${this._esc(this.runId || '')}</span>
        ${badge}
        <span>Stage ${stage}/${total}</span>
        <span>${updated}</span>
      </div>
      <div class="proj-pbar-lg ${barCls}"><div style="width:${pct}%"></div></div>`;
  },

  _updateHead() {
    const el = document.getElementById('proj-head');
    if (el && this.runId) el.innerHTML = this._headHtml();
  },

  _tabsHtml() {
    const tabs = [['overview', 'Overview'], ['interact', 'Interact'], ['manage', 'Manage']];
    return tabs.map(([k, label]) =>
      `<span class="proj-tab${this.tab === k ? ' active' : ''}" data-tab="${k}" onclick="ProjectHome.switchTab('${k}')">${label}</span>`
    ).join('');
  },

  switchTab(tab) {
    if (!['overview', 'interact', 'manage'].includes(tab) || tab === this.tab) return;
    this.tab = tab;
    if (window.App) App.currentTab = tab;
    const tabsEl = document.getElementById('proj-tabs');
    if (tabsEl) tabsEl.innerHTML = this._tabsHtml();
    this._renderBody();
  },

  _renderBody() {
    if (this.tab === 'interact') this._renderInteract();
    else if (this.tab === 'manage') this._renderManage();
    else this._renderOverview();
  },

  /* ================= Overview tab ================= */

  _renderOverview() {
    const body = document.getElementById('proj-body');
    if (!body) return;
    body.innerHTML = `
      <div id="proj-banner-slot">${this._bannerHtml()}</div>
      <div class="card"><h2>Stages</h2><div id="proj-stages"><p style="color:var(--text-muted)">Loading...</p></div></div>
      <div class="proj-ov-grid">
        <div class="card"><h2>Key Metrics</h2><div id="proj-metrics"><p style="color:var(--text-muted)">Loading...</p></div></div>
        <div class="card"><h2>Paper</h2><div id="proj-paper-card"><p style="color:var(--text-muted)">Loading...</p></div></div>
      </div>`;
    this._loadStages();
    this._loadMetrics();
    this._loadPaperCard();
  },

  _bannerHtml() {
    if (!this._gate) return '';
    const g = this._gate;
    return `
      <div class="proj-banner">
        <span>🚦 ${I18N.t('Gate: waiting for your decision')} — Stage ${g.stage ?? '?'} ${this._esc(g.stage_name || '')}${g.reason ? ` · ${this._esc(g.reason)}` : ''}</span>
        <button class="proj-btn primary" onclick="ProjectHome.switchTab('interact')">${this._esc(I18N.t('Go handle it'))}</button>
      </div>`;
  },

  _updateBanner() {
    const el = document.getElementById('proj-banner-slot');
    if (el) el.innerHTML = this._bannerHtml();
  },

  async _ensureRun() {
    if (this._run !== undefined || !this.runId) return this._run;
    try { this._run = await API.getRun(this.runId); } catch (e) { this._run = null; }
    return this._run;
  },

  async _refreshRun() {
    if (!this.runId || !document.getElementById('proj-root')) return;
    try {
      this._run = await API.getRun(this.runId);
    } catch (e) { return; }
    // 同步 App.projects 里的项目信息（轮询可能还没刷新到）
    if (window.App && App.projects) {
      const p = App.projects.find(x => x.id === this.runId);
      if (p) this.project = p;
    }
    this._updateHead();
    if (this.tab === 'overview') {
      this._loadStages();
      this._loadPaperCard();
    } else if (this.tab === 'manage') {
      // 文件列表顶层目录签名变化才重渲染（不收叠用户展开的目录/查看器）
      this._refreshArtifacts();
    }
  },

  async _loadStages() {
    const el = document.getElementById('proj-stages');
    if (!el || !this.runId) return;
    try {
      if (!this._stagesMeta) {
        const r = await API.pipelineStages();
        this._stagesMeta = (r && r.stages) || [];
      }
      await this._ensureRun();
    } catch (e) {
      el.innerHTML = `<p style="color:var(--error);font-size:13px">${this._esc(e.message || e)}</p>`;
      return;
    }
    if (!document.getElementById('proj-stages')) return;
    const dirs = new Set((this._run && this._run.stages_completed) || []);
    const ckpt = (this._run && this._run.checkpoint) || {};
    const lastDone = ckpt.last_completed_stage || 0;
    const status = (this.project && this.project.status) || ckpt.status || '';
    const curStage = this._gate ? this._gate.stage
      : (status === 'running' ? lastDone + 1 : 0);
    const nn = n => String(n).padStart(2, '0');
    el.innerHTML = this._stagesMeta.map(s => {
      const done = dirs.has(`stage-${nn(s.number)}`);
      const cur = !done && s.number === curStage;
      const cls = done ? 'done' : cur ? 'cur' : 'todo';
      return `<div class="proj-stage-line ${cls}"><span class="dot"></span><span class="num">${s.number}.</span> <span>${this._esc(s.label || s.name)}</span>${s.phase ? `<span class="phase">${this._esc(s.phase)}</span>` : ''}</div>`;
    }).join('') || '<p style="color:var(--text-muted)">Loading...</p>';
  },

  async _loadMetrics() {
    const el = document.getElementById('proj-metrics');
    if (!el || !this.runId) return;
    let data = null;
    try {
      const r = await API.hitlFile(this.runId, 'stage-14/experiment_summary.json');
      data = JSON.parse(r.content || 'null');
    } catch (e) { data = null; }
    if (!document.getElementById('proj-metrics')) return;
    if (!data || typeof data !== 'object') {
      el.innerHTML = `<p style="color:var(--text-muted);font-size:13px">${this._esc(I18N.t('No experiment summary yet.'))}</p>`;
      return;
    }
    // 常见嵌套：{metrics_summary: {...}} → 下钻一层展示条目
    let table = data;
    const nestKey = ['metrics_summary', 'metrics', 'results', 'summary']
      .find(k => data[k] && typeof data[k] === 'object' && !Array.isArray(data[k]));
    if (nestKey && Object.keys(data).length <= 2) table = data[nestKey];
    const rows = [];
    for (const [k, v] of Object.entries(table)) {
      if (rows.length >= 10) break;
      let val;
      if (v == null) continue;
      else if (typeof v === 'number') val = String(Math.round(v * 10000) / 10000);
      else if (typeof v === 'string') val = this._trunc(v, 90);
      else if (typeof v === 'boolean') val = String(v);
      else val = this._trunc(JSON.stringify(v), 90);
      rows.push(`<div class="proj-kv"><span class="k">${this._esc(k)}</span><span>${this._esc(val)}</span></div>`);
    }
    el.innerHTML = rows.length ? rows.join('')
      : `<p style="color:var(--text-muted);font-size:13px">${this._esc(I18N.t('No experiment summary yet.'))}</p>`;
  },

  async _loadPaperCard() {
    const el = document.getElementById('proj-paper-card');
    if (!el || !this.runId) return;
    const p = this.project || {};
    const mark = ok => ok
      ? `<span style="color:var(--success)">✓ ${I18N.t('available')}</span>`
      : `<span style="color:var(--text-muted)">— ${I18N.t('not yet')}</span>`;
    let vzh = '—', ven = '—';
    try { const r = await API.paperVersions(this.runId, 'zh'); vzh = String(this._paperNormVersions(r).length); } catch (e) {}
    try { const r = await API.paperVersions(this.runId, 'en'); ven = String(this._paperNormVersions(r).length); } catch (e) {}
    if (!document.getElementById('proj-paper-card')) return;
    el.innerHTML = `
      <div class="proj-kv"><span class="k">${I18N.t('Chinese')} (zh)</span><span>${mark(!!p.has_paper_zh)}</span></div>
      <div class="proj-kv"><span class="k">${I18N.t('English')} (en)</span><span>${mark(!!p.has_paper_en)}</span></div>
      <div class="proj-kv"><span class="k">${I18N.t('Versions')}</span><span>zh: ${vzh} · en: ${ven}</span></div>
      <p style="font-size:12px;color:var(--text-muted);margin-top:8px">${this._esc(I18N.t('Revise in the Interact tab.'))}</p>`;
  },

  /* ================= Interact tab：对话 ================= */

  _chatKey() { return this.runId || '__new__'; },

  _chatMessages() {
    const key = this._chatKey();
    if (!this._chatCache[key]) {
      this._chatCache[key] = [{
        role: 'assistant',
        content: this.runId
          ? 'This is the conversation bound to the current project. Confirm plans, inject ideas, or ask about progress here — I will answer in this project\'s context.'
          : 'Welcome! Two ways to work: (1) give me a research topic directly ("start research: XXX") and I\'ll launch the 23-stage pipeline for you; (2) just chat about your insight and I\'ll help sharpen it into a topic. You can also ask "what stage are we at?", "how are the results?", "stop", or "switch the primary model to xxx".',
      }];
    }
    return this._chatCache[key];
  },

  _renderInteract() {
    const body = document.getElementById('proj-body');
    if (!body) return;
    body.innerHTML = `
      <div id="proj-gate-slot">${this.runId ? this._gateHtml() : ''}</div>
      <div class="proj-chat" id="proj-chat-messages"></div>
      <div class="proj-chat-input">
        <input id="proj-chat-input" type="text" autocomplete="off"
               placeholder="${this.runId ? 'Chat with this project...' : 'New research — describe your topic or idea to start'}" />
        <button class="proj-send" id="proj-chat-send">Send</button>
      </div>
      ${this.runId ? `
      <div class="card" style="margin-top:20px">
        <h2>Paper Review</h2>
        <div id="proj-paper-body"><p style="color:var(--text-muted)">Loading...</p></div>
      </div>` : ''}`;

    const msgs = document.getElementById('proj-chat-messages');
    msgs.innerHTML = this._chatMessages().map(m => this._msgHtml(m.role, m.content)).join('');
    msgs.scrollTop = msgs.scrollHeight;

    const input = document.getElementById('proj-chat-input');
    const send = document.getElementById('proj-chat-send');
    send.addEventListener('click', () => this._sendChat());
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._sendChat(); }
    });

    if (this.runId) {
      this._renderGateSlot();
      this._updatePaper();
      this._paperFetchVersions();
    }
  },

  _attachChat() {
    this._detachChat();
    this._onChatResp = (data) => {
      this._addMessage('assistant', (data && data.message) || '');
    };
    this._onChatErr = (data) => {
      const err = (data && data.error) || 'unknown';
      this._addMessage('assistant', 'Error: ' + (typeof err === 'string' ? err : 'connection error'));
    };
    chatWS.on('chat_response', this._onChatResp);
    chatWS.on('error', this._onChatErr);
    if (!chatWS.ws || chatWS.ws.readyState === WebSocket.CLOSED) chatWS.connect();
  },

  _detachChat() {
    if (this._onChatResp) { chatWS.off('chat_response', this._onChatResp); this._onChatResp = null; }
    if (this._onChatErr) { chatWS.off('error', this._onChatErr); this._onChatErr = null; }
  },

  _sendChat() {
    const input = document.getElementById('proj-chat-input');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    this._addMessage('user', text);
    const payload = { text };
    if (this.runId) payload.run_id = this.runId;   // 绑定项目对话
    chatWS.send(JSON.stringify(payload));
    input.value = '';
    input.focus();
  },

  _addMessage(role, content) {
    if (!content) return;
    this._chatMessages().push({ role, content });
    const msgs = document.getElementById('proj-chat-messages');
    if (msgs) {
      msgs.insertAdjacentHTML('beforeend', this._msgHtml(role, content));
      msgs.scrollTop = msgs.scrollHeight;
    }
  },

  _msgHtml(role, content) {
    if (role === 'sys') {
      return `<div class="proj-msg sys">${this._esc(content)}</div>`;
    }
    const who = role === 'assistant' ? '<div class="who">ResearchClaw</div>' : '';
    return `<div class="proj-msg ${role}">${who}${this._mdLite(content)}</div>`;
  },

  // 先转义再做轻量 markdown（防服务端内容注入）
  _mdLite(content) {
    return this._esc(content)
      .replace(/```([\s\S]*?)```/g, (m, code) => `<pre class="proj-pre">${code}</pre>`)
      .replace(/`([^`\n]+)`/g, '<code class="proj-code">$1</code>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\n/g, '<br>');
  },

  /* ================= Interact tab：门控卡片 ================= */

  async _refreshWaiting() {
    if (!this.runId || !document.getElementById('proj-root')) return;
    let w;
    try { w = await API.hitlRunWaiting(this.runId); } catch (e) { return; }
    if (!document.getElementById('proj-root')) return;
    const gate = (w && w.waiting) ? w : null;
    const key = gate ? `${gate.stage}:${gate.since || ''}` : '';
    if (key === this._gateKey && !!gate === !!this._gate) { this._updateHead(); return; }
    this._gate = gate;
    this._gateKey = key;
    this._injectOpen = false;
    this._editMode = false;
    this._rollbackOpen = false;
    this._gateFiles = ((gate && gate.output_files) || []).map(name => ({
      name, open: false, loading: true, content: '', truncated: false, error: null,
    }));
    this._renderGateSlot();
    this._updateBanner();
    this._updateHead();
    if (gate) this._loadGateFiles();
  },

  async _loadGateFiles() {
    const gate = this._gate;
    if (!gate) return;
    const key = this._gateKey;
    const nn = String(gate.stage == null ? 0 : gate.stage).padStart(2, '0');
    await Promise.all(this._gateFiles.map(async f => {
      try {
        const r = await API.hitlFile(this.runId, `stage-${nn}/${f.name}`);
        f.content = r.content || '';
        f.truncated = !!r.truncated;
      } catch (e) {
        f.error = String(e.message || e);
      }
      f.loading = false;
    }));
    if (key !== this._gateKey) return;  // 门控已切换/解决
    this._renderGateSlot();
  },

  _renderGateSlot() {
    const el = document.getElementById('proj-gate-slot');
    if (el) el.innerHTML = this._gateHtml();
  },

  _gateHtml() {
    const g = this._gate;
    if (!g) return '';
    return `
      <div class="proj-gate">
        <div class="proj-gate-head">
          🚦 Gate: Stage ${g.stage ?? '?'} — ${this._esc(g.stage_name || '')}
          <span class="proj-badge waiting">${this._esc(I18N.t('Waiting for your decision'))}</span>
        </div>
        <div class="proj-gate-summary">${this._esc(g.reason || '')}${g.since ? ` · ${this._esc(this._ago(g.since))}` : ''}</div>
        ${g.context_summary ? `<pre class="proj-pre" style="max-height:180px">${this._esc(g.context_summary)}</pre>` : ''}
        <div id="proj-gate-notice" style="display:none;margin:8px 0;font-size:13px"></div>
        <div id="proj-gate-files">${this._gateFilesHtml()}</div>
        <div id="proj-gate-actions">${this._gateActionsHtml(g)}</div>
      </div>`;
  },

  _gateFilesHtml() {
    if (!this._gateFiles.length) {
      return `<p style="color:var(--text-muted);font-size:13px;margin:6px 0">${this._esc(I18N.t('No output files.'))}</p>`;
    }
    return this._gateFiles.map((f, i) => {
      let body = '';
      if (f.loading) {
        body = '<p style="color:var(--text-muted);font-size:12px;padding:6px 10px">Loading...</p>';
      } else if (f.error) {
        body = `<p style="color:var(--error);font-size:12px;padding:6px 10px">${this._esc(f.error)}</p>`;
      } else if (this._editMode) {
        body = `<textarea class="gate-textarea" data-file="${this._esc(f.name)}" rows="12">${this._esc(f.content)}</textarea>`;
      } else if (f.open) {
        body = `<pre class="proj-pre" style="border:none;border-top:1px solid var(--border);border-radius:0;max-height:480px">${this._esc(f.content)}</pre>`;
      }
      return `
        <div class="proj-gate-file">
          <div class="proj-gate-file-head" onclick="ProjectHome._toggleGateFile(${i})">
            <span style="font-family:var(--font-mono);font-size:12px;flex:1">📄 ${this._esc(f.name)}</span>
            ${f.truncated ? `<span class="gate-badge">${I18N.t('truncated')}</span>` : ''}
            ${this._editMode ? '' : `<span style="color:var(--text-muted);font-size:12px">${f.open ? '▾' : '▸'}</span>`}
          </div>
          ${body}
        </div>`;
    }).join('');
  },

  _toggleGateFile(i) {
    if (this._editMode) return;
    const f = this._gateFiles[i];
    if (!f || f.loading || f.error) return;
    f.open = !f.open;
    const el = document.getElementById('proj-gate-files');
    if (el) el.innerHTML = this._gateFilesHtml();
  },

  _gateActionsHtml(gate) {
    const acts = (Array.isArray(gate.available_actions) && gate.available_actions.length)
      ? gate.available_actions : ['approve', 'reject'];
    const labels = {
      approve: 'Approve', reject: 'Reject', skip: 'Skip', abort: 'Abort',
      inject: 'Inject', edit: 'Edit', rollback: 'Rollback',
      collaborate: 'Collaborate', take_over: 'Take Over', resume: 'Resume',
    };
    const danger = ['reject', 'abort', 'rollback'];
    let html = '<div class="proj-gate-actions">';
    for (const a of acts) {
      const cls = a === 'approve' ? 'proj-btn primary'
        : danger.includes(a) ? 'proj-btn danger' : 'proj-btn';
      html += `<button class="${cls}" onclick="ProjectHome._act('${this._escAttr(a)}')">${I18N.t(labels[a] || a)}</button>`;
    }
    html += '</div>';
    if (this._injectOpen) {
      html += `
        <div style="margin-top:10px">
          <textarea id="proj-inject-text" class="gate-textarea" rows="4" placeholder="${this._esc(I18N.t('Guidance to inject (optional)'))}"></textarea>
          <div style="margin-top:6px;display:flex;gap:8px">
            <button class="proj-btn primary" onclick="ProjectHome._submitInject()">${I18N.t('Submit')}</button>
            <button class="proj-btn" onclick="ProjectHome._act('inject')">${I18N.t('Cancel')}</button>
          </div>
        </div>`;
    }
    if (this._rollbackOpen) {
      html += `
        <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input id="proj-rollback-stage" class="gate-input" type="number" min="1" max="23"
                 placeholder="${this._esc(I18N.t('Target stage'))}" style="width:140px" />
          <button class="proj-btn danger" onclick="ProjectHome._submitRollback()">${I18N.t('Submit')}</button>
          <button class="proj-btn" onclick="ProjectHome._act('rollback')">${I18N.t('Cancel')}</button>
        </div>`;
    }
    if (this._editMode) {
      html += `
        <div style="margin-top:10px">
          <p style="font-size:12px;color:var(--text-muted);margin-bottom:6px">${this._esc(I18N.t('Edit output files, then submit.'))}</p>
          <div style="display:flex;gap:8px">
            <button class="proj-btn primary" onclick="ProjectHome._submitEdit()">${I18N.t('Submit')}</button>
            <button class="proj-btn" onclick="ProjectHome._act('edit')">${I18N.t('Cancel')}</button>
          </div>
        </div>`;
    }
    return html;
  },

  _updateGateActions() {
    const el = document.getElementById('proj-gate-actions');
    if (el && this._gate) el.innerHTML = this._gateActionsHtml(this._gate);
  },

  _updateGateFiles() {
    const el = document.getElementById('proj-gate-files');
    if (el) el.innerHTML = this._gateFilesHtml();
  },

  async _act(action) {
    if (action === 'inject') { this._injectOpen = !this._injectOpen; this._updateGateActions(); return; }
    if (action === 'rollback') { this._rollbackOpen = !this._rollbackOpen; this._updateGateActions(); return; }
    if (action === 'edit') {
      this._editMode = !this._editMode;
      if (this._editMode) this._gateFiles.forEach(f => { f.open = true; });
      this._updateGateFiles();
      this._updateGateActions();
      return;
    }
    if (['reject', 'abort'].includes(action)) {
      if (!confirm(`${I18N.t('Are you sure?')} (${action})`)) return;
    }
    await this._respond({ action, message: '', guidance: '' });
  },

  async _submitInject() {
    const t = document.getElementById('proj-inject-text');
    const guidance = t ? t.value.trim() : '';
    const ok = await this._respond({ action: 'inject', guidance, message: guidance });
    if (ok) { this._injectOpen = false; this._updateGateActions(); }
  },

  async _submitEdit() {
    const edited_files = {};
    document.querySelectorAll('#proj-gate-files textarea[data-file]').forEach(ta => {
      edited_files[ta.dataset.file] = ta.value;  // key 用原始文件名（不带 stage-NN 前缀）
    });
    const ok = await this._respond({ action: 'edit', edited_files, message: '' });
    if (ok) { this._editMode = false; this._updateGateFiles(); this._updateGateActions(); }
  },

  async _submitRollback() {
    const inp = document.getElementById('proj-rollback-stage');
    const n = parseInt(inp && inp.value, 10);
    if (!Number.isInteger(n) || n < 1) { alert(I18N.t('Invalid stage number.')); return; }
    if (!confirm(`${I18N.t('Are you sure?')} (rollback → stage ${n})`)) return;
    const ok = await this._respond({ action: 'rollback', rollback_to_stage: n, message: '' });
    if (ok) { this._rollbackOpen = false; this._updateGateActions(); }
  },

  // 并发保护：respond 前重新查该 run 是否仍在门控等待
  async _respond(body) {
    const runId = this.runId;
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
      this._gateNotice(I18N.t('Response sent.'), false);
      this._addMessage('sys', `Gate ${body.action}: ${I18N.t('Response sent.')}`);
      await this._refreshWaiting();
      return true;
    } catch (e) {
      alert(I18N.t('Respond failed') + ': ' + (e.message || e));
      return false;
    }
  },

  _gateNotice(msg, isErr) {
    const el = document.getElementById('proj-gate-notice');
    if (!el) return;
    el.textContent = msg;
    el.style.color = isErr ? 'var(--error)' : 'var(--success)';
    el.style.display = 'block';
    setTimeout(() => { if (el) el.style.display = 'none'; }, 4000);
  },

  /* ================= Interact tab：Paper Review（迁移自 GateConsole） ================= */

  _updatePaper() {
    const el = document.getElementById('proj-paper-body');
    if (el) el.innerHTML = this._paperBodyHtml();
  },

  _paperBodyHtml() {
    const busy = !!this._paperBusy;
    const langBtn = l =>
      `<button class="gate-btn gate-toggle${this._paperLang === l ? ' active' : ''}"${busy ? ' disabled' : ''} onclick="ProjectHome._paperSetLang('${l}')">${l}</button>`;
    const btn = (kind, label, cls) => {
      const loading = this._paperBusy === kind;
      const text = loading && kind === 'revise'
        ? I18N.t('Revising… this may take a few minutes')
        : loading && kind === 'translate'
          ? I18N.t('Translating… this may take a few minutes')
          : I18N.t(label) + (loading ? ' …' : '');
      return `<button class="${cls}"${busy ? ' disabled' : ''} onclick="ProjectHome._paperAct('${kind}')">${this._esc(text)}</button>`;
    };
    const notice = this._paperNotice
      ? `<div class="gate-notice" style="color:${this._paperNotice.isErr ? 'var(--error)' : 'var(--success)'}">${this._esc(this._paperNotice.msg)}</div>`
      : '';
    let revisePanel = '';
    if (this._paperReviseOpen && !busy) {
      revisePanel = `
        <div style="margin-top:10px">
          <textarea id="proj-paper-instruction" class="gate-textarea" rows="3"
                    placeholder="${this._esc(I18N.t('Optional extra instruction, e.g. 把讨论部分压缩一半'))}"></textarea>
          <div style="margin-top:6px;display:flex;gap:8px">
            <button class="gate-btn primary" onclick="ProjectHome._paperSubmitRevise()">${I18N.t('Confirm Revise')}</button>
            <button class="gate-btn" onclick="ProjectHome._paperToggleRevise()">${I18N.t('Cancel')}</button>
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
        <span style="font-size:13px;color:var(--text-secondary)">${I18N.t('Language')}</span>
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
      <div>${this._paperAnnHtml()}</div>
      <h3 class="gate-h3">${I18N.t('Versions')}</h3>
      <div>${this._paperVersionsHtml()}</div>`;
  },

  _paperAnnHtml() {
    if (this._paperAnnErr) {
      return `<p style="color:var(--error);font-size:13px">${this._esc(this._paperAnnErr)}</p>`;
    }
    const a = this._paperAnnotations;
    if (a === null) return '<p style="color:var(--text-muted);font-size:13px">Loading...</p>';
    if (a === undefined) {
      return `<p style="color:var(--text-muted);font-size:13px">${this._esc(I18N.t('Click "View Annotations" or pull from Overleaf to load the annotation report.'))}</p>`;
    }
    if (!a.has_user_version) {
      return `<p style="color:var(--warning);font-size:13px">${this._esc(I18N.t('No pulled user version yet — pull from Overleaf first.'))}</p>`;
    }
    const comments = Array.isArray(a.comments) ? a.comments : [];
    const edits = Array.isArray(a.edits) ? a.edits : [];
    let html = `<h4 style="font-size:13px;margin:8px 0 6px">${I18N.t('Comments')} (${comments.length})</h4>`;
    if (!comments.length) {
      html += `<p style="color:var(--text-muted);font-size:13px">${I18N.t('No comments.')}</p>`;
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
      html += `<p style="color:var(--text-muted);font-size:13px">${I18N.t('No direct edits.')}</p>`;
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
    if (!v || !v.length) return `<p style="color:var(--text-muted);font-size:13px">${I18N.t('No versions yet.')}</p>`;
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
    this._paperBusy = null;
    this._paperNotice = null;
    this._paperAnnotations = undefined;
    this._paperAnnErr = null;
    this._paperReviseOpen = false;
    this._paperReviseResult = null;
    this._paperVersions = null;
    this._paperVerErr = null;
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
    const runId = this.runId;
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
    const runId = this.runId;
    if (!runId || this._paperBusy) return;
    if (!confirm(I18N.t('Translate the finalized Chinese paper to English (IEEE format) and push to Overleaf en/?'))) return;
    const token = ++this._paperToken;
    this._paperBusy = 'translate';
    this._paperNotice = null;
    this._paperReviseResult = null;
    this._updatePaper();
    try {
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
    const runId = this.runId;
    if (!runId || this._paperBusy) return;
    const ta = document.getElementById('proj-paper-instruction');
    const instruction = ta ? ta.value.trim() : '';
    const token = ++this._paperToken;
    this._paperBusy = 'revise';
    this._paperNotice = null;
    this._paperReviseResult = null;
    this._updatePaper();
    try {
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
    const runId = this.runId;
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
    const runId = this.runId;
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

  /* ================= Manage tab ================= */

  _renderManage() {
    const body = document.getElementById('proj-body');
    if (!body) return;
    const rid = this._esc(this.runId || '');
    body.innerHTML = `
      <div class="card">
        <h2>Artifacts</h2>
        <p style="font-size:12px;color:var(--text-muted);margin-bottom:10px">${this._esc(I18N.t('Click a stage dir to browse its files.'))}</p>
        <div id="proj-artifacts"><p style="color:var(--text-muted)">Loading...</p></div>
      </div>
      <div class="card">
        <h2>Overleaf</h2>
        <div class="proj-kv"><span class="k">zh</span><span class="mono">runs/${rid}/zh/</span></div>
        <div class="proj-kv"><span class="k">en</span><span class="mono">runs/${rid}/en/</span></div>
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          <button class="gate-btn" id="proj-ov-push-zh" onclick="ProjectHome._overleafPush('zh')">${I18N.t('Push to Overleaf')} (zh)</button>
          <button class="gate-btn" id="proj-ov-push-en" onclick="ProjectHome._overleafPush('en')">${I18N.t('Push to Overleaf')} (en)</button>
        </div>
        <div id="proj-ov-notice" class="gate-notice" style="display:none"></div>
      </div>
      <div class="card">
        <h2>Intervention History</h2>
        <div id="proj-iv"><p style="color:var(--text-muted)">Loading...</p></div>
      </div>
      <div class="card proj-danger-card">
        <h2>Danger Zone</h2>
        <p style="font-size:12px;color:var(--text-muted);margin-bottom:10px">${this._esc(I18N.t('Abort this run? If it is waiting at a gate, an abort response will be sent; otherwise the pipeline will be stopped.'))}</p>
        <button class="proj-btn danger" onclick="ProjectHome.abortRun()">${I18N.t('Abort Run')}</button>
      </div>`;
    this._filesList = null;   // 每次进入 Manage 重取全量列表（tab 内 15s 轮询只按目录集合增量刷新）
    this._renderArtifacts();
    this._loadInterventions();
  },

  /* ---------- Manage：产物浏览（/api/runs/{id}/files 全量列表，按顶层目录分组） ---------- */

  // 顶层目录集合签名：仅目录增删触发重渲染，组内文件增删不触发（保住展开态/查看器）
  _filesSig(files) {
    const dirs = new Set();
    for (const f of files) {
      const i = f.path.indexOf('/');
      dirs.add(i < 0 ? '' : f.path.slice(0, i));
    }
    return JSON.stringify([...dirs].sort());
  },

  async _fetchFiles() {
    try {
      const r = await API.runFiles(this.runId);
      this._filesList = (r && r.files) || [];
    } catch (e) {
      this._filesList = null;
      throw e;
    }
    return this._filesList;
  },

  // 15s 轮询入口：重取列表，签名变了才重渲染（保留展开态）
  async _refreshArtifacts() {
    if (!this.runId || !document.getElementById('proj-artifacts')) return;
    let files;
    try { files = await this._fetchFiles(); } catch (e) { return; }
    const sig = this._filesSig(files);
    if (sig !== this._artsKey) {
      this._artsKey = sig;
      this._renderArtifacts();
    }
  },

  async _renderArtifacts() {
    const el = document.getElementById('proj-artifacts');
    if (!el || !this.runId) return;
    if (this._filesList === null) {
      el.innerHTML = '<p style="color:var(--text-muted)">Loading...</p>';
      try { await this._fetchFiles(); } catch (e) {
        const el3 = document.getElementById('proj-artifacts');
        if (el3) el3.innerHTML = `<p style="color:var(--error);font-size:13px">${this._esc(this._errDetail(e))}</p>`;
        return;
      }
    }
    const el2 = document.getElementById('proj-artifacts');
    if (!el2) return;
    const files = this._filesList || [];
    this._artsKey = this._filesSig(files);
    if (!files.length) {
      el2.innerHTML = `<p style="color:var(--text-muted);font-size:13px">${this._esc(I18N.t('No artifacts yet.'))}</p>`;
      return;
    }
    // 按顶层目录分组（'' = 根目录文件）
    const groups = {};
    for (const f of files) {
      const i = f.path.indexOf('/');
      const top = i < 0 ? '' : f.path.slice(0, i);
      (groups[top] = groups[top] || []).push(f);
    }
    const tops = Object.keys(groups);
    const stageRe = /^stage-(\d+)$/;
    const stages = tops.filter(t => stageRe.test(t))
      .sort((a, b) => parseInt(a.match(stageRe)[1], 10) - parseInt(b.match(stageRe)[1], 10));
    const hist = tops.filter(t => t && !stageRe.test(t) && this._histDirRe.test(t)).sort();
    const others = tops.filter(t => t && !stageRe.test(t) && !this._histDirRe.test(t)).sort();
    const hasRoot = '' in groups;

    let html = '';
    if (hasRoot) html += this._groupHtml('', groups['']);
    for (const t of stages) html += this._groupHtml(t, groups[t]);
    for (const t of others) html += this._groupHtml(t, groups[t]);
    if (hist.length) {
      html += `
        <details style="margin-top:10px">
          <summary style="cursor:pointer;font-size:12px;color:var(--text-muted)">${this._esc(I18N.t('Archived / versioned dirs'))} (${hist.length})</summary>
          <div style="margin-top:6px">${hist.map(t => this._groupHtml(t, groups[t], true)).join('')}</div>
        </details>`;
    }
    el2.innerHTML = html;
  },

  // 单个目录分组：头部可折叠，文件点击只读查看；>8 个文件的目录默认折叠
  _groupHtml(top, files, forceCollapse) {
    const key = top || '_root';
    if (!(key in this._expandedDirs)) {
      this._expandedDirs[key] = forceCollapse ? false : (top === '' ? true : files.length <= 8);
    }
    const open = this._expandedDirs[key];
    const label = top === '' ? I18N.t('Root Files') : top;
    return `
      <div class="proj-dir-row" onclick="ProjectHome._toggleGroup('${this._escAttr(key)}')">
        <span class="mono">${top === '' ? '📄' : '📁'} ${this._esc(label)}</span>
        <span style="color:var(--text-muted);font-size:11px">(${files.length})</span>
        <span id="proj-grp-arrow-${this._esc(key)}" style="color:var(--text-muted);font-size:12px;margin-left:auto">${open ? '▾' : '▸'}</span>
      </div>
      <div id="proj-grp-${this._esc(key)}" class="proj-dir-files"${open ? '' : ' style="display:none"'}>
        ${files.map(f => {
          const name = top === '' ? f.path : f.path.slice(top.length + 1);
          return `<div class="proj-file-row" onclick="ProjectHome._openFile('${this._escAttr(f.path)}')">
            <span class="mono" style="flex:1;word-break:break-all">📄 ${this._esc(name)}</span>
            <span style="color:var(--text-muted);font-size:11px;flex-shrink:0">${this._fmtSize(f.size)}</span>
          </div>`;
        }).join('')}
      </div>`;
  },

  _toggleGroup(key) {
    this._expandedDirs[key] = !this._expandedDirs[key];
    const open = this._expandedDirs[key];
    const body = document.getElementById('proj-grp-' + key);
    const arrow = document.getElementById('proj-grp-arrow-' + key);
    if (body) body.style.display = open ? '' : 'none';
    if (arrow) arrow.textContent = open ? '▾' : '▸';
  },

  _fmtSize(n) {
    const v = Number(n) || 0;
    if (v < 1024) return `${v} B`;
    if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
    return `${(v / 1024 / 1024).toFixed(1)} MB`;
  },

  async _openFile(path) {
    // 查看器：就近插入当前点击位置下方（统一用 Artifacts 卡片底部查看器）
    const host = document.getElementById('proj-artifacts');
    if (!host) return;
    let viewer = document.getElementById('proj-artifact-viewer');
    if (!viewer) {
      host.insertAdjacentHTML('beforeend', '<div id="proj-artifact-viewer"></div>');
      viewer = document.getElementById('proj-artifact-viewer');
    }
    viewer.innerHTML = `
      <div class="proj-viewer-head mono">${this._esc(path)} <span style="color:var(--text-muted)">Loading...</span></div>`;
    let cached = this._artifactCache[path];
    if (!cached) {
      try {
        const r = await API.hitlFile(this.runId, path);
        cached = this._artifactCache[path] = { content: r.content || '', truncated: !!r.truncated };
      } catch (e) {
        cached = { error: String(e.message || e) };
      }
    }
    const v = document.getElementById('proj-artifact-viewer');
    if (!v) return;
    if (cached.error) {
      v.innerHTML = `<div class="proj-viewer-head mono">${this._esc(path)}</div><p style="color:var(--error);font-size:13px">${this._esc(cached.error)}</p>`;
      return;
    }
    v.innerHTML = `
      <div class="proj-viewer-head mono">${this._esc(path)} ${cached.truncated ? `<span class="gate-badge">${I18N.t('truncated')}</span>` : ''}</div>
      <pre class="proj-pre" style="max-height:520px">${this._esc(cached.content)}</pre>`;
  },

  async _overleafPush(lang) {
    if (!this.runId) return;
    const btn = document.getElementById('proj-ov-push-' + lang);
    const notice = document.getElementById('proj-ov-notice');
    if (btn) btn.disabled = true;
    try {
      const r = await API.paperPush(this.runId, lang);
      const n = Array.isArray(r.pushed) ? r.pushed.length : (r.pushed || 0);
      if (notice) {
        notice.style.display = 'block';
        notice.style.color = 'var(--success)';
        notice.textContent = `${I18N.t('Pushed to Overleaf')}: ${r.folder || ''} (${n})`;
      }
    } catch (e) {
      if (notice) {
        notice.style.display = 'block';
        notice.style.color = 'var(--error)';
        notice.textContent = this._errDetail(e);
      }
    }
    if (btn) btn.disabled = false;
  },

  async _loadInterventions() {
    const el = document.getElementById('proj-iv');
    if (!el || !this.runId) return;
    if (this._ivData === undefined) {
      this._ivData = null;
      try {
        const r = await API.hitlInterventions(this.runId);
        this._ivData = (r && r.interventions) || [];
      } catch (e) {
        this._ivData = [];
      }
    }
    const el2 = document.getElementById('proj-iv');
    if (el2) el2.innerHTML = this._timelineHtml(this._ivData);
  },

  _timelineHtml(items) {
    if (items === null || items === undefined) return '<p style="color:var(--text-muted);font-size:13px">Loading...</p>';
    if (!items.length) {
      return `<p style="color:var(--text-muted);font-size:13px">${this._esc(I18N.t('No interventions recorded.'))}</p>`;
    }
    return '<div class="gate-tl">' + items.slice().reverse().map(iv => {
      const hi = iv.human_input || {};
      const action = iv.action || hi.action || iv.type || '?';
      const msg = String(iv.guidance || iv.message || hi.guidance || hi.message || '');
      const short = msg.length > 200 ? msg.slice(0, 200) + '…' : msg;
      return `
        <div class="gate-tl-item">
          <div style="font-size:12px;color:var(--text-muted)">${this._esc(iv.timestamp || '')}</div>
          <div style="font-size:13px">
            <span class="status-badge running">${this._esc(action)}</span>
            ${iv.stage != null ? `<span style="margin-left:6px;color:var(--text-secondary)">Stage ${this._esc(iv.stage)}${iv.stage_name ? ` ${this._esc(iv.stage_name)}` : ''}</span>` : ''}
          </div>
          ${short ? `<div style="font-size:12px;color:var(--text-secondary);margin-top:2px">${this._esc(short)}</div>` : ''}
        </div>`;
    }).join('') + '</div>';
  },

  async abortRun() {
    if (!this.runId) return;
    if (!confirm(I18N.t('Abort this run? If it is waiting at a gate, an abort response will be sent; otherwise the pipeline will be stopped.'))) return;
    try {
      let w = null;
      try { w = await API.hitlRunWaiting(this.runId); } catch (e) { w = null; }
      if (w && w.waiting) {
        await API.hitlRespond(this.runId, { action: 'abort', message: '', guidance: '' });
      } else {
        await API.stopPipeline();
      }
      alert(I18N.t('Aborted.'));
      this._refreshWaiting();
      this._refreshRun();
    } catch (e) {
      alert(I18N.t('Abort failed') + ': ' + this._errDetail(e));
    }
  },

  /* ================= 工具 ================= */

  _ago(ts) {
    const t = typeof ts === 'number' ? ts : Date.parse(ts);
    if (isNaN(t)) return String(ts || '');
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return I18N.t('just now');
    if (s < 3600) return I18N.t(`${Math.floor(s / 60)}m ago`);
    if (s < 86400) return I18N.t(`${Math.floor(s / 3600)}h ago`);
    return I18N.t(`${Math.floor(s / 86400)}d ago`);
  },

  _trunc(s, n) {
    const str = String(s == null ? '' : s);
    return str.length > n ? str.slice(0, n) + '…' : str;
  },

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

  _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  },

  _escAttr(s) {
    return String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  },
};
