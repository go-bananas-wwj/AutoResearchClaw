"""实验树棘轮：只有全部关卡通过且主指标按规格方向改进的节点才能成为 best。"""
import pytest

from researchclaw.pipeline.experiment_tree import GATES, ExperimentTree, primary_value


def _pass_all(tree, node, metric):
    for gate in GATES:
        tree.gate(node, gate, True)
    tree.set_metric(node, metric)


def test_ratchet_minimize(tmp_path):
    tree = ExperimentTree(tmp_path, 'minimize')
    n0 = tree.add({'main.py': 'print(1)\n'}, action='implement', parent=None, stage=10)['id']
    _pass_all(tree, n0, 0.5)
    assert tree.promote(n0)[0] and tree.best()['node'] == n0
    worse = tree.add({'main.py': 'print(2)\n'}, action='improve', parent=n0, stage=13)['id']
    _pass_all(tree, worse, 0.7)
    ok, why = tree.promote(worse)
    assert not ok and '未优于' in why and tree.best()['node'] == n0
    failed = tree.add({'main.py': 'print(3)\n'}, action='improve', parent=n0, stage=13)['id']
    _pass_all(tree, failed, 0.1)
    tree.gate(failed, 'review', False, '方法偷换')
    ok, why = tree.promote(failed)
    assert not ok and 'review' in why and tree.best()['node'] == n0
    better = tree.add({'main.py': 'print(4)\n'}, action='improve', parent=n0, stage=13)['id']
    _pass_all(tree, better, 0.2)
    assert tree.promote(better)[0] and tree.best()['metric'] == 0.2
    assert tree.files_at(tree.best()['commit'])['main.py'] == 'print(4)\n'
    assert {n['id']: n['status'] for n in tree.nodes()}[failed] == 'failed'


def test_direction_required_and_metric_lookup(tmp_path):
    with pytest.raises(ValueError):
        ExperimentTree(tmp_path, '')
    assert primary_value({'xinmo/CRPS': 0.3, 'CRPS_std': 9}, 'CRPS') == 0.3
    assert primary_value({'CRPS_mean_alias': 0.3}, 'CRPS') is None  # no fuzzy aliasing
    assert primary_value({'CRPS': float('nan')}, 'CRPS') is None
