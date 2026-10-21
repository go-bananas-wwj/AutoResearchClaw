"""Run-scoped conservative budget reservations, explicitly distinct from invoices."""
from __future__ import annotations

import contextvars
import functools
import json
import threading
from pathlib import Path

_current = contextvars.ContextVar("researchclaw_cost_tracker", default=None)


class CostTracker:
    def __init__(self, run_dir=None):
        self.path = Path(run_dir) / "cost_log.jsonl" if run_dir else None
        self.lock = threading.RLock()

    @property
    def total_cost_usd(self):
        with self.lock:
            if not self.path or not self.path.exists():
                return 0.0
            return sum(float(json.loads(line).get("cost_usd", 0)) for line in self.path.read_text().splitlines() if line.strip())

    def check_budget(self, budget):
        return budget <= 0 or self.total_cost_usd < budget

    def record(self, response, messages):
        if not self.path:
            return
        inputs = response.prompt_tokens or len(json.dumps(messages, ensure_ascii=False).encode())
        outputs = response.completion_tokens or len(response.content.encode())
        record = {"model": response.model, "prompt_tokens": inputs, "completion_tokens": outputs,
                  "cost_usd": (inputs * 5 + outputs * 25) / 1_000_000,
                  "cost_basis": "conservative_reservation_5_input_25_output_usd_per_million_not_invoice"}
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as stream:
                stream.write(json.dumps(record) + "\n")


def get_global_tracker():
    # Compatibility with the existing runner/CostGuard, without cross-run global spend.
    return _current.get() or CostTracker()


def tracked_run(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        token = _current.set(CostTracker(kwargs.get("run_dir")))
        try:
            return function(*args, **kwargs)
        finally:
            _current.reset(token)
    return wrapped
