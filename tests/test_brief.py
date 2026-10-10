"""Tests for researchclaw.ideation.brief + Chat BRIEF flow（离线冒烟）。

不依赖 GPU / LLM / 网络 / Docker / fastapi：覆盖 materialize 产物落盘与
stage-9 schema 守卫兼容、suggest_from_stage 逻辑、baseline_repo 拷贝排除规则、
选题产物复用、BRIEF 整稿审阅流程（plan-mode，mock LLM）、PipelineStartRequest 新字段
（fastapi/pydantic 缺失时自动跳过）。
pytest 缺失时也可直接 ``python3 tests/test_brief.py`` 跑全部用例。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from researchclaw.ideation.brief import (
    ResearchBrief,
    materialize,
    resolve_artifact_ref,
    suggest_from_stage,
)
from researchclaw.pipeline.stages import Stage


def _full_brief(**over) -> ResearchBrief:
    data = {
        "topic": "用边缘保持先验改进小样本遥感地物分割",
        "scientific_question": "边缘保持先验能否在小样本条件下提升分割边界质量？",
        "survey_summary": "现有小样本分割方法忽略边界约束，边缘区域 IoU 明显偏低。",
        "hypotheses": ["H1: 边缘保持损失提升边界 F1", "H2: 先验在小样本下收益更大"],
        "experiment_scope": "在 2 个数据集上对比 3 个基线，主指标 mIoU",
        "datasets": ["iSAID", "LoveDA"],
        "metrics": [{"metric_key": "mIoU", "direction": "maximize"}],
        "target_conference": "IEEE TGRS",
    }
    data.update(over)
    return ResearchBrief.from_dict(data)


# ---------------------------------------------------------------------------
# suggest_from_stage
# ---------------------------------------------------------------------------


class TestSuggestFromStage:
    def test_empty_brief(self):
        assert suggest_from_stage(ResearchBrief()) == Stage.TOPIC_INIT

    def test_hypotheses_only(self):
        brief = ResearchBrief.from_dict(
            {"topic": "t", "hypotheses": ["h1"], "survey_summary": "s"}
        )
        assert suggest_from_stage(brief) == Stage.EXPERIMENT_DESIGN

    def test_scientific_question_only(self):
        brief = ResearchBrief.from_dict({"topic": "t", "scientific_question": "q"})
        assert suggest_from_stage(brief) == Stage.EXPERIMENT_DESIGN

    def test_full_confirmed(self):
        assert suggest_from_stage(_full_brief()) == Stage.CODE_GENERATION

    def test_scope_trumps_missing_hypotheses(self):
        brief = ResearchBrief.from_dict({"topic": "t", "experiment_scope": "scope"})
        assert suggest_from_stage(brief) == Stage.CODE_GENERATION


# ---------------------------------------------------------------------------
# ResearchBrief.from_dict 容忍性
# ---------------------------------------------------------------------------


class TestFromDict:
    def test_defaults(self):
        brief = ResearchBrief.from_dict({})
        assert brief.topic == "" and brief.hypotheses == [] and brief.metrics == []

    def test_str_coercions(self):
        brief = ResearchBrief.from_dict(
            {"hypotheses": "single", "datasets": "ds1", "metrics": ["mIoU"]}
        )
        assert brief.hypotheses == ["single"]
        assert brief.datasets == ["ds1"]
        assert brief.metrics == [{"metric_key": "mIoU", "direction": ""}]

    def test_roundtrip(self):
        brief = _full_brief()
        assert ResearchBrief.from_dict(brief.to_dict()) == brief

    def test_non_dict(self):
        assert ResearchBrief.from_dict(None) == ResearchBrief()


# ---------------------------------------------------------------------------
# materialize：产物落盘 + schema 守卫兼容
# ---------------------------------------------------------------------------


def _schema_guard_ok(plan: dict) -> bool:
    """与 _experiment_design.py:352 同一判定：三者至少其一非空。"""
    return any(plan.get(k) for k in ("baselines", "proposed_methods", "ablations"))


class TestMaterialize:
    def test_full_brief_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            brief = _full_brief()
            stage = materialize(run_dir, brief, None)
            assert stage == Stage.CODE_GENERATION

            for rel in (
                "stage-01/goal.md",
                "stage-02/problem_tree.md",
                "stage-07/synthesis.md",
                "stage-08/hypotheses.md",
                "stage-09/exp_plan.yaml",
                "research_brief.json",
            ):
                assert (run_dir / rel).is_file(), f"missing {rel}"

            goal = (run_dir / "stage-01/goal.md").read_text(encoding="utf-8")
            assert brief.topic in goal and "mIoU" in goal and "IEEE TGRS" in goal
            hyps = (run_dir / "stage-08/hypotheses.md").read_text(encoding="utf-8")
            assert "H1" in hyps and "H2" in hyps

            plan = yaml.safe_load(
                (run_dir / "stage-09/exp_plan.yaml").read_text(encoding="utf-8")
            )
            assert _schema_guard_ok(plan)
            assert plan["datasets"] == ["iSAID", "LoveDA"]
            assert plan["metrics"] == ["mIoU"]

            record = json.loads(
                (run_dir / "research_brief.json").read_text(encoding="utf-8")
            )
            assert record["topic"] == brief.topic
            assert record["suggested_from_stage"] == "CODE_GENERATION"

    def test_hypotheses_only_no_exp_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            brief = ResearchBrief.from_dict(
                {"topic": "t", "scientific_question": "q", "hypotheses": ["h1"]}
            )
            stage = materialize(run_dir, brief, None)
            assert stage == Stage.EXPERIMENT_DESIGN
            assert not (run_dir / "stage-09" / "exp_plan.yaml").exists()
            assert (run_dir / "stage-08" / "hypotheses.md").is_file()

    def test_minimal_scope_still_passes_guard(self):
        """实验范围确认但其它都空：exp_plan 也必须过 schema 守卫。"""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            brief = ResearchBrief.from_dict(
                {"topic": "t", "experiment_scope": "做个对比实验"}
            )
            materialize(run_dir, brief, None)
            plan = yaml.safe_load(
                (run_dir / "stage-09" / "exp_plan.yaml").read_text(encoding="utf-8")
            )
            assert _schema_guard_ok(plan)

    def test_ideation_stages_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "id-test-run"
            for num in (3, 4, 5, 6):
                d = src / f"stage-{num:02d}"
                d.mkdir(parents=True)
                (d / "marker.txt").write_text(f"stage {num}", encoding="utf-8")
            run_dir = Path(tmp) / "run"
            brief = _full_brief(source_ideation_run=str(src))
            materialize(run_dir, brief, None)
            for num in (3, 4, 5, 6):
                assert (
                    run_dir / f"stage-{num:02d}" / "marker.txt"
                ).is_file(), f"stage-{num:02d} not copied"

    def test_baseline_repo_copy_excludes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repro = Path(tmp) / "reproduction" / "slug-x"
            repo = repro / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "__pycache__").mkdir()
            (repo / "__pycache__" / "m.cpython-310.pyc").write_text("x")
            (repo / "rc_reproduce_plan.json").write_text("{}")
            (repo / "rc_reproduce_entry.py").write_text("pass")
            (repo / "train.py").write_text("print('hi')")
            (repo / "README.md").write_text("# baseline")

            run_dir = Path(tmp) / "run"
            brief = _full_brief(reproduced_baseline=str(repro))
            materialize(run_dir, brief, None)

            dst = run_dir / "baseline_repo"
            assert (dst / "train.py").is_file()
            assert (dst / "README.md").is_file()
            assert not (dst / "rc_reproduce_plan.json").exists()
            assert not (dst / "rc_reproduce_entry.py").exists()
            assert not (dst / ".git").exists()
            assert not (dst / "__pycache__").exists()

            # exp_plan 的 baselines 标注 "our reproduction"
            plan = yaml.safe_load(
                (run_dir / "stage-09" / "exp_plan.yaml").read_text(encoding="utf-8")
            )
            names = json.dumps(plan["baselines"], ensure_ascii=False)
            assert "our reproduction" in names and "slug-x" in names

    def test_missing_refs_warn_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            brief = _full_brief(
                source_ideation_run="id-nonexistent",
                reproduced_baseline="artifacts/nope/reproduction/none",
            )
            stage = materialize(run_dir, brief, None)
            assert stage == Stage.CODE_GENERATION
            record = json.loads(
                (run_dir / "research_brief.json").read_text(encoding="utf-8")
            )
            assert len(record["warnings"]) >= 2
            assert not (run_dir / "baseline_repo").exists()


class TestResolveRef:
    def test_absolute(self):
        p = resolve_artifact_ref("/tmp/xyz")
        assert p == Path("/tmp/xyz")

    def test_artifacts_relative(self):
        p = resolve_artifact_ref("id-abc")
        assert p.name == "id-abc" and p.parent.name == "artifacts"


# ---------------------------------------------------------------------------
# BRIEF 步骤机（mock LLM）
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """整稿模式 fake：起草与修改都返回固定完整任务书 JSON（处理修改意见时目标会议换成 NeurIPS）。"""

    _DRAFT = {
        "topic": "用边缘保持先验改进小样本遥感地物分割",
        "scientific_question": "边缘保持先验能否提升小样本分割边界质量？",
        "survey_summary": "现有方法忽略边界约束。",
        "hypotheses": ["H1: 边缘损失提升边界 F1"],
        "experiment_scope": {
            "scope": "2 数据集 3 基线",
            "datasets": ["iSAID"],
            "metrics": [{"metric_key": "mIoU", "direction": "maximize"}],
        },
        "target_conference": "IEEE TGRS",
        "inferred": ["survey_summary", "target_conference"],
    }

    def chat(self, messages, system=None, json_mode=False, max_tokens=600):
        prompt = messages[-1]["content"]
        draft = dict(self._DRAFT)
        if "修改意见" in prompt:
            draft["target_conference"] = "NeurIPS"
            draft["inferred"] = ["survey_summary"]
        return _FakeResp(json.dumps(draft, ensure_ascii=False))


class _FakeStartResp:
    run_id = "rc-20990101-000000-abcdef"


def _new_session():
    from researchclaw.server.dialog.session import ChatSession

    return ChatSession(client_id="test-brief")


class TestBriefFlow:
    def _setup(self, monkeypatch_start=True):
        from researchclaw.server.dialog import router

        router._llm_client = _FakeLLM()  # _llm() 直接返回 fake
        if monkeypatch_start:
            async def _fake_start(brief):
                self.last_brief = brief
                return _FakeStartResp(), ""

            router._start_brief_pipeline = _fake_start
        self.last_brief = None
        return router

    def test_plan_mode_full_draft_once(self):
        """进入流程即一次性给出完整草案（不逐步提问），并标注 AI 推断字段。"""
        router = self._setup()
        session = _new_session()

        reply = asyncio.run(router._handle_brief("开始确认", session))
        assert session.pending.get("flow") == "brief"
        assert "step" not in session.pending
        for label in ("研究题目", "核心科学问题", "调研结论", "研究假设", "实验范围", "目标会议"):
            assert label in reply
        assert "AI 推断" in reply
        assert "确认" in reply
        collected = session.pending["collected"]
        assert collected["topic"] == "用边缘保持先验改进小样本遥感地物分割"

    def test_revision_updates_whole_draft(self):
        """修改意见 → 整稿更新：只改指定字段，其余保留，改动字段移出 inferred。"""
        router = self._setup()
        session = _new_session()
        asyncio.run(router._handle_brief("开始确认", session))

        reply = asyncio.run(router._handle_brief_flow("目标会议改成 NeurIPS", session))
        assert "NeurIPS" in reply
        assert "改动：目标会议/期刊" in reply
        collected = session.pending["collected"]
        assert collected["target_conference"] == "NeurIPS"
        assert collected["topic"] == "用边缘保持先验改进小样本遥感地物分割"  # 其余保留
        assert "target_conference" not in session.pending["inferred"]

    def test_confirm_starts_pipeline(self):
        router = self._setup()
        session = _new_session()
        asyncio.run(router._handle_brief("开始确认", session))
        reply = asyncio.run(router._handle_brief_flow("确认", session))
        # 一次确认后 pending 清空，启动被调用
        assert not session.pending
        assert self.last_brief is not None
        assert "rc-20990101-000000-abcdef" in reply
        assert "CODE_GENERATION" in reply
        brief = self.last_brief
        assert brief.topic == "用边缘保持先验改进小样本遥感地物分割"
        assert brief.datasets == ["iSAID"]
        assert brief.metrics[0]["metric_key"] == "mIoU"
        assert brief.target_conference == "IEEE TGRS"
        assert session.current_run == "rc-20990101-000000-abcdef"

    def test_cancel(self):
        router = self._setup()
        session = _new_session()
        asyncio.run(router._handle_brief("开始确认", session))
        reply = asyncio.run(router._handle_brief_flow("取消", session))
        assert not session.pending
        assert "已取消" in reply

    def test_fallback_when_llm_empty(self):
        """LLM 起草失败时：用户原话兜底为题目，其余槽位留空标待补充。"""
        from researchclaw.server.dialog import router

        class _EmptyLLM:
            def chat(self, messages, system=None, json_mode=False, max_tokens=600):
                return _FakeResp("not json at all")

        router._llm_client = _EmptyLLM()
        session = _new_session()
        reply = asyncio.run(router._handle_brief("开始确认：我想做城市内涝制图", session))
        collected = session.pending["collected"]
        assert "城市内涝" in collected["topic"]
        assert not collected["hypotheses"]
        assert "待补充" in reply


class TestIdeationPrefill:
    def test_card_prefill(self):
        from researchclaw.server.dialog import router

        router._llm_client = _FakeLLM()
        with tempfile.TemporaryDirectory() as tmp:
            # 造一个假选题报告并替换 router 的 REPO_ROOT 查找路径
            idr = Path(tmp) / "artifacts" / "id-test-1"
            idr.mkdir(parents=True)
            (idr / "ideation_report.json").write_text(
                json.dumps(
                    {
                        "cards": [
                            {
                                "rank": 1,
                                "question": "Q1 科学问题",
                                "note_zh": "注解",
                                "gap_evidence": ["证据甲"],
                                "novelty_hint": "新颖",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            old_root = router.REPO_ROOT
            router.REPO_ROOT = Path(tmp)
            try:
                session = _new_session()
                reply = asyncio.run(router._handle_brief("就选第 1 个，开始确认", session))
            finally:
                router.REPO_ROOT = old_root
            collected = session.pending.get("collected") or {}
            # 证据卡为权威材料：硬覆盖 LLM 起草值
            assert collected.get("topic") == "Q1 科学问题"
            assert collected.get("scientific_question") == "Q1 科学问题"
            assert "证据甲" in (collected.get("survey_summary") or "")
            assert session.pending.get("source_ideation_run") == "id-test-1"
            # 整稿一次给出，不再分步
            assert "step" not in session.pending
            assert "研究假设" in reply


# ---------------------------------------------------------------------------
# PipelineStartRequest 新字段（fastapi/pydantic 缺失时跳过）
# ---------------------------------------------------------------------------


class TestPipelineStartRequest:
    def test_new_fields(self):
        try:
            from researchclaw.server.routes.pipeline import (
                PipelineStartRequest,
                _parse_from_stage,
            )
        except ModuleNotFoundError:
            print("SKIP TestPipelineStartRequest (no fastapi/pydantic on host)")
            return

        req = PipelineStartRequest(
            topic="t",
            brief={"topic": "t", "hypotheses": ["h"]},
            from_stage="10",
        )
        data = req.model_dump()
        assert data["brief"]["hypotheses"] == ["h"]
        assert data["from_stage"] == "10"

        # 缺省为 None，老调用方行为不变
        req2 = PipelineStartRequest(topic="x")
        assert req2.brief is None and req2.from_stage is None

        assert _parse_from_stage("10") == Stage.CODE_GENERATION
        assert _parse_from_stage("stage-9") == Stage.EXPERIMENT_DESIGN
        assert _parse_from_stage("code_generation") == Stage.CODE_GENERATION
        assert _parse_from_stage("EXPERIMENT_DESIGN") == Stage.EXPERIMENT_DESIGN

        from fastapi import HTTPException

        for bad in ("99", "no_such_stage", "stage-x"):
            try:
                _parse_from_stage(bad)
            except HTTPException:
                pass
            else:
                raise AssertionError(f"expected 400 for {bad}")


if __name__ == "__main__":
    failures = 0
    for cls in list(vars().values()):
        if not (isinstance(cls, type) and cls.__name__.startswith("Test")):
            continue
        inst = cls()
        for name in dir(inst):
            if not name.startswith("test_"):
                continue
            try:
                getattr(inst, name)()
                print(f"PASS {cls.__name__}.{name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {cls.__name__}.{name}: {exc}")
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURES'}")
    sys.exit(1 if failures else 0)
