"""Durable, run-scoped public activity. Never expose private reasoning blocks."""
from __future__ import annotations

import contextvars
from datetime import datetime, timezone
import functools
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from typing import Any

_current = contextvars.ContextVar("researchclaw_activity_run", default=None)
_lock = threading.Lock()
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean(value: Any, limit: int = 6000) -> str:
    text = _ANSI.sub("", str(value or ""))
    # Return only public answers, never an internal reasoning transcript.
    from researchclaw.utils.thinking_tags import strip_thinking_tags
    text = strip_thinking_tags(text)
    for name, secret in os.environ.items():
        if len(secret) >= 8 and any(k in name.lower() for k in ("key", "token", "password", "secret")):
            text = text.replace(secret, "[已隐藏凭据]")
    text = re.sub(r"(?:gh[pousr]_[A-Za-z0-9]{15,}|github_pat_[A-Za-z0-9_]+|\bsk-[A-Za-z0-9_-]{15,})", "[已隐藏凭据]", text)
    text = re.sub(r"(?i)(authorization[\"':=\s]+(?:bearer|basic)\s+)\S+", r"\1[已隐藏凭据]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)[\"']?\s*[:=]\s*)[\"']?[^\s,\"'}]+", r"\1[已隐藏凭据]", text)
    return text[:limit] + ("\n…内容较长，已收起" if len(text) > limit else "")


def run_from_path(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        if re.fullmatch(r"rc-\d{8}-\d{6}-[a-f0-9]+", parent.name):
            return parent
    return _current.get()


def emit(kind: str, title: str, detail: Any = "", *, run_dir: Path | None = None,
         status: str = "", phase: str = "", model: str = "", **extra: Any) -> None:
    root = run_dir or _current.get()
    if root is None:
        return
    row = {"timestamp": datetime.now(timezone.utc).isoformat(), "kind": kind,
           "title": clean(title, 500), "detail": clean(detail), "status": status,
           "phase": phase, "model": clean(model, 100), **extra}
    try:
        with _lock:
            root = Path(root)
            root.mkdir(parents=True, exist_ok=True)
            with (root / "agent_activity.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        # Progress reporting cannot turn a valid experiment into a failure.
        pass


def observed_run(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        token = _current.set(Path(kwargs["run_dir"]))
        emit("run", "科研任务开始执行", phase="pipeline", status="running",
             from_stage=int(kwargs.get("from_stage", 1)))
        try:
            result = function(*args, **kwargs)
            last = result[-1] if result else None
            emit("run", "本轮执行结束", getattr(last, "error", ""), phase="pipeline",
                 status=getattr(getattr(last, "status", None), "value", "stopped"))
            return result
        except Exception as exc:
            emit("error", "科研任务执行异常", str(exc), phase="pipeline", status="failed")
            raise
        finally:
            _current.reset(token)
    return wrapped


def opencode_event(event: dict, workspace: Path, model: str) -> None:
    root = run_from_path(workspace)
    kind = event.get("type")
    part = event.get("part") or {}
    if not isinstance(part, dict):
        return
    if kind == "tool_use":
        state = part.get("state") or {}
        tool = str(part.get("tool", "tool"))
        inputs = state.get("input") or {}
        labels = {"bash": "执行命令", "read": "读取文件", "write": "写入文件",
                  "edit": "修改文件", "apply_patch": "修改代码", "glob": "查找文件",
                  "grep": "搜索代码", "webfetch": "获取网页", "todowrite": "更新工作计划"}
        command = ""
        if isinstance(inputs, dict):
            command = str(inputs.get("command") or inputs.get("filePath") or inputs.get("path") or inputs.get("pattern") or "")
        detail = state.get("output") or state.get("error") or ""
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
        emit("tool", labels.get(tool, "调用工具") + (" · " + clean(command, 220) if command else ""),
             detail, run_dir=root, status=str(state.get("status", "running")), phase="implementation",
             model=model, tool=clean(tool, 80), tool_call_id=clean(part.get("callID", ""), 120))
    elif kind == "text" and part.get("text"):
        emit("message", "智能体进展", part["text"], run_dir=root, phase="implementation", model=model)
    elif kind == "step_start":
        emit("step", "智能体正在处理下一步", run_dir=root, status="running", phase="implementation", model=model)
    elif kind == "error":
        emit("error", "智能体遇到错误", json.dumps(event.get("error", {}), ensure_ascii=False),
             run_dir=root, status="failed", phase="implementation", model=model)


def consume_events(path: Path, offset: int, workspace: Path, model: str) -> int:
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            for _ in range(100):
                start = stream.tell()
                line = stream.readline(2 * 1024 * 1024)
                if not line or not line.endswith(b"\n"):
                    return start
                offset = stream.tell()
                try:
                    event = json.loads(_ANSI.sub("", line.decode("utf-8", errors="replace")).strip())
                    if isinstance(event, dict):
                        opencode_event(event, workspace, model)
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass
    return offset


def read_activity(root: Path, after: int = 0, limit: int = 100) -> dict:
    path = root / "agent_activity.jsonl"
    if not path.is_file():
        return {"events": [], "cursor": 0, "more": False, "reset": after > 0}
    events = []
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        reset = after > size
        offset = after if 0 < after <= size else max(0, size - 256000)
        stream.seek(offset)
        if not after and offset > 0:
            stream.readline()
        cursor = stream.tell()
        for _ in range(min(max(limit, 1), 100)):
            start = stream.tell()
            line = stream.readline(100000)
            if not line or not line.endswith(b"\n"):
                stream.seek(start)
                break
            cursor = stream.tell()
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    continue
                row["title"] = clean(row.get("title"), 500)
                row["detail"] = clean(row.get("detail"))
                row["id"] = cursor
                events.append(row)
            except ValueError:
                continue
        return {"events": events, "cursor": cursor, "more": cursor < size, "reset": reset}


def run_with_output(command: list[str], staging: Path, root: Path, timeout: int, entry: str):
    """Keep the full receipt and publish small batches while Docker executes."""
    import time
    stdout_path, stderr_path = staging / ".rc_stdout.log", staging / ".rc_stderr.log"
    offsets = {stdout_path: 0, stderr_path: 0}

    def forward():
        for path in offsets:
            with path.open("rb") as stream:
                stream.seek(offsets[path])
                chunk = stream.read(32000)
                offsets[path] = stream.tell()
            if chunk:
                emit("output", "实验输出 · " + entry, chunk.decode("utf-8", errors="replace"),
                     run_dir=root, phase="experiment", status="running")

    start = time.monotonic()
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
        try:
            while True:
                forward()
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    forward()
                    raise subprocess.TimeoutExpired(command, timeout, output=stdout_path.read_text(errors="replace"),
                                                    stderr=stderr_path.read_text(errors="replace"))
                try:
                    code = process.wait(timeout=min(1, remaining))
                    forward()
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return subprocess.CompletedProcess(command, code, stdout_path.read_text(errors="replace"), stderr_path.read_text(errors="replace"))
