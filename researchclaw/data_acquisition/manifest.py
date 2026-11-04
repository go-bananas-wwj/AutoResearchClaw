"""data_manifest.json: one entry per file or reference, with provenance and SHA-256.

The host recomputes hashes in :func:`verify`; an entry the model or a script
merely *claims* exists never counts as data.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from researchclaw.data_acquisition.download import sha256_file

MANIFEST = "data_manifest.json"


class Manifest:
    def __init__(self, root: Path, files_root: Path | None = None) -> None:
        """``root`` holds data_manifest.json; entry paths are relative to ``files_root``
        (default ``root``) so a run manifest can reference a shared store."""
        self.path = root / MANIFEST
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {"entries": []}
        if files_root is not None:
            data["files_root"] = str(files_root)
        self.root = Path(data.get("files_root") or root)
        self.manifest_dir = root
        self.data = data
        self._index = {e["id"]: e for e in data["entries"]}

    def __contains__(self, entry_id: str) -> bool:
        return entry_id in self._index

    def get(self, entry_id: str) -> dict[str, Any] | None:
        return self._index.get(entry_id)

    def add_file(self, entry_id: str, path: Path, *, source_url: str, license: str,
                 kind: str, **meta: Any) -> dict[str, Any]:
        entry = {"id": entry_id, "kind": kind, "path": str(path.relative_to(self.root)),
                 "source_url": source_url, "license": license,
                 "downloaded_at": datetime.now(timezone.utc).isoformat(),
                 "bytes": path.stat().st_size, "sha256": sha256_file(path), **meta}
        return self._put(entry)

    def add_reference(self, entry_id: str, *, source_url: str, license: str, **meta: Any) -> dict[str, Any]:
        entry = {"id": entry_id, "kind": "reference", "path": None, "source_url": source_url,
                 "license": license, "recorded_at": datetime.now(timezone.utc).isoformat(), **meta}
        return self._put(entry)

    def _put(self, entry: dict[str, Any]) -> dict[str, Any]:
        if entry["id"] in self._index:
            self.data["entries"] = [e for e in self.data["entries"] if e["id"] != entry["id"]]
        self.data["entries"].append(entry)
        self._index[entry["id"]] = entry
        return entry

    def remove(self, entry_id: str) -> None:
        if self._index.pop(entry_id, None) is not None:
            self.data["entries"] = [e for e in self.data["entries"] if e["id"] != entry_id]

    def save(self) -> None:
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def verify(self) -> dict[str, Any]:
        """Recompute every file hash; return {ok, files, missing, mismatched}."""
        missing, mismatched, files = [], [], 0
        for entry in self.data["entries"]:
            if not entry.get("path"):
                continue
            files += 1
            path = self.root / entry["path"]
            if not path.is_file():
                missing.append(entry["id"])
            elif path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
                mismatched.append(entry["id"])
        return {"ok": not missing and not mismatched, "files": files,
                "missing": missing, "mismatched": mismatched}
