const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
function setup(){
  const timers=new Map(),beats=new Map(),events=new Map(),sockets=[];let id=0;
  const target={addEventListener:(k,f)=>events.set(k,f),removeEventListener:k=>events.delete(k),visibilityState:'visible'};
  class Socket{static OPEN=1;static CONNECTING=0;constructor(url){this.url=url;this.readyState=0;this.sent=[];sockets.push(this);}send(x){this.sent.push(x);}close(){this.readyState=3;this.onclose?.();}open(){this.readyState=1;this.onopen?.();}}
  const ctx={window:target,document:target,location:{protocol:'http:',host:'fixture'},WebSocket:Socket,console,
    setTimeout:(f,delay)=>{timers.set(++id,{f,delay});return id;},clearTimeout:x=>timers.delete(x),
    setInterval:f=>{beats.set(++id,f);return id;},clearInterval:x=>beats.delete(x)};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/services/websocket.js'),'utf8')+';globalThis.Client=RCWebSocket;',ctx);
  return {client:new ctx.Client('/ws/events'),timers,beats,events,sockets};
}
test('retries continue past ten failures with capped delays and recover',()=>{
  const s=setup();s.client.connect();
  for(let i=0;i<13;i++){s.sockets.at(-1).close();assert.equal(s.timers.size,1);const [id,t]=[...s.timers][0];assert.ok(t.delay<=30000);s.timers.delete(id);t.f();}
  assert.equal(s.sockets.length,14);s.sockets.at(-1).open();assert.equal(s.client._reconnects,0);
  [...s.beats.values()][0]();assert.deepEqual(s.sockets.at(-1).sent,['ping']);
  s.client.close();assert.equal(s.timers.size,0);assert.equal(s.beats.size,0);assert.equal(s.events.size,0);
});
test('wake reconnects promptly without duplicate sockets or orphan retries',()=>{
  const s=setup();s.client.connect();s.sockets[0].close();assert.equal(s.timers.size,1);
  s.events.get('online')();assert.equal(s.sockets.length,2);assert.equal(s.timers.size,0);
  s.events.get('visibilitychange')();assert.equal(s.sockets.length,2);
  s.sockets[0].onclose();assert.equal(s.timers.size,0);
  s.sockets[1].close();s.client.close();assert.equal(s.timers.size,0);
});
