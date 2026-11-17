#!/usr/bin/env node
'use strict';

// Real dashboard handlers with disposable HTTP fixtures in both host modes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {CdpSession, findChromium, freePort, waitForCdp, stopProcess} = require('./mobile_smoke');
const ROOT = path.resolve(__dirname, '..');
const output = process.env.WARDROBE_SMOKE_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), 'selfie-wardrobe-smoke-'));
const source = name => fs.readFileSync(path.join(ROOT, 'pages/dashboard/source', name), 'utf8');
const sdk = `
  if(window.parent!==window) {
    let serial=0;const pending=new Map();
    window.addEventListener('message',event=>{
      if(event.source!==parent)return;
      const item=pending.get(event.data.id);if(!item)return;
      pending.delete(event.data.id);item(event.data.result);
    });
    const request=(op,endpoint,body)=>new Promise(resolve=>{const id=++serial;pending.set(id,resolve);parent.postMessage({id,op,endpoint,body},'*');});
    window.AstrBotPluginPage={ready:async()=>{},apiGet:(endpoint,body)=>request('GET',endpoint,body),apiPost:(endpoint,body)=>request('POST',endpoint,body)};
  }
`;
const page = fs.readFileSync(path.join(ROOT, 'pages/dashboard/index.source.html'), 'utf8')
  .replace('<body>', '<body class="authed">')
  .replace(/<script src="\/api\/plugin\/page\/bridge-sdk.js" defer><\/script>/, `<script>${sdk}</script>`)
  .replace(/<!-- dashboard-source: ([\w.-]+) -->/g, (_, name) => name === 'bootstrap.js'
    ? source(name).split('    (async function init()')[0] : source(name));
const host = `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
  <body style="margin:0"><iframe src="/fixture" sandbox="allow-scripts" style="display:block;width:100%;height:100vh;border:0"></iframe><script>
  window.addEventListener('message',async event=>{
    if(event.source!==document.querySelector('iframe').contentWindow||!event.data.op)return;
    const {id,op,endpoint,body}=event.data;
    const response=await fetch('/api/'+endpoint,op==='POST'?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
    event.source.postMessage({id,result:await response.json()},'*');
  });</script></body></html>`;
let items, selectionMode, current, calls, failFile, holdUpload, releaseUpload, scheduleReady;
const wardrobe = () => ({items, selection_mode:selectionMode, current_item_id:current,
  current_item_name:items.find(item=>item.id===current)?.name || '', manual_today:!!current});
const daily = () => ({schedule:{date:'2026-11-17',source:'local',outfits:{main:items.find(item=>item.id===current)?.name || '日常'},
  events:[{id:'day',start:'08:00',end:'18:00',title:'日间安排',outfit_ref:'main'}]}});
const missingDaily = () => ({schedule:null,current_event:null,next_event:null});

async function main() {
  fs.mkdirSync(output,{recursive:true});
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'selfie-wardrobe-browser-'));
  const server=http.createServer(async (request,response)=>{
    const url=new URL(request.url,'http://fixture');
    if(url.pathname==='/fixture'||url.pathname==='/host') {
      response.setHeader('Content-Type','text/html; charset=utf-8');response.end(url.pathname==='/host'?host:page);return;
    }
    if(!url.pathname.startsWith('/api/')){response.end();return;}
    const chunks=[];for await(const chunk of request)chunks.push(chunk);
    const body=chunks.length?JSON.parse(Buffer.concat(chunks)):{};
    calls.push({path:url.pathname,body});
    let data={},error='';
    if(url.pathname==='/api/selfie-wardrobe/add') {
      if(holdUpload)await new Promise(resolve=>{releaseUpload=resolve;});
      if(body.name==='retry'&&failFile)error='fixture upload error';
      else if(!items.some(item=>item.name===body.name))items.push({id:`wardrobe_${items.length}`,name:body.name,enabled:true,tags:[]});
      data=wardrobe();
    } else if(url.pathname==='/api/selfie-wardrobe/settings') {
      selectionMode=body.selection_mode;data=wardrobe();
    } else if(url.pathname==='/api/selfie-wardrobe/select') {
      current=body.id;data={wardrobe:wardrobe(),daily_schedule:daily()};
    } else if(url.pathname==='/api/selfie-profile/ensure-today') {
      scheduleReady=true;data={wardrobe:wardrobe(),daily_schedule:daily()};
    } else if(url.pathname==='/api/selfie-reference') {
      data={has_image:false,wardrobe:wardrobe(),daily_schedule:scheduleReady?daily():missingDaily()};
    } else if(url.pathname==='/api/selfie-profile/refresh-local') {
      scheduleReady=true;data={has_image:false,wardrobe:wardrobe(),daily_schedule:daily()};
    }
    response.statusCode=error?400:200;response.setHeader('Content-Type','application/json');
    response.end(JSON.stringify(error?{success:false,error}:{success:true,data}));
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');
  let browser,cdp;
  try {
    const port=await freePort();
    browser=spawn(findChromium(),['--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--disable-site-isolation-trials',
      '--remote-allow-origins=*',`--remote-debugging-port=${port}`,`--user-data-dir=${profile}`,'about:blank'],{stdio:'ignore'});
    cdp=new CdpSession(await waitForCdp(port,15000),15000);
    await cdp.send('Page.enable');await cdp.send('Runtime.enable');
    let contextId;const exceptions=[];
    cdp.on('Runtime.executionContextCreated',({context})=>{if(context.auxData?.isDefault)contextId=context.id;});
    cdp.on('Runtime.exceptionThrown',event=>exceptions.push(event.exceptionDetails?.exception?.description||event.exceptionDetails?.text));
    const evaluate=async expression=>{
      const result=await cdp.send('Runtime.evaluate',{expression,contextId,awaitPromise:true,returnByValue:true,userGesture:true});
      if(result.exceptionDetails)throw new Error(result.exceptionDetails.exception?.description||'browser exception');
      return result.result?.value;
    };
    const until=async expression=>{
      for(let i=0;i<120;i++){if(await evaluate(expression))return;await new Promise(resolve=>setTimeout(resolve,30));}
      throw new Error('Timed out: '+expression+'; '+await evaluate('document.body.innerText.slice(-1500)'));
    };
    const click=async selector=>{
      const point=await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.scrollIntoView({block:'center'});const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
      const frame=await cdp.send('Runtime.evaluate',{expression:"(()=>{const r=document.querySelector('iframe')?.getBoundingClientRect();return r?{x:r.x,y:r.y}:{x:0,y:0};})()",returnByValue:true});
      point.x+=frame.result.value.x;point.y+=frame.result.value.y;
      await cdp.send('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});
      await cdp.send('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',clickCount:1});
    };
    const choose=names=>evaluate(`(()=>{const transfer=new DataTransfer();${JSON.stringify(names)}.forEach(name=>transfer.items.add(new File(['fixture '+name],name,{type:'image/png'})));$('wardrobeFile').files=transfer.files;$('wardrobeFile').dispatchEvent(new Event('change'));})()`);
    for(const embedded of [false,true])for(const width of [1280,390,320]) {
      items=[{id:'disabled',name:'已停用的服装',enabled:false,tags:[]}];selectionMode='random';current='';calls=[];scheduleReady=false;
      failFile=true;holdUpload=false;releaseUpload=null;contextId=undefined;
      const height=width===1280?900:width===320?568:844;
      await cdp.send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<500});
      await cdp.send('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/${embedded?'host':'fixture'}`});
      await new Promise(resolve=>setTimeout(resolve,350));
      await until("typeof navigateToTab === 'function'");
      const hostLayout=await cdp.send('Runtime.evaluate',{expression:'document.documentElement.scrollWidth <= innerWidth+1',returnByValue:true});
      assert.equal(hostLayout.result.value,true,'fixture host overflows horizontally');
      await evaluate("navigateToTab('selfie')");await until("document.querySelector('.wardrobe-wear') && $('selfieDailySchedule').textContent.includes('日间安排')");
      assert.equal(calls.filter(call=>call.path.endsWith('/ensure-today')).length,1,'missing schedule was not ensured exactly once');
      assert.equal(await evaluate("document.querySelector('.wardrobe-wear').disabled"),true);
      await evaluate("$('wardrobeSelectionMode').value='sequential';$('wardrobeSelectionMode').dispatchEvent(new Event('change'));");
      await until("!$('wardrobeSelectionMode').disabled");assert.equal(selectionMode,'sequential');
      await choose(['first.png','retry.png','third.png']);await click('#uploadWardrobe');
      await until("!WARDROBE_BATCH_BUSY && $('wardrobeBatchProgressLabel').textContent.includes('失败 1')");
      assert.deepEqual(calls.filter(call=>call.path.endsWith('/add')).map(call=>call.body.name),['first','retry','third']);
      assert.equal(await evaluate('WARDROBE_PENDING_UPLOADS.length'),1);
      assert.equal(await evaluate("$('wardrobeBatchResult').textContent.includes('retry.png：fixture upload error')"),true);
      failFile=false;await click('#uploadWardrobe');await until('!WARDROBE_BATCH_BUSY && !WARDROBE_PENDING_UPLOADS');
      assert.deepEqual(calls.filter(call=>call.path.endsWith('/add')).map(call=>call.body.name),['first','retry','third','retry']);
      await click('[data-wardrobe-id="wardrobe_2"] .wardrobe-wear');
      await until("$('wardrobeCurrent').textContent.includes('third')");
      assert.equal(current,'wardrobe_2');
      assert.equal(await evaluate("$('selfieDailySchedule').textContent.includes('third')"),true);
      await click('[onclick="refreshDailySelfieLocal()"]');await until("$('confirmModal').classList.contains('show')");await click('#confirmProceedBtn');
      await until("!$('confirmModal').classList.contains('show') && $('toastWrap').textContent.includes('今日日程已重新生成')");
      assert.equal(await evaluate("$('wardrobeSelectionMode').value"),'sequential');
      assert.equal(await evaluate("document.querySelectorAll('.wardrobe-item').length"),4);

      await choose(['stop-a.png','stop-b.png','stop-c.png']);holdUpload=true;
      await click('#uploadWardrobe');
      for(let i=0;i<100&&!releaseUpload;i++)await new Promise(resolve=>setTimeout(resolve,20));
      const batchState=await evaluate("JSON.stringify({busy:WARDROBE_BATCH_BUSY,disabled:$('uploadWardrobe')?.disabled,fileCount:$('wardrobeFile')?.files?.length,pending:WARDROBE_PENDING_UPLOADS?.length,progress:$('wardrobeBatchProgressLabel')?.textContent,tail:document.body.innerText.slice(-800)})");
      assert(releaseUpload,`upload never started: ${batchState}`);
      const beforeCancel=calls.filter(call=>call.path.endsWith('/add')).length;
      assert.equal(await evaluate("$('uploadWardrobe').disabled && $('wardrobeFile').disabled"),true);
      await click('#wardrobeBatchCancel');holdUpload=false;releaseUpload();
      await until('!WARDROBE_BATCH_BUSY');
      assert.equal(calls.filter(call=>call.path.endsWith('/add')).length,beforeCancel);
      assert.equal(await evaluate('WARDROBE_PENDING_UPLOADS.length'),2);
      await click('#uploadWardrobe');await until('!WARDROBE_BATCH_BUSY && !WARDROBE_PENDING_UPLOADS');
      assert.deepEqual(calls.filter(call=>call.path.endsWith('/add')).slice(-3).map(call=>call.body.name),['stop-a','stop-b','stop-c']);

      await evaluate("$('toastWrap').innerHTML='';$('wardrobeSelectionMode').scrollIntoView({block:'start'});");
      assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),true);
      const overflow=await evaluate(`Array.from(document.querySelectorAll('.wardrobe-item, .wardrobe-item button, .wardrobe-item input')).filter(el=>{const r=el.getBoundingClientRect();return r.left<0||r.right>innerWidth+1||el.scrollWidth>el.clientWidth+1;}).map(el=>el.outerHTML.slice(0,120))`);
      assert.deepEqual(overflow,[]);
      const shot=await cdp.send('Page.captureScreenshot',{format:'png'});
      fs.writeFileSync(path.join(output,`${embedded?'embedded':'standalone'}-${width}.png`),Buffer.from(shot.data,'base64'));
      console.log(`[wardrobe-smoke] ${embedded?'sandbox iframe':'standalone'} ${width}x${height}: batch import, retry, cancel, rotation, today selection, refresh and layout passed`);
    }
    assert.deepEqual(exceptions,[]);
    console.log(`[wardrobe-smoke] screenshots: ${output}`);
  } finally {
    if(releaseUpload)releaseUpload();
    if(cdp)cdp.close();await stopProcess(browser);
    await new Promise(resolve=>server.close(resolve));
    fs.rmSync(profile,{recursive:true,force:true,maxRetries:3,retryDelay:100});
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
