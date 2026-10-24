"""Whole-paper coverage, isolated model contexts and resumable long reading."""
import json
import threading
from types import SimpleNamespace

import pytest

from researchclaw.ideation.prompts import PROMPTS
from researchclaw.ideation.service import IdeationService, WorkflowFailure, passages, reference_aliases


class Reader:
    def __init__(self):
        self.calls = []
        self.fail_part = None

    def chat(self, messages, *, system, **kwargs):
        payload = json.loads(messages[0]['content'])
        stage = next(k for k, v in PROMPTS.items() if v == system)
        self.calls.append((stage, payload))
        if stage == 'read_part' and payload['part'] == self.fail_part:
            raise RuntimeError('temporary provider failure')
        source = payload['passages'][0]
        result = {k: '本文的模型阅读记录' for k in ('problem', 'method', 'data', 'experiments', 'findings', 'strengths', 'limitations')}
        result.update(has_methods=True, evidence=[{'passage_id': source['passage_id'], 'claim': '原文的方法', 'kind': 'source', 'role': 'method'}])
        return SimpleNamespace(content=json.dumps(result, ensure_ascii=False), model='fake', prompt_tokens=20, completion_tokens=20, finish_reason='stop')


@pytest.fixture
def reader(tmp_path):
    llm = Reader()
    config = SimpleNamespace(llm=SimpleNamespace(primary_model='deepseek-flash', base_url='https://api.deepseek.com/v1'), hitl=SimpleNamespace(cost_budget_usd=0))
    service = IdeationService(tmp_path, config, llm=llm)
    data = service.store.create('论文的独立阅读测试', 0)
    return service, data, threading.Event(), {'paper_id': 'p-long', 'title': 'Long methods paper'}


def document(size):
    text = ('Methods and independent observations. ' * (size // 37 + 1))[:size]
    return {'pages': [{'location': f'第 {i + 1} 页', 'text': text[i * size // 156:(i + 1) * size // 156]} for i in range(156)], 'source_url': 'https://example.org/paper.pdf'}


def test_156_page_paper_uses_one_independent_full_context(reader):
    service, data, event, paper = reader
    source = document(329485)
    note = service._read_document(data, event, paper, source)
    assert [c[0] for c in service.llm.calls] == ['read']
    assert note['coverage'] == {'complete': True, 'paper_id': paper['paper_id'], 'source_characters': 329485, 'processed_characters': 329485, 'parts': 1, 'context_limit_tokens': 1000000}
    assert note['passages'] == passages(paper['paper_id'], source)
    assert data['usage'][0]['paper_ids'] == [paper['paper_id']]
    assert note['evidence'][0]['quote'] == note['passages'][0]['text']
    assert data['reading'] == {}  # Pre-approval reading does not approve/register evidence.


def test_partition_resume_does_not_repeat_finished_parts(reader, monkeypatch):
    service, data, event, paper = reader
    monkeypatch.setattr(service, '_input_limit', lambda stage: 20000)
    source = document(45000)
    service.llm.fail_part = 2
    with pytest.raises(WorkflowFailure, match='Long methods paper'):
        service._read_document(data, event, paper, source)
    assert [c[1]['part'] for c in service.llm.calls] == [1, 2]
    service.llm.fail_part = None
    note = service._read_document(data, event, paper, source)
    stages = [c[0] for c in service.llm.calls]
    assert 'read_merge' in stages and stages.count('read') == 0
    assert [c[1]['part'] for c in service.llm.calls if c[0] == 'read_part'].count(1) == 1
    assert note['coverage']['processed_characters'] == 45000
    assert note['coverage']['parts'] > 1
    assert all(u['paper_ids'] == [paper['paper_id']] for u in data['usage'])
    count = len(service.llm.calls)
    service._read_document(data, event, paper, source)
    assert len(service.llm.calls) == count
    restarted = IdeationService(service.store.root, service.config, llm=Reader())
    monkeypatch.setattr(restarted, '_input_limit', lambda stage: 20000)
    restarted._read_document(restarted.store.load(data['session_id']), threading.Event(), paper, source)
    assert not restarted.llm.calls
    source['pages'][-1]['text'] += ' New verified source.'
    service._read_document(data, event, paper, source)
    assert len(service.llm.calls) > count  # Source changes invalidate checkpoints.


@pytest.mark.parametrize('stage', ['read', 'read_part', 'read_merge', 'summarize'])
def test_model_reading_refuses_mixed_paper_contexts(reader, stage):
    service, data, event, paper = reader
    with pytest.raises(WorkflowFailure, match='一篇论文'):
        service._json(data, event, stage, {'paper_id': paper['paper_id'], 'notes': [{'paper_id': 'p-other'}]})
    assert not service.llm.calls


def test_same_paper_new_context_for_each_call(reader):
    service, data, event, paper = reader
    service._read_document(data, event, paper, document(3000))
    other = {'paper_id': 'p-other', 'title': 'Independent other paper'}
    service._read_document(data, event, other, document(3500))
    assert len(service.llm.calls) == 2
    assert [u['paper_ids'] for u in data['usage']] == [[paper['paper_id']], [other['paper_id']]]


def test_structured_landscape_restores_request_local_paper_ids():
    payload, restore = reference_aliases({'reading': [{'paper_id': 'p-real-hash'}]})
    model_id = payload['reading'][0]['paper_id']
    restored = restore({'sections': [{'paper_ids': [model_id]}]})
    assert restored['sections'][0]['paper_ids'] == ['p-real-hash']
