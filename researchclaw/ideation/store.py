"""Atomic, versioned ideation sessions; formal rc-* runs only exist after approval."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SessionError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class SessionStore:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()

    def directory(self, sid: str) -> Path:
        if not re.fullmatch(r"is-[a-f0-9]{20}", sid):
            raise SessionError("无效调研会话编号", 400)
        return self.root / sid

    def load(self, sid: str) -> dict[str, Any]:
        with self.lock:
            try:
                return json.loads((self.directory(sid) / "session.json").read_text())
            except FileNotFoundError:
                raise SessionError("调研会话不存在", 404) from None

    def save(self, data: dict[str, Any]) -> None:
        with self.lock:
            directory = self.directory(data["session_id"])
            directory.mkdir(parents=True, exist_ok=True)
            data["updated_at"] = now()
            target = directory / "session.json"
            temp = directory / (uuid.uuid4().hex + ".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            temp.replace(target)

    def create(self, direction: str, budget: float) -> dict[str, Any]:
        direction = direction.strip()
        if not 4 <= len(direction) <= 4000:
            raise SessionError("研究方向须为4–4000字符", 400)
        data = {"session_id": "is-" + uuid.uuid4().hex[:20], "direction": direction,
                "revision": 1, "created_at": now(), "status": "pending", "phase": "retrieval",
                "papers": [], "cards": [], "rejected_cards": [], "reviews": [],
                "approved_revision": None, "reading": {}, "synthesis": None,
                "search_log": [], "usage": [], "cost_usd": 0.0,
                "budget_usd": budget, "missing": [], "run_id": None, "feedback": ""}
        self.save(data)
        return data

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            if not self.root.exists():
                return []
            items = [self.load(p.parent.name) for p in self.root.glob("is-*/session.json")]
        return sorted(items, key=lambda d: d["updated_at"], reverse=True)

    def require_revision(self, data: dict[str, Any], revision: int) -> None:
        if data["revision"] != revision:
            raise SessionError("内容已更新，请刷新后审阅当前版本")

    def invalidate(self, data: dict[str, Any], reason: str) -> None:
        if data.get("run_id"):
            raise SessionError("已交接科研的会话不可修改，请新建调研")
        # Immutable history prevents old evidence/cards from being silently promoted.
        directory = self.directory(data["session_id"]) / "history"
        directory.mkdir(exist_ok=True)
        (directory / f"r{data['revision']}-{uuid.uuid4().hex[:8]}.json").write_text(
            json.dumps(copy.deepcopy(data), ensure_ascii=False, indent=2))
        data["reviews"].append({"event": "invalidate", "reason": reason, "at": now(),
                                "revision": data["revision"]})
        for paper in data["papers"]:
            if paper.get("fulltext_status") == "read":
                paper["fulltext_status"] = "downloaded"
        for key in ("progress", "progress_message", "presentation_task", "download_task"):
            data.pop(key, None)
        data["revision"] += 1
        data.update(approved_revision=None, reading={}, synthesis=None, cards=[], rejected_cards=[],
                    status="waiting", phase="literature_review", missing=[])

    def recover(self) -> None:
        for data in self.list():
            if data["status"] in ("running", "stopping", "pending", "handoff"):
                data.update(status="interrupted", missing=["服务重启中断了任务，可从最近完成点继续。"])
                self.save(data)
