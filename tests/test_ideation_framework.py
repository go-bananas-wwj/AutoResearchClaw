"""Orchestration contracts: mocks never populate real research sessions."""
import json
import threading
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
        elif system in (PROMPTS['screen'],PROMPTS['summarize'],PROMPTS['neighbors']):
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


def test_off_topic_paper_missing_overview_does_not_block_related_downloads(engine):
    engine.sources=Sources(4);original=engine.llm.chat
    def incomplete_excluded(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['screen']:
            result=json.loads(response.content);row=result['papers'][-1]
            row.update(relevance='off_topic',reason='Different research object')
            row['overview'].pop('method');response.content=json.dumps(result)
        return response
    engine.llm.chat=incomplete_excluded
    data=first(engine)
    assert data['phase']=='literature_review' and data['status']=='waiting'
    assert len(engine.sources.fulltext_calls)==3 and len(engine._related(data))==3
    assert not data['approved_revision'] and not data['reading'] and not data['run_id']
    assert data['papers'][-1]['relevance']=='off_topic' and not data['papers'][-1]['in_shortlist']


def test_related_overview_missing_field_is_corrected_before_batch_is_accepted(engine):
    original=engine.llm.chat;calls=[]
    def incomplete_once(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['summarize']:
            payload=json.loads(messages[0]['content']);calls.append(payload)
            if len(calls)==1:
                result=json.loads(response.content);result['papers'][0]['overview'].pop('method');response.content=json.dumps(result)
        return response
    engine.llm.chat=incomplete_once
    data=first(engine)
    assert data['phase']=='literature_review' and len(calls)==2
    assert calls[1]['validation_feedback']['errors'] and calls[1]['validation_feedback']['errors'][0]['paper_id']
    assert len(engine.sources.fulltext_calls)==3 and not data['reading']


def test_candidate_and_shortlist_caps_bound_original_downloads(engine):
    engine.sources=Sources(100)
    data=first(engine)
    assert len(data['papers'])<=60
    assert data['selection_stats']['selected']==20
    assert len(engine._related(data))==20 and len(set(engine.sources.fulltext_calls))==20
    assert not data['approved_revision'] and not data['reading']
    assert all(not p['in_shortlist'] for p in data['papers'] if p['paper_id'] not in engine.sources.fulltext_calls)


def test_custom_paper_limit_is_persistent_and_does_not_drop_pinned_material(engine):
    engine.sources=Sources(45)
    data=engine.create('General mechanism research question',paper_limit=10);data=wait(engine,data['session_id'])
    assert data['paper_limit']==10 and len(engine._related(data))==10 and len(data['papers'])<=30
    pinned=paper_record({'title':'User supplied essential method','doi':'10.1234/pinned','url':'https://example.org/pinned','abstract':'A user supplied relevant method with independent verification.'})
    changed=engine.add_paper(data['session_id'],data['revision'],pinned)
    assert len(engine._related(changed))==10
    assert pinned['paper_id'] not in {p['paper_id'] for p in engine._related(changed)}
    assert pinned['paper_id'] in {p['paper_id'] for p in engine._ranked_candidates(changed)}
    assert not changed['approved_revision']
    with pytest.raises(Exception):engine.create('Invalid oversized survey',paper_limit=31)


def test_shortlist_keeps_core_relevance_and_recent_classic_coverage(engine):
    data=engine.store.create('General mechanism research question',50,paper_limit=4)
    data['papers']=[paper_record({'title':f'Method {i}','year':2000+i,'citation_count':1000 if i==0 else 0,'abstract':'A directly related mechanism and verification.'}) for i in range(8)]
    for p in data['papers']:p['relevance']='core'
    data['papers'].append(paper_record({'title':'Background review','year':2026,'citation_count':9999}))
    data['papers'][-1]['relevance']='adjacent'
    ranked=engine._ranked_candidates(data)[:4]
    ids={p['paper_id'] for p in ranked}
    assert data['papers'][0]['paper_id'] in ids and data['papers'][7]['paper_id'] in ids
    assert data['papers'][-1]['paper_id'] not in ids


@pytest.mark.asyncio
async def test_creation_api_validates_and_returns_survey_limits(engine,monkeypatch):
    import httpx
    from fastapi import FastAPI
    from researchclaw.server.app import _app_state
    from researchclaw.server.routes import ideation
    app=FastAPI();app.include_router(ideation.router);monkeypatch.setitem(_app_state,'ideation_service',engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post('/api/ideation/sessions',json={'direction':'General mechanism direction','paper_limit':31})).status_code==422
        response=await client.post('/api/ideation/sessions',json={'direction':'General mechanism direction','paper_limit':10})
        assert response.status_code==200 and response.json()['limits']['paper_limit']==10
        done=await __import__('asyncio').to_thread(wait,engine,response.json()['session_id'])
        assert done['phase']=='literature_review' and not done['approved_revision']


def test_deepseek_summary_receives_original_and_preserves_its_exact_text(engine):
    original=engine.llm.chat;payloads=[]
    paragraph='模型撰写的连贯总结，程序必须原样保留，不从字段重新拼接。'
    def writer(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['summarize']:
            payloads.append(json.loads(messages[0]['content']))
            result=json.loads(response.content)
            for row in result['papers']:row['summary_zh']=paragraph
            response.content=json.dumps(result)
        return response
    engine.llm.chat=writer;data=first(engine)
    assert payloads and all(p['material']['source_type']=='fulltext' and 'Methods:' in p['material']['passages'][0]['text'] for p in payloads[0]['papers'])
    assert all(p['summary_zh']==paragraph and p['summary_basis']=='fulltext' for p in data['papers'])
    assert all(p['summary_source_hash'] and p['summary_prompt_hash'] for p in data['papers'])
    assert not data['reading'] and not data['approved_revision'] and not data['run_id']
    calls=list(engine.llm.calls)
    engine._summarize_selected(data,threading.Event())
    assert engine.llm.calls==calls  # Completed unchanged summaries are checkpointed.


def test_abstract_only_sources_never_get_summary_or_first_review(engine):
    engine.sources.abstract_only=True;original=engine.llm.chat;payloads=[]
    def capture(messages,*,system,**kwargs):
        if system==PROMPTS['summarize']:payloads.append(json.loads(messages[0]['content']))
        return original(messages,system=system,**kwargs)
    engine.llm.chat=capture;data=first(engine)
    assert not payloads and not engine._related(data)
    assert data['status']=='blocked' and not data['reading']
    assert all(not p.get('summary_zh') and not p.get('in_shortlist') for p in data['papers'])


def test_no_source_has_no_title_stitched_or_generated_summary(engine):
    engine.sources.abstract_only=True
    for p in engine.sources.rows:p['abstract']=''
    data=first(engine)
    assert data['status']=='blocked' and 'summarize' not in engine.llm.calls
    assert all(not p['overview'] and not p['summary_zh'] and p['summary_state']=='awaiting_source' for p in data['papers'])


def test_screen_requires_only_relevance_and_reason(engine):
    original=engine.llm.chat
    def triage(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['screen']:
            result=json.loads(response.content)
            result['papers']=[{k:r[k] for k in ('paper_id','relevance','reason')} for r in result['papers']]
            response.content=json.dumps(result)
        return response
    engine.llm.chat=triage;data=first(engine)
    assert data['status']=='waiting' and all(p['summary_state']=='ready' for p in data['papers'])


def test_unavailable_originals_are_replaced_without_entering_review(engine):
    engine.sources=Sources(12)
    engine.sources.fail_ids={p['paper_id'] for p in engine.sources.rows[:5]}
    data=engine.create('Generic method with independent verification',paper_limit=4)
    data=wait(engine,data['session_id']);related=engine._related(data)
    assert data['status']=='waiting' and len(related)==4
    assert not engine.sources.fail_ids.intersection(p['paper_id'] for p in related)
    assert all(p['summary_basis']=='fulltext' and p['selection']['evidence'] for p in related)
    assert all(not p['in_shortlist'] and not p.get('summary_zh') for p in data['papers'] if p['paper_id'] in engine.sources.fail_ids)
    assert len(set(engine.sources.fulltext_calls))<=12
    assert not data['approved_revision'] and not data['reading'] and not data['run_id']


def test_high_citations_cannot_override_fulltext_method_failure(engine):
    engine.sources=Sources(6);engine.sources.rows[0]['citation_count']=999999
    original=engine.llm.chat
    def quality(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['summarize']:
            payload=json.loads(messages[0]['content']);result=json.loads(response.content)
            for row,paper in zip(result['papers'],payload['papers']):
                if paper['title']=='LS method 0':
                    row['selection'].update(decision='exclude',rigor='weak',reason='所给正文缺少支持核心结论的有效对照')
            response.content=json.dumps(result)
        return response
    engine.llm.chat=quality;data=engine.create('Generic independent method validation',paper_limit=3)
    data=wait(engine,data['session_id']);rejected=data['papers'][0]
    assert data['status']=='waiting' and len(engine._related(data))==3
    assert rejected['fulltext_status']=='downloaded' and not rejected['in_shortlist']
    assert rejected['selection']['rigor']=='weak'


def test_fabricated_selection_quote_is_rejected_before_first_gate(engine):
    original=engine.llm.chat
    def invented(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['summarize']:
            result=json.loads(response.content)
            for row in result['papers']:row['selection']['evidence'][0]['quote']='This quotation has no match in the supplied paper.'
            response.content=json.dumps(result)
        return response
    engine.llm.chat=invented;data=first(engine)
    assert data['status']=='blocked' and not data.get('curation_version') and not data['approved_revision']
    assert not data['reading'] and not data['cards'] and not data['run_id']


def test_added_paper_requires_curation_before_direction_approval(engine):
    from researchclaw.ideation.store import SessionError
    data=first(engine)
    row=paper_record({'title':'New relevant paper','doi':'10.1234/supplement','abstract':'An independent experimental design with controls.'})
    changed=engine.add_paper(data['session_id'],data['revision'],row)
    with pytest.raises(SessionError,match='整理全文文献'):
        engine.confirm_direction(changed['session_id'],changed['revision'])
    with pytest.raises(SessionError,match='全文'):
        engine.review_literature(changed['session_id'],changed['revision'],[row['paper_id']],[])
    assert not changed['approved_revision'] and row['paper_id'] not in {p['paper_id'] for p in engine._related(changed)}


def test_selection_page_can_be_corrected_only_from_exact_unique_same_paper_quote(engine):
    original=engine.llm.chat
    def wrong_label(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['summarize']:
            result=json.loads(response.content)
            for row in result['papers']:row['selection']['evidence'][0]['location']='invented section label'
            response.content=json.dumps(result)
        return response
    engine.llm.chat=wrong_label;data=first(engine)
    assert data['status']=='waiting'
    for paper in engine._related(data):
        e=paper['selection']['evidence'][0]
        assert e['location']=='第 2 页' and e['model_location']=='invented section label'
        assert e['citation_resolution']=='unique_exact_quote'


def test_summary_excerpts_keep_entire_original_and_model_selects_source_ids(engine):
    original=engine.llm.chat;materials=[]
    def references(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['summarize']:
            payload=json.loads(messages[0]['content']);result=json.loads(response.content)
            for row,paper in zip(result['papers'],payload['papers']):
                material=paper['material'];materials.append(material)
                row['selection'].pop('evidence')
                row['selection']['evidence_refs']=[material['passages'][0]['passage_id']]
            response.content=json.dumps(result)
        return response
    engine.llm.chat=references;data=first(engine)
    assert data['status']=='waiting'
    for material,paper in zip(materials,engine.sources.rows):
        assert 'pages' not in material
        assert ''.join(p['text'] for p in material['passages'])==engine.sources.fulltext(paper,None)['pages'][0]['text']
    for p in engine._related(data):
        evidence=p['selection']['evidence'][0]
        assert evidence['passage_id'] in p['selection']['evidence_refs']
        assert len(evidence['quote'])==160 and evidence['location']=='第 2 页'


def test_summary_cannot_use_unknown_source_ids(engine):
    original=engine.llm.chat
    def unknown(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['summarize']:
            result=json.loads(response.content)
            for row in result['papers']:row['selection']['evidence_refs']=['unknown-source-id']
            response.content=json.dumps(result)
        return response
    engine.llm.chat=unknown;data=first(engine)
    assert data['status']=='blocked' and not data['approved_revision'] and not data['cards']


def test_long_technical_overview_is_sent_back_to_ai_instead_of_program_shortening(engine):
    original=engine.llm.chat;attempts=[]
    def verbose(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['summarize']:
            attempts.append(json.loads(messages[0]['content']))
            if len(attempts)==1:
                result=json.loads(response.content)
                result['papers'][0]['overview']['method']='专业术语和长流程'*50
                response.content=json.dumps(result)
        return response
    engine.llm.chat=verbose;data=first(engine)
    assert data['status']=='waiting' and len(attempts)==2
    assert '160' in attempts[1]['validation_feedback']['errors'][0]['error']
    assert all(p['overview']['method']=='DEM汇流' for p in engine._related(data))


def test_missing_required_original_cannot_be_skipped_via_direction_confirmation(engine):
    from researchclaw.ideation.store import SessionError
    data=first(engine);sid=data['session_id']
    row=paper_record({'title':'Required unavailable method','doi':'10.1234/required-unavailable','abstract':'A related method needing unavailable original material.'})
    engine.sources.fail_ids.add(row['paper_id'])
    changed=engine.add_paper(sid,data['revision'],row)
    engine.acquire_fulltexts(sid,changed['revision']);done=wait(engine,sid)
    assert done['status']=='blocked' and done.get('curation_version') is None and not done['approved_revision']
    with pytest.raises(SessionError):engine.confirm_direction(sid,done['revision'])
    # The compatibility endpoint also cannot drop the requested paper.
    done['curation_version']=1;engine.store.save(done)
    with pytest.raises(SessionError,match='指定论文'):
        engine.review_literature(sid,done['revision'],[p['paper_id'] for p in engine._related(done)],[])


def test_summary_missing_selection_comma_preserves_model_text_and_avoids_repeat_call(engine):
    original=engine.llm.chat
    def missing_comma(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['summarize']:
            result=json.loads(response.content)
            for row in result['papers']:
                selection=row.pop('selection');paragraph=row.pop('summary_zh')
                row['summary_zh']=paragraph;row['selection']=selection
            response.content=json.dumps(result).replace(', "selection":',' "selection":',1)
        return response
    engine.llm.chat=missing_comma;data=first(engine)
    assert data['status']=='waiting' and engine.llm.calls.count('summarize')==1
    assert all(p['summary_zh']=='基于DEM的LS计算。' for p in engine._related(data))
    assert next(u for u in data['usage'] if u['stage']=='summarize')['json_syntax_repair']=='missing_comma_before_selection'
