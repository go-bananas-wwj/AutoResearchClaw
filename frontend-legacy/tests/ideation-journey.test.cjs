const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../src/components/IdeationView.js'),'utf8');
const sandbox={window:{}};vm.runInNewContext(source+';globalThis.view=IdeationView;',sandbox);
const view=sandbox.view;
function state(extra={}) { return {status:'running',phase:'retrieval',revision:1,approved_revision:null,plan:{research_object:'坡长坡度因子'},papers:[],reading:{},cards:[],reviews:[],progress:{operation:'plan'},...extra}; }
function current(d) { return view._journeySteps(d).find(s=>s.state==='current'); }

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
    assert.match(html,/^<div class="card ideation-route-card" data-material-stage="workflow">/);
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
  assert.ok(!html.includes('id="idea-confirm-card"'));assert.match(html,/待审查/);assert.match(html,/待完善/);
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
