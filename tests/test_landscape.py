"""Tests for researchclaw.ideation.landscape + 调研→insight 对话链路（离线冒烟）。

不依赖 GPU / 网络 / 真实 LLM；routes 里的 fastapi 依赖用 sys.modules 假模块顶替。
pytest 缺失时也可直接 ``python3 tests/test_landscape.py`` 跑全部用例。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """按 prompt 关键词返回讲解/精读/任务书整稿的固定内容，并记录所有 prompt。"""

    _BRIEF_JSON = {
        "topic": "SAR 城市内涝制图",
        "scientific_question": "极化特征能否提升边界精度？",
        "survey_summary": "缺口：城市区假阳性高。",
        "hypotheses": ["H1"],
        "experiment_scope": {"scope": "s", "datasets": ["d1"], "metrics": []},
        "target_conference": "IEEE TGRS",
        "inferred": ["survey_summary"],
    }

    def __init__(self):
        self.prompts: list[str] = []

    def chat(self, messages, system=None, json_mode=False, max_tokens=600):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        if "领域现状讲解" in prompt:
            return _FakeResp("## 这个领域在解决什么问题\n讲解正文：别人主要这么做。")
        if "批判性精读" in prompt:
            return _FakeResp("## 它怎么做\n方法概述\n## 可质疑的问题\nQ1 样本外泛化？")
        if json_mode:
            return _FakeResp(json.dumps(self._BRIEF_JSON, ensure_ascii=False))
        return _FakeResp("(空)")


def _make_ideation_run(tmp: Path, name: str = "id-test-1") -> Path:
    run = tmp / "artifacts" / name
    (run / "stage-05").mkdir(parents=True)
    papers = [
        {
            "paper_id": "oalex-W1",
            "title": "Flood mapping with SAR and U-Net",
            "year": 2023,
            "venue": "Remote Sensing",
            "abstract": "We propose a U-Net variant for flood extent mapping...",
            "keep_reason": "直接相关：SAR 洪水制图",
            "url": "https://doi.org/10.1000/xyz1",
            "doi": "10.1000/xyz1",
            "arxiv_id": "",
            "cite_key": "a2023flood",
        },
        {
            "paper_id": "oalex-W2",
            "title": "Polarimetric SAR water boundary refinement",
            "year": 2024,
            "venue": "IEEE TGRS",
            "abstract": "Polarimetric features improve water boundary delineation...",
            "keep_reason": "极化特征提升边界",
            "url": "",
            "doi": "",
            "arxiv_id": "2301.12345",
            "cite_key": "b2024polsar",
        },
    ]
    with (run / "stage-05" / "shortlist.jsonl").open("w", encoding="utf-8") as f:
        for p in papers:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    (run / "ideation_report.json").write_text(
        json.dumps({"ok": True, "direction": "灾害遥感", "cards": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    return run


def _ensure_fastapi_stub() -> None:
    """宿主机无 fastapi 时顶个假模块（handler 里只用 HTTPException）。"""
    try:
        import fastapi  # noqa: F401

        return
    except ModuleNotFoundError:
        pass

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: object = None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fake = types.ModuleType("fastapi")
    fake.HTTPException = _HTTPException
    sys.modules["fastapi"] = fake


def _install_fake_routes(module_name: str, **attrs) -> types.ModuleType:
    _ensure_fastapi_stub()
    fake = types.ModuleType(module_name)
    for k, v in attrs.items():
        setattr(fake, k, v)
    sys.modules[module_name] = fake
    return fake


def _new_session():
    from researchclaw.server.dialog.session import ChatSession

    return ChatSession(client_id="test-landscape")


class TestLandscapeModule:
    def test_load_shortlist_and_fallback(self):
        from researchclaw.ideation.landscape import load_shortlist

        with tempfile.TemporaryDirectory() as td:
            run = _make_ideation_run(Path(td))
            papers = load_shortlist(run)
            assert len(papers) == 2
            assert papers[0]["title"].startswith("Flood mapping")
            # 删掉 stage-05 → 回退 stage-04
            (run / "stage-05" / "shortlist.jsonl").unlink()
            (run / "stage-04").mkdir()
            (run / "stage-04" / "candidates.jsonl").write_text(
                json.dumps({"title": "fallback paper"}) + "\n", encoding="utf-8"
            )
            assert load_shortlist(run)[0]["title"] == "fallback paper"

    def test_format_shortlist(self):
        from researchclaw.ideation.landscape import format_shortlist, load_shortlist

        with tempfile.TemporaryDirectory() as td:
            run = _make_ideation_run(Path(td))
            text = format_shortlist(load_shortlist(run))
            assert "1. **Flood mapping with SAR and U-Net**（2023，Remote Sensing）" in text
            assert "2. **Polarimetric SAR water boundary refinement**" in text

    def test_build_landscape_caches(self):
        from researchclaw.ideation.landscape import build_landscape

        with tempfile.TemporaryDirectory() as td:
            run = _make_ideation_run(Path(td))
            llm = _FakeLLM()
            text1 = build_landscape(run, llm)
            assert "讲解正文" in text1
            assert (run / "survey_landscape_zh.md").is_file()
            text2 = build_landscape(run, llm)
            assert text2 == text1
            assert len(llm.prompts) == 1  # 第二次走缓存
            # 摘要要真的进了 prompt
            assert "U-Net" in llm.prompts[0] and "灾害遥感" in llm.prompts[0]

    def test_critical_reading_and_note(self):
        from researchclaw.ideation.landscape import (
            critical_reading,
            load_shortlist,
            save_paper_note,
        )

        with tempfile.TemporaryDirectory() as td:
            run = _make_ideation_run(Path(td))
            paper = load_shortlist(run)[0]
            note = critical_reading(paper, _FakeLLM())
            assert "可质疑的问题" in note
            path = save_paper_note(run, 1, paper, note)
            assert path.name == "01-a2023flood.md"
            assert "精读笔记" in path.read_text(encoding="utf-8")


class TestRouterJourney:
    def _setup_router(self, tmp: Path):
        from researchclaw.server.dialog import router

        router._llm_client = _FakeLLM()
        self._old_root = router.REPO_ROOT
        router.REPO_ROOT = tmp
        return router

    def _teardown_router(self, router):
        router.REPO_ROOT = self._old_root
        router._llm_client = None
        for name in (
            "researchclaw.server.routes.ideation",
            "researchclaw.server.routes.reproduce",
        ):
            sys.modules.pop(name, None)

    def test_landscape_branch(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _make_ideation_run(tmp)
            router = self._setup_router(tmp)

            async def _status():
                return {"status": "completed"}

            _install_fake_routes(
                "researchclaw.server.routes.ideation", ideation_status=_status
            )
            try:
                reply = asyncio.run(router._handle_ideate("讲一下现状", _new_session()))
            finally:
                self._teardown_router(router)
            assert "讲解正文" in reply
            assert "1. **Flood mapping" in reply
            assert "精读第 N 篇" in reply

    def test_paper_read_flow(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run = _make_ideation_run(tmp)
            router = self._setup_router(tmp)
            try:
                # 不带编号 → 列清单
                reply = asyncio.run(router._handle_paper_read("精读", _new_session()))
                assert "短名单共 2 篇" in reply
                # 带编号 → 精读笔记 + 落盘 + 下一步指引
                reply = asyncio.run(
                    router._handle_paper_read("精读第 1 篇", _new_session())
                )
                assert "可质疑的问题" in reply
                assert "复现第 1 篇" in reply
                assert list((run / "paper_notes").glob("01-*.md"))
                # 越界编号
                reply = asyncio.run(
                    router._handle_paper_read("精读第 9 篇", _new_session())
                )
                assert "只有 2 篇" in reply
            finally:
                self._teardown_router(router)

    def test_reproduce_nth_paper(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _make_ideation_run(tmp)
            router = self._setup_router(tmp)
            captured = {}

            class _Req:
                def __init__(self, paper, run_id=None, repo_url=None):
                    self.paper = paper

            class _Resp:
                run_id = "rc-test"
                paper_slug = "polsar"

            async def _start(req):
                captured["paper"] = req.paper
                return _Resp()

            _install_fake_routes(
                "researchclaw.server.routes.reproduce",
                ReproduceStartRequest=_Req,
                start_reproduce=_start,
            )
            try:
                reply = asyncio.run(
                    router._handle_reproduce("复现第 2 篇", _new_session())
                )
            finally:
                self._teardown_router(router)
            assert captured["paper"] == "2301.12345"  # 有 arxiv_id 优先用
            assert "复现任务已启动" in reply

    def test_brief_grounded_by_reproduction(self):
        """开始确认带「复现」→ 复现报告的问题/机会进入起草 prompt。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repro = tmp / "artifacts" / "rc-x" / "reproduction" / "s"
            repro.mkdir(parents=True)
            (repro / "reproduction_report.json").write_text(
                json.dumps(
                    {
                        "issues": {"environment": ["torch 版本不锁定"]},
                        "opportunities": [
                            {"issue": "数据划分未固化", "hypothesis": "固化种子后可复现并超越"}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            router = self._setup_router(tmp)
            _install_fake_routes(
                "researchclaw.server.routes.reproduce",
                _state={"status": "completed", "run_id": "rc-x", "paper_slug": "s"},
            )
            try:
                reply = asyncio.run(
                    router._handle_brief("开始确认，基于复现发现来做", _new_session())
                )
                prompts = router._llm_client.prompts
            finally:
                self._teardown_router(router)
            assert "复现发现的问题与改进机会" in prompts[-1]
            assert "固化种子后可复现并超越" in prompts[-1]
            assert "研究任务书" in reply
            assert "已关联最近的复现基线" in reply


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
