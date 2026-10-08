"""《复现报告》生成——reproduction_report.md + reproduction_report.json。

报告内容：论文信息、repo、宣称值 vs 实测值表、问题清单（环境/代码/超参/数据
四类，由运行日志关键词启发式分类）、候选改进机会（每条问题附一句改进假设，
LLM 可用时由 LLM 润色）。渲染函数是纯函数，离线可测。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 四类问题的关键词启发式（对 stderr/stdout 尾部文本分类）
_ISSUE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("environment", (
        "modulenotfounderror", "importerror", "no module named", "pip install",
        "version", "cuda error", "cudnn", "out of memory", "no space left",
        "command not found", "permission denied",
    )),
    ("data", (
        "filenotfounderror", "no such file", "dataset", "download", "404",
        "connectionerror", "connection refused", "timed out", "checksum",
    )),
    ("hyperparameter", (
        "learning rate", "lr ", "epochs", "batch_size", "nan", "keyerror",
        "unrecognized arguments", "invalid value", "config",
    )),
    ("code", (
        "typeerror", "attributeerror", "syntaxerror", "indexerror",
        "valueerror", "runtimeerror", "nameerror", "traceback",
    )),
)

_ISSUE_LABELS = {
    "environment": "环境",
    "code": "代码",
    "hyperparameter": "超参",
    "data": "数据",
}

_STATUS_LABELS = {
    "matched": "复现一致",
    "close": "基本接近",
    "diverged": "明显偏离",
    "missing": "未测到",
}


def classify_issues(run_history: list[dict[str, Any]]) -> dict[str, list[str]]:
    """把运行历史里的失败日志按 环境/代码/超参/数据 四类归类。"""
    issues: dict[str, list[str]] = {k: [] for k in _ISSUE_LABELS}
    seen: set[str] = set()
    for record in run_history:
        if record.get("returncode") == 0:
            continue
        text = (
            str(record.get("stderr_tail") or "")
            + "\n"
            + str(record.get("stdout_tail") or "")
        )
        if record.get("timed_out"):
            text += "\ntimed out"
        category = "code"
        best = -1
        text_lower = text.lower()
        for name, keywords in _ISSUE_RULES:
            score = sum(1 for kw in keywords if kw in text_lower)
            if score > best:
                best = score
                category = name
        # 取最有信息量的一行作为问题描述
        line = ""
        for raw in reversed(text.strip().splitlines()):
            raw = raw.strip()
            if raw and not raw.startswith("=== [rc-driver]"):
                line = raw
                break
        if not line:
            line = f"round {record.get('round')} 失败（exit {record.get('returncode')}）"
        line = line[:200]
        key = f"{category}:{line}"
        if key not in seen:
            seen.add(key)
            issues[category].append(line)
    return {k: v for k, v in issues.items() if v}


def suggest_opportunities(
    issues: dict[str, list[str]],
    comparison: dict[str, Any],
    llm: Any = None,
) -> list[dict[str, str]]:
    """每条问题附一句改进假设；diverged 指标也生成机会。LLM 可润色。"""
    opportunities: list[dict[str, str]] = []
    templates = {
        "environment": "固定依赖版本（锁 requirements/conda env），把环境差异变成可研究变量。",
        "data": "把数据预处理/划分脚本化并固化随机种子，数据管道本身可作为鲁棒性改进点。",
        "hyperparameter": "对关键超参做系统扫描（而非沿用论文默认值），可能复现并超越宣称值。",
        "code": "修补官方代码缺陷并向上游提 PR；缺陷修复前后的差异可作为消融实验。",
    }
    for category, lines in issues.items():
        for line in lines:
            opportunities.append(
                {
                    "category": category,
                    "issue": line,
                    "hypothesis": templates.get(category, "修复该问题后可得到更可靠的 baseline。"),
                }
            )
    for row in (comparison.get("rows") or []):
        if row.get("status") == "diverged":
            opportunities.append(
                {
                    "category": "metric_gap",
                    "issue": (
                        f"{row.get('dataset')}/{row.get('metric')}：宣称 "
                        f"{row.get('claimed')} vs 实测 {row.get('measured')}"
                    ),
                    "hypothesis": "弥合该差距的训练技巧（增广/调度/正则）即为候选改进方向。",
                }
            )
    if llm is not None and opportunities:
        opportunities = _polish_opportunities(opportunities, llm)
    return opportunities[:12]


def _polish_opportunities(
    opportunities: list[dict[str, str]], llm: Any
) -> list[dict[str, str]]:
    """LLM 把每条改进假设润色成更具体的一句（失败则保留模板原文）。"""
    from researchclaw.reproduce.runner import _extract_json

    payload = [
        {"issue": o["issue"][:150], "hypothesis": o["hypothesis"]}
        for o in opportunities[:12]
    ]
    prompt = (
        "下面是论文复现中发现的问题及初步改进假设。请把每条 hypothesis 改写成"
        "一句更具体、可验证的改进假设（中文，≤40 字）。输出严格 JSON：\n"
        '{"hypotheses": ["...", ...]}（数量与输入一致）\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        resp = llm.chat(
            [{"role": "user", "content": prompt}],
            system="你是论文复现专家，输出严格 JSON。",
            json_mode=True,
            max_tokens=800,
        )
        hyps = _extract_json(resp.content).get("hypotheses") or []
        for opp, hyp in zip(opportunities, hyps):
            hyp = str(hyp).strip()
            if hyp:
                opp["hypothesis"] = hyp
    except Exception as exc:  # noqa: BLE001
        logger.warning("opportunity polish failed: %s", exc)
    return opportunities


def build_report(
    *,
    paper: dict[str, Any],
    repo_url: str,
    candidates: list[dict[str, Any]],
    repro_result: Any,
    comparison: dict[str, Any],
    llm: Any = None,
) -> dict[str, Any]:
    """汇总复现报告 dict（reproduction_report.json 的内容）。"""
    issues = classify_issues(list(getattr(repro_result, "run_history", []) or []))
    opportunities = suggest_opportunities(issues, comparison, llm)
    return {
        "title": f"复现报告：{paper.get('title') or paper.get('identifier') or repo_url}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paper": paper,
        "repository": {
            "url": repo_url,
            "candidates": candidates,
        },
        "execution": {
            "ok": bool(getattr(repro_result, "ok", False)),
            "rounds_used": int(getattr(repro_result, "rounds_used", 0)),
            "elapsed_sec": round(float(getattr(repro_result, "elapsed_sec", 0.0)), 1),
            "final_error": str(getattr(repro_result, "final_error", "") or ""),
        },
        "metrics_table": list(comparison.get("rows") or []),
        "metrics_summary": dict(comparison.get("summary") or {}),
        "measured_metrics": dict(getattr(repro_result, "measured_metrics", {}) or {}),
        "attribution": str(comparison.get("attribution") or ""),
        "issues": issues,
        "opportunities": opportunities,
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    """把报告 dict 渲染成 Markdown（纯函数）。"""
    paper = report.get("paper") or {}
    repo = report.get("repository") or {}
    execution = report.get("execution") or {}
    lines: list[str] = [
        f"# {report.get('title') or '复现报告'}",
        "",
        f"生成时间：{report.get('generated_at', '')}",
        "",
        "## 论文信息",
        "",
        f"- 标识：{paper.get('identifier') or '(未知)'}",
        f"- 标题：{paper.get('title') or '(未知)'}",
        f"- arXiv：{paper.get('arxiv_id') or '(无)'}",
        "",
        "## 代码仓库",
        "",
        f"- 使用：{repo.get('url') or '(无)'}",
    ]
    for cand in (repo.get("candidates") or [])[:5]:
        official = "官方" if cand.get("is_official") else "非官方"
        lines.append(
            f"- 候选：{cand.get('url')}（{official}，stars={cand.get('stars', 0)}，"
            f"来源 {cand.get('source', '')}）"
        )
    lines += [
        "",
        "## 执行情况",
        "",
        f"- 结果：{'✅ 跑通' if execution.get('ok') else '❌ 未跑通'}",
        f"- 修复轮数：{execution.get('rounds_used', 0)}",
        f"- 耗时：{execution.get('elapsed_sec', 0)}s",
    ]
    if execution.get("final_error"):
        lines.append(f"- 最终错误：{execution['final_error']}")

    lines += ["", "## 宣称值 vs 实测值", ""]
    rows = report.get("metrics_table") or []
    if rows:
        lines += [
            "| 数据集 | 指标 | Split | 宣称值 | 实测值 | 相对差 | 判定 |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            measured = r.get("measured")
            rel = r.get("rel_diff")
            lines.append(
                f"| {r.get('dataset') or '-'} | {r.get('metric')} | "
                f"{r.get('split') or '-'} | {r.get('claimed')} | "
                f"{'-' if measured is None else measured} | "
                f"{'-' if rel is None else f'{rel:.1%}'} | "
                f"{_STATUS_LABELS.get(r.get('status'), r.get('status', ''))} |"
            )
    else:
        measured = report.get("measured_metrics") or {}
        if measured:
            lines.append("（无论文宣称值可对比，以下为实测指标）")
            lines += ["", "| 实测指标 | 数值 |", "|---|---|"]
            for key, val in sorted(measured.items())[:20]:
                lines.append(f"| {key} | {val} |")
        else:
            lines.append("（无可用指标）")

    if report.get("attribution"):
        lines += ["", "## 差异归因", "", report["attribution"]]

    lines += ["", "## 问题清单", ""]
    issues = report.get("issues") or {}
    if issues:
        for category, items in issues.items():
            label = _ISSUE_LABELS.get(category, category)
            lines.append(f"### {label}（{len(items)}）")
            lines += ["", *[f"- {item}" for item in items], ""]
    else:
        lines.append("（复现过程未记录到问题）")

    lines += ["", "## 候选改进机会", ""]
    opportunities = report.get("opportunities") or []
    if opportunities:
        for opp in opportunities:
            label = _ISSUE_LABELS.get(opp.get("category", ""), opp.get("category", ""))
            lines.append(f"- [{label}] {opp.get('issue')}")
            lines.append(f"  - 改进假设：{opp.get('hypothesis')}")
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)


def write_report(report_dir: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    """落盘 reproduction_report.md + reproduction_report.json。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / "reproduction_report.md"
    json_path = report_dir / "reproduction_report.json"
    md_path.write_text(render_report_markdown(report), encoding="utf-8")
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return md_path, json_path
