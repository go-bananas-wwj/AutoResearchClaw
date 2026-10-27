import json

import yaml

from researchclaw.server.research_workspace import workspace_evidence


def test_workspace_uses_only_current_formal_runs_and_original_claims(tmp_path):
    (tmp_path / "stage-09").mkdir()
    (tmp_path / "research_brief.json").write_text(json.dumps({"scientific_question": "原科学问题"}))
    (tmp_path / "stage-09/exp_plan.yaml").write_text(yaml.safe_dump({
        "method_requirements": [{"id": "M1", "description": "原方法要求", "acceptance_test": "实际断言"}],
        "execution_target": {"metric_key": "CRPS", "direction": "maximize"}}))
    (tmp_path / "metric_direction.json").write_text(json.dumps({"metric_key": "CRPS", "direction": "minimize"}))
    for folder, value in (("stage-12/runs", 2.0), ("stage-12_v1/runs", 999),
                          ("recovery_history/old/stage-12/runs", 999)):
        directory = tmp_path / folder
        directory.mkdir(parents=True)
        (directory / "run-1.json").write_text(json.dumps({"status": "completed", "metrics": {"CRPS": value}}))
    result = workspace_evidence(tmp_path)
    assert result["brief"]["scientific_question"] == "原科学问题"
    assert result["plan"]["method_requirements"][0]["description"] == "原方法要求"
    assert len(result["runs"]) == 1
    assert result["runs"][0]["metrics"] == {"CRPS": 2.0}
    assert result["target"]["direction"] == "minimize"
    assert result["acceptance"]["ok"] is False


def test_workspace_rejects_receipt_with_missing_code_and_reports_bad_material(tmp_path):
    (tmp_path / "stage-10").mkdir()
    (tmp_path / "stage-10/execution_acceptance.json").write_text(json.dumps({"ok": True, "files": {"main.py": "hash"}}))
    (tmp_path / "research_brief.json").write_text("invalid json")
    result = workspace_evidence(tmp_path)
    assert result["acceptance"]["ok"] is False
    assert result["acceptance"]["error"]
    assert result["read_errors"][0]["path"] == "research_brief.json"


def test_workspace_does_not_read_credentials_from_brief_or_plan(tmp_path):
    (tmp_path / "research_brief.json").write_text(json.dumps({"scientific_question": "question", "api_key": "private"}))
    assert "private" not in json.dumps(workspace_evidence(tmp_path))
