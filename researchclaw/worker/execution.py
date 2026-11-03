"""Run one pipeline job; shared by in-process (Web) and rc-worker execution."""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Config fields the Web request may change; the worker re-applies them on top
# of the same config file, so both executors see an identical RCConfig.
OVERRIDE_FIELDS = (
    ("research", "topic"),
    ("experiment", "metric_key"),
    ("experiment", "metric_direction"),
    ("experiment", "time_budget_sec"),
    ("export", "target_conference"),
)


def config_overrides(config: Any) -> dict[str, Any]:
    return {f"{section}.{key}": getattr(getattr(config, section), key) for section, key in OVERRIDE_FIELDS}


def apply_overrides(config: Any, overrides: dict[str, Any]) -> Any:
    for dotted, value in overrides.items():
        section, key = dotted.split(".", 1)
        if (section, key) not in OVERRIDE_FIELDS:
            raise ValueError(f"不支持的配置覆盖: {dotted}")
        config = dataclasses.replace(
            config, **{section: dataclasses.replace(getattr(config, section), **{key: value})})
    return config


def load_job_config(job: dict[str, Any]) -> Any:
    from researchclaw.config import RCConfig

    return apply_overrides(RCConfig.load(job["config_path"], check_paths=False), job.get("overrides") or {})


def _materialize_brief(run_dir: Path, job: dict[str, Any], config: Any) -> Any:
    from researchclaw.ideation.brief import ResearchBrief, materialize

    brief = ResearchBrief.from_dict(job["brief"])
    from_stage = materialize(run_dir, brief, config)
    if job.get("idempotency_key"):
        evidence = Path(brief.source_ideation_run)
        shutil.copytree(evidence, run_dir / "ideation_evidence", dirs_exist_ok=True)
        shutil.copyfile(evidence / "cost_log.jsonl", run_dir / "cost_log.jsonl")
        from researchclaw.hardware import HardwareProfile, detect_hardware
        hardware = detect_hardware()
        if not hardware.has_gpu and config.experiment.mode == "docker" and config.experiment.docker.gpu_enabled:
            hardware = HardwareProfile(has_gpu=True, gpu_type="cuda",
                gpu_name="GPU available in docker sibling sandbox", vram_mb=None, tier="limited", warning="")
        (run_dir / "stage-01/hardware_profile.json").write_text(json.dumps(hardware.to_dict()))
    return from_stage


def execute_job(run_dir: Path, job: dict[str, Any], config: Any, cancel_event: threading.Event,
                on_start: Callable[[int], None] | None = None) -> dict[str, Any]:
    """Execute the job and return {status, reason, stages_done, stages_failed}.

    ``job["from_stage"]`` wins over the brief's suggested stage; a brief is
    materialized only once (``job["materialized"]``) so a resumed job never
    rewrites the confirmed research brief.
    """
    from researchclaw.adapters import AdapterBundle
    from researchclaw.pipeline.runner import execute_pipeline
    from researchclaw.pipeline.stages import Stage

    kb_root = Path(config.knowledge_base.root) if config.knowledge_base.root else None
    if kb_root:
        kb_root.mkdir(parents=True, exist_ok=True)

    from_stage = Stage.TOPIC_INIT
    if job.get("brief") and not job.get("materialized"):
        from_stage = _materialize_brief(run_dir, job, config)
    if job.get("from_stage"):
        from_stage = Stage(int(job["from_stage"]))
    if on_start:
        on_start(int(from_stage))

    adapters = AdapterBundle()
    if not job.get("auto_approve"):
        # Web 启动 = co-pilot：挂 HITLSession，门控处写 hitl/waiting.json
        # 并轮询 hitl/response.json（无 input_callback → 文件 IPC），跨进程可用。
        hitl_cfg = getattr(config, "hitl", None)
        try:
            if hitl_cfg is None or not getattr(hitl_cfg, "enabled", False):
                from researchclaw.hitl.presets import copilot_preset

                hitl_cfg = copilot_preset()
            from researchclaw.hitl.session import HITLSession

            adapters.hitl = HITLSession(run_id=run_dir.name, config=hitl_cfg, run_dir=run_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("HITL session setup failed: %s", exc)

    results = execute_pipeline(
        run_dir=run_dir, run_id=run_dir.name, config=config, adapters=adapters,
        from_stage=from_stage, auto_approve_gates=bool(job.get("auto_approve")),
        skip_noncritical=True, kb_root=kb_root, cancel_event=cancel_event,
    )
    done = sum(1 for r in results if r.status.value == "done")
    failed = sum(1 for r in results if r.status.value == "failed")
    # A paused quality/data gate or a partial run is never a completed paper.
    complete = any(r.stage == Stage.CITATION_VERIFY and r.status.value == "done" for r in results)
    status = "stopped" if cancel_event.is_set() else (
        "completed" if complete and failed == 0 else "failed" if failed else "blocked")
    outcome: dict[str, Any] = {"status": status, "stages_done": done, "stages_failed": failed}
    if results and not complete:
        outcome["reason"] = getattr(results[-1], "error", "") or "流水线未到达论文交付阶段，请检查阶段产物。"
    return outcome
