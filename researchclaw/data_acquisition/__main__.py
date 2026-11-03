"""``python -m researchclaw.data_acquisition verify <manifest_dir>`` — recompute every hash."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from researchclaw.data_acquisition.manifest import Manifest

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "verify":
        raise SystemExit(__doc__)
    print(json.dumps(Manifest(Path(sys.argv[2])).verify(), indent=1))
