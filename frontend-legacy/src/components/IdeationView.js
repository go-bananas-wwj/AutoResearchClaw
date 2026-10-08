/**
 * IdeationView — 选题引擎：方向输入 → 运行 → 证据卡 → 一键启动完整 run。
 */
const IdeationView = {
  _runId: null,
  _pollTimer: null,
  _cards: null,

  render(container) {
    container.innerHTML = `
      <div class="card">
        <h2>Research Ideation</h2>
        <p style="color:var(--text-secondary);font-size:13px;margin-bottom:12px">
          Give a research direction — the engine scans the literature (OpenAlex/S2/arXiv),
          finds gaps, and returns evidence-backed candidate scientific questions.
        </p>
        <div style="display:flex;gap:10px;margin-bottom:12px">
          <input id="id-direction" class="wiz-input" style="flex:1;margin:0"
                 placeholder="e.g., 遥感嵌入 + 灾害预警" />
          <button id="id-start" class="wiz-btn primary">Find Topics</button>
          <button id="id-stop" class="wiz-btn" style="display:none">Stop</button>
        </div>
        <div id="id-status" style="font-size:13px;color:var(--text-muted);margin-bottom:10px"></div>
      </div>
      <div id="id-cards"></div>
      <style>
        .idea-card { padding:14px; border:1px solid var(--border); border-radius:var(--radius);
                     margin-bottom:12px; background:var(--bg-secondary); }
        .idea-card h3 { font-size:15px; margin-bottom:6px; }
        .idea-meta { font-size:12px; color:var(--text-muted); margin:4px 0; }
        .idea-gap { font-size:12px; color:var(--text-secondary); border-left:3px solid var(--accent);
                    padding-left:8px; margin:6px 0; }
        .rank-badge { display:inline-block; min-width:26px; text-align:center; border-radius:12px;
                      background:var(--accent); color:#fff; font-size:12px; padding:2px 8px; margin-right:8px; }
      </style>
    `;

    document.getElementById('id-start').addEventListener('click', () => this._start());
    document.getElementById('id-stop').addEventListener('click', () => this._stop());
    document.getElementById('id-direction').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') this._start();
    });
    this._resumeIfRunning();
  },

  async _resumeIfRunning() {
    try {
      const st = await API.ideationStatus();
      if (st.status === 'running' && st.run_id) {
        this._runId = st.run_id;
        this._setStatus(`Ideation running: ${st.run_id} — direction: ${st.direction}`);
        this._poll();
      } else if (st.status === 'completed' && st.run_id) {
        this._runId = st.run_id;
        this._setStatus(`Last run: ${st.run_id} (${st.cards} cards)`);
        this._loadReport();
      }
    } catch (e) { /* idle */ }
  },

  async _start() {
    const direction = document.getElementById('id-direction').value.trim();
    if (direction.length < 4) { this._setStatus('Please enter a direction (≥4 chars).', true); return; }
    try {
      const r = await API.ideationStart(direction);
      this._runId = r.run_id;
      document.getElementById('id-stop').style.display = 'inline-block';
      document.getElementById('id-cards').innerHTML = '';
      this._setStatus(`Running ${r.run_id} — literature scan + gap analysis (may take 10-30 min)...`);
      this._poll();
    } catch (e) { this._setStatus(String(e.message || e), true); }
  },

  async _stop() {
    try { await API.post('/ideation/stop'); this._setStatus('Stopped.'); this._clearPoll(); }
    catch (e) { this._setStatus(String(e.message || e), true); }
  },

  _poll() {
    this._clearPoll();
    this._pollTimer = setInterval(async () => {
      try {
        const st = await API.ideationStatus();
        if (st.status === 'completed') {
          this._clearPoll();
          document.getElementById('id-stop').style.display = 'none';
          this._setStatus(`Done: ${st.cards} candidate questions with evidence cards.`);
          this._loadReport();
        } else if (st.status === 'failed' || st.status === 'stopped') {
          this._clearPoll();
          document.getElementById('id-stop').style.display = 'none';
          this._setStatus(`${st.status}: ${st.error || ''}`, true);
        }
      } catch (e) { /* transient */ }
    }, 5000);
  },

  _clearPoll() { if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; } },

  async _loadReport() {
    try {
      const rep = await API.ideationReport(this._runId);
      const cards = rep.cards || [];
      this._cards = cards;
      document.getElementById('id-cards').innerHTML = `
        ${rep.novelty_score != null ? `<div class="card" style="font-size:13px">Novelty score: <b>${rep.novelty_score}</b> (${rep.novelty_assessment || ''})</div>` : ''}
        ${cards.map((c, i) => `
          <div class="idea-card">
            <h3><span class="rank-badge">#${c.rank || '?'}</span>${c.question}</h3>
            ${c.note_zh ? `<div class="idea-meta">${c.note_zh}</div>` : ''}
            ${(c.gap_evidence || []).map(g => `<div class="idea-gap">Gap: ${g}</div>`).join('')}
            ${c.novelty_hint ? `<div class="idea-meta">Novelty: ${c.novelty_hint}</div>` : ''}
            ${(c.suggested_datasets || []).length ? `<div class="idea-meta">Datasets: ${c.suggested_datasets.join(', ')}</div>` : ''}
            ${c.feasibility && c.feasibility.budget_note ? `<div class="idea-meta">Feasibility: ${c.feasibility.budget_note}</div>` : ''}
            ${c.risks ? `<div class="idea-meta">Risks: ${c.risks}</div>` : ''}
            <button class="wiz-btn primary" style="margin-top:8px" onclick="IdeationView._launch('${(c.question || '').replace(/'/g, "\\'")}')">
              Start full pipeline with this topic
            </button>
            <button class="wiz-btn" style="margin-top:8px;margin-left:8px" onclick="IdeationView._reproduce(${i})">
              复现这篇
            </button>
          </div>`).join('')}`;
    } catch (e) { this._setStatus(String(e.message || e), true); }
  },

  async _reproduce(idx) {
    const c = (this._cards || [])[idx];
    if (!c) return;
    // 从卡片证据/提示里找 arXiv id 或代码仓库链接；找不到就用问题文本当标题查询
    const haystack = [c.question, c.note_zh, c.novelty_hint, ...(c.gap_evidence || [])].filter(Boolean).join('\n');
    const m = haystack.match(/https?:\/\/[^\s<>"')\]]+|\b\d{4}\.\d{4,5}(?:v\d+)?\b/);
    const paper = m ? m[0] : (c.question || '');
    if (paper.length < 4) { this._setStatus('无法从该卡片确定论文标识。', true); return; }
    this._setStatus(`Starting reproduction for: ${paper} ...`);
    try {
      const r = await API.reproduceStart({ paper, paper_title: c.question || '', run_id: this._runId });
      this._setStatus(`Reproduction started: ${r.run_id} — output artifacts/${r.run_id}/reproduction/${r.paper_slug}/ (may take 1-2h; ask chat「复现结果」).`);
    } catch (e) { this._setStatus(String(e.message || e), true); }
  },

  async _launch(topic) {
    try {
      const r = await API.startPipeline({ topic, auto_approve: false });
      this._setStatus(`Full pipeline started: ${r.run_id} — watch progress on Dashboard.`);
    } catch (e) { this._setStatus(String(e.message || e), true); }
  },

  _setStatus(text, isErr) {
    const el = document.getElementById('id-status');
    if (el) { el.textContent = text; el.style.color = isErr ? 'var(--danger, #f85149)' : 'var(--text-muted)'; }
  },

  onEvent(event) {
    if (event.type === 'stage_complete' && this._runId) { /* progress visible via poll */ }
  }
};
