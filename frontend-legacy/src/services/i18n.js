/**
 * i18n.js — ResearchClaw 仪表盘中英双语支持。
 * 机制：字典精确匹配 + 少量正则模式；MutationObserver 在每次视图渲染后翻译文本节点，
 * 原始英文存于 WeakMap，切回 EN 即还原。语言偏好存 localStorage，默认跟随浏览器。
 * 依赖：需在其它脚本之前加载（index.html 已保证）。暴露全局 window.I18N。
 */
(function () {
  'use strict';

  var norm = function (s) { return (s || '').replace(/\s+/g, ' ').trim(); };
  var tc = function (s) {
    return s.split(' ').map(function (w) { return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase(); }).join(' ');
  };

  /* ---------- 界面短语字典（英文 → 中文） ---------- */
  var DICT = {
    /* 头部 / 导航 */
    'Autonomous Research Pipeline': '自主科研流水线',
    'Overview': '总览', 'Interact': '交互', 'Manage': '管理',
    'Dashboard': '仪表盘', 'Pipeline': '流水线', 'Experiments': '实验',
    'Chat': '对话', 'Paper': '论文', 'Projects': '项目', 'Wizard': '向导',
    'Connecting...': '连接中…', 'Connected': '已连接', 'Disconnected': '已断开',
    'Loading...': '加载中…',
    /* Dashboard */
    'Pipeline Progress': '流水线进度', 'Recent Runs': '最近的运行',
    'Status': '状态', 'Current Stage': '当前阶段', 'Run ID': '运行编号', 'Topic': '主题',
    'No runs found.': '暂无运行记录。',
    'Pipeline completed!': '流水线已完成！',
    /* PipelineView */
    '23-Stage Research Pipeline': '23 阶段科研流水线',
    'Each stage is executed sequentially. Gate stages require approval.':
      '各阶段顺序执行；门控阶段需要审批。',
    /* ChatPanel */
    'Welcome to ResearchClaw! I can help you with research topics, running experiments, monitoring progress, and editing papers. Just type your question below.':
      '欢迎使用 ResearchClaw！我可以帮你确定研究主题、跑实验、监控进度、编辑论文。直接在下方输入问题即可。',
    'Ask about your research...': '问问你的研究…',
    'Send': '发送', 'Voice input': '语音输入',
    /* ExperimentMonitor */
    'Experiment Metrics': '实验指标', 'Experiment Log': '实验日志',
    'No active run.': '当前没有进行中的运行。',
    'No metrics available yet.': '暂无指标数据。',
    /* PaperPreview */
    'Paper Preview': '论文预览',
    'Paper preview will appear here after Stage 17 (Paper Draft) completes.':
      '第 17 阶段（论文起草）完成后，论文预览将显示在这里。',
    'Paper generated!': '论文已生成！', 'Run:': '运行：',
    'Markdown: available': 'Markdown：可用', 'LaTeX: available': 'LaTeX：可用', 'PDF: available': 'PDF：可用',
    /* ProjectList */
    'Projects': '项目',
    'No projects found. Start a pipeline run first.': '暂无项目。请先启动一次流水线运行。',
    /* Wizard */
    'Setup Wizard': '设置向导',
    'What do you want to research?': '你想研究什么？',
    'e.g., Domain generalization under distribution shift': '例：分布偏移下的域泛化',
    'Research Topic': '研究主题', 'Research Domain': '研究领域',
    'Select your domain:': '选择你的领域：',
    'Computer Vision': '计算机视觉', 'Natural Language Processing': '自然语言处理',
    'Reinforcement Learning': '强化学习', 'General ML': '通用机器学习', 'AI for Science': '科学智能（AI4Science）',
    'Experiment Mode': '实验模式',
    'How should experiments run?': '实验如何执行？',
    'Docker (recommended — isolated, GPU support)': 'Docker（推荐——隔离、支持 GPU）',
    'Simulated (quick demo, no real experiments)': '模拟（快速演示，不跑真实实验）',
    'Local sandbox (runs on host machine)': '本地沙箱（运行在宿主机上）',
    'Ready!': '就绪！',
    'Configuration complete! Click "Finish" to generate your config.':
      '配置完成！点击“完成”生成你的配置文件。',
    'Back': '上一步', 'Next': '下一步', 'Finish': '完成',
    'Configuration Generated': '配置已生成',
    'Copy this to your config.yaml or use the CLI:': '将以下内容复制到 config.yaml，或使用命令行：',
  };

  /* ---------- 23 阶段名（规范下划线名 → 中文） ---------- */
  var STAGES = {
    'TOPIC_INIT': '主题初始化', 'PROBLEM_DECOMPOSE': '问题分解', 'SEARCH_STRATEGY': '检索策略',
    'LITERATURE_COLLECT': '文献采集', 'LITERATURE_SCREEN': '文献筛选', 'KNOWLEDGE_EXTRACT': '知识抽取',
    'SYNTHESIS': '知识合成', 'HYPOTHESIS_GEN': '假设生成', 'EXPERIMENT_DESIGN': '实验设计',
    'CODE_GENERATION': '代码生成', 'RESOURCE_PLANNING': '资源规划', 'EXPERIMENT_RUN': '实验执行',
    'ITERATIVE_REFINE': '迭代优化', 'RESULT_ANALYSIS': '结果分析', 'RESEARCH_DECISION': '研究决策',
    'PAPER_OUTLINE': '论文大纲', 'PAPER_DRAFT': '论文起草', 'PEER_REVIEW': '同行评审',
    'PAPER_REVISION': '论文修订', 'QUALITY_GATE': '质量门', 'KNOWLEDGE_ARCHIVE': '知识归档',
    'EXPORT_PUBLISH': '导出发布', 'CITATION_VERIFY': '引用核验',
  };
  for (var k in STAGES) {
    var spaced = k.replace(/_/g, ' ');
    DICT[spaced] = STAGES[k];              // "TOPIC INIT"
    DICT[tc(spaced)] = STAGES[k];          // "Topic Init"
  }

  /* ---------- 状态词 ---------- */
  var STATUS = {
    'idle': '空闲', 'running': '运行中', 'completed': '已完成', 'failed': '失败',
    'pending': '等待中', 'unknown': '未知', 'paused': '已暂停', 'gate': '待审批',
  };

  /* ---------- 模式规则（含动态数字的短语） ---------- */
  var PATTERNS = [
    [/^(\d+)\/23 stages \((\d+)%\)$/, '$1/23 阶段（$2%）'],
    [/^Stage: (\d+)$/, '阶段：$1'],
    [/^Phase ([A-H])$/, '阶段组 $1'],
    [/^Stage failed: (.*)$/, '阶段失败：$1'],
  ];

  /* ---------- 语言状态 ---------- */
  var stored = null;
  try { stored = localStorage.getItem('rc-lang'); } catch (e) {}
  var lang = stored || ((navigator.language || '').toLowerCase().indexOf('zh') === 0 ? 'zh' : 'en');

  var originals = new WeakMap();
  var suppress = false;

  function translateText(orig) {
    var n = norm(orig);
    if (!n) return null;
    if (DICT[n]) return DICT[n];
    var low = n.toLowerCase();
    if (STATUS[low]) return STATUS[low];
    for (var i = 0; i < PATTERNS.length; i++) {
      if (PATTERNS[i][0].test(n)) return orig.replace(PATTERNS[i][0], PATTERNS[i][1]);
    }
    return null;
  }

  function translateNode(node) {
    if (!node || node.nodeType === 8) return;               // 跳过注释等
    if (node.nodeType === 3) {                              // 文本节点：以"原始英文"为基准双向翻译
      var parent = node.parentNode;
      if (!parent || parent.nodeName === 'SCRIPT' || parent.nodeName === 'STYLE') return;
      if (parent.closest && parent.closest('[data-no-i18n]')) return;
      if (!originals.has(node)) originals.set(node, node.nodeValue);
      var orig = originals.get(node);
      var out = (lang === 'zh') ? (translateText(orig) || orig) : orig;
      if (node.nodeValue !== out) node.nodeValue = out;
      return;
    }
    if (node.nodeType === 1) {                              // 元素：处理 placeholder + 递归
      var store = originals.get(node) || {};
      if (node.hasAttribute && node.hasAttribute('placeholder')) {
        if (!('placeholder' in store)) store.placeholder = node.getAttribute('placeholder');
        var phOut = (lang === 'zh') ? (translateText(store.placeholder) || store.placeholder) : store.placeholder;
        if (node.getAttribute('placeholder') !== phOut) node.setAttribute('placeholder', phOut);
      }
      originals.set(node, store);
      var walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, null);
      var t;
      while ((t = walker.nextNode())) translateNode(t);
    }
  }

  function applyAll(root) {
    suppress = true;
    translateNode(root || document.body);
    suppress = false;
  }

  function setLang(next) {
    lang = next;
    try { localStorage.setItem('rc-lang', lang); } catch (e) {}
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
    document.title = lang === 'zh' ? 'ResearchClaw — 自主科研流水线' : 'ResearchClaw — Autonomous Research Pipeline';
    updateToggle();
    applyAll(document.body);
    window.dispatchEvent(new CustomEvent('rc-lang', { detail: { lang: lang } }));
  }

  function updateToggle() {
    var btn = document.getElementById('lang-toggle');
    if (btn) btn.textContent = lang === 'zh' ? 'EN' : '中文';
  }

  /* ---------- 对外 API ---------- */
  window.I18N = {
    t: function (en) { return lang === 'zh' ? (translateText(en) || en) : en; },
    lang: function () { return lang; },
    toggle: function () { setLang(lang === 'zh' ? 'en' : 'zh'); },
  };

  /* ---------- 启动 ---------- */
  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('lang-toggle');
    if (btn) btn.addEventListener('click', function () { window.I18N.toggle(); });
    updateToggle();
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
    if (lang === 'zh') document.title = 'ResearchClaw — 自主科研流水线';

    // 视图/徽章等任何 DOM 变化后自动翻译
    new MutationObserver(function (muts) {
      if (suppress) return;
      muts.forEach(function (m) {
        if (m.type === 'childList') {
          for (var i = 0; i < m.addedNodes.length; i++) translateNode(m.addedNodes[i]);
        } else if (m.type === 'characterData') {
          translateNode(m.target);
        }
      });
    }).observe(document.body, { childList: true, subtree: true, characterData: true });

    applyAll(document.body);
  });
})();
