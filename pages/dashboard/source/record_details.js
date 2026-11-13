    function copyIconSvg() {
      return '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
    }
    function openIconSvg() {
      return '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3h7v7"></path><path d="M10 14 21 3"></path><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path></svg>';
    }
    function promptDetailBlock(title, field, text) {
      return `
        <div class="detail-title">
          <h3>${escapeHtml(title)}</h3>
          <button class="copy-btn" type="button" title="复制${escapeHtml(title)}" aria-label="复制${escapeHtml(title)}" onclick="copyRecordField('${field}')">${copyIconSvg()}</button>
        </div>
        <pre>${escapeHtml(text || '')}</pre>
      `;
    }
    function collectResponseBase64Copies(value, path = [], result = []) {
      if (Array.isArray(value)) {
        value.forEach((item, index) => collectResponseBase64Copies(item, path.concat(index), result));
        return result;
      }
      if (!value || typeof value !== 'object') return result;
      const type = String(value.type || '').trim().toLowerCase();
      const text = String(value.value || '');
      if (type === 'base64' && (value.deferred === true || text.startsWith('[Base64'))) {
        let imageIndex;
        for (let i = path.length - 2; i >= 0; i -= 1) {
          if (path[i] === 'generated_image_sources' && Number.isInteger(path[i + 1])) {
            imageIndex = path[i + 1];
            break;
          }
        }
        const copyArg = {type: 'base64', deferred: true};
        if (Number.isInteger(imageIndex)) copyArg.index = imageIndex;
        result.push(copyArg);
      }
      Object.entries(value).forEach(([key, item]) => {
        collectResponseBase64Copies(item, path.concat(key), result);
      });
      return result;
    }
    function responseDataDetailBlock(value) {
      const data = value && typeof value === 'object' ? value : {};
      const raw = JSON.stringify(data, null, 2);
      const copies = collectResponseBase64Copies(data);
      const marker = escapeHtml('[Base64，点击复制按钮获取原文]');
      let rendered = escapeHtml(raw);
      for (const copyArg of copies) {
        const pos = rendered.indexOf(marker);
        if (pos < 0) break;
        const button = `<button class="base64-copy" type="button" title="打开原始 Base64" onclick="copyMediaSource(${escapeJs(JSON.stringify(copyArg))})">${openIconSvg()}Base64（点击打开）</button>`;
        rendered = rendered.slice(0, pos) + button + rendered.slice(pos + marker.length);
      }
      return `
        <div class="detail-title">
          <h3>响应数据</h3>
          <button class="copy-btn" type="button" title="复制响应数据" aria-label="复制响应数据" onclick="copyRecordField('response_data')">${copyIconSvg()}</button>
        </div>
        <pre>${rendered}</pre>
      `;
    }
    async function copyTextToClipboard(text) {
      const value = String(text ?? '');
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
        return;
      }
      const area = document.createElement('textarea');
      area.value = value;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.left = '-9999px';
      document.body.appendChild(area);
      area.focus();
      area.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(area);
      if (!ok) throw new Error('复制失败');
    }
    function openMediaSourceModal(label = '原始来源') {
      const modal = $('mediaSourceModal');
      const title = $('mediaSourceModalTitle');
      const preview = $('mediaSourcePreview');
      if (!modal || !preview) return false;
      if (title) title.textContent = label || '原始来源';
      preview.replaceChildren();
      const loading = document.createElement('div');
      loading.className = 'muted';
      loading.style.color = '#fff';
      loading.textContent = '正在加载原始来源…';
      preview.appendChild(loading);
      modal.classList.add('show');
      return true;
    }
    function closeMediaSourceModal() {
      const modal = $('mediaSourceModal');
      const preview = $('mediaSourcePreview');
      if (preview) preview.replaceChildren();
      if (modal) modal.classList.remove('show');
    }
    let MEDIA_LIGHTBOX_PATH = '';
    function openMediaLightbox(path, title = '图片预览') {
      const modal = $('mediaLightboxModal');
      const image = $('mediaLightboxImage');
      const status = $('mediaLightboxStatus');
      if (!modal || !image || !path) return false;
      MEDIA_LIGHTBOX_PATH = String(path || '').trim();
      if ($('mediaLightboxTitle')) $('mediaLightboxTitle').textContent = title || '图片预览';
      if ($('mediaLightboxDownload')) $('mediaLightboxDownload').disabled = false;
      image.removeAttribute('src');
      image.alt = title || '图片预览';
      image.hidden = true;
      if (status) { status.hidden = false; status.textContent = '正在加载图片…'; }
      modal.classList.add('show');
      getProtectedMedia(MEDIA_LIGHTBOX_PATH).then(media => {
        if (!MEDIA_LIGHTBOX_PATH || !modal.classList.contains('show')) return;
        const url = protectedMediaUrl(media);
        if (!url) throw new Error('图片已清理');
        image.onload = () => { image.hidden = false; if (status) status.hidden = true; };
        image.onerror = () => { image.hidden = true; if (status) { status.hidden = false; status.textContent = '图片加载失败'; } };
        image.src = url;
      }).catch(error => {
        if (status) { status.hidden = false; status.textContent = error.message || '图片读取失败'; }
      });
      return true;
    }
    function closeMediaLightbox() {
      MEDIA_LIGHTBOX_PATH = '';
      const image = $('mediaLightboxImage');
      if (image) { image.onload = null; image.onerror = null; image.removeAttribute('src'); image.hidden = true; }
      $('mediaLightboxModal')?.classList.remove('show');
    }
    function mediaSourceMime(source, kind = 'image') {
      const value = String(source?.value || '').trim();
      const declared = String(source?.mime || source?.mime_type || '').trim().toLowerCase();
      if (declared) return declared;
      const dataMatch = value.match(/^data:([^;,]+)/i);
      if (dataMatch) return dataMatch[1].toLowerCase();
      const path = value.split(/[?#]/, 1)[0].toLowerCase();
      const extensionMimes = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif',
        '.webp': 'image/webp', '.bmp': 'image/bmp', '.svg': 'image/svg+xml'
      };
      const match = Object.keys(extensionMimes).find(ext => path.endsWith(ext));
      return match ? extensionMimes[match] : 'image/png';
    }
    function mediaSourceBrowserValue(source, kind = 'image') {
      const value = String(source?.value || '').trim();
      if (!value) return '';
      if (source?.type !== 'base64') return value;
      if (/^data:/i.test(value)) return value;
      const payload = value.replace(/^base64:\/\//i, '').replace(/\s+/g, '').replace(/-/g, '+').replace(/_/g, '/');
      const padded = payload + '='.repeat((4 - payload.length % 4) % 4);
      return `data:${mediaSourceMime(source, kind)};base64,${padded}`;
    }
    function renderMediaSourceModal(sources, label = '原始来源', kind = 'image') {
      const preview = $('mediaSourcePreview');
      if (!preview) return;
      const title = $('mediaSourceModalTitle');
      if (title) title.textContent = label || '原始来源';
      preview.replaceChildren();
      const list = Array.isArray(sources) ? sources : [sources];
      list.forEach(source => {
        const value = mediaSourceBrowserValue(source, source?.mediaKind || kind);
        if (!value) return;
        const media = document.createElement('img');
        media.src = value;
        media.alt = label || '原始图片';
        media.decoding = 'async';
        preview.appendChild(media);
      });
      if (!preview.children.length) {
        const empty = document.createElement('div');
        empty.className = 'status';
        empty.textContent = '没有可渲染的原始来源';
        preview.appendChild(empty);
      }
    }
    async function loadRecordMediaSources() {
      const id = String(CURRENT_RECORD?.id || '').trim();
      if (!id) return {generated_image_sources: []};
      if (RECORD_MEDIA_SOURCE_CACHE.has(id)) return RECORD_MEDIA_SOURCE_CACHE.get(id);
      if (RECORD_MEDIA_SOURCE_REQUESTS.has(id)) return RECORD_MEDIA_SOURCE_REQUESTS.get(id);
      const request = api('/api/records/' + encodeURIComponent(id) + '/media-sources')
        .then(res => {
          const data = res.data || {};
          if (RECORD_MEDIA_SOURCE_CACHE.size >= 12) {
            const oldest = RECORD_MEDIA_SOURCE_CACHE.keys().next().value;
            if (oldest && oldest !== id) RECORD_MEDIA_SOURCE_CACHE.delete(oldest);
          }
          RECORD_MEDIA_SOURCE_CACHE.set(id, data);
          return data;
        })
        .finally(() => RECORD_MEDIA_SOURCE_REQUESTS.delete(id));
      RECORD_MEDIA_SOURCE_REQUESTS.set(id, request);
      return request;
    }
    function prefetchRecordMediaSources() {
      if (!String(CURRENT_RECORD?.id || '').trim()) return;
      loadRecordMediaSources().catch(() => {});
    }
    function cachedRecordMediaSources() {
      const id = String(CURRENT_RECORD?.id || '').trim();
      return id ? RECORD_MEDIA_SOURCE_CACHE.get(id) || null : null;
    }
    function resolveCachedMediaSource(item) {
      const data = cachedRecordMediaSources();
      if (!data) return null;
      const all = normalizeMediaSources(data.generated_image_sources || []);
      return all[item?.index ?? -1] || null;
    }
    async function resolveMediaSourceForDisplay(source) {
      const item = normalizeMediaSources(source)[0];
      let resolved = item;
      if (!resolved || resolved.deferred || !resolved.value || resolved.value.startsWith('[Base64')) {
        resolved = resolveCachedMediaSource(item);
        if (!resolved && String(CURRENT_RECORD?.id || '').trim()) {
          try {
            resolved = resolveCachedMediaSource(item);
            if (!resolved) {
              await loadRecordMediaSources();
              resolved = resolveCachedMediaSource(item);
            }
          } catch (_) {
            resolved = null;
          }
        }
        if (!resolved) {
          showToast(RECORD_MEDIA_SOURCE_REQUESTS.has(String(CURRENT_RECORD?.id || '').trim()) ? '原始来源正在加载，请稍后再点' : '原始来源未加载', 'bad');
          return null;
        }
      }
      if (!resolved || !resolved.value) {
        showToast('没有原始链接或 base64', 'bad');
        return null;
      }
      return resolved;
    }
    async function copyMediaSourceValue(source, kind = 'image') {
      const resolved = await resolveMediaSourceForDisplay(source);
      if (!resolved) return;
      if (resolved.type === 'local') {
        showToast('当前只有本地缓存，没有可复制的原始来源', 'bad');
        return;
      }
      try {
        await copyTextToClipboard(resolved.value);
        showToast(`${mediaSourceLabel(resolved)}来源已复制`, 'ok');
      } catch (e) {
        showToast(e.message || '复制失败', 'bad');
      }
    }
    async function copyMediaSourceValues(sources, kind = 'image') {
      const list = normalizeMediaSources(sources);
      const values = [];
      for (const source of list) {
        const resolved = await resolveMediaSourceForDisplay(source);
        if (resolved && resolved.type !== 'local' && resolved.value) values.push(resolved.value);
      }
      if (!values.length) {
        showToast('没有可复制的原始来源', 'bad');
        return;
      }
      try {
        await copyTextToClipboard(values.join('\n'));
        showToast(`已复制 ${values.length} 项原始来源`, 'ok');
      } catch (e) {
        showToast(e.message || '复制失败', 'bad');
      }
    }
    async function copyMediaSource(source, kind = 'image') {
      if (!openMediaSourceModal('原始来源')) return;
      const resolved = await resolveMediaSourceForDisplay(source);
      if (!resolved) {
        closeMediaSourceModal();
        return;
      }
      if (resolved.type === 'local') {
        closeMediaSourceModal();
        showToast('当前只有本地缓存，没有可打开的原始来源', 'bad');
        return;
      }
      try {
        resolved.mediaKind = resolved.mediaKind || kind;
        renderMediaSourceModal([resolved], `原始来源（${mediaSourceLabel(resolved)}）`, kind);
      } catch (e) {
        closeMediaSourceModal();
        showToast(e.message || '原始来源读取失败', 'bad');
      }
    }
    async function copyMediaSources(sources, kind = 'image') {
      if (!openMediaSourceModal('原始来源')) return;
      const list = normalizeMediaSources(sources);
      let resolvedList = list;
      if (list.some(item => item.deferred || !item.value || item.value.startsWith('[Base64'))) {
        const data = cachedRecordMediaSources();
        if (data) {
          resolvedList = normalizeMediaSources(data.generated_image_sources || []);
        } else if (String(CURRENT_RECORD?.id || '').trim()) {
          try {
            const loaded = await loadRecordMediaSources();
            resolvedList = normalizeMediaSources(loaded.generated_image_sources || []);
          } catch (_) {
            resolvedList = [];
          }
        }
      }
      if (!resolvedList.length) {
        closeMediaSourceModal();
        showToast('没有原始链接或 base64', 'bad');
        return;
      }
      resolvedList = resolvedList.filter(item => item && item.value && !item.value.startsWith('[Base64'));
      resolvedList = resolvedList.filter(item => item.type !== 'local');
      if (!resolvedList.length) {
        closeMediaSourceModal();
        showToast('没有原始链接或 base64', 'bad');
        return;
      }
      try {
        const label = resolvedList.length > 1 ? `原始来源（${resolvedList.length}项）` : `原始来源（${mediaSourceLabel(resolvedList[0])}）`;
        resolvedList.forEach(item => { item.mediaKind = item.mediaKind || kind; });
        renderMediaSourceModal(resolvedList, label, kind);
      } catch (e) {
        closeMediaSourceModal();
        showToast(e.message || '原始来源读取失败', 'bad');
      }
    }
    async function copyRecordField(field) {
      const r = CURRENT_RECORD || {};
      const text = field === 'image_code' ? String(r.image_code || '') : field === 'request_prompt'
        ? (r.request_prompt || r.prompt || '')
        : field === 'final_prompt'
          ? (r.final_prompt || r.request_data?.request_prompt_en || r.request_prompt || r.prompt || '')
          : field === 'request_prompt_en'
            ? (r.request_data?.request_prompt_en || '')
        : field === 'request_data'
          ? JSON.stringify(r.request_data || {}, null, 2)
          : field === 'response_data'
            ? JSON.stringify(r.response_data || {}, null, 2)
            : (r.original_prompt || '');
      if (!text) {
        showToast('内容为空', 'bad');
        return;
      }
      try {
        await copyTextToClipboard(text);
        showToast('已复制到剪贴板', 'ok');
      } catch (e) {
        showToast(e.message || '复制失败', 'bad');
      }
    }
    function recordDiagnosticsText(r) {
      const selection = Object.assign({}, r.prompt_selection || {});
      for (const key of ['cos', 'cos_pose', 'cos_scene', 'cos_lighting', 'cos_framing', 'cos_composition', 'cos_view']) {
        if (r[key]) selection[key] = r[key];
      }
      return [
        `生图编号：${r.image_code || ''}`,
        `时间：${r.time || ''}`,
        `调用入口：${r.source || ''}`,
        `模型：${r.used_model || r.model || ''}`,
        `状态：${r.success ? '成功' : '失败'}`,
        `随机条目：${JSON.stringify(selection)}`,
        `原始输入：\n${r.original_prompt || ''}`,
        `最终实际提示词：\n${r.final_prompt || r.request_data?.request_prompt_en || r.request_prompt || r.prompt || ''}`,
      ].join('\n');
    }
    async function copyRecordDiagnostics() {
      if (!CURRENT_RECORD) return;
      try {
        await copyTextToClipboard(recordDiagnosticsText(CURRENT_RECORD));
        showToast('排查信息已复制', 'ok');
      } catch (e) {
        showToast(e.message || '复制失败', 'bad');
      }
    }
    function generatedMediaDetailBlock(record) {
      const imagePaths = (record.generated_image_paths?.length ? record.generated_image_paths : (record.response_data?.generated_image_paths || [])).filter(Boolean);
      const sources = Array.isArray(record.generated_image_sources) && record.generated_image_sources.length
        ? record.generated_image_sources
        : (record.response_data?.generated_image_sources || []);
      return imageThumbs(imagePaths, '生成图', sources);
    }
    function recordTaskSummaryBlock(record) {
      const summary = record?.task_summary;
      if (!summary || !summary.task_id) return '';
      const requested = Number(summary.requested_count || 0);
      const completed = Number(summary.completed_count || 0);
      const progress = requested ? ` · ${completed}/${requested}` : '';
      const stage = String(summary.generation_stage_label || '').trim();
      const status = taskQueueStatusLabel(String(summary.status || ''));
      return `<div class="detail-title" style="margin-top:12px"><h3>关联任务</h3><button class="secondary mini record-task-open" type="button" data-task-id="${escapeHtml(String(summary.task_id))}">查看任务</button></div><div class="status"><span class="mono">${escapeHtml(String(summary.task_id))}</span> · ${escapeHtml(displaySourceLabel(summary))} · ${escapeHtml(status)}${progress}${stage ? ` · ${escapeHtml(stage)}` : ''}</div>`;
    }
    async function openRecordDetail(id) {
      closeMediaSourceModal();
      if ($('recordDetailBody')) {
        $('recordDetailBody').innerHTML = '<div class="status">正在加载记录详情…</div>';
        $('recordModal')?.classList.add('show');
      }
      let r = RECORDS.find(item => String(item.id || '') === String(id || ''));
      try {
        const res = await api('/api/records/' + encodeURIComponent(String(id || '')));
        r = res.data || r;
      } catch (e) {
        showToast(e.message || '记录详情读取失败', 'bad');
      }
      if (!r) return;
      CURRENT_RECORD = r;
      prefetchRecordMediaSources();
      $('recordDetailBody').innerHTML = `
        <div class="detail-title"><h3 class="mono">${escapeHtml(r.image_code || '')}</h3><div class="actions"><button class="secondary mini" type="button" onclick="copyRecordField('image_code')">复制编号</button><button class="secondary mini" type="button" onclick="copyRecordDiagnostics()">复制排查信息</button></div></div>
        <div class="grid">
          <div><label>时间</label><div class="status">${escapeHtml(r.time || '')}</div></div>
          <div><label>来源</label><div class="status">${escapeHtml(displaySourceLabel(r))}</div></div>
          <div><label>状态</label><div class="status">${r.success ? '成功' : '失败'}</div></div>
          <div><label>类型</label><div class="status">图片</div></div>
          <div><label>MD5</label><div class="status" style="overflow-wrap:anywhere">${escapeHtml(String(r.md5 || '空'))}</div></div>
          <div><label>用时</label><div class="status">${escapeHtml(formatRecordElapsed(r))}</div></div>
          ${r.generation_success !== undefined || r.delivery_success !== undefined ? `<div><label>生成/发送</label><div class="status">${r.generation_success === true ? '生成成功' : r.generation_success === false ? '生成失败' : '-'}${r.delivery_success === true ? ' · 已发送' : r.delivery_success === false ? ' · 发送失败' : ''}</div></div>` : ''}
          <div><label>模型重试</label><div class="status">${recordRetryModelChain(r)}</div></div>
          ${!r.task_summary && r.task_id ? `<div><label>关联任务</label><div class="status mono">${escapeHtml(String(r.task_id))}</div></div>` : ''}
          <div><label>调用入口</label><div class="status">${escapeHtml(r.source || '')}</div></div>
          <div><label>群号</label><div class="status">${escapeHtml(r.group_id || '')}</div></div>
          <div><label>Q号</label><div class="status">${escapeHtml(r.user_id || '')}</div></div>
          ${r.success === false ? `<div><label>最后失败</label><div class="status">${escapeHtml(recordFailureReason(r) || r.error || '')}</div></div>` : ''}
        </div>
        ${recordTaskSummaryBlock(r)}
        ${recordFailureReasonsBlock(r)}
        ${recordParameterBlock(r)}
        ${promptDetailBlock('原始提示词', 'original_prompt', r.original_prompt || '')}
        ${promptDetailBlock('请求提示词', 'request_prompt', r.request_prompt || r.prompt || '')}
        ${r.final_prompt || r.request_data?.request_prompt_en ? promptDetailBlock('最终实际提示词（发送给模型）', 'final_prompt', r.final_prompt || r.request_data?.request_prompt_en || '') : ''}
        ${r.request_data?.request_prompt_en ? promptDetailBlock('英文请求提示词', 'request_prompt_en', r.request_data.request_prompt_en) : ''}
        <div class="detail-title"><h3>资产标记</h3></div>
        <div class="grid">
          <label class="checkline"><input id="recordFavorite" type="checkbox" ${r.favorite ? 'checked' : ''}> 收藏</label>
          <label class="checkline"><input id="recordPinned" type="checkbox" ${r.pinned ? 'checked' : ''}> 置顶</label>
          <div><label>标签</label><input id="recordTags" value="${escapeHtml((r.tags || []).join(', '))}" placeholder="多个标签用逗号分隔"></div>
          <div><label>备注</label><input id="recordNote" value="${escapeHtml(r.note || '')}" maxlength="2000"></div>
        </div>
        <div class="actions"><button class="ok mini" type="button" onclick="saveRecordAsset()">保存标记</button><button class="secondary mini" type="button" onclick="reuseRecordPrompt()">复用提示词</button></div>
        <div class="detail-title"><h3>重试生成</h3></div>
        <div class="grid"><div><label>修改要求（可选）</label><input id="recordRetryFeedback" maxlength="2000" placeholder="例如：更明亮、动作更自然"></div></div>
        <div><label>重试策略</label><select id="recordRetryStrategy"><option value="full">完整保留参数</option><option value="model_only">只换模型</option><option value="channel_only">只换渠道</option><option value="lower_resolution">降低分辨率</option></select></div>
        <div class="actions"><button class="ok mini" type="button" onclick="retryCurrentRecord()">重试生成</button><span id="recordRetryStatus" class="muted"></span></div>
        ${promptDetailBlock('请求数据', 'request_data', JSON.stringify(r.request_data || {}, null, 2))}
        ${responseDataDetailBlock(r.response_data || {})}
        ${recordRequestReferences(r)}
        ${generatedMediaDetailBlock(r)}
      `;
      $('recordModal').classList.add('show');
      $('recordDetailBody').querySelector('.record-task-open')?.addEventListener('click', event => {
        const taskId = String(event.currentTarget?.dataset?.taskId || '').trim();
        if (!taskId) return;
        closeRecordDetail();
        openTaskDetail(taskId);
      });
      loadProtectedImages($('recordDetailBody'));
      loadPersonaReferenceThumbs($('recordDetailBody')).catch(() => {});
    }
    function closeRecordDetail() {
      closeMediaSourceModal();
      $('recordModal').classList.remove('show');
      CURRENT_RECORD = null;
    }
    async function saveRecordAsset() {
      const id = String(CURRENT_RECORD?.id || '').trim();
      if (!id) return;
      try {
        const tags = String($('recordTags')?.value || '').split(/[,，]/).map(item => item.trim()).filter(Boolean);
        const res = await api('/api/records/' + encodeURIComponent(id) + '/metadata', {
          method: 'POST', body: JSON.stringify({
            favorite: !!$('recordFavorite')?.checked,
            pinned: !!$('recordPinned')?.checked,
            tags,
            note: String($('recordNote')?.value || ''),
          })
        });
        CURRENT_RECORD = Object.assign(CURRENT_RECORD || {}, res.data || {});
        clearRecordPageCache();
        clearAssetPageCache();
        showToast('资产标记已保存', 'ok');
        await loadRecords(false);
      } catch (e) { showToast(e.message || '保存失败', 'bad'); }
    }
    async function reuseRecordPrompt() {
      const id = String(CURRENT_RECORD?.id || '').trim();
      if (!id) return;
      try {
        const res = await api('/api/records/' + encodeURIComponent(id) + '/reuse');
        const prompt = String(res.data?.prompt || '');
        if (!prompt) throw new Error('历史提示词为空');
        await copyTextToClipboard(prompt);
        showToast('提示词已复制，可粘贴到出图输入框', 'ok');
      } catch (e) { showToast(e.message || '复用失败', 'bad'); }
    }
    function recordRetryTaskStatusPath(taskId) {
      return `/api/test-image-channel/tasks/${encodeURIComponent(taskId)}`;
    }
    async function pollRecordRetryTask(taskId, failStreak = 0) {
      const status = $('recordRetryStatus');
      try {
        const res = await api(recordRetryTaskStatusPath(taskId));
        const task = res.data || {};
        if (task.status === 'queued' || task.status === 'running') {
          if (status) status.textContent = task.status === 'queued' ? '排队中…' : `生成中 ${Number(task.running_seconds || 0)}s`;
          setTimeout(() => pollRecordRetryTask(taskId, 0), 2000);
          return;
        }
        if (status) status.textContent = task.success ? '已完成' : (task.error || '生成失败');
        if (task.success) {
          clearRecordPageCache();
          clearAssetPageCache();
          clearGalleryPickerCache();
          showToast('重试生成完成', 'ok');
          await Promise.all([loadRecords(false), loadAssets(false), loadAssetTags(), loadMetrics()]);
        } else {
          showToast(task.error || '重试生成失败', 'bad');
        }
      } catch (e) {
        if (failStreak < 6) {
          if (status) status.textContent = `状态读取失败，正在重试（${failStreak + 1}/6）…`;
          setTimeout(() => pollRecordRetryTask(taskId, failStreak + 1), 2000);
        } else if (status) {
          status.textContent = e.message || '重试状态读取失败';
        }
      }
    }
    async function retryCurrentRecord() {
      const id = String(CURRENT_RECORD?.id || '').trim();
      if (!id) return;
      const feedback = String($('recordRetryFeedback')?.value || '').trim();
      const strategy = String($('recordRetryStrategy')?.value || 'full');
      const status = $('recordRetryStatus');
      try {
        if (status) status.textContent = `正在提交（${retryStrategyLabel(strategy)}）…`;
        const res = await api('/api/records/' + encodeURIComponent(id) + '/retry', {
          method: 'POST', body: JSON.stringify({feedback, strategy})
        });
        const task = res.data || {};
        if (!task.task_id) throw new Error('后台任务提交失败：未返回 task_id');
        if (status) status.textContent = `任务 ${task.task_id} 已提交`;
        showToast('已提交重试任务', 'ok');
        pollRecordRetryTask(task.task_id);
      } catch (e) {
        if (status) status.textContent = e.message || '提交失败';
        showToast(e.message || '重试提交失败', 'bad');
      }
    }
