/** Two-review, evidence-first research intake. Sessions survive refresh/navigation. */
const IdeationView = {
  host: null, data: null, sessionId: null, busy: false,
  _timer: null, _token: 0, _signature: '', _dialog: null, _returnFocus: null, _listener: null, _error: '',
  phaseLabel(p) { return ({retrieval:'检索与筛选',literature_review:'待确认研究方向',reading:'全文精读',synthesis:'问题定位',generate:'构思创新点',audit:'最近邻查重与审查',idea_review:'待审核创新点',handed_off:'已交接自动科研'})[p] || p; },
  statusLabel(s) { return ({pending:'待执行',running:'进行中',handoff:'正在交接科研',waiting:'等待审核',blocked:'需要补充材料',interrupted:'已中断',stopped:'已停止',stopping:'停止中',completed:'已完成'})[s] || s; },
  _esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
  _link(url, label) { return /^https?:\/\//i.test(url || '') ? `<a href="${this._esc(url)}" target="_blank" rel="noopener noreferrer">${this._esc(label || '原文链接')}</a>` : ''; },
  _short(s,n=110) { const a=Array.from(String(s || '').replace(/\s+/g,' ').trim()); return a.slice(0,n).join('')+(a.length>n?'…':''); },

  render(host, opts={}) {
    this.destroy(); this.host=host; this.data=null; this.sessionId=opts.sessionId || null;
    this.busy=false; this._error=''; this._signature=''; this._paperPage=0; this._pending='';
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
      const signature=JSON.stringify([data.revision,data.phase,data.status,data.download_task,data.progress,data.progress_message,data.plan,data.papers,data.cards,data.missing,Object.keys(data.reading || {}),data.usage.length]);
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
    const d=this.data;
    this.host.dataset.revision=String(d && d.revision);
    this.host.innerHTML=`<div class="ideation-workspace">
      <div class="ideation-heading"><div><span class="idea-step">研究准备</span><h2>文献调研与方向确认</h2></div><button class="idea-text-btn" data-action="prompt">生成方式与提示词</button></div>
      <p class="proj-welcome-sub">智能体先调研相关文献，你确认方向后，它会自动精读并寻找创新点。</p>
      <div class="ideation-steps"><span class="${!d || ['retrieval','literature_review'].includes(d.phase)?'active':''}">1 · 确认文献方向</span><span class="${d && ['reading','synthesis','generate','audit','idea_review'].includes(d.phase)?'active':''}">2 · 确认创新点</span><span class="${d && d.run_id?'active':''}">3 · 自动科研与论文</span></div>
      ${this._pending?`<div class="card literature-live" role="status" aria-live="polite"><span class="literature-spinner" aria-hidden="true"></span>${this._esc(this._pending)}</div>`:''}
      ${this._error?`<p class="ideation-alert" role="alert">${this._esc(this._error)}</p>`:''}
      ${!this.sessionId?this._newHtml():!d?'<p role="status">正在读取调研会话…</p>':this._sessionHtml(d)}
      </div>`;
    Object.entries(inputs).forEach(([id,value])=>{const el=document.getElementById(id);if(el)el.value=value;});
    const focused=document.getElementById(focus); if(focused){focused.focus({preventScroll:true});if(typeof selection==='number' && focused.setSelectionRange)focused.setSelectionRange(selection,selection);}
    this.host.scrollTop=top;
    const upload=document.getElementById('lit-upload');if(upload)upload.addEventListener('change',()=>this._upload(upload.files[0]));

  },
  _newHtml() { return `<div class="card ideation-intake"><label for="idea-direction">你想研究什么？</label>
    <textarea id="idea-direction" class="gate-textarea" rows="3" placeholder="例如：我想研究流域建模的 LS 因子"></textarea>
    <button class="proj-btn primary" data-action="start" ${this.busy?'disabled':''}>${this.busy?'正在创建调研…':'开始调研'}</button>
    <p class="idea-evidence-note">先找到相关文献，再请你确认研究方向。</p></div>`; },
  _sessionHtml(d) {
    const active=['running','pending','stopping'].includes(d.status), locked=active || this.busy || d.status==='handoff' || !!d.run_id;
    return `<div class="card ideation-brief">
      ${active || ['blocked','stopped','interrupted','handoff'].includes(d.status)?`<div class="ideation-progress"><strong>${this._esc(d.progress_message || this.phaseLabel(d.phase))}</strong><span class="proj-badge">${this._esc(this.statusLabel(d.status))}</span>
      ${active?`<button class="proj-btn" data-action="stop" ${d.status==='stopping'?'disabled':''}>停止调研</button>`:''}
      ${['blocked','stopped','interrupted'].includes(d.status) && (!['literature_review','idea_review'].includes(d.phase) || d.download_task)?'<button class="proj-btn" data-action="resume">继续调研</button>':''}</div>`:''}
      ${d.progress && (active || d.status==='blocked')?`<div class="literature-live" role="status" aria-live="polite">${active?'<span class="literature-spinner" aria-hidden="true"></span>':''}<div>${d.progress.current?`<strong>${this._esc(d.progress.unit || '论文')} ${d.progress.current} / ${d.progress.total}</strong>`:''}${d.progress.title?`<p data-no-i18n>${this._esc(d.progress.title)}</p>`:''}<p>${d.papers.filter(p=>['downloaded','read'].includes(p.fulltext_status)).length} 篇已获取原文 · ${Object.keys(d.reading || {}).length} 篇已完成精读</p></div></div>`:''}
      <dl class="ideation-brief-fields"><div><dt>用户输入</dt><dd data-no-i18n>${this._esc(d.direction)}</dd></div>
      <div><dt>研究对象</dt><dd data-no-i18n>${this._esc(this._researchObject(d))}</dd></div></dl>
      ${!active && (d.missing || []).length?`<div class="ideation-alert" role="alert">${d.missing.map(m=>`<p>${this._esc(m)}</p>`).join('')}</div>`:''}
      ${d.run_id?`<p>科研任务已开始：<button class="idea-text-btn" data-action="project">打开科研项目</button></p>`:''}</div>
      ${!locked?`<details class="card" id="ideation-scope-editor"><summary>调整方向或补充材料</summary><div class="ideation-supplement">
        <input id="lit-reference" class="gate-input" type="text" placeholder="论文 DOI 或公开 URL" /><button class="proj-btn" data-action="add-paper">补充论文</button>
        <label class="proj-btn">上传 PDF<input id="lit-upload" type="file" accept="application/pdf,.pdf" hidden /></label>
        <textarea id="lit-scope" class="gate-textarea" rows="2">${this._esc(d.direction)}</textarea><button class="proj-btn" data-action="scope">按这个方向重新调研</button>
</div></details>`:''}
      ${d.papers.length?this._papersHtml(d):''}
      ${d.phase==='literature_review' && !locked?`<div class="card ideation-direction-confirm"><h3>这些文献的研究方向符合你的想法吗？</h3>
        <div class="ideation-confirm-actions"><button class="proj-btn primary" data-action="confirm-direction">是这个方向，继续寻找创新点</button><button class="proj-btn" data-action="adjust-direction">调整研究方向</button></div>
        <details class="ideation-optional-feedback"><summary>补充关注点</summary><textarea id="lit-feedback" class="gate-textarea" rows="2" placeholder="例如：重点关注 LS 因子计算方法，不研究其他侵蚀因子"></textarea></details></div>`:''}

      ${d.synthesis?`<details class="card" open><summary>领域现状与问题定位</summary><div class="ideation-landscape" data-no-i18n>${this._esc(d.synthesis.landscape_zh)}</div></details>`:''}
      ${d.cards.length?this._cardsHtml(d):''}
      ${(d.rejected_cards || []).length?`<details class="card"><summary>未通过审查的候选（${d.rejected_cards.length}）</summary>${d.rejected_cards.map(c=>`<div class="idea-detail-section"><h3>${this._esc(c.title)}</h3><p>${this._esc(c.rejection_reason)}</p></div>`).join('')}</details>`:''}
      ${d.approved_revision===d.revision && !locked && !['reading','synthesis'].includes(d.phase)?`<div class="card"><h3>修改意见与重新生成</h3><textarea id="idea-feedback" class="gate-textarea" rows="3" placeholder="例如：优先研究跨 DEM 分辨率的稳定性，不采用深度学习；补充真实数据入口。"></textarea><button class="proj-btn" data-action="regenerate">基于已批准文献重新生成</button></div>`:''}
      `;

  },
  _researchObject(d) {
    const plan=d.plan || {};
    let text=String(plan.research_object || plan.interpretation || d.direction || '').replace(/\s+/g,' ').trim();
    text=text.replace(/（([^）]*)）/g,(_,inside)=>'（'+inside.split(/[，,；;]/)[0]+'）').split(/[。；！？\n]/)[0];
    text=text.replace(/，(?:用于|范围|当前理解|不是|若用户).*$/,'').replace(/^(研究对象是|研究对象[:：]|当前理解[:：])\s*/,'');
    if(!plan.research_object && !plan.interpretation) text=text.replace(/^(我想|我要|我希望|请|帮我)\s*/,'');
    if(!text) return '正在归纳研究对象…';
    return (/^(研究|分析|探讨|评估|识别)/.test(text)?text:'研究'+text)+'。';
  },
  _paperCitation(p) {
    const authors=[...new Set((p.authors || []).map(a=>typeof a==='string'?a:a.name).filter(Boolean))];
    const institutions=[...new Set([...(p.institutions || []),...(p.authors || []).flatMap(a=>typeof a.affiliation==='string'?a.affiliation.split(/[；;]/):[])].map(s=>String(s).trim()).filter(Boolean))];
    const type=p.venue_type || (p.source==='arxiv' || p.arxiv_id?'preprint':/DOAJ|repository/i.test(p.venue || '')?'repository':'unknown');
    const label=({journal:'期刊',conference:'会议',preprint:'预印本',book:'出版物',repository:'收录平台'})[type] || '来源';
    return `<dl class="literature-citation">${type==='repository'?'<div><dt>期刊/会议</dt><dd>未提供</dd></div>':''}<div><dt>${label}</dt><dd data-no-i18n>${this._esc(p.venue || '未提供')}${p.year?' · '+p.year:''} ${this._link(p.url || (p.doi?'https://doi.org/'+p.doi:''))}</dd></div>
      <div><dt>作者</dt><dd data-no-i18n>${this._esc(authors.join('、') || '未提供')}</dd></div>
      <div><dt>机构</dt><dd data-no-i18n>${this._esc(institutions.join('；') || '未提供')}</dd></div></dl>`;
  },
  _papersHtml(d) {
    const rank={core:0,adjacent:1,unreviewed:2,off_topic:3};
    const relevant=d.papers.map((paper,index)=>({paper,index})).filter(x=>x.paper.relevance!=='off_topic' && !x.paper.user_excluded).sort((a,b)=>(rank[a.paper.relevance]??2)-(rank[b.paper.relevance]??2));
    const size=6,pages=Math.max(1,Math.ceil(relevant.length/size));
    this._paperPage=Math.min(this._paperPage || 0,pages-1);
    const cards=relevant.slice(this._paperPage*size,(this._paperPage+1)*size).map(({paper:p,index:i})=>{
      const o=p.overview || {},r=d.reading[p.paper_id];
      const basis=r?'全文精读':p.summary_basis==='title'?'题名信息':p.abstract?'摘要概览':'题名信息';
      const state=this._fulltextLabel(p);
      return `<button class="literature-tile" data-action="paper" data-index="${i}" aria-haspopup="dialog">
        <span class="literature-tile-top"><span>${this._esc(p.year || '年份未提供')}</span><span>${this._esc(basis)}</span></span>
        <h3 data-no-i18n>${this._esc(p.title)}</h3><p class="literature-tile-venue" data-no-i18n>${this._esc(p.venue || '期刊/会议未提供')}</p>
        <div class="literature-tile-overview" data-no-i18n><strong>科学问题</strong><p>${this._esc(this._short(o.problem || p.summary_zh || '正在整理论文概览',100))}</p><strong>怎么做</strong><p>${this._esc(this._short(o.method || '点击查看论文概览',90))}</p></div>
        <span class="literature-tile-bottom"><span>${this._esc(state)}</span><span>查看详情 ↗</span></span></button>`;
    }).join('');
    const updatable=!this.busy && !['running','pending','stopping','handoff'].includes(d.status) && !d.run_id;
    return `<section class="card literature-gallery"><div class="literature-gallery-head"><h3>相关文献 · ${relevant.length} 篇</h3>${updatable && relevant.some(x=>!['downloaded','read'].includes(x.paper.fulltext_status))?'<button class="idea-text-btn" data-action="fulltexts">获取相关原文</button>':''}${updatable && relevant.some(x=>x.paper.summary_version!==2 && !d.reading[x.paper.paper_id])?'<button class="idea-text-btn" data-action="summaries">更新论文概览</button>':''}</div><div class="literature-grid">${cards || '<p>暂无相关文献，请调整研究方向或补充材料。</p>'}</div><nav class="literature-pagination" aria-label="文献翻页"><button class="proj-btn" data-action="papers-prev" ${this._paperPage===0?'disabled':''}>← 上一页</button><span aria-live="polite">${this._paperPage+1} / ${pages}</span><button class="proj-btn" data-action="papers-next" ${this._paperPage===pages-1?'disabled':''}>下一页 →</button></nav></section>`;
  },
  _fulltextLabel(p) {
    return ({read:'已完成精读',downloaded:'已获取正文 · 待精读',abstract_only:'仅有摘要',failed:'正文获取失败',not_fetched:'待获取正文'})[p.fulltext_status] || '待获取正文';
  },
  _cardsHtml(d) { return `<div class="card"><h3>第二轮：确认创新点</h3><p class="idea-evidence-note">查看方案的方法、文献依据与验证实验，选择你想推进的创新点。</p></div><div class="idea-cards">${d.cards.map((c,i)=>`<button class="proj-big-card idea-card" data-action="card" data-index="${i}" aria-haspopup="dialog"><span class="idea-card-top">候选 ${i+1}<span>查看详情</span></span><span class="proj-big-card-title idea-card-title" data-no-i18n>${this._esc(c.title)}</span><span class="idea-card-summary" data-no-i18n>${this._esc(this._short(c.one_liner))}</span><span class="idea-card-more">方法 · 最近邻差异 · 原文依据 · 实验设计</span></button>`).join('')}</div>`; },

  async _action(name,body) {
    if(this.busy) return; this.busy=true; this._error='';
    this._pending=({'direction-confirm':'方向已提交，正在安排全文获取与精读…',resume:'正在恢复调研，从最近完成点继续…',stop:'正在请求停止，等待当前操作结束…',fulltexts:'正在获取相关论文原文…',summaries:'正在更新科学问题、方法、优势与局限…'})[name] || '正在保存并更新调研…';
    this._paint();
    this.host.querySelector('[role="status"]')?.scrollIntoView?.({block:'nearest',behavior:'smooth'});
    const token=this._token;
    try { await API.ideationAction(this.sessionId,name,{revision:this.data.revision,...body}); }
    catch(e) { if(token===this._token)this._error=e.message; }
    finally { if(token===this._token){this.busy=false; this._pending=''; await this.refresh(true); if(window.App)App._loadIdeations();} }
  },
  async _act(action,index) {
    if(action==='prompt')return this._showPrompt();
    if(action==='card')return this._openCard(Number(index));
    if(action==='paper')return this._openPaper(Number(index));
    if(action==='papers-prev' || action==='papers-next'){this._paperPage+=(action==='papers-next'?1:-1);this._paint();this.host.querySelector('.literature-gallery')?.scrollIntoView?.({block:'start',behavior:'smooth'});return;}
    if(action==='summaries' || action==='fulltexts')return this._action(action,{});
    if(action==='project')return App.openProject(this.data.run_id);
    if(this.busy)return;
    if(action==='start'){
      const direction=document.getElementById('idea-direction').value.trim();if(direction.length<4){this._error='请至少写4个字符描述方向。';this._paint();return;}
      this.busy=true;this._paint(); const token=this._token;
      try { const data=await API.createIdeation(direction);if(token===this._token)App.openIdeation(data.session_id); }
      catch(e){if(token===this._token){this.busy=false;this._error=e.message;this._paint();}} return;
    }
    if(action==='confirm-direction')return this._action('direction-confirm',{feedback:document.getElementById('lit-feedback').value});
    if(action==='adjust-direction'){
      const editor=document.getElementById('ideation-scope-editor');if(editor)editor.open=true;
      const input=document.getElementById('lit-scope');if(input){input.scrollIntoView?.({block:'center',behavior:'smooth'});input.focus();}return;
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
  _section(title,value) { return `<section class="idea-detail-section"><h3>${this._esc(title)}</h3><p data-no-i18n>${this._esc(value || '未提供')}</p></section>`; },
  _openPaper(index) {
    const p=this.data.papers[index];if(!p)return;
    const r=this.data.reading[p.paper_id],o=p.overview || {};
    let body=this._paperCitation(p)+`<p class="literature-basis">${r?'全文精读':p.summary_basis==='title'?'仅有题名':p.abstract?'根据摘要整理':'仅有题名'} · ${this._esc(this._fulltextLabel(p))}</p>`;
    if(o.problem && p.summary_basis!=='title')body+=['problem','method','strengths','limitations'].map((k,i)=>this._section(['科学问题','怎么做','优势与适用条件','局限与待核实之处'][i],o[k])).join('');
    else body+=this._section('论文概览',p.summary_zh || '尚未整理概览');
    body+=this._section('与研究方向的关系',p.reason);
    if(r){
      body+=['data','experiments','findings'].map((k,i)=>this._section(['数据','实验与验证','结论'][i],r[k])).join('');
      body+='<h3>原文依据</h3>'+r.evidence.map(e=>`<blockquote class="ideation-evidence"><small>${this._esc(e.location)} · ${e.kind==='inference'?'模型推断':'原文事实'}</small><p data-no-i18n>${this._esc(e.quote)}</p><p data-no-i18n>${this._esc(e.claim)}</p></blockquote>`).join('');
    }
    if(['downloaded','read'].includes(p.fulltext_status))body+=this._link(window.location.origin+'/api/ideation/sessions/'+encodeURIComponent(this.sessionId)+'/papers/'+encodeURIComponent(p.paper_id)+'/fulltext','查看已获取原文');
    if(p.fulltext_url)body+=' '+this._link(p.fulltext_url,'原文来源');
    if(p.fulltext_error)body+=this._section('当前处理结果',p.fulltext_error);
    if((p.fetch_attempts || []).length)body+='<details class="idea-prompt-block"><summary>全文获取记录</summary>'+p.fetch_attempts.map(a=>`<p>${this._link(a.url,this._short(a.url,90))}：${this._esc(a.outcome)}</p>`).join('')+'</details>';
    if(p.abstract)body+=`<details class="idea-prompt-block"><summary>原始摘要</summary><p data-no-i18n>${this._esc(p.abstract)}</p></details>`;
    const editable=!this.busy && !['running','pending','stopping','handoff'].includes(this.data.status) && !this.data.run_id;
    const dialog=this._showDialog(p.title,body,`<button class="proj-btn" data-dialog-close>返回文献卡片</button>${!this.data.run_id?`<button class="proj-btn literature-remove" id="paper-remove" ${editable?'':'disabled title="停止调研后可删除"'}>从相关文献中删除</button>`:''}`);
    dialog.querySelector('#paper-remove')?.addEventListener('click',()=>{if(!editable)return;this._closeDialog(false);this._action('papers/'+encodeURIComponent(p.paper_id)+'/remove',{});});
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
      dialog.querySelector('.idea-dialog-body').innerHTML=`<p class="idea-evidence-note">配置模型：${this._esc(info.model)}。先检索、审文献，再精读、构思、查重和审创新点。</p>`+Object.entries(info.stages).map(([key,text])=>`<details class="idea-prompt-block"><summary>${this._esc(({plan:'方向理解与检索规划',screen:'文献相关性与论文概览',summarize:'科学问题、方法、优势与局限',neighbors:'最近邻召回筛选',read:'全文精读',synthesis:'领域现状与缺口',generate:'候选机制生成',audit:'独立最近邻审查'})[key])} · 输出预算 ${info.output_tokens[key]}</summary><pre class="proj-pre" data-no-i18n>${this._esc(text)}</pre></details>`).join('');
    }catch(e){if(this._dialog===dialog)dialog.querySelector('.idea-dialog-body').textContent='读取失败：'+e.message;}
  },
};
window.IdeationView=IdeationView;
