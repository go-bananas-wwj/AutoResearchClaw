"""选题证据卡构建：Stage 1–8 产出 → ideation_report.json。

设计取舍：synthesis.md / hypotheses.md 是自由文本，不做脆弱的格式解析，
而是把三份原始产出（缺口分析、假设、查新报告）交给 LLM 做一次结构化抽取
（json_mode），失败时回退到按标题切分的朴素卡片。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CARD_SCHEMA = """{
  "cards": [
    {
      "question": "具体的科学问题（英文，一句话，可检验）",
      "note_zh": "中文一句话注解",
      "gap_evidence": ["来自缺口分析/文献的直接证据短语，1-3 条"],
      "novelty_hint": "结合查新结果的新颖性说明",
      "suggested_datasets": ["建议数据集"],
      "feasibility": {"v100_pilot": true, "budget_note": "在单卡 V100 上的缩尺验证方案"},
      "risks": "主要风险（数据/新颖性/算力）",
      "rank": 1
    }
  ]
}"""

_PROMPT = """你是一名资深遥感/机器学习方向的科研选题顾问。下面是一次自动选题流程的三份产出：

【研究方向】{direction}

【文献缺口分析 synthesis.md】
{synthesis}

【候选假设 hypotheses.md】
{hypotheses}

【查新报告 novelty_report.json】
{novelty}

请把它们综合成 3-5 张"选题证据卡"，要求：
1. 每张卡是一个**具体、可检验**的科学问题（不是宽泛方向）；
2. gap_evidence 必须引用缺口分析或文献中的**真实短语**，不许编造引用；
3. 结合查新报告（novelty_score={score}, assessment={assessment}）写 novelty_hint；
4. feasibility 站在"单张 V100-32GB 缩尺验证"的角度评估（数据集规模、训练时长量级）；
5. rank 按"新颖性×可行性×影响力"综合排序（1 最优）。
严格输出 JSON，格式：{schema}"""


def _read_text(path: Path, limit: int = 12000) -> str:
    try:
        text = path.read_text(encoding="utf-8")
        return text[:limit]
    except OSError:
        return ""


def _fallback_cards(hypotheses: str) -> list[dict[str, Any]]:
    """LLM 抽取失败时的朴素回退：按 markdown 标题切假设。"""
    cards: list[dict[str, Any]] = []
    chunks = re.split(r"^#{1,3}\s+", hypotheses, flags=re.M)
    idx = 1
    for chunk in chunks:
        chunk = chunk.strip()
        if len(chunk) < 30:
            continue
        first_line = chunk.splitlines()[0].strip()
        cards.append(
            {
                "question": first_line,
                "note_zh": "",
                "gap_evidence": [],
                "novelty_hint": "（LLM 结构化失败，未生成）",
                "suggested_datasets": [],
                "feasibility": {"v100_pilot": None, "budget_note": ""},
                "risks": "",
                "rank": idx,
            }
        )
        idx += 1
        if idx > 5:
            break
    return cards


def build_ideation_report(
    run_dir: Path,
    config: Any,
    llm_client: Any = None,
) -> dict[str, Any]:
    """读取 run_dir 的 stage-07/08 产出，生成并落盘 ideation_report.json。"""
    run_dir = Path(run_dir)
    synthesis = _read_text(run_dir / "stage-07" / "synthesis.md")
    hypotheses = _read_text(run_dir / "stage-08" / "hypotheses.md")
    novelty: dict[str, Any] = {}
    try:
        novelty = json.loads(
            (run_dir / "stage-08" / "novelty_report.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        pass

    direction = config.research.topic
    if not hypotheses.strip():
        return {"ok": False, "reason": "hypotheses.md 不存在或为空", "run_dir": str(run_dir)}

    cards: list[dict[str, Any]] = []
    try:
        if llm_client is None:
            from researchclaw.llm.client import LLMClient

            llm_client = LLMClient.from_rc_config(config)
        prompt = _PROMPT.format(
            direction=direction,
            synthesis=synthesis or "（无）",
            hypotheses=hypotheses,
            novelty=json.dumps(novelty, ensure_ascii=False)[:4000] or "（无）",
            score=novelty.get("novelty_score", "?"),
            assessment=novelty.get("assessment", "?"),
            schema=_CARD_SCHEMA,
        )
        resp = llm_client.chat(
            [{"role": "user", "content": prompt}], json_mode=True, max_tokens=2500
        )
        parsed = json.loads(resp.content)
        raw_cards = parsed.get("cards", []) if isinstance(parsed, dict) else []
        for c in raw_cards:
            if isinstance(c, dict) and c.get("question"):
                cards.append(c)
    except Exception:  # noqa: BLE001
        logger.warning("Ideation card extraction via LLM failed, falling back", exc_info=True)

    if not cards:
        cards = _fallback_cards(hypotheses)

    cards = sorted(cards, key=lambda c: c.get("rank", 99))[:5]

    # 查新可信度：检索 0 篇时 novelty_score=1.0 是"没查到"而非"真新颖"，必须标注
    _n_papers = novelty.get("total_papers_retrieved")
    if _n_papers is None:
        _n_papers = len(novelty.get("similar_papers", []) or [])
    novelty_trust = (
        "low"
        if (not novelty) or (isinstance(_n_papers, int) and _n_papers == 0)
        else "normal"
    )

    report = {
        "ok": True,
        "direction": direction,
        "cards": cards,
        "novelty_score": novelty.get("novelty_score"),
        "novelty_assessment": novelty.get("assessment"),
        "novelty_trust": novelty_trust,
        "novelty_note": (
            "查新检索返回 0 篇文献（可能限流/网络问题），novelty_score 不可信，仅供参考"
            if novelty_trust == "low"
            else ""
        ),
        "run_dir": str(run_dir),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out = run_dir / "ideation_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Ideation report: %d cards -> %s", len(cards), out)
    return report
