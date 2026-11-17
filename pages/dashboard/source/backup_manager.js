    let BACKUP_OPTIONS = [];
    let BACKUP_BUSY = false;
    let BACKUP_RESTORE_PENDING = false;
    let BACKUP_LOAD_SEQ = 0;
    let BACKUP_MUTATION_SEQ = 0;
    let BACKUP_LOAD_REQUEST = null;
    let BACKUP_OPTIONS_CACHE = null;
    let BACKUP_LIST_CACHE = null;
    let BACKUP_LIST_CACHE_AT = 0;
    const BACKUP_PERSIST_NAMESPACE = 'backup-manager';
    const BACKUP_LIST_CACHE_TTL_MS = 30 * 1000;
    let BACKUP_LIST = [];
    let BACKUP_CAN_UPLOAD = true;
    let BACKUP_RESTORE_FOCUS = null;
    let ARCHIVE_UPLOAD = null;
    const ARCHIVE_CHUNK_BYTES = 1024 * 1024;
    const ARCHIVE_CRC_TABLE = Array.from({length:256}, (_, index) => {
      let value = index;
      for (let bit = 0; bit < 8; bit += 1) value = (value & 1) ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
      return value >>> 0;
    });

    function formatBackupBytes(value) {
      let size = Number(value || 0);
      if (!Number.isFinite(size) || size < 0) size = 0;
      const units = ['B', 'KB', 'MB', 'GB'];
      let index = 0;
      while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
      return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
    }

    function bytesToBase64(bytes) {
      let binary = '';
      const chunk = 0x8000;
      for (let index = 0; index < bytes.length; index += chunk) {
        binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
      }
      return btoa(binary);
    }

    function archiveChunkCRC(bytes) {
      let value = 0xffffffff;
      for (const byte of bytes) value = ARCHIVE_CRC_TABLE[(value ^ byte) & 255] ^ (value >>> 8);
      return ((value ^ 0xffffffff) >>> 0).toString(16).padStart(8, '0');
    }

    function archiveUploadCancelled() {
      const error = new Error('上传已取消');
      error.name = 'AbortError';
      return error;
    }

    async function archiveTimedRequest(request, signal, timeout = 30000, timeoutMessage = '归档请求超时，请重试') {
      if (signal?.aborted) throw archiveUploadCancelled();
      const controller = new AbortController();
      let timer;
      let abort;
      try {
        const response = await Promise.race([
          request(controller.signal),
          new Promise((_, reject) => {
            timer = setTimeout(() => {
              reject(new Error(timeoutMessage));
              controller.abort();
            }, timeout);
            abort = () => {
              reject(archiveUploadCancelled());
              controller.abort();
            };
            signal?.addEventListener('abort', abort, {once:true});
            if (signal?.aborted) abort();
          })
        ]);
        if (signal?.aborted) throw archiveUploadCancelled();
        return response;
      } finally {
        clearTimeout(timer);
        if (abort) signal?.removeEventListener('abort', abort);
      }
    }

    async function archiveRequest(path, body, signal, timeout = 30000) {
      return archiveTimedRequest(requestSignal => api(path, {
        method:'POST', body:JSON.stringify(body), signal:requestSignal,
      }), signal, timeout);
    }

    async function uploadArchiveFile(file, kind, restoreOptions = {}) {
      if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > 128 * ARCHIVE_CHUNK_BYTES) throw new Error('归档须为非空文件，且不得超过 128MB');
      const operation = {controller:new AbortController(), committing:false};
      ARCHIVE_UPLOAD = operation;
      let transferId = '';
      try {
        setBackupProgress(0, '正在准备上传');
        const started = await archiveRequest('/api/archive-transfers/uploads', {kind, filename:file.name, size:file.size}, operation.controller.signal);
        const session = started.data || {};
        transferId = session.id;
        if (!transferId || !Number.isInteger(session.chunk_size) || session.chunk_size <= 0 || session.chunk_size > ARCHIVE_CHUNK_BYTES) throw new Error('归档上传响应无效');
        for (let offset = 0; offset < file.size;) {
          if (operation.controller.signal.aborted) throw archiveUploadCancelled();
          const bytes = new Uint8Array(await file.slice(offset, offset + session.chunk_size).arrayBuffer());
          if (!bytes.length) throw new Error('归档读取中断');
          const payload = {offset, content_base64:bytesToBase64(bytes), crc32:archiveChunkCRC(bytes)};
          let response;
          for (let attempt = 0; attempt < 2; attempt += 1) {
            try {
              response = await archiveRequest(`/api/archive-transfers/${encodeURIComponent(transferId)}/chunk`, payload, operation.controller.signal);
              break;
            } catch (error) {
              if (operation.controller.signal.aborted || attempt) throw error;
            }
          }
          if (Number(response.data?.next_offset) !== offset + bytes.length) throw new Error('归档上传偏移无效');
          offset += bytes.length;
          setBackupProgress(offset / file.size * 85, '正在上传');
        }
        if (operation.controller.signal.aborted) throw archiveUploadCancelled();
        operation.committing = true;
        setBackupProgress(90, kind === 'backup' ? '正在校验并准备恢复' : '正在导入衣橱');
        return await archiveRequest(`/api/archive-transfers/${encodeURIComponent(transferId)}/commit`, restoreOptions, null, 300000);
      } finally {
        ARCHIVE_UPLOAD = null;
        if (transferId) {
          try { await archiveRequest(`/api/archive-transfers/${encodeURIComponent(transferId)}/discard`, {}, null, 5000); }
          catch (_) { /* The server also expires disconnected transfers. */ }
        }
      }
    }

    function cancelArchiveUpload() {
      if (ARCHIVE_UPLOAD && !ARCHIVE_UPLOAD.committing) ARCHIVE_UPLOAD.controller.abort();
    }

    function setBackupProgress(percent, label, visible = true) {
      const value = Math.max(0, Math.min(100, Number(percent || 0)));
      for (const prefix of ['backup', 'wardrobeArchive']) {
        const wrap = $(`${prefix}ProgressWrap`);
        if (!wrap) continue;
        wrap.hidden = !visible;
        if ($(`${prefix}Progress`)) $(`${prefix}Progress`).value = value;
        if ($(`${prefix}ProgressValue`)) $(`${prefix}ProgressValue`).textContent = `${Math.round(value)}%`;
        if ($(`${prefix}ProgressLabel`)) $(`${prefix}ProgressLabel`).textContent = String(label || '处理中');
        const cancel = $(`${prefix}CancelUpload`);
        if (cancel) cancel.hidden = !visible || !ARCHIVE_UPLOAD || ARCHIVE_UPLOAD.committing;
      }
    }

    function renderBackupOptions() {
      const root = $('backupOptions');
      if (!root) return;
      root.innerHTML = BACKUP_OPTIONS.map(item => {
        const required = item.required === true;
        const checked = required || item.default_selected !== false;
        const reason = item.reason ? ` · ${escapeHtml(item.reason)}` : '';
        return `<label class="backup-option"><input type="checkbox" value="${escapeHtml(item.id)}" ${checked ? 'checked' : ''} ${required ? 'disabled' : ''}><span><strong>${escapeHtml(item.label || item.id)}${required ? '（必选）' : ''}</strong><small>${escapeHtml(item.description || '')}${reason}</small></span></label>`;
      }).join('');
    }

    function renderBackupList(payload) {
      const root = $('backupList');
      if (!root) return;
      const rows = Array.isArray(payload?.items) ? payload.items : [];
      BACKUP_LIST = rows;
      BACKUP_CAN_UPLOAD = payload?.can_create !== false;
      const max = Number(payload?.max || 5);
      const status = $('backupLimitStatus');
      BACKUP_RESTORE_PENDING = payload?.restore_pending === true;
      const pending = BACKUP_RESTORE_PENDING ? '，恢复已准备，请重启插件后生效' : '';
      const invalid = Number(payload?.invalid_count || 0) ? `，${Number(payload.invalid_count)} 个归档校验失败` : '';
      if (status) status.textContent = `已保存 ${rows.length}/${max} 个备份${pending || invalid || (rows.length >= max ? '，请先删除旧备份后再创建或上传' : '')}`;
      if ($('createBackupBtn')) $('createBackupBtn').disabled = BACKUP_BUSY || BACKUP_RESTORE_PENDING || payload?.can_create === false;
      if ($('uploadBackupBtn')) $('uploadBackupBtn').disabled = BACKUP_BUSY || BACKUP_RESTORE_PENDING;
      if (!rows.length) {
        root.innerHTML = '<div class="muted">暂无备份</div>';
        return;
      }
      root.innerHTML = rows.map(row => `<div class="daily-event backup-row" data-backup-id="${escapeHtml(row.id || '')}">
        <div class="daily-event-heading"><strong>${escapeHtml(row.filename || row.id || '备份')}</strong><span class="daily-event-status">${escapeHtml(formatBackupBytes(row.size))}</span></div>
        <div class="muted">${escapeHtml(row.created_at || '')} · ${escapeHtml((row.selected || []).map(id => BACKUP_OPTIONS.find(item => item.id === id)?.label || id).join('、'))} · ${Number(row.file_count || 0)} 个文件</div>
        <div class="actions" style="margin-top:8px"><button class="secondary backup-download" type="button">下载</button><button class="secondary backup-restore" type="button">恢复</button><button class="danger backup-delete" type="button">删除</button></div>
      </div>`).join('');
      root.querySelectorAll('.backup-download').forEach(button => button.onclick = () => downloadBackup(button.closest('[data-backup-id]')?.dataset.backupId));
      root.querySelectorAll('.backup-restore').forEach(button => button.onclick = () => openBackupRestore(button.closest('[data-backup-id]')?.dataset.backupId));
      root.querySelectorAll('.backup-delete').forEach(button => button.onclick = () => deleteBackup(button.closest('[data-backup-id]')?.dataset.backupId));
      root.querySelectorAll('.backup-restore, .backup-delete').forEach(button => { button.disabled = BACKUP_BUSY || BACKUP_RESTORE_PENDING; });
    }

    function invalidateBackupListCache() {
      BACKUP_LIST_CACHE = null;
      BACKUP_LIST_CACHE_AT = 0;
      if (typeof removeDashboardPersistCache === 'function') {
        removeDashboardPersistCache(BACKUP_PERSIST_NAMESPACE, 'state');
      }
    }

    function restorePersistedBackupManager() {
      if (typeof readDashboardPersistCacheEntry !== 'function') return false;
      const cached = readDashboardPersistCacheEntry(BACKUP_PERSIST_NAMESPACE, 'state');
      const storedAt = Number(cached?.storedAt || 0);
      const value = cached?.data;
      if (!storedAt || Date.now() - storedAt >= BACKUP_LIST_CACHE_TTL_MS
        || !Array.isArray(value?.options) || !value?.list || typeof value.list !== 'object') return false;
      BACKUP_OPTIONS_CACHE = value.options;
      BACKUP_LIST_CACHE = value.list;
      BACKUP_LIST_CACHE_AT = storedAt;
      return true;
    }

    function persistBackupManager() {
      if (typeof writeDashboardPersistCache !== 'function' || !BACKUP_LIST_CACHE) return;
      writeDashboardPersistCache(BACKUP_PERSIST_NAMESPACE, 'state', {
        options: BACKUP_OPTIONS_CACHE || BACKUP_OPTIONS,
        list: BACKUP_LIST_CACHE,
      });
    }

    function renderCachedBackupManager() {
      if (!BACKUP_OPTIONS_CACHE || !BACKUP_LIST_CACHE) return false;
      BACKUP_OPTIONS = BACKUP_OPTIONS_CACHE;
      renderBackupOptions();
      renderBackupList(BACKUP_LIST_CACHE);
      return true;
    }

    async function loadBackupManager(force = false) {
      if (BACKUP_LOAD_REQUEST) {
        const pending = BACKUP_LOAD_REQUEST;
        return force ? pending.then(() => loadBackupManager(true)) : pending;
      }
      if (!force && !BACKUP_OPTIONS_CACHE && !BACKUP_LIST_CACHE && restorePersistedBackupManager()) {
        renderCachedBackupManager();
        return;
      }
      if (!force && Date.now() - BACKUP_LIST_CACHE_AT < BACKUP_LIST_CACHE_TTL_MS && renderCachedBackupManager()) return;
      const loadSeq = ++BACKUP_LOAD_SEQ;
      const mutationSeq = BACKUP_MUTATION_SEQ;
      const request = (async () => {
        try {
          const optionsRequest = BACKUP_OPTIONS_CACHE
            ? Promise.resolve({data:{items:BACKUP_OPTIONS_CACHE}})
            : api('/api/backups/options');
          const [options, list] = await Promise.all([optionsRequest, api('/api/backups')]);
          if (loadSeq !== BACKUP_LOAD_SEQ || mutationSeq !== BACKUP_MUTATION_SEQ) return;
          BACKUP_OPTIONS = Array.isArray(options.data?.items) ? options.data.items : [];
          BACKUP_OPTIONS_CACHE = BACKUP_OPTIONS;
          BACKUP_LIST_CACHE = list.data || {};
          BACKUP_LIST_CACHE_AT = Date.now();
          persistBackupManager();
          renderBackupOptions();
          renderBackupList(BACKUP_LIST_CACHE);
        } catch (error) {
          if (loadSeq !== BACKUP_LOAD_SEQ || mutationSeq !== BACKUP_MUTATION_SEQ) return;
          const message = error.message || '备份列表读取失败';
          const root = $('backupList');
          if (root) {
            root.innerHTML = `<div class="muted">读取失败：${escapeHtml(message)}</div><div class="actions"><button class="secondary mini" id="backupListRetry" type="button">重试</button></div>`;
            $('backupListRetry').onclick = () => void loadBackupManager();
          }
          showToast(message, 'bad');
        }
      })().finally(() => {
        if (BACKUP_LOAD_REQUEST === request) BACKUP_LOAD_REQUEST = null;
      });
      BACKUP_LOAD_REQUEST = request;
      return request;
    }

    async function waitBackupTask(taskId, label) {
      if (!taskId) throw new Error('服务器没有返回任务编号');
      setBackupProgress(0, label || '等待处理');
      for (let attempt = 0; attempt < 1800; attempt += 1) {
        const response = await api(`/api/backups/tasks/${encodeURIComponent(taskId)}`);
        const task = response.data || {};
        setBackupProgress(task.percent || 0, task.message || label || '处理中');
        if (task.status === 'completed') {
          setBackupProgress(100, task.message || '处理完成');
          return task;
        }
        if (task.status === 'failed' || task.status === 'cancelled') throw new Error(task.error || task.message || '备份任务失败');
        await new Promise(resolve => setTimeout(resolve, 400));
      }
      throw new Error('备份任务超时，请稍后查看列表');
    }

    async function createBackup() {
      if (BACKUP_BUSY || BACKUP_RESTORE_PENDING) return;
      const selected = [...document.querySelectorAll('#backupOptions input[type="checkbox"]:checked')].map(input => input.value);
      setBackupBusy(true);
      let refresh = false;
      try {
        BACKUP_MUTATION_SEQ += 1;
        invalidateBackupListCache();
        const response = await api('/api/backups', {method:'POST', body: JSON.stringify({selected})});
        refresh = true;
        await waitBackupTask(response.data?.task_id, '正在创建备份');
        showToast('备份已完成', 'ok');
      } catch (error) { showToast(error.message || '创建备份失败', 'bad'); }
      finally {
        setBackupBusy(false);
        setBackupProgress(100, '就绪', false);
        if (refresh) await loadBackupManager(true);
      }
    }

    function setBackupBusy(busy) {
      BACKUP_BUSY = busy;
      $('createBackupBtn').disabled = busy || BACKUP_RESTORE_PENDING || !BACKUP_CAN_UPLOAD;
      $('uploadBackupBtn').disabled = busy || BACKUP_RESTORE_PENDING;
      $('backupList').querySelectorAll('.backup-restore, .backup-delete').forEach(button => {
        button.disabled = busy || BACKUP_RESTORE_PENDING;
      });
      $('backupRestoreSubmit').disabled = busy;
      $('backupRestoreClose').disabled = busy;
      $('backupRestoreCancel').disabled = busy;
    }

    function updateBackupRestoreSource() {
      const id = $('backupRestoreSource').value;
      const row = BACKUP_LIST.find(item => item.id === id);
      $('backupRestoreFileField').hidden = Boolean(id);
      $('backupRestoreStatus').textContent = id ? '' : (BACKUP_CAN_UPLOAD ? 'ZIP 备份，最大 128MB；仅恢复归档中包含的勾选项。' : '已达到备份数量上限，请先删除一个旧备份再上传。');
      const options = row ? BACKUP_OPTIONS.filter(item => row.selected?.includes(item.id)) : BACKUP_OPTIONS;
      $('backupRestoreOptions').innerHTML = options.map(item => `<label class="backup-option"><input type="checkbox" value="${escapeHtml(item.id)}" checked><span><strong>${escapeHtml(item.label || item.id)}</strong><small>${escapeHtml(item.description || '')}</small></span></label>`).join('');
    }

    function openBackupRestore(id = '') {
      if (BACKUP_BUSY || BACKUP_RESTORE_PENDING) return;
      BACKUP_RESTORE_FOCUS = document.activeElement;
      $('backupRestoreSource').innerHTML = '<option value="">上传本地备份文件</option>' + BACKUP_LIST.map(row => `<option value="${escapeHtml(row.id)}">${escapeHtml(row.filename || row.id)}</option>`).join('');
      $('backupRestoreSource').value = id;
      $('backupUploadFile').value = '';
      updateBackupRestoreSource();
      $('backupRestoreModal').classList.add('show');
      $('backupRestoreSource').focus();
    }

    function closeBackupRestore() {
      if (BACKUP_BUSY) return;
      $('backupRestoreModal').classList.remove('show');
      $('backupUploadFile').value = '';
      if (document.contains(BACKUP_RESTORE_FOCUS)) BACKUP_RESTORE_FOCUS?.focus();
      BACKUP_RESTORE_FOCUS = null;
    }

    async function submitBackupRestore() {
      if (BACKUP_BUSY || BACKUP_RESTORE_PENDING) return;
      const id = $('backupRestoreSource').value;
      const file = id ? null : $('backupUploadFile').files?.[0];
      const selected = [...$('backupRestoreOptions').querySelectorAll('input:checked')].map(input => input.value);
      const status = $('backupRestoreStatus');
      if (!id && !file) { status.textContent = '请选择备份文件'; return; }
      if (!selected.length) { status.textContent = '请至少勾选一项恢复内容'; return; }
      if (file && (!BACKUP_CAN_UPLOAD || !file.size || file.size > 128 * ARCHIVE_CHUNK_BYTES)) {
        status.textContent = !BACKUP_CAN_UPLOAD ? '请先删除一个旧备份再上传' : '备份须为非空 ZIP 文件，且不得超过 128MB';
        return;
      }
      setBackupBusy(true);
      let refresh = false;
      try {
        const name = file?.name || BACKUP_LIST.find(item => item.id === id)?.filename || id;
        const labels = selected.map(value => BACKUP_OPTIONS.find(item => item.id === value)?.label || value).join('、');
        if (!await confirmAction(`从 ${name} 恢复：${labels}。\n所选数据将被覆盖，重启插件后生效。是否继续？`, {title:'确认恢复备份', confirmText:file ? '上传并恢复' : '确认恢复'})) return;
        $('backupRestoreModal').classList.remove('show');
        $('backupUploadFile').value = '';
        BACKUP_MUTATION_SEQ += 1;
        invalidateBackupListCache();
        refresh = true;
        const response = file ? await uploadArchiveFile(file, 'backup', {selected})
          : await api(`/api/backups/${encodeURIComponent(id)}/restore`, {method:'POST', body:JSON.stringify({selected})});
        await waitBackupTask(response.data?.task_id, '正在校验并准备恢复');
        BACKUP_RESTORE_PENDING = true;
        showToast('恢复已准备，请重启插件后生效', 'ok');
      } catch (error) { showToast(error.message || '恢复备份失败', 'bad'); }
      finally {
        setBackupBusy(false);
        setBackupProgress(100, '就绪', false);
        if (refresh) await loadBackupManager(true);
      }
    }

    async function deleteBackup(id) {
      if (!id || BACKUP_BUSY || BACKUP_RESTORE_PENDING) return;
      setBackupBusy(true);
      try {
        const name = BACKUP_LIST.find(item => item.id === id)?.filename || id;
        if (!await confirmAction(`删除 ${name}？删除后无法找回此备份。`, {title:'确认删除备份', confirmText:'确认删除'})) return;
        BACKUP_MUTATION_SEQ += 1;
        invalidateBackupListCache();
        const response = await api('/api/backups/delete', {method:'POST', body: JSON.stringify({id})});
        BACKUP_LIST_CACHE = response.data || {};
        BACKUP_LIST_CACHE_AT = Date.now();
        persistBackupManager();
        renderBackupList(BACKUP_LIST_CACHE);
        showToast('备份已删除', 'ok');
      } catch (error) { showToast(error.message || '删除备份失败', 'bad'); }
      finally { setBackupBusy(false); }
    }

    async function downloadArchive(path, defaultFilename) {
      let blob;
      let filename = defaultFilename;
      if (!isDashboardPage()) {
        blob = await archiveTimedRequest(async signal => {
          const response = await fetch(`${path}?format=file`, {headers:headers(), signal});
          if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(apiErrorMessage(response.status, error));
          }
          return response.blob();
        }, null, 300000, '归档下载超时，请重试');
      } else {
        const bridge = await archiveTimedRequest(async () => {
          const result = await waitForDashboardBridge();
          if (typeof result.ready === 'function') await result.ready();
          return result;
        }, null, 30000, '归档下载超时，请重试');
        // Download in the host: sandboxed plugin iframes may block blob links.
        if (typeof bridge.download === 'function') {
          await archiveTimedRequest(() => bridge.download(bridgeEndpoint(path), {format:'file'}, defaultFilename),
            null, 300000, '归档下载超时，请重试');
          return;
        }
        const parts = [];
        let offset = 0;
        let revision = '';
        let totalSize;
        for (;;) {
          const response = await archiveTimedRequest(signal => api(
            `${path}?format=chunk&offset=${offset}&revision=${encodeURIComponent(revision)}`, {signal}),
            null, 30000, '归档下载超时，请重试');
          const payload = response.data || {};
          if (!payload.revision || (revision && revision !== payload.revision)) throw new Error('归档文件已变化，请重新下载');
          const size = Number(payload.size);
          if (!Number.isSafeInteger(size) || size <= 0 || size > 128 * ARCHIVE_CHUNK_BYTES || (totalSize !== undefined && size !== totalSize)) throw new Error('归档下载大小无效');
          if (typeof payload.content_base64 !== 'string' || payload.content_base64.length > 4 * Math.ceil(ARCHIVE_CHUNK_BYTES / 3)) throw new Error('归档下载分块无效');
          const binary = atob(payload.content_base64);
          const bytes = new Uint8Array(binary.length);
          for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
          if (bytes.length > ARCHIVE_CHUNK_BYTES || Number(payload.offset) !== offset || Number(payload.next_offset) !== offset + bytes.length || Number(payload.next_offset) > size) throw new Error('归档下载响应无效');
          parts.push(bytes);
          totalSize = size;
          revision = payload.revision;
          filename = payload.filename || filename;
          offset = Number(payload.next_offset);
          if (payload.eof && offset === size) break;
          if (!bytes.length) throw new Error('归档下载中断，请重新下载');
        }
        blob = new Blob(parts, {type:'application/zip'});
      }
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    }

    async function downloadBackup(id) {
      if (!id) { showToast('缺少备份编号', 'bad'); return; }
      try {
        showToast('正在准备下载', 'ok');
        await downloadArchive(`/api/backups/${encodeURIComponent(id)}/download`, `${id}.zip`);
        showToast('已开始下载', 'ok');
      } catch (error) { showToast(error.message || '下载备份失败', 'bad'); }
    }

    async function exportWardrobe() {
      if (BACKUP_BUSY || BACKUP_RESTORE_PENDING) return;
      BACKUP_BUSY = true;
      let transferId = '';
      try {
        setBackupProgress(10, '正在导出衣橱');
        const response = await archiveRequest('/api/archive-transfers/wardrobe-export', {}, null, 300000);
        const payload = response.data || {};
        transferId = payload.id;
        if (!transferId) throw new Error('衣橱导出响应无效');
        await downloadArchive(`/api/archive-transfers/${encodeURIComponent(transferId)}/download`, payload.filename || 'selfie_image_wardrobe.zip');
      } catch (error) { showToast(error.message || '导出衣橱失败', 'bad'); }
      finally {
        if (transferId) {
          try { await archiveRequest(`/api/archive-transfers/${encodeURIComponent(transferId)}/discard`, {}, null, 5000); }
          catch (_) { /* Expiry handles cleanup after a lost connection. */ }
        }
        BACKUP_BUSY = false;
        setBackupProgress(100, '就绪', false);
      }
    }

    async function importWardrobeFile(file) {
      if (!file || BACKUP_BUSY || BACKUP_RESTORE_PENDING) return;
      if (file.size > 128 * 1024 * 1024) { showToast('衣橱归档不得超过 128MB', 'bad'); return; }
      BACKUP_BUSY = true;
      try {
        await uploadArchiveFile(file, 'wardrobe');
        setBackupProgress(100, '衣橱导入完成');
        await refreshSelfie(true);
        showToast('衣橱已导入', 'ok');
      } catch (error) { showToast(error.message || '导入衣橱失败', 'bad'); }
      finally { BACKUP_BUSY = false; setBackupProgress(100, '就绪', false); }
    }

    $('createBackupBtn')?.addEventListener('click', createBackup);
    $('uploadBackupBtn')?.addEventListener('click', () => openBackupRestore());
    $('backupRestoreSource')?.addEventListener('change', updateBackupRestoreSource);
    $('backupUploadFile')?.addEventListener('change', () => { $('backupRestoreStatus').textContent = ''; });
    $('backupRestoreSubmit')?.addEventListener('click', submitBackupRestore);
    $('backupRestoreClose')?.addEventListener('click', closeBackupRestore);
    $('backupRestoreCancel')?.addEventListener('click', closeBackupRestore);
    $('exportWardrobe')?.addEventListener('click', exportWardrobe);
    $('importWardrobeBtn')?.addEventListener('click', () => $('importWardrobeFile')?.click());
    $('importWardrobeFile')?.addEventListener('change', event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) importWardrobeFile(file); });
    $('backupCancelUpload')?.addEventListener('click', cancelArchiveUpload);
    $('wardrobeArchiveCancelUpload')?.addEventListener('click', cancelArchiveUpload);
