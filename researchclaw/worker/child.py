"""Child process that executes one job: ``python -m researchclaw.worker.child <run_dir>``.

The supervisor starts one child per job.  The child records its final state
in job.json; if it dies before doing so (OOM, kill, container restart) the
supervisor sees a running job without a live process and resumes it.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from researchclaw.worker import jobs


def _watch_cancel(run_dir: Path, cancel_event: threading.Event) -> None:
    while not cancel_event.wait(2.0):
        if jobs.cancel_requested(run_dir):
            cancel_event.set()


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_dir = Path(argv[0])
    job = jobs.read_job(run_dir)
    if job is None:
        print(f"job.json missing in {run_dir}", file=sys.stderr)
        return 2
    from researchclaw.worker.execution import execute_job, load_job_config

    cancel_event = threading.Event()
    threading.Thread(target=_watch_cancel, args=(run_dir, cancel_event), daemon=True).start()

    def on_start(from_stage: int) -> None:
        # Brief materialization happens once; later resumes start from the checkpoint.
        jobs.update_job(run_dir, materialized=True, from_stage=from_stage)

    try:
        outcome = execute_job(run_dir, job, load_job_config(job), cancel_event, on_start=on_start)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("Pipeline job failed")
        outcome = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    jobs.update_job(run_dir, state=outcome.pop("status"), finished_at=jobs.now(), pid=None, **outcome)
    jobs.clear_cancel(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
