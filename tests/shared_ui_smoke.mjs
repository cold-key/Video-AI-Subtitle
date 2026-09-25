// Run with Node 22+ and CHROME_PATH pointing to a local Chromium browser.
// Uses an isolated headless profile and mocked APIs: no account or paid model calls.
import {spawn} from "node:child_process";
import {createServer} from "node:http";
import {readFile, mkdir, writeFile} from "node:fs/promises";
import path from "node:path";
import assert from "node:assert/strict";

const root=path.resolve(import.meta.dirname,"..");
const output=path.join(root,".test-data","shared-ui");
await mkdir(output,{recursive:true});
const shim=`
window.testErrors=[];window.addEventListener('error',e=>testErrors.push(e.message));
window.addEventListener('unhandledrejection',e=>testErrors.push(String(e.reason)));
window.testRequests=[];window.captionRequests=0;window.testState='cached';
window.confirm=()=>true;
let listener;
window.chrome={runtime:{sendMessage:async message=>{
  if(message.type==='resolve-bilibili-resource')return {identity:{bvid:'BV1test123',cid:123}};
  if(message.type==='fetch-bilibili-subtitles'){captionRequests++;return {segments:[],status:'no_tracks',identity:{bvid:'BV1test123',cid:123}};}
  return {ok:true};
},onMessage:{addListener:fn=>{listener=fn;window.startTest=()=>fn({type:'start'});}}},storage:{local:{get:async defaults=>defaults,set:async()=>{}},onChanged:{addListener:()=>{}}}};
const result={video_id:'BV1test123',title:'共享字幕测试',url:location.href,duration:2,source:'whisper',platform:location.hostname.includes('bilibili')?'bilibili':'youtube',source_language:'en',segments:[{start:0,end:2,en:'Hello from another computer.',zh:'这是另一台电脑生成的字幕。'}],summary:'共享缓存摘要',key_points:[]};
const config={base_url:'https://model.invalid/v1',api_key_configured:true,translation_model:'test',summary_model:'test',whisper_model:'small',device:'cpu',shared_cache_enabled:true,shared_cache_url:'https://cache.invalid',shared_cache_token_configured:true};
window.fetch=async (url,options={})=>{
  const pathname=new URL(url,location.href).pathname;testRequests.push(pathname);
  let data={ok:true};
  if(pathname==='/config')data=config;
  else if(pathname==='/shared/jobs')data={id:'shared-test',state:'queued',stage:'正在检查共享缓存',progress:0,shared_state:'waiting'};
  else if(pathname==='/jobs/shared-test')data=testState==='offline'?{id:'shared-test',state:'running',stage:'等待共享服务恢复',shared_state:'offline',progress:0}:{id:'shared-test',state:'completed',stage:'已从共享缓存加载',shared_state:'cached',cache_origin:'共享',progress:100,result,preview_segments:result.segments,total_segments:1,translated_segments:1,summary_state:'completed',summary_partial:result.summary};
  else if(pathname==='/shared/status')data={pending:0,import:{state:'idle',total:0,done:0,uploaded:0,existing:0,skipped:0,failed:0,details:[]}};
  else if(pathname==='/prompts')data={summary:'test/prompts'};
  else if(pathname==='/models/status'||pathname==='/cuda/status')data={state:'idle',valid:false,local_models:[]};
  return {ok:true,json:async()=>data};
};`;

const server=createServer(async(req,res)=>{
  try{
    const pathname=new URL(req.url,"http://localhost").pathname;
    if(pathname==='/options.html'){
      const html=await readFile(path.join(root,"extension/options.html"),"utf8");
      res.setHeader("Content-Type","text/html;charset=utf-8");
      res.end(html.replace('<script src="options.js"></script>',`<script>${shim}</script><script src="/options.js"></script>`));
    }else if(pathname.startsWith('/video/')||pathname==='/watch'){
      res.setHeader("Content-Type","text/html;charset=utf-8");
      res.end(`<!doctype html><meta charset="utf-8"><title>Isolated video UI test</title><link rel="stylesheet" href="/content.css"><style>body{background:#242936;color:white;font:20px Arial}video{width:800px;height:450px;background:#12151b}</style><h1>共享缓存视频面板测试</h1><div class="bpx-player-container html5-video-player"><video controls></video></div><script>${shim}</script><script src="/content.js"></script>`);
    }else if(['/options.js','/content.js','/content.css'].includes(pathname)){
      res.setHeader("Content-Type",pathname.endsWith('.css')?'text/css':'application/javascript');
      res.end(await readFile(path.join(root,'extension',pathname.slice(1))));
    }else{res.writeHead(404);res.end();}
  }catch(error){res.writeHead(500);res.end(String(error));}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const port=server.address().port;
const profile=path.join(output,`profile-${Date.now()}`);
await mkdir(profile,{recursive:true});
const chrome=spawn(process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',[
  '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--no-proxy-server',
  '--remote-debugging-port=0',`--user-data-dir=${profile}`,'--window-size=1280,1100','about:blank',
],{stdio:['ignore','ignore','pipe'],windowsHide:true});
let browserErrors='';chrome.stderr.on('data',chunk=>{browserErrors+=chunk;});
let socket;
let failure;
try{
  let devtools;
  for(let attempt=0;attempt<100;attempt++){
    try{devtools=(await readFile(path.join(profile,'DevToolsActivePort'),'utf8')).split('\n');break;}catch{await new Promise(r=>setTimeout(r,100));}
  }
  assert(devtools,'Chrome did not open its isolated debugging endpoint');
  const pages=await (await fetch(`http://127.0.0.1:${devtools[0]}/json/list`,{signal:AbortSignal.timeout(10000)})).json();
  socket=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('Browser connection timed out')),10000);socket.onopen=()=>{clearTimeout(timer);resolve();};socket.onerror=()=>{clearTimeout(timer);reject(new Error('Browser connection failed'));};});
  let sequence=0;const pending=new Map();
  socket.onmessage=event=>{const message=JSON.parse(event.data);if(message.id){const item=pending.get(message.id);pending.delete(message.id);message.error?item.reject(new Error(message.error.message)):item.resolve(message.result);}};
  const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(new Error('Browser command timed out: '+method));},10000);pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}});socket.send(JSON.stringify({id,method,params}));});
  const evaluate=async expression=>{const result=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value;};
  const wait=async expression=>{for(let i=0;i<80;i++){try{if(await evaluate(expression))return;}catch{}await new Promise(r=>setTimeout(r,100));}await writeFile(path.join(output,'failed.png'),Buffer.from((await call('Page.captureScreenshot')).data,'base64'));throw new Error('Timed out: '+expression+' '+await evaluate('JSON.stringify({errors:window.testErrors,body:document.body.innerText})'));};
  await call('Page.enable');
  await call('Page.navigate',{url:`http://localhost:${port}/options.html`});
  await wait("document.querySelector('#shared_cache_enabled')?.checked");
  await evaluate("document.querySelector('details.card').open=true;document.querySelector('#test_shared_cache').click()");
  await wait("document.querySelector('#shared_state').textContent==='连接成功'");
  assert.deepEqual(await evaluate('testErrors'),[]);
  await writeFile(path.join(output,'settings.png'),Buffer.from((await call('Page.captureScreenshot',{captureBeyondViewport:true})).data,'base64'));
  for(const [site,url] of [['bilibili',`http://bilibili.com.localhost:${port}/video/BV1test123`],['youtube',`http://localhost:${port}/watch?v=test123`]]){
    await call('Page.navigate',{url});
    await wait("typeof startTest==='function'");
    await evaluate('startTest()');
    await wait("document.querySelector('[data-shared-regenerate]')?.hidden===false");
    assert.equal(await evaluate('captionRequests'),0);
    assert.equal(await evaluate("testRequests.includes('/jobs')"),false);
    assert.match(await evaluate("document.querySelector('[data-status]').textContent"),/共享缓存/);
    assert.deepEqual(await evaluate('testErrors'),[]);
    await writeFile(path.join(output,`${site}-cached.png`),Buffer.from((await call('Page.captureScreenshot')).data,'base64'));
    await evaluate("testState='offline';startTest()");
    await wait("document.querySelector('[data-shared-local]')?.hidden===false");
    assert.deepEqual(await evaluate('testErrors'),[]);
  }
  console.log('Settings and Bilibili/YouTube shared-cache UI smoke checks passed. Screenshots: '+output);
  await call('Browser.close');
}catch(error){failure=error;}
finally{socket?.close();chrome.kill();server.close();}
if(failure){await writeFile(path.join(output,'browser-error.log'),browserErrors);console.error(failure);process.exitCode=1;}
