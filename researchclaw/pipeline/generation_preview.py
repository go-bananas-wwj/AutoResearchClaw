"""Public code previews for a single native generation request, never acceptance evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

from researchclaw.pipeline.activity import clean


class GenerationPreview:
    def __init__(self, stage_dir: Path, filename: str):
        self.path = stage_dir / ".implementation" / "live.json"
        self.filename = filename
        self.body = ""
        self.model = ""
        self.last_write = 0.0
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.write("requesting", force=True)

    def reset(self, model: str) -> None:
        self.body = ""
        self.model = model
        self.write("requesting", force=True)

    def content(self, chunk: str) -> None:
        self.body += chunk
        self.write("generating")

    def write(self, status: str, *, force: bool = False, code: str | None = None, error: str = "") -> None:
        if not force and time.monotonic() - self.last_write < 1:
            return
        self.last_write = time.monotonic()
        text = clean(self.body, 200000)
        match = re.search(r"```(?:python|filename:[^\n]+|[^\n]*\.py)\s*\n", text)
        if code is not None:
            public = clean(code, 200000)
        elif match:
            public = text[match.end():].split("```", 1)[0]
        elif re.match(r"\s*(?:import |from |def |class )", text):
            public = text
        else:
            public = ""
        # Withhold a partial line, which could contain an incomplete markup tag.
        if status in ("requesting", "generating"):
            public = public.rsplit("\n", 1)[0] if "\n" in public else ""
        value = {"target_file": self.filename, "model": self.model, "status": status,
            "started_at": self.started_at, "updated_at": datetime.now(timezone.utc).isoformat(),
            "content": public, "lines": len(public.splitlines()), "error": clean(error),
            "sha256": hashlib.sha256(public.encode()).hexdigest()}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            pass  # A preview failure must not alter the experiment execution.
