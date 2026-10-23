"""Two human reviews, real-evidence boundaries, persistence and stage-9 handoff."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from researchclaw.ideation.prompts import PROMPTS
from researchclaw.ideation.service import IdeationService
from researchclaw.ideation.sources import paper_record
from researchclaw.ideation.store import SessionError


class Sources:
    def __init__(self, count=3):
        self.rows = [paper_record({'title':f'LS method {i}', 'doi':f'10.1234/ls{i}', 'year':2020+i,
            'abstract':'Study of the slope length and steepness factor.', 'url':f'https://example.org/ls{i}'}, source='fixture') for i in range(count)]
        self.fulltext_calls = []
        self.empty = False
        self.abstract_only = False
        self.fail_ids = set()
        self.collision_empty = False
        self.started = None
        self.release = None

    def search(self, query, period, source):
        if self.started:
            self.started.set()
            assert self.release.wait(5)
        return [] if self.empty or (query.startswith('collision') and self.collision_empty) else self.rows

    def fulltext(self, paper, directory):
        self.fulltext_calls.append(paper['paper_id'])
        if paper['paper_id'] in self.fail_ids:
            raise ValueError('full text unavailable')
        text = 'Abstract only' if self.abstract_only else ('Methods: contributing area is routed across a terrain grid. Independent observations validate erosion. '*35)
        return {'pages':[{'location':'第 2 页','text':text}], 'source_url':paper['url']}


class LLM:
    def __init__(self):
        self.calls = []
        self.off_topic = False
        self.fabricate = False
        self.truncate = ''
        self.overlap = False
        self.bad_gap = False

    def chat(self, messages, *, system, **kwargs):
        stage = next(k for k,v in PROMPTS.items() if v == system)
        data = json.loads(messages[0]['content']); self.calls.append(stage)
        if stage == 'plan':
            result = {'interpretation':'USLE/RUSLE坡长—坡度因子','domain':'水土保持',
                'queries':[{'query':f'LS factor watershed {i}','purpose':'方法与验证','period':'recent' if i == 3 else 'all'} for i in range(4)]}
        elif stage in ('screen','summarize','neighbors'):
            result = {'papers':[{'paper_id':p['paper_id'],'relevance':'off_topic' if self.off_topic else 'core',
                'reason':'相关：分析坡长与坡度','relationship':'same_mechanism','summary_zh':'基于DEM的LS计算。','overview':{'problem':'LS计算','background':'已有方法存在边界，需独立验证','innovation':'比较已有计算方法的适用条件','method':'DEM汇流','data':'DEM与独立观测','results':'原文提供条件性验证结论','strengths':'提供对照','limitations':'原文未说明'}} for p in data['papers']]}
            if stage == 'summarize':
                for row,paper in zip(result['papers'],data['papers']):
                    page=paper['material']['passages'][0]
                    row['selection']={'decision':'include','fit':'direct','rigor':'adequate','reason':'方法和验证直接回答用户的问题',
                        'evidence':[{'location':page['location'],'quote':page['text'][:80]}]}
        elif stage == 'read':
            p = data['passages'][0]
            result = {'has_methods':True,'problem':'LS','method':'汇流计算','data':'DEM','experiments':'对照',
                'findings':'条件性结论','strengths':'有依据的对照贡献','limitations':'特定地形','evidence':[{'passage_id':p['passage_id'],
                'quote':'this quotation was invented' if self.fabricate else p['text'][:80], 'claim':'采用汇流计算','kind':'source','role':'method'}]}
        elif stage == 'synthesis':
            ref = data['reading'][0]['evidence'][0]['passage_id']
            result = {'landscape_zh':'现有方法及适用边界。','method_comparison':[{'paper_id':data['reading'][0]['paper_id'],'method':'汇流计算','boundary':'特定地形'}],
                'gaps':[{'gap_id':'g1','description':'待检验的差异','evidence_refs':['invented' if self.bad_gap else ref],'inference':'模型推断，待验证'}]}
        elif stage == 'generate':
            ref = data['synthesis']['gaps'][0]['evidence_refs'][0]
            pid = data['reading'][0]['paper_id']
            result = {'cards':[{'title':'LS稳定性机制','one_liner':'测试真实机制','scientific_question':'能否改善稳定性？',
                'gap_id':'g1','method':'实施机制并对照','falsification':'独立观测不改善即推翻','novelty':'待查证差异',
                'datasets':['DEM'],'data_sources':[{'name':'DEM','url':'https://example.org/dem','availability':'public'}],
                'metrics':[{'metric_key':'RMSE','direction':'decrease'}], 'baselines':[{'name':'已知LS方法'}],
                'evidence_refs':[ref],'closest_paper_ids':[pid],'collision_queries':['collision LS operator']}]}
        else:
            ns = data['neighbors']
            result = {'verdict':'overlap' if self.overlap else 'pass','design_check':{'verdict':'pass','issues':[],'corrected_falsification':''},'reason':'在本次范围内有具体机制差异',
                'closest_paper_ids':[n['paper_id'] for n in ns],'evidence_refs':[n['evidence'][0]['passage_id'] for n in ns],
                'comparisons':[{'paper_id':n['paper_id'],'problem':'同类问题','mechanism':'存在机制差异',
                'data':'相同DEM','experiment':'使用独立观测','difference':'待验证的新机制'} for n in ns]}
        return SimpleNamespace(content=json.dumps(result,ensure_ascii=False), model='fake', prompt_tokens=20,
            completion_tokens=30, finish_reason='length' if stage == self.truncate else 'stop', truncated=stage == self.truncate)


@pytest.fixture
def engine(tmp_path):
    config = SimpleNamespace(llm=SimpleNamespace(primary_model='fake'),hitl=SimpleNamespace(cost_budget_usd=50),
        literature_search=SimpleNamespace(sources=['openalex']), export=SimpleNamespace(target_conference='tgrs'))
    return IdeationService(tmp_path/'ideation',config,llm=LLM(),sources=Sources())


def wait(engine, sid):
    engine.jobs[sid][0].join(10)
    assert not engine.jobs[sid][0].is_alive()
    return engine.snapshot(sid)


def first(engine):
    data = engine.create('我想研究流域建模的 LS 因子')
    return wait(engine,data['session_id'])


def develop(engine):
    data = first(engine)
    assert data['phase'] == 'literature_review'
    engine.review_literature(data['session_id'],data['revision'],[p['paper_id'] for p in data['papers']],[])
    return wait(engine,data['session_id'])


def test_two_reviews_and_provenance(engine):
    data = first(engine); sid=data['session_id']
    assert data['status']=='waiting' and len(engine.sources.fulltext_calls)==3
    assert not data['reading'] and all(p['fulltext_status']=='downloaded' for p in data['papers'])
    assert engine.llm.calls==['plan','screen','summarize']
    assert not list(engine.store.root.parent.glob('rc-*'))
    with pytest.raises(SessionError): engine.regenerate(sid,data['revision'],'skip')
    with pytest.raises(SessionError): engine.confirmation(sid,data['revision'],'fake')
    with pytest.raises(SessionError): engine.review_literature(sid,999,[],[])
    engine.review_literature(sid,data['revision'],[p['paper_id'] for p in data['papers']],[])
    data=wait(engine,sid)
    assert data['phase']=='idea_review' and data['status']=='waiting' and not data['run_id']
    assert len(data['cards'])==1 and len(data['reading'])==3
    assert data['cards'][0]['evidence'][0]['location']=='第 2 页'
    assert data['cards'][0]['audit']['verdict']=='pass'
    assert data['cost_usd']>0
    selected,card=engine.confirmation(sid,data['revision'],data['cards'][0]['card_id'])
    brief,path=engine.export(selected,card)
    assert brief['source_ideation_run']==str(path.resolve())
    assert (path/'stage-04/references.bib').exists()
    assert (path/'manifest.json').exists()
    assert not list(engine.store.root.parent.glob('rc-*'))


@pytest.mark.parametrize('failure',['empty','off_topic','abstract','fabricate','truncated','bad_gap','collision_empty','overlap'])
def test_no_fallback_success(engine,failure):
    if failure=='empty': engine.sources.empty=True
    if failure=='off_topic': engine.llm.off_topic=True
    if failure=='abstract': engine.sources.abstract_only=True
    if failure=='fabricate': engine.llm.fabricate=True
    if failure=='truncated': engine.llm.truncate='generate'
    if failure=='bad_gap': engine.llm.bad_gap=True
    if failure=='collision_empty': engine.sources.collision_empty=True
    if failure=='overlap': engine.llm.overlap=True
    data=first(engine)
    if data['phase']=='literature_review':
        engine.review_literature(data['session_id'],data['revision'],[p['paper_id'] for p in data['papers']],[])
        data=wait(engine,data['session_id'])
    assert data['status']=='blocked' and data['cards']==[] and data['missing']
    assert not data['run_id']
    if data['approved_revision']==data['revision'] and not data.get('synthesis'):
        with pytest.raises(SessionError):
            engine.regenerate(data['session_id'],data['revision'],'不得跳过精读与证据阶段')


def test_must_read_is_not_optional(engine):
    engine.sources=Sources(4)
    pid=engine.sources.rows[0]['paper_id'];engine.sources.fail_ids={pid}
    data=first(engine)
    with pytest.raises(SessionError,match='全文'):
        engine.review_literature(data['session_id'],data['revision'],[p['paper_id'] for p in data['papers']],[pid])
    assert not engine.snapshot(data['session_id'])['approved_revision']
    assert pid not in {p['paper_id'] for p in engine._related(data)}


def test_revision_invalidation_and_session_isolation(engine):
    data=develop(engine); sid=data['session_id']; revision=data['revision']; card_id=data['cards'][0]['card_id']
    engine.add_paper(sid,revision,paper_record({'title':'Supplemental method','doi':'10.1234/new'},source='user'))
    changed=engine.snapshot(sid)
    assert changed['revision']>revision and changed['approved_revision'] is None
    assert changed['reading']=={} and changed['cards']==[] and changed['phase']=='literature_review'
    with pytest.raises(SessionError): engine.confirmation(sid,revision,card_id)
    second=first(engine)
    assert second['session_id']!=sid and second['reading']=={} and second['cards']==[]


def test_regenerate_uses_approved_evidence(engine):
    data=develop(engine); old=data['revision']; calls=len(engine.sources.fulltext_calls)
    engine.regenerate(data['session_id'],old,'请缩小机制')
    data=wait(engine,data['session_id'])
    assert data['phase']=='idea_review' and data['revision']==old+1
    assert len(engine.sources.fulltext_calls)==calls
    assert data['cards'][0]['card_id'].startswith(f'r{old+1}-')


def test_direction_confirmation_delegates_selection_and_reading(engine):
    engine.sources=Sources(5)
    data=first(engine);sid=data['session_id']
    data['papers'][0]['selected']=False  # Stale per-paper toggles must not control this flow.
    data['papers'][1]['relevance']='adjacent'
    data['papers'][2]['relevance']='off_topic'
    data['papers'][2]['selected']=True
    engine.store.save(data)
    chosen={p['paper_id'] for p in data['papers'] if p['relevance'] in ('core','adjacent')}
    assert not data['reading'] and 'read' not in engine.llm.calls
    engine.confirm_direction(sid,data['revision'],'关注流域尺度的LS计算')
    result=wait(engine,sid)
    assert result['phase']=='idea_review' and result['run_id'] is None
    assert {p['paper_id'] for p in result['papers'] if p['selected']}==chosen
    review=next(r for r in result['reviews'] if r.get('type')=='literature')
    assert review['selection_by']=='agent' and review['decision']=='direction_confirmed'
    assert not review['required']
    assert {pid for pid,n in result['reading'].items() if not n.get('supplemental')}==chosen
    with pytest.raises(SessionError):engine.confirm_direction(sid,data['revision'])


def test_metadata_refresh_does_not_approve_or_start_reading(engine):
    data=first(engine);sid=data['session_id'];calls=list(engine.llm.calls);downloads=list(engine.sources.fulltext_calls)
    engine.sources.enrich_metadata=lambda paper:{'venue':'Soil Research','venue_type':'journal',
        'authors':[{'name':'Researcher A','affiliation':'University A'}],'institutions':['University A'], 'bibliography_version':1}
    engine.refresh_metadata(sid,data['revision']);result=wait(engine,sid)
    assert result['phase']=='literature_review' and result['status']=='waiting'
    assert result['revision']==data['revision'] and result['approved_revision'] is None
    assert not result['reading'] and not result['cards'] and not result['run_id']
    assert engine.llm.calls==calls and engine.sources.fulltext_calls==downloads
    assert all(p['venue']=='Soil Research' for p in result['papers'])


def test_publication_source_survives_handoff_export(engine):
    engine.sources.enrich_metadata=lambda paper:{'venue':'Soil Research','venue_type':'journal',
        'authors':[{'name':'Test Author','affiliation':'Test University'}],'institutions':['Test University'],'bibliography_version':1}
    data=develop(engine);card=data['cards'][0]
    _,directory=engine.export(data,card)
    bib=(directory/'stage-04/references.bib').read_text()
    assert 'journal = {Soil Research}' in bib and 'author = {Test Author}' in bib


@pytest.mark.asyncio
async def test_direction_confirmation_api_accepts_no_paper_checklist(engine,monkeypatch):
    import httpx
    from fastapi import FastAPI
    from researchclaw.server.app import _app_state
    from researchclaw.server.routes import ideation
    data=first(engine);sid=data['session_id']
    app=FastAPI();app.include_router(ideation.router)
    monkeypatch.setitem(_app_state,'ideation_service',engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        stale=await client.post(f'/api/ideation/sessions/{sid}/direction-confirm',json={'revision':999})
        assert stale.status_code==409
        result=await client.post(f'/api/ideation/sessions/{sid}/direction-confirm',json={'revision':data['revision']})
        assert result.status_code==200,result.text
        done=await asyncio.to_thread(wait,engine,sid)
        assert done['phase']=='idea_review' and done['run_id'] is None


def test_stop_waits_for_worker_and_resume(engine):
    engine.sources.started=threading.Event();engine.sources.release=threading.Event()
    data=engine.create('我想研究流域建模的 LS 因子');sid=data['session_id']
    assert engine.sources.started.wait(3)
    assert engine.stop(sid)['status']=='stopping'
    with pytest.raises(SessionError): engine.revise_scope(sid,data['revision'],'另一个研究方向')
    engine.sources.release.set()
    data=wait(engine,sid);assert data['status']=='stopped' and not data['cards']
    engine.sources.started=None
    engine.resume(sid,data['revision']);data=wait(engine,sid)
    assert data['phase']=='literature_review'


def test_restart_and_budget(engine):
    data=engine.store.create('新的研究方向',0.001);data['status']='running';engine.store.save(data)
    recovered=IdeationService(engine.store.root,engine.config,llm=engine.llm,sources=engine.sources)
    assert recovered.snapshot(data['session_id'])['status']=='interrupted'
    recovered.config.hitl.cost_budget_usd=0.001
    recovered.resume(data['session_id'],1);data=wait(recovered,data['session_id'])
    assert data['status']=='blocked' and recovered.llm.calls==[]


@pytest.mark.asyncio
async def test_api_handoff_and_legacy_bypass(engine,tmp_path,monkeypatch):
    import httpx
    from researchclaw.config import RCConfig
    from researchclaw.server.app import create_app,_app_state
    from researchclaw.server.routes import ideation,pipeline
    from researchclaw.pipeline.executor import StageResult
    from researchclaw.pipeline.stages import Stage,StageStatus
    from researchclaw.hardware import HardwareProfile
    data=develop(engine);sid=data['session_id'];card=data['cards'][0]
    config=RCConfig.from_dict({'project':{'name':'test'},'research':{'topic':'test'},'runtime':{'timezone':'UTC'},
        'notifications':{'channel':'console'},'knowledge_base':{'root':str(tmp_path/'kb')},
        'llm':{'base_url':'https://example.org','api_key_env':'TEST'}},check_paths=False)
    app=create_app(config);_app_state['ideation_service']=engine
    monkeypatch.chdir(tmp_path);monkeypatch.setattr(pipeline,'_active_run',None)
    handoff_started, handoff_release = threading.Event(), threading.Event()
    def access_check(card):
        handoff_started.set()
        assert handoff_release.wait(5)
    monkeypatch.setattr(ideation,'_check_data_access',access_check)
    monkeypatch.setattr('researchclaw.hardware.detect_hardware',lambda:HardwareProfile(False,'cpu','CPU',None,'limited',''))
    calls=[]
    def runner(**kwargs):
        calls.append(kwargs)
        return [StageResult(stage=Stage.EXPERIMENT_DESIGN,status=StageStatus.DONE,artifacts=()),StageResult(stage=Stage.CITATION_VERIFY,status=StageStatus.DONE,artifacts=())]
    monkeypatch.setattr('researchclaw.pipeline.runner.execute_pipeline',runner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        for path,body in [('/api/ideation/cards',{'direction':'任意研究方向'}),('/api/ideation/confirm',{'direction':'x','card':card})]:
            assert (await client.post(path,json=body)).status_code==409
        assert (await client.post(f'/api/ideation/sessions/{sid}/confirm',json={'revision':999,'card_id':card['card_id']})).status_code==409
        payload={'revision':data['revision'],'card_id':card['card_id']}
        pending=asyncio.create_task(client.post(f'/api/ideation/sessions/{sid}/confirm',json=payload))
        assert await asyncio.to_thread(handoff_started.wait,3)
        mutation=await client.post(f'/api/ideation/sessions/{sid}/scope',json={'revision':data['revision'],'direction':'交接期间修改方向'})
        assert mutation.status_code==409
        assert (await client.post(f'/api/ideation/sessions/{sid}/stop')).status_code==409
        handoff_release.set()
        a=await pending
        assert a.status_code==200,a.text
        await pipeline._run_task
        b=await client.post(f'/api/ideation/sessions/{sid}/confirm',json=payload)
        assert a.json()['run_id']==b.json()['run_id']
        assert len(calls)==1 and calls[0]['from_stage']==Stage.EXPERIMENT_DESIGN
        assert calls[0]['auto_approve_gates'] is True and 'to_stage' not in calls[0]
        assert pipeline._active_run['status']=='completed'
        run=tmp_path/'artifacts'/a.json()['run_id']
        assert (run/'ideation_evidence/manifest.json').exists()
        assert any(r.get('type')=='idea' for r in json.loads((run/'ideation_evidence/manifest.json').read_text())['reviews'])
        assert (run/'stage-04/references.bib').exists()
        assert (run/'stage-01/hardware_profile.json').exists()
        snapshot=await client.get(f'/api/ideation/sessions/{sid}')
        assert snapshot.json()['run_id']==a.json()['run_id']


def test_conservative_cost_is_run_scoped(tmp_path):
    from researchclaw.cost_tracker import tracked_run,get_global_tracker
    from researchclaw.hitl.cost_guard import CostGuard
    @tracked_run
    def run(*,run_dir):
        tracker=get_global_tracker()
        before=tracker.total_cost_usd
        tracker.record(SimpleNamespace(model='fake',prompt_tokens=100,completion_tokens=100,content='{}'),[])
        return before,tracker.total_cost_usd
    first=tmp_path/'a'; second=tmp_path/'b'
    assert run(run_dir=first)==(0,0.003)
    assert run(run_dir=second)==(0,0.003)
    assert run(run_dir=first)==(0.003,0.006)
    assert CostGuard(budget_usd=0.005).check(first).over_budget
    assert get_global_tracker().total_cost_usd==0


def test_unknown_dataset_blocks_handoff_without_network():
    from researchclaw.server.routes.ideation import _check_data_access
    with pytest.raises(SessionError):
        _check_data_access({'data_sources':[{'name':'not verified','url':'','availability':'unknown'}]})


@pytest.mark.asyncio
async def test_pdf_upload_invalidates_review(engine,monkeypatch):
    import httpx,fitz
    from researchclaw.server.app import _app_state
    from fastapi import FastAPI
    from researchclaw.server.routes import ideation
    from researchclaw.ideation.sources import Sources as RealSources
    engine.sources.pdf_document=RealSources(engine.config).pdf_document
    data=first(engine);sid=data['session_id']
    app=FastAPI();app.include_router(ideation.router)
    monkeypatch.setitem(_app_state,'ideation_service',engine)
    doc=fitz.open();page=doc.new_page();page.insert_text((50,50),'Methods: actual uploaded PDF text. '*6,fontsize=8)
    doc.set_metadata({'title':'User supplied LS paper'});raw=doc.tobytes();doc.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        path=f'/api/ideation/sessions/{sid}/papers/upload?revision=1'
        bad=await client.post(path,content=b'not a pdf');assert bad.status_code==400
        uploaded=await client.post(path,content=raw,headers={'Content-Type':'application/pdf','X-Filename':'paper.pdf'})
        assert uploaded.status_code==200,uploaded.text
        result=uploaded.json();assert result['revision']==2 and result['approved_revision'] is None
        paper=result['papers'][-1];assert paper['source']=='user_pdf' and paper['required']
        assert (engine.store.directory(sid)/paper['uploaded_pdf']).exists()
        stale=await client.post(path,content=raw);assert stale.status_code==409


def test_remove_paper_invalidates_downstream_and_agent_does_not_reselect(engine):
    engine.sources=Sources(4)
    data=develop(engine);sid=data['session_id'];old=data['revision'];pid=data['papers'][0]['paper_id']
    updated=engine.remove_paper(sid,old,pid)
    assert updated['revision']==old+1 and updated['approved_revision'] is None
    assert not updated['reading'] and not updated['cards'] and updated['phase']=='literature_review'
    assert next(p for p in updated['papers'] if p['paper_id']==pid)['user_excluded']
    with pytest.raises(SessionError):engine.confirm_direction(sid,old)
    calls=len(engine.sources.fulltext_calls)
    engine.confirm_direction(sid,updated['revision']);done=wait(engine,sid)
    assert done['phase']=='idea_review' and not done['run_id']
    assert pid not in {p['paper_id'] for p in done['papers'] if p['selected']}
    assert pid not in engine.sources.fulltext_calls[calls:]


def test_summary_refresh_is_presentation_only(engine):
    data=first(engine);sid=data['session_id'];before=list(engine.llm.calls);downloads=list(engine.sources.fulltext_calls)
    engine.refresh_summaries(sid,data['revision']);done=wait(engine,sid)
    assert done['revision']==data['revision'] and done['approved_revision'] is None
    assert done['phase']=='literature_review' and done['status']=='waiting'
    assert not done['reading'] and not done['cards'] and not done['run_id']
    assert engine.sources.fulltext_calls==downloads and engine.llm.calls[len(before):]==['summarize']
    assert all(p['summary_version']==5 and set(p['overview'])=={'problem','background','innovation','method','data','results','strengths','limitations'} for p in done['papers'])


def test_backfill_uses_related_reserve_and_never_exceeds_fifteen(engine):
    engine.sources=Sources(18)
    engine.sources.fail_ids={p['paper_id'] for p in engine.sources.rows[:8]}
    data=first(engine);sid=data['session_id']
    engine.confirm_direction(sid,data['revision']);done=wait(engine,sid)
    assert done['phase'] in ('audit','idea_review')
    primary=[p for p in done['papers'] if p.get('fulltext_status')=='read']
    assert len(primary)>=3 and len([r for r in done['reading'].values() if not r.get('supplemental')])<=15
    assert not done['run_id']


def test_adjacent_fulltext_cannot_replace_three_core_papers(engine):
    data=first(engine);sid=data['session_id']
    data['papers'][0]['relevance']='adjacent';engine.store.save(data)
    engine.confirm_direction(sid,data['revision']);done=wait(engine,sid)
    assert len(done['reading'])==3 and done['status']=='blocked' and not done['cards']
    assert '2篇核心论文' in done['missing'][0]


def test_read_repair_preserves_exact_source_requirement(engine):
    original=engine.llm.chat;attempts=[]
    def chat(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['read']:
            payload=json.loads(messages[0]['content']);attempts.append(payload)
            if 'validation_feedback' not in payload:
                result=json.loads(response.content);result['evidence'][0]['quote']='a fabricated quotation with no source'
                response.content=json.dumps(result)
        return response
    engine.llm.chat=chat
    data=develop(engine)
    assert data['phase']=='idea_review' and any('validation_feedback' in p for p in attempts)
    assert all(e['quote']!='a fabricated quotation with no source' for r in data['reading'].values() for e in r['evidence'])


def test_invalid_json_is_retried_once_and_accounted(engine):
    data=first(engine);sid=data['session_id'];before=len(data['usage'])
    original=engine.llm.chat
    def malformed(messages,*,system,**kwargs):
        result=original(messages,system=system,**kwargs)
        if system==PROMPTS['summarize']:
            result.content='{"papers": [broken JSON'
        return result
    engine.llm.chat=malformed
    engine.refresh_summaries(sid,data['revision']);done=wait(engine,sid)
    assert done['status']=='blocked' and '两次' in done['missing'][0]
    assert len(done['usage'])==before+2 and not done['approved_revision'] and not done['cards']
    assert len(list((engine.store.directory(sid)/'model_outputs').glob('*summarize.txt')))==3


def test_empty_abstract_label_does_not_create_a_long_invented_summary(engine):
    paper={'title':'LS factor methods','abstract':'摘要：'}
    engine._apply_summary(paper,{'overview':dict.fromkeys(['problem','method','strengths','limitations'],'unsupported'),
                                'summary_zh':'unsupported long generic paragraph'})
    assert paper['summary_basis'] is None and not paper['summary_zh']
    assert paper['overview']=={} and paper['summary_state']=='awaiting_source'


def test_quote_id_can_only_be_corrected_from_unique_exact_same_paper_text(engine):
    original=engine.llm.chat
    def wrong_id(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['read']:
            result=json.loads(response.content)
            result['evidence'][0]['passage_id']='wrong-passage-id'
            result['evidence'][0]['quote']='a fabricated quotation that is absent from the article'
            response.content=json.dumps(result)
        return response
    engine.llm.chat=wrong_id
    data=develop(engine)
    assert data['status']=='blocked' and not data['reading'] and not data['cards']
    # Use a source whose passage text is unique rather than repeated fixture boilerplate.
    engine.llm.chat=original
    source_fulltext=engine.sources.fulltext
    def fulltext(paper,directory):
        doc=source_fulltext(paper,directory)
        doc['pages'][0]['text']='Methods: unique routing operator with independent observations. '+doc['pages'][0]['text']
        return doc
    engine.sources.fulltext=fulltext
    def correctable(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['read']:
            result=json.loads(response.content);result['evidence'][0]['passage_id']='wrong-passage-id'
            response.content=json.dumps(result)
        return response
    engine.llm.chat=correctable
    engine.sources.fulltext=source_fulltext
    ambiguous=develop(engine)
    assert ambiguous['status']=='blocked' and not ambiguous['reading'] and not ambiguous['cards']
    engine.sources.fulltext=fulltext
    fresh=develop(engine)
    assert fresh['phase']=='idea_review'
    for note in fresh['reading'].values():
        e=note['evidence'][0]
        assert e['model_passage_id']=='wrong-passage-id' and e['citation_resolution']=='unique_exact_quote'
        assert e['passage_id'] in {p['passage_id'] for p in note['passages']}


def test_acquisition_covers_all_related_without_starting_deep_reading(engine):
    engine.sources=Sources(18)
    data=first(engine)
    assert len(set(engine.sources.fulltext_calls))==18
    assert all(p['fulltext_status']=='downloaded' for p in data['papers'])
    assert not data['reading'] and not data['cards'] and data['approved_revision'] is None and data['run_id'] is None
    assert 'read' not in engine.llm.calls
    assert len(list((engine.store.directory(data['session_id'])/'fulltext').glob('*.json')))==18


def test_original_downloads_are_checkpoints_not_new_approvals(engine):
    data=first(engine);sid=data['session_id'];calls=list(engine.llm.calls);downloads=list(engine.sources.fulltext_calls)
    engine.acquire_fulltexts(sid,data['revision']);done=wait(engine,sid)
    assert done['phase']=='literature_review' and done['status']=='waiting'
    assert done['revision']==data['revision'] and done['approved_revision'] is None
    assert not done['reading'] and not done['run_id'] and engine.llm.calls==calls
    assert engine.sources.fulltext_calls==downloads and 'download_task' not in done


def test_original_download_stop_and_restart_resume_preserve_first_gate(engine):
    data=first(engine);sid=data['session_id'];first_id=data['papers'][0]['paper_id']
    (engine.store.directory(sid)/'fulltext'/(first_id+'.json')).unlink()
    started=threading.Event();release=threading.Event();original=engine.sources.fulltext
    def slow(paper,directory):
        started.set();assert release.wait(5)
        return original(paper,directory)
    engine.sources.fulltext=slow
    engine.acquire_fulltexts(sid,data['revision']);assert started.wait(3)
    assert engine.stop(sid)['status']=='stopping'
    release.set();stopped=wait(engine,sid)
    assert stopped['status']=='stopped' and stopped['download_task']
    count=len(engine.sources.fulltext_calls)
    restarted=IdeationService(engine.store.root,engine.config,llm=engine.llm,sources=engine.sources)
    restarted.resume(sid,stopped['revision']);done=wait(restarted,sid)
    assert done['status']=='waiting' and done['phase']=='literature_review'
    assert not done['approved_revision'] and not done['reading'] and not done['run_id']
    assert len(engine.sources.fulltext_calls)==count and 'download_task' not in done


def test_read_must_quote_method_instead_of_only_results(engine):
    original=engine.llm.chat
    def result_only(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['read']:
            payload=json.loads(response.content);payload['evidence'][0]['role']='result';response.content=json.dumps(payload)
        return response
    engine.llm.chat=result_only
    data=develop(engine)
    assert data['status']=='blocked' and not data['reading'] and not data['cards']
    assert all(p['fulltext_status']=='downloaded' for p in data['papers'])
    assert any('方法正文' in p.get('fulltext_error','') for p in data['papers'])


@pytest.mark.asyncio
async def test_original_api_serves_private_cache_without_credentials_or_approval(engine,monkeypatch):
    import httpx
    from fastapi import FastAPI
    from researchclaw.server.app import _app_state
    from researchclaw.server.routes import ideation
    data=first(engine);sid=data['session_id'];pid=data['papers'][0]['paper_id']
    app=FastAPI();app.include_router(ideation.router);monkeypatch.setitem(_app_state,'ideation_service',engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        url=f'/api/ideation/sessions/{sid}/papers/{pid}/fulltext'
        response=await client.get(url)
        assert response.status_code==200 and 'Methods:' in response.text
        pdf=engine.store.directory(sid)/'downloads'/(pid+'.pdf');pdf.parent.mkdir(exist_ok=True);pdf.write_bytes(b'%PDF-source-fixture')
        response=await client.get(url)
        assert response.status_code==200 and response.content==b'%PDF-source-fixture'
        assert response.headers['content-type']=='application/pdf'
        stale=await client.post(f'/api/ideation/sessions/{sid}/fulltexts',json={'revision':999})
        assert stale.status_code==409
        response=await client.post(f'/api/ideation/sessions/{sid}/fulltexts',json={'revision':data['revision']})
        assert response.status_code==200
        done=await asyncio.to_thread(wait,engine,sid)
        assert not done['approved_revision'] and not done['reading'] and not done['run_id']
        assert (await client.get(f'/api/ideation/sessions/{sid}/papers/p-unknown/fulltext')).status_code==404


def test_regeneration_refreshes_latest_evidence_and_retains_collision_lessons(engine):
    data=develop(engine);sid=data['session_id'];old=data['revision'];old_card=data['cards'][0]['card_id']
    data['rejected_cards']=[{'title':'已被覆盖的机制','method':'常规算法比较','rejection_reason':'已有同机制工作','audit':{'verdict':'overlap'}}]
    engine.store.save(data)
    payloads=[];original=engine.llm.chat
    def capture(messages,*,system,**kwargs):
        if system in (PROMPTS['synthesis'],PROMPTS['generate']):payloads.append((system,json.loads(messages[0]['content'])))
        return original(messages,system=system,**kwargs)
    engine.llm.chat=capture
    engine.regenerate(sid,old,'基于新增正文重新定位问题');done=wait(engine,sid)
    assert done['phase']=='idea_review' and done['revision']==old+1 and not done['run_id']
    assert len(payloads)==2 and all(p['prior_rejections'][0]['audit']['verdict']=='overlap' for _,p in payloads)
    history=next(r for r in done['reviews'] if r.get('type')=='regenerate')
    assert history['synthesis'] and history['rejected_cards'][0]['title']=='已被覆盖的机制'
    assert history['inherited_literature_approval']==old
    with pytest.raises(SessionError):engine.confirmation(sid,old,old_card)


def test_regeneration_accumulates_binding_lessons_across_rounds(engine):
    data=develop(engine);sid=data['session_id']
    data['prior_rejections']=[{'title':'首轮方案','method':'旧机制','reason':'已有明确先例','audit':{'verdict':'overlap'}}]
    data['rejected_cards']=[{'title':'次轮方案','method':'另一旧机制','rejection_reason':'先例仍覆盖','audit':{'verdict':'overlap'}}]
    engine.store.save(data)
    engine.regenerate(sid,data['revision'],'保留两轮教训');done=wait(engine,sid)
    assert {x['title'] for x in done['prior_rejections']}=={'首轮方案','次轮方案'}
    assert done['phase']=='idea_review' and done['run_id'] is None


def test_reversed_falsification_requires_independent_recheck(engine):
    original=engine.llm.chat;reviews=[]
    patch='只在所有允许参数都不能补偿给定空间扰动、且输出差异超过预先指定容差时，才反驳该扰动不可辨识的假设。'
    def review(messages,*,system,**kwargs):
        response=original(messages,system=system,**kwargs)
        if system==PROMPTS['audit']:
            payload=json.loads(messages[0]['content']);reviews.append(payload)
            result=json.loads(response.content)
            if payload['candidate']['falsification']!=patch:
                result['design_check']={'verdict':'revise','issues':['推翻判据方向写反'],'corrected_falsification':patch}
            response.content=json.dumps(result)
        return response
    engine.llm.chat=review
    data=develop(engine)
    assert data['phase']=='idea_review' and len(reviews)==2
    assert data['cards'][0]['falsification']==patch and data['cards'][0]['design_revisions']
    assert data['cards'][0]['audit']['design_check']['verdict']=='pass'


def test_design_repair_cannot_override_novelty_overlap(engine):
    engine.llm.overlap=True
    data=develop(engine)
    assert data['status']=='blocked' and not data['cards']
    assert data['rejected_cards'][0]['audit']['verdict']=='overlap'


def test_nominated_supplemental_baseline_cannot_be_silently_omitted(engine):
    data=develop(engine);sid=data['session_id']
    extra=paper_record({'title':'Critical supplemental baseline','doi':'10.1234/supplemental','url':'https://example.org/supplemental'},source='fixture')
    engine._read_paper(data,threading.Event(),extra,extra=True)
    data['candidates'][0]['closest_paper_ids'].append(extra['paper_id'])
    data.update(cards=[],rejected_cards=[],phase='audit');engine.store.save(data)
    engine.launch(sid);done=wait(engine,sid)
    assert done['phase']=='idea_review'
    assert extra['paper_id'] in done['cards'][0]['audit']['closest_paper_ids']
    assert extra['paper_id'] in {p['paper_id'] for p in done['cards'][0]['closest_papers']}
