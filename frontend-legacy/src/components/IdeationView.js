/** Two-review, evidence-first research intake. Sessions survive refresh/navigation. */
const IdeationView = {
  host: null, data: null, sessionId: null, busy: false,
  _timer: null, _token: 0, _signature: '', _dialog: null, _returnFocus: null, _listener: null, _error: '',
  phaseLabel(p) { return ({retrieval:'检索与筛选',literature_review:'待确认研究方向',reading:'原文证据核对',synthesis:'问题定位',generate:'构思创新点',audit:'检查相近研究',idea_review:'待选择创新点',handed_off:'已交接自动科研'})[p] || p; },
  statusLabel(s) { return ({pending:'待执行',running:'进行中',handoff:'正在交接科研',waiting:'等待审核',blocked:'已暂停',interrupted:'已中断',stopped:'已停止',stopping:'停止中',completed:'已完成',needs_work:'待完善',idea_ready:'可选择',validated:'审查通过'})[s] || s; },
  stateLabel(d) { return d.status==='blocked' && d.blocker ? ({technical:'调用待重试',evidence:'证据待补充',scientific:'方案待调整',budget:'预算待调整'})[d.blocker.category] || '已暂停' : this.statusLabel(d.status); },
  _esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
  _link(url, label) { return /^https?:\/\//i.test(url || '') ? `<a href="${this._esc(url)}" target="_blank" rel="noopener noreferrer">${this._esc(label || '原文链接')}</a>` : ''; },
  _short(s,n=110) { const a=Array.from(String(s || '').replace(/\s+/g,' ').trim()); return a.slice(0,n).join('')+(a.length>n?'…':''); },

  render(host, opts={}) {
    this.destroy(); this.host=host; this.data=null; this.sessionId=opts.sessionId || null;
    this.busy=false; this._bookmark=null; this._error=''; this._signature=''; this._paperPage=0; this._pending='';
    this._listener=e=>{ const button=e.target.closest('[data-action]'); if(button && !button.disabled) this._act(button.dataset.action,button.dataset.index); };
    host.addEventListener('click',this._listener);
    if(this.sessionId) { this.refresh(); this._timer=setInterval(()=>this.refresh(),4000); }
    else this._paint();
  },
  destroy() {
    this._token++; clearInterval(this._timer); this._timer=null; this._closeDialog(false);
    const slot=typeof document==='undefined'?null:document.getElementById('ideation-header-nav');
    if(slot){slot.hidden=true;slot.innerHTML='';if(this._headerListener)slot.removeEventListener('click',this._headerListener);slot.closest('.header')?.classList.remove('ideation-header');slot.closest('.app')?.classList.remove('has-ideation-header');}
    this._headerListener=null;
    if(this.host && this._listener) this.host.removeEventListener('click',this._listener);
    this.host=null;
  },
  async refresh(force=false) {
    if(!this.sessionId || !this.host || this.busy) return;
    const token=this._token;
    try {
      const data=await API.ideationSession(this.sessionId);
      if(token!==this._token || !this.host) return;
      const signature=JSON.stringify([data.revision,data.phase,data.status,data.blocker,data.refinement_round,data.download_task,data.presentation_task,data.progress,data.progress_message,data.plan,data.papers,data.cards,data.idea_pool,data.ideaspark_batch,data.missing,data.reviews,data.warnings,Object.keys(data.reading || {}),data.usage.length]);
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
      <div class="ideation-steps"><span class="${!d || ['retrieval','literature_review'].includes(d.phase)?'active':''}">1 · 确认文献方向</span><span class="${d && ['reading','synthesis','generate','audit','idea_review'].includes(d.phase)?'active':''}">${d?.phase==='idea_review'?'2 · 确认创新点':'2 · 构思与相近研究检查'}</span><span class="${d && d.run_id?'active':''}">3 · 自动科研与论文</span></div>
      ${d?this._statusStripHtml(d):''}
      ${this._pending?`<div class="card literature-live" role="status" aria-live="polite"><span class="literature-spinner" aria-hidden="true"></span>${this._esc(this._pending)}</div>`:''}
      ${this._error?`<p class="ideation-alert" role="alert">${this._esc(this._error)}</p>`:''}
      ${!this.sessionId?this._newHtml():!d?'<p role="status">正在读取调研会话…</p>':this._sessionHtml(d)}
      </div>`;
    this._renderHeaderBookmarks(d);
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
    return `<div class="card ideation-route-card" id="ideation-workflow" data-material-stage="workflow">${this._progressHtml(d,active)}</div>
      <div class="card ideation-brief" id="ideation-status" data-material-stage="latest-status">
      <div class="ideation-progress"><strong>最新进展</strong><span class="proj-badge">${this._esc(d.run_id?'已交接自动科研':this.stateLabel(d))}</span>
      ${active?`<button class="proj-btn" data-action="stop" ${d.status==='stopping'?'disabled':''}>停止调研</button>`:''}
      ${['blocked','stopped','interrupted'].includes(d.status) && d.blocker?.category!=='scientific' && (!['literature_review','idea_review'].includes(d.phase) || d.download_task)?`<button class="proj-btn" data-action="resume">${d.blocker?.category==='technical'?'重试未完成的审查':'继续调研'}</button>`:''}</div>
      ${active?`<p class="ideation-latest-operation" data-no-i18n>${this._esc(this._operationLabel(d))}</p>`:''}
      ${!active && (d.missing || []).length?this._blockerHtml(d):''}
      ${d.ideaspark_batch?.workflow==='ideas_then_neighbors_v1' && d.phase==='idea_review'?`<p>已整理 ${d.cards.length} 个可选创新点。查看相近研究的比较与待确认事项，选定后再生成具体实验方案。</p>`:''}
      ${d.run_id?`<p>科研任务已开始：<button class="idea-text-btn" data-action="project">打开科研项目</button></p>`:''}</div>
      ${d.ideaspark_batch?.workflow==='ideas_then_neighbors_v1'?this._ideaChoicesHtml(d):(d.cards.length?this._cardsHtml(d):'')+this._ideaPoolHtml(d)}
      ${!active && (d.rejected_cards || []).length?this._reviewResultsHtml(d):''}
      ${d.approved_revision===d.revision && !locked && !['reading','synthesis'].includes(d.phase)?`<section class="card" id="ideation-feedback" data-material-stage="next-action"><h3>继续优化方案</h3><p class="idea-evidence-note">沿用已确认的文献和审查意见，继续构思方案。</p><textarea id="idea-feedback" class="gate-textarea" rows="3" placeholder="可选：指定要优先解决的问题、验证方式或数据条件。"></textarea><button class="proj-btn" data-action="regenerate">${d.cards.length?'修改方案并重新生成':'根据审查意见继续优化'}</button></section>`:''}
      ${d.synthesis?this._landscapeHtml(d):''}
      ${!active?this._warningsHtml(d):''}
      <div class="card" id="ideation-input" data-material-stage="research-input"><dl class="ideation-brief-fields"><div><dt>用户输入</dt><dd data-no-i18n>${this._esc(d.direction)}</dd></div>
      <div><dt>研究对象</dt><dd data-no-i18n>${this._esc(this._researchObject(d))}</dd></div></dl></div>
      ${!locked?`<details class="card" id="ideation-scope-editor"><summary>调整方向或补充材料</summary><div class="ideation-supplement">
        <input id="lit-reference" class="gate-input" type="text" placeholder="论文 DOI 或公开 URL" /><button class="proj-btn" data-action="add-paper">补充论文</button>
        <label class="proj-btn">上传 PDF<input id="lit-upload" type="file" accept="application/pdf,.pdf" hidden /></label>
        <textarea id="lit-scope" class="gate-textarea" rows="2">${this._esc(d.direction)}</textarea><button class="proj-btn" data-action="scope">按这个方向重新调研</button>
</div></details>`:''}
      ${d.phase==='literature_review' && !locked?`<div class="card ideation-direction-confirm"><h3>这些文献的研究方向符合你的想法吗？</h3>
        <div class="ideation-confirm-actions"><button class="proj-btn primary" data-action="confirm-direction" ${d.curation_version!==1?'disabled title="请先整理全文文献"':''}>是这个方向，继续寻找创新点</button><button class="proj-btn" data-action="adjust-direction">调整研究方向</button></div>
        <details class="ideation-optional-feedback"><summary>补充关注点</summary><textarea id="lit-feedback" class="gate-textarea" rows="2" placeholder="例如：重点关注的方法、验证问题或数据条件"></textarea></details></div>`:''}

      ${d.papers.length?this._papersHtml(d):''}`;

  },
  _renderHeaderBookmarks(d) {
    const slot=document.getElementById('ideation-header-nav');if(!slot)return;
    slot.hidden=!d;slot.closest('.header')?.classList.toggle('ideation-header',!!d);slot.closest('.app')?.classList.toggle('has-ideation-header',!!d);
    slot.innerHTML=d?this._bookmarksHtml(d):'';
    if(!this._headerListener){this._headerListener=e=>{const b=e.target.closest('[data-action="jump"]');if(b)this._jumpTo(b.dataset.index);};slot.addEventListener('click',this._headerListener);}
  },
  _bookmarksHtml(d) {
    const items=[['ideation-workflow','完整流程'],['ideation-status','最新进展']];
    if(d.ideaspark_batch?.workflow==='ideas_then_neighbors_v1')items.push(['ideation-approved','创新点']);
    else{
      if(d.cards?.length)items.push(['ideation-approved','可确认方案']);
      if(d.ideaspark_batch || d.idea_pool?.length)items.push(['ideation-candidates','候选卡片']);
    }
    if(d.synthesis)items.push(['ideation-analysis','领域分析']);
    if(d.synthesis?.method_comparison?.length)items.push(['ideation-comparison','方案对照']);
    items.push(['ideation-input','研究方向']);
    if(!this.busy && !['running','pending','stopping','handoff'].includes(d.status) && !d.run_id)items.push(['ideation-scope-editor','调整方向']);
    if(!this.busy && d.approved_revision===d.revision && !['running','pending','stopping','handoff'].includes(d.status) && !d.run_id && !['reading','synthesis'].includes(d.phase))items.push(['ideation-feedback','修改意见']);
    if(d.papers?.length)items.push(['ideation-literature','论文总结']);
    return `<nav class="ideation-bookmarks" aria-label="调研内容书签">${items.map(([id,label])=>`<button type="button" id="ideation-nav-${id}" class="idea-text-btn" data-action="jump" data-index="${id}"${this._bookmark===id?' aria-current="location"':''}>${label}</button>`).join('')}</nav>`;
  },
  _jumpTo(id) {
    if(!/^ideation-[a-z-]+$/.test(id || ''))return;
    const target=this.host?.querySelector('[id="'+id+'"]');if(!target)return;
    if(target.tagName==='DETAILS')target.open=true;
    this._bookmark=id;document.querySelectorAll('#ideation-header-nav .ideation-bookmarks button').forEach(b=>{if(b.dataset.index===id)b.setAttribute('aria-current','location');else b.removeAttribute('aria-current');});
    target.setAttribute('tabindex','-1');target.focus({preventScroll:true});target.scrollIntoView?.({block:'start',behavior:'smooth'});
  },
  _reviewResultsHtml(d) {
    const e=this._esc.bind(this),labels={mechanism_overlap:'审查认为已有工作覆盖',invalid_design:'实验设计或验证条件待完善',insufficient_evidence:'关键证据待落实',fulltext_unavailable:'关键论文获取未完成'};
    return `<section class="card ideation-review-results" data-material-stage="review"><h3>最新一轮候选审查</h3><p class="idea-evidence-note">展开候选查看原始审查意见和待落实条件。</p><ol>${d.rejected_cards.map(c=>{
      const audit=c.audit || {},missing=Array.isArray(audit.missing)?audit.missing:[],issues=Array.isArray(audit.design_check?.issues)?audit.design_check.issues:[],reason=c.rejection_reason || c.failure?.message || '';
      return `<li><details><summary><span data-no-i18n>${e(c.title)}</span><span class="review-verdict">${e(labels[c.failure?.code] || '尚未通过审查')}</span></summary><div class="review-result-body">
        <h4>审查依据</h4><p data-no-i18n>${e(reason)}</p>
        ${missing.length?`<h4>待落实条件</h4><ul>${missing.map(x=>`<li data-no-i18n>${e(x)}</li>`).join('')}</ul>`:''}
        ${issues.length?`<details class="review-design-issues"><summary>实验逻辑检查（${issues.length} 项）</summary><ul>${issues.map(x=>`<li data-no-i18n>${e(x)}</li>`).join('')}</ul></details>`:''}
        ${c.failure?.context?.paper_title?this._issueHtml(c.failure.context):''}</div></details></li>`;
    }).join('')}</ol></section>`;
  },
  _ideaPoolHtml(d) {
    const batch=d.ideaspark_batch,pool=d.idea_pool || [];
    if(!batch && !pool.length)return '';
    const label=c=>this._reviewState(c,d).label;
    const jobs=batch?.jobs || [],finished=jobs.filter(j=>j.status!=='running').length;
    const simple=batch?.workflow==='ideas_then_neighbors_v1';
    const cards=pool.map((c,i)=>({c,i})).filter(x=>x.c.skill_status!=='validated' && !x.c.selection_ready).reverse();
    return `<section class="card ideation-pool" id="ideation-candidates" data-material-stage="idea-pool"><div class="literature-gallery-head"><h3>候选卡片 · ${pool.length} 张</h3>${!['running','pending','stopping','handoff'].includes(d.status) && !d.run_id?'<button class="idea-text-btn" data-action="card-summaries">更新讲解与公式</button>':''}<span class="proj-badge">${finished} / ${jobs.length || batch?.target || 12} 个任务已结束 · ${(d.cards || []).length} 个可确认</span></div>
      <p class="idea-evidence-note">${jobs.length?`本批已分配 ${jobs.length} 个独立构思任务${jobs.length!==(batch?.target || 12)?`（目标 ${batch?.target || 12} 个）`:''}。`:`正在规划独立任务，目标 ${batch?.target || 12} 个。`}${simple?'只生成创新点并检查相近研究；选定后再落实具体技术和实验。':'候选逐张展示，审查通过后可确认启动科研。'}</p>
      <div class="idea-cards">${cards.map(({c,i})=>`<button class="proj-big-card idea-card" data-action="pool-card" data-index="${i}" aria-haspopup="dialog"><span class="idea-card-top">候选 ${i+1}<span>${this._esc(label(c))}</span></span><span class="proj-big-card-title idea-card-title" data-no-i18n>${this._esc(c.title)}</span><span class="idea-card-summary" data-no-i18n>${this._esc(this._short(c.one_liner))}</span><span class="idea-card-more">查看背景、方法与审查进度</span></button>`).join('') || (pool.length?'已通过的方案显示在上方。':'正在进行官方瓶颈诊断与独立构思，生成后逐张显示。')}</div>
      ${jobs.some(j=>j.reason || j.presentation_error)?`<details class="idea-prompt-block"><summary>任务处理记录</summary>${jobs.filter(j=>j.reason || j.presentation_error).map(j=>`<p data-no-i18n>${this._esc(j.focus)}：${this._esc(j.reason || j.presentation_error)}</p>`).join('')}</details>`:''}</section>`;
  },
  _ideaChoicesHtml(d) {
    const e=this._esc.bind(this),ready=d.cards || [],seen=new Set(ready.map(c=>c.card_id));
    const choices=ready.map((c,index)=>({c,index,action:'card'})).concat((d.idea_pool || []).map((c,index)=>({c,index,action:'pool-card'})).filter(({c})=>!seen.has(c.card_id)));
    return `<section class="card ideation-choices" id="ideation-approved"><h3>创新点 · ${choices.length} 个</h3><p class="idea-evidence-note">点一张卡片查看详情，确定后在详情底部点击“启动科研项目”。</p><div class="idea-cards">${choices.map(({c,index,action},i)=>`<button class="proj-big-card idea-card" data-action="${action}" data-index="${index}" aria-haspopup="dialog"><span class="idea-card-top">候选 ${i+1}<span>${e(c.selection_ready?'查看详情':'整理中')}</span></span><span class="proj-big-card-title idea-card-title" data-no-i18n>${e(c.title)}</span><span class="idea-card-summary" data-no-i18n>${e(this._short(c.one_liner))}</span></button>`).join('') || '<p role="status">正在整理创新点，完成后会显示在这里。</p>'}</div></section>`;
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
    if(d.ideaspark_batch && !(d.cards || []).length) {
      const jobs=d.ideaspark_batch.jobs || [],technical=jobs.filter(j=>j.retryable || j.failure_category==='technical').length;
      const changes=jobs.filter(j=>j.status==='needs_work' && !j.retryable && j.failure_category!=='technical').length;
      return `<div class="ideation-review-outcome" role="status"><h3>${(d.idea_pool || []).length} 张草稿已生成，当前 0 个方案可确认</h3>
        <p>${technical?`${technical} 个任务的审查因技术问题中断，不能据此认定想法不成立。`:''}${changes?`${changes} 个方案需按审查意见修改。`:''}实验尚未启动。</p>
        ${technical?'<p>可重试未完成的审查；每张卡片会显示停在哪一步和具体原因。</p>':'<p>打开候选查看修改意见，再调整方案或补充材料。</p>'}</div>`;
    }
    if(!(d.cards || []).length && ['refinement_exhausted','no_new_information','candidate_evidence_incomplete'].includes(d.blocker?.code)) {
      const rejected=d.rejected_cards || [],overlap=rejected.filter(c=>c.failure?.code==='mechanism_overlap').length;
      const design=rejected.filter(c=>c.failure?.code==='invalid_design').length,passed=d.cards?.length || 0;
      const why=d.blocker.code==='refinement_exhausted'?'已达到本次自动修订轮次上限。':d.blocker.code==='no_new_information'?'新一轮仍重复已有方案，没有获得新的依据。':'目前仍缺少支撑候选方案的关键依据。';
      return `<div class="ideation-review-outcome" role="status"><h3>本次构思与审查已结束，暂无通过方案</h3><p>已完成 ${Number(d.refinement_round || 0)+1} 轮构思与审查，最后一轮 ${rejected.length+passed} 个候选，${passed} 个通过。</p>
        <div class="review-outcome-counts">${overlap?`<span>已有工作覆盖 · ${overlap} 个</span>`:''}${design?`<span>实验设计或验证条件待完善 · ${design} 个</span>`:''}</div>
        <p>${why}目前没有可交给你确认的创新点，后续实验尚未启动。</p></div>`;
    }
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
  _operationLabel(d) {
    const raw=d.progress_message || d.progress?.message || this.phaseLabel(d.phase);
    if(!raw.startsWith('IdeaSpark：'))return raw;
    let stage=raw.slice(10),prefix='正在';
    if(stage.startsWith('Prepare version-bound ')){stage=stage.slice(22);prefix='准备';}
    if(stage.startsWith('Commit version-bound ')){stage=stage.slice(21);prefix='校验并保存';}
    const labels={queued_idea_check:'等待检查相近研究',idea_generation:'根据论文总结构思创新点',neighbor_check:'检查相近研究',generation:'独立构思方案',coherence:'核对机制一致性',critique:'审查最近邻与新颖性',formula_repair:'修正机制中的公式',revision:'根据审查意见修订',post_revision:'独立复核修订结果',post_novelty:'复核近邻差异',technical_fill:'完善具体方法',implementability:'检查可实施性与资源',technical_repair:'补全来源信息',technical_review:'复核技术细节',plain_derive:'撰写通俗中文方案',fidelity:'核对方案讲解与原始机制',json_transport_repair:'修复返回数据的JSON格式','Run existing dual-channel collision retrieval':'检索最接近的已有论文（查重）','Materialize canonical candidate and blocking evidence':'保存候选与审查记录','Preserve draft and report blocking findings':'保留草稿与待修改意见'};
    if(stage.endsWith(':execution_binding'))return 'IdeaSpark：正在核对算例的真实执行结果';
    return labels[stage]?'IdeaSpark：'+prefix+labels[stage]:raw;
  },
  _batchSummary(d) {
    const pool=d.idea_pool || [],jobs=d.ideaspark_batch?.jobs || [];
    const parallel=d.ideaspark_batch?.parallel,active=Object.keys(parallel?.active || {}).length;
    return `${active?`正在并行 ${active} 个任务（上限 ${parallel.workers}） · `:''}已生成 ${pool.length} 张候选草稿 · ${(d.cards || []).length} 张${d.ideaspark_batch?.workflow==='ideas_then_neighbors_v1'?'可选择':'通过审查'} · ${jobs.filter(j=>j.status==='needs_work').length} 个任务待完善`;
  },
  _statusStripHtml(d) {
    const running=d.status==='running',waiting=d.status==='waiting';
    const label=running?'正在进行':waiting?'等待你确认':this.stateLabel(d);
    const tasks=Object.entries(d.ideaspark_batch?.parallel?.active || {}).map(([id,t])=>({...t,id})).sort((a,b)=>a.number-b.number);
    const action=running?(d.presentation_task==='cards'?'正在更新卡片的入门背景与公式':tasks.length>1?(d.phase==='audit'?'创新点已生成，正在并行检查相近研究':'IdeaSpark 正在并行构思方案'):this._operationLabel(d)):waiting?(d.phase==='idea_review'?'请查看创新点，选择想推进的方案':'请确认文献是否符合研究方向'):d.run_id?'后续进度请在科研项目中查看':`停在「${this.phaseLabel(d.phase)}」`;
    return `<div class="ideation-current-status" role="status" aria-live="polite"><span class="ideation-status-dot${running?' is-active':''}" aria-hidden="true"></span><div><strong>${this._esc(label)}：${this._esc(action)}</strong>${d.ideaspark_batch?`<p>${this._esc(this._batchSummary(d))}</p><button class="idea-text-btn" data-action="job-output">查看任务详情与实时输出 →</button>`:''}${running && tasks.length?`<ul class="ideation-parallel-tasks">${tasks.map(t=>`<li><button class="idea-text-btn" data-action="job-output" data-index="${this._esc(t.id)}" data-no-i18n>任务 ${this._esc(t.number)}：${this._esc(this._operationLabel({progress_message:t.message || 'IdeaSpark：'+t.stage}))} ↗</button></li>`).join('')}</ul>`:''}</div></div>`;
  },
  _progressDetails(d) {
    const p=d.progress || {},op=p.operation;
    let line='',detail='';
    if(op==='ideaspark') {
      const total=d.ideaspark_batch?.jobs?.length || p.total || d.ideaspark_batch?.target || 12;
      const tasks=Object.values(d.ideaspark_batch?.parallel?.active || {}).sort((a,b)=>a.number-b.number);
      line=tasks.length>1?`当前并行：${tasks.map(t=>t.number).join('、')} 号任务（共 ${total} 个）`:`当前处理：第 ${p.current || 0} 号任务（共 ${total} 个，逐轮复核）`;
      detail=this._batchSummary(d);
    }
    else if(d.phase==='retrieval' && ['plan','search','screen'].includes(op)) {
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
    return {line,title:Object.keys(d.ideaspark_batch?.parallel?.active || {}).length>1?'各任务独立审查，结果完成后逐张更新。':p.title || '',detail};
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
      {id:'audit',title:d.ideaspark_batch?.workflow==='ideas_then_neighbors_v1'?'检查相近研究':'查重与审查创新点',done:d.ideaspark_batch?.workflow==='ideas_then_neighbors_v1'?'已整理相近研究的比较和待确认事项':d.cards?.length?`${d.cards.length} 个方案通过本轮审查`:'已完成候选方案的近邻比较与审查',next:d.ideaspark_batch?.workflow==='ideas_then_neighbors_v1'?'检索相似想法，说明可能重合与差异':'检索近邻工作，核对方法差异与实验逻辑'},
      {id:'idea_review',title:'确认创新点',human:true,done:'你已选择并提交研究方案',next:'查看创新点卡片，选择要推进的方案'},
      {id:'research',title:'实验方案、技术验证与中文论文',next:'选定创新点后，落实具体技术、设计实验并验证'}
    ];
    return steps.map((step,i)=>{
      let state=i===current?'current':i<base?'done':'future';
      // A direction approval is server-owned; viewing this route never approves it.
      if(i===3 && !approved && base<9 && i!==current)state='future';
      const again=state==='future' && repeat && i>=5 && i<=7;
      let status=state==='done'?(step.human?'已确认':'已完成'):again?'待再次执行':step.human?'待你确认':'待开始';
      if(state==='current')status=d.run_id?'已交接':d.status==='handoff'?'交接中':d.status==='stopping'?'停止中':step.human && d.status==='waiting'?'等待你确认':active?'正在进行':this.stateLabel(d);
      if(i===8 && state==='future' && d.status==='blocked' && !(d.cards || []).length)status='方案通过审查后解锁';
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
      const message=showLive?this._operationLabel(d):'';
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
      ${d.refinement_round && history.length?`<details class="journey-history"><summary>已完成的构思与审查轮次（${history.length}）</summary><ol>${history.map((r,i)=>({record:r,round:i+1})).reverse().map(({record:r,round})=>`<li>第 ${round} 轮：已完成构思与审查，进入下一轮修订${(r.cards || []).length+(r.rejected_cards || []).length?` · ${(r.cards || []).length+(r.rejected_cards || []).length} 个候选`:''}</li>`).join('')}</ol></details>`:''}</section>`;
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
    return `<section class="card ideation-landscape-panel" id="ideation-analysis"><div class="landscape-heading"><div><span class="landscape-eyebrow">文献综合</span><h3>领域现状与问题定位</h3></div><span class="proj-badge">${comparison.length} 篇方法对照 · ${gaps.length} 个待验证问题</span></div>
      ${s.overview_zh?`<p class="landscape-overview" data-no-i18n>${e(s.overview_zh)}</p>`:''}
      ${cards?`<div class="landscape-block"><h3>研究路线</h3><div class="landscape-grid">${cards}</div></div>`:''}
      ${rows?`<div class="landscape-block" id="ideation-comparison"><h3>方法对照</h3><div class="landscape-table-wrap"><table class="landscape-table"><thead><tr><th scope="col">论文</th><th scope="col">主要做法</th><th scope="col">适用范围与不足</th></tr></thead><tbody>${rows}</tbody></table></div></div>`:''}
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
    return `<section class="card literature-gallery" id="ideation-literature"><div class="literature-gallery-head"><h3>相关文献 · ${relevant.length} 篇（上限 ${this._paperLimit(d)} 篇）</h3>${updatable && d.phase!=='retrieval'?'<button class="idea-text-btn" data-action="fulltexts">整理全文文献</button>':''}${updatable && d.phase!=='retrieval' && relevant.some(x=>x.paper.summary_version!==6)?'<button class="idea-text-btn" data-action="summaries">更新论文概览</button>':''}</div>${this._retrievalInfoHtml(d,relevant)}<div class="literature-grid">${cards || (['running','pending','stopping'].includes(d.status)?'<p>正在阅读和筛选全文，完成后展示文献卡片。</p>':'<p>暂无通过全文筛选的论文，可整理全文文献、调整方向或补充 PDF。</p>')}</div><nav class="literature-pagination" aria-label="文献翻页"><button class="proj-btn" data-action="papers-prev" ${this._paperPage===0?'disabled':''}>← 上一页</button><span aria-live="polite">${this._paperPage+1} / ${pages}</span><button class="proj-btn" data-action="papers-next" ${this._paperPage===pages-1?'disabled':''}>下一页 →</button></nav></section>`;
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
  _cardsHtml(d) { return `<div class="card" id="ideation-approved"><h3>${d.phase==='idea_review'?'第二轮：确认创新点':'可选择的创新点'}</h3><p class="idea-evidence-note">打开一张卡片查看详情，再点击弹窗底部“选择这个创新点，启动科研项目”。系统会保存文献与创新点、创建项目，自动开始实验设计。</p></div><div class="idea-cards">${d.cards.map((c,i)=>`<button class="proj-big-card idea-card" data-action="card" data-index="${i}" aria-haspopup="dialog"><span class="idea-card-top">候选 ${i+1}<span>查看详情</span></span><span class="proj-big-card-title idea-card-title" data-no-i18n>${this._esc(c.title)}</span><span class="idea-card-summary" data-no-i18n>${this._esc(this._short(c.one_liner))}</span><span class="idea-card-more">背景 · 方法 · 差异 · 验证</span></button>`).join('')}</div>`; },

  async _action(name,body) {
    if(this.busy) return; this.busy=true; this._error='';
    this._pending=({'direction-confirm':'方向已提交，正在安排论文比较与原文证据核对…',resume:'正在恢复调研，从最近完成点继续…',stop:'正在请求停止，等待当前操作结束…',fulltexts:'正在筛选和解读可获取全文的论文…',summaries:'正在更新科学问题、方法、优势与局限…','card-summaries':'正在更新候选的入门背景与公式排版…'})[name] || '正在保存并更新调研…';
    this._paint();
    this.host.querySelector('[role="status"]')?.scrollIntoView?.({block:'nearest',behavior:'smooth'});
    const token=this._token;
    try { await API.ideationAction(this.sessionId,name,{revision:this.data.revision,...body}); }
    catch(e) { if(token===this._token)this._error=e.message; }
    finally { if(token===this._token){this.busy=false; this._pending=''; await this.refresh(true); if(window.App)App._loadIdeations();} }
  },
  async _act(action,index) {
    if(action==='job-output')return this._openJobOutput(index);
    if(action==='jump')return this._jumpTo(index);
    if(action==='prompt')return this._showPrompt();
    if(action==='card')return this._openCard(Number(index));
    if(action==='pool-card')return this._openCard(Number(index),true);
    if(action==='paper')return this._openPaper(Number(index));
    if(action==='papers-prev' || action==='papers-next'){this._paperPage+=(action==='papers-next'?1:-1);this._paint();this.host.querySelector('.literature-gallery')?.scrollIntoView?.({block:'start',behavior:'smooth'});return;}
    if(action==='card-summaries' || action==='summaries' || action==='fulltexts')return this._action(action,{});
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
  _textHtml(value) {
    const text=String(value || '未提供');
    return text.split(/\n+/).flatMap(line=>{
      if(line.length<=180)return [line];
      const parts=[];let paragraph='';
      line.split(/(?<=[。！？])/u).forEach(sentence=>{if(paragraph && paragraph.length+sentence.length>180){parts.push(paragraph);paragraph='';}paragraph+=sentence;});
      if(paragraph)parts.push(paragraph);return parts;
    }).filter(x=>x.trim()).map(x=>`<p data-no-i18n>${this._inlineHtml(x)}</p>`).join('');
  },
  _inlineHtml(value) {
    return String(value).split(/(\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|\$\$[\s\S]*?\$\$|\$[^$\n]+\$)/g).map(part=>{
      const display=part.startsWith('\\[') || part.startsWith('$$');
      if(display || part.startsWith('\\(') || part.startsWith('$') && part.endsWith('$') && part.length>2) {
        const n=display || part.startsWith('\\(')?2:1;
        return `<span class="${display?'idea-math':'idea-math-inline'}" data-math="${this._esc(part.slice(n,-n))}"${display?'':' data-math-inline="true"'}></span>`;
      }
      return this._esc(part).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
    }).join('');
  },
  _groupMathScripts(formula) {
    // TeX forbids two scripts of the same kind on one atom. Group the
    // already-scripted atom; preserve every original symbol and operator.
    const group=String.raw`\{(?:[^{}]|\{[^{}]*\})*\}`;
    const script=String.raw`[_^](?:${group}|\\[A-Za-z]+|[A-Za-z0-9*])`;
    const atom=new RegExp(String.raw`(\\[A-Za-z]+|[A-Za-z0-9])((?:${script}){2,})`,'g');
    return String(formula).replace(atom,(whole,base,scripts)=>{
      let result=base;const seen=new Set();
      for(const token of scripts.match(new RegExp(script,'g')) || []) {
        if(seen.has(token[0])){result='{'+result+'}';seen.clear();}
        result+=token;seen.add(token[0]);
      }
      return result;
    });
  },
  _reviewState(card,d=this.data) {
    const job=(d?.ideaspark_batch?.jobs || []).find(j=>j.id===card.skill_job_id);
    if(card.selection_ready)return {job,technical:false,label:card.neighbor_check?.status==='partial'?'可选择 · 检索有缺失':'可选择 · 相近研究已比较',reason:card.neighbor_check?.summary || '具体技术和实验方案在选定后继续。'};
    const technical=job?.status!=='running' && (!!job?.retryable || job?.failure_category==='technical');
    const label=card.audit?.verdict==='pass'?'审查通过':job?.status==='running'?'正在审查':technical?'审查中断':job?.status==='needs_work'?'方案待修改':'审查未完成';
    return {job,technical,label,reason:job?.reason || card.skill_reason || (technical?'本次技术检查没有完成，可重试。':'本方案尚无完整的审查通过记录。')};
  },
  _section(title,value) { return `<section class="idea-detail-section"><h3>${this._esc(title)}</h3>${this._textHtml(value)}</section>`; },
  _plainStepsHtml(title,rows) {
    return `<section class="idea-detail-section"><h3>${this._esc(title)}</h3><ol class="idea-explainer-steps">${rows.map(r=>`<li><h4 data-no-i18n>${this._esc(r.title)}</h4>${this._textHtml(r.text)}</li>`).join('')}</ol></section>`;
  },
  _cardBodyHtml(card) {
    const plain=card.plain_language,e=this._esc.bind(this);
    let body='';
    if(plain)body+=this._section('先了解研究背景',plain.background || plain.problem)+this._section('与已有做法相比，改变了什么',plain.difference);
    if(plain?.terms?.length)body+=`<section class="idea-detail-section"><h3>先看懂这里的词</h3><dl class="idea-term-list">${plain.terms.map(t=>`<div><dt data-no-i18n>${e(t.name)}</dt><dd data-no-i18n>${e(t.meaning)}</dd></div>`).join('')}</dl></section>`;
    body+=`<section class="idea-question"><span>一句话看懂这个想法</span><p data-no-i18n>${e(card.one_liner)}</p></section>`;
    if(plain && Array.isArray(plain.steps) && Array.isArray(plain.test)) {
      body+=this._plainStepsHtml('准备怎么做',plain.steps)+this._plainStepsHtml('怎么判断这个想法有没有用',plain.test)
        +this._section('需要什么数据',plain.data)+this._section('哪些地方可能行不通',plain.risks);
    } else body+=this._section('科学问题',card.scientific_question);
    body+=`<details class="idea-technical-details"><summary>展开技术细节与公式</summary>`;
    if(card.formulas?.length)body+=card.formulas.map(f=>`<section class="idea-formula"><h4 data-no-i18n>${e(f.title)}</h4><div class="idea-math" data-math="${e(f.latex)}" aria-label="${e(f.title)}"></div>${this._textHtml(f.explanation)}<dl class="idea-term-list">${(f.symbols || []).map(s=>`<div><dt data-no-i18n><span data-math="${e(s.symbol)}" data-math-inline="true"></span></dt><dd data-no-i18n>${e(s.meaning)}</dd></div>`).join('')}</dl></section>`).join('');
    body+='<details class="idea-prompt-block"><summary>原始技术说明</summary>';
    body+=this._section('科学问题（技术说明）',card.scientific_question);
    for(const [title,value] of [['方法与机制',card.method],['差异与新颖性边界',card.novelty],['可证伪实验',card.falsification]])
      body+=`<section class="idea-detail-section"><h3>${e(title)}</h3><pre class="idea-technical-source" data-no-i18n>${e(value || '未提供')}</pre></section>`;
    body+='</details>'+this._section('数据可得性',card.data_availability);
    if(Array.isArray(card.data_sources) && card.data_sources.length)body+=`<section class="idea-detail-section"><h3>数据来源</h3><ul>${card.data_sources.map(s=>`<li data-no-i18n>${e(s.name)}${s.url?' · '+this._link(s.url,'来源入口'):''}</li>`).join('')}</ul></section>`;
    const list=value=>Array.isArray(value)?value.map(x=>typeof x==='string'?x:JSON.stringify(x)).join('\n'):value;
    body+=this._section('基线与指标',[list(card.baselines),list(card.metrics)].filter(Boolean).join('\n'))+this._section('风险与资源',[card.risks,card.compute].filter(Boolean).join('\n'))+'</details>';
    return body;
  },
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
  _openCard(index,pool=false) {
    const card=(pool?this.data.idea_pool:this.data.cards)?.[index];if(!card)return;
    const confirmable=!pool && !this.data.run_id && (card.audit?.verdict==='pass' || card.selection_ready) && this.data.phase==='idea_review' && ['waiting','blocked','interrupted'].includes(this.data.status);
    const review=this._reviewState(card);
    let body=this._cardBodyHtml(card);
    if(card.similar_candidate_job_ids?.length)body+=this._section('本批内有相近想法','这张卡片的核心机制与本批其他卡片相同或相近，属于相关变体，不能据此算作多个独立创新方向。选定后需要明确真正差异。');
    body+=this._section((card.selection_ready?'相近研究：':'审查进度：')+review.label,review.technical?'本方案的技术检查未完成，当前不能确认启动实验。可重试审查，草稿和已完成记录会保留。':review.reason);
    if(card.neighbor_check){
      const check=card.neighbor_check;
      (check.comparisons || []).forEach(c=>{const p=(check.papers || []).find(p=>p.paper_id===c.paper_id);body+=this._section(p?.title || c.paper_id,c.difference)+(p?this._link(p.url):'');});
      if(check.issues?.length)body+=this._section('检查中未完成的事项',check.issues.join('\n'));
      body+=this._section('选定后继续', '具体技术实现、数据划分、基线、消融和实验验证由后续实验设计阶段完成。相近研究比较不代表新颖性或效果已经得到证明。');
    }
    if(review.technical)body+=`<details class="idea-prompt-block"><summary>查看具体中断记录</summary>${this._textHtml(review.reason)}</details>`;
    if(card.skill_job_id)body+='<button class="idea-text-btn" id="idea-task-output">查看本任务的实时输出与历史 →</button>';
    if(card.skill)body+='<details class="idea-prompt-block"><summary>生成来源</summary>'+this._section('执行信息',`Microsoft IdeaSpark ${card.skill.workflow?'官方构思资料 · 简化流程':'官方技能'} · ${card.skill.commit}\n模型：DeepSeek API${card.presentation_model?' · '+card.presentation_model:''}\n${card.selection_ready?'创新点可选择，具体技术与实验尚待后续设计':card.skill_status==='validated'?'提案审查通过，尚未经实验验证':'审查记录尚不完整，不能启动科研'}`)+'</details>';
    if(card.skill_job_id && card.skill_card_available)body+=this._link(window.location.origin+'/api/ideation/sessions/'+encodeURIComponent(this.sessionId)+'/ideaspark/jobs/'+encodeURIComponent(card.skill_job_id)+'/card','查看官方IdeaSpark完整中文方案');
    else if(card.skill_job_id && !card.selection_ready)body+='<p class="idea-evidence-note">当前内容为候选草稿。</p>';
    (card.audit?.comparisons || []).forEach(c=>{const p=(card.closest_papers || []).find(p=>p.paper_id===c.paper_id);body+=this._section('最近邻：'+(p?p.title:c.paper_id),`问题：${c.problem}\n机制：${c.mechanism}\n数据：${c.data}\n实验：${c.experiment}\n差异：${c.difference}`)+(p?this._link(p.url):'');});

    const idle=['blocked','stopped','interrupted'].includes(this.data.status) && !this.data.run_id;
    const retry=idle && review.technical;
    const footer='<div id="idea-confirm-feedback" class="idea-confirm-feedback" role="status" aria-live="polite" hidden></div><button class="proj-btn" data-dialog-close>返回卡片</button>'+(this.data.run_id?'<button class="proj-btn primary" id="idea-open-project">打开已启动项目</button>':confirmable?'<button class="proj-btn primary" id="idea-confirm-card">启动科研项目</button>':
      `<span class="idea-confirm-help">${review.technical?'审查中断，尚不能启动实验':card.audit?.verdict==='pass'?'本批审查结束后可确认':'此方案仍需完成审查或修改'}</span>`+
      (retry?'<button class="proj-btn primary" id="idea-retry-review">重试未完成的审查</button>':idle?'<button class="proj-btn" id="idea-edit-card">填写修改意见</button>':''));
    const dialog=this._showDialog(card.title,body,footer);
    dialog.querySelector('#idea-open-project')?.addEventListener('click',()=>{App.currentTab='overview';App.openProject(this.data.run_id);});
    dialog.querySelector('#idea-task-output')?.addEventListener('click',()=>{const opener=this._returnFocus;this._openJobOutput(card.skill_job_id);this._returnFocus=opener;});
    dialog.querySelector('#idea-retry-review')?.addEventListener('click',()=>{this._closeDialog(false);this._action('resume',{});});
    dialog.querySelector('#idea-edit-card')?.addEventListener('click',()=>{this._closeDialog(false);this._jumpTo('ideation-feedback');document.getElementById('idea-feedback')?.focus();});
    dialog.querySelector('#idea-confirm-card')?.addEventListener('click',e=>this._confirmCard(card,e.currentTarget,dialog));
  },
  async _confirmCard(card,button,dialog) {
    const feedback=dialog.querySelector('#idea-confirm-feedback');
    const show=(text,error=false)=>{if(!feedback)return;feedback.hidden=false;feedback.setAttribute('role',error?'alert':'status');feedback.textContent=text;};
    if(this.busy){if(!button.disabled)show(this._pending || '正在完成上一项操作，请稍候再选择。');return;}
    this.busy=true;this._error='';button.disabled=true;button.setAttribute('aria-busy','true');
    button.textContent='正在创建科研项目…';const token=this._token;
    show('正在保存选中的创新点与文献材料，随后启动实验设计。成功后会自动打开项目页。');
    try {
      const response=await API.ideationAction(this.sessionId,'confirm',{revision:this.data.revision,card_id:card.card_id});
      if(token!==this._token)return;
      if(typeof response?.run_id!=='string' || !response.run_id.trim())throw new Error('确认接口未返回项目编号，请重试。');
      show('科研项目已创建，正在打开实验设计进度…');
      this.data.run_id=response.run_id;this.data.phase='handed_off';this.data.status='completed';
      App.currentTab='overview';App.openProject(response.run_id);
      if(window.App)App._loadProjects();
    } catch(error) {
      if(token===this._token){
        let message=error.message || '创建项目失败，请重试。';
        try{const start=message.indexOf('{');if(start>=0){const parsed=JSON.parse(message.slice(start));if(typeof parsed.detail==='string')message=parsed.detail;}}catch(_){}
        this._error=message;show('未能启动科研项目：'+message,true);
        button.disabled=false;button.textContent='重试创建科研项目';button.removeAttribute('aria-busy');
      }
    } finally {
      if(token===this._token){this.busy=false;button.removeAttribute('aria-busy');await this.refresh(true);}
    }
  },
  _openJobOutput(jobId) {
    const jobs=this.data?.ideaspark_batch?.jobs || [];
    const job=jobs.find(j=>j.id===jobId) || jobs.find(j=>j.status==='running') || jobs[0];if(!job)return;
    this._jobCall='';this._jobFull=false;
    const body=`<p data-no-i18n>${this._esc(job.focus)}</p><div class="idea-live-meta" id="job-output-status" role="status">正在读取任务详情…</div><label class="idea-live-call-label" for="job-output-call">调用记录</label><select id="job-output-call" class="gate-input"><option value="">跟随最新调用</option></select><p class="idea-evidence-note" id="job-output-description"></p><button class="idea-text-btn" id="job-output-full" hidden>显示全部已收到的内容</button><pre class="idea-live-output" id="job-output-content" tabindex="0" aria-label="模型返回内容">等待读取…</pre>`;
    const dialog=this._showDialog('任务 '+job.number+' · 实时输出',body);dialog.classList.add('idea-output-dialog');
    dialog.querySelector('#job-output-call').addEventListener('change',e=>{this._jobCall=e.target.value;clearTimeout(this._jobOutputTimer);this._pollJobOutput(dialog,job.id);});
    dialog.querySelector('#job-output-full').addEventListener('click',()=>{this._jobFull=true;clearTimeout(this._jobOutputTimer);this._pollJobOutput(dialog,job.id);});
    this._pollJobOutput(dialog,job.id);
  },
  _elapsed(start,end) {
    const time=Date.parse(start);if(!Number.isFinite(time))return '尚未记录';
    const seconds=Math.max(0,Math.floor(((end?Date.parse(end):Date.now())-time)/1000));
    return seconds>=60?`${Math.floor(seconds/60)}分${seconds%60}秒`:`${seconds}秒`;
  },
  async _pollJobOutput(dialog,jobId) {
    const token=this._jobPollToken=(this._jobPollToken || 0)+1;
    try {
      const params=new URLSearchParams();if(this._jobCall)params.set('call_id',this._jobCall);if(this._jobFull)params.set('full','true');const query=params.toString();const suffix=query?'?'+query:'';
      const info=await API.get('/ideation/sessions/'+encodeURIComponent(this.sessionId)+'/ideaspark/jobs/'+encodeURIComponent(jobId)+'/activity'+suffix);
      if(this._dialog!==dialog || this._jobPollToken!==token)return;
      const out=info.output,tool=info.activity || {},retrieval=/collision retrieval/.test(info.stage),streaming=out?.status==='pending' && out.mode==='streaming' && !out.finished_at && !this._jobCall;
      const step=this._operationLabel({progress_message:'IdeaSpark：'+info.stage});
      const elapsed=this._elapsed(tool.kind==='tool' && tool.status==='running'?tool.started_at:retrieval?info.stage_started_at:out?.started_at || info.stage_started_at,out?.status!=='pending' && !retrieval?out?.finished_at:null);
      dialog.querySelector('#job-output-status').textContent=`${step} · ${this.statusLabel(info.status)} · 已耗时 ${elapsed}`;
      const select=dialog.querySelector('#job-output-call'),options=JSON.stringify(info.calls.map(c=>[c.call_id,c.status]));
      if(select.dataset.options!==options){select.dataset.options=options;select.innerHTML='<option value="">跟随最新调用</option>'+info.calls.slice().reverse().map(c=>`<option value="${this._esc(c.call_id)}">${this._esc(c.stage)} · ${this._esc(c.status==='pending'?'进行中':'已结束')}</option>`).join('');select.value=this._jobCall;}
      dialog.querySelector('#job-output-description').textContent=retrieval&&!this._jobCall?'正在检索最接近的已有论文，当前不是生成新草稿。下方可查看最近一次模型返回。'+(tool.timeout_sec?`本次命令等待上限 ${Math.ceil(tool.timeout_sec/60)} 分钟。`:''):out?.finish_reason==='length'?'本次返回达到输出上限；这里保留已收到的片段，审查仍需继续。':streaming?'正在接收 DeepSeek 的真实流式返回。':out?out.mode==='buffered'?'这次调用没有记录流式分片；显示已返回的完整内容。':'显示这次调用已收到的内容。':'这一步尚无模型返回，可在调用开始后查看。';
      dialog.querySelector('#job-output-full').hidden=!out?.windowed;
      const pre=dialog.querySelector('#job-output-content'),follow=pre.scrollHeight-pre.scrollTop-pre.clientHeight<60;
      pre.textContent=out?.content || (retrieval?'本步骤正在检索文献，没有模型生成内容。':out?.status==='pending'?'请求已发出，等待模型返回首段内容…':info.reason || '暂无模型输出记录。');
      if(follow)pre.scrollTop=pre.scrollHeight;
    } catch(error){if(this._dialog===dialog && this._jobPollToken===token)dialog.querySelector('#job-output-status').textContent='读取失败：'+error.message;}
    if(this._dialog===dialog && this._jobPollToken===token)this._jobOutputTimer=setTimeout(()=>this._pollJobOutput(dialog,jobId),1000);
  },
  _showDialog(title,body,footer) {
    const opener=document.activeElement;this._closeDialog(false);this._returnFocus=opener;
    const dialog=document.createElement('dialog');dialog.className='idea-dialog';dialog.setAttribute('aria-labelledby','idea-dialog-title');
    dialog.innerHTML=`<div class="idea-dialog-head"><h2 id="idea-dialog-title" data-no-i18n>${this._esc(title)}</h2><button class="idea-text-btn" data-dialog-close>关闭</button></div><div class="idea-dialog-body">${body}</div><div class="idea-dialog-foot">${footer || '<button class="proj-btn" data-dialog-close>返回</button>'}</div>`;
    document.body.appendChild(dialog);this._dialog=dialog;
    dialog.querySelectorAll('[data-math]').forEach(el=>{
      const formula=el.dataset.math;
      try {
        if(!window.katex)throw new Error('Formula renderer unavailable');
        const options={displayMode:!el.hasAttribute('data-math-inline'),throwOnError:true,trust:false,strict:'error',maxExpand:1000,maxSize:10};
        try{window.katex.render(formula,el,options);}
        catch(error){
          if(!/Double (subscript|superscript)/.test(error.message))throw error;
          const grouped=this._groupMathScripts(formula);if(grouped===formula)throw error;
          window.katex.render(grouped,el,options);el.dataset.mathLayout='grouped';
        }
      }
      catch(error){el.classList.add('idea-math-fallback');el.textContent='公式排版待校正';const source=document.createElement('details');source.innerHTML='<summary>查看公式源码</summary><pre></pre>';source.querySelector('pre').textContent=formula;el.appendChild(source);}
    });
    dialog.querySelectorAll('[data-dialog-close]').forEach(b=>b.addEventListener('click',()=>this._closeDialog()));
    dialog.addEventListener('cancel',e=>{e.preventDefault();this._closeDialog();});dialog.addEventListener('close',()=>{if(this._dialog===dialog)this._closeDialog();});dialog.addEventListener('click',e=>{if(e.target===dialog)this._closeDialog();});dialog.showModal();return dialog;
  },
  _closeDialog(restore=true) {clearTimeout(this._jobOutputTimer);this._jobPollToken=(this._jobPollToken || 0)+1;const d=this._dialog;if(!d)return;this._dialog=null;if(d.open)d.close();d.remove();if(restore && this._returnFocus && this._returnFocus.isConnected)this._returnFocus.focus();this._returnFocus=null;},
  async _showPrompt() {
    const dialog=this._showDialog('各阶段生成方式与实际提示词','<p>正在读取…</p>');
    try{const info=await API.ideaPrompt();if(this._dialog!==dialog)return;
      dialog.querySelector('.idea-dialog-body').innerHTML=`<p class="idea-evidence-note">配置模型：${this._esc(info.model)}。${info.skill?`参考官方构思资料：Microsoft IdeaSpark · ${this._esc(info.skill.commit)}；目标 ${info.card_target} 个独立任务。同时最多 ${this._esc(info.workers || 3)} 个候选；每次使用独立上下文，深度推理${info.thinking==='enabled'?'按配置启用':'默认关闭'}，截断后调整输出重试。`:'内置证据驱动提示词，未调用官方IdeaSpark执行器。'}</p>`+Object.entries(info.stages).map(([key,text])=>`<details class="idea-prompt-block"><summary>${this._esc(({plan:'方向理解与检索规划',screen:'文献相关性筛选',summarize:'问题、现状、创新、方法与优缺点',idea_generation:'创新点生成（简化流程）',neighbor_check:'相近研究比较',neighbors:'相近研究筛选',read:'单篇全文阅读与证据核对',read_part:'同篇长论文分段阅读',read_merge:'同篇分段记录汇总',synthesis:'领域现状与缺口',generate:'候选机制生成',audit:'独立最近邻审查',spark_card:'官方方案中文卡片呈现'})[key] || (key.startsWith('ideaspark:')?'IdeaSpark官方阶段：'+key.slice(10):key))}${info.output_tokens[key]?' · 输出预算 '+info.output_tokens[key]:''}</summary><pre class="proj-pre" data-no-i18n>${this._esc(text)}</pre></details>`).join('');
    }catch(e){if(this._dialog===dialog)dialog.querySelector('.idea-dialog-body').textContent='读取失败：'+e.message;}
  },
};
window.IdeationView=IdeationView;
