"""研究日志（只读视图 + 用户意见写入）：每轮的计划、汇报、导师意见与图，按时间线给网页与 API。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from researchclaw.research_loop.common import loop_dir, now, read_json, results_rows, round_dir, state

_FIGURE = re.compile(r"^research_loop/rounds/round-\d{2}/figures/[\w.\-]+\.png$")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def journal(run_dir: Path) -> dict[str, Any]:
    root = run_dir / "research_loop"
    rounds = []
    for d in sorted((root / "rounds").glob("round-*")) if root.is_dir() else []:
        n = int(d.name.split("-")[1])
        if n < 1:
            continue
        meta = read_json(d / "round.json", {}) or {}
        review = read_json(d / "review.json", {}) or {}
        rounds.append({
            "round": n, "plan": _text(d / "plan.md"), "report": _text(d / "report.md"), "review": _text(d / "review.md"),
            "decision": review.get("decision"), "node": meta.get("node"), "improved": meta.get("improved"),
            "failure": (meta.get("outcome") or {}).get("failure"),
            "figures": [f"research_loop/rounds/{d.name}/{f}" for f in meta.get("figures", [])],
            "user_notes": [_text(p) for p in sorted(d.glob("advisor_note*.md"))],
        })
    return {"enabled": root.is_dir(), "state": state(run_dir) if root.is_dir() else {},
            "charter": _text(root / "charter.md"), "results": results_rows(run_dir) if root.is_dir() else [],
            "rounds": rounds}


def figure_path(run_dir: Path, rel: str) -> Path:
    """只允许读取各轮 figures/ 下的 PNG（防路径穿越）。"""
    if not _FIGURE.match(rel or ""):
        raise ValueError("只能读取研究循环各轮 figures/ 下的 PNG")
    path = (run_dir / rel).resolve()
    if run_dir.resolve() not in path.parents or not path.is_file():
        raise FileNotFoundError(rel)
    return path


def add_user_note(run_dir: Path, text: str) -> str:
    """用户（真正的导师）随时写下的意见：写入当前轮（未完成）或下一轮，下一次计划/审阅时读取，优先级高于 AI 导师。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("意见不能为空")
    loop_dir(run_dir)
    current = int(state(run_dir).get("round") or 1)
    meta = read_json(round_dir(run_dir, current) / "round.json", {}) or {}
    target = current + 1 if meta.get("done") else current
    stamp = now().replace(":", "").replace("-", "")
    path = round_dir(run_dir, target) / f"advisor_note_user_{stamp}.md"
    path.write_text(text, encoding="utf-8")
    return path.relative_to(run_dir).as_posix()
