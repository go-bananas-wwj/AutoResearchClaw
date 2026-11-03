"""ExperimentSpec: the single machine-checkable source of truth for an experiment.

Stage 9 writes ``stage-09/experiment_spec.json`` from the accepted plan, the
confirmed brief and the measured resources/data.  Later stages read the
primary metric and its direction, the data inventory and the method checklist
from here instead of re-deriving them from prose (docs/AUTORESEARCH_ARCHITECTURE_CN.md §3.2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from researchclaw.pipeline.execution_contract import digest, write_json

SPEC_FILE = "experiment_spec.json"
SCHEMA_VERSION = 1
_MINIMIZE = {"minimize", "min", "lower", "lower_better", "lower_is_better"}
_MAXIMIZE = {"maximize", "max", "higher", "higher_better", "higher_is_better"}


def normalize_direction(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in _MINIMIZE:
        return "minimize"
    if text in _MAXIMIZE:
        return "maximize"
    return None


def manifest_summary(root: Path) -> dict[str, Any] | None:
    """Summarize a data_manifest.json written by researchclaw.data_acquisition."""
    path = root / "data_manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = data.get("entries", []) if isinstance(data, dict) else []
    kinds: dict[str, int] = {}
    total = 0
    for entry in entries:
        kinds[entry.get("kind", "?")] = kinds.get(entry.get("kind", "?"), 0) + 1
        total += int(entry.get("bytes") or 0)
    return {"root": str(root), "manifest": str(path), "manifest_sha256": digest(entries),
            "entries": len(entries), "by_kind": kinds, "bytes": total,
            "sites": sorted({e["site"] for e in entries if e.get("site")}),
            "references": [{k: e.get(k) for k in ("id", "source_url", "failure_date", "site")}
                           for e in entries if e.get("kind") == "reference"]}


def real_data_roots(data_roots: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    return [s for root in data_roots if (s := manifest_summary(Path(root)))]


def build_spec(plan: dict, brief: dict, resources: dict, *, metric_key: str, metric_direction: str) -> dict:
    direction = normalize_direction(metric_direction)
    requirements = [{"id": r.get("id"), "description": r.get("description"),
                     "acceptance_test": r.get("acceptance_test"), "source_quote": r.get("source_quote"),
                     "passes": False, "evidence": ""}
                    for r in plan.get("method_requirements") or [] if isinstance(r, dict)]
    execution = plan.get("execution") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "topic": brief.get("topic") or plan.get("topic"),
        "scientific_question": brief.get("scientific_question") or plan.get("scientific_question"),
        "primary_metric": {"key": metric_key, "direction": direction},
        "secondary_metrics": [m for m in plan.get("metrics") or [] if isinstance(m, dict)
                              and m.get("metric_key") != metric_key],
        "validation_level": execution.get("validation_level"),
        "datasets": execution.get("datasets") or [],
        "real_data": [d for d in resources.get("real_data", [])],
        "method_requirements": requirements,
        "baselines": plan.get("baselines"),
        "ablations": plan.get("ablations"),
        "entrypoint": plan.get("entrypoint") or "main.py",
        "resources": {k: resources.get(k) for k in ("gpu_count", "gpus", "memory_limit_mb", "time_budget_sec")},
        "deferred_validation": execution.get("deferred_validation") or [],
        "hashes": {"plan": digest(plan), "brief": digest(brief)},
    }


def validate_spec(spec: dict, plan: dict) -> list[str]:
    """Static checks that must pass before any code is written."""
    errors: list[str] = []
    primary = spec.get("primary_metric") or {}
    key, direction = primary.get("key"), primary.get("direction")
    if not key:
        errors.append("ExperimentSpec 缺少主指标")
    if direction not in ("minimize", "maximize"):
        errors.append(f"主指标 {key} 的优化方向未确定（须为 minimize/maximize）")
    # Every place the plan mentions the primary metric must agree on its direction.
    mentions = []
    target = plan.get("execution_target") or {}
    if target.get("metric_key") == key:
        mentions.append(("execution_target.direction", target.get("direction")))
    for metric in plan.get("metrics") or []:
        if isinstance(metric, dict) and metric.get("metric_key") == key:
            for field in ("direction", "scoring_direction", "semantic_direction"):
                if metric.get(field):
                    mentions.append((f"metrics[{key}].{field}", metric[field]))
    for where, value in mentions:
        norm = normalize_direction(value)
        if norm and direction and norm != direction:
            errors.append(f"主指标方向矛盾：{where}={value!r} 与规格方向 {direction} 不一致")
    if not spec.get("method_requirements"):
        errors.append("ExperimentSpec 缺少方法要求清单")
    for req in spec.get("method_requirements", []):
        if not req.get("id") or not req.get("acceptance_test"):
            errors.append(f"方法要求 {req.get('id')} 缺少可执行验收")
    datasets = spec.get("datasets") or []
    kinds = {d.get("kind") for d in datasets if isinstance(d, dict)}
    if spec.get("real_data") and kinds <= {"synthetic"}:
        errors.append("已盘点到真实数据清单（" + ", ".join(r["root"] for r in spec["real_data"])
                      + "），计划却只使用合成数据；须使用真实数据，或说明无法使用的具体原因")
    for data in datasets:
        if isinstance(data, dict) and data.get("kind") == "local":
            path = Path(str(data.get("path", "")))
            if not path.exists():
                errors.append(f"数据路径不存在：{path}")
    return errors


def write_spec(run_dir: Path, spec: dict) -> Path:
    path = run_dir / "stage-09" / SPEC_FILE
    write_json(path, spec)
    return path


def load_spec(run_dir: Path) -> dict | None:
    try:
        return json.loads((run_dir / "stage-09" / SPEC_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def primary_direction(run_dir: Path, fallback: str) -> str:
    spec = load_spec(run_dir)
    direction = ((spec or {}).get("primary_metric") or {}).get("direction")
    return direction if direction in ("minimize", "maximize") else fallback
