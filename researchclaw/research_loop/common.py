"""研究循环的公共部分：目录与状态、结果账、模型 JSON 调用、引文逐字核对。"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOOP_DIR = "research_loop"  # run_dir 根下，不在阶段目录里：恢复归档阶段目录时轮次不丢
RESULTS_TSV = "results.tsv"
RESULT_FIELDS = ("round", "hypothesis", "node", "primary_metric", "best_before", "improved", "decision", "at")


def loop_dir(run_dir: Path) -> Path:
    path = run_dir / LOOP_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def round_dir(run_dir: Path, n: int) -> Path:
    path = loop_dir(run_dir) / "rounds" / f"round-{n:02d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def state(run_dir: Path) -> dict[str, Any]:
    return read_json(loop_dir(run_dir) / "state.json", {}) or {}


def save_state(run_dir: Path, **changes: Any) -> dict[str, Any]:
    current = {**state(run_dir), **changes, "updated_at": now()}
    write_json(loop_dir(run_dir) / "state.json", current)
    return current


def results_rows(run_dir: Path) -> list[dict[str, str]]:
    path = loop_dir(run_dir) / RESULTS_TSV
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def append_result(run_dir: Path, row: dict[str, Any]) -> None:
    """results.tsv：每轮一行（autoresearch 式），同一轮重复写入时覆盖该行。"""
    rows = [r for r in results_rows(run_dir) if r.get("round") != str(row.get("round"))]
    rows.append({k: " ".join(str(row.get(k, "")).split()) for k in RESULT_FIELDS})
    rows.sort(key=lambda r: int(r.get("round") or 0))
    path = loop_dir(run_dir) / RESULTS_TSV
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=RESULT_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def parse_json(text: str) -> Any:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            pass
    return None


def ask_json(llm: Any, system: str, payload: Any, *, max_tokens: int) -> dict[str, Any]:
    """一次 JSON 调用：截断按次翻倍重试（_chat_with_prompt）；解析失败返回带 _error 的字典交调用方反馈重试。"""
    from researchclaw.pipeline._helpers import _chat_with_prompt
    user = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=1)
    resp = _chat_with_prompt(llm, system, user, json_mode=True, max_tokens=max_tokens)
    data = parse_json(resp.content)
    return data if isinstance(data, dict) else {"_error": "回复不是 JSON 对象"}


def _norm(text: str) -> str:
    from researchclaw.data_acquisition.planner import norm
    return norm(text)


def cite_problems(cites: Any, run_dir: Path, *, artifacts: dict[str, str] | None = None) -> list[str]:
    """引文必须逐字出现在已读全文（paper:<paper_id>）或本项目产物（artifact:<相对路径>）里。

    与数据方案、创新点生成同样的接地要求：诊断与假设要有出处，宿主只核对是否逐字存在，不评判科学内容。"""
    from researchclaw.data_acquisition.planner import fulltext
    problems = []
    for i, cite in enumerate(cites or []):
        if not isinstance(cite, dict):
            problems.append(f"cites[{i}] 不是对象")
            continue
        source, quote = str(cite.get("source") or ""), str(cite.get("quote") or "")
        if len(_norm(quote)) < 6:
            problems.append(f"cites[{i}] 引文过短")
            continue
        if source.startswith("paper:"):
            text = fulltext(run_dir, source[6:])
        elif source.startswith("artifact:"):
            rel = source[9:]
            text = (artifacts or {}).get(rel)
            if text is None:
                path = (run_dir / rel).resolve()
                text = path.read_text(encoding="utf-8", errors="replace") if (
                    path.is_file() and run_dir.resolve() in path.parents and path.stat().st_size < 5_000_000) else ""
        else:
            problems.append(f"cites[{i}] 的 source 须为 paper:<paper_id> 或 artifact:<相对路径>")
            continue
        if not text:
            problems.append(f"cites[{i}] 的来源 {source!r} 不存在")
        elif _norm(quote) not in _norm(text):
            problems.append(f"cites[{i}] 的引文未在 {source!r} 中逐字找到；请复制连续原文")
    return problems
