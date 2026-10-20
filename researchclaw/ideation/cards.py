"""Step 1 — 创新点卡片生成与确认（IdeaSpark 模式库 + DeepSeek）。

用户给一句自然语言方向 -> LLM 按 15 种构思模式产出 3-5 张可审核的创新点卡片
-> 用户在前端选一张 -> 物化成 ResearchBrief 并从实验设计/代码生成阶段启动。
没有审核通过，不产生任何 run。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from researchclaw.ideation.brief import ResearchBrief

logger = logging.getLogger(__name__)

# 15 种构思模式（IdeaSpark / ResearchStudio-Idea 的领域无关动作词表）
PATTERNS: tuple[dict[str, str], ...] = (
    {"id": "assumption_audit_and_pivot", "name": "审计并推翻假设", "when": "结果/防御依赖一个可被放松或违反的隐含假设"},
    {"id": "architectural_operator_substitution", "name": "替换算子或表征", "when": "昂贵算子可换成更便宜的代理且保留关键性质"},
    {"id": "generative_process_redesign", "name": "重设计生成过程", "when": "某个惯例固定的组件（调度/终点/隐码）可变成自由设计变量"},
    {"id": "controlled_diagnostic_design", "name": "受控诊断设计", "when": "测量被混淆因素抬高，需要配对/控制实例隔离真实性质"},
    {"id": "unify_into_shared_representation", "name": "统一到共享表征", "when": "异构输入/模态/任务可映射到一个统一空间"},
    {"id": "reframe_as_solvable_object", "name": "重构为可解对象", "when": "难解问题可重构成子集选择/博弈/约束满足等成熟对象"},
    {"id": "self_supervised_signal_engineering", "name": "自监督信号工程", "when": "缺标注，可从模型自身输出/不确定性/生成样本造监督"},
    {"id": "structural_prior_encoding", "name": "结构先验编码", "when": "已知不变性/对称性/物理约束可直接写进结构"},
    {"id": "algebraic_equivalence_unification", "name": "代数等价统一", "when": "两个不同过程/目标可证明等价并合并"},
    {"id": "heterogeneous_decomposition", "name": "异构分解", "when": "异质资源可按判别性质分区并分别处理"},
    {"id": "decompose_and_delegate", "name": "分解并委托", "when": "单体任务可拆成子问题交给最合适的求解器"},
    {"id": "relax_discrete_search_to_continuous", "name": "离散搜索松弛为连续", "when": "离散结构搜索可微分化/摊销化并与任务目标联合优化"},
    {"id": "adapt_via_conditioning", "name": "条件化适配", "when": "新任务可表达为条件（示例/检索/目标）而不更新参数"},
    {"id": "characterize_limit_then_surpass", "name": "刻画极限再超越", "when": "先形式化方法类的可分辨性/表达力极限，再构造超越它的算子"},
    {"id": "targeted_self_supervised_objective", "name": "定向自监督目标", "when": "设计只有目标结构性质才能最小化的无标签 pretext 目标"},
)

_CARD_FIELDS = (
    "title", "one_liner", "scientific_question", "method", "falsification",
    "novelty", "risks", "compute", "datasets", "metrics", "patterns", "evidence",
    "baselines",
)

_SYSTEM = """你是一名资深科研选题顾问。你的任务是把用户的模糊研究方向，变成 3-5 张"可审核、可执行、可证伪"的创新点卡片。

你可以使用下面 15 种构思模式（ideation patterns）作为诊断词汇。它们是动作类型，不是模板；每张卡要说明用了哪 1-2 种、以及为什么它恰好能补上这个缺口。

{patterns}

硬性要求：
1. 每张卡必须是一个具体机制，不能是"用深度学习做 X"这种方向。
2. 必须给出可证伪预测：最小实验 + 哪个指标朝哪个方向动 + 一个负对照。
3. 必须说明与最近邻工作的差异（没有证据时如实说"证据缺口"）。
4. 必须可在单张 32GB GPU、不超过 0.5 GPU-day 内做缩尺验证。
5. 不要编造论文；不确定就写"需检索确认"。
6. 只输出 JSON，不要 markdown 代码围栏，不要解释。
"""

_USER = """研究方向（用户原话）：
{direction}

请输出 JSON：
{{
  "cards": [
    {{
      "title": "英文短标题（不超过 12 词）",
      "one_liner": "中文一句话说清这个创新点",
      "scientific_question": "可检验的科学问题（一句话）",
      "method": "具体机制 3-6 句：数据、模型/算法、训练目标、关键设计",
      "falsification": "可证伪预测：最小实验 + 指标方向 + 负对照",
      "novelty": "与最近邻工作的差异；无证据时写证据缺口",
      "risks": "最大风险（数据/算力/新颖性）",
      "compute": "缩尺验证所需算力",
      "datasets": ["数据集名"],
      "metrics": [{{"metric_key": "指标名", "direction": "increase 或 decrease"}}],
      "patterns": ["模式 id 1", "模式 id 2"],
      "evidence": ["支撑证据（论文标题或'需检索确认'）"],
      "baselines": [{{"name": "基线方法名", "note": "为什么它是合理基线"}}]
    }}
  ]
}}

数量：{n} 张。排序按"新颖性 x 可行性 x 影响力"从优到劣。
"""


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 回复里抽出 JSON 对象（容忍代码围栏与前后噪声）。"""
    s = (text or "").strip()
    fence = chr(96) * 3
    s = re.sub(r"^" + fence + r"(?:json)?\s*", "", s)
    s = re.sub(r"\s*" + fence + r"$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in response")
    raw = s[start : end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", raw)
        return json.loads(repaired)


def _normalize_card(card: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _CARD_FIELDS:
        out[key] = card.get(key)
    for key in ("title", "one_liner", "scientific_question", "method",
                "falsification", "novelty", "risks", "compute"):
        out[key] = str(out[key] or "").strip()
    out["title"] = out["title"] or "Untitled idea"
    out["datasets"] = [str(x) for x in (out["datasets"] or []) if str(x).strip()]
    metrics = []
    for m in out["metrics"] or []:
        if isinstance(m, str) and m.strip():
            metrics.append({"metric_key": m.strip(), "direction": "increase"})
        elif isinstance(m, dict) and m.get("metric_key"):
            metrics.append({"metric_key": str(m["metric_key"]),
                            "direction": str(m.get("direction") or "increase")})
    out["metrics"] = metrics
    out["patterns"] = [str(x) for x in (out["patterns"] or []) if str(x).strip()]
    out["evidence"] = [str(x) for x in (out["evidence"] or []) if str(x).strip()]
    baselines = []
    for b in out["baselines"] or []:
        if isinstance(b, str) and b.strip():
            baselines.append({"name": b.strip(), "note": ""})
        elif isinstance(b, dict) and b.get("name"):
            baselines.append({"name": str(b["name"]), "note": str(b.get("note") or "")})
    out["baselines"] = baselines
    return out


def build_prompt(direction: str, n: int = 4) -> tuple[str, str]:
    pattern_lines = "\n".join(f"- {p['id']}（{p['name']}）：{p['when']}" for p in PATTERNS)
    return _SYSTEM.format(patterns=pattern_lines), _USER.format(direction=direction.strip(), n=n)


def generate_cards(direction: str, llm: Any, n: int = 4) -> list[dict[str, Any]]:
    """调 LLM 生成创新点卡片（不做检索；证据缺口要如实标注）。"""
    if not direction or len(direction.strip()) < 4:
        raise ValueError("研究方向太短，至少 4 个字符")
    system, user = build_prompt(direction, n)
    resp = llm.chat(
        [{"role": "user", "content": user}],
        system=system,
        max_tokens=4096,
        json_mode=True,
        temperature=0.7,
    )
    data = _extract_json(getattr(resp, "content", "") or "")
    cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(cards, list) or not cards:
        raise ValueError("LLM 没有返回 cards 列表")
    return [_normalize_card(c) for c in cards[:5] if isinstance(c, dict)]


def card_to_brief(card: dict[str, Any], target_conference: str = "") -> ResearchBrief:
    card = _normalize_card(card)
    return ResearchBrief(
        topic=card["title"],
        scientific_question=card["scientific_question"] or card["one_liner"],
        survey_summary=card["novelty"],
        hypotheses=[card["scientific_question"]] if card["scientific_question"] else [],
        experiment_scope=card["method"],
        datasets=card["datasets"],
        metrics=card["metrics"] or [{"metric_key": "primary_metric", "direction": "increase"}],
        target_conference=target_conference,
    )
