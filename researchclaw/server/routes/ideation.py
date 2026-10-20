"""Ideation REST 路由——选题引擎的浏览器/对话入口。

跑 Stage 1–8（文献扫描→缺口分析→候选问题+查新），随后用
researchclaw.ideation.report 生成证据卡 ideation_report.json。
模式与 routes/pipeline.py 一致：单租户、后台 executor、轮询状态。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ideation"])

REPO_ROOT = Path(__file__).resolve().parents[3]


class IdeationStartRequest(BaseModel):
    """请求体：选题方向（自然语言）。"""

    direction: str


class IdeationStartResponse(BaseModel):
    run_id: str
    status: str
    output_dir: str


_state: dict[str, Any] | None = None
_task: asyncio.Task[Any] | None = None
_lock = asyncio.Lock()


def _get_app_state() -> dict[str, Any]:
    from researchclaw.server.app import _app_state

    return _app_state


@router.post("/ideation/start", response_model=IdeationStartResponse)
async def start_ideation(req: IdeationStartRequest) -> IdeationStartResponse:
    """启动一次选题运行（Stage 1-8 + 证据卡）。"""
    global _state, _task

    direction = req.direction.strip()
    if len(direction) < 4:
        raise HTTPException(status_code=400, detail="direction 太短")

    async with _lock:
        if _state and _state.get("status") == "running":
            raise HTTPException(status_code=409, detail="一次选题运行正在进行中")

        config = _get_app_state()["config"]
        config = dataclasses.replace(
            config, research=dataclasses.replace(config.research, topic=direction)
        )

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dhash = hashlib.sha256(direction.encode()).hexdigest()[:6]
        run_id = f"id-{ts}-{dhash}"
        run_dir = REPO_ROOT / "artifacts" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        _state = {
            "run_id": run_id,
            "status": "running",
            "direction": direction,
            "output_dir": str(run_dir),
        }

    async def _run() -> None:
        global _state
        try:
            from researchclaw.adapters import AdapterBundle
            from researchclaw.pipeline.runner import execute_pipeline
            from researchclaw.pipeline.stages import Stage

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: execute_pipeline(
                    run_dir=run_dir,
                    run_id=run_id,
                    config=config,
                    adapters=AdapterBundle(),
                    to_stage=Stage.HYPOTHESIS_GEN,
                    auto_approve_gates=True,
                ),
            )
            from researchclaw.ideation.report import build_ideation_report

            report = await loop.run_in_executor(
                None, lambda: build_ideation_report(run_dir, config)
            )
            if _state:
                _state["status"] = "completed" if report.get("ok") else "failed"
                _state["cards"] = len(report.get("cards", []))
                if not report.get("ok"):
                    _state["error"] = report.get("reason")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ideation run failed")
            if _state:
                _state["status"] = "failed"
                _state["error"] = str(exc)

    _task = asyncio.create_task(_run())
    return IdeationStartResponse(run_id=run_id, status="running", output_dir=str(run_dir))


@router.get("/ideation/status")
async def ideation_status() -> dict[str, Any]:
    """当前/最近一次选题运行状态。"""
    if not _state:
        return {"status": "idle"}
    return _state


@router.get("/ideation/report")
async def ideation_report(run_id: str | None = None) -> dict[str, Any]:
    """读取证据卡（默认当前 run；可指定历史 id-xxx run）。"""
    rid = run_id or (_state or {}).get("run_id")
    if not rid:
        raise HTTPException(status_code=404, detail="还没有选题运行记录")
    if not rid.startswith("id-"):
        raise HTTPException(status_code=400, detail="run_id 必须是 id- 开头的选题 run")
    path = REPO_ROOT / "artifacts" / rid / "ideation_report.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"证据卡尚未生成：{rid}")
    return json.loads(path.read_text(encoding="utf-8"))


@router.post("/ideation/stop")
async def stop_ideation() -> dict[str, str]:
    """中止当前选题运行。"""
    global _state, _task
    if not _task or not _state or _state.get("status") != "running":
        raise HTTPException(status_code=404, detail="没有进行中的选题运行")
    _task.cancel()
    _state["status"] = "stopped"
    return {"status": "stopped"}


# ---------------------------------------------------------------------------
# Step 1 — 创新点审核（IdeaSpark 模式库 + DeepSeek）
# ---------------------------------------------------------------------------

class IdeaCardsRequest(BaseModel):
    direction: str
    n: int = 4


class IdeaConfirmRequest(BaseModel):
    direction: str
    card: dict[str, Any]
    auto_start: bool = True
    auto_approve: bool = True
    # Step 1 只跑"到实验完成为止"；传 null 才跑完整 23 阶段
    to_stage: str | None = "EXPERIMENT_RUN"


@router.post("/ideation/cards")
async def idea_cards(req: IdeaCardsRequest) -> dict[str, Any]:
    """根据自然语言方向生成创新点卡片（不建 run，纯审核用）。"""
    from researchclaw.ideation.cards import generate_cards
    from researchclaw.llm.client import LLMClient

    direction = (req.direction or "").strip()
    if len(direction) < 4:
        raise HTTPException(status_code=400, detail="研究方向太短，至少 4 个字符")

    config = _get_app_state()["config"]
    llm = LLMClient.from_rc_config(config)
    loop = asyncio.get_event_loop()
    try:
        cards = await loop.run_in_executor(
            None, lambda: generate_cards(direction, llm, max(3, min(int(req.n), 5)))
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("idea card generation failed")
        raise HTTPException(status_code=500, detail=f"生成失败：{exc}") from exc

    return {
        "direction": direction,
        "model": getattr(getattr(llm, "config", None), "primary_model", ""),
        "cards": cards,
    }


@router.post("/ideation/confirm")
async def idea_confirm(req: IdeaConfirmRequest) -> dict[str, Any]:
    """用户选中的卡片 -> 物化成任务书 ->（可选）立即从实验阶段启动。"""
    import dataclasses
    import hashlib
    from datetime import datetime, timezone

    from researchclaw.adapters import AdapterBundle
    from researchclaw.ideation.brief import materialize
    from researchclaw.ideation.cards import _normalize_card, card_to_brief
    from researchclaw.pipeline.runner import execute_pipeline
    from researchclaw.pipeline.stages import Stage

    state = _get_app_state()
    config = state["config"]
    card = _normalize_card(req.card or {})
    if not card.get("title") or not card.get("method"):
        raise HTTPException(status_code=400, detail="卡片缺少 title 或 method")

    brief = card_to_brief(card, getattr(config.export, "target_conference", ""))
    config = dataclasses.replace(
        config, research=dataclasses.replace(config.research, topic=brief.topic)
    )
    if brief.metrics:
        first = brief.metrics[0]
        over: dict[str, Any] = {}
        if first.get("metric_key"):
            over["metric_key"] = first["metric_key"]
        if first.get("direction") in ("increase", "maximize"):
            over["metric_direction"] = "maximize"
        elif first.get("direction") in ("decrease", "minimize"):
            over["metric_direction"] = "minimize"
        if over:
            config = dataclasses.replace(
                config, experiment=dataclasses.replace(config.experiment, **over)
            )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    topic_hash = hashlib.sha256(brief.topic.encode()).hexdigest()[:6]
    run_id = f"rc-{ts}-{topic_hash}"
    run_dir = REPO_ROOT / "artifacts" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_event_loop()
    from_stage = await loop.run_in_executor(
        None, lambda: materialize(run_dir, brief, config)
    )

    # 卡片里的基线/方法/消融覆盖物化出的 exp_plan
    plan_overrides: dict[str, Any] = {}
    if card.get("baselines"):
        plan_overrides["baselines"] = card["baselines"]
    if plan_overrides:
        import yaml as _yaml

        plan_path = run_dir / "stage-09" / "exp_plan.yaml"
        if plan_path.is_file():
            _plan = _yaml.safe_load(plan_path.read_text(encoding="utf-8")) or {}
            _plan.update(plan_overrides)
            plan_path.write_text(
                _yaml.dump(_plan, default_flow_style=False, allow_unicode=True),
                encoding="utf-8",
            )

    # 存证：原始方向 + 选中的卡片 + 任务书
    (run_dir / "idea_card.json").write_text(
        json.dumps(
            {"direction": req.direction, "card": card, "confirmed_at": datetime.now(timezone.utc).isoformat()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # docker-sibling 部署：控制面看不到 GPU，显式写硬件档案
    s1 = run_dir / "stage-01"
    s1.mkdir(parents=True, exist_ok=True)
    (s1 / "hardware_profile.json").write_text(
        json.dumps(
            {"has_gpu": True, "gpu_type": "cuda", "gpu_name": "NVIDIA Tesla V100-PCIE-32GB",
             "vram_mb": 32768, "tier": "high", "warning": ""},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if req.auto_start:
        async def _run() -> None:
            adapters = AdapterBundle()
            if not req.auto_approve:
                try:
                    from researchclaw.hitl.presets import copilot_preset
                    from researchclaw.hitl.session import HITLSession

                    hitl_cfg = getattr(config, "hitl", None) or copilot_preset()
                    adapters.hitl = HITLSession(run_id=run_id, config=hitl_cfg, run_dir=run_dir)
                except Exception:  # noqa: BLE001
                    logger.warning("HITL session setup failed", exc_info=True)
            kb_root = Path(config.knowledge_base.root) if config.knowledge_base.root else None
            to_stage = None
            if req.to_stage:
                raw = req.to_stage.strip().upper().replace("STAGE-", "")
                to_stage = Stage[raw] if raw in Stage.__members__ else None
            await loop.run_in_executor(
                None,
                lambda: execute_pipeline(
                    run_dir=run_dir,
                    run_id=run_id,
                    config=config,
                    adapters=adapters,
                    from_stage=from_stage,
                    to_stage=to_stage,
                    auto_approve_gates=req.auto_approve,
                    skip_noncritical=True,
                    kb_root=kb_root,
                ),
            )

        asyncio.create_task(_run())

    return {
        "run_id": run_id,
        "output_dir": str(run_dir),
        "from_stage": from_stage.name,
        "topic": brief.topic,
        "started": req.auto_start,
    }
