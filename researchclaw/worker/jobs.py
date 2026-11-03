"""Durable job records shared by the Web process and the rc-worker supervisor.

A job lives in ``<run_dir>/job.json``.  The Web process only creates or
re-queues jobs and reads their state; the supervisor owns execution.  All
writes go through a temp file + rename under an ``fcntl`` lock, so the two
processes (and a crashing child) never leave a half-written record.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

JOB_FILE = "job.json"
CONTROL_DIR = ".control"
ACTIVE_STATES = ("queued", "running", "stopping")
FINAL_STATES = ("completed", "failed", "blocked", "stopped", "interrupted")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _locked(run_dir: Path) -> Iterator[None]:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / ".job.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write(run_dir: Path, job: dict[str, Any]) -> None:
    tmp = run_dir / f".{JOB_FILE}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, run_dir / JOB_FILE)


def read_job(run_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((run_dir / JOB_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_job(run_dir: Path, job: dict[str, Any]) -> dict[str, Any]:
    with _locked(run_dir):
        job = {**job, "updated_at": now()}
        _write(run_dir, job)
    return job


def update_job(run_dir: Path, *, expect_state: tuple[str, ...] | None = None,
               **fields: Any) -> dict[str, Any] | None:
    """Merge ``fields`` into the job; return None when ``expect_state`` does not match."""
    with _locked(run_dir):
        job = read_job(run_dir)
        if job is None or (expect_state is not None and job.get("state") not in expect_state):
            return None
        job.update(fields)
        job["updated_at"] = now()
        _write(run_dir, job)
        return job


def append_event(run_dir: Path, kind: str, **fields: Any) -> None:
    """Supervisor history (one JSON line per launch/resume/exit) for audit."""
    with (run_dir / "worker_events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": now(), "kind": kind, **fields}, ensure_ascii=False) + "\n")


def request_cancel(run_dir: Path) -> None:
    control = run_dir / CONTROL_DIR
    control.mkdir(exist_ok=True)
    (control / "cancel").write_text(now())


def cancel_requested(run_dir: Path) -> bool:
    return (run_dir / CONTROL_DIR / "cancel").exists()


def clear_cancel(run_dir: Path) -> None:
    (run_dir / CONTROL_DIR / "cancel").unlink(missing_ok=True)


def next_stage_from_checkpoint(run_dir: Path) -> int | None:
    try:
        data = json.loads((run_dir / "checkpoint.json").read_text())
        return int(data.get("last_completed_stage") or 0) + 1
    except (OSError, ValueError, TypeError):
        return None


def archive_resume_outputs(run_dir: Path, from_stage: int) -> Path:
    """Preserve previous evidence while isolating the next execution attempt."""
    from researchclaw.pipeline.stages import Stage

    history = run_dir / "recovery_history" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    history.mkdir(parents=True)
    for path in list(run_dir.iterdir()):
        match = re.match(r"^stage-(\d{2})(?:$|_)", path.name)
        if match and int(match[1]) >= int(from_stage):
            shutil.move(str(path), history / path.name)
    for name in ("checkpoint.json", "pipeline_summary.json", "heartbeat.json", "experiment_repair_result.json"):
        path = run_dir / name
        if path.is_file():
            shutil.copy2(path, history / name)
            if name != "checkpoint.json":
                path.unlink()
    for name in ("deliverables", "experiment_summary_best.json", "experiment_diagnosis.json", "repair_prompt.txt"):
        path = run_dir / name
        if path.exists():
            shutil.move(str(path), history / name)
    plan = run_dir / "stage-09" / "exp_plan.yaml"
    if plan.is_file():
        shutil.copy2(plan, history / "exp_plan_before_resume.yaml")
    if (run_dir / "hitl").is_dir():
        shutil.move(str(run_dir / "hitl"), history / "hitl")
    previous = max(0, int(from_stage) - 1)
    (run_dir / "checkpoint.json").write_text(json.dumps({"run_id": run_dir.name,
        "last_completed_stage": previous, "last_completed_name": Stage(previous).name if previous else "",
        "status": "running", "timestamp": now()}))
    return history
