    function creativeCanvasApi(path, opts = {}) { return api('/api/creative-canvas' + path, opts); }
    function creativeCanvasTemplateList() {
      return CREATIVE_CANVAS.templates?.length ? CREATIVE_CANVAS.templates : [
        {id:'duo', title:'双人合影'}, {id:'group', title:'多人合影'},
        {id:'selfie', title:'自拍 / 看看 / 看看腿'}, {id:'clothes', title:'换装 / COS'},
        {id:'i2i', title:'图生图精修'}, {id:'t2i', title:'文生图'}, {id:'blank', title:'空白'}
      ];
    }
    function renderCreativeCanvasTemplateSelect() {
      const el = $('creativeCanvasTemplateSelect');
      if (!el) return;
      const current = el.value || 'duo';
      el.innerHTML = creativeCanvasTemplateList().map(item => `<option value="${escapeHtml(item.id || '')}">${escapeHtml(item.title || item.id || '')}${item.description ? ' · ' + escapeHtml(item.description) : ''}</option>`).join('');
      if ([...el.options].some(item => item.value === current)) el.value = current;
    }
    function syncCreativeCanvasVisibility() {
      const has = !!CREATIVE_CANVAS.current?.id;
      if ($('creativeCanvasEditor')) $('creativeCanvasEditor').hidden = !has;
      if ($('creativeCanvasEmptyState')) $('creativeCanvasEmptyState').hidden = has;
      if ($('creativeCanvasDeleteBtn')) $('creativeCanvasDeleteBtn').disabled = !has;
      const last = CREATIVE_CANVAS.current?.last_run || {};
      const hasSuccess = String(last.status || '').toLowerCase() === 'succeeded' || (CREATIVE_CANVAS.current?.results || []).length > 0;
      if ($('creativeCanvasRunBtn')) $('creativeCanvasRunBtn').textContent = hasSuccess ? '生成图片' : '保存后生成';
      if ($('creativeCanvasRerunBtn')) {
        $('creativeCanvasRerunBtn').disabled = !hasSuccess || CREATIVE_CANVAS.running;
        $('creativeCanvasRerunBtn').hidden = !hasSuccess;
      }
    }
    function renderCreativeCanvasSessionSelect() {
      const select = $('creativeCanvasSessionSelect');
      if (!select) return;
      select.innerHTML = '<option value="">选择画布…</option>' + (CREATIVE_CANVAS.sessions || []).map(item => {
        const id = String(item.id || '');
        const suffix = id.slice(-4).toUpperCase();
        const template = item.template || item.template_id || '';
        const status = item.last_status || '';
        const detail = [template, Number(item.result_count || 0) ? `${Number(item.result_count)} 图` : '', status, suffix ? `#${suffix}` : ''].filter(Boolean).join(' · ');
        return `<option value="${escapeHtml(id)}">${escapeHtml(item.title || '未命名')} · ${escapeHtml(detail)}</option>`;
      }).join('');
      if (CREATIVE_CANVAS.current?.id) select.value = CREATIVE_CANVAS.current.id;
    }
    function fillCreativeCanvasAspect(value = '自动') {
      setSelectOptions('creativeCanvasAspect', ['自动','1:1','2:3','3:2','3:4','4:3','4:5','5:4','9:16','16:9','21:9'], value || '自动');
    }
    function renderCreativeCanvasPresetPanel() {
      renderPresetPanel('creativeCanvasPresetPanel', 'creativeCanvasPrompt', !!CREATIVE_CANVAS.presetOpen, 'creativeCanvasPresetBtn');
    }
    function renderCreativeCanvasCosPanel() {
      if (PICKER_LOADING.has('creativeCanvasCosPanel')) {
        const panel = $('creativeCanvasCosPanel');
        if (panel) { panel.classList.add('open'); panel.innerHTML = '<div class="status">正在加载 COS 池…</div>'; }
        return;
      }
      renderCosPanel('creativeCanvasCosPanel', 'creativeCanvasPrompt', !!CREATIVE_CANVAS.cosOpen, 'creativeCanvasCosBtn');
    }
    async function toggleCreativeCanvasPresets() {
      CREATIVE_CANVAS.presetOpen = !CREATIVE_CANVAS.presetOpen;
      CREATIVE_CANVAS.cosOpen = false;
      if (CREATIVE_CANVAS.presetOpen) {
        const state = catalogPickerState('creativeCanvasPresetPanel');
        await loadCatalogPickerPage('creativeCanvasPresetPanel', 'image', state.page);
      }
      renderCreativeCanvasPresetPanel(); renderCreativeCanvasCosPanel();
    }
    async function toggleCreativeCanvasCos() {
      CREATIVE_CANVAS.cosOpen = !CREATIVE_CANVAS.cosOpen;
      CREATIVE_CANVAS.presetOpen = false;
      if (CREATIVE_CANVAS.cosOpen) {
        const state = catalogPickerState('creativeCanvasCosPanel');
        await loadCatalogPickerPage('creativeCanvasCosPanel', 'cos', state.page);
      }
      renderCreativeCanvasPresetPanel(); renderCosPanel('creativeCanvasCosPanel', 'creativeCanvasPrompt', CREATIVE_CANVAS.cosOpen, 'creativeCanvasCosBtn');
    }
    function renderCreativeCanvasSlots() {
      const wrap = $('creativeCanvasSlots');
      const session = CREATIVE_CANVAS.current;
      if (!wrap) return;
      if (!session) { wrap.innerHTML = '<div class="status">还没有创作画布</div>'; return; }
      wrap.innerHTML = '';
      (session.slots || []).forEach((slot, index, slots) => {
        const card = document.createElement('div');
        const has = !!slot.image_path;
        card.className = 'studio-slot creative-canvas-slot ' + (has ? 'has-image' : 'empty');
        card.dataset.slotId = slot.id || '';
        card.innerHTML = `<div class="slot-label">${escapeHtml(slot.label || slot.role || '参考图')}</div>${has ? `<img data-cache-path="${escapeHtml(slot.image_path)}" alt="">` : '<div class="status">暂无参考图</div>'}<div class="slot-actions"><button type="button" class="secondary mini" data-slot-action="upload">上传</button><button type="button" class="secondary mini" data-slot-action="record">图库</button><button type="button" class="secondary mini" data-slot-action="up" ${index === 0 ? 'disabled' : ''}>上移</button><button type="button" class="secondary mini" data-slot-action="down" ${index === slots.length - 1 ? 'disabled' : ''}>下移</button><button type="button" class="secondary mini" data-slot-action="clear" ${has ? '' : 'disabled'}>清空</button></div>`;
        card.querySelector('[data-slot-action="upload"]').onclick = () => { CREATIVE_CANVAS.uploadSlotId = slot.id; $('creativeCanvasFilePick')?.click(); };
        card.querySelector('[data-slot-action="record"]').onclick = () => openCreativeCanvasGallery(slot.id);
        card.querySelector('[data-slot-action="up"]').onclick = () => moveCreativeCanvasSlot(index, -1);
        card.querySelector('[data-slot-action="down"]').onclick = () => moveCreativeCanvasSlot(index, 1);
        card.querySelector('[data-slot-action="clear"]').onclick = () => setCreativeCanvasSlot(slot.id, {clear:true});
        wrap.appendChild(card);
      });
      wrap.dataset.protectedMediaQueued = 'true';
      loadProtectedImages(wrap);
    }
    async function setCreativeCanvasSlot(slotId, payload) {
      if (!CREATIVE_CANVAS.current) return;
      const res = await creativeCanvasApi(`/sessions/${encodeURIComponent(CREATIVE_CANVAS.current.id)}/slots/${encodeURIComponent(slotId)}`, {method:'POST', body:JSON.stringify(payload || {})});
      if (!res.success) return showToast(res.message || '参考图操作失败', 'bad');
      CREATIVE_CANVAS.current = res.data; renderCreativeCanvas();
    }
    async function moveCreativeCanvasSlot(index, delta) {
      if (!CREATIVE_CANVAS.current) return;
      const slots = [...(CREATIVE_CANVAS.current.slots || [])]; const next = index + delta;
      if (next < 0 || next >= slots.length) return;
      [slots[index], slots[next]] = [slots[next], slots[index]];
      const res = await creativeCanvasApi(`/sessions/${encodeURIComponent(CREATIVE_CANVAS.current.id)}/reorder`, {method:'POST', body:JSON.stringify({order:slots.map(item => item.id)})});
      if (!res.success) return showToast(res.message || '排序失败', 'bad');
      CREATIVE_CANVAS.current = res.data; renderCreativeCanvas();
    }
    async function addCreativeCanvasSlot() {
      if (!CREATIVE_CANVAS.current) return;
      const res = await creativeCanvasApi(`/sessions/${encodeURIComponent(CREATIVE_CANVAS.current.id)}/slots`, {method:'POST', body:JSON.stringify({role:'extra',label:'额外参考'})});
      if (!res.success) return showToast(res.message || '添加参考图失败', 'bad');
      CREATIVE_CANVAS.current = res.data; renderCreativeCanvas();
    }
    async function uploadCreativeCanvasFile(file) {
      if (!file || !CREATIVE_CANVAS.current || !CREATIVE_CANVAS.uploadSlotId) return;
      const dataUrl = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
      await setCreativeCanvasSlot(CREATIVE_CANVAS.uploadSlotId, {image:dataUrl, source:'upload'});
      CREATIVE_CANVAS.uploadSlotId = '';
    }
    function closeCreativeCanvasGallery() {
      CREATIVE_CANVAS.galleryRequestSeq += 1;
      document.querySelectorAll('#creativeCanvasSlots .creative-canvas-slot-picker').forEach(panel => {
        const card = panel.closest('.studio-slot');
        if (card) card.classList.remove('picking-record');
        panel.remove();
      });
      CREATIVE_CANVAS.galleryTargetSlotId = '';
    }
    function creativeCanvasGalleryTargetSlot(slotId = '') {
      const slots = CREATIVE_CANVAS.current?.slots || [];
      const requested = String(slotId || '').trim();
      if (requested) return slots.some(item => String(item.id || '') === requested) ? requested : '';
      return String(slots.find(item => !item.image_path)?.id || '').trim();
    }
    function setCreativeCanvasGallerySelection(panel, cell, item) {
      panel.querySelectorAll('.studio-gallery-item.selected').forEach(node => node.classList.remove('selected'));
      cell.classList.add('selected');
      panel.dataset.selectedPath = String(item.path || '');
      panel.dataset.selectedRecordId = String(item.record_id || '');
      const confirm = panel.querySelector('[data-creative-gallery-confirm]');
      if (confirm) confirm.disabled = !panel.dataset.selectedPath;
    }
    async function openCreativeCanvasGallery(slotId = '') {
      if (!CREATIVE_CANVAS.current) return;
      const targetSlotId = creativeCanvasGalleryTargetSlot(slotId);
      if (!targetSlotId) return showToast('请先添加一个空的参考图槽位', 'bad');
      const card = [...document.querySelectorAll('#creativeCanvasSlots .studio-slot')].find(item => item.dataset.slotId === targetSlotId);
      if (!card) return showToast('参考图槽位已刷新，请重试', 'bad');
      closeCreativeCanvasGallery();
      CREATIVE_CANVAS.galleryTargetSlotId = targetSlotId;
      const requestSeq = CREATIVE_CANVAS.galleryRequestSeq;
      card.classList.add('picking-record');
      const panel = document.createElement('div');
      panel.className = 'studio-slot-picker creative-canvas-slot-picker';
      panel.innerHTML = '<div class="status">加载记录图…</div>';
      card.appendChild(panel);
      const res = await loadGalleryPickerItems();
      if (requestSeq !== CREATIVE_CANVAS.galleryRequestSeq || !panel.isConnected) return;
      if (!res.success) {
        panel.innerHTML = `<div class="between"><div class="status">${escapeHtml(res.message || '加载失败')}</div><button type="button" class="secondary mini" data-creative-gallery-close>收起</button></div>`;
        panel.querySelector('[data-creative-gallery-close]').onclick = closeCreativeCanvasGallery;
        return;
      }
      const items = res.data?.items || [];
      if (!items.length) {
        panel.innerHTML = '<div class="between"><div class="status">暂无可用图片</div><button type="button" class="secondary mini" data-creative-gallery-close>收起</button></div>';
        panel.querySelector('[data-creative-gallery-close]').onclick = closeCreativeCanvasGallery;
        return;
      }
      panel.innerHTML = `<div class="between"><div class="status">选择图片后点击确定</div><div class="actions" style="margin-top:0"><button type="button" class="secondary mini" data-creative-gallery-close>收起</button><button type="button" class="ok mini" data-creative-gallery-confirm disabled>确定</button></div></div><div class="studio-gallery-grid"></div>`;
      panel.querySelector('[data-creative-gallery-close]').onclick = closeCreativeCanvasGallery;
      panel.querySelector('[data-creative-gallery-confirm]').onclick = () => pickCreativeCanvasGallerySelection(panel);
      const grid = panel.querySelector('.studio-gallery-grid');
      panel.dataset.galleryOffset = '0';
      renderGalleryPage(panel, items, (item, cell) => setCreativeCanvasGallerySelection(panel, cell, item));
    }
    async function pickCreativeCanvasGallerySelection(panel) {
      const imagePath = String(panel?.dataset.selectedPath || '').trim();
      const slotId = String(CREATIVE_CANVAS.galleryTargetSlotId || '').trim();
      if (!imagePath || !slotId || !CREATIVE_CANVAS.current) return;
      const confirm = panel.querySelector('[data-creative-gallery-confirm]');
      if (confirm) { confirm.disabled = true; confirm.textContent = '保存中…'; }
      const res = await creativeCanvasApi(`/sessions/${encodeURIComponent(CREATIVE_CANVAS.current.id)}/slots/${encodeURIComponent(slotId)}`, {
        method:'POST', body:JSON.stringify({image_path:imagePath, source:'record', source_record_id:String(panel.dataset.selectedRecordId || '')})
      });
      if (!res.success) {
        if (confirm) { confirm.disabled = false; confirm.textContent = '确定'; }
        return showToast(res.message || '放入失败', 'bad');
      }
      CREATIVE_CANVAS.current = res.data;
      closeCreativeCanvasGallery();
      renderCreativeCanvas();
      showToast('已从图库放入参考图', 'ok');
    }
    function renderCreativeCanvasResults() {
      const wrap = $('creativeCanvasResults'); if (!wrap) return;
      wrap.innerHTML = '';
      for (const item of CREATIVE_CANVAS.current?.results || []) {
        const card = document.createElement('div'); card.className = 'card';
        card.innerHTML = `${item.image_path ? `<img data-cache-path="${escapeHtml(item.image_path)}" alt="">` : '<div class="status">无图</div>'}<div class="actions"><button type="button" class="secondary mini" data-result-role="base">作底图</button><button type="button" class="secondary mini" data-result-role="outfit">作服装</button><button type="button" class="secondary mini" data-result-role="peer">作同框</button><button type="button" class="secondary mini" data-result-role="identity">作形象</button></div><div class="status" style="font-size:11px">${escapeHtml(item.created_at || '')}${item.used_model ? ' · ' + escapeHtml(item.used_model) : ''}</div>`;
        card.querySelectorAll('[data-result-role]').forEach(button => button.onclick = () => promoteCreativeCanvasResult(item.id, button.dataset.resultRole));
        wrap.appendChild(card);
      }
      wrap.dataset.protectedMediaQueued = 'true';
      loadProtectedImages(wrap);
    }
    async function promoteCreativeCanvasResult(resultId, role) {
      if (!CREATIVE_CANVAS.current) return;
      const res = await creativeCanvasApi(`/sessions/${encodeURIComponent(CREATIVE_CANVAS.current.id)}/promote`, {method:'POST', body:JSON.stringify({result_id:resultId, role, create_if_missing:true})});
      if (!res.success) return showToast(res.message || '放入参考图失败', 'bad');
      CREATIVE_CANVAS.current = res.data; renderCreativeCanvas(); showToast('已放入参考图', 'ok');
    }
    function renderCreativeCanvas() {
      syncCreativeCanvasVisibility(); renderCreativeCanvasSessionSelect(); renderCreativeCanvasTemplateSelect();
      if (!CREATIVE_CANVAS.current) return;
      const session = CREATIVE_CANVAS.current; const graph = session.graph || {};
      $('creativeCanvasTitle').value = session.title || '';
      $('creativeCanvasMode').value = graph.mode || 'group';
      fillCreativeCanvasAspect(graph.aspect_ratio || '自动');
      $('creativeCanvasResolution').value = graph.resolution || '1K';
      $('creativeCanvasCount').value = graph.count || 1;
      $('creativeCanvasUsePersona').checked = graph.use_persona_identity !== false;
      $('creativeCanvasPrompt').value = graph.prompt || '';
      const last = session.last_run || {};
      $('creativeCanvasStatus').textContent = last.status === 'running' ? '生成中…' : last.status === 'succeeded' ? `完成 · ${(last.result_paths || []).length} 张` : last.status === 'failed' ? `失败：${last.error || ''}` : '已加载：' + (session.title || session.id);
      renderCreativeCanvasPresetPanel(); renderCreativeCanvasCosPanel(); renderCreativeCanvasSlots(); renderCreativeCanvasResults();
    }
    async function loadCreativeCanvasSessionList(force = false) {
      const fresh = !force && CREATIVE_CANVAS.sessionsLoadedAt && (Date.now() - CREATIVE_CANVAS.sessionsLoadedAt) < CANVAS_SESSION_CACHE_TTL_MS;
      if (fresh) return {success:true, data:{sessions:CREATIVE_CANVAS.sessions, templates:CREATIVE_CANVAS.templates}};
      if (CREATIVE_CANVAS.sessionsRequest) return CREATIVE_CANVAS.sessionsRequest;
      const request = creativeCanvasApi('/sessions').then(res => {
        if (!res.success) return res;
        CREATIVE_CANVAS.sessions = res.data?.sessions || [];
        CREATIVE_CANVAS.templates = res.data?.templates || CREATIVE_CANVAS.templates;
        CREATIVE_CANVAS.sessionsLoadedAt = Date.now();
        return res;
      }).finally(() => { CREATIVE_CANVAS.sessionsRequest = null; });
      CREATIVE_CANVAS.sessionsRequest = request;
      return request;
    }
    async function loadCreativeCanvasList(selectId = '', options = {}) {
      const force = options === true || options?.force === true;
      const id = String(selectId || CREATIVE_CANVAS.current?.id || '').trim();
      if (id && CREATIVE_CANVAS.current?.id && id !== String(CREATIVE_CANVAS.current.id)) {
        stopCreativeCanvasPoll();
        CREATIVE_CANVAS.running = false;
        CREATIVE_CANVAS.taskId = '';
      }
      const loadSeq = ++CREATIVE_CANVAS.loadSeq;
      const needsDetail = Boolean(id) && (force || CREATIVE_CANVAS.current?.id !== id);
      const detailRequest = needsDetail
        ? creativeCanvasApi('/sessions/' + encodeURIComponent(id))
        : Promise.resolve(null);
      const [res, detail] = await Promise.all([loadCreativeCanvasSessionList(force), detailRequest]);
      if (!res.success) return showToast(res.message || '创作画布列表加载失败', 'bad');
      if (id && needsDetail) {
        if (loadSeq !== CREATIVE_CANVAS.loadSeq) return;
        if (detail.success) CREATIVE_CANVAS.current = detail.data;
      } else if (!id) {
        CREATIVE_CANVAS.current = null;
      }
      if (loadSeq !== CREATIVE_CANVAS.loadSeq) return;
      renderCreativeCanvas();
      const last = CREATIVE_CANVAS.current?.last_run || {};
      const activeTaskId = String(last.task_id || '').trim();
      if (activeTaskId && ['queued','running'].includes(String(last.status || '').toLowerCase())) {
        CREATIVE_CANVAS.taskId = activeTaskId;
        CREATIVE_CANVAS.running = true;
        if ($('creativeCanvasCancelBtn')) $('creativeCanvasCancelBtn').style.display = '';
        creativeCanvasPollTask(activeTaskId);
      }
    }
    function openCreativeCanvasCreateModal() { renderCreativeCanvasTemplateSelect(); $('creativeCanvasCreateTitle').value = ''; $('creativeCanvasCreateStatus').textContent = ''; if ($('creativeCanvasCreateConfirmBtn')) $('creativeCanvasCreateConfirmBtn').disabled = false; $('creativeCanvasCreateModal').classList.add('show'); }
    function closeCreativeCanvasCreateModal() { $('creativeCanvasCreateModal')?.classList.remove('show'); }
    async function createCreativeCanvas() {
      const confirm = $('creativeCanvasCreateConfirmBtn');
      const status = $('creativeCanvasCreateStatus');
      if (confirm) confirm.disabled = true;
      if (status) status.textContent = '创建中…';
      try {
        const title = $('creativeCanvasCreateTitle').value.trim();
        const template = $('creativeCanvasTemplateSelect').value || 'duo';
        const res = await creativeCanvasApi('/sessions', {method:'POST', body:JSON.stringify({title, template})});
        if (!res.success) throw new Error(res.message || '创建失败');
        CREATIVE_CANVAS.current = res.data;
        const session = res.data || {};
        CREATIVE_CANVAS.sessions = [{
          id: session.id,
          title: session.title || title,
          updated_at: session.updated_at || session.created_at || '',
          template: session.template || template,
          result_count: (session.results || []).length,
          last_status: session.last_run?.status || '',
          last_run: session.last_run || null,
        }, ...(CREATIVE_CANVAS.sessions || []).filter(item => item.id !== session.id)];
        CREATIVE_CANVAS.sessionsLoadedAt = Date.now();
        closeCreativeCanvasCreateModal();
        renderCreativeCanvas();
        showToast('已创建创作画布', 'ok');
      } catch (error) {
        if (status) status.textContent = error.message || '创建失败';
        if (confirm) confirm.disabled = false;
      }
    }
    async function saveCreativeCanvas() {
      if (!CREATIVE_CANVAS.current) return false;
      const body = {title:$('creativeCanvasTitle').value.trim(), graph:{prompt:$('creativeCanvasPrompt').value, mode:$('creativeCanvasMode').value, aspect_ratio:$('creativeCanvasAspect').value, resolution:$('creativeCanvasResolution').value, count:Number($('creativeCanvasCount').value || 1), use_persona_identity:$('creativeCanvasUsePersona').checked}};
      const res = await creativeCanvasApi('/sessions/' + encodeURIComponent(CREATIVE_CANVAS.current.id), {method:'POST', body:JSON.stringify(body)});
      if (!res.success) { showToast(res.message || '保存失败', 'bad'); return false; }
      CREATIVE_CANVAS.current = res.data; renderCreativeCanvas(); showToast('创作画布已保存', 'ok');
      return true;
    }
    async function deleteCreativeCanvas() {
      if (!CREATIVE_CANVAS.current || !await confirmAction(`确定删除创作画布「${CREATIVE_CANVAS.current.title || '当前画布'}」吗？`, {title:'删除画布', confirmText:'删除画布'})) return;
      const res = await creativeCanvasApi('/sessions/' + encodeURIComponent(CREATIVE_CANVAS.current.id) + '/delete', {method:'POST', body:'{}'});
      if (!res.success) return showToast(res.message || '删除失败', 'bad');
      CREATIVE_CANVAS.current = null; await loadCreativeCanvasList('', {force:true}); showToast('创作画布已删除', 'ok');
    }
    async function runCreativeCanvas() {
      if (!CREATIVE_CANVAS.current || CREATIVE_CANVAS.running) return;
      CREATIVE_CANVAS.running = true;
      if ($('creativeCanvasRunBtn')) { $('creativeCanvasRunBtn').disabled = true; $('creativeCanvasRunBtn').textContent = '保存中…'; }
      if ($('creativeCanvasRerunBtn')) $('creativeCanvasRerunBtn').disabled = true;
      if ($('creativeCanvasCancelBtn')) $('creativeCanvasCancelBtn').style.display = '';
      $('creativeCanvasStatus').textContent = '正在保存并提交…';
      try {
        if (!await saveCreativeCanvas()) throw new Error('保存失败，未启动生成');
        const res = await creativeCanvasApi('/sessions/' + encodeURIComponent(CREATIVE_CANVAS.current.id) + '/run', {method:'POST', body:'{}'});
        if (!res.success) throw new Error(res.message || '启动失败');
        CREATIVE_CANVAS.taskId = res.data?.task_id || '';
        if (!CREATIVE_CANVAS.taskId) throw new Error('后台未返回任务 ID');
        $('creativeCanvasStatus').textContent = '已提交生成…';
        creativeCanvasPollTask(CREATIVE_CANVAS.taskId);
      } catch (error) {
        CREATIVE_CANVAS.running = false; CREATIVE_CANVAS.taskId = '';
        if ($('creativeCanvasRunBtn')) { $('creativeCanvasRunBtn').disabled = false; $('creativeCanvasRunBtn').textContent = '保存后生成'; }
        if ($('creativeCanvasRerunBtn')) $('creativeCanvasRerunBtn').disabled = true;
        if ($('creativeCanvasCancelBtn')) { $('creativeCanvasCancelBtn').style.display = 'none'; $('creativeCanvasCancelBtn').disabled = false; }
        $('creativeCanvasStatus').textContent = error.message || '启动失败';
        showToast(error.message || '启动失败', 'bad');
      }
    }
    async function cancelCreativeCanvasTask() {
      const taskId = String(CREATIVE_CANVAS.taskId || '').trim();
      if (!taskId) return;
      stopCreativeCanvasPoll();
      if ($('creativeCanvasCancelBtn')) $('creativeCanvasCancelBtn').disabled = true;
      $('creativeCanvasStatus').textContent = '正在取消任务…';
      try {
        const res = await api('/api/tasks/' + encodeURIComponent(taskId) + '/cancel', {method:'POST', body:JSON.stringify({})});
        const task = res.data || {};
        CREATIVE_CANVAS.taskId = '';
        CREATIVE_CANVAS.running = false;
        $('creativeCanvasRunBtn').disabled = false;
        $('creativeCanvasRerunBtn').disabled = false;
        if ($('creativeCanvasCancelBtn')) $('creativeCanvasCancelBtn').style.display = 'none';
        $('creativeCanvasStatus').textContent = task.error || '任务已取消';
        if (CREATIVE_CANVAS.current?.id) await loadCreativeCanvasList(CREATIVE_CANVAS.current.id);
      } catch (error) {
        if ($('creativeCanvasCancelBtn')) $('creativeCanvasCancelBtn').disabled = false;
        $('creativeCanvasStatus').textContent = error.message || '取消失败';
      }
    }
    function stopCreativeCanvasPoll() { if (CREATIVE_CANVAS.pollTimer) { clearTimeout(CREATIVE_CANVAS.pollTimer); CREATIVE_CANVAS.pollTimer = null; } }
    async function creativeCanvasPollTask(taskId) {
      stopCreativeCanvasPoll();
      const poll = async () => {
        try {
          const res = await api('/api/studio/tasks/' + encodeURIComponent(taskId));
          if (!res.success) throw new Error(res.message || '状态读取失败');
          const task = res.data || {};
          if (task.status === 'queued' || task.status === 'running') { $('creativeCanvasStatus').textContent = '生成中… ' + (taskStageText(task) || taskProgressText(task) || ''); CREATIVE_CANVAS.pollTimer = setTimeout(poll, 1200); return; }
          CREATIVE_CANVAS.running = false; CREATIVE_CANVAS.taskId = ''; clearGalleryPickerCache(); $('creativeCanvasRunBtn').disabled = false; $('creativeCanvasRerunBtn').disabled = false;
          if ($('creativeCanvasCancelBtn')) $('creativeCanvasCancelBtn').style.display = 'none';
          await loadCreativeCanvasList(CREATIVE_CANVAS.current?.id, {force:true}); showToast(task.success ? '图片生成完成' : (task.error || '生成失败'), task.success ? 'ok' : 'bad');
        } catch (error) { $('creativeCanvasStatus').textContent = error.message || '状态读取失败'; CREATIVE_CANVAS.pollTimer = setTimeout(poll, 2600); }
      };
      CREATIVE_CANVAS.pollTimer = setTimeout(poll, 900);
    }
