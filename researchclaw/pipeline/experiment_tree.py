"""Experiment tree with a git ratchet (docs/AUTORESEARCH_ARCHITECTURE_CN.md §3.3).

Every implementation attempt is a node: a git commit in
``<run_dir>/experiment_tree/workspace`` plus one line in ``nodes.jsonl`` with
the result of each gate (syntax → method_checks → dry_run → formal → review →
data_hash).  ``best.json`` moves only through :meth:`ExperimentTree.promote`,
which requires every gate to pass and the primary metric to improve in the
ExperimentSpec direction — a failed or worse node can never replace the best.
Results are looked up by node id, never by guessing version directories.
"""

from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GATES = ("syntax", "method_checks", "dry_run", "formal", "review", "data_hash")


def primary_value(metrics: dict[str, Any] | None, key: str) -> float | None:
    """Exact primary-metric lookup (``key`` or ``<condition>/key``); no fuzzy aliasing."""
    for name, value in (metrics or {}).items():
        if (name == key or name.endswith("/" + key)) and isinstance(value, (int, float)) \
                and not isinstance(value, bool) and math.isfinite(value):
            return float(value)
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExperimentTree:
    def __init__(self, run_dir: Path, direction: str) -> None:
        if direction not in ("minimize", "maximize"):
            raise ValueError(f"实验树需要明确的主指标方向，收到 {direction!r}")
        self.root = run_dir / "experiment_tree"
        self.workspace = self.root / "workspace"
        self.nodes_path = self.root / "nodes.jsonl"
        self.best_path = self.root / "best.json"
        self.direction = direction

    # ------------------------------------------------------------ git
    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.workspace), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def _ensure_repo(self) -> None:
        if (self.workspace / ".git").is_dir():
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q")
        self._git("config", "user.email", "researchclaw@local")
        self._git("config", "user.name", "ResearchClaw")

    def commit(self, files: dict[str, str], message: str) -> str:
        self._ensure_repo()
        for path in self.workspace.rglob("*"):
            if path.is_file() and ".git" not in path.relative_to(self.workspace).parts:
                path.unlink()
        for name, content in files.items():
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", message)
        return self._git("rev-parse", "HEAD")

    def files_at(self, commit: str) -> dict[str, str]:
        names = self._git("ls-tree", "-r", "--name-only", commit).splitlines()
        return {n: self._git("show", f"{commit}:{n}") + "\n" for n in names}

    # ------------------------------------------------------------ ledger
    def nodes(self) -> list[dict[str, Any]]:
        try:
            lines = self.nodes_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        latest: dict[str, dict[str, Any]] = {}
        for line in lines:
            if line.strip():
                node = json.loads(line)
                latest[node["id"]] = node  # later lines update earlier ones
        return list(latest.values())

    def node(self, node_id: str) -> dict[str, Any] | None:
        return next((n for n in self.nodes() if n["id"] == node_id), None)

    def _append(self, node: dict[str, Any]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        node["updated_at"] = _now()
        with self.nodes_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(node, ensure_ascii=False) + "\n")
        return node

    def add(self, files: dict[str, str], *, action: str, parent: str | None, stage: int,
            note: str = "") -> dict[str, Any]:
        node_id = f"n{len(self.nodes())}"
        commit = self.commit(files, f"{node_id} {action} (stage {stage}) {note}".strip())
        return self._append({"id": node_id, "parent": parent, "action": action, "stage": stage,
                             "commit": commit, "gates": {}, "metric": None, "status": "open",
                             "note": note, "created_at": _now()})

    def gate(self, node_id: str, gate: str, ok: bool, detail: str = "") -> dict[str, Any]:
        if gate not in GATES:
            raise ValueError(f"未知关卡 {gate}")
        node = self.node(node_id)
        if node is None:
            raise KeyError(node_id)
        node["gates"][gate] = {"ok": bool(ok), "detail": str(detail)[:2000], "at": _now()}
        if not ok:
            node["status"] = "failed"
        return self._append(node)

    def set_metric(self, node_id: str, value: float | None) -> dict[str, Any]:
        node = self.node(node_id)
        if node is None:
            raise KeyError(node_id)
        node["metric"] = value if isinstance(value, (int, float)) and math.isfinite(value) else None
        return self._append(node)

    # ------------------------------------------------------------ ratchet
    def best(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.best_path.read_text())
        except (OSError, ValueError):
            return None

    def better(self, value: float, than: float | None) -> bool:
        if than is None:
            return True
        return value < than if self.direction == "minimize" else value > than

    def promote(self, node_id: str, required: tuple[str, ...] = GATES) -> tuple[bool, str]:
        """Make ``node_id`` the best only if every required gate passed and the metric improved."""
        node = self.node(node_id)
        if node is None:
            return False, "节点不存在"
        missing = [g for g in required if not node["gates"].get(g, {}).get("ok")]
        if missing:
            return False, "未通过关卡：" + ", ".join(missing)
        if node["metric"] is None:
            return False, "没有有效主指标"
        current = self.best()
        if current and not self.better(node["metric"], current.get("metric")):
            return False, f"主指标 {node['metric']} 未优于当前最佳 {current.get('metric')}（{self.direction}）"
        best = {"node": node_id, "commit": node["commit"], "metric": node["metric"],
                "direction": self.direction, "promoted_at": _now()}
        tmp = self.best_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(best, ensure_ascii=False, indent=1))
        tmp.replace(self.best_path)
        node["status"] = "best"
        self._append(node)
        return True, "已成为当前最佳"
