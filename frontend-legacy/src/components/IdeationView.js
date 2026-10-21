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
  _dialog: null,
  _returnFocus: null,
  _session: 0,
  _patterns: {},

  render(host) {
    this._closeDialog(false);
    this._session++;
    this.host = host;
    this.cards = [];
    this.selected = -1;
    this.busy = false;
    this._paint();
  },

  destroy() {
    this._session++;
    this._closeDialog(false);
    this.host = null;
  },

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
          '<span class="idea-step">第 1 步</span>' +
          '<h2 style="margin:0">创新点审核</h2>' +
        '</div>' +
        '<p style="color:var(--text-muted);margin:6px 0 16px">' +
          '描述研究方向，点击卡片查看详情，审阅并选定方案后再开始实验。' +
          '<b>确认前不会创建研究项目。</b>' +
        '</p>' +
        '<div class="card" style="padding:16px">' +
          '<textarea id="idea-direction" rows="3" placeholder="例如：我想做跨传感器遥感变化检测，但不确定还有什么可做的"' +
            ' style="width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid var(--border);' +
            'background:var(--bg-tertiary);color:var(--text-primary);font-size:14px;resize:vertical"></textarea>' +
          '<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:10px">' +
            '<button class="proj-btn primary" id="idea-generate">生成创新点卡片</button>' +
            '<button class="idea-text-btn" id="idea-prompt">生成方式与提示词</button>' +
            '<a href="#" id="idea-chat-link" style="font-size:12px;color:var(--text-muted)">或者：跳过审核，直接对话</a>' +
          '</div>' +
          '<p class="idea-evidence-note">卡片是待验证的研究设想，尚未进行文献检索或新颖性核验。</p>' +
          '<p id="idea-status" class="idea-status" role="status" aria-live="polite"></p>' +
        '</div>' +
        '<div id="idea-cards" class="idea-cards"></div>' +
        '<div id="idea-actions" style="margin-top:16px;display:none;align-items:center;gap:12px;flex-wrap:wrap">' +
          '<button class="proj-btn primary" id="idea-confirm" disabled>确认选中卡片，开始实验</button>' +
          '<span style="font-size:12px;color:var(--text-muted)">确认后生成研究任务书，并执行到实验完成；完成后可在项目中查看结果。</span>' +
        '</div>' +
      '</div>';

    var self = this;
    var gen = document.getElementById('idea-generate');
    if (gen) gen.addEventListener('click', function () { self.generate(); });
    var confirm = document.getElementById('idea-confirm');
    if (confirm) confirm.addEventListener('click', function () { self.confirm(); });
    document.getElementById('idea-prompt').addEventListener('click', function () { self._showPrompt(); });
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
    el.style.color = isError ? 'var(--error)' : 'var(--text-muted)';
  },

  _renderCards() {
    var box = document.getElementById('idea-cards');
    var actions = document.getElementById('idea-actions');
    if (!box) return;
    var self = this;
    box.innerHTML = this.cards.map(function (c, i) {
      var sel = self.selected === i;
      return '<button type="button" class="proj-big-card idea-card' + (sel ? ' selected' : '') + '" data-idx="' + i + '" aria-haspopup="dialog">' +
        '<span class="idea-card-top"><span>候选方案 ' + String(i + 1).padStart(2, '0') + '</span>' +
          '<span class="idea-card-selection">' + (sel ? '已选中' : '待审阅') + '</span></span>' +
        '<span class="proj-big-card-title idea-card-title" data-no-i18n>' + self._esc(c.title) + '</span>' +
        '<span class="idea-card-summary" data-no-i18n>' + self._esc(self._shortText(c.one_liner || c.scientific_question, 110)) + '</span>' +
        '<span class="idea-card-more">查看完整方案</span>' +
      '</button>';
    }).join('');

    Array.prototype.forEach.call(box.querySelectorAll('[data-idx]'), function (el) {
      el.addEventListener('click', function () {
        self._openCard(parseInt(el.getAttribute('data-idx'), 10));
      });
    });
    if (actions) actions.style.display = this.cards.length ? 'flex' : 'none';
    this._updateControls();
  },

  _updateControls() {
    if (!this.host) return;
    var generate = document.getElementById('idea-generate');
    if (generate) generate.disabled = this.busy;
    var confirm = document.getElementById('idea-confirm');
    if (confirm) confirm.disabled = this.selected < 0 || this.busy;
    this.host.querySelectorAll('.idea-card').forEach(el => { el.disabled = this.busy; });
  },

  _shortText(text, limit) {
    var chars = Array.from(String(text || '').replace(/\s+/g, ' ').trim());
    return chars.length > limit ? chars.slice(0, limit).join('') + '…' : chars.join('');
  },

  _rememberPatterns(catalog) {
    (catalog || []).forEach(p => { this._patterns[p.id] = p.name; });
  },

  _openCard(index) {
    var card = this.cards[index];
    if (!card || this.busy) return;
    var section = (label, value) => '<section class="idea-detail-section"><h3>' + this._esc(label) + '</h3><p data-no-i18n>' + this._esc(value || '暂未提供') + '</p></section>';
    var list = values => (values || []).join('；');
    var metrics = (card.metrics || []).map(m => {
      var direction = ['increase', 'maximize'].includes(m.direction) ? '越高越好'
        : ['decrease', 'minimize'].includes(m.direction) ? '越低越好' : m.direction;
      return m.metric_key + (direction ? '（' + direction + '）' : '');
    });
    var baselines = (card.baselines || []).map(b => typeof b === 'string' ? b : b.name + (b.note ? '：' + b.note : ''));
    var body = '<p class="idea-dialog-intro" data-no-i18n>' + this._esc(card.one_liner || '') + '</p>' +
      '<p class="idea-evidence-note">以下为模型提出的候选方案；引用、数据可得性和新颖性仍需独立核验。</p>' +
      section('科学问题', card.scientific_question) + section('方法与实验设计', card.method) +
      section('可证伪预测与负对照', card.falsification) + section('新颖性与证据缺口', card.novelty) +
      section('主要风险', card.risks) + section('算力预算', card.compute) +
      section('数据集', list(card.datasets)) + section('评价指标', list(metrics)) +
      section('对比基线', list(baselines)) + section('支撑证据（未核验）', list(card.evidence)) +
      section('构思模式', list((card.patterns || []).map(p => this._patterns[p] || p)));
    var dialog = this._showDialog(card.title || '创新点详情', body,
      '<button class="proj-btn" data-dialog-close>返回卡片</button><button class="proj-btn primary" id="idea-select-card">选择这张方案</button>');
    dialog.querySelector('#idea-select-card').addEventListener('click', () => {
      this.selected = index;
      this._renderCards();
      this._closeDialog(false);
      var selectedCard = this.host && this.host.querySelector('[data-idx="' + index + '"]');
      if (selectedCard) selectedCard.focus();
      this._status('已选择方案 ' + (index + 1) + '。确认无误后，点击「确认选中卡片，开始实验」。');
    });
  },

  _showDialog(title, body, footer) {
    var opener = document.activeElement;
    this._closeDialog(false);
    this._returnFocus = opener;
    var dialog = document.createElement('dialog');
    dialog.className = 'idea-dialog';
    dialog.setAttribute('aria-labelledby', 'idea-dialog-title');
    dialog.innerHTML = '<div class="idea-dialog-head"><h2 id="idea-dialog-title" data-no-i18n>' + this._esc(title) + '</h2>' +
      '<button class="idea-text-btn" data-dialog-close aria-label="关闭弹窗">关闭</button></div>' +
      '<div class="idea-dialog-body">' + body + '</div>' +
      '<div class="idea-dialog-foot">' + (footer || '<button class="proj-btn" data-dialog-close>返回</button>') + '</div>';
    document.body.appendChild(dialog);
    this._dialog = dialog;
    dialog.querySelectorAll('[data-dialog-close]').forEach(el => el.addEventListener('click', () => this._closeDialog()));
    dialog.addEventListener('cancel', e => { e.preventDefault(); this._closeDialog(); });
    dialog.addEventListener('close', () => { if (this._dialog === dialog) this._closeDialog(); });
    dialog.addEventListener('click', e => { if (e.target === dialog) this._closeDialog(); });
    dialog.showModal();
    return dialog;
  },

  _closeDialog(restoreFocus = true) {
    var dialog = this._dialog;
    if (!dialog) return;
    this._dialog = null;
    if (dialog.open) dialog.close();
    dialog.remove();
    var opener = this._returnFocus;
    this._returnFocus = null;
    if (restoreFocus && opener && opener.isConnected) opener.focus();
  },

  async _showPrompt() {
    var dialog = this._showDialog('生成方式与提示词', '<p role="status">正在读取实际使用的提示词…</p>');
    try {
      var info = await API.ideaPrompt();
      if (this._dialog !== dialog) return;
      this._rememberPatterns(info.pattern_catalog);
      var options = info.generation || {};
      dialog.querySelector('.idea-dialog-body').innerHTML =
        '<p class="idea-dialog-intro">研究方向与 15 种构思模式一起提交给模型，默认请求 4 张候选卡片。排序由模型按提示词完成，没有额外评分器，也没有文献检索或新颖性核验。</p>' +
        '<p class="idea-evidence-note">标题较长且为英文、方法细节较多，来自下方模板的明确要求。列表仅显示摘要，完整内容保留在详情弹窗中。</p>' +
        '<dl class="idea-generation-meta"><div><dt>配置主模型</dt><dd>' + this._esc(info.model) + '</dd></div>' +
        '<div><dt>生成参数</dt><dd>temperature=' + this._esc(options.temperature) + ' · max_tokens=' + this._esc(options.max_tokens) + ' · JSON 输出</dd></div></dl>' +
        '<details class="idea-prompt-block" open><summary>系统提示词（含完整 15 种构思模式）</summary><pre class="proj-pre" data-no-i18n>' + this._esc(info.system) + '</pre></details>' +
        '<details class="idea-prompt-block"><summary>用户提示词模板与输出字段（研究方向替换为你的输入）</summary><pre class="proj-pre" data-no-i18n>' + this._esc(info.user) + '</pre></details>';
    } catch (e) {
      if (this._dialog === dialog) dialog.querySelector('.idea-dialog-body').textContent = '读取失败：' + (e.message || e);
    }
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
    var session = this._session;
    this._updateControls();
    this._status('DeepSeek 正在按 15 种构思模式生成卡片（约 10-30 秒）…');
    var btn = document.getElementById('idea-generate');
    if (btn) btn.disabled = true;
    try {
      var r = await API.ideaCards(direction, 4);
      if (this._session !== session) return;
      this._rememberPatterns(r && r.pattern_catalog);
      this.direction = direction;
      this.cards = (r && r.cards) || [];
      this.selected = -1;
      this._status('已生成 ' + this.cards.length + ' 张卡片，点击卡片查看完整方案，再选择并确认。');
      this._renderCards();
    } catch (e) {
      if (this._session === session) this._status('生成失败：' + (e && e.message ? e.message : e), true);
    } finally {
      if (this._session === session) {
        this.busy = false;
        this._updateControls();
      }
    }
  },

  async confirm() {
    if (this.busy || this.selected < 0) return;
    this.busy = true;
    this._updateControls();
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
      this._updateControls();
    }
  },
};

window.IdeationView = IdeationView;
