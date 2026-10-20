/**
 * Step 1 — 创新点审核（IdeaSpark 模式库 + DeepSeek）。
 * 用户输入自然语言方向 -> 生成 3-5 张可审核卡片 -> 选一张 -> 才创建 run 并启动实验。
 */
const IdeationView = {
  host: null,
  direction: '',
  cards: [],
  selected: -1,
  busy: false,

  render(host) {
    this.host = host;
    this.cards = [];
    this.selected = -1;
    this.busy = false;
    this._paint();
  },

  destroy() {},

  _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  },

  _paint() {
    if (!this.host) return;
    this.host.innerHTML =
      '<div style="max-width:1180px">' +
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">' +
          '<span style="background:#5b7cfa;color:#fff;border-radius:999px;padding:2px 10px;font-size:12px">Step 1</span>' +
          '<h2 style="margin:0">创新点审核</h2>' +
        '</div>' +
        '<p style="color:var(--text-muted);margin:6px 0 16px">' +
          '用一句话描述方向 &#8594; DeepSeek 按 IdeaSpark 的 15 种构思模式生成卡片 &#8594; 你选一张 &#8594; 才开始跑实验。' +
          '<b>未确认前不会产生任何 run。</b>' +
        '</p>' +
        '<div class="card" style="padding:16px">' +
          '<textarea id="idea-direction" rows="3" placeholder="例如：我想做跨传感器遥感变化检测，但不确定还有什么可做的"' +
            ' style="width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid var(--border);' +
            'background:var(--bg-tertiary);color:var(--text-primary);font-size:14px;resize:vertical"></textarea>' +
          '<div style="display:flex;align-items:center;gap:12px;margin-top:10px">' +
            '<button class="proj-btn" id="idea-generate">生成创新点卡片</button>' +
            '<a href="#" id="idea-chat-link" style="font-size:12px;color:var(--text-muted)">或者：跳过审核，直接对话</a>' +
            '<span id="idea-status" style="font-size:13px;color:var(--text-muted)"></span>' +
          '</div>' +
        '</div>' +
        '<div id="idea-cards" style="margin-top:14px;display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px"></div>' +
        '<div id="idea-actions" style="margin-top:16px;display:none;align-items:center;gap:12px">' +
          '<button class="proj-btn" id="idea-confirm" disabled>确认选中卡片，开始实验</button>' +
          '<span style="font-size:12px;color:var(--text-muted)">确认后会物化任务书并跑到实验完成（Step 1 的范围），结果出来再进入 Step 2 审核</span>' +
        '</div>' +
      '</div>';

    var self = this;
    var gen = document.getElementById('idea-generate');
    if (gen) gen.addEventListener('click', function () { self.generate(); });
    var confirm = document.getElementById('idea-confirm');
    if (confirm) confirm.addEventListener('click', function () { self.confirm(); });
    var chat = document.getElementById('idea-chat-link');
    if (chat) chat.addEventListener('click', function (e) {
      e.preventDefault();
      if (window.App) App.startNewResearchChat();
    });
    var ta = document.getElementById('idea-direction');
    if (ta) ta.focus();
  },

  _status(msg, isError) {
    var el = document.getElementById('idea-status');
    if (!el) return;
    el.textContent = msg || '';
    el.style.color = isError ? '#e5484d' : 'var(--text-muted)';
  },

  _renderCards() {
    var box = document.getElementById('idea-cards');
    var actions = document.getElementById('idea-actions');
    if (!box) return;
    var self = this;
    box.innerHTML = this.cards.map(function (c, i) {
      var sel = self.selected === i;
      var patterns = (c.patterns || []).map(function (p) { return self._esc(p); }).join(' · ');
      var metrics = (c.metrics || []).map(function (m) { return self._esc(m.metric_key) + ' (' + self._esc(m.direction) + ')'; }).join(', ');
      var baselines = (c.baselines || []).map(function (b) { return self._esc(b.name || b); }).join('、');
      return '<div class="proj-big-card" data-idx="' + i + '"' +
        ' style="cursor:pointer;border:2px solid ' + (sel ? '#5b7cfa' : 'var(--border)') + ';padding:14px">' +
        '<div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">' +
          '<div class="proj-big-card-title" style="margin:0;font-size:15px">' + self._esc(c.title) + '</div>' +
          '<span style="font-size:11px;color:' + (sel ? '#5b7cfa' : 'var(--text-muted)') + '">' + (sel ? '已选中' : '点击选择') + '</span>' +
        '</div>' +
        '<p style="margin:8px 0;font-size:13px;color:var(--text-secondary)">' + self._esc(c.one_liner) + '</p>' +
        '<div style="font-size:12px;color:var(--text-muted);line-height:1.6">' +
          '<div><b>科学问题：</b>' + self._esc(c.scientific_question) + '</div>' +
          '<div><b>方法：</b>' + self._esc(c.method) + '</div>' +
          '<div><b>可证伪：</b>' + self._esc(c.falsification) + '</div>' +
          '<div><b>新颖性：</b>' + self._esc(c.novelty) + '</div>' +
          '<div><b>风险：</b>' + self._esc(c.risks) + '</div>' +
          '<div><b>算力 / 指标：</b>' + self._esc(c.compute) + (metrics ? ' · ' + metrics : '') + '</div>' +
          (baselines ? '<div><b>基线：</b>' + baselines + '</div>' : '') +
          (patterns ? '<div><b>模式：</b>' + patterns + '</div>' : '') +
        '</div>' +
      '</div>';
    }).join('');

    Array.prototype.forEach.call(box.querySelectorAll('[data-idx]'), function (el) {
      el.addEventListener('click', function () {
        self.selected = parseInt(el.getAttribute('data-idx'), 10);
        self._renderCards();
      });
    });
    if (actions) actions.style.display = 'flex';
    var confirm = document.getElementById('idea-confirm');
    if (confirm) confirm.disabled = this.selected < 0 || this.busy;
  },

  async generate() {
    if (this.busy) return;
    var ta = document.getElementById('idea-direction');
    var direction = (ta && ta.value || '').trim();
    if (direction.length < 4) {
      this._status('请至少写 4 个字描述方向', true);
      return;
    }
    this.busy = true;
    this._status('DeepSeek 正在按 15 种构思模式生成卡片（约 10-30 秒）…');
    var btn = document.getElementById('idea-generate');
    if (btn) btn.disabled = true;
    try {
      var r = await API.ideaCards(direction, 4);
      this.direction = direction;
      this.cards = (r && r.cards) || [];
      this.selected = -1;
      this._status('已生成 ' + this.cards.length + ' 张卡片（模型 ' + ((r && r.model) || '?') + '），点选一张再确认');
      this._renderCards();
    } catch (e) {
      this._status('生成失败：' + (e && e.message ? e.message : e), true);
    } finally {
      this.busy = false;
      if (btn) btn.disabled = false;
    }
  },

  async confirm() {
    if (this.busy || this.selected < 0) return;
    this.busy = true;
    var card = this.cards[this.selected];
    var confirmBtn = document.getElementById('idea-confirm');
    if (confirmBtn) confirmBtn.disabled = true;
    this._status('正在物化任务书并启动流水线…');
    try {
      var r = await API.ideaConfirm({
        direction: this.direction,
        card: card,
        auto_start: true,
        auto_approve: true,
      });
      this._status('已创建 ' + r.run_id + '，从 ' + r.from_stage + ' 启动，正在跳转…');
      if (window.App) {
        App._loadProjects();
        var rid = r.run_id;
        setTimeout(function () { App.openProject(rid); }, 1200);
      }
    } catch (e) {
      this._status('确认失败：' + (e && e.message ? e.message : e), true);
      if (confirmBtn) confirmBtn.disabled = false;
    } finally {
      this.busy = false;
    }
  },
};

window.IdeationView = IdeationView;
