"""Run backup download and restart-state interactions in the Node VM."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_backup_download_and_restart_interactions():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = Path(__file__).resolve().parents[1] / "pages/dashboard/source/backup_manager.js"
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
(async () => {
  for (const dashboard of [false, true]) {
    const data = Buffer.from('archive-bytes-with-many-chunks');
    const clicks = [], errors = [], blobs = [];
    const elements = new Map();
    const context = vm.createContext({
      Blob, Uint8Array, URLSearchParams, AbortController, clearTimeout,
      atob: value => Buffer.from(value, 'base64').toString('binary'),
      btoa: value => Buffer.from(value, 'binary').toString('base64'),
      isDashboardPage: () => dashboard,
      headers: () => ({'X-Selfie-Image-Token': 'test'}),
      apiErrorMessage: status => `HTTP ${status}`,
      api: async path => {
        const query = new URL(path, 'http://test').searchParams;
        const offset = Number(query.get('offset'));
        const chunk = data.subarray(offset, offset + 5);
        assert.equal(query.get('format'), 'chunk');
        if (offset) assert.equal(query.get('revision'), 'revision');
        return {data: {offset, next_offset: offset + chunk.length, size: data.length,
          revision: 'revision', eof: offset + chunk.length === data.length,
          content_base64: chunk.toString('base64'), filename:'backup.zip'}};
      },
      fetch: async (path, options) => {
        assert(path.endsWith('?format=file'));
        assert.equal(options.headers['X-Selfie-Image-Token'], 'test');
        return {ok:true, blob:async () => new Blob([data])};
      },
      URL: {createObjectURL: blob => {blobs.push(blob); return 'blob:test';}, revokeObjectURL: () => {}},
      document: {body:{appendChild:() => {}}, createElement:() => ({click(){clicks.push(true);}, remove(){}})},
      showToast: (message, kind) => {if(kind === 'bad') errors.push([message,kind]);},
      waitForDashboardBridge: async () => ({}),
      setTimeout: (callback, delay) => delay >= 30000 ? setTimeout(callback, delay) : callback(),
      $: id => {
        if(!elements.has(id)) elements.set(id, {querySelectorAll:() => [], addEventListener:() => {}});
        return elements.get(id);
      },
      escapeHtml: String,
    });
    vm.runInContext(source + '\nglobalThis.testHandlers={downloadBackup,waitBackupTask,renderBackupList};', context);
    await context.testHandlers.downloadBackup('backup-id');
    assert.equal(errors.length, 0);
    assert.equal(clicks.length, 1);
    assert.equal(await blobs[0].text(), data.toString());
    context.testHandlers.renderBackupList({items:[],max:5,can_create:false,restore_pending:true});
    assert.equal(elements.get('createBackupBtn').disabled,true);
    assert.equal(elements.get('uploadBackupBtn').disabled,true);
    context.api=async () => ({data:{status:'cancelled',message:'cancelled'}});
    await assert.rejects(context.testHandlers.waitBackupTask('task'),/cancelled/);
    if (dashboard) {
      context.api=async () => ({data:{offset:0,next_offset:99,size:5,revision:'r',content_base64:'YQ==',eof:false}});
      await context.testHandlers.downloadBackup('backup-id');
      assert.equal(clicks.length,1);
      assert.equal(errors.at(-1)[1],'bad');
      for (const mode of ['oversize','changed-size']) {
        let chunks=0;
        context.api=async () => {
          chunks+=1;
          assert(chunks<3,'invalid size was accepted for multiple chunks');
          return {data:{offset:chunks-1,next_offset:chunks,
            size:mode==='oversize'?129*1024*1024:chunks===1?2:3,
            revision:'r',content_base64:'YQ==',eof:false}};
        };
        await context.testHandlers.downloadBackup('backup-id');
        assert.equal(clicks.length,1);
        assert(chunks<3);
        assert.equal(errors.at(-1)[1],'bad');
      }
    }
  }
})().catch(error => {process.stderr.write(error.stack+'\n');process.exitCode=1;});
"""
    result = subprocess.run([node, "-", str(source)], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_backup_manager_deduplicates_and_caches_metadata_requests():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = Path(__file__).resolve().parents[1] / "pages/dashboard/source/backup_manager.js"
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const elements = new Map();
const element = () => ({innerHTML:'', textContent:'', hidden:false, disabled:false,
  querySelectorAll:() => [], addEventListener:() => {}, focus:() => {}});
for (const id of ['backupOptions','backupList','backupLimitStatus','createBackupBtn','uploadBackupBtn']) elements.set(id, element());
let now = 1000;
const calls = [];
const context = vm.createContext({
  Date: {now: () => now},
  Promise, setTimeout, clearTimeout,
  document: {contains:() => false, activeElement:null},
  $: id => elements.get(id) || element(),
  escapeHtml: String,
  showToast: () => {},
  api: async path => {
    calls.push(path);
    if (path === '/api/backups/options') return {data:{items:[{id:'config',label:'配置',required:true}]}};
    assert.equal(path, '/api/backups');
    return {data:{items:[],max:5,can_create:true,restore_pending:false}};
  },
});
vm.runInContext(source + '\nglobalThis.handlers={loadBackupManager};', context);
(async () => {
  await Promise.all([context.handlers.loadBackupManager(), context.handlers.loadBackupManager()]);
  assert.deepEqual(calls, ['/api/backups/options', '/api/backups']);
  await context.handlers.loadBackupManager();
  assert.deepEqual(calls, ['/api/backups/options', '/api/backups']);
  now += 30001;
  await context.handlers.loadBackupManager();
  assert.deepEqual(calls, ['/api/backups/options', '/api/backups', '/api/backups']);
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-", str(source)], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_archive_upload_slices_replay_and_cancel():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = Path(__file__).resolve().parents[1] / "pages/dashboard/source/backup_manager.js"
    script = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(process.argv[2],'utf8');
async function scenario(mode) {
  const data=Buffer.alloc(2*1024*1024+17, 73);
  const reads=[], stored=[], calls=[], discarded=[];
  let committed=false, loseReply=mode==='retry', cancelStarted=false;
  const context=vm.createContext({
    Uint8Array, AbortController, setTimeout, clearTimeout,
    btoa:value=>Buffer.from(value,'binary').toString('base64'),
    $:()=>({addEventListener(){}}),
    api:async (path,options)=>{
      const body=JSON.parse(options.body); calls.push(path);
      if(path.endsWith('/uploads'))return {data:{id:'transfer',chunk_size:1024*1024}};
      if(path.endsWith('/discard')){discarded.push(true); return {data:{discarded:true}};}
      if(path.endsWith('/commit')){committed=true; return {data:{imported:true}};}
      assert(path.endsWith('/chunk'));
      const chunk=Buffer.from(body.content_base64,'base64');
      assert(chunk.length<=1024*1024);
      assert.match(body.crc32,/^[a-f0-9]{8}$/);
      if(mode==='cancel' && !cancelStarted) {
        cancelStarted=true;
        context.handlers.cancelArchiveUpload();
        await new Promise(resolve=>setTimeout(resolve,1));
      }
      if(!stored[body.offset])stored[body.offset]=chunk;
      else assert.deepEqual(stored[body.offset],chunk);
      if(loseReply){loseReply=false; throw new Error('lost response');}
      return {data:{next_offset:body.offset+chunk.length}};
    }
  });
  vm.runInContext(source+'\nglobalThis.handlers={uploadArchiveFile,cancelArchiveUpload,archiveChunkCRC};',context);
  assert.equal(context.handlers.archiveChunkCRC(new Uint8Array(Buffer.from('123456789'))),'cbf43926');
  const file={size:data.length,name:'test.zip',arrayBuffer(){throw new Error('whole-file read');},
    slice(start,end){reads.push([start,end]);return {async arrayBuffer(){return data.subarray(start,end);}};}};
  if(mode==='cancel')await assert.rejects(context.handlers.uploadArchiveFile(file,'wardrobe'),/取消/);
  else {
    const result=await context.handlers.uploadArchiveFile(file,'wardrobe');
    assert.equal(result.data.imported,true);
    assert.equal(committed,true);
    assert.deepEqual(Buffer.concat(Object.values(stored)),data);
    assert.equal(reads.length,3);
    assert.equal(calls.filter(p=>p.endsWith('/chunk')).length,mode==='retry'?4:3);
  }
  assert.equal(committed,mode!=='cancel');
  assert.equal(discarded.length,1);
}
(async()=>{for(const mode of ['normal','retry','cancel'])await scenario(mode);})()
  .catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
"""
    result = subprocess.run([node, "-", str(source)], input=script, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_archive_timeout_aborts_transport_and_external_cancellation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = Path(__file__).resolve().parents[1] / "pages/dashboard/source/backup_manager.js"
    script = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const context=vm.createContext({
  AbortController,setTimeout,clearTimeout,
  $:()=>({addEventListener(){}}),
  api:(_path,options)=>{context.transportSignal=options.signal;return new Promise(()=>{});},
});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8')+'\nglobalThis.request=archiveRequest;',context);
(async()=>{
  await assert.rejects(context.request('/timeout',{},null,5),/超时/);
  assert.equal(context.transportSignal.aborted,true);
  const controller=new AbortController();
  const pending=context.request('/cancel',{},controller.signal);
  controller.abort();
  await assert.rejects(pending,/取消/);
  assert.equal(context.transportSignal.aborted,true);
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
"""
    result = subprocess.run([node, "-", str(source)], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", [
    "file_headers", "file_body", "file_error_body", "bridge_discovery", "bridge_ready",
    "bridge_download", "bridge_chunk", "bridge_partial",
])
def test_download_timeout_releases_export_and_allows_retry(mode):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = Path(__file__).resolve().parents[1] / "pages/dashboard/source/backup_manager.js"
    script = r"""
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const mode=process.argv[3], pending=[], never=()=>new Promise(resolve=>pending.push(resolve));
let stalled=true, transportSignal, discarded=0, saved=0, bridgeCalls=0, chunkCalls=0;
const errors=[], calls=[];
const bridge={
  ready:()=>stalled&&mode==='bridge_ready'?never():Promise.resolve(),
  download:()=>{bridgeCalls++;return stalled&&mode==='bridge_download'?never():Promise.resolve();},
};
if(mode==='bridge_chunk'||mode==='bridge_partial')delete bridge.download;
const context=vm.createContext({
  Blob,Uint8Array,AbortController,clearTimeout,
  setTimeout:(callback,ms)=>setTimeout(callback,ms>=30000?10:ms===2000?0:ms),
  atob:value=>Buffer.from(value,'base64').toString('binary'),
  isDashboardPage:()=>mode.startsWith('bridge_'),headers:()=>({'X-Selfie-Image-Token':'fixture'}),
  bridgeEndpoint:path=>path.replace(/^\/api\//,''),
  apiErrorMessage:()=> 'HTTP failure',
  waitForDashboardBridge:()=>stalled&&mode==='bridge_discovery'?never():Promise.resolve(bridge),
  api:async (path,options)=>{
    calls.push({path,options});
    if(path.endsWith('/wardrobe-export'))return {data:{id:'export-fixture',filename:'clothes.zip'}};
    if(path.endsWith('/discard')){discarded++;return {data:{discarded:true}};}
    assert(path.includes('/download?format=chunk'));
    transportSignal=options?.signal;
    chunkCalls++;
    const offset=Number(new URL(path,'http://fixture').searchParams.get('offset'));
    if(mode==='bridge_partial'&&offset===0)return {data:{revision:'r',size:6,offset:0,next_offset:3,eof:false,content_base64:'emlw'}};
    if(stalled)return never();
    if(mode==='bridge_partial')return {data:{revision:'r',size:6,offset:3,next_offset:6,eof:true,content_base64:'emlw'}};
    return {data:{revision:'r',size:3,offset:0,next_offset:3,eof:true,content_base64:'emlw'}};
  },
  fetch:async (path,options)=>{
    assert(path.endsWith('/download?format=file'));
    assert.equal(options.headers['X-Selfie-Image-Token'],'fixture');
    transportSignal=options.signal;
    if(stalled&&mode==='file_headers')return never();
    return {ok:!(stalled&&mode==='file_error_body'),
      blob:()=>stalled?never():Promise.resolve(new Blob(['zip'])),json:never};
  },
  URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL:()=>{}},
  document:{body:{appendChild(){}},createElement:()=>({click(){saved++;},remove(){}})},
  showToast:(message,kind)=>{if(kind==='bad')errors.push(message);},
  $:()=>({addEventListener(){}}),
});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8')+
  '\nglobalThis.handlers={exportWardrobe,busy:()=>BACKUP_BUSY};',context);
(async()=>{
  const result=await Promise.race([
    context.handlers.exportWardrobe().then(()=> 'settled'),
    new Promise(resolve=>setTimeout(()=>resolve('stuck'),100)),
  ]);
  assert.equal(result,'settled','stalled '+mode+' left export pending');
  assert.equal(context.handlers.busy(),false);
  assert.equal(discarded,1);
  assert.equal(saved,0);
  assert.equal(errors.length,1);
  assert.match(errors[0],/超时/);
  if(mode.startsWith('file_')||mode==='bridge_chunk'||mode==='bridge_partial')assert.equal(transportSignal.aborted,true);
  const timedOutChunkCalls=chunkCalls;
  for(const resolve of pending.splice(0))resolve(mode==='bridge_discovery'?bridge:{
    ok:true,blob:async()=>new Blob(['late']),data:{revision:'r',size:6,offset:3,next_offset:6,eof:true,content_base64:'emlw'},
  });
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(saved,0,'late response saved a timed-out download');
  assert.equal(discarded,1);
  assert.equal(chunkCalls,timedOutChunkCalls,'late response continued a timed-out download');
  assert.equal(errors.length,1);
  const firstCalls=calls.length;
  stalled=false;
  await context.handlers.exportWardrobe();
  assert.equal(context.handlers.busy(),false);
  assert.equal(discarded,2);
  assert.equal(errors.length,1);
  assert.equal(saved,mode==='bridge_chunk'||mode==='bridge_partial'||mode.startsWith('file_')?1:0);
  assert.equal(bridgeCalls,mode==='bridge_download'?2:mode.startsWith('bridge_')&&!['bridge_chunk','bridge_partial'].includes(mode)?1:0);
  assert(calls.length>firstCalls);
  assert.equal(calls.filter(call=>call.path.endsWith('/wardrobe-export')).length,2);
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
"""
    result = subprocess.run([node, "-", str(source), mode], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
