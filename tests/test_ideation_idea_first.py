"""Idea selection, real receipts and handoff boundaries; no network/model calls."""
import copy
import threading

import pytest

from researchclaw.ideation.idea_first import IdeaFirstBatch, WORKFLOW, verify_idea_card
from researchclaw.ideation.ideaspark import IdeaSparkBatch, read, write
from researchclaw.ideation.prompts import PROMPTS
from researchclaw.ideation.store import SessionError, fingerprint
from tests.test_grounded_ideation import engine, develop


def prepared(engine, count=1):
    data = develop(engine)
    template = copy.deepcopy(data['cards'][0])
    data.update(cards=[], idea_pool=[], phase='audit', status='running')
    jobs = []
    h = IdeaFirstBatch(engine, data, threading.Event())
    for i in range(count):
        job = {'id': str(i), 'number': i+1, 'focus': 'idea '+str(i), 'directory': 'idea-'+str(i),
               'status': 'needs_work', 'steps': 9, 'stage': 'formula_repair', 'reason': 'old technical error'}
        pid = next(iter(data['reading']))
        native = {'title': 'idea '+str(i), 'core_mechanism': 'new question '+str(i),
            'scientific_question': 'hypothesis '+str(i), 'falsification_prediction': 'compare with baseline',
            'differentiation_from_lit': [{'paper_id': pid, 'difference': 'test a different assumption'}],
            'collision_queries': ['mechanism '+str(i)]}
        write(h.directory/job['directory']/'phase2_generate/phase2_generate_output.json', native)
        card = {**copy.deepcopy(template), 'skill_job_id': job['id'], 'skill_status': 'needs_work',
                'card_id': 'r1-spark-'+str(i+1), 'native_candidate_hash': fingerprint(native)}
        card.pop('audit', None)
        data['idea_pool'].append(card)
        job['presentation_hash'] = fingerprint([native, PROMPTS['spark_card']])
        jobs.append(job)
    data['ideaspark_batch'] = {'jobs': jobs, 'scope_revision': data['revision'], 'target': count}
    engine.store.save(data)
    return h, jobs


def comparisons(h, stage, system, payload, *args, **kwargs):
    assert stage == 'neighbor_check'  # Any technical/model audit call fails this test.
    return {'summary': '摘要和已有解读层面比较，未证明新颖性。', 'comparisons': [
        {'paper_id': p['paper_id'], 'relationship': 'possible_overlap', 'difference': '可能重合，需要在实验设计时明确差异。'}
        for p in payload['papers'][:1]]}


def test_existing_drafts_skip_all_deep_checks_and_keep_selectable_overlap(engine, monkeypatch):
    h, jobs = prepared(engine, 3)
    monkeypatch.setattr(IdeaFirstBatch, 'model', comparisons)
    monkeypatch.setattr(IdeaSparkBatch, 'step', lambda *a: pytest.fail('full quality navigator must not run'))
    h.run()
    assert h.data['phase'] == 'idea_review' and h.data['status'] == 'waiting'
    assert len(h.data['cards']) == 3 and not h.data['run_id']
    assert all(j['status'] == 'idea_ready' for j in jobs)
    assert h.data['ideaspark_batch']['workflow'] == WORKFLOW
    assert not h.data['ideaspark_batch']['parallel']['active']
    for card in h.data['cards']:
        assert card['audit']['verdict'] == 'checked' and card['selection_ready']
        assert card['neighbor_check']['comparisons'][0]['relationship'] == 'possible_overlap'
        verify_idea_card(engine, h.data, card)
    assert h.data['reviews'][-1]['previous_batch']['jobs'][0]['stage'] == 'formula_repair'


def test_unavailable_neighbors_are_visible_and_do_not_block_idea_selection(engine, monkeypatch):
    h, jobs = prepared(engine)
    monkeypatch.setattr(engine.sources, 'search', lambda *a: [])
    monkeypatch.setattr(IdeaFirstBatch, 'model', lambda *a, **kw: (_ for _ in ()).throw(ValueError('fixture transport error')))
    h.run()
    card = h.data['cards'][0]
    assert card['neighbor_check']['status'] == 'partial'
    assert len(card['neighbor_check']['issues']) == 2
    assert card['audit']['verdict'] != 'pass'
    saved, chosen = engine.confirmation(h.data['session_id'], h.data['revision'], card['card_id'])
    assert chosen['selection_ready'] and not saved['run_id']
    brief, path = engine.export(saved, chosen)
    assert '阶段9负责' in brief['experiment_scope'] and '相近研究比较未完成' in brief['experiment_scope']
    assert read(path/'ideaspark/idea_review/neighbors.json')['status'] == 'partial'
    assert not list(path.glob('stage-09/*'))


def test_changed_candidate_or_check_cannot_reuse_selection_receipt(engine, monkeypatch):
    h, jobs = prepared(engine)
    monkeypatch.setattr(IdeaFirstBatch, 'model', comparisons)
    h.run()
    card = h.data['cards'][0]
    run = h.directory/jobs[0]['directory']
    original = read(run/'phase2_generate/phase2_generate_output.json')
    write(run/'phase2_generate/phase2_generate_output.json', {**original, 'core_mechanism': 'changed claim'})
    with pytest.raises(SessionError, match='记录已变化'):
        verify_idea_card(engine, h.data, card)
    write(run/'phase2_generate/phase2_generate_output.json', original)
    receipt = read(run/'idea_review/neighbors.json')
    write(run/'idea_review/neighbors.json', {**receipt, 'summary': 'different comparison'})
    with pytest.raises(SessionError, match='记录已变化'):
        verify_idea_card(engine, h.data, card)


def test_new_idea_uses_official_reference_prompts_and_has_no_execution_stage(engine, monkeypatch):
    h, jobs = prepared(engine)
    job = jobs[0]
    native_path = h.directory/job['directory']/'phase2_generate/phase2_generate_output.json'
    native = read(native_path);native_path.unlink()
    def model(host, stage, system, payload, *args, **kwargs):
        assert stage == 'idea_generation'
        assert 'ideate_generate.txt' in ' '.join(payload['official_references'])
        assert payload['reading'] and payload['focus'] == job['focus']
        assert '不执行代码' in system and '阶段9' in system
        return native
    monkeypatch.setattr(IdeaFirstBatch, 'model', model)
    h.generate_idea(job)
    assert h.candidate(job) == native
    assert not list((h.directory/job['directory']).glob('host_executions/*'))


@pytest.mark.asyncio
async def test_selecting_an_idea_hands_unknown_data_to_stage9_once(engine, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from fastapi import FastAPI
    from researchclaw.server.app import _app_state
    from researchclaw.server.routes import ideation, pipeline
    h, _ = prepared(engine)
    monkeypatch.setattr(IdeaFirstBatch, 'model', comparisons)
    h.run()
    card = h.data['cards'][0]
    card['data_sources'] = [{'name': 'not chosen yet', 'availability': 'unknown', 'url': ''}]
    engine.store.save(h.data)
    monkeypatch.setitem(_app_state, 'ideation_service', engine)
    monkeypatch.setattr(ideation, '_check_data_access', lambda *a: pytest.fail('Data selection belongs to stage 9'))
    requests = []
    async def start(req):
        requests.append(req)
        return SimpleNamespace(run_id='rc-fixture-idea-first')
    monkeypatch.setattr(pipeline, 'start_pipeline', start)
    app = FastAPI();app.include_router(ideation.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        url = '/api/ideation/sessions/'+h.data['session_id']+'/confirm'
        body = {'revision': h.data['revision'], 'card_id': card['card_id']}
        first = await client.post(url, json=body)
        again = await client.post(url, json=body)
    assert first.status_code == again.status_code == 200
    assert len(requests) == 1 and requests[0].from_stage == 'EXPERIMENT_DESIGN'
    assert '数据可得性' in requests[0].brief['experiment_scope']
    assert requests[0].auto_approve is True
