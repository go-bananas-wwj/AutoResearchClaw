"""Persistent, two-review research intake. No pipeline or model-only evidence shortcuts."""
from __future__ import annotations

import copy
import json
import re
import threading
import unicodedata
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from researchclaw.ideation.prompts import OUTPUT_TOKENS, PROMPTS
from researchclaw.ideation.reading import input_bytes, pack
from researchclaw.ideation.sources import Sources, paper_record, readable_document, safe_url
from researchclaw.ideation.store import SessionError, SessionStore, fingerprint, now


class Cancelled(Exception):
    pass


class WorkflowFailure(SessionError):
    def __init__(self, message, category="technical", code="stage_failed", context=None, recoverable=False):
        super().__init__(message)
        self.category = category
        self.code = code
        self.context = context or {}
        self.recoverable = recoverable


class CandidateRoundFailure(SessionError):
    pass


OVERVIEW_FIELDS = ("problem", "background", "innovation", "method", "data", "results", "strengths", "limitations")
SUMMARY_VERSION = 6
DEFAULT_PAPER_LIMIT = 20
MAX_CANDIDATES = 60
MAX_AUTO_REFINEMENTS = 2  # Initial generation plus at most two evidence-driven revisions.


def quoted(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).replace("\u00ad", ""))


def summary_json(text):
    """Repair bounded punctuation errors outside strings; never rewrite paper content."""
    from researchclaw.ideation.cards import _extract_json
    try:
        return _extract_json(text), False
    except json.JSONDecodeError:
        raw = text[text.find("{"):text.rfind("}") + 1]
        # Some model replies contain redundant closing brackets. Remove at most three,
        # using a string/escape-aware stack; never append missing structure or values.
        stack, output, in_string, escaped, removed = [], [], False, False, 0
        for char in raw:
            if in_string:
                output.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True; output.append(char)
            elif char in "{[":
                stack.append(char); output.append(char)
            elif char in "}]":
                if stack and stack[-1] == ("{" if char == "}" else "["):
                    stack.pop(); output.append(char)
                else:
                    removed += 1
                    if removed > 3:
                        raise ValueError("Too many unmatched JSON delimiters")
            else:
                output.append(char)
        repaired = "".join(output)
        try:
            return json.loads(repaired), bool(removed)
        except json.JSONDecodeError as exc:
            if exc.msg != "Expecting ',' delimiter" or not repaired[:exc.pos].rstrip().endswith('"') or not re.match(r'"selection"\s*:', repaired[exc.pos:]):
                raise
            return json.loads(repaired[:exc.pos] + "," + repaired[exc.pos:]), True


def reference_aliases(payload):
    """Lossless request-local ids: model copies E1/P1, server retains content hashes."""
    papers, excerpts = {}, {}
    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                mapping = papers if key == "paper_id" else excerpts if key == "passage_id" else None
                if mapping is not None and isinstance(item, str):
                    mapping.setdefault(item, ("P" if mapping is papers else "E") + str(len(mapping) + 1))
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(payload)
    def convert(value, reverse=False, field=""):
        if isinstance(value, dict):
            return {k: convert(v, reverse, k) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v, reverse, field) for v in value]
        mapping = papers if field in ("paper_id", "paper_ids", "closest_paper_ids") else excerpts if field in ("passage_id", "evidence_refs") else {}
        if reverse:
            mapping = {v: k for k, v in mapping.items()}
        return mapping.get(value, value) if isinstance(value, str) else value
    return convert(payload), lambda result: convert(result, True)


def passages(paper_id: str, document: dict[str, Any]) -> list[dict[str, str]]:
    output = []
    for page in document["pages"]:
        text = page["text"]
        # Bounded chunks retain source location and a content-bound stable identity.
        for start in range(0, len(text), 1800):
            chunk = text[start:start + 1800]
            if not chunk.strip():
                continue
            output.append({"passage_id": paper_id + ":" + fingerprint([page["location"], start, chunk])[:16],
                           "paper_id": paper_id, "location": page["location"], "text": chunk})
    return output


class IdeationService:
    def __init__(self, root: Path, config: Any, llm: Any = None, sources: Any = None):
        self.store = SessionStore(root)
        self.config = config
        self.llm = llm
        self.sources = sources or Sources(config)
        self.jobs: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.store.recover()

    def _client(self):
        if self.llm is None:
            from researchclaw.llm.client import LLMClient
            self.llm = LLMClient.from_rc_config(self.config)
        return self.llm

    def snapshot(self, sid: str) -> dict[str, Any]:
        data = self.store.load(sid)
        if (data.get('blocker') or {}).get('code')=='ideaspark_no_validated_cards':
            jobs=(data.get('ideaspark_batch') or {}).get('jobs',[])
            pending=sum(bool(j.get('retryable')) for j in jobs)
            if pending:
                message=f'已有{len(data.get("idea_pool",[]))}张草稿；{pending}个任务因技术问题未完成审查，尚不能确认。'
                data.update(missing=[message],blocker={'category':'technical','code':'ideaspark_review_incomplete',
                    'message':message,'actions':['重试未完成的审查'],'context':{}})
        # Old sessions retain real failed-source receipts, even without typed errors.
        for item in data.get("rejected_cards", []):
            failure = item.get("failure") or {}
            if failure.get("context") or not str(failure.get("message", "")).startswith("未取得方法正文"):
                continue
            paper = next((p for p in item.get("closest_papers", []) if p["paper_id"] not in data.get("reading", {}) and p.get("fetch_attempts")), None)
            if paper:
                old = failure["message"]
                exc = self._paper_failure(paper, SessionError(old), "获取全文")
                exc.context.update(candidate_id=item.get("card_id"), candidate_title=item.get("title"), impact="该候选暂缓，继续处理其他候选")
                failure.update(message=str(exc), category=exc.category, code=exc.code, context=exc.context, recoverable=True)
                item["rejection_reason"] = str(exc)
                if (data.get("blocker") or {}).get("message") == old:
                    data["blocker"].update(failure)
                    data["missing"] = [str(exc) if m == old else m for m in data.get("missing", [])]
        return data

    @staticmethod
    def _paper_failure(paper, exc, operation):
        if isinstance(exc, WorkflowFailure) and exc.category == "budget":
            return exc
        if isinstance(exc, WorkflowFailure) and exc.context:
            return exc
        reason = re.sub(r"^未取得方法正文[:：]\s*", "", str(exc))[:500]
        context = {"paper_id": paper["paper_id"], "paper_title": paper["title"], "doi": paper.get("doi", ""),
                   "url": safe_url(paper.get("url") or ""), "operation": operation, "reason": reason,
                   "attempts": [{"url": safe_url(a.get("url") or ""), "outcome": a.get("outcome", "")}
                                for a in paper.get("fetch_attempts", [])]}
        return WorkflowFailure(f"《{paper['title']}》{operation}未完成：{reason}",
            getattr(exc, "category", "evidence" if operation in ("获取全文", "核对正文") else "technical"),
            getattr(exc, "code", "fulltext_unavailable" if operation == "获取全文" else "paper_read_failed"), context, recoverable=True)

    def _warning(self, data, event, exc, candidate):
        context = {**exc.context, "candidate_id": candidate.get("card_id"), "candidate_title": candidate.get("title")}
        context["impact"] = "该候选暂缓，继续处理其他候选" if context.get("critical") else "跳过本篇补充对照，继续审查"
        key = fingerprint([data["revision"], context.get("paper_id"), candidate.get("candidate_key"), exc.code])
        entry = {"warning_id": key, "at": now(), "revision": data["revision"], "message": str(exc), "context": context, "code": exc.code}
        data["warnings"] = [w for w in data.get("warnings", []) if w["warning_id"] != key] + [entry]
        self._save(data, event)

    def _sync_unlimited_budget(self, data):
        configured = float(getattr(getattr(self.config, "hitl", None), "cost_budget_usd", 0) or 0)
        if configured <= 0 and data.get("budget_usd", 0) > 0:
            data["reviews"].append({"event": "budget_policy", "at": now(), "previous_limit": data["budget_usd"], "limit": 0})
            data["budget_usd"] = 0

    def _check(self, data, event):
        if event.is_set():
            raise Cancelled()
        if data["budget_usd"] > 0 and data["cost_usd"] >= data["budget_usd"]:
            raise WorkflowFailure("调研预算已用完，请调整成本预算后恢复。", "budget", "budget_exhausted")

    def _save(self, data, event):
        with self.store.lock:
            self._check(data, event)
            self.store.save(data)

    def _progress(self, data, event, message, **fields):
        data["progress"] = {**data.get("progress", {}), **fields, "message": message, "at": now()}
        data["progress_message"] = message
        self._save(data, event)

    def _context_capacity(self):
        llm = self.config.llm
        model = str(llm.primary_model)
        if urllib.parse.urlsplit(getattr(llm, "base_url", "")).hostname == "api.deepseek.com" and (model == "deepseek-flash" or model.startswith("deepseek-v4")):
            return 1_000_000
        return int(getattr(llm, "context_window_tokens", 128_000) or 128_000)

    def _input_limit(self, stage):
        # UTF-8 bytes conservatively upper-bound tokens, including aliases/JSON overhead.
        return self._context_capacity() - OUTPUT_TOKENS[stage] * 2 - len(PROMPTS[stage].encode()) - 16384

    def _json(self, data, event, stage, payload, _format_attempt=0, _output_scale=1):
        self._check(data, event)
        labels = {"plan": "正在理解研究方向并规划检索", "screen": "正在核对文献相关性与科学问题",
                  "summarize": "正在整理论文的科学问题、方法、优势与局限", "neighbors": "正在筛选最近邻方法", "synthesis": "正在比较方法并定位研究缺口",
                  "generate": "正在根据原文证据构思创新点", "audit": "正在独立审查最近邻工作与方案差异"}
        if stage in labels:
            self._progress(data, event, labels[stage], operation=stage,
                           **({"title": "", "paper_id": "", "current": 0, "total": 0} if stage in ("synthesis", "generate", "audit") else {}))
        isolated = stage in ("read", "read_part", "read_merge", "summarize")
        paper_ids = set()
        def collect_papers(value):
            if isinstance(value, dict):
                if isinstance(value.get("paper_id"), str):
                    paper_ids.add(value["paper_id"])
                for item in value.values():
                    collect_papers(item)
            elif isinstance(value, list):
                for item in value:
                    collect_papers(item)
        if isolated:
            collect_papers(payload)
        if isolated and len(paper_ids) != 1:
            raise WorkflowFailure("单篇阅读请求必须且只能包含一篇论文。", "technical", "mixed_paper_context")
        original_payload = payload
        payload, restore = reference_aliases(payload)
        prompt = json.dumps(payload, ensure_ascii=False)
        if isolated and len((PROMPTS[stage] + prompt).encode()) + OUTPUT_TOKENS[stage] * _output_scale + 2048 > self._context_capacity():
            raise WorkflowFailure("阅读请求超过配置的上下文容量，自动分段参数需要修正。", "technical", "read_partition_capacity")
        # Explicit conservative reservation, NOT a claim about vendor prices.
        # UTF-8 bytes upper-bound input tokens. Reserve $5/M input, $25/M output.
        output_budget = OUTPUT_TOKENS[stage] * _output_scale
        reserve = (len((PROMPTS[stage] + prompt).encode()) * 5 + output_budget * 25) / 1_000_000
        if data["budget_usd"] > 0 and data["cost_usd"] + reserve > data["budget_usd"]:
            raise WorkflowFailure("剩余预算不足以预留下一次调用，请调整预算后恢复。", "budget", "budget_exhausted")
        # Reserve before the request so a process restart cannot erase an in-flight charge.
        data["cost_usd"] = round(data["cost_usd"] + reserve, 6)
        entry = {"call_id": uuid.uuid4().hex, "stage": stage, "at": now(), "cost_usd": reserve, "status": "pending",
                 "cost_basis": "conservative_reservation_5_input_25_output_usd_per_million_not_invoice",
                 "prompt_hash": fingerprint(PROMPTS[stage])[:16], "output_budget": output_budget,
                 "paper_ids": sorted(paper_ids) if isolated else [], "input_bytes": len((PROMPTS[stage] + prompt).encode()),
                 "context_limit_tokens": self._context_capacity()}
        data["usage"].append(entry)
        self.store.save(data)
        response = None
        from .streams import CallStream
        from researchclaw.llm.client import LLMStreamAborted
        stream=CallStream(self.store.directory(data['session_id']),entry['call_id'],stage,event)
        try:
            client=self._client()
            try:
                response = client.chat([{"role": "user", "content": prompt}],
                    system=PROMPTS[stage], max_tokens=output_budget, temperature=0.3, json_mode=True,**stream.options(client))
            except LLMStreamAborted:
                self._check(data,event);raise
        finally:
            stream.finish(response)
            # Failed/cancelled/truncated calls also retain their reservation and usage.
            entry.update({"status": "returned" if response is not None else "failed",
                "model": getattr(response, "model", self.config.llm.primary_model),
                "prompt_tokens": getattr(response, "prompt_tokens", 0),
                "completion_tokens": getattr(response, "completion_tokens", 0)})
            if event.is_set():
                data["status"] = "stopping"
            self.store.save(data)
        self._check(data, event)
        if getattr(response, "truncated", False) or getattr(response, "finish_reason", "") == "length":
            if not _format_attempt:
                return self._json(data, event, stage, {**original_payload,
                    "output_feedback": "上次输出被截断，本次输出上限已扩大；请按结构完整且简洁地返回。"},
                    _format_attempt=1, _output_scale=2)
            raise WorkflowFailure(f"{stage} 扩大输出上限后仍被截断，未接受不完整内容。", "technical", "model_truncated")
        from researchclaw.ideation.cards import _extract_json
        output_dir = self.store.directory(data["session_id"]) / "model_outputs"
        output_dir.mkdir(exist_ok=True)
        (output_dir / f"r{data['revision']}-{entry['call_id']}-{stage}.txt").write_text(response.content)
        try:
            if stage == "summarize":
                result, repaired = summary_json(response.content)
                if repaired:
                    entry["json_syntax_repair"] = "bounded_structural_punctuation"
                    self.store.save(data)
            else:
                result = _extract_json(response.content)
        except ValueError as exc:
            if not _format_attempt:
                return self._json(data, event, stage, {**original_payload,
                    "json_validation_feedback": "上次输出不是有效JSON。请重新生成完整严格JSON，所有字符串内的双引号和换行必须正确转义；不附代码围栏或说明。"}, _format_attempt=1, _output_scale=_output_scale)
            raise SessionError("模型输出格式两次未通过校验，已暂停；可重试，不会保存不完整结果。") from exc
        if not isinstance(result, dict):
            raise SessionError(f"{stage} 未返回有效结构")
        return restore(result)

    def create(self, direction: str, paper_limit: int = DEFAULT_PAPER_LIMIT) -> dict[str, Any]:
        with self.store.lock:
            self._ensure_capacity()
            budget = float(getattr(getattr(self.config, "hitl", None), "cost_budget_usd", 0) or 0)
            data = self.store.create(direction, budget, paper_limit)
            self.launch(data["session_id"])
            return self.snapshot(data["session_id"])

    def _ensure_capacity(self):
        if any(t.is_alive() for t, _ in self.jobs.values()):
            raise SessionError("已有调研任务正在运行，请完成或停止后重试。")

    def launch(self, sid: str) -> None:
        with self.store.lock:
            self._ensure_capacity()
            data = self.snapshot(sid)
            if data.get("run_id") or (data["phase"] in ("literature_review", "idea_review", "handed_off") and not data.get("download_task") and data.get('presentation_task')!='cards'):
                raise SessionError("当前阶段需要审核，不能跳过。")
            data.update(status="running", missing=[], blocker=None, unavailable_fulltexts={})
            self.store.save(data)
            event = threading.Event()
            thread = threading.Thread(target=self._work, args=(sid, event), daemon=True)
            self.jobs[sid] = thread, event
            thread.start()

    def _work(self, sid, event):
        data = self.snapshot(sid)
        self._sync_unlimited_budget(data)
        try:
            self.sources.progress = lambda paper, message: self._progress(data, event, message,
                paper_id=paper["paper_id"], title=paper["title"], operation="download")
            if data.get('presentation_task')=='cards':
                from .ideaspark import IdeaSparkBatch
                if (data.get('ideaspark_batch') or {}).get('workflow') == 'ideas_then_neighbors_v1':
                    from .idea_first import IdeaFirstBatch as IdeaSparkBatch
                from .parallel import run_candidates
                previous=data['card_presentation_return']
                host=IdeaSparkBatch(self,data,event)
                run_candidates(host,data['ideaspark_batch']['jobs'],'refresh',getattr(self.config.llm,'ideation_workers',3))
                data.update({k:v for k,v in previous.items() if k!='batch_phase'})
                data['ideaspark_batch']['phase']=previous['batch_phase']
                data.pop('presentation_task',None);data.pop('card_presentation_return',None)
                data.pop('progress',None);data.pop('progress_message',None)
                self._save(data,event)
            elif data.get("download_task"):
                previous = dict(data["download_task"])
                self._curate_fulltext_literature(data, event, attempt_id=data["download_task"]["attempt_id"])
                data.pop("download_task", None)
                data.update(status="waiting" if data.get("approved_revision") is None else previous["return_status"], missing=[])
                data.pop("progress", None)
                data.pop("progress_message", None)
                self._save(data, event)
            elif data["phase"] == "retrieval":
                self._retrieve(data, event)
            else:
                self._develop(data, event)
        except Cancelled:
            data.update(status="stopped", missing=["已停止，可从最近完成点继续。"])
            self.store.save(data)
        except Exception as exc:
            # No fallback cards, invented metadata or perfect novelty on search failure.
            category, code = getattr(exc, "category", "technical"), getattr(exc, "code", "stage_failed")
            actions = {"technical": ["重试当前阶段"], "evidence": ["补充原文", "调整文献范围"],
                       "scientific": ["补充文献", "调整研究方向"], "budget": ["调整成本预算"]}
            data.update(status="blocked", missing=[str(exc)[:1000]],
                blocker={"category": category, "code": code, "message": str(exc)[:1000], "actions": actions[category],
                         "context": getattr(exc, "context", {})})
            data.pop("progress_message", None)
            self.store.save(data)

    def stop(self, sid):
        with self.store.lock:
            data = self.snapshot(sid)
            job = self.jobs.get(sid)
            if data["status"] == "handoff":
                raise SessionError("正在交接科研，请等待交接结果后在正式项目停止。")
            if job and job[0].is_alive():
                job[1].set()
                data["status"] = "stopping"
            elif data.get("run_id"):
                raise SessionError("科研已启动，请在正式项目中停止。")
            else:
                data["status"] = "stopped"
            self.store.save(data)
            return data

    def _editable(self, data):
        if data["status"] in ("running", "stopping", "handoff"):
            raise SessionError("请先停止当前任务，等待停止完成后修改。")
        if data.get("run_id"):
            raise SessionError("已启动科研的会话不可修改。")

    def add_paper(self, sid, revision, row):
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            self.store.invalidate(data, "补充论文")
            data.pop("curation_version", None)
            row = {**row, "selected": True, "required": True, "relevance": "core",
                   "reason": "用户补充，请审核相关性", "summary_zh": ""}
            data["papers"] = [p for p in data["papers"] if p["paper_id"] != row["paper_id"]] + [row]
            if data.get("curation_candidate_ids") is not None and row["paper_id"] not in data["curation_candidate_ids"]:
                data["curation_candidate_ids"].append(row["paper_id"])
            self._select_shortlist(data)
            self.store.save(data)
            return data

    def remove_paper(self, sid, revision, paper_id):
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            paper = next((p for p in data["papers"] if p["paper_id"] == paper_id), None)
            if not paper:
                raise SessionError("论文不存在", 404)
            self.store.invalidate(data, "用户从相关文献中移除论文：" + paper_id)
            paper.update(user_excluded=True, selected=False, required=False, relevance="off_topic",
                         reason="用户认为不属于预期方向，已移出相关文献")
            self._select_shortlist(data)
            data.pop("progress", None)
            data.pop("progress_message", None)
            self.store.save(data)
            return data

    @staticmethod
    def paper_limit(data):
        if data.get("paper_limit"):
            return data["paper_limit"]
        # Preserve already-approved legacy scopes; new/unapproved surveys use the bounded default.
        if data.get("approved_revision") is not None:
            return max(DEFAULT_PAPER_LIMIT, sum(p.get("selected", False) for p in data["papers"]))
        return DEFAULT_PAPER_LIMIT

    def _related(self, data):
        related = [p for p in data["papers"] if p.get("relevance") in ("core", "adjacent")
                   and not p.get("user_excluded") and p.get("in_shortlist") is not False
                   and p.get("fulltext_status") in ("downloaded", "read")
                   and self._summary_material(data, p) is not None]
        return related[:self.paper_limit(data)]

    def _ranked_candidates(self, data):
        eligible = [p for p in data["papers"] if p.get("relevance") in ("core", "adjacent") and not p.get("user_excluded") and not p.get("is_retracted")]
        if data.get("curation_candidate_ids") is not None:
            eligible = [p for p in eligible if p["paper_id"] in data["curation_candidate_ids"]]
        ordered = [p for p in eligible if p.get("required")]
        for kind in ("core", "adjacent"):
            group = [p for p in eligible if p.get("relevance") == kind]
            recent = sorted(group, key=lambda p: -int(p.get("year") or 0))
            classic = sorted(group, key=lambda p: (-int(p.get("citation_count") or 0), int(p.get("year") or 0)))
            for pair in zip(recent, classic):
                for paper in pair:
                    if paper["paper_id"] not in {p["paper_id"] for p in ordered}:
                        ordered.append(paper)
        return ordered

    def _select_shortlist(self, data):
        limit = self.paper_limit(data)
        ranked = self._ranked_candidates(data)
        if sum(bool(p.get("required")) for p in ranked) > limit:
            raise SessionError("补充文献已超过本会话上限，请移除部分文献。", 400)
        eligible = [p for p in ranked if p.get("fulltext_status") in ("downloaded", "read")
                    and self._summary_material(data, p) is not None
                    and (p.get("selection") or {}).get("decision") == "include"]
        # Fulltext-grounded relevance and rigor precede citation counts or recency.
        eligible.sort(key=lambda p: (not p.get("required"),
            (p.get("selection") or {}).get("fit") != "direct",
            (p.get("selection") or {}).get("rigor") != "strong"))
        chosen = eligible[:limit]
        ids = {p["paper_id"] for p in chosen}
        for paper in data["papers"]:
            paper["in_shortlist"] = paper["paper_id"] in ids
            paper["selection_reason"] = (paper.get("selection") or {}).get("reason") or "尚未完成全文筛选"
        data.update(shortlist_ready=True, selection_stats={"candidates": len(data["papers"]), "selected": len(ids), "limit": limit})

    def _curate_fulltext_literature(self, data, event, attempt_id=None):
        """Only readable full texts with model-grounded selection enter the human review."""
        ranked = self._ranked_candidates(data)
        limit = self.paper_limit(data)
        old_ids = {p["paper_id"] for p in data["papers"] if p.get("in_shortlist")}
        data.pop("curation_version", None)
        attempt_id = attempt_id or f"scope-r{data['revision']}"
        ready = []
        # The retrieval candidate ceiling bounds network attempts even when many originals fail.
        start = 0
        while start < min(len(ranked), MAX_CANDIDATES) and len(ready) < limit:
            batch = ranked[start:start + min(4, limit - len(ready), MAX_CANDIDATES - start)]
            for index, paper in enumerate(batch, start + 1):
                self._check(data, event)
                if paper.get("fulltext_attempt_id") != attempt_id or self._summary_material(data, paper) is not None:
                    self._progress(data, event, "正在获取并筛选可阅读全文的论文", operation="download", unit="篇",
                                   current=index, total=min(len(ranked), MAX_CANDIDATES), title=paper["title"], paper_id=paper["paper_id"])
                    try:
                        self._obtain_fulltext(data, event, paper)
                        paper.pop("fulltext_error", None)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        paper.update(fulltext_status="failed", fulltext_error=str(exc)[:500])
                    paper["fulltext_attempt_id"] = attempt_id
                if self._summary_material(data, paper) is None:
                    paper.update(in_shortlist=False, selected=False, overview={}, summary_zh="", summary_basis=None,
                                 summary_state="unavailable", shortlist_exclusion="未取得可用于阅读的完整正文")
                self._save(data, event)
            start += len(batch)
            self._summarize_selected(data, event, papers=batch)
            ready += [p for p in batch if (p.get("selection") or {}).get("decision") == "include" and self._summary_material(data, p) is not None]
            self._select_shortlist(data)
            self._save(data, event)
        self._select_shortlist(data)
        new_ids = {p["paper_id"] for p in self._related(data)}
        if data["phase"] != "retrieval" and old_ids != new_ids:
            self.store.invalidate(data, "全文筛选改变文献范围")
            self._select_shortlist(data)
        self._archive_index(data)
        self._save(data, event)
        missing = [p["title"] for p in ranked if p.get("required") and p["paper_id"] not in new_ids]
        if missing:
            raise WorkflowFailure("指定论文未通过全文获取或正文检查，请补充原文：" + "；".join(missing), "evidence", "required_fulltext_missing")
        if not new_ids:
            raise WorkflowFailure("本轮没有取得既相关又能阅读全文的合格论文，请补充PDF或调整方向后重试。", "evidence", "no_readable_literature")
        data["curation_version"] = 1
        self._save(data, event)

    def acquire_fulltexts(self, sid, revision):
        """Curate readable originals; scope changes invalidate downstream approval."""
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            if data["phase"] == "retrieval":
                raise SessionError("请先完成文献筛选，再获取入选文献原文。")
            self._ensure_capacity()
            if not data.get("download_task"):
                data["download_task"] = {"attempt_id": now(), "return_status": data["status"],
                                         "return_missing": data["missing"]}
            self.store.save(data)
            self.launch(sid)
            return self.snapshot(sid)

    def _archive_paper(self, data, paper, document):
        """Store source formats, without synthesizing or rewriting paper content."""
        import os
        import shutil
        directory = self.store.directory(data["session_id"])
        asset = directory / "related work" / paper["paper_id"]
        asset.mkdir(parents=True, exist_ok=True)
        text = asset / "fulltext.txt"
        if paper.get("asset_document_hash") != fingerprint(document) or not text.exists():
            temporary = text.with_suffix(".tmp")
            temporary.write_text("\n\n".join(p["location"] + "\n" + p["text"] for p in document["pages"]))
            temporary.replace(text)
        files = {"extracted_text": str(text.relative_to(directory)), "latex_source": None}
        candidates = [directory / "downloads" / (paper["paper_id"] + ".pdf")]
        if paper.get("uploaded_pdf"):
            candidates.append(directory / paper["uploaded_pdf"])
        for source in candidates:
            if source.exists() and source.resolve().is_relative_to(directory.resolve()):
                target = asset / "paper.pdf"
                if not target.exists():
                    try:
                        os.link(source, target)
                    except OSError:
                        shutil.copy2(source, target)
                files["pdf"] = str(target.relative_to(directory))
                break
        paper.update(reading_assets=files, asset_document_hash=fingerprint(document))

    def _archive_index(self, data):
        directory = self.store.directory(data["session_id"]) / "related work"
        directory.mkdir(exist_ok=True)
        papers = [{k: p.get(k) for k in ("paper_id", "title", "year", "doi", "venue", "reading_assets", "overview", "selection_reason")}
                  for p in self._related(data)]
        target = directory / "index.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"revision": data["revision"], "papers": papers}, ensure_ascii=False, indent=2))
        temporary.replace(target)

    def _valid_document(self, paper, document):
        validate = getattr(self.sources, "validate_document", None)
        return readable_document(document) and (validate(paper, document) if callable(validate) else True)

    def _obtain_fulltext(self, data, event, paper):
        directory = self.store.directory(data["session_id"])
        cache = directory / "fulltext"
        cache.mkdir(exist_ok=True)
        target = cache / (paper["paper_id"] + ".json")
        self._progress(data, event, "正在读取已缓存原文" if target.exists() else "正在获取论文原文",
                       paper_id=paper["paper_id"], title=paper["title"], operation="download")
        document = json.loads(target.read_text()) if target.exists() else None
        if document is not None and not self._valid_document(paper, document):
            quarantine = directory / "rejected_fulltext"
            quarantine.mkdir(exist_ok=True)
            target.replace(quarantine / (paper["paper_id"] + ".json"))
            document = None
        if document is None:
            identity = fingerprint([paper.get(k) for k in ("paper_id", "url", "doi", "arxiv_id", "pdf_urls", "uploaded_pdf")])
            previous = data.get("unavailable_fulltexts", {}).get(identity)
            if previous:
                raise WorkflowFailure(previous["message"], previous["category"], previous["code"], copy.deepcopy(previous["context"]), recoverable=True)
            try:
                document = self.sources.fulltext(paper, directory)
            except Cancelled:
                raise
            except Exception as exc:
                failure = self._paper_failure(paper, exc, "获取全文")
                data.setdefault("unavailable_fulltexts", {})[identity] = {"message": str(failure), "category": failure.category,
                    "code": failure.code, "context": copy.deepcopy(failure.context)}
                raise failure from exc
        if sum(len(p["text"]) for p in document.get("pages", [])) < 2000 or not self._valid_document(paper, document):
            raise self._paper_failure(paper, SessionError("只取得摘要或不可用正文，未标记为已读。"), "核对正文")
        if not target.exists():
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(document, ensure_ascii=False))
            temp.replace(target)
        self._archive_paper(data, paper, document)
        paper.update(fulltext_status="read" if paper["paper_id"] in data["reading"] else "downloaded",
                     fulltext_url=document.get("source_url"))
        return document

    def _acquire_fulltexts(self, data, event, attempt_id=None):
        attempt_id = attempt_id or f"scope-r{data['revision']}"
        related = self._related(data)
        related.sort(key=lambda p: (p.get("relevance") != "core", -int(p.get("citation_count") or 0)))
        for index, paper in enumerate(related, 1):
            self._check(data, event)
            if paper.get("fulltext_attempt_id") == attempt_id:
                continue
            self._progress(data, event, "正在获取相关文献原文", current=index, total=len(related),
                           title=paper["title"], paper_id=paper["paper_id"], operation="download", unit="论文")
            try:
                self._obtain_fulltext(data, event, paper)
                paper.pop("fulltext_error", None)
            except Cancelled:
                raise
            except Exception as exc:
                paper.update(fulltext_status="abstract_only" if paper.get("abstract") else "failed",
                             fulltext_error=str(exc)[:500])
            paper["fulltext_attempt_id"] = attempt_id
            self._save(data, event)

    def refresh_cards(self,sid,revision):
        with self.store.lock:
            data=self.snapshot(sid)
            self.store.require_revision(data,revision);self._editable(data);self._ensure_capacity()
            if not data.get('ideaspark_batch'):
                raise SessionError('当前没有IdeaSpark候选卡片。')
            data['card_presentation_return']={k:copy.deepcopy(data.get(k)) for k in ('phase','status','blocker','missing')}
            data['card_presentation_return']['batch_phase']=data['ideaspark_batch'].get('phase','review')
            data['presentation_task']='cards';self.store.save(data);self.launch(sid)
            return self.snapshot(sid)

    def refresh_summaries(self, sid, revision):
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            self._ensure_capacity()
            if data["phase"] == "retrieval":
                raise SessionError("请先完成文献筛选，系统会自动获取材料并调用AI总结。", 409)
            previous = data["status"]
            self._sync_unlimited_budget(data)
            data.update(status="running", presentation_task="summaries")
            self.store.save(data)
            event = threading.Event()
            thread = threading.Thread(target=self._summary_work, args=(sid, event, previous), daemon=True)
            self.jobs[sid] = thread, event
            thread.start()
            return self.snapshot(sid)

    def _summary_work(self, sid, event, previous):
        data = self.snapshot(sid)
        try:
            self._summarize_selected(data, event, force=True, presentation_only=True)
            data["status"] = previous
            if (data.get("blocker") or {}).get("category") == "budget" and data["budget_usd"] <= 0:
                data.update(status="waiting" if data["phase"] in ("literature_review", "idea_review") else "interrupted", missing=[], blocker=None)
        except Cancelled:
            data.update(status="stopped")
        except Exception as exc:
            category = getattr(exc, "category", "technical")
            data.update(status="blocked", missing=[str(exc)[:1000]], blocker={"category": category,
                "code": getattr(exc, "code", "summary_failed"), "message": str(exc)[:1000],
                "actions": ["重试全文解读"], "context": getattr(exc, "context", {})})
        data.pop("presentation_task", None)
        data.pop("progress_message", None)
        data.pop("progress", None)
        self.store.save(data)

    def _summary_material(self, data, paper):
        path = self.store.directory(data["session_id"]) / "fulltext" / (paper["paper_id"] + ".json")
        if path.exists():
            document = json.loads(path.read_text())
            pages = document.get("pages") or []
            size = sum(len(p.get("text", "")) for p in pages)
            if size >= 2000 and self._valid_document(paper, document):
                return {"source_type": "fulltext", "pages": pages}
        return None

    @staticmethod
    def _summary_excerpts(paper_id, material):
        """Lossless source segmentation; these are excerpts, never generated summaries."""
        return [{"passage_id": paper_id + ":overview:" + fingerprint([page["location"], start, page["text"][start:start + 160]])[:16],
                 "location": page["location"], "text": page["text"][start:start + 160]}
                for page in material["pages"] for start in range(0, len(page["text"]), 160)]

    def _summarize_selected(self, data, event, force=False, papers=None, presentation_only=False):
        """DeepSeek writes every content field; code selects sources and validates structure only."""
        tasks = []
        for paper in (self._related(data) if papers is None else papers):
            if paper["paper_id"] in data["reading"] and not presentation_only:
                continue
            material = self._summary_material(data, paper)
            if material is None:
                self._apply_summary(paper, {})
                continue
            self._archive_paper(data, paper, json.loads((self.store.directory(data["session_id"]) / "fulltext" / (paper["paper_id"] + ".json")).read_text()))
            source_hash = fingerprint(material)
            if not force and paper.get("summary_source_hash") == source_hash and paper.get("summary_prompt_hash") == fingerprint(PROMPTS["summarize"]) and paper.get("summary_version") == SUMMARY_VERSION:
                continue
            tasks.append((paper, material, source_hash))
        completed = 0
        while tasks:
            batch = [tasks.pop(0)]
            self._progress(data, event, "正在由DeepSeek阅读材料并撰写论文概览", operation="summarize",
                           current=completed + 1, total=completed + len(batch) + len(tasks), unit="篇",
                           title=batch[0][0]["title"], paper_id=batch[0][0]["paper_id"])
            payload = {"direction": data["direction"], "presentation_only": presentation_only, "papers": [{"paper_id": p["paper_id"], "title": p["title"],
                                   "year": p.get("year"), "venue": p.get("venue"), "venue_type": p.get("venue_type"),
                                   "material": {"source_type": "fulltext", "passages": self._summary_excerpts(p["paper_id"], material)}}
                                   for p, material, _ in batch]}
            if input_bytes(payload) > self._input_limit("summarize"):
                paper, material, _ = batch[0]
                document = json.loads((self.store.directory(data["session_id"]) / "fulltext" / (paper["paper_id"] + ".json")).read_text())
                note = self._read_document(data, event, paper, document)
                payload["papers"][0]["material"] = {"source_type": "fulltext", "coverage": note["coverage"],
                    "reading_notes": {k: v for k, v in note.items() if k not in ("passages",)},
                    "passages": [c for c in self._summary_excerpts(paper["paper_id"], material) if any(
                        c["location"] == e["location"] and (c["text"] in e["quote"] or e["quote"] in c["text"]) for e in note["evidence"])]}
            for attempt in range(2):
                result = self._json(data, event, "summarize", payload)
                rows = result.get("papers") or []
                by_id = {p["paper_id"]: (p, material, source_hash) for p, material, source_hash in batch}
                errors = []
                valid_ids = len(rows) == len(batch) and {p.get("paper_id") for p in rows} == set(by_id)
                if valid_ids:
                    for row in rows:
                        paper, material, _ = by_id[row["paper_id"]]
                        try:
                            self._apply_summary(copy.deepcopy(paper), row, material, preserve_selection=presentation_only)
                        except SessionError as exc:
                            errors.append({"paper_id": row["paper_id"], "error": str(exc),
                                           "invalid_evidence": (row.get("selection") or {}).get("evidence", [])})
                if valid_ids and not errors:
                    break
                if attempt:
                    raise WorkflowFailure("AI论文概览返回不完整，已保留此前结果，可重试本步。", "technical", "invalid_paper_overview")
                payload["validation_feedback"] = {"errors": errors, "required_ids": list(by_id),
                    "instruction": "重新返回完整JSON。概览每项不超过160字符，用普通人能懂的短句。selection.evidence_refs只需选择本篇1–2个能支持判断的输入passage_id，不自己抄写引文或编造页码；服务器会展示对应原文。材料未说明的内容写未报告。"}
            for row in rows:
                paper, material, source_hash = by_id[row["paper_id"]]
                self._apply_summary(paper, row, material, preserve_selection=presentation_only)
                paper.update(summary_source_hash=source_hash, summary_prompt_hash=fingerprint(PROMPTS["summarize"]))
            completed += len(batch)
            self._save(data, event)
        self._archive_index(data)
        self._save(data, event)

    @staticmethod
    def _apply_summary(paper, info, material=None, preserve_selection=False):
        basis = material.get("source_type") if material else None
        if not basis:
            paper.update(overview={}, summary_zh="", summary_basis=None, summary_state="awaiting_source", summary_version=SUMMARY_VERSION)
            paper.pop("summary_source_hash", None)
            return
        overview = info.get("overview") or {}
        if not isinstance(info.get("summary_zh"), str) or not info["summary_zh"].strip() or not all(
                isinstance(overview.get(k), str) and overview[k].strip() for k in OVERVIEW_FIELDS):
            raise WorkflowFailure("AI论文概览返回不完整，已保留此前结果，可重试本步。", "technical", "invalid_paper_overview")
        if any(len(overview[k]) > 160 for k in OVERVIEW_FIELDS):
            raise SessionError("概览太长：每项用零基础读者能懂的1–2个短句，硬上限160字符；删掉算法清单、参数与长流程，用普通话保留核心意思。")
        if preserve_selection:
            paper.update(overview=overview, summary_zh=info["summary_zh"], summary_basis=basis,
                         summary_state="ready", summary_version=SUMMARY_VERSION)
            return
        selection = info.get("selection") or {}
        if selection.get("decision") not in ("include", "exclude") or selection.get("fit") not in ("direct", "supporting", "off_topic") or selection.get("rigor") not in ("strong", "adequate", "weak", "unclear") or not selection.get("reason"):
            raise SessionError("缺少基于正文的相关性与方法质量判断。")
        if "evidence_refs" in selection:
            refs = selection["evidence_refs"]
            excerpts = {p["passage_id"]: p for p in IdeationService._summary_excerpts(paper["paper_id"], material)}
            if not isinstance(refs, list) or not 1 <= len(refs) <= 2 or any(not isinstance(ref, str) or ref not in excerpts for ref in refs):
                raise SessionError("selection.evidence_refs必须选择本篇输入中1–2个真实原文片段ID，不能引用其他论文或未知编号。")
            selection["evidence"] = [{"passage_id": ref, "location": excerpts[ref]["location"], "quote": excerpts[ref]["text"]} for ref in refs]
        evidence = selection.get("evidence") or []
        pages = material.get("pages", [])
        if not evidence:
            raise SessionError("selection.evidence缺少原文引文。")
        for index, item in enumerate(evidence, 1):
            if not isinstance(item, dict) or not isinstance(item.get("quote"), str) or not 12 <= len(item["quote"]) <= 200:
                raise SessionError(f"selection第{index}条引文必须为12–200字符；请只摘一个连续短句，不复制整段。")
            matches = [page for page in pages if quoted(item["quote"]) in quoted(page.get("text", ""))]
            if not any(item.get("location") == page.get("location") for page in matches) and len(matches) == 1:
                item.update(model_location=item.get("location"), location=matches[0]["location"],
                            citation_resolution="unique_exact_quote")
            if not any(item.get("location") == page.get("location") for page in matches):
                raise SessionError(f"selection第{index}条引文未在对应页码逐字匹配。请重新从原文复制连续短句，保留断行连字符。")
        if selection["decision"] == "include" and (selection["fit"] == "off_topic" or selection["rigor"] in ("weak", "unclear")):
            raise SessionError("相关性或方法依据不足的论文不能入选。")
        paper["selection"] = selection
        paper.update(overview=overview, summary_zh=info["summary_zh"], summary_basis=basis,
                     summary_state="ready", summary_version=SUMMARY_VERSION)

    def revise_scope(self, sid, revision, direction):
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            if not 4 <= len(direction.strip()) <= 4000:
                raise SessionError("研究方向须为4–4000字符", 400)
            self.store.invalidate(data, "修改研究范围")
            data.update(direction=direction.strip(), papers=[], search_log=[], completed_queries=[], phase="retrieval", feedback="")
            data.pop("plan", None)
            data.pop("citation_completed", None)
            data.pop("citation_seed_ids", None)
            data.pop("curation_candidate_ids", None)
            self.store.save(data)
            self.launch(sid)
            return self.snapshot(sid)

    def confirm_direction(self, sid, revision, feedback=""):
        """Human confirms the direction; the agent owns paper selection and deep reading."""
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            if data["phase"] != "literature_review":
                raise SessionError("请等待相关文献调研完成后确认研究方向。")
            related = self._related(data)
            if data.get("curation_version") != 1:
                raise SessionError("请先点击整理全文文献，完成原文与方法质量筛选。")
            if not related:
                raise SessionError("暂未找到相关文献，请调整研究方向后重新调研。")
            return self.review_literature(sid, revision, [p["paper_id"] for p in related],
                [p["paper_id"] for p in related if p.get("uploaded_pdf") or p.get("required")],
                feedback, selection_by="agent")

    def refresh_metadata(self, sid, revision):
        """Upgrade pending bibliographies without approving the direction or starting reading."""
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            if data["phase"] != "literature_review":
                raise SessionError("仅可更新待确认方向的文献信息。")
            self._ensure_capacity()
            data.update(status="running", progress_message="正在整理论文来源、作者与机构")
            self.store.save(data)
            event = threading.Event()
            thread = threading.Thread(target=self._metadata_work, args=(sid, event), daemon=True)
            self.jobs[sid] = thread, event
            thread.start()
            return self.snapshot(sid)

    def _metadata_work(self, sid, event):
        data = self.snapshot(sid)
        try:
            self._enrich_papers(data, event)
            self._check(data, event)
            data.update(status="waiting", missing=[])
        except Cancelled:
            data.update(status="stopped")
        except Exception as exc:
            data.update(status="blocked", missing=[str(exc)[:1000]])
        data.pop("progress_message", None)
        self.store.save(data)

    def review_literature(self, sid, revision, selected, required, feedback="", *, selection_by="user"):
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            ids = {p["paper_id"] for p in self._related(data)}
            if data.get("curation_version") != 1:
                raise SessionError("须先完成全文与方法质量筛选。")
            pinned = {p["paper_id"] for p in data["papers"] if p.get("required") and not p.get("user_excluded")}
            if not pinned <= set(selected):
                raise SessionError("指定论文尚未通过全文筛选，不能跳过；请补充原文或明确移除该论文。")
            if len(selected) > self.paper_limit(data):
                raise SessionError("保留文献数量超过本会话上限。", 400)
            if not selected or not set(selected) <= ids or not set(required) <= set(selected):
                raise SessionError("只能确认通过全文筛选的论文；指定论文也必须有可读正文。", 400)
            if len(required) > 15:
                raise SessionError("必读论文最多15篇，请缩小范围。", 400)
            self._ensure_capacity()
            self.store.invalidate(data, "确认文献范围")
            for paper in data["papers"]:
                paper.update(selected=paper["paper_id"] in selected, required=paper["paper_id"] in required)
            data.update(approved_revision=data["revision"], phase="reading", status="pending", feedback=feedback)
            data["reviews"].append({"type": "literature", "revision": data["revision"], "at": now(),
                                    "selected": selected, "required": required, "feedback": feedback,
                                    "selection_by": selection_by,
                                    "decision": "direction_confirmed" if selection_by == "agent" else "paper_selection_confirmed"})
            self.store.save(data)
            self.launch(sid)
            return self.snapshot(sid)

    def regenerate(self, sid, revision, feedback):
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            if data.get("approved_revision") != data["revision"]:
                raise SessionError("请先完成文献审核。")
            if not data.get("synthesis") or len([r for r in data["reading"].values() if not r.get("supplemental")]) < 3:
                raise SessionError("全文精读和有证据支撑的问题定位尚未完成，不能跳过这些阶段生成创新点。")
            self._ensure_capacity()
            self._prepare_refinement(data, feedback)
            self.store.save(data)
            self.launch(sid)
            return self.snapshot(sid)

    def _prepare_refinement(self, data, feedback, automatic=False):
        revision = data["revision"]
        lessons = data.get("prior_rejections", []) + [{"title": c.get("title"), "method": c.get("method"),
            "reason": c.get("rejection_reason"), "audit": c.get("audit"), "failure": c.get("failure")}
            for c in data["rejected_cards"]]
        prior_rejections = list({fingerprint([c.get("title"), c.get("method"), c.get("reason")]): c for c in lessons}.values())[-12:]
        data["reviews"].append({"type": "auto_refine" if automatic else "regenerate", "at": now(), "feedback": feedback,
            "revision": revision, "cards": copy.deepcopy(data["cards"]),
            "rejected_cards": copy.deepcopy(data["rejected_cards"]),
            "synthesis": copy.deepcopy(data["synthesis"]), "inherited_literature_approval": revision,
            "ideaspark_batch": copy.deepcopy(data.get("ideaspark_batch")), "idea_pool":copy.deepcopy(data.get("idea_pool",[]))})
        data.pop('ideaspark_batch',None);data.pop('idea_pool',None)
        data["revision"] += 1
        data.update(approved_revision=data["revision"], cards=[], rejected_cards=[], synthesis=None,
                    phase="synthesis" if automatic else "reading", refresh_evidence=not automatic,
                    prior_rejections=prior_rejections, feedback=feedback, missing=[], blocker=None,
                    refinement_round=data.get("refinement_round", 0) + 1 if automatic else 0)

    def resume(self, sid, revision):
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            if data["phase"] in ("literature_review", "idea_review") and not data.get("download_task"):
                raise SessionError("当前需要人工审核，请使用对应审核按钮。")
            if data["phase"] == "reading" and data["status"] == "blocked" and not data.get("download_task"):
                for paper in data["papers"]:
                    if paper.get("fulltext_status") in ("abstract_only", "failed"):
                        paper.pop("fulltext_attempt_id", None)
            if data["phase"] == "audit" and not data.get("download_task"):
                data["rejected_cards"] = []  # Retry failed evidence fetches; keep successful receipts.
            if data["phase"] == "retrieval" and not any(p.get("relevance") == "core" for p in data["papers"]):
                data["completed_queries"] = []  # Retry recovered providers, even when another returned only noise.
            data["budget_usd"] = float(getattr(getattr(self.config, "hitl", None), "cost_budget_usd", 0) or 0)
            self.store.save(data)
            self.launch(sid)
            return self.snapshot(sid)

    def _search(self, data, event, query, period="all"):
        all_rows = []
        general_records = False
        configured = getattr(getattr(self.config, "literature_search", None), "sources", None)
        for source in configured or ["openalex", "semantic_scholar", "arxiv"]:
            self._check(data, event)
            self._progress(data, event, "正在检索文献来源：" + source, operation="search", source=source, query=query)
            try:
                rows = self.sources.search(query, period, source)
                outcome = "records" if rows else "empty_or_unavailable"
            except Exception:
                rows, outcome = [], "unavailable"
            data["search_log"].append({"query": query, "period": period, "source": source,
                "at": now(), "count": len(rows), "outcome": outcome})
            for row in rows:
                row.setdefault("discovery_relation", "classic_search" if period == "classic" else "recent_search" if period == "recent" else "topic_search")
            all_rows.extend(rows)
            if source in ("openalex", "semantic_scholar") and rows:
                general_records = True
            self._save(data, event)
        if not general_records:
            self._check(data, event)
            try:
                rows = self.sources.search(query, period, "crossref")
            except Exception:
                rows = []
            data["search_log"].append({"query": query, "period": period, "source": "crossref",
                "at": now(), "count": len(rows), "outcome": "records" if rows else "empty_or_unavailable"})
            all_rows.extend(rows)
            self._save(data, event)
        return all_rows

    @staticmethod
    def _merge(rows):
        merged = {}
        for paper in rows:
            key = paper.get("doi") or re.sub(r"\W", "", paper["title"].lower())
            if key in merged:
                old = merged[key]
                old["provenance"].extend(paper.get("provenance", []))
                if paper.get("discovery_relation"):
                    old["discovery_routes"] = sorted(set(old.get("discovery_routes", []) + [paper["discovery_relation"]]))
                if not old.get("abstract"):
                    old["abstract"] = paper.get("abstract", "")
            else:
                merged[key] = copy.deepcopy(paper)
        return list(merged.values())

    def _screen(self, data, event, rows, direction, stage="screen"):
        # Short request-local aliases eliminate hash-copy mistakes without weakening identity checks.
        mapping = {f"P{i + 1}": p["paper_id"] for i, p in enumerate(rows)}
        payload = {"direction": direction, "papers": [{"paper_id": alias,
            **{k: p.get(k) for k in ("title", "year", "abstract")}}
            for alias, p in zip(mapping, rows)]}
        for attempt in range(2):
            response = self._json(data, event, stage, payload)
            output = response.get("papers") or []
            valid_relations = stage != "neighbors" or all(p.get("relationship") in
                ("same_claim", "same_mechanism", "alternative_mechanism", "background") for p in output)
            valid_ids = len(output) == len(mapping) and {p.get("paper_id") for p in output} == set(mapping)
            errors = []
            if valid_ids:
                for item in output:
                    if item.get("relevance") not in ("core", "adjacent", "off_topic") or not item.get("reason"):
                        errors.append(item["paper_id"] + "缺少有效相关性或理由")
            if valid_relations and valid_ids and not errors:
                return [{**p, "paper_id": mapping[p["paper_id"]]} for p in output]
            payload["validation_feedback"] = "请完整返回全部给定ID：" + ", ".join(mapping) + "。" + "；".join(errors)
        raise WorkflowFailure("文献筛选返回格式经修正仍不完整，已保留此前结果，可重试本步。", "technical", "invalid_paper_screen")

    def _expand_citations(self, data, event, rows, candidate_limit):
        expand = getattr(self.sources, "citation_neighbors", None)
        if not callable(expand) or len(rows) >= candidate_limit:
            return rows
        if "citation_seed_ids" not in data:
            core = [p for p in rows if p.get("relevance") == "core" and not p.get("user_excluded")]
            classic = sorted(core, key=lambda p: -int(p.get("citation_count") or 0))
            recent = sorted(core, key=lambda p: -int(p.get("year") or 0))
            seed_ids = []
            for pair in zip(classic, recent):
                for paper in pair:
                    if paper["paper_id"] not in seed_ids and len(seed_ids) < 3:
                        seed_ids.append(paper["paper_id"])
            data["citation_seed_ids"] = seed_ids
        by_id = {p["paper_id"]: p for p in rows}
        seeds = [by_id[pid] for pid in data["citation_seed_ids"] if pid in by_id]
        completed = data.setdefault("citation_completed", [])
        for seed in seeds:
            if seed["paper_id"] in completed:
                continue
            self._progress(data, event, "正在追踪关键论文的经典来源与后续研究", operation="citations", title=seed["title"], paper_id=seed["paper_id"])
            try:
                found = expand(seed, limit=8)
                outcome = "records" if found else "empty_or_unavailable"
            except Exception:
                found, outcome = [], "unavailable"
            known = {p["paper_id"] for p in rows}
            accepted = []
            for paper in found:
                if paper["paper_id"] not in known and len(rows) + len(accepted) < candidate_limit and len(accepted) < 4:
                    known.add(paper["paper_id"]); accepted.append(paper)
            rows = self._merge(rows + accepted)
            data["search_log"].append({"query": "引用追踪：" + seed["title"], "period": "citation", "source": "openalex", "seed_id": seed["paper_id"], "at": now(), "count": len(found), "accepted": len(accepted), "outcome": outcome})
            completed.append(seed["paper_id"])
            data["papers"] = rows
            self._save(data, event)
        return rows

    def _screen_pending(self, data, event, rows):
        pending = [p for p in rows if p.get("relevance") == "unreviewed"]
        for start in range(0, len(pending), 8):
            batch = pending[start:start + 8]
            done = sum(p.get("relevance") != "unreviewed" for p in rows)
            self._progress(data, event, "正在筛选文献相关性", operation="screen", unit="篇", current=done, total=len(rows), title="", paper_id="", source="", query="")
            screened = {p["paper_id"]: p for p in self._screen(data, event, batch, data["direction"] + "\n" + data["plan"]["interpretation"])}
            for paper in batch:
                info = screened[paper["paper_id"]]
                paper.update(relevance=info["relevance"], reason=info["reason"], selected=info["relevance"] == "core")
            self._progress(data, event, "已完成本组文献筛选", operation="screen", unit="篇", current=done + len(batch), total=len(rows))

    def _retrieve(self, data, event):
        if not data.get("plan"):
            plan = self._json(data, event, "plan", {"direction": data["direction"]})
            queries = plan.get("queries") or []
            if not plan.get("interpretation") or not 4 <= len(queries) <= 6 or any(not q.get("query") for q in queries):
                raise SessionError("检索规划不完整，请重试或补充研究范围。")
            if not any(q.get("period") == "classic" for q in queries):
                queries[0]["period"] = "classic"
            if not any(q.get("period") == "recent" for q in queries):
                queries[-1]["period"] = "recent"
            data["retrieval_strategy_version"] = 2
            data["plan"] = plan
            self._save(data, event)
        data.setdefault("paper_limit", DEFAULT_PAPER_LIMIT)
        candidate_limit = min(MAX_CANDIDATES, self.paper_limit(data) * 3)
        rows = list(data["papers"])
        if len(rows) > candidate_limit:
            ranked = sorted(rows, key=lambda p: (not p.get("required"), p.get("relevance") == "unreviewed"))
            data.setdefault("excluded_candidates", []).extend({**p, "exclusion_reason": "超出检索候选上限"} for p in ranked[candidate_limit:])
            rows = ranked[:candidate_limit]
            data["papers"] = rows
        completed = data.setdefault("completed_queries", [])
        queries = data["plan"]["queries"]
        reserve = min(12, candidate_limit // 3) if callable(getattr(self.sources, "citation_neighbors", None)) else 0
        direct_limit = candidate_limit - reserve
        per_query = max(1, (direct_limit + len(queries) - 1) // len(queries))
        for query_index, query in enumerate(queries, 1):
            if query in completed:
                continue
            self._progress(data, event, "正在检索：" + query["query"], operation="search",
                           current=query_index, total=len(queries), unit="检索式", title="", paper_id="")
            found = self._search(data, event, query["query"], query.get("period", "all"))
            existing = {p["paper_id"] for p in rows}
            new_ids = set()
            accepted = []
            for paper in found:
                pid = paper["paper_id"]
                if pid in existing or pid in new_ids:
                    accepted.append(paper)
                elif len(new_ids) < per_query and len(rows) + len(new_ids) < direct_limit:
                    new_ids.add(pid)
                    accepted.append(paper)
            rows = self._merge(rows + accepted)
            data["papers"] = rows
            # Empty search must be retryable, not a permanently completed query.
            if found:
                completed.append(query)
            self._save(data, event)
        if not rows:
            raise SessionError("检索未取得真实论文：可能限流、网络不可用或检索范围不合适。请重试、改方向或补充DOI/PDF。")
        self._screen_pending(data, event, rows)
        rows = self._expand_citations(data, event, rows, candidate_limit)
        self._screen_pending(data, event, rows)
        if not any(p["relevance"] == "core" for p in rows):
            raise SessionError("未筛选出核心相关论文，请修改范围或补充文献；不会补入无关论文凑数。")
        self._select_shortlist(data)
        self._save(data, event)
        self._enrich_papers(data, event)
        self._curate_fulltext_literature(data, event)
        self._enrich_papers(data, event)
        data.pop("progress_message", None)
        data.pop("progress", None)
        data.update(phase="literature_review", status="waiting", missing=[])
        self._save(data, event)

    def _enrich_papers(self, data, event):
        enrich = getattr(self.sources, "enrich_metadata", None)
        if enrich is None:
            return
        papers = [p for p in (self._related(data) or self._ranked_candidates(data)[:self.paper_limit(data)]) if p.get("bibliography_version") != 1]
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {pool.submit(enrich, p): p for p in papers}
            for future in as_completed(pending):
                self._check(data, event)
                try:
                    pending[future].update(future.result())
                except Exception:
                    pass  # Missing publisher fields remain missing; never ask the model to invent them.
                self._save(data, event)

    def _read_paper(self, data, event, paper, extra=False):
        key = paper["paper_id"]
        if key in data["reading"]:
            path = self.store.directory(data["session_id"]) / "fulltext" / (key + ".json")
            if not path.exists() or not self._valid_document(paper, json.loads(path.read_text())) or fingerprint(json.loads(path.read_text())) != data["reading"][key]["source_hash"]:
                data["reading"].pop(key)
                raise WorkflowFailure("原精读记录对应的材料不是可读全文，请先修复正文并重新核对证据。", "evidence", "invalid_cached_fulltext")
            return data["reading"][key]
        if data["phase"] == "reading" and paper.get("fulltext_attempt_id") == f"scope-r{data['revision']}" and paper.get("fulltext_status") in ("abstract_only", "failed"):
            raise SessionError(paper.get("fulltext_error") or "本轮未获取原文，请补充材料或稍后重试。")
        document = self._obtain_fulltext(data, event, paper)
        try:
            result = self._read_document(data, event, paper, document)
        except Cancelled:
            raise
        except Exception as exc:
            raise self._paper_failure(paper, exc, "核对方法证据") from exc
        result["supplemental"] = extra
        data["reading"][key] = result
        paper["fulltext_status"] = "read"
        # Detailed evidence notes must not overwrite the reader-facing, AI-written overview.
        self._save(data, event)
        return result

    def _read_request(self, data, event, paper, stage, payload, chunks, required_methods, checkpoint):
        signature = fingerprint([payload, PROMPTS[stage]])
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_text())
            if saved.get("input_hash") == signature:
                return saved["result"]
        by_id = {p["passage_id"]: p for p in chunks}
        for attempt in range(2):
            result = self._json(data, event, stage, payload)
            evidence = result.get("evidence") or []
            fields = ("problem", "method", "data", "experiments", "findings", "strengths", "limitations")
            invalid = []
            for item in evidence:
                if not isinstance(item, dict):
                    invalid.append(item); continue
                source = by_id.get(item.get("passage_id"))
                quote = item.get("quote")
                if quote and isinstance(quote, str) and len(quote) >= 12 and (not source or quoted(quote) not in quoted(source["text"])):
                    matches = [c for c in chunks if quoted(quote) in quoted(c["text"])]
                    if len(matches) == 1:
                        source = matches[0]
                        item.update(model_passage_id=item.get("passage_id"), passage_id=source["passage_id"], citation_resolution="unique_exact_quote")
                if not source or item.get("kind") not in ("source", "inference") or item.get("role") not in ("method", "result", "limitation", "context") or not item.get("claim"):
                    invalid.append(item); continue
                if quote is None or quote == "":
                    item.update(quote=source["text"], citation_resolution="source_passage")
                elif not isinstance(quote, str) or len(quote) < 12 or quoted(quote) not in quoted(source["text"]):
                    invalid.append(item); continue
                item.update(location=source["location"], paper_id=paper["paper_id"])
            valid = all(isinstance(result.get(k), str) and result[k].strip() for k in fields) and isinstance(result.get("has_methods"), bool)
            method = any(isinstance(e, dict) and e.get("kind") == "source" and e.get("role") == "method" for e in evidence)
            if valid and not invalid and (not required_methods or (result["has_methods"] and method)):
                break
            if attempt:
                if valid and not invalid and required_methods and (not result["has_methods"] or not method):
                    raise WorkflowFailure("已处理全文，但未取得方法正文证据。", "evidence", "no_method_evidence")
                raise WorkflowFailure("论文证据标注校正后仍未通过，请重试本篇的自动核对。", "technical", "invalid_read_evidence")
            payload["validation_feedback"] = {"invalid_evidence": invalid,
                "instruction": "完整返回规定字段。只使用本次输入的passage_id，标明claim/kind/role；不要抄写quote，服务器会按ID取原文。完整论文须有方法正文证据；分段未涉及的信息明确写该分段未说明，不能借用其他论文。"}
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = checkpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps({"input_hash": signature, "paper_id": paper["paper_id"], "result": result}, ensure_ascii=False))
        temporary.replace(checkpoint)
        self._save(data, event)
        return result

    def _read_document(self, data, event, paper, document):
        try:
            return self._read_document_contents(data, event, paper, document)
        except Cancelled:
            raise
        except Exception as exc:
            raise self._paper_failure(paper, exc, "核对方法证据") from exc

    def _read_document_contents(self, data, event, paper, document):
        key = paper["paper_id"]
        chunks = passages(key, document)
        size = sum(len(c["text"]) for c in chunks)
        if size < 2000:
            raise SessionError("正文不足以核对方法证据，系统将继续寻找可用全文。")
        version = fingerprint([fingerprint(document), PROMPTS["read"], PROMPTS["read_part"], PROMPTS["read_merge"]])[:20]
        directory = self.store.directory(data["session_id"]) / "reading_parts" / key / version
        groups = pack(chunks, self._input_limit("read_part"))
        if input_bytes({"paper": paper["title"], "paper_id": key, "passages": chunks}) <= self._input_limit("read"):
            groups = [chunks]
        notes = []
        for index, group in enumerate(groups, 1):
            self._check(data, event)
            self._progress(data, event, "正在逐篇核对原文依据" if len(groups) == 1 else "正在自动分段阅读长论文",
                           operation="read", paper_id=key, title=paper["title"], part_current=index, part_total=len(groups), source_characters=size)
            stage = "read" if len(groups) == 1 else "read_part"
            payload = {"paper": paper["title"], "paper_id": key, "part": index, "parts": len(groups), "passages": group}
            note = self._read_request(data, event, paper, stage, payload, group, len(groups) == 1, directory / f"part-{index:04}.json")
            notes.append(note)
        # Merge notes from this paper only. Recursive groups also support exceptionally long books.
        all_sources = {c["passage_id"]: c for c in chunks}
        while len(notes) > 1:
            records = []
            for note in notes:
                refs = dict.fromkeys(e["passage_id"] for e in note["evidence"])
                records.append({"note": note, "passages": [all_sources[pid] for pid in refs]})
            grouped = pack(records, self._input_limit("read_merge"))
            if len(grouped) == len(notes):
                raise WorkflowFailure("分段汇总配置过小，无法合并阅读记录。", "technical", "read_merge_capacity")
            merged = []
            for group in grouped:
                sources = {c["passage_id"]: c for r in group for c in r["passages"]}
                payload = {"paper": paper["title"], "paper_id": key, "notes": [r["note"] for r in group], "passages": list(sources.values())}
                self._progress(data, event, "正在汇总同一篇论文的分段阅读记录", operation="read", paper_id=key, title=paper["title"], part_current=len(groups), part_total=len(groups))
                merged.append(self._read_request(data, event, paper, "read_merge", payload, list(sources.values()),
                    any(r["note"].get("has_methods") for r in group), directory / ("merge-" + fingerprint(payload)[:20] + ".json")))
            notes = merged
        result = notes[0]
        if not result.get("has_methods") or not any(e.get("role") == "method" and e.get("kind") == "source" for e in result["evidence"]):
            raise WorkflowFailure("已处理全文，但未取得方法正文依据。", "evidence", "no_method_evidence")
        result.update(paper_id=key, title=paper["title"], passages=chunks, source_hash=fingerprint(document), source_url=document.get("source_url"),
            coverage={"complete": True, "paper_id": key, "source_characters": size, "processed_characters": size,
                      "parts": len(groups), "context_limit_tokens": self._context_capacity()})
        return result

    def _evidence(self, data):
        return {c["passage_id"]: c for r in data["reading"].values() for c in r["passages"]}

    def _develop(self, data, event):
        while True:
            try:
                self._develop_round(data, event)
                return
            except CandidateRoundFailure:
                rejected = data["rejected_cards"]
                if not rejected:
                    raise WorkflowFailure("现有证据未形成可审查候选，请补充文献或调整方向。", "evidence", "no_supported_candidate")
                non_scientific = next((c["failure"] for c in rejected if c["failure"]["category"] != "scientific" and not c["failure"].get("recoverable")), None)
                if non_scientific:
                    raise WorkflowFailure(non_scientific["message"], non_scientific["category"], non_scientific["code"], non_scientific.get("context"))
                previous = {x.get("failure", {}).get("lesson_key") for x in data.get("prior_rejections", []) if x.get("failure")}
                new_lessons = {c["failure"]["lesson_key"] for c in rejected} - previous
                if not new_lessons:
                    if any(c["failure"].get("recoverable") for c in rejected):
                        raise WorkflowFailure("本轮候选均已处理，暂未得到证据完整的方案；具体缺失论文和受影响候选见处理记录。", "evidence", "candidate_evidence_incomplete")
                    raise WorkflowFailure("新一轮仍重复已被否决的机制，未获得新增依据，已暂停自动修订。", "scientific", "no_new_information")
                if data.get("refinement_round", 0) >= MAX_AUTO_REFINEMENTS:
                    if any(c["failure"].get("recoverable") for c in rejected):
                        raise WorkflowFailure("已完成三轮候选构思与审查，暂未得到证据完整的方案；缺失论文和其他审查结论已汇总在处理记录中。", "evidence", "candidate_evidence_incomplete")
                    raise WorkflowFailure("三轮构思与审查仍未形成可审核候选，已保留全部否决依据，请补充文献或调整方向。", "scientific", "refinement_exhausted")
                self._prepare_refinement(data, data["feedback"], automatic=True)
                self._progress(data, event, "正在根据审查依据自动修订方案", operation="refine",
                               current=data["refinement_round"] + 1, total=MAX_AUTO_REFINEMENTS + 1, unit="轮次")

    def _develop_round(self, data, event):
        if data.get("approved_revision") != data["revision"]:
            raise SessionError("文献版本尚未批准。")
        if data.get('ideaspark_batch') and data['phase'] in ('generate','audit'):
            from .idea_first import IdeaFirstBatch as IdeaSparkBatch
            IdeaSparkBatch(self,data,event).run()
            return
        if data["phase"] == "reading":
            if not data.get("refresh_evidence"):
                self._acquire_fulltexts(data, event)
            chosen = [p for p in data["papers"] if p["selected"] and not p.get("user_excluded")]
            required = [p for p in chosen if p.get("required")]
            optional = [p for p in chosen if not p.get("required")]
            source_cache = self.store.directory(data["session_id"]) / "fulltext"
            recent = sorted(optional, key=lambda p: (p.get("relevance") != "core",
                not (source_cache / (p["paper_id"] + ".json")).exists(), -int(p.get("year") or 0)))
            foundational = sorted(optional, key=lambda p: (p.get("relevance") != "core", -int(p.get("citation_count") or 0), int(p.get("year") or 0)))
            pool = list(required)
            for paper in recent[:6] + foundational + recent:
                if len(pool) >= max(10, len(required)):
                    break
                if paper["paper_id"] not in {p["paper_id"] for p in pool}:
                    pool.append(paper)
            # Backfill failed optional sources within the 15-paper ceiling; never add unrelated papers.
            for paper in foundational + recent:
                if len(pool) >= 15:
                    break
                if paper["paper_id"] not in {p["paper_id"] for p in pool}:
                    pool.append(paper)
            for number, p in enumerate(pool[:15], 1):
                if number > max(10, len(required)) and len([r for r in data["reading"].values() if not r.get("supplemental")]) >= max(10, len(required)):
                    break
                self._progress(data, event, "正在处理论文", current=number, total=len(pool[:15]),
                               title=p["title"], paper_id=p["paper_id"], operation="download", unit="论文")
                self._check(data, event)
                if p["paper_id"] not in data["reading"] and len([r for r in data["reading"].values() if not r.get("supplemental")]) >= 15:
                    continue
                if data.get("refresh_evidence") and p["paper_id"] not in data["reading"] and not (source_cache / (p["paper_id"] + ".json")).exists():
                    continue  # Refresh uses the newly cached approved corpus; explicit fulltext retry owns network retries.
                try:
                    self._read_paper(data, event, p)
                    p.pop("fulltext_error", None)
                except Cancelled:
                    raise
                except Exception as exc:
                    if isinstance(exc, WorkflowFailure) and exc.category == "budget":
                        raise
                    p.update(fulltext_status="downloaded" if p.get("fulltext_status") == "downloaded" else "abstract_only" if p.get("abstract") else "failed", fulltext_error=str(exc)[:500])
                    self._save(data, event)
            missing = [p["title"] for p in required if p["paper_id"] not in data["reading"]]
            core_read = sum(p.get("relevance") == "core" and p["paper_id"] in data["reading"] for p in chosen)
            if missing or core_read < 3:
                downloaded = sum(p.get("fulltext_status") == "downloaded" for p in pool[:15])
                raise WorkflowFailure(f"已完成{core_read}篇核心论文的依据核对，另有{downloaded}篇已取得正文但原文核对未通过。至少需要3篇核心论文的可靠正文证据。" +
                                   ("缺少指定论文：" + "；".join(missing) if missing else "请查看论文详情中的处理结果后重试。"), "evidence", "core_fulltext_missing")
            data.pop("refresh_evidence", None)
            data["phase"] = "synthesis"
            self._save(data, event)
        if data["phase"] == "synthesis":
            synthesis = self._json(data, event, "synthesis", {"direction": data["direction"], "reading": self._compact_reading(data),
                "feedback": data["feedback"], "prior_rejections": data.get("prior_rejections", [])})
            allowed = self._evidence(data)
            gaps = synthesis.get("gaps") or []
            if not synthesis.get("landscape_zh") or not gaps:
                raise WorkflowFailure("未形成有证据支撑的研究缺口：" + str(synthesis.get("missing") or "请补充研究范围。"), "evidence", "no_supported_gap")
            comparison = synthesis.get("method_comparison") or []
            if not comparison or any(p.get("paper_id") not in data["reading"] or not p.get("method") or not p.get("boundary") for p in comparison):
                raise SessionError("领域图景缺少基于已读论文的方法与适用边界对照。")
            sections = synthesis.get("sections", [])
            if not isinstance(sections, list) or any(not isinstance(s, dict) or not s.get("title") or not s.get("summary")
                    or not isinstance(s.get("paper_ids"), list) or not s["paper_ids"] or not set(s["paper_ids"]) <= data["reading"].keys() for s in sections):
                raise SessionError("研究路线必须关联本轮已读论文，不能引用未知论文。")
            if "takeaways" in synthesis and (not isinstance(synthesis["takeaways"], list) or any(not isinstance(x, str) for x in synthesis["takeaways"])):
                raise SessionError("关键认识必须是逐条的文字记录。")
            for gap in gaps:
                refs = gap.get("evidence_refs") or []
                if not refs or not set(refs) <= allowed.keys():
                    raise SessionError("研究缺口引用了不存在的原文证据。")
            data.update(synthesis=synthesis, phase="generate")
            self._save(data, event)
        if data["phase"] == "generate":
            if getattr(self.config.llm,'ideation_backend','builtin')=='ideaspark':
                from .idea_first import IdeaFirstBatch as IdeaSparkBatch
                IdeaSparkBatch(self,data,event).run()
                return
            result = self._json(data, event, "generate", {"direction": data["direction"], "synthesis": data["synthesis"],
                "reading": self._compact_reading(data), "feedback": data["feedback"],
                "prior_rejections": data.get("prior_rejections", [])})
            data.update(candidates=(result.get("cards") or [])[:4], phase="audit", cards=[], rejected_cards=[])
            self._save(data, event)
        if data["phase"] == "audit":
            self._audit(data, event)
            if not data["cards"]:
                raise CandidateRoundFailure("没有候选通过证据与最近邻审查。")
            data.pop("progress_message", None)
            data.pop("progress", None)
            data.update(phase="idea_review", status="waiting", missing=[])
            self._save(data, event)

    def _compact_reading(self, data):
        return [{k: v for k, v in note.items() if k != "passages"} for note in data["reading"].values()]

    def _audit(self, data, event):
        checked = {c["candidate_key"] for c in data["cards"] + data["rejected_cards"]}
        for index, candidate in enumerate(data.get("candidates", [])):
            key = fingerprint(candidate)
            if key in checked:
                continue
            item = {**candidate, "candidate_key": key, "card_id": f"r{data['revision']}-c{index + 1}"}
            self._progress(data, event, "正在检索与审查候选机制", current=index + 1, total=len(data["candidates"]),
                           title=candidate.get("title", ""), unit="候选", operation="audit")
            authors_neighbors = candidate.get("closest_paper_ids") or []
            try:
                refs = candidate.get("evidence_refs") or []
                gap_ids = {g["gap_id"] for g in data["synthesis"]["gaps"]}
                if not refs or not set(refs) <= self._evidence(data).keys() or candidate.get("gap_id") not in gap_ids:
                    raise SessionError("候选缺口或原文引用无效。")
                if not candidate.get("method") or not candidate.get("falsification") or not candidate.get("title"):
                    raise SessionError("候选机制或可证伪实验不完整。")
                authors_neighbors = candidate.get("closest_paper_ids") or []
                if not authors_neighbors or not set(authors_neighbors) <= data["reading"].keys():
                    raise SessionError("候选引用的最近邻未完成全文精读。")
                queries = candidate.get("collision_queries") or []
                if not queries:
                    raise SessionError("缺少按具体机制执行的查重检索式。")
                found = []
                for query in queries[:2]:
                    found += self._search(data, event, str(query))
                if not found:
                    raise SessionError("最近邻查重未取得论文，不能以零检索结果声称新颖。")
                # Rank actual retrieved abstracts against the mechanism using a fresh screen call.
                found = self._merge(found)
                relevance = []
                for start in range(0, min(len(found), 32), 8):
                    relevance.extend(self._screen(data, event, found[start:start + 8], candidate["method"], stage="neighbors"))
                relation_rank = {"same_claim": 0, "same_mechanism": 1, "alternative_mechanism": 2}
                matches = {p["paper_id"]: p for p in relevance if p.get("relevance") == "core" and p.get("relationship") in relation_rank}
                neighbors = sorted([p for p in found if p["paper_id"] in matches],
                    key=lambda p: relation_rank[matches[p["paper_id"]]["relationship"]])[:3]
                known = {pid: {"paper_id": pid, "title": note["title"], "url": note.get("source_url", "")}
                         for pid, note in data["reading"].items()}
                known.update({p["paper_id"]: p for p in data["papers"]})
                approved_ids = {p["paper_id"] for p in data["papers"] if p.get("selected")}
                for pid in candidate.get("closest_paper_ids") or []:
                    if pid in known and all(p["paper_id"] != pid for p in neighbors):
                        neighbors.append(known[pid])
                if not neighbors:
                    raise SessionError("未定位可比较的最近邻方法。")
                item["closest_papers"] = neighbors
                unread = []
                for p in neighbors:
                    try:
                        self._read_paper(data, event, p, extra=p["paper_id"] not in approved_ids)
                    except Cancelled:
                        raise
                    except Exception as exc:
                        failure = self._paper_failure(p, exc, "核对方法证据")
                        if failure.category == "budget":
                            raise failure
                        relationship = matches.get(p["paper_id"], {}).get("relationship")
                        critical = p["paper_id"] in authors_neighbors or relationship in ("same_claim", "same_mechanism")
                        failure.context.update(critical=critical, relationship=relationship,
                            candidate_id=item["card_id"], candidate_title=item["title"])
                        self._warning(data, event, failure, item)
                        unread.append({"paper_id": p["paper_id"], "title": p["title"], "relationship": relationship,
                                       "critical": critical, "failure": failure})
                critical = [p for p in unread if p["critical"]]
                if critical:
                    failure = critical[0]["failure"]
                    raise WorkflowFailure(str(failure), failure.category, failure.code,
                        {**failure.context, "papers": [p["failure"].context for p in critical]}, recoverable=True)
                readable = [p for p in neighbors if p["paper_id"] in data["reading"]]
                if not readable:
                    raise WorkflowFailure("本候选没有可核对的最近邻正文，已转向其他方案。", "evidence", "no_readable_neighbors", recoverable=True)
                audit_payload = {"candidate": candidate,
                    "neighbors": [{k: v for k, v in data["reading"][p["paper_id"]].items() if k != "passages"} for p in readable],
                    "unread_neighbors": [{k: v for k, v in p.items() if k != "failure"} for p in unread]}
                for design_attempt in range(2):
                    review = self._json(data, event, "audit", audit_payload)
                    design = review.get("design_check") or {}
                    valid_review = review.get("verdict") in ("pass", "overlap", "insufficient_evidence") and design.get("verdict") in ("pass", "revise", "fail")
                    if not valid_review:
                        if not design_attempt:
                            audit_payload["review_validation_feedback"] = "必须完整返回verdict及design_check.verdict，严格使用模板给定枚举；不省略审查字段。"
                            continue
                        raise WorkflowFailure("审查输出两次缺少有效结论或实验逻辑字段，未接受该结果。", "technical", "invalid_review_format")
                    if review.get("verdict") == "overlap" or design.get("verdict") == "pass":
                        break
                    patch = design.get("corrected_falsification")
                    if design_attempt or design.get("verdict") != "revise" or not isinstance(patch, str) or len(patch.strip()) < 20:
                        break
                    item.setdefault("design_revisions", []).append({"issues": design.get("issues", []),
                        "before": item["falsification"], "after": patch})
                    item["falsification"] = patch
                    audit_payload["candidate"] = {**candidate, "falsification": patch}
                item["audit"] = review
                ids = review.get("closest_paper_ids") or []
                review_refs = review.get("evidence_refs") or []
                comparisons = review.get("comparisons") or []
                if review.get("verdict") != "pass":
                    category = "scientific" if review.get("verdict") == "overlap" or (design and design.get("verdict") != "pass") else "evidence"
                    code = "mechanism_overlap" if review.get("verdict") == "overlap" else "invalid_design" if category == "scientific" else "insufficient_evidence"
                    raise WorkflowFailure(str(review.get("reason") or review.get("missing") or "未通过最近邻审查"), category, code)
                if design.get("verdict") != "pass":
                    raise WorkflowFailure("实验逻辑未通过独立核对：" + str(design.get("issues") or "缺少可证伪性核对结果"), "scientific" if design else "technical", "invalid_design" if design else "invalid_review_format")
                if not set(authors_neighbors) <= set(ids):
                    raise SessionError("审查未覆盖候选依赖的全部关键最近邻。")
                if not ids or not set(ids) <= {p["paper_id"] for p in neighbors} or not review_refs or not set(review_refs) <= self._evidence(data).keys():
                    raise SessionError("审查缺少真实最近邻与原文引用。")
                referenced_papers = {self._evidence(data)[ref]["paper_id"] for ref in review_refs}
                if not set(ids) <= referenced_papers or not referenced_papers <= {p["paper_id"] for p in neighbors}:
                    raise SessionError("最近邻比较没有引用对应论文的方法正文。")
                if {p.get("paper_id") for p in comparisons} != set(ids) or any(
                    not all(p.get(k) for k in ("problem", "mechanism", "data", "experiment", "difference")) for p in comparisons):
                    raise SessionError("最近邻比较未完整覆盖问题、机制、数据和实验差异。")
                item.update(audit=review, closest_papers=readable,
                    evidence=[self._evidence(data)[r] for r in dict.fromkeys(refs + review_refs)])
                data["cards"].append(item)
            except Cancelled:
                raise
            except Exception as exc:
                item["rejection_reason"] = str(exc)[:1000]
                item["failure"] = {"category": getattr(exc, "category", "technical"),
                    "code": getattr(exc, "code", "stage_failed"), "message": str(exc)[:1000],
                    "context": getattr(exc, "context", {}), "recoverable": getattr(exc, "recoverable", False),
                    "lesson_key": fingerprint([candidate.get("scientific_question"), candidate.get("method"), candidate.get("falsification"),
                                               getattr(exc, "code", "stage_failed"), sorted(authors_neighbors or [])])}
                data["rejected_cards"].append(item)
            self._save(data, event)

    def confirmation(self, sid, revision, card_id):
        """Resolve a server-owned card and freeze its evidence before pipeline handoff."""
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            if data.get("run_id"):
                if data.get("confirmed_card_id") != card_id:
                    raise SessionError("此版本已确认另一个方案，请新建调研。")
                return data, None
            self._editable(data)
            if data["phase"] != "idea_review" or data.get("approved_revision") != revision:
                raise SessionError("须先完成当前版本的文献审核、全文精读与创新点审查。")
            card = next((c for c in data["cards"] if c["card_id"] == card_id), None)
            if not card or not (card.get("audit", {}).get("verdict") == "pass" or
                                 card.get('skill_job_id') and card.get('selection_ready')):
                raise SessionError("该创新点不属于当前已审查的候选。")
            if card.get('skill_job_id'):
                from .ideaspark import verify_card
                verify_card(self,data,card)
            for note in data["reading"].values():
                path = self.store.directory(sid) / "fulltext" / (note["paper_id"] + ".json")
                if not path.exists() or not self._valid_document({"title": note["title"], "doi": next((p.get("doi", "") for p in data["papers"] if p["paper_id"] == note["paper_id"]), "")}, json.loads(path.read_text())) or fingerprint(json.loads(path.read_text())) != note["source_hash"]:
                    raise SessionError("原文内容与精读版本不一致，请重新审核文献。")
            return data, card

    def export(self, data, card):
        """Stage-compatible records plus a complete immutable evidence manifest."""
        import os
        import shutil
        from researchclaw.ideation.brief import ResearchBrief
        from researchclaw.literature.models import Paper, Author

        directory = self.store.directory(data["session_id"])
        native_source = None
        native_job = None
        if card.get('skill_job_id'):
            from .ideaspark import IdeaSparkBatch
            native_job=next((j for j in data.get('ideaspark_batch',{}).get('jobs',[]) if j['id']==card['skill_job_id']),None)
            if not native_job:raise SessionError('未找到选定IdeaSpark方案的执行记录。')
            import threading
            native_source=IdeaSparkBatch(self,data,threading.Event()).candidate(native_job)
            if not native_source or fingerprint(native_source)!=card.get('native_candidate_hash'):
                raise SessionError('IdeaSpark原始方案与已审查版本不一致。')
        target = directory / "exports" / f"r{data['revision']}-{card['card_id']}"
        final_target = target
        if not target.exists():
            import uuid
            target = target.with_name(target.name + "-tmp-" + uuid.uuid4().hex[:8])
            target.mkdir(parents=True)
            for num in range(3, 9):
                (target / f"stage-{num:02d}").mkdir()
            rows = self._merge([p for p in data["papers"] if p.get("selected")] + card["closest_papers"] +
                               card.get('neighbor_check', {}).get('papers', []))
            serialized = "\n".join(json.dumps(p, ensure_ascii=False) for p in rows) + "\n"
            (target / "stage-03/search_plan.yaml").write_text(json.dumps(data.get("plan", {}), ensure_ascii=False))
            (target / "stage-04/candidates.jsonl").write_text(serialized)
            (target / "stage-05/shortlist.jsonl").write_text(serialized)
            bib = []
            for p in rows:
                authors = tuple(Author(a["name"]) for a in p.get("authors", []) if isinstance(a, dict) and a.get("name"))
                bib.append(Paper(paper_id=p["paper_id"], title=p["title"], authors=authors,
                                 year=int(p.get("year") or 0), doi=p.get("doi", ""), url=p.get("url", ""),
                                 venue="" if p.get("venue_type") == "repository" else p.get("venue", ""),
                                 arxiv_id=p.get("arxiv_id", ""), source=p.get("source", "")).to_bibtex())
            (target / "stage-04/references.bib").write_text("\n\n".join(bib))
            for pid, note in data["reading"].items():
                (target / "stage-06" / (pid + ".md")).write_text(json.dumps(note, ensure_ascii=False, indent=2))
            (target / "stage-07/synthesis.md").write_text(data["synthesis"]["landscape_zh"])
            (target / "stage-08/hypotheses.md").write_text(json.dumps(card, ensure_ascii=False, indent=2))
            shutil.copytree(directory / "fulltext", target / "fulltext")
            if (directory / "related work").exists():
                shutil.copytree(directory / "related work", target / "related work")
            if native_job:
                job_dir=(directory/native_job['directory']).resolve()
                if not job_dir.is_relative_to(directory.resolve()):raise SessionError('无效IdeaSpark执行目录。')
                shutil.copytree(job_dir,target/'ideaspark',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
                calls=job_dir.parent/'host_calls'
                for request in calls.glob('*/request.json'):
                    saved=json.loads(request.read_text());inputs=saved.get('input',{}).get('request',{}).get('inputs',[])
                    shared=saved.get('stage') in ('pattern_tagging','bottleneck','batch_plan')
                    if shared or any(str(name).startswith(str(job_dir)+os.sep) for name in inputs):
                        shutil.copytree(request.parent,target/'ideaspark'/'host_calls'/request.parent.name)
            (target / "manifest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
            (target / "cost_log.jsonl").write_text("\n".join(json.dumps(x) for x in data["usage"]) + "\n")
            target.rename(final_target)
        target = final_target
        scope = card["method"] + "\n\n可证伪实验：" + card["falsification"]
        scope += "\n\n真实基线：" + json.dumps(card.get("baselines", []), ensure_ascii=False)
        scope += "\n\n数据访问：" + json.dumps(card.get("data_sources", []), ensure_ascii=False)
        if native_source:
            scope += '\n\nIdeaSpark原始方案规范（方法、变量、假设、可证伪判据与资源以原始版本为准；预期不等于实验结果）：\n'+json.dumps(native_source,ensure_ascii=False)
        if card.get('selection_ready'):
            check = card.get('neighbor_check', {})
            scope += '\n\n选题阶段仅完成创新点与相近研究比较，未证明新颖性、可实现性或效果。阶段9负责根据此想法落实具体技术、数据可得性、数据划分、基线、消融、评价指标及资源；先处理以下重合风险或检索缺失，不沿用旧技术审查的通过/失败标签。未落实真实数据前不得训练或编造实验数据，必要时报告具体缺失。\n'+json.dumps({k:check[k] for k in ('status','summary','comparisons','issues','deferred','boundary') if k in check},ensure_ascii=False)
        brief = ResearchBrief.from_dict({"topic": card["title"], "scientific_question": card["scientific_question"],
            "survey_summary": data["synthesis"]["landscape_zh"], "hypotheses": [card["scientific_question"]],
            "experiment_scope": scope, "datasets": card.get("datasets") or [],
            "metrics": card.get("metrics") or [], "source_ideation_run": str(target.resolve()),
            "target_conference": self.config.export.target_conference})
        return brief.to_dict(), target
