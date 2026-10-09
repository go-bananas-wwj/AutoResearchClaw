"""Overleaf 拉回稿的批注解析与版本 diff。

用户在 Overleaf 上直接改 .tex 或写 ``% 批注: ...`` 注释；拉回后本模块把
两类改动结构化：注释行 → comment 条目，正文改动 → diff hunk 条目。
"""

from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_ANNOTATION_LINE_RE = re.compile(
    r"^\s*%\s*(?:批注|注解|REVIEW|TODO|FIXME|comment)\s*[:：]\s*(.*)$",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(r"\\(?:sub)*section\{([^}]*)\}")


def _section_above(lines: list[str], idx: int) -> str:
    """第 idx 行（0 基）上方最近的 section/subsection 标题。"""
    for j in range(idx - 1, -1, -1):
        m = _SECTION_RE.search(lines[j])
        if m:
            return m.group(1).strip()
    return ""


def parse_annotations(tex: str) -> list[dict]:
    """解析 ``% 批注: ...`` 风格注释行，返回 comment 条目列表。"""
    lines = tex.split("\n")
    out: list[dict] = []
    for i, line in enumerate(lines):
        m = _ANNOTATION_LINE_RE.match(line)
        if not m:
            continue
        context = ""
        for j in range(i - 1, -1, -1):
            s = lines[j].strip()
            if s and not s.startswith("%"):
                context = s[:150]
                break
        out.append({
            "kind": "comment",
            "text": m.group(1).strip(),
            "line_no": i + 1,
            "section": _section_above(lines, i),
            "context": context,
        })
    return out


def diff_versions(base_tex: str, new_tex: str) -> list[dict]:
    """difflib 分 hunk 抽取正文改动，跳过仅空行差异的 hunk。"""
    base_lines = base_tex.splitlines()
    new_lines = new_tex.splitlines()
    sm = difflib.SequenceMatcher(None, base_lines, new_lines, autojunk=False)
    out: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        b = base_lines[i1:i2]
        a = new_lines[j1:j2]
        if [l for l in b if l.strip()] == [l for l in a if l.strip()]:
            continue
        out.append({
            "kind": "edit",
            "section": _section_above(new_lines, j1),
            "before": "\n".join(b)[:800],
            "after": "\n".join(a)[:800],
        })
    return out


def load_annotation_report(run_dir: Path, language: str) -> dict:
    """组装完整批注报告：用户拉回稿的 comments + 相对 base 的 edits。"""
    run_dir = Path(run_dir)
    pulled = run_dir / "paper_annotations" / language / "paper.tex"
    if not pulled.is_file():
        return {
            "has_user_version": False,
            "comments": [],
            "edits": [],
            "pulled_file": "",
        }
    tex = pulled.read_text(encoding="utf-8")
    base_path = run_dir / "paper_versions" / language / "base.tex"
    if not base_path.is_file():
        base_path = run_dir / "deliverables" / "paper.tex"
    edits = []
    if base_path.is_file():
        edits = diff_versions(base_path.read_text(encoding="utf-8"), tex)
    return {
        "has_user_version": True,
        "comments": parse_annotations(tex),
        "edits": edits,
        "pulled_file": str(pulled),
    }
