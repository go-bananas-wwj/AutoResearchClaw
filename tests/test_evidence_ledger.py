"""证据账本与主张校验：数字/设置/引用都须有出处；超参数不再被消毒为 ---。"""
import json

from researchclaw.pipeline.experiment_tree import GATES, ExperimentTree
from researchclaw.pipeline.verified_registry import VerifiedRegistry
from researchclaw.writing.ledger import build_ledger, check_claims


def _run(tmp_path):
    run = tmp_path / 'rc-20261025-145941-c30e99'
    tree = ExperimentTree(run, 'minimize')
    node = tree.add({'config.py': 'LEARNING_RATE = 0.001\nBATCH = 64\nWAVELENGTH_CM = 5.546\nSEEDS = (0, 1, 2)\n'},
                    action='implement', parent=None, stage=10)['id']
    for g in GATES:
        tree.gate(node, g, True)
    tree.set_metric(node, 0.4213)
    assert tree.promote(node)[0]
    formal = run / '.formal_runs' / f'c30e99-{node}'
    formal.mkdir(parents=True)
    (formal / 'results.json').write_text(json.dumps({'留一滑坡CRPS': 0.4213, 'xinmo': {'coverage': 0.874}}))
    (run / 'stage-04').mkdir()
    (run / 'stage-04' / 'references.bib').write_text('@article{intrieri2018,\n  title = {The Maoxian landslide},\n  doi = {10.1007/s10346-017-0915-7},\n  year = {2018}\n}\n')
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'data_manifest.json').write_text(json.dumps({'entries': [
        {'id': 'xinmo/failure_time/0', 'kind': 'reference', 'failure_date': '2017-06-24',
         'source_url': 'https://doi.org/10.1007/s10346-017-0915-7', 'site': 'xinmo'}]}))
    return run, data, node


def test_ledger_sources_and_claim_check(tmp_path):
    run, data, node = _run(tmp_path)
    ledger = build_ledger(run, data_roots=(str(data),))
    kinds = {e['kind'] for e in ledger['entries']}
    assert kinds == {'setting', 'result', 'reference', 'literature'}
    lr = next(e for e in ledger['entries'] if e.get('name') == 'LEARNING_RATE')
    assert lr['file'] == 'config.py' and lr['line'] == 1 and lr['node'] == node
    good = (r'\begin{document} 学习率 0.001，批大小 64，波长 5.546 cm。留一滑坡 CRPS 为 0.421，'
            r'新磨覆盖率 87.4\%，破坏发生于 2017-06-24 \cite{intrieri2018}。共 3 个种子。% 批注: 0.999 不算')
    report = check_claims(good, ledger)
    assert report['unsupported'] == [] and report['citations_checked'] == 1
    bad = r'\begin{document} CRPS 降低了 37.5\%，见 \cite{made_up2024}。'
    kinds = sorted(u['kind'] for u in check_claims(bad, ledger)['unsupported'])
    assert kinds == ['citation', 'number']


def test_registry_whitelists_settings_from_ledger(tmp_path):
    run, data, _ = _run(tmp_path)
    build_ledger(run, data_roots=(str(data),))
    reg = VerifiedRegistry.from_run_dir(run, metric_direction='minimize')
    assert reg.is_verified(0.001) and reg.is_verified(64) and reg.is_verified(5.546)
    assert not reg.is_verified(0.777)


def test_long_arrays_are_not_evidence_and_differences_are_derived(tmp_path):
    run, data, node = _run(tmp_path)
    formal = run / '.formal_runs' / f'c30e99-{node}' / 'results.json'
    formal.write_text(json.dumps({'留一滑坡CRPS': 413.4460722, 'negative_control_crps_mm': 819.2214894,
                                  'per_fold': [1.25, 2.5], 'samples': [[i * 1.37 for i in range(50)]]}))
    ledger = build_ledger(run, data_roots=(str(data),))
    names = {e.get('name') for e in ledger['entries']}
    assert 'per_fold[1]' in names and not any(str(n).startswith('samples') for n in names)
    report = check_claims(r'\begin{document} CRPS 由 413.4461 升至 819.2215，相差 405.7754；折 2.5。', ledger)
    assert report['unsupported'] == [] and report['derived'][0]['value'] == '405.7754'
    # 只出现在样本数组里的数、以及与任何差值都不符的数仍须拦截
    bad = check_claims(r'\begin{document} 样本 67.13，增量 405.9。', ledger)
    assert sorted(u['value'] for u in bad['unsupported']) == ['405.9', '67.13']


def test_run_record_and_algorithm_steps(tmp_path):
    run, data, node = _run(tmp_path)
    (run / 'stage-12' / 'runs').mkdir(parents=True)
    (run / 'stage-12' / 'runs' / 'run-1.json').write_text(json.dumps(
        {'metrics': {'留一滑坡CRPS': 0.4213}, 'elapsed_sec': 481.99, 'receipt': {'time_budget_sec': 900}}))
    ledger = build_ledger(run, data_roots=(str(data),))
    text = '\\begin{document} 运行约 482 s 完成，预算 900 s。\n11: Compute r\n12: return\n'
    assert check_claims(text, ledger)['unsupported'] == []
    assert [u['value'] for u in check_claims('\\begin{document} 增量 11 mm', ledger)['unsupported']] == ['11']
