"""Pipeline control API routes."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from researchclaw.worker.execution import config_overrides

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
    idempotency_key: str | None = None
    resume_run_id: str | None = None


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
_run_cancel_event: threading.Event | None = None


def _get_app_state() -> dict[str, Any]:
    """Get shared application state (set by app.py)."""
    from researchclaw.server.app import _app_state
    return _app_state


def _archive_resume_outputs(run_dir: Path, from_stage: Any) -> Path:
    """Preserve previous evidence while isolating the next execution attempt."""
    from researchclaw.worker.jobs import archive_resume_outputs

    return archive_resume_outputs(run_dir, int(from_stage))


def _worker_mode(config: Any) -> bool:
    return getattr(config.server, "executor", "inprocess") == "worker"


@router.post("/pipeline/start", response_model=PipelineStartResponse)
async def start_pipeline(req: PipelineStartRequest) -> PipelineStartResponse:
    """Start a new pipeline run."""
    global _active_run, _run_task, _run_cancel_event

    async with _run_lock:
        resume_dir = None
        if req.resume_run_id:
            if req.idempotency_key or req.brief or not req.from_stage:
                raise HTTPException(400, detail="恢复原项目须指定起始阶段，不能同时传入新任务书或选题交接标识")
            resume_dir = _validated_run_dir(req.resume_run_id)
            if not (resume_dir / 'checkpoint.json').is_file():
                raise HTTPException(404, detail="未找到可恢复的项目检查点")
        if req.idempotency_key:
            if not _re.fullmatch(r"is-[a-f0-9]{20}:\d+:r\d+-(?:c\d+|spark-\d+)", req.idempotency_key):
                raise HTTPException(400, detail="无效的调研交接标识")
            for origin in Path("artifacts").glob("rc-*/ideation_origin.json"):
                record = json.loads(origin.read_text())
                if record.get("key") == req.idempotency_key:
                    return PipelineStartResponse(run_id=origin.parent.name,
                        status=(_active_run or {}).get("status", "interrupted"), output_dir=str(origin.parent))
        state = _get_app_state()
        config = state["config"]
        if _worker_mode(config):
            from researchclaw.worker.jobs import ACTIVE_STATES, read_job
            if resume_dir is not None and (read_job(resume_dir) or {}).get("state") in ACTIVE_STATES:
                raise HTTPException(status_code=409, detail="该项目已在排队或运行中")
        elif _active_run and _active_run.get("status") in ("running", "stopping"):
            raise HTTPException(status_code=409, detail="A pipeline is already running")

        import dataclasses

        if resume_dir is not None:
            saved_brief = resume_dir / 'research_brief.json'
            if saved_brief.is_file():
                saved = json.loads(saved_brief.read_text())
                saved_topic = saved.get('topic')
                if saved_topic:
                    config = dataclasses.replace(config, research=dataclasses.replace(config.research, topic=saved_topic))
                metrics = saved.get('metrics') or []
                if metrics and isinstance(metrics[0], dict) and metrics[0].get('metric_key'):
                    metric_overrides = {'metric_key':metrics[0]['metric_key']}
                    if metrics[0].get('direction') in ('minimize', 'maximize'):
                        metric_overrides['metric_direction'] = metrics[0]['direction']
                    else:
                        try:
                            resolved = json.loads((resume_dir / 'metric_direction.json').read_text())
                            if (resolved.get('metric_key') == metrics[0]['metric_key']
                                    and resolved.get('direction') in ('minimize', 'maximize')):
                                metric_overrides['metric_direction'] = resolved['direction']
                        except (OSError, ValueError):
                            pass
                    config = dataclasses.replace(config, experiment=dataclasses.replace(config.experiment, **metric_overrides))
                if saved.get('target_conference'):
                    config = dataclasses.replace(config, export=dataclasses.replace(config.export, target_conference=saved['target_conference']))
            if req.topic:
                raise HTTPException(400, detail="恢复原项目不能更改研究题目")

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
        topic_hash = hashlib.sha256((config.research.topic + (req.idempotency_key or "")).encode()).hexdigest()[:6]
        run_id = req.resume_run_id or f"rc-{ts}-{topic_hash}"
        run_dir = _validated_run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        if (req.config_overrides or {}).get('metric_direction') in ('minimize', 'maximize'):
            (run_dir / 'metric_direction.json').write_text(json.dumps({
                'metric_key': config.experiment.metric_key,
                'direction': config.experiment.metric_direction,
                'source': 'explicit_pipeline_override', 'at': datetime.now(timezone.utc).isoformat()}))
        if resume_dir is not None:
            archived = _archive_resume_outputs(run_dir, forced_from_stage)
            with (run_dir / 'resume_history.jsonl').open('a') as history:
                history.write(json.dumps({'at':datetime.now(timezone.utc).isoformat(), 'from_stage':forced_from_stage.name,
                                          'archived':str(archived.relative_to(run_dir))})+'\n')
            from researchclaw.pipeline.activity import emit
            emit("run", "正在恢复原项目", f"从阶段 {int(forced_from_stage)} 继续，原产物已保留在历史记录中。",
                 run_dir=run_dir, status="running", phase="pipeline")
        if req.idempotency_key:
            (run_dir / "ideation_origin.json").write_text(json.dumps({"key": req.idempotency_key}))
        job = {
            "run_id": run_id,
            "config_path": state.get("config_path", ""),
            "overrides": config_overrides(config),
            "from_stage": int(forced_from_stage) if forced_from_stage is not None else None,
            "auto_approve": req.auto_approve,
            "brief": req.brief,
            "idempotency_key": req.idempotency_key,
            "materialized": False,
        }
        if _worker_mode(config):
            if not job["config_path"]:
                raise HTTPException(500, detail="worker 模式需要服务以 --config 启动")
            from researchclaw.worker import jobs as _jobs
            _jobs.clear_cancel(run_dir)
            _jobs.write_job(run_dir, {**job, "state": "queued", "queued_at": _jobs.now(),
                                      "auto_resumes": 0, "attempts": 0})
            _jobs.append_event(run_dir, "queued", source="web")
            return PipelineStartResponse(run_id=run_id, status="queued", output_dir=str(run_dir))

        cancel_event = threading.Event()
        _run_cancel_event = cancel_event

        _active_run = {
            "run_id": run_id,
            "status": "running",
            "output_dir": str(run_dir),
            "topic": config.research.topic,
        }

    async def _run_in_background() -> None:
        global _active_run
        try:
            from researchclaw.pipeline.stages import Stage
            from researchclaw.worker.execution import execute_job

            def on_start(from_stage: int) -> None:
                if _active_run:
                    _active_run["from_stage"] = Stage(from_stage).name

            loop = asyncio.get_event_loop()
            outcome = await loop.run_in_executor(
                None, lambda: execute_job(run_dir, job, config, cancel_event, on_start=on_start))
            if _active_run:
                _active_run.update(outcome)
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


def _worker_jobs(active_only: bool = True) -> list[dict[str, Any]]:
    from researchclaw.worker.jobs import ACTIVE_STATES, read_job

    found = []
    for path in Path("artifacts").glob("rc-*/job.json"):
        job = read_job(path.parent)
        if job and (not active_only or job.get("state") in ACTIVE_STATES):
            found.append(job)
    return sorted(found, key=lambda job: job.get("updated_at", ""), reverse=True)


def _job_status(job: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": job.get("run_id"), "status": job.get("state"),
            "output_dir": str(Path("artifacts") / str(job.get("run_id"))),
            "reason": job.get("reason", ""), "auto_resumes": job.get("auto_resumes", 0),
            "executor": "worker"}


@router.post("/pipeline/stop")
async def stop_pipeline(run_id: str | None = None) -> dict[str, str]:
    """Stop the currently running pipeline."""
    global _active_run, _run_task

    if _worker_mode(_get_app_state()["config"]):
        from researchclaw.worker.jobs import request_cancel, update_job
        targets = [job for job in _worker_jobs() if not run_id or job.get("run_id") == run_id]
        if not targets:
            raise HTTPException(status_code=404, detail="No pipeline is running")
        for job in targets:
            run_dir = _validated_run_dir(str(job["run_id"]))
            request_cancel(run_dir)
            if not update_job(run_dir, expect_state=("queued",), state="stopped", reason="用户已停止"):
                update_job(run_dir, expect_state=("running",), state="stopping")
        return {"status": "stopping"}

    if not _run_task or not _active_run:
        raise HTTPException(status_code=404, detail="No pipeline is running")

    if _run_cancel_event:
        _run_cancel_event.set()
    _active_run["status"] = "stopping"
    return {"status": "stopping"}


@router.get("/pipeline/status")
async def pipeline_status() -> dict[str, Any]:
    """Get current pipeline run status."""
    if _worker_mode(_get_app_state()["config"]):
        active = _worker_jobs()
        return {**_job_status(active[0]), "active": [_job_status(j) for j in active]} if active else {"status": "idle"}
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
    statuses = {}
    for name in stage_dirs:
        if not _re.fullmatch(r"stage-\d{2}", name):
            continue
        for filename in ("stage_health.json", "decision.json"):
            try:
                status = json.loads((run_dir / name / filename).read_text()).get("status")
                if status:
                    statuses[name] = status
                    break
            except (OSError, ValueError):
                continue
    info["stage_statuses"] = statuses
    info["stages_completed"] = [name for name, status in statuses.items() if status == "done"]
    from researchclaw.server.routes.projects import _project_info
    info["project"] = _project_info(run_dir)

    # Check for paper
    for pattern in ["paper.md", "paper.tex", "paper.pdf"]:
        found = list(run_dir.rglob(pattern))
        if found:
            info[f"has_{pattern.split('.')[1]}"] = True

    return info


@router.get("/runs/{run_id}/activity")
def run_activity(run_id: str, after: int = 0, limit: int = 100) -> dict[str, Any]:
    root = _validated_run_dir(run_id)
    if not root.is_dir():
        raise HTTPException(404, detail="项目不存在")
    if after < 0 or not 1 <= limit <= 100:
        raise HTTPException(400, detail="无效的活动记录游标或条数")
    from researchclaw.pipeline.activity import read_activity
    from researchclaw.server.routes.projects import _project_info
    return {"run_id": run_id, **read_activity(root, after, limit), "project": _project_info(root)}


@router.get("/runs/{run_id}/research-workspace")
def research_workspace(run_id: str) -> dict[str, Any]:
    root = _validated_run_dir(run_id)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="Run not found")
    from researchclaw.server.research_workspace import workspace_evidence
    return {"run_id": run_id, **workspace_evidence(root)}


@router.get("/runs/{run_id}/research-loop")
def research_loop_journal(run_id: str) -> dict[str, Any]:
    """v2 研究日志：立项书、每轮计划/汇报/导师意见/图、results.tsv。"""
    root = _validated_run_dir(run_id)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="Run not found")
    from researchclaw.research_loop.journal import journal
    return {"run_id": run_id, **journal(root)}


@router.get("/runs/{run_id}/research-loop/figure")
def research_loop_figure(run_id: str, path: str) -> Any:
    from fastapi.responses import FileResponse
    from researchclaw.research_loop.journal import figure_path
    try:
        return FileResponse(figure_path(_validated_run_dir(run_id), path), media_type="image/png")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="图不存在") from None


class ResearchNoteRequest(BaseModel):
    text: str


@router.post("/runs/{run_id}/research-loop/note")
def research_loop_note(run_id: str, req: ResearchNoteRequest) -> dict[str, Any]:
    """用户意见：写入当前/下一轮，学生计划与导师审阅都会读取，优先级高于 AI 导师。"""
    root = _validated_run_dir(run_id)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="Run not found")
    from researchclaw.research_loop.journal import add_user_note
    try:
        return {"run_id": run_id, "path": add_user_note(root, req.text)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/runs/{run_id}/implementation")
def implementation_status(run_id: str) -> dict[str, Any]:
    root = _validated_run_dir(run_id)
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="Run not found")
    from researchclaw.server.implementation import implementation_evidence
    from researchclaw.server.routes.projects import _project_info
    return {"run_id": run_id, **implementation_evidence(root, _project_info(root))}


@router.get("/runs/{run_id}/implementation/file")
def implementation_artifact(run_id: str, path: str, sha256: str) -> dict[str, Any]:
    from researchclaw.server.implementation import implementation_file
    try:
        return implementation_file(_validated_run_dir(run_id), path, sha256)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="本轮没有该实施产物") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


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
    edited_files: dict[str, str] = {}  # edit 动作：相对路径 -> 新内容
    rollback_to_stage: int | None = None  # rollback 动作：目标阶段编号


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
        HumanInput(
            action=action,
            message=req.message,
            guidance=req.guidance,
            edited_files=req.edited_files,
            rollback_to_stage=req.rollback_to_stage,
        ),
    )
    return {"ok": True, "run_id": run_id, "action": req.action}


# ---------------------------------------------------------------- HITL console


_HITL_FILE_MAX_CHARS = 200_000


@router.get("/hitl/waiting")
async def hitl_waiting_all() -> dict[str, Any]:
    """列出所有正在门控处等待人工输入的 run（控制台核心数据源）。

    注意路径是 /api/hitl/waiting 而不是 /api/runs/waiting——后者会被
    先注册的 /runs/{run_id} 路由吃掉。
    """
    artifacts = Path("artifacts")
    waiting: list[dict[str, Any]] = []
    if artifacts.exists():
        for d in sorted(artifacts.iterdir(), reverse=True):
            if not (d.is_dir() and d.name.startswith("rc-")):
                continue
            p = d / "hitl" / "waiting.json"
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            data["waiting"] = True
            data["run_id"] = d.name
            waiting.append(data)
    return {"waiting_runs": waiting}


@router.get("/runs/{run_id}/hitl/file")
async def hitl_read_file(run_id: str, path: str) -> dict[str, Any]:
    """读取 run 目录下的门控产物全文（沙箱限制在 run_dir 内，防路径穿越）。"""
    run_dir = _validated_run_dir(run_id)
    target = (run_dir / path).resolve()
    if not target.is_relative_to(run_dir.resolve()):
        raise HTTPException(status_code=400, detail=f"非法路径: {path}")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail=f"非文本文件: {path}") from None
    truncated = len(content) > _HITL_FILE_MAX_CHARS
    return {
        "run_id": run_id,
        "path": path,
        "content": content[:_HITL_FILE_MAX_CHARS],
        "truncated": truncated,
    }


@router.get("/runs/{run_id}/files")
async def list_run_files(run_id: str) -> dict[str, Any]:
    """列举 run 目录下的产物文件（相对路径 + 大小），供前端产物浏览器使用。"""
    run_dir = _validated_run_dir(run_id)
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    files: list[dict[str, Any]] = []
    for p in sorted(run_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(run_dir).as_posix()
        try:
            files.append({"path": rel, "size": p.stat().st_size})
        except OSError:
            continue
        if len(files) >= 3000:
            break
    return {"run_id": run_id, "files": files}


@router.get("/runs/{run_id}/hitl/interventions")
async def hitl_interventions(run_id: str, limit: int = 100) -> dict[str, Any]:
    """干预历史时间线（hitl/interventions.jsonl，含以往的批准/注入记录）。"""
    run_dir = _validated_run_dir(run_id)
    p = run_dir / "hitl" / "interventions.jsonl"
    records: list[dict[str, Any]] = []
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {"run_id": run_id, "interventions": records[-limit:]}


# ---------------------------------------------------------------- Overleaf sync (web)


@router.post("/runs/{run_id}/overleaf/pull")
async def overleaf_pull(run_id: str, language: str = "zh") -> dict[str, Any]:
    """从 Overleaf 拉回用户批注/修改到 run_dir/paper_annotations/<language>/。"""
    from researchclaw.overleaf.run_sync import pull_run_from_overleaf

    run_dir = _validated_run_dir(run_id)
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    config = _get_app_state().get("config")
    try:
        return await asyncio.to_thread(
            pull_run_from_overleaf, run_dir, run_id, config, language
        )
    except Exception as exc:
        logger.exception("overleaf pull failed for %s", run_id)
        raise HTTPException(status_code=500, detail=f"拉取失败: {exc}") from exc


@router.post("/runs/{run_id}/overleaf/push")
async def overleaf_push(run_id: str, language: str | None = None) -> dict[str, Any]:
    """把 run 的论文产物（或改稿后的最新版）推送到 Overleaf。"""
    from researchclaw.overleaf.run_sync import sync_run_to_overleaf

    run_dir = _validated_run_dir(run_id)
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    config = _get_app_state().get("config")
    try:
        return await asyncio.to_thread(
            sync_run_to_overleaf, run_dir, run_id, config, language
        )
    except Exception as exc:
        logger.exception("overleaf push failed for %s", run_id)
        raise HTTPException(status_code=500, detail=f"推送失败: {exc}") from exc
