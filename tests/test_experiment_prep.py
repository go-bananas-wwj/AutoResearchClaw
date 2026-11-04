"""实验准备：模型写 prepare.py，宿主实际运行、核对来源与数组，登记后供阶段 10 只读使用。"""
from __future__ import annotations

import json
import sys

import numpy as np
import pytest

from researchclaw.config import ExecutionCheckConfig, ExperimentConfig, SandboxConfig
from researchclaw.data_acquisition.manifest import Manifest
from researchclaw.llm.client import LLMResponse
from researchclaw.pipeline import experiment_prep as ep
from researchclaw.pipeline.experiment_spec import manifest_summary
from researchclaw.pipeline.patch_agent import DONE


class Scripted:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def chat(self, messages, **kw):
        self.calls += 1
        return LLMResponse(content=self.replies.pop(0), model="deepseek-flash", finish_reason="stop")


def _setup(tmp_path):
    base = tmp_path / "rc-data"
    raw = base / "shared" / "site_phase.npy"
    raw.parent.mkdir(parents=True)
    np.save(raw, np.arange(6, dtype=float).reshape(2, 3))
    run_dir = tmp_path / "artifacts" / "rc-x"
    run_dir.mkdir(parents=True)
    manifest_dir = base / "runs" / "rc-x"
    m = Manifest(manifest_dir, files_root=base)
    m.add_file("site/phase", raw, source_url="https://x", license="c", kind="insar_product", target="site")
    m.save()
    spec = {"hashes": {"plan": "p", "brief": "b"}, "method_requirements": [],
            "datasets": [{"id": "R", "kind": "local", "path": str(manifest_dir)},
                         {"id": "S", "kind": "synthetic", "generator": "seed 0"}],
            "real_data": [manifest_summary(manifest_dir)]}
    exp = ExperimentConfig(mode="sandbox", time_budget_sec=60,
                           sandbox=SandboxConfig(python_path=sys.executable),
                           execution_check=ExecutionCheckConfig(enabled=True, data_base=str(base)))
    config = type("C", (), {"experiment": exp})()
    return config, run_dir, spec, raw


def _script(raw, *, sources=True):
    src = "['site/phase']" if sources else "[]"
    body = (
        "import json, os, numpy as np\n"
        "os.makedirs('prepared', exist_ok=True)\n"
        f"a = np.load({str(raw)!r})\n"
        "np.save('prepared/site_phase.npy', a * 2)\n"
        "np.savez('prepared/synthetic.npz', x=np.random.default_rng(0).normal(size=(4,)))\n"
        "json.dump([{'dataset_id': 'R', 'file': 'site_phase.npy', 'description': 'phase', 'units': 'rad',"
        f" 'sources': {src}}},\n"
        "           {'dataset_id': 'S', 'file': 'synthetic.npz', 'description': 'syn', 'units': '-',"
        " 'sources': []}], open('prepared/index.json', 'w'))\n"
        "print('RC_PREP_OK')\n")
    return f"<<<FILE prepare.py>>>\n{body}<<<END>>>\n{DONE}\n"


def test_check_prepared_rejects_unsourced_real_data_and_missing_sets(tmp_path):
    out = tmp_path / "prepared"
    out.mkdir()
    np.save(out / "a.npy", np.ones(3))
    spec = {"datasets": [{"id": "R", "kind": "local"}, {"id": "S", "kind": "synthetic"}]}
    raw = {"site/phase": {}}
    (out / "index.json").write_text(json.dumps([{"dataset_id": "R", "file": "a.npy", "sources": []}]))
    errors, _ = ep.check_prepared(out, spec, raw)
    assert any("必须列出读取的原始清单条目" in e for e in errors) and any("'S'" in e and "没有准备好的文件" in e for e in errors)
    (out / "index.json").write_text(json.dumps([{"dataset_id": "R", "file": "a.npy", "sources": ["nope"]}]))
    assert any("不在原始清单" in e for e in ep.check_prepared(out, spec, raw)[0])


def test_prepare_runs_fixes_publishes_and_reuses(tmp_path):
    config, run_dir, spec, raw = _setup(tmp_path)
    fix = _script(raw).replace("<<<FILE prepare.py>>>", "<<<FILE prepare.py>>>", 1)
    llm = Scripted([_script(raw, sources=False), fix])
    card = ep.ensure_prepared(config, run_dir, llm, spec, "plan")
    assert llm.calls == 2  # 第一次真实数据没列来源，被宿主拒绝并反馈；修复后通过
    attempt = json.loads((run_dir / "prep" / "attempt.json").read_text())
    assert attempt["errors"] == []
    root = tmp_path / "rc-data" / "runs" / "rc-x" / "prepared"
    assert card["root"] == str(root) and (root / "site_phase.npy").is_file()
    rows = {r["dataset_id"]: r for r in card["files"]}
    assert rows["R"]["arrays"][0]["shape"] == [2, 3] and rows["R"]["sources"] == ["site/phase"]
    assert Manifest(root).verify()["ok"]
    summary = ep.card_summary(card)
    assert "sha256" not in summary["files"][0] and summary["files"][0]["path"].startswith(str(root))
    again = Scripted([])
    assert ep.ensure_prepared(config, run_dir, again, spec, "plan")["root"] == str(root)
    assert again.calls == 0  # 输入未变且文件哈希完好：直接复用


def test_prepare_disabled_without_data_base(tmp_path):
    config, run_dir, spec, _ = _setup(tmp_path)
    config.experiment = ExperimentConfig(execution_check=ExecutionCheckConfig(enabled=True, data_base=""))
    assert ep.ensure_prepared(config, run_dir, Scripted([]), spec, "plan") is None


def test_tampered_raw_data_blocks_preparation(tmp_path):
    config, run_dir, spec, raw = _setup(tmp_path)
    np.save(raw, np.zeros((2, 3)))
    with pytest.raises(ep.PrepError, match="原始数据清单复核失败"):
        ep.ensure_prepared(config, run_dir, Scripted([]), spec, "plan")


def test_identical_arrays_for_different_targets_are_rejected(tmp_path):
    out = tmp_path / "prepared"
    out.mkdir()
    crop = np.arange(64 * 64, dtype=np.float32).reshape(64, 64)
    mask = np.ones((64, 64), bool)
    np.savez(out / "a.npz", phase=crop, dates=np.arange(3), valid=mask)
    np.savez(out / "b.npz", phase=crop, dates=np.arange(3), valid=mask)
    spec = {"datasets": [{"id": "R", "kind": "local"}]}
    raw = {"A/x": {"target": "A"}, "B/x": {"target": "B"}}
    (out / "index.json").write_text(json.dumps([
        {"dataset_id": "R", "file": "a.npz", "sources": ["A/x"]},
        {"dataset_id": "R", "file": "b.npz", "sources": ["B/x"]}]))
    errors, _ = ep.check_prepared(out, spec, raw)
    assert len(errors) == 1 and "['phase']" in errors[0] and "不是独立样本" in errors[0]  # 一维共享元数据不算
    np.savez(out / "b.npz", phase=crop + 1, dates=np.arange(3))
    assert ep.check_prepared(out, spec, raw)[0] == []


def test_array_with_all_false_validity_mask_is_rejected(tmp_path):
    out = tmp_path / "prepared"
    out.mkdir()
    np.savez(out / "a.npz", bperp=np.zeros(5), bperp_valid=np.zeros(5, bool), phase=np.ones((4, 4)),
             phase_valid=np.ones((4, 4), bool))
    spec = {"datasets": [{"id": "R", "kind": "local"}]}
    (out / "index.json").write_text(json.dumps([{"dataset_id": "R", "file": "a.npz", "sources": ["A/x"]}]))
    errors, _ = ep.check_prepared(out, spec, {"A/x": {"target": "A"}})
    assert errors and "['bperp']" in errors[0] and "有效掩码全部为 False" in errors[0]
