"""Dialog router — routes messages to appropriate handlers.

双通道设计：
  - 原流程（自动发现）：terminal/Settings 给 topic → 23 阶段流水线，co-pilot 门控，完全不变；
  - 对话通道：用户在 Chat 里给 insight/研究问题 → 聊出来的题目注入同一条流水线
    （调用与 /api/pipeline/start 相同的 start_pipeline）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml

from researchclaw.server.dialog.intents import Intent, classify_intent
from researchclaw.server.dialog.session import ChatSession, SessionManager

logger = logging.getLogger(__name__)

_session_manager = SessionManager()

REPO_ROOT = Path(__file__).resolve().parents[3]

_llm_client: Any = None


def _llm() -> Any:
    """Lazy-build singleton LLM client from the running server's config."""
    global _llm_client
    if _llm_client is None:
        from researchclaw.server.app import _app_state
        from researchclaw.llm.client import LLMClient

        _llm_client = LLMClient.from_rc_config(_app_state["config"])
    return _llm_client


async def route_message(raw_message: str, client_id: str) -> str:
    """Route incoming chat message and return response."""
    try:
        msg_data = json.loads(raw_message)
        text = msg_data.get("message", msg_data.get("text", raw_message))
    except (json.JSONDecodeError, TypeError):
        text = raw_message

    session = _session_manager.get_or_create(client_id)
    session.add_message("user", text)

    # 研究任务书确认流程（BRIEF flow）进行中 → 所有消息交给步骤机（其内部处理取消/上一步）
    if session.pending.get("flow") == "brief":
        reply = await _handle_brief_flow(text, session)
        session.add_message("assistant", reply)
        return reply

    # 上一条在等 topic → 本条优先按 topic 处理（除非显式取消）
    if session.pending.get("awaiting") == "topic":
        if re.search(r"(?:\bcancel\b|取消|算了|先不)", text, re.IGNORECASE):
            session.pending.pop("awaiting", None)
            reply = "好的，已取消。随时告诉我你想研究什么，或直接说题目我帮你启动。"
        else:
            topic = text.strip().strip("。")
            session.pending.pop("awaiting", None)
            reply = await _do_start(topic, session)
        session.add_message("assistant", reply)
        return reply

    # 上一条在等选题方向 → 本条按方向启动选题引擎
    if session.pending.get("awaiting") == "ideation":
        session.pending.pop("awaiting", None)
        reply = await _handle_ideate(f"找选题：{text.strip()}", session)
        session.add_message("assistant", reply)
        return reply

    # 上一条在等复现目标 → 本条按论文标识启动复现
    if session.pending.get("awaiting") == "reproduce":
        session.pending.pop("awaiting", None)
        reply = await _handle_reproduce(f"复现 {text.strip()}", session)
        session.add_message("assistant", reply)
        return reply

    intent, confidence = classify_intent(text)
    logger.debug("Intent: %s (%.2f) for: %s", intent.value, confidence, text[:50])

    handler = _HANDLERS.get(intent, _handle_general)
    response = await handler(text, session)

    session.add_message("assistant", response)
    return response


# ---------------------------------------------------------------- actions

_CMD_PREFIX = re.compile(
    r"^(?:请|帮我|给我|我要|我想|start|run|begin|launch|开始|启动|开跑|跑|运行|开始研究|开个?)"
    r"[\s,:：]*(?:研究一下|一个|一次|一下|new|research|experiment|实验|研究|课题|课题方向|pipeline)?[\s,:：]*",
    re.IGNORECASE,
)


def _extract_topic(text: str) -> str:
    """从指令式输入里剥出研究题目；剥不出或太短则返回空串。"""
    t = _CMD_PREFIX.sub("", text.strip()).strip("。！!？?")
    t = re.sub(r"^(?:研究|实验|experiment|pipeline|一下)\s*[:：]?\s*", "", t, flags=re.IGNORECASE)
    has_cjk = re.search(r"[\u4e00-\u9fff]", t) is not None
    if (has_cjk and len(t) >= 6) or (not has_cjk and len(t.split()) >= 3):
        return t
    return ""


async def _do_start(topic: str, session: ChatSession) -> str:
    from fastapi import HTTPException
    from researchclaw.server.routes.pipeline import (
        PipelineStartRequest,
        start_pipeline,
    )

    if len(topic) < 6:
        session.pending["awaiting"] = "topic"
        return (
            "想开始一次研究运行。告诉我**研究题目**（越具体越好，例如"
            "「用边缘保持先验改进小样本遥感地物分割」），我立刻替你启动。"
        )
    try:
        resp = await start_pipeline(
            PipelineStartRequest(topic=topic, auto_approve=True)
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return (
                "已有一条流水线在跑。问我「进度如何」看状态，或说「停止」先停掉再开新的。"
            )
        logger.exception("pipeline start failed from chat")
        return f"启动失败：{exc.detail}"

    session.current_run = resp.run_id
    return (
        f"已启动研究运行 **{resp.run_id}**\n"
        f"- 主题：{topic}\n"
        f"- 模式：全自动（23 阶段流水线，与原流程同一条）\n"
        f"- 进度：仪表盘 Dashboard 页实时看，或随时问我「到哪一步了」\n"
        f"- 想人工把关：终端 `researchclaw attach artifacts/{resp.run_id}` 接管门控（原 co-pilot 流程完全保留）\n"
        f"- 想中止：对我说「停止」"
    )


async def _handle_start(text: str, session: ChatSession) -> str:
    topic = _extract_topic(text)
    if not topic:
        session.pending["awaiting"] = "topic"
        return (
            "好，开一个新的研究运行。告诉我**研究题目**（越具体越好，例如"
            "「用边缘保持先验改进小样本遥感地物分割」），我立刻替你启动。"
        )
    return await _do_start(topic, session)


async def _handle_stop(text: str, session: ChatSession) -> str:
    from researchclaw.server.routes.pipeline import stop_pipeline

    try:
        result = await stop_pipeline()
    except Exception as exc:  # noqa: BLE001
        logger.exception("pipeline stop failed from chat")
        return f"停止失败：{exc}"
    session.current_run = ""
    detail = result if isinstance(result, dict) else {}
    return f"已请求停止当前运行{('：' + detail.get('run_id')) if detail.get('run_id') else ''}。"


async def _handle_ideate(text: str, session: ChatSession) -> str:
    """「找选题」：启动/查询选题引擎（Stage 1-8 + 证据卡）。"""
    from fastapi import HTTPException

    # 查询结果分支
    if re.search(r"结果|报告|证据卡|出来了吗|好了吗|咋样了", text):
        from researchclaw.server.routes.ideation import ideation_report, ideation_status

        st = await ideation_status()
        if st.get("status") == "running":
            return (
                f"选题引擎还在跑（{st.get('run_id')}，方向：{st.get('direction')}）。"
                "文献扫描通常 10–30 分钟，稍后问我「选题结果」。"
            )
        try:
            rep = await ideation_report()
        except HTTPException:
            return "还没有选题结果。给我一个方向，比如「找选题：遥感嵌入+灾害预警」。"
        lines = [
            f"查新评分 {rep.get('novelty_score')}（{rep.get('novelty_assessment')}），"
            f"候选科学问题 {len(rep.get('cards', []))} 个："
        ]
        for c in rep.get("cards", []):
            lines.append(f"\n**#{c.get('rank')} {c.get('question')}**")
            if c.get("note_zh"):
                lines.append(f"  {c['note_zh']}")
            for ev in (c.get("gap_evidence") or [])[:2]:
                lines.append(f"  缺口证据：{ev[:120]}")
            if c.get("novelty_hint"):
                lines.append(f"  新颖性：{c['novelty_hint'][:120]}")
        lines.append("\n看中哪个，说「开始研究：那个题目」我就启动完整流水线。")
        return "\n".join(lines)

    # 启动分支
    direction = _extract_topic(text)
    if not direction:
        m = re.search(r"(?:找选题|选题|ideate)\s*[:：]?\s*(.+)", text)
        direction = (m.group(1).strip() if m else "").strip("。")
    if len(direction) < 4:
        session.pending["awaiting"] = "ideation"
        return (
            "好，帮你跑选题引擎（真实扫描 OpenAlex/S2/arXiv → 找缺口 → 候选科学问题+证据卡）。"
            "告诉我**研究方向**（例如「遥感嵌入+灾害预警」）。"
        )

    from researchclaw.server.routes.ideation import IdeationStartRequest, start_ideation

    try:
        resp = await start_ideation(IdeationStartRequest(direction=direction))
    except HTTPException as exc:
        if exc.status_code == 409:
            return "已有一次选题运行在跑，问我「选题结果」看进度。"
        return f"启动失败：{exc.detail}"
    return (
        f"选题引擎已启动（{resp.run_id}，方向：{direction}）。\n"
        "它会真实扫描文献找缺口，通常 10–30 分钟。"
        "完成后对我说「选题结果」，我把候选科学问题和证据卡发你。"
    )


async def _handle_reproduce(text: str, session: ChatSession) -> str:
    """「复现这篇」：启动/查询论文复现（找官方 repo → 沙箱跑通 → 复现报告）。"""
    from fastapi import HTTPException

    # 停止分支
    if re.search(r"(?:停止|终止|取消|\b(?:stop|cancel|abort)\b)", text, re.IGNORECASE):
        from researchclaw.server.routes.reproduce import stop_reproduce

        try:
            await stop_reproduce()
            return "已请求停止当前复现任务。"
        except HTTPException as exc:
            return str(exc.detail)

    # 查询结果分支
    if re.search(r"结果|报告|出来了吗|好了吗|咋样了|进度", text):
        from researchclaw.server.routes.reproduce import (
            reproduce_report,
            reproduce_status,
        )

        st = await reproduce_status()
        if st.get("status") == "running":
            phase_zh = {
                "finding_repo": "找官方代码仓库",
                "reproducing": "沙箱里装环境/跑通",
                "comparing": "对比宣称 vs 实测指标",
                "reporting": "生成复现报告",
            }.get(st.get("phase"), st.get("phase", ""))
            return (
                f"复现任务还在跑（{st.get('run_id')}，当前：{phase_zh}）。"
                "复现可能要 1~2 小时，稍后问我「复现结果」。"
            )
        try:
            rep = await reproduce_report()
        except HTTPException:
            return "还没有复现结果。给我论文，比如「复现 1706.03762」或「复现 https://arxiv.org/abs/xxx」。"
        exe = rep.get("execution") or {}
        lines = [
            f"**{rep.get('title', '复现报告')}**",
            f"- repo：{(rep.get('repository') or {}).get('url', '(无)')}",
            f"- 沙箱执行：{'✅ 跑通' if exe.get('ok') else '❌ 未跑通'}"
            f"（{exe.get('rounds_used', 0)} 轮修复，{exe.get('elapsed_sec', 0)}s）",
        ]
        rows = rep.get("metrics_table") or []
        if rows:
            lines.append("- 宣称 vs 实测：")
            for r in rows[:5]:
                lines.append(
                    f"  - {r.get('dataset') or '-'}/{r.get('metric')}：宣称 {r.get('claimed')}"
                    f" vs 实测 {r.get('measured')}（{r.get('status')}）"
                )
        issues = rep.get("issues") or {}
        if issues:
            total = sum(len(v) for v in issues.values())
            lines.append(f"- 问题清单 {total} 条（{ '/'.join(f'{k}:{len(v)}' for k, v in issues.items()) }）")
        opps = rep.get("opportunities") or []
        if opps:
            lines.append(f"- 改进机会 {len(opps)} 条，第一条：{opps[0].get('hypothesis')}")
        report_md = st.get("report_md") or "reproduction_report.md"
        lines.append(f"- 完整报告：`{report_md}`")
        return "\n".join(lines)

    # 启动分支：解析论文标识（arXiv id / URL / GitHub 链接 / 标题）
    paper = ""
    m = re.search(r"(https?://\S+|\b\d{4}\.\d{4,5}(?:v\d+)?\b)", text)
    if m:
        paper = m.group(1)
    else:
        m = re.search(r"复现\s*[:：]?\s*(.+)", text)
        paper = (m.group(1).strip() if m else "").strip("。")
    if len(paper) < 4:
        session.pending["awaiting"] = "reproduce"
        return (
            "好，帮你复现一篇论文。给我**论文标识**：arXiv id（如 1706.03762）、"
            "arXiv/GitHub 链接，或论文标题（我会先找官方代码仓库，再进沙箱真跑）。"
        )

    from researchclaw.server.routes.reproduce import (
        ReproduceStartRequest,
        start_reproduce,
    )

    try:
        resp = await start_reproduce(
            ReproduceStartRequest(
                paper=paper,
                run_id=session.current_run or None,
                repo_url=paper if paper.startswith(("http://", "https://")) and "github.com" in paper else None,
            )
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return "已有一个复现任务在跑，问我「复现结果」看进度。"
        return f"启动失败：{exc.detail}"
    return (
        f"复现任务已启动（run：{resp.run_id}，目录 artifacts/{resp.run_id}/reproduction/{resp.paper_slug}/）。\n"
        "流程：找官方 repo → 克隆进沙箱 → LLM 装环境/跑通（最多 8 轮修复）→ 宣称 vs 实测对比。\n"
        "可能要 1~2 小时，完成后对我说「复现结果」，我把报告摘要发你。"
    )


# ---------------------------------------------------------------- BRIEF flow（研究任务书）

# 6 个确认槽位：(key, 中文标签, 提问语)
_BRIEF_SLOTS: list[tuple[str, str, str]] = [
    ("topic", "研究题目", "用一句话说明**研究题目**（越具体越好）"),
    ("scientific_question", "核心科学问题", "这项研究要回答的**核心科学问题**是什么（一句话、可检验）？"),
    ("survey_summary", "调研结论与研究空白", "相关文献的**调研结论和研究空白**是什么？"),
    ("hypotheses", "研究假设", "你提出的**可检验假设**是什么（可以有多条，逐条说）？"),
    ("experiment_scope", "实验范围/数据/指标", "**实验怎么做**：范围、数据集、基线、评价指标（含方向，如 mIoU 越高越好）？"),
    ("target_conference", "目标会议/期刊", "**目标投稿会议或期刊**（如 IEEE TGRS、NeurIPS）？"),
]

_BRIEF_EXTRACT_SCHEMAS: dict[str, str] = {
    "topic": '{"value": "研究题目（保留原文语言，一句话）"}',
    "scientific_question": '{"value": "核心科学问题（一句话、可检验）"}',
    "survey_summary": '{"value": "调研结论与研究空白（保留要点，200 字内）"}',
    "hypotheses": '{"value": ["假设1", "假设2"]}',
    "experiment_scope": (
        '{"scope": "实验范围/设计描述", "datasets": ["数据集1"], '
        '"metrics": [{"metric_key": "指标名", "direction": "maximize 或 minimize"}]}'
    ),
    "target_conference": '{"value": "目标会议/期刊名"}',
}


def _brief_fallback_value(slot_key: str, text: str) -> Any:
    """LLM 抽取失败时兜底：原文直接作为槽位内容。"""
    t = text.strip()
    if slot_key == "hypotheses":
        return [t] if t else []
    if slot_key == "experiment_scope":
        return {"scope": t, "datasets": [], "metrics": []}
    return t


def _brief_normalize_value(slot_key: str, data: Any) -> Any:
    """把 LLM json_mode 输出规整成槽位值；取不出内容时返回假值。"""
    if not isinstance(data, dict):
        return None
    if slot_key == "experiment_scope":
        scope = str(data.get("scope") or "").strip()
        datasets = [
            str(d).strip() for d in (data.get("datasets") or []) if str(d).strip()
        ]
        metrics: list[dict[str, str]] = []
        for m in data.get("metrics") or []:
            if isinstance(m, str) and m.strip():
                metrics.append({"metric_key": m.strip(), "direction": ""})
            elif isinstance(m, dict) and m.get("metric_key"):
                direction = str(m.get("direction") or "").lower()
                metrics.append(
                    {
                        "metric_key": str(m["metric_key"]).strip(),
                        "direction": direction
                        if direction in ("maximize", "minimize")
                        else "",
                    }
                )
        if not (scope or datasets or metrics):
            return None
        return {"scope": scope, "datasets": datasets, "metrics": metrics}
    value = data.get("value")
    if slot_key == "hypotheses":
        if isinstance(value, str):
            value = [value]
        items = [str(h).strip() for h in (value or []) if str(h).strip()]
        return items or None
    text_value = str(value or "").strip()
    return text_value or None


async def _brief_extract(slot_key: str, text: str, session: ChatSession) -> Any:
    """用 LLM（json_mode）从用户自然语言里抽取当前槽位内容；失败回退原文。"""
    label = dict((k, lbl) for k, lbl, _ in _BRIEF_SLOTS)[slot_key]
    prompt = (
        f"用户正在逐步确认一份《研究任务书》，当前要确认的槽位是「{label}」。\n"
        f"用户的原话：\n{text}\n\n"
        f"请把原话里属于「{label}」的内容抽取出来（不要编造原话没有的信息；"
        "指标方向只有 maximize/minimize 两种，越高越好=maximize）。\n"
        f"严格输出 JSON，格式：{_BRIEF_EXTRACT_SCHEMAS[slot_key]}"
    )
    try:
        client = _llm()
        resp = await asyncio.to_thread(
            client.chat,
            [{"role": "user", "content": prompt}],
            json_mode=True,
            max_tokens=800,
        )
        value = _brief_normalize_value(slot_key, json.loads(resp.content))
        if value:
            return value
    except Exception:  # noqa: BLE001
        logger.debug("brief slot extraction failed, fallback to raw text", exc_info=True)
    return _brief_fallback_value(slot_key, text)


def _brief_format_value(value: Any) -> str:
    """槽位值的人类可读回显。"""
    if isinstance(value, dict):  # experiment_scope
        lines = []
        if value.get("scope"):
            lines.append(f"- 实验范围：{value['scope']}")
        if value.get("datasets"):
            lines.append("- 数据集：" + "、".join(value["datasets"]))
        if value.get("metrics"):
            parts = [
                m["metric_key"]
                + (
                    f"（{'越高越好' if m.get('direction') == 'maximize' else '越低越好'}）"
                    if m.get("direction")
                    else ""
                )
                for m in value["metrics"]
            ]
            lines.append("- 指标：" + "、".join(parts))
        return "\n".join(lines) or "（空）"
    if isinstance(value, list):
        return "\n".join(f"{i}. {h}" for i, h in enumerate(value, 1)) or "（空）"
    return str(value or "（空）")


def _brief_confirm_prompt(label: str, draft: Any) -> str:
    return (
        f"我理解你要确认的**{label}**是：\n\n{_brief_format_value(draft)}\n\n"
        "确认吗？可回复：**确认** / **修改：……** / **上一步** / **取消**"
    )


def _load_ideation_card(rank: int) -> tuple[dict[str, Any] | None, str]:
    """读最近一次选题引擎 run 的 ideation_report.json，取第 N 张证据卡。"""
    artifacts = REPO_ROOT / "artifacts"
    reports = sorted(
        artifacts.glob("id-*/ideation_report.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not reports:
        return None, ""
    try:
        report = json.loads(reports[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ""
    cards = report.get("cards") or []
    card = next((c for c in cards if c.get("rank") == rank), None)
    if card is None and 1 <= rank <= len(cards):
        card = cards[rank - 1]
    return card, reports[0].parent.name


def _latest_reproduction_ref() -> str:
    """最近一次已完成的复现产物引用（artifacts/<run>/reproduction/<slug>）。"""
    try:
        from researchclaw.server.routes.reproduce import _state as repro_state
    except Exception:  # noqa: BLE001
        return ""
    if not repro_state or repro_state.get("status") != "completed":
        return ""
    run_id = repro_state.get("run_id") or ""
    slug = repro_state.get("paper_slug") or ""
    if not run_id or not slug:
        return ""
    ref = f"artifacts/{run_id}/reproduction/{slug}"
    return ref if (REPO_ROOT / ref).is_dir() else ""


async def _start_brief_pipeline(brief: Any) -> tuple[Any | None, str]:
    """用任务书启动流水线（独立成函数便于测试 mock）。"""
    from fastapi import HTTPException

    from researchclaw.server.routes.pipeline import (
        PipelineStartRequest,
        start_pipeline,
    )

    try:
        resp = await start_pipeline(
            PipelineStartRequest(
                topic=brief.topic,
                brief=brief.to_dict(),
                auto_approve=False,  # 任务书启动 = co-pilot，门控处人工把关
            )
        )
        return resp, ""
    except HTTPException as exc:
        return None, f"{exc.status_code}: {exc.detail}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("pipeline start from brief failed")
        return None, str(exc)


async def _finalize_brief(session: ChatSession) -> str:
    """6 槽全部确认 → 生成 ResearchBrief → 物化并启动流水线。"""
    from researchclaw.ideation.brief import ResearchBrief, suggest_from_stage

    pending = session.pending
    collected = pending.get("collected") or {}
    scope = collected.get("experiment_scope") or {}
    if isinstance(scope, str):
        scope = {"scope": scope, "datasets": [], "metrics": []}
    brief = ResearchBrief.from_dict(
        {
            "topic": collected.get("topic") or "",
            "scientific_question": collected.get("scientific_question") or "",
            "survey_summary": collected.get("survey_summary") or "",
            "hypotheses": collected.get("hypotheses") or [],
            "experiment_scope": scope.get("scope") or "",
            "datasets": scope.get("datasets") or [],
            "metrics": scope.get("metrics") or [],
            "target_conference": collected.get("target_conference") or "",
            "source_ideation_run": pending.get("source_ideation_run") or "",
            "reproduced_baseline": pending.get("reproduced_baseline") or "",
        }
    )
    resp, err = await _start_brief_pipeline(brief)
    if resp is None:
        # 启动失败不清 pending，用户可修正后重说「确认」
        if "409" in err:
            return "已有一条流水线在跑。问我「进度如何」看状态，或说「停止」后再重试。"
        return f"任务书已确认完整，但启动失败：{err}。检查配置后再试。"

    session.pending.clear()
    session.current_run = resp.run_id
    from_stage = suggest_from_stage(brief)
    lines = [
        f"✅ 《研究任务书》已确认并启动流水线 **{resp.run_id}**",
        "",
        f"- 题目：{brief.topic}",
        f"- 科学问题：{brief.scientific_question or '（见假设）'}",
        f"- 假设：{len(brief.hypotheses)} 条",
        f"- 数据集：{'、'.join(brief.datasets) or '（待细化）'}",
        f"- 目标会议：{brief.target_conference or '（未指定）'}",
    ]
    if brief.source_ideation_run:
        lines.append(f"- 复用选题文献扫描：{brief.source_ideation_run}（stage-03..06）")
    if brief.reproduced_baseline:
        lines.append(f"- 复现基线：{brief.reproduced_baseline} → baseline_repo/")
    lines += [
        f"- 起始阶段：**{from_stage.name}**（已确认的前期阶段已物化跳过）",
        "- 模式：co-pilot，门控处在 Dashboard/横幅审批",
        "- 存证：`research_brief.json` 在 run 目录下",
    ]
    return "\n".join(lines)


async def _handle_brief_flow(text: str, session: ChatSession) -> str:
    """BRIEF 步骤机：逐槽位抽取 → 回显确认 → 前进/回退/取消。"""
    pending = session.pending
    collected = pending.setdefault("collected", {})

    # 取消
    if re.search(r"(?:\bcancel\b|取消|算了|先不弄|退出确认)", text, re.IGNORECASE):
        session.pending.clear()
        return "好的，已取消任务书确认。想重新开始就说「开始确认」。"

    step = int(pending.get("step", 0))
    step = max(0, min(step, len(_BRIEF_SLOTS) - 1))
    key, label, ask = _BRIEF_SLOTS[step]

    # 上一步
    if re.search(r"(?:上一步|退回|返回上一个|go\s*back)", text, re.IGNORECASE):
        if step == 0 and "draft" not in pending:
            return "已经在第一步（研究题目）了。" + ask
        pending.pop("draft", None)
        prev_step = step - 1 if step > 0 else 0
        pending["step"] = prev_step
        pkey, plabel, pask = _BRIEF_SLOTS[prev_step]
        current = _brief_format_value(collected.get(pkey))
        return (
            f"好，回到第 {prev_step + 1}/6 步——**{plabel}**。\n"
            f"当前记录：\n{current}\n\n{pask}"
        )

    draft = pending.get("draft")
    if draft is not None:
        # 等待确认中
        if re.search(r"^\s*(?:确认|对的?|是的?|没问题|可以|ok|okay|confirm|yes)\s*[。！!]*\s*$", text, re.IGNORECASE):
            collected[key] = draft
            pending.pop("draft", None)
            next_step = step + 1
            pending["step"] = next_step
            if next_step >= len(_BRIEF_SLOTS):
                return await _finalize_brief(session)
            nkey, nlabel, nask = _BRIEF_SLOTS[next_step]
            prefill_note = ""
            if collected.get(nkey):
                prefill_note = f"\n（当前已有记录：{_brief_format_value(collected[nkey])}，直接回复新内容可覆盖）\n"
            return f"已记录**{label}**。\n\n第 {next_step + 1}/6 步——{nask}{prefill_note}"
        # 修改：显式前缀或直接补充新内容，都重新抽取
        m = re.match(r"^\s*(?:修改|改为|改成|不对[:，,]?)\s*[:：]?\s*(.+)$", text, re.S)
        new_text = m.group(1) if m else text
        new_draft = await _brief_extract(key, new_text, session)
        pending["draft"] = new_draft
        return _brief_confirm_prompt(label, new_draft)

    # 本槽位首次作答 → 抽取并回显确认
    draft = await _brief_extract(key, text, session)
    pending["draft"] = draft
    return _brief_confirm_prompt(label, draft)


async def _handle_brief(text: str, session: ChatSession) -> str:
    """「开始确认/定题/就选第 N 个」：初始化 BRIEF flow（可从选题卡片/复现预填）。"""
    if session.pending.get("flow") == "brief":
        return await _handle_brief_flow(text, session)

    collected: dict[str, Any] = {}
    source_ideation_run = ""
    prefill_note = ""

    m = re.search(r"第\s*(\d+)\s*个", text)
    if m:
        card, run_name = _load_ideation_card(int(m.group(1)))
        if card:
            source_ideation_run = run_name
            collected["topic"] = str(card.get("question") or "")
            collected["scientific_question"] = str(card.get("question") or "")
            evidence = [e for e in (card.get("gap_evidence") or []) if e]
            parts = []
            if card.get("note_zh"):
                parts.append(str(card["note_zh"]))
            if evidence:
                parts.append("缺口证据：" + "；".join(evidence[:3]))
            if card.get("novelty_hint"):
                parts.append("新颖性：" + str(card["novelty_hint"]))
            collected["survey_summary"] = "\n".join(parts)
            prefill_note = (
                f"\n已从选题报告 **{run_name}** 的第 {m.group(1)} 张证据卡预填前 3 项，"
                "我们直接从第 4 步（研究假设）开始；说「上一步」可回看修改。"
            )
        else:
            prefill_note = "\n（没找到对应的选题证据卡，从头开始确认。）"

    reproduced_baseline = ""
    if re.search(r"复现", text):
        reproduced_baseline = _latest_reproduction_ref()

    # 第一个未预填的槽位
    step = 0
    for i, (skey, _lbl, _ask) in enumerate(_BRIEF_SLOTS):
        if not collected.get(skey):
            step = i
            break
    else:
        step = len(_BRIEF_SLOTS) - 1

    session.pending = {
        "flow": "brief",
        "step": step,
        "collected": collected,
        "source_ideation_run": source_ideation_run,
        "reproduced_baseline": reproduced_baseline,
    }
    key, label, ask = _BRIEF_SLOTS[step]
    baseline_note = (
        f"\n已关联最近的复现基线：{reproduced_baseline}（将作为 our reproduction 写入实验计划）。"
        if reproduced_baseline
        else ""
    )
    return (
        "好，开始确认**《研究任务书》**。我会跟你逐步确认 6 件事：\n"
        "①研究题目 ②核心科学问题 ③调研结论/空白 ④研究假设 ⑤实验范围/数据/指标 ⑥目标会议。\n"
        "每步我会先复述我的理解请你确认；全部确认后物化为流水线产物并启动（co-pilot 模式）。"
        "随时可说「取消」退出。"
        + prefill_note
        + baseline_note
        + f"\n\n第 {step + 1}/6 步——{ask}"
    )


async def _handle_overleaf(text: str, session: ChatSession) -> str:
    """「同步到 Overleaf」：把指定/当前/最近 run 的论文推到共享 Overleaf 项目。"""
    from researchclaw.server.app import _app_state

    # 1) 解析目标 run：消息里显式给的 run_id > 会话当前 run > 最近的 run
    m = re.search(r"(rc-[\w-]+)", text)
    run_id = m.group(1) if m else session.current_run
    if not run_id:
        from researchclaw.dashboard.collector import DashboardCollector

        runs = DashboardCollector().collect_all()
        if not runs:
            return "还没有任何运行记录——先开一条流水线，跑完再同步。"
        run_id = runs[0].run_id

    run_dir = REPO_ROOT / "artifacts" / run_id
    if not run_dir.is_dir():
        return f"找不到 run 目录：artifacts/{run_id}"

    from researchclaw.overleaf.run_sync import sync_run_to_overleaf

    try:
        result = await asyncio.to_thread(
            sync_run_to_overleaf, run_dir, run_id, _app_state["config"]
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("overleaf sync failed from chat")
        return f"同步失败：{exc}"

    if not result.get("ok"):
        return f"同步未完成：{result.get('reason', 'unknown')}"
    note = "（内容无变化，已是最新）" if not result.get("pushed") else ""
    folder = result.get("folder", f"runs/{run_id}/")
    return (
        f"已同步 **{run_id}** 到 Overleaf 共享项目 {note}\n"
        f"- 位置：`{folder}/`（paper.tex + references.bib + 图表目录）\n"
        "- 在 Overleaf 打开该文件夹，把 paper.tex 设为 Main document 即可编译\n"
        "- 在 Overleaf 里用 `% 批注: 你的意见` 写批注或直接改正文，然后对我说「拉取 Overleaf 改动」取回"
    )


async def _handle_overleaf_pull(text: str, session: ChatSession) -> str:
    """「拉取 Overleaf 改动」：把用户在 Overleaf 上的批注/修改拉回 run 目录。"""
    from researchclaw.server.app import _app_state

    m = re.search(r"(rc-[\w-]+)", text)
    run_id = m.group(1) if m else session.current_run
    if not run_id:
        from researchclaw.dashboard.collector import DashboardCollector

        runs = DashboardCollector().collect_all()
        if not runs:
            return "还没有任何运行记录。"
        run_id = runs[0].run_id

    run_dir = REPO_ROOT / "artifacts" / run_id
    if not run_dir.is_dir():
        return f"找不到 run 目录：artifacts/{run_id}"

    lang = "en" if re.search(r"英文|英语|\ben\b", text, re.IGNORECASE) else "zh"

    from researchclaw.overleaf.run_sync import pull_run_from_overleaf

    try:
        result = await asyncio.to_thread(
            pull_run_from_overleaf, run_dir, run_id, _app_state["config"], lang
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("overleaf pull failed from chat")
        return f"拉取失败：{exc}"

    if not result.get("ok"):
        return f"拉取未完成：{result.get('reason', 'unknown')}"
    copied = result.get("copied", [])
    if not copied:
        return f"Overleaf 上 `{run_id}`（{lang}）没有新的改动。"
    return (
        f"已拉回 **{run_id}**（{lang}）的 {len(copied)} 个变更文件：\n"
        + "\n".join(f"- `{name}`" for name in copied)
        + f"\n存放于 `paper_annotations/{lang}/`，下一步对我说「按批注改稿」即可。"
    )


# ---------------------------------------------------------------- model switch

def _update_primary_model(model: str) -> str:
    global _llm_client
    p = REPO_ROOT / "config.arc.yaml"
    if not p.is_file():
        return "没找到 `config.arc.yaml`——先在 Settings 页保存一份配置，再让我切模型。"
    real = p.resolve()
    text = real.read_text(encoding="utf-8")

    new_text, n = re.subn(
        r'^(\s*primary_model:\s*).*$', rf'\g<1>"{model}"', text, count=1, flags=re.M
    )
    if n == 0:
        new_text = text.rstrip() + f'\nllm:\n  primary_model: "{model}"\n'

    bak = real.with_name(real.name + f".bak.{int(time.time())}")
    bak.write_text(text, encoding="utf-8")
    real.write_text(new_text, encoding="utf-8")

    # 同步热更新服务进程的内存配置：下一个 run 立即生效，无需重启
    live_note = ""
    try:
        import dataclasses

        from researchclaw.server.app import _app_state

        cfg = _app_state["config"]
        _app_state["config"] = dataclasses.replace(
            cfg, llm=dataclasses.replace(cfg.llm, primary_model=model)
        )
        _llm_client = None
        live_note = "（并已热更新到当前服务进程，下一个 run 立即生效）"
    except Exception:  # noqa: BLE001
        live_note = "（内存热更新失败，需重启 dashboard 容器后生效）"

    return (
        f"已把主模型切换为 **{model}**{live_note}。\n"
        f"- 文件：`{real.name}`（自动备份 `{bak.name}`）\n"
        "- 兜底模型链也可以在 Settings 页的 `llm.fallback_models` 里改，"
        "或直接对我说「把兜底模型换成 X 和 Y」"
    )


async def _handle_config(text: str, session: ChatSession) -> str:
    m = re.search(
        r"(?:主模型|primary[_\s]?model|主力模型)[^\w]*(?:换成|改为|切换成?|=>|=)\s*([\w.\-:]+)",
        text,
        re.IGNORECASE,
    )
    if m:
        return _update_primary_model(m.group(1))
    m = re.search(r"(?:把|将)?(?:模型|model)?\s*(?:换成|改为|切换成?)\s*([\w.\-:]+)", text, re.IGNORECASE)
    if m and re.search(r"model|模型", text, re.IGNORECASE):
        return _update_primary_model(m.group(1))
    return (
        "想改模型的话，直接对我说：\n"
        "- 「把主模型换成 qwen-plus」→ 我改配置并热更新（含自动备份）\n"
        "- 兜底模型链在 Settings 页改 `llm.fallback_models`（YAML 列表）\n\n"
        "其它配置（实验预算、指标、HITL 模式等）在 Settings 页直接编辑保存即可。"
    )


# ---------------------------------------------------------------- status / results

async def _handle_status(text: str, session: ChatSession) -> str:
    from researchclaw.dashboard.collector import DashboardCollector

    collector = DashboardCollector()
    runs = collector.collect_all()
    if not runs:
        return "还没有任何运行记录。告诉我研究题目，我帮你开第一条。"

    active = [r for r in runs if r.is_active]
    if active:
        r = active[0]
        return (
            f"**进行中的运行**：{r.run_id}\n"
            f"- 阶段：{r.current_stage}/23（{r.current_stage_name}）\n"
            f"- 状态：{r.status}\n"
            f"- 主题：{r.topic or '(未设置)'}"
        )

    latest = runs[0]
    return (
        f"**最近的运行**：{latest.run_id}\n"
        f"- 阶段：{latest.current_stage}/23\n"
        f"- 状态：{latest.status}\n"
        f"- 已完成阶段：{len(latest.stages_completed)}"
    )


async def _handle_results(text: str, session: ChatSession) -> str:
    from researchclaw.dashboard.collector import DashboardCollector

    collector = DashboardCollector()
    runs = collector.collect_all()
    if not runs:
        return "还没有结果——先开一条流水线。"

    latest = runs[0]
    if not latest.metrics:
        return f"运行 {latest.run_id} 还没有指标（当前 {latest.current_stage}/23 阶段）。"

    lines = [f"**{latest.run_id} 的结果**：\n"]
    for key, value in latest.metrics.items():
        if isinstance(value, (int, float)):
            lines.append(f"- {key}: {value}")
    return "\n".join(lines) if len(lines) > 1 else f"指标：{latest.metrics}"


async def _llm_reply(session: ChatSession, extra_system: str = "") -> str:
    """统一的 LLM 应答入口：会话历史 + 角色系统提示（可附加状态上下文）。"""
    try:
        client = _llm()
        messages = session.get_context(12)
        system = _SYSTEM_PROMPT + (f"\n\n{extra_system}" if extra_system else "")
        resp = await asyncio.to_thread(
            client.chat, messages, system=system, max_tokens=600
        )
        return resp.content
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM chat failed")
        return f"（模型调用失败，请检查 Settings 页的 API 配置后重试）\n错误：{exc}"


async def _handle_paper(text: str, session: ChatSession) -> str:
    from researchclaw.dashboard.collector import DashboardCollector

    runs = DashboardCollector().collect_all()
    if runs:
        latest = runs[0]
        ctx = (
            f"当前论文状态：最近运行 {latest.run_id}，阶段 {latest.current_stage}/23，"
            f"状态 {latest.status}。"
            + ("论文已生成，可结合其内容审读。" if latest.current_stage >= 17
               else "论文尚未起草（第 17 阶段才生成），如用户想要新论文，引导其先给研究题目启动 run。")
        )
    else:
        ctx = "当前论文状态：尚无任何运行记录，如用户想要论文，引导其先给研究题目启动 run。"
    extra = (
        ctx
        + "\n用户正在咨询论文相关问题。你可以：结合运行状态给写作/修改建议、"
        "讲解 TGRS 期刊论文结构、评估选题可行性。"
        "注意如实说明：你不能直接改动 Overleaf/磁盘上的 tex 文件，"
        "实际编辑在 Overleaf 进行（同步通道已通）。"
    )
    return await _llm_reply(session, extra_system=extra)


# ---------------------------------------------------------------- LLM-backed general chat

_SYSTEM_PROMPT = (
    "你是 ResearchClaw 的研究助理，运行在用户本地的自动科研流水线（23 阶段："
    "文献→假设→实验设计→训练→论文写作→自动评审）上。始终用与用户相同的语言回答"
    "（用户中文你中文）。\n"
    "系统有**两条并行使用通道，回答中不要混淆**：\n"
    "1) 原流程：用户直接给研究题目（终端 researchclaw run 或 Settings 配置），"
    "流水线全自动推进、门控处可人工把关（co-pilot）；\n"
    "2) 对话通道：用户在这里聊 insight/研究问题——你可以启发式追问帮他把想法聊成"
    "具体题目，题目明确后调用启动（用户说「开始/跑起来」即启动同一条流水线）。\n"
    "你能实际做的事：启动/停止运行、查状态、看结果、切换主模型与兜底模型、回答科研问题。\n"
    "**研究任务书（BRIEF）通道**：用户说「开始确认/定题/确认选题」时，进入 6 步确认流程"
    "（研究题目→科学问题→调研结论/空白→假设→实验范围/数据/指标→目标会议）；"
    "流程中你的角色是引导用户把每件事说具体（可检验、可衡量），全部确认后系统会"
    "把任务书物化为流水线产物并从中间阶段启动。选题引擎的证据卡可以预填前 3 步。\n"
    "你做不到（如实说明）：在门控节点代替用户审批——那在终端 researchclaw attach 做。\n"
    "回答简洁（≤250字），要步骤时用列表。"
)


async def _handle_general(text: str, session: ChatSession) -> str:
    return await _llm_reply(session)


async def _handle_help(text: str, session: ChatSession) -> str:
    return (
        "我可以这样帮你（**两条通道并存**）：\n"
        "- **对话式**：直接说你的 insight/研究问题，我帮你聊成题目并启动\n"
        "- **原流程**：你在 Settings/终端给好 topic，流水线全自动跑（门控处终端把关）\n\n"
        "常用语：\n"
        "- 「开始研究：XXX」/ 「跑起来」→ 启动（缺题目我会追问）\n"
        "- 「到哪一步了」/ 「结果怎么样」→ 状态与指标\n"
        "- 「停止」→ 中止当前运行\n"
        "- 「把主模型换成 qwen-plus」→ 切换模型（含备份+热更新）\n"
        "- 随便聊 → 我按研究助理身份回答（已接入大模型）"
    )


async def _handle_topic(text: str, session: ChatSession) -> str:
    # 选题类问题交给 LLM 深度回答，比固定话术更有用
    return await _handle_general(text, session)


_HANDLERS = {
    Intent.BRIEF: _handle_brief,
    Intent.HELP: _handle_help,
    Intent.CHECK_STATUS: _handle_status,
    Intent.START_PIPELINE: _handle_start,
    Intent.STOP_PIPELINE: _handle_stop,
    Intent.SYNC_OVERLEAF: _handle_overleaf,
    Intent.PULL_OVERLEAF: _handle_overleaf_pull,
    Intent.IDEATION: _handle_ideate,
    Intent.REPRODUCE: _handle_reproduce,
    Intent.TOPIC_SELECTION: _handle_topic,
    Intent.MODIFY_CONFIG: _handle_config,
    Intent.DISCUSS_RESULTS: _handle_results,
    Intent.EDIT_PAPER: _handle_paper,
    Intent.GENERAL_CHAT: _handle_general,
}
