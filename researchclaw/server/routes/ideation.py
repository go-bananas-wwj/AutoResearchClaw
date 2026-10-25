"""Evidence-first ideation sessions with two explicit human reviews."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from researchclaw.ideation.service import IdeationService
from researchclaw.ideation.store import SessionError, now

router = APIRouter(prefix="/api", tags=["ideation"])
REPO_ROOT = Path(__file__).resolve().parents[3]
_confirm_lock = asyncio.Lock()


def get_service() -> IdeationService:
    from researchclaw.server.app import _app_state
    if "ideation_service" not in _app_state:
        _app_state["ideation_service"] = IdeationService(REPO_ROOT / "artifacts/ideation", _app_state["config"])
    return _app_state["ideation_service"]


def invoke(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def public(data):
    result = dict(data)
    service = get_service()
    result["papers"] = [{**p, "fulltext_verified": service._summary_material(data, p) is not None}
                        for p in data.get("papers", [])]
    result["limits"] = {"paper_limit": IdeationService.paper_limit(data), "candidate_limit": min(60, IdeationService.paper_limit(data) * 3),
                        "reading_default": 10, "reading_max": 15,
                        "idea_card_target":getattr(service.config.llm,'ideation_card_target',12)}
    result["reading"] = {pid: {k: v for k, v in note.items() if k != "passages"}
                         for pid, note in data.get("reading", {}).items()}
    return result


class IdeationStartRequest(BaseModel):
    direction: str = Field(min_length=4, max_length=4000)
    paper_limit: int = Field(default=20, ge=3, le=30)


class IdeationStartResponse(BaseModel):
    session_id: str
    run_id: str  # compatibility alias, NOT a formal rc-* run
    status: str
    output_dir: str


class VersionRequest(BaseModel):
    revision: int = Field(ge=1)


class LiteratureReview(VersionRequest):
    selected_paper_ids: list[str]
    required_paper_ids: list[str] = Field(default_factory=list)
    feedback: str = Field(default="", max_length=4000)


class DirectionConfirmation(VersionRequest):
    feedback: str = Field(default="", max_length=4000)


class ScopeRequest(VersionRequest):
    direction: str = Field(min_length=4, max_length=4000)


class PaperRequest(VersionRequest):
    reference: str = Field(min_length=4, max_length=2000)


class CardRequest(VersionRequest):
    feedback: str = Field(default="", max_length=8000)


class ConfirmRequest(VersionRequest):
    card_id: str


@router.post("/ideation/sessions")
async def create_session(req: IdeationStartRequest):
    return public(invoke(get_service().create, req.direction, req.paper_limit))


@router.get("/ideation/sessions")
async def list_sessions():
    fields = ("session_id", "direction", "status", "phase", "revision", "updated_at", "run_id")
    return {"sessions": [{k: s.get(k) for k in fields} for s in get_service().store.list()]}


@router.get("/ideation/sessions/{sid}")
async def session_detail(sid: str):
    return public(invoke(get_service().snapshot, sid))


@router.get('/ideation/sessions/{sid}/ideaspark/jobs/{job_id}/activity')
async def idea_job_activity(sid: str, job_id: str, call_id: str | None = None, full: bool = False):
    service=get_service();data=invoke(service.snapshot,sid)
    job=next((j for j in (data.get('ideaspark_batch') or {}).get('jobs',[]) if j['id']==job_id),None)
    if not job:raise HTTPException(404,'未找到该调研任务')
    root=service.store.directory(sid).resolve();run=(root/job['directory']).resolve()
    if not run.is_relative_to(root):raise HTTPException(403,'无效任务目录')
    def read_json(path):
        try:return json.loads(path.read_text())
        except (FileNotFoundError,json.JSONDecodeError):return {}
    activity=read_json(run/'host_activity.json')
    entries=[u for u in data.get('usage',[]) if u.get('skill_job_id')==job_id and re.fullmatch(r'[a-f0-9]{32}',u.get('call_id',''))]
    entry=next((u for u in entries if u['call_id']==call_id),None) if call_id else entries[-1] if entries else None
    if call_id and entry is None:raise HTTPException(404,'未找到本任务的调用记录')
    output=None
    if entry:
        directory=root/'streams'/entry['call_id'];meta=read_json(directory/'stream.json');text='';legacy_file=None
        filename=meta.get('file','')
        if re.fullmatch(r'content-\d+\.txt',filename):
            try:
                with (directory/filename).open(encoding='utf-8') as f:text=f.read(2_000_000)
            except FileNotFoundError:pass
        elif entry.get('request_file'):
            path=(root/entry['request_file']).resolve()
            if path.is_relative_to(root):
                legacy_file=path.with_name('response.json');text=read_json(legacy_file).get('content','')
        elif re.fullmatch(r'[a-z_]+',entry['stage']):
            legacy_file=root/'model_outputs'/f"r{data['revision']}-{entry['call_id']}-{entry['stage']}.txt"
            if legacy_file.exists():text=legacy_file.read_text()[:2_000_000]
        active=data['status']=='running' and job['status']=='running'
        output={'call_id':entry['call_id'],'stage':entry['stage'],'model':entry.get('model') or meta.get('model',''),
            'status':entry['status'] if active or entry['status']!='pending' else 'interrupted',
            'mode':meta.get('mode','buffered'),'started_at':meta.get('started_at') or entry['at'],
            'updated_at':meta.get('updated_at'),'finished_at':meta.get('finished_at') or (
                datetime.fromtimestamp(legacy_file.stat().st_mtime,timezone.utc).isoformat() if legacy_file and legacy_file.exists() else None),
            'attempt':meta.get('attempt',1),'characters':meta.get('characters',len(text)),
            'content':text if full else text[-65536:],'windowed':not full and len(text)>65536,'finish_reason':entry.get('finish_reason') or meta.get('finish_reason','')}
    return {'job_id':job_id,'number':job['number'],'focus':job['focus'],'status':job['status'],
        'session_status':data['status'],'stage':job.get('stage',''),'stage_started_at':job.get('stage_started_at'),
        'reason':job.get('reason',''),'activity':{k:v for k,v in activity.items() if k in
            ('kind','status','stage','command','started_at','finished_at','timeout_sec','exit_code')},
        'output':output,'calls':[{k:u.get(k) for k in ('call_id','stage','at','status','model','prompt_tokens','completion_tokens')}
            for u in entries[-20:]]}


@router.post("/ideation/sessions/{sid}/literature-review")
async def review_literature(sid: str, req: LiteratureReview):
    return public(invoke(get_service().review_literature, sid, req.revision,
                         req.selected_paper_ids, req.required_paper_ids, req.feedback))


@router.post("/ideation/sessions/{sid}/direction-confirm")
async def confirm_direction(sid: str, req: DirectionConfirmation):
    return public(invoke(get_service().confirm_direction, sid, req.revision, req.feedback))


@router.post("/ideation/sessions/{sid}/metadata")
async def refresh_metadata(sid: str, req: VersionRequest):
    return public(invoke(get_service().refresh_metadata, sid, req.revision))


@router.post("/ideation/sessions/{sid}/fulltexts")
async def acquire_fulltexts(sid: str, req: VersionRequest):
    return public(invoke(get_service().acquire_fulltexts, sid, req.revision))


@router.get("/ideation/sessions/{sid}/papers/{paper_id}/fulltext")
async def paper_fulltext(sid: str, paper_id: str):
    service = get_service()
    data = invoke(service.snapshot, sid)
    if not re.fullmatch(r"p-[a-f0-9]{20}", paper_id) or paper_id not in {p["paper_id"] for p in data["papers"]} | data["reading"].keys():
        raise HTTPException(404, "论文不存在")
    directory = service.store.directory(sid)
    cache = directory / "fulltext" / (paper_id + ".json")
    if not cache.exists():
        raise HTTPException(404, "该论文尚未获取原文")
    paper = next((p for p in data["papers"] if p["paper_id"] == paper_id),
                 {"paper_id": paper_id, "title": data.get("reading", {}).get(paper_id, {}).get("title", "")})
    if service._summary_material(data, paper) is None:
        raise HTTPException(409, "缓存未通过全文与论文身份核验，请重新整理全文文献。")
    pdf = directory / "downloads" / (paper_id + ".pdf")
    if pdf.exists():
        return FileResponse(pdf, media_type="application/pdf", filename=paper_id + ".pdf")
    document = json.loads(cache.read_text())
    return PlainTextResponse("\n\n".join(p["location"] + "\n" + p["text"] for p in document["pages"]))


@router.post("/ideation/sessions/{sid}/summaries")
async def refresh_summaries(sid: str, req: VersionRequest):
    return public(invoke(get_service().refresh_summaries, sid, req.revision))


@router.post('/ideation/sessions/{sid}/card-summaries')
async def refresh_card_summaries(sid: str, req: VersionRequest):
    return public(invoke(get_service().refresh_cards,sid,req.revision))


@router.post("/ideation/sessions/{sid}/papers/{paper_id}/remove")
async def remove_paper(sid: str, paper_id: str, req: VersionRequest):
    return public(invoke(get_service().remove_paper, sid, req.revision, paper_id))


@router.post("/ideation/sessions/{sid}/scope")
async def revise_scope(sid: str, req: ScopeRequest):
    return public(invoke(get_service().revise_scope, sid, req.revision, req.direction))


@router.post("/ideation/sessions/{sid}/papers")
async def add_paper(sid: str, req: PaperRequest):
    service = get_service()
    data = invoke(service.snapshot, sid)
    invoke(service.store.require_revision, data, req.revision)
    invoke(service._editable, data)
    try:
        paper = await asyncio.to_thread(service.sources.resolve, req.reference)
    except Exception as exc:
        raise HTTPException(400, "论文元数据获取失败，请检查 DOI/URL 或改用 PDF。") from exc
    return public(invoke(service.add_paper, sid, req.revision, paper))


@router.post("/ideation/sessions/{sid}/papers/upload")
async def upload_paper(sid: str, revision: int, request: Request):
    service = get_service()
    data = invoke(service.snapshot, sid)
    invoke(service.store.require_revision, data, revision)
    invoke(service._editable, data)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 25 * 1024 * 1024:
            raise HTTPException(413, "PDF最大25MB")
    if not content.startswith(b"%PDF"):
        raise HTTPException(400, "请选择有效 PDF 文件")
    relative = "uploads/" + uuid.uuid4().hex + ".pdf"
    path = service.store.directory(sid) / relative
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    try:
        doc = await asyncio.to_thread(service.sources.pdf_document, path)
        from researchclaw.ideation.sources import paper_record
        filename = Path(unquote(request.headers.get("X-Filename", "用户论文.pdf"))).name
        paper = paper_record({"title": doc.get("title") or filename, "abstract": doc.get("abstract", ""),
            "source": "user_pdf", "uploaded_pdf": relative, "content_hash": hashlib.sha256(content).hexdigest(),
            "year": 0}, source="user_pdf")
        return public(invoke(service.add_paper, sid, revision, paper))
    except Exception as exc:
        path.unlink(missing_ok=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(400, "PDF解析失败，请提供含可提取正文的PDF。") from exc


@router.post("/ideation/sessions/{sid}/cards")
async def regenerate_cards(sid: str, req: CardRequest):
    return public(invoke(get_service().regenerate, sid, req.revision, req.feedback))


@router.post("/ideation/sessions/{sid}/resume")
async def resume_session(sid: str, req: VersionRequest):
    return public(invoke(get_service().resume, sid, req.revision))


@router.post("/ideation/sessions/{sid}/stop")
async def stop_session(sid: str):
    return public(invoke(get_service().stop, sid))


def _check_data_access(card):
    from researchclaw.ideation.sources import PublicRedirect
    from researchclaw.web._ssrf import check_url_ssrf
    import urllib.request
    sources = card.get("data_sources") or []
    if not sources or len(sources) > 5 or any(s.get("availability") != "public" or not s.get("url") for s in sources):
        raise SessionError("数据可得性未确认：请在修改意见中补充公开数据来源后重新生成，不会用虚构数据启动实验。")
    for source in sources:
        url = source["url"]
        if check_url_ssrf(url):
            raise SessionError("数据来源须为公共地址。")
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "ResearchClaw/0.5"})
            with urllib.request.build_opener(PublicRedirect()).open(req, timeout=20) as response:
                if response.status >= 400:
                    raise ValueError("unavailable")
        except Exception as exc:
            raise SessionError("数据来源无法访问或需要登录，请补充可用数据入口后重试。") from exc


@router.post("/ideation/sessions/{sid}/confirm")
async def confirm_session(sid: str, req: ConfirmRequest):
    from researchclaw.server.routes.pipeline import PipelineStartRequest, start_pipeline
    async with _confirm_lock:
        service = get_service()
        data, card = invoke(service.confirmation, sid, req.revision, req.card_id)
        if data.get("run_id"):
            return {"run_id": data["run_id"], "started": True, "from_stage": "EXPERIMENT_DESIGN"}
        data["status"] = "handoff"
        if not any(r.get("type") == "idea" and r.get("revision") == req.revision and r.get("card_id") == req.card_id for r in data["reviews"]):
            data["reviews"].append({"type": "idea", "revision": req.revision, "card_id": req.card_id, "at": now()})
        service.store.save(data)
        try:
            await asyncio.to_thread(_check_data_access, card)
            brief, snapshot = await asyncio.to_thread(service.export, data, card)
            key = f"{sid}:{req.revision}:{req.card_id}"
            response = await start_pipeline(PipelineStartRequest(topic=card["title"], brief=brief,
                from_stage="EXPERIMENT_DESIGN", auto_approve=True, idempotency_key=key))
        except SessionError as exc:
            data.update(status="blocked", missing=[str(exc)])
            service.store.save(data)
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            data.update(status="blocked", missing=[str(getattr(exc, "detail", "科研交接失败，请重试。"))])
            service.store.save(data)
            raise
        data.update(run_id=response.run_id, confirmed_card_id=req.card_id, status="completed", phase="handed_off", missing=[])
        data["reviews"].append({"type": "idea", "revision": req.revision, "card_id": req.card_id,
                                "run_id": response.run_id, "at": now()})
        service.store.save(data)
        return {"run_id": response.run_id, "started": True, "from_stage": "EXPERIMENT_DESIGN"}


@router.get("/ideation/prompt")
async def idea_prompt():
    from researchclaw.ideation.prompts import PROMPTS, OUTPUT_TOKENS
    from researchclaw.ideation.cards import PATTERNS
    service = get_service()
    stages=dict(PROMPTS)
    skill=None
    if getattr(service.config.llm,'ideation_backend','builtin')=='ideaspark':
        from researchclaw.ideation.ideaspark import ROOT, profile
        skill=profile()
        stages.pop('generate',None);stages.pop('audit',None)
        stages.update({'ideaspark:'+p.stem:p.read_text() for p in sorted((ROOT/'references/system-prompts').glob('*.txt'))})
    return {"stages": stages, "output_tokens": OUTPUT_TOKENS, "model": service.config.llm.primary_model,
            "skill":skill,"card_target":getattr(service.config.llm,'ideation_card_target',12),
            "workers":getattr(service.config.llm,'ideation_workers',3),
            "thinking":getattr(service.config.llm,'ideation_thinking','disabled'),
            "retrieval": True, "pattern_catalog": PATTERNS,
            "budget_basis": "预算为保守预留：输入$5/百万tokens、输出$25/百万tokens，非供应商账单。"}


@router.get('/ideation/sessions/{sid}/ideaspark/jobs/{job_id}/card')
async def ideaspark_original_card(sid: str, job_id: str, language: str='zh'):
    data=invoke(get_service().snapshot,sid)
    job=next((j for j in (data.get('ideaspark_batch') or {}).get('jobs',[]) if j['id']==job_id),None)
    names={'zh':'idea.std.zh.md','en':'idea.std.en.md','detail':'idea.detail.en.md'}
    if not job or language not in names:raise HTTPException(404,'未找到IdeaSpark方案')
    root=get_service().store.directory(sid).resolve()
    directory=(root/job['directory']).resolve()
    if not directory.is_relative_to(root):raise HTTPException(403,'无效执行目录')
    target=directory/'phase4'/names[language]
    if not target.exists():raise HTTPException(404,'IdeaSpark尚未生成完整方案，当前可查看候选卡片')
    return PlainTextResponse(target.read_text(),media_type='text/markdown')


@router.post("/ideation/start", response_model=IdeationStartResponse)
async def start_ideation(req: IdeationStartRequest):
    data = invoke(get_service().create, req.direction, req.paper_limit)
    return IdeationStartResponse(session_id=data["session_id"], run_id=data["session_id"], status=data["status"],
        output_dir=str(get_service().store.directory(data["session_id"])))


@router.get("/ideation/status")
async def ideation_status(session_id: str | None = None):
    service = get_service()
    rows = service.store.list()
    data = invoke(service.snapshot, session_id) if session_id else (rows[0] if rows else None)
    return public(data) if data else {"status": "idle"}


@router.get("/ideation/report")
async def ideation_report(run_id: str | None = None, session_id: str | None = None):
    sid = session_id or run_id
    if sid and sid.startswith("id-"):
        if not re.fullmatch(r"id-\d{8}-\d{6}-[a-f0-9]+", sid):
            raise HTTPException(400, "无效历史调研编号")
        path = REPO_ROOT / "artifacts" / sid / "ideation_report.json"
        if not path.is_file():
            raise HTTPException(404, "历史调研不存在")
        return json.loads(path.read_text())
    return await ideation_status(sid)


@router.post("/ideation/stop")
async def stop_ideation(session_id: str | None = None):
    state = await ideation_status(session_id)
    if not state.get("session_id"):
        raise HTTPException(404, "没有调研会话")
    return await stop_session(state["session_id"])


class LegacyCardsRequest(BaseModel):
    session_id: str | None = None
    revision: int | None = None
    feedback: str = ""
    card_id: str | None = None


@router.post("/ideation/cards")
async def idea_cards(req: LegacyCardsRequest):
    if not req.session_id or req.revision is None:
        raise HTTPException(409, "请先创建调研会话并审核文献；不再支持无证据直接生成卡片。")
    return await regenerate_cards(req.session_id, CardRequest(revision=req.revision, feedback=req.feedback))


@router.post("/ideation/confirm")
async def idea_confirm(req: LegacyCardsRequest):
    if not req.session_id or req.revision is None or not req.card_id:
        raise HTTPException(409, "确认须引用已审查会话的当前版本和卡片ID，不能提交任意卡片启动科研。")
    return await confirm_session(req.session_id, ConfirmRequest(revision=req.revision, card_id=req.card_id))
