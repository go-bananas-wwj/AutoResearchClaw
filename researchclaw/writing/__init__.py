"""Overleaf 批注驱动的论文改稿循环：批注解析 / 版本 diff / LLM 改稿。"""

from researchclaw.writing.annotations import (
    diff_versions,
    load_annotation_report,
    parse_annotations,
)
from researchclaw.writing.reviser import PaperReviser

__all__ = [
    "parse_annotations",
    "diff_versions",
    "load_annotation_report",
    "PaperReviser",
]
