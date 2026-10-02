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
