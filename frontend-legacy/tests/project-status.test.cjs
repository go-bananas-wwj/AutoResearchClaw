const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
test('project failure is visible independently of the connection badge and disappears on resume',()=>{
  const ctx={window:{},I18N:{t:x=>x}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/components/ProjectHome.js'),'utf8')+';globalThis.view=ProjectHome;',ctx);
  const v=ctx.view;v.runId='rc-fixture';v.project={status:'failed',current_stage:9,reason:"name '_pm' is not defined <img>"};
  const failed=v._headHtml();assert.match(failed,/任务已停止/);assert.match(failed,/role="alert"/);assert.match(failed,/_pm/);assert.ok(!failed.includes('<img>'));
  v.project.status='running';assert.ok(!v._headHtml().includes('任务已停止'));
});
