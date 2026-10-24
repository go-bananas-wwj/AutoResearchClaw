/** Two-review, evidence-first research intake. Sessions survive refresh/navigation. */
const IdeationView = {
  host: null, data: null, sessionId: null, busy: false,
  _timer: null, _token: 0, _signature: '', _dialog: null, _returnFocus: null, _listener: null, _error: '',
  phaseLabel(p) { return ({retrieval:'检索与筛选',literature_review:'待确认研究方向',reading:'原文证据核对',synthesis:'问题定位',generate:'构思创新点',audit:'最近邻查重与审查',idea_review:'待审核创新点',handed_off:'已交接自动科研'})[p] || p; },
  statusLabel(s) { return ({pending:'待执行',running:'进行中',handoff:'正在交接科研',waiting:'等待审核',blocked:'已暂停',interrupted:'已中断',stopped:'已停止',stopping:'停止中',completed:'已完成'})[s] || s; },
  stateLabel(d) { return d.status==='blocked' && d.blocker ? ({technical:'调用待重试',evidence:'证据待补充',scientific:'方案待调整',budget:'预算待调整'})[d.blocker.category] || '已暂停' : this.statusLabel(d.status); },
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
      const signature=JSON.stringify([data.revision,data.phase,data.status,data.blocker,data.refinement_round,data.download_task,data.presentation_task,data.progress,data.progress_message,data.plan,data.papers,data.cards,data.missing,data.reviews,data.warnings,Object.keys(data.reading || {}),data.usage.length]);
      this.data=data;
      if(force || signature!==this._signature) { this._signature=signature; this._paint(); }
    } catch(e) { if(token===this._token) { this._error=e.message; this._paint(); } }
  },
  _paint() {
    if(!this.host) return;
    const inputs={}; this.host.querySelectorAll('textarea,input[type="text"],select').forEach(x=>{inputs[x.id]=x.value;});
    const focus=document.activeElement && document.activeElement.id;
    const selection=document.activeElement && document.activeElement.selectionStart;
    const top=this.host.scrollTop;
    const d=this.data;
    this.host.dataset.revision=String(d && d.revision);
    this.host.innerHTML=`<div class="ideation-workspace">
      <div class="ideation-heading"><div><span class="idea-step">研究准备</span><h2>文献调研与方向确认</h2></div><button class="idea-text-btn" data-action="prompt">生成方式与提示词</button></div>
      <p class="proj-welcome-sub">智能体先调研相关文献，你确认方向后，它会比较论文、核对依据并寻找创新点。</p>
      <div class="ideation-steps"><span class="${!d || ['retrieval','literature_review'].includes(d.phase)?'active':''}">1 · 确认文献方向</span><span class="${d && ['reading','synthesis','generate','audit','idea_review'].includes(d.phase)?'active':''}">${d?.phase==='idea_review'?'2 · 确认创新点':'2 · 创新点准备与审查'}</span><span class="${d && d.run_id?'active':''}">3 · 自动科研与论文</span></div>
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
    <div class="ideation-limit"><label for="idea-paper-limit">调研文献上限</label><select id="idea-paper-limit" class="gate-input"><option value="10">10 篇</option><option value="20" selected>20 篇</option><option value="30">30 篇</option></select></div>
    <button class="proj-btn primary" data-action="start" ${this.busy?'disabled':''}>${this.busy?'正在创建调研…':'开始调研'}</button>
    <p class="idea-evidence-note">先找到相关文献，再请你确认研究方向。</p></div>`; },
  _sessionHtml(d) {
    const active=['running','pending','stopping'].includes(d.status), locked=active || this.busy || d.status==='handoff' || !!d.run_id;
    return `<div class="card ideation-brief">
      <div class="ideation-progress"><strong>调研进度</strong><span class="proj-badge">${this._esc(d.run_id?'已交接自动科研':this.stateLabel(d))}</span>
      ${active?`<button class="proj-btn" data-action="stop" ${d.status==='stopping'?'disabled':''}>停止调研</button>`:''}
      ${['blocked','stopped','interrupted'].includes(d.status) && d.blocker?.category!=='scientific' && (!['literature_review','idea_review'].includes(d.phase) || d.download_task)?'<button class="proj-btn" data-action="resume">继续调研</button>':''}</div>
      ${this._progressHtml(d,active)}
      <dl class="ideation-brief-fields"><div><dt>用户输入</dt><dd data-no-i18n>${this._esc(d.direction)}</dd></div>
      <div><dt>研究对象</dt><dd data-no-i18n>${this._esc(this._researchObject(d))}</dd></div></dl>
      ${!active && (d.missing || []).length?this._blockerHtml(d):''}
      ${d.run_id?`<p>科研任务已开始：<button class="idea-text-btn" data-action="project">打开科研项目</button></p>`:''}</div>
      ${!locked?`<details class="card" id="ideation-scope-editor"><summary>调整方向或补充材料</summary><div class="ideation-supplement">
        <input id="lit-reference" class="gate-input" type="text" placeholder="论文 DOI 或公开 URL" /><button class="proj-btn" data-action="add-paper">补充论文</button>
        <label class="proj-btn">上传 PDF<input id="lit-upload" type="file" accept="application/pdf,.pdf" hidden /></label>
        <textarea id="lit-scope" class="gate-textarea" rows="2">${this._esc(d.direction)}</textarea><button class="proj-btn" data-action="scope">按这个方向重新调研</button>
</div></details>`:''}
      ${d.papers.length?this._papersHtml(d):''}
      ${d.phase==='literature_review' && !locked?`<div class="card ideation-direction-confirm"><h3>这些文献的研究方向符合你的想法吗？</h3>
        <div class="ideation-confirm-actions"><button class="proj-btn primary" data-action="confirm-direction" ${d.curation_version!==1?'disabled title="请先整理全文文献"':''}>是这个方向，继续寻找创新点</button><button class="proj-btn" data-action="adjust-direction">调整研究方向</button></div>
        <details class="ideation-optional-feedback"><summary>补充关注点</summary><textarea id="lit-feedback" class="gate-textarea" rows="2" placeholder="例如：重点关注的方法、验证问题或数据条件"></textarea></details></div>`:''}

      ${d.synthesis?this._landscapeHtml(d):''}
      ${d.cards.length?this._cardsHtml(d):''}
      ${!active && (d.rejected_cards || []).length?`<details class="card"><summary>未通过审查的候选（${d.rejected_cards.length}）</summary>${d.rejected_cards.map(c=>`<div class="idea-detail-section"><h3>${this._esc(c.title)}</h3><p>${this._esc(c.rejection_reason)}</p></div>`).join('')}</details>`:''}
      ${d.approved_revision===d.revision && !locked && !['reading','synthesis'].includes(d.phase)?`<div class="card"><h3>修改意见与重新生成</h3><textarea id="idea-feedback" class="gate-textarea" rows="3" placeholder="例如：调整核心假设、验证方式或数据要求。"></textarea><button class="proj-btn" data-action="regenerate">基于已批准文献重新生成</button></div>`:''}
      ${!active?this._warningsHtml(d):''}`;

  },
  _issueHtml(context) {
    const e=this._esc.bind(this),c=context || {};
    return `<article class="ideation-source-issue">${c.paper_title?`<h4 data-no-i18n>${e(c.paper_title)}</h4><p>${this._link(c.doi?'https://doi.org/'+c.doi:c.url,'论文来源')}${c.doi?` · DOI ${e(c.doi)}`:''}</p>`:''}
      ${c.candidate_title?`<p data-no-i18n><strong>受影响方案</strong>${e(c.candidate_title)}</p>`:''}
      ${c.operation || c.reason?`<p data-no-i18n><strong>${e(c.operation || '处理结果')}</strong>${e(c.reason || '')}</p>`:''}
      ${c.impact?`<p>${e(c.impact)}</p>`:''}
      ${(c.attempts || []).length?`<details><summary>已尝试的来源（${c.attempts.length}）</summary><ul>${c.attempts.map(a=>`<li>${this._link(a.url,a.url)}<span>${e(a.outcome)}</span></li>`).join('')}</ul></details>`:''}</article>`;
  },
  _blockerHtml(d) {
    const c=d.blocker?.context;
    return `<div class="ideation-alert" role="alert">${c?.paper_title?`<p><strong>有一篇论文的${this._esc(c.operation || '处理')}尚未完成</strong></p>${this._issueHtml(c)}`:(d.missing || []).map(m=>`<p>${this._esc(m)}</p>`).join('')}</div>`;
  },
  _warningsHtml(d) {
    const warnings=d.warnings || [];
    if(!warnings.length)return '';
    const grouped=new Map();
    warnings.forEach(w=>{const c=w.context || {reason:w.message},key=c.paper_id?c.paper_id+':'+c.operation:w.warning_id;
      if(!grouped.has(key))grouped.set(key,{...c,candidates:new Set()});
      if(c.candidate_title)grouped.get(key).candidates.add(c.candidate_title);
    });
    return `<details class="card ideation-warning-summary"><summary>调研处理记录 · ${grouped.size} 项</summary><p>以下问题已记录，其余文献和候选的处理结果见上方。</p>${[...grouped.values()].map(c=>this._issueHtml({...c,candidate_title:[...c.candidates].join('；')})).join('')}</details>`;
  },
  _progressDetails(d) {
    const p=d.progress || {},op=p.operation;
    let line='',detail='';
    if(d.phase==='retrieval' && ['plan','search','screen'].includes(op)) {
      if(op==='plan')line='正在理解研究方向与规划检索';
      if(op==='search')line=p.unit==='检索式' && p.total?`检索式 ${p.current} / ${p.total}`:'正在检索候选文献';
      if(op==='screen') {
        const done=d.papers.filter(x=>x.relevance && x.relevance!=='unreviewed').length;
        line=`文献筛选：${done} / ${d.papers.length} 篇`;
        detail='只展示可阅读全文的相关文献；确认方向后比较论文并寻找创新点。';
      }
    } else if(p.unit==='候选' && p.total)line=`候选 ${p.current} / ${p.total}`;
    else if(op==='download' && p.total)line=`正在获取第 ${p.current} / ${p.total} 篇原文`;
    else if(op==='read' && p.part_total>1)line=`正在自动阅读本篇第 ${p.part_current} / ${p.part_total} 段`;
    else if(op==='read' && p.total)line=`正在核对第 ${p.current} / ${p.total} 篇的原文依据`;
    else if(op==='summarize')line=p.total?`DeepSeek 正在总结第 ${p.current} / ${p.total} 篇论文`:'DeepSeek 正在撰写论文概览';
    if(op==='download' || d.phase==='literature_review') {
      const available=d.papers.filter(x=>['downloaded','read'].includes(x.fulltext_status)).length;
      detail=`${available} 篇原文已获取`;
    }
    if(d.approved_revision!=null && ['reading','synthesis','generate','audit','idea_review'].includes(d.phase))
      detail+=(detail?' · ':'')+`${Object.keys(d.reading || {}).length} 篇已核对原文依据`;
    return {line,title:p.title || '',detail};
  },
  _journeySteps(d) {
    const p=d.progress || {},op=p.operation,approved=d.approved_revision===d.revision;
    const phaseIndex={retrieval:0,literature_review:3,reading:4,synthesis:5,generate:6,audit:7,idea_review:8,handed_off:9};
    let base=phaseIndex[d.phase]??0;
    if(d.phase==='retrieval')base=['download','summarize','read'].includes(op)?2:op==='plan' || !d.plan?0:1;
    if(d.status==='handoff' || d.run_id)base=9;
    const current=(d.presentation_task==='summaries' || d.download_task) && !d.run_id?2:base;
    const papers=(d.papers || []).filter(p=>!p.user_excluded && p.in_shortlist!==false && ['core','adjacent'].includes(p.relevance) && p.summary_basis==='fulltext' && p.fulltext_verified!==false && ['downloaded','read'].includes(p.fulltext_status));
    const notes=Object.values(d.reading || {}),primary=notes.filter(n=>!n.supplemental).length;
    const round=(d.refinement_round || 0)+1,repeat=round>1 && approved;
    const active=d.status==='running';
    const steps=[
      {id:'direction',title:'理解研究方向',done:'已明确研究对象与检索范围',next:'根据你的描述明确研究对象'},
      {id:'search',title:'检索与筛选文献',done:'已完成相关文献检索与初步筛选',next:'检索经典与近期工作，筛选相关文献'},
      {id:'overview',title:'阅读全文并整理文献',done:papers.length?`${papers.length} 篇全文解读已整理`:'已整理相关文献的全文解读',next:'获取可读全文，逐篇整理科学问题与方法'},
      {id:'literature_review',title:'确认文献方向',human:true,done:'你已确认文献符合研究方向',next:'请核对下方文献是否符合你的方向'},
      {id:'reading',title:'比较论文、核对原文证据',done:primary?`已完成 ${primary} 篇主要文献的证据核对`:'已完成主要文献的证据核对',next:'核对方法、数据和结论的原文依据'},
      {id:'synthesis',title:'梳理现状与研究缺口',done:'已整理研究现状、方法差异与待验证问题',next:'比较已有方法，定位有依据的研究问题'},
      {id:'generate',title:'构思创新点',done:'已形成候选研究方案',next:'基于文献证据构思可验证的创新点'},
      {id:'audit',title:'查重与审查创新点',done:d.cards?.length?`${d.cards.length} 个方案通过本轮审查`:'已完成候选方案的近邻比较与审查',next:'检索近邻工作，核对方法差异与实验逻辑'},
      {id:'idea_review',title:'确认创新点',human:true,done:'你已选择并提交研究方案',next:'查看创新点卡片，选择要推进的方案'},
      {id:'research',title:'自动实验与中文论文',next:'确认方案后，自动推进实验与中文论文'}
    ];
    return steps.map((step,i)=>{
      let state=i===current?'current':i<base?'done':'future';
      // A direction approval is server-owned; viewing this route never approves it.
      if(i===3 && !approved && base<9 && i!==current)state='future';
      const again=state==='future' && repeat && i>=5 && i<=7;
      let status=state==='done'?(step.human?'已确认':'已完成'):again?'待再次执行':step.human?'待你确认':'待开始';
      if(state==='current')status=d.run_id?'已交接':d.status==='handoff'?'交接中':d.status==='stopping'?'停止中':step.human && d.status==='waiting'?'等待你确认':active?'正在进行':this.stateLabel(d);
      return {...step,state,status,again,index:i,round:i>=5 && i<=7?round:null,
        description:state==='done'?step.done:state==='current'?(d.run_id?'已创建科研项目并交接后续工作':step.next):again?'上一轮已完成，本轮将再次执行':''};
    });
  },
  _progressHtml(d,active) {
    const e=this._esc.bind(this),steps=this._journeySteps(d),live=this._progressDetails(d);
    const current=steps.find(s=>s.state==='current');
    const next=steps.find(s=>s.index>(current?.index??-1) && s.state==='future');
    const rows=steps.map(s=>{
      const here=s.state==='current',human=s.human;
      const icon=s.state==='done'?'✓':human?'你':s.index+1;
      const showLive=here && !human && d.status!=='handoff' && !d.run_id;
      const message=showLive?(d.progress_message || d.progress?.message || ''):'';
      return `<li class="journey-station is-${s.state}${s.again?' is-repeat':''}${here && d.status==='running'?' is-running':''}" data-step="${s.id}"${here?' aria-current="step"':''}>
        <span class="journey-marker" aria-hidden="true">${icon}</span><div class="journey-station-body">
          <div class="journey-station-heading"><h4>${e(s.title)}</h4>${human?'<span class="journey-human">由你确认</span>':''}${s===next?'<span class="journey-next">下一步</span>':''}<span class="journey-station-status">${e(s.status)}</span></div>
          ${s.description?`<p class="journey-description">${e(s.description)}</p>`:''}
          ${here && s.round>1?`<span class="journey-round">第 ${s.round} 轮构思与审查</span>`:''}
          ${here?`<div class="journey-current-detail" role="status" aria-live="polite" aria-atomic="true">${message?`<p>${e(active?message:'上次进度：'+message)}</p>`:''}${showLive && live.line?`<p class="journey-current-count">${e(live.line)}</p>`:''}${showLive && live.title?`<p class="journey-current-paper" data-no-i18n>${e(live.title)}</p>`:''}${showLive && live.detail?`<p class="journey-current-evidence">${e(live.detail)}</p>`:''}${d.run_id?'<p>后续实验与论文进度请在科研项目中查看。</p>':''}</div>`:''}
        </div></li>`;
    }).join('');
    const history=(d.reviews || []).filter(r=>r.type==='auto_refine').slice(-(d.refinement_round || 0));
    return `<section class="ideation-journey" aria-label="完整调研流程"><div class="journey-heading"><h3>从研究方向到中文论文</h3><span>${current?`当前：${e(current.title)}`:'调研流程'}</span></div>
      <ol class="journey-stations" aria-label="调研步骤">${rows}</ol>
      ${d.refinement_round && history.length?`<details class="journey-history"><summary>已完成的构思与审查轮次（${history.length}）</summary><ol>${history.map((r,i)=>`<li>第 ${i+1} 轮：已完成构思与审查，进入下一轮修订${(r.cards || []).length+(r.rejected_cards || []).length?` · ${(r.cards || []).length+(r.rejected_cards || []).length} 个候选`:''}</li>`).join('')}</ol></details>`:''}</section>`;
  },
  _paperLimit(d) { return d.paper_limit || d.limits?.paper_limit || (d.approved_revision!=null?Math.max(20,d.papers.filter(p=>p.selected).length):20); },
  _landscapeHtml(d) {
    const s=d.synthesis,reading=d.reading || {},e=this._esc.bind(this);
    const paperLink=id=>{
      const index=d.papers.findIndex(p=>p.paper_id===id),note=reading[id];
      if(index>=0)return `<button class="landscape-paper" data-action="paper" data-index="${index}">${e(d.papers[index].title)}</button>`;
      return this._link(note?.source_url,note?.title || '补充文献');
    };
    const sections=Array.isArray(s.sections)?s.sections:[];
    const comparison=s.method_comparison || [],gaps=s.gaps || [];
    const cards=sections.map((section,i)=>`<article class="landscape-route"><span class="landscape-number">${i+1}</span><h4>${e(section.title)}</h4><p data-no-i18n>${e(section.summary)}</p><div class="landscape-sources">${(section.paper_ids || []).map(paperLink).join('')}</div></article>`).join('');
    const rows=comparison.map(item=>`<tr><th scope="row">${paperLink(item.paper_id)}</th><td data-no-i18n>${e(item.method)}</td><td data-no-i18n>${e(item.boundary)}</td></tr>`).join('');
    const evidence={};Object.values(reading).forEach(note=>(note.evidence || []).forEach(item=>{evidence[item.passage_id]=note.paper_id;}));
    const gapCards=gaps.map((gap,i)=>{const ids=[...new Set((gap.evidence_refs || []).map(ref=>evidence[ref]).filter(Boolean))];return `<article class="landscape-gap"><h4><span class="landscape-number">${i+1}</span>待验证的问题</h4><p data-no-i18n>${e(gap.description)}</p>${gap.inference?`<p class="landscape-inference" data-no-i18n><strong>推断与边界</strong>${e(gap.inference)}</p>`:''}<div class="landscape-sources">${ids.map(paperLink).join('')}</div></article>`;}).join('');
    return `<section class="card ideation-landscape-panel"><div class="landscape-heading"><div><span class="landscape-eyebrow">文献综合</span><h3>领域现状与问题定位</h3></div><span class="proj-badge">${comparison.length} 篇方法对照 · ${gaps.length} 个待验证问题</span></div>
      ${s.overview_zh?`<p class="landscape-overview" data-no-i18n>${e(s.overview_zh)}</p>`:''}
      ${cards?`<div class="landscape-block"><h3>研究路线</h3><div class="landscape-grid">${cards}</div></div>`:''}
      ${rows?`<div class="landscape-block"><h3>方法对照</h3><div class="landscape-table-wrap"><table class="landscape-table"><thead><tr><th scope="col">论文</th><th scope="col">主要做法</th><th scope="col">适用范围与不足</th></tr></thead><tbody>${rows}</tbody></table></div></div>`:''}
      ${(s.takeaways || []).length?`<div class="landscape-block landscape-takeaways"><h3>关键认识</h3><ul>${s.takeaways.map(x=>`<li data-no-i18n>${e(x)}</li>`).join('')}</ul></div>`:''}
      ${gapCards?`<div class="landscape-block"><h3>还需要验证什么</h3><div class="landscape-grid">${gapCards}</div></div>`:''}
      <details class="landscape-original"><summary>完整分析记录</summary><div class="ideation-landscape" data-no-i18n>${String(s.landscape_zh || '').split(/\n\s*\n|(?=第[一二三四五六七八九十]+条|总体)/).filter(x=>x.trim()).map(x=>`<p>${e(x.trim())}</p>`).join('')}</div></details></section>`;
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
    const relevant=d.papers.map((paper,index)=>({paper,index})).filter(x=>['core','adjacent'].includes(x.paper.relevance) && !x.paper.user_excluded && x.paper.in_shortlist!==false && ['downloaded','read'].includes(x.paper.fulltext_status) && x.paper.summary_basis==='fulltext' && x.paper.fulltext_verified!==false).sort((a,b)=>(rank[a.paper.relevance]??2)-(rank[b.paper.relevance]??2)).slice(0,this._paperLimit(d));
    const size=6,pages=Math.max(1,Math.ceil(relevant.length/size));
    this._paperPage=Math.min(this._paperPage || 0,pages-1);
    const cards=relevant.slice(this._paperPage*size,(this._paperPage+1)*size).map(({paper:p,index:i})=>{
      const o=p.overview || {},r=d.reading[p.paper_id];
      const basis=r?'原文依据已核对':'全文解读';
      const state=this._fulltextLabel(p);
      return `<button class="literature-tile" data-action="paper" data-index="${i}" aria-haspopup="dialog">
        <span class="literature-tile-top"><span>${this._esc(p.year || '年份未提供')}</span><span>${this._esc(basis)}</span></span>
        <h3 data-no-i18n>${this._esc(p.title)}</h3><p class="literature-tile-venue" data-no-i18n>${this._esc(p.venue || '期刊/会议未提供')}</p>
        <div class="literature-tile-overview" data-no-i18n><strong>提出的科学问题</strong><p>${this._esc(this._short(o.problem || p.summary_zh || '等待AI读取材料后总结',100))}</p><strong>方法</strong><p>${this._esc(this._short(o.method || '暂无总结',90))}</p></div>
        <span class="literature-tile-bottom"><span>${this._esc(state)}</span><span>查看详情 ↗</span></span></button>`;
    }).join('');
    const updatable=!this.busy && !['running','pending','stopping','handoff'].includes(d.status) && !d.run_id;
    return `<section class="card literature-gallery"><div class="literature-gallery-head"><h3>相关文献 · ${relevant.length} 篇（上限 ${this._paperLimit(d)} 篇）</h3>${updatable && d.phase!=='retrieval'?'<button class="idea-text-btn" data-action="fulltexts">整理全文文献</button>':''}${updatable && d.phase!=='retrieval' && relevant.some(x=>x.paper.summary_version!==6)?'<button class="idea-text-btn" data-action="summaries">更新论文概览</button>':''}</div>${this._retrievalInfoHtml(d,relevant)}<div class="literature-grid">${cards || (['running','pending','stopping'].includes(d.status)?'<p>正在阅读和筛选全文，完成后展示文献卡片。</p>':'<p>暂无通过全文筛选的论文，可整理全文文献、调整方向或补充 PDF。</p>')}</div><nav class="literature-pagination" aria-label="文献翻页"><button class="proj-btn" data-action="papers-prev" ${this._paperPage===0?'disabled':''}>← 上一页</button><span aria-live="polite">${this._paperPage+1} / ${pages}</span><button class="proj-btn" data-action="papers-next" ${this._paperPage===pages-1?'disabled':''}>下一页 →</button></nav></section>`;
  },
  _retrievalInfoHtml(d,relevant) {
    const logs=d.search_log || [],queries=d.plan?.queries || [],years=relevant.map(x=>Number(x.paper.year)).filter(y=>y>0);
    const names={openalex:'OpenAlex',semantic_scholar:'Semantic Scholar',arxiv:'arXiv',crossref:'Crossref'};
    const sources=[...new Set(logs.filter(l=>l.count>0).map(l=>names[l.source] || l.source))];
    const dates=years.length?`${Math.min(...years)}—${Math.max(...years)} 年`:'年份未提供';
    return `<details class="idea-prompt-block literature-retrieval-info"><summary>这些文献是怎么找到的？</summary><p>本轮 ${queries.length} 组检索式，${d.papers.length} 篇候选，入选 ${relevant.length} 篇；数量上限 ${this._paperLimit(d)} 篇。入选年份：${dates}。</p><p>取得结果的来源：${this._esc(sources.join('、') || '尚无结果')}。</p><p>${d.retrieval_strategy_version>=2?'经典方法、领域综述与近期进展并行检索，再追踪关键论文的参考文献和后续引用。':'本轮依据下列既有检索记录；新建或重新调研会增加经典检索与前后向引用追踪。'}相关性和全文的方法、验证依据决定是否入选，引用量和年份用于兼顾经典与近期覆盖。</p><ul>${queries.map(q=>`<li data-no-i18n>${this._esc(q.query)} · ${this._esc(({classic:'经典检索，不限起始年份',recent:'近期检索',all:'不限年份检索'})[q.period] || q.period)}</li>`).join('')}</ul></details>`;
  },
  _fulltextLabel(p) {
    if(p.fulltext_status==='read')return '原文依据已核对';
    if(p.fulltext_status==='downloaded')return p.summary_basis==='fulltext' && p.summary_state==='ready'?'已读全文 · 概览完成':'全文已获取';
    return '未入选：未取得全文';
  },
  _cardsHtml(d) { return `<div class="card"><h3>第二轮：确认创新点</h3><p class="idea-evidence-note">查看方案的方法、文献依据与验证实验，选择你想推进的创新点。</p></div><div class="idea-cards">${d.cards.map((c,i)=>`<button class="proj-big-card idea-card" data-action="card" data-index="${i}" aria-haspopup="dialog"><span class="idea-card-top">候选 ${i+1}<span>查看详情</span></span><span class="proj-big-card-title idea-card-title" data-no-i18n>${this._esc(c.title)}</span><span class="idea-card-summary" data-no-i18n>${this._esc(this._short(c.one_liner))}</span><span class="idea-card-more">方法 · 最近邻差异 · 原文依据 · 实验设计</span></button>`).join('')}</div>`; },

  async _action(name,body) {
    if(this.busy) return; this.busy=true; this._error='';
    this._pending=({'direction-confirm':'方向已提交，正在安排论文比较与原文证据核对…',resume:'正在恢复调研，从最近完成点继续…',stop:'正在请求停止，等待当前操作结束…',fulltexts:'正在筛选和解读可获取全文的论文…',summaries:'正在更新科学问题、方法、优势与局限…'})[name] || '正在保存并更新调研…';
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
      try { const data=await API.createIdeation(direction,Number(document.getElementById('idea-paper-limit').value));if(token===this._token)App.openIdeation(data.session_id); }
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
    let body=this._paperCitation(p)+`<p class="literature-basis">${r?'已完成跨论文分析所需的原文核对':'DeepSeek 已阅读原文并整理概览'} · ${this._esc(this._fulltextLabel(p))}</p>`;
    if(o.problem && p.summary_basis!=='title') {
      body+=this._section('提出的科学问题',o.problem)+this._section('研究意义与研究现状',o.background)+this._section('创新点',o.innovation);
      body+=`<section class="idea-detail-section"><h3>方法</h3><p data-no-i18n>${this._esc(o.method || '尚未更新全文解读')}</p><h4>数据与数据集</h4><p data-no-i18n>${this._esc(o.data || '尚未更新全文解读')}</p><h4>验证与效果</h4><p data-no-i18n>${this._esc(o.results || '尚未更新全文解读')}</p></section>`;
      body+=`<section class="idea-detail-section"><h3>方法的优点和缺点</h3><h4>优点</h4><p data-no-i18n>${this._esc(o.strengths || '尚未更新全文解读')}</p><h4>缺点与适用边界</h4><p data-no-i18n>${this._esc(o.limitations || '尚未更新全文解读')}</p></section>`;
    } else body+=this._section('论文概览',p.summary_zh || '尚未整理概览');
    body+=this._section('为什么推荐这篇',p.selection_reason || p.reason);
    if(!r)body+='<p class="idea-evidence-note">已根据全文生成解读；确认方向后，将比较不同论文并逐条核对创新点依据。</p>';
    if(r){
      body+='<details class="idea-prompt-block"><summary>深入阅读记录</summary>'+['data','experiments','findings'].map((k,i)=>this._section(['数据','实验与验证','结论'][i],r[k])).join('')+'</details>';
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
      dialog.querySelector('.idea-dialog-body').innerHTML=`<p class="idea-evidence-note">配置模型：${this._esc(info.model)}。先读全文并确认方向，再比较论文、核对依据和审创新点。</p>`+Object.entries(info.stages).map(([key,text])=>`<details class="idea-prompt-block"><summary>${this._esc(({plan:'方向理解与检索规划',screen:'文献相关性筛选',summarize:'问题、现状、创新、方法与优缺点',neighbors:'最近邻召回筛选',read:'单篇全文阅读与证据核对',read_part:'同篇长论文分段阅读',read_merge:'同篇分段记录汇总',synthesis:'领域现状与缺口',generate:'候选机制生成',audit:'独立最近邻审查'})[key])} · 输出预算 ${info.output_tokens[key]}</summary><pre class="proj-pre" data-no-i18n>${this._esc(text)}</pre></details>`).join('');
    }catch(e){if(this._dialog===dialog)dialog.querySelector('.idea-dialog-body').textContent='读取失败：'+e.message;}
  },
};
window.IdeationView=IdeationView;
