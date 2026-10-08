"""Pipeline control API routes."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

import re as _re
_RUN_ID_RE = _re.compile(r"^rc-\d{8}-\d{6}-[a-f0-9]+$")


def _validated_run_dir(run_id: str) -> Path:
    """Validate run_id format and return the run directory path."""
    if not _RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail=f"Invalid run_id format: {run_id}")
    run_dir = Path("artifacts") / run_id
    # Ensure resolved path is under artifacts/
    if not run_dir.resolve().is_relative_to(Path("artifacts").resolve()):
        raise HTTPException(status_code=400, detail=f"Invalid run_id: {run_id}")
    return run_dir

router = APIRouter(prefix="/api", tags=["pipeline"])


class PipelineStartRequest(BaseModel):
    """Request body for starting a pipeline run."""

    topic: str | None = None
    config_overrides: dict[str, Any] | None = None
    auto_approve: bool = False
    # 研究任务书（Chat 多轮确认产物）：先物化为 stage 产物，再从中间阶段启动
    brief: dict[str, Any] | None = None
    # 显式指定起始阶段（Stage 名或编号），优先级高于 brief 的建议值
    from_stage: str | None = None


def _parse_from_stage(raw: str) -> "Any":
    """把 'CODE_GENERATION' / 'code_generation' / '10' / 'stage-10' 解析为 Stage。"""
    from researchclaw.pipeline.stages import Stage

    s = raw.strip()
    if not s:
        raise HTTPException(status_code=400, detail="from_stage 不能为空")
    m = _re.match(r"^(?:stage-?)?(\d{1,2})$", s, _re.IGNORECASE)
    if m:
        try:
            return Stage(int(m.group(1)))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"非法阶段编号: {raw}") from None
    try:
        return Stage[s.upper()]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"未知阶段名: {raw}") from None


class PipelineStartResponse(BaseModel):
    """Response after starting a pipeline."""

    run_id: str
    status: str
    output_dir: str


# In-memory tracking of the active run (single-tenant MVP)
_active_run: dict[str, Any] | None = None
_run_task: asyncio.Task[Any] | None = None
_run_lock = asyncio.Lock()


def _get_app_state() -> dict[str, Any]:
    """Get shared application state (set by app.py)."""
    from researchclaw.server.app import _app_state
    return _app_state


@router.post("/pipeline/start", response_model=PipelineStartResponse)
async def start_pipeline(req: PipelineStartRequest) -> PipelineStartResponse:
    """Start a new pipeline run."""
    global _active_run, _run_task

    async with _run_lock:
        if _active_run and _active_run.get("status") == "running":
            raise HTTPException(status_code=409, detail="A pipeline is already running")

        state = _get_app_state()
        config = state["config"]

        import dataclasses

        if req.topic:
            new_research = dataclasses.replace(config.research, topic=req.topic)
            config = dataclasses.replace(config, research=new_research)

        # 研究任务书：确认出的指标/目标会议/预算覆盖 config（dataclasses.replace 模式）
        brief_obj = None
        if req.brief:
            from researchclaw.ideation.brief import ResearchBrief

            brief_obj = ResearchBrief.from_dict(req.brief)
            if brief_obj.topic and not req.topic:
                config = dataclasses.replace(
                    config,
                    research=dataclasses.replace(
                        config.research, topic=brief_obj.topic
                    ),
                )
            if brief_obj.metrics:
                first = brief_obj.metrics[0]
                exp_over: dict[str, Any] = {}
                if first.get("metric_key"):
                    exp_over["metric_key"] = first["metric_key"]
                if first.get("direction") in ("minimize", "maximize"):
                    exp_over["metric_direction"] = first["direction"]
                if exp_over:
                    config = dataclasses.replace(
                        config,
                        experiment=dataclasses.replace(config.experiment, **exp_over),
                    )
            if brief_obj.target_conference:
                config = dataclasses.replace(
                    config,
                    export=dataclasses.replace(
                        config.export, target_conference=brief_obj.target_conference
                    ),
                )

        # config_overrides 里允许的少量标量覆盖（与 brief 覆盖同模式）
        for key, section in (
            ("metric_key", "experiment"),
            ("metric_direction", "experiment"),
            ("time_budget_sec", "experiment"),
            ("target_conference", "export"),
        ):
            value = (req.config_overrides or {}).get(key)
            if value is None:
                continue
            if key == "metric_direction" and value not in ("minimize", "maximize"):
                continue
            config = dataclasses.replace(
                config,
                **{section: dataclasses.replace(getattr(config, section), **{key: value})},
            )

        # 显式 from_stage 同步校验（非法值直接 400，不进后台任务）
        forced_from_stage = _parse_from_stage(req.from_stage) if req.from_stage else None

        import hashlib
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        topic_hash = hashlib.sha256(config.research.topic.encode()).hexdigest()[:6]
        run_id = f"rc-{ts}-{topic_hash}"
        run_dir = _validated_run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        _active_run = {
            "run_id": run_id,
            "status": "running",
            "output_dir": str(run_dir),
            "topic": config.research.topic,
        }

    async def _run_in_background() -> None:
        global _active_run
        try:
            from researchclaw.adapters import AdapterBundle
            from researchclaw.pipeline.runner import execute_pipeline
            from researchclaw.pipeline.stages import Stage

            kb_root = Path(config.knowledge_base.root) if config.knowledge_base.root else None
            if kb_root:
                kb_root.mkdir(parents=True, exist_ok=True)

            # 研究任务书物化：写 stage-01/02/07/08[/09] 产物 + research_brief.json，
            # 并决定 from_stage（显式 from_stage 参数优先于任务书建议值）
            from_stage = Stage.TOPIC_INIT
            if brief_obj is not None:
                from researchclaw.ideation.brief import materialize

                loop0 = asyncio.get_event_loop()
                from_stage = await loop0.run_in_executor(
                    None, lambda: materialize(run_dir, brief_obj, config)
                )
            if forced_from_stage is not None:
                from_stage = forced_from_stage
            if _active_run:
                _active_run["from_stage"] = from_stage.name

            adapters = AdapterBundle()
            if not req.auto_approve:
                # Web 启动 = co-pilot：挂 HITLSession，门控处写 hitl/waiting.json
                # 并轮询 hitl/response.json（无 input_callback → 文件 IPC）。
                # REST /runs/{id}/hitl/{waiting,respond} 与前端横幅都基于这两个文件。
                hitl_cfg = getattr(config, "hitl", None)
                try:
                    if hitl_cfg is None or not getattr(hitl_cfg, "enabled", False):
                        from researchclaw.hitl.presets import copilot_preset

                        hitl_cfg = copilot_preset()
                    from researchclaw.hitl.session import HITLSession

                    adapters.hitl = HITLSession(
                        run_id=run_id,
                        config=hitl_cfg,
                        run_dir=run_dir,
                    )
                except Exception as _hitl_exc:
                    logger.warning("HITL session setup failed: %s", _hitl_exc)

            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                lambda: execute_pipeline(
                    run_dir=run_dir,
                    run_id=run_id,
                    config=config,
                    adapters=adapters,
                    from_stage=from_stage,
                    auto_approve_gates=req.auto_approve,
                    skip_noncritical=True,
                    kb_root=kb_root,
                ),
            )
            done = sum(1 for r in results if r.status.value == "done")
            failed = sum(1 for r in results if r.status.value == "failed")
            if _active_run:
                _active_run["status"] = "completed" if failed == 0 else "failed"
                _active_run["stages_done"] = done
                _active_run["stages_failed"] = failed
        except Exception as exc:
            logger.exception("Pipeline run failed")
            if _active_run:
                _active_run["status"] = "failed"
                _active_run["error"] = str(exc)

    _run_task = asyncio.create_task(_run_in_background())

    return PipelineStartResponse(
        run_id=run_id,
        status="running",
        output_dir=str(run_dir),
    )


@router.post("/pipeline/stop")
async def stop_pipeline() -> dict[str, str]:
    """Stop the currently running pipeline."""
    global _active_run, _run_task

    if not _run_task or not _active_run:
        raise HTTPException(status_code=404, detail="No pipeline is running")

    _run_task.cancel()
    _active_run["status"] = "stopped"
    return {"status": "stopped"}


@router.get("/pipeline/status")
async def pipeline_status() -> dict[str, Any]:
    """Get current pipeline run status."""
    if not _active_run:
        return {"status": "idle"}
    return _active_run


@router.get("/pipeline/stages")
async def pipeline_stages() -> dict[str, Any]:
    """Get the 23-stage pipeline definition."""
    from researchclaw.pipeline.stages import Stage

    stages = []
    for s in Stage:
        stages.append({
            "number": int(s),
            "name": s.name,
            "label": getattr(s, "label", s.name.replace("_", " ").title()),
            "phase": getattr(s, "phase", ""),
        })
    return {"stages": stages}


@router.get("/runs")
async def list_runs() -> dict[str, Any]:
    """List historical pipeline runs from artifacts/ directory."""
    artifacts = Path("artifacts")
    runs: list[dict[str, Any]] = []
    if artifacts.exists():
        for d in sorted(artifacts.iterdir(), reverse=True):
            if d.is_dir() and d.name.startswith("rc-"):
                info: dict[str, Any] = {"run_id": d.name, "path": str(d)}
                # Try reading checkpoint
                ckpt = d / "checkpoint.json"
                if ckpt.exists():
                    try:
                        with ckpt.open() as f:
                            info["checkpoint"] = json.load(f)
                    except Exception:
                        pass
                runs.append(info)
    return {"runs": runs[:50]}  # limit to 50 most recent


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    """Get details for a specific run."""
    run_dir = _validated_run_dir(run_id)
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    info: dict[str, Any] = {"run_id": run_id, "path": str(run_dir)}

    ckpt = run_dir / "checkpoint.json"
    if ckpt.exists():
        try:
            with ckpt.open() as f:
                info["checkpoint"] = json.load(f)
        except Exception:
            pass

    # List stage directories
    stage_dirs = sorted(
        [d.name for d in run_dir.iterdir() if d.is_dir() and d.name.startswith("stage-")]
    )
    info["stages_completed"] = stage_dirs

    # Check for paper
    for pattern in ["paper.md", "paper.tex", "paper.pdf"]:
        found = list(run_dir.rglob(pattern))
        if found:
            info[f"has_{pattern.split('.')[1]}"] = True

    return info


@router.get("/runs/{run_id}/metrics")
async def get_run_metrics(run_id: str) -> dict[str, Any]:
    """Get experiment metrics for a run."""
    run_dir = _validated_run_dir(run_id)
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    metrics: dict[str, Any] = {}
    results_file = run_dir / "results.json"
    if results_file.exists():
        try:
            with results_file.open() as f:
                metrics = json.load(f)
        except Exception:
            pass

    return {"run_id": run_id, "metrics": metrics}


# ---------------------------------------------------------------- HITL gates (web)


class HITLRespondRequest(BaseModel):
    """门控应答：批准/拒绝/注入指导等。"""

    action: str  # approve | reject | edit | skip | collaborate | inject | rollback | abort
    message: str = ""
    guidance: str = ""


@router.get("/runs/{run_id}/hitl/waiting")
async def hitl_waiting(run_id: str) -> dict[str, Any]:
    """该 run 是否正在门控处等待人工输入（读 hitl/waiting.json）。"""
    run_dir = _validated_run_dir(run_id)
    p = run_dir / "hitl" / "waiting.json"
    if not p.is_file():
        return {"waiting": False, "run_id": run_id}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"waiting": False, "run_id": run_id}
    data["waiting"] = True
    data["run_id"] = run_id
    return data


@router.post("/runs/{run_id}/hitl/respond")
async def hitl_respond(run_id: str, req: HITLRespondRequest) -> dict[str, Any]:
    """向等待中的门控写入人工决策（hitl/response.json，pipeline 轮询自取）。"""
    from researchclaw.hitl.file_wait import write_response
    from researchclaw.hitl.intervention import HumanAction, HumanInput

    run_dir = _validated_run_dir(run_id)
    if not (run_dir / "hitl" / "waiting.json").is_file():
        raise HTTPException(status_code=409, detail="该 run 当前没有在等待人工输入")
    try:
        action = HumanAction(req.action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"非法 action: {req.action}") from exc

    write_response(
        run_dir / "hitl",
        HumanInput(action=action, message=req.message, guidance=req.guidance),
    )
    return {"ok": True, "run_id": run_id, "action": req.action}
