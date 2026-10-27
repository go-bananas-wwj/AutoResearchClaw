"""Regression checks for real execution, resource limits and method evidence."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from researchclaw.config import ExperimentConfig, SandboxConfig
from researchclaw.llm.client import LLMClient, LLMConfig, LLMResponse
from researchclaw.pipeline.execution_contract import (
    ExecutionContractError, audit_implementation, digest, execution_failure, has_real_metric,
    prepare_agent_tools, validate_plan, validate_workspace, verify_accepted_code,
    repair_acceptance_code, accept_final_code,
)
from researchclaw.pipeline.opencode_bridge import OpenCodeBridge


def plan_fixture():
    source = {"experiment_scope": "通过加权最小二乘估计斜率，比较样本外误差。"}
    plan = {"execution": {"gpu_count": 1, "estimated_runtime_sec": 600,
        "memory_mb": 1024, "validation_level": "synthetic",
        "datasets": [{"id": "D1", "kind": "synthetic", "generator": "固定种子线性观测"}]},
        "method_requirements": [{"id": "M1", "source_quote": "通过加权最小二乘估计斜率",
            "description": "实现正规方程", "acceptance_test": "无噪声恢复正确斜率"}]}
    resources = {"gpu_count": 1, "time_budget_sec": 900, "memory_limit_mb": 24576, "datasets": []}
    return plan, resources, source


def test_disabled_opencode_repairs_via_native_llm_and_preserves_modules(tmp_path):
    from researchclaw.prompts import PromptManager
    from unittest.mock import Mock
    config = SimpleNamespace(experiment=replace(ExperimentConfig(),
        opencode=replace(ExperimentConfig().opencode, enabled=False)))
    llm = Mock()
    llm.chat.return_value = LLMResponse(content="```filename:main.py\nimport method\nprint('crps: 1')\n```", model="native")
    with patch.object(OpenCodeBridge, "generate", side_effect=AssertionError("OpenCode must stay disabled")):
        files = repair_acceptance_code({"main.py": "broken", "method.py": "slope = 2"},
            "main entry is broken", config, llm, PromptManager(), tmp_path,
            exp_plan="preserve the slope method", metric="crps")
    assert files["main.py"].startswith("import method")
    assert files["method.py"] == "slope = 2"
    assert llm.chat.call_args.kwargs["max_tokens"] == 32768


def test_native_acceptance_repair_without_code_fails(tmp_path):
    from researchclaw.prompts import PromptManager
    from unittest.mock import Mock
    config = SimpleNamespace(experiment=replace(ExperimentConfig(),
        opencode=replace(ExperimentConfig().opencode, enabled=False)))
    llm = Mock()
    llm.chat.return_value = LLMResponse(content="", model="native")
    with pytest.raises(ExecutionContractError, match="内置模型验收修复失败"):
        repair_acceptance_code({"main.py": "broken"}, "no entry", config, llm,
            PromptManager(), tmp_path, exp_plan="same method", metric="crps")


def test_refined_workspace_is_actually_validated_and_failure_is_rejected(tmp_path):
    plan, _, source = plan_fixture()
    workspace = tmp_path / "stage-13/experiment_v1"
    workspace.mkdir(parents=True)
    metadata = tmp_path / "stage-13/acceptance_v1"
    metadata.mkdir()
    config = SimpleNamespace(experiment=ExperimentConfig())
    with patch("researchclaw.pipeline.execution_contract.audit_implementation", return_value={
        "ok": True, "source_hash": digest(source)}), patch(
        "researchclaw.pipeline.execution_contract.validate_workspace", return_value=1) as validate:
        with pytest.raises(ExecutionContractError, match="实际执行"):
            accept_final_code({"main.py": "pass"}, plan, config, tmp_path, metadata,
                              object(), workspace=workspace)
    assert validate.call_args.args[0] == workspace
    assert not (metadata / "execution_acceptance.json").exists()


def test_four_gpus_and_200_hours_cannot_pass_v100_plan():
    plan, resources, source = plan_fixture()
    plan["execution"].update(gpu_count=4, estimated_runtime_sec=200 * 3600)
    errors = validate_plan(plan, resources, source)
    assert any("gpu_count" in s for s in errors)
    assert any("runtime" in s for s in errors)


def test_unavailable_real_data_and_invented_method_quote_are_rejected():
    plan, resources, source = plan_fixture()
    plan["execution"]["datasets"] = [{"id": "D1", "kind": "local", "path": "/missing/gnss.csv"}]
    plan["method_requirements"][0]["source_quote"] = "用 GRU 替代全部方法"
    errors = validate_plan(plan, resources, source)
    assert any("数据" in s for s in errors)
    assert any("source_quote" in s for s in errors)


def test_runtime_that_catches_all_exceptions_and_exits_zero_fails():
    result = SimpleNamespace(returncode=0, timed_out=False, stdout="[error] GRU got 4D\n",
                             metrics={"total_time_s": 2.9})
    assert execution_failure(result, "crps")
    result.stdout = ""  # Missing main guard also exits zero.
    assert execution_failure(result, "crps")


def test_seed_namespaces_and_scientific_time_endpoints_remain_valid():
    assert has_real_metric({"proposed/seed0/crps": 1.2}, "crps")
    assert has_real_metric({"failure_time_rmse": 2.5}, "failure_time_rmse")
    assert not has_real_metric({"total_time_s": 1.2})


def test_method_review_cannot_invent_code_evidence():
    plan, _, source = plan_fixture()
    reply = {"ok": True, "issues": [], "requirements": [{"id": "M1", "status": "implemented",
        "code_evidence": [{"file": "main.py", "quote": "solve_normal_equation()"}],
        "test_evidence": [{"file": "method_checks.py", "quote": "assert recovered == 2"}]}]}
    llm = SimpleNamespace(chat=lambda **kw: LLMResponse(content=json.dumps(reply), model="reviewer"))
    audit = audit_implementation({"main.py": "pass", "method_checks.py": "pass"}, plan, source, llm)
    assert not audit["ok"]
    assert any("证据" in s for s in audit["issues"])


def test_actual_subprocess_execution_requires_real_metric_and_method_assertions(tmp_path):
    import sys
    exp = ExperimentConfig(mode="sandbox", time_budget_sec=20, metric_key="rmse",
                           sandbox=SandboxConfig(python_path=sys.executable))
    prepare_agent_tools(tmp_path, exp)
    (tmp_path / "main.py").write_text(
        "import math\nxs=[0.,1.,2.]\nys=[1.,3.,5.]\n"
        "slope=sum(x*(y-1) for x,y in zip(xs,ys))/sum(x*x for x in xs)\n"
        "print('rmse: '+str(abs(slope-2)))\n")
    checks = tmp_path / "method_checks.py"
    checks.write_text("import main\nassert abs(main.slope-2.) < 1e-12\nprint('RC_METHOD_CHECKS_OK')\n")
    assert validate_workspace(tmp_path, timeout=20) == 0
    plan, _, _ = plan_fixture()
    (tmp_path / "EXPERIMENT_PLAN.yaml").write_text(yaml.safe_dump(plan, allow_unicode=True))
    assert validate_workspace(tmp_path, timeout=20) == 1  # A declared dataset is not proof of availability.
    with (tmp_path / "main.py").open("a") as code:
        code.write("import json\nfrom pathlib import Path\n"
            "Path('observations.json').write_text(json.dumps({'x':xs,'y':ys}))\n"
            "Path('data_manifest.json').write_text(json.dumps([{'id':'D1','path':'observations.json'}]))\n")
    assert validate_workspace(tmp_path, timeout=20) == 0
    receipts = json.loads((tmp_path / ".rc-validation/data_receipts.json").read_text())
    assert receipts[0]["bytes"] > 0 and len(receipts[0]["sha256"]) == 64
    checks.write_text("assert False, 'wrong method'\nprint('RC_METHOD_CHECKS_OK')\n")
    assert validate_workspace(tmp_path, timeout=20) == 1
    (tmp_path / "main.py").write_text("def main():\n    print('rmse: 0')\n")
    assert validate_workspace(tmp_path, timeout=20) == 1


def test_code_or_confirmed_method_change_invalidates_acceptance(tmp_path):
    plan, _, source = plan_fixture()
    (tmp_path / "stage-09").mkdir()
    (tmp_path / "stage-09/exp_plan.yaml").write_text(yaml.safe_dump(plan, allow_unicode=True))
    (tmp_path / "research_brief.json").write_text(json.dumps(source, ensure_ascii=False))
    stage = tmp_path / "stage-10"
    exp = stage / "experiment"
    exp.mkdir(parents=True)
    files = {"main.py": "print('error: 0')"}
    (exp / "main.py").write_text(files["main.py"])
    receipt = {"ok": True, "code_hash": digest(files), "plan_hash": digest(plan),
        "source_hash": digest(source), "files": {"main.py": hashlib.sha256(files["main.py"].encode()).hexdigest()}}
    (stage / "execution_acceptance.json").write_text(json.dumps(receipt))
    verify_accepted_code(exp, tmp_path)
    (exp / "main.py").write_text("print('error: 100')")
    with pytest.raises(ExecutionContractError):
        verify_accepted_code(exp, tmp_path)


def test_thinking_mode_preserves_requested_budget_and_rejects_incomplete_output():
    client = LLMClient(LLMConfig(base_url="https://api.deepseek.com/v1", api_key="test",
        primary_model="deepseek-flash", fallback_models=[], thinking="enabled"))
    with patch.object(client, "_call_with_retry", return_value=LLMResponse(content="ok", model="deepseek-flash")) as request:
        client.chat(messages=[{"role": "user", "content": "x"}], max_tokens=1024)
        assert request.call_args.args[2] == 1024
    with patch.object(client, "_call_with_retry", return_value=LLMResponse(content="partial", model="deepseek-flash", truncated=True)):
        with pytest.raises(RuntimeError, match="截断"):
            client.chat(messages=[{"role": "user", "content": "x"}])


def test_opencode_deepseek_uses_chat_provider_and_interleaved_reasoning():
    bridge = OpenCodeBridge(model="deepseek-flash", llm_base_url="https://api.deepseek.com/v1",
                            api_key_env="DEEPSEEK_API_KEY", thinking="enabled")
    config = bridge._build_opencode_config()
    provider = config["provider"]["deepseek"]
    model = provider["models"]["deepseek-flash"]
    assert provider["npm"] == "@ai-sdk/deepseek"
    assert model["interleaved"] == {"field": "reasoning_content"}
    assert config["model"] == bridge._resolve_opencode_model() == "deepseek/deepseek-flash"
    assert "{env:DEEPSEEK_API_KEY}" == provider["options"]["apiKey"]


def test_stage12_failure_starts_repair_without_a_stage14_summary(tmp_path):
    from researchclaw.config import ExperimentRepairConfig
    from researchclaw.pipeline.experiment_repair import run_repair_loop
    experiment = tmp_path / "stage-10/experiment"
    experiment.mkdir(parents=True)
    (experiment / "main.py").write_text("raise RuntimeError('bad shape')")
    runs = tmp_path / "stage-12/runs"
    runs.mkdir(parents=True)
    (runs / "run-1.json").write_text(json.dumps({"status": "failed", "metrics": {},
        "stdout": "[error] GRU: expected 3D, got 4D", "stderr": "bad shape", "returncode": 1}))
    config = SimpleNamespace(experiment=ExperimentConfig(mode="sandbox",
        repair=ExperimentRepairConfig(max_cycles=1, use_opencode=False)))
    with patch("researchclaw.llm.create_llm_client", return_value=SimpleNamespace()), patch(
        "researchclaw.pipeline.experiment_repair._get_repaired_code", return_value=None) as repair:
        result = run_repair_loop(tmp_path, config)
    assert result.total_cycles == 1
    assert "bad shape" in repair.call_args.args[0]


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="Linux process tree check")
def test_agent_timeout_terminates_descendant_processes(tmp_path):
    script = tmp_path / "fake-agent.sh"
    script.write_text("sleep 30 &\necho $! > child.pid\nwait\n")
    bridge = OpenCodeBridge(timeout_sec=1, experiment_config=ExperimentConfig(mode="sandbox"))
    with patch.object(bridge, "_build_opencode_command", return_value=["/bin/sh", str(script)]):
        ok, log, _ = bridge._invoke_opencode(tmp_path, "test timeout")
    assert not ok and "TIMEOUT" in log
    child = int((tmp_path / "child.pid").read_text())
    status = Path(f"/proc/{child}/stat")
    if status.exists():
        assert status.read_text().rsplit(")", 1)[1].split()[0] == "Z"
    assert (tmp_path / ".rc_agent_events.jsonl").is_file()


def test_repair_promotion_uses_current_cycle_and_real_best_run_metrics(tmp_path):
    from researchclaw.pipeline.runner import _promote_best_repair_code
    target = tmp_path / "stage-10/experiment"
    target.mkdir(parents=True)
    (target / "main.py").write_text("broken")
    for cycle in (1, 9):
        directory = tmp_path / f"stage-14_repair_v{cycle}"
        (directory / "experiment").mkdir(parents=True)
        (directory / "experiment/main.py").write_text(f"print('rmse: {cycle}')")
        (directory / "experiment_summary.json").write_text(json.dumps({
            "best_run": {"metrics": {"proposed/rmse": float(cycle)}},
            "metrics_summary": {}, "total_metric_keys": cycle}))
    (tmp_path / "experiment_repair_result.json").write_text(json.dumps({
        "cycle_history": [{"cycle": 1, "repair_applied": True, "error": ""}]}))
    assert _promote_best_repair_code(tmp_path, "test")
    assert (target / "main.py").read_text() == "print('rmse: 1')"
