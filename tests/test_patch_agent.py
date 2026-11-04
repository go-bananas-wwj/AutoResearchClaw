"""补丁式实现：单步截断只重试该步，恢复时按提示词哈希复用已完成步骤。"""
from __future__ import annotations

import pytest

from researchclaw.llm.client import LLMResponse
from researchclaw.pipeline.patch_agent import (DONE, PatchCodeAgent, PatchError, apply_edits, parse_edits,
                                               static_problems)

SPEC = {'hashes': {'plan': 'p'}, 'primary_metric': {'key': 'CRPS', 'direction': 'minimize'},
        'method_requirements': [{'id': 'M1', 'description': 'wrap', 'acceptance_test': 'W(x+2pi)=W(x)'}]}


def reply(*blocks):
    return LLMResponse(content='\n'.join(blocks) + f'\n{DONE}\n', model='deepseek-flash', finish_reason='stop')


LAYOUT = reply('<<<FILE main.py>>>\nimport method\n<<<END>>>',
               '<<<FILE method.py>>>\ndef wrap(x):\n    pass\n<<<END>>>',
               '<<<FILE method_checks.py>>>\nprint("RC_METHOD_CHECKS_OK")\n<<<END>>>')
REQ = reply('<<<EDIT method.py>>>\n<<<SEARCH>>>\n    pass\n<<<REPLACE>>>\n    return x % 6.283\n<<<END>>>')
WIRE = reply('<<<EDIT main.py>>>\n<<<SEARCH>>>\nimport method\n<<<REPLACE>>>\nimport method\nprint(method.wrap(1.0))\n<<<END>>>')


class Scripted:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def chat(self, messages, **kw):
        self.calls += 1
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _agent(tmp_path, llm):
    stage = tmp_path / 'stage-10'
    stage.mkdir(exist_ok=True)
    return PatchCodeAgent(llm, run_dir=tmp_path, stage_dir=stage, spec=SPEC, plan_text='plan')


def test_parse_and_apply_all_or_nothing():
    edits = parse_edits('<<<FILE a.py>>>\nx=1\n<<<END>>>\n<<<EDIT a.py>>>\n<<<SEARCH>>>\nx=1\n<<<REPLACE>>>\nx=2\n<<<END>>>\n' + DONE)
    assert apply_edits({}, edits) == {'a.py': 'x=2\n'}
    with pytest.raises(PatchError):
        parse_edits('<<<FILE a.py>>>\nx=1\n<<<END>>>')  # no DONE marker: treated as truncated
    with pytest.raises(PatchError):
        apply_edits({'a.py': 'y=1\ny=1\n'}, [{'op': 'edit', 'path': 'a.py', 'search': 'y=1', 'replace': 'z'}])
    with pytest.raises(PatchError):
        apply_edits({}, [{'op': 'file', 'path': '../x.py', 'body': ''}])
    assert static_problems({'a.py': 'def f(:\n'})


def test_truncated_step_retried_alone(tmp_path):
    cut = LLMResponse(content='<<<FILE method.py>>>\ndef wr', model='m', finish_reason='length', truncated=True)
    llm = Scripted([LAYOUT, cut, RuntimeError('推理模型返回截断或空正文'), REQ, WIRE])
    files = _agent(tmp_path, llm).generate()
    assert 'return x % 6.283' in files['method.py'] and 'print(method.wrap' in files['main.py']
    assert llm.calls == 5  # layout once, M1 three attempts, wire once


def test_resume_replays_finished_steps_without_model(tmp_path):
    first = Scripted([LAYOUT, REQ, RuntimeError('network down')] + [RuntimeError('down')] * 2)
    with pytest.raises(PatchError):
        _agent(tmp_path, first).generate()
    second = Scripted([WIRE])
    agent = _agent(tmp_path, second)
    files = agent.generate()
    assert second.calls == 1 and agent.model_calls == 1
    assert [r.reused for r in agent.records][:2] == [True, True]
    assert 'print(method.wrap' in files['main.py']


def test_bad_search_feedback_then_fix(tmp_path):
    bad = reply('<<<EDIT method.py>>>\n<<<SEARCH>>>\nnot there\n<<<REPLACE>>>\nx\n<<<END>>>')
    llm = Scripted([LAYOUT, bad, REQ, WIRE])
    agent = _agent(tmp_path, llm)
    agent.generate()
    m1 = [r for r in agent.records if r.step == 'req:M1'][0]
    assert m1.attempts == 2 and 'SEARCH' in m1.feedback[0]


def test_truncation_doubles_only_that_step_budget(tmp_path):
    class Capped(Scripted):
        config = type('C', (), {'reasoning_max_tokens': 65536})()

        def chat(self, messages, **kw):
            self.budgets.append(kw.get('max_tokens'))
            return super().chat(messages, **kw)
    llm = Capped([LAYOUT, RuntimeError('推理模型返回截断或空正文'), RuntimeError('推理模型返回截断或空正文'),
                  REQ, WIRE])
    llm.budgets = []
    _agent(tmp_path, llm).generate()
    assert llm.budgets == [16384, 16384, 32768, 65536, 16384]  # 下一步骤回到原上限
