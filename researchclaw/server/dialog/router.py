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
    return (
        f"已同步 **{run_id}** 到 Overleaf 共享项目 {note}\n"
        f"- 位置：`runs/{run_id}/`（paper.tex + references.bib + figures/）\n"
        "- 在 Overleaf 打开该文件夹，把 paper.tex 设为 Main document 即可编译\n"
        "- 之后你在 Overleaf 上的修改会经 git 双向同步，流水线内我说「拉取 Overleaf 改动」可取回"
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


async def _handle_paper(text: str, session: ChatSession) -> str:
    return (
        "论文在第 17 阶段（起草）完成后可编辑。\n\n"
        "我可以帮你：\n"
        "- 审读摘要并提修改建议\n"
        "- 检查引言结构\n"
        "- 核对实验描述与真实结果是否一致\n"
        "- 扩充相关工作\n\n"
        "想先从哪一节开始？"
    )


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
    "你做不到（如实说明）：在门控节点代替用户审批——那在终端 researchclaw attach 做。\n"
    "回答简洁（≤250字），要步骤时用列表。"
)


async def _handle_general(text: str, session: ChatSession) -> str:
    try:
        client = _llm()
        messages = session.get_context(12)
        resp = await asyncio.to_thread(
            client.chat, messages, system=_SYSTEM_PROMPT, max_tokens=600
        )
        return resp.content
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM chat failed")
        return (
            "（模型调用失败，请检查 Settings 页的 API 配置后重试）\n"
            f"错误：{exc}"
        )


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
    Intent.HELP: _handle_help,
    Intent.CHECK_STATUS: _handle_status,
    Intent.START_PIPELINE: _handle_start,
    Intent.STOP_PIPELINE: _handle_stop,
    Intent.SYNC_OVERLEAF: _handle_overleaf,
    Intent.TOPIC_SELECTION: _handle_topic,
    Intent.MODIFY_CONFIG: _handle_config,
    Intent.DISCUSS_RESULTS: _handle_results,
    Intent.EDIT_PAPER: _handle_paper,
    Intent.GENERAL_CHAT: _handle_general,
}
