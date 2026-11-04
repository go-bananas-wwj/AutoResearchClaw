"""真实数据获取：断点续传、按文件校验、HyP3 不重复提交。"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from researchclaw.data_acquisition import download, planner, sources
from researchclaw.data_acquisition.manifest import Manifest


class Resp:
    def __init__(self, body, status=200, headers=None, fail_after=None):
        self.body, self.status_code, self.headers, self.fail_after = body, status, headers or {}, fail_after

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise download.requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        for i, b in enumerate([self.body[k:k + 4] for k in range(0, len(self.body), 4)]):
            if self.fail_after is not None and i >= self.fail_after:
                raise download.requests.ConnectionError('reset')
            yield b


class Session:
    def __init__(self, data):
        self.data, self.calls = data, []

    def get(self, url, headers=None, **kw):
        self.calls.append(dict(headers or {}))
        start = int(headers['Range'].split('=')[1].rstrip('-')) if headers and 'Range' in headers else 0
        fail = 1 if len(self.calls) == 1 else None
        return Resp(self.data[start:], 206 if start else 200,
                    {'Content-Length': str(len(self.data) - start)}, fail_after=fail)


def test_fetch_resumes_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(download.time, 'sleep', lambda s: None)
    data = b'0123456789abcdef'
    session = Session(data)
    dest = download.fetch('https://x/f.tif', tmp_path / 'f.tif', expected_size=len(data), session=session)
    assert dest.read_bytes() == data
    assert session.calls[1]['Range'] == 'bytes=4-'  # second attempt continued, not restarted
    assert not (tmp_path / 'f.tif.part').exists()


def test_manifest_verify_detects_tampering(tmp_path):
    f = tmp_path / 'a.tif'
    f.write_bytes(b'abc')
    m = Manifest(tmp_path)
    m.add_file('a', f, source_url='https://x', license='open', kind='dem')
    m.add_reference('r', source_url='https://doi.org/x', license='citation')
    m.save()
    assert Manifest(tmp_path).verify()['ok']
    f.write_bytes(b'abd')
    assert Manifest(tmp_path).verify()['mismatched'] == ['a']


def test_pairs_nearest_neighbours():
    scenes = [{'granule': g, 'time': t} for g, t in zip('ABCD', ('1', '2', '3', '4'))]
    assert sources._pairs(scenes, 2) == [('A', 'B'), ('A', 'C'), ('B', 'C'), ('B', 'D'), ('C', 'D')]


def _run_with_paper(tmp_path):
    run = tmp_path / 'rc-20261025-145941-c30e99'
    paper = run / 'ideation_evidence' / 'related work' / 'p-1'
    paper.mkdir(parents=True)
    (paper / 'fulltext.txt').write_text('… the landslide occurred in the early morning of 24 June 2017 and killed …')
    (run / 'ideation_evidence' / 'manifest.json').write_text(json.dumps({'papers': [
        {'paper_id': 'p-1', 'title': 'T', 'selected': True}]}))
    (run / 'research_brief.json').write_text(json.dumps({'topic': 't', 'metrics': []}))
    return run


class FakeTool:
    name = 'fake'
    describe = 'fake'

    def __init__(self):
        self.acquired = []

    def estimate(self, params, target):
        if params.get('bad'):
            raise ValueError('collection does not exist')
        return {'credits': params.get('n', 1), 'gb': 0.1}

    def acquire(self, params, ctx):
        f = ctx.base / 'shared' / 'fake' / f"{ctx.target['id']}.bin"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b'x')
        ctx.manifest.add_file(f"{ctx.target['id']}/fake", f, source_url='https://x', license='open', kind='fake')
        self.acquired.append(ctx.target['id'])


PLAN = {'targets': [{'id': 'xinmo', 'name': 'Xinmo', 'lat': 32.07, 'lon': 103.66, 'event_date': '2017-06-24',
                     'reference': {'paper_id': 'p-1', 'quote': 'occurred in the early morning of 24 June 2017',
                                   'claim': 'failure date'}}],
        'acquisitions': [{'tool': 'fake', 'target': 'xinmo', 'params': {'n': 3}}]}


def test_validate_requires_verbatim_quote_registry_and_budget(tmp_path, monkeypatch):
    run = _run_with_paper(tmp_path)
    monkeypatch.setitem(sources.REGISTRY, 'fake', FakeTool())
    monkeypatch.setattr(planner, 'REGISTRY', sources.REGISTRY)
    errors, est = planner.validate(PLAN, run, {'max_credits': 10, 'max_gb': 1})
    assert errors == [] and est['total']['credits'] == 3
    bad = json.loads(json.dumps(PLAN))
    bad['targets'][0]['reference']['quote'] = 'failed on 25 June 2017'  # not in the paper
    bad['acquisitions'].append({'tool': 'made_up', 'target': 'xinmo'})
    bad['acquisitions'].append({'tool': 'fake', 'target': 'xinmo', 'params': {'bad': True}})
    errors, _ = planner.validate(bad, run, {'max_credits': 2, 'max_gb': 1})
    joined = ' '.join(errors)
    assert '逐字' in joined and '未注册' in joined and '参数检索失败' in joined and '超过预算' in joined


def test_draft_revises_on_feedback_then_executes(tmp_path, monkeypatch):
    from researchclaw.llm.client import LLMResponse
    run = _run_with_paper(tmp_path)
    tool = FakeTool()
    monkeypatch.setitem(sources.REGISTRY, 'fake', tool)
    wrong = json.loads(json.dumps(PLAN))
    wrong['targets'][0]['reference']['quote'] = '24 June 2017 failure'

    class LLM:
        def __init__(self):
            self.replies = [wrong, PLAN]
            self.prompts = []

        def chat(self, messages, **kw):
            self.prompts.append(json.loads(messages[0]['content']))
            return LLMResponse(content=json.dumps(self.replies.pop(0)), model='m', finish_reason='stop')
    llm = LLM()
    accepted = planner.draft(llm, run, {'max_credits': 10, 'max_gb': 1})
    assert llm.prompts[1]['previous_errors'] and accepted['targets'][0]['id'] == 'xinmo'
    import shutil
    shutil.rmtree(run / 'stage-09')  # resume archives the stage dir
    again = planner.draft(llm, run, {'max_credits': 10, 'max_gb': 1})  # no reply left: must not call the model
    assert again['targets'] == accepted['targets'] and len(llm.prompts) == 2
    # A stricter contract re-opens the saved plan and feeds its errors to the model.
    llm.replies = [PLAN]
    with pytest.raises(RuntimeError):
        planner.draft(llm, run, {'max_credits': 2, 'max_gb': 1}, max_rounds=1)
    assert any('超过预算' in e for e in llm.prompts[-1]['previous_errors'])
    base = tmp_path / 'data'
    mdir = planner.execute(accepted, run, base, should_stop=lambda: False)
    m = Manifest(mdir)
    assert tool.acquired == ['xinmo'] and m.verify()['ok']
    ref = m.get('xinmo/reference')
    assert ref['failure_date'] == '2017-06-24' and ref['quote'].startswith('occurred')


def test_no_project_specific_choices_in_sources():
    import inspect
    text = inspect.getsource(sources) + inspect.getsource(planner)
    for word in ('Xinmo', 'Baige', 'Mud Creek', 'Brienz', '103.665', '272973'):
        assert word not in text


def test_targets_too_close_are_not_independent(tmp_path, monkeypatch):
    run = _run_with_paper(tmp_path)
    monkeypatch.setitem(sources.REGISTRY, 'fake', FakeTool())
    plan = json.loads(json.dumps(PLAN))
    twin = dict(plan['targets'][0], id='twin', lat=32.0705, lon=103.6605)
    plan['targets'].append(twin)
    errors, _ = planner.validate(plan, run, {'max_credits': 10, 'max_gb': 1})
    assert any('不能作为独立研究对象' in e for e in errors)


def test_manifest_root_is_accepted_as_local_data(tmp_path):
    from researchclaw.pipeline.execution_contract import validate_plan
    root = tmp_path / "runs" / "rc-x"
    root.mkdir(parents=True)
    plan = {"execution": {"gpu_count": 0, "estimated_runtime_sec": 1, "memory_mb": 1,
                          "validation_level": "real",
                          "datasets": [{"id": "real", "kind": "local", "path": str(root)}]}}
    resources = {"gpu_count": 1, "time_budget_sec": 100, "memory_limit_mb": 100, "datasets": [],
                 "real_data": [{"root": str(root)}]}
    errors = validate_plan(plan, resources, {})
    assert not any("本地数据未经清单验证" in e for e in errors)
    resources["real_data"] = []
    assert any("本地数据未经清单验证" in e for e in validate_plan(plan, resources, {}))


def test_manifest_remove_drops_stale_entry(tmp_path):
    m = Manifest(tmp_path)
    m.add_reference("old/reference", source_url="p", license="c", target="old")
    m.add_reference("new/reference", source_url="p", license="c", target="new")
    m.remove("old/reference")
    m.save()
    again = Manifest(tmp_path)
    assert "old/reference" not in again and "new/reference" in again
