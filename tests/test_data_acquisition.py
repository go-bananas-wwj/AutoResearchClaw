"""真实数据获取：断点续传、按文件校验、HyP3 不重复提交。"""
from __future__ import annotations

import io
import zipfile

import pytest

from researchclaw.data_acquisition import download, insar_landslide
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


def test_plan_pairs_nearest_two():
    scenes = [{'granule': g} for g in 'ABCD']
    assert insar_landslide.plan_pairs(scenes) == [('A', 'B'), ('A', 'C'), ('B', 'C'), ('B', 'D'), ('C', 'D')]


class FakeHyp3:
    def __init__(self, existing):
        self.existing, self.submitted = existing, []

    def jobs(self, name):
        return self.existing

    def credits(self):
        return 100

    def submit(self, name, pairs):
        self.submitted.extend(pairs)


def test_submit_missing_skips_existing_pairs(tmp_path):
    state = {'tracks': {'s/b': {'name': 'n', 'pairs': [['A', 'B'], ['B', 'C']]}}}
    hyp3 = FakeHyp3([{'job_parameters': {'granules': ['A', 'B']}, 'status_code': 'RUNNING'},
                     {'job_parameters': {'granules': ['B', 'C']}, 'status_code': 'FAILED'}])
    assert insar_landslide.submit_missing(tmp_path, hyp3, state) == 1
    assert hyp3.submitted == [('B', 'C')]  # failed job resubmitted, running one not


def test_collect_extracts_and_hashes_products(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('P/P_wrapped_phase.tif', b'phase')
        z.writestr('P/P_corr.tif', b'corr')
        z.writestr('P/P_amp.tif', b'skip')
    payload = buf.getvalue()

    def fake_fetch(url, dest, expected_size=None, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return dest
    monkeypatch.setattr(insar_landslide, 'fetch', fake_fetch)
    ref, sec = 'S1_272973_IW3_20170601T000000_VV_AAAA-BURST', 'S1_272973_IW3_20170613T000000_VV_BBBB-BURST'
    job = {'job_id': 'j1', 'status_code': 'SUCCEEDED', 'job_parameters': {'granules': [ref, sec]},
           'files': [{'url': 'https://hyp3/p.zip', 'size': len(payload), 'filename': 'p.zip'}]}
    state = {'tracks': {'xinmo/b': {'name': 'n', 'site': 'xinmo', 'burst': 'b', 'pairs': [[ref, sec]]}}}
    counts = insar_landslide.collect(tmp_path, FakeHyp3([job]), state)
    assert counts == {'SUCCEEDED': 1}
    m = Manifest(tmp_path)
    ids = sorted(e['id'] for e in m.data['entries'])
    assert ids == ['xinmo/b/20170601_20170613/P_corr.tif', 'xinmo/b/20170601_20170613/P_wrapped_phase.tif']
    assert m.verify()['ok'] and not (tmp_path / 'downloads' / 'p.zip').exists()
    # second pass is a no-op (.complete flag)
    monkeypatch.setattr(insar_landslide, 'fetch', lambda *a, **k: pytest.fail('re-download'))
    insar_landslide.collect(tmp_path, FakeHyp3([job]), state)
