"""Read current research evidence for the project workspace, without synthesizing claims."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def workspace_evidence(root: Path) -> dict[str, Any]:
    errors: list[dict[str, str]] = []

    def read(relative: str, *, yaml_file: bool = False) -> Any:
        path = root / relative
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            return yaml.safe_load(text) if yaml_file else json.loads(text)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append({"path": relative, "error": type(exc).__name__})
            return None

    brief = read("research_brief.json") or {}
    plan = read("stage-09/exp_plan.yaml", yaml_file=True) or {}
    if not isinstance(brief, dict):
        brief = {}
    if not isinstance(plan, dict):
        plan = {}
    method = read("stage-10/method_acceptance.json")
    receipt = read("stage-10/execution_acceptance.json")
    accepted = False
    acceptance_error = ""
    if isinstance(receipt, dict) and receipt.get("ok") is True:
        from researchclaw.pipeline.execution_contract import verify_accepted_code
        try:
            verify_accepted_code(root / "stage-10/experiment", root)
            accepted = True
        except (ValueError, OSError, KeyError, TypeError) as exc:
            acceptance_error = str(exc)

    runs = []
    # Explicitly exclude recovery_history and stage-12_vN. Old or agent-only
    # trial outputs are not formal scientific results of this attempt.
    for path in sorted((root / "stage-12/runs").glob("run-*.json")):
        row = read(str(path.relative_to(root)))
        if isinstance(row, dict):
            runs.append({"id": path.stem, "path": str(path.relative_to(root)),
                **{key: row[key] for key in ("status", "metrics", "returncode", "timed_out", "elapsed_sec") if key in row}})
    resources = read("stage-09/execution_resources.json")
    direction = read("metric_direction.json")
    target = plan.get("execution_target")
    target = dict(target) if isinstance(target, dict) else {}
    metrics = brief.get("metrics")
    primary = metrics[0] if isinstance(metrics, list) and metrics and isinstance(metrics[0], dict) else {}
    key = primary.get("metric_key") or target.get("metric_key")
    if key:
        target["metric_key"] = key
    if primary.get("direction"):
        target["direction"] = primary["direction"]
    elif isinstance(direction, dict) and direction.get("metric_key") == key:
        target["direction"] = direction.get("direction")
    return {
        "brief": {key: brief[key] for key in ("scientific_question", "hypotheses", "experiment_scope", "metrics", "risks") if key in brief},
        "plan": {key: plan[key] for key in ("baselines", "ablations", "method_requirements", "execution", "metrics") if key in plan},
        "target": target, "resources": resources,
        "acceptance": {"ok": accepted, "error": acceptance_error,
            "method_review": method if isinstance(method, dict) else None},
        "runs": runs, "read_errors": errors,
        "sources": {"brief": "research_brief.json", "plan": "stage-09/exp_plan.yaml"},
    }
