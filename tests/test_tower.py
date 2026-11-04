"""Tower 式多智能体实现：mission 分支并行、scope 检查、独立审查、合并、恢复不重做、集成修复分派。"""
from __future__ import annotations

import json
import re
import threading
import time

import pytest

from researchclaw.llm.client import LLMResponse
from researchclaw.pipeline import tower as tw
from researchclaw.pipeline.patch_agent import DONE

SPEC = {"hashes": {"plan": "p", "brief": "b"}, "primary_metric": {"key": "CRPS", "direction": "minimize"},
        "method_requirements": [{"id": "M1", "description": "wrap", "acceptance_test": "W(x+2pi)=W(x)"},
                                {"id": "M2", "description": "coherence", "acceptance_test": "g(0)=1"}]}

LAYOUT = f"<<<FILE main.py>>>\nimport methods\n<<<END>>>\n{DONE}\n"


def _edit(path, search, replace):
    return f"<<<EDIT {path}>>>\n<<<SEARCH>>>\n{search}\n<<<REPLACE>>>\n{replace}\n<<<END>>>\n{DONE}\n"


class FakeDeepSeek:
    """按提示词内容扮演 worker / reviewer / 协调者；记录并发度与调用。"""

    def __init__(self, workers=None, reviewer=None, deps=None):
        self.workers = workers or {}
        self.reviewer = reviewer
        self.deps = deps or {}
        self.calls: list[str] = []
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def chat(self, messages, system=None, **kw):
        prompt = messages[0]["content"]
        if system == tw.REVIEW_SYSTEM:
            data = json.loads(prompt)
            self.calls.append("review:" + data["mission"])
            if self.reviewer:
                return LLMResponse(content=json.dumps(self.reviewer(data)), model="m", finish_reason="stop")
            added, current = [], None
            for line in data["diff"].splitlines():
                if line.startswith("+++ b/"):
                    current = line[6:]
                elif line.startswith("+") and line[1:].strip():
                    added.append((current, line[1:]))
            return LLMResponse(content=json.dumps({"approve": True, "issues": [],
                               "evidence": [{"file": added[-1][0], "quote": added[-1][1]}]}),
                               model="m", finish_reason="stop")
        if system and "实现协调者" in system:
            self.calls.append("coordinator")
            return LLMResponse(content=json.dumps({"deps": self.deps}), model="m", finish_reason="stop")
        step = re.search(r"## 当前步骤：(\S+)", prompt).group(1)
        mission, attempt = step.split("#")
        self.calls.append(step)
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        reply = self.workers.get(mission, self.workers.get(step))
        if callable(reply):
            reply = reply(int(attempt))
        if reply is None:
            reply = {"layout": LAYOUT,
                     "M1": f"<<<FILE methods/m1.py>>>\ndef f():\n    return 'm1 done'\n<<<END>>>\n{DONE}\n",
                     "M2": f"<<<FILE methods/m2.py>>>\ndef f():\n    return 'm2 done'\n<<<END>>>\n{DONE}\n",
                     "wire": _edit("main.py", "import methods", "import methods\nprint('CRPS: 1.0')")}.get(mission)
        return LLMResponse(content=reply, model="m", finish_reason="stop")


def _tower(tmp_path, llm, **kw):
    return tw.Tower(llm, run_dir=tmp_path, spec=SPEC, plan_text="plan", max_parallel=kw.pop("max_parallel", 3), **kw)


def test_missions_run_in_parallel_review_and_merge(tmp_path):
    llm = FakeDeepSeek()
    files = _tower(tmp_path, llm).run()
    assert "m1 done" in files["methods/m1.py"] and "m2 done" in files["methods/m2.py"]
    assert "CRPS: 1.0" in files["main.py"]
    assert llm.peak >= 2  # M1 与 M2 互不依赖，在各自 worktree 并行实现
    events = [json.loads(line) for line in (tmp_path / ".tower" / "comms.jsonl").read_text().splitlines()]
    assert [e["mission"] for e in events if e["event"] == "合并"][0] == "layout"
    assert {e["mission"] for e in events if e["event"] == "合并"} == {"layout", "M1", "M2", "wire"}
    state = json.loads((tmp_path / ".tower" / "missions.json").read_text())
    assert all(m["status"] == "merged" for m in state["missions"])


def test_out_of_scope_edit_is_rejected_then_redone(tmp_path):
    llm = FakeDeepSeek(workers={"M1": lambda attempt: f"<<<FILE methods/m2.py>>>\nstolen = 1\n<<<END>>>\n{DONE}\n"
                                if attempt == 0 else f"<<<FILE methods/m1.py>>>\ndef f():\n    return 'm1 done'\n<<<END>>>\n{DONE}\n"})
    files = _tower(tmp_path, llm).run()
    assert "m1 done" in files["methods/m1.py"] and "m2 done" in files["methods/m2.py"]
    assert "M1#0" in llm.calls and "M1#1" in llm.calls
    comms = (tmp_path / ".tower" / "comms.jsonl").read_text()
    assert "scope" in comms


def test_reviewer_quote_must_be_verbatim(tmp_path):
    llm = FakeDeepSeek(reviewer=lambda data: {"approve": True, "issues": [],
                                              "evidence": [{"file": "methods/m1.py", "quote": "invented code"}]})
    with pytest.raises(tw.TowerError, match="逐字"):
        _tower(tmp_path, llm, max_attempts=2).run()


def test_resume_does_not_redo_merged_missions_and_repair_reopens_owner(tmp_path):
    first = FakeDeepSeek()
    _tower(tmp_path, first).run()
    again = FakeDeepSeek()
    _tower(tmp_path, again).run()
    assert again.calls == []  # 全部已合并：重启后不调用模型
    fixer = FakeDeepSeek(workers={"M2#0": _edit("methods/m2.py", "    return 'm2 done'", "    return 'm2 fixed'")})
    feedback = 'Traceback (most recent call last):\n  File "/workspace/methods/m2.py", line 2, in f\nValueError: bad\n'
    files = _tower(tmp_path, fixer).repair(feedback)
    assert "m2 fixed" in files["methods/m2.py"]
    assert [c for c in fixer.calls if not c.startswith("review")] == ["M2#0"]  # 只重做拥有出错文件的 mission


def test_audit_missing_requirement_reopens_that_mission(tmp_path):
    _tower(tmp_path, FakeDeepSeek()).run()
    fixer = FakeDeepSeek(workers={"M1#0": _edit("methods/m1.py", "    return 'm1 done'", "    return 'm1 complete'")})
    audit = {"issues": ["M1 未接入主流程"], "requirements": [{"id": "M1", "status": "missing"},
                                                         {"id": "M2", "status": "implemented"}]}
    files = _tower(tmp_path, fixer).repair("方法实现未通过验收：M1 未接入主流程", audit)
    assert "m1 complete" in files["methods/m1.py"]
    assert [c for c in fixer.calls if not c.startswith("review")] == ["M1#0"]
    comms = (tmp_path / ".tower" / "comms.jsonl").read_text()
    assert "分派集成修复" in comms


def test_spec_change_archives_old_tower(tmp_path):
    _tower(tmp_path, FakeDeepSeek()).run()
    changed = dict(SPEC, hashes={"plan": "p2", "brief": "b"})
    tw.Tower(FakeDeepSeek(), run_dir=tmp_path, spec=changed, plan_text="plan")
    assert list(tmp_path.glob(".tower_superseded_*")) and not (tmp_path / ".tower").exists()


def test_scope_matching_is_per_path_segment():
    assert tw.in_scope("main.py", ["*.py"]) and not tw.in_scope("methods/m1.py", ["*.py"])
    assert tw.in_scope("methods/m1_util.py", ["methods/m1_*.py"]) and tw.in_scope("a/b/c.py", ["**"])


def test_coordinator_dependency_cycle_is_ignored(tmp_path):
    llm = FakeDeepSeek(deps={"M1": ["M2"], "M2": ["M1"]})
    t = _tower(tmp_path, llm)
    t.plan()
    assert t.missions["M1"].deps == ["layout"] and t.missions["M2"].deps == ["layout"]


def test_acceptance_repair_routes_to_tower_backend(tmp_path):
    from types import SimpleNamespace
    from researchclaw.pipeline import execution_contract as ec
    (tmp_path / "stage-09").mkdir()
    (tmp_path / "stage-09" / "experiment_spec.json").write_text(json.dumps(SPEC))
    stage = tmp_path / "stage-10"
    stage.mkdir()
    config = SimpleNamespace(experiment=SimpleNamespace(
        opencode=SimpleNamespace(enabled=False),
        code_agent=SimpleNamespace(backend="tower", step_max_tokens=16384, tower_parallel=2, tower_max_attempts=3)))
    from researchclaw.pipeline.stage_impls._code_generation import _tower
    _tower(config, tmp_path, FakeDeepSeek(), SPEC, "plan").run()
    fixer = FakeDeepSeek(workers={"M1#0": _edit("methods/m1.py", "    return 'm1 done'", "    return 'm1 v2'")})
    (stage / "method_acceptance.json").write_text(json.dumps(
        {"issues": ["M1 不对"], "requirements": [{"id": "M1", "status": "missing"}]}))
    files = ec.repair_acceptance_code({}, "方法实现未通过验收：M1 不对", config, fixer, None, stage,
                                      exp_plan="plan", metric="CRPS")
    assert "m1 v2" in files["methods/m1.py"] and ec.REPAIR_STATS["model_calls"] >= 1


def test_account_error_pauses_without_burning_attempts(tmp_path):
    from researchclaw.llm.client import LLMAccountError

    class Broke(FakeDeepSeek):
        def chat(self, messages, system=None, **kw):
            if system and "实现协调者" in system:
                return super().chat(messages, system=system, **kw)
            raise LLMAccountError("余额不足")
    with pytest.raises(LLMAccountError):
        _tower(tmp_path, Broke()).run()
    state = json.loads((tmp_path / ".tower" / "missions.json").read_text())
    assert all(m["status"] == "planned" and m["attempts"] == 0 for m in state["missions"])
    files = _tower(tmp_path, FakeDeepSeek()).run()  # 账户恢复后原样继续
    assert "m1 done" in files["methods/m1.py"]


def test_client_maps_402_to_account_error(monkeypatch):
    import io
    import urllib.error
    from researchclaw.llm.client import LLMAccountError, LLMClient, LLMConfig
    client = LLMClient(LLMConfig(base_url="https://x", api_key="k", primary_model="deepseek-flash"))

    def boom(*a, **k):
        raise urllib.error.HTTPError("https://x", 402, "Payment Required", {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(client, "_raw_call", boom)
    with pytest.raises(LLMAccountError, match="余额不足"):
        client.chat([{"role": "user", "content": "hi"}])


def test_relative_run_dir_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifacts" / "rc-x").mkdir(parents=True)
    files = tw.Tower(FakeDeepSeek(), run_dir=tw.Path("artifacts/rc-x"), spec=SPEC, plan_text="plan").run()
    assert "m1 done" in files["methods/m1.py"]


def test_host_scaffold_and_runner_are_protected(tmp_path):
    llm = FakeDeepSeek(workers={"layout": lambda attempt: f"<<<FILE method_checks.py>>>\nprint('RC_METHOD_CHECKS_OK')\n<<<END>>>\n{DONE}\n"
                                if attempt == 0 else LAYOUT})
    files = _tower(tmp_path, llm).run()
    assert "importlib.import_module" in files["method_checks.py"] and "assert ran ==" in files["method_checks.py"]
    assert "layout#1" in llm.calls  # 改宿主运行器被判越界，退回重做
    assert [r for r in tw.scaffold(SPEC["method_requirements"])] == [
        "methods/__init__.py", "checks/__init__.py", "methods/m1.py", "checks/check_m1.py",
        "methods/m2.py", "checks/check_m2.py", "method_checks.py"]


def test_only_blocking_issues_reject_and_reviewer_sees_history(tmp_path):
    seen = []

    def reviewer(data):
        seen.append(data["previous_feedback"])
        added = [l[1:] for l in data["diff"].splitlines() if l.startswith("+") and not l.startswith("+++") and l[1:].strip()]
        file = [l[6:] for l in data["diff"].splitlines() if l.startswith("+++ b/")][-1]
        quote = [l[1:] for l in data["diff"].splitlines() if l.startswith("+") and not l.startswith("+++")
                 and l[1:].strip()][-1]
        if data["mission"] == "M1" and not data["previous_feedback"]:
            return {"blocking": ["M1 没有实现 wrap"], "suggestions": [], "evidence": []}
        return {"blocking": [], "suggestions": ["可以多加注释"], "evidence": [{"file": file, "quote": quote}]}
    files = _tower(tmp_path, FakeDeepSeek(reviewer=reviewer)).run()
    assert "m1 done" in files["methods/m1.py"]
    assert ["M1 没有实现 wrap"] in seen  # 第二次审查看到了第一次的阻断意见
    comms = (tmp_path / ".tower" / "comms.jsonl").read_text()
    assert "审查建议" in comms and comms.count("审查驳回") == 1


def test_workspace_has_host_spec_for_runtime(tmp_path):
    spec = dict(SPEC, prepared_data={"files": [{"path": "/data/x.npz"}]})
    files = tw.Tower(FakeDeepSeek(), run_dir=tmp_path, spec=spec, plan_text="plan").run()
    assert json.loads(files["experiment_spec.json"])["prepared_data"]["files"][0]["path"] == "/data/x.npz"
    assert "experiment_spec.json" in tw.HOST_OWNED


def test_real_check_failure_is_fed_back_and_retry_continues_from_branch(tmp_path):
    import sys
    from researchclaw.config import ExperimentConfig, SandboxConfig
    exp = ExperimentConfig(mode="sandbox", time_budget_sec=60, sandbox=SandboxConfig(python_path=sys.executable))
    runner = tw.make_check_runner(exp, tmp_path / "chk", 60)
    m1_first = (f"<<<FILE methods/m1.py>>>\ndef f():\n    return 'm1 done'\n<<<END>>>\n"
                f"<<<FILE checks/check_m1.py>>>\nfrom methods import m1\n\n\ndef check():\n"
                f"    assert m1.f() == 'wrong', 'mismatch'\n<<<END>>>\n{DONE}\n")
    m1_fix = _edit("checks/check_m1.py", "    assert m1.f() == 'wrong', 'mismatch'", "    assert m1.f() == 'm1 done'")
    m2 = (f"<<<FILE methods/m2.py>>>\ndef f():\n    return 'm2 done'\n<<<END>>>\n"
          f"<<<FILE checks/check_m2.py>>>\nfrom methods import m2\n\n\ndef check():\n    assert m2.f() == 'm2 done'\n"
          f"<<<END>>>\n{DONE}\n")
    prompts = {}

    class Rec(FakeDeepSeek):
        def chat(self, messages, system=None, **kw):
            m = re.search(r"## 当前步骤：(\S+)", messages[0]["content"])
            if m:
                prompts[m.group(1)] = messages[0]["content"]
            return super().chat(messages, system=system, **kw)
    llm = Rec(workers={"M1#0": m1_first, "M1#1": m1_fix, "M2": m2})
    files = tw.Tower(llm, run_dir=tmp_path, spec=SPEC, plan_text="plan", check_runner=runner).run()
    assert "assert m1.f() == 'm1 done'" in files["checks/check_m1.py"]
    assert "mismatch" in prompts["M1#1"] and "AssertionError" in prompts["M1#1"]  # 真实 traceback 反馈给 worker
    assert "return 'm1 done'" in prompts["M1#1"]  # 第二次在上次提交的分支上继续，而不是从 base 重来
    assert "review:M1" in llm.calls and llm.calls.count("review:M1") == 1  # 检查失败的那次没有送审


def test_failed_mission_reopens_only_after_framework_change(tmp_path, monkeypatch):
    bad = FakeDeepSeek(workers={"M1": f"<<<FILE methods/m2.py>>>\nx = 1\n<<<END>>>\n{DONE}\n"})
    with pytest.raises(tw.TowerError):
        _tower(tmp_path, bad).run()
    with pytest.raises(tw.TowerError):  # 框架未变：仍是真失败，不重试
        _tower(tmp_path, FakeDeepSeek()).run()
    monkeypatch.setattr(tw, "framework_version", lambda: "changed")
    files = _tower(tmp_path, FakeDeepSeek()).run()  # 框架修复后恢复：重开失败的 mission
    assert "m1 done" in files["methods/m1.py"]
