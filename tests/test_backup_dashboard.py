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
      Blob, Uint8Array, URLSearchParams,
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
      setTimeout: callback => {callback();},
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
