"""Real threads and real state publication; model and scientific results are fixtures."""
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from researchclaw.ideation.ideaspark import IdeaSparkBatch, read, write
from researchclaw.ideation.parallel import CandidateStore, run_candidates
from researchclaw.ideation.service import Cancelled, WorkflowFailure
from tests.test_ideaspark_adapter import engine, host


def jobs_for(h, count=3):
    jobs=[{'id':str(i),'number':i+1,'focus':'fixture '+str(i),'directory':'job-'+str(i),'status':'running','steps':0} for i in range(count)]
    h.data.update(phase='audit',status='running',idea_pool=[],ideaspark_batch={'phase':'review','jobs':jobs})
    h.service.store.save(h.data)
    return jobs


def test_parallel_calls_overlap_and_publish_all_usage_cards_and_files(engine, monkeypatch):
    h=host(engine);jobs=jobs_for(h);barrier=threading.Barrier(3,timeout=10)
    peak=[]
    class Client:
        def chat(self,messages,**kwargs):
            barrier.wait()
            with engine.store.lock:peak.append(len(h.data['ideaspark_batch']['parallel']['active']))
            return SimpleNamespace(content='{"ok":true}',finish_reason='stop',model='fixture',prompt_tokens=12,completion_tokens=3)
    engine.llm=Client()
    def process(worker,job,phase):
        worker.service._progress(worker.data,worker.event,'IdeaSpark：coherence',operation='ideaspark',current=job['number'])
        worker.model('coherence','fixture system',{'candidate':job['number']})
        worker.service._json(worker.data,worker.event,'synthesis',{'candidate':job['number']})
        worker.data['reading']['fixture-'+job['id']]={'fixture':job['number']}
        worker.data['idea_pool'].append({'skill_job_id':job['id'],'skill_status':'validated','title':job['focus']})
        job['status']='validated'
    monkeypatch.setattr(IdeaSparkBatch,'process_job',process)
    run_candidates(h,jobs,'review',3)
    saved=engine.store.load(h.data['session_id'])
    assert max(peak)==3 and len(saved['cards'])==3
    assert len(saved['usage'])==6 and len({u['call_id'] for u in saved['usage']})==6
    assert all(u['status']=='returned' for u in saved['usage'])
    assert saved['cost_usd']==pytest.approx(sum(u['cost_usd'] for u in saved['usage']),abs=1e-5)
    assert len(saved['reading'])==3 and not saved['ideaspark_batch']['parallel']['active']
    assert len(list((h.directory/'model_outputs').glob('*synthesis.txt')))==3
    assert len(list((h.directory/'ideaspark/r1/host_calls').glob('*/response.json')))==3
    assert all(read(p)['thinking']=='disabled' for p in (h.directory/'ideaspark/r1/host_calls').glob('*/request.json'))


def test_stale_worker_cannot_overspend_shared_budget(engine):
    h=host(engine);jobs_for(h,2);h.data['budget_usd']=0.15
    h.data['ideaspark_batch']['parallel']={'workers':2,'active':{}}
    stores=[CandidateStore(h,str(i)) for i in range(2)]
    for s in stores:
        s.data['cost_usd']+=0.1;s.data['usage'].append({'stage':'fixture','cost_usd':0.1,'status':'pending'})
    stores[0].save(stores[0].data)
    with pytest.raises(WorkflowFailure,match='合并预算'):stores[1].save(stores[1].data)
    assert h.data['cost_usd']==0.1 and len(h.data['usage'])==1


def test_parallel_success_removes_stale_error_instead_of_resurrecting_it(engine):
    h=host(engine);jobs=jobs_for(h,1)
    jobs[0]['presentation_error']='old failed display'
    h.data['ideaspark_batch']['parallel']={'workers':1,'active':{}}
    store=CandidateStore(h,jobs[0]['id'])
    store.data['ideaspark_batch']['jobs'][0].pop('presentation_error')
    store.save(store.data)
    assert 'presentation_error' not in h.data['ideaspark_batch']['jobs'][0]


def test_parallel_cancellation_keeps_returned_calls_and_clears_active_workers(engine,monkeypatch):
    h=host(engine);jobs=jobs_for(h);barrier=threading.Barrier(3,timeout=10)
    def process(worker,job,phase):
        worker.data['usage'].append({'stage':'fixture','status':'returned','cost_usd':0})
        worker.service.store.save(worker.data)
        barrier.wait();worker.event.set();worker.service._check(worker.data,worker.event)
    monkeypatch.setattr(IdeaSparkBatch,'process_job',process)
    with pytest.raises(Cancelled):run_candidates(h,jobs,'review',3)
    saved=engine.store.load(h.data['session_id'])
    assert len(saved['usage'])==3 and saved['status']=='stopping'
    assert not saved['ideaspark_batch']['parallel']['active']


def test_parallel_drafts_finish_before_review_and_one_failure_does_not_stop_siblings(engine,monkeypatch):
    h=host(engine);h.data.update(phase='generate',approved_revision=1,synthesis={})
    engine.config.llm.ideation_card_target=6;engine.config.llm.ideation_workers=3
    def seed(worker,root):
        (root/'phase0').mkdir(parents=True);write(root/'phase1/phase1_output.json',{'state':'proceed'});return {'state':'proceed'}
    monkeypatch.setattr(IdeaSparkBatch,'seed',seed)
    monkeypatch.setattr(IdeaSparkBatch,'model',lambda *a,**kw:{'focuses':['focus '+str(i) for i in range(6)]})
    def step(worker,job):
        run=worker.directory/job['directory']
        if worker.candidate(job):
            assert len(list((worker.directory/'ideaspark/r1').glob('idea-*/phase2_generate/phase2_generate_output.json')))==6
            if job['number']==2:raise ValueError('fixture bad transport')
            job['status']='validated'
        else:write(run/'phase2_generate/phase2_generate_output.json',{'title':job['focus']})
    monkeypatch.setattr(IdeaSparkBatch,'step',step)
    def present(worker,job):
        card={'skill_job_id':job['id'],'skill_status':job['status'],'title':job['focus']}
        worker.data['idea_pool']=[c for c in worker.data.get('idea_pool',[]) if c['skill_job_id']!=job['id']]+[card]
        worker.data['cards']=[c for c in worker.data['idea_pool'] if c['skill_status']=='validated']
    monkeypatch.setattr(IdeaSparkBatch,'present',present)
    h.run()
    assert len(h.data['cards'])==5 and h.data['phase']=='idea_review'
    bad=next(j for j in h.data['ideaspark_batch']['jobs'] if j['number']==2)
    assert bad['status']=='needs_work' and bad['retryable']
    assert not h.data['ideaspark_batch']['parallel']['active']
