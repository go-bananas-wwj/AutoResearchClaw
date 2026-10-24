"""Process-isolated access to the pinned official navigator, without shell parsing."""
from __future__ import annotations
import contextlib
import io
import json
import sys
from pathlib import Path


def main():
    root, run = (Path(p).resolve() for p in sys.argv[1:3])
    sys.path.insert(0, str(root))
    from scripts.quality_flow import navigate
    from scripts.next_step import _retrieval_next_step
    def emit(state, step, kind, **fields):
        return {"state": state, "step": step, "kind": kind, **fields}
    with contextlib.redirect_stdout(io.StringIO()):
        action = navigate(run, root, "", emit,
            lambda d, r, q: _retrieval_next_step(d, r, q, emit=emit))
    print(json.dumps(action, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
