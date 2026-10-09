"""Tests for researchclaw.writing — Overleaf 批注驱动的论文改稿循环（离线冒烟）。

不依赖 LLM / 网络 / Overleaf：批注解析、版本 diff、批注报告组装、
版本快照自增、以及用 fake LLM 跑 revise 全流程的文件落盘与返回值。
pytest 缺失时也可直接 ``python3 tests/test_writing_annotations.py``。
"""

from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from researchclaw.config import RCConfig
from researchclaw.writing.annotations import (
    diff_versions,
    load_annotation_report,
    parse_annotations,
)
from researchclaw.writing.reviser import PaperReviser, _next_version, _strip_fence


# ---------------------------------------------------------------------------
# 批注解析
# ---------------------------------------------------------------------------


class TestParseAnnotations:
    def test_chinese_annotation_with_section_and_context(self):
        tex = (
            "\\section{实验结果}\n"
            "我们的方法达到了 88.5 的精度。\n"
            "% 批注：这个数值和摘要不一致\n"
        )
        out = parse_annotations(tex)
        assert len(out) == 1
        c = out[0]
        assert c["kind"] == "comment"
        assert c["text"] == "这个数值和摘要不一致"
        assert c["line_no"] == 3
        assert c["section"] == "实验结果"
        assert c["context"] == "我们的方法达到了 88.5 的精度。"

    def test_keywords_and_fullwidth_colon(self):
        tex = (
            "% REVIEW: check this\n"
            "%TODO：补一下引用\n"
            "  % fixme: broken ref\n"
            "% Comment: english lower\n"
            "% 这只是普通注释，不是批注\n"
        )
        out = parse_annotations(tex)
        assert [c["text"] for c in out] == [
            "check this",
            "补一下引用",
            "broken ref",
            "english lower",
        ]

    def test_context_skips_comment_and_blank_lines(self):
        tex = "正文行\n\n% 无关注释\n% 批注：x\n"
        out = parse_annotations(tex)
        assert out[0]["context"] == "正文行"

    def test_no_annotations(self):
        assert parse_annotations("hello\n\\section{A}\n") == []

    def test_subsection_detected(self):
        tex = "\\subsection{消融实验}\nline\n% 批注：y\n"
        assert parse_annotations(tex)[0]["section"] == "消融实验"


# ---------------------------------------------------------------------------
# 版本 diff
# ---------------------------------------------------------------------------


class TestDiffVersions:
    def test_single_line_change(self):
        base = "\\section{引言}\n第一段。\n第二段。\n"
        new = "\\section{引言}\n第一段改写了。\n第二段。\n"
        out = diff_versions(base, new)
        assert len(out) == 1
        e = out[0]
        assert e["kind"] == "edit"
        assert e["before"] == "第一段。"
        assert e["after"] == "第一段改写了。"
        assert e["section"] == "引言"

    def test_blank_line_only_diff_skipped(self):
        base = "a\nb\n"
        new = "a\n\nb\n"
        assert diff_versions(base, new) == []

    def test_truncation(self):
        base = "x\n"
        new = "y" * 2000 + "\n"
        out = diff_versions(base, new)
        assert len(out[0]["after"]) == 800


# ---------------------------------------------------------------------------
# 批注报告组装
# ---------------------------------------------------------------------------


class TestLoadAnnotationReport:
    def test_no_user_version(self, tmp_path):
        rep = load_annotation_report(tmp_path, "zh")
        assert rep["has_user_version"] is False
        assert rep["comments"] == [] and rep["edits"] == []
        assert rep["pulled_file"] == ""

    def test_user_version_without_base_uses_deliverables(self, tmp_path):
        pulled = tmp_path / "paper_annotations" / "zh"
        pulled.mkdir(parents=True)
        (pulled / "paper.tex").write_text("新稿\n% 批注：改这里\n", encoding="utf-8")
        deliv = tmp_path / "deliverables"
        deliv.mkdir()
        (deliv / "paper.tex").write_text("旧稿\n", encoding="utf-8")
        rep = load_annotation_report(tmp_path, "zh")
        assert rep["has_user_version"] is True
        assert len(rep["comments"]) == 1
        assert rep["edits"]  # 相对 deliverables/paper.tex 有改动
        assert rep["pulled_file"].endswith("paper_annotations/zh/paper.tex")

    def test_user_version_with_base(self, tmp_path):
        pulled = tmp_path / "paper_annotations" / "en"
        pulled.mkdir(parents=True)
        (pulled / "paper.tex").write_text("same\nnew line\n", encoding="utf-8")
        vdir = tmp_path / "paper_versions" / "en"
        vdir.mkdir(parents=True)
        (vdir / "base.tex").write_text("same\nold line\n", encoding="utf-8")
        deliv = tmp_path / "deliverables"
        deliv.mkdir()
        (deliv / "paper.tex").write_text("不该用作 base\n", encoding="utf-8")
        rep = load_annotation_report(tmp_path, "en")
        assert len(rep["edits"]) == 1
        assert rep["edits"][0]["before"] == "old line"
        assert rep["edits"][0]["after"] == "new line"


# ---------------------------------------------------------------------------
# 版本快照编号 / 围栏剥离
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_next_version_empty(self, tmp_path):
        assert _next_version(tmp_path / "nope") == 1

    def test_next_version_increments(self, tmp_path):
        (tmp_path / "v1.tex").write_text("a", encoding="utf-8")
        (tmp_path / "v3.tex").write_text("a", encoding="utf-8")
        (tmp_path / "base.tex").write_text("a", encoding="utf-8")
        assert _next_version(tmp_path) == 4

    def test_strip_fence(self):
        assert _strip_fence("```latex\nABC\n```") == "ABC"
        assert _strip_fence("ABC") == "ABC"


# ---------------------------------------------------------------------------
# revise 全流程（fake LLM）
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    def __init__(self, tex: str) -> None:
        self._tex = tex
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return _FakeResp(self._tex)


def _minimal_config() -> RCConfig:
    data = {
        "project": {"name": "t"},
        "research": {"topic": "x"},
        "runtime": {"timezone": "UTC"},
        "notifications": {"channel": "console"},
        "knowledge_base": {"root": "docs/kb"},
        "llm": {"base_url": "http://x", "api_key_env": "KEY"},
    }
    return RCConfig.from_dict(data, check_paths=False)


class TestRevise:
    def _make_run_dir(self, tmp_path: Path) -> Path:
        run_dir = tmp_path / "artifacts" / "rc-20261009-000000-abc123"
        (run_dir / "deliverables").mkdir(parents=True)
        (run_dir / "deliverables" / "paper.tex").write_text(
            "\\section{实验}\n旧稿正文。\n", encoding="utf-8"
        )
        pulled = run_dir / "paper_annotations" / "zh"
        pulled.mkdir(parents=True)
        (pulled / "paper.tex").write_text(
            "\\section{实验}\n旧稿正文。\n% 批注：加一句总结\n",
            encoding="utf-8",
        )
        return run_dir

    def test_revise_full_flow(self, tmp_path):
        run_dir = self._make_run_dir(tmp_path)
        new_tex = "\\section{实验}\n旧稿正文。\n加了一句总结。"
        llm = _FakeLLM(f"```latex\n{new_tex}\n```")
        reviser = PaperReviser(_minimal_config(), llm=llm)
        res = reviser.revise(run_dir, "rc-20261009-000000-abc123", "zh")

        assert res["ok"] is True
        assert res["version"] == 1
        assert res["applied_comments"] == 1
        assert res["reverted_numbers"] == []
        assert res["registry"] == "unavailable"  # 无实验数据，降级
        assert res["pushed"] is False  # 测试配置未启用 overleaf

        # 文件落盘：新稿写回 + zh 副本 + base + 旧稿快照
        assert (run_dir / "deliverables" / "paper.tex").read_text(
            encoding="utf-8"
        ) == new_tex
        assert (run_dir / "deliverables" / "paper_zh.tex").read_text(
            encoding="utf-8"
        ) == new_tex
        vdir = run_dir / "paper_versions" / "zh"
        assert (vdir / "base.tex").read_text(encoding="utf-8") == new_tex
        assert "旧稿正文" in (vdir / "v1.tex").read_text(encoding="utf-8")

        # prompt 里应包含批注与硬规则
        prompt = llm.calls[0]["messages"][0]["content"]
        assert "加一句总结" in prompt
        assert "硬规则" in prompt

    def test_revise_second_round_increments_version(self, tmp_path):
        run_dir = self._make_run_dir(tmp_path)
        vdir = run_dir / "paper_versions" / "zh"
        vdir.mkdir(parents=True)
        (vdir / "v1.tex").write_text("历史快照\n", encoding="utf-8")
        # 假输出必须足够长，否则会触发截断守卫（输出 < 底稿 80% 拒写）
        llm = _FakeLLM("\\section{实验}\n旧稿正文。\n改后的新稿，长度足够通过截断守卫。\n")
        reviser = PaperReviser(_minimal_config(), llm=llm)
        res = reviser.revise(run_dir, "rc-20261009-000000-abc123", "zh")
        assert res["version"] == 2
        assert (vdir / "v2.tex").is_file()

    def test_revise_no_user_version_no_instruction(self, tmp_path):
        run_dir = tmp_path / "artifacts" / "rc-20261009-000000-abc123"
        (run_dir / "deliverables").mkdir(parents=True)
        (run_dir / "deliverables" / "paper.tex").write_text("稿\n", encoding="utf-8")
        llm = _FakeLLM("不应被调用\n")
        reviser = PaperReviser(_minimal_config(), llm=llm)
        res = reviser.revise(run_dir, "rc-20261009-000000-abc123", "zh")
        assert res["ok"] is False
        assert "拉取" in res["reason"]
        assert llm.calls == []

    def test_revise_instruction_only_uses_deliverables(self, tmp_path):
        run_dir = tmp_path / "artifacts" / "rc-20261009-000000-abc123"
        (run_dir / "deliverables").mkdir(parents=True)
        (run_dir / "deliverables" / "paper.tex").write_text("原稿\n", encoding="utf-8")
        llm = _FakeLLM("改后稿。\n")
        reviser = PaperReviser(_minimal_config(), llm=llm)
        res = reviser.revise(
            run_dir, "rc-20261009-000000-abc123", "en", "把摘要改短"
        )
        assert res["ok"] is True
        # en 不写 paper_zh.tex
        assert not (run_dir / "deliverables" / "paper_zh.tex").exists()
        prompt = llm.calls[0]["messages"][0]["content"]
        assert "把摘要改短" in prompt
        assert "原稿" in prompt
    def test_revise_truncated_output_aborts(self, tmp_path):
        """LLM 输出明显短于底稿 → 判截断，不写回任何文件（2026-10-09 实测事故）。"""
        run_dir = self._make_run_dir(tmp_path)
        before = (run_dir / "deliverables" / "paper.tex").read_text(encoding="utf-8")
        llm = _FakeLLM("\\section{实验}\n只写了开头就断了")
        reviser = PaperReviser(_minimal_config(), llm=llm)
        res = reviser.revise(run_dir, "rc-20261009-000000-abc123", "zh")
        assert res["ok"] is False
        assert res["truncated"] is True
        assert (run_dir / "deliverables" / "paper.tex").read_text(
            encoding="utf-8"
        ) == before  # 原稿未被污染


if __name__ == "__main__":
    failures = 0
    for cls in list(vars().values()):
        if not (isinstance(cls, type) and cls.__name__.startswith("Test")):
            continue
        inst = cls()
        for name in dir(inst):
            if not name.startswith("test_"):
                continue
            fn = getattr(inst, name)
            kwargs = {}
            if "tmp_path" in inspect.signature(fn).parameters:
                td = tempfile.TemporaryDirectory()
                kwargs["tmp_path"] = Path(td.name)
            try:
                fn(**kwargs)
                print(f"PASS {cls.__name__}.{name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {cls.__name__}.{name}: {exc}")
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURES'}")
