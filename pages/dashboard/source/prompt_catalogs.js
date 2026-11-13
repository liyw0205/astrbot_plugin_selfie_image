    const SPECIAL_PRESET_GROUP_LABELS = {action:'动作预设', upper:'上身预设', lower:'下身预设'};
    const SPECIAL_PRESET_GROUP_ORDER = {action:0, upper:1, lower:2};
    function promptPresetGroup(item) {
      return String(item?.preset_group || item?.special_group || '').trim();
    }
    function promptPresetGroupLabel(item) {
      return SPECIAL_PRESET_GROUP_LABELS[promptPresetGroup(item)] || '';
    }
    function promptPresetSortKey(item) {
      const name = String(item?.name || item?.title || '').trim();
      const group = promptPresetGroup(item);
      return [group ? 1 : 0, SPECIAL_PRESET_GROUP_ORDER[group] ?? 99, name];
    }
    function promptPresetCompare(a, b) {
      const ka = promptPresetSortKey(a);
      const kb = promptPresetSortKey(b);
      return ka[0] - kb[0] || ka[1] - kb[1] || ka[2].localeCompare(kb[2], 'zh');
    }
    function mergePromptPresets(rows) {
      const map = new Map();
      for (const item of rows || []) {
        const name = String(item.name || item.title || '').trim();
        const prompt = String(item.prompt || '').trim();
        if (!name || !prompt) continue;
        map.set(name, Object.assign({}, item, { name, title: name, prompt }));
      }
      return [...map.values()].sort(promptPresetCompare);
    }
    function pickerCacheFresh(entry) {
      return entry.loadedAt > 0 && (Date.now() - entry.loadedAt) < PICKER_CATALOG_CACHE_TTL_MS;
    }
    async function loadAssetTagsCached(force = false) {
      const entry = PICKER_CACHE.tags;
      if (!force && pickerCacheFresh(entry)) return entry.data;
      if (entry.request) return entry.request;
      const version = entry.version;
      let request;
      request = (async () => {
        const res = await api('/api/assets/tags');
        const rows = Array.isArray(res.data) ? res.data : [];
        if (entry.version === version) {
          entry.data = rows;
          entry.loadedAt = Date.now();
        }
        return rows;
      })().finally(() => {
        if (entry.request === request) entry.request = null;
      });
      entry.request = request;
      return request;
    }
    function clearPickerCache(kind = '') {
      PICKER_CATALOG_CACHE_VERSION += 1;
      const normalized = String(kind || '').trim().toLowerCase();
      const clearEntry = entry => {
        entry.data = [];
        entry.loadedAt = 0;
        entry.request = null;
        entry.version += 1;
      };
      if (!normalized || normalized === 'tags') clearEntry(PICKER_CACHE.tags);
      if (!normalized || normalized === 'image') {
        ASSET_STUDIO_PRESETS = [];
      }
      for (const key of [...PICKER_PAGE_CACHE.keys()]) {
        if (!normalized || key.startsWith(`${normalized}:`)) PICKER_PAGE_CACHE.delete(key);
      }
      for (const key of [...MANAGED_PAGE_CACHE.keys()]) {
        if (!normalized || key.startsWith(`${normalized}:`)) MANAGED_PAGE_CACHE.delete(key);
      }
      for (const state of PICKER_PANEL_STATES.values()) {
        if (!normalized || state.kind === normalized) {
          state.items = [];
          state.total = 0;
          state.loaded = false;
          state.error = '';
        }
      }
    }
    function syncPresetToggleButton(btnId, open) {
      const btn = $(btnId);
      if (!btn) return;
      btn.textContent = open ? '收回' : '预设';
    }
    function syncCosToggleButton(btnId, open) {
      const btn = $(btnId);
      if (!btn) return;
      btn.textContent = open ? '收回' : 'COS池';
    }
    function catalogPickerState(panelId) {
      if (!PICKER_PANEL_STATES.has(panelId)) {
        PICKER_PANEL_STATES.set(panelId, {
          kind: '', items: [], total: 0, page: 1, query: '', loaded: false, autoLoadScheduled: false, error: '', seq: 0,
        });
      }
      return PICKER_PANEL_STATES.get(panelId);
    }
    async function fetchCatalogPage(kind, query, page, force = false) {
      const offset = Math.max(0, (page - 1) * PICKER_PAGE_SIZE);
      const normalizedQuery = String(query || '').trim();
      const key = `${kind}:${normalizedQuery.toLocaleLowerCase()}:${page}`;
      let cached = PICKER_PAGE_CACHE.get(key);
      if (force) { PICKER_PAGE_CACHE.delete(key); cached = null; }
      if (cached?.data && Date.now() - cached.loadedAt < PICKER_CATALOG_CACHE_TTL_MS) return cached.data;
      if (cached?.request) return cached.request;
      const cacheVersion = PICKER_CATALOG_CACHE_VERSION;
      const params = new URLSearchParams({limit: String(PICKER_PAGE_SIZE), offset: String(offset), search: normalizedQuery});
      if (kind === 'image') params.set('kind', 'image');
      const endpoint = kind === 'cos' ? '/api/cos-pools?' : '/api/prompt-presets?';
      const request = (async () => {
        const res = await api(endpoint + params.toString());
        const payload = res.data || {};
        if (payload.load_status && payload.load_status.ok === false) throw new Error(payload.load_status.error || '预设加载失败');
        let items = Array.isArray(payload.items) ? payload.items : [];
        let total = Number(payload.total ?? res.count ?? items.length);
        if (Array.isArray(payload)) {
          const filtered = payload.filter(item => !normalizedQuery || `${item.name || item.title || ''} ${item.prompt || ''}`.toLowerCase().includes(normalizedQuery.toLowerCase()));
          total = filtered.length;
          items = filtered.slice(offset, offset + PICKER_PAGE_SIZE);
        } else if (kind === 'image' && !Array.isArray(payload.items)) {
          const rows = mergePromptPresets(Array.isArray(payload.presets) ? payload.presets : []);
          const filtered = rows.filter(item => !normalizedQuery || `${item.name || item.title || ''} ${item.prompt || ''} ${item.description || ''}`.toLowerCase().includes(normalizedQuery.toLowerCase()));
          total = filtered.length;
          items = filtered.slice(offset, offset + PICKER_PAGE_SIZE);
        } else if (kind === 'cos' && !Array.isArray(payload.items)) {
          const rows = flattenCosPickerPayload(payload);
          const filtered = rows.filter(item => !normalizedQuery || `${item.title || ''} ${item.id || ''} ${item.prompt || ''} ${(item.tags || []).join(' ')}`.toLowerCase().includes(normalizedQuery.toLowerCase()));
          total = filtered.length;
          items = filtered.slice(offset, offset + PICKER_PAGE_SIZE);
        }
        if (kind === 'image') items = mergePromptPresets(items);
        const data = {items, total, offset, limit: PICKER_PAGE_SIZE};
        if (cacheVersion === PICKER_CATALOG_CACHE_VERSION) {
          PICKER_PAGE_CACHE.set(key, {data, loadedAt: Date.now(), request: null});
          while (PICKER_PAGE_CACHE.size > 80) PICKER_PAGE_CACHE.delete(PICKER_PAGE_CACHE.keys().next().value);
        }
        return data;
      })().catch(error => {
        if (cacheVersion === PICKER_CATALOG_CACHE_VERSION) PICKER_PAGE_CACHE.delete(key);
        throw error;
      });
      PICKER_PAGE_CACHE.set(key, {data: null, loadedAt: 0, request});
      return request;
    }
    function prefetchCatalogPages(kind, query, page, total) {
      const totalPages = Math.max(1, Math.ceil(Number(total || 0) / PICKER_PAGE_SIZE));
      const targets = Array.from({length: PAGE_PREFETCH_AHEAD}, (_, index) => page + index + 1)
        .filter(target => target <= totalPages);
      targets.forEach(target => {
        void fetchCatalogPage(kind, query, target).catch(() => {});
      });
    }
    async function fetchManagedCatalogPage(kind, searchQuery, page, force = false) {
      const normalizedQuery = String(searchQuery || '').trim();
      const normalizedKind = ['cos', 'nai'].includes(kind) ? kind : 'image';
      const query = normalizedQuery;
      const key = `${normalizedKind}:${normalizedQuery.toLocaleLowerCase()}:${page}`;
      let cached = MANAGED_PAGE_CACHE.get(key);
      if (force) { MANAGED_PAGE_CACHE.delete(key); cached = null; }
      if (cached?.data && Date.now() - cached.loadedAt < PICKER_CATALOG_CACHE_TTL_MS) return cached.data;
      if (cached?.request) return cached.request;
      const cacheVersion = PICKER_CATALOG_CACHE_VERSION;
      const params = new URLSearchParams({
        limit: String(PICKER_PAGE_SIZE),
        offset: String(Math.max(0, (page - 1) * PICKER_PAGE_SIZE)),
        search:query,
      });
      if (normalizedKind !== 'cos') params.set('kind', normalizedKind);
      const endpoint = normalizedKind === 'cos' ? '/api/cos-pools?' : '/api/prompt-presets/manage?';
      const request = (async () => {
        const res = await api(endpoint + params.toString());
        const raw = res.data;
        const rawIsArray = Array.isArray(raw);
        const data = rawIsArray ? {items: raw, total: raw.length} : (raw || {});
        const loadStatus = data.load_status || res.load_status;
        if (loadStatus && loadStatus.ok === false) throw new Error(loadStatus.error || '预设加载失败');
        let items = Array.isArray(data.items) ? data.items : [];
        let total = Number(data.total ?? items.length);
        if (rawIsArray) {
          const rows = normalizedKind === 'cos' ? flattenCosPickerPayload(data) : normalizedKind === 'nai' ? items : mergePromptPresets(items);
          const filtered = rows.filter(item => !normalizedQuery || `${item.name || item.title || item.id || ''}\n${item.prompt || ''}`.toLowerCase().includes(normalizedQuery.toLowerCase()));
          total = filtered.length;
          items = filtered.slice((page - 1) * PICKER_PAGE_SIZE, page * PICKER_PAGE_SIZE);
        } else if (!Array.isArray(data.items)) {
          if (normalizedKind !== 'cos') {
            const sourceRows = Array.isArray(data.presets) ? data.presets : (Array.isArray(raw) ? raw : []);
            const rows = normalizedKind === 'nai' ? sourceRows : mergePromptPresets(sourceRows);
            const filtered = rows.filter(item => !normalizedQuery || `${item.name || item.title || ''}\n${item.prompt || ''}`.toLowerCase().includes(normalizedQuery.toLowerCase()));
            total = filtered.length;
            items = filtered.slice((page - 1) * PICKER_PAGE_SIZE, page * PICKER_PAGE_SIZE);
          } else {
            const rows = flattenCosPickerPayload(data);
            const filtered = rows.filter(item => !normalizedQuery || `${item.title || item.id || ''}\n${item.prompt || ''}`.toLowerCase().includes(normalizedQuery.toLowerCase()));
            total = filtered.length;
            items = filtered.slice((page - 1) * PICKER_PAGE_SIZE, page * PICKER_PAGE_SIZE);
          }
        }
        if (normalizedKind === 'image') items = mergePromptPresets(items);
        const value = {items, total, offset: (page - 1) * PICKER_PAGE_SIZE, limit: PICKER_PAGE_SIZE};
        if (cacheVersion === PICKER_CATALOG_CACHE_VERSION) {
          MANAGED_PAGE_CACHE.set(key, {data: value, loadedAt: Date.now(), request: null});
          while (MANAGED_PAGE_CACHE.size > 80) MANAGED_PAGE_CACHE.delete(MANAGED_PAGE_CACHE.keys().next().value);
        }
        return value;
      })().catch(error => {
        if (cacheVersion === PICKER_CATALOG_CACHE_VERSION) MANAGED_PAGE_CACHE.delete(key);
        throw error;
      });
      MANAGED_PAGE_CACHE.set(key, {data: null, loadedAt: 0, request});
      return request;
    }
    function prefetchManagedCatalogPages(kind, query, page, total) {
      const totalPages = Math.max(1, Math.ceil(Number(total || 0) / PICKER_PAGE_SIZE));
      Array.from({length: PAGE_PREFETCH_AHEAD}, (_, index) => page + index + 1)
        .filter(target => target <= totalPages)
        .forEach(target => { void fetchManagedCatalogPage(kind, query, target).catch(() => {}); });
    }
    function flattenCosPickerPayload(payload) {
      const pools = payload && typeof payload === 'object' ? payload : {};
      const builtins = Array.isArray(pools.builtin) ? pools.builtin : [];
      const custom = Array.isArray(pools.custom) ? pools.custom : [];
      const builtinIds = new Set(builtins.map(item => String(item?.id || '').trim()).filter(Boolean));
      return [...builtins, ...custom.filter(item => !builtinIds.has(String(item?.id || '').trim()))]
        .filter(item => item && item.id && item.prompt).sort(cosPoolSort);
    }
    function pickerPagerMarkup(state) {
      const pages = Math.max(1, Math.ceil(state.total / PICKER_PAGE_SIZE));
      if (pages <= 1) return '';
      const start = (state.page - 1) * PICKER_PAGE_SIZE + 1;
      const end = Math.min(state.total, start + state.items.length - 1);
      return `<div class="catalog-pager"><span class="muted">${start}-${end} / ${state.total}</span><button class="secondary mini" type="button" data-picker-page="-1" ${state.page <= 1 ? 'disabled' : ''}>上一页</button><button class="secondary mini" type="button" data-picker-page="1" ${state.page >= pages ? 'disabled' : ''}>下一页</button></div>`;
    }
    async function loadCatalogPickerPage(panelId, kind, page = 1, force = false) {
      const state = catalogPickerState(panelId);
      state.kind = kind;
      state.page = Math.max(1, Number(page) || 1);
      state.error = '';
      const seq = ++state.seq;
      PICKER_LOADING.add(panelId);
      if (!state.loaded) renderCatalogPicker(panelId);
      try {
        const data = await fetchCatalogPage(kind, state.query, state.page, force);
        if (seq !== state.seq) return;
        state.items = data.items;
        state.total = data.total;
        state.loaded = true;
        const pageCount = Math.max(1, Math.ceil(state.total / PICKER_PAGE_SIZE));
        if (state.page > pageCount) return loadCatalogPickerPage(panelId, kind, pageCount, force);
        prefetchCatalogPages(kind, state.query, state.page, state.total);
      } catch (error) {
        if (seq === state.seq) state.error = error.message || '列表读取失败';
      } finally {
        if (seq === state.seq) {
          PICKER_LOADING.delete(panelId);
          renderCatalogPicker(panelId);
        }
      }
    }
    function renderCatalogPicker(panelId) {
      const kind = panelId.toLowerCase().includes('cos') ? 'cos' : 'image';
      if (kind === 'cos') {
        const panel = $(panelId);
        if (!panel) return;
        const mapping = {
          studioCosPanel: ['studioPrompt', 'studioCosBtn', !!STUDIO.cosOpen],
          studioNodeCosPanel: ['studioNodePrompt', 'studioNodeCosBtn', !!STUDIO.nodeCosOpen],
          testCosPanel: ['testPrompt', 'testCosBtn', !!window.__TEST_COS_OPEN],
          creativeCanvasCosPanel: ['creativeCanvasPrompt', 'creativeCanvasCosBtn', !!CREATIVE_CANVAS.cosOpen],
        }[panelId] || ['', '', true];
        renderCosPanel(panelId, mapping[0], mapping[2], mapping[1]);
        return;
      }
      const mapping = {
        studioPresetPanel: ['studioPrompt', 'studioPresetBtn', !!STUDIO.presetOpen],
        studioNodePresetPanel: ['studioNodePrompt', 'studioNodePresetBtn', !!STUDIO.nodePresetOpen],
        testPresetPanel: ['testPrompt', 'testPresetBtn', !!window.__TEST_PRESET_OPEN],
        creativeCanvasPresetPanel: ['creativeCanvasPrompt', 'creativeCanvasPresetBtn', !!CREATIVE_CANVAS.presetOpen],
      }[panelId] || ['', '', true];
      renderPresetPanel(panelId, mapping[0], mapping[2], mapping[1]);
    }
    function bindCatalogPickerControls(panel, panelId, kind) {
      const state = catalogPickerState(panelId);
      panel.querySelectorAll('[data-picker-page]').forEach(button => button.onclick = () => {
        void loadCatalogPickerPage(panelId, kind, state.page + Number(button.dataset.pickerPage));
      });
      const search = panel.querySelector(kind === 'cos' ? '[data-cos-search]' : '[data-prompt-search]');
      if (search) search.addEventListener('input', event => {
        const value = String(event.target.value || '');
        state.query = value;
        state.page = 1;
        state.seq += 1;
        clearTimeout(PICKER_SEARCH_TIMERS.get(panelId));
        PICKER_SEARCH_TIMERS.set(panelId, setTimeout(() => void loadCatalogPickerPage(panelId, kind, 1), 250));
      });
    }
    function renderPresetPanel(panelId, targetInputId, open, btnId) {
      const panel = $(panelId);
      if (!panel) return;
      if (btnId) syncPresetToggleButton(btnId, open);
      if (!open) { panel.classList.remove('open'); panel.innerHTML = ''; return; }
      panel.classList.add('open');
      const state = catalogPickerState(panelId);
      if (PICKER_LOADING.has(panelId)) { panel.innerHTML = '<div class="status">正在加载预设…</div>'; return; }
      if (state.error) {
        panel.innerHTML = `<div class="status">${escapeHtml(state.error)} <button type="button" class="secondary mini" data-picker-retry>重试</button></div>`;
        panel.querySelector('[data-picker-retry]')?.addEventListener('click', () => void loadCatalogPickerPage(panelId, 'image', state.page, true));
        return;
      }
      if (!state.loaded) {
        panel.innerHTML = '<div class="status">正在加载预设…</div>';
        if (!state.autoLoadScheduled) {
          state.autoLoadScheduled = true;
          queueMicrotask(() => { state.autoLoadScheduled = false; void loadCatalogPickerPage(panelId, 'image', state.page); });
        }
        return;
      }
      const rows = state.items;
      const query = state.query;
      let lastGroup = '';
      const presetButtons = rows.map((item, idx) => {
        const name = escapeHtml(item.name || item.title || '');
        const group = promptPresetGroup(item);
        const heading = group && group !== lastGroup ? `<div class="preset-group-heading">${escapeHtml(promptPresetGroupLabel(item))}</div>` : '';
        lastGroup = group;
        return `${heading}<button type="button" class="preset-chip" data-preset-idx="${idx}" title="${name}">${name}</button>`;
      }).join('');
      const pages = Math.max(1, Math.ceil(state.total / PICKER_PAGE_SIZE));
      panel.innerHTML = `<div class="cos-panel-toolbar"><span class="muted">第 ${state.page}/${pages} 页 · 共 ${state.total} 项</span></div><input class="studio-node-search" data-prompt-search type="search" placeholder="搜索预设" aria-label="搜索预设" value="${escapeHtml(query)}">` + (presetButtons || `<div class="status">${query ? '没有匹配的预设' : '暂无预设'}</div>` ) + pickerPagerMarkup(state);
      panel.querySelectorAll('[data-preset-idx]').forEach(btn => btn.onclick = () => {
        const item = rows[Number(btn.getAttribute('data-preset-idx'))];
        if (!item) return;
        const input = $(targetInputId);
        if (input) {
          input.value = item.mode === 'special'
            ? `${item.prompt || ''} 【cos:${item.id || ''}】【cos_mode:special】`
            : (item.prompt || '');
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        panel.querySelectorAll('.preset-chip').forEach(el => el.classList.remove('active'));
        btn.classList.add('active');
      });
      bindCatalogPickerControls(panel, panelId, 'image');
    }
    function presetManagerStatus(text, bad = false) {
      const el = $('presetManagerStatus');
      if (!el) return;
      el.textContent = text || '';
      el.className = 'status' + (bad ? ' bad' : '');
    }
    function presetCosManagerStatus(text, bad = false) {
      const el = $('presetCosManagerStatus');
      if (!el) return;
      el.textContent = text || '';
      el.className = 'status' + (bad ? ' bad' : '');
    }
    function renderManagedCosPool() {
      const wrap = $('presetCosManagerList');
      if (!wrap) return;
      const rows = COS_MANAGE_ROWS;
      const query = String(PRESET_MANAGER_QUERIES.cos || '').trim();
      if (!rows.length) {
        wrap.innerHTML = `<div class="status">${query ? '没有匹配的 COS 池项目。' : '暂无 COS 池项目，点击“维护 COS 池”添加自定义项目。'}</div>`;
        renderPresetManagerPager('cos');
        return;
      }
      wrap.innerHTML = rows.map((item, idx) => {
        const title = escapeHtml(item.title || item.id || '未命名套装');
        const prompt = escapeHtml(item.prompt || '');
        const source = item.source === 'custom' ? '自定义' : '内置';
        const tags = Array.isArray(item.tags) ? item.tags.filter(Boolean).join('、') : '';
        return `<article class="preset-manager-row">
          <div><div class="preset-manager-name">${title}</div><div class="preset-manager-meta"><span class="pill">${source}</span>${item.favorite ? '<span class="pill green">已收藏</span>' : ''}${tags ? `<span class="pill">${escapeHtml(tags)}</span>` : ''}</div></div>
          <div class="preset-manager-prompt">${prompt}</div>
          <div class="preset-manager-actions"><button class="secondary mini" type="button" data-cos-manager-favorite="${idx}">${item.favorite ? '取消收藏' : '收藏'}</button>${item.source === 'custom' ? `<button class="danger mini" type="button" data-cos-manager-delete="${idx}">删除</button>` : ''}</div>
        </article>`;
      }).join('');
      wrap.querySelectorAll('[data-cos-manager-favorite]').forEach(button => button.onclick = async () => {
        const item = rows[Number(button.dataset.cosManagerFavorite)];
        if (!item?.id) return;
        button.disabled = true;
        try {
          await api('/api/cos-pools/favorite', {method:'POST', body:JSON.stringify({id:item.id, enabled:!item.favorite})});
          await refreshCosCatalogViews(true);
        } catch (e) {
          presetCosManagerStatus(e.message || '收藏失败', true);
          button.disabled = false;
        }
      });
      wrap.querySelectorAll('[data-cos-manager-delete]').forEach(button => button.onclick = async () => {
        const item = rows[Number(button.dataset.cosManagerDelete)];
        if (!item?.id || !await confirmAction(`确定删除自定义 COS「${item.title || item.id}」吗？`, {title:'删除自定义 COS', confirmText:'删除'})) return;
        try {
          await api('/api/cos-pools/custom/delete', {method:'POST', body:JSON.stringify({id:item.id})});
          await refreshCosCatalogViews(true);
          showToast('自定义 COS 已删除', 'ok');
        } catch (e) { presetCosManagerStatus(e.message || '删除失败', true); }
      });
      renderPresetManagerPager('cos');
    }
    async function loadManagedCosPool(force = false) {
      PRESET_KIND = 'cos';
      presetCosManagerStatus('正在读取 COS 池…');
      const loadSeq = ++PRESET_LOAD_SEQ;
      const page = PRESET_MANAGER_PAGES.cos;
      const query = String(PRESET_MANAGER_QUERIES.cos || '').trim();
      try {
        const data = await fetchManagedCatalogPage('cos', query, page, force);
        if (loadSeq !== PRESET_LOAD_SEQ || PRESET_KIND !== 'cos') return;
        COS_MANAGE_ROWS = data.items;
        PRESET_MANAGER_TOTALS.cos = Number(data.total || 0);
        const pageCount = Math.max(1, Math.ceil(PRESET_MANAGER_TOTALS.cos / PICKER_PAGE_SIZE));
        if (page > pageCount) { PRESET_MANAGER_PAGES.cos = pageCount; return loadManagedCosPool(force); }
        prefetchManagedCatalogPages('cos', query, page, PRESET_MANAGER_TOTALS.cos);
        renderManagedCosPool();
        presetCosManagerStatus(`COS 池 · 共 ${PRESET_MANAGER_TOTALS.cos} 个 · 第 ${page}/${pageCount} 页`);
      } catch (e) {
        if (loadSeq !== PRESET_LOAD_SEQ) return;
        COS_MANAGE_ROWS = [];
        PRESET_MANAGER_TOTALS.cos = 0;
        renderManagedCosPool();
        presetCosManagerStatus(e.message || 'COS 池读取失败', true);
      }
    }
    async function loadManagedPromptPresets(kind = PRESET_KIND) {
      if (kind === 'cos') return loadManagedCosPool();
      const requestedKind = kind === 'nai' ? 'nai' : 'image';
      PRESET_KIND = requestedKind;
      presetManagerStatus('正在读取预设…');
      const loadSeq = ++PRESET_LOAD_SEQ;
      const page = PRESET_MANAGER_PAGES[requestedKind];
      const query = String(PRESET_MANAGER_QUERIES[requestedKind] || '').trim();
      try {
        const data = await fetchManagedCatalogPage(requestedKind, query, page);
        // A slower request from the previous tab must not overwrite the current list.
        if (loadSeq !== PRESET_LOAD_SEQ || requestedKind !== PRESET_KIND) return;
        PRESET_MANAGE_ROWS = data.items;
        PRESET_MANAGER_TOTALS[requestedKind] = Number(data.total || 0);
        const pageCount = Math.max(1, Math.ceil(PRESET_MANAGER_TOTALS[requestedKind] / PICKER_PAGE_SIZE));
        if (page > pageCount) { PRESET_MANAGER_PAGES[requestedKind] = pageCount; return loadManagedPromptPresets(requestedKind); }
        prefetchManagedCatalogPages(requestedKind, query, page, PRESET_MANAGER_TOTALS[requestedKind]);
        renderManagedPromptPresets();
        presetManagerStatus(`${requestedKind === 'nai' ? 'NAI 画师串' : '生图预设'} · 共 ${PRESET_MANAGER_TOTALS[requestedKind]} 个 · 第 ${page}/${pageCount} 页`);
      } catch (e) {
        if (loadSeq !== PRESET_LOAD_SEQ || requestedKind !== PRESET_KIND) return;
        PRESET_MANAGE_ROWS = [];
        PRESET_MANAGER_TOTALS[requestedKind] = 0;
        renderManagedPromptPresets();
        presetManagerStatus(e.message || '预设读取失败', true);
      }
    }
    function renderManagedPromptPresets() {
      const wrap = $('presetManagerList');
      if (!wrap) return;
      const kind = PRESET_KIND === 'nai' ? 'nai' : 'image';
      const query = String(PRESET_MANAGER_QUERIES[kind] || '').trim();
      if (!PRESET_MANAGE_ROWS.length) {
        wrap.innerHTML = `<div class="status">${query ? '没有匹配的预设。' : '暂无预设，先新增一个。'}</div>`;
        renderPresetManagerPager(kind);
        return;
      }
      let lastGroup = '';
      wrap.innerHTML = PRESET_MANAGE_ROWS.map((item, idx) => {
        const name = escapeHtml(item.name || item.title || '未命名');
        const prompt = escapeHtml(item.prompt || '');
        const desc = escapeHtml(item.description || '');
        const source = item.source === 'builtin' ? '内置' : '自定义';
        const meta = [item.aspect_ratio ? '比例 ' + escapeHtml(item.aspect_ratio) : '', item.resolution ? '清晰度 ' + escapeHtml(item.resolution) : '', source]
          .filter(Boolean).map(v => `<span class="pill">${v}</span>`).join('');
        const group = promptPresetGroup(item);
        const heading = group && group !== lastGroup ? `<div class="preset-group-heading">${escapeHtml(promptPresetGroupLabel(item))}</div>` : '';
        lastGroup = group;
        return `${heading}<article class="preset-manager-row">
          <div><div class="preset-manager-name">${name}</div><div class="preset-manager-meta">${meta}</div>${desc ? `<div class="muted" style="margin-top:6px">${desc}</div>` : ''}</div>
          <div class="preset-manager-prompt">${prompt}</div>
          <div class="preset-manager-actions"><button class="secondary mini" type="button" data-preset-edit="${idx}">编辑</button><button class="danger mini" type="button" data-preset-delete="${idx}">删除</button></div>
        </article>`;
      }).join('');
      wrap.querySelectorAll('[data-preset-edit]').forEach(btn => btn.onclick = () => openPresetEditor(PRESET_MANAGE_ROWS[Number(btn.dataset.presetEdit)]));
      wrap.querySelectorAll('[data-preset-delete]').forEach(btn => btn.onclick = () => deleteManagedPromptPreset(PRESET_MANAGE_ROWS[Number(btn.dataset.presetDelete)]));
      renderPresetManagerPager(kind);
    }
    function renderPresetManagerPager(kind) {
      const id = kind === 'cos' ? 'presetCosManagerPager' : 'presetManagerPager';
      const wrap = $(id);
      if (!wrap) return;
      const total = PRESET_MANAGER_TOTALS[kind] || 0;
      const page = PRESET_MANAGER_PAGES[kind] || 1;
      const pages = Math.max(1, Math.ceil(total / PICKER_PAGE_SIZE));
      if (pages <= 1) { wrap.innerHTML = ''; return; }
      const start = (page - 1) * PICKER_PAGE_SIZE + 1;
      const end = Math.min(total, start + (kind === 'cos' ? COS_MANAGE_ROWS.length : PRESET_MANAGE_ROWS.length) - 1);
      wrap.innerHTML = `<span class="muted">${start}-${end} / ${total}</span><button class="secondary mini" type="button" data-manager-page="-1" ${page <= 1 ? 'disabled' : ''}>上一页</button><button class="secondary mini" type="button" data-manager-page="1" ${page >= pages ? 'disabled' : ''}>下一页</button>`;
      wrap.querySelectorAll('[data-manager-page]').forEach(button => button.onclick = () => {
        PRESET_MANAGER_PAGES[kind] = page + Number(button.dataset.managerPage);
        if (kind === 'cos') void loadManagedCosPool();
        else void loadManagedPromptPresets(kind);
      });
    }
    function schedulePresetManagerSearch(kind, value) {
      PRESET_MANAGER_QUERIES[kind] = String(value || '');
      PRESET_MANAGER_PAGES[kind] = 1;
      clearTimeout(PRESET_MANAGER_SEARCH_TIMERS[kind]);
      PRESET_MANAGER_SEARCH_TIMERS[kind] = setTimeout(() => {
        if (PRESET_KIND !== kind) return;
        if (kind === 'cos') void loadManagedCosPool();
        else void loadManagedPromptPresets(kind);
      }, 250);
    }
    async function switchPresetKind(kind) {
      PRESET_KIND = ['cos', 'nai'].includes(kind) ? kind : 'image';
      Object.values(PRESET_MANAGER_SEARCH_TIMERS).forEach(clearTimeout);
      $('presetKindImage').classList.toggle('active', PRESET_KIND === 'image');
      $('presetKindCos').classList.toggle('active', PRESET_KIND === 'cos');
      $('presetKindNai').classList.toggle('active', PRESET_KIND === 'nai');
      $('presetManagerSearch').value = PRESET_MANAGER_QUERIES[PRESET_KIND] || '';
      $('presetManagerSearch').placeholder = PRESET_KIND === 'nai' ? '搜索画师名或画师串' : '搜索预设名或提示词';
      $('presetManagerSearch').setAttribute('aria-label', $('presetManagerSearch').placeholder);
      const isCos = PRESET_KIND === 'cos';
      const promptManager = $('presetPromptManager');
      const cosManager = $('presetCosManager');
      if (promptManager) {
        promptManager.hidden = isCos;
        promptManager.style.display = isCos ? 'none' : '';
      }
      if (cosManager) {
        cosManager.hidden = !isCos;
        cosManager.style.display = isCos ? '' : 'none';
      }
      if ($('presetAddBtn')) $('presetAddBtn').style.display = isCos ? 'none' : '';
      if ($('presetImportBtn')) $('presetImportBtn').textContent = isCos ? '导入池' : '导入文本';
      if ($('presetExportBtn')) $('presetExportBtn').textContent = isCos ? '导出池' : '导出并复制';
      presetManagerStatus(isCos ? 'COS 池管理' : '正在读取预设…');
      if (isCos) await loadManagedCosPool();
      else await loadManagedPromptPresets(PRESET_KIND);
    }
    function openPresetEditor(row = null) {
      PRESET_EDIT_KIND = PRESET_KIND;
      PRESET_EDIT_ORIGINAL = row ? String(row.name || '') : '';
      $('presetEditorTitle').textContent = (row ? '编辑' : '新增') + (PRESET_KIND === 'nai' ? '画师串' : '预设');
      $('presetName').value = row?.name || '';
      $('presetPrompt').value = row?.prompt || '';
      $('presetDescription').value = row?.description || '';
      $('presetAspect').value = row?.aspect_ratio || '';
      $('presetResolution').value = row?.resolution || '';
      $('presetExtraPrompt').value = row?.extra_prompt || '';
      $('presetAspectWrap').style.display = PRESET_KIND === 'nai' ? 'none' : '';
      $('presetResolutionWrap').style.display = PRESET_KIND === 'nai' ? 'none' : '';
      $('presetEditorStatus').textContent = '';
      $('presetEditorModal').classList.add('show');
      $('presetName').focus();
    }
    function closePresetEditor() {
      $('presetEditorModal').classList.remove('show');
      PRESET_EDIT_ORIGINAL = '';
    }
    async function saveManagedPromptPreset() {
      const body = {
        original_name: PRESET_EDIT_ORIGINAL,
        name: $('presetName').value.trim(),
        prompt: $('presetPrompt').value.trim(),
        description: $('presetDescription').value.trim(),
        aspect_ratio: $('presetAspect').value.trim(),
        resolution: $('presetResolution').value.trim(),
        extra_prompt: $('presetExtraPrompt').value.trim(),
        kind: PRESET_EDIT_KIND
      };
      if (!body.name || !body.prompt) {
        $('presetEditorStatus').textContent = '预设名和提示词不能为空';
        return;
      }
      $('presetEditorStatus').textContent = '保存中…';
      try {
        const isNew = !PRESET_EDIT_ORIGINAL;
        const res = await api('/api/prompt-presets/manage/save', {method:'POST', body:JSON.stringify(body)});
        closePresetEditor();
        if (isNew) PRESET_MANAGER_PAGES[body.kind] = 1;
        clearPickerCache(body.kind);
        if (body.kind === 'image') await refreshPromptCatalogViews(true);
        if (PRESET_KIND === body.kind) await loadManagedPromptPresets(body.kind);
        showToast((res.data && res.data.message) || '预设已保存', 'ok');
      } catch (e) {
        $('presetEditorStatus').textContent = e.message || '保存失败';
      }
    }
    async function deleteManagedPromptPreset(row) {
      if (!row || !row.name) return;
      const kind = PRESET_KIND;
      if (!await confirmAction(`确定删除预设「${row.name}」吗？`, {title:'删除预设', confirmText:'删除预设'})) return;
      try {
        const res = await api('/api/prompt-presets/manage/delete', {method:'POST', body:JSON.stringify({name:row.name, kind})});
        clearPickerCache(kind);
        if (kind === 'image') await refreshPromptCatalogViews(true);
        if (PRESET_KIND === kind) await loadManagedPromptPresets(kind);
        showToast((res.data && res.data.message) || '预设已删除', 'ok');
      } catch (e) {
        presetManagerStatus(e.message || '删除失败', true);
      }
    }
    function presetTransferPayload(rows = PRESET_MANAGE_ROWS, kind = PRESET_KIND) {
      return {
        version: 1,
        type: 'selfie-image-prompt-presets',
        kind,
        presets: rows.map(item => ({
          name: item.name || item.title || '', prompt: item.prompt || '', description: item.description || '',
          aspect_ratio: item.aspect_ratio || '', resolution: item.resolution || '', extra_prompt: item.extra_prompt || ''
        }))
      };
    }
    async function openPresetExport() {
      if (PRESET_KIND === 'cos') {
        exportCosPool();
        return;
      }
      const kind = PRESET_KIND;
      try {
        const res = await api('/api/prompt-presets/manage?kind=' + encodeURIComponent(kind));
        if (PRESET_KIND !== kind) return;
        const data = res.data;
        const loadStatus = Array.isArray(data) ? res.load_status : data?.load_status;
        if (loadStatus && loadStatus.ok === false) throw new Error(loadStatus.error || '预设读取失败');
        const rows = Array.isArray(data) ? data : (Array.isArray(data?.presets) ? data.presets : []);
        $('presetTransferTitle').textContent = '导出预设文本';
        $('presetTransferHint').textContent = '已生成 JSON 文本。系统会尝试写入剪贴板，失败时请在下方文本框中手动全选复制。';
        $('presetTransferText').value = JSON.stringify(presetTransferPayload(rows, kind), null, 2);
        $('presetTransferImportBtn').style.display = 'none';
        $('presetTransferCopyBtn').style.display = '';
        $('presetTransferStatus').textContent = '';
        $('presetTransferModal').classList.add('show');
        try { await copyPresetTransfer(); } catch (e) {
          $('presetTransferStatus').textContent = e.message || '复制失败，请手动复制';
        }
      } catch (e) {
        showToast(e.message || '预设导出失败', 'bad');
      }
    }
    function openPresetImport() {
      if (PRESET_KIND === 'cos') {
        openCosPoolImport();
        return;
      }
      PRESET_TRANSFER_KIND = PRESET_KIND;
      $('presetTransferTitle').textContent = '导入预设文本';
      $('presetTransferHint').textContent = '把导出的 JSON 粘贴到这里，导入会按名称合并，重名预设将被覆盖。';
      $('presetTransferText').value = '';
      $('presetTransferCopyBtn').style.display = 'none';
      $('presetTransferImportBtn').style.display = '';
      $('presetTransferStatus').textContent = '';
      $('presetTransferModal').classList.add('show');
      $('presetTransferText').focus();
    }
    function closePresetTransfer() { $('presetTransferModal').classList.remove('show'); }
    async function copyPresetTransfer() {
      const text = $('presetTransferText').value || '';
      if (!text) return;
      try {
        await copyTextToClipboard(text);
        $('presetTransferStatus').textContent = '已写入剪贴板，也可以继续从上方文本框手动复制。';
        showToast('预设 JSON 已复制', 'ok');
      } catch (e) {
        $('presetTransferStatus').textContent = '自动复制失败，请点击文本框后手动全选复制。';
        $('presetTransferText').focus();
        $('presetTransferText').select();
      }
    }
    async function importPresetTransfer() {
      const kind = PRESET_TRANSFER_KIND;
      let parsed;
      try { parsed = JSON.parse($('presetTransferText').value || ''); }
      catch (_) { $('presetTransferStatus').textContent = 'JSON 格式不正确'; return; }
      if (parsed?.type === 'selfie-image-prompt-presets' && parsed.kind && parsed.kind !== kind) {
        $('presetTransferStatus').textContent = '预设分类不一致，请切换到对应分类后导入';
        return;
      }
      const presets = Array.isArray(parsed) ? parsed : parsed && (parsed.presets || parsed.items || parsed);
      if (!Array.isArray(presets) && (!presets || typeof presets !== 'object')) {
        $('presetTransferStatus').textContent = '没有找到 presets 数组';
        return;
      }
      $('presetTransferStatus').textContent = '导入中…';
      try {
        const res = await api('/api/prompt-presets/manage/import', {method:'POST', body:JSON.stringify({presets, kind})});
        closePresetTransfer();
        PRESET_MANAGER_PAGES[kind] = 1;
        clearPickerCache(kind);
        if (kind === 'image') await refreshPromptCatalogViews(true);
        if (PRESET_KIND === kind) await loadManagedPromptPresets(kind);
        showToast((res.data && res.data.message) || '预设已导入', 'ok');
      } catch (e) {
        $('presetTransferStatus').textContent = e.message || '导入失败';
      }
    }
    function renderCosPanel(panelId, targetInputId, open, btnId) {
      const panel = $(panelId);
      if (!panel) return;
      if (btnId) syncCosToggleButton(btnId, open);
      if (!open) { panel.classList.remove('open'); panel.innerHTML = ''; return; }
      panel.classList.add('open');
      if (PICKER_LOADING.has(panelId)) { panel.innerHTML = '<div class="status">正在加载 COS 池…</div>'; return; }
      const state = catalogPickerState(panelId);
      if (state.error) {
        panel.innerHTML = `<div class="status">${escapeHtml(state.error)} <button type="button" class="secondary mini" data-picker-retry>重试</button></div>`;
        panel.querySelector('[data-picker-retry]')?.addEventListener('click', () => void loadCatalogPickerPage(panelId, 'cos', state.page, true));
        return;
      }
      if (!state.loaded) {
        panel.innerHTML = '<div class="status">正在加载 COS 池…</div>';
        if (!state.autoLoadScheduled) {
          state.autoLoadScheduled = true;
          queueMicrotask(() => { state.autoLoadScheduled = false; void loadCatalogPickerPage(panelId, 'cos', state.page); });
        }
        return;
      }
      const rows = state.items;
      const pages = Math.max(1, Math.ceil(state.total / PICKER_PAGE_SIZE));
      panel.innerHTML = `<div class="cos-panel-toolbar"><button class="secondary mini" type="button" data-cos-maintain>维护 COS 池</button><span class="muted">第 ${state.page}/${pages} 页 · 共 ${state.total} 项</span></div><input class="studio-node-search" data-cos-search type="search" placeholder="搜索 COS" aria-label="搜索 COS" value="${escapeHtml(state.query)}">` + (rows.length ? rows.map((item, idx) => {
        const title = escapeHtml(item.title || item.id || '未命名套装');
        const compatibility = item.compatibility && item.compatibility.variation_enabled
          ? '可随机姿势/场景'
          : '固定构图';
        const source = item.source === 'custom' ? '自定义' : (item.favorite ? '已收藏' : '内置');
        return `<div class="cos-pool-row"><button type="button" class="preset-chip" data-cos-idx="${idx}" title="${title} · ${compatibility}">${title}<small>${escapeHtml(source)}</small></button><button type="button" class="secondary mini cos-favorite-btn" data-cos-favorite="${idx}" title="${item.favorite ? '取消收藏' : '收藏'}" aria-label="${item.favorite ? '取消收藏' : '收藏'}">${item.favorite ? '★' : '☆'}</button>${item.source === 'custom' ? `<button type="button" class="danger mini cos-delete-btn" data-cos-delete="${idx}" title="删除自定义 COS" aria-label="删除自定义 COS">×</button>` : ''}</div>`;
      }).join('') : `<div class="status">${state.query ? '没有匹配的 COS 项目' : 'COS 池为空'}</div>`) + pickerPagerMarkup(state);
      panel.querySelector('[data-cos-maintain]')?.addEventListener('click', openCosPoolModal);
      bindCatalogPickerControls(panel, panelId, 'cos');
      panel.querySelectorAll('[data-cos-idx]').forEach(btn => {
        btn.onclick = () => {
          const item = rows[Number(btn.getAttribute('data-cos-idx'))];
          if (!item) return;
          const input = $(targetInputId);
          if (input) {
            input.value = item.mode === 'special'
              ? `${item.prompt || ''} 【cos:${item.id || ''}】【cos_mode:special】`
              : (item.prompt || '');
            input.dispatchEvent(new Event('input', {bubbles:true}));
          }
          panel.querySelectorAll('.preset-chip').forEach(el => el.classList.remove('active'));
          btn.classList.add('active');
        };
      });
      panel.querySelectorAll('[data-cos-favorite]').forEach(btn => btn.onclick = async event => {
        event.stopPropagation();
        const item = rows[Number(btn.dataset.cosFavorite)];
        if (!item?.id) return;
        try {
          await api('/api/cos-pools/favorite', {method:'POST', body: JSON.stringify({id:item.id, enabled:!item.favorite})});
          await refreshCosCatalogViews(true);
        } catch (e) { showToast(e.message || '收藏失败', 'bad'); }
      });
      panel.querySelectorAll('[data-cos-delete]').forEach(btn => btn.onclick = async event => {
        event.stopPropagation();
        const item = rows[Number(btn.dataset.cosDelete)];
        if (!item?.id || !await confirmAction(`确定删除自定义 COS「${item.title || item.id}」吗？`, {title:'删除自定义 COS', confirmText:'删除'})) return;
        try {
          await api('/api/cos-pools/custom/delete', {method:'POST', body: JSON.stringify({id:item.id})});
          await refreshCosCatalogViews(true);
          showToast('自定义 COS 已删除', 'ok');
        } catch (e) { showToast(e.message || '删除失败', 'bad'); }
      });
    }
    function cosPoolSort(a, b) {
      if (!!a.favorite !== !!b.favorite) return a.favorite ? -1 : 1;
      if (String(a.source || '') !== String(b.source || '')) return a.source === 'custom' ? -1 : 1;
      return String(a.title || a.id || '').localeCompare(String(b.title || b.id || ''), 'zh');
    }
    async function ensureCosLooksLoaded(force = false) {
      return refreshCosCatalogViews(force);
    }
    async function refreshCosCatalogViews(force = true) {
      clearPickerCache('cos');
      const jobs = [];
      const openPanels = [
        ['studioCosPanel', !!STUDIO.cosOpen],
        ['studioNodeCosPanel', !!STUDIO.nodeCosOpen],
        ['testCosPanel', !!window.__TEST_COS_OPEN],
        ['creativeCanvasCosPanel', !!CREATIVE_CANVAS.cosOpen],
      ];
      for (const [panelId, isOpen] of openPanels) {
        if (!isOpen) continue;
        const state = catalogPickerState(panelId);
        jobs.push(loadCatalogPickerPage(panelId, 'cos', state.page, force));
      }
      if (PRESET_KIND === 'cos' && document.querySelector('nav.page-nav button.active')?.dataset.tab === 'presets') {
        jobs.push(loadManagedCosPool(true));
      }
      await Promise.all(jobs);
    }
    async function refreshPromptCatalogViews(force = true) {
      clearPickerCache('image');
      const jobs = [];
      const openPanels = [
        ['studioPresetPanel', !!STUDIO.presetOpen],
        ['studioNodePresetPanel', !!STUDIO.nodePresetOpen],
        ['testPresetPanel', !!window.__TEST_PRESET_OPEN],
        ['creativeCanvasPresetPanel', !!CREATIVE_CANVAS.presetOpen],
      ];
      for (const [panelId, isOpen] of openPanels) {
        if (!isOpen) continue;
        const state = catalogPickerState(panelId);
        jobs.push(loadCatalogPickerPage(panelId, 'image', state.page, force));
      }
      if ($('assetStudioPreset')) jobs.push(loadAssetStudioPresets(ASSET_STUDIO_PRESET_PAGE));
      await Promise.all(jobs);
    }
    function openCosPoolModal() {
      ['cosCustomId','cosCustomTitle','cosCustomPrompt','cosCustomTags'].forEach(id => { if ($(id)) $(id).value = ''; });
      if ($('cosPoolStatus')) $('cosPoolStatus').textContent = '普通自定义 COS 使用兼容矩阵；带 mode=special 的固定 COS 会完整保留自己的场景、风格、姿势和镜头。';
      $('cosPoolModal')?.classList.add('show');
      $('cosCustomId')?.focus();
    }
    function closeCosPoolModal() { $('cosPoolModal')?.classList.remove('show'); }
    async function saveCustomCos() {
      const id = String($('cosCustomId')?.value || '').trim();
      const title = String($('cosCustomTitle')?.value || '').trim();
      const prompt = String($('cosCustomPrompt')?.value || '').trim();
      const status = $('cosPoolStatus');
      if (!id || !title || !prompt) { if (status) status.textContent = 'ID、名称和服饰提示词不能为空'; return; }
      try {
        await api('/api/cos-pools/custom/save', {method:'POST', body: JSON.stringify({id, title, prompt, tags:String($('cosCustomTags')?.value || '').split(/[,，]/).map(v => v.trim()).filter(Boolean)})});
        await ensureCosLooksLoaded(true);
        closeCosPoolModal();
        showToast('自定义 COS 已保存', 'ok');
      } catch (e) { if (status) status.textContent = e.message || '保存失败'; }
    }
    function exportCosPool() {
      api('/api/cos-pools/export').then(res => {
        const blob = new Blob([JSON.stringify(res.data || {}, null, 2)], {type:'application/json;charset=utf-8'});
        const url = URL.createObjectURL(blob); const link = document.createElement('a');
        link.href = url; link.download = `selfie-image-cos-pool-${new Date().toISOString().slice(0,10)}.json`;
        document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      }).catch(e => showToast(e.message || '导出失败', 'bad'));
    }
    function openCosPoolImport() {
      const input = document.createElement('input'); input.type = 'file'; input.accept = 'application/json,.json';
      input.onchange = async () => {
        const file = input.files?.[0]; if (!file) return;
        try {
          const payload = JSON.parse(await file.text());
          await api('/api/cos-pools/import', {method:'POST', body: JSON.stringify(payload)});
          await ensureCosLooksLoaded(true); closeCosPoolModal();
          showToast('COS 池已导入', 'ok');
        } catch (e) { if ($('cosPoolStatus')) $('cosPoolStatus').textContent = e.message || '导入失败'; }
      };
      input.click();
    }
    async function toggleStudioPresets() {
      STUDIO.presetOpen = !STUDIO.presetOpen;
      STUDIO.cosOpen = false;
      renderCosPanel('studioCosPanel', 'studioPrompt', false, 'studioCosBtn');
      if (STUDIO.presetOpen) {
        const state = catalogPickerState('studioPresetPanel');
        await loadCatalogPickerPage('studioPresetPanel', 'image', state.page);
        window.__TEST_PRESET_OPEN = false;
        window.__TEST_COS_OPEN = false;
        renderPresetPanel('testPresetPanel', 'testPrompt', false, 'testPresetBtn');
        renderCosPanel('testCosPanel', 'testPrompt', false, 'testCosBtn');
      }
      renderPresetPanel('studioPresetPanel', 'studioPrompt', STUDIO.presetOpen, 'studioPresetBtn');
    }
    async function toggleStudioCos() {
      STUDIO.cosOpen = !STUDIO.cosOpen;
      STUDIO.presetOpen = false;
      renderPresetPanel('studioPresetPanel', 'studioPrompt', false, 'studioPresetBtn');
      if (STUDIO.cosOpen) {
        const state = catalogPickerState('studioCosPanel');
        await loadCatalogPickerPage('studioCosPanel', 'cos', state.page);
        window.__TEST_PRESET_OPEN = false;
        window.__TEST_COS_OPEN = false;
        renderPresetPanel('testPresetPanel', 'testPrompt', false, 'testPresetBtn');
        renderCosPanel('testCosPanel', 'testPrompt', false, 'testCosBtn');
      }
      renderCosPanel('studioCosPanel', 'studioPrompt', STUDIO.cosOpen, 'studioCosBtn');
    }
    async function toggleTestPresets() {
      window.__TEST_PRESET_OPEN = !window.__TEST_PRESET_OPEN;
      window.__TEST_COS_OPEN = false;
      renderCosPanel('testCosPanel', 'testPrompt', false, 'testCosBtn');
      if (window.__TEST_PRESET_OPEN) {
        const state = catalogPickerState('testPresetPanel');
        await loadCatalogPickerPage('testPresetPanel', 'image', state.page);
        STUDIO.presetOpen = false;
        STUDIO.cosOpen = false;
        renderPresetPanel('studioPresetPanel', 'studioPrompt', false, 'studioPresetBtn');
        renderCosPanel('studioCosPanel', 'studioPrompt', false, 'studioCosBtn');
      }
      renderPresetPanel('testPresetPanel', 'testPrompt', !!window.__TEST_PRESET_OPEN, 'testPresetBtn');
    }
    async function toggleTestCos() {
      window.__TEST_COS_OPEN = !window.__TEST_COS_OPEN;
      window.__TEST_PRESET_OPEN = false;
      renderPresetPanel('testPresetPanel', 'testPrompt', false, 'testPresetBtn');
      if (window.__TEST_COS_OPEN) {
        const state = catalogPickerState('testCosPanel');
        await loadCatalogPickerPage('testCosPanel', 'cos', state.page);
        STUDIO.presetOpen = false;
        STUDIO.cosOpen = false;
        renderPresetPanel('studioPresetPanel', 'studioPrompt', false, 'studioPresetBtn');
        renderCosPanel('studioCosPanel', 'studioPrompt', false, 'studioCosBtn');
      }
      renderCosPanel('testCosPanel', 'testPrompt', !!window.__TEST_COS_OPEN, 'testCosBtn');
    }
