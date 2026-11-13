    function renderSelfieDailySchedule(payload) {
      const root = $('selfieDailySchedule');
      if (!root) return;
      const schedule = payload && payload.schedule;
      if (!schedule || !Array.isArray(schedule.events) || !schedule.events.length) {
        root.hidden = true;
        root.innerHTML = '';
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

    async function refreshSelfie(notify = false) {
      try {
        const res = await api('/api/selfie-reference');
        const data = res.data || {};
        SELFIE_REFERENCE_DATA_URL = data.has_image && String(data.image || '').startsWith('data:image/')
          ? String(data.image)
          : '';
        if (SELFIE_REFERENCE_DATA_URL) {
          $('selfiePreview').src = SELFIE_REFERENCE_DATA_URL;
          $('selfiePreview').style.display = 'block';
        } else {
          $('selfiePreview').removeAttribute('src');
          $('selfiePreview').style.display = 'none';
        }
        const appearanceLabel = ({auto:'自动', real:'真人', anime:'动漫'})[data.appearance_type || 'auto'] || '自动';
        $('selfieStatus').textContent = `${data.has_image ? '形象参考图已设置' : '尚未设置形象参考图'} · 类型：${appearanceLabel}`;
        renderSelfieDailySchedule(data.daily_schedule);
        if ($('selfieAppearanceType')) {
          $('selfieAppearanceType').value = data.appearance_type || 'auto';
        }
        if (notify) showToast('形象参考已刷新', 'ok');
      } catch (e) {
        $('selfieStatus').textContent = '形象信息暂不可用';
        showToast(e.message || '形象信息读取失败', 'bad');
      }
    }
    async function refreshDailySelfieLocal() {
      try {
        const res = await api('/api/selfie-profile/refresh-local', {method:'POST', body:'{}'});
        renderSelfieDailySchedule(res.data && res.data.daily_schedule);
        showToast('本地今日穿搭已刷新', 'ok');
      } catch (e) {
        showToast(e.message, 'bad');
      }
    }
    async function uploadSelfie() {
      const file = $('selfieFile').files[0];
      if (!file) { showToast('请选择图片', 'bad'); return; }
      try {
        const image = await readFileDataUrl(file);
        await api('/api/selfie-reference', {method:'POST', body: JSON.stringify({image, mime_type:file.type, filename:file.name})});
        await refreshSelfie();
        showToast('参考图已更新', 'ok');
      } catch (e) { showToast(e.message || '参考图上传失败', 'bad'); }
    }
    async function clearSelfie() {
      if (!await confirmAction('确定清除当前自拍形象参考图吗？清除后后续生成将不再使用这张形象图。', {title:'清除形象参考图', confirmText:'清除参考图'})) return;
      try { await api('/api/selfie-reference/clear', {method:'POST', body:'{}'}); await refreshSelfie(); showToast('参考图已清除', 'ok'); }
      catch (e) { showToast(e.message || '参考图清除失败', 'bad'); }
    }
    async function saveAppearanceType() {
      try {
        const appearance_type = ($('selfieAppearanceType') && $('selfieAppearanceType').value) || 'auto';
        const res = await api('/api/selfie-appearance-type', {method:'POST', body: JSON.stringify({appearance_type})});
        const data = res.data || {};
        if ($('selfieAppearanceType')) $('selfieAppearanceType').value = data.appearance_type || appearance_type;
        const appearanceLabel = ({auto:'自动', real:'真人', anime:'动漫'})[data.appearance_type || appearance_type] || '自动';
        $('selfieStatus').textContent = `${data.has_image ? '形象参考图已设置' : '尚未设置形象参考图'} · 类型：${appearanceLabel}`;
        showToast('形象类型已保存', 'ok');
      } catch (e) {
        showToast(e.message, 'bad');
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
      try {
        const res = await api('/api/config');
        CONFIG = res.data || {};
        if (!isDashboardPage()) {
          if (AUTH_TOKEN) safeStorageSet('selfieImageToken', AUTH_TOKEN);
          else safeStorageRemove('selfieImageToken');
          safeStorageRemove('aicatToken');
        }
        fillForms();
        document.body.classList.add('authed');
        if (isDashboardPage()) {
          document.body.classList.add('dashboard-embedded');
          if ($('loginPage')) $('loginPage').style.display = 'none';
          if ($('logoutBtn')) $('logoutBtn').style.display = 'none';
        }
        if ($('loginStatus')) $('loginStatus').textContent = '';
        await checkHealth();
        await refreshSelfie();
        await loadRecords(false);
        await loadAssets(false);
        await loadAssetTags();
        await loadMetrics();
        await loadStudioList(STUDIO.current && STUDIO.current.id);
        await resumeImageTestTask();
      } catch (e) {
        document.body.classList.remove('authed');
        const message = e.message || '登录失败';
        if (isDashboardPage()) {
          // Keep a visible error instead of falling back to token login.
          if ($('loginPage')) {
            $('loginPage').style.display = '';
            const box = $('loginPage').querySelector('.login-box');
            if (box) {
              box.innerHTML = `<h1>管理页加载失败</h1><p class="muted">请刷新后重试。</p><div class="status">${escapeHtml(message)}</div>`;
            }
          }
          if ($('loginStatus')) $('loginStatus').textContent = message;
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
        el.addEventListener(eventName, () => scheduleFormAutoSave());
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
