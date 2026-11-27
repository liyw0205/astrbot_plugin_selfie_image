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
  {id:'channel-main', name:channelName, provider_type:'openai', enabled:true, base_url:'https://example.test', enabled_models:[modelName], models_cache:[modelName,'cached-model']},
  {name:'生图备用渠道名称'.repeat(16), provider_type:'openai', enabled:false, enabled_models:[], models_cache:[]},
], audit_channels:[], proxies:[{id:'download-proxy',name:'Result proxy',protocol:'http',host:'proxy.test',port:8080,enabled:true}]};
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
  const assetCalls=[];
  const mediaCalls=[];
  const catalogCalls=[];
  const identityCalls=[];
  const nodeWrites=[];
  let fixtureCanvas=null;
  const testWrites=[];
  const groupWrites=[];
  let groupPermission={},groupRevision=30;
  let failConfigWrite=false;
  let recordRows=[];
  const recordFields={original_prompt:'Original fixture '.repeat(1200),request_prompt:'Request fixture',final_prompt:'Final fixture',request_prompt_en:'',request_data:{model:modelName},response_data:{result:'ok'}};
  const recordFixture=(id,overrides={})=>({id,image_code:id,time:'2026-11-26 12:00:00',source:'web-test',success:true,elapsed_seconds:12.5,model:modelName,request_data:{model:modelName},generated_image_paths:[],...overrides});
  const assetRows=['asset-one','asset-two'].map(id=>({...recordFixture(id),used_model:modelName,original_prompt:'Asset prompt',generated_image_paths:[id+'.png']}));
  const previewData=fs.readFileSync(path.join(ROOT,'logo.png')).toString('base64');
  let galleryPreviews={};
  const server=http.createServer(async (request,response)=>{
    const url=new URL(request.url,'http://fixture');
    if(url.pathname==='/fixture'||url.pathname==='/host') {
      response.setHeader('Content-Type','text/html; charset=utf-8');response.end(url.pathname==='/host'?host:page);return;
    }
    if(url.pathname==='/logo.png') {
      response.setHeader('Content-Type','image/png');response.end(fs.readFileSync(path.join(ROOT,'logo.png')));return;
    }
    if(url.pathname==='/api/qq-groups') {
      response.setHeader('Content-Type','application/json');
      response.end(JSON.stringify({success:true,data:{groups:[{group_id:'10000',group_name:'群聊名单验证',config:{}}],available:true,models:[],bot_ids:['99999'],permission:groupPermission,revision:groupRevision}}));return;
    }
    if(url.pathname==='/api/qq-groups/config'&&request.method==='POST') {
      const chunks=[];for await(const chunk of request)chunks.push(chunk);
      const payload=JSON.parse(Buffer.concat(chunks));
      groupWrites.push(payload);
      if(payload.revision!==groupRevision) {
        response.writeHead(409,{'Content-Type':'application/json'}).end(JSON.stringify({success:false,message:'stale group revision'}));return;
      }
      groupPermission={...groupPermission,...payload.permission};groupRevision++;
      response.setHeader('Content-Type','application/json');
      response.end(JSON.stringify({success:true,data:{group_chat_configs:Object.fromEntries(payload.groups.map(row=>[row.group_id,row.config])),permission:groupPermission,revision:groupRevision}}));return;
    }
    if(url.pathname==='/api/tasks/'+task.task_id||url.pathname==='/api/test-image-channel/tasks/'+task.task_id) {
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:task}));return;
    }
    if(url.pathname==='/api/tasks') {
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:{tasks:[],summary:{total_active:3,queued:1,running:2}}}));return;
    }
    if(url.pathname==='/api/studio/gallery') {
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:{items:[
        {path:'gallery-original.png',record_id:'gallery-record',prompt:'方图'},
        {path:'gallery-portrait.png',record_id:'gallery-portrait',prompt:'9:16 竖图'},
        {path:'gallery-landscape.png',record_id:'gallery-landscape',prompt:'16:9 横图'},
      ]}}));return;
    }
    if(['/api/prompt-presets','/api/prompt-presets/manage','/api/cos-pools'].includes(url.pathname)) {
      catalogCalls.push(url.pathname+url.search);
      const cos=url.pathname==='/api/cos-pools',query=(url.searchParams.get('search')||'').toLowerCase();
      const rows=Array.from({length:36},(_,index)=>({id:`fixture-${index}`,name:`Preset ${String(index).padStart(2,'0')}`,title:`COS ${String(index).padStart(2,'0')}`,prompt:`${cos?'COS':'Preset'} prompt ${index}`}))
        .filter(item=>!query||`${item.name} ${item.title} ${item.prompt}`.toLowerCase().includes(query));
      const offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||30);
      await new Promise(resolve=>setTimeout(resolve,60));
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:{items:rows.slice(offset,offset+limit),total:rows.length,revision:1}}));return;
    }
    if(url.pathname==='/api/selfie-identity-thumbnails'||url.pathname==='/api/selfie-reference') {
      identityCalls.push(url.pathname+url.search);
      const data=url.pathname.endsWith('thumbnails')?{items:[{slot:1,data_url:'data:image/png;base64,'+previewData}]}:{has_image:true,image:'data:image/png;base64,'+previewData};
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data}));return;
    }
    if(url.pathname==='/api/proxies/test'||url.pathname==='/api/proxies/quality-check') {
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:{success:true,http_status:200,latency_ms:45,ip_address:'192.0.2.10',quality_grade:'A',quality_score:95,ok_count:1,total:1,results:[{label:'Fixture',http_status:200,reachable:true,latency_ms:45}]}}));return;
    }
    if(url.pathname==='/api/selfie-identity'&&request.method==='POST') {
      const chunks=[];for await(const chunk of request)chunks.push(chunk);
      const payload=JSON.parse(Buffer.concat(chunks));
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:{identity_mode:payload.identity_mode,image_pool:[],persona_pool:[],pool_limit:3}}));return;
    }
    if(url.pathname==='/api/studio/sessions/canvas-fixture/canvas'&&request.method==='POST') {
      const chunks=[];for await(const chunk of request)chunks.push(chunk);
      const payload=JSON.parse(Buffer.concat(chunks));nodeWrites.push(payload);
      for(const update of payload.nodes||[]) {
        const node=fixtureCanvas.nodes.find(node=>node.id===update.id);
        if(!node)continue;
        if(update.image||update.image_path)node.result={media_path:update.image_path||'node-upload.png',thumbnail_path:update.image_path||'node-upload.png'};
        if(update.params)node.params=update.params;
        if(update.extra_refs)node.extra_refs=update.extra_refs.map(item=>item.image?{id:item.id,image_path:'extra-upload.png',source:'upload'}:item);
      }
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:fixtureCanvas}));return;
    }
    if(url.pathname==='/api/assets') {
      assetCalls.push(url.pathname);
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:assetRows,total:assetRows.length,filtered:assetRows.length}));return;
    }
    if(url.pathname==='/api/assets/tags'||url.pathname==='/api/assets/collections') {
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:[]}));return;
    }
    if(url.pathname==='/api/cache-image-thumbnail'||url.pathname==='/api/cache-image-preview') {
      assetCalls.push(url.pathname);
      mediaCalls.push({endpoint:url.pathname,path:url.searchParams.get('path')});
      const imagePath=url.searchParams.get('path')||'';
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:imagePath.startsWith('missing-')
        ? {available:false,message:'图片已清理'}
        : {available:true,mime_type:'image/png',data_url:galleryPreviews[imagePath]||'data:image/png;base64,'+previewData}}));return;
    }
    if(url.pathname==='/api/test-image-channel/tasks') {
      const chunks=[];for await(const chunk of request)chunks.push(chunk);
      testWrites.push(JSON.parse(Buffer.concat(chunks)));
      response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:true,data:{task_id:task.task_id}}));return;
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
      response.end(JSON.stringify({success:true,data:field?{[field]:recordFields[field]}:recordFixture(url.pathname.split('/').at(-1),{generated_image_paths:['record-output.png'],request_image_paths:['record-input.png']})}));return;
    }
    if(url.pathname==='/api/config'&&request.method==='POST') {
      try {
        const chunks=[];
        for await(const chunk of request)chunks.push(chunk);
        const {config:patch}=JSON.parse(Buffer.concat(chunks));
        if(failConfigWrite) {
          response.writeHead(500,{'Content-Type':'application/json'}).end(JSON.stringify({success:false,message:'fixture save failure'}));return;
        }
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
      const shot=await cdp.send('Page.captureScreenshot',{format:'png'});
      fs.writeFileSync(path.join(output,'timeout.png'),Buffer.from(shot.data,'base64'));
      throw new Error('Timed out: '+expression);
    };
    const click=async (selector,block='center')=>{
      await evaluate("document.querySelector('.toast-wrap')?.replaceChildren();");
      const point=await evaluate(`(async()=>{const el=document.querySelector(${JSON.stringify(selector)});el.scrollIntoView({block:${JSON.stringify(block)}});await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));const r=el.getBoundingClientRect();const x=r.x+r.width/2,y=r.y+r.height/2;const hit=document.elementFromPoint(x,y);if(!hit||!(el.contains(hit)||hit.contains(el)))throw new Error('Click target obstructed: '+${JSON.stringify(selector)}+' by '+hit?.outerHTML.slice(0,300));return {x,y};})()`);
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
    const galleryFits=id=>`(()=>{
      const grid=$('${id}').querySelector('.studio-gallery-grid'),cells=[...grid.children],images=cells.map(cell=>cell.querySelector('img'));
      return getComputedStyle(grid).gridTemplateColumns.trim().split(/\\s+/).length===3&&cells.length===3
        &&cells.every(cell=>Math.abs(cell.getBoundingClientRect().top-cells[0].getBoundingClientRect().top)<1)
        &&images.every(image=>image.naturalWidth>0&&getComputedStyle(image).objectFit==='cover'
          &&Math.abs(image.parentElement.getBoundingClientRect().width/image.parentElement.getBoundingClientRect().height-.75)<.02)
        &&images[1].naturalWidth/images[1].naturalHeight===9/16&&images[2].naturalWidth/images[2].naturalHeight===16/9
        &&grid.scrollWidth<=grid.clientWidth+1;
    })()`;
    const filtersFit=id=>`(()=>{
      const grid=$('${id}').closest('.dashboard-filter-grid');
      if(getComputedStyle(grid).gridTemplateColumns.trim().split(/\\s+/).length!==2)return false;
      return [...grid.querySelectorAll('input, select')].every(control=>{
        const field=control.parentElement.getBoundingClientRect(),box=control.getBoundingClientRect();
        const full=control.parentElement.classList.contains('filter-full');
        if(full&&Math.abs(field.width-grid.getBoundingClientRect().width)>1)return false;
        if(control.tagName==='INPUT'&&!full)return false;
        if(!full&&field.width>grid.getBoundingClientRect().width/2)return false;
        return box.width>0&&box.left>=field.left&&box.right<=field.right+1&&field.right<=innerWidth+1;
      });
    })()`;
    for(const embedded of [false,true])for(const [width,height] of [[1280,900],[390,844],[320,568]]) {
      const label=`${embedded?'embedded':'standalone'}-${width}`;contextId=undefined;
      await cdp.send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<500});
      await cdp.send('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/${embedded?'host':'fixture'}`});
      await new Promise(resolve=>setTimeout(resolve,350));await until("typeof navigateToTab==='function'");
      if(!galleryPreviews['gallery-portrait.png']) galleryPreviews=await evaluate(`(async()=>{
        const image=new Image();image.src='data:image/png;base64,${previewData}';await image.decode();
        const resize=(width,height)=>{const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;canvas.getContext('2d').drawImage(image,0,0,width,height);return canvas.toDataURL('image/png');};
        return {'gallery-portrait.png':resize(90,160),'gallery-landscape.png':resize(160,90)};
      })()`);
      if(embedded) {
        await check(label+' embedded login stays hidden',`document.documentElement.classList.contains('embedded-dashboard')&&getComputedStyle($('loginPage')).display==='none'`);
        await evaluate("document.documentElement.classList.add('embedded-dashboard-ready');");
      }
      await evaluate('CONFIG={};fillForms();');
      await evaluate("navigateToTab('image');");
      await evaluate('window.scrollTo(0,0);');
      await until("$('sidebarLogoMenu').hidden");
      if(width<=1180) {
        await check(label+' original menu is visible at the top',"$('bentoMobileMenu').getBoundingClientRect().top>=0&&getComputedStyle($('bentoMobileMenu')).display!=='none'");
        await evaluate("window.scrollTo(0,$('bentoMobileMenu').getBoundingClientRect().bottom-1);");
        await check(label+' partial original menu keeps logo hidden',"$('bentoMobileMenu').getBoundingClientRect().bottom>0&&$('sidebarLogoMenu').hidden");
        await evaluate('window.scrollTo(0,400);');
        await until("!$('sidebarLogoMenu').hidden");
        await check(label+' floating menu uses three bars',"!$('sidebarLogoMenu').querySelector('img')&&$('sidebarLogoMenu').querySelector('svg path').getAttribute('d')==='M4 6h16M4 12h16M4 18h16'");
        await check(label+' floating menu stays at the top',`(()=>{const r=$('sidebarLogoMenu').getBoundingClientRect();return r.width===48&&r.height===48&&r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.top<=24&&r.bottom<=innerHeight&&getComputedStyle($('sidebarLogoMenu')).position==='fixed';})()`);
        const logoTop=await evaluate("$('sidebarLogoMenu').getBoundingClientRect().top");
        await evaluate('window.scrollTo(0,650);');
        assert.equal(await evaluate("$('sidebarLogoMenu').getBoundingClientRect().top"),logoTop,'logo should stay fixed while scrolling');
        await screenshot(label+'-floating-sidebar-logo');
        const menuScroll=await evaluate('window.scrollY');
        await click('#sidebarLogoMenu');
        await until("$('dashboardSidebar').getBoundingClientRect().left>=0");
        await check(label+' logo opens drawer and updates both controls',"document.body.classList.contains('sidebar-open')&&$('sidebarBackdrop').getAttribute('aria-hidden')==='false'&&$('sidebarLogoMenu').hidden&&[$('bentoMobileMenu'),$('sidebarLogoMenu')].every(button=>button.getAttribute('aria-expanded')==='true')");
        await screenshot(label+'-sidebar-from-logo');
        await cdp.send('Input.dispatchMouseEvent',{type:'mousePressed',x:width-8,y:height/2,button:'left',clickCount:1});
        await cdp.send('Input.dispatchMouseEvent',{type:'mouseReleased',x:width-8,y:height/2,button:'left',clickCount:1});
        await until("!document.body.classList.contains('sidebar-open')&&!$('sidebarLogoMenu').hidden&&$('dashboardSidebar').getBoundingClientRect().right<=0");
        await check(label+' backdrop restores floating entry and scroll position',`document.activeElement===$('sidebarLogoMenu')&&window.scrollY===${menuScroll}&&$('sidebarBackdrop').getAttribute('aria-hidden')==='true'`);
        await click('#sidebarLogoMenu');
        await until("$('dashboardSidebar').getBoundingClientRect().left>=0");
        await click('#pageNav [data-tab="base"]');
        await check(label+' navigation closes drawer',"!document.body.classList.contains('sidebar-open')&&$('bentoMobileMenu').getAttribute('aria-expanded')==='false'");
        await evaluate("navigateToTab('image');window.scrollTo(0,400);");
        await until("!$('sidebarLogoMenu').hidden");
        await evaluate("document.body.classList.remove('authed');");
        await until("$('sidebarLogoMenu').hidden");
        await check(label+' logo hidden on login page',"getComputedStyle($('sidebarLogoMenu')).display==='none'");
        await evaluate("document.body.classList.add('authed');window.scrollTo(0,400);");
        await until("!$('sidebarLogoMenu').hidden");
        await evaluate('window.scrollTo(0,0);');
        await until("$('sidebarLogoMenu').hidden");
        await click('#bentoMobileMenu');
        await until("$('dashboardSidebar').getBoundingClientRect().left>=0");
        await cdp.send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});
        await cdp.send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape'});
        await until("$('dashboardSidebar').getBoundingClientRect().right<=0");
        await check(label+' original entry still opens and Escape closes drawer',"!document.body.classList.contains('sidebar-open')&&document.activeElement===$('bentoMobileMenu')&&$('sidebarLogoMenu').hidden");
      } else {
        await evaluate('window.scrollTo(0,400);');
        await check(label+' desktop never shows floating entry',"$('sidebarLogoMenu').hidden&&getComputedStyle($('sidebarLogoMenu')).display==='none'");
        await cdp.send('Emulation.setDeviceMetricsOverride',{width:1000,height:480,deviceScaleFactor:1,mobile:false});
        await evaluate('window.scrollTo(0,400);');
        await until("!$('sidebarLogoMenu').hidden");
        await click('#sidebarLogoMenu');
        await check(label+' narrow desktop opens drawer',"document.body.classList.contains('sidebar-open')");
        await cdp.send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
        await until("!document.body.classList.contains('sidebar-open')&&$('sidebarLogoMenu').hidden");
        await evaluate('window.scrollTo(0,0);');
      }
      if(width<=1180) {
        await click('#bentoMobileMenu');
        await until("$('dashboardSidebar').getBoundingClientRect().left>=0");
      }
      await until("!document.querySelector('#pageNav .nav-switching')");
      await check(label+' selected navigation is smaller and labels stay black',`(()=>{
        const current=document.querySelector('#pageNav .active'),other=document.querySelector('#pageNav [data-tab="channels"]');
        return current.getAttribute('aria-current')==='page'&&current.getBoundingClientRect().width<other.getBoundingClientRect().width
          &&[...document.querySelectorAll('#pageNav button')].every(button=>getComputedStyle(button).color==='rgb(26, 27, 39)'&&getComputedStyle(button,'::before').color==='rgb(89, 100, 242)');
      })()`);
      await evaluate("document.querySelector('#pageNav [data-tab=\"channels\"]').scrollIntoView({block:'center'});");
      const navPoint=await evaluate("(()=>{const r=document.querySelector('#pageNav [data-tab=\"channels\"]').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()");
      await cdp.send('Input.dispatchMouseEvent',{type:'mousePressed',...navPoint,button:'left',clickCount:1});
      await until("document.querySelector('#pageNav [data-tab=\"channels\"]').classList.contains('nav-expanded')");
      await until("(()=>{const button=document.querySelector('#pageNav [data-tab=\"channels\"]');return button.getBoundingClientRect().width>button.offsetWidth*1.035&&parseFloat(getComputedStyle(button,'::after').top)<-3.9;})()");
      await check(label+' long press enlarges the frame until it meets its neighbor',`(()=>{
        const button=document.querySelector('#pageNav [data-tab="channels"]'),r=button.getBoundingClientRect(),above=button.previousElementSibling.getBoundingClientRect();
        return r.top+parseFloat(getComputedStyle(button,'::after').top)*1.04<=above.bottom+1;
      })()`);
      await screenshot(label+'-sidebar-pressed');
      await cdp.send('Input.dispatchMouseEvent',{type:'mouseReleased',...navPoint,button:'left',clickCount:1});
      await until("document.querySelector('#pageNav [data-tab=\"channels\"]').classList.contains('active')&&!document.querySelector('#pageNav .nav-expanded, #pageNav .nav-switching')");
      await check(label+' release selects and shrinks the navigation item',"document.querySelector('#pageNav .active').getBoundingClientRect().width<document.querySelector('#pageNav [data-tab=\"groups\"]').getBoundingClientRect().width");
      await check(label+' scrolling gesture cancels a pending long press',`(async()=>{
        const button=document.querySelector('#pageNav [data-tab="groups"]');
        button.dispatchEvent(new PointerEvent('pointerdown',{isPrimary:true,button:0,clientX:50,clientY:50}));
        button.dispatchEvent(new PointerEvent('pointermove',{isPrimary:true,clientX:50,clientY:75}));
        await new Promise(resolve=>setTimeout(resolve,220));
        return !button.classList.contains('nav-expanded');
      })()`);
      await check(label+' pointer cancellation restores a held navigation item',`(async()=>{
        const button=document.querySelector('#pageNav [data-tab="groups"]');
        button.dispatchEvent(new PointerEvent('pointerdown',{isPrimary:true,button:0,clientX:50,clientY:50}));
        await new Promise(resolve=>setTimeout(resolve,220));
        const expanded=button.classList.contains('nav-expanded');
        window.dispatchEvent(new PointerEvent('pointercancel'));
        return expanded&&!button.classList.contains('nav-expanded');
      })()`);
      groupPermission={usable_users:[],usable_users_enabled:false,blocked_users:['11111'],blocked_user_reasons:{'11111':'command black'},whitelist_users:[],whitelist_user_reasons:{}};groupRevision=30;
      await evaluate("navigateToTab('groups');");
      await until("$('groupBlockedUsers').value==='11111'");
      await check(label+' group refresh reads shared access lists',"CONFIG.permission.blocked_users[0]==='11111'&&CONFIG_REVISION===30&&$('groupAccessSummary').textContent.includes('command black')");
      await check(label+' group icon actions float at right center',`(()=>{
        const actions=$('refreshGroupsBtn').closest('.floating-actions'),box=actions.getBoundingClientRect();
        return getComputedStyle(actions).position==='fixed'&&Math.abs(box.top+box.height/2-innerHeight/2)<1&&box.right<=innerWidth&&box.left>innerWidth/2
          &&['refreshGroupsBtn','saveGroupsBtn'].every(id=>$(id).querySelector('svg')&&!$(id).textContent.trim()&&$(id).getAttribute('aria-label'));
      })()`);
      groupPermission={...groupPermission,blocked_users:['22222'],blocked_user_reasons:{'22222':'new command'}};groupRevision++;
      await click('#refreshGroupsBtn');await until("$('groupBlockedUsers').value==='22222'");
      await check(label+' refresh updates reasons and config revision',"CONFIG_REVISION===31&&$('groupAccessSummary').textContent.includes('new command')&&!$('groupAccessSummary').textContent.includes('11111')");
      await evaluate("navigateToTab('base');");
      groupPermission={...groupPermission,whitelist_users:['55555'],whitelist_user_reasons:{'55555':'command white'}};groupRevision++;
      await evaluate("navigateToTab('groups');");await until("$('groupWhitelistUsers').value==='55555'");
      await check(label+' revisiting groups updates command whitelist',"$('groupAccessSummary').textContent.includes('command white')");
      await evaluate("$('groupBlockedUsers').value+='\\n33333';$('groupBlockedUsers').dispatchEvent(new Event('input'));");
      groupPermission={...groupPermission,blocked_users:['22222','44444']};groupRevision++;
      await evaluate('loadQQGroups(true);');
      await check(label+' refresh preserves local and command additions',"textList('groupBlockedUsers').join(',')==='22222,44444,33333'&&CONFIG.permission.blocked_users.join(',')==='22222,44444'");
      const groupWritesBefore=groupWrites.length;
      await click('#saveGroupsBtn');await until('CONFIG_REVISION===34');
      assert.equal(groupWrites.length,groupWritesBefore+1,'group save submits once');
      assert.deepEqual(groupWrites.at(-1).permission.blocked_users,['22222','44444','33333']);
      await evaluate("document.querySelector('.toast-wrap')?.replaceChildren();document.activeElement?.blur();window.scrollTo(0,0);");await screenshot(label+'-group-access');
      await evaluate("navigateToTab('image');window.scrollTo(0,0);");
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
      await check(label+' output selects paired and inputs full width',filtersFit('defaultAspect'));
      await evaluate('window.scrollTo(0,0);');
      await screenshot(label+'-image-settings');
      await check(label+' alert thresholds have defaults after config load',`$('alertRecordSuccessRate').value==='80'&&$('alertGenerationP95').value==='60'&&$('alertCacheOrphanGrowth').value==='10'`);
      await evaluate("navigateToTab('monitor');");
      await check(label+' monitor filters fit side by side',filtersFit('monitorMedia'));
      await until("$('taskQueueCount').textContent==='3'");
      await check(label+' task queue starts collapsed',"!$('taskQueueModal').classList.contains('show')&&!$('taskQueueScope').getBoundingClientRect().height");
      await evaluate("$('monitorMedia').scrollIntoView({block:'start'});");
      await screenshot(label+'-monitor-filters');
      await click('#openTaskQueueBtn');
      await check(label+' task queue dialog fits',modalFits('taskQueueModal'));
      await check(label+' task filters fit side by side',filtersFit('taskQueueScope'));
      await evaluate("$('taskQueueScope').scrollIntoView({block:'center'});");
      await screenshot(label+'-task-filters');
      await cdp.send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});
      await cdp.send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape'});
      await check(label+' task queue closes and restores focus',"!$('taskQueueModal').classList.contains('show')&&document.activeElement===$('openTaskQueueBtn')");
      await evaluate("navigateToTab('assets');");
      await check(label+' asset filters fit side by side',filtersFit('assetModel'));
      await check(label+' asset actions are simplified',"!$('assetStudioPreset')&&!$('assetImportFile')&&!$('assetMedia')&&!document.querySelector('#assets [onclick*=\"exportAssets\"]')");
      await evaluate('window.scrollTo(0,0);');
      await screenshot(label+'-asset-filters');
      await until("$('assetGrid').querySelectorAll('.asset-card').length===2");
      await evaluate("$('assetGrid').scrollIntoView({block:'start'});");
      await until("[...$('assetGrid').querySelectorAll('img')].every(img=>img.naturalWidth>0)");
      await check(label+' asset cards only offer download and detail',"[...$('assetGrid').querySelectorAll('.asset-card')].every(card=>[...card.querySelectorAll('.asset-card-actions button')].map(button=>button.dataset.assetAction).join(',')==='download,detail')");
      await check(label+' assets have two image columns and actions underneath',`(()=>{
        const cards=[...$('assetGrid').querySelectorAll('.asset-card')],a=cards[0].getBoundingClientRect(),b=cards[1].getBoundingClientRect();
        return Math.abs(a.top-b.top)<1&&a.right<=b.left&&!$('assetGrid').querySelector('.asset-card-body,.asset-card-prompt,.asset-card-context')
          &&cards.every(card=>card.querySelector('.asset-card-actions').getBoundingClientRect().top>=card.querySelector('.asset-thumb').getBoundingClientRect().bottom-1);
      })()`);
      await evaluate("window.assetNodes=[...$('assetGrid').querySelectorAll('.asset-card,img')];window.assetMutations=0;window.assetObserver=new MutationObserver(records=>{window.assetMutations+=records.length;});window.assetObserver.observe($('assetGrid'),{childList:true,subtree:true});");
      const assetCallsBefore=assetCalls.length;
      await evaluate("$('assetGrid').querySelector('input[data-asset-select]').scrollIntoView({block:'end'});");
      const selectedScroll=await evaluate('scrollY');
      await click('#assetGrid input[data-asset-select]','end');
      await check(label+' selecting assets preserves cards thumbnails and scroll',`ASSET_SELECTION.has('asset-one')&&$('assetSelectionStatus').textContent.includes('1')&&assetNodes.every(node=>node.isConnected)&&assetMutations===0&&scrollY===${selectedScroll}`);
      await evaluate('selectAllAssets();');
      await check(label+' select all checks both without rerender',"ASSET_SELECTION.size===2&&[...$('assetGrid').querySelectorAll('input')].every(input=>input.checked)&&assetNodes.every(node=>node.isConnected)&&assetMutations===0");
      await evaluate('clearAssetSelection();');
      await check(label+' clearing selection preserves media',"ASSET_SELECTION.size===0&&[...$('assetGrid').querySelectorAll('input')].every(input=>!input.checked)&&assetNodes.every(node=>node.isConnected)&&assetMutations===0");
      assert.equal(assetCalls.length,assetCallsBefore,'selection must not reload assets or thumbnails');
      await evaluate('assetObserver.disconnect();');
      await evaluate("$('assetGrid').scrollIntoView({block:'start'});");
      await screenshot(label+'-asset-cards');
      const recordsBeforeImage=recordCalls.length;
      await evaluate("document.querySelector('.toast-wrap')?.replaceChildren();");
      await click('#assetGrid .asset-thumb img');
      await until("$('mediaLightboxModal').classList.contains('show')&&!$('mediaLightboxImage').hidden");
      assert.equal(recordCalls.length,recordsBeforeImage,'image click must not load record details');
      await check(label+' asset thumbnail opens original without zoom overlay',"!$('recordModal').classList.contains('show')&&!$('assetGrid').querySelector('.media-zoom-button')&&MEDIA_LIGHTBOX_PATH==='asset-one.png'");
      assert(mediaCalls.some(call=>call.endpoint==='/api/cache-image-preview'&&call.path==='asset-one.png'),'lightbox must fetch the original');
      await screenshot(label+'-asset-lightbox');
      await evaluate('closeMediaLightbox();');
      await click('#assetGrid [data-asset-action="detail"]');
      await until("$('recordModal').classList.contains('show')&&!$('recordDetailBody').textContent.includes('正在加载记录详情')");
      await evaluate('closeRecordDetail();');
      fixtureCanvas={root_node_id:'node-one',nodes:[{id:'node-one',title:'缩略图节点',template_id:'i2i',status:'succeeded',position:{x:0,y:0},params:{prompt:'Preserve node prompt',aspect_ratio:'9:16',resolution:'1K'},result:{media_path:`node-original-${label}.png`,thumbnail_path:`node-original-${label}.png`}}],edges:[],viewport:{x:120,y:80,zoom:1}};
      const canvasMediaStart=mediaCalls.length;
      await evaluate(`document.querySelectorAll('main.app-shell>section').forEach(section=>section.classList.toggle('active',section.id==='studio'));document.querySelectorAll('nav.page-nav button').forEach(button=>button.classList.toggle('active',button.dataset.tab==='studio'));STUDIO.current={id:'canvas-fixture',title:'节点图片验证',slots:[],graph:{}};STUDIO.sessions=[STUDIO.current];STUDIO.templates=[{id:'i2i',title:'图生图'}];STUDIO.canvas=${JSON.stringify(fixtureCanvas)};STUDIO.selectedNodeId='';STUDIO.viewportFittedForSession=false;renderStudio();`);
      await evaluate("$('studioCanvasViewport').scrollIntoView({block:'center'});");
      await until("$('studioCanvasNodes').querySelector('img')?.naturalWidth>0");
      await click('#studioCanvasNodes .node-media img');
      await check(label+' node image selects without magnifying',"STUDIO.selectedNodeId==='node-one'&&$('studioCanvasNodes').querySelector('.selected')&&!$('mediaLightboxModal').classList.contains('show')&&!$('studioCanvasNodes').querySelector('.media-zoom-shell')");
      await check(label+' node detail has upload and gallery',"!!$('studioNodePanelBody').querySelector('[data-panel-action=\"upload\"]')&&!!$('studioNodePanelBody').querySelector('[data-panel-action=\"gallery\"]')");
      await check(label+' image preview has no action button background',"getComputedStyle($('studioNodePanelBody').querySelector('.media-zoom-shell')).backgroundColor==='rgba(0, 0, 0, 0)'");
      await click('#studioNodePanelBody [data-panel-action="gallery"]');
      await until("$('referenceGalleryBody').querySelector('.studio-gallery-item')!==null");
      await until("[...$('referenceGalleryBody').querySelectorAll('img')].every(image=>image.naturalWidth>0)");
      await check(label+' main image gallery opens in a dialog',modalFits('referenceGalleryModal'));
      await check(label+' main image gallery uses three cropped portrait frames',galleryFits('referenceGalleryBody'));
      await screenshot(label+'-node-gallery');
      await click('#referenceGalleryBody .studio-gallery-item img');
      await check(label+' gallery image selects without magnifying',"$('referenceGalleryBody').querySelector('.studio-gallery-item.selected')!==null&&!$('mediaLightboxModal').classList.contains('show')");
      const beforeGalleryWrite=nodeWrites.length;
      await click('#referenceGalleryBody [data-ref-confirm]');
      await until("studioNode('node-one').result.media_path==='gallery-original.png'&&!STUDIO.nodeImageRequest&&!$('referenceGalleryModal').classList.contains('show')");
      assert.equal(nodeWrites.length,beforeGalleryWrite+1);
      assert.equal(nodeWrites.at(-1).nodes[0].image_path,'gallery-original.png');
      assert.equal(nodeWrites.at(-1).nodes[0].params.prompt,'Preserve node prompt');
      await evaluate(`(()=>{const input=$('studioNodeFilePick'),transfer=new DataTransfer();transfer.items.add(new File([Uint8Array.from(atob(${JSON.stringify(previewData)}),char=>char.charCodeAt(0))],'node.png',{type:'image/png'}));input.files=transfer.files;input.dispatchEvent(new Event('change'));})()`);
      await until("studioNode('node-one').result.media_path==='node-upload.png'&&!STUDIO.nodeImageRequest");
      await evaluate("$('studioNodePanel').scrollIntoView({block:'start'});");
      await until("$('studioNodePanelBody').querySelector('.studio-node-result')?.naturalWidth>0");
      assert(nodeWrites.at(-1).nodes[0].image.startsWith('data:image/png;base64,'));
      assert.equal(nodeWrites.at(-1).nodes[0].params.prompt,'Preserve node prompt');
      const writesBeforeStale=nodeWrites.length;
      await evaluate("studioSetNodeImage(studioNode('node-one'),{image_path:'wrong.png'},'previous-session')");
      assert.equal(nodeWrites.length,writesBeforeStale,'stale upload must not alter another session');
      await evaluate("document.querySelector('.toast-wrap')?.replaceChildren();$('studioNodePanel').scrollIntoView({block:'start'});");
      await screenshot(label+'-node-detail');
      await check(label+' canvas template controls and retired page removed',"!$('creativeCanvas')&&!$('studioNodeTemplate')&&!$('studioTemplateSelect')");
      await click('#studioExtraReferences [data-ref-add]');
      await until("studioNode('node-one').extra_refs?.length===1&&!STUDIO.nodeImageRequest");
      await click('#studioExtraReferences [data-ref-gallery]');
      await until("$('referenceGalleryBody').querySelector('.studio-gallery-grid img')?.naturalWidth>0");
      await check(label+' extra reference gallery modal fits',modalFits('referenceGalleryModal'));
      await check(label+' reference modal uses three cropped portrait frames',galleryFits('referenceGalleryBody'));
      await click('#referenceGalleryBody .studio-gallery-item');
      await click('#referenceGalleryBody [data-ref-confirm]');
      await until("!$('referenceGalleryModal').classList.contains('show')&&studioNode('node-one').extra_refs?.[0].image_path==='gallery-original.png'&&!STUDIO.nodeImageRequest");
      await click('#studioExtraReferences [data-ref-add]');
      await until("studioNode('node-one').extra_refs?.length===2&&!STUDIO.nodeImageRequest");
      await evaluate(`(()=>{const input=$('studioExtraReferences').querySelectorAll('[data-ref-file]')[1],transfer=new DataTransfer();transfer.items.add(new File([Uint8Array.from(atob(${JSON.stringify(previewData)}),char=>char.charCodeAt(0))],'extra.png',{type:'image/png'}));input.files=transfer.files;input.dispatchEvent(new Event('change'));})()`);
      await until("studioNode('node-one').extra_refs?.[1].image_path==='extra-upload.png'&&!STUDIO.nodeImageRequest");
      await evaluate("$('studioExtraReferences').scrollIntoView({block:'center'});");
      await screenshot(label+'-node-extra-references');
      assert(mediaCalls.slice(canvasMediaStart).every(call=>call.endpoint==='/api/cache-image-thumbnail'),'node, reference and gallery previews must only request thumbnails');
      assert(mediaCalls.slice(canvasMediaStart).some(call=>call.path===`node-original-${label}.png`));
      assert(mediaCalls.some(call=>call.path==='extra-upload.png'));
      await click('#studioExtraReferences [data-ref-remove]');
      await until("studioNode('node-one').extra_refs?.length===1&&!STUDIO.nodeImageRequest");
      fixtureCanvas.nodes.push(
        {id:'node-child',parent_id:'node-one',title:'已失效图片的后续节点',status:'failed',position:{x:280,y:0},params:{prompt:'Keep child prompt'},result:{media_path:`missing-node-${label}.png`},extra_refs:[{id:'replace-me',image_path:`missing-reference-${label}.png`}]},
        {id:'node-active',parent_id:'node-one',title:'生成中的后续节点',status:'running',position:{x:280,y:200},params:{}}
      );
      await evaluate(`STUDIO.canvas=${JSON.stringify(fixtureCanvas)};STUDIO.running=true;STUDIO.taskNodeId='node-active';studioRenderPanel();`);
      await check(label+' another running node does not lock root image controls',"[...$('studioNodePanelBody').querySelectorAll('[data-panel-action=\"upload\"],[data-panel-action=\"gallery\"],#studioExtraReferences button')].every(button=>!button.disabled)");
      await click('#studioExtraReferences [data-ref-add]');
      await until("studioNode('node-one').extra_refs?.length===2&&!STUDIO.nodeImageRequest");
      await evaluate("STUDIO.selectedNodeId='node-active';studioRenderPanel();");
      await check(label+' the generating node keeps reference edits locked',"!$('studioNodePanelBody').querySelector('[data-panel-action=\"upload\"],[data-panel-action=\"gallery\"]')&&$('studioExtraReferences').querySelector('[data-ref-add]').disabled");
      await evaluate("STUDIO.selectedNodeId='node-child';studioRenderPanel();");
      await check(label+' later nodes hide main image pickers and can repair cleared references',"!$('studioNodeFilePick')&&!$('studioNodeGallery')&&!$('studioNodePanelBody').querySelector('[data-panel-action=\"upload\"],[data-panel-action=\"gallery\"]')&&[...$('studioExtraReferences').querySelectorAll('button')].every(button=>!button.disabled)");
      await evaluate(`(()=>{const input=$('studioExtraReferences').querySelector('[data-ref-file]'),transfer=new DataTransfer();transfer.items.add(new File([Uint8Array.from(atob(${JSON.stringify(previewData)}),char=>char.charCodeAt(0))],'replacement.png',{type:'image/png'}));input.files=transfer.files;input.dispatchEvent(new Event('change'));})()`);
      await until("studioNode('node-child').extra_refs?.[0].image_path==='extra-upload.png'&&!STUDIO.nodeImageRequest");
      await click('#studioExtraReferences [data-ref-gallery]');
      await until("$('referenceGalleryBody').querySelector('.studio-gallery-item')!==null");
      await click('#referenceGalleryBody .studio-gallery-item:nth-child(2)');
      await click('#referenceGalleryBody [data-ref-confirm]');
      await until("studioNode('node-child').extra_refs?.[0].image_path==='gallery-portrait.png'&&!STUDIO.nodeImageRequest&&!$('referenceGalleryModal').classList.contains('show')");
      await click('#studioExtraReferences [data-ref-add]');
      await until("studioNode('node-child').extra_refs?.length===2&&!STUDIO.nodeImageRequest");
      await evaluate("$('studioExtraReferences').scrollIntoView({block:'center'});");
      await screenshot(label+'-child-extra-reference-repair');
      await evaluate("STUDIO.running=false;STUDIO.taskNodeId='';STUDIO.selectedNodeId='node-one';studioRenderPanel();");
      for(const [kind,selector,panelId] of [['image','#studioNodePresetBtn','catalogPresetPanel'],['cos','#studioNodeCosBtn','catalogCosPanel']]) {
        await click(selector);
        await until(`$('${panelId}').querySelector('[data-${kind==='cos'?'cos':'preset'}-idx]')!==null`);
        await check(label+' node '+kind+' picker dialog fits',modalFits('catalogPickerModal'));
        await screenshot(label+'-node-'+kind+'-picker');
        await click(`#${panelId} [data-${kind==='cos'?'cos':'preset'}-idx]`);
        await check(label+' node '+kind+' choice updates prompt and closes dialog',`!$('catalogPickerModal').classList.contains('show')&&$('studioNodePrompt').value==='${kind==='cos'?'COS':'Preset'} prompt 0'&&studioNode('node-one').params.prompt===$('studioNodePrompt').value`);
      }
      await evaluate('clearTimeout(STUDIO.canvasSaveTimer);');
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
      await check(label+' channel switches sit to the right outside status pills',"[...$('channelList').querySelectorAll('.card')].every(card=>{const input=card.querySelector('.channel-state-toggle input[role=\"switch\"]'),label=card.querySelector('[data-channel-state]');return input&&label&&!input.closest('.pill')&&input.getBoundingClientRect().left>=label.getBoundingClientRect().right;})&&!$('channelList').textContent.includes('协议随模型')&&!$('channelList').querySelector('.checkline')");
      await evaluate("window.channelSwitch=$('channelList').querySelector('.channel-state-toggle input');window.channelSwitch.checked=false;window.channelSwitch.onchange({target:window.channelSwitch});");
      await until("$('channelList').querySelector('[data-channel-state]').textContent==='已停用'&&CONFIG.image_channels[0].enabled===false");
      await evaluate("window.channelSwitch.checked=true;window.channelSwitch.onchange({target:window.channelSwitch});");
      await until("$('channelList').querySelector('[data-channel-state]').textContent==='使用中'&&CONFIG.image_channels[0].enabled===true");
      await check(label+' channel toggling preserves switch node',"channelSwitch.isConnected");
      failConfigWrite=true;
      await evaluate("window.channelSwitch.checked=false;window.channelSwitch.onchange({target:window.channelSwitch});");
      failConfigWrite=false;
      await check(label+' failed channel toggle restores checked status',"CONFIG.image_channels[0].enabled===true&&$('channelList').querySelector('.channel-state-toggle input').checked&&$('channelList').querySelector('[data-channel-state]').textContent==='使用中'");
      await evaluate(`CONFIG=${JSON.stringify(config)};renderAllChannelLists();refreshModelSelectors();`);
      await evaluate('window.scrollTo(0,0);');
      await screenshot(label+'-channels');
      await evaluate("navigateToTab('test');setSelectOptions('testAspect',ASPECTS,'9:16');");
      for(const [kind,selector,panelId] of [['image','#testPresetBtn','catalogPresetPanel'],['cos','#testCosBtn','catalogCosPanel']]) {
        const beforeCatalog=catalogCalls.length;
        await click(selector);
        await until(`$('${panelId}').querySelector('[data-${kind==='cos'?'cos':'preset'}-idx]')!==null`);
        await evaluate(`for(let i=0;i<8;i++)${kind==='cos'?'toggleTestCos':'toggleTestPresets'}();`);
        await check(label+' test '+kind+' picker fits',modalFits('catalogPickerModal'));
        await click(`#${panelId} [data-${kind==='cos'?'cos':'preset'}-idx]`);
        assert.equal(catalogCalls.length,beforeCatalog,'node and quick-test pickers must reuse paginated cache');
        await check(label+' test '+kind+' updates prompt',`$('testPrompt').value==='${kind==='cos'?'COS':'Preset'} prompt 0'&&!$('catalogPickerModal').classList.contains('show')`);
      }
      await check(label+' quick test pairs select fields',filtersFit('testChannel'));
      await check(label+' quick test template fields removed',"!document.querySelector('#test [id^=\"testTemplate\"]')&&!$('testVariationEnabled')");
      await evaluate('window.scrollTo(0,0);');
      await screenshot(label+'-quick-test');
      await click('#testExtraReferences [data-ref-add]');
      await click('#testExtraReferences [data-ref-gallery]');
      await until("$('referenceGalleryBody').querySelector('.studio-gallery-item')!==null");
      await click('#referenceGalleryBody .studio-gallery-item');
      await click('#referenceGalleryBody [data-ref-confirm]');
      await until("TEST_EXTRA_REFS[0]?.image_path==='gallery-original.png'&&!$('referenceGalleryModal').classList.contains('show')");
      const testWritesBefore=testWrites.length;
      await evaluate("$('testPrompt').value='Fixture prompt {场景}';runImageTest();");
      await check(label+' quick test submission succeeds',"!$('testStatus').textContent.includes('失败')");
      if(testWrites.length!==testWritesBefore+1)throw new Error('Quick test did not submit: '+await evaluate("$('testStatus').textContent"));
      await until("$('testResponseData').textContent.includes('succeeded')&&!TEST_TASK_ID");
      assert.equal(testWrites.length,testWritesBefore+1,'quick test should submit one generation request');
      assert.equal(testWrites.at(-1).prompt,'Fixture prompt {场景}');
      assert.equal(testWrites.at(-1).images.length,1);
      assert(testWrites.at(-1).images[0].startsWith('data:image/png;base64,'));
      assert.equal(testWrites.at(-1).prompt_enhance,false);
      await check(label+' quick test stays inline until a data button is clicked',"!$('testDataModal').classList.contains('show')&&!$('promptEnhance')");
      for(const kind of ['request','response','result']) {
        await evaluate(`showTestPanel('${kind}');`);
        await check(label+' quick test '+kind+' dialog fits',modalFits('testDataModal'));
        await check(label+' quick test only requested panel is visible',`$('test${kind[0].toUpperCase()+kind.slice(1)}Panel').classList.contains('active')`);
        await evaluate('closeTestPanel();');
      }
      for(const key of ['template_values','template_randomize','variation_enabled','variation_fields'])assert(!(key in testWrites.at(-1)),'quick test must not add '+key);
      await evaluate("navigateToTab('channels');");
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
      await check(label+' enable toggle follows channel name',"$('modalEnabled').closest('label').previousElementSibling===$('modalChannelName')");
      await check(label+' cached models are lazy',"!$('cacheModelsModal').classList.contains('show')&&!$('cacheModels').children.length&&!$('channelModal').querySelector('#cacheModels')");
      await check(label+' model options are absent from main editor',"!$('enabledModels').querySelector('select,input')");
      await click('#openCacheModels');
      await check(label+' cached model dialog fits',modalFits('cacheModelsModal'));
      await check(label+' cache lists models',"$('cacheModels').querySelectorAll('.model-row').length===2");
      await evaluate("$('cacheSearch').value='cached-model';$('cacheSearch').dispatchEvent(new Event('input'));");
      await check(label+' cache search narrows models',"$('cacheModels').querySelectorAll('.model-row').length===1");
      await click('#cacheModels .model-row button');
      await check(label+' cache enables model in channel',"collectModalChannel().enabled_models.includes('cached-model')");
      await screenshot(label+'-cache-models');
      await cdp.send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});
      await cdp.send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape'});
      await check(label+' Escape closes cache only',"!$('cacheModelsModal').classList.contains('show')&&$('channelModal').classList.contains('show')&&!$('channelModal').inert&&document.activeElement===$('openCacheModels')");
      await click('#enabledModels .model-edit');
      await check(label+' model settings fit',modalFits('modelSettingsModal'));
      await click('#modelSettingsBody .model-image-to-text');
      await click('#modelSettingsBody .model-prompt-en');
      await evaluate("(()=>{const type=$('modelSettingsBody').querySelector('.model-provider');type.value='openai_chat';type.dispatchEvent(new Event('change'));const proxy=$('modelSettingsBody').querySelector('.model-download-proxy');proxy.value='download-proxy';proxy.dispatchEvent(new Event('change'));})()");
      await screenshot(label+'-model-settings');
      await cdp.send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});
      await cdp.send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape'});
      await check(label+' Escape closes settings only',"!$('modelSettingsModal').classList.contains('show')&&$('channelModal').classList.contains('show')&&!$('channelModal').inert&&document.activeElement===$('enabledModels').querySelector('.model-edit')");
      await evaluate('moveEnabledModel(0,1);');
      const channelWrites=configWrites.length;
      await click('#modalSave');
      await until("!CHANNEL_MODAL_SAVING");
      assert.equal(configWrites.length,channelWrites+1);
      await check(label+' saving preserves channel dialog',"$('channelModal').classList.contains('show')&&!EDITING_CHANNEL_IS_NEW&&!CHANNEL_MODAL_DIRTY");
      const savedChannel=configWrites.at(-1).image_channels[0];
      assert.deepEqual(savedChannel.enabled_models,['cached-model',modelName]);
      assert.deepEqual(savedChannel.image_to_text_models,[modelName]);
      assert.deepEqual(savedChannel.prompt_en_models,[modelName]);
      assert.equal(savedChannel.model_provider_types[modelName],'openai_chat');
      assert.equal(savedChannel.model_download_proxy_ids[modelName],'download-proxy');
      await evaluate('openModelSettings(1)');
      await check(label+' model settings survive reordering and saving',"$('modelSettingsBody').querySelector('.model-image-to-text').checked&&$('modelSettingsBody').querySelector('.model-prompt-en').checked&&$('modelSettingsBody').querySelector('.model-provider').value==='openai_chat'&&$('modelSettingsBody').querySelector('.model-download-proxy').value==='download-proxy'");
      await click('#modelSettingsBody .model-image-to-text');
      await click('#modelSettingsBody .model-prompt-en');
      await evaluate("(()=>{const type=$('modelSettingsBody').querySelector('.model-provider');type.value='';type.dispatchEvent(new Event('change'));const proxy=$('modelSettingsBody').querySelector('.model-download-proxy');proxy.value='';proxy.dispatchEvent(new Event('change'));closeModelSettings();})()");
      await click('#modalSave');await until("!CHANNEL_MODAL_SAVING");
      assert.deepEqual(configWrites.at(-1).image_channels[0].image_to_text_models,[]);
      assert.deepEqual(configWrites.at(-1).image_channels[0].prompt_en_models,[]);
      assert.deepEqual(configWrites.at(-1).image_channels[0].model_provider_types,{});
      assert.deepEqual(configWrites.at(-1).image_channels[0].model_download_proxy_ids,{});
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
      for(const kind of ['image','audit']) {
        await evaluate(`openChannelModal(-1,${JSON.stringify(kind)},{isNew:true});$('modalChannelName').value='new-${kind}';addEnabledModel('new-model');openModelSettings(0);`);
        await check(label+' '+kind+' new model settings fit',modalFits('modelSettingsModal'));
        await check(label+' '+kind+' model toggles match channel kind',`!!$('modelSettingsBody').querySelector('.model-image-to-text')===${kind==='image'}`);
        await evaluate('closeModelSettings();');
        const newWrites=configWrites.length;
        await click('#modalSave');await until("!CHANNEL_MODAL_SAVING");
        const channelId=await evaluate(`CONFIG.${kind}_channels.at(-1).id`);
        await check(label+' '+kind+' new channel remains editable',"$('channelModal').classList.contains('show')&&!EDITING_CHANNEL_IS_NEW");
        await evaluate("$('modalChannelName').value+='-edited';$('modalChannelName').dispatchEvent(new Event('input'));");
        failConfigWrite=true;
        await click('#modalSave');await until("!CHANNEL_MODAL_SAVING");
        failConfigWrite=false;
        await check(label+' '+kind+' failed save remains editable',"$('channelModal').classList.contains('show')&&!$('modalSave').disabled&&$('modalStatus').textContent.includes('保存失败')");
        await click('#modalSave');await until("!CHANNEL_MODAL_SAVING");
        assert.equal(configWrites.length,newWrites+2);
        const saved=configWrites.at(-1)[kind+'_channels'];
        assert.equal(saved.filter(channel=>channel.id===channelId).length,1,'repeated saves should update the newly added channel');
        assert.equal(saved.find(channel=>channel.id===channelId).name,`new-${kind}-edited`);
        await evaluate('closeChannelModal();');
      }
      await evaluate("window.priorityBefore={image:$('priorityList').value,audit:$('auditPriorityList').value};$('priorityList').value='Channel A/model-a';refreshPriorityPickerWithLabels('image',['Channel A/model-a','Channel A/model-b','Channel B/model-c']);");
      await check(label+' priority model filters selected models',"$('priorityChannel').value==='Channel A'&&$('priorityPicker').options.length===1&&$('priorityPicker').value==='Channel A/model-b'&&$('priorityPicker').options[0].textContent==='model-b'");
      await evaluate("$('priorityList').value+=String.fromCharCode(10)+'Channel A/model-b';refreshPriorityPickerWithLabels('image',['Channel A/model-a','Channel A/model-b','Channel B/model-c']);");
      await check(label+' priority filters exhausted channels',"$('priorityChannel').options.length===1&&$('priorityChannel').value==='Channel B'&&$('priorityPicker').value==='Channel B/model-c'");
      await evaluate("$('priorityList').value+=String.fromCharCode(10)+'Channel B/model-c';refreshPriorityPickerWithLabels('image',['Channel A/model-a','Channel A/model-b','Channel B/model-c']);");
      await check(label+' exhausted priority lists have no selectable value',"!$('priorityChannel').value&&!$('priorityPicker').value");
      await evaluate("$('priorityList').value=priorityBefore.image;$('auditPriorityList').value=priorityBefore.audit;refreshPriorityPicker('image');refreshPriorityPicker('audit');window.beforeProxyConfig=JSON.parse(JSON.stringify(CONFIG));CONFIG.image_channels=[{name:'Channel use',proxy_id:'download-proxy',enabled_models:['a','b'],model_download_proxy_ids:{a:'download-proxy'}},{name:'Model use',enabled_models:['c'],model_download_proxy_ids:{c:'download-proxy',disabled:'download-proxy'}}];CONFIG.audit_channels=[];document.querySelectorAll('main.app-shell>section').forEach(section=>section.classList.toggle('active',section.id==='proxies'));renderProxyList();");
      await evaluate(`CONFIG.proxies=${JSON.stringify(config.proxies)};renderProxyList();`);
      await check(label+' proxy usage counts channels once and enabled model overrides once',"proxyUsage('download-proxy').length===2&&$('proxyList').querySelector('[data-act=\"usage\"] span').textContent==='2'");
      await click('#proxyList [data-act="usage"]');
      await check(label+' proxy usage dialog fits',modalFits('proxyInfoModal'));
      await check(label+' proxy usage lists channels and models',"$('proxyInfoBody').textContent.includes('Channel use')&&$('proxyInfoBody').textContent.includes('Model use')&&$('proxyInfoBody').querySelectorAll('.proxy-usage-row').length===2");
      await screenshot(label+'-proxy-usage');
      await evaluate('closeProxyInfo();');
      for(const [action,text] of [['test','连通'],['quality','评分']]) {
        await click(`#proxyList [data-act="${action}"]`);
        await until(`$('proxyInfoBody').textContent.includes('${text}')`);
        await check(label+' proxy '+action+' dialog fits',modalFits('proxyInfoModal'));
        await check(label+' proxy results stay out of the list',"!$('proxyList').querySelector('.proxy-result')&&$('proxyInfoModal').classList.contains('show')");
        await evaluate('closeProxyInfo();');
      }
      await evaluate("CONFIG=beforeProxyConfig;renderProxyList();document.querySelectorAll('main.app-shell>section').forEach(section=>section.classList.toggle('active',section.id==='selfie'));renderSelfieIdentity({identity_mode:'image',image_pool:[],persona_pool:[],pool_limit:3});");
      await click('[data-identity-mode="persona"]');
      await until("$('selfiePersonaEnabled').checked&&!$('selfiePersonaEnabled').disabled");
      await check(label+' persona switch selects persona pool',"document.querySelector('[data-identity-mode=\"persona\"]').getAttribute('aria-pressed')==='true'&&!$('selfiePersonaPoolPanel').hidden&&$('selfieImagePoolPanel').hidden");
      await click('[data-identity-mode="image"]');
      await until("!$('selfiePersonaEnabled').checked&&!$('selfiePersonaEnabled').disabled");
      await check(label+' image switch selects image pool',"document.querySelector('[data-identity-mode=\"image\"]').getAttribute('aria-pressed')==='true'&&$('selfiePersonaPoolPanel').hidden&&!$('selfieImagePoolPanel').hidden");
      await screenshot(label+'-identity-switch');
      const beforeIdentity=identityCalls.length;
      await evaluate("renderSelfieIdentity({identity_mode:'image',has_image:true,active_image_slot:1,image_pool:[{slot:1,available:true,mime_type:'image/png'}],persona_pool:[],pool_limit:3});");
      await until("$('selfiePreview').naturalWidth>0&&$('selfieImagePoolList').querySelector('img')?.naturalWidth>0");
      assert(!identityCalls.slice(beforeIdentity).some(call=>call.includes('view=original')),'identity page must only fetch thumbnails');
      await click('#selfiePreview');
      await until("$('mediaLightboxModal').classList.contains('show')&&!$('mediaLightboxImage').hidden");
      const originalCalls=identityCalls.filter(call=>call.includes('view=original')).length;
      await evaluate('closeMediaLightbox();');
      await click('#selfieImagePoolList img');
      await until("$('mediaLightboxModal').classList.contains('show')&&!$('mediaLightboxImage').hidden");
      assert.equal(identityCalls.filter(call=>call.includes('view=original')).length,originalCalls,'current image and pool image must share original cache');
      await screenshot(label+'-identity-original');
      await evaluate('closeMediaLightbox();');
      await evaluate("(()=>{$('recordTable').replaceChildren();window.initialRecordLoad=loadRecords(false);window.recordLoadingVisible=!!$('recordTable').querySelector('[data-initial-loading]');})()");
      await check(label+' initial record load shows progress',"recordLoadingVisible");
      await evaluate('initialRecordLoad');
      await evaluate("(()=>{$('assetGrid').replaceChildren();window.initialAssetLoad=loadAssets(false);window.assetLoadingVisible=!!$('assetGrid').querySelector('[data-initial-loading]');})()");
      await check(label+' initial asset load shows progress',"assetLoadingVisible");
      await evaluate('initialAssetLoad');
      await evaluate(`openTaskDetail(${JSON.stringify(task.task_id)})`);
      await until("$('taskDetailBody').textContent.includes('UPSTREAM_TIMEOUT')");
      await screenshot(label+'-task');
      await check(label+' task modal',modalFits('taskModal'));
      await evaluate("$('taskDetailBody').querySelector('.task-record-open').scrollIntoView({block:'center'});");
      await screenshot(label+'-task-records');
      await check(label+' linked records',`[...document.querySelectorAll('.task-record-open')].every(el=>{const r=el.getBoundingClientRect(),body=$('taskModal').querySelector('.modal-body').getBoundingClientRect();return r.left>=body.left&&r.right<=body.right+1&&el.scrollWidth<=el.clientWidth+1;})`);
      await check(label+' attempt cells',`[...document.querySelectorAll('#taskDetailBody table td')].every(el=>el.scrollWidth<=el.clientWidth+1)&&document.querySelector('#taskDetailBody table td:nth-child(2)').getBoundingClientRect().width>=100`);
      await evaluate("document.querySelector('#taskModal button[onclick=\"closeTaskDetail()\"]').click();");await until("!$('taskModal').classList.contains('show')");
      recordRows=[recordFixture('record-old',{source:'command',group_id:'10000',user_id:'12345'}),recordFixture('record-same-day',{time:'2026-11-26 11:45:30'})];
      await evaluate("(async()=>{clearTimeout(TASK_LOAD_TIMER);clearRecordPageCache();await loadRecords(false);})()");
      recordRows.unshift(recordFixture('record-new',{time:'2026-11-27 08:09:10',source:'command',user_id:'54321'}));
      await evaluate("navigateToTab('image');navigateToTab('monitor');");
      await until("RECORDS[0]?.id==='record-new'");
      await check(label+' records group dates and stack elapsed and clock under code',`(()=>{
        const headings=[...$('recordTable').querySelectorAll('thead th')].map(th=>th.textContent),row=$('recordTable').querySelector('[data-record-id="record-new"]'),code=row.cells[1].querySelector('.mono'),elapsed=row.cells[1].querySelector('.record-elapsed'),clock=row.cells[1].querySelector('.record-clock');
        const dates=[...$('recordTable').querySelectorAll('.record-date-row')];
        return headings.join('|')==='|编号|来源|模型'&&elapsed.textContent==='12.50秒'&&elapsed.getBoundingClientRect().top>=code.getBoundingClientRect().bottom
          &&clock.textContent==='08时09分10秒'&&clock.getBoundingClientRect().top>=elapsed.getBoundingClientRect().bottom
          &&dates.map(row=>row.textContent).join('|')==='2026年11月27日|2026年11月26日'
          &&dates.every(row=>!row.classList.contains('record-detail-row')&&!row.querySelector('input')&&row.cells[0].colSpan===4);
      })()`);
      await check(label+' record origins use stacked numbers and distinct colors',`(()=>{
        const group=$('recordTable').querySelector('[data-record-id="record-old"] .record-source-group'),personal=$('recordTable').querySelector('[data-record-id="record-new"] .record-source-private'),other=$('recordTable').querySelector('[data-record-id="record-same-day"] .record-source-other');
        return group?.children[0].textContent==='10000'&&group.children[1].textContent==='12345'
          &&group.children[1].getBoundingClientRect().top>=group.children[0].getBoundingClientRect().bottom&&personal?.textContent==='54321'
          &&other?.textContent==='Web/快速试画'&&new Set([group,personal,other].map(item=>getComputedStyle(item).backgroundColor)).size===3
          &&recordSourceBadge({source_label:'群 10000 / QQ 12345'}).includes('record-source-group')&&recordSourceBadge({source_label:'QQ 54321'}).includes('record-source-private');
      })()`);
      await evaluate("$('recordTable').scrollIntoView({block:'start'});");
      await screenshot(label+'-records-grouped');
      await click('#recordTable .record-date-row');
      await check(label+' date group does not open record details',"!$('recordModal').classList.contains('show')");
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
      const requestOriginals=mediaCalls.filter(call=>call.endpoint==='/api/cache-image-preview'&&call.path==='record-input.png').length;
      for(const index of [0,1]) {
        await evaluate(`$('recordDetailBody').querySelectorAll('.images img')[${index}].scrollIntoView({block:'center'});`);
        await until(`$('recordDetailBody').querySelectorAll('.images img')[${index}].naturalWidth>0`);
      }
      assert(mediaCalls.some(call=>call.endpoint==='/api/cache-image-thumbnail'&&call.path==='record-output.png'));
      assert(mediaCalls.some(call=>call.endpoint==='/api/cache-image-thumbnail'&&call.path==='record-input.png'));
      assert.equal(mediaCalls.filter(call=>call.endpoint==='/api/cache-image-preview'&&call.path==='record-input.png').length,requestOriginals);
      await click('#recordDetailBody .images img');
      await until("$('mediaLightboxModal').classList.contains('show')&&!$('mediaLightboxImage').hidden");
      assert(mediaCalls.some(call=>call.endpoint==='/api/cache-image-preview'&&call.path==='record-input.png'));
      await evaluate('closeMediaLightbox();');
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
      console.log(`[content-smoke] ${label} ${width}x${height}: checked sidebar scrolling, filters, channels, tasks, incremental records and lazy detail dialogs`);
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
