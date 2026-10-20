#!/usr/bin/env python3
"""IdeaSpark -> AutoResearchClaw bridge.

Takes IdeaSpark Phase 0/1 artifacts (+ an idea card), builds a ResearchBrief,
materialises the standard stage-01/02/07/08/09 artifacts into a run dir, and
prints the suggested --from-stage. Provenance is copied under run_dir/ideaspark/.

Usage (inside the control container):
  python3 scripts/ideaspark_bridge.py \
      --phase0 /path/to/ideaspark_run/phase0 \
      --phase1 /path/to/ideaspark_run/phase1 \
      --idea-card /path/to/idea_card.json \
      --out artifacts/rc-YYYYMMDD-HHMMSS-xxxx \
      --config config.rc-remote-sensing.yaml
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from researchclaw.config import RCConfig
from researchclaw.ideation.brief import ResearchBrief, materialize


def _hardware_profile() -> dict:
    """Docker-sibling deployment: the control container cannot see the GPU,
    stage-01 trusts the config, so stamp the host profile explicitly."""
    return {
        "has_gpu": True,
        "gpu_type": "cuda",
        "gpu_name": "NVIDIA Tesla V100-PCIE-32GB",
        "vram_mb": 32768,
        "tier": "high",
        "warning": "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase0", required=True)
    ap.add_argument("--phase1", default="")
    ap.add_argument("--idea-card", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default="config.rc-remote-sensing.yaml")
    ap.add_argument("--topic", default="")
    args = ap.parse_args()

    card = json.loads(Path(args.idea_card).read_text(encoding="utf-8"))
    phase0 = Path(args.phase0)
    phase1 = Path(args.phase1) if args.phase1 else None

    brief = ResearchBrief(
        topic=card.get("title") or args.topic,
        scientific_question=card.get("scientific_question", ""),
        survey_summary=card.get("motivation", ""),
        hypotheses=list(card.get("hypotheses") or []),
        experiment_scope=card.get("method", ""),
        datasets=list(card.get("datasets") or []),
        metrics=[
            {"metric_key": str(m), "direction": "increase"}
            for m in (card.get("metrics") or [])
        ],
        target_conference=card.get("target_conference", ""),
    )

    run_dir = Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)
    config = RCConfig.load(args.config, check_paths=False)
    stage = materialize(run_dir, brief, config)

    # Provenance: keep the exact ideation evidence the brief came from.
    prov = run_dir / "ideaspark"
    prov.mkdir(parents=True, exist_ok=True)
    if phase0.is_dir():
        shutil.copytree(phase0, prov / "phase0", dirs_exist_ok=True)
    if phase1 and phase1.is_dir():
        shutil.copytree(phase1, prov / "phase1", dirs_exist_ok=True)
    plan_overrides = card.get("plan") or {}
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

    (run_dir / "idea_card.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # stage-01 hardware profile for docker-sibling deployments
    s1 = run_dir / "stage-01"
    s1.mkdir(parents=True, exist_ok=True)
    (s1 / "hardware_profile.json").write_text(
        json.dumps(_hardware_profile(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps({
        "run_dir": str(run_dir),
        "from_stage": stage.name,
        "from_stage_num": int(stage),
        "topic": brief.topic,
        "n_hypotheses": len(brief.hypotheses),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
