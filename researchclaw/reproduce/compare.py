"""宣称指标 vs 实测指标对比。

- :func:`extract_claimed_metrics` —— 用 LLM（json_mode）从论文摘要/正文里
  抽取宣称指标（数据集、指标名、数值）；无 LLM 时返回空表。
- :func:`compare_metrics` —— 与沙箱实测指标按名称模糊匹配对比，
  给出 matched/close/diverged/missing 判定与（可选的）LLM 差异归因。
纯函数路径（无 LLM）完全离线可测。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from researchclaw.reproduce.runner import _extract_json

logger = logging.getLogger(__name__)

# 相对误差阈值：≤2% matched，≤10% close，否则 diverged
_MATCH_TOL = 0.02
_CLOSE_TOL = 0.10


@dataclass(frozen=True)
class ClaimedMetric:
    dataset: str
    metric: str
    value: float
    split: str = ""  # test / val / ""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def extract_claimed_metrics(
    paper_text: str,
    llm: Any,
    *,
    max_items: int = 12,
) -> list[ClaimedMetric]:
    """LLM 从论文文本抽取宣称指标；任何失败返回 []。"""
    if not paper_text.strip() or llm is None:
        return []
    system = (
        "你是论文复现专家，负责从论文文本中抽取作者宣称的实验指标。"
        "输出严格 JSON。"
    )
    prompt = (
        "从下面的论文文本中抽取作者宣称的主要实验结果（最多 "
        f"{max_items} 条，优先主表/test split）。\n"
        '输出 JSON：{"claimed": [{"dataset": "...", "metric": "...", '
        '"value": 0.0, "split": "test|val|"}]}\n'
        "要求：value 必须是数值（百分数按原文数值，不要换算）；"
        "只抽论文自己方法的结果，不抽 baseline；不确定的宁可不抽。\n\n"
        f"论文文本（截断）：\n{paper_text[:8000]}"
    )
    try:
        resp = llm.chat(
            [{"role": "user", "content": prompt}],
            system=system,
            json_mode=True,
            max_tokens=1200,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("extract_claimed_metrics LLM call failed: %s", exc)
        return []
    data = _extract_json(resp.content)
    claimed: list[ClaimedMetric] = []
    for item in list(data.get("claimed") or [])[:max_items]:
        if not isinstance(item, dict):
            continue
        try:
            value = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        claimed.append(
            ClaimedMetric(
                dataset=str(item.get("dataset") or "").strip(),
                metric=str(item.get("metric") or "").strip(),
                value=value,
                split=str(item.get("split") or "").strip(),
            )
        )
    return [c for c in claimed if c.metric]


def _find_measured(
    claimed: ClaimedMetric, measured: dict[str, float]
) -> tuple[str, float] | None:
    """在实测指标里找与宣称指标匹配的键（名称模糊匹配，数据集优先）。"""
    metric_norm = _norm(claimed.metric)
    dataset_norm = _norm(claimed.dataset)
    if not metric_norm:
        return None
    best: tuple[str, float] | None = None
    best_score = -1
    for key, val in measured.items():
        key_norm = _norm(key)
        if metric_norm not in key_norm:
            continue
        score = 1
        if dataset_norm and dataset_norm in key_norm:
            score += 2
        if claimed.split and claimed.split.lower() in key.lower():
            score += 1
        if score > best_score:
            best_score = score
            best = (key, val)
    return best


def compare_metrics(
    claimed: list[ClaimedMetric],
    measured: dict[str, float],
    llm: Any = None,
) -> dict[str, Any]:
    """对比宣称值与实测值。

    返回 ``{"rows": [...], "summary": {...}, "attribution": "..."}``。
    ``attribution`` 由 LLM 生成（无 LLM 或调用失败时为空串）。
    """
    rows: list[dict[str, Any]] = []
    counts = {"matched": 0, "close": 0, "diverged": 0, "missing": 0}
    for c in claimed:
        hit = _find_measured(c, measured)
        row: dict[str, Any] = {
            "dataset": c.dataset,
            "metric": c.metric,
            "split": c.split,
            "claimed": c.value,
            "measured": None,
            "measured_key": "",
            "abs_diff": None,
            "rel_diff": None,
            "status": "missing",
        }
        if hit is not None:
            key, val = hit
            abs_diff = val - c.value
            rel_diff = abs(abs_diff) / max(abs(c.value), 1e-8)
            if rel_diff <= _MATCH_TOL:
                status = "matched"
            elif rel_diff <= _CLOSE_TOL:
                status = "close"
            else:
                status = "diverged"
            row.update(
                measured=val,
                measured_key=key,
                abs_diff=round(abs_diff, 6),
                rel_diff=round(rel_diff, 4),
                status=status,
            )
        counts[row["status"]] += 1
        rows.append(row)

    summary = {
        "total_claimed": len(claimed),
        "total_measured": len(measured),
        **counts,
    }
    attribution = _attribute_differences(rows, llm) if llm is not None and rows else ""
    return {"rows": rows, "summary": summary, "attribution": attribution}


def _attribute_differences(rows: list[dict[str, Any]], llm: Any) -> str:
    """LLM 生成差异归因（一段中文/英文叙述，跟随输入语言）。"""
    interesting = [r for r in rows if r["status"] in ("diverged", "missing", "close")]
    if not interesting:
        interesting = rows[:5]
    lines = [
        f"- {r['dataset']}/{r['metric']}: 宣称 {r['claimed']} vs "
        f"实测 {r['measured']}（{r['status']}）"
        for r in interesting[:10]
    ]
    prompt = (
        "下面是论文宣称指标与沙箱实测指标的对比。用 3~6 句话分析差异的"
        "可能原因（随机种子/数据划分/训练预算/环境版本/评测协议），"
        "语言与输入一致，不要编造具体数值：\n" + "\n".join(lines)
    )
    try:
        resp = llm.chat(
            [{"role": "user", "content": prompt}],
            system="你是论文复现专家，擅长分析复现差异。",
            max_tokens=600,
        )
        return resp.content.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("attribution LLM call failed: %s", exc)
        return ""
