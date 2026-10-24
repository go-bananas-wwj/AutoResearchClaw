"""Isolated candidate snapshots with serialized, delta-only session publication.

No worker writes a whole stale session over another worker's receipts or costs.
Model calls and subprocesses run outside the publication lock.
"""
from __future__ import annotations

import copy
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from .store import now


class CandidateStore:
    def __init__(self, host, job_id):
        self.host, self.job_id = host, job_id
        self.store = host.service.store
        self.lock = self.store.lock
        with self.lock:
            self.data = copy.deepcopy(host.data)
        self.initial_usage = len(self.data['usage'])
        self.published_cost = self.data['cost_usd']
        self.baseline = copy.deepcopy(self.data)

    def directory(self, sid):
        return self.store.directory(sid)

    def load(self, sid):
        return self.store.load(sid)

    def save(self, local):
        from .service import WorkflowFailure
        with self.lock:
            shared = self.host.data
            delta = local['cost_usd'] - self.published_cost
            if delta > 0 and shared['budget_usd'] > 0 and shared['cost_usd'] + delta > shared['budget_usd']:
                raise WorkflowFailure('并行调研的合并预算不足以预留下一次调用。', 'budget', 'budget_exhausted')
            shared['cost_usd'] = round(shared['cost_usd'] + delta, 6)
            self.published_cost = local['cost_usd']
            # UUIDs identify both pending and returned versions of the same call.
            positions = {u.get('call_id'): i for i, u in enumerate(shared['usage']) if u.get('call_id')}
            for entry in local['usage'][self.initial_usage:]:
                entry.setdefault('call_id', uuid.uuid4().hex)
                entry['skill_job_id'] = self.job_id
                if entry['call_id'] in positions:
                    shared['usage'][positions[entry['call_id']]] = copy.deepcopy(entry)
                else:
                    positions[entry['call_id']] = len(shared['usage'])
                    shared['usage'].append(copy.deepcopy(entry))
            for pid, note in local.get('reading', {}).items():
                if note != self.baseline.get('reading', {}).get(pid):
                    shared['reading'][pid] = copy.deepcopy(note)
            by_id = {p['paper_id']: p for p in shared['papers']}
            old = {p['paper_id']: p for p in self.baseline['papers']}
            for paper in local['papers']:
                pid = paper['paper_id']
                if pid not in by_id:
                    shared['papers'].append(copy.deepcopy(paper)); by_id[pid] = shared['papers'][-1]
                else:
                    for key, value in paper.items():
                        if value != old.get(pid, {}).get(key):
                            by_id[pid][key] = copy.deepcopy(value)
            for key, identity in (('warnings', 'warning_id'), ('search_log', None)):
                existing = shared.setdefault(key, [])
                for item in local.get(key, []):
                    if item in self.baseline.get(key, []):
                        continue
                    if identity and item.get(identity):
                        existing[:] = [e for e in existing if e.get(identity) != item[identity]]
                    if item not in existing:
                        existing.append(copy.deepcopy(item))
            job = next(j for j in local['ideaspark_batch']['jobs'] if j['id'] == self.job_id)
            shared_job = next(j for j in shared['ideaspark_batch']['jobs'] if j['id'] == self.job_id)
            shared_job.update(copy.deepcopy(job))
            own = [c for c in local.get('idea_pool', []) if c.get('skill_job_id') == self.job_id]
            for card in own:
                digest = card.get('native_intervention_hash')
                if digest and any(c.get('native_intervention_hash') == digest and c.get('skill_job_id') != self.job_id for c in shared.get('idea_pool', [])):
                    shared_job.update(status='needs_work', reason='该候选与本批已有方案的机制完全相同，未重复计数。')
                    own = []; break
            shared['idea_pool'] = [c for c in shared.get('idea_pool', []) if c.get('skill_job_id') != self.job_id] + copy.deepcopy(own)
            shared['cards'] = [c for c in shared['idea_pool'] if c.get('skill_status') == 'validated']
            progress = copy.deepcopy(local.get('progress', {}))
            parallel = shared['ideaspark_batch']['parallel']
            parallel['active'][self.job_id] = {
                'number': job['number'], 'stage': job.get('stage', ''),
                'message': progress.get('message', ''), 'title': job['focus'], 'at': now(),
            }
            shared['progress'] = progress
            shared['progress_message'] = local.get('progress_message', '')
            shared['phase'] = 'generate' if shared['ideaspark_batch']['phase'] == 'drafts' else 'audit'
            if self.host.event.is_set():
                shared['status'] = 'stopping'
            self.store.save(shared)
            # Compare only unpublished paper/note changes on subsequent saves.
            self.baseline['reading'] = copy.deepcopy(local.get('reading', {}))
            self.baseline['papers'] = copy.deepcopy(local['papers'])
            self.baseline['warnings'] = copy.deepcopy(local.get('warnings', []))
            self.baseline['search_log'] = copy.deepcopy(local.get('search_log', []))

    def close(self):
        with self.lock:
            self.host.data['ideaspark_batch']['parallel']['active'].pop(self.job_id, None)
            self.store.save(self.host.data)


def run_candidates(host, jobs, phase, workers):
    """A phase barrier preserves breadth-first drafts; each candidate stays serial."""
    from .service import Cancelled
    with host.service.store.lock:
        host.data['ideaspark_batch'].update(phase=phase, parallel={'workers': workers, 'active': {}})
        host.service.store.save(host.data)

    def task(job_id):
        store = CandidateStore(host, job_id)
        worker = copy.copy(host)
        worker.data = store.data
        worker.service = copy.copy(host.service)
        worker.service.store = store
        worker.client = None
        # LLMClient has mutable retry/transport state. Every worker gets its own.
        from researchclaw.llm.client import LLMClient
        if isinstance(worker.service.llm, LLMClient):
            worker.service.llm = LLMClient(worker.service.llm.config)
        job = next(j for j in worker.data['ideaspark_batch']['jobs'] if j['id'] == job_id)
        try:
            worker.process_job(job, phase)
        finally:
            try:
                store.save(worker.data)
            finally:
                store.close()

    failure = None
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='ideaspark') as pool:
        pending = [pool.submit(task, j['id']) for j in jobs]
        for future in as_completed(pending):
            try:
                future.result()
            except Exception as exc:
                if failure is None or not isinstance(exc, Cancelled):
                    failure = exc
                host.event.set()
    if failure is not None:
        raise failure
