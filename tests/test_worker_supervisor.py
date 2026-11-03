"""rc-worker：Web 只投递任务，看门狗续跑中断的项目（docs/AUTORESEARCH_ARCHITECTURE_CN.md §3.1）。"""
import json

import pytest

from researchclaw.worker import jobs
from researchclaw.worker.supervisor import Supervisor

RUN_ID = 'rc-20261025-145941-c30e99'


class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.code = None

    def poll(self):
        return self.code


def _run(tmp_path, state='queued', last_completed=9, **extra):
    root = tmp_path / 'artifacts' / RUN_ID
    (root / 'stage-09').mkdir(parents=True)
    (root / 'stage-10' / 'native_draft').mkdir(parents=True)
    (root / 'stage-09/exp_plan.yaml').write_text('plan')
    (root / 'checkpoint.json').write_text(json.dumps({'last_completed_stage': last_completed}))
    jobs.write_job(root, {'run_id': RUN_ID, 'state': state, 'config_path': 'cfg.yaml', 'overrides': {},
                          'from_stage': 10, 'queued_at': jobs.now(), **extra})
    return root


def _supervisor(tmp_path, procs, **kw):
    def spawn(run_dir):
        proc = FakeProc()
        procs.append(proc)
        return proc
    return Supervisor(tmp_path / 'artifacts', spawn=spawn, backoff_base_sec=0, **kw)


def test_launches_queued_job_and_records_pid(tmp_path):
    root = _run(tmp_path)
    procs = []
    sup = _supervisor(tmp_path, procs)
    sup.scan_once()
    job = jobs.read_job(root)
    assert job['state'] == 'running' and job['pid'] == 4242 and job['attempts'] == 1
    assert job['boot_id'] == sup.boot_id
    sup.scan_once()  # still alive -> no second launch
    assert len(procs) == 1


def test_child_death_resumes_from_checkpoint_and_preserves_evidence(tmp_path):
    root = _run(tmp_path)
    procs = []
    sup = _supervisor(tmp_path, procs)
    sup.scan_once()
    procs[0].code = -9  # killed without writing a final state
    sup.scan_once()
    job = jobs.read_job(root)
    # backoff 0 → relaunched within the same scan
    assert job['state'] == 'running' and job['auto_resumes'] == 1 and job['from_stage'] == 10
    assert len(procs) == 2
    history = [json.loads(l) for l in (root / 'resume_history.jsonl').read_text().splitlines()]
    assert history[-1]['auto'] is True and history[-1]['from_stage'] == 'CODE_GENERATION'
    archived = root / history[-1]['archived']
    assert (archived / 'stage-10' / 'native_draft').is_dir()
    assert (root / 'stage-09/exp_plan.yaml').read_text() == 'plan'
    events = [json.loads(l)['kind'] for l in (root / 'worker_events.jsonl').read_text().splitlines()]
    assert events[:3] == ['launch', 'child_exit', 'auto_resume']


def test_new_supervisor_resumes_jobs_of_previous_boot(tmp_path):
    root = _run(tmp_path, state='running', last_completed=11, boot_id='old', pid=99)
    procs = []
    _supervisor(tmp_path, procs).scan_once()
    job = jobs.read_job(root)
    assert job['state'] == 'running' and job['from_stage'] == 12 and job['auto_resumes'] == 1


def test_finished_failure_is_not_retried(tmp_path):
    root = _run(tmp_path)
    procs = []
    sup = _supervisor(tmp_path, procs)
    sup.scan_once()
    jobs.update_job(root, state='failed', reason='方法审查未通过')
    procs[0].code = 0
    sup.scan_once()
    assert jobs.read_job(root)['state'] == 'failed' and len(procs) == 1


def test_resume_limit_gives_up_with_reason(tmp_path):
    root = _run(tmp_path, state='running', boot_id='old', auto_resumes=2)
    _supervisor(tmp_path, [], max_auto_resumes=2).scan_once()
    job = jobs.read_job(root)
    assert job['state'] == 'interrupted' and '上限' in job['reason']


def test_cancel_marks_stopped_instead_of_resuming(tmp_path):
    root = _run(tmp_path, state='stopping', boot_id='old')
    jobs.request_cancel(root)
    _supervisor(tmp_path, []).scan_once()
    assert jobs.read_job(root)['state'] == 'stopped'
    assert not jobs.cancel_requested(root)


def test_concurrency_limit_and_backoff(tmp_path):
    root = _run(tmp_path, not_before='2999-01-01T00:00:00+00:00')
    procs = []
    _supervisor(tmp_path, procs).scan_once()
    assert jobs.read_job(root)['state'] == 'queued' and not procs


def test_child_executes_job_and_writes_final_state(tmp_path, monkeypatch):
    from researchclaw.pipeline.executor import StageResult
    from researchclaw.pipeline.stages import Stage, StageStatus
    from researchclaw.worker import child, execution

    root = _run(tmp_path, state='running', auto_approve=True)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return [StageResult(stage=Stage.CITATION_VERIFY, status=StageStatus.DONE, artifacts=())]

    class Cfg:
        knowledge_base = type('KB', (), {'root': ''})()
    monkeypatch.setattr('researchclaw.pipeline.runner.execute_pipeline', execute)
    monkeypatch.setattr(execution, 'load_job_config', lambda job: Cfg())
    assert child.main([str(root)]) == 0
    job = jobs.read_job(root)
    assert job['state'] == 'completed' and job['pid'] is None and job['materialized'] is True
    assert calls[0]['from_stage'] == Stage.CODE_GENERATION


def test_overrides_roundtrip():
    from researchclaw.config import RCConfig
    from researchclaw.worker.execution import apply_overrides, config_overrides

    base = RCConfig.from_dict({'project': {'name': 'f'}, 'research': {'topic': 'x'}, 'runtime': {'timezone': 'UTC'},
        'notifications': {'channel': 'console'}, 'knowledge_base': {'root': 'kb'},
        'llm': {'base_url': 'https://example.org', 'api_key_env': 'TEST'}}, check_paths=False)
    changed = apply_overrides(base, {'research.topic': '真实原题', 'experiment.metric_direction': 'minimize'})
    again = apply_overrides(base, config_overrides(changed))
    assert again.research.topic == '真实原题' and again.experiment.metric_direction == 'minimize'
    with pytest.raises(ValueError):
        apply_overrides(base, {'llm.api_key_env': 'X'})


@pytest.mark.asyncio
async def test_worker_mode_route_only_queues(tmp_path, monkeypatch):
    import dataclasses

    import httpx
    from researchclaw.config import RCConfig
    from researchclaw.server.app import _app_state, create_app
    from researchclaw.server.routes import pipeline

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline, '_active_run', None)
    config = RCConfig.from_dict({'project': {'name': 'f'}, 'research': {'topic': 'placeholder'},
        'runtime': {'timezone': 'UTC'}, 'notifications': {'channel': 'console'},
        'knowledge_base': {'root': str(tmp_path / 'kb')}, 'server': {'executor': 'worker'},
        'llm': {'base_url': 'https://example.org', 'api_key_env': 'TEST'}}, check_paths=False)
    assert config.server.executor == 'worker'
    app = create_app(config)
    _app_state['config_path'] = str(tmp_path / 'cfg.yaml')
    root = _run(tmp_path, state='failed')
    (root / 'research_brief.json').write_text(json.dumps({'topic': '真实原题', 'metrics': [{'metric_key': 'CRPS', 'direction': 'minimize'}]}))
    monkeypatch.setattr('researchclaw.pipeline.runner.execute_pipeline', lambda **kw: pytest.fail('web must not execute'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://f') as client:
        response = await client.post('/api/pipeline/start', json={'resume_run_id': RUN_ID, 'from_stage': 'CODE_GENERATION'})
        assert response.status_code == 200 and response.json()['status'] == 'queued'
        job = jobs.read_job(root)
        assert job['state'] == 'queued' and job['from_stage'] == 10
        assert job['overrides']['research.topic'] == '真实原题'
        assert job['overrides']['experiment.metric_direction'] == 'minimize'
        dup = await client.post('/api/pipeline/start', json={'resume_run_id': RUN_ID, 'from_stage': 'CODE_GENERATION'})
        assert dup.status_code == 409
        status = (await client.get('/api/pipeline/status')).json()
        assert status['run_id'] == RUN_ID and status['status'] == 'queued'
        project = (await client.get('/api/projects')).json()['projects'][0]
        assert project['status'] == 'queued'
        stop = await client.post('/api/pipeline/stop')
        assert stop.status_code == 200 and jobs.read_job(root)['state'] == 'stopped'
    _app_state.pop('config_path', None)
