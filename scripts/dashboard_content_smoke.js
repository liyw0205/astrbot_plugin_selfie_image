#!/usr/bin/env node
'use strict';

// Render dashboard settings and long content with disposable HTTP fixtures.
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
    const request=(method,endpoint,body)=>new Promise((resolve,reject)=>{const id=++next;pending.set(id,{resolve,reject});parent.postMessage({id,method,endpoint,body},'*');});
    window.AstrBotPluginPage={ready:async()=>{},apiGet:(endpoint,body)=>request('GET',endpoint,body),apiPost:(endpoint,body)=>request('POST',endpoint,body)};
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
      const {id,method,endpoint,body}=event.data;
      try {
        const url='/api/'+endpoint;
        const response=method==='POST'
          ? await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
          : await fetch(url+'?'+new URLSearchParams(body||{}));
        if(!response.ok)throw new Error('fixture request failed');
        event.source.postMessage({id,result:await response.json()},'*');
      }catch(error){event.source.postMessage({id,error:error.message},'*');}
    });
  </script></body></html>`;

async function main() {
  fs.mkdirSync(output,{recursive:true});
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'selfie-content-browser-'));
  const failures=[];
  const configWrites=[];
  const recordCalls=[];
  let recordRows=[];
  const recordFields={original_prompt:'Original fixture '.repeat(1200),request_prompt:'Request fixture',final_prompt:'Final fixture',request_prompt_en:'',request_data:{model:modelName},response_data:{result:'ok'}};
  const recordFixture=id=>({id,image_code:id,time:'2026-11-26 12:00:00',source:'web-test',success:true,model:modelName,request_data:{model:modelName},generated_image_paths:[]});
  const server=http.createServer(async (request,response)=>{
    const url=new URL(request.url,'http://fixture');
    if(url.pathname==='/fixture'||url.pathname==='/host') {
      response.setHeader('Content-Type','text/html; charset=utf-8');response.end(url.pathname==='/host'?host:page);return;
    }
    if(url.pathname==='/api/tasks/'+task.task_id) {
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:task}));return;
    }
    if(url.pathname==='/api/tasks') {
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:{tasks:[],summary:{}}}));return;
    }
    if(url.pathname==='/api/records') {
      recordCalls.push({path:url.pathname,known:url.searchParams.has('known_records')});
      const known=JSON.parse(url.searchParams.get('known_records')||'{}');
      const offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||30);
      const rows=recordRows.slice(offset,offset+limit);
      response.setHeader('Content-Type','application/json');
      response.end(JSON.stringify({success:true,data:rows.filter(row=>known[row.id]!=='v1'),total:recordRows.length,filtered:recordRows.length,offset,limit,
        page_ids:rows.map(row=>row.id),record_revisions:Object.fromEntries(rows.map(row=>[row.id,'v1']))}));return;
    }
    if(url.pathname.startsWith('/api/records/')) {
      const field=url.searchParams.get('field');
      recordCalls.push({path:url.pathname,field,view:url.searchParams.get('view')});
      response.setHeader('Content-Type','application/json');
      response.end(JSON.stringify({success:true,data:field?{[field]:recordFields[field]}:recordFixture(url.pathname.split('/').at(-1))}));return;
    }
    if(url.pathname==='/api/config'&&request.method==='POST') {
      try {
        const chunks=[];
        for await(const chunk of request)chunks.push(chunk);
        const {config:patch}=JSON.parse(Buffer.concat(chunks));
        configWrites.push(patch);
        response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:patch}));return;
      } catch {
        response.writeHead(400).end();return;
      }
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
    const filtersFit=(id,columns)=>`(()=>{
      const grid=$('${id}').closest('.dashboard-filter-grid');
      if(getComputedStyle(grid).gridTemplateColumns.trim().split(/\\s+/).length!==${columns})return false;
      return [...grid.querySelectorAll('input, select')].every(control=>{
        const field=control.parentElement.getBoundingClientRect(),box=control.getBoundingClientRect();
        return box.width>0&&box.left>=field.left&&box.right<=field.right+1&&field.right<=innerWidth+1;
      });
    })()`;
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
      await evaluate("navigateToTab('monitor');");
      const filterColumns=width<=360?1:width<=760?2:width<=1100?3:4;
      await check(label+' monitor filters fit side by side',filtersFit('monitorMedia',filterColumns));
      await check(label+' task filters fit side by side',filtersFit('taskQueueScope',filterColumns));
      await evaluate("$('monitorMedia').scrollIntoView({block:'start'});");
      await screenshot(label+'-monitor-filters');
      await evaluate("$('taskQueueScope').scrollIntoView({block:'center'});");
      await screenshot(label+'-task-filters');
      await evaluate("navigateToTab('assets');");
      await check(label+' asset filters fit side by side',filtersFit('assetModel',filterColumns));
      await evaluate("$('assetKeyword').scrollIntoView({block:'start'});");
      await screenshot(label+'-asset-filters');
      await evaluate("navigateToTab('monitor');");
      await click('#openMetricAlertSettings');await until("$('metricAlertSettingsModal').classList.contains('show')");
      await check(label+' alert settings modal fits',modalFits('metricAlertSettingsModal'));
      await check(label+' threshold save button stays visible',`(()=>{const r=$('saveMetricAlertSettings').getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;})()`);
      if(width<=390) {
        await check(label+' thresholds can scroll on mobile',`(()=>{const body=$('metricAlertSettingsModal').querySelector('.modal-body');body.scrollTop=body.scrollHeight;return body.scrollTop>0;})()`);
        await evaluate("$('metricAlertSettingsModal').querySelector('.modal-body').scrollTop=0;");
      }
      await screenshot(label+'-alert-settings');
      await cdp.send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});
      await cdp.send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape'});
      await until("!$('metricAlertSettingsModal').classList.contains('show')");
      await check(label+' dialog close restores launcher focus',"document.activeElement===$('openMetricAlertSettings')");
      await click('#openMetricAlertSettings');
      await evaluate("$('alertRecordSuccessRate').value='75';$('alertRecordSuccessRate').dispatchEvent(new Event('input'));");
      const writesBeforeSave=configWrites.length;
      await click('#saveMetricAlertSettings');
      await until("!$('metricAlertSettingsModal').classList.contains('show')");
      assert.equal(configWrites.length,writesBeforeSave+1);
      assert.deepEqual(Object.keys(configWrites.at(-1)),['monitoring']);
      assert.equal(configWrites.at(-1).monitoring.generation_alerts.record_success_rate_below,0.75);
      await evaluate(`CONFIG=${JSON.stringify(config)};renderAllChannelLists();navigateToTab('channels');`);
      await screenshot(label+'-channels');
      await evaluate("switchChannelPane('image');");
      await check(label+' channel add actions follow selected pane',"getComputedStyle(document.querySelector('[data-channel-add=\"image\"]')).display!=='none'&&getComputedStyle(document.querySelector('[data-channel-add=\"audit\"]')).display==='none'");
      await check(label+' channel titles',`[...document.querySelectorAll('.channel-row b')].every(el=>{const range=document.createRange();range.selectNodeContents(el);const box=el.parentElement.getBoundingClientRect();return [...range.getClientRects()].every(r=>r.left>=box.left-1&&r.right<=box.right+1);})`);
      await check(label+' add actions',`[...document.querySelectorAll('[data-channel-add]')].every(button=>{const b=button.getBoundingClientRect();return [...document.querySelectorAll('.channel-row b')].every(el=>{const range=document.createRange();range.selectNodeContents(el);return [...range.getClientRects()].every(r=>r.right<=b.left||r.left>=b.right||r.bottom<=b.top||r.top>=b.bottom);});})`);
      for(const kind of ['image','audit']) {
        await evaluate(`switchChannelPane(${JSON.stringify(kind)});`);
        assert.equal(await evaluate(
          `getComputedStyle(document.querySelector('[data-channel-add="${kind}"]')).display!=='none'&&document.querySelector('[data-channel-add="${kind==='image'?'audit':'image'}"]').hidden`),
        true,`${kind} add action visibility`);
        await evaluate(`document.querySelector('[data-channel-add="${kind}"]').click();`);
        await until("$('channelModal').classList.contains('show')");
        assert.equal(await evaluate(`JSON.stringify({isNew:EDITING_CHANNEL_IS_NEW,kind:EDITING_CHANNEL_KIND,modal:$('channelModal').classList.contains('show')})`),
          JSON.stringify({isNew:true,kind,modal:true}), `channel editor did not open as new ${kind} channel`);
        await evaluate("document.querySelector('#channelModalCloseButton').click();");
        assert.equal(await evaluate('CONFIG.image_channels.length'),2);
        assert.equal(await evaluate('CONFIG.audit_channels.length'),0);
      }
      await evaluate("document.querySelector('#channelTabImage').click();document.querySelector('#channelList [data-act=\"edit\"]').click();");
      await until("$('channelModal').classList.contains('show')");
      await evaluate("$('enabledModels').scrollIntoView({block:'center'});");
      await screenshot(label+'-models');
      await check(label+' channel modal',modalFits('channelModal'));
      await check(label+' model names',`[...document.querySelectorAll('#enabledModels .name, #cacheModels .name')].every(el=>el.scrollWidth<=el.clientWidth+1)`);
      assert.equal(await evaluate("$('modalChannelName').value"),channelName);
      assert.equal(await evaluate("$('enabledModels').querySelector('.name').textContent"),modelName);
      await evaluate(`setStatus('modalStatus',${JSON.stringify(longError)});`);
      await check(label+' channel status',modalFits('channelModal'));
      await check(label+' channel controls',`[...document.querySelectorAll('#channelModal .modal-footer button, #channelModal .between > button')].every(el=>{const r=el.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight&&r.width>=28&&r.height>=28;})`);
      await evaluate("document.querySelector('#channelModalCloseButton').click();");await until("!$('channelModal').classList.contains('show')");
      await evaluate("document.querySelector('[data-channel-add=\"image\"]').click();");await until("$('channelModal').classList.contains('show')");
      await evaluate("$('modalProvider').value='novelai';$('modalProvider').dispatchEvent(new Event('change'));");
      assert.equal(await evaluate("$('manualFillNai').style.display !== 'none'"),true,'NovelAI fill button should be visible');
      await click('#manualFillNai');await click('#manualFillNai');
      assert.deepEqual(await evaluate("Array.from(document.querySelectorAll('#enabledModels .name')).map(el=>el.textContent)"),
        ['nai-diffusion-4-5-full','nai-diffusion-5-full'],'filling twice should add each NovelAI model once');
      await evaluate("$('modalProvider').value='openai';$('modalProvider').dispatchEvent(new Event('change'));");
      assert.equal(await evaluate("$('manualFillNai').style.display === 'none'"),true,'non-NovelAI fill button should be hidden');
      await evaluate("document.querySelector('#channelModalCloseButton').click();");await until("!$('channelModal').classList.contains('show')");
      await evaluate(`openTaskDetail(${JSON.stringify(task.task_id)})`);
      await until("$('taskDetailBody').textContent.includes('UPSTREAM_TIMEOUT')");
      await screenshot(label+'-task');
      await check(label+' task modal',modalFits('taskModal'));
      await evaluate("$('taskDetailBody').querySelector('.task-record-open').scrollIntoView({block:'center'});");
      await screenshot(label+'-task-records');
      await check(label+' linked records',`[...document.querySelectorAll('.task-record-open')].every(el=>{const r=el.getBoundingClientRect(),body=$('taskModal').querySelector('.modal-body').getBoundingClientRect();return r.left>=body.left&&r.right<=body.right+1&&el.scrollWidth<=el.clientWidth+1;})`);
      await check(label+' attempt cells',`[...document.querySelectorAll('#taskDetailBody table td')].every(el=>el.scrollWidth<=el.clientWidth+1)&&document.querySelector('#taskDetailBody table td:nth-child(2)').getBoundingClientRect().width>=100`);
      await evaluate("document.querySelector('#taskModal button[onclick=\"closeTaskDetail()\"]').click();");await until("!$('taskModal').classList.contains('show')");
      recordRows=[recordFixture('record-old')];
      await evaluate("(async()=>{clearTimeout(TASK_LOAD_TIMER);clearRecordPageCache();await loadRecords(false);})()");
      recordRows.unshift(recordFixture('record-new'));
      await evaluate("navigateToTab('image');navigateToTab('monitor');");
      await until("RECORDS[0]?.id==='record-new'");
      assert(recordCalls.some(item=>item.known),'re-entering monitor should check the cached page incrementally');
      recordRows.unshift(recordFixture('record-task-result'));
      await evaluate("loadTasks(false)");
      await until("RECORDS[0]?.id==='record-task-result'");
      await check(label+' unchanged background refresh keeps the table',"(async()=>{const head=$('recordTable').firstElementChild;await loadRecords(false,{background:true});return head===$('recordTable').firstElementChild;})()");
      const callsBeforeDetail=recordCalls.length;
      await evaluate("openRecordDetail('record-new')");
      await check(label+' lazy detail has buttons',"$('recordDetailBody').querySelectorAll('.record-field-actions button').length===6&&!$('recordDetailBody').querySelector('pre')");
      assert(recordCalls.slice(callsBeforeDetail).some(item=>item.view==='summary'));
      assert(!recordCalls.slice(callsBeforeDetail).some(item=>item.field||item.path.endsWith('/media-sources')));
      await screenshot(label+'-record-details');
      await check(label+' record detail fits',modalFits('recordModal'));
      await evaluate("openRecordField('original_prompt')");
      await check(label+' lazy prompt loaded',"$('recordFieldModal').classList.contains('show')&&$('recordFieldBody').textContent.startsWith('Original fixture')");
      await screenshot(label+'-record-field');
      await check(label+' record field fits',modalFits('recordFieldModal'));
      const fieldCalls=recordCalls.filter(item=>item.field==='original_prompt').length;
      await evaluate("(async()=>{closeRecordField();await openRecordField('original_prompt');})()");
      assert.equal(recordCalls.filter(item=>item.field==='original_prompt').length,fieldCalls,'opening the same field should use cache');
      await cdp.send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});
      await cdp.send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape'});
      await check(label+' Escape closes the field only',"!$('recordFieldModal').classList.contains('show')&&$('recordModal').classList.contains('show')");
      await evaluate("openRecordField('response_data')");
      await check(label+' response loads on demand',"$('recordFieldBody').textContent.includes('ok')");
      await evaluate("closeRecordDetail();clearTimeout(TASK_LOAD_TIMER);");
      if(embedded) {
        await evaluate("document.documentElement.classList.remove('embedded-dashboard-ready');void initializeEmbeddedDashboard();");
        await until("$('embeddedBootRetry') && !$('embeddedBootRetry').hidden");
        assert.equal(await evaluate("getComputedStyle($('loginPage')).display === 'none'"),true,'embedded errors must not show token login');
        assert.equal(await evaluate("getComputedStyle($('embeddedBootPage')).display !== 'none'"),true,'embedded errors should show connection retry');
      }
      await check(label+' page width','document.documentElement.scrollWidth<=innerWidth+1');
      console.log(`[content-smoke] ${label} ${width}x${height}: checked filters, channels, tasks, incremental records and lazy detail dialogs`);
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
