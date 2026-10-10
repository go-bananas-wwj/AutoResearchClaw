"""调研讲解层——把选题引擎的文献扫描产物变成可读的中文叙事 + 单篇精读笔记。

用户旅程（对话驱动）：
「找选题：方向」（文献扫描）→「讲一下现状」领域图景讲解 →「精读第 N 篇」批判性提问
→「复现第 N 篇」真跑验证 → 问题/改进机会 →「开始确认」任务书（plan-mode 整稿审阅）。

产物（落在选题 run 目录下）：
- ``survey_landscape_zh.md``：领域图景讲解（缓存，force 才重生成）；
- ``paper_notes/NN-<slug>.md``：单篇精读笔记。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LANDSCAPE_FILENAME = "survey_landscape_zh.md"
PAPER_NOTES_DIR = "paper_notes"


def latest_ideation_run(repo_root: Path) -> Path | None:
    """最近一次选题引擎 run 目录（按 ideation_report.json mtime）。"""
    reports = sorted(
        (repo_root / "artifacts").glob("id-*/ideation_report.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return reports[0].parent if reports else None


def load_direction(run_dir: Path) -> str:
    try:
        report = json.loads(
            (run_dir / "ideation_report.json").read_text(encoding="utf-8")
        )
        return str(report.get("direction") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def load_shortlist(run_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    """读 stage-05 短名单；缺失时回退 stage-04 候选前 limit 条。"""
    for rel in ("stage-05/shortlist.jsonl", "stage-04/candidates.jsonl"):
        f = run_dir / rel
        if not f.is_file():
            continue
        papers: list[dict[str, Any]] = []
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                papers.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(papers) >= limit:
                break
        if papers:
            return papers
    return []


def format_shortlist(papers: list[dict[str, Any]]) -> str:
    """编号列表：标题 + 年份/venue + 一句话相关性理由。"""
    lines: list[str] = []
    for i, p in enumerate(papers, 1):
        meta = "，".join(
            x for x in (str(p.get("year") or ""), str(p.get("venue") or "")) if x
        )
        lines.append(f"{i}. **{p.get('title') or '(无标题)'}**（{meta}）")
        reason = str(p.get("keep_reason") or "")[:80]
        if reason:
            lines.append(f"   相关性：{reason}")
    return "\n".join(lines)


def _paper_digest(p: dict[str, Any]) -> str:
    return (
        f"标题：{p.get('title')}\n"
        f"年份/来源：{p.get('year') or ''} {p.get('venue') or ''}\n"
        f"摘要：{str(p.get('abstract') or '')[:1200]}"
    )


def build_landscape(run_dir: Path, llm: Any, force: bool = False) -> str:
    """领域图景讲解（中文叙事）：主流路线/代表工作/共同局限。带磁盘缓存。"""
    cache = run_dir / LANDSCAPE_FILENAME
    if cache.is_file() and not force:
        return cache.read_text(encoding="utf-8")
    papers = load_shortlist(run_dir)
    if not papers:
        return ""
    direction = load_direction(run_dir)
    digest = "\n\n---\n\n".join(_paper_digest(p) for p in papers[:12])
    prompt = (
        f"你是科研领域分析助手。下面是「{direction}」方向文献扫描得到的代表论文"
        "（标题+摘要）。请用中文写一份**领域现状讲解**，面向准备进入该方向的研究生，"
        "800–1200 字，结构：\n"
        "## 这个领域在解决什么问题\n"
        "## 主流技术路线（分 2–4 条，每条：核心思路 + 代表工作（引用下面论文标题）+ 各自优缺点）\n"
        "## 大家共同没解决好的问题（这是机会所在）\n"
        "要求：只基于给定论文的标题/摘要讲解，不要编造论文没有的结果数字；"
        "语言通俗但准确。\n\n"
        f"【论文清单】\n{digest}"
    )
    try:
        resp = llm.chat([{"role": "user", "content": prompt}], max_tokens=6000)
    except Exception:  # noqa: BLE001
        logger.warning("landscape narrative LLM call failed", exc_info=True)
        return ""
    text = str(getattr(resp, "content", "") or "").strip()
    if text:
        try:
            cache.write_text(text, encoding="utf-8")
        except OSError:
            logger.debug("landscape cache write failed", exc_info=True)
    return text


def critical_reading(paper: dict[str, Any], llm: Any) -> str:
    """单篇精读：它怎么做 / 关键假设 / 可质疑的问题 / 可升级点。"""
    prompt = (
        "你是严格的论文评审员。请批判性精读下面这篇论文（只有标题+摘要）：\n\n"
        f"{_paper_digest(paper)}\n\n"
        "输出中文精读笔记，结构：\n"
        "## 它怎么做（3 句话以内）\n"
        "## 关键假设（它默认成立但未必成立的前提，2–4 条）\n"
        "## 可质疑的问题（3–5 条，每条一句话、具体、可检验）\n"
        "## 可升级的点（2–3 条 insight：如果 XXX，可能提升/解决 YYY）\n"
        "只基于标题和摘要推断；推断不稳的地方明说「需读全文验证」。\n"
    )
    try:
        resp = llm.chat([{"role": "user", "content": prompt}], max_tokens=4096)
    except Exception:  # noqa: BLE001
        logger.warning("critical reading LLM call failed", exc_info=True)
        return ""
    return str(getattr(resp, "content", "") or "").strip()


def save_paper_note(
    run_dir: Path, index: int, paper: dict[str, Any], note: str
) -> Path:
    """精读笔记落盘：paper_notes/NN-<slug>.md。"""
    slug = str(paper.get("cite_key") or paper.get("paper_id") or f"paper{index}")
    slug = re.sub(r"[^\w-]", "_", slug)[:40]
    notes_dir = run_dir / PAPER_NOTES_DIR
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / f"{index:02d}-{slug}.md"
    path.write_text(
        f"# 精读笔记：{paper.get('title') or '(无标题)'}\n\n"
        f"- 年份/来源：{paper.get('year') or ''} {paper.get('venue') or ''}\n"
        f"- 链接：{paper.get('url') or paper.get('doi') or '(无)'}\n\n"
        f"{note}\n",
        encoding="utf-8",
    )
    return path
