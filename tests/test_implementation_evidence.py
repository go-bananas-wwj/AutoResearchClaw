import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from researchclaw.pipeline.execution_contract import digest
from researchclaw.pipeline.generation_preview import GenerationPreview
from researchclaw.server.implementation import implementation_evidence, implementation_file


def fixture(root):
    (root / "stage-09").mkdir()
    (root / "stage-10/experiment").mkdir(parents=True)
    source = {"scientific_question": "用斜率估计预测观测"}
    plan = {"method_requirements": [{"id": "M1", "description": "斜率估计", "source_quote": "斜率估计",
                                    "acceptance_test": "无噪声恢复斜率"}]}
    (root / "research_brief.json").write_text(json.dumps(source))
    (root / "stage-09/exp_plan.yaml").write_text(yaml.safe_dump(plan))
    files = {"main.py": "def estimate():\n    return 2\n", "method_checks.py": "from main import estimate\nassert estimate() == 2\n", "config.json": '{"step": 0.1}'}
    for name, code in files.items():
        (root / "stage-10/experiment" / name).write_text(code)
    hashes = {name: hashlib.sha256(code.encode()).hexdigest() for name, code in files.items()}
    audit = {"ok": True, "code_hash": digest(files), "source_hash": digest(source), "plan_hash": digest(plan),
        "files": hashes, "requirements": [{"id": "M1", "status": "implemented",
            "code_evidence": [{"file": "main.py", "quote": "return 2"}],
            "test_evidence": [{"file": "method_checks.py", "quote": "assert estimate() == 2"}]}]}
    (root / "stage-10/method_acceptance.json").write_text(json.dumps(audit))
    return audit


def test_current_method_evidence_has_real_lines_and_needs_execution_acceptance(tmp_path):
    fixture(tmp_path)
    data = implementation_evidence(tmp_path, {"status": "failed"})
    assert data["methods"][0]["status"] == "reviewed"
    assert data["methods"][0]["code"][0]["line"] == 2
    assert not data["acceptance"]["ok"]
    (tmp_path / "stage-10/experiment/main.py").write_text("def estimate():\n    return 999\n")
    data = implementation_evidence(tmp_path, {"status": "running"})
    assert data["methods"][0]["status"] == "pending"
    assert data["methods"][0]["code"] == []


def test_only_matching_acceptance_marks_implementation_accepted(tmp_path):
    audit = fixture(tmp_path)
    (tmp_path / "stage-10/execution_acceptance.json").write_text(json.dumps(audit))
    assert implementation_evidence(tmp_path, {})["methods"][0]["status"] == "accepted"
    (tmp_path / "stage-10/experiment/extra.py").write_text("pass")
    data = implementation_evidence(tmp_path, {})
    assert not data["acceptance"]["ok"]
    assert not data["acceptance"]["review_bound"]


def test_new_refinement_candidate_does_not_inherit_stage10_acceptance(tmp_path):
    audit = fixture(tmp_path)
    (tmp_path / "stage-10/execution_acceptance.json").write_text(json.dumps(audit))
    candidate = tmp_path / "stage-13/experiment_v2"
    candidate.mkdir(parents=True)
    (candidate / "main.py").write_text("def estimate():\n    return 3\n")
    data = implementation_evidence(tmp_path, {"status":"running", "current_stage":13})
    assert not data["acceptance"]["ok"]
    assert data["methods"][0]["status"] == "pending"
    assert data["latest"]["target_file"].startswith("stage-13/experiment_v2")
    assert not any(item["path"].startswith("stage-10/experiment/") for item in data["files"])


def test_preview_endpoint_excludes_history_symlinks_credentials_and_stale_versions(tmp_path):
    fixture(tmp_path)
    experiment = tmp_path / "stage-10/experiment"
    (experiment / ".env").write_text("SECRET=hidden")
    (tmp_path / "recovery_history/old").mkdir(parents=True)
    (tmp_path / "recovery_history/old/main.py").write_text("old")
    (experiment / "old.py").symlink_to(tmp_path / "recovery_history/old/main.py")
    data = implementation_evidence(tmp_path, {})
    paths = [item["path"] for item in data["files"]]
    assert not any("history" in path or ".env" in path or "old.py" in path for path in paths)
    artifact = next(item for item in data["files"] if item["name"] == "main.py")
    assert "return 2" in implementation_file(tmp_path, artifact["path"], artifact["sha256"])["content"]
    with pytest.raises(FileNotFoundError):
        implementation_file(tmp_path, "../private.py", "x")
    (experiment / "main.py").write_text("changed")
    with pytest.raises(ValueError, match="版本"):
        implementation_file(tmp_path, artifact["path"], artifact["sha256"])


def test_filtered_preview_keeps_original_source_line_numbers(tmp_path):
    fixture(tmp_path)
    path = tmp_path / "stage-10/experiment/main.py"
    path.write_text("<think>private\nprivate</think>\n\ndef estimate():\n    return 2\n")
    artifact = next(item for item in implementation_evidence(tmp_path, {})["files"] if item["name"] == "main.py")
    preview = implementation_file(tmp_path, artifact["path"], artifact["sha256"])
    assert preview["line_numbers"][:2] == [4, 5]


def test_latest_activity_excludes_previous_attempt_and_keeps_failure_target(tmp_path):
    fixture(tmp_path)
    rows = [{"kind":"implementation","title":"Generating old.py (order=1)"},
            {"kind":"run","title":"科研任务开始执行","status":"running"},
            {"kind":"implementation","title":"Generating methods.py (order=2)","timestamp":"2026-10-27T03:00:00Z"},
            {"kind":"error","detail":"truncated","status":"failed"}]
    (tmp_path / "agent_activity.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    data = implementation_evidence(tmp_path, {"status":"failed"})
    assert data["latest"]["target_file"] == "methods.py"
    assert data["latest"]["error"] == "truncated"
    assert not any("old.py" in row["title"] for row in data["events"])


def test_stream_preview_hides_reasoning_credentials_and_resets_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "task-specific-secret-123")
    preview = GenerationPreview(tmp_path, "method.py")
    preview.content("<think>private reasoning</think>\n```python\nimport math\nkey='task-specific-secret-123'\n")
    preview.write("generating", force=True)
    data = json.loads(preview.path.read_text())
    assert "import math" in data["content"]
    assert "private reasoning" not in data["content"]
    assert "task-specific-secret-123" not in data["content"]
    preview.reset("deepseek-flash")
    assert json.loads(preview.path.read_text())["content"] == ""
    preview.content("```python\nimport math\nunfinished")
    preview.write("failed", force=True, error="truncated")
    assert json.loads(preview.path.read_text())["status"] == "failed"
    assert not (tmp_path / "native_draft/method.py").exists()


def test_native_stream_completed_file_remains_a_draft(tmp_path):
    from researchclaw.config import CodeAgentConfig
    from researchclaw.llm.client import LLMClient, LLMConfig, LLMResponse
    from researchclaw.pipeline.code_agent import CodeAgent
    from researchclaw.prompts import PromptManager
    llm = LLMClient(LLMConfig(base_url="https://api.deepseek.com/v1", api_key="fixture"))
    def request(*args, **kwargs):
        kwargs["on_reset"]("deepseek-flash")
        kwargs["on_content"]("```python\nimport math\nprint(math.pi)\n```")
        return LLMResponse(content="```python\nimport math\nprint(math.pi)\n```", model="deepseek-flash")
    agent = CodeAgent(llm=llm, prompts=PromptManager(), config=CodeAgentConfig(), stage_dir=tmp_path)
    with patch.object(llm, "chat", side_effect=request):
        files = agent._phase2_sequential_generate("topic", "plan", "metric", "", "", {"files":[{"name":"main.py","generation_order":1}]})
    assert (tmp_path / "native_draft/main.py").read_text() == files["main.py"]
    assert json.loads((tmp_path / ".implementation/live.json").read_text())["status"] == "saved"
    assert not (tmp_path / "execution_acceptance.json").exists()
