"""Current-attempt implementation evidence and version-bound public file previews."""
from __future__ import annotations

import hashlib
import json
import difflib
from pathlib import Path
import re
from typing import Any

from researchclaw.pipeline.activity import clean
from researchclaw.pipeline.execution_contract import confirmed_method, digest
from researchclaw.server.research_workspace import workspace_evidence


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _public_path(root: Path, relative: str) -> Path | None:
    path = root / relative
    if path.resolve() != root.resolve() / relative or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        return None
    return path


def _code_context(root: Path) -> tuple[str, str]:
    candidates = [(int(path.name.removeprefix("experiment_v")), path)
                  for path in (root / "stage-13").glob("experiment_v*")
                  if path.is_dir() and re.fullmatch(r"experiment_v\d+", path.name)]
    if candidates:
        number, path = max(candidates)
        return str(path.relative_to(root)) + "/", f"stage-13/acceptance_v{number}/"
    return "stage-10/experiment/", "stage-10/"


def implementation_artifacts(root: Path) -> list[dict]:
    paths: dict[str, str] = {}
    code_prefix, review_prefix = _code_context(root)
    folders = [(root / code_prefix, "code")]
    if code_prefix.startswith("stage-10/"):
        folders.insert(0, (root / "stage-10/native_draft", "draft"))
    for base, role in folders:
        for path in sorted(base.rglob("*")):
            name = path.relative_to(base)
            if any(part.startswith((".", "__")) for part in name.parts):
                continue
            if path.suffix in (".py", ".yaml", ".yml", ".json", ".md") or path.name == "requirements.txt":
                paths[str(path.relative_to(root))] = role
    for relative, role in (
        ("stage-09/exp_plan.yaml", "plan"),
        ("stage-10/architecture_response.txt", "blueprint"),
        (review_prefix + "method_acceptance.json", "review"),
        (review_prefix + "execution_acceptance.json", "receipt"),
        (code_prefix + ".rc-validation/execution.json", "execution"),
        (code_prefix + ".rc-validation/method_tests.json", "tests"),
        (code_prefix + ".rc-validation/data_receipts.json", "data"),
        ("stage-13/decision.json" if code_prefix.startswith("stage-13/") else "stage-10/decision.json", "failure"),
    ):
        paths[relative] = role
    result = []
    for relative, role in paths.items():
        path = _public_path(root, relative)
        if path is None:
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            content = path.read_bytes()
            result.append({"path": relative, "name": path.name, "role": role,
                "bytes": len(content), "lines": len(content.splitlines()),
                "sha256": hashlib.sha256(content).hexdigest(), "updated_at": path.stat().st_mtime})
        except OSError:
            continue
    return result


def implementation_file(root: Path, relative: str, expected_hash: str) -> dict:
    artifact = next((item for item in implementation_artifacts(root) if item["path"] == relative), None)
    if artifact is None:
        raise FileNotFoundError(relative)
    # Read once and bind the response to these exact bytes, even during a save.
    path = _public_path(root, relative)
    if path is None:
        raise FileNotFoundError(relative)
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if not expected_hash or sha != expected_hash:
        raise ValueError("文件版本已更新，请刷新实施记录后重新打开")
    text = raw.decode("utf-8", errors="replace")
    content = clean(text, 200000)
    original_lines, visible_lines = text.splitlines(), content.splitlines()
    line_numbers = [0] * len(visible_lines)
    for tag, first, last, start, stop in difflib.SequenceMatcher(None, original_lines, visible_lines, autojunk=False).get_opcodes():
        for index in range(start, stop):
            line_numbers[index] = first + index - start + 1 if tag == "equal" else min(first + index - start + 1, max(first + 1, last))
    return {**artifact, "sha256": sha, "content": content,
            "line_numbers": line_numbers, "truncated": len(text) > 200000, "redacted": content != text}


def _activity(root: Path) -> list[dict]:
    path = root / "agent_activity.jsonl"
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 512000)
            stream.seek(offset)
            if offset:
                stream.readline()
            rows = []
            for line in stream:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append({k: clean(value.get(k), 6000 if k == "detail" else 500)
                                     for k in ("timestamp", "kind", "title", "detail", "status", "model")})
                except ValueError:
                    pass
        starts = [i for i, row in enumerate(rows) if row["kind"] == "run" and row["status"] == "running"]
        return rows[starts[-1]:] if starts else rows
    except OSError:
        return []


def implementation_evidence(root: Path, project: dict) -> dict:
    context = workspace_evidence(root)
    code_prefix, review_prefix = _code_context(root)
    artifacts = implementation_artifacts(root)
    by_path = {row["path"]: row for row in artifacts}
    rows = _activity(root)
    target_event = next((row for row in reversed(rows)
                         if row["kind"] == "implementation" and "Generating " in row["title"]), None)
    target = re.search(r"Generating (.*?) \(order=", target_event["title"]) if target_event else None
    last_failure = next((row for row in reversed(rows) if row["status"] == "failed"), None)
    live = _json(root / "stage-10/.implementation/live.json") if code_prefix.startswith("stage-10/") else None
    if not isinstance(live, dict):
        live = None
    if live:
        live = {key: clean(live.get(key), 200000 if key == "content" else 6000)
                for key in ("target_file", "status", "started_at", "updated_at", "model", "content", "error")}
    blueprint = []
    path = _public_path(root, "stage-10/architecture_response.txt")
    if path:
        try:
            value, _ = json.JSONDecoder().raw_decode(path.read_text().lstrip())
            blueprint = [item for item in value.get("files", []) if isinstance(item, dict)]
        except (ValueError, AttributeError, OSError):
            pass
    planned = []
    for item in blueprint:
        name = item.get("name")
        if not isinstance(name, str):
            continue
        match = by_path.get(code_prefix + name) or (by_path.get("stage-10/native_draft/" + name) if code_prefix.startswith("stage-10/") else None)
        planned.append({"name": clean(name, 200), "purpose": clean(item.get("purpose"), 2000),
                        "artifact": match, "status": "saved" if match else "not_saved"})

    audit = _json(root / review_prefix / "method_acceptance.json") or {}
    bound_review = False
    manifest = audit.get("files")
    if isinstance(manifest, dict) and manifest:
        files = {}
        for name, expected in manifest.items():
            path = _public_path(root, code_prefix + name)
            if path is None:
                break
            try:
                content = path.read_text()
            except OSError:
                break
            if hashlib.sha256(content.encode()).hexdigest() != expected:
                break
            files[name] = content
        plan = _json_plan(root)
        # Extra Python modules also invalidate a previous implementation review.
        current_py = {item["path"].removeprefix(code_prefix) for item in artifacts
                      if item["role"] == "code" and item["name"].endswith(".py")}
        bound_review = (len(files) == len(manifest)
            and current_py == {name for name in manifest if name.endswith(".py")}
            and digest(files) == audit.get("code_hash")
            and digest(plan) == audit.get("plan_hash")
            and digest(confirmed_method(root, plan)) == audit.get("source_hash"))
    receipt = _json(root / review_prefix / "execution_acceptance.json") or {}
    observed_py = {item["path"].removeprefix(code_prefix) for item in artifacts
                   if item["role"] == "code" and item["name"].endswith(".py")}
    receipt_files = receipt.get("files") if isinstance(receipt.get("files"), dict) else {}
    plan = _json_plan(root)
    receipt_contents = {}
    for name in receipt_files:
        path = _public_path(root, code_prefix + name)
        if path:
            try:
                receipt_contents[name] = path.read_text()
            except (OSError, UnicodeError):
                pass
    accepted = (receipt.get("ok") is True and bool(receipt_files)
                and digest(receipt_contents) == receipt.get("code_hash")
                and digest(plan) == receipt.get("plan_hash")
                and digest(confirmed_method(root, plan)) == receipt.get("source_hash")
                and all(hashlib.sha256(receipt_contents.get(name, "").encode()).hexdigest() == value
                        for name, value in receipt_files.items())
                and observed_py == {name for name in receipt_files if name.endswith(".py")})
    if accepted and all(audit.get(key) == receipt.get(key) for key in ("code_hash", "plan_hash", "source_hash")):
        bound_review = True

    def evidence(entries: Any) -> list[dict]:
        result = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            name, quote = entry.get("file"), entry.get("quote")
            item = by_path.get(code_prefix + name) if isinstance(name, str) else None
            if not item or not isinstance(quote, str) or not quote:
                continue
            try:
                text = (root / item["path"]).read_text()
            except (OSError, UnicodeError):
                continue
            if hashlib.sha256(text.encode()).hexdigest() != item["sha256"]:
                continue
            offset = text.find(quote)
            if offset < 0:
                continue
            result.append({"path": item["path"], "sha256": item["sha256"],
                "line": text[:offset].count("\n") + 1, "quote": clean(quote, 3000)})
        return result

    reviews = {row.get("id"): row for row in audit.get("requirements", []) if isinstance(row, dict)} if bound_review else {}
    methods = []
    for requirement in context["plan"].get("method_requirements", []):
        if not isinstance(requirement, dict):
            continue
        review = reviews.get(requirement.get("id"), {})
        code, tests = evidence(review.get("code_evidence")), evidence(review.get("test_evidence"))
        implemented = review.get("status") == "implemented" and bool(code) and bool(tests)
        state = "accepted" if accepted and implemented else "reviewed" if implemented and audit.get("ok") is True else "needs_work" if review else "pending"
        methods.append({**{key: clean(requirement.get(key), 6000) for key in ("id", "description", "source_quote", "acceptance_test")},
                        "status": state, "code": code, "tests": tests})
    return {"project": project, "latest": {
        "target_file": code_prefix.rstrip("/") + "（优化候选）" if code_prefix.startswith("stage-13/") else live["target_file"] if live else target.group(1) if target else "",
        "started_at": "" if code_prefix.startswith("stage-13/") else live["started_at"] if live else target_event["timestamp"] if target_event else "",
        "updated_at": rows[-1]["timestamp"] if rows else "",
        "model": next((row["model"] for row in reversed(rows) if row["model"]), ""),
        "error": clean(project.get("reason") or (last_failure or {}).get("detail")),
    }, "live": live, "files": artifacts, "planned_files": planned, "methods": methods,
        "acceptance": {"ok": accepted, "review_bound": bound_review,
            "error": "代码、计划或任务书与验收版本不一致" if receipt and not accepted else ""},
        "events": rows[-8:], "formal_runs": len(context["runs"]),
        "sources": [item for item in artifacts if item["role"] not in ("draft", "code")],
    }


def _json_plan(root: Path) -> dict:
    import yaml
    try:
        value = yaml.safe_load((root / "stage-09/exp_plan.yaml").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}
