"""Regression for the failed entry repair and recovery of the same project."""
import json
from types import SimpleNamespace

import pytest


def test_missing_entry_uses_supplied_prompt_manager_and_writes_repaired_file(tmp_path):
    from researchclaw.pipeline.stage_impls._code_generation import _ensure_executable_entry
    calls=[]
    class Prompts:
        def sub_prompt(self,name,**values):
            calls.append((name,values));return SimpleNamespace(system='repair',user='complete entry')
    class Client:
        def chat(self,*args,**kwargs):
            return SimpleNamespace(content='```python\ndef main():\n    print("done")\nif __name__ == "__main__":\n    main()\n```',finish_reason='stop')
    files={'main.py':'def main():\n    pass\n'}
    assert _ensure_executable_entry(files,Client(),exp_dir=tmp_path,prompts=Prompts())
    assert calls[0][0]=='code_repair' and calls[0][1]['fname']=='main.py'
    assert (tmp_path/'main.py').read_text()==files['main.py']


def test_failed_entry_completion_remains_a_failure():
    from researchclaw.pipeline.stage_impls._code_generation import _ensure_executable_entry
    client=SimpleNamespace(chat=lambda *a,**kw:SimpleNamespace(content='def main():\n    pass',finish_reason='stop'))
    files={'main.py':'def main():\n    pass'}
    assert not _ensure_executable_entry(files,client)


@pytest.mark.asyncio
async def test_resume_uses_same_run_preserves_plan_and_reports_running(tmp_path,monkeypatch):
    import httpx
    from researchclaw.config import RCConfig
    from researchclaw.server.app import create_app
    from researchclaw.server.routes import pipeline
    from researchclaw.pipeline.stages import Stage,StageStatus
    from researchclaw.pipeline.executor import StageResult
    monkeypatch.chdir(tmp_path);monkeypatch.setattr(pipeline,'_active_run',None)
    config=RCConfig.from_dict({'project':{'name':'fixture'},'research':{'topic':'placeholder'},'runtime':{'timezone':'UTC'},
        'notifications':{'channel':'console'},'knowledge_base':{'root':str(tmp_path/'kb')},
        'llm':{'base_url':'https://example.org','api_key_env':'TEST'}},check_paths=False)
    app=create_app(config)
    run_id='rc-20261025-145941-c30e99';root=tmp_path/'artifacts'/run_id
    (root/'stage-09').mkdir(parents=True);(root/'stage-10').mkdir()
    (root/'checkpoint.json').write_text(json.dumps({'last_completed_stage':9}))
    (root/'pipeline_summary.json').write_text(json.dumps({'final_status':'failed','final_stage':10}))
    (root/'stage-10/decision.json').write_text(json.dumps({'error':"name '_pm' is not defined"}))
    (root/'research_brief.json').write_text(json.dumps({'topic':'真实原题','metrics':[{'metric_key':'CRPS','direction':'minimize'}]}))
    (root/'stage-09/exp_plan.yaml').write_text('original plan')
    calls=[]
    def execute(**kwargs):
        calls.append(kwargs)
        return [StageResult(stage=Stage.CODE_GENERATION,status=StageStatus.DONE,artifacts=()),
                StageResult(stage=Stage.CITATION_VERIFY,status=StageStatus.DONE,artifacts=())]
    monkeypatch.setattr('researchclaw.pipeline.runner.execute_pipeline',execute)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
        before=(await client.get('/api/projects')).json()['projects'][0]
        assert before['status']=='failed' and '_pm' in before['reason']
        response=await client.post('/api/pipeline/start',json={'resume_run_id':run_id,'from_stage':'CODE_GENERATION','auto_approve':True})
        assert response.status_code==200,response.text
        assert response.json()['run_id']==run_id
        await pipeline._run_task
        after=(await client.get('/api/projects')).json()['projects'][0]
        assert after['status']=='completed'
        invalid=await client.post('/api/pipeline/start',json={'resume_run_id':'../secret','from_stage':'CODE_GENERATION'})
        assert invalid.status_code==400
    assert len(calls)==1 and calls[0]['from_stage']==Stage.CODE_GENERATION
    assert calls[0]['config'].research.topic=='真实原题'
    assert calls[0]['config'].experiment.metric_key=='CRPS'
    assert (root/'stage-09/exp_plan.yaml').read_text()=='original plan'
    assert len(list((tmp_path/'artifacts').glob('rc-*')))==1
    assert (root/'resume_history.jsonl').is_file()
