"""Real SSE parsing and persisted deltas; all provider traffic is mocked."""
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from researchclaw.llm.client import LLMClient, LLMConfig, LLMStreamAborted
from researchclaw.ideation.streams import CallStream
from researchclaw.ideation.store import now
from tests.test_grounded_ideation import engine


class SSE:
    headers={'Content-Type':'text/event-stream; charset=utf-8'}
    def __init__(self, events):self.events=events
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def __iter__(self):
        for event in self.events:
            yield ('data: '+(event if isinstance(event,str) else json.dumps(event,ensure_ascii=False))+'\n\n').encode()


def chunk(text='',finish=None,usage=None,reasoning=None):
    delta={'content':text}
    if reasoning:delta['reasoning_content']=reasoning
    return {'model':'fixture-model','choices':[{'delta':delta,'finish_reason':finish,'index':0}],'usage':usage}


def client(**kwargs):
    return LLMClient(LLMConfig(base_url='https://fixture.invalid/v1',api_key='fixture',primary_model='fixture',fallback_models=[],retry_base_delay=0,**kwargs))


def test_sse_content_arrives_before_completion_and_terminal_usage_is_preserved(monkeypatch):
    received=[];requests=[]
    class Observe(SSE):
        def __iter__(self):
            yield from SSE([chunk('前半段',reasoning='DO_NOT_EXPOSE')])
            assert received==['前半段']  # Must already be delivered, not buffered until DONE.
            yield from SSE([chunk('后半段'),chunk(finish='stop',usage={'prompt_tokens':12,'completion_tokens':7,'total_tokens':19}),'[DONE]'])
    def open_(request,**kwargs):requests.append(json.loads(request.data));return Observe([])
    monkeypatch.setattr('urllib.request.urlopen',open_)
    response=client().chat([{'role':'user','content':'fixture'}],on_content=received.append,json_mode=True)
    assert response.content=='前半段后半段' and response.total_tokens==19
    assert response.finish_reason=='stop' and not response.truncated
    assert requests[0]['stream'] and requests[0]['stream_options']['include_usage']
    assert 'DO_NOT_EXPOSE' not in response.content and 'DO_NOT_EXPOSE' not in json.dumps(response.raw)


def test_stream_retry_resets_partial_output_instead_of_concatenating_attempts(monkeypatch):
    content=[];resets=[];requests=[]
    def open_(*a,**kw):
        requests.append(1)
        return SSE([chunk('abandoned')]) if len(requests)==1 else SSE([chunk('final'),chunk(finish='stop'),'[DONE]'])
    monkeypatch.setattr('urllib.request.urlopen',open_)
    def reset(model):resets.append(model);content.clear()
    response=client(max_retries=2).chat([{'role':'user','content':'fixture'}],on_content=content.append,on_reset=reset)
    assert len(requests)==2 and len(resets)==2 and content==['final'] and response.content=='final'


def test_stream_cancellation_never_tries_another_model(monkeypatch):
    requests=[]
    def open_(*a,**kw):requests.append(1);return SSE([chunk('delta'),chunk(finish='stop'),'[DONE]'])
    monkeypatch.setattr('urllib.request.urlopen',open_)
    c=client();c._model_chain=['first','second']
    def cancel(text):raise LLMStreamAborted('cancelled')
    with pytest.raises(LLMStreamAborted):c.chat([{'role':'user','content':'fixture'}],on_content=cancel)
    assert len(requests)==1


def test_partial_stream_is_durable_and_attempts_stay_separate(tmp_path):
    stream=CallStream(tmp_path,'a'*32,'fixture',threading.Event());stream.options(client())
    stream.reset('first');stream.append('old')
    assert (stream.directory/'content-1.txt').read_text()=='old'
    stream.reset('second');stream.append('new')
    stream.finish(SimpleNamespace(content='new',finish_reason='stop',raw={'streamed':True}))
    meta=json.loads((stream.directory/'stream.json').read_text())
    assert meta['file']=='content-2.txt' and meta['mode']=='streaming' and meta['status']=='returned'
    assert (stream.directory/'content-1.txt').read_text()=='old' and (stream.directory/'content-2.txt').read_text()=='new'


def test_activity_endpoint_reads_real_partial_output_and_enforces_job_membership(engine,monkeypatch):
    from researchclaw.server.routes import ideation
    data=engine.store.create('测试真实任务流式记录',0);sid=data['session_id'];call_id='a'*32
    data.update(status='running',ideaspark_batch={'jobs':[{'id':'job','number':1,'focus':'fixture','directory':'fixture','status':'running','stage':'coherence'}]})
    data['usage']=[{'call_id':call_id,'skill_job_id':'job','stage':'ideaspark:coherence','status':'pending','at':now()}]
    engine.store.save(data);stream=CallStream(engine.store.directory(sid),call_id,'ideaspark:coherence',threading.Event())
    stream.options(client());stream.reset('fixture');stream.append('{"partial":')
    app=FastAPI();app.include_router(ideation.router);monkeypatch.setattr(ideation,'get_service',lambda:engine)
    with TestClient(app) as api:
        path=f'/api/ideation/sessions/{sid}/ideaspark/jobs/job/activity'
        result=api.get(path).json()
        assert result['output']['content']=='{"partial":' and result['output']['mode']=='streaming'
        assert result['output']['status']=='pending'
        stream.append('x'*70000);stream.file.flush();stream.save()
        assert api.get(path).json()['output']['windowed']
        assert len(api.get(path+'?full=true').json()['output']['content'])>65536
        assert api.get(path+'?call_id='+'b'*32).status_code==404
        assert api.get(path.replace('/job/','/other/')).status_code==404
        assert 'system' not in result and 'api_key' not in result
    stream.finish(None)
