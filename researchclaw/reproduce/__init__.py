"""论文复现模块（reproduction-driven research）。

用户流程：选题阶段产出候选论文 → 挑 1~2 篇有公开代码的 → 克隆官方代码进
Docker 沙箱真跑 → 产出《复现报告》（宣称指标 vs 实测指标 + 问题清单 +
候选改进机会）。复现产物（``<repro_dir>/repo/``）可作为实验阶段的
baseline 代码起点。
"""

from __future__ import annotations

from researchclaw.reproduce.compare import (
    ClaimedMetric,
    compare_metrics,
    extract_claimed_metrics,
)
from researchclaw.reproduce.finder import (
    FinderResult,
    PaperRef,
    RepoCandidate,
    find_code_repos,
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
from researchclaw.reproduce.runner import ReproductionResult, ReproductionRunner

__all__ = [
    "ClaimedMetric",
    "FinderResult",
    "PaperRef",
    "RepoCandidate",
    "ReproductionResult",
    "ReproductionRunner",
    "build_report",
    "classify_issues",
    "compare_metrics",
    "extract_claimed_metrics",
    "find_code_repos",
    "normalize_repo_url",
    "paper_slug",
    "parse_paper_ref",
    "render_report_markdown",
    "suggest_opportunities",
    "write_report",
]
