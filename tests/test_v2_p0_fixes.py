"""P0：阶段 13 候选须带子包；多视角分析截断重试、全部失败不在空输入上综合。"""
import pytest

from researchclaw.pipeline import _helpers
from researchclaw.pipeline.stage_impls._execution import load_project_files


def test_load_project_files_keeps_subpackages(tmp_path):
    (tmp_path / 'methods').mkdir()
    (tmp_path / 'methods' / '__init__.py').write_text('')
    (tmp_path / 'methods' / 'm1.py').write_text('X = 1\n')
    (tmp_path / 'main.py').write_text('import methods\n')
    (tmp_path / '__pycache__').mkdir()
    (tmp_path / '__pycache__' / 'main.cpython-311.pyc').write_text('junk')
    (tmp_path / '.hidden.py').write_text('')
    files = load_project_files(tmp_path)
    assert set(files) == {'main.py', 'methods/__init__.py', 'methods/m1.py'}


class _LLM:
    def __init__(self, fail):
        self.fail, self.calls = fail, []
        self.config = type('C', (), {'reasoning_max_tokens': 65536, 'max_tokens': 4096})()

    def chat(self, messages, system=None, max_tokens=None, **kw):
        self.calls.append(max_tokens)
        if self.fail or max_tokens is None:
            raise RuntimeError('推理模型返回截断或空正文')
        return type('R', (), {'content': f'ok@{max_tokens}'})()


ROLES = {r: {'system': 's', 'user': 'u {data_context}'} for r in ('optimist', 'skeptic', 'methodologist')}


def test_perspectives_retry_truncation(tmp_path):
    llm = _LLM(fail=False)
    out = _helpers._multi_perspective_generate(llm, ROLES, {'data_context': 'm'}, tmp_path)
    assert set(out) == set(ROLES) and all(v == 'ok@8192' for v in out.values())


def test_all_perspectives_failing_raises(tmp_path):
    with pytest.raises(RuntimeError, match='0/3'):
        _helpers._multi_perspective_generate(_LLM(fail=True), ROLES, {'data_context': 'm'}, tmp_path)
