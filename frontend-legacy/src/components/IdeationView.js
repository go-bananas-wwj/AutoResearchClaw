/** Two-review, evidence-first research intake. Sessions survive refresh/navigation. */
const IdeationView = {
  host: null, data: null, sessionId: null, busy: false,
  _timer: null, _token: 0, _signature: '', _dialog: null, _returnFocus: null, _listener: null, _error: '',
  phaseLabel(p) { return ({retrieval:'检索与筛选',literature_review:'待审核文献',reading:'全文精读',synthesis:'问题定位',generate:'构思创新点',audit:'最近邻查重与审查',idea_review:'待审核创新点',handed_off:'已交接自动科研'})[p] || p; },
  statusLabel(s) { return ({pending:'待执行',running:'进行中',handoff:'正在交接科研',waiting:'等待审核',blocked:'需要补充材料',interrupted:'已中断',stopped:'已停止',stopping:'停止中',completed:'已完成'})[s] || s; },
  _esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
  _link(url, label) { return /^https?:\/\//i.test(url || '') ? `<a href="${this._esc(url)}" target="_blank" rel="noopener noreferrer">${this._esc(label || '原文链接')}</a>` : ''; },
  _short(s,n=110) { const a=Array.from(String(s || '').replace(/\s+/g,' ').trim()); return a.slice(0,n).join('')+(a.length>n?'…':''); },

  render(host, opts={}) {
    this.destroy(); this.host=host; this.data=null; this.sessionId=opts.sessionId || null;
    this.busy=false; this._error=''; this._signature='';
    this._listener=e=>{ const button=e.target.closest('[data-action]'); if(button && !button.disabled) this._act(button.dataset.action,button.dataset.index); };
    host.addEventListener('click',this._listener);
    if(this.sessionId) { this.refresh(); this._timer=setInterval(()=>this.refresh(),4000); }
    else this._paint();
  },
  destroy() {
    this._token++; clearInterval(this._timer); this._timer=null; this._closeDialog(false);
    if(this.host && this._listener) this.host.removeEventListener('click',this._listener);
    this.host=null;
  },
  async refresh(force=false) {
    if(!this.sessionId || !this.host || this.busy) return;
    const token=this._token;
    try {
      const data=await API.ideationSession(this.sessionId);
      if(token!==this._token || !this.host) return;
      const signature=JSON.stringify([data.revision,data.phase,data.status,data.papers,data.cards,data.missing,Object.keys(data.reading || {}),data.usage.length]);
      this.data=data;
      if(force || signature!==this._signature) { this._signature=signature; this._paint(); }
    } catch(e) { if(token===this._token) { this._error=e.message; this._paint(); } }
  },
  _paint() {
    if(!this.host) return;
    const inputs={}; this.host.querySelectorAll('textarea,input[type="text"]').forEach(x=>{inputs[x.id]=x.value;});
    const focus=document.activeElement && document.activeElement.id;
    const selection=document.activeElement && document.activeElement.selectionStart;
    const top=this.host.scrollTop;
    const decisions={}; this.host.querySelectorAll('[data-paper]').forEach(x=>{decisions[x.dataset.paper+':'+x.dataset.field]=x.checked;});
    const sameRevision=this.host.dataset.revision===String(this.data && this.data.revision);
    const d=this.data;
    this.host.dataset.revision=String(d && d.revision);
    this.host.innerHTML=`<div class="ideation-workspace">
      <div class="ideation-heading"><div><span class="idea-step">研究准备</span><h2>文献调研与问题定位</h2></div><button class="idea-text-btn" data-action="prompt">生成方式与提示词</button></div>
      <p class="proj-welcome-sub">先审核相关文献，再审核创新点。确认方案后，自动推进实验、分析与中文论文。</p>
      <div class="ideation-steps"><span class="${!d || ['retrieval','literature_review'].includes(d.phase)?'active':''}">1 · 文献审核</span><span class="${d && ['reading','synthesis','generate','audit','idea_review'].includes(d.phase)?'active':''}">2 · 创新点审核</span><span class="${d && d.run_id?'active':''}">3 · 自动科研与论文</span></div>
      ${this._error?`<p class="ideation-alert" role="alert">${this._esc(this._error)}</p>`:''}
      ${!this.sessionId?this._newHtml():!d?'<p role="status">正在读取调研会话…</p>':this._sessionHtml(d)}
      </div>`;
    Object.entries(inputs).forEach(([id,value])=>{const el=document.getElementById(id);if(el)el.value=value;});
    if(sameRevision) this.host.querySelectorAll('[data-paper]').forEach(x=>{const key=x.dataset.paper+':'+x.dataset.field;if(key in decisions)x.checked=decisions[key];});
    const focused=document.getElementById(focus); if(focused){focused.focus({preventScroll:true});if(typeof selection==='number' && focused.setSelectionRange)focused.setSelectionRange(selection,selection);}
    this.host.scrollTop=top;
    const upload=document.getElementById('lit-upload');if(upload)upload.addEventListener('change',()=>this._upload(upload.files[0]));
    this.host.querySelectorAll('[data-paper]').forEach(x=>x.addEventListener('change',()=>{
      const row=x.closest('.literature-paper');if(x.dataset.field==='required' && x.checked)row.querySelector('[data-field="selected"]').checked=true;
      if(x.dataset.field==='selected' && !x.checked)row.querySelector('[data-field="required"]').checked=false;
    }));
  },
  _newHtml() { return `<div class="card ideation-intake"><label for="idea-direction">你想研究什么？</label>
    <textarea id="idea-direction" class="gate-textarea" rows="3" placeholder="例如：我想研究流域建模的 LS 因子"></textarea>
    <button class="proj-btn primary" data-action="start" ${this.busy?'disabled':''}>${this.busy?'正在创建调研…':'开始调研'}</button>
    <p class="idea-evidence-note">自动检索与筛选后，先由你确认论文是否相关。此时不会创建正式科研项目或启动实验。</p></div>`; },
  _sessionHtml(d) {
    const active=['running','pending','stopping'].includes(d.status), locked=active || d.status==='handoff' || !!d.run_id;
    const selected=d.papers.filter(p=>p.selected);
    return `<div class="card"><div class="ideation-progress"><strong>${this._esc(this.phaseLabel(d.phase))}</strong><span class="proj-badge">${this._esc(this.statusLabel(d.status))}</span>
      ${active?`<button class="proj-btn" data-action="stop" ${d.status==='stopping'?'disabled':''}>停止调研</button>`:''}
      ${['blocked','stopped','interrupted'].includes(d.status) && !['literature_review','idea_review'].includes(d.phase)?'<button class="proj-btn" data-action="resume">从完成点继续</button>':''}</div>
      <p class="idea-dialog-intro" data-no-i18n>${this._esc(d.direction)}</p>
      ${d.plan?`<p class="idea-evidence-note">当前理解：${this._esc(d.plan.interpretation)}</p>`:''}
      <p class="idea-evidence-note">文献 ${d.papers.length} 篇 · 保留 ${selected.length} 篇 · 已精读 ${Object.keys(d.reading || {}).length} 篇 · 版本 ${d.revision}</p>
      <p class="idea-evidence-note">预算保守预留 $${Number(d.cost_usd).toFixed(3)}${d.budget_usd?' / $'+d.budget_usd:''}（非供应商账单）</p>
      ${(d.missing || []).length?`<div class="ideation-alert" role="alert">${d.missing.map(m=>`<p>${this._esc(m)}</p>`).join('')}</div>`:''}
      ${d.run_id?`<p>已启动第 9–23 阶段：<button class="idea-text-btn" data-action="project">打开科研项目</button></p>`:''}</div>
      ${!locked?`<details class="card"><summary>补充论文或修改研究范围</summary><div class="ideation-supplement">
        <input id="lit-reference" class="gate-input" type="text" placeholder="论文 DOI 或公开 URL" /><button class="proj-btn" data-action="add-paper">补充论文</button>
        <label class="proj-btn">上传 PDF（最大25MB）<input id="lit-upload" type="file" accept="application/pdf,.pdf" hidden /></label>
        <textarea id="lit-scope" class="gate-textarea" rows="2">${this._esc(d.direction)}</textarea><button class="proj-btn" data-action="scope">修改范围并重新检索</button>
        <p class="idea-evidence-note">增加文献或修改范围会使原审批与候选失效，返回文献审核。</p></div></details>`:''}
      ${d.papers.length?this._papersHtml(d,locked):''}
      ${d.phase==='literature_review' && !locked?'<div class="card"><h3>第一轮：确认文献范围</h3><p class="idea-evidence-note">保留相关论文，可标记必读；确认后自动获取全文、精读并构思。至少需要3篇含方法正文的核心论文。</p><textarea id="lit-feedback" class="gate-textarea" rows="2" placeholder="可选：关注的问题、数据或研究边界"></textarea><button class="proj-btn primary" data-action="review-literature">确认文献，继续精读与构思</button></div>':''}
      ${d.synthesis?`<details class="card" open><summary>领域现状与问题定位</summary><div class="ideation-landscape" data-no-i18n>${this._esc(d.synthesis.landscape_zh)}</div></details>`:''}
      ${d.cards.length?this._cardsHtml(d):''}
      ${(d.rejected_cards || []).length?`<details class="card"><summary>未通过审查的候选（${d.rejected_cards.length}）</summary>${d.rejected_cards.map(c=>`<div class="idea-detail-section"><h3>${this._esc(c.title)}</h3><p>${this._esc(c.rejection_reason)}</p></div>`).join('')}</details>`:''}
      ${d.approved_revision===d.revision && !locked && !['reading','synthesis'].includes(d.phase)?`<div class="card"><h3>修改意见与重新生成</h3><textarea id="idea-feedback" class="gate-textarea" rows="3" placeholder="例如：优先研究跨 DEM 分辨率的稳定性，不采用深度学习；补充真实数据入口。"></textarea><button class="proj-btn" data-action="regenerate">基于已批准文献重新生成</button></div>`:''}
      ${d.search_log.length?`<details class="card"><summary>检索来源与过程（${d.search_log.length} 次查询）</summary>${d.search_log.map(s=>`<p class="idea-evidence-note" data-no-i18n>${this._esc(s.source)} · ${s.count} 篇 · ${this._esc(s.query)}${s.count?'':' · 未返回记录或来源不可用'}</p>`).join('')}</details>`:''}`;
  },
  _papersHtml(d,locked) {
    locked = locked || d.phase !== 'literature_review';
    const state={not_fetched:'尚未获取全文',read:'全文已读',abstract_only:'只有摘要',failed:'全文获取失败'};
    const renderRow=({paper:p,index:i})=>`<article class="literature-paper">
      <div class="literature-paper-head"><label><input type="checkbox" data-paper="${p.paper_id}" data-field="selected" ${p.selected?'checked':''} ${locked?'disabled':''}/>保留</label>
      <label><input type="checkbox" data-paper="${p.paper_id}" data-field="required" ${p.required?'checked':''} ${locked?'disabled':''}/>必读</label><span class="proj-badge">${this._esc(state[p.fulltext_status] || '待获取')}</span></div>
      <h3 data-no-i18n>${this._esc(p.title)}</h3><p class="idea-evidence-note">${p.year || '年份待核对'} · ${this._esc(({core:'核心相关',adjacent:'相邻工作',off_topic:'不相关',unreviewed:'待筛选'})[p.relevance])} ${this._link(p.url || (p.doi?'https://doi.org/'+p.doi:''))}</p>
      <p class="idea-dialog-intro" data-no-i18n>${this._esc(p.summary_zh || '尚未完成摘要筛选')}</p><p class="idea-evidence-note">相关性理由：${this._esc(p.reason || '待判断')}</p>
      ${p.fulltext_error?`<p class="idea-evidence-note">${this._esc(p.fulltext_error)}</p>`:''}
      ${d.reading[p.paper_id]?`<button class="idea-text-btn" data-action="paper" data-index="${i}">查看精读记录与原文证据</button>`:''}</article>`;
    const rows=d.papers.map((paper,index)=>({paper,index}));
    const rank={core:0,adjacent:1,unreviewed:2,off_topic:3};
    const relevant=rows.filter(x=>x.paper.relevance!=='off_topic').sort((a,b)=>(rank[a.paper.relevance]??2)-(rank[b.paper.relevance]??2));
    const excluded=rows.filter(x=>x.paper.relevance==='off_topic');
    return `<details class="card" ${['literature_review','reading'].includes(d.phase)?'open':''}><summary>文献清单与相关性审核（相关及待核对 ${relevant.length} 篇）</summary><div class="literature-list">${relevant.map(renderRow).join('')}${excluded.length?`<details><summary>已排除的无关论文（${excluded.length} 篇，可展开复核）</summary>${excluded.map(renderRow).join('')}</details>`:''}</div></details>`;

  },
  _cardsHtml(d) { return `<div class="card"><h3>第二轮：审核创新点</h3><p class="idea-evidence-note">候选已做本次检索范围内的最近邻审查，尚未得到实验验证。点击查看完整机制、差异与证据。</p></div><div class="idea-cards">${d.cards.map((c,i)=>`<button class="proj-big-card idea-card" data-action="card" data-index="${i}" aria-haspopup="dialog"><span class="idea-card-top">候选 ${i+1}<span>查看详情</span></span><span class="proj-big-card-title idea-card-title" data-no-i18n>${this._esc(c.title)}</span><span class="idea-card-summary" data-no-i18n>${this._esc(this._short(c.one_liner))}</span><span class="idea-card-more">方法 · 最近邻差异 · 原文依据 · 实验设计</span></button>`).join('')}</div>`; },

  async _action(name,body) {
    if(this.busy) return; this.busy=true; this._error='';
    const token=this._token;
    try { await API.ideationAction(this.sessionId,name,{revision:this.data.revision,...body}); }
    catch(e) { if(token===this._token)this._error=e.message; }
    finally { if(token===this._token){this.busy=false; await this.refresh(true); if(window.App)App._loadIdeations();} }
  },
  async _act(action,index) {
    if(action==='prompt')return this._showPrompt();
    if(action==='card')return this._openCard(Number(index));
    if(action==='paper')return this._openPaper(Number(index));
    if(action==='project')return App.openProject(this.data.run_id);
    if(this.busy)return;
    if(action==='start'){
      const direction=document.getElementById('idea-direction').value.trim();if(direction.length<4){this._error='请至少写4个字符描述方向。';this._paint();return;}
      this.busy=true;this._paint(); const token=this._token;
      try { const data=await API.createIdeation(direction);if(token===this._token)App.openIdeation(data.session_id); }
      catch(e){if(token===this._token){this.busy=false;this._error=e.message;this._paint();}} return;
    }
    if(action==='review-literature'){
      const chosen=field=>[...this.host.querySelectorAll(`[data-field="${field}"]:checked`)].map(x=>x.dataset.paper);
      return this._action('literature-review',{selected_paper_ids:chosen('selected'),required_paper_ids:chosen('required'),feedback:document.getElementById('lit-feedback').value});
    }
    if(action==='scope')return this._action('scope',{direction:document.getElementById('lit-scope').value});
    if(action==='add-paper')return this._action('papers',{reference:document.getElementById('lit-reference').value});
    if(action==='regenerate')return this._action('cards',{feedback:document.getElementById('idea-feedback').value});
    if(action==='resume' || action==='stop')return this._action(action,{});
  },
  async _upload(file) {
    if(!file || this.busy)return;
    if(file.size>25*1024*1024){this._error='PDF最大25MB';this._paint();return;}
    this.busy=true;const token=this._token;
    try{await API.uploadIdeationPaper(this.sessionId,this.data.revision,file);}catch(e){if(token===this._token)this._error=e.message;}
    finally{if(token===this._token){this.busy=false;await this.refresh(true);}}
  },
  _section(title,value) { return `<section class="idea-detail-section"><h3>${this._esc(title)}</h3><p data-no-i18n>${this._esc(value || '未提供，须进一步核实')}</p></section>`; },
  _openPaper(index) {
    const p=this.data.papers[index],r=this.data.reading[p.paper_id];if(!r)return;
    this._showDialog(p.title,['problem','method','data','experiments','findings','limitations'].map((k,i)=>this._section(['问题','方法','数据','实验','结论','局限与适用条件'][i],r[k])).join('')+
      this._section('原文证据（source=原文事实；inference=模型推断）',JSON.stringify(r.evidence,null,2)));
  },
  _openCard(index) {
    const card=this.data.cards[index];if(!card)return;
    let body=this._section('研究摘要',card.one_liner)+this._section('科学问题',card.scientific_question)+this._section('方法与机制',card.method)+this._section('差异与新颖性边界',card.novelty)+this._section('可证伪实验',card.falsification)+this._section('数据可得性',card.data_availability)+this._section('数据来源',JSON.stringify(card.data_sources,null,2))+this._section('风险与资源',card.risks+'\n'+card.compute)+this._section('基线与指标',JSON.stringify({baselines:card.baselines,metrics:card.metrics},null,2));
    body+=this._section('独立审查结果',card.audit.reason);
    (card.audit.comparisons || []).forEach(c=>{const p=card.closest_papers.find(p=>p.paper_id===c.paper_id);body+=this._section('最近邻：'+(p?p.title:c.paper_id),`问题：${c.problem}\n机制：${c.mechanism}\n数据：${c.data}\n实验：${c.experiment}\n差异：${c.difference}`)+(p?this._link(p.url):'');});
    body+='<h3 class="idea-detail-section">原文依据</h3>'+(card.evidence || []).map(e=>`<blockquote class="ideation-evidence" data-no-i18n><small>${this._esc(e.paper_id)} · ${this._esc(e.location)} · ${this._esc(e.passage_id)}</small><p>${this._esc(e.text)}</p></blockquote>`).join('');
    const dialog=this._showDialog(card.title,body,'<button class="proj-btn" data-dialog-close>返回继续审核</button><button class="proj-btn primary" id="idea-confirm-card">确认创新点，自动完成科研</button>');
    dialog.querySelector('#idea-confirm-card').addEventListener('click',async e=>{
      if(this.busy)return;this.busy=true;e.target.disabled=true;e.target.textContent='正在核验数据并交接…';const token=this._token;
      try{const response=await API.ideationAction(this.sessionId,'confirm',{revision:this.data.revision,card_id:card.card_id});if(token===this._token){this._closeDialog(false);App.openProject(response.run_id);}}
      catch(error){if(token===this._token){this._error=error.message;this._closeDialog();}}
      finally{if(token===this._token){this.busy=false;await this.refresh(true);if(window.App)App._loadProjects();}}
    });
  },
  _showDialog(title,body,footer) {
    const opener=document.activeElement;this._closeDialog(false);this._returnFocus=opener;
    const dialog=document.createElement('dialog');dialog.className='idea-dialog';dialog.setAttribute('aria-labelledby','idea-dialog-title');
    dialog.innerHTML=`<div class="idea-dialog-head"><h2 id="idea-dialog-title" data-no-i18n>${this._esc(title)}</h2><button class="idea-text-btn" data-dialog-close>关闭</button></div><div class="idea-dialog-body">${body}</div><div class="idea-dialog-foot">${footer || '<button class="proj-btn" data-dialog-close>返回</button>'}</div>`;
    document.body.appendChild(dialog);this._dialog=dialog;
    dialog.querySelectorAll('[data-dialog-close]').forEach(b=>b.addEventListener('click',()=>this._closeDialog()));
    dialog.addEventListener('cancel',e=>{e.preventDefault();this._closeDialog();});dialog.addEventListener('close',()=>{if(this._dialog===dialog)this._closeDialog();});dialog.addEventListener('click',e=>{if(e.target===dialog)this._closeDialog();});dialog.showModal();return dialog;
  },
  _closeDialog(restore=true) {const d=this._dialog;if(!d)return;this._dialog=null;if(d.open)d.close();d.remove();if(restore && this._returnFocus && this._returnFocus.isConnected)this._returnFocus.focus();this._returnFocus=null;},
  async _showPrompt() {
    const dialog=this._showDialog('各阶段生成方式与实际提示词','<p>正在读取…</p>');
    try{const info=await API.ideaPrompt();if(this._dialog!==dialog)return;
      dialog.querySelector('.idea-dialog-body').innerHTML=`<p class="idea-evidence-note">配置模型：${this._esc(info.model)}。先检索、审文献，再精读、构思、查重和审创新点。两次审核之前不启动实验。${this._esc(info.budget_basis)}</p>`+Object.entries(info.stages).map(([key,text])=>`<details class="idea-prompt-block"><summary>${this._esc(({plan:'方向理解与检索规划',screen:'文献相关性筛选',read:'全文精读',synthesis:'领域现状与缺口',generate:'候选机制生成',audit:'独立最近邻审查'})[key])} · 输出预算 ${info.output_tokens[key]}</summary><pre class="proj-pre" data-no-i18n>${this._esc(text)}</pre></details>`).join('');
    }catch(e){if(this._dialog===dialog)dialog.querySelector('.idea-dialog-body').textContent='读取失败：'+e.message;}
  },
};
window.IdeationView=IdeationView;
