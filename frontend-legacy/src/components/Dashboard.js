/**
 * Dashboard component — overview stats and pipeline progress.
 */
const Dashboard = {
  _chart: null,

  async render(container) {
    container.innerHTML = `
      <div id="gate-banner" style="display:none"></div>
      <div class="stats-grid" id="stats-grid"></div>
      <div class="card">
        <h2>Pipeline Progress</h2>
        <div class="progress-bar"><div class="fill" id="progress-fill" style="width:0%"></div></div>
        <div id="progress-text" style="font-size:13px;color:var(--text-secondary);margin-bottom:12px"></div>
        <div class="pipeline-stages" id="pipeline-stages"></div>
      </div>
      <div class="card">
        <h2>Recent Runs</h2>
        <div id="runs-list" style="font-size:14px"></div>
      </div>
    `;
    await this.refresh();
  },

  async refresh() {
    try {
      const [status, stages, runs] = await Promise.all([
        API.pipelineStatus(),
        API.pipelineStages(),
        API.listRuns(),
      ]);
      this._renderStats(status);
      this._renderStages(stages.stages, status);
      this._renderRuns(runs.runs);
      this._refreshGate(status);
    } catch (e) {
      console.warn('Dashboard refresh failed:', e);
    }
  },

  async _refreshGate(status) {
    const banner = document.getElementById('gate-banner');
    if (!banner) return;
    const runId = status.run_id;
    if (!runId || status.status !== 'running') { banner.style.display = 'none'; return; }
    let w;
    try { w = await API.get(`/runs/${runId}/hitl/waiting`); } catch (e) { banner.style.display = 'none'; return; }
    if (!w.waiting) { banner.style.display = 'none'; return; }
    banner.style.display = 'block';
    banner.innerHTML = `
      <div class="card" style="border:2px solid var(--warning,#d29922);background:rgba(210,153,34,0.08)">
        <h2 style="color:var(--warning,#d29922)">Gate: waiting for your decision</h2>
        <div style="font-size:14px;margin:8px 0">
          <b>Stage ${w.stage ?? '?'} (${w.stage_name || ''})</b> — ${w.reason || ''}
        </div>
        ${w.context_summary ? `<div style="font-size:13px;color:var(--text-secondary);margin-bottom:8px">${String(w.context_summary).slice(0,400)}</div>` : ''}
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
          <button class="wiz-btn primary" onclick="Dashboard._gateRespond('${runId}','approve')">Approve</button>
          <button class="wiz-btn" style="border-color:var(--danger,#f85149);color:var(--danger,#f85149)" onclick="Dashboard._gateRespond('${runId}','reject')">Reject</button>
          <input id="gate-guidance" class="wiz-input" style="flex:1;min-width:220px;margin:0" placeholder="Guidance to inject (optional)" />
          <button class="wiz-btn" onclick="Dashboard._gateRespond('${runId}','inject')">Inject guidance</button>
        </div>
      </div>`;
  },

  async _gateRespond(runId, action) {
    const g = document.getElementById('gate-guidance');
    const guidance = g ? g.value.trim() : '';
    try {
      await API.post(`/runs/${runId}/hitl/respond`, { action, guidance, message: guidance });
      this.refresh();
    } catch (e) {
      alert('Gate respond failed: ' + (e.message || e));
    }
  },

  _renderStats(status) {
    const grid = document.getElementById('stats-grid');
    if (!grid) return;
    const stage = status.current_stage || 0;
    const s = status.status || 'idle';
    grid.innerHTML = `
      <div class="stat-card">
        <div class="label">Status</div>
        <div class="value ${s === 'running' ? 'accent' : s === 'completed' ? 'success' : ''}">${s}</div>
      </div>
      <div class="stat-card">
        <div class="label">Current Stage</div>
        <div class="value accent">${stage}/23</div>
      </div>
      <div class="stat-card">
        <div class="label">Run ID</div>
        <div class="value" style="font-size:14px">${status.run_id || '—'}</div>
      </div>
      <div class="stat-card">
        <div class="label">Topic</div>
        <div class="value" style="font-size:14px">${status.topic || '—'}</div>
      </div>
    `;
  },

  _renderStages(stages, status) {
    const el = document.getElementById('pipeline-stages');
    const fill = document.getElementById('progress-fill');
    const text = document.getElementById('progress-text');
    if (!el) return;

    const current = status.current_stage || 0;
    const pct = Math.round((current / 23) * 100);
    if (fill) fill.style.width = `${pct}%`;
    if (text) text.textContent = `${current}/23 stages (${pct}%)`;

    el.innerHTML = stages.map(s => {
      let cls = '';
      if (s.number < current) cls = 'done';
      else if (s.number === current && status.status === 'running') cls = 'running';
      return `<div class="stage-cell ${cls}">
        <div class="stage-num">${s.number}</div>
        <div class="stage-name">${s.name.replace(/_/g, ' ')}</div>
      </div>`;
    }).join('');
  },

  _renderRuns(runs) {
    const el = document.getElementById('runs-list');
    if (!el) return;
    if (!runs || !runs.length) {
      el.innerHTML = '<p style="color:var(--text-muted)">No runs found.</p>';
      return;
    }
    el.innerHTML = runs.slice(0, 10).map(r => `
      <div style="padding:8px 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px">
        <span style="font-family:var(--font-mono);font-size:13px;color:var(--accent)">${r.run_id}</span>
        <span class="status-badge ${r.checkpoint?.status || 'idle'}">${r.checkpoint?.status || 'unknown'}</span>
        <span style="color:var(--text-muted);font-size:12px">${r.checkpoint?.stage_name || ''}</span>
      </div>
    `).join('');
  },

  onEvent(event) {
    if (['stage_complete', 'stage_start', 'pipeline_started', 'pipeline_completed',
         'run_discovered', 'run_status_changed'].includes(event.type)) {
      this.refresh();
    }
  }
};
