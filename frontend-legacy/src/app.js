/**
 * ResearchClaw SPA — 项目中心壳（概念图 A）。
 * 状态：{currentProject, currentTab, view('home'|'settings'), newMode(未绑定新研究)}。
 * 左侧项目列表 10s 轮询 /api/projects；主区渲染 ProjectHome / SettingsView / 欢迎页。
 */
(function () {
  'use strict';

  const App = {
    projects: [],
    totalStages: 23,
    currentProject: null,      // runId | null
    currentTab: 'overview',    // 'overview' | 'interact' | 'manage'
    view: 'home',              // 'home' | 'settings'
    newMode: false,            // 「+ New Research」未绑定对话
    _pollTimer: null,
    _active: null,             // 当前主区组件（destroy 钩子）
    _health: null,

    /* ---------- 项目列表 ---------- */

    async _loadProjects() {
      let r;
      try {
        r = await API.listProjects();
      } catch (e) {
        console.warn('projects poll failed:', e);
        return;
      }
      this.projects = (r && r.projects) || [];
      this.totalStages = (r && r.total_stages) || 23;
      this._renderSidebar();
      // 当前项目被删/外部变化时同步头部
      if (this.currentProject && this._active === ProjectHome) {
        const p = this.projects.find(x => x.id === this.currentProject);
        if (p) ProjectHome.project = p;
      }
      // 欢迎页可见时随数据刷新
      if (this.view === 'home' && !this.currentProject && !this.newMode) {
        this._renderWelcome();
      }
    },

    _renderSidebar() {
      const el = document.getElementById('sidebar-projects');
      if (!el) return;
      if (!this.projects.length) {
        el.innerHTML = `<p class="proj-side-empty">No projects yet.</p>`;
        return;
      }
      el.innerHTML = this.projects.map(p => {
        const stage = p.current_stage || 0;
        const st = this._statusOf(p);
        const pct = Math.max(0, Math.min(100, Math.round(100 * stage / this.totalStages)));
        const badge = p.waiting
          ? `<span class="proj-badge waiting">● ${I18N.t('Waiting')}</span>`
          : st === 'completed'
            ? `<span class="proj-badge done">● ${I18N.t('Done')}</span>`
            : st === 'running'
              ? `<span class="proj-badge running">● ${I18N.t('Running')}</span>`
              : `<span class="proj-badge">● ${this._esc(I18N.t(st))}</span>`;
        const barCls = p.waiting ? 'waiting' : (st === 'completed' ? 'done' : '');
        const papers = [p.has_paper_zh && 'zh', p.has_paper_en && 'en'].filter(Boolean).join('+');
        const meta = st === 'completed' && papers
          ? `${stage}/${this.totalStages} · ${papers}`
          : `Stage ${stage}/${this.totalStages}`;
        return `
          <div class="proj-card${p.id === this.currentProject && this.view === 'home' ? ' active' : ''}"
               onclick="App.openProject('${this._escAttr(p.id)}')">
            <div class="proj-card-title">${this._esc(p.topic || p.id)}</div>
            <div class="proj-card-row">${badge}<span>${this._esc(meta)}</span></div>
            <div class="proj-pbar ${barCls}"><div style="width:${pct}%"></div></div>
          </div>`;
      }).join('');
    },

    /* ---------- 导航 ---------- */

    openProject(runId) {
      this.view = 'home';
      this.newMode = false;
      this.currentProject = runId;   // currentTab 保留（跨项目记住用户所在 tab）
      this._renderMain();
      this._renderSidebar();
    },

    startNewResearch() {
      this.view = 'home';
      this.newMode = true;
      this.currentProject = null;
      this.currentTab = 'interact';
      this._renderMain();
      this._renderSidebar();
    },

    navigateTo(view) {
      if (view !== 'settings') return;
      this.view = 'settings';
      this._renderMain();
      this._renderSidebar();
    },

    goHome() {
      this.view = 'home';
      this._renderMain();
      this._renderSidebar();
    },

    showAbout() {
      const v = (this._health && this._health.version) || '?';
      alert(`ResearchClaw v${v}\nAutonomous Research Pipeline — 23-stage co-pilot.`);
    },

    /* ---------- 主区渲染 ---------- */

    _renderMain() {
      const main = document.getElementById('main-content');
      if (!main) return;
      if (this._active && this._active.destroy) this._active.destroy();
      this._active = null;

      if (this.view === 'settings') {
        main.innerHTML = `
          <div style="margin-bottom:12px">
            <button class="proj-btn" onclick="App.goHome()">← Back</button>
          </div>
          <div id="settings-host"></div>`;
        SettingsView.render(document.getElementById('settings-host'));
        this._active = SettingsView;
        return;
      }

      if (this.newMode) {
        ProjectHome.render(main, { runId: null, tab: 'interact', project: null });
        this._active = ProjectHome;
        return;
      }

      if (this.currentProject) {
        const p = this.projects.find(x => x.id === this.currentProject) || null;
        ProjectHome.render(main, { runId: this.currentProject, tab: this.currentTab, project: p });
        this._active = ProjectHome;
        return;
      }

      this._renderWelcome();
    },

    _renderWelcome() {
      const main = document.getElementById('main-content');
      if (!main) return;
      const cards = this.projects.map(p => {
        const stage = p.current_stage || 0;
        const st = this._statusOf(p);
        const pct = Math.max(0, Math.min(100, Math.round(100 * stage / this.totalStages)));
        const badge = p.waiting
          ? `<span class="proj-badge waiting">● ${I18N.t('Waiting')}</span>`
          : st === 'completed'
            ? `<span class="proj-badge done">● ${I18N.t('Done')}</span>`
            : st === 'running'
              ? `<span class="proj-badge running">● ${I18N.t('Running')}</span>`
              : `<span class="proj-badge">● ${this._esc(I18N.t(st))}</span>`;
        const barCls = p.waiting ? 'waiting' : (st === 'completed' ? 'done' : '');
        return `
          <div class="proj-big-card" onclick="App.openProject('${this._escAttr(p.id)}')">
            <div class="proj-big-card-title">${this._esc(p.topic || p.id)}</div>
            <div class="proj-card-row">${badge}<span>Stage ${stage}/${this.totalStages}</span></div>
            <div class="proj-pbar ${barCls}"><div style="width:${pct}%"></div></div>
          </div>`;
      }).join('');
      main.innerHTML = `
        <div class="proj-welcome">
          <h2>Your Projects</h2>
          ${this.projects.length ? '' : `<p class="proj-welcome-sub">No projects yet.</p>`}
          <div class="proj-grid">
            ${cards}
            <div class="proj-big-card new" onclick="App.startNewResearch()">
              <div class="plus">＋</div>
              <div>New Research</div>
              <div class="hint">Start your first research project from here.</div>
            </div>
          </div>
        </div>`;
    },

    /* ---------- WS 事件 ---------- */

    _onWSEvent(data) {
      if (!data || !data.type) return;
      if (['run_discovered', 'stage_complete', 'stage_start', 'run_status_changed',
           'pipeline_started', 'pipeline_completed', 'stage_fail'].includes(data.type)) {
        this._loadProjects();
      }
      // 未绑定新研究对话孵化出了 run → 自动绑定到该项目
      if (this.newMode && data.type === 'run_discovered' && data.data && data.data.run_id) {
        this.openProject(data.data.run_id);
        return;
      }
      if (this._active && this._active.onEvent) this._active.onEvent(data);

      if (Notification.permission === 'granted') {
        if (data.type === 'pipeline_completed') {
          new Notification('ResearchClaw', { body: I18N.t('Pipeline completed!') });
        } else if (data.type === 'stage_fail') {
          new Notification('ResearchClaw', { body: I18N.t(`Stage failed: ${data.data?.current_stage_name || ''}`) });
        }
      }
    },

    /* ---------- 工具 ---------- */

    // checkpoint 可能缺 status 字段（老 run）：阶段已满则按已完成展示
    _statusOf(p) {
      const s = (p && p.status) || 'unknown';
      if ((s === 'unknown' || s === 'no_checkpoint') && p && (p.current_stage || 0) >= this.totalStages) {
        return 'completed';
      }
      return s;
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

  window.App = App;

  // 语言切换时重渲染（i18n.js 派发事件）
  window.addEventListener('rc-lang', () => {
    App._renderSidebar();
    App._renderMain();
  });

  document.addEventListener('DOMContentLoaded', async () => {
    document.getElementById('btn-new-research').addEventListener('click', () => App.startNewResearch());
    document.getElementById('nav-settings').addEventListener('click', (e) => {
      e.preventDefault();
      if (App.view === 'settings') App.goHome(); else App.navigateTo('settings');
    });
    document.getElementById('nav-about').addEventListener('click', (e) => {
      e.preventDefault();
      App.showAbout();
    });

    eventsWS.on('open', () => {
      const badge = document.getElementById('connection-badge');
      if (badge) { badge.textContent = 'Connected'; badge.className = 'status-badge running'; }
    });
    eventsWS.on('close', () => {
      const badge = document.getElementById('connection-badge');
      if (badge) { badge.textContent = 'Disconnected'; badge.className = 'status-badge failed'; }
    });
    eventsWS.on('message', (data) => App._onWSEvent(data));
    eventsWS.connect();

    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission();
    }

    // 初始渲染 + 项目列表轮询（10s）
    App._renderMain();
    App._loadProjects();
    App._pollTimer = setInterval(() => App._loadProjects(), 10000);

    try {
      App._health = await API.health();
      const statusEl = document.getElementById('server-status');
      if (statusEl) statusEl.textContent = `v${App._health.version}`;
    } catch (e) {
      console.warn('Health check failed:', e);
    }
  });
})();
