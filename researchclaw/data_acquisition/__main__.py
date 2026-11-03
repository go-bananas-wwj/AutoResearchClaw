"""``python -m researchclaw.data_acquisition insar-landslide [--root DIR] [--plan-only|--once|--verify]``"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from researchclaw.data_acquisition import insar_landslide
from researchclaw.data_acquisition.manifest import Manifest


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m researchclaw.data_acquisition")
    parser.add_argument("dataset", choices=["insar-landslide"])
    parser.add_argument("--root", default="/home/wwj/rc-data/insar_landslide")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(args.root)
    if args.verify:
        print(json.dumps(Manifest(root).verify(), indent=1))
        return 0
    if args.plan_only:
        state = insar_landslide.plan(root)
        print(json.dumps({k: {"scenes": len(t["scenes"]), "pairs": len(t["pairs"])}
                          for k, t in state["tracks"].items()}, indent=1))
        return 0
    print(json.dumps(insar_landslide.run(root, once=args.once), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
