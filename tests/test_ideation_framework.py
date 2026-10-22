"""Orchestration contracts: mocks never populate real research sessions."""
import json
from types import SimpleNamespace

import pytest

from researchclaw.ideation.prompts import PROMPTS
from researchclaw.ideation.sources import paper_record
from tests.test_grounded_ideation import engine, first, develop, wait, LLM, Sources


def test_scientific_rejection_automatically_refines_without_another_human_gate(engine):
    original=engine.llm.chat;generations=[]
    def learn(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs);result=json.loads(response.content)
        if system==PROMPTS['generate']:
            generations.append(json.loads(messages[0]['content']))
            result['cards'][0]['method']='Original mechanism' if len(generations)==1 else 'Different mechanism with a controlled prediction'
        if system==PROMPTS['audit'] and len(generations)==1:
            result.update(verdict='overlap',reason='A prior work already contains the original mechanism')
        response.content=json.dumps(result);return response
    engine.llm.chat=learn
    data=develop(engine)
    assert data['phase']=='idea_review' and data['status']=='waiting' and not data['run_id']
    assert len(generations)==2 and generations[1]['prior_rejections']
    assert data['refinement_round']==1
    assert len([r for r in data['reviews'] if r.get('type')=='literature'])==1
    assert len([r for r in data['reviews'] if r.get('type')=='auto_refine'])==1
    assert data['cards'][0]['method'].startswith('Different mechanism')


def test_repeated_scientific_failure_stops_without_new_information(engine):
    engine.llm.overlap=True
    data=develop(engine)
    assert data['status']=='blocked' and data['blocker']['category']=='scientific'
    assert data['blocker']['code']=='no_new_information'
    assert engine.llm.calls.count('generate')==2 and not data['cards'] and not data['run_id']


def test_changed_but_rejected_methods_are_bounded_to_three_generations(engine):
    original=engine.llm.chat;number=0
    def changing(messages,*,system,**kwargs):
        nonlocal number
        response=original(messages,system=system,**kwargs);result=json.loads(response.content)
        if system==PROMPTS['generate']:
            number+=1;result['cards'][0]['method']=f'Different proposed mechanism {number}'
        if system==PROMPTS['audit']:result.update(verdict='overlap',reason='This proposed mechanism also has a precedent')
        response.content=json.dumps(result);return response
    engine.llm.chat=changing
    data=develop(engine)
    assert number==3 and data['refinement_round']==2
    assert data['blocker']['code']=='refinement_exhausted' and not data['cards']
    assert len([r for r in data['reviews'] if r.get('type')=='auto_refine'])==2


def test_truncation_is_retried_with_larger_budget_and_accounted(engine):
    original=engine.llm.chat;budgets=[]
    def truncated_once(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['generate']:
            budgets.append(kwargs['max_tokens'])
            if len(budgets)==1:response.truncated=True;response.finish_reason='length'
        return response
    engine.llm.chat=truncated_once
    data=develop(engine)
    assert data['phase']=='idea_review' and budgets[1]==2*budgets[0]
    assert len([u for u in data['usage'] if u['stage']=='generate'])==2
    assert not any(r.get('type')=='auto_refine' for r in data['reviews'])


def test_persistent_technical_failure_does_not_invent_another_scientific_direction(engine):
    engine.llm.truncate='audit'
    data=develop(engine)
    assert data['blocker']['category']=='technical' and data['blocker']['code']=='model_truncated'
    assert engine.llm.calls.count('generate')==1 and engine.llm.calls.count('audit')==2
    assert not data['cards'] and not any(r.get('type')=='auto_refine' for r in data['reviews'])


def test_scope_change_discards_old_failure_constraints(engine):
    data=first(engine);sid=data['session_id']
    data.update(prior_rejections=[{'method':'old topic constraint'}],refinement_round=2,
                blocker={'category':'scientific','code':'no_new_information'})
    engine.store.save(data)
    engine.revise_scope(sid,data['revision'],'催化剂表面反应机制与寿命')
    done=wait(engine,sid)
    assert done['phase']=='literature_review' and not done['approved_revision']
    assert not done.get('prior_rejections') and not done.get('refinement_round') and not done.get('blocker')


def test_background_reference_does_not_displace_a_direct_mechanism_neighbor(engine):
    data=develop(engine);sid=data['session_id']
    background=paper_record({'title':'General background guide','doi':'10.1234/background','url':'https://example.org/background'})
    original_search=engine.sources.search;original_chat=engine.llm.chat
    engine.sources.search=lambda query,period,source:[background]+original_search(query,period,source)
    def relationships(messages,*,system,**kwargs):
        response=original_chat(messages,system=system,**kwargs)
        if system==PROMPTS['neighbors']:
            payload=json.loads(messages[0]['content']);result=json.loads(response.content)
            ids={p['paper_id'] for p in payload['papers'] if p['title']=='General background guide'}
            for p in result['papers']:
                if p['paper_id'] in ids:p['relationship']='background'
            response.content=json.dumps(result)
        return response
    engine.llm.chat=relationships
    data.update(cards=[],rejected_cards=[],phase='audit');engine.store.save(data);engine.launch(sid)
    done=wait(engine,sid)
    assert done['phase']=='idea_review'
    assert background['paper_id'] not in engine.sources.fulltext_calls
    assert background['paper_id'] not in {p['paper_id'] for p in done['cards'][0]['closest_papers']}


@pytest.mark.parametrize('direction,topic',[
    ('我想研究催化剂表面缺陷对反应寿命的影响','catalyst surface reaction'),
    ('我想研究排队系统的尾延迟与调度机制','queueing scheduling latency'),
])
def test_the_same_framework_reaches_idea_gate_for_other_domains(engine,direction,topic):
    sources=Sources()
    sources.rows=[paper_record({'title':f'{topic} study {i}','doi':f'10.1234/domain{i}',
                    'url':f'https://example.org/domain{i}','abstract':f'Methods and controlled validation for {topic}.'}) for i in range(3)]
    def fulltext(paper,directory):
        sources.fulltext_calls.append(paper['paper_id'])
        return {'pages':[{'location':'Methods section','text':(f'Methods: {topic} is measured under controlled conditions. Validation compares the proposed and existing mechanism. '*40)}], 'source_url':paper['url']}
    sources.fulltext=fulltext;engine.sources=sources;original=engine.llm.chat
    def domain_llm(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs);r=json.loads(response.content)
        if system==PROMPTS['plan']:
            r.update(interpretation=direction,research_object=direction,domain=topic)
            r['queries']=[{'query':f'{topic} mechanism {i}','purpose':'mechanism and validation','period':'all'} for i in range(4)]
        elif system in (PROMPTS['screen'],PROMPTS['neighbors']):
            for row in r['papers']:
                row['reason']='Relevant to '+topic
                if 'overview' in row:
                    row['overview']={'problem':direction,'method':'Controlled comparison','strengths':'Evidence-based comparison','limitations':'Declared validation conditions'}
                    row['summary_zh']='Controlled comparison for '+topic
        elif system==PROMPTS['read']:
            r.update(problem=direction,method='Controlled comparison of the declared mechanism',data='Declared experimental inputs',findings='Conditional measured finding',limitations='Declared validation conditions')
        elif system==PROMPTS['synthesis']:
            r['landscape_zh']=direction
            for x in r['method_comparison']:x['method']='Controlled comparison of the declared mechanism'
        elif system==PROMPTS['generate']:
            c=r['cards'][0];c.update(title=direction,scientific_question=direction,method='Controlled alternative mechanism',
                datasets=['Declared experimental inputs'],data_sources=[{'name':'Fixture inputs','url':'https://example.org/inputs','availability':'public'}],
                baselines=[{'name':'Existing mechanism'}],collision_queries=[f'collision {topic} mechanism'])
        elif system==PROMPTS['audit']:
            for x in r['comparisons']:x['data']='Declared experimental inputs'
        response.content=json.dumps(r);return response
    engine.llm.chat=domain_llm
    data=engine.create(direction);data=wait(engine,data['session_id'])
    assert data['phase']=='literature_review' and not data['reading'] and not data['run_id']
    engine.confirm_direction(data['session_id'],data['revision']);done=wait(engine,data['session_id'])
    assert done['phase']=='idea_review' and done['status']=='waiting' and not done['run_id']
    assert done['direction']==direction and done['cards'][0]['title']==direction
    assert 'DEM' not in json.dumps(done['cards']) and 'LS' not in json.dumps(done['cards'])


def test_stop_and_restart_during_auto_refinement_preserves_gate_and_round(engine):
    from researchclaw.ideation.service import IdeationService
    original=engine.llm.chat;generation=0
    def interrupted(messages,*,system,**kwargs):
        nonlocal generation
        response=original(messages,system=system,**kwargs);result=json.loads(response.content)
        if system==PROMPTS['generate']:
            generation+=1;result['cards'][0]['method']='Original mechanism' if generation==1 else 'Different controlled mechanism'
            if generation==2:
                next(event for thread,event in engine.jobs.values() if thread.is_alive()).set()
        if system==PROMPTS['audit'] and generation==1:result.update(verdict='overlap',reason='Known mechanism')
        response.content=json.dumps(result);return response
    engine.llm.chat=interrupted
    data=develop(engine)
    assert data['status']=='stopped' and data['phase']=='generate' and data['refinement_round']==1
    assert not data['cards'] and not data['run_id']
    restarted=IdeationService(engine.store.root,engine.config,llm=engine.llm,sources=engine.sources)
    restarted.resume(data['session_id'],data['revision']);done=wait(restarted,data['session_id'])
    assert done['phase']=='idea_review' and done['refinement_round']==1
    assert len([r for r in done['reviews'] if r.get('type')=='literature'])==1
    assert not done['run_id']


def test_budget_shortfall_is_not_misreported_as_missing_scientific_evidence(engine):
    data=first(engine)
    data['budget_usd']=data['cost_usd']+0.000001
    engine.store.save(data)
    engine.confirm_direction(data['session_id'],data['revision']);done=wait(engine,data['session_id'])
    assert done['status']=='blocked' and done['blocker']['category']=='budget'
    assert done['blocker']['code']=='budget_exhausted'
    assert not done['cards'] and not done['run_id']


@pytest.mark.parametrize('recovers',[True,False])
def test_missing_review_schema_is_retried_as_technical_not_scientific(engine,recovers):
    original=engine.llm.chat;calls=0
    def missing_once(messages,*,system,**kwargs):
        nonlocal calls
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['audit']:
            calls+=1
            if calls==1 or not recovers:
                result=json.loads(response.content);result.pop('design_check');response.content=json.dumps(result)
        return response
    engine.llm.chat=missing_once
    data=develop(engine)
    assert calls==2 and engine.llm.calls.count('generate')==1
    if recovers:assert data['phase']=='idea_review'
    else:assert data['status']=='blocked' and data['blocker']['category']=='technical' and data['blocker']['code']=='invalid_review_format'
    assert not data['run_id']
