"""Paper 改稿 REST 路由——Overleaf 批注驱动的论文改稿循环入口。

pull（拉回 Overleaf 用户改动）由 routes/pipeline.py 的 overleaf pull
接口先做；本路由负责：批注报告查询 → LLM 改稿 → 版本快照查询。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from researchclaw.server.routes.pipeline import _get_app_state, _validated_run_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["paper"])


class PaperReviseRequest(BaseModel):
    """请求体：语言 + 可选的补充改稿指令。"""

    language: str = "zh"
    instruction: str = ""


@router.get("/runs/{run_id}/paper/annotations")
async def paper_annotations(run_id: str, language: str = "zh") -> dict[str, Any]:
    """拉回稿的批注 + diff 报告（pull 之后调用）。"""
    run_dir = _validated_run_dir(run_id)
    from researchclaw.writing.annotations import load_annotation_report

    return load_annotation_report(run_dir, language)


@router.post("/runs/{run_id}/paper/revise")
async def paper_revise(run_id: str, req: PaperReviseRequest) -> dict[str, Any]:
    """跑一轮 LLM 改稿（可能 1-3 分钟，放线程池）。"""
    run_dir = _validated_run_dir(run_id)
    config = _get_app_state().get("config")
    from researchclaw.writing.reviser import PaperReviser

    try:
        reviser = PaperReviser(config)
        return await asyncio.to_thread(
            reviser.revise, run_dir, run_id, req.language, req.instruction
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("paper revise failed for %s", run_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/runs/{run_id}/paper/versions")
async def paper_versions(run_id: str, language: str = "zh") -> dict[str, Any]:
    """列出 paper_versions/<language>/ 下的快照文件名和 mtime。"""
    run_dir = _validated_run_dir(run_id)
    vdir = run_dir / "paper_versions" / language
    versions = []
    if vdir.is_dir():
        for p in sorted(vdir.glob("*.tex")):
            versions.append({"file": p.name, "mtime": p.stat().st_mtime})
    return {"run_id": run_id, "language": language, "versions": versions}


@router.post("/runs/{run_id}/paper/translate")
async def paper_translate(run_id: str) -> dict[str, Any]:
    """中文定稿 → 英文 IEEE 版（LLM 翻译 + 数值保真，可能 2-5 分钟，放线程池）。"""
    run_dir = _validated_run_dir(run_id)
    config = _get_app_state().get("config")
    from researchclaw.writing.translator import PaperTranslator

    try:
        translator = PaperTranslator(config)
        return await asyncio.to_thread(translator.translate, run_dir, run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("paper translate failed for %s", run_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
