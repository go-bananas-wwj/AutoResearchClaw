"""rc-worker supervisor: launch queued jobs, resume interrupted ones.

``python -m researchclaw.worker [--artifacts artifacts] [--config cfg.yaml]``

The supervisor makes no LLM calls.  It only checks process liveness and the
job records, so it is cheap enough to run continuously.  A job whose process
vanished without writing a final state (kill, OOM, container restart) is
resumed from ``checkpoint.json`` with exponential backoff, up to
``server.max_auto_resumes`` times.  A pipeline that *finished* as failed is a
real result and is never retried automatically.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from researchclaw.worker import jobs

logger = logging.getLogger(__name__)

Spawner = Callable[[Path], subprocess.Popen]


def _default_spawn(run_dir: Path) -> subprocess.Popen:
    log = (run_dir / "worker.log").open("a")
    return subprocess.Popen([sys.executable, "-m", "researchclaw.worker.child", str(run_dir)],
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


class Supervisor:
    def __init__(self, artifacts: Path, *, max_concurrent: int = 1, max_auto_resumes: int = 5,
                 spawn: Spawner | None = None, backoff_base_sec: float = 30.0) -> None:
        self.artifacts = artifacts
        self.max_concurrent = max_concurrent
        self.max_auto_resumes = max_auto_resumes
        self.spawn = spawn or _default_spawn
        self.backoff_base_sec = backoff_base_sec
        self.boot_id = uuid.uuid4().hex
        self.children: dict[str, subprocess.Popen] = {}

    # -------------------------------------------------------------- scanning
    def _run_dirs(self) -> list[Path]:
        return sorted(p.parent for p in self.artifacts.glob("rc-*/" + jobs.JOB_FILE))

    def scan_once(self) -> None:
        for run_id, proc in list(self.children.items()):
            code = proc.poll()
            if code is None:
                continue
            del self.children[run_id]
            run_dir = self.artifacts / run_id
            job = jobs.read_job(run_dir)
            jobs.append_event(run_dir, "child_exit", code=code, state=(job or {}).get("state"))
            if job and job.get("state") in ("running", "stopping"):
                self._interrupted(run_dir, job, f"执行进程意外退出（exit {code}）")

        queued: list[tuple[str, Path, dict[str, Any]]] = []
        for run_dir in self._run_dirs():
            job = jobs.read_job(run_dir)
            if not job:
                continue
            state = job.get("state")
            if state in ("running", "stopping") and run_dir.name not in self.children:
                # Started by an earlier supervisor (container restart) or orphaned.
                self._interrupted(run_dir, job, "执行进程已不存在（worker 重启或进程被终止）")
                job = jobs.read_job(run_dir) or {}
                state = job.get("state")
            if state == "queued" and run_dir.name not in self.children:
                queued.append((job.get("queued_at", ""), run_dir, job))

        now = datetime.now(timezone.utc)
        for _, run_dir, job in sorted(queued, key=lambda item: item[0]):
            if len(self.children) >= self.max_concurrent:
                break
            not_before = job.get("not_before")
            if not_before and datetime.fromisoformat(not_before) > now:
                continue
            self._launch(run_dir)

    # -------------------------------------------------------------- actions
    def _launch(self, run_dir: Path) -> None:
        job = jobs.update_job(run_dir, expect_state=("queued",), state="running",
                              boot_id=self.boot_id, started_at=jobs.now())
        if job is None:
            return
        proc = self.spawn(run_dir)
        self.children[run_dir.name] = proc
        jobs.update_job(run_dir, pid=proc.pid, attempts=int(job.get("attempts") or 0) + 1)
        jobs.append_event(run_dir, "launch", pid=proc.pid, from_stage=job.get("from_stage"))
        logger.info("launched %s (pid %s)", run_dir.name, proc.pid)

    def _interrupted(self, run_dir: Path, job: dict[str, Any], reason: str) -> None:
        if job.get("state") == "stopping" or jobs.cancel_requested(run_dir):
            jobs.update_job(run_dir, state="stopped", pid=None, reason="用户已停止")
            jobs.clear_cancel(run_dir)
            return
        resumes = int(job.get("auto_resumes") or 0)
        next_stage = jobs.next_stage_from_checkpoint(run_dir) or job.get("from_stage")
        if resumes >= self.max_auto_resumes or not next_stage:
            jobs.update_job(run_dir, state="interrupted", pid=None,
                            reason=f"{reason}；自动续跑已达上限 {self.max_auto_resumes} 次，请检查后手动恢复")
            jobs.append_event(run_dir, "give_up", reason=reason, auto_resumes=resumes)
            return
        from researchclaw.pipeline.stages import Stage

        next_stage = min(int(next_stage), int(max(Stage)))
        archived = jobs.archive_resume_outputs(run_dir, next_stage)
        with (run_dir / "resume_history.jsonl").open("a") as history:
            history.write(json.dumps({"at": jobs.now(), "from_stage": Stage(next_stage).name,
                                      "archived": str(archived.relative_to(run_dir)), "auto": True,
                                      "reason": reason}, ensure_ascii=False) + "\n")
        delay = min(600.0, self.backoff_base_sec * (2 ** resumes))
        not_before = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        jobs.update_job(run_dir, state="queued", pid=None, from_stage=next_stage, materialized=True,
                        auto_resumes=resumes + 1, queued_at=jobs.now(), not_before=not_before,
                        reason=reason)
        jobs.append_event(run_dir, "auto_resume", reason=reason, from_stage=next_stage,
                          auto_resumes=resumes + 1, delay_sec=delay)
        try:
            from researchclaw.pipeline.activity import emit
            emit("run", "执行中断，已安排自动续跑",
                 f"{reason}。约 {int(delay)} 秒后从阶段 {next_stage} 继续，原产物已保留在历史记录中。",
                 run_dir=run_dir, status="running", phase="pipeline")
        except Exception:  # noqa: BLE001
            logger.debug("activity emit failed", exc_info=True)

    def serve_forever(self, interval: float = 5.0) -> None:
        logger.info("rc-worker supervisor started (boot %s, artifacts=%s)", self.boot_id, self.artifacts)
        while True:
            try:
                self.scan_once()
            except Exception:  # noqa: BLE001
                logger.exception("supervisor scan failed")
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m researchclaw.worker")
    parser.add_argument("--artifacts", default="artifacts")
    parser.add_argument("--config", default="")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    max_concurrent, max_resumes = 1, 5
    if args.config:
        from researchclaw.config import RCConfig

        server = RCConfig.load(args.config, check_paths=False).server
        max_concurrent, max_resumes = server.max_concurrent_runs, server.max_auto_resumes
    Supervisor(Path(args.artifacts), max_concurrent=max_concurrent,
               max_auto_resumes=max_resumes).serve_forever(args.interval)
    return 0
