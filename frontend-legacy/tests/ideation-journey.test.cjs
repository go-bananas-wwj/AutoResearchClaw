const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../src/components/IdeationView.js'),'utf8');
const sandbox={window:{},setTimeout,clearTimeout,setInterval,clearInterval};vm.runInNewContext(source+';globalThis.view=IdeationView;',sandbox);
const view=sandbox.view;
function state(extra={}) { return {status:'running',phase:'retrieval',revision:1,approved_revision:null,plan:{research_object:'坡长坡度因子'},papers:[],reading:{},cards:[],reviews:[],progress:{operation:'plan'},...extra}; }
function current(d) { return view._journeySteps(d).find(s=>s.state==='current'); }

test('idea-first cards remain selectable with explicit partial neighbor checks and deferred design',()=>{
  const card={title:'待验证的想法',one_liner:'能不能更早发现变化？',skill_job_id:'job',skill_status:'idea_ready',selection_ready:true,
    neighbor_check:{status:'partial',summary:'摘要层面比较，尚有缺失',issues:['一个检索来源不可用'],comparisons:[],papers:[]},audit:{verdict:'checked'}};
  const d=state({phase:'idea_review',status:'waiting',approved_revision:1,idea_pool:[card],cards:[card],
    ideaspark_batch:{workflow:'ideas_then_neighbors_v1',jobs:[{id:'job',status:'idea_ready'}]}});
  assert.match(view._batchSummary(d),/1 张可选择/);
  assert.equal(view._reviewState(card,d).technical,false);
  assert.match(view._reviewState(card,d).label,/检索有缺失/);
  assert.equal(view._journeySteps(d)[7].title,'检查相近研究');
  let footer='',body='';view.data=d;
  const original=view._showDialog;
  view._showDialog=(title,b,f)=>{body=b;footer=f;return {querySelector:()=>null};};
  try{view._openCard(0);}finally{view._showDialog=original;}
  assert.match(footer,/启动科研项目/);
  assert.match(body,/一个检索来源不可用/);
  assert.match(body,/新颖性或效果/);
  assert.doesNotMatch(body,/审查通过|当前不能确认启动实验/);
});

test('route retains completed and future stations with two human confirmation points',()=>{
  const d=state({phase:'audit',approved_revision:1,progress:{operation:'read',title:'Supplemental method',current:2,total:4,unit:'候选'}});
  const steps=view._journeySteps(d),html=view._progressHtml(d,true);
  assert.equal(steps.length,10);assert.equal(steps.filter(s=>s.human).length,2);
  assert.equal(current(d).id,'audit');assert.equal(steps[3].status,'已确认');assert.equal(steps[8].state,'future');
  assert.equal((html.match(/aria-current="step"/g)||[]).length,1);
  assert.match(html,/候选 2 \/ 4/);assert.match(html,/Supplemental method/);
  assert.ok(!html.includes('data-action='));
});

test('pre-approval long reading belongs to fulltext overview, not approved evidence comparison',()=>{
  const d=state({progress:{operation:'read',part_current:2,part_total:3}});
  assert.equal(current(d).id,'overview');assert.equal(view._journeySteps(d)[4].state,'future');
  assert.match(view._progressHtml(d,true),/第 2 \/ 3 段/);
});

test('direction review waits for user rather than marking it complete',()=>{
  const d=state({phase:'literature_review',status:'waiting'});
  assert.equal(current(d).id,'literature_review');assert.equal(current(d).status,'等待你确认');
  assert.equal(view._journeySteps(d)[4].state,'future');
  assert.ok(!view._progressHtml(d,false).includes('is-running'));
});

test('innovation review preserves full route and does not start downstream work',()=>{
  const d=state({phase:'idea_review',status:'waiting',approved_revision:1,cards:[{title:'Verified candidate'}]});
  assert.equal(current(d).id,'idea_review');assert.equal(view._journeySteps(d)[7].status,'已完成');
  assert.equal(view._journeySteps(d)[9].status,'待开始');
});

test('automatic revision distinguishes prior work from upcoming repeated work',()=>{
  const d=state({phase:'synthesis',revision:3,approved_revision:3,refinement_round:2,reviews:[
    {type:'auto_refine',cards:[],rejected_cards:[{},{}]},
    {type:'auto_refine',cards:[],rejected_cards:[{}]}
  ]});
  const steps=view._journeySteps(d),html=view._progressHtml(d,true);
  assert.equal(current(d).id,'synthesis');assert.equal(steps[6].status,'待再次执行');assert.equal(steps[7].status,'待再次执行');
  assert.equal(steps[4].state,'done');assert.match(html,/第 3 轮构思与审查/);assert.match(html,/已完成的构思与审查轮次（2）/);
  assert.ok(html.indexOf('第 2 轮：')<html.indexOf('第 1 轮：'));
});

test('workflow comes first, followed by newest outputs and finally literature',()=>{
  const d=state({phase:'audit',status:'blocked',approved_revision:1,refinement_round:2,
    blocker:{category:'scientific',code:'refinement_exhausted'},missing:['三轮未通过'],
    rejected_cards:[{title:'需修订的候选',failure:{code:'invalid_design'},audit:{missing:['缺少独立观测'],design_check:{issues:['对照条件未落实']}}}],
    synthesis:{landscape_zh:'此前生成的领域分析',method_comparison:[],gaps:[]},
    papers:[{paper_id:'p1',title:'早期相关论文',relevance:'core',fulltext_status:'read',summary_basis:'fulltext',summary_version:6,overview:{problem:'问题',method:'方法'}}]});
  const html=view._sessionHtml(d),review=html.indexOf('data-material-stage="review"'),synthesis=html.indexOf('ideation-landscape-panel'),route=html.indexOf('data-material-stage="workflow"'),papers=html.indexOf('class="card literature-gallery"');
  const latest=html.indexOf('data-material-stage="latest-status"');
  assert.ok(route>=0 && route<latest && latest<review && review<synthesis && synthesis<papers);
  assert.match(html,/达到本次自动修订轮次上限/);assert.match(html,/缺少独立观测/);assert.match(html,/根据审查意见继续优化/);
  assert.equal(view._journeySteps(d)[8].status,'方案通过审查后解锁');
  assert.ok(!html.slice(papers).includes('data-material-stage='));
});

test('workflow stays the first panel through running, review, paused and handoff states',()=>{
  for(const extra of [
    {},{status:'waiting',phase:'literature_review'},
    {status:'waiting',phase:'idea_review',approved_revision:1},
    {status:'blocked',phase:'audit',approved_revision:1},
    {status:'completed',phase:'handed_off',approved_revision:1,run_id:'rc-example'}
  ]) {
    const html=view._sessionHtml(state(extra));
    assert.match(html,/^<div class="card ideation-route-card" id="ideation-workflow" data-material-stage="workflow">/);
    assert.equal((html.match(/data-material-stage="workflow"/g)||[]).length,1);
  }
});

test('finished review counts are explicit and retain model-written reasons',()=>{
  const d=state({phase:'audit',status:'blocked',approved_revision:1,refinement_round:2,blocker:{code:'refinement_exhausted'},
    rejected_cards:[{title:'方案甲',failure:{code:'mechanism_overlap'},rejection_reason:'原始审查依据甲'},
      {title:'方案乙',failure:{code:'mechanism_overlap'},rejection_reason:'原始审查依据乙'},
      {title:'方案丙',failure:{code:'invalid_design'},audit:{missing:['验证数据尚未落实']}},
      {title:'方案丁',failure:{code:'invalid_design'},audit:{missing:'legacy malformed field'}}]});
  const summary=view._blockerHtml(d),details=view._reviewResultsHtml(d);
  assert.match(summary,/3 轮/);assert.match(summary,/4 个候选，0 个通过/);
  assert.match(summary,/已有工作覆盖 · 2 个/);assert.match(summary,/实验设计或验证条件待完善 · 2 个/);
  assert.match(details,/原始审查依据甲/);assert.match(details,/验证数据尚未落实/);
});

test('paused and stopped runs keep their place without an active beacon',()=>{
  for(const status of ['blocked','stopped','interrupted','stopping']) {
    const d=state({phase:'audit',approved_revision:1,status,progress_message:'正在核对原文',progress:{operation:'read',title:'Last paper'}});
    const html=view._progressHtml(d,status==='stopping');
    assert.equal(current(d).id,'audit');assert.equal(view._journeySteps(d)[5].state,'done');
    assert.ok(!html.includes('is-running'));
    if(status!=='stopping')assert.match(html,/上次进度/);
  }
});

test('handoff is not presented as completed experiments or a delivered paper',()=>{
  const d=state({phase:'handed_off',status:'completed',approved_revision:1,run_id:'rc-example'});
  const html=view._progressHtml(d,false),last=current(d);
  assert.equal(last.id,'research');assert.equal(last.state,'current');assert.equal(last.status,'已交接');
  assert.match(html,/后续实验与论文进度请在科研项目中查看/);assert.ok(!html.includes('论文已完成'));
  assert.ok(!html.includes('aria-current="step" data-action'));
});

test('overview refresh preserves already approved and completed milestones',()=>{
  const d=state({phase:'audit',approved_revision:1,presentation_task:'summaries'}),steps=view._journeySteps(d);
  assert.equal(current(d).id,'overview');assert.equal(steps[3].status,'已确认');assert.equal(steps[6].state,'done');assert.equal(steps[7].state,'future');
});

test('paper titles and progress content remain escaped',()=>{
  const d=state({phase:'reading',approved_revision:1,progress:{operation:'read',title:'<img src=x onerror="alert(1)">',current:1,total:10},progress_message:'<script>bad()</script>'});
  const html=view._progressHtml(d,true);
  assert.ok(!html.includes('<img'));assert.ok(!html.includes('<script>'));assert.match(html,/&lt;img/);
});

test('a new session has a full route without implying work has already happened',()=>{
  const d=state({plan:null,status:'pending',progress:{}}),steps=view._journeySteps(d);
  assert.equal(current(d).id,'direction');assert.equal(steps.filter(s=>s.state==='done').length,0);
  assert.equal(current(d).status,'待执行');assert.ok(!view._progressHtml(d,true).includes('is-running'));
  assert.equal(steps.filter(s=>s.state==='future').length,9);
});

test('IdeaSpark pool can show many drafts without exposing experiment confirmation',()=>{
  const pool=Array.from({length:12},(_,i)=>({card_id:'draft-'+i,title:'独立候选 '+i,one_liner:'依据已确认文献提出的待验证假设',skill_status:i===0?'needs_work':'running'}));
  const d=state({phase:'generate',idea_pool:pool,ideaspark_batch:{target:12,jobs:pool.map((c,i)=>({id:String(i),status:c.skill_status}))}});
  const html=view._ideaPoolHtml(d);
  assert.match(html,/12 张/);assert.equal((html.match(/data-action="pool-card"/g)||[]).length,12);
  assert.ok(!html.includes('id="idea-confirm-card"'));assert.match(html,/审查未完成/);
});

test('status distinguishes generated drafts, accepted cards and cycling task number',()=>{
  const d=state({phase:'audit',idea_pool:Array.from({length:9},()=>({})),cards:[],
    ideaspark_batch:{target:12,jobs:Array.from({length:10},(_,i)=>({status:i===6?'needs_work':'running'}))},
    progress:{operation:'ideaspark',current:3,total:12},progress_message:'IdeaSpark：coherence'});
  const progress=view._progressDetails(d),strip=view._statusStripHtml(d);
  assert.match(progress.line,/第 3 号任务（共 10 个，逐轮复核）/);
  assert.match(strip,/已生成 9 张候选草稿/);assert.match(strip,/0 张通过审查/);
  assert.match(strip,/1 个任务待完善/);assert.match(strip,/核对机制一致性/);
  assert.ok(!strip.includes('正在进行：IdeaSpark：coherence'));
  assert.ok(!progress.line.includes('3 / 10'));
});

test('plain explanation comes first, with ordered steps and collapsed technical sources',()=>{
  const card={title:'标题',one_liner:'能不能用卫星更早发现危险？',method:'a_b = <unsafe> & x',scientific_question:'技术问题',
    plain_language:{problem:'为什么难',steps:[{title:'先观察',text:'重复拍照。'},{title:'再比较',text:'检查变化。'}],
      test:[{title:'设对照',text:'比较已有方法。'},{title:'判断',text:'没有改善就不支持假设。'}],difference:'待验证差异',data:'真实数据',risks:'尚未验证',terms:[{name:'<img>',meaning:'&解释'}]},
    formulas:[{title:'位移',latex:'d_k = d_{k-1} + v_k',explanation:'位置如何变化',symbols:[{symbol:'d_k',meaning:'位置'}]}]};
  const html=view._cardBodyHtml(card);
  assert.ok(html.indexOf('一句话看懂这个想法')<html.indexOf('准备怎么做'));
  assert.equal((html.match(/class="idea-explainer-steps"/g)||[]).length,2);
  assert.match(html,/<details class="idea-technical-details"><summary>/);
  assert.ok(!html.includes('idea-technical-details" open'));assert.match(html,/data-math="d_k = d_\{k-1\} \+ v_k"/);
  assert.match(html,/a_b = &lt;unsafe&gt; &amp; x/);assert.ok(!html.includes('<img>'));
});

test('paragraph layout preserves every sentence and escapes model-supplied HTML',()=>{
  const text='这是解释。'.repeat(80)+'<script>bad()</script>';
  const html=view._textHtml(text);
  assert.ok((html.match(/<p /g)||[]).length>1);assert.equal((html.match(/这是解释。/g)||[]).length,80);
  assert.match(html,/&lt;script&gt;/);assert.ok(!html.includes('<script>'));
});

test('parallel status lists each active task and keeps draft counts separate',()=>{
  const d=state({phase:'audit',idea_pool:Array.from({length:9},()=>({})),ideaspark_batch:{jobs:[],parallel:{workers:3,active:{
    a:{number:1,message:'IdeaSpark：coherence'},b:{number:4,message:'IdeaSpark：critique'},c:{number:6,message:'IdeaSpark：plain_derive'}
  }}},progress:{operation:'ideaspark',current:6}});
  const html=view._statusStripHtml(d),p=view._progressDetails(d);
  assert.match(html,/正在并行 3 个任务（上限 3）/);assert.match(html,/已生成 9 张候选草稿/);
  assert.match(html,/任务 1：IdeaSpark：正在核对机制一致性/);assert.match(html,/任务 4：/);assert.match(html,/任务 6：/);
  assert.match(p.line,/当前并行：1、4、6 号任务/);
});

test('bookmarks point to existing sections and retain workflow as first card',()=>{
  const d=state({status:'blocked',phase:'audit',approved_revision:1,ideaspark_batch:{jobs:[]},idea_pool:[],
    synthesis:{landscape_zh:'现状',method_comparison:[{paper_id:'p1',method:'方法',boundary:'边界'}],gaps:[]},
    papers:[{paper_id:'p1',title:'论文',relevance:'core',fulltext_status:'read',summary_basis:'fulltext',overview:{problem:'问题',method:'方法'}}]});
  const nav=view._bookmarksHtml(d),html=view._sessionHtml(d);
  for(const match of nav.matchAll(/data-index="([^"]+)"/g))assert.ok(html.includes('id="'+match[1]+'"'),match[1]);
  assert.match(nav,/方案对照/);assert.match(nav,/论文总结/);assert.match(nav,/候选卡片/);
  assert.match(html,/^<div class="card ideation-route-card"/);
});

test('background and existing-method comparison precede the one-line idea',()=>{
  const html=view._cardBodyHtml({one_liner:'能不能提前发现危险？',plain_language:{background:'先用重复拍照看地表移动。',problem:'问题',
    difference:'已有方法的局限与本方案变化。',terms:[{name:'雷达',meaning:'一种观测手段'}],steps:[],test:[]}});
  assert.ok(html.indexOf('先用重复拍照')<html.indexOf('一句话看懂这个想法'));
  assert.ok(html.indexOf('与已有做法相比')<html.indexOf('一句话看懂这个想法'));
  assert.ok(html.indexOf('先看懂这里的词')<html.indexOf('一句话看懂这个想法'));
});

test('interrupted review offers retry instead of a disabled confirmation, with no raw quotations',()=>{
  const previous={show:view._showDialog,data:view.data};let body,footer;
  const card={card_id:'draft',title:'草稿',one_liner:'能不能……',skill_job_id:'job',evidence:[{text:'RAW_QUOTE_NOT_FOR_DISPLAY'}]};
  view.data=state({status:'blocked',phase:'audit',approved_revision:1,idea_pool:[card],ideaspark_batch:{jobs:[{id:'job',status:'needs_work',retryable:true,reason:'JSON syntax error'}]}});
  view._showDialog=(title,b,f)=>{body=b;footer=f;return {querySelector:()=>null};};
  try {
    view._openCard(0,true);
    assert.match(body,/技术检查未完成/);assert.ok(!body.includes('原文依据'));assert.ok(!body.includes('RAW_QUOTE_NOT_FOR_DISPLAY'));
    assert.match(footer,/id="idea-retry-review"/);assert.ok(!footer.includes('id="idea-confirm-card"'));assert.ok(!footer.includes('disabled'));
    assert.equal(card.evidence[0].text,'RAW_QUOTE_NOT_FOR_DISPLAY');
  } finally {view._showDialog=previous.show;view.data=previous.data;}
});

test('inline LaTeX is escaped and marked for math rendering',()=>{
  const html=view._inlineHtml('位置 $d_p$ 与 \\(\\Delta_t\\)；<img src=x>');
  assert.match(html,/data-math="d_p" data-math-inline/);assert.match(html,/data-math="\\Delta_t"/);
  assert.ok(!html.includes('<img'));assert.match(html,/&lt;img/);
});

test('repeated TeX scripts are grouped without changing symbols or operators',()=>{
  for(const raw of [String.raw`\Delta_t^{*}_p(k)`,String.raw`\Delta_t^{\mathrm{used}}_p(k)`,String.raw`a_b_c`]) {
    const grouped=view._groupMathScripts(raw);
    assert.notEqual(grouped,raw);assert.equal(grouped.replace(/[{}]/g,''),raw.replace(/[{}]/g,''));
  }
  assert.equal(view._groupMathScripts(String.raw`\sigma_{kk'}`),String.raw`\sigma_{kk'}`);
});

test('bookmarks mount in the app header, handle clicks, and clean up on leaving research',()=>{
  const oldDocument=sandbox.document,oldHost=view.host;const headerClasses=new Set(),appClasses=new Set(),listeners=new Map();
  const classes=set=>({toggle:(name,on)=>on?set.add(name):set.delete(name),remove:name=>set.delete(name)});
  const header={classList:classes(headerClasses)},app={classList:classes(appClasses)};
  const button={dataset:{index:'ideation-workflow'},setAttribute(){},removeAttribute(){}};
  const target={setAttribute(){},focus(){this.focused=true;},scrollIntoView(){this.scrolled=true;}};
  const slot={hidden:true,innerHTML:'',closest:s=>s==='.header'?header:app,
    addEventListener:(type,fn)=>listeners.set(type,fn),removeEventListener:type=>listeners.delete(type)};
  sandbox.document={getElementById:()=>slot,querySelectorAll:()=>[button]};
  view.host={querySelector:()=>target,removeEventListener(){}};
  try {
    view._renderHeaderBookmarks(state());
    assert.equal(slot.hidden,false);assert.ok(slot.innerHTML.includes('完整流程'));assert.ok(headerClasses.has('ideation-header'));
    listeners.get('click')({target:{closest:()=>button}});
    assert.ok(target.focused && target.scrolled);assert.equal(view._bookmark,'ideation-workflow');
    view.destroy();assert.equal(slot.hidden,true);assert.equal(slot.innerHTML,'');assert.equal(listeners.size,0);
    assert.ok(!headerClasses.size && !appClasses.size);
  } finally {sandbox.document=oldDocument;view.host=oldHost;}
});

test('progress opens output details and translates collision retrieval accurately',()=>{
  const d=state({phase:'audit',ideaspark_batch:{jobs:[],parallel:{workers:3,active:{job:{number:1,message:'IdeaSpark：Run existing dual-channel collision retrieval'}}}}});
  const html=view._statusStripHtml(d);
  assert.match(html,/data-action="job-output"/);assert.match(html,/data-index="job"/);
  assert.match(html,/检索最接近的已有论文（查重）/);assert.ok(!html.includes('Run existing dual-channel'));
  assert.equal(view._elapsed('2026-10-25T00:00:00Z','2026-10-25T00:01:05Z'),'1分5秒');
});

test('idea confirmation sends one request and opens the new project overview',async()=>{
  const saved={data:view.data,refresh:view.refresh,busy:view.busy,token:view._token,api:sandbox.API,app:sandbox.App,windowApp:sandbox.window.App};
  const feedback={hidden:true,setAttribute(k,v){this[k]=v;}},button={setAttribute(){},removeAttribute(){}};
  let resolve,requests=0,opened='',loaded=0;
  sandbox.API={ideationAction(sid,action,body){requests++;assert.equal(action,'confirm');assert.equal(body.card_id,'selected-card');return new Promise(r=>{resolve=r;});}};
  sandbox.App=sandbox.window.App={currentTab:'manage',openProject(id){opened=id;},_loadProjects(){loaded++;}};
  view.data=state({phase:'idea_review',status:'waiting'});view.busy=false;view.refresh=async()=>{};
  try{
    const pending=view._confirmCard({card_id:'selected-card'},button,{querySelector:()=>feedback});
    await view._confirmCard({card_id:'selected-card'},button,{querySelector:()=>feedback});
    assert.equal(requests,1);assert.equal(button.disabled,true);assert.equal(feedback.hidden,false);
    assert.match(feedback.textContent,/启动实验设计/);
    resolve({run_id:'rc-new-project'});await pending;
    assert.equal(opened,'rc-new-project');assert.equal(sandbox.App.currentTab,'overview');assert.equal(loaded,1);
    assert.equal(view.data.run_id,'rc-new-project');assert.equal(view.busy,false);
  }finally{view.data=saved.data;view.refresh=saved.refresh;view.busy=saved.busy;view._token=saved.token;sandbox.API=saved.api;sandbox.App=saved.app;sandbox.window.App=saved.windowApp;}
});

test('handoff errors stay visible in the dialog and the same choice can be retried',async()=>{
  const saved={data:view.data,refresh:view.refresh,busy:view.busy,api:sandbox.API,app:sandbox.App,windowApp:sandbox.window.App};
  const feedback={hidden:true,setAttribute(k,v){this[k]=v;}},button={setAttribute(){},removeAttribute(){}};
  let attempts=0,opened='';sandbox.API={async ideationAction(){if(++attempts===1)throw new Error('API 409: {"detail":"另一个项目正在运行"}');return {run_id:'rc-retry-project'};}};
  sandbox.App=sandbox.window.App={openProject(id){opened=id;},_loadProjects(){}};
  view.data=state({phase:'idea_review',status:'waiting'});view.busy=false;view.refresh=async()=>{};
  try{
    await view._confirmCard({card_id:'same-choice'},button,{querySelector:()=>feedback});
    assert.equal(feedback.role,'alert');assert.match(feedback.textContent,/另一个项目正在运行/);
    assert.equal(view._error,'另一个项目正在运行');assert.equal(button.disabled,false);assert.equal(button.textContent,'重试创建科研项目');assert.equal(view.busy,false);
    await view._confirmCard({card_id:'same-choice'},button,{querySelector:()=>feedback});
    assert.equal(attempts,2);assert.equal(opened,'rc-retry-project');
  }finally{view.data=saved.data;view.refresh=saved.refresh;view.busy=saved.busy;sandbox.API=saved.api;sandbox.App=saved.app;sandbox.window.App=saved.windowApp;view._error='';}
});

test('the route remains informational and a failed handoff can be reopened',()=>{
  const card={title:'选定想法',selection_ready:true,neighbor_check:{summary:'比较已保存'},card_id:'chosen'};
  const d=state({phase:'idea_review',status:'blocked',cards:[card],approved_revision:1});
  assert.ok(!view._progressHtml(d,false).includes('data-action='));
  const saved={data:view.data,show:view._showDialog};let footer='';view.data=d;
  view._showDialog=(title,body,f)=>{footer=f;return {querySelector:()=>null};};
  try{
    view._openCard(0);assert.match(footer,/id="idea-confirm-card"/);
    view.data={...d,phase:'handed_off',status:'completed',run_id:'rc-existing'};
    view._openCard(0);assert.match(footer,/id="idea-open-project"/);assert.ok(!footer.includes('id="idea-confirm-card"'));
  }finally{view.data=saved.data;view._showDialog=saved.show;}
});

test('simplified ideation has one candidate list and one navigation bookmark',()=>{
  const first={card_id:'first',title:'可选创新点',selection_ready:true,one_liner:'能不能改进？'};
  const pending={card_id:'pending',title:'待整理创新点',skill_status:'running'};
  const d=state({phase:'idea_review',status:'waiting',cards:[first],idea_pool:[first,pending],
    ideaspark_batch:{workflow:'ideas_then_neighbors_v1',jobs:[]}});
  const html=view._sessionHtml(d),nav=view._bookmarksHtml(d);
  assert.equal((html.match(/data-action="card"/g)||[]).length,1);
  assert.equal((html.match(/data-action="pool-card"/g)||[]).length,1);
  assert.equal((html.match(/id="ideation-approved"/g)||[]).length,1);
  assert.ok(!html.includes('id="ideation-candidates"'));
  assert.match(nav,/data-index="ideation-approved"[^>]*>创新点/);
  assert.ok(!nav.includes('data-index="ideation-candidates"'));
  assert.ok(!html.includes('选择创新点并启动项目 ↓'));
});
