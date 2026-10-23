"""Persistent, two-review research intake. No pipeline or model-only evidence shortcuts."""
from __future__ import annotations

import copy
import json
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from researchclaw.ideation.prompts import OUTPUT_TOKENS, PROMPTS
from researchclaw.ideation.sources import Sources, paper_record
from researchclaw.ideation.store import SessionError, SessionStore, fingerprint, now


class Cancelled(Exception):
    pass


class WorkflowFailure(SessionError):
    def __init__(self, message, category="technical", code="stage_failed"):
        super().__init__(message)
        self.category = category
        self.code = code


class CandidateRoundFailure(SessionError):
    pass


DEFAULT_PAPER_LIMIT = 20
MAX_CANDIDATES = 60
MAX_AUTO_REFINEMENTS = 2  # Initial generation plus at most two evidence-driven revisions.


def quoted(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).replace("\u00ad", ""))


def summary_json(text):
    """Parse model JSON; tolerate one missing delimiter before the known selection key only."""
    from researchclaw.ideation.cards import _extract_json
    try:
        return _extract_json(text), False
    except json.JSONDecodeError as exc:
        raw = text[text.find("{"):text.rfind("}") + 1]
        if exc.msg != "Expecting ',' delimiter" or not raw[:exc.pos].rstrip().endswith('"') or not re.match(r'"selection"\s*:', raw[exc.pos:]):
            raise
        # Insert punctuation outside strings; never rewrite a scientific claim or invent a field.
        return json.loads(raw[:exc.pos] + "," + raw[exc.pos:]), True


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
        mapping = papers if field in ("paper_id", "closest_paper_ids") else excerpts if field in ("passage_id", "evidence_refs") else {}
        if reverse:
            mapping = {v: k for k, v in mapping.items()}
        return mapping.get(value, value) if isinstance(value, str) else value
    return convert(payload), lambda result: convert(result, True)


def passages(paper_id: str, document: dict[str, Any]) -> list[dict[str, str]]:
    output = []
    for page in document["pages"]:
        text = page["text"].strip()
        # Bounded chunks retain source location and a content-bound stable identity.
        for start in range(0, len(text), 1800):
            chunk = text[start:start + 1800]
            if len(chunk.strip()) < 40:
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
        return self.store.load(sid)

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

    def _json(self, data, event, stage, payload, _format_attempt=0, _output_scale=1):
        self._check(data, event)
        labels = {"plan": "正在理解研究方向并规划检索", "screen": "正在核对文献相关性与科学问题",
                  "summarize": "正在整理论文的科学问题、方法、优势与局限", "neighbors": "正在筛选最近邻方法", "synthesis": "正在比较方法并定位研究缺口",
                  "generate": "正在根据原文证据构思创新点", "audit": "正在独立审查最近邻工作与方案差异"}
        if stage in labels:
            self._progress(data, event, labels[stage], operation=stage,
                           **({"title": "", "paper_id": "", "current": 0, "total": 0} if stage in ("synthesis", "generate", "audit") else {}))
        original_payload = payload
        payload, restore = reference_aliases(payload)
        prompt = json.dumps(payload, ensure_ascii=False)
        # Explicit conservative reservation, NOT a claim about vendor prices.
        # UTF-8 bytes upper-bound input tokens. Reserve $5/M input, $25/M output.
        output_budget = OUTPUT_TOKENS[stage] * _output_scale
        reserve = (len((PROMPTS[stage] + prompt).encode()) * 5 + output_budget * 25) / 1_000_000
        if data["budget_usd"] > 0 and data["cost_usd"] + reserve > data["budget_usd"]:
            raise WorkflowFailure("剩余预算不足以预留下一次调用，请调整预算后恢复。", "budget", "budget_exhausted")
        # Reserve before the request so a process restart cannot erase an in-flight charge.
        data["cost_usd"] = round(data["cost_usd"] + reserve, 6)
        entry = {"stage": stage, "at": now(), "cost_usd": reserve, "status": "pending",
                 "cost_basis": "conservative_reservation_5_input_25_output_usd_per_million_not_invoice",
                 "prompt_hash": fingerprint(PROMPTS[stage])[:16], "output_budget": output_budget}
        data["usage"].append(entry)
        self.store.save(data)
        response = None
        try:
            response = self._client().chat([{"role": "user", "content": prompt}],
                system=PROMPTS[stage], max_tokens=output_budget, temperature=0.3, json_mode=True)
        finally:
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
        (output_dir / f"r{data['revision']}-{len(data['usage'])}-{stage}.txt").write_text(response.content)
        try:
            if stage == "summarize":
                result, repaired = summary_json(response.content)
                if repaired:
                    entry["json_syntax_repair"] = "missing_comma_before_selection"
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
            if data.get("run_id") or (data["phase"] in ("literature_review", "idea_review", "handed_off") and not data.get("download_task")):
                raise SessionError("当前阶段需要审核，不能跳过。")
            data.update(status="running", missing=[], blocker=None)
            self.store.save(data)
            event = threading.Event()
            thread = threading.Thread(target=self._work, args=(sid, event), daemon=True)
            self.jobs[sid] = thread, event
            thread.start()

    def _work(self, sid, event):
        data = self.snapshot(sid)
        try:
            self.sources.progress = lambda paper, message: self._progress(data, event, message,
                paper_id=paper["paper_id"], title=paper["title"], operation="download")
            if data.get("download_task"):
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
                blocker={"category": category, "code": code, "message": str(exc)[:1000], "actions": actions[category]})
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
        eligible = [p for p in data["papers"] if p.get("relevance") in ("core", "adjacent") and not p.get("user_excluded")]
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

    def _obtain_fulltext(self, data, event, paper):
        directory = self.store.directory(data["session_id"])
        cache = directory / "fulltext"
        cache.mkdir(exist_ok=True)
        target = cache / (paper["paper_id"] + ".json")
        self._progress(data, event, "正在读取已缓存原文" if target.exists() else "正在获取论文原文",
                       paper_id=paper["paper_id"], title=paper["title"], operation="download")
        document = json.loads(target.read_text()) if target.exists() else self.sources.fulltext(paper, directory)
        if sum(len(p["text"]) for p in document.get("pages", [])) < 2000:
            raise SessionError("仅取得摘要或过短正文，未标记为原文已获取。")
        if not target.exists():
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(document, ensure_ascii=False))
            temp.replace(target)
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

    def refresh_summaries(self, sid, revision):
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            self._ensure_capacity()
            if data["phase"] == "retrieval":
                raise SessionError("请先完成文献筛选，系统会自动获取材料并调用AI总结。", 409)
            previous = data["status"]
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
            old_ids = {p["paper_id"] for p in self._related(data)}
            self._summarize_selected(data, event, force=True)
            if data.get("approved_revision") is None:
                self._select_shortlist(data)
                if old_ids != {p["paper_id"] for p in self._related(data)}:
                    self.store.invalidate(data, "正文检查改变推荐文献")
                    self._select_shortlist(data)
            data["status"] = previous
        except Cancelled:
            data.update(status="stopped")
        except Exception as exc:
            data.update(status="blocked", missing=[str(exc)[:1000]])
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
            if 2000 <= size <= 180_000:
                return {"source_type": "fulltext", "pages": pages}
        return None

    @staticmethod
    def _summary_excerpts(paper_id, material):
        """Lossless source segmentation; these are excerpts, never generated summaries."""
        return [{"passage_id": paper_id + ":overview:" + fingerprint([page["location"], start, page["text"][start:start + 160]])[:16],
                 "location": page["location"], "text": page["text"][start:start + 160]}
                for page in material["pages"] for start in range(0, len(page["text"]), 160)]

    def _summarize_selected(self, data, event, force=False, papers=None):
        """DeepSeek writes every content field; code selects sources and validates structure only."""
        tasks = []
        for paper in (self._related(data) if papers is None else papers):
            if paper["paper_id"] in data["reading"]:
                continue
            material = self._summary_material(data, paper)
            if material is None:
                self._apply_summary(paper, {})
                continue
            source_hash = fingerprint(material)
            if not force and paper.get("summary_source_hash") == source_hash and paper.get("summary_prompt_hash") == fingerprint(PROMPTS["summarize"]) and paper.get("summary_version") == 4:
                continue
            tasks.append((paper, material, source_hash))
        completed = 0
        while tasks:
            batch, size = [], 0
            while tasks and len(batch) < 4:
                item = tasks[0]
                count = len(json.dumps(item[1], ensure_ascii=False))
                if batch and size + count > 120_000:
                    break
                batch.append(tasks.pop(0)); size += count
            self._progress(data, event, "正在由DeepSeek阅读材料并撰写论文概览", operation="summarize",
                           current=completed + 1, total=completed + len(batch) + len(tasks), unit="篇", title="", paper_id="")
            payload = {"direction": data["direction"], "papers": [{"paper_id": p["paper_id"], "title": p["title"],
                                   "year": p.get("year"), "venue": p.get("venue"), "venue_type": p.get("venue_type"),
                                   "material": {"source_type": "fulltext", "passages": self._summary_excerpts(p["paper_id"], material)}}
                                   for p, material, _ in batch]}
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
                            self._apply_summary(copy.deepcopy(paper), row, material)
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
                self._apply_summary(paper, row, material)
                paper.update(summary_source_hash=source_hash, summary_prompt_hash=fingerprint(PROMPTS["summarize"]))
            completed += len(batch)
            self._save(data, event)
        self._save(data, event)

    @staticmethod
    def _apply_summary(paper, info, material=None):
        basis = material.get("source_type") if material else None
        if not basis:
            paper.update(overview={}, summary_zh="", summary_basis=None, summary_state="awaiting_source", summary_version=4)
            paper.pop("summary_source_hash", None)
            return
        overview = info.get("overview") or {}
        if not isinstance(info.get("summary_zh"), str) or not info["summary_zh"].strip() or not all(
                isinstance(overview.get(k), str) and overview[k].strip() for k in ("problem", "method", "strengths", "limitations")):
            raise WorkflowFailure("AI论文概览返回不完整，已保留此前结果，可重试本步。", "technical", "invalid_paper_overview")
        if any(len(overview[k]) > 160 for k in ("problem", "method", "strengths", "limitations")):
            raise SessionError("概览太长：每项用零基础读者能懂的1–2个短句，硬上限160字符；删掉算法清单、参数与长流程，用普通话保留核心意思。")
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
                     summary_state="ready", summary_version=4)

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
            "synthesis": copy.deepcopy(data["synthesis"]), "inherited_literature_approval": revision})
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

    def _retrieve(self, data, event):
        if not data.get("plan"):
            plan = self._json(data, event, "plan", {"direction": data["direction"]})
            queries = plan.get("queries") or []
            if not plan.get("interpretation") or not 4 <= len(queries) <= 6 or any(not q.get("query") for q in queries):
                raise SessionError("检索规划不完整，请重试或补充研究范围。")
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
        per_query = max(1, (candidate_limit + len(queries) - 1) // len(queries))
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
                elif len(new_ids) < per_query and len(rows) + len(new_ids) < candidate_limit:
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
        pending = [p for p in rows if p.get("relevance") == "unreviewed"]
        for start in range(0, len(pending), 8):
            batch = pending[start:start + 8]
            done = sum(p.get("relevance") != "unreviewed" for p in rows)
            self._progress(data, event, "正在筛选文献相关性", operation="screen", unit="篇",
                           current=done, total=len(rows), title="", paper_id="", source="", query="")
            screened = {p["paper_id"]: p for p in self._screen(data, event, batch,
                data["direction"] + "\n" + data["plan"]["interpretation"])}
            for p in batch:
                info = screened[p["paper_id"]]
                p.update(relevance=info["relevance"], reason=info["reason"], selected=info["relevance"] == "core")
            self._progress(data, event, "已完成本组文献筛选", operation="screen", unit="篇",
                           current=done + len(batch), total=len(rows))
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
            return data["reading"][key]
        if data["phase"] == "reading" and paper.get("fulltext_attempt_id") == f"scope-r{data['revision']}" and paper.get("fulltext_status") in ("abstract_only", "failed"):
            raise SessionError(paper.get("fulltext_error") or "本轮未获取原文，请补充材料或稍后重试。")
        document = self._obtain_fulltext(data, event, paper)
        chunks = passages(key, document)
        if sum(len(c["text"]) for c in chunks) < 2000:
            raise SessionError("正文不足以精读，不能使用摘要代替全文。")
        # Read all source text; reject oversize rather than silently claiming to read omitted pages.
        if sum(len(c["text"]) for c in chunks) > 180_000:
            raise WorkflowFailure("论文超过精读窗口，请补充聚焦方法正文的版本；未将未读内容视为已读。", "evidence", "read_window_exceeded")
        by_id = {c["passage_id"]: c for c in chunks}
        payload = {"paper": paper["title"], "passages": chunks}
        for attempt in range(2):
            self._progress(data, event, "正在核对论文方法与原文依据" + ("（校正引文）" if attempt else ""),
                           paper_id=key, title=paper["title"], operation="read")
            result = self._json(data, event, "read", payload)
            evidence = result.get("evidence") or []
            if result.get("has_methods") is not True or not all(result.get(k) for k in
                ("problem", "method", "data", "experiments", "findings", "strengths", "limitations")) or not evidence:
                raise SessionError("未取得可靠方法正文或精读证据。")
            invalid = []
            for item in evidence:
                source = by_id.get(item.get("passage_id"))
                quote = item.get("quote", "")
                if len(quote) >= 12 and (not source or quoted(quote) not in quoted(source["text"])):
                    matches = [c for c in chunks if quoted(quote) in quoted(c["text"])]
                    if len(matches) == 1:
                        # Correct a copied id only when the exact text uniquely identifies its location.
                        source = matches[0]
                        item.update(model_passage_id=item.get("passage_id"), passage_id=source["passage_id"],
                                    citation_resolution="unique_exact_quote")
                if not source or len(item.get("quote", "")) < 12 or quoted(item["quote"]) not in quoted(source["text"]) or item.get("kind") not in ("source", "inference") or item.get("role") not in ("method", "result", "limitation", "context"):
                    invalid.append(item)
                else:
                    item.update(location=source["location"], paper_id=key)
            if not invalid:
                break
            if attempt:
                raise SessionError("精读引用未匹配原文，拒绝保存虚构证据。")
            payload["validation_feedback"] = {"invalid_evidence": invalid,
                "instruction": "上次引文未通过逐字匹配。重新返回完整精读JSON；quote必须保持原文语言，从对应片段连续复制12–120字符，不能翻译、拼接或省略。"}
        if not any(e["kind"] == "source" and e.get("role") == "method" for e in evidence):
            raise SessionError("精读缺少来自方法正文的原文证据，未接受只含摘要或模型推断的记录。")
        result.update(paper_id=key, title=paper["title"], passages=chunks,
                      source_hash=fingerprint(document), source_url=document.get("source_url"), supplemental=extra)
        data["reading"][key] = result
        paper["fulltext_status"] = "read"
        paper.update(overview={"problem": result["problem"], "method": result["method"],
                     "strengths": result["strengths"], "limitations": result["limitations"]}, summary_basis="fulltext", summary_version=4)
        self._save(data, event)
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
                non_scientific = next((c["failure"] for c in rejected if c["failure"]["category"] != "scientific"), None)
                if non_scientific:
                    raise WorkflowFailure(non_scientific["message"], non_scientific["category"], non_scientific["code"])
                previous = {x.get("failure", {}).get("lesson_key") for x in data.get("prior_rejections", []) if x.get("failure")}
                new_lessons = {c["failure"]["lesson_key"] for c in rejected} - previous
                if not new_lessons:
                    raise WorkflowFailure("新一轮仍重复已被否决的机制，未获得新增依据，已暂停自动修订。", "scientific", "no_new_information")
                if data.get("refinement_round", 0) >= MAX_AUTO_REFINEMENTS:
                    raise WorkflowFailure("三轮构思与审查仍未形成可审核候选，已保留全部否决依据，请补充文献或调整方向。", "scientific", "refinement_exhausted")
                self._prepare_refinement(data, data["feedback"], automatic=True)
                self._progress(data, event, "正在根据审查依据自动修订方案", operation="refine",
                               current=data["refinement_round"] + 1, total=MAX_AUTO_REFINEMENTS + 1, unit="轮次")

    def _develop_round(self, data, event):
        if data.get("approved_revision") != data["revision"]:
            raise SessionError("文献版本尚未批准。")
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
            for gap in gaps:
                refs = gap.get("evidence_refs") or []
                if not refs or not set(refs) <= allowed.keys():
                    raise SessionError("研究缺口引用了不存在的原文证据。")
            data.update(synthesis=synthesis, phase="generate")
            self._save(data, event)
        if data["phase"] == "generate":
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
                for p in neighbors:
                    self._read_paper(data, event, p, extra=p["paper_id"] not in approved_ids)
                audit_payload = {"candidate": candidate,
                    "neighbors": [data["reading"][p["paper_id"]] for p in neighbors]}
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
                item.update(audit=review, closest_papers=neighbors,
                    evidence=[self._evidence(data)[r] for r in dict.fromkeys(refs + review_refs)])
                data["cards"].append(item)
            except Cancelled:
                raise
            except Exception as exc:
                item["rejection_reason"] = str(exc)[:1000]
                item["failure"] = {"category": getattr(exc, "category", "technical"),
                    "code": getattr(exc, "code", "stage_failed"), "message": str(exc)[:1000],
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
            if not card or card.get("audit", {}).get("verdict") != "pass":
                raise SessionError("该创新点不属于当前已审查的候选。")
            for note in data["reading"].values():
                path = self.store.directory(sid) / "fulltext" / (note["paper_id"] + ".json")
                if not path.exists() or fingerprint(json.loads(path.read_text())) != note["source_hash"]:
                    raise SessionError("原文内容与精读版本不一致，请重新审核文献。")
            return data, card

    def export(self, data, card):
        """Stage-compatible records plus a complete immutable evidence manifest."""
        import shutil
        from researchclaw.ideation.brief import ResearchBrief
        from researchclaw.literature.models import Paper, Author

        directory = self.store.directory(data["session_id"])
        target = directory / "exports" / f"r{data['revision']}-{card['card_id']}"
        final_target = target
        if not target.exists():
            import uuid
            target = target.with_name(target.name + "-tmp-" + uuid.uuid4().hex[:8])
            target.mkdir(parents=True)
            for num in range(3, 9):
                (target / f"stage-{num:02d}").mkdir()
            rows = self._merge([p for p in data["papers"] if p.get("selected")] + card["closest_papers"])
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
            (target / "manifest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
            (target / "cost_log.jsonl").write_text("\n".join(json.dumps(x) for x in data["usage"]) + "\n")
            target.rename(final_target)
        target = final_target
        scope = card["method"] + "\n\n可证伪实验：" + card["falsification"]
        scope += "\n\n真实基线：" + json.dumps(card.get("baselines", []), ensure_ascii=False)
        scope += "\n\n数据访问：" + json.dumps(card.get("data_sources", []), ensure_ascii=False)
        brief = ResearchBrief.from_dict({"topic": card["title"], "scientific_question": card["scientific_question"],
            "survey_summary": data["synthesis"]["landscape_zh"], "hypotheses": [card["scientific_question"]],
            "experiment_scope": scope, "datasets": card.get("datasets") or [],
            "metrics": card.get("metrics") or [], "source_ideation_run": str(target.resolve()),
            "target_conference": self.config.export.target_conference})
        return brief.to_dict(), target
