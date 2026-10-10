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
        msg_data = None
        text = raw_message

    session = _session_manager.get_or_create(client_id)
    # 前端项目上下文绑定：消息带 run_id 时显式切换对话的当前项目
    if isinstance(msg_data, dict):
        rid = msg_data.get("run_id")
        if isinstance(rid, str) and rid.startswith("rc-"):
            session.current_run = rid
    session.add_message("user", text)

    # 研究任务书确认流程（BRIEF flow）进行中 → 所有消息交给审阅机（其内部处理确认/修改/取消）
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


# ---------------------------------------------------------------- BRIEF flow（研究任务书，plan-mode 风格）

# 6 个槽位：(key, 中文标签)。一次性生成完整草案，用户整体审阅、逐条提修改意见（类 Codex plan mode），
# 不再逐槽位问答。
_BRIEF_SLOTS: list[tuple[str, str]] = [
    ("topic", "研究题目"),
    ("scientific_question", "核心科学问题"),
    ("survey_summary", "调研结论与研究空白"),
    ("hypotheses", "研究假设"),
    ("experiment_scope", "实验范围/数据/指标"),
    ("target_conference", "目标会议/期刊"),
]

_BRIEF_LABELS: dict[str, str] = dict(_BRIEF_SLOTS)

_BRIEF_FULL_SCHEMA = (
    '{"topic": "研究题目（一句话）", '
    '"scientific_question": "核心科学问题（一句话、可检验）", '
    '"survey_summary": "调研结论与研究空白（要点式，200 字内）", '
    '"hypotheses": ["可检验假设1", "假设2"], '
    '"experiment_scope": {"scope": "实验范围/设计描述", "datasets": ["数据集1"], '
    '"metrics": [{"metric_key": "指标名", "direction": "maximize 或 minimize"}]}, '
    '"target_conference": "目标会议/期刊名", '
    '"inferred": ["以上字段中用户没有明说、由你推断补全的 key 列表"]}'
)

_BRIEF_REVIEW_HINT = (
    "----------\n"
    "✏️ 要改哪里直接说（如「目标会议改成 NeurIPS」「假设加一条：…」「第 2 条重写：…」），我改完发你新版；\n"
    "👍 全部满意回复「**确认**」，立即物化并启动流水线（co-pilot，门控处再把关）；\n"
    "🔄 推倒重来：「重新生成：补充说明…」；\n"
    "✋ 退出：「取消」。"
)


def _brief_slot_filled(key: str, val: Any) -> bool:
    """槽位是否有实质内容。"""
    if key == "experiment_scope":
        return bool(
            isinstance(val, dict)
            and (val.get("scope") or val.get("datasets") or val.get("metrics"))
        )
    if isinstance(val, list):
        return bool(val)
    return bool(str(val or "").strip())


def _brief_normalize_full(data: Any) -> tuple[dict[str, Any], list[str]]:
    """把 LLM 整稿 JSON 规整成 collected 槽位字典 + inferred（AI 推断）列表。"""
    collected: dict[str, Any] = {k: "" for k, _ in _BRIEF_SLOTS}
    collected["hypotheses"] = []
    collected["experiment_scope"] = {"scope": "", "datasets": [], "metrics": []}
    if not isinstance(data, dict):
        return collected, []
    for key in ("topic", "scientific_question", "survey_summary", "target_conference"):
        collected[key] = str(data.get(key) or "").strip()
    hyps = data.get("hypotheses")
    if isinstance(hyps, str):
        hyps = [hyps]
    collected["hypotheses"] = [str(h).strip() for h in (hyps or []) if str(h).strip()]
    scope = data.get("experiment_scope")
    if isinstance(scope, str):
        scope = {"scope": scope}
    scope = scope if isinstance(scope, dict) else {}
    metrics: list[dict[str, str]] = []
    for m in scope.get("metrics") or []:
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
    collected["experiment_scope"] = {
        "scope": str(scope.get("scope") or "").strip(),
        "datasets": [
            str(d).strip() for d in (scope.get("datasets") or []) if str(d).strip()
        ],
        "metrics": metrics,
    }
    inferred = [k for k in (data.get("inferred") or []) if k in _BRIEF_LABELS]
    return collected, inferred


async def _brief_llm_json(prompt: str, max_tokens: int = 8192) -> dict[str, Any] | None:
    """整稿 LLM 调用（json_mode）；推理型模型会消耗思考 token，预算给足。失败返回 None。"""
    try:
        client = _llm()
        resp = await asyncio.to_thread(
            client.chat,
            [{"role": "user", "content": prompt}],
            json_mode=True,
            max_tokens=max_tokens,
        )
        data = json.loads(resp.content)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        logger.debug("brief full-draft LLM call failed", exc_info=True)
        return None


async def _brief_generate_full(
    seed: str, session: ChatSession, prefill: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """一次性起草完整任务书：用户原话 + 近期对话 + 预填材料（权威）→ 6 槽位整稿。"""
    history = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}：{m['content']}"
        for m in session.get_context(last_n=6)
    )
    known = {k: v for k, v in prefill.items() if _brief_slot_filled(k, v)}
    prompt = (
        "你是科研项目规划助手。请根据下面的材料，一次性起草一份完整的《研究任务书》草稿。\n\n"
        f"【用户最新输入】\n{seed}\n\n"
        + (f"【近期对话】\n{history}\n\n" if history else "")
        + (
            "【已有材料】（权威内容，将原样保留，请在其基础上补全其余字段）\n"
            + json.dumps(known, ensure_ascii=False)
            + "\n\n"
            if known
            else ""
        )
        + "要求：\n"
        "- 六个字段都尽量填完整、具体、可执行；用户没明说的可以合理推断，"
        "并把推断的字段 key 写进 inferred 列表（用户明说或已有材料给出的不要列进去）。\n"
        "- 不要编造具体数值结果；指标方向只有 maximize/minimize 两种（越高越好=maximize）。\n"
        "- 用中文填写。\n"
        f"严格输出 JSON，格式：{_BRIEF_FULL_SCHEMA}"
    )
    data = await _brief_llm_json(prompt)
    collected, inferred = _brief_normalize_full(data)
    # 预填材料权威：硬覆盖对应槽位，且不算推断
    for key, val in known.items():
        collected[key] = val
        if key in inferred:
            inferred.remove(key)
    if not collected.get("topic") and seed.strip():
        # LLM 彻底失败兜底：用户原话当题目，其余留空待用户补
        collected["topic"] = seed.strip()[:120]
    return collected, inferred


async def _brief_apply_revision(
    collected: dict[str, Any], instruction: str
) -> tuple[dict[str, Any], list[str]] | None:
    """把用户修改意见应用到整稿：LLM 输出更新后的完整任务书。

    返回 (合并后的 collected, 变更的槽位 key 列表)；抽不出有效修改返回 None。
    """
    prompt = (
        "你是科研项目规划助手。这是一份《研究任务书》草稿的当前内容（JSON）：\n"
        f"{json.dumps(collected, ensure_ascii=False)}\n\n"
        f"【用户的修改意见】\n{instruction}\n\n"
        "请输出修改后的完整任务书：只按意见改动相关字段，其余原样保留；"
        "意见里明确给出的字段不再是推断（inferred 列表相应去掉）。\n"
        "不要编造具体数值结果；用中文。\n"
        f"严格输出 JSON，格式：{_BRIEF_FULL_SCHEMA}"
    )
    data = await _brief_llm_json(prompt)
    if data is None:
        return None
    new_collected, _ = _brief_normalize_full(data)
    merged = dict(collected)
    changed: list[str] = []
    for key, _label in _BRIEF_SLOTS:
        new_val = new_collected.get(key)
        if not _brief_slot_filled(key, new_val):
            continue  # LLM 丢空的槽位保留旧值
        if json.dumps(new_val, ensure_ascii=False, sort_keys=True) != json.dumps(
            collected.get(key), ensure_ascii=False, sort_keys=True
        ):
            changed.append(key)
        merged[key] = new_val
    if not changed:
        return None
    return merged, changed


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


def _brief_format_full(collected: dict[str, Any], inferred: Any = ()) -> str:
    """整稿回显：6 个槽位一次列全，空位标待补充，AI 推断位标重点核对。"""
    inferred_set = set(inferred or ())
    lines = ["**《研究任务书》草案**", ""]
    for i, (key, label) in enumerate(_BRIEF_SLOTS, 1):
        val = collected.get(key)
        if _brief_slot_filled(key, val):
            tag = " `（AI 推断，请重点核对）`" if key in inferred_set else ""
            body = _brief_format_value(val)
        else:
            tag = ""
            body = "（待补充——直接告诉我即可）"
        lines.append(f"{i}. **{label}**{tag}\n{body}")
    return "\n\n".join(lines)


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
    """BRIEF 审阅机（plan-mode）：确认启动 / 修改意见整稿更新 / 重新生成 / 取消。"""
    pending = session.pending
    collected = pending.setdefault("collected", {})
    inferred = list(pending.get("inferred") or [])

    # 取消
    if re.search(r"(?:\bcancel\b|取消|算了|先不弄|退出确认)", text, re.IGNORECASE):
        session.pending.clear()
        return "好的，已取消任务书确认。想重新开始就说「开始确认」。"

    # 确认 → 物化并启动
    if re.search(
        r"^\s*(?:确认|对的?|是的?|没问题|可以|ok|okay|confirm|yes)\s*[。！!]*\s*$",
        text,
        re.IGNORECASE,
    ):
        if not str(collected.get("topic") or "").strip():
            return (
                "研究题目还是空的——先告诉我一句题目（或直接给修改意见），再确认启动。\n\n"
                + _brief_format_full(collected, inferred)
                + "\n\n"
                + _BRIEF_REVIEW_HINT
            )
        return await _finalize_brief(session)

    # 重新生成：可带补充说明
    m = re.match(r"^\s*(?:重新生成|推倒重来|重来)\s*[:：]?\s*(.*)$", text, re.S)
    if m:
        guidance = m.group(1).strip()
        seed = guidance or json.dumps(collected, ensure_ascii=False)
        new_collected, new_inferred = await _brief_generate_full(seed, session, collected)
        pending["collected"] = new_collected
        pending["inferred"] = new_inferred
        return (
            "🔄 已重新起草：\n\n"
            + _brief_format_full(new_collected, new_inferred)
            + "\n\n"
            + _BRIEF_REVIEW_HINT
        )

    # 其余一律视为修改意见 → 整稿更新
    result = await _brief_apply_revision(collected, text)
    if result is None:
        return (
            "这条意见我没理解到位，任务书保持原样。换个说法试试，例如"
            "「目标会议改成 NeurIPS」「假设加一条：…」「第 2 条重写：…」。\n\n"
            + _brief_format_full(collected, inferred)
            + "\n\n"
            + _BRIEF_REVIEW_HINT
        )
    new_collected, changed = result
    pending["collected"] = new_collected
    inferred = [k for k in inferred if k not in changed]
    pending["inferred"] = inferred
    changed_labels = "、".join(_BRIEF_LABELS[k] for k in changed)
    return (
        f"✏️ 已按你的意见更新（改动：{changed_labels}）：\n\n"
        + _brief_format_full(new_collected, inferred)
        + "\n\n"
        + _BRIEF_REVIEW_HINT
    )


async def _handle_brief(text: str, session: ChatSession) -> str:
    """「开始确认/定题/就选第 N 个」：一次性起草完整《研究任务书》供整体审阅（plan-mode 风格）。

    可从选题证据卡/复现基线预填；预填材料权威，原样保留。
    """
    if session.pending.get("flow") == "brief":
        return await _handle_brief_flow(text, session)

    prefill: dict[str, Any] = {}
    source_ideation_run = ""
    notes: list[str] = []

    m = re.search(r"第\s*(\d+)\s*个", text)
    if m:
        card, run_name = _load_ideation_card(int(m.group(1)))
        if card:
            source_ideation_run = run_name
            prefill["topic"] = str(card.get("question") or "")
            prefill["scientific_question"] = str(card.get("question") or "")
            evidence = [e for e in (card.get("gap_evidence") or []) if e]
            parts = []
            if card.get("note_zh"):
                parts.append(str(card["note_zh"]))
            if evidence:
                parts.append("缺口证据：" + "；".join(evidence[:3]))
            if card.get("novelty_hint"):
                parts.append("新颖性：" + str(card["novelty_hint"]))
            prefill["survey_summary"] = "\n".join(parts)
            notes.append(f"已纳入选题报告 **{run_name}** 的第 {m.group(1)} 张证据卡（前 3 项原样保留）。")
        else:
            notes.append("（没找到对应的选题证据卡，按你的描述起草。）")

    reproduced_baseline = ""
    if re.search(r"复现", text):
        reproduced_baseline = _latest_reproduction_ref()
        if reproduced_baseline:
            notes.append(
                f"已关联最近的复现基线：{reproduced_baseline}（将作为 our reproduction 写入实验计划）。"
            )

    collected, inferred = await _brief_generate_full(text, session, prefill)
    session.pending = {
        "flow": "brief",
        "mode": "review",
        "collected": collected,
        "inferred": inferred,
        "source_ideation_run": source_ideation_run,
        "reproduced_baseline": reproduced_baseline,
    }
    intro = (
        "好，不逐条问你了——我根据你的描述和现有材料一次性起草了完整的**《研究任务书》**，"
        "你直接在整体上审阅修改："
    )
    if notes:
        intro += "\n" + "\n".join(notes)
    return (
        intro
        + "\n\n"
        + _brief_format_full(collected, inferred)
        + "\n\n"
        + _BRIEF_REVIEW_HINT
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
    "**研究任务书（BRIEF）通道**：用户说「开始确认/定题/确认选题」时，系统一次性起草完整任务书草案"
    "（研究题目/科学问题/调研结论/假设/实验范围/目标会议，AI 推断的字段会标注），"
    "用户整体审阅、直接回复修改意见迭代，「确认」后物化为流水线产物并从中间阶段启动；"
    "选题引擎的证据卡会作为权威材料预填。不要替用户逐条提问。\n"
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
