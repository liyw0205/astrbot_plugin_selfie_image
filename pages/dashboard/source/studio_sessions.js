    function renderStudioPromptChips() {
      const wrap = $('studioPromptChips');
      if (!wrap) return;
      wrap.innerHTML = '';
      const tid = studioActiveTemplateId();
      const items = (STUDIO.prompts || []).filter(item => {
        const tags = item.templates;
        if (!tags || !tags.length) return true; // empty tags = all templates
        return tags.includes(tid);
      }).sort(promptPresetCompare);
      let lastGroup = '';
      for (const item of items) {
        const group = promptPresetGroup(item);
        if (group && group !== lastGroup) {
          const heading = document.createElement('div');
          heading.className = 'preset-group-heading';
          heading.textContent = promptPresetGroupLabel(item);
          wrap.appendChild(heading);
        }
        lastGroup = group;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'secondary';
        btn.textContent = item.title || item.id;
        btn.onclick = () => { $('studioPrompt').value = item.prompt || ''; };
        wrap.appendChild(btn);
      }
    }
    function renderStudioSessionSelect() {
      const el = $('studioSessionSelect');
      if (!el) return;
      const cur = STUDIO.current && STUDIO.current.id;
      el.innerHTML = '';
      const blank = document.createElement('option');
      blank.value = '';
      blank.textContent = '（未选择）';
      el.appendChild(blank);
      const tplName = (id) => {
        const t = (STUDIO.templates || []).find(x => x.id === id);
        return (t && t.title) || id || '';
      };
      for (const s of STUDIO.sessions || []) {
        const opt = document.createElement('option');
        opt.value = s.id;
        const badge = [s.template ? tplName(s.template) : '', s.result_count ? (s.result_count + '结果') : '', s.last_status || '']
          .filter(Boolean).join(' · ');
        opt.textContent = `${s.title || s.id}${badge ? ' · ' + badge : ''}`;
        if (s.id === cur) opt.selected = true;
        el.appendChild(opt);
      }
    }
    function upsertStudioSessionSummary(session) {
      if (!session || !session.id) return;
      const summary = {
        id: session.id,
        title: session.title || '',
        updated_at: session.updated_at || session.created_at || '',
        template: session.template || '',
        mode: (session.graph && session.graph.mode) || '',
        slot_count: (session.slots || []).length,
        result_count: (session.results || []).length,
        last_status: (session.last_run && session.last_run.status) || '',
        last_run: session.last_run || null,
      };
      STUDIO.sessions = [summary, ...(STUDIO.sessions || []).filter(item => item.id !== summary.id)];
    }
    function syncStudioEditorVisibility() {
      const hasSession = !!(STUDIO.current && STUDIO.current.id);
      const editor = $('studioEditor');
      const empty = $('studioEmptyState');
      if (editor) editor.hidden = !hasSession;
      if (empty) empty.hidden = hasSession;
      if ($('studioDeleteBtn')) $('studioDeleteBtn').disabled = !hasSession;
    }
    function openStudioCreateModal() {
      renderStudioTemplateSelect();
      $('studioCreateTitle').value = '';
      $('studioCreateStatus').textContent = '';
      $('studioCreateConfirmBtn').disabled = false;
      $('studioCreateModal').classList.add('show');
      setTimeout(() => $('studioCreateTitle')?.focus(), 0);
    }
    function closeStudioCreateModal() {
      $('studioCreateModal').classList.remove('show');
      $('studioCreateConfirmBtn').disabled = false;
    }
    function renderStudioSlots() {
      const wrap = $('studioSlots');
      if (!wrap) return;
      wrap.innerHTML = '';
      const session = STUDIO.current;
      if (!session) { wrap.innerHTML = '<div class="status">还没有无限画布</div>'; return; }
      const slots = session.slots || [];
      slots.forEach((slot, index) => {
        const card = document.createElement('div');
        const has = !!(slot.image_path);
        card.className = 'studio-slot ' + (has ? 'has-image' : 'empty');
        card.draggable = true;
        card.title = '拖动调整参考图顺序';
        card.dataset.slotId = slot.id;
        // Must use data-cache-path + loadProtectedImage (auth/token); bare /api/cache-image src is broken.
        card.innerHTML = `
          <div class="slot-label">${escapeHtml(slot.label || slot.role || '参考图')}</div>
          ${has ? `<img data-cache-path="${escapeHtml(slot.image_path)}" alt="">` : '<div class="status" style="font-size:12px">空</div>'}
          <div class="slot-actions">
            <button type="button" class="secondary mini" data-act="upload">上传</button>
            <button type="button" class="secondary mini" data-act="record">图库</button>
            <button type="button" class="secondary mini" data-act="up" ${index === 0 ? 'disabled' : ''}>上移</button>
            <button type="button" class="secondary mini" data-act="down" ${index >= slots.length - 1 ? 'disabled' : ''}>下移</button>
            <button type="button" class="secondary mini" data-act="clear" ${has ? '' : 'disabled'}>清空</button>
          </div>`;
        card.querySelector('[data-act="upload"]').onclick = () => {
          STUDIO.uploadSlotId = slot.id;
          STUDIO.galleryTargetSlotId = '';
          $('studioFilePick').click();
        };
        card.querySelector('[data-act="record"]').onclick = () => openStudioGallery(slot.id);
        card.querySelector('[data-act="clear"]').onclick = async () => {
          if (!await confirmAction(`确定清空参考图「${slot.label || slot.role || '当前参考图'}」吗？图片引用会移除，原图片不会删除。`, {
            title: '清空参考图',
            confirmText: '清空参考图'
          })) return;
          const res = await studioApi(`/api/studio/sessions/${encodeURIComponent(session.id)}/slots/${encodeURIComponent(slot.id)}`, {
            method: 'POST', body: JSON.stringify({ clear: true })
          });
          if (!res.success) { showToast(res.message || '清空失败', 'bad'); return; }
          STUDIO.current = res.data; renderStudio();
        };
        card.querySelector('[data-act="up"]').onclick = () => studioMoveSlot(index, -1);
        card.querySelector('[data-act="down"]').onclick = () => studioMoveSlot(index, 1);
        card.addEventListener('dragstart', event => {
          event.dataTransfer.effectAllowed = 'move';
          event.dataTransfer.setData('text/plain', String(slot.id || ''));
          card.classList.add('dragging');
        });
        card.addEventListener('dragend', () => {
          card.classList.remove('dragging');
          wrap.querySelectorAll('.drag-over').forEach(item => item.classList.remove('drag-over'));
        });
        card.addEventListener('dragover', event => {
          event.preventDefault();
          event.dataTransfer.dropEffect = 'move';
          card.classList.add('drag-over');
        });
        card.addEventListener('dragleave', event => {
          if (!card.contains(event.relatedTarget)) card.classList.remove('drag-over');
        });
        card.addEventListener('drop', event => {
          event.preventDefault();
          card.classList.remove('drag-over');
          const fromId = event.dataTransfer.getData('text/plain');
          studioDropSlot(fromId, index);
        });
        wrap.appendChild(card);
      });
      wrap.dataset.protectedMediaQueued = 'true';
      loadProtectedImages(wrap);
    }
    async function studioMoveSlot(index, delta) {
      if (!STUDIO.current) return;
      const slots = [...(STUDIO.current.slots || [])];
      const j = index + delta;
      if (j < 0 || j >= slots.length) return;
      const tmp = slots[index];
      slots[index] = slots[j];
      slots[j] = tmp;
      const order = slots.map(s => s.id);
      const res = await studioApi(`/api/studio/sessions/${encodeURIComponent(STUDIO.current.id)}/reorder`, {
        method: 'POST', body: JSON.stringify({ order })
      });
      if (!res.success) return showToast(res.message || '排序失败', 'bad');
      STUDIO.current = res.data;
      renderStudio();
    }
    async function studioDropSlot(fromId, targetIndex) {
      if (!STUDIO.current) return;
      const slots = [...(STUDIO.current.slots || [])];
      const fromIndex = slots.findIndex(slot => String(slot.id || '') === String(fromId || ''));
      const target = Number(targetIndex);
      if (fromIndex < 0 || !Number.isInteger(target) || target < 0 || target >= slots.length || fromIndex === target) return;
      const [moved] = slots.splice(fromIndex, 1);
      slots.splice(target, 0, moved);
      const res = await studioApi(`/api/studio/sessions/${encodeURIComponent(STUDIO.current.id)}/reorder`, {
        method: 'POST', body: JSON.stringify({ order: slots.map(slot => slot.id) })
      });
      if (!res.success) return showToast(res.message || '排序失败', 'bad');
      STUDIO.current = res.data;
      renderStudio();
    }
    async function studioPromoteRole(resultId, role, okText) {
      if (!STUDIO.current) return;
      const res = await studioApi(`/api/studio/sessions/${encodeURIComponent(STUDIO.current.id)}/promote`, {
        method: 'POST',
        body: JSON.stringify({ result_id: resultId, role, create_if_missing: true })
      });
      if (!res.success) return showToast(res.message || '失败', 'bad');
      STUDIO.current = res.data;
      renderStudio();
      showToast(okText || '已放入参考图', 'ok');
    }
    function renderStudioResults() {
      const wrap = $('studioResults');
      if (!wrap) return;
      wrap.innerHTML = '';
      const session = STUDIO.current;
      if (!session) return;
      for (const item of session.results || []) {
        const card = document.createElement('div');
        card.className = 'card';
        const path = item.image_path || '';
        const sourceAssetIds = Array.isArray(item.source_asset_ids) ? item.source_asset_ids.filter(Boolean) : [];
        card.innerHTML = `
          ${path ? `<img data-cache-path="${escapeHtml(path)}" alt="">` : '<div class="status">无图</div>'}
          <div class="actions" style="margin-top:6px">
            <button type="button" class="secondary mini" data-act="base">作底图</button>
            <button type="button" class="secondary mini" data-act="outfit">作服装</button>
            <button type="button" class="secondary mini" data-act="peer">作同框</button>
            <button type="button" class="secondary mini" data-act="id">作形象</button>
            <button type="button" class="secondary mini" data-act="again">再生成</button>
            ${sourceAssetIds.length ? '<button type="button" class="secondary mini" data-act="source">来源资产</button>' : ''}
          </div>
          <div class="status" style="font-size:11px">${escapeHtml(item.created_at || '')}${item.used_model ? ' · ' + escapeHtml(item.used_model) : ''}${sourceAssetIds.length ? ' · 来源资产 ' + escapeHtml(sourceAssetIds.join(', ')) : ''}</div>`;
        card.querySelector('[data-act="base"]').onclick = () => studioPromoteRole(item.id, 'base', '已作底图');
        card.querySelector('[data-act="outfit"]').onclick = () => studioPromoteRole(item.id, 'outfit', '已作服装参考');
        card.querySelector('[data-act="peer"]').onclick = () => studioPromoteRole(item.id, 'peer', '已作同框');
        card.querySelector('[data-act="id"]').onclick = () => studioPromoteRole(item.id, 'identity', '已作形象');
        card.querySelector('[data-act="again"]').onclick = () => studioRun();
        const sourceButton = card.querySelector('[data-act="source"]');
        if (sourceButton) sourceButton.onclick = () => openRecordDetail(sourceAssetIds[0]);
        wrap.appendChild(card);
      }
      const last = session.last_run;
      if (last && last.status === 'failed' && last.error) {
        const err = document.createElement('div');
        err.className = 'status';
        err.textContent = '上次失败：' + last.error;
        wrap.prepend(err);
      }
      wrap.dataset.protectedMediaQueued = 'true';
      loadProtectedImages(wrap);
    }
    function setStudioRunningUI(running) {
      STUDIO.running = !!running;
      const btn = $('studioRunBtn');
      const again = $('studioRerunBtn');
      if (btn) {
        btn.disabled = !!running;
        btn.textContent = running ? '生成中…' : '开始生成';
      }
      if (again) again.disabled = !!running;
      if ($('studioPickRecordBtn')) $('studioPickRecordBtn').disabled = !!running;
      if ($('studioCancelBtn')) $('studioCancelBtn').style.display = running && STUDIO.taskId ? '' : 'none';
      if ($('studioRetryStatusBtn') && (running || !STUDIO.taskId)) $('studioRetryStatusBtn').style.display = 'none';
    }
    async function cancelStudioTask() {
      const taskId = String(STUDIO.taskId || '').trim();
      if (!taskId) return;
      studioStopPoll();
      $('studioCancelBtn').disabled = true;
      $('studioStatus').textContent = '正在取消任务…';
      try {
        const res = await studioApi('/api/tasks/' + encodeURIComponent(taskId) + '/cancel', {method:'POST', body: JSON.stringify({})});
        const task = res.data || {};
        STUDIO.taskId = '';
        setStudioRunningUI(false);
        $('studioStatus').textContent = task.error || '任务已取消';
        if (STUDIO.current?.id) await loadStudioList(STUDIO.current.id);
      } catch (e) {
        $('studioCancelBtn').disabled = false;
        $('studioStatus').textContent = e.message || '取消失败';
      }
    }
    function closeStudioGallery() {
      const panel = $('studioGallery');
      if (panel) { panel.classList.remove('open'); panel.innerHTML = ''; }
      document.querySelectorAll('#studioSlots .studio-slot-picker').forEach(picker => {
        const card = picker.closest('.studio-slot');
        if (card) { card.classList.remove('picking-record'); card.draggable = true; }
        picker.remove();
      });
      STUDIO.galleryTargetSlotId = '';
    }
    async function openStudioGallery(slotId) {
      if (!STUDIO.current) return showToast('请先选择画布', 'bad');
      closeStudioGallery();
      STUDIO.galleryTargetSlotId = slotId || '';
      let panel = $('studioGallery');
      if (slotId) {
        const card = [...document.querySelectorAll('#studioSlots .studio-slot')]
          .find(item => item.dataset.slotId === String(slotId));
        if (!card) return showToast('参考图已刷新，请重试', 'bad');
        card.classList.add('picking-record');
        card.draggable = false;
        panel = document.createElement('div');
        panel.className = 'studio-slot-picker';
        card.appendChild(panel);
      } else if (panel) {
        panel.classList.add('open');
      }
      if (!panel) return;
      panel.innerHTML = '<div class="status">加载记录图…</div>';
      const res = await loadGalleryPickerItems();
      if (!res.success) {
        panel.innerHTML = `<div class="between"><div class="status">${escapeHtml(res.message || '加载失败')}</div>
          <button type="button" class="secondary mini" data-studio-gallery-close>收起</button></div>`;
        panel.querySelector('[data-studio-gallery-close]').onclick = closeStudioGallery;
        return;
      }
      const items = (res.data && res.data.items) || [];
      if (!items.length) {
        panel.innerHTML = '<div class="between"><div class="status">暂无成功记录图</div><button type="button" class="secondary mini" data-studio-gallery-close>收起</button></div>';
        panel.querySelector('[data-studio-gallery-close]').onclick = closeStudioGallery;
        return;
      }
      const targetHint = STUDIO.galleryTargetSlotId
        ? '选择一张图片放入此参考图位置'
        : '点图后将放入默认参考图位置';
      panel.innerHTML = `<div class="between"><div class="status">${targetHint}</div>
        <button type="button" class="secondary mini" data-studio-gallery-close>收起</button></div>
        <div class="studio-gallery-grid"></div>`;
      panel.querySelector('[data-studio-gallery-close]').onclick = closeStudioGallery;
      const grid = panel.querySelector('.studio-gallery-grid');
      panel.dataset.galleryOffset = '0';
      renderGalleryPage(panel, items, item => studioPickGalleryPath(item.path, item.record_id));
    }
    async function studioPickGalleryPath(imagePath, recordId = '') {
      if (!STUDIO.current || !imagePath) return;
      let slotId = STUDIO.galleryTargetSlotId;
      if (!slotId) {
        const empty = (STUDIO.current.slots || []).find(s => !s.image_path);
        const identity = (STUDIO.current.slots || []).find(s => s.role === 'identity' || s.role === 'base');
        slotId = (empty && empty.id) || (identity && identity.id) || ((STUDIO.current.slots || [])[0] || {}).id;
      }
      if (!slotId) return showToast('没有可用参考图位置', 'bad');
      const res = await studioApi(`/api/studio/sessions/${encodeURIComponent(STUDIO.current.id)}/slots/${encodeURIComponent(slotId)}`, {
        method: 'POST', body: JSON.stringify({ image_path: imagePath, source: 'record', source_record_id: recordId || '' })
      });
      if (!res.success) return showToast(res.message || '放入失败', 'bad');
      STUDIO.current = res.data;
      closeStudioGallery();
      renderStudio();
      showToast('已从图库放入参考图', 'ok');
    }
    function renderStudio() {
      syncStudioEditorVisibility();
      renderStudioSessionSelect();
      renderStudioTemplateSelect();
      fillStudioAspect();
      renderStudioPromptChips();
      const s = STUDIO.current;
      if (!s) {
        $('studioTitle').value = '';
        $('studioPrompt').value = '';
        $('studioStatus').textContent = '选择模板开始创作，或打开已有画布。';
        renderStudioSlots();
        renderStudioResults();
        return;
      }
      const g = s.graph || {};
      $('studioTitle').value = s.title || '';
      $('studioPrompt').value = g.prompt || '';
      $('studioMode').value = g.mode || 'group';
      $('studioResolution').value = g.resolution || '1K';
      $('studioCount').value = g.count || 1;
      $('studioUsePersona').checked = g.use_persona_identity !== false;
      setSelectOptions('studioAspect', ['自动','1:1','2:3','3:2','3:4','4:3','4:5','5:4','9:16','16:9','21:9'], g.aspect_ratio || '自动');
      const lr = s.last_run;
      const running = !!(lr && lr.status === 'running') || !!STUDIO.running;
      if (lr && lr.status === 'running') STUDIO.taskId = lr.task_id || STUDIO.taskId;
      setStudioRunningUI(running && lr && lr.status === 'running');
      if (lr && lr.status === 'running') $('studioStatus').textContent = '生成中… ' + (lr.task_id || '');
      else if (lr && lr.status === 'succeeded') $('studioStatus').textContent = '完成 · ' + (lr.used_model || '') + ' · ' + ((lr.result_paths||[]).length) + ' 张';
      else if (lr && lr.status === 'cancelled') $('studioStatus').textContent = '已取消';
      else if (lr && lr.status === 'partial_success') $('studioStatus').textContent = '部分完成 · ' + ((lr.result_paths||[]).length) + ' 张';
      else if (lr && lr.status === 'failed') $('studioStatus').textContent = '失败：' + (lr.error || '');
      else $('studioStatus').textContent = '已加载：' + (s.title || '当前画布');
      renderStudioSlots();
      renderStudioResults();
    }
    async function loadStudioList(selectId) {
      const res = await studioApi('/api/studio/sessions');
      if (!res.success) { showToast(res.message || '画布列表失败', 'bad'); return; }
      const storageStatus = res.data && res.data.storage_status;
      if (storageStatus && storageStatus.ok === false) {
        showToast(storageStatus.error || '画布存储读取失败，请修复文件后重试', 'bad');
        return;
      }
      STUDIO.sessions = (res.data && res.data.sessions) || [];
      STUDIO.prompts = (res.data && res.data.builtin_prompts) || [];
      STUDIO.templates = (res.data && res.data.templates) || STUDIO.templates || [];
      const presetStatus = res.data && res.data.prompt_preset_status;
      if (presetStatus && presetStatus.ok === false) {
        showToast(presetStatus.error || '预设加载失败', 'bad');
      }
      if (res.data && res.data.prompt_presets) {
        STUDIO.promptPresetsByKind.image = mergePromptPresets(res.data.prompt_presets);
        STUDIO.promptPresets = STUDIO.promptPresetsByKind.image;
      }
      if (res.data && Array.isArray(res.data.cos_look_sets)) STUDIO.cosLooks = res.data.cos_look_sets;
      if (selectId) {
        const detail = await studioApi('/api/studio/sessions/' + encodeURIComponent(selectId));
        if (detail.success) STUDIO.current = detail.data;
      } else if (STUDIO.current && STUDIO.current.id) {
        const detail = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id));
        if (detail.success) STUDIO.current = detail.data;
      }
      renderStudio();
    }
    async function studioCreate() {
      const template = ($('studioTemplateSelect') && $('studioTemplateSelect').value) || 'duo';
      const title = ($('studioCreateTitle').value || '').trim();
      const confirm = $('studioCreateConfirmBtn');
      const status = $('studioCreateStatus');
      if (confirm) confirm.disabled = true;
      if (status) status.textContent = '创建中…';
      try {
        const res = await studioApi('/api/studio/sessions', {
          method:'POST',
          body: JSON.stringify({ title, template })
        });
        if (!res.success) throw new Error(res.message || '创建失败');
        STUDIO.current = res.data;
        upsertStudioSessionSummary(res.data);
        closeStudioCreateModal();
        renderStudio();
        showToast('已新建：' + (res.data.template || template), 'ok');
      } catch (e) {
        if (status) status.textContent = e.message || '创建失败';
        if (confirm) confirm.disabled = false;
      }
    }
    async function studioSave() {
      if (!STUDIO.current) return showToast('请先新建或选择画布', 'bad');
      const body = {
        title: $('studioTitle').value.trim(),
        graph: {
          prompt: $('studioPrompt').value,
          mode: $('studioMode').value,
          aspect_ratio: $('studioAspect').value,
          resolution: $('studioResolution').value,
          count: Number($('studioCount').value || 1),
          use_persona_identity: $('studioUsePersona').checked,
        }
      };
      const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id), {
        method:'POST', body: JSON.stringify(body)
      });
      if (!res.success) throw new Error(res.message || '保存失败');
      STUDIO.current = res.data;
      await loadStudioList(STUDIO.current.id);
      showToast('画布已保存', 'ok');
    }
    async function studioDelete() {
      if (!STUDIO.current) return;
      const title = String(STUDIO.current.title || STUDIO.current.id || '当前画布');
      if (!await confirmAction(`确定删除画布「${title}」吗？画布中的参考图和生成记录也会移除。`, {title:'删除无限画布', confirmText:'删除画布'})) return;
      const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/delete', {
        method:'POST', body: JSON.stringify({})
      });
      if (!res.success) return showToast(res.message || '删除失败', 'bad');
      STUDIO.current = null;
      await loadStudioList();
      showToast('已删除', 'ok');
    }
    async function studioRun() {
      if (!STUDIO.current) return showToast('请先新建或选择画布', 'bad');
      if (STUDIO.running) return showToast('正在生成，请稍候', 'bad');
      try {
        await studioSave();
      } catch (e) {
        setStudioRunningUI(false);
        $('studioStatus').textContent = e.message || '画布保存失败，未启动生成';
        showToast(e.message || '画布保存失败，未启动生成', 'bad');
        return;
      }
      if (!STUDIO.current) return;
      setStudioRunningUI(true);
      $('studioStatus').textContent = '已提交生成…';
      let res;
      try {
        res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/run', {
          method:'POST', body: JSON.stringify({})
        });
      } catch (e) {
        setStudioRunningUI(false);
        $('studioStatus').textContent = e.message || '启动失败，未提交生成';
        showToast(e.message || '启动失败，未提交生成', 'bad');
        return;
      }
      if (!res.success) {
        setStudioRunningUI(false);
        $('studioStatus').textContent = res.message || '启动失败';
        return showToast(res.message || '启动失败', 'bad');
      }
      const taskId = res.data && res.data.task_id;
      STUDIO.taskId = taskId || '';
      STUDIO.pollFailures = 0;
      setStudioRunningUI(true);
      showToast('开始生成', 'ok');
      studioStopPoll();
      const pollStudio = async () => {
        STUDIO.pollTimer = null;
        try {
          const st = await studioApi('/api/studio/tasks/' + encodeURIComponent(taskId));
          if (!st.success) throw new Error(st.message || '状态读取失败');
          const task = st.data || {};
          if (task.status === 'queued' || task.status === 'running') {
            const progress = taskProgressText(task);
            const stage = taskStageText(task);
            $('studioStatus').textContent = '生成中… ' + (stage ? stage + ' · ' : '') + (progress ? progress + ' · ' : '') + (task.running_seconds != null ? task.running_seconds + 's' : '');
            setStudioRunningUI(true);
            STUDIO.pollTimer = setTimeout(pollStudio, 1500);
            return;
          }
          studioStopPoll();
          STUDIO.taskId = '';
          setStudioRunningUI(false);
          clearGalleryPickerCache();
          await loadStudioList(STUDIO.current.id, {force:true});
          if (task.success) showToast('画布生成完成', 'ok');
          else showToast(task.error || '生成失败', 'bad');
        } catch (e) {
          STUDIO.pollFailures = Number(STUDIO.pollFailures || 0) + 1;
          if (STUDIO.pollFailures < 4) {
            $('studioStatus').textContent = `状态读取失败（${STUDIO.pollFailures}/3），正在退避重试…`;
            STUDIO.pollTimer = setTimeout(pollStudio, Math.min(12000, 1500 * (2 ** STUDIO.pollFailures)));
            return;
          }
          studioStopPoll();
          STUDIO.taskId = taskId || STUDIO.taskId;
          setStudioRunningUI(false);
          if ($('studioCancelBtn')) $('studioCancelBtn').style.display = STUDIO.taskId ? '' : 'none';
          if ($('studioRetryStatusBtn')) $('studioRetryStatusBtn').style.display = STUDIO.taskId ? '' : 'none';
          $('studioStatus').textContent = `${e.message || '状态读取失败'}；可重新点击生成或取消任务`;
          showToast('画布状态读取失败，请重试', 'bad');
        }
      };
      STUDIO.pollTimer = setTimeout(pollStudio, 1500);
    }
    async function studioUploadFile(file) {
      if (!STUDIO.current || !STUDIO.uploadSlotId || !file) return;
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      const res = await studioApi(`/api/studio/sessions/${encodeURIComponent(STUDIO.current.id)}/slots/${encodeURIComponent(STUDIO.uploadSlotId)}`, {
        method:'POST', body: JSON.stringify({ image: dataUrl, source: 'upload' })
      });
      STUDIO.uploadSlotId = '';
      if (!res.success) return showToast(res.message || '上传失败', 'bad');
      STUDIO.current = res.data;
      renderStudio();
      showToast('参考图已更新', 'ok');
    }

    // Relationship graph view. The existing Studio task and record APIs stay
    // in place; this layer only projects them into an infinite canvas.
