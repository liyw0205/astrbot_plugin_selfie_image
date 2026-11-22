#!/usr/bin/env node
'use strict';

// Render long channel/model names and task states with disposable HTTP fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {CdpSession, findChromium, freePort, waitForCdp, stopProcess} = require('./mobile_smoke');
const ROOT = path.resolve(__dirname, '..');
const output = process.env.DASHBOARD_CONTENT_SMOKE_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), 'selfie-content-smoke-'));
const channelName = 'production_image_channel_'.repeat(6);
const modelName = 'image-model-long-identifier-'.repeat(6);
const longError = 'UPSTREAM_TIMEOUT_WITH_TRACE_'.repeat(12);
const config = {image_channels: [
  {name:channelName, provider_type:'openai', enabled:true, base_url:'https://example.test', enabled_models:[modelName], models_cache:[modelName]},
  {name:'生图备用渠道名称'.repeat(16), provider_type:'openai', enabled:false, enabled_models:[], models_cache:[]},
], audit_channels:[]};
const task = {
  task_id:'web-content-fixture', status:'succeeded', generation_success:true,
  delivery_success:null, delivery_unknown:true, source:'web-test', used_model:channelName+'/'+modelName,
  generation_stage_label:'等待投递回执确认'.repeat(12), error:longError,
  original_prompt:'长提示词包含中文与标识符：'+modelName, final_prompt:modelName.repeat(3),
  record_ids:Array.from({length:5},(_,i)=>`record-${i}-0123456789abcdef0123456789abcdef`),
  parameters:{model:modelName}, attempts:[{attempt:1,model:modelName,success:false,error:longError}],
};
const sdk = `
  if(parent!==window) {
    let next=0;const pending=new Map();
    addEventListener('message',event=>{
      if(event.source!==parent)return;
      const item=pending.get(event.data.id);if(!item)return;
      pending.delete(event.data.id);
      event.data.error?item.reject(new Error(event.data.error)):item.resolve(event.data.result);
    });
    const request=(endpoint,body)=>new Promise((resolve,reject)=>{const id=++next;pending.set(id,{resolve,reject});parent.postMessage({id,endpoint,body},'*');});
    window.AstrBotPluginPage={ready:async()=>{},apiGet:(endpoint,body)=>request(endpoint,body)};
  }
`;
const page = fs.readFileSync(path.join(ROOT,'pages/dashboard/index.source.html'),'utf8')
  .replace('<body>','<body class="authed">')
  .replace(/<script src="\/api\/plugin\/page\/bridge-sdk.js" defer><\/script>/,`<script>${sdk}</script>`)
  .replace(/<!-- dashboard-source: ([\w.-]+) -->/g,(_,name)=>{
    const source=fs.readFileSync(path.join(ROOT,'pages/dashboard/source',name),'utf8');
    return name==='bootstrap.js'?source.split('    (async function init()')[0]:source;
  });
const host = `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
  <body style="margin:0"><iframe src="/fixture" sandbox="allow-scripts" style="display:block;width:100%;height:100vh;border:0"></iframe><script>
    addEventListener('message',async event=>{
      if(event.source!==document.querySelector('iframe').contentWindow||!event.data.endpoint)return;
      const {id,endpoint,body}=event.data;
      try {
        const response=await fetch('/api/'+endpoint+'?'+new URLSearchParams(body||{}));
        if(!response.ok)throw new Error('fixture request failed');
        event.source.postMessage({id,result:await response.json()},'*');
      }catch(error){event.source.postMessage({id,error:error.message},'*');}
    });
  </script></body></html>`;

async function main() {
  fs.mkdirSync(output,{recursive:true});
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'selfie-content-browser-'));
  const failures=[];
  const server=http.createServer((request,response)=>{
    const url=new URL(request.url,'http://fixture');
    if(url.pathname==='/fixture'||url.pathname==='/host') {
      response.setHeader('Content-Type','text/html; charset=utf-8');response.end(url.pathname==='/host'?host:page);return;
    }
    if(url.pathname==='/api/tasks/'+task.task_id) {
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:task}));return;
    }
    response.statusCode=404;response.end();
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
      for(let i=0;i<100;i++){if(await evaluate(expression))return;await new Promise(resolve=>setTimeout(resolve,30));}
      throw new Error('Timed out: '+expression);
    };
    const click=async selector=>{
      const point=await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.scrollIntoView({block:'center'});const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
      const frame=await cdp.send('Runtime.evaluate',{expression:"(()=>{const r=document.querySelector('iframe')?.getBoundingClientRect();return r?{x:r.x,y:r.y}:{x:0,y:0};})()",returnByValue:true});
      point.x+=frame.result.value.x;point.y+=frame.result.value.y;
      await cdp.send('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});
      await cdp.send('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',clickCount:1});
    };
    const screenshot=async name=>{
      const shot=await cdp.send('Page.captureScreenshot',{format:'png'});
      fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(shot.data,'base64'));
    };
    const check=async (label,expression)=>{
      const result=await evaluate(expression);
      if(result!==true)failures.push({label,result});
    };
    const modalFits=id=>`(()=>{const modal=$('${id}').querySelector('.modal'),body=modal.querySelector('.modal-body'),r=modal.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.top>=0&&r.bottom<=innerHeight+1&&body.scrollWidth<=body.clientWidth+1;})()`;
    for(const embedded of [false,true])for(const [width,height] of [[1280,900],[390,844],[320,568]]) {
      const label=`${embedded?'embedded':'standalone'}-${width}`;contextId=undefined;
      await cdp.send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<500});
      await cdp.send('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/${embedded?'host':'fixture'}`});
      await new Promise(resolve=>setTimeout(resolve,350));await until("typeof navigateToTab==='function'");
      if(embedded) {
        await check(label+' embedded login stays hidden',`document.documentElement.classList.contains('embedded-dashboard')&&getComputedStyle($('loginPage')).display==='none'`);
        await evaluate("document.documentElement.classList.add('embedded-dashboard-ready');");
      }
      await evaluate('CONFIG={};fillForms();');
      await evaluate("navigateToTab('image');");
      await check(label+' preview hiding defaults off', "!$('enablePreviewHidden').checked");
      await check(label+' preview hiding above daily limit', `(()=>{
        const preview=$('enablePreviewHidden').closest('label').getBoundingClientRect();
        const daily=$('enableDailyLimit').closest('label').getBoundingClientRect();
        return preview.bottom<=daily.top&&Math.abs(preview.left-daily.left)<1
          &&preview.right<=innerWidth+1&&daily.right<=innerWidth+1;
      })()`);
      await click('#enablePreviewHidden');
      await evaluate('collectForms();fillForms();');
      await check(label+' preview hiding round trip on', "CONFIG.image.enable_preview_hidden===true&&$('enablePreviewHidden').checked&&!CONFIG.image.enable_daily_limit");
      await click('#enablePreviewHidden');
      await evaluate('collectForms();fillForms();');
      await check(label+' preview hiding round trip off', "CONFIG.image.enable_preview_hidden===false&&!$('enablePreviewHidden').checked");
      await screenshot(label+'-image-settings');
      await check(label+' alert thresholds have defaults after config load',`$('alertRecordSuccessRate').value==='80'&&$('alertGenerationP95').value==='60'&&$('alertCacheOrphanGrowth').value==='10'`);
      await evaluate(`CONFIG=${JSON.stringify(config)};renderAllChannelLists();navigateToTab('channels');`);
      await screenshot(label+'-channels');
      await check(label+' channel titles',`[...document.querySelectorAll('.channel-row b')].every(el=>{const range=document.createRange();range.selectNodeContents(el);const box=el.parentElement.getBoundingClientRect();return [...range.getClientRects()].every(r=>r.left>=box.left-1&&r.right<=box.right+1);})`);
      await check(label+' add actions',`[...document.querySelectorAll('[data-channel-add]')].every(button=>{const b=button.getBoundingClientRect();return [...document.querySelectorAll('.channel-row b')].every(el=>{const range=document.createRange();range.selectNodeContents(el);return [...range.getClientRects()].every(r=>r.right<=b.left||r.left>=b.right||r.bottom<=b.top||r.top>=b.bottom);});})`);
      for(const kind of ['image','audit']) {
        await click(`[data-channel-add="${kind}"]`);
        await until("$('channelModal').classList.contains('show')");
        assert.equal(await evaluate('EDITING_CHANNEL_IS_NEW && EDITING_CHANNEL_KIND === '+JSON.stringify(kind)),true);
        await click('#channelModalCloseButton');
        assert.equal(await evaluate('CONFIG.image_channels.length'),2);
        assert.equal(await evaluate('CONFIG.audit_channels.length'),0);
      }
      await click('#channelTabImage');
      await click('#channelList [data-act="edit"]');await until("$('channelModal').classList.contains('show')");
      await evaluate("$('enabledModels').scrollIntoView({block:'center'});");
      await screenshot(label+'-models');
      await check(label+' channel modal',modalFits('channelModal'));
      await check(label+' model names',`[...document.querySelectorAll('#enabledModels .name, #cacheModels .name')].every(el=>el.scrollWidth<=el.clientWidth+1)`);
      assert.equal(await evaluate("$('modalChannelName').value"),channelName);
      assert.equal(await evaluate("$('enabledModels').querySelector('.name').textContent"),modelName);
      await evaluate(`setStatus('modalStatus',${JSON.stringify(longError)});`);
      await check(label+' channel status',modalFits('channelModal'));
      await check(label+' channel controls',`[...document.querySelectorAll('#channelModal .modal-footer button, #channelModal .between > button')].every(el=>{const r=el.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight&&r.width>=28&&r.height>=28;})`);
      await click('#channelModalCloseButton');await until("!$('channelModal').classList.contains('show')");
      await click('[data-channel-add="image"]');await until("$('channelModal').classList.contains('show')");
      await evaluate("$('modalProvider').value='novelai';$('modalProvider').dispatchEvent(new Event('change'));");
      assert.equal(await evaluate("$('manualFillNai').style.display !== 'none'"),true,'NovelAI fill button should be visible');
      await click('#manualFillNai');await click('#manualFillNai');
      assert.deepEqual(await evaluate("Array.from(document.querySelectorAll('#enabledModels .name')).map(el=>el.textContent)"),
        ['nai-diffusion-4-5-full','nai-diffusion-5-full'],'filling twice should add each NovelAI model once');
      await evaluate("$('modalProvider').value='openai';$('modalProvider').dispatchEvent(new Event('change'));");
      assert.equal(await evaluate("$('manualFillNai').style.display === 'none'"),true,'non-NovelAI fill button should be hidden');
      await click('#channelModalCloseButton');await until("!$('channelModal').classList.contains('show')");
      await evaluate(`openTaskDetail(${JSON.stringify(task.task_id)})`);
      await until("$('taskDetailBody').textContent.includes('UPSTREAM_TIMEOUT')");
      await screenshot(label+'-task');
      await check(label+' task modal',modalFits('taskModal'));
      await evaluate("$('taskDetailBody').querySelector('.task-record-open').scrollIntoView({block:'center'});");
      await screenshot(label+'-task-records');
      await check(label+' linked records',`[...document.querySelectorAll('.task-record-open')].every(el=>{const r=el.getBoundingClientRect(),body=$('taskModal').querySelector('.modal-body').getBoundingClientRect();return r.left>=body.left&&r.right<=body.right+1&&el.scrollWidth<=el.clientWidth+1;})`);
      await check(label+' attempt cells',`[...document.querySelectorAll('#taskDetailBody table td')].every(el=>el.scrollWidth<=el.clientWidth+1)&&document.querySelector('#taskDetailBody table td:nth-child(2)').getBoundingClientRect().width>=100`);
      await click('#taskModal button[onclick="closeTaskDetail()"]');await until("!$('taskModal').classList.contains('show')");
      if(embedded) {
        await evaluate("document.documentElement.classList.remove('embedded-dashboard-ready');void initializeEmbeddedDashboard();");
        await until("$('embeddedBootRetry') && !$('embeddedBootRetry').hidden");
        assert.equal(await evaluate("getComputedStyle($('loginPage')).display === 'none'"),true,'embedded errors must not show token login');
        assert.equal(await evaluate("getComputedStyle($('embeddedBootPage')).display !== 'none'"),true,'embedded errors should show connection retry');
      }
      await check(label+' page width','document.documentElement.scrollWidth<=innerWidth+1');
      console.log(`[content-smoke] ${label} ${width}x${height}: checked channels, model editor, status and task details`);
    }
    assert.deepEqual(exceptions,[]);
    assert.deepEqual(failures,[]);
  } finally {
    console.log(`[content-smoke] screenshots: ${output}`);
    if(cdp)cdp.close();await stopProcess(browser);
    await new Promise(resolve=>server.close(resolve));
    fs.rmSync(profile,{recursive:true,force:true,maxRetries:3,retryDelay:100});
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
