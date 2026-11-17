#!/usr/bin/env node
'use strict';

// Exercise the actual backup controls in Chromium, including a restricted iframe.
// All requests use disposable fixtures; no plugin data is read or restored.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const {spawn, execFileSync} = require('node:child_process');
const {once} = require('node:events');
const {CdpSession, findChromium, freePort, waitForCdp, stopProcess} = require('./mobile_smoke');
const ROOT = path.resolve(__dirname, '..');
const source = name => fs.readFileSync(path.join(ROOT, 'pages/dashboard/source', name), 'utf8');
const output = process.env.BACKUP_SMOKE_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), 'selfie-backup-smoke-'));
const archive = execFileSync('python', ['-c', 'import io,sys,zipfile; stream=io.BytesIO(); z=zipfile.ZipFile(stream,"w"); z.writestr("fixture.txt","backup smoke fixture"); z.close(); sys.stdout.buffer.write(stream.getvalue())']);
const fixtureToken = 'backup-smoke-token';
const options = [{id:'config', label:'核心配置', description:'配置与渠道', required:true},
  {id:'wardrobe', label:'衣橱', description:'衣服与配件'}];
options.push(...['形象资料','今日日程','预设与 COS','画布与资产集合','任务状态','用量统计','生成记录','图片缓存','引用媒体']
  .map((label,index)=>({id:`optional${index}`,label,description:'可选恢复数据'})));
const rows = [{id:'bkp_fixture', filename:'bkp_fixture.zip', selected:['config','wardrobe'], size:2048, file_count:2}];
let calls = [], pending = false, deleted = false, rejectRestore = false;
let visibleRows = rows, downloadMode = 'normal', releaseChunk = null, holdChunk = false;
const longFilename = 'backup_' + 'long_filename_'.repeat(12) + '.zip';

const sdk = `
  if (window.parent !== window) {
    let serial=0; const requests=new Map();
    window.addEventListener('message', event => {
      if(event.source!==window.parent)return;
      const item=requests.get(event.data.id); if(!item)return;
      requests.delete(event.data.id);
      event.data.error ? item.reject(new Error(event.data.error)) : item.resolve(event.data.result);
    });
    const request=(op,endpoint,body,filename)=>new Promise((resolve,reject)=>{
      const id=++serial; requests.set(id,{resolve,reject});
      parent.postMessage({id,op,endpoint,body,filename},'*');
    });
    window.AstrBotPluginPage={ready:async()=>{}, apiGet:(endpoint,body)=>request('GET',endpoint,body),
      apiPost:(endpoint,body)=>request('POST',endpoint,body),
      download:(endpoint,body,filename)=>request('download',endpoint,body,filename)};
    if(location.search.includes('legacy'))delete window.AstrBotPluginPage.download;
  }
`;
const page = fs.readFileSync(path.join(ROOT, 'pages/dashboard/index.source.html'), 'utf8')
  .replace('<body>', '<body class="authed">')
  .replace(/<script src="\/api\/plugin\/page\/bridge-sdk.js" defer><\/script>/, `<script>${sdk}</script>`)
  .replace(/<!-- dashboard-source: ([\w.-]+) -->/g, (_, name) => {
    if (name === 'bootstrap.js') return source(name).split('    (async function init()')[0];
    return source(name);
  });
const host = `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
  <body style="margin:0"><iframe src="/fixture" sandbox="allow-scripts" style="display:block;width:100%;height:100vh;border:0"></iframe><script>
  window.addEventListener('message',async event=>{
    if(event.source!==document.querySelector('iframe').contentWindow)return;
    const {id,op,endpoint,body,filename}=event.data;
    if(!op)return;
    try {
      const query=new URLSearchParams(body||{});
      const response=await fetch('/api/'+endpoint+(op==='POST'?'':'?'+query),op==='POST'?{method:'POST',headers:{'Content-Type':'application/json','X-Selfie-Image-Token':'${fixtureToken}'},body:JSON.stringify(body)}:{headers:{'X-Selfie-Image-Token':'${fixtureToken}'}});
      if(!response.ok)throw new Error((await response.json()).error || 'download failed');
      let result;
      if(op==='download') {
        const blob=await response.blob(),url=URL.createObjectURL(blob);
        const link=document.createElement('a');link.href=url;link.download=filename;link.click();
        setTimeout(()=>URL.revokeObjectURL(url),2000);result={};
      } else result=await response.json();
      event.source.postMessage({id,result},'*');
    } catch(error) {event.source.postMessage({id,error:error.message},'*');}
  });</script></body></html>`;

async function main() {
  fs.mkdirSync(output, {recursive:true});
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'selfie-backup-browser-'));
  const server = http.createServer(async (request,response) => {
    const url = new URL(request.url, 'http://fixture');
    if (['/fixture','/host','/legacy'].includes(url.pathname)) {
      response.setHeader('Content-Type', 'text/html; charset=utf-8');
      response.end(url.pathname === '/legacy'
        ? host.replace('src="/fixture"','src="/fixture?legacy=1"').replace('sandbox="allow-scripts"','sandbox="allow-scripts allow-downloads"')
        : url.pathname === '/host' ? host : page); return;
    }
    if (!url.pathname.startsWith('/api/')) {response.end(); return;}
    if(request.headers['x-selfie-image-token']!==fixtureToken) {
      response.statusCode=401;response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:false,error:'fixture unauthorized'}));return;
    }
    const chunks=[]; for await (const chunk of request) chunks.push(chunk);
    const body=chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
    calls.push({path:url.pathname, method:request.method, body, format:url.searchParams.get('format'), offset:url.searchParams.get('offset')});
    if (url.pathname.endsWith('/download')) {
      if(downloadMode==='failure') {
        response.statusCode=503;response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:false,error:'fixture download unavailable'}));return;
      }
      if(url.searchParams.get('format')==='chunk') {
        const offset=Number(url.searchParams.get('offset')),chunk=archive.subarray(offset,offset+48);
        assert.equal(url.searchParams.get('revision'),offset?'fixture-revision':'');
        if(downloadMode==='slow')await new Promise(resolve=>setTimeout(resolve,250));
        response.setHeader('Content-Type','application/json');
        response.end(JSON.stringify({success:true,data:{offset,next_offset:offset+chunk.length,size:archive.length,
          revision:'fixture-revision',content_base64:chunk.toString('base64'),eof:offset+chunk.length===archive.length,filename:'bkp_fixture.zip'}}));return;
      }
      response.setHeader('Content-Type','application/zip');
      response.setHeader('Content-Disposition','attachment; filename="bkp_fixture.zip"');
      if(downloadMode==='slow') {
        response.write(archive.subarray(0,8));
        await new Promise(resolve=>setTimeout(resolve,250));
        response.end(archive.subarray(8));return;
      }
      response.end(archive); return;
    }
    let data={};
    if(url.pathname==='/api/backups/options')data={items:options};
    else if(url.pathname==='/api/backups')data={items:deleted?[]:visibleRows, max:5, can_create:visibleRows.length<5, restore_pending:pending};
    else if(url.pathname==='/api/backups/delete'){deleted=true;data={items:[],max:5};}
    else if(url.pathname.endsWith('/restore') || url.pathname.endsWith('/commit')) {
      if(rejectRestore){response.statusCode=400;response.setHeader('Content-Type','application/json');response.end(JSON.stringify({success:false,error:'fixture restore error'}));return;}
      pending=true;data={task_id:'restore-fixture'};
    } else if(url.pathname.includes('/tasks/'))data={status:'completed',percent:100};
    else if(url.pathname.endsWith('/uploads'))data={id:'upload-fixture',chunk_size:1048576};
    else if(url.pathname.endsWith('/chunk')) {
      if(holdChunk)await new Promise(resolve=>{releaseChunk=resolve;});
      data={next_offset:body.offset+Buffer.from(body.content_base64,'base64').length};
    }
    response.setHeader('Content-Type','application/json'); response.end(JSON.stringify({success:true,data}));
  });
  server.listen(0,'127.0.0.1'); await once(server,'listening');
  let browser, cdp;
  try {
    const port=await freePort();
    browser=spawn(findChromium(),['--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--disable-site-isolation-trials',
      '--remote-allow-origins=*',`--remote-debugging-port=${port}`,`--user-data-dir=${profile}`,'about:blank'],{stdio:'ignore'});
    cdp=new CdpSession(await waitForCdp(port,15000),15000);
    const exceptions=[];
    cdp.on('Runtime.exceptionThrown', event=>exceptions.push(event.exceptionDetails?.exception?.description || event.exceptionDetails?.text));
    await cdp.send('Page.enable'); await cdp.send('Runtime.enable');
    await cdp.send('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:output});
    let contextId;
    cdp.on('Runtime.executionContextCreated',({context})=>{
      if(context.auxData?.isDefault)contextId=context.id;
    });
    const evaluate=async expression=>{
      const result=await cdp.send('Runtime.evaluate',{expression,contextId,awaitPromise:true,returnByValue:true,userGesture:true});
      if(result.exceptionDetails)throw new Error(result.exceptionDetails.exception?.description || 'browser exception');
      return result.result?.value;
    };
    const until=async expression=>{
      for(let i=0;i<100;i++) {if(await evaluate(expression))return;await new Promise(resolve=>setTimeout(resolve,30));}
      throw new Error('Timed out: '+expression+'; '+await evaluate('document.body.innerText.slice(-1200)'));
    };
    const click=async selector=>{
      const point=await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.scrollIntoView({block:'center'});const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
      const frame=await cdp.send('Runtime.evaluate',{expression:"(()=>{const r=document.querySelector('iframe')?.getBoundingClientRect();return r?{x:r.x,y:r.y}:{x:0,y:0};})()",returnByValue:true});
      point.x+=frame.result.value.x;point.y+=frame.result.value.y;
      await cdp.send('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});
      await cdp.send('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',clickCount:1});
    };
    for(const mode of ['standalone','host','legacy'])for(const width of [1280,390,320]) {
      const embedded=mode!=='standalone';
      calls=[];pending=false;deleted=false;rejectRestore=false;contextId=undefined;visibleRows=rows;downloadMode='normal';
      const height=width===1280?900:width===320?568:844;
      await cdp.send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<500});
      await cdp.send('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/${embedded?mode:'fixture'}`});
      await new Promise(resolve=>setTimeout(resolve,350));
      await until("typeof navigateToTab === 'function'");
      await evaluate(`AUTH_TOKEN=${JSON.stringify(fixtureToken)}`);
      const hostLayout=await cdp.send('Runtime.evaluate',{expression:'document.documentElement.scrollWidth <= innerWidth+1',returnByValue:true});
      assert.equal(hostLayout.result.value,true,'fixture host overflows horizontally');
      if(embedded)assert.equal(await evaluate("window.confirm('sandbox probe')"),false);
      await evaluate("navigateToTab('backup')"); await until("document.querySelector('.backup-restore')");
      assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth+1'),true);
      const downloadDir=path.join(output,`${mode}-${width}-downloads`);
      fs.mkdirSync(downloadDir,{recursive:true});
      await cdp.send('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloadDir});
      const downloaded=()=>fs.readdirSync(downloadDir).filter(name=>name.endsWith('.zip'));
      const previousDownloads=downloaded().length;
      downloadMode='failure';await click('.backup-download');
      await until("[...document.querySelectorAll('.toast')].some(el=>el.textContent.includes('fixture download unavailable'))");
      assert.equal(downloaded().length,previousDownloads);
      downloadMode='slow';await evaluate("$('toastWrap').innerHTML=''");await click('.backup-download');
      await until("[...document.querySelectorAll('.toast')].some(el=>el.textContent.includes('已开始下载'))");
      assert(calls.some(call=>call.path.endsWith('/download')&&call.format===(mode==='legacy'?'chunk':'file')));
      if(mode==='legacy')assert.deepEqual(calls.filter(call=>call.path.endsWith('/download')).slice(1).map(call=>Number(call.offset)),Array.from({length:Math.ceil(archive.length/48)},(_,i)=>i*48));
      for(let i=0;i<100&&downloaded().length===previousDownloads;i++)await new Promise(resolve=>setTimeout(resolve,30));
      assert.equal(downloaded().length,previousDownloads+1,'download did not leave the sandbox');
      assert(downloaded().every(name=>fs.readFileSync(path.join(downloadDir,name)).equals(archive)));
      execFileSync('python',['-c','import sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); assert z.testzip() is None; assert z.read("fixture.txt")==b"backup smoke fixture"',path.join(downloadDir,downloaded().at(-1))]);
      visibleRows=Array.from({length:5},(_,index)=>({...rows[0],id:`full_${index}`,filename:`${index}_${longFilename}`}));
      await evaluate('loadBackupManager()');
      assert.equal(await evaluate("$('createBackupBtn').disabled"),true);
      assert.equal(await evaluate("document.querySelectorAll('.backup-row').length"),5);
      assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth+1'),true);
      await click('.backup-restore');
      assert.equal(await evaluate("$('backupRestoreModal').querySelector('.modal').scrollWidth <= $('backupRestoreModal').querySelector('.modal').clientWidth+1"),true);
      await click('#backupRestoreCancel');
      visibleRows=rows;await evaluate('loadBackupManager()');
      await click('.backup-delete'); await until("$('confirmModal').classList.contains('show')");
      await click('#confirmCancelBtn'); await until("!BACKUP_BUSY");
      assert(!calls.some(call=>call.path.endsWith('/delete')));
      await click('.backup-restore');
      assert.equal(await evaluate("$('backupRestoreModal').classList.contains('show')"),true);
      assert.equal(await evaluate("$('backupRestoreSource').value"),'bkp_fixture');
      await cdp.send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape'});
      assert.equal(await evaluate("$('backupRestoreModal').classList.contains('show')"),false);
      await click('.backup-restore');
      await evaluate("$('backupRestoreOptions').querySelectorAll('input').forEach(input=>{input.checked=false;})");
      await click('#backupRestoreSubmit');
      assert.equal(await evaluate("$('backupRestoreStatus').textContent"),'请至少勾选一项恢复内容');
      await evaluate("$('backupRestoreOptions').querySelectorAll('input').forEach(input=>{input.checked=true;})");
      await click('#backupRestoreOptions input[value="config"]');
      await click('#backupRestoreSubmit'); await until("$('confirmModal').classList.contains('show')");
      await click('#confirmCancelBtn'); await until('!BACKUP_BUSY');
      assert(!calls.some(call=>call.path.endsWith('/restore')));
      rejectRestore=true;
      await click('#backupRestoreSubmit'); await click('#confirmProceedBtn');
      await until('!BACKUP_BUSY');
      assert.equal(await evaluate("document.body.textContent.includes('fixture restore error')"),true);
      rejectRestore=false;
      await click('.backup-restore');
      await click('#backupRestoreOptions input[value="config"]');
      await click('#backupRestoreSubmit'); await click('#confirmProceedBtn'); await until('BACKUP_RESTORE_PENDING && !BACKUP_BUSY');
      assert.deepEqual(calls.filter(call=>call.path.endsWith('/restore')).at(-1).body,{selected:['wardrobe']});
      assert.equal(await evaluate("document.querySelector('.backup-restore').disabled"),true);
      pending=false;await evaluate('loadBackupManager()');
      await click('#uploadBackupBtn');
      await click('#backupRestoreSubmit');
      assert.equal(await evaluate("$('backupRestoreStatus').textContent"),'请选择备份文件');
      await evaluate(`(()=>{const transfer=new DataTransfer();transfer.items.add(new File(['fixture-upload'],${JSON.stringify(longFilename)},{type:'application/zip'}));$('backupUploadFile').files=transfer.files;})()`);
      await evaluate(`$('backupUploadFile').dispatchEvent(new Event('change')); $('backupRestoreOptions').querySelectorAll('input').forEach(input=>{input.checked=input.value==='wardrobe';}); $('backupRestoreOptions').closest('.modal-body').scrollTop=0; $('toastWrap').innerHTML='';`);
      const bounds=await evaluate(`(()=>{const modal=$('backupRestoreModal').querySelector('.modal'),r=modal.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,overflow:modal.scrollWidth>modal.clientWidth+1};})()`);
      assert(bounds.left>=0&&bounds.right<=width+1&&bounds.top>=0&&bounds.bottom<=height&&!bounds.overflow,JSON.stringify(bounds));
      assert.equal(await evaluate("[...$('backupRestoreOptions').querySelectorAll('input')].every(el=>el.getBoundingClientRect().width<=20)"),true);
      const shot=await cdp.send('Page.captureScreenshot',{format:'png'});
      fs.writeFileSync(path.join(output,`${mode}-${width}.png`),Buffer.from(shot.data,'base64'));
      holdChunk=true;
      await click('#backupRestoreSubmit');await click('#confirmProceedBtn');
      for(let i=0;i<100&&!releaseChunk;i++)await new Promise(resolve=>setTimeout(resolve,20));
      assert(releaseChunk,'upload chunk never arrived');
      await evaluate('window.cancelOperation=ARCHIVE_UPLOAD');
      await click('#backupCancelUpload');
      assert.equal(await evaluate('window.cancelOperation?.controller.signal.aborted'),true,
        await evaluate(`(()=>{const el=$('backupCancelUpload'),r=el.getBoundingClientRect();return JSON.stringify({rect:{x:r.x,y:r.y,width:r.width,height:r.height},hit:document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)?.outerHTML,hidden:el.hidden,committing:ARCHIVE_UPLOAD?.committing});})()`));
      holdChunk=false;releaseChunk();releaseChunk=null;
      await until('!BACKUP_BUSY && !ARCHIVE_UPLOAD');
      assert.equal(calls.filter(call=>call.path.endsWith('/commit')).length,0);
      assert(calls.some(call=>call.path.endsWith('/discard')));
      await click('#uploadBackupBtn');
      await evaluate(`(()=>{const transfer=new DataTransfer();transfer.items.add(new File(['fixture-upload'],${JSON.stringify(longFilename)},{type:'application/zip'}));$('backupUploadFile').files=transfer.files;$('backupUploadFile').dispatchEvent(new Event('change'));$('backupRestoreOptions').querySelectorAll('input').forEach(input=>{input.checked=input.value==='wardrobe';});})()`);
      await click('#backupRestoreSubmit'); await click('#confirmProceedBtn'); await until('BACKUP_RESTORE_PENDING && !BACKUP_BUSY');
      assert.deepEqual(calls.find(call=>call.path.endsWith('/commit')).body,{selected:['wardrobe']});
      assert.equal(calls.filter(call=>call.path.endsWith('/commit')).length,1);
      pending=false;await evaluate('loadBackupManager()');
      await click('.backup-delete'); await click('#confirmProceedBtn'); await until('!BACKUP_BUSY');
      assert.equal(calls.filter(call=>call.path.endsWith('/delete')).length,1);
      assert.equal(await evaluate("$('backupList').textContent.includes('暂无备份')"),true);
      console.log(`[backup-smoke] ${mode} ${width}x${height}: valid ZIP, download failure/retry/delay, restore, upload cancel/retry, full/empty list and long filename passed`);
    }
    assert.deepEqual(exceptions,[]);
    console.log(`[backup-smoke] screenshots: ${output}`);
  } finally {
    if(releaseChunk)releaseChunk();
    if(cdp)cdp.close();await stopProcess(browser);
    await new Promise(resolve=>server.close(resolve));
    fs.rmSync(profile,{recursive:true,force:true,maxRetries:3,retryDelay:100});
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
