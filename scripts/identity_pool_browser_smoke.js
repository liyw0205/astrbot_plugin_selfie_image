#!/usr/bin/env node
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {CdpSession, findChromium, freePort, waitForCdp, stopProcess} = require('./mobile_smoke');
const root = path.resolve(__dirname, '..');
const output = fs.mkdtempSync(path.join(os.tmpdir(), 'selfie-identity-smoke-'));
const png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jf00AAAAASUVORK5CYII=';
const sdk = `<script>
if (parent !== window) {
  let serial = 0; const pending = new Map();
  addEventListener('message', event => {
    if (event.source !== parent) return;
    const resolve = pending.get(event.data.id); if (!resolve) return;
    pending.delete(event.data.id); resolve(event.data.result);
  });
  const call = (op, endpoint, body) => new Promise(resolve => {
    const id = ++serial; pending.set(id, resolve); parent.postMessage({id, op, endpoint, body}, '*');
  });
  window.AstrBotPluginPage = {ready:async()=>{}, apiGet:(url,body)=>call('GET',url,body), apiPost:(url,body)=>call('POST',url,body)};
}
</script>`;
const page = fs.readFileSync(path.join(root, 'pages/dashboard/index.source.html'), 'utf8')
  .replace('<body>', '<body class="authed">')
  .replace(/<script src="\/api\/plugin\/page\/bridge-sdk.js" defer><\/script>/, sdk)
  .replace(/<!-- dashboard-source: ([\w.-]+) -->/g, (_, name) => {
    const source = fs.readFileSync(path.join(root, 'pages/dashboard/source', name), 'utf8');
    return name === 'bootstrap.js' ? source.split('    (async function init()')[0] : source;
  });
const host = `<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
<body style="margin:0"><iframe src="/fixture" sandbox="allow-scripts" style="width:100%;height:100vh;border:0;display:block"></iframe><script>
addEventListener('message', async event => {
  if (event.source !== document.querySelector('iframe').contentWindow || !event.data.op) return;
  const {id, op, endpoint, body} = event.data;
  const response = await fetch('/api/' + endpoint, op === 'POST' ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {});
  event.source.postMessage({id,result:await response.json()}, '*');
});</script>`;
let state, calls, failAdd;
const nextSlot = pool => Array.from({length:10}, (_, i) => i + 1).find(slot => !pool.some(item => item.slot === slot));
const payload = () => ({...state, pool_limit:10, appearance_type:'auto',
  has_image:state.identity_mode === 'image' && state.image_pool.length > 0,
  image:state.identity_mode === 'image' ? png : undefined, wardrobe:{items:[],selection_mode:'random'},
  daily_schedule:{schedule:{date:'2026-11-19',outfits:{},events:[{id:'day',start:'08:00',end:'18:00',title:'今日安排'}]}},
});

async function main() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'selfie-identity-browser-'));
  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, 'http://fixture');
    if (url.pathname === '/fixture' || url.pathname === '/host') {
      response.setHeader('Content-Type', 'text/html; charset=utf-8'); response.end(url.pathname === '/host' ? host : page); return;
    }
    if (!url.pathname.startsWith('/api/')) { response.end(); return; }
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    calls.push({path:url.pathname, method:request.method, body}); let error = '';
    if (request.method === 'GET' && url.pathname === '/api/selfie-identity-thumbnails') {
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify({success:true,data:{items:state.image_pool.map(item => ({slot:item.slot,data_url:png}))}}));
      return;
    }
    if (request.method === 'POST' && url.pathname === '/api/selfie-reference') {
      const slot = nextSlot(state.image_pool);
      state.image_pool.push({slot,available:true,mime_type:'image/png'}); state.active_image_slot = slot;
    } else if (request.method === 'POST' && url.pathname === '/api/selfie-identity') {
      if (body.action === 'mode') state.identity_mode = body.identity_mode;
      if (body.action === 'image_select') state.active_image_slot = body.slot;
      if (body.action === 'image_remove') state.image_pool = state.image_pool.filter(item => item.slot !== body.slot);
      if (body.action === 'persona_add') {
        if (failAdd) error = 'fixture save failed';
        else state.persona_pool.push({slot:nextSlot(state.persona_pool),name:body.name,prompt:body.prompt});
      }
      if (body.action === 'persona_select') state.active_persona_slot = body.slot;
      if (body.action === 'persona_remove') state.persona_pool = state.persona_pool.filter(item => item.slot !== body.slot);
    }
    response.statusCode = error ? 400 : 200; response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify(error ? {success:false,error} : {success:true,data:payload()}));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening'); let browser, cdp;
  try {
    const port = await freePort();
    browser = spawn(findChromium(), ['--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
      '--disable-site-isolation-trials','--remote-allow-origins=*',`--remote-debugging-port=${port}`,`--user-data-dir=${profile}`,'about:blank'], {stdio:'ignore'});
    cdp = new CdpSession(await waitForCdp(port, 15000), 15000);
    await cdp.send('Page.enable'); await cdp.send('Runtime.enable'); let contextId; const exceptions = [];
    cdp.on('Runtime.executionContextCreated', ({context}) => { if (context.auxData?.isDefault) contextId = context.id; });
    cdp.on('Runtime.exceptionThrown', event => exceptions.push(event.exceptionDetails?.exception?.description || event.exceptionDetails?.text));
    const evaluate = async expression => {
      const result = await cdp.send('Runtime.evaluate', {expression,contextId,awaitPromise:true,returnByValue:true,userGesture:true});
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || 'browser exception'); return result.result?.value;
    };
    const until = async expression => {
      for (let attempt = 0; attempt < 120; attempt++) {
        if (await evaluate(expression)) return; await new Promise(resolve => setTimeout(resolve, 25));
      }
      throw new Error(`Timed out: ${expression}`);
    };
    const click = selector => evaluate(`document.querySelector(${JSON.stringify(selector)}).click(); true`);
    for (const embedded of [false, true]) for (const width of [1280, 390, 320]) {
      state = {identity_mode:'image',active_image_slot:1,active_persona_slot:1,
        image_pool:[1,2,4].map(slot => ({slot,available:true,mime_type:'image/png'})),
        persona_pool:[{slot:1,name:'小玉',prompt:'成年女性，柔和的椭圆脸、细长眉、大杏眼、小巧鼻尖、深栗棕长直发和轻薄齐刘海。'}]};
      calls = []; failAdd = false; contextId = undefined; const height = width === 1280 ? 900 : 844;
      await cdp.send('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:1,mobile:width < 500});
      await cdp.send('Page.navigate', {url:`http://127.0.0.1:${server.address().port}/${embedded ? 'host' : 'fixture'}`});
      await new Promise(resolve => setTimeout(resolve, 250)); await until("typeof navigateToTab === 'function'");
      await evaluate("navigateToTab('selfie')"); await until("$('selfieImagePoolCount').textContent === '3/10'");
      assert.equal(await evaluate("document.querySelectorAll('[data-selfie-image-slot]').length"), 10);
      await until("document.querySelector('[data-selfie-identity-thumb=\"1\"] img')?.getAttribute('src') === " + JSON.stringify(png));
      assert.equal(await evaluate("document.querySelector('[data-selfie-image-slot=\"3\"] .selfie-identity-copy').textContent.includes('空位')"), true);
      assert.equal(await evaluate("Array.from(document.querySelectorAll('#selfieImagePoolList .selfie-identity-copy')).every(node => !node.textContent.includes('image/png') && !node.textContent.includes('当前使用') && !node.textContent.includes('可用'))"), true);
      await click('[data-selfie-image-slot="2"] .selfie-image-select'); await until('SELFIE_IDENTITY_DATA.active_image_slot === 2');
      await click('[data-selfie-image-slot="2"] .selfie-image-remove'); await until("$('confirmModal').classList.contains('show')");
      assert(state.image_pool.some(item => item.slot === 2), 'removal occurred before confirmation');
      await click('#confirmProceedBtn'); await until("$('selfieImagePoolCount').textContent === '2/10'");
      const reads = calls.filter(call => call.method === 'GET' && call.path === '/api/selfie-identity').length;
      await evaluate(`{const transfer = new DataTransfer();transfer.items.add(new File([new Uint8Array([1])], 'new.png', {type:'image/png'}));$('selfieFile').files = transfer.files;}`);
      await click('#uploadSelfie'); await until("$('selfieImagePoolCount').textContent === '3/10'");
      assert.deepEqual(state.image_pool.map(item => item.slot).sort((a,b)=>a-b), [1,2,4]);
      assert.equal(calls.filter(call => call.method === 'GET' && call.path === '/api/selfie-identity').length, reads, 'upload triggered a duplicate state read');
      await click('#selfiePersonaEnabled'); await until("!$('selfiePersonaPoolPanel').hidden");
      assert.equal(await evaluate("$('selfieImagePoolPanel').hidden && $('selfiePreview').style.display === 'none' && $('useLogoWhenNoPersona').disabled"), true);
      await evaluate("$('selfiePersonaName').value='第二人设';$('selfiePersonaPrompt').value='银色短发，成年女性';");
      failAdd = true; await click('#addSelfiePersona'); await until("$('toastWrap').textContent.includes('fixture save failed')");
      assert.equal(await evaluate("$('selfiePersonaPrompt').value"), '银色短发，成年女性');
      failAdd = false; await click('#addSelfiePersona'); await until('SELFIE_IDENTITY_DATA.persona_pool.length === 2');
      await click('[data-selfie-persona-slot="2"] .selfie-persona-select'); await until('SELFIE_IDENTITY_DATA.active_persona_slot === 2');
      await click('[data-selfie-persona-slot="1"] .selfie-persona-remove'); await until("$('confirmModal').classList.contains('show')");
      await click('#confirmProceedBtn'); await until('SELFIE_IDENTITY_DATA.persona_pool.length === 1');
      await evaluate("$('selfiePersonaName').value='补位';$('selfiePersonaPrompt').value='黑色齐颈短发';");
      await click('#addSelfiePersona'); await until('SELFIE_IDENTITY_DATA.persona_pool.length === 2');
      assert(state.persona_pool.some(item => item.slot === 1 && item.name === '补位')); assert.equal(state.active_persona_slot, 2);
      await evaluate("$('toastWrap').innerHTML='';$('selfiePersonaEnabled').scrollIntoView({block:'start'});");
      assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), true);
      await click('#selfiePersonaEnabled'); await until("!$('selfieImagePoolPanel').hidden");
      assert.equal(await evaluate("$('selfieDailySchedule').hidden"), false);
      const shot = await cdp.send('Page.captureScreenshot', {format:'png'});
      fs.writeFileSync(path.join(output, `${embedded ? 'embedded' : 'standalone'}-${width}.png`), Buffer.from(shot.data, 'base64'));
      console.log(`[identity-pool-browser] ${embedded ? 'embedded' : 'standalone'} ${width}: pool operations, confirmation, modes, failure recovery, request count and layout passed`);
    }
    assert.deepEqual(exceptions, []); console.log(`[identity-pool-browser] screenshots: ${output}`);
  } finally {
    if (cdp) cdp.close(); await stopProcess(browser); await new Promise(resolve => server.close(resolve));
    fs.rmSync(profile, {recursive:true,force:true,maxRetries:3,retryDelay:100});
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
