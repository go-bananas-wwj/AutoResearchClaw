"""Reproduce REST 路由——论文复现的浏览器/对话入口。

流程：找官方 repo（可选）→ 克隆进沙箱真跑 → 宣称 vs 实测对比 → 落盘
《复现报告》（artifacts/<run_id>/reproduction/<paper_slug>/）。
模式与 routes/ideation.py 一致：单租户、后台 task、轮询状态。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["reproduce"])

REPO_ROOT = Path(__file__).resolve().parents[3]


class ReproduceStartRequest(BaseModel):
    """请求体：论文标识 + 可选的 repo / run_id。"""

    paper: str  # arXiv id / 标题 / URL
    run_id: str | None = None  # 缺省新建 rp-<ts>-<hash>
    repo_url: str | None = None  # 缺省调 finder 找官方 repo
    paper_title: str | None = None
    paper_text: str | None = None  # 摘要/正文（对比宣称指标用）


class ReproduceStartResponse(BaseModel):
    run_id: str
    paper_slug: str
    status: str
    output_dir: str


_state: dict[str, Any] | None = None
_task: asyncio.Task[Any] | None = None
_lock = asyncio.Lock()


def _get_app_state() -> dict[str, Any]:
    from researchclaw.server.app import _app_state

    return _app_state


def _find_report_json(run_id: str, paper_slug: str | None) -> Path | None:
    base = REPO_ROOT / "artifacts" / run_id / "reproduction"
    if paper_slug:
        p = base / paper_slug / "reproduction_report.json"
        return p if p.is_file() else None
    if base.is_dir():
        hits = sorted(base.glob("*/reproduction_report.json"))
        return hits[-1] if hits else None
    return None


@router.post("/reproduce/start", response_model=ReproduceStartResponse)
async def start_reproduce(req: ReproduceStartRequest) -> ReproduceStartResponse:
    """启动一次论文复现（找 repo → 沙箱跑通 → 对比 → 报告）。"""
    global _state, _task

    paper = req.paper.strip()
    if len(paper) < 4:
        raise HTTPException(status_code=400, detail="paper 标识太短")

    config = _get_app_state()["config"]
    if not config.reproduce.enabled:
        raise HTTPException(status_code=400, detail="复现模块未启用（reproduce.enabled=false）")

    from researchclaw.reproduce.finder import paper_slug

    slug = paper_slug(req.repo_url or paper)

    async with _lock:
        if _state and _state.get("status") == "running":
            raise HTTPException(status_code=409, detail="一次复现任务正在进行中")

        run_id = (req.run_id or "").strip()
        if run_id:
            if not run_id.replace("-", "").replace("_", "").isalnum():
                raise HTTPException(status_code=400, detail="非法 run_id")
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            phash = hashlib.sha256(paper.encode()).hexdigest()[:6]
            run_id = f"rp-{ts}-{phash}"
        repro_dir = REPO_ROOT / "artifacts" / run_id / "reproduction" / slug
        repro_dir.mkdir(parents=True, exist_ok=True)

        _state = {
            "run_id": run_id,
            "paper_slug": slug,
            "status": "running",
            "phase": "finding_repo",
            "paper": paper,
            "repo_url": req.repo_url or "",
            "output_dir": str(repro_dir),
        }

    async def _run() -> None:
        global _state
        loop = asyncio.get_event_loop()
        try:
            from researchclaw.llm import create_llm_client
            from researchclaw.reproduce.compare import (
                compare_metrics,
                extract_claimed_metrics,
            )
            from researchclaw.reproduce.finder import FinderResult, find_code_repos
            from researchclaw.reproduce.report import build_report, write_report
            from researchclaw.reproduce.runner import ReproductionRunner

            def _llm_or_none() -> Any:
                try:
                    return create_llm_client(config)
                except Exception:  # noqa: BLE001
                    logger.warning("LLM client unavailable — heuristic reproduce")
                    return None

            llm = await loop.run_in_executor(None, _llm_or_none)

            # --- 1. 找 repo ---
            repo_url = (req.repo_url or "").strip()
            finder_result: FinderResult | None = None
            if not repo_url:
                finder_result = await loop.run_in_executor(
                    None,
                    lambda: find_code_repos(
                        paper,
                        s2_api_key=config.llm.s2_api_key,
                        timeout_sec=config.reproduce.finder_timeout_sec,
                    ),
                )
                if not finder_result.ok:
                    raise RuntimeError(
                        "未找到公开代码仓库：" + "; ".join(finder_result.errors or ("未知原因",))
                    )
                repo_url = finder_result.candidates[0].url
                if _state:
                    _state["repo_url"] = repo_url
                    _state["repo_candidates"] = len(finder_result.candidates)

            paper_title = req.paper_title or (finder_result.paper_title if finder_result else "")
            paper_text = req.paper_text or (finder_result.paper_abstract if finder_result else "")

            # --- 2. 沙箱复现 ---
            if _state:
                _state["phase"] = "reproducing"
            runner = ReproductionRunner(config, llm)
            repro_result = await loop.run_in_executor(
                None,
                lambda: runner.run(
                    repo_url,
                    repro_dir,
                    paper_title=paper_title or "",
                    paper_text=paper_text or "",
                ),
            )

            # --- 3. 宣称 vs 实测 ---
            if _state:
                _state["phase"] = "comparing"
            claimed = await loop.run_in_executor(
                None, lambda: extract_claimed_metrics(paper_text or "", llm)
            )
            comparison = await loop.run_in_executor(
                None,
                lambda: compare_metrics(claimed, repro_result.measured_metrics, llm),
            )

            # --- 4. 报告 ---
            if _state:
                _state["phase"] = "reporting"
            candidates = (
                [dataclasses.asdict(c) for c in finder_result.candidates]
                if finder_result
                else []
            )
            report = build_report(
                paper={
                    "identifier": paper,
                    "title": paper_title or "",
                    "arxiv_id": finder_result.arxiv_id if finder_result else "",
                },
                repo_url=repo_url,
                candidates=candidates,
                repro_result=repro_result,
                comparison=comparison,
                llm=llm,
            )
            md_path, json_path = await loop.run_in_executor(
                None, lambda: write_report(repro_dir, report)
            )

            if _state:
                _state["status"] = "completed"
                _state["phase"] = "done"
                _state["repro_ok"] = repro_result.ok
                _state["rounds_used"] = repro_result.rounds_used
                _state["metrics"] = (comparison.get("summary") or {})
                _state["report_md"] = str(md_path)
                _state["report_json"] = str(json_path)
                if not repro_result.ok:
                    _state["error"] = repro_result.final_error
        except Exception as exc:  # noqa: BLE001
            logger.exception("Reproduce run failed")
            if _state:
                _state["status"] = "failed"
                _state["error"] = str(exc)

    _task = asyncio.create_task(_run())
    return ReproduceStartResponse(
        run_id=run_id, paper_slug=slug, status="running", output_dir=str(repro_dir)
    )


@router.get("/reproduce/status")
async def reproduce_status() -> dict[str, Any]:
    """当前/最近一次复现任务状态。"""
    if not _state:
        return {"status": "idle"}
    return _state


@router.get("/reproduce/report")
async def reproduce_report(
    run_id: str | None = None, paper_slug: str | None = None
) -> dict[str, Any]:
    """读取《复现报告》JSON（默认当前任务；可指定历史 run_id / paper_slug）。"""
    rid = run_id or (_state or {}).get("run_id")
    if not rid:
        raise HTTPException(status_code=404, detail="还没有复现任务记录")
    slug = paper_slug or ((_state or {}).get("paper_slug") if rid == (_state or {}).get("run_id") else None)
    path = _find_report_json(rid, slug)
    if path is None:
        raise HTTPException(status_code=404, detail=f"复现报告尚未生成：{rid}")
    return json.loads(path.read_text(encoding="utf-8"))


@router.post("/reproduce/stop")
async def stop_reproduce() -> dict[str, str]:
    """中止当前复现任务。"""
    global _state, _task
    if not _task or not _state or _state.get("status") != "running":
        raise HTTPException(status_code=404, detail="没有进行中的复现任务")
    _task.cancel()
    _state["status"] = "stopped"
    return {"status": "stopped"}
