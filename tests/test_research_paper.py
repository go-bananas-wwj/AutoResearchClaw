"""v2 写作器：大纲校验、问题分派到小节、组装与导出、整条写作流程（模型与编译用替身）。"""
import json
from types import SimpleNamespace

from researchclaw.config import ResearchLoopConfig
from researchclaw.pipeline.experiment_tree import GATES, ExperimentTree
from researchclaw.research_loop import paper as pp
from researchclaw.research_loop.venues import venue_profile

KEY = '留一滑坡CRPS'
BIB = '@article{smith2020,\n  title = {InSAR landslides},\n  year = {2020},\n  journal = {RSE},\n}\n'


def _run(tmp_path):
    run = tmp_path / 'rc-20261025-145941-c30e99'
    tree = ExperimentTree(run, 'minimize')
    node = tree.add({'config.py': 'PRIOR_SIGMA = 0.001\n'}, action='implement', parent=None, stage=10)['id']
    for g in GATES:
        tree.gate(node, g, True)
    tree.set_metric(node, 413.4461)
    assert tree.promote(node)[0]
    formal = run / '.formal_runs' / f'c30e99-{node}'
    formal.mkdir(parents=True)
    (formal / 'results.json').write_text(json.dumps({'metrics': {KEY: 413.4461, 'sbas_baseline_crps_mm': 15.8896},
                                                     'per_site': {'a': {'crps': [1.0, 2.0, 3.0]}}}, ensure_ascii=False))
    (run / 'stage-04').mkdir()
    (run / 'stage-04' / 'references.bib').write_text(BIB)
    (run / 'research_loop').mkdir()
    (run / 'research_loop' / 'charter.json').write_text(json.dumps({'primary_metric': {'key': KEY, 'direction': 'minimize'},
                                                                    'scientific_question': 'Q?', 'source_hash': 'h'}))
    return run, tree


def _config():
    return SimpleNamespace(experiment=SimpleNamespace(
        research_loop=ResearchLoopConfig(enabled=True), metric_key=KEY, metric_direction='minimize', mode='docker',
        execution_check=SimpleNamespace(data_roots=()), code_agent=SimpleNamespace(step_max_tokens=16384)))


SECTIONS = [{'id': i, 'title': i.title(), 'purpose': 'p', 'target_words': 500,
             'claims': [{'text': 'c', 'evidence': [f'result:metrics/{KEY}', 'lit:smith2020']}]}
            for i in ('introduction', 'method', 'results', 'conclusion')]
OUTLINE = {'title': 'Phase ambiguity as a distribution', 'core_claim': 'c',
           'abstract_plan': {k: 'x' for k in ('context', 'gap', 'approach', 'result', 'significance')},
           'index_terms': ['InSAR', 'landslide', 'CRPS', 'phase'], 'sections': SECTIONS,
           'figures': [{'id': 'crps', 'kind': 'data', 'data_keys': ['metrics'], 'section': 'results', 'caption': 'CRPS.',
                        'plot': 'bars', 'width': 'single'},
                       {'id': 'sites', 'kind': 'data', 'data_keys': ['per_site/a'], 'section': 'results', 'caption': 'Sites.'},
                       {'id': 'flow', 'kind': 'schematic', 'section': 'method', 'caption': 'Flow.'}]}


def test_validate_outline(tmp_path):
    run, tree = _run(tmp_path)
    w = pp.PaperWriter(run, _config(), None, tree)
    v = lambda o: pp.validate_outline(o, ledger=w.ledger, lit_keys=set(w.bib), schema=w.schema, venue=w.venue)
    assert v(OUTLINE) == []
    bad = {**OUTLINE, 'sections': SECTIONS[::-1], 'figures': OUTLINE['figures'][:1] + [{'id': 'x', 'kind': 'data',
           'data_keys': ['nope'], 'section': 'results', 'caption': 'c'}]}
    errors = v(bad)
    assert any('introduction' in e for e in errors) and any('2' in e and '图' in e for e in errors) and any('data_keys' in e for e in errors)
    fake = {**OUTLINE, 'sections': [{**SECTIONS[0], 'claims': [{'text': 'c', 'evidence': ['result:made_up', 'lit:nobody']}]}, *SECTIONS[1:]]}
    assert sum('不存在' in e for e in v(fake)) == 2


def test_results_schema_describes_shapes():
    s = pp.results_schema({'a': {'b': [[1, 2], [3, 4]], 'c': 1.5}})
    assert s == {'a/b': 'list[2, 2] of int', 'a/c': 'float=1.5'}


def test_problems_routed_to_sections(tmp_path):
    run, tree = _run(tmp_path)
    w = pp.PaperWriter(run, _config(), None, tree)
    (w.dir / 'sections').mkdir()
    for s in SECTIONS:
        (w.dir / 'sections' / f"{s['id']}.tex").write_text(('long text ' * (400 if s['id'] == 'results' else 50))
                                                            + ('CRPS was 999.9 mm.' if s['id'] == 'results' else ''))
    (w.dir / 'sections' / 'abstract.tex').write_text('abs')
    report = {'compile': {'ok': False, 'errors': ['sections/method.tex:3: Undefined control sequence.'], 'pages': 0},
              'claims': {'unsupported': [{'kind': 'number', 'value': '999.9', 'context': 'CRPS was 999.9 mm.'}]}}
    by = w.problems(OUTLINE, report)
    assert any('编译错误' in p for p in by['method']) and any('999.9' in p for p in by['results'])
    by = w.problems(OUTLINE, {'compile': {'ok': True, 'errors': [], 'pages': 7}, 'claims': {'unsupported': []}})
    assert 'results' in by and '7 页' in by['results'][0]


class FakeLLM:
    def __init__(self):
        self.config = SimpleNamespace(reasoning_max_tokens=65536, max_tokens=4096)
        self.calls = []

    def chat(self, messages, system=None, json_mode=False, max_tokens=None, **kw):
        self.calls.append(system[:20])
        if '第一作者' in system:
            out = json.dumps(OUTLINE, ensure_ascii=False)
        elif 'TikZ' in system:
            out = '\\begin{tikzpicture}\\node{a};\\end{tikzpicture}'
        elif '独立审稿人' in system:
            out = json.dumps({'recommendation': 'minor', 'summary': 's', 'issues': []})
        elif '翻译' in system:
            out = '中文 ' + messages[0]['content']
        elif system.startswith('为这篇 IEEE GRSL letter 写英文摘要'):
            out = f'CRPS is 413.45 mm against 15.89 mm for SBAS.'
        else:
            out = 'The leave-one-landslide CRPS is 413.45 mm \\cite{smith2020}, see Fig.~\\ref{fig:crps}.'
        return SimpleNamespace(content=out)


def test_writer_end_to_end_with_fakes(tmp_path, monkeypatch):
    run, tree = _run(tmp_path)

    def fake_figs(self, figs, out):
        for f in figs:
            (out / f"{f['id']}.pdf").write_bytes(b'%PDF' + b'0' * 2000)
        return []
    compiled = []

    def fake_compile(workdir, tex='paper.tex', engine='pdf', timeout=300):
        compiled.append((tex, engine))
        (workdir / tex.replace('.tex', '.pdf')).write_bytes(b'%PDF')
        return {'ok': True, 'pages': 4, 'errors': [], 'log_tail': ''}
    monkeypatch.setattr(pp.PaperWriter, '_data_figures', fake_figs)
    monkeypatch.setattr(pp, 'compile_tex', fake_compile)
    llm = FakeLLM()
    status = pp.PaperWriter(run, _config(), llm, tree).run()
    assert status['ok'], status
    assert status['pages'] == 4 and status['unsupported'] == [] and status['review']['recommendation'] == 'minor'
    d = pp.paper_dir(run)
    tex = (d / 'paper.tex').read_text()
    assert '\\documentclass[journal]{IEEEtran}' in tex and 'figures/crps.pdf' in tex and 'figures/flow.tex' in tex
    assert '@article{smith2020' in (d / 'references.bib').read_text()
    assert ('paper_zh.tex', 'xelatex') in compiled
    # 导出：小节内联，阶段 23 与 Overleaf 只看单个 tex
    stage22 = run / 'stage-22'
    files = pp.export_paper(run, stage22)
    assert 'paper.tex' in files and '\\input{sections/' not in (stage22 / 'paper.tex').read_text()
    assert (run / 'deliverables' / 'paper_en.tex').is_file() and (run / 'deliverables' / 'figures' / 'crps.pdf').is_file()
    # 输入不变：再跑不再调用模型
    n = len(llm.calls)
    pp.PaperWriter(run, _config(), llm, tree).run()
    assert len(llm.calls) - n <= 2  # 只有审稿（已缓存）与检查，不重写大纲/小节/翻译


def test_unsupported_number_is_sent_back_to_section(tmp_path, monkeypatch):
    run, tree = _run(tmp_path)
    monkeypatch.setattr(pp.PaperWriter, '_data_figures', lambda self, figs, out: [])
    monkeypatch.setattr(pp, 'compile_tex', lambda workdir, tex='paper.tex', engine='pdf', timeout=300:
                        {'ok': True, 'pages': 4, 'errors': [], 'log_tail': ''})
    llm = FakeLLM()
    orig = llm.chat
    fixes = []

    def chat(messages, system=None, **kw):
        if '修改已有的这一节' in (system or ''):
            fixes.append(messages[0]['content'])
            return SimpleNamespace(content='The CRPS is 413.45 mm.')
        if system and 'IEEE Geoscience and Remote Sensing Letters 投稿的一节' in system:
            return SimpleNamespace(content='The CRPS dropped by 77.7 percent.')
        return orig(messages, system=system, **kw)
    llm.chat = chat
    status = pp.PaperWriter(run, _config(), llm, tree).run()
    assert fixes and '77.7' in fixes[0] and status['unsupported'] == []


def test_schematic_errors_go_to_figure_regeneration(tmp_path):
    run, tree = _run(tmp_path)
    w = pp.PaperWriter(run, _config(), None, tree)
    (w.dir / 'sections').mkdir()
    for s in SECTIONS:
        (w.dir / 'sections' / f"{s['id']}.tex").write_text('text')
    (w.dir / 'sections' / 'abstract.tex').write_text('abs')
    by = w.problems(OUTLINE, {'compile': {'ok': False, 'pages': 0, 'errors': [
        'figures/flow.tex:10: LaTeX Error: Unicode character 缠'], 'log_tail': ''}, 'claims': {'unsupported': []}})
    assert list(by) == ['figure:flow']


def test_compile_errors_without_line_use_last_opened_file(tmp_path, monkeypatch):
    from researchclaw.research_loop import latex
    log = '(./paper.tex (./sections/introduction.tex) (./sections/results.tex\n! Missing $ inserted.\n<inserted text>\nl.12 a_b\n'

    def fake_run(cmd, **kw):
        (tmp_path / 'compile.log').write_text(log)
        (tmp_path / 'compile.rc').write_text('12')
    monkeypatch.setattr(latex.subprocess, 'run', fake_run)
    out = latex.compile_tex(tmp_path, 'paper.tex')
    assert not out['ok'] and out['errors'][0].startswith('sections/results.tex:0: Missing $ inserted.')
