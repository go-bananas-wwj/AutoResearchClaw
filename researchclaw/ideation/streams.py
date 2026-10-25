"""Persist real content deltas; never synthesize progress or expose reasoning deltas."""
import json
import time
import uuid
from pathlib import Path

from .store import now
from researchclaw.llm.client import LLMClient, LLMStreamAborted


class CallStream:
    def __init__(self, directory, call_id, stage, event):
        self.directory=Path(directory)/'streams'/call_id
        self.directory.mkdir(parents=True,exist_ok=True)
        self.event=event;self.file=None;self.last_flush=0;self.attempt=0
        self.meta={'call_id':call_id,'stage':stage,'mode':'buffered','status':'waiting',
                   'started_at':now(),'updated_at':now(),'characters':0,'attempt':0}
        self.save()

    def save(self):
        self.meta['updated_at']=now()
        path=self.directory/(uuid.uuid4().hex+'.tmp')
        path.write_text(json.dumps(self.meta,ensure_ascii=False));path.replace(self.directory/'stream.json')

    def reset(self, model):
        if self.file:self.file.close()
        self.attempt+=1
        self.meta.update(model=model,attempt=self.attempt,status='waiting',characters=0,started_at=now(),
                         file=f'content-{self.attempt}.txt')
        self.file=(self.directory/self.meta['file']).open('w',encoding='utf-8')
        self.save()

    def append(self, content):
        if self.event.is_set():raise LLMStreamAborted('Research task cancelled')
        if self.file is None:self.reset(self.meta.get('model',''))
        self.file.write(content);self.meta['characters']+=len(content);self.meta['status']='streaming'
        if time.monotonic()-self.last_flush>=0.25:
            self.file.flush();self.save();self.last_flush=time.monotonic()

    def options(self, client):
        if isinstance(client,LLMClient) and not client._anthropic and client._normalize_wire_api(client.config.wire_api)=='chat_completions':
            self.meta['mode']='streaming';self.save()
            return {'on_content':self.append,'on_reset':self.reset}
        return {}

    def finish(self, response):
        if response is not None and self.meta['mode']=='buffered' and response.content:
            if self.file is None:self.reset(getattr(response,'model',''))
            self.file.write(response.content);self.meta['characters']=len(response.content)
        if self.file:self.file.flush();self.file.close();self.file=None
        if response is not None and getattr(response,'raw',{}).get('streamed') is False:self.meta['mode']='buffered'
        self.meta.update(status='returned' if response is not None else 'interrupted' if self.event.is_set() else 'failed',
            finished_at=now(),finish_reason=getattr(response,'finish_reason',''))
        self.save()
