"""v2 研究循环：立项书校验/继承、引文接地、导师决策校验、断点续作与用户门控。"""
import json
from types import SimpleNamespace

import pytest

from researchclaw.config import ResearchLoopConfig
from researchclaw.pipeline.experiment_tree import GATES, ExperimentTree
from researchclaw.research_loop import advisor, charter, loop
from researchclaw.research_loop.common import append_result, cite_problems, results_rows, round_dir
from researchclaw.research_loop.round import figure_problems, metrics_table

KEY = '留一滑坡CRPS'
GOOD = {'success_criteria': [{'metric': KEY, 'direction': 'minimize', 'threshold': 15.0, 'rationale': '低于 SBAS 基线'}],
        'falsification': '完整方法 CRPS 不低于仅相位变体', 'baselines': ['SBAS'], 'ablations': ['仅相位'],
        'negative_controls': ['固定阈值'], 'statistical_design': {'objects': 2, 'folds': 2, 'seeds': 3, 'power_note': '有限'},
        'round_plan': ['诊断先验'], 'max_rounds': 4, 'venue_fit': ['CRPS 对比图']}


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0
        self.config = SimpleNamespace(reasoning_max_tokens=65536, max_tokens=4096)

    def chat(self, messages, system=None, json_mode=False, max_tokens=None, **kw):
        self.calls += 1
        return SimpleNamespace(content=json.dumps(self.replies.pop(0), ensure_ascii=False))


def _config(**loop_kw):
    return SimpleNamespace(experiment=SimpleNamespace(
        research_loop=ResearchLoopConfig(enabled=True, **loop_kw), metric_key=KEY, metric_direction='minimize',
        time_budget_sec=900, mode='docker', code_agent=SimpleNamespace(step_max_tokens=16384)))


def _run(tmp_path):
    run = tmp_path / 'rc-20261025-145941-c30e99'
    (run / 'stage-09').mkdir(parents=True)
    (run / 'stage-09' / 'experiment_spec.json').write_text(json.dumps({'primary_metric': {'key': KEY, 'direction': 'minimize'}}))
    (run / 'stage-09' / 'exp_plan.yaml').write_text('execution: {validation_level: real}\n')
    (run / 'research_brief.json').write_text(json.dumps({'topic': '相位模糊', 'scientific_question': '能否…？',
                                                         'hypotheses': ['H1'], 'source_ideation_run': ''}, ensure_ascii=False))
    tree = ExperimentTree(run, 'minimize')
    node = tree.add({'main.py': 'print(1)\n'}, action='implement', parent=None, stage=10)['id']
    for g in GATES:
        tree.gate(node, g, True)
    tree.set_metric(node, 413.4)
    assert tree.promote(node)[0]
    return run, tree


def test_charter_validation():
    assert charter.validate(GOOD, metric_key=KEY, direction='minimize', max_rounds=6) == []
    bad = {**GOOD, 'success_criteria': [{'metric': 'other', 'threshold': 1}], 'baselines': [], 'max_rounds': 9}
    errors = charter.validate(bad, metric_key=KEY, direction='minimize', max_rounds=6)
    assert any(KEY in e for e in errors) and any('baselines' in e for e in errors) and any('max_rounds' in e for e in errors)


def test_charter_accepts_prefixed_metric_key():
    alias = {**GOOD, 'success_criteria': [{**GOOD['success_criteria'][0], 'metric': f'metrics/{KEY}'}]}
    assert charter.validate(alias, metric_key=KEY, direction='minimize', max_rounds=6) == []


def test_charter_inherits_brief_and_is_reused(tmp_path):
    run, _ = _run(tmp_path)
    llm = FakeLLM([{**GOOD, 'scientific_question': '模型想改题目'}])
    c = charter.draft(run, _config(), llm, context={'x': 1})
    assert c['primary_metric'] == {'key': KEY, 'direction': 'minimize'} and c['venue']['id'] == 'grsl'
    again = charter.draft(run, _config(), FakeLLM([]), context={'x': 1})
    assert again['source_hash'] == c['source_hash']


def test_charter_keeps_brief_question_verbatim(tmp_path):
    run, _ = _run(tmp_path)
    c = charter.draft(run, _config(), FakeLLM([{**GOOD, 'scientific_question': '模型想改题目'}]), context={})
    assert c['scientific_question'] == '能否…？'


def test_cites_are_checked_verbatim(tmp_path):
    run, _ = _run(tmp_path)
    paper = run / 'ideation_evidence' / 'related work' / 'p-1'
    paper.mkdir(parents=True)
    (paper / 'fulltext.txt').write_text('The landslide failed on July 23, 2019 after heavy rain.')
    (round_dir(run, 1) / 'report.md').write_text('后验覆盖率为 1.0，说明分布过宽。')
    ok = [{'source': 'paper:p-1', 'quote': 'failed on July 23, 2019'},
          {'source': 'artifact:research_loop/rounds/round-01/report.md', 'quote': '覆盖率为 1.0'}]
    assert cite_problems(ok, run) == []
    bad = [{'source': 'paper:p-1', 'quote': 'failed in August 2020'},
           {'source': 'artifact:../../etc/passwd', 'quote': 'root:x:0:0'}, {'source': 'web', 'quote': 'abcdefg'}]
    assert len(cite_problems(bad, run)) == 3


def test_advisor_decision_rules(tmp_path):
    run, _ = _run(tmp_path)
    (round_dir(run, 1) / 'report.md').write_text('覆盖率 1.0，CRPS 413。')
    cite = [{'source': 'artifact:research_loop/rounds/round-01/report.md', 'quote': 'CRPS 413'}]
    pool = [{'card_id': 'r9-spark-3'}]
    v = lambda r, final=False: advisor.validate(r, final_round=final, pool=pool, run_dir=run, artifacts={})
    assert v({'decision': 'continue', 'next_task': '把静态先验方差放大十倍，观察覆盖率是否下降到名义水平', 'base': 'best', 'cites': cite}) == []
    assert v({'decision': 'continue', 'next_task': 'x' * 30, 'base': 'best', 'cites': cite}, final=True)
    assert v({'decision': 'pivot', 'pivot_to': 'unknown', 'cites': cite})
    assert v({'decision': 'pivot', 'pivot_to': 'r9-spark-3', 'cites': cite}) == []
    assert v({'decision': 'write', 'cites': cite})
    assert v({'decision': 'write', 'paper_core_claim': '机制成立但精度不足', 'cites': []})


def test_results_tsv_overwrites_same_round(tmp_path):
    run, _ = _run(tmp_path)
    append_result(run, {'round': 1, 'hypothesis': 'a\tb', 'decision': 'continue'})
    append_result(run, {'round': 1, 'hypothesis': 'c', 'decision': 'write'})
    rows = results_rows(run)
    assert len(rows) == 1 and rows[0]['decision'] == 'write'


def test_metrics_table_and_figure_checks():
    c = {'primary_metric': {'key': KEY, 'direction': 'minimize'}, 'success_criteria': GOOD['success_criteria']}
    t = metrics_table({KEY: 238.0, 'config/x': 1, 'metrics/coverage': 0.9}, c, 413.4)
    assert t['value'] == 238.0 and t['best_before'] == 413.4 and 'config/x' not in t['all']
    good = 'import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\nax.set_xlabel("t")\nax.set_ylabel("CRPS (mm)")\n'
    assert figure_problems({'figures.py': good}, ['figures/a.png']) == []
    assert len(figure_problems({'figures.py': 'x = 1\n'}, [])) == 2


def _reviews(*decisions):
    out = []
    for d in decisions:
        r = {'decision': d, 'assessment': f'{d} 结论', 'next_task': '下一轮把先验方差放大十倍并观察覆盖率', 'base': 'best',
             'paper_core_claim': '机制成立但精度不足', 'cites': []}
        out.append(r)
    return out


def _patch_rounds(monkeypatch, decisions):
    seq = _reviews(*decisions)
    ran = []

    def fake_run(self, previous):
        review = seq[self.n - 1]
        (self.dir / 'review.json').write_text(json.dumps(review, ensure_ascii=False))
        (self.dir / 'review.md').write_text(f'# 第 {self.n} 轮 {review["decision"]}')
        (self.dir / 'round.json').write_text(json.dumps({'done': True}))
        ran.append((self.n, previous and previous.get('decision')))
        return review
    monkeypatch.setattr(loop.Round, 'run', fake_run)
    monkeypatch.setattr(loop.charter_mod, 'draft', lambda run_dir, config, llm, context: {
        **GOOD, 'topic': 't', 'primary_metric': {'key': KEY, 'direction': 'minimize'},
        'venue': {'id': 'grsl', 'name': 'GRSL', 'max_pages': 5, 'figures': [3, 5], 'language': 'en'}, 'confirmed': False})
    return ran


def test_loop_runs_until_write_and_resumes(tmp_path, monkeypatch):
    run, tree = _run(tmp_path)
    ran = _patch_rounds(monkeypatch, ['continue', 'write'])
    stage = run / 'stage-13'
    stage.mkdir()
    out = loop.run_research_loop(run, stage, _config(), FakeLLM([]))
    assert out['status'] == 'write' and out['round'] == 2
    assert ran == [(1, None), (2, 'continue')]
    assert (stage / 'experiment_final' / 'main.py').is_file()
    assert json.loads((stage / 'refinement_log.json').read_text())['outcome']['status'] == 'write'
    ran.clear()
    again = loop.run_research_loop(run, stage, _config(), FakeLLM([]))
    assert again['status'] == 'write' and ran == []


class FakeHITL:
    def __init__(self, answers):
        self.answers, self.paused = list(answers), []

    def pause(self, stage, name, reason, **kw):
        self.paused.append(name)

    def wait_for_human(self):
        from researchclaw.hitl.intervention import HumanAction, HumanInput
        action, msg = self.answers.pop(0)
        return HumanInput(action=HumanAction(action), message=msg)


def test_user_rejecting_write_sends_note_to_next_round(tmp_path, monkeypatch):
    run, _ = _run(tmp_path)
    ran = _patch_rounds(monkeypatch, ['write', 'write'])
    hitl = FakeHITL([('approve', ''), ('reject', '先补 3 个种子再写'), ('approve', '')])
    stage = run / 'stage-13'
    stage.mkdir()
    out = loop.run_research_loop(run, stage, _config(), FakeLLM([]), hitl=hitl)
    assert hitl.paused == ['确认立项书', '开始写作', '开始写作']
    assert (round_dir(run, 2) / 'advisor_note_gate.md').read_text() == '先补 3 个种子再写'
    assert out['status'] == 'write' and [n for n, _ in ran] == [1, 2]


def test_user_rejecting_charter_stops_before_rounds(tmp_path, monkeypatch):
    run, _ = _run(tmp_path)
    ran = _patch_rounds(monkeypatch, ['write'])
    stage = run / 'stage-13'
    stage.mkdir()
    out = loop.run_research_loop(run, stage, _config(), FakeLLM([]), hitl=FakeHITL([('reject', '成功阈值太宽松')]))
    assert out['status'] == 'rejected' and ran == []
    assert '太宽松' in (round_dir(run, 1) / 'advisor_note_charter.md').read_text()


def test_round_end_to_end_with_fakes(tmp_path, monkeypatch):
    from researchclaw.research_loop import round as rnd
    run, tree = _run(tmp_path)
    c = {**GOOD, 'topic': 't', 'primary_metric': {'key': KEY, 'direction': 'minimize'},
         'venue': {'id': 'grsl', 'name': 'GRSL', 'max_pages': 5, 'figures': [3, 5], 'language': 'en'}}
    # 上一轮汇报，供本轮计划引用
    (run / 'research_loop' / 'rounds' / 'round-00').mkdir(parents=True)
    (run / 'research_loop' / 'rounds' / 'round-00' / 'report.md').write_text('覆盖率 1.0，后验过宽。')
    cite = [{'source': 'artifact:research_loop/rounds/round-00/report.md', 'quote': '覆盖率 1.0，后验过宽'}]
    plan = {'hypothesis': '静态先验过强导致后验过宽', 'variable_changed': '先验方差', 'change_summary': '放大先验方差',
            'implementation_instruction': '在 config.py 把 prior_sigma 放大十倍', 'decision_rule': 'CRPS 下降且覆盖率接近 0.9',
            'figures_instruction': 'CRPS 对比柱状图', 'expected_outcome': '覆盖率下降', 'cites': cite}
    report = {'summary': '放大先验后 CRPS 降到 238', 'hypothesis_status': 'supported', 'evidence': 'CRPS 238 < 413',
              'diagnosis': [], 'gap_to_threshold': '仍高于 15', 'next_hypotheses': ['模糊周期失配'],
              'figure_captions': {}, 'cites': []}
    review = {'decision': 'continue', 'assessment': '有改进', 'next_task': '检验模糊周期失配：把周期改成半波长并比较 CRPS',
              'base': 'this_round', 'cites': [{'source': 'artifact:research_loop/rounds/round-01/metrics.json', 'quote': '"value": 238.0'}]}
    llm = FakeLLM([plan, report, review])

    class Agent:
        def step(self, name, instruction, files):
            return {**files, 'figures.py': 'ax.set_xlabel("a")\nax.set_ylabel("b")\n'}
    monkeypatch.setattr(rnd.Round, '_agent', lambda self: Agent())

    def fake_gate(self, files, parent):
        node = self.tree.add(files, action='round-1', parent=parent, stage=13)['id']
        for g in GATES:
            self.tree.gate(node, g, True)
        return node

    def fake_formal(self, node):
        self.tree.set_metric(node, 238.0)
        self.tree.promote(node)
        (self.dir / 'run_log.txt').write_text('ok')
        return {KEY: 238.0}, '', run / '.formal_runs' / 'x', SimpleNamespace(elapsed_sec=1.0)
    monkeypatch.setattr(rnd.Round, '_gate', fake_gate)
    monkeypatch.setattr(rnd.Round, '_formal', fake_formal)
    monkeypatch.setattr(rnd.Round, 'figures', lambda self, outcome: self._save(figures=[], figure_problems=[]) or [])
    r = rnd.Round(1, run_dir=run, config=_config(), llm=llm, charter=c, spec={}, exec_plan={}, plan_text='',
                  tree=tree, final_round=False)
    out = r.run(None)
    assert out['decision'] == 'continue' and llm.calls == 3
    d = round_dir(run, 1)
    assert {'plan.md', 'report.md', 'review.md', 'metrics.json', 'round.json'} <= {p.name for p in d.iterdir()}
    assert '238.0' in (d / 'report.md').read_text() and '413.4' in (d / 'report.md').read_text()
    row = results_rows(run)[0]
    assert row['improved'] == 'True' and row['decision'] == 'continue' and row['primary_metric'] == '238.0'
    # 断点续作：已完成的环节不再调用模型
    again = rnd.Round(1, run_dir=run, config=_config(), llm=FakeLLM([]), charter=c, spec={}, exec_plan={},
                      plan_text='', tree=tree, final_round=False).run(None)
    assert again['decision'] == 'continue'


def test_journal_figures_and_user_notes(tmp_path):
    from researchclaw.research_loop import journal as jr
    from researchclaw.research_loop.common import save_state
    run, _ = _run(tmp_path)
    d = round_dir(run, 1)
    (d / 'figures').mkdir()
    (d / 'figures' / 'crps.png').write_bytes(b'\x89PNG' + b'0' * 2000)
    (d / 'round.json').write_text(json.dumps({'figures': ['figures/crps.png'], 'done': True}))
    (d / 'plan.md').write_text('# 计划')
    assert jr.figure_path(run, 'research_loop/rounds/round-01/figures/crps.png').name == 'crps.png'
    for bad in ('research_loop/rounds/round-01/../../research_brief.json', 'research_brief.json',
                'research_loop/rounds/round-01/figures/x.svg'):
        with pytest.raises((ValueError, FileNotFoundError)):
            jr.figure_path(run, bad)
    save_state(run, round=1, status='running')
    rel = jr.add_user_note(run, '先补种子')
    assert rel.startswith('research_loop/rounds/round-02/advisor_note_user_')  # 第 1 轮已完成 → 写给下一轮
    view = jr.journal(run)
    assert view['enabled'] and view['rounds'][0]['figures'] == ['research_loop/rounds/round-01/figures/crps.png']
    with pytest.raises(ValueError):
        jr.add_user_note(run, '  ')


def test_new_note_after_write_reopens_loop(tmp_path, monkeypatch):
    from researchclaw.research_loop.common import save_state
    run, _ = _run(tmp_path)
    ran = _patch_rounds(monkeypatch, ['write', 'write'])
    stage = run / 'stage-13'
    stage.mkdir()
    assert loop.run_research_loop(run, stage, _config(), FakeLLM([]))['round'] == 1
    (round_dir(run, 2) / 'advisor_note_review.md').write_text('审稿人：补负对照')
    ran.clear()
    out = loop.run_research_loop(run, stage, _config(), FakeLLM([]))
    assert out['status'] == 'write' and out['round'] == 2 and ran == [(2, 'continue')]
