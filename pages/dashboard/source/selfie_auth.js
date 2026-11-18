    function renderSelfieDailySchedule(payload, stateMessage = '') {
      const root = $('selfieDailySchedule');
      if (!root) return;
      const schedule = payload && payload.schedule;
      if (!schedule || !Array.isArray(schedule.events) || !schedule.events.length) {
        root.innerHTML = `<div class="daily-schedule-head"><div class="daily-schedule-title"><strong>今日穿搭与日程</strong><span class="muted">今日日程暂不可用</span></div></div><div class="daily-current-event"><span class="daily-event-status">${escapeHtml(stateMessage || '暂无今日日程，正在等待恢复。')}</span><button type="button" class="secondary" id="ensureSelfieDailySchedule">重新加载今日日程</button></div>`;
        root.hidden = false;
        $('ensureSelfieDailySchedule').onclick = retrySelfieDailySchedule;
        return;
      }
      const currentId = payload.current_event && String(payload.current_event.id || '');
      const renderField = (label, value) => value
        ? `<div class="daily-event-field"><span class="daily-event-field-label">${escapeHtml(label)}</span><span class="daily-event-field-value">${escapeHtml(String(value))}</span></div>`
        : '';
      const outfitFor = event => schedule.outfits && schedule.outfits[event.outfit_ref]
        ? schedule.outfits[event.outfit_ref] : '';
      const events = schedule.events.map(event => {
        const outfit = outfitFor(event);
        return `<div class="daily-event${String(event.id || '') === currentId ? ' current' : ''}">
          <div class="daily-event-time"><strong>${escapeHtml(event.start || '')} - ${escapeHtml(event.end || '')}</strong>${String(event.id || '') === currentId ? '<span class="daily-schedule-current">当前</span>' : ''}</div>
          <div class="daily-event-body">
            <div class="daily-event-heading"><div class="daily-event-title">${escapeHtml(event.title || '日程事件')}</div>${event.status ? `<div class="daily-event-status">${escapeHtml(event.status)}</div>` : ''}</div>
            <div class="daily-event-fields">
              ${renderField('场景', event.scene)}
              ${renderField('动作', event.action)}
              ${renderField('穿搭', outfit)}
              ${renderField('变化', event.outfit_change)}
            </div>
          </div>
        </div>`;
      }).join('');
      const currentEvent = payload.current_event;
      const sourceLabel = ({local:'本地日程', fallback:'本地兜底', llm:'LLM生成'})[String(schedule.source || '')] || String(schedule.source || '日程');
      const facts = [
        ['今日主题', schedule.theme],
        ['当前心情', schedule.mood],
        ['时令氛围', schedule.occasion],
      ].filter(([, value]) => String(value || '').trim());
      const factMarkup = facts.map(([label, value]) => `<div class="daily-schedule-fact"><span class="daily-schedule-fact-label">${escapeHtml(label)}</span><span class="daily-schedule-fact-value">${escapeHtml(value)}</span></div>`).join('');
      const currentMarkup = currentEvent
        ? `<div class="daily-current-event"><div class="daily-current-event-head"><strong class="daily-current-event-title">${escapeHtml(currentEvent.title || '当前安排')}</strong><span class="daily-current-event-time">${escapeHtml(currentEvent.start || '')} - ${escapeHtml(currentEvent.end || '')}</span></div><div class="daily-event-fields">${renderField('场景', currentEvent.scene)}${renderField('动作', currentEvent.action)}${renderField('穿搭', outfitFor(currentEvent))}${renderField('变化', currentEvent.outfit_change)}</div></div>`
        : '<div class="daily-current-event"><span class="daily-event-status">当前没有匹配的日程事件</span></div>';
      root.innerHTML = `<div class="daily-schedule-head"><div class="daily-schedule-title"><strong>今日穿搭与日程</strong><span class="muted">${escapeHtml(schedule.date || '今日')} · ${escapeHtml(sourceLabel)}</span></div><span class="daily-schedule-current">${schedule.events.length} 个安排</span></div>${factMarkup ? `<div class="daily-schedule-facts">${factMarkup}</div>` : ''}${currentMarkup}<details class="daily-schedule-details"><summary><span>全天安排</span><span>${schedule.events.length} 个时段</span></summary><div class="daily-schedule-list">${events}</div></details>`;
      root.hidden = false;
    }

    let SELFIE_DAILY_ENSURE_REQUEST = null;
    let SELFIE_DAILY_LOAD_SEQ = 0;
    let SELFIE_MUTATION_SEQ = 0;
    function invalidateSelfieDailySchedule() {
      SELFIE_DAILY_LOAD_SEQ += 1;
      // A mutation must not reuse an ensure request that started against the
      // previous wardrobe/schedule snapshot. The old promise may still settle;
      // its identity check keeps it from clearing a newer request.
      SELFIE_DAILY_ENSURE_REQUEST = null;
    }
    function beginSelfieMutation() {
      SELFIE_MUTATION_SEQ += 1;
      SELFIE_REFERENCE_VERSION += 1;
      SELFIE_REFERENCE_DATA_URL = '';
      SELFIE_REFERENCE_REQUEST = null;
      invalidateSelfieDailySchedule();
      return SELFIE_MUTATION_SEQ;
    }
    async function loadSelfieDailySchedule() {
      let request = SELFIE_DAILY_ENSURE_REQUEST;
      if (!request) {
        const loadSeq = ++SELFIE_DAILY_LOAD_SEQ;
        request = {loadSeq, promise: null};
        const promise = api('/api/selfie-profile/ensure-today', {method:'POST', body:'{}'})
          .then(res => ({
            schedule: res.data?.daily_schedule || null,
            wardrobe: res.data?.wardrobe || null,
            error: '',
          }))
          .catch(e => ({schedule: null, wardrobe: null, error: e.message || '今日日程恢复失败'}))
          .finally(() => {
            if (SELFIE_DAILY_ENSURE_REQUEST === request) SELFIE_DAILY_ENSURE_REQUEST = null;
          });
        request.promise = promise;
        SELFIE_DAILY_ENSURE_REQUEST = request;
      }
      const result = await request.promise;
      return {...result, loadSeq: request.loadSeq};
    }

    async function retrySelfieDailySchedule() {
      const button = $('ensureSelfieDailySchedule');
      if (button) { button.disabled = true; button.textContent = '正在恢复…'; }
      const result = await loadSelfieDailySchedule();
      if (result.loadSeq !== SELFIE_DAILY_LOAD_SEQ) return;
      if (result.wardrobe) renderWardrobe(result.wardrobe);
      renderSelfieDailySchedule(result.schedule, result.error || '服务暂未返回今日日程，请重试。');
      if (result.schedule?.schedule) showToast('今日日程已恢复', 'ok');
    }

    let WARDROBE_BATCH_BUSY = false;
    let WARDROBE_BATCH_CANCELLED = false;
    let WARDROBE_PENDING_UPLOADS = null;
    let WARDROBE_MAX_IMAGE_BYTES = 25 * 1024 * 1024;

    function renderWardrobe(payload) {
      const root = $('selfieWardrobeList');
      if (!root) return;
      const items = payload && Array.isArray(payload.items) ? payload.items : [];
      if ($('wardrobeSelectionMode')) $('wardrobeSelectionMode').value = payload?.selection_mode || 'random';
      if (payload?.max_image_bytes > 0) WARDROBE_MAX_IMAGE_BYTES = payload.max_image_bytes;
      if ($('wardrobeCurrent')) $('wardrobeCurrent').textContent = payload?.current_item_name
        ? `今日：${payload.current_item_name}${payload.manual_today ? ' · 手动指定' : ''}` : '今日服装：待选择';
      window.__selfieWardrobeItems = items;
      if (!items.length) {
        root.innerHTML = '<div class="muted">衣橱为空，添加服装图后普通自拍会优先使用。</div>';
        return;
      }
      root.innerHTML = items.map((item, index) => `<div class="daily-event wardrobe-item${item.id === payload?.current_item_id ? ' current' : ''}" data-wardrobe-id="${escapeHtml(item.id || '')}">
        <div class="daily-event-body">
          <div class="daily-event-heading"><input class="wardrobe-name" maxlength="120" value="${escapeHtml(item.name || '')}" aria-label="衣橱名称"><span class="daily-event-status">${index + 1} · ${item.enabled ? '启用' : '停用'}${item.id === payload?.current_item_id ? ' · 今日服装' : ''}</span></div>
          <div class="daily-event-fields"><span class="muted">${escapeHtml((item.tags || []).join(', '))}</span><span class="muted">${escapeHtml(item.id || '')}</span></div>
        </div>
        <div class="actions"><button type="button" class="secondary wardrobe-wear" ${item.enabled ? '' : 'disabled'}>今天穿这套</button><button type="button" class="secondary wardrobe-save">保存</button><button type="button" class="secondary wardrobe-toggle">${item.enabled ? '停用' : '启用'}</button><button type="button" class="danger wardrobe-delete">删除</button></div>
      </div>`).join('');
      root.querySelectorAll('.wardrobe-save').forEach(button => button.onclick = () => updateWardrobe(button.closest('[data-wardrobe-id]').dataset.wardrobeId));
      root.querySelectorAll('.wardrobe-toggle').forEach(button => button.onclick = () => toggleWardrobe(button.closest('[data-wardrobe-id]').dataset.wardrobeId));
      root.querySelectorAll('.wardrobe-delete').forEach(button => button.onclick = () => deleteWardrobe(button.closest('[data-wardrobe-id]').dataset.wardrobeId));
      root.querySelectorAll('.wardrobe-wear').forEach(button => button.onclick = () => wearWardrobe(button));
    }

    let SELFIE_IDENTITY_DATA = null;
    function identityPoolLimit(data) {
      const value = Number(data?.pool_limit);
      return Number.isFinite(value) && value > 0 ? Math.min(10, Math.max(1, Math.floor(value))) : 10;
    }
    function identityMode(data) {
      return data?.identity_mode === 'persona' || data?.use_persona_pool === true ? 'persona' : 'image';
    }
    function hasIdentityPoolPayload(data) {
      return Boolean(data && typeof data === 'object' && (
        'identity_mode' in data || 'use_persona_pool' in data || 'image_pool' in data || 'persona_pool' in data
      ));
    }
    function renderSelfieIdentity(data) {
      data = data || {};
      SELFIE_IDENTITY_DATA = data;
      const mode = identityMode(data);
      const limit = identityPoolLimit(data);
      const imagePool = Array.isArray(data.image_pool) ? data.image_pool : [];
      const personaPool = Array.isArray(data.persona_pool) ? data.persona_pool : [];
      const imageBySlot = new Map(imagePool.map(item => [Number(item.slot), item]));
      const personaBySlot = new Map(personaPool.map(item => [Number(item.slot), item]));
      const imagePanel = $('selfieImagePoolPanel');
      const personaPanel = $('selfiePersonaPoolPanel');
      if ($('selfiePersonaEnabled')) $('selfiePersonaEnabled').checked = mode === 'persona';
      if ($('useLogoWhenNoPersona')) $('useLogoWhenNoPersona').disabled = mode === 'persona';
      if (imagePanel) imagePanel.hidden = mode !== 'image';
      if (personaPanel) personaPanel.hidden = mode !== 'persona';
      if ($('selfieIdentityModeHint')) $('selfieIdentityModeHint').textContent = mode === 'persona'
        ? '当前使用文字人设池；切换不会修改另一种池中的内容。'
        : '当前使用形象图池；切换不会修改另一种池中的内容。';
      const activeImageSlot = Number(data.active_image_slot) || 0;
      const activePersonaSlot = Number(data.active_persona_slot) || 0;
      const imageRows = [];
      for (let slot = 1; slot <= limit; slot += 1) {
        const item = imageBySlot.get(slot);
        const occupied = Boolean(item && (item.mime_type || item.available !== undefined || item.active));
        const available = occupied && item.available !== false;
        const active = slot === activeImageSlot || item?.active === true;
        const status = !occupied ? '空位' : available ? (active ? '当前使用' : '可用') : '文件缺失';
        imageRows.push(`<div class="selfie-identity-row${active ? ' active' : ''}" data-selfie-image-slot="${slot}">
          <span class="selfie-identity-slot">${slot}</span>
          <div class="selfie-identity-copy"><strong>形象图 ${slot}</strong><span>${status}${occupied && item.mime_type ? ` · ${escapeHtml(item.mime_type)}` : ''}</span></div>
          <div class="actions">${occupied && available && !active ? '<button type="button" class="secondary selfie-image-select">选择</button>' : ''}${occupied ? '<button type="button" class="danger selfie-image-remove">移除</button>' : ''}</div>
        </div>`);
      }
      if ($('selfieImagePoolList')) $('selfieImagePoolList').innerHTML = imageRows.join('');
      const imageCount = imagePool.filter(item => item && (item.mime_type || item.available !== undefined)).length;
      if ($('selfieImagePoolCount')) $('selfieImagePoolCount').textContent = `${imageCount}/${limit}`;
      if ($('uploadSelfie')) {
        $('uploadSelfie').disabled = imageCount >= limit;
        $('uploadSelfie').title = imageCount >= limit ? '形象图池已满，请先移除一项' : '';
      }
      $('selfieImagePoolList')?.querySelectorAll('.selfie-image-select').forEach(button => {
        button.onclick = () => selfieIdentityAction({action:'image_select', slot:Number(button.closest('[data-selfie-image-slot]').dataset.selfieImageSlot)}, '形象图已切换');
      });
      $('selfieImagePoolList')?.querySelectorAll('.selfie-image-remove').forEach(button => {
        button.onclick = () => removeSelfieImage(Number(button.closest('[data-selfie-image-slot]').dataset.selfieImageSlot));
      });

      const personaRows = [];
      for (let slot = 1; slot <= limit; slot += 1) {
        const item = personaBySlot.get(slot);
        if (!item || (!item.name && !item.prompt)) continue;
        const active = slot === activePersonaSlot || item.active === true;
        personaRows.push(`<div class="selfie-identity-row${active ? ' active' : ''}" data-selfie-persona-slot="${slot}">
          <span class="selfie-identity-slot">${slot}</span>
          <div class="selfie-identity-copy"><strong>${escapeHtml(item.name || `人设 ${slot}`)}</strong><span>${active ? '当前使用 · ' : ''}${escapeHtml(item.prompt || '未填写描述')}</span></div>
          <div class="actions">${active ? '' : '<button type="button" class="secondary selfie-persona-select">选择</button>'}<button type="button" class="danger selfie-persona-remove">移除</button></div>
        </div>`);
      }
      if ($('selfiePersonaPoolList')) $('selfiePersonaPoolList').innerHTML = personaRows.length
        ? personaRows.join('') : '<div class="muted">暂无人设，请添加一个固定的人物描述。</div>';
      const personaCount = personaPool.filter(item => item && (item.name || item.prompt)).length;
      if ($('selfiePersonaPoolCount')) $('selfiePersonaPoolCount').textContent = `${personaCount}/${limit}`;
      if ($('addSelfiePersona')) {
        $('addSelfiePersona').disabled = personaCount >= limit;
        $('addSelfiePersona').title = personaCount >= limit ? '形象人设池已满，请先移除一项' : '';
      }
      $('selfiePersonaPoolList')?.querySelectorAll('.selfie-persona-select').forEach(button => {
        button.onclick = () => selfieIdentityAction({action:'persona_select', slot:Number(button.closest('[data-selfie-persona-slot]').dataset.selfiePersonaSlot)}, '形象人设已切换');
      });
      $('selfiePersonaPoolList')?.querySelectorAll('.selfie-persona-remove').forEach(button => {
        button.onclick = () => removeSelfiePersona(Number(button.closest('[data-selfie-persona-slot]').dataset.selfiePersonaSlot));
      });
      const image = data.has_image && String(data.image || '').startsWith('data:image/') ? String(data.image) : '';
      SELFIE_REFERENCE_DATA_URL = image;
      const preview = $('selfiePreview');
      if (preview && image && mode === 'image') {
        preview.src = image;
        preview.style.display = 'block';
      } else if (preview) {
        preview.removeAttribute('src');
        preview.style.display = 'none';
      }
      const appearanceLabel = ({auto:'自动', real:'真人', anime:'动漫'})[data.appearance_type || 'auto'] || '自动';
      if ($('selfieStatus')) $('selfieStatus').textContent = `${mode === 'persona' ? '使用形象人设池' : (data.has_image ? '形象参考图已设置' : '尚未设置形象参考图')} · 类型：${appearanceLabel}`;
    }
    async function selfieIdentityAction(payload, successMessage = '') {
      const mutationSeq = beginSelfieMutation();
      try {
        const res = await api('/api/selfie-identity', {method:'POST', body: JSON.stringify(payload)});
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        const data = res.data || {};
        renderSelfieIdentity(data);
        if (successMessage) showToast(successMessage, 'ok');
        return true;
      } catch (e) {
        if (mutationSeq === SELFIE_MUTATION_SEQ) {
          renderSelfieIdentity(SELFIE_IDENTITY_DATA || {});
          showToast(e.message || '形象池操作失败', 'bad');
        }
        return false;
      }
    }
    async function removeSelfieImage(slot) {
      if (!await confirmAction(`确定移除形象图 ${slot} 吗？编号会保留，后续添加会优先补这个空位。`, {title:'移除形象图', confirmText:'移除'})) return;
      await selfieIdentityAction({action:'image_remove', slot}, `形象图 ${slot} 已移除`);
    }
    async function removeSelfiePersona(slot) {
      if (!await confirmAction(`确定移除形象人设 ${slot} 吗？`, {title:'移除形象人设', confirmText:'移除'})) return;
      await selfieIdentityAction({action:'persona_remove', slot}, `形象人设 ${slot} 已移除`);
    }
    async function addSelfiePersona() {
      const button = $('addSelfiePersona');
      if (button?.disabled) return;
      const name = ($('selfiePersonaName')?.value || '').trim();
      const prompt = ($('selfiePersonaPrompt')?.value || '').trim();
      if (!name || !prompt) { showToast('请填写人设名称和形象描述', 'bad'); return; }
      if (button) button.disabled = true;
      try {
        const added = await selfieIdentityAction({action:'persona_add', name, prompt}, '形象人设已添加');
        if (added) {
          if ($('selfiePersonaName')) $('selfiePersonaName').value = '';
          if ($('selfiePersonaPrompt')) $('selfiePersonaPrompt').value = '';
        }
      } finally {
        if (button) button.disabled = (SELFIE_IDENTITY_DATA?.persona_pool?.length || 0) >= identityPoolLimit(SELFIE_IDENTITY_DATA);
      }
    }
    async function saveSelfieIdentityMode() {
      const checkbox = $('selfiePersonaEnabled');
      if (!checkbox) return;
      const requested = checkbox.checked ? 'persona' : 'image';
      const previous = identityMode(SELFIE_IDENTITY_DATA || {});
      const mutationSeq = beginSelfieMutation();
      checkbox.disabled = true;
      try {
        const res = await api('/api/selfie-identity', {method:'POST', body: JSON.stringify({action:'mode', identity_mode: requested})});
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        const data = res.data || {};
        renderSelfieIdentity(data);
        showToast(requested === 'persona' ? '已切换到形象人设池' : '已切换到形象图池', 'ok');
      } catch (e) {
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        checkbox.checked = previous === 'persona';
        renderSelfieIdentity(SELFIE_IDENTITY_DATA || {});
        showToast(e.message || '形象池切换失败', 'bad');
      } finally { checkbox.disabled = false; }
    }
    async function refreshSelfieIdentity(expectedMutationSeq = null) {
      const res = await api('/api/selfie-identity');
      const data = res.data || {};
      if (!hasIdentityPoolPayload(data)) throw Object.assign(new Error('形象池接口尚未提供完整状态'), {status: 404});
      if (expectedMutationSeq !== null && expectedMutationSeq !== SELFIE_MUTATION_SEQ) return null;
      renderSelfieIdentity(data);
      return data;
    }

    async function refreshSelfie(notify = false) {
      const mutationSeq = SELFIE_MUTATION_SEQ;
      const dailyLoadSeq = SELFIE_DAILY_LOAD_SEQ;
      try {
        let res;
        try {
          res = await api('/api/selfie-identity');
          if (!hasIdentityPoolPayload(res.data)) throw Object.assign(new Error('形象池接口尚未提供完整状态'), {status: 404});
        } catch (error) {
          if (error?.status !== 404 && error?.status !== 405) throw error;
          res = await api('/api/selfie-reference');
        }
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        const data = res.data || {};
        renderSelfieIdentity(data);
        SELFIE_REFERENCE_DATA_URL = data.has_image && String(data.image || '').startsWith('data:image/')
          ? String(data.image)
          : '';
        if (SELFIE_REFERENCE_DATA_URL && identityMode(data) === 'image') {
          $('selfiePreview').src = SELFIE_REFERENCE_DATA_URL;
          $('selfiePreview').style.display = 'block';
        } else {
          $('selfiePreview').removeAttribute('src');
          $('selfiePreview').style.display = 'none';
        }
        let dailySchedule = data.daily_schedule;
        let scheduleError = '';
        let scheduleLoadSeq = dailyLoadSeq;
        if (!Array.isArray(dailySchedule?.schedule?.events) || !dailySchedule.schedule.events.length) {
          const ensured = await loadSelfieDailySchedule();
          if (mutationSeq !== SELFIE_MUTATION_SEQ || ensured.loadSeq !== SELFIE_DAILY_LOAD_SEQ) return;
          scheduleLoadSeq = ensured.loadSeq;
          dailySchedule = ensured.schedule || dailySchedule;
          scheduleError = ensured.error || (
            Array.isArray(ensured.schedule?.schedule?.events) && ensured.schedule.schedule.events.length
              ? '' : '服务暂未返回今日日程，请点击重试。'
          );
          if (ensured.wardrobe) data.wardrobe = ensured.wardrobe;
        }
        if (mutationSeq !== SELFIE_MUTATION_SEQ || scheduleLoadSeq !== SELFIE_DAILY_LOAD_SEQ) return;
        renderSelfieDailySchedule(dailySchedule, scheduleError);
        renderWardrobe(data.wardrobe);
        if ($('selfieAppearanceType')) {
          $('selfieAppearanceType').value = data.appearance_type || 'auto';
        }
        if (notify) showToast('形象参考已刷新', 'ok');
      } catch (e) {
        if (mutationSeq !== SELFIE_MUTATION_SEQ || dailyLoadSeq !== SELFIE_DAILY_LOAD_SEQ) return;
        $('selfieStatus').textContent = '形象信息暂不可用';
        renderSelfieDailySchedule(null, e.message || '形象信息读取失败');
        showToast(e.message || '形象信息读取失败', 'bad');
      }
    }
    async function uploadWardrobe() {
      if (WARDROBE_BATCH_BUSY) return;
      const files = Array.from($('wardrobeFile')?.files || []);
      const name = ($('wardrobeName')?.value || '').trim();
      const queue = WARDROBE_PENDING_UPLOADS || files.map((file, index) => ({file,
        name: name ? (files.length > 1 ? `${name.slice(0, 110)} ${index + 1}` : name) : file.name.replace(/\.[^.]+$/, ''),
        description: ($('wardrobeDescription')?.value || '').trim(),
        tags: ($('wardrobeTags')?.value || '').trim(),
      }));
      if (!queue.length) { showToast('请选择服装图片', 'bad'); return; }
      WARDROBE_BATCH_BUSY = true;
      const batchMutationSeq = beginSelfieMutation();
      WARDROBE_BATCH_CANCELLED = false;
      const controlIds = ['uploadWardrobe', 'wardrobeFile', 'wardrobeName', 'wardrobeDescription', 'wardrobeTags', 'importWardrobeBtn'];
      controlIds.forEach(id => { if ($(id)) $(id).disabled = true; });
      $('wardrobeBatchProgressWrap').hidden = false;
      $('wardrobeBatchCancel').hidden = false;
      $('wardrobeBatchCancel').disabled = false;
      $('wardrobeBatchResult').textContent = '';
      $('wardrobeBatchProgress').max = queue.length;
      $('wardrobeBatchProgress').value = 0;
      let succeeded = 0, processed = 0;
      const pending = [], failures = [];
      try {
        for (const [index, task] of queue.entries()) {
          if (WARDROBE_BATCH_CANCELLED) { pending.push(...queue.slice(index)); break; }
          $('wardrobeBatchProgressLabel').textContent = `正在导入 ${index + 1}/${queue.length} · ${task.file.name}`;
          try {
            if (task.file.size > WARDROBE_MAX_IMAGE_BYTES) throw new Error('图片超过大小限制');
            const image = await readFileDataUrl(task.file);
            if (WARDROBE_BATCH_CANCELLED) { pending.push(...queue.slice(index)); break; }
            await api('/api/selfie-wardrobe/add', {method:'POST', body: JSON.stringify({
              image, mime_type: task.file.type, name: task.name, description: task.description, tags: task.tags,
            })});
            succeeded++;
          } catch (error) { pending.push(task); failures.push(`${task.file.name}：${error.message || '读取失败'}`); }
          processed++;
          $('wardrobeBatchProgress').value = processed;
        }
        WARDROBE_PENDING_UPLOADS = pending.length ? pending : null;
        if (!pending.length) $('wardrobeFile').value = '';
        const message = `${WARDROBE_BATCH_CANCELLED ? '已停止' : '导入完成'}：成功 ${succeeded} 张，失败 ${failures.length} 张${queue.length > processed ? `，未处理 ${queue.length - processed} 张` : ''}`;
        $('wardrobeBatchProgressLabel').textContent = message;
        $('wardrobeBatchResult').textContent = failures.join('\n');
        if (batchMutationSeq !== SELFIE_MUTATION_SEQ) return;
        await refreshSelfie();
        if (batchMutationSeq !== SELFIE_MUTATION_SEQ) return;
        showToast(message, failures.length ? 'bad' : 'ok');
      } finally {
        WARDROBE_BATCH_BUSY = false;
        controlIds.forEach(id => { if ($(id)) $(id).disabled = false; });
        $('wardrobeBatchCancel').hidden = true;
        $('uploadWardrobe').textContent = WARDROBE_PENDING_UPLOADS ? '重试未完成的图片' : '添加到衣橱';
      }
    }
    function cancelWardrobeBatch() {
      WARDROBE_BATCH_CANCELLED = true;
      $('wardrobeBatchCancel').disabled = true;
      $('wardrobeBatchProgressLabel').textContent = '正在停止，等待当前图片处理完成';
    }
    async function saveWardrobeMode() {
      const select = $('wardrobeSelectionMode');
      select.disabled = true;
      const mutationSeq = beginSelfieMutation();
      try {
        const res = await api('/api/selfie-wardrobe/settings', {method:'POST', body: JSON.stringify({selection_mode:select.value})});
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        renderWardrobe(res.data);
        showToast('轮换方式已保存，今天的服装保留', 'ok');
      } catch (e) {
        if (mutationSeq === SELFIE_MUTATION_SEQ) { showToast(e.message || '轮换方式保存失败', 'bad'); await refreshSelfie(); }
      }
      finally { select.disabled = false; }
    }
    async function wearWardrobe(button) {
      const id = button.closest('[data-wardrobe-id]').dataset.wardrobeId;
      button.disabled = true;
      const mutationSeq = beginSelfieMutation();
      try {
        const res = await api('/api/selfie-wardrobe/select', {method:'POST', body: JSON.stringify({id})});
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        renderSelfieDailySchedule(res.data?.daily_schedule);
        renderWardrobe(res.data?.wardrobe);
        showToast('今日服装已指定，明天继续自动轮换', 'ok');
      } catch (e) { if (mutationSeq === SELFIE_MUTATION_SEQ) showToast(e.message || '今日服装设置失败', 'bad'); }
      finally { button.disabled = false; }
    }
    async function updateWardrobe(id) {
      const row = document.querySelector(`[data-wardrobe-id="${CSS.escape(id)}"]`);
      if (!row) return;
      const mutationSeq = beginSelfieMutation();
      try {
        await api('/api/selfie-wardrobe/update', {method:'POST', body: JSON.stringify({id, name: row.querySelector('.wardrobe-name')?.value || ''})});
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        await refreshSelfie();
        showToast('衣橱服装已更新', 'ok');
      } catch (e) { if (mutationSeq === SELFIE_MUTATION_SEQ) showToast(e.message || '衣橱服装更新失败', 'bad'); }
    }
    async function toggleWardrobe(id) {
      const item = (window.__selfieWardrobeItems || []).find(value => String(value.id) === String(id));
      const mutationSeq = beginSelfieMutation();
      try {
        await api('/api/selfie-wardrobe/toggle', {method:'POST', body: JSON.stringify({id, enabled: !(item && item.enabled)})});
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        await refreshSelfie();
      } catch (e) { if (mutationSeq === SELFIE_MUTATION_SEQ) showToast(e.message || '衣橱服装状态更新失败', 'bad'); }
    }
    async function deleteWardrobe(id) {
      if (!await confirmAction('确定删除这套衣橱服装吗？', {title:'删除衣橱服装', confirmText:'删除'})) return;
      const mutationSeq = beginSelfieMutation();
      try {
        await api('/api/selfie-wardrobe/delete', {method:'POST', body: JSON.stringify({id})});
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        await refreshSelfie();
        showToast('衣橱服装已删除', 'ok');
      } catch (e) { if (mutationSeq === SELFIE_MUTATION_SEQ) showToast(e.message || '衣橱服装删除失败', 'bad'); }
    }
    async function refreshDailySelfieLocal() {
      if (!await confirmAction('重新生成会替换今天已有的安排，不会清空衣橱中的服装。', {title:'重新生成今日日程', confirmText:'重新生成'})) return;
      const mutationSeq = beginSelfieMutation();
      try {
        const res = await api('/api/selfie-profile/refresh-local', {method:'POST', body:'{}'});
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        renderSelfieDailySchedule(res.data && res.data.daily_schedule);
        renderWardrobe(res.data && res.data.wardrobe);
        showToast('今日日程已重新生成', 'ok');
      } catch (e) {
        if (mutationSeq === SELFIE_MUTATION_SEQ) showToast(e.message, 'bad');
      }
    }
    async function uploadSelfie() {
      const file = $('selfieFile').files[0];
      if (!file) { showToast('请选择图片', 'bad'); return; }
      const mutationSeq = beginSelfieMutation();
      const uploadButton = $('uploadSelfie');
      if (uploadButton) uploadButton.disabled = true;
      try {
        const image = await readFileDataUrl(file);
        const res = await api('/api/selfie-reference', {method:'POST', body: JSON.stringify({image, mime_type:file.type, filename:file.name})});
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        if (hasIdentityPoolPayload(res.data)) {
          renderSelfieIdentity(res.data);
          $('selfieFile').value = '';
          showToast('形象图已添加并选中', 'ok');
        } else {
          await refreshSelfie();
          showToast('参考图已更新', 'ok');
        }
      } catch (e) { if (mutationSeq === SELFIE_MUTATION_SEQ) showToast(e.message || '参考图上传失败', 'bad'); }
      finally {
        if (uploadButton) {
          const limit = identityPoolLimit(SELFIE_IDENTITY_DATA || {});
          const count = Array.isArray(SELFIE_IDENTITY_DATA?.image_pool)
            ? SELFIE_IDENTITY_DATA.image_pool.filter(item => item && (item.mime_type || item.available !== undefined)).length : 0;
          uploadButton.disabled = count >= limit;
        }
      }
    }
    async function clearSelfie() {
      const slot = Number(SELFIE_IDENTITY_DATA?.active_image_slot) || 0;
      if (slot) return removeSelfieImage(slot);
      if (!await confirmAction('确定清除当前自拍形象参考图吗？清除后后续生成将不再使用这张形象图。', {title:'清除形象参考图', confirmText:'清除参考图'})) return;
      const mutationSeq = beginSelfieMutation();
      try { await api('/api/selfie-reference/clear', {method:'POST', body:'{}'}); if (mutationSeq !== SELFIE_MUTATION_SEQ) return; await refreshSelfie(); showToast('参考图已清除', 'ok'); }
      catch (e) { if (mutationSeq === SELFIE_MUTATION_SEQ) showToast(e.message || '参考图清除失败', 'bad'); }
    }
    async function saveAppearanceType() {
      const mutationSeq = beginSelfieMutation();
      try {
        const appearance_type = ($('selfieAppearanceType') && $('selfieAppearanceType').value) || 'auto';
        const res = await api('/api/selfie-appearance-type', {method:'POST', body: JSON.stringify({appearance_type})});
        if (mutationSeq !== SELFIE_MUTATION_SEQ) return;
        const data = res.data || {};
        const hasIdentity = hasIdentityPoolPayload(data);
        if (hasIdentity) renderSelfieIdentity(data);
        if ($('selfieAppearanceType')) $('selfieAppearanceType').value = data.appearance_type || appearance_type;
        if (!hasIdentity) {
          const appearanceLabel = ({auto:'自动', real:'真人', anime:'动漫'})[data.appearance_type || appearance_type] || '自动';
          $('selfieStatus').textContent = `${data.has_image ? '形象参考图已设置' : '尚未设置形象参考图'} · 类型：${appearanceLabel}`;
        }
        showToast('形象类型已保存', 'ok');
      } catch (e) {
        if (mutationSeq === SELFIE_MUTATION_SEQ) showToast(e.message, 'bad');
      }
    }

    function escapeHtml(text) {
      return String(text ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    function escapeJs(text) {
      return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/\n/g, '\\n')
        .replace(/\r/g, '');
    }
    function setStatus(id, text) { $(id).textContent = text; }
    function setMultiStatus(text, type = 'info') {
      if (text) showToast(text, type);
    }
    let STARTUP_REFRESH_REQUEST = null;
    async function refreshDashboardAfterStartupReady() {
      if (STARTUP_REFRESH_REQUEST) return STARTUP_REFRESH_REQUEST;
      STARTUP_REFRESH_REQUEST = (async () => {
        for (let attempt = 0; attempt < 40; attempt += 1) {
          await new Promise(resolve => setTimeout(resolve, 1500));
          let health;
          try {
            const response = await api('/api/health');
            health = response.data || {};
          } catch (_) {
            continue;
          }
          const startup = health.startup || {};
          if (startup.status === 'error') {
            showToast(startup.error || '启动数据加载失败，请查看运行状态', 'bad');
            return false;
          }
          if (startup.status !== 'ready') continue;
          // The first login request may have observed the temporary empty
          // state. Refresh only pages the user has actually opened.
          const refreshes = [];
          if (DASHBOARD_VISITED_TABS.has('monitor')) {
            invalidateMetricsRequests();
            TASK_LOAD_SEQ += 1;
            TASK_REQUESTS.clear();
            refreshes.push(loadRecords(true), loadMetrics(), loadTasks(false));
          }
          if (DASHBOARD_VISITED_TABS.has('assets')) refreshes.push(loadAssets(true), loadAssetTags(true));
          await Promise.all(refreshes);
          await checkHealth();
          return true;
        }
        return false;
      })().finally(() => { STARTUP_REFRESH_REQUEST = null; });
      return STARTUP_REFRESH_REQUEST;
    }
    async function enterApp(silent = false) {
      if (!silent && $('loginStatus')) $('loginStatus').textContent = '登录中...';
      if (isDashboardPage()) {
        AUTH_TOKEN = '';
        document.body.classList.add('dashboard-embedded');
        if ($('loginPage')) $('loginPage').style.display = 'none';
        if ($('logoutBtn')) $('logoutBtn').style.display = 'none';
      } else {
        AUTH_TOKEN = ($('loginToken')?.value || '').trim();
      }
      ensureProtectedMediaScope();
      // A standalone page may boot before the user enters a token. Hydrate the
      // token-scoped page/catalog cache after authentication is established.
      await ensureDashboardPersistentCachesHydrated();
      try {
        const res = await api('/api/config');
        CONFIG = res.data || {};
        if (Number(res.revision) > 0) CONFIG_REVISION = Number(res.revision);
        rememberConfigServerSnapshot(CONFIG);
        markConfigLoaded();
        if (!isDashboardPage()) {
          if (AUTH_TOKEN) safeStorageSet('selfieImageToken', AUTH_TOKEN);
          else safeStorageRemove('selfieImageToken');
          safeStorageRemove('aicatToken');
        }
        fillForms();
        document.body.classList.add('authed');
        if (isDashboardPage()) {
          document.body.classList.add('dashboard-embedded');
          document.documentElement.classList.add('embedded-dashboard', 'embedded-dashboard-ready');
          if ($('loginPage')) $('loginPage').style.display = 'none';
          if ($('logoutBtn')) $('logoutBtn').style.display = 'none';
        }
        if ($('loginStatus')) $('loginStatus').textContent = '';
        const health = await checkHealth();
        // The base tab only needs config and health. Heavy data pages load when
        // their tab is first opened and reuse their own caches thereafter.
        await resumeImageTestTask();
        if (health?.status === 'starting' || health?.startup?.status === 'loading') {
          void refreshDashboardAfterStartupReady();
        }
      } catch (e) {
        document.body.classList.remove('authed');
        const message = e.message || '登录失败';
        if (isDashboardPage()) {
          document.documentElement.classList.add('embedded-dashboard');
          document.documentElement.classList.remove('embedded-dashboard-ready');
          if ($('loginPage')) $('loginPage').style.display = 'none';
          if ($('embeddedBootPage')) $('embeddedBootPage').style.display = '';
          if ($('embeddedBootMessage')) $('embeddedBootMessage').textContent = message;
          if ($('embeddedBootRetry')) $('embeddedBootRetry').hidden = false;
          return;
        }
        if (!silent || AUTH_TOKEN) {
          if ($('loginStatus')) $('loginStatus').textContent = message;
        }
      }
    }
    function logout() {
      if (isDashboardPage()) {
        showToast('当前已登录，无需退出', 'ok');
        return;
      }
      AUTH_TOKEN = '';
      clearProtectedMediaCache();
      clearGalleryPickerCache();
      clearRecordPageCache();
      clearAssetPageCache();
      clearPickerCache();
      clearAssetCollectionsCache();
      for (const namespace of ['record-page', 'asset-page', 'picker-page', 'managed-page', 'asset-tags', 'gallery-picker', 'asset-collections']) {
        clearDashboardPersistNamespace(namespace);
      }
      safeStorageRemove('selfieImageToken');
      safeStorageRemove('aicatToken');
      if ($('loginToken')) $('loginToken').value = '';
      document.body.classList.remove('authed');
      if ($('loginStatus')) $('loginStatus').textContent = '已退出登录';
    }
    function setupAutoSave() {
      // 仅配置型页面字段自动保存；渠道弹窗/试画/记录/高级 JSON 排除。
      // 渠道新增/编辑：点「保存渠道」；列表启停/删/复制/优先级：scheduleChannelListAutoSave。
      const allowSections = new Set(['base', 'image', 'selfie', 'audit']);
      document.querySelectorAll('main.app-shell input, main.app-shell select, main.app-shell textarea').forEach(el => {
        if (el.type === 'file' || el.id === 'configText') return;
        if (el.closest('#channelModal') || el.closest('#recordModal')) return;
        if (el.closest('#test') || el.closest('#monitor') || el.closest('#assets') || el.closest('#raw')) return;
        // 渠道页里除 priority 隐藏域外，列表操作走专用 schedule，不绑通用 input 自动保存
        if (el.closest('#channels') && el.id !== 'priorityList') return;
        const section = el.closest('section');
        if (section && section.id && !allowSections.has(section.id) && section.id !== 'channels') return;
        const eventName = el.tagName === 'SELECT' || el.type === 'checkbox' || el.type === 'number' ? 'change' : 'input';
        el.addEventListener(eventName, () => {
          // A pending autosave is still local state. Invalidate config reads
          // immediately so changing tabs cannot repaint the old server form.
          CONFIG_MUTATION_SEQ += 1;
          CONFIG_LOADED_MUTATION_SEQ = -1;
          CONFIG_LOADED_AT = 0;
          scheduleFormAutoSave();
        });
      });
    }


    function studioCacheUrl(path) {
      return cacheImageUrl(path);
    }
    async function studioApi(path, opts = {}) {
      return api(path, opts);
    }
    function studioStopPoll() {
      if (STUDIO.pollTimer) { clearInterval(STUDIO.pollTimer); STUDIO.pollTimer = null; }
    }
    function fillStudioAspect() {
      const aspects = ['自动','1:1','2:3','3:2','3:4','4:3','4:5','5:4','9:16','16:9','21:9'];
      setSelectOptions('studioAspect', aspects, (STUDIO.current && STUDIO.current.graph && STUDIO.current.graph.aspect_ratio) || '自动');
    }
    function renderStudioTemplateSelect() {
      const el = $('studioTemplateSelect');
      if (!el) return;
      const cur = el.value || 'duo';
      el.innerHTML = '';
      const list = STUDIO.templates && STUDIO.templates.length ? STUDIO.templates : [
        {id:'duo', title:'双人合影'}, {id:'group', title:'多人合影'}, {id:'selfie', title:'自拍 / 看看 / 看看腿', description:'看看腿会按姿势搭配光腿神器、白丝、黑丝或连裤袜'},
        {id:'clothes', title:'换装 / COS'}, {id:'i2i', title:'图生图精修'}, {id:'t2i', title:'文生图'}, {id:'blank', title:'空白'}
      ];
      for (const t of list) {
        const opt = document.createElement('option');
        opt.value = t.id;
        opt.textContent = (t.title || t.id) + (t.description ? ' · ' + t.description : '');
        if (t.id === cur) opt.selected = true;
        el.appendChild(opt);
      }
      if (![...el.options].some(o => o.selected) && el.options.length) el.selectedIndex = 0;
    }
    function studioActiveTemplateId() {
      if (STUDIO.current && STUDIO.current.template) return String(STUDIO.current.template);
      return ($('studioTemplateSelect') && $('studioTemplateSelect').value) || 'duo';
    }
