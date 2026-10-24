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
