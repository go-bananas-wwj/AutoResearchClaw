"""P0：阶段10按提示词哈希复用已完成文件；生成失败与写作失败如实失败，不造假不占位。"""
from __future__ import annotations

import pytest

from researchclaw.llm.client import LLMResponse
from researchclaw.pipeline.code_agent import CodeAgent, CodeAgentConfig
from researchclaw.prompts import PromptManager

BLUEPRINT = {'files': [{'name': 'method.py', 'generation_order': 1, 'dependencies': []},
                       {'name': 'main.py', 'generation_order': 2, 'dependencies': ['method.py']}]}


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _ok(code):
    return LLMResponse(content=f'```python\n{code}\n```', model='deepseek-flash', finish_reason='stop')


def test_interrupted_sequential_generation_reuses_finished_files(tmp_path):
    stage = tmp_path / 'stage-10'
    stage.mkdir()
    first = ScriptedLLM([_ok('def estimate(): return 2'), RuntimeError('推理模型返回截断或空正文')])
    agent = CodeAgent(llm=first, prompts=PromptManager(), config=CodeAgentConfig(), stage_dir=stage)
    with pytest.raises(RuntimeError):
        agent._phase2_sequential_generate('t', 'plan', 'crps', '', '', BLUEPRINT)
    # Resume archives stage-10; the checkpoint cache sits beside it in the run dir.
    stage.rename(tmp_path / 'stage-10-archived')
    stage.mkdir()
    second = ScriptedLLM([_ok('from method import estimate\nprint(estimate())')])
    agent = CodeAgent(llm=second, prompts=PromptManager(), config=CodeAgentConfig(), stage_dir=stage)
    files = agent._phase2_sequential_generate('t', 'plan', 'crps', '', '', BLUEPRINT)
    assert second.calls == 1  # method.py replayed, only main.py hit the model
    assert files['method.py'] == 'def estimate(): return 2'
    assert (stage / 'native_draft' / 'method.py').is_file()


def test_truncated_reply_is_never_cached(tmp_path):
    stage = tmp_path / 'stage-10'
    stage.mkdir()
    cut = LLMResponse(content='```python\ndef estim', model='m', finish_reason='length', truncated=True)
    agent = CodeAgent(llm=ScriptedLLM([cut]), prompts=PromptManager(), config=CodeAgentConfig(), stage_dir=stage)
    agent._chat('s', 'u', reuse=True)
    assert not list((tmp_path / '.cache').rglob('*.json'))


def test_stage10_has_no_synthetic_fallback_source():
    import inspect

    from researchclaw.pipeline.stage_impls import _code_generation
    source = inspect.getsource(_code_generation)
    assert 'synthetic objective' not in source and 'Fallback experiment' not in source


def test_paper_draft_part_fails_instead_of_placeholder():
    from researchclaw.pipeline.stage_impls._paper_writing import PaperDraftError, _draft_part

    class Fails:
        def chat(self, *a, **kw):
            raise RuntimeError('boom')

    class Cut:
        def chat(self, *a, **kw):
            return LLMResponse(content='## Method\n半', model='m', finish_reason='length', truncated=True)

    for llm in (Fails(), Cut()):
        with pytest.raises(PaperDraftError):
            _draft_part(llm, 'sys', 'user', 1000, 'Part 2')


def test_truncation_escalates_only_that_call():
    from types import SimpleNamespace

    from researchclaw.pipeline._helpers import _chat_with_prompt

    class LLM:
        config = SimpleNamespace(max_tokens=4096, reasoning_max_tokens=65536)

        def __init__(self):
            self.seen = []

        def chat(self, messages, **kw):
            self.seen.append(kw.get('max_tokens'))
            if (kw.get('max_tokens') or 4096) < 16384:
                raise RuntimeError('All models failed. Last error: 推理模型返回截断或空正文')
            return LLMResponse(content='ok', model='m', finish_reason='stop')

    llm = LLM()
    assert _chat_with_prompt(llm, 's', 'u').content == 'ok'
    assert llm.seen == [None, 8192, 16384]

    class Always(LLM):
        def chat(self, messages, **kw):
            self.seen.append(kw.get('max_tokens'))
            raise RuntimeError('推理模型返回截断或空正文')
    always = Always()
    with pytest.raises(RuntimeError):
        _chat_with_prompt(always, 's', 'u')
    assert always.seen == [None, 8192, 16384, 32768]  # bounded, truncated output never accepted


def test_failure_signature_tracks_check_not_line_numbers():
    from researchclaw.pipeline.execution_contract import failure_signature
    tb = ('Traceback (most recent call last):\n  File "/workspace/method_checks.py", line {n}, in main\n'
          '    check()\n  File "/workspace/method_checks.py", line {m}, in {fn}\n    assert x\nAssertionError: {msg}\n')
    a = failure_signature(tb.format(n=517, m=99, fn="check_m2_coherence", msg=""))
    b = failure_signature(tb.format(n=537, m=99, fn="check_m2_coherence", msg=""))
    c = failure_signature(tb.format(n=537, m=477, fn="check_m10_oracle_calibration", msg="coverage 1.0"))
    assert a == b and a != c
