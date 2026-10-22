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


def quoted(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).replace("\u00ad", ""))


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
            raise SessionError("调研预算已用完，请调整成本预算后恢复。")

    def _save(self, data, event):
        with self.store.lock:
            self._check(data, event)
            self.store.save(data)

    def _progress(self, data, event, message, **fields):
        data["progress"] = {**data.get("progress", {}), **fields, "message": message, "at": now()}
        data["progress_message"] = message
        self._save(data, event)

    def _json(self, data, event, stage, payload, _format_attempt=0):
        self._check(data, event)
        labels = {"plan": "正在理解研究方向并规划检索", "screen": "正在核对文献相关性与科学问题",
                  "summarize": "正在整理论文的科学问题、方法、优势与局限", "synthesis": "正在比较方法并定位研究缺口",
                  "generate": "正在根据原文证据构思创新点", "audit": "正在独立审查最近邻工作与方案差异"}
        if stage in labels:
            self._progress(data, event, labels[stage], operation=stage,
                           **({"title": "", "paper_id": "", "current": 0, "total": 0} if stage in ("synthesis", "generate", "audit") else {}))
        original_payload = payload
        payload, restore = reference_aliases(payload)
        prompt = json.dumps(payload, ensure_ascii=False)
        # Explicit conservative reservation, NOT a claim about vendor prices.
        # UTF-8 bytes upper-bound input tokens. Reserve $5/M input, $25/M output.
        reserve = (len((PROMPTS[stage] + prompt).encode()) * 5 + OUTPUT_TOKENS[stage] * 25) / 1_000_000
        if data["budget_usd"] > 0 and data["cost_usd"] + reserve > data["budget_usd"]:
            raise SessionError("剩余预算不足以预留下一次调用，请调整预算后恢复。")
        # Reserve before the request so a process restart cannot erase an in-flight charge.
        data["cost_usd"] = round(data["cost_usd"] + reserve, 6)
        entry = {"stage": stage, "at": now(), "cost_usd": reserve, "status": "pending",
                 "cost_basis": "conservative_reservation_5_input_25_output_usd_per_million_not_invoice"}
        data["usage"].append(entry)
        self.store.save(data)
        response = None
        try:
            response = self._client().chat([{"role": "user", "content": prompt}],
                system=PROMPTS[stage], max_tokens=OUTPUT_TOKENS[stage], temperature=0.3, json_mode=True)
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
            raise SessionError(f"{stage} 输出被截断，未接受不完整内容。请重试。")
        from researchclaw.ideation.cards import _extract_json
        output_dir = self.store.directory(data["session_id"]) / "model_outputs"
        output_dir.mkdir(exist_ok=True)
        (output_dir / f"r{data['revision']}-{len(data['usage'])}-{stage}.txt").write_text(response.content)
        try:
            result = _extract_json(response.content)
        except ValueError as exc:
            if not _format_attempt:
                return self._json(data, event, stage, {**original_payload,
                    "json_validation_feedback": "上次输出不是有效JSON。请重新生成完整严格JSON，所有字符串内的双引号和换行必须正确转义；不附代码围栏或说明。"}, _format_attempt=1)
            raise SessionError("模型输出格式两次未通过校验，已暂停；可重试，不会保存不完整结果。") from exc
        if not isinstance(result, dict):
            raise SessionError(f"{stage} 未返回有效结构")
        return restore(result)

    def create(self, direction: str) -> dict[str, Any]:
        with self.store.lock:
            self._ensure_capacity()
            budget = float(getattr(getattr(self.config, "hitl", None), "cost_budget_usd", 0) or 0)
            data = self.store.create(direction, budget)
            self.launch(data["session_id"])
            return self.snapshot(data["session_id"])

    def _ensure_capacity(self):
        if any(t.is_alive() for t, _ in self.jobs.values()):
            raise SessionError("已有调研任务正在运行，请完成或停止后重试。")

    def launch(self, sid: str) -> None:
        with self.store.lock:
            self._ensure_capacity()
            data = self.snapshot(sid)
            if data.get("run_id") or data["phase"] in ("literature_review", "idea_review", "handed_off"):
                raise SessionError("当前阶段需要审核，不能跳过。")
            data.update(status="running", missing=[])
            self.store.save(data)
            event = threading.Event()
            thread = threading.Thread(target=self._work, args=(sid, event), daemon=True)
            self.jobs[sid] = thread, event
            thread.start()

    def _work(self, sid, event):
        data = self.snapshot(sid)
        try:
            if data["phase"] == "retrieval":
                self._retrieve(data, event)
            else:
                self.sources.progress = lambda paper, message: self._progress(data, event, message,
                    paper_id=paper["paper_id"], title=paper["title"], operation="download")
                self._develop(data, event)
        except Cancelled:
            data.update(status="stopped", missing=["已停止，可从最近完成点继续。"])
            self.store.save(data)
        except Exception as exc:
            # No fallback cards, invented metadata or perfect novelty on search failure.
            data.update(status="blocked", missing=[str(exc)[:1000]])
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
            row = {**row, "selected": True, "required": True, "relevance": "core",
                   "reason": "用户补充，请审核相关性", "summary_zh": row.get("abstract") or "用户补充材料，尚未精读"}
            data["papers"] = [p for p in data["papers"] if p["paper_id"] != row["paper_id"]] + [row]
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
            data.pop("progress", None)
            data.pop("progress_message", None)
            self.store.save(data)
            return data

    def refresh_summaries(self, sid, revision):
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            self._ensure_capacity()
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
            papers = [p for p in data["papers"] if p.get("relevance") in ("core", "adjacent") and not p.get("user_excluded") and p["paper_id"] not in data["reading"]]
            for start in range(0, len(papers), 8):
                batch = papers[start:start + 8]
                self._progress(data, event, "正在更新论文概览", current=start + 1, total=len(papers), operation="summarize")
                result = self._json(data, event, "summarize", {"papers": [{k: p.get(k) for k in
                    ("paper_id", "title", "abstract")} for p in batch]})
                rows = result.get("papers") or []
                if len(rows) != len(batch) or {p.get("paper_id") for p in rows} != {p["paper_id"] for p in batch}:
                    raise SessionError("概览未覆盖全部论文身份，未接受该批结果。")
                by_id = {p["paper_id"]: p for p in batch}
                for row in rows:
                    self._apply_summary(by_id[row["paper_id"]], row)
                self._save(data, event)
            data["status"] = previous
        except Cancelled:
            data.update(status="stopped")
        except Exception as exc:
            data.update(status="blocked", missing=[str(exc)[:1000]])
        data.pop("presentation_task", None)
        data.pop("progress_message", None)
        data.pop("progress", None)
        self.store.save(data)

    @staticmethod
    def _apply_summary(paper, info):
        overview = info.get("overview") or {}
        if not info.get("summary_zh") or not all(isinstance(overview.get(k), str) and overview[k].strip()
                for k in ("problem", "method", "strengths", "limitations")):
            raise SessionError("论文概览缺少科学问题、方法、优势或局限，未接受。")
        has_abstract = len(re.sub(r"\W", "", paper.get("abstract") or "")) >= 20
        if not has_abstract:
            overview = {"problem": "题名所指研究：" + paper["title"], "method": "需正文核实",
                        "strengths": "需正文核实", "limitations": "需正文核实"}
            info = {**info, "summary_zh": "题名所指研究：" + paper["title"] + "。方法、优势与局限需正文核实。"}
        paper.update(overview=overview, summary_zh=info["summary_zh"], summary_basis="abstract" if has_abstract else "title",
                     summary_version=2)

    def revise_scope(self, sid, revision, direction):
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            if not 4 <= len(direction.strip()) <= 4000:
                raise SessionError("研究方向须为4–4000字符", 400)
            self.store.invalidate(data, "修改研究范围")
            data.update(direction=direction.strip(), papers=[], search_log=[], completed_queries=[], phase="retrieval")
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
            related = [p for p in data["papers"] if p.get("relevance") in ("core", "adjacent") and not p.get("user_excluded")]
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
            ids = {p["paper_id"] for p in data["papers"] if not p.get("user_excluded")}
            if not selected or not set(selected) <= ids or not set(required) <= set(selected):
                raise SessionError("须保留真实论文，且必读论文必须在保留列表中。", 400)
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
            # Cards and review refer to this exact evidence version, new generation gets a revision.
            data["reviews"].append({"type": "regenerate", "at": now(), "feedback": feedback,
                                   "revision": revision, "cards": data["cards"]})
            data["revision"] += 1
            data.update(approved_revision=data["revision"], cards=[], rejected_cards=[],
                        phase="generate", feedback=feedback, missing=[])
            self.store.save(data)
            self.launch(sid)
            return self.snapshot(sid)

    def resume(self, sid, revision):
        with self.store.lock:
            data = self.snapshot(sid)
            self.store.require_revision(data, revision)
            self._editable(data)
            if data["phase"] in ("literature_review", "idea_review"):
                raise SessionError("当前需要人工审核，请使用对应审核按钮。")
            if data["phase"] == "audit":
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

    def _screen(self, data, event, rows, direction):
        # Short request-local aliases eliminate hash-copy mistakes without weakening identity checks.
        mapping = {f"P{i + 1}": p["paper_id"] for i, p in enumerate(rows)}
        payload = {"direction": direction, "papers": [{"paper_id": alias,
            **{k: p.get(k) for k in ("title", "year", "abstract")}}
            for alias, p in zip(mapping, rows)]}
        for attempt in range(2):
            response = self._json(data, event, "screen", payload)
            output = response.get("papers") or []
            if len(output) == len(mapping) and {p.get("paper_id") for p in output} == set(mapping):
                return [{**p, "paper_id": mapping[p["paper_id"]]} for p in output]
            payload["validation_feedback"] = "上次结果未覆盖全部给定ID。仅允许并必须完整返回：" + ", ".join(mapping)
        raise SessionError("论文筛选结果两次未完整覆盖给定论文ID，已暂停；未接受未知或遗漏身份。")

    def _retrieve(self, data, event):
        if not data.get("plan"):
            plan = self._json(data, event, "plan", {"direction": data["direction"]})
            queries = plan.get("queries") or []
            if not plan.get("interpretation") or not 4 <= len(queries) <= 6 or any(not q.get("query") for q in queries):
                raise SessionError("检索规划不完整，请重试或补充研究范围。")
            data["plan"] = plan
            self._save(data, event)
        rows = list(data["papers"])
        completed = data.setdefault("completed_queries", [])
        for query in data["plan"]["queries"]:
            if query in completed:
                continue
            self._progress(data, event, "正在检索：" + query["query"], operation="search",
                           current=len(completed) + 1, total=len(data["plan"]["queries"]))
            found = self._search(data, event, query["query"], query.get("period", "all"))
            rows = self._merge(rows + found)
            data["papers"] = rows
            # Empty search must be retryable, not a permanently completed query.
            if found:
                completed.append(query)
            self._save(data, event)
        if not rows:
            raise SessionError("检索未取得真实论文：可能限流、网络不可用或检索范围不合适。请重试、改方向或补充DOI/PDF。")
        for start in range(0, len(rows), 8):
            batch = rows[start:start + 8]
            if all(p.get("relevance") != "unreviewed" for p in batch):
                continue
            screened = {p["paper_id"]: p for p in self._screen(data, event, batch,
                data["direction"] + "\n" + data["plan"]["interpretation"])}
            if set(screened) != {p["paper_id"] for p in batch}:
                raise SessionError("论文筛选结果不完整或含未知论文ID，未接受该批结果。")
            for p in batch:
                info = screened[p["paper_id"]]
                if info.get("relevance") not in ("core", "adjacent", "off_topic") or not info.get("reason") or not info.get("summary_zh"):
                    raise SessionError("论文相关性判断缺少理由或摘要说明。")
                self._apply_summary(p, info)
                p.update(relevance=info["relevance"], reason=info["reason"], summary_zh=p["summary_zh"],
                         selected=info["relevance"] == "core")
            self._save(data, event)
        if not any(p["relevance"] == "core" for p in rows):
            raise SessionError("未筛选出核心相关论文，请修改范围或补充文献；不会补入无关论文凑数。")
        self._enrich_papers(data, event)
        data.pop("progress_message", None)
        data.pop("progress", None)
        data.update(phase="literature_review", status="waiting", missing=[])
        self._save(data, event)

    def _enrich_papers(self, data, event):
        enrich = getattr(self.sources, "enrich_metadata", None)
        if enrich is None:
            return
        papers = [p for p in data["papers"] if p.get("relevance") in ("core", "adjacent") and p.get("bibliography_version") != 1]
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
        directory = self.store.directory(data["session_id"])
        cache = directory / "fulltext"
        cache.mkdir(exist_ok=True)
        target = cache / (key + ".json")
        self._progress(data, event, "正在读取缓存正文" if target.exists() else "正在寻找公开全文",
                       paper_id=key, title=paper["title"], operation="download")
        document = json.loads(target.read_text()) if target.exists() else self.sources.fulltext(paper, directory)
        paper["fulltext_status"] = "downloaded"
        paper["fulltext_url"] = document.get("source_url")
        chunks = passages(key, document)
        if sum(len(p["text"]) for p in chunks) < 2000:
            raise SessionError("正文不足以精读，不能使用摘要代替全文。")
        if not target.exists():
            target.write_text(json.dumps(document, ensure_ascii=False))
        # Read all source text; reject oversize rather than silently claiming to read omitted pages.
        if sum(len(c["text"]) for c in chunks) > 180_000:
            raise SessionError("论文超过精读窗口，请提供聚焦方法正文的版本。")
        by_id = {c["passage_id"]: c for c in chunks}
        payload = {"paper": paper["title"], "passages": chunks}
        for attempt in range(2):
            self._progress(data, event, "正在精读并核对原文引文" + ("（校正引文）" if attempt else ""),
                           paper_id=key, title=paper["title"], operation="read")
            result = self._json(data, event, "read", payload)
            evidence = result.get("evidence") or []
            if result.get("has_methods") is not True or not all(result.get(k) for k in
                ("problem", "method", "data", "experiments", "findings", "limitations")) or not evidence:
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
                if not source or len(item.get("quote", "")) < 12 or quoted(item["quote"]) not in quoted(source["text"]) or item.get("kind") not in ("source", "inference"):
                    invalid.append(item)
                else:
                    item.update(location=source["location"], paper_id=key)
            if not invalid:
                break
            if attempt:
                raise SessionError("精读引用未匹配原文，拒绝保存虚构证据。")
            payload["validation_feedback"] = {"invalid_evidence": invalid,
                "instruction": "上次引文未通过逐字匹配。重新返回完整精读JSON；quote必须保持原文语言，从对应片段连续复制12–120字符，不能翻译、拼接或省略。"}
        if not any(e["kind"] == "source" for e in evidence):
            raise SessionError("精读只有模型推断，缺少原文事实证据。")
        result.update(paper_id=key, title=paper["title"], passages=chunks,
                      source_hash=fingerprint(document), source_url=document.get("source_url"), supplemental=extra)
        data["reading"][key] = result
        paper["fulltext_status"] = "read"
        paper.update(overview={"problem": result["problem"], "method": result["method"],
                     "strengths": result.get("strengths") or "全文提供了研究结论，未单独陈述比较优势。", "limitations": result["limitations"]}, summary_basis="fulltext", summary_version=2)
        self._save(data, event)
        return result

    def _evidence(self, data):
        return {c["passage_id"]: c for r in data["reading"].values() for c in r["passages"]}

    def _develop(self, data, event):
        if data.get("approved_revision") != data["revision"]:
            raise SessionError("文献版本尚未批准。")
        if data["phase"] == "reading":
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
                               title=p["title"], paper_id=p["paper_id"], operation="download")
                self._check(data, event)
                try:
                    self._read_paper(data, event, p)
                    p.pop("fulltext_error", None)
                except Cancelled:
                    raise
                except Exception as exc:
                    p.update(fulltext_status="downloaded" if p.get("fulltext_status") == "downloaded" else "abstract_only" if p.get("abstract") else "failed", fulltext_error=str(exc)[:500])
                    self._save(data, event)
            missing = [p["title"] for p in required if p["paper_id"] not in data["reading"]]
            core_read = sum(p.get("relevance") == "core" and p["paper_id"] in data["reading"] for p in chosen)
            if missing or core_read < 3:
                downloaded = sum(p.get("fulltext_status") == "downloaded" for p in pool[:15])
                raise SessionError(f"已完成{core_read}篇核心论文精读，另有{downloaded}篇已取得正文但精读核对未通过。至少需要3篇核心论文的可靠正文证据。" +
                                   ("缺少指定论文：" + "；".join(missing) if missing else "请查看论文详情中的处理结果后重试。"))
            data["phase"] = "synthesis"
            self._save(data, event)
        if data["phase"] == "synthesis":
            synthesis = self._json(data, event, "synthesis", {"direction": data["direction"], "reading": self._compact_reading(data)})
            allowed = self._evidence(data)
            gaps = synthesis.get("gaps") or []
            if not synthesis.get("landscape_zh") or not gaps:
                raise SessionError("未形成有证据支撑的研究缺口：" + str(synthesis.get("missing") or "请补充研究范围。"))
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
                "reading": self._compact_reading(data), "feedback": data["feedback"]})
            data.update(candidates=(result.get("cards") or [])[:4], phase="audit", cards=[], rejected_cards=[])
            self._save(data, event)
        if data["phase"] == "audit":
            self._audit(data, event)
            if not data["cards"]:
                raise SessionError("没有候选通过证据与最近邻审查。请查看未通过原因，补充材料或修改意见后重新生成。")
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
                for start in range(0, min(len(found), 32), 16):
                    relevance.extend(self._screen(data, event, found[start:start + 16], candidate["method"]))
                relevant_ids = {p["paper_id"] for p in relevance if p.get("relevance") == "core"}
                neighbors = [p for p in found if p["paper_id"] in relevant_ids][:3]
                known = {p["paper_id"]: p for p in data["papers"]}
                approved_ids = {p["paper_id"] for p in data["papers"] if p.get("selected")}
                for pid in candidate.get("closest_paper_ids") or []:
                    if pid in known and all(p["paper_id"] != pid for p in neighbors):
                        neighbors.append(known[pid])
                if not neighbors:
                    raise SessionError("未定位可比较的最近邻方法。")
                for p in neighbors:
                    self._read_paper(data, event, p, extra=p["paper_id"] not in approved_ids)
                review = self._json(data, event, "audit", {"candidate": candidate,
                    "neighbors": [data["reading"][p["paper_id"]] for p in neighbors]})
                ids = review.get("closest_paper_ids") or []
                review_refs = review.get("evidence_refs") or []
                comparisons = review.get("comparisons") or []
                if review.get("verdict") != "pass":
                    raise SessionError(str(review.get("reason") or review.get("missing") or "未通过最近邻审查"))
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
