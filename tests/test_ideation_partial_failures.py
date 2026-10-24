"""A source outage is reported per paper and does not stop unrelated candidates."""
import json

from researchclaw.ideation.prompts import PROMPTS
from researchclaw.ideation.sources import paper_record
from tests.test_grounded_ideation import engine, develop, first, wait, Sources


def unavailable_neighbor(engine, relation='alternative_mechanism', two_candidates=False):
    missing = paper_record({'title': 'Unavailable comparison paper', 'doi': '10.1234/unavailable', 'url': 'https://example.org/missing?api_key=private-value'}, source='test')
    search = engine.sources.search
    engine.sources.search = lambda query, period, source: [missing] if query == 'collision missing' else search(query, period, source)
    fetch = engine.sources.fulltext
    def fulltext(paper, directory):
        if paper['paper_id'] == missing['paper_id']:
            engine.sources.fulltext_calls.append(paper['paper_id'])
            paper['fetch_attempts'] = [{'url': paper['url'], 'outcome': '访问被拒绝'}]
            raise ValueError('访问被拒绝')
        return fetch(paper, directory)
    engine.sources.fulltext = fulltext
    chat = engine.llm.chat
    audits = []
    def model(messages, *, system, **kwargs):
        response = chat(messages, system=system, **kwargs)
        payload = json.loads(messages[0]['content'])
        result = json.loads(response.content)
        if system == PROMPTS['generate']:
            result['cards'][0]['collision_queries'] = ['collision missing']
            if two_candidates:
                other = {**result['cards'][0], 'title': 'Independent supported candidate', 'collision_queries': ['collision good']}
                result['cards'].append(other)
        if system == PROMPTS['neighbors']:
            for item, paper in zip(result['papers'], payload['papers']):
                if paper['title'] == missing['title']:
                    item['relationship'] = relation
        if system == PROMPTS['audit']:
            audits.append(payload)
        response.content = json.dumps(result)
        return response
    engine.llm.chat = model
    return missing, audits


def test_unavailable_optional_comparison_is_deferred_to_final_report(engine):
    missing, audits = unavailable_neighbor(engine)
    data = develop(engine)
    assert data['status'] == 'waiting' and data['phase'] == 'idea_review'
    assert len(data['cards']) == 1 and not data['run_id']
    warning = data['warnings'][0]
    assert warning['context']['paper_title'] == missing['title']
    assert warning['context']['candidate_title'] == data['cards'][0]['title']
    assert warning['context']['reason'] == '访问被拒绝'
    assert warning['context']['attempts'][0]['url'] == 'https://example.org/missing'
    assert 'private-value' not in json.dumps(warning)
    assert not warning['context']['critical']
    unread = audits[0]['unread_neighbors'][0]
    assert unread['title'] == missing['title']
    assert unread['paper_id'] not in {n['paper_id'] for n in audits[0]['neighbors']}
    assert data['missing'] == []


def test_missing_same_claim_holds_one_candidate_but_other_candidate_finishes(engine):
    missing, audits = unavailable_neighbor(engine, relation='same_claim', two_candidates=True)
    data = develop(engine)
    assert data['phase'] == 'idea_review' and data['status'] == 'waiting'
    assert [c['title'] for c in data['cards']] == ['Independent supported candidate']
    failure = data['rejected_cards'][0]['failure']
    assert failure['recoverable'] and failure['category'] == 'evidence'
    assert failure['context']['paper_id'] == missing['paper_id']
    assert failure['context']['critical']
    assert len(audits) == 1  # No false novelty pass with missing same-claim work.
    assert not data['run_id'] and data['missing'] == []


def test_all_candidates_with_missing_key_work_refine_before_final_summary(engine):
    missing, audits = unavailable_neighbor(engine, relation='same_mechanism')
    data = develop(engine)
    assert engine.llm.calls.count('generate') == 2
    assert not audits and not data['cards']
    assert data['blocker']['code'] == 'candidate_evidence_incomplete'
    assert data['warnings'] and data['prior_rejections']
    assert all(w['context']['paper_id'] == missing['paper_id'] for w in data['warnings'])
    assert engine.sources.fulltext_calls.count(missing['paper_id']) == 1


def test_explicit_resume_retries_recovered_source(engine):
    missing, audits = unavailable_neighbor(engine, relation='same_claim')
    data = develop(engine)
    assert data['status'] == 'blocked'
    engine.sources.fulltext = lambda paper, directory: Sources.fulltext(engine.sources, paper, directory)
    engine.resume(data['session_id'], data['revision'])
    done = wait(engine, data['session_id'])
    assert done['phase'] == 'idea_review' and done['cards']
    assert missing['paper_id'] in done['reading']
    assert engine.sources.fulltext_calls.count(missing['paper_id']) == 2


def test_old_error_gets_real_paper_identity_without_changing_approval(engine):
    data = first(engine)
    old = '未取得方法正文：访问被拒绝'
    data.update(status='blocked', phase='audit', missing=[old], blocker={'message': old, 'category': 'technical', 'code': 'stage_failed'},
        rejected_cards=[{'title': 'Affected candidate', 'card_id': 'r1-c1', 'failure': {'message': old},
                         'closest_papers': [{'paper_id': 'p-missing', 'title': 'Identifiable original paper', 'doi': '10.1234/id', 'url': 'https://example.org/id',
                                             'fetch_attempts': [{'url': 'https://example.org/file?token=private', 'outcome': '访问被拒绝'}]}]}])
    engine.store.save(data)
    snapshot = engine.snapshot(data['session_id'])
    assert snapshot['blocker']['context']['paper_title'] == 'Identifiable original paper'
    assert snapshot['blocker']['context']['candidate_title'] == 'Affected candidate'
    assert 'Identifiable original paper' in snapshot['missing'][0]
    assert 'private' not in json.dumps(snapshot['blocker'])
    assert snapshot['approved_revision'] == data['approved_revision']
    assert snapshot['reading'] == data['reading'] and not snapshot['run_id']
