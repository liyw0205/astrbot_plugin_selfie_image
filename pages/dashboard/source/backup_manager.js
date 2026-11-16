    let BACKUP_OPTIONS = [];
    let BACKUP_BUSY = false;

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

    async function fileToBase64(file) {
      return bytesToBase64(new Uint8Array(await file.arrayBuffer()));
    }

    function setBackupProgress(percent, label, visible = true) {
      const wrap = $('backupProgressWrap');
      if (!wrap) return;
      wrap.hidden = !visible;
      const value = Math.max(0, Math.min(100, Number(percent || 0)));
      if ($('backupProgress')) $('backupProgress').value = value;
      if ($('backupProgressValue')) $('backupProgressValue').textContent = `${Math.round(value)}%`;
      if ($('backupProgressLabel')) $('backupProgressLabel').textContent = String(label || '处理中');
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
      const max = Number(payload?.max || 5);
      const status = $('backupLimitStatus');
      if (status) status.textContent = `已保存 ${rows.length}/${max} 个备份${rows.length >= max ? '，请先删除旧备份后再创建或上传' : ''}`;
      if ($('createBackupBtn')) $('createBackupBtn').disabled = BACKUP_BUSY || rows.length >= max;
      if ($('uploadBackupBtn')) $('uploadBackupBtn').disabled = BACKUP_BUSY || rows.length >= max;
      if (!rows.length) {
        root.innerHTML = '<div class="muted">暂无备份</div>';
        return;
      }
      root.innerHTML = rows.map(row => `<div class="daily-event backup-row" data-backup-id="${escapeHtml(row.id || '')}">
        <div class="daily-event-heading"><strong>${escapeHtml(row.filename || row.id || '备份')}</strong><span class="daily-event-status">${escapeHtml(formatBackupBytes(row.size))}</span></div>
        <div class="muted">${escapeHtml(row.created_at || '')} · ${escapeHtml((row.selected || []).join('、'))} · ${Number(row.file_count || 0)} 个文件</div>
        <div class="actions" style="margin-top:8px"><button class="secondary backup-download" type="button">下载</button><button class="secondary backup-restore" type="button">恢复</button><button class="danger backup-delete" type="button">删除</button></div>
      </div>`).join('');
      root.querySelectorAll('.backup-download').forEach(button => button.onclick = () => downloadBackup(button.closest('[data-backup-id]')?.dataset.backupId));
      root.querySelectorAll('.backup-restore').forEach(button => button.onclick = () => restoreBackup(button.closest('[data-backup-id]')?.dataset.backupId));
      root.querySelectorAll('.backup-delete').forEach(button => button.onclick = () => deleteBackup(button.closest('[data-backup-id]')?.dataset.backupId));
    }

    async function loadBackupManager() {
      try {
        const [options, list] = await Promise.all([api('/api/backups/options'), api('/api/backups')]);
        BACKUP_OPTIONS = Array.isArray(options.data?.items) ? options.data.items : [];
        renderBackupOptions();
        renderBackupList(list.data || {});
      } catch (error) {
        showToast(error.message || '备份列表读取失败', 'bad');
      }
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
        if (task.status === 'failed') throw new Error(task.error || task.message || '备份任务失败');
        await new Promise(resolve => setTimeout(resolve, 400));
      }
      throw new Error('备份任务超时，请稍后查看列表');
    }

    async function createBackup() {
      if (BACKUP_BUSY) return;
      const selected = [...document.querySelectorAll('#backupOptions input[type="checkbox"]:checked')].map(input => input.value);
      BACKUP_BUSY = true;
      try {
        const response = await api('/api/backups', {method:'POST', body: JSON.stringify({selected})});
        await waitBackupTask(response.data?.task_id, '正在创建备份');
        showToast('备份已完成', 'ok');
        await loadBackupManager();
      } catch (error) { showToast(error.message || '创建备份失败', 'bad'); }
      finally { BACKUP_BUSY = false; setBackupProgress(100, '就绪', false); loadBackupManager(); }
    }

    async function uploadBackupFile(file) {
      if (!file || BACKUP_BUSY) return;
      BACKUP_BUSY = true;
      try {
        setBackupProgress(10, '正在读取上传文件');
        const content_base64 = await fileToBase64(file);
        setBackupProgress(35, '正在上传备份');
        const response = await api('/api/backups/upload', {method:'POST', body: JSON.stringify({filename:file.name, content_base64})});
        await waitBackupTask(response.data?.task_id, '上传后正在自动恢复');
        showToast('备份上传并恢复完成', 'ok');
        await loadBackupManager();
      } catch (error) { showToast(error.message || '上传备份失败', 'bad'); }
      finally { BACKUP_BUSY = false; setBackupProgress(100, '就绪', false); loadBackupManager(); }
    }

    async function restoreBackup(id) {
      if (!id || BACKUP_BUSY || !window.confirm('恢复会覆盖所选备份中的配置和数据，是否继续？')) return;
      BACKUP_BUSY = true;
      try {
        const response = await api(`/api/backups/${encodeURIComponent(id)}/restore`, {method:'POST', body:'{}'});
        await waitBackupTask(response.data?.task_id, '正在恢复备份');
        showToast('备份已恢复，建议刷新页面', 'ok');
      } catch (error) { showToast(error.message || '恢复备份失败', 'bad'); }
      finally { BACKUP_BUSY = false; setBackupProgress(100, '就绪', false); loadBackupManager(); }
    }

    async function deleteBackup(id) {
      if (!id || !window.confirm('删除后无法恢复这个备份，是否继续？')) return;
      try {
        const response = await api('/api/backups/delete', {method:'POST', body: JSON.stringify({id})});
        renderBackupList(response.data || {});
        showToast('备份已删除', 'ok');
      } catch (error) { showToast(error.message || '删除备份失败', 'bad'); }
    }

    async function downloadBackup(id) {
      try {
        const response = await api(`/api/backups/${encodeURIComponent(id)}/download`);
        const payload = response.data || {};
        const binary = atob(payload.content_base64 || '');
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
        const url = URL.createObjectURL(new Blob([bytes], {type:'application/zip'}));
        const link = document.createElement('a'); link.href = url; link.download = payload.filename || 'selfie_image_backup.zip'; document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
      } catch (error) { showToast(error.message || '下载备份失败', 'bad'); }
    }

    async function exportWardrobe() {
      try {
        const response = await api('/api/selfie-wardrobe/export');
        const payload = response.data || {};
        const binary = atob(payload.content_base64 || '');
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
        const url = URL.createObjectURL(new Blob([bytes], {type:'application/zip'}));
        const link = document.createElement('a'); link.href = url; link.download = payload.filename || 'selfie_image_wardrobe.zip'; document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
      } catch (error) { showToast(error.message || '导出衣橱失败', 'bad'); }
    }

    async function importWardrobeFile(file) {
      if (!file) return;
      try {
        setBackupProgress(15, '正在读取衣橱归档');
        const content_base64 = await fileToBase64(file);
        setBackupProgress(60, '正在导入衣橱');
        await api('/api/selfie-wardrobe/import', {method:'POST', body: JSON.stringify({filename:file.name, content_base64})});
        setBackupProgress(100, '衣橱导入完成');
        await refreshSelfie(true);
        showToast('衣橱已导入', 'ok');
      } catch (error) { showToast(error.message || '导入衣橱失败', 'bad'); }
      finally { setBackupProgress(100, '就绪', false); }
    }

    $('createBackupBtn')?.addEventListener('click', createBackup);
    $('uploadBackupBtn')?.addEventListener('click', () => $('backupUploadFile')?.click());
    $('backupUploadFile')?.addEventListener('change', event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) uploadBackupFile(file); });
    $('exportWardrobe')?.addEventListener('click', exportWardrobe);
    $('importWardrobeBtn')?.addEventListener('click', () => $('importWardrobeFile')?.click());
    $('importWardrobeFile')?.addEventListener('change', event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) importWardrobeFile(file); });
