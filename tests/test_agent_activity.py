"""Real activity persistence, privacy, cursor replay and project API checks."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.pipeline.activity import consume_events, emit, observed_run, read_activity


def test_cursor_replay_survives_partial_lines_and_does_not_duplicate(tmp_path):
    emit("tool", "读取数据", "ok", run_dir=tmp_path)
    first = read_activity(tmp_path)
    assert len(first["events"]) == 1
    with (tmp_path / "agent_activity.jsonl").open("a") as stream:
        stream.write('{"title":"运行命令","kind":"tool"')
    partial = read_activity(tmp_path, first["cursor"])
    assert partial["events"] == [] and partial["cursor"] == first["cursor"]
    with (tmp_path / "agent_activity.jsonl").open("a") as stream:
        stream.write('}\n')
    second = read_activity(tmp_path, first["cursor"])
    assert [e["title"] for e in second["events"]] == ["运行命令"]
    assert read_activity(tmp_path, second["cursor"])["events"] == []


def test_raw_agent_tools_are_live_but_reasoning_and_credentials_are_not(tmp_path, monkeypatch):
    root = tmp_path / "rc-20261025-145941-c30e99"
    workspace = root / "stage-10/opencode-work"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("TEST_API_KEY", "secret-value-from-environment")
    raw = workspace / ".rc_agent_events.jsonl"
    raw.write_text(json.dumps({"type": "reasoning", "part": {"text": "PRIVATE_THOUGHT"}}) + "\n")
    with raw.open("a") as stream:
        stream.write(json.dumps({"type": "tool_use", "part": {"tool": "bash", "callID": "call1",
            "state": {"status": "completed", "input": {"command": "python main.py"},
                      "output": "rmse: 0.5 secret-value-from-environment"}}}) + "\n")
    cursor = consume_events(raw, 0, workspace, "deepseek-flash")
    events = read_activity(root)["events"]
    assert len(events) == 1 and "执行命令" in events[0]["title"]
    assert "rmse: 0.5" in events[0]["detail"]
    assert "secret-value" not in json.dumps(events)
    assert "PRIVATE_THOUGHT" not in json.dumps(events)
    assert consume_events(raw, cursor, workspace, "deepseek-flash") == cursor
    assert len(read_activity(root)["events"]) == 1


def test_activity_context_does_not_leak_to_another_project(tmp_path):
    @observed_run
    def run(*, run_dir):
        emit("model", "正在请求模型", model="deepseek-flash")
        return []
    run(run_dir=tmp_path)
    count = len(read_activity(tmp_path)["events"])
    emit("model", "项目外请求")
    assert len(read_activity(tmp_path)["events"]) == count


@pytest.mark.asyncio
async def test_project_activity_api_is_scoped_and_reports_running_stage(tmp_path, monkeypatch):
    import httpx
    from researchclaw.config import RCConfig
    from researchclaw.server.app import create_app
    from researchclaw.server.routes import pipeline
    monkeypatch.chdir(tmp_path)
    run_id = "rc-20261025-145941-c30e99"
    root = tmp_path / "artifacts" / run_id
    (root / "stage-10").mkdir(parents=True)
    (root / "checkpoint.json").write_text(json.dumps({"last_completed_stage": 9}))
    (root / "ideation_origin.json").write_text(json.dumps({"key": "is-fixture:1:r1-spark-1"}))
    (root / "heartbeat.json").write_text(json.dumps({"last_stage": 10, "last_stage_name": "CODE_GENERATION"}))
    emit("plan", "修订实验计划", "负对照需要修改", run_dir=root)
    monkeypatch.setattr(pipeline, "_active_run", {"run_id": run_id, "status": "running"})
    config = RCConfig.from_dict({"project": {"name": "test"}, "research": {"topic": "test"},
        "runtime": {"timezone": "UTC"}, "notifications": {"channel": "console"},
        "knowledge_base": {"root": str(tmp_path / "kb")},
        "llm": {"base_url": "https://example.org", "api_key_env": "TEST"}}, check_paths=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config)), base_url="http://test") as client:
        response = await client.get(f"/api/runs/{run_id}/activity")
        assert response.status_code == 200
        data = response.json()
        assert data["project"]["current_stage"] == 10
        assert data["project"]["research_entry_stage"] == 9
        assert data["events"][0]["title"] == "修订实验计划"
        replay = await client.get(f"/api/runs/{run_id}/activity?after={data['cursor']}")
        assert replay.json()["events"] == []
        assert (await client.get(f"/api/runs/{run_id}/activity?after=-1")).status_code == 400
        run = (await client.get(f"/api/runs/{run_id}")).json()
        assert "stage-10" not in run["stages_completed"]  # An existing directory is not a completed stage.
        workspace = await client.get(f"/api/runs/{run_id}/research-workspace")
        assert workspace.status_code == 200
        assert workspace.json()["run_id"] == run_id
        assert workspace.json()["runs"] == []
        implementation = await client.get(f"/api/runs/{run_id}/implementation")
        assert implementation.status_code == 200
        assert implementation.json()["methods"] == []
        assert (await client.get(f"/api/runs/{run_id}/implementation/file", params={"path":"../secret", "sha256":"x"})).status_code == 404
        assert (await client.get('/api/runs/not-a-run/research-workspace')).status_code == 400


def test_resume_archives_failed_outputs_and_keeps_confirmed_plan(tmp_path):
    from researchclaw.server.routes.pipeline import _archive_resume_outputs
    from researchclaw.pipeline.stages import Stage
    (tmp_path / "stage-09").mkdir()
    (tmp_path / "stage-09/exp_plan.yaml").write_text("confirmed: plan")
    (tmp_path / "stage-10").mkdir()
    (tmp_path / "stage-10/main.py").write_text("broken")
    (tmp_path / "checkpoint.json").write_text(json.dumps({"last_completed_stage": 11}))
    backup = _archive_resume_outputs(tmp_path, Stage.CODE_GENERATION)
    assert (backup / "stage-10/main.py").read_text() == "broken"
    assert not (tmp_path / "stage-10").exists()
    assert (tmp_path / "stage-09/exp_plan.yaml").read_text() == "confirmed: plan"
    assert json.loads((tmp_path / "checkpoint.json").read_text())["last_completed_stage"] == 9


def test_experiment_stdout_is_visible_before_process_completes(tmp_path):
    import sys
    import threading
    import time
    from researchclaw.pipeline.activity import run_with_output
    staging = tmp_path / "sandbox"
    staging.mkdir()
    results = []
    command = [sys.executable, "-u", "-c", "import time; print('epoch=1 rmse: 0.4',flush=True); time.sleep(2); print('epoch=2 rmse: 0.3')"]
    thread = threading.Thread(target=lambda: results.append(run_with_output(command, staging, tmp_path, 5, "main.py")))
    thread.start()
    observed = []
    deadline = time.monotonic() + 1.8
    while time.monotonic() < deadline:
        observed = read_activity(tmp_path)["events"]
        if observed:
            break
        time.sleep(.05)
    assert thread.is_alive() and "epoch=1" in observed[0]["detail"]
    thread.join(timeout=5)
    assert results[0].returncode == 0
    assert "epoch=2" in results[0].stdout


def test_failed_stage_is_not_presented_as_a_deliverable_for_approval(tmp_path):
    from researchclaw.pipeline.executor import _run_hitl_post_stage, StageResult
    from researchclaw.pipeline.stages import Stage, StageStatus
    from researchclaw.adapters import AdapterBundle
    class Session:
        def should_pause_after(self, stage):
            raise AssertionError("Failed code must not request approval")
    adapters = AdapterBundle()
    adapters.hitl = Session()
    result = StageResult(stage=Stage.CODE_GENERATION, status=StageStatus.FAILED,
                         artifacts=(), error="output truncated")
    assert _run_hitl_post_stage(Stage.CODE_GENERATION, result, tmp_path, adapters) is result


def test_explicit_effort_keeps_thinking_enabled_without_raising_output_cap(monkeypatch):
    import urllib.request
    from researchclaw.llm.client import LLMClient, LLMConfig
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}).encode()
    def request(req, **kwargs):
        captured.update(json.loads(req.data))
        return Response()
    monkeypatch.setattr(urllib.request, "urlopen", request)
    client = LLMClient(LLMConfig(base_url="https://api.deepseek.com/v1", api_key="fixture",
        primary_model="deepseek-flash", fallback_models=[], thinking="enabled", reasoning_effort="low"))
    client.chat(messages=[{"role": "user", "content": "JSON"}], max_tokens=1024)
    assert captured["thinking"] == {"type": "enabled"}
    assert captured["reasoning_effort"] == "low"
    assert captured["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_recovery_retains_explicit_direction_when_old_brief_omitted_it(tmp_path, monkeypatch):
    import httpx
    from researchclaw.config import RCConfig
    from researchclaw.server.app import create_app
    from researchclaw.server.routes import pipeline
    from researchclaw.pipeline.executor import StageResult
    from researchclaw.pipeline.stages import Stage, StageStatus
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline, '_active_run', None)
    run_id = 'rc-20261025-145941-c30e99'
    root = tmp_path / 'artifacts' / run_id
    root.mkdir(parents=True)
    (root / 'checkpoint.json').write_text(json.dumps({'last_completed_stage': 9}))
    brief = {'topic': 'original', 'metrics': [{'metric_key': 'CRPS', 'direction': ''}]}
    (root / 'research_brief.json').write_text(json.dumps(brief))
    config = RCConfig.from_dict({'project': {'name': 'test'}, 'research': {'topic': 'placeholder'},
        'runtime': {'timezone': 'UTC'}, 'notifications': {'channel': 'console'},
        'knowledge_base': {'root': str(tmp_path / 'kb')}, 'experiment': {'metric_direction': 'maximize'},
        'llm': {'base_url': 'https://example.org', 'api_key_env': 'TEST'}}, check_paths=False)
    calls = []
    def execute(**kwargs):
        calls.append(kwargs['config'].experiment.metric_direction)
        return [StageResult(stage=Stage.CITATION_VERIFY, status=StageStatus.DONE, artifacts=())]
    monkeypatch.setattr('researchclaw.pipeline.runner.execute_pipeline', execute)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config)), base_url='http://test') as client:
        for extra in ({'config_overrides': {'metric_direction': 'minimize'}}, {}):
            response = await client.post('/api/pipeline/start', json={
                'resume_run_id': run_id, 'from_stage': 'CODE_GENERATION', 'auto_approve': True, **extra})
            assert response.status_code == 200
            await pipeline._run_task
    assert calls == ['minimize', 'minimize']
    assert json.loads((root / 'research_brief.json').read_text()) == brief


def test_failed_gate_stage_stays_failed_instead_of_becoming_approvable(tmp_path, monkeypatch):
    from researchclaw.config import RCConfig
    from researchclaw.adapters import AdapterBundle
    from researchclaw.pipeline import executor
    from researchclaw.pipeline.stages import Stage, StageStatus
    config = RCConfig.from_dict({'project': {'name': 'test'}, 'research': {'topic': 'test'},
        'runtime': {'timezone': 'UTC'}, 'notifications': {'channel': 'console'},
        'knowledge_base': {'root': str(tmp_path / 'kb')},
        'llm': {'base_url': 'https://example.org', 'api_key_env': 'TEST'}}, check_paths=False)
    monkeypatch.setattr(executor, '_read_prior_artifact', lambda *args: 'prior evidence')
    def fail(*args, **kwargs):
        return executor.StageResult(stage=Stage.EXPERIMENT_DESIGN, status=StageStatus.FAILED,
                                    artifacts=(), error='method mismatch')
    monkeypatch.setitem(executor._STAGE_EXECUTORS, Stage.EXPERIMENT_DESIGN, fail)
    result = executor.execute_stage(Stage.EXPERIMENT_DESIGN, run_dir=tmp_path, run_id='fixture',
                                    config=config, adapters=AdapterBundle(), auto_approve_gates=False)
    assert result.status == StageStatus.FAILED and result.error == 'method mismatch'
