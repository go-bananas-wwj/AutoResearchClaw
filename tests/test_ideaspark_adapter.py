"""Pinned official ledger, isolated calls, truthful traces and batch boundaries.

No real model, literature network request or scientific experiment runs here.
"""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.ideation.ideaspark import IdeaSparkBatch, ROOT, PIN, verify_installation, verify_card, read, write, missing_method_references
from researchclaw.ideation.store import SessionError, fingerprint
from tests.test_grounded_ideation import engine, develop


def host(engine):
    data=engine.store.create('测试独立的官方构思执行器',0)
    return IdeaSparkBatch(engine,data,threading.Event())


def test_vendored_official_files_are_pinned_and_unmodified():
    manifest=verify_installation()
    assert manifest['commit']==PIN and len(manifest['files'])>100


def test_only_missing_transport_id_can_be_bound_without_changing_artifacts():
    request={'request_id':'current-uuid','outputs':['candidate.json']}
    artifacts={'candidate.json':{'mechanism':'unchanged original model content'}}
    result=IdeaSparkBatch.bind_envelope({'contract_version':2,'artifacts':artifacts},request)
    assert result['request_id']=='current-uuid' and result['artifacts']==artifacts
    with pytest.raises(SessionError):
        IdeaSparkBatch.bind_envelope({'request_id':'old-uuid','artifacts':artifacts},request)


def test_native_success_cannot_substitute_for_missing_method_body(tmp_path):
    data={'reading':{'p-approved':{}},'papers':[{'paper_id':'p-approved','doi':'10.1234/known'}]}
    native={'almost_prior_paper_id':'10.1234/unknown','differentiation_from_lit':[{'paper_id':'p-approved'}]}
    assert missing_method_references(data,native,tmp_path)==['10.1234/unknown']
    native['almost_prior_paper_id']='10.1234/known'
    assert missing_method_references(data,native,tmp_path)==[]


def test_real_navigator_starts_with_empty_directory_contract(engine):
    h=host(engine);run=h.directory/'official-run'
    action=h.navigator(run)
    assert action['state']=='NEW_RUN' and action['kind']=='bash' and not run.exists()
    code,_,_=h.command(['python3',str(ROOT/'scripts/run.py'),'quality_init','--dir',str(run),'--query',h.data['direction']])
    assert code==0 and read(run/'run_contract.json')['contract_version']==2
    assert h.navigator(run)['kind']=='llm_subagent'


def test_bottleneck_rediagnosis_handshake_uses_official_prompt_and_imported_scope(engine,monkeypatch):
    h=host(engine);run=h.directory/'retry';p0=run/'phase0';p0.mkdir(parents=True)
    (p0/'user_query.txt').write_text(h.data['direction']);(p0/'lit_table.md').write_text('approved corpus')
    write(p0/'lit_results.json',[{'paper_id':'p1'}]);write(p0/'fulltext_cache.json',{'p1':{'method':'verified source excerpt'}})
    job={'id':'job','number':1,'directory':'retry','focus':'a different bottleneck','status':'running'}
    h.data['ideaspark_batch']={'target':1,'jobs':[job],'phase':'review'}
    action={'kind':'llm_subagent','step':'Phase 1 — bottleneck identification','state':'phase1',
            'prompt':str(ROOT/'references/system-prompts/bottleneck_identify.txt'),'inputs':['annotated inputs from official navigator']}
    monkeypatch.setattr(h,'navigator',lambda run:action)
    monkeypatch.setattr(h,'model',lambda *a,**kw:{'state':'proceed','closest_adjacent':[{'paper_id':'p1'}]})
    h.step(job)
    assert read(run/'phase1/phase1_output.json')['state']=='proceed'
    assert h.data['phase']=='audit'


def test_native_quality_record_rejects_changed_inputs_and_wrong_request_id(engine):
    h=host(engine);run=h.directory/'ledger'
    assert h.command(['python3',str(ROOT/'scripts/run.py'),'quality_init','--dir',str(run)])[0]==0
    source=run/'input.json';write(source,{'source':'version one'})
    spec={'stage':'adapter_contract_test','inputs':[str(source)],'outputs':['test-output.json'],
          'prompts':[str(ROOT/'references/system-prompts/ideate_generate.txt')],'notes':''}
    prepare=['python3',str(ROOT/'scripts/run.py'),'quality_prepare','--dir',str(run),'--spec-json',json.dumps(spec)]
    record=['python3',str(ROOT/'scripts/run.py'),'quality_record','--dir',str(run),'--stage',spec['stage']]
    assert h.command(prepare)[0]==0
    request=read(run/'.quality'/spec['stage']/'request.json')
    write(run/'.quality'/spec['stage']/'result.json',{'request_id':request['request_id'],'artifacts':{'test-output.json':{'fixture':True}}})
    write(source,{'source':'changed after model call'})
    assert h.command(record)[0]!=0 and not (run/'test-output.json').exists()
    assert h.command(prepare)[0]==0
    request=read(run/'.quality'/spec['stage']/'request.json')
    write(run/'.quality'/spec['stage']/'result.json',{'request_id':'wrong-id','artifacts':{'test-output.json':{'fixture':True}}})
    assert h.command(record)[0]!=0 and not (run/'test-output.json').exists()


def test_model_calls_are_fresh_checkpointed_and_accounted(engine):
    calls=[]
    class Client:
        def chat(self,messages,**kwargs):
            calls.append((messages,kwargs));return SimpleNamespace(content='{"fixture":true}',finish_reason='stop',model='fake',prompt_tokens=10,completion_tokens=20)
    engine.llm=Client();h=host(engine);path=h.directory/'model-checkpoint.json'
    h.model('fixture','system',{'version':1},path)
    h.model('fixture','system',{'version':1},path)
    h.model('fixture','system',{'version':2},path)
    assert len(calls)==2 and all(len(m)==1 for m,_ in calls)
    assert len(h.data['usage'])==2 and h.data['cost_usd']>0
    assert all(u['skill_commit']==PIN for u in h.data['usage'])


def test_inputs_cannot_read_outside_skill_or_current_run(engine,tmp_path):
    h=host(engine);run=h.directory/'run';run.mkdir()
    secret=tmp_path/'private-file';secret.write_text('not model input')
    link=run/'linked';link.symlink_to(secret)
    with pytest.raises(SessionError):h.inputs([str(link)],run)


def test_claimed_execution_must_match_real_host_stdout(engine,monkeypatch):
    h=host(engine);run=h.directory/'trace';run.mkdir()
    artifact={'artifacts':{'trace.json':{'dry_run':{'execution':{'script':'print(2)','mode':'executed','output':'FAKE'}}}}}
    def sandbox(arguments,**kwargs):
        assert '--network=none' in arguments and '--read-only' in arguments
        assert kwargs['input_text']=='print(2)'
        return 0,'2\n',''
    monkeypatch.setattr(h,'command',sandbox)
    monkeypatch.setattr(h,'model',lambda *args,**kwargs:artifact)
    with pytest.raises(SessionError,match='实际输出不符'):
        h.bind_execution(artifact,{'stage':'coherence'},{},'prompt',run)
    assert len(list((run/'host_executions').glob('*.json')))==1


def test_skill_status_cannot_be_faked_to_start_pipeline(engine):
    data=develop(engine);card=data['cards'][0]
    card['skill_job_id']='fake-job'
    engine.store.save(data)
    with pytest.raises(SessionError,match='IdeaSpark'):
        engine.confirmation(data['session_id'],data['revision'],card['card_id'])
    assert not data['run_id']


def test_visible_draft_card_is_not_a_confirmable_research_project(engine):
    data=develop(engine);card=data['cards'].pop()
    card.update(skill_job_id='draft-job',skill_status='running')
    data['idea_pool']=[card];engine.store.save(data)
    with pytest.raises(SessionError,match='已审查'):
        engine.confirmation(data['session_id'],data['revision'],card['card_id'])
    assert not data['run_id']


def test_export_keeps_original_native_spec_and_receipts(engine):
    data=develop(engine);card=data['cards'][0]
    directory=engine.store.directory(data['session_id']);run=directory/'ideaspark'/'selected-job'
    native={'title':'Native fixture proposal','core_mechanism':'Original mechanism, not a displayed paraphrase','falsification_prediction':'Original falsification rule','compute_budget':'unchanged resource envelope'}
    write(run/'phase2_generate/phase2_generate_output.json',native)
    write(run/'.quality/generation/receipt.json',{'fixture':'version receipt'})
    card.update(skill_job_id='selected-job',native_candidate_hash=fingerprint(native))
    data['ideaspark_batch']={'scope_revision':data['revision'],'jobs':[{'id':'selected-job','directory':str(run.relative_to(directory))}]}
    brief,path=engine.export(data,card)
    assert read(path/'ideaspark/phase2_generate/phase2_generate_output.json')==native
    assert read(path/'ideaspark/.quality/generation/receipt.json')['fixture']=='version receipt'
    assert native['core_mechanism'] in brief['experiment_scope']
    assert not list(directory.parent.parent.glob('rc-*'))


def test_batch_schedules_all_drafts_before_deep_review(engine,monkeypatch):
    h=host(engine);h.data.update(phase='generate',approved_revision=1,synthesis={})
    engine.config.llm.ideation_card_target=12
    def seed(root):
        (root/'phase0').mkdir(parents=True);write(root/'phase1/phase1_output.json',{'state':'proceed'});return {'state':'proceed'}
    monkeypatch.setattr(h,'seed',seed)
    monkeypatch.setattr(h,'model',lambda *a,**kw:{'focuses':['distinct focus '+str(i) for i in range(12)]})
    order=[]
    def step(job):
        run=h.directory/job['directory'];native=h.candidate(job)
        if native:job['status']='validated';order.append('review')
        else:write(run/'phase2_generate/phase2_generate_output.json',{'title':job['focus']});order.append('draft')
    monkeypatch.setattr(h,'step',step)
    def present(job):
        job['presentation_hash']='fixture'
        if job['status']=='validated':h.data['cards'].append({'title':job['focus']})
    monkeypatch.setattr(h,'present',present)
    h.run()
    assert order[:12]==['draft']*12 and order[12:]==['review']*12
    assert len(h.data['cards'])==12 and h.data['phase']=='idea_review'
    assert len({j['directory'] for j in h.data['ideaspark_batch']['jobs']})==12


def plain_card():
    return {'one_liner':'能不能更早发现危险，并检验预测是否可信？',
        'plain_language':{'problem':'已有观测仍有盲区。','difference':'改进是假设，需对照验证。',
            'data':'使用原候选声明的数据。','risks':'局限不变。',
            'steps':[{'title':'观察','text':'读入原候选数据。'},{'title':'比较','text':'计算原候选输出。'}],
            'test':[{'title':'对照','text':'保持原对照。'},{'title':'否定','text':'保留原失败条件。'}],
            'terms':[]},'formulas':[]}


def test_plain_card_requires_model_authored_question_and_steps():
    from researchclaw.ideation.ideaspark import validate_card_explanation
    card=plain_card();validate_card_explanation(card)
    extra=plain_card();extra['plain_language']['steps']*=3;validate_card_explanation(extra)
    for patch in ({'one_liner':'一段技术摘要'}, {'plain_language':{}}, {'formulas':[{'latex':'incomplete'}]}):
        with pytest.raises(SessionError):validate_card_explanation({**card,**patch})
    card['plain_language']['steps']='a big blob'
    with pytest.raises(SessionError):validate_card_explanation(card)


def test_failed_display_does_not_fail_native_job_or_expose_stale_approval(engine,monkeypatch):
    h=host(engine);job={'id':'job','status':'running'}
    card={'skill_job_id':'job','skill_status':'validated','audit':{'verdict':'pass'},'native_candidate_hash':'old'}
    h.data.update(idea_pool=[card],cards=[card])
    monkeypatch.setattr(h,'candidate',lambda job:{'mechanism':'new'})
    monkeypatch.setattr(h,'present',lambda job:(_ for _ in ()).throw(SessionError('display JSON invalid')))
    h.present_safely(job)
    assert job['status']=='running' and job['presentation_error']=='display JSON invalid'
    assert not h.data['cards'] and 'audit' not in card
    assert card['skill_status']=='revising'


def test_new_presentation_prompt_refreshes_cache_without_touching_native_candidate(engine,monkeypatch):
    from researchclaw.ideation.prompts import PROMPTS
    data=develop(engine);card={**data['cards'][0],**plain_card()};data['idea_pool']=[]
    h=IdeaSparkBatch(engine,data,threading.Event())
    job={'id':'fixture-job','number':1,'directory':'fixture','status':'running','focus':'fixture'}
    data['ideaspark_batch']={'jobs':[job]}
    native={'title':'Original native candidate','core_mechanism':'unchanged'}
    monkeypatch.setattr(h,'candidate',lambda job:native)
    calls=[]
    def render(*args,**kwargs):calls.append(args[2]);return {'cards':[dict(card)]}
    monkeypatch.setattr(engine,'_json',render)
    h.present(job);h.present(job)
    assert len(calls)==1
    monkeypatch.setitem(PROMPTS,'spark_card',PROMPTS['spark_card']+'\nUpdated presentation instruction')
    h.present(job)
    assert len(calls)==2 and native=={'title':'Original native candidate','core_mechanism':'unchanged'}
    assert data['idea_pool'][0]['plain_language']==plain_card()['plain_language']


def test_formula_guard_requests_model_correction_instead_of_guessing_math():
    from researchclaw.ideation.ideaspark import validate_card_explanation
    card=plain_card()
    for latex in ('Delta_d_max', 'S = {a: 存在 a}'):
        card['formulas']=[{'title':'关系','latex':latex,'explanation':'原关系的含义','symbols':[]}]
        with pytest.raises(SessionError,match='LaTeX'):validate_card_explanation(card)
    card['formulas'][0]['latex']=r'\Delta_{d,\max} = \text{允许的变化}'
    validate_card_explanation(card)


def test_invalid_formula_gets_model_feedback_and_native_candidate_is_preserved(engine,monkeypatch):
    data=develop(engine);original={**data['cards'][0],**plain_card()}
    h=IdeaSparkBatch(engine,data,threading.Event());native={'title':'native','core_mechanism':'unchanged'}
    job={'id':'job','number':1,'directory':'fixture','focus':'fixture','status':'running'}
    data['ideaspark_batch']={'jobs':[job]};monkeypatch.setattr(h,'candidate',lambda job:native)
    requests=[]
    def model(data,event,stage,payload):
        requests.append(payload)
        card=dict(original)
        card['formulas']=[{'title':'关系','latex':'Delta_d_max' if len(requests)==1 else r'\Delta_{d,\max}',
            'explanation':'原始关系','symbols':[]}]
        return {'cards':[card]}
    monkeypatch.setattr(engine,'_json',model)
    h.present(job)
    assert len(requests)==2 and 'LaTeX' in requests[1]['output_feedback']
    assert data['idea_pool'][0]['formulas'][0]['latex']==r'\Delta_{d,\max}'
    assert native=={'title':'native','core_mechanism':'unchanged'}
