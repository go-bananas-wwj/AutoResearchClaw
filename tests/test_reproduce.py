"""Tests for researchclaw.reproduce — 论文复现模块（离线冒烟）。

不依赖 GPU / LLM / 网络 / Docker：覆盖 finder 的 URL 解析与候选去重排序、
compare 的宣称 vs 实测对比、report 的问题分类与 Markdown 渲染、
runner 的文件编辑应用与驱动脚本生成、config 的 reproduce 段解析。
pytest 缺失时也可直接 ``python3 tests/test_reproduce.py`` 跑全部用例。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from researchclaw.config import RCConfig, ReproduceConfig, _parse_reproduce_config
from researchclaw.reproduce.compare import ClaimedMetric, compare_metrics
from researchclaw.reproduce.finder import (
    RepoCandidate,
    _dedupe_and_rank,
    normalize_repo_url,
    paper_slug,
    parse_paper_ref,
)
from researchclaw.reproduce.report import (
    build_report,
    classify_issues,
    render_report_markdown,
    suggest_opportunities,
    write_report,
)
from researchclaw.reproduce.runner import (
    ReproductionResult,
    _apply_file_edits,
    _extract_json,
    _git_clone,
)


# ---------------------------------------------------------------------------
# finder: URL 解析 / 去重排序
# ---------------------------------------------------------------------------


class TestParsePaperRef:
    def test_bare_arxiv_id(self):
        ref = parse_paper_ref("1706.03762")
        assert ref.kind == "arxiv_id"
        assert ref.arxiv_id == "1706.03762"

    def test_arxiv_id_with_version(self):
        ref = parse_paper_ref("arXiv:1706.03762v3")
        assert ref.kind == "arxiv_id"
        assert ref.arxiv_id == "1706.03762"

    def test_arxiv_abs_url(self):
        ref = parse_paper_ref("https://arxiv.org/abs/1706.03762")
        assert ref.kind == "arxiv_id"
        assert ref.arxiv_id == "1706.03762"

    def test_repo_url(self):
        ref = parse_paper_ref("https://github.com/foo/bar")
        assert ref.kind == "url"
        assert ref.arxiv_id == ""

    def test_plain_title(self):
        ref = parse_paper_ref("Attention Is All You Need")
        assert ref.kind == "title"

    def test_paper_slug(self):
        assert paper_slug("1706.03762") == "1706-03762"
        assert paper_slug("Attention Is All You Need") == "attention-is-all-you-need"
        assert paper_slug("!!!") == "paper"


class TestDedupeAndRank:
    def test_normalize_repo_url(self):
        assert (
            normalize_repo_url("https://GitHub.com/Foo/Bar.git/")
            == "https://github.com/Foo/Bar"
        )

    def test_dedupe_and_official_first(self):
        candidates = [
            RepoCandidate(url="https://github.com/a/x", stars=500, is_official=False),
            RepoCandidate(url="https://github.com/a/x.git", stars=10, is_official=True),
            RepoCandidate(url="https://github.com/b/y", stars=100, is_official=True),
        ]
        ranked = _dedupe_and_rank(candidates, 10)
        # 去重后 2 个；重复项合并时官方标注+最高 stars 胜出；官方按 stars 降序
        assert len(ranked) == 2
        assert all(c.is_official for c in ranked)
        assert normalize_repo_url(ranked[0].url) == "https://github.com/a/x"
        assert ranked[0].stars == 500

    def test_max_results(self):
        candidates = [
            RepoCandidate(url=f"https://github.com/a/r{i}", stars=i) for i in range(20)
        ]
        assert len(_dedupe_and_rank(candidates, 5)) == 5


# ---------------------------------------------------------------------------
# compare: 宣称 vs 实测（无 LLM 纯函数路径）
# ---------------------------------------------------------------------------


class TestCompareMetrics:
    def _claimed(self):
        return [
            ClaimedMetric(dataset="CIFAR-10", metric="accuracy", value=95.3, split="test"),
            ClaimedMetric(dataset="CIFAR-10", metric="f1", value=94.0, split="test"),
            ClaimedMetric(dataset="ImageNet", metric="top1", value=80.0, split="val"),
        ]

    def test_matched_close_diverged_missing(self):
        measured = {
            "cifar10_test_accuracy": 95.4,   # rel diff ~0.1% → matched
            "cifar10_f1": 88.0,              # rel diff ~6.4% → close
        }
        result = compare_metrics(self._claimed(), measured, llm=None)
        rows = result["rows"]
        assert rows[0]["status"] == "matched"
        assert rows[0]["measured"] == 95.4
        assert rows[1]["status"] == "close"
        assert rows[2]["status"] == "missing"
        assert result["summary"]["matched"] == 1
        assert result["summary"]["close"] == 1
        assert result["summary"]["missing"] == 1
        assert result["attribution"] == ""  # 无 LLM → 空归因

    def test_diverged(self):
        measured = {"imagenet_top1": 60.0}
        result = compare_metrics(self._claimed(), measured)
        row = [r for r in result["rows"] if r["metric"] == "top1"][0]
        assert row["status"] == "diverged"
        assert row["rel_diff"] > 0.1

    def test_empty_claimed(self):
        result = compare_metrics([], {"a": 1.0})
        assert result["rows"] == []
        assert result["summary"]["total_measured"] == 1


# ---------------------------------------------------------------------------
# report: 问题分类 / 机会建议 / Markdown 渲染 / 落盘
# ---------------------------------------------------------------------------


def _fake_history():
    return [
        {
            "round": 1,
            "returncode": 1,
            "stderr_tail": "Traceback (most recent call last):\nModuleNotFoundError: No module named 'timm'",
            "stdout_tail": "",
        },
        {
            "round": 2,
            "returncode": 1,
            "stderr_tail": "FileNotFoundError: [Errno 2] No such file or directory: 'data/train.csv'",
            "stdout_tail": "",
        },
        {
            "round": 3,
            "returncode": 0,
            "stderr_tail": "",
            "stdout_tail": "accuracy: 0.95",
            "metrics": {"accuracy": 0.95},
        },
    ]


class TestClassifyIssues:
    def test_categories(self):
        issues = classify_issues(_fake_history())
        assert any("timm" in line for line in issues["environment"])
        assert any("train.csv" in line for line in issues["data"])
        # 成功的 round 不产生问题
        assert all("accuracy" not in line for v in issues.values() for line in v)

    def test_empty_history(self):
        assert classify_issues([]) == {}


class TestSuggestOpportunities:
    def test_one_hypothesis_per_issue(self):
        issues = classify_issues(_fake_history())
        comparison = compare_metrics(
            [ClaimedMetric(dataset="CIFAR-10", metric="accuracy", value=95.3)],
            {"accuracy": 80.0},
        )
        opps = suggest_opportunities(issues, comparison, llm=None)
        assert len(opps) >= 3  # 2 issues + 1 diverged metric
        assert all(o["hypothesis"] for o in opps)
        assert any(o["category"] == "metric_gap" for o in opps)


class TestRenderReport:
    def _report(self):
        repro = ReproductionResult(
            ok=True,
            repo_url="https://github.com/foo/bar",
            repo_dir="/tmp/x/repo",
            rounds_used=3,
            elapsed_sec=120.0,
            measured_metrics={"cifar10_test_accuracy": 95.4},
            run_history=_fake_history(),
        )
        comparison = compare_metrics(
            [ClaimedMetric(dataset="CIFAR-10", metric="accuracy", value=95.3, split="test")],
            repro.measured_metrics,
        )
        return build_report(
            paper={"identifier": "1706.03762", "title": "Test Paper", "arxiv_id": "1706.03762"},
            repo_url="https://github.com/foo/bar",
            candidates=[{"url": "https://github.com/foo/bar", "stars": 100, "is_official": True, "source": "papers_with_code"}],
            repro_result=repro,
            comparison=comparison,
            llm=None,
        )

    def test_markdown_sections(self):
        md = render_report_markdown(self._report())
        for section in ("论文信息", "代码仓库", "执行情况", "宣称值 vs 实测值", "问题清单", "候选改进机会"):
            assert section in md, f"missing section: {section}"
        assert "95.3" in md and "95.4" in md
        assert "复现一致" in md
        assert "✅" in md

    def test_write_report_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_path, json_path = write_report(Path(tmp), self._report())
            assert md_path.is_file() and json_path.is_file()
            data = json.loads(json_path.read_text(encoding="utf-8"))
            assert data["execution"]["ok"] is True
            assert data["metrics_table"][0]["status"] == "matched"
            assert data["issues"]["environment"]


# ---------------------------------------------------------------------------
# runner: 文件编辑应用 / JSON 抽取 / clone 失败降级
# ---------------------------------------------------------------------------


class TestApplyFileEdits:
    def test_write_and_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "train.py").write_text("epochs = 100\n", encoding="utf-8")
            applied = _apply_file_edits(repo, [
                {"path": "config.yaml", "content": "lr: 0.001\n"},
                {"path": "train.py", "find": "epochs = 100", "replace": "epochs = 1"},
            ])
            assert sorted(applied) == ["config.yaml", "train.py"]
            assert (repo / "config.yaml").read_text() == "lr: 0.001\n"
            assert (repo / "train.py").read_text() == "epochs = 1\n"

    def test_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            applied = _apply_file_edits(repo, [
                {"path": "../evil.py", "content": "x = 1"},
                {"path": "/abs/evil.py", "content": "x = 1"},
            ])
            assert applied == []
            assert not (Path(tmp) / "evil.py").exists()

    def test_find_not_present_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
            applied = _apply_file_edits(repo, [
                {"path": "a.py", "find": "missing string", "replace": "y"},
            ])
            assert applied == []
            assert (repo / "a.py").read_text() == "x = 1\n"


class TestExtractJson:
    def test_plain(self):
        assert _extract_json('{"commands": ["ls"]}') == {"commands": ["ls"]}

    def test_wrapped_in_prose(self):
        data = _extract_json('好的，方案如下：\n```json\n{"a": 1}\n```\n以上。')
        assert data == {"a": 1}

    def test_garbage(self):
        assert _extract_json("no json here") == {}
        assert _extract_json('{"broken": ') == {}
        assert _extract_json('[1, 2, 3]') == {}


class TestGitCloneFallback:
    def test_clone_failure_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, err = _git_clone(
                "https://invalid.invalid/nonexistent/repo.git",
                Path(tmp) / "repo",
                timeout_sec=15,
            )
            assert ok is False
            assert err  # 有可读的错误说明


# ---------------------------------------------------------------------------
# config: reproduce 段解析
# ---------------------------------------------------------------------------


class TestReproduceConfig:
    def test_defaults_when_section_missing(self):
        cfg = _parse_reproduce_config({})
        assert cfg == ReproduceConfig()
        assert cfg.max_rounds == 8
        assert cfg.time_budget_sec == 7200
        assert cfg.network_policy == "full"

    def test_partial_override(self):
        cfg = _parse_reproduce_config({"max_rounds": 3, "enabled": False})
        assert cfg.max_rounds == 3
        assert cfg.enabled is False
        assert cfg.time_budget_sec == 7200

    def test_bad_values_clamped(self):
        cfg = _parse_reproduce_config({"max_rounds": 0, "network_policy": "bogus"})
        assert cfg.max_rounds == 1
        assert cfg.network_policy == "full"

    def test_rcconfig_from_dict_without_reproduce_section(self):
        """老配置（无 reproduce 段）也能加载并拿到默认值。"""
        data = {
            "project": {"name": "t"},
            "research": {"topic": "x"},
            "runtime": {"timezone": "UTC"},
            "notifications": {"channel": "console"},
            "knowledge_base": {"root": "docs/kb"},
            "llm": {"base_url": "http://x", "api_key_env": "KEY"},
        }
        cfg = RCConfig.from_dict(data, check_paths=False)
        assert isinstance(cfg.reproduce, ReproduceConfig)
        assert cfg.reproduce.enabled is True


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
