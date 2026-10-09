"""Project listing / status API routes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["projects"])

_TOTAL_STAGES = 23
_TOPIC_RE = re.compile(r"^#\s*\*\*Topic\*\*[:：]\s*(.+?)\s*$", re.MULTILINE)


def _read_topic(run_dir: Path) -> str:
    """从 stage-01/goal.md 解析论文题目（没有就回退空串，由前端回退 run_id）。"""
    goal = run_dir / "stage-01" / "goal.md"
    if goal.is_file():
        try:
            m = _TOPIC_RE.search(goal.read_text(encoding="utf-8", errors="replace")[:4000])
            if m:
                return m.group(1)
        except OSError:
            pass
    return ""


def _project_info(d: Path) -> dict[str, Any]:
    proj: dict[str, Any] = {"id": d.name, "path": str(d)}

    ckpt = d / "checkpoint.json"
    if ckpt.exists():
        try:
            with ckpt.open() as f:
                ckpt_data = json.load(f)
            proj["current_stage"] = ckpt_data.get("last_completed_stage")
            proj["stage_name"] = ckpt_data.get("last_completed_name", "")
            proj["status"] = ckpt_data.get("status", "unknown")
        except Exception:
            proj["status"] = "unknown"
    else:
        proj["status"] = "no_checkpoint"

    proj["topic"] = _read_topic(d)

    # 门控等待状态（读 hitl/waiting.json，项目卡片要亮"等门控"徽章）
    waiting_file = d / "hitl" / "waiting.json"
    proj["waiting"] = False
    if waiting_file.is_file():
        try:
            w = json.loads(waiting_file.read_text(encoding="utf-8"))
            proj["waiting"] = True
            proj["waiting_stage"] = w.get("stage")
            proj["waiting_stage_name"] = w.get("stage_name", "")
        except json.JSONDecodeError:
            pass

    # 论文状态（deliverables 下的中文/英文版）
    deliverables = d / "deliverables"
    proj["has_paper_zh"] = (deliverables / "paper.tex").is_file()
    proj["has_paper_en"] = (deliverables / "paper_en.tex").is_file()

    # 排序/显示用：目录最后修改时间
    try:
        proj["updated_at"] = d.stat().st_mtime
    except OSError:
        proj["updated_at"] = 0
    return proj


@router.get("/projects")
async def list_projects() -> dict[str, Any]:
    """列出全部项目（artifacts/rc-*），一个项目对应一篇论文。

    返回字段：id / topic / status / current_stage / stage_name /
    waiting(+waiting_stage) / has_paper_zh / has_paper_en / updated_at。
    """
    artifacts = Path("artifacts")
    projects: list[dict[str, Any]] = []
    if artifacts.exists():
        for d in sorted(artifacts.iterdir(), reverse=True):
            if d.is_dir() and d.name.startswith("rc-"):
                projects.append(_project_info(d))
    return {"projects": projects, "total_stages": _TOTAL_STAGES}
