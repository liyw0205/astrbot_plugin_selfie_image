    function uniq(values) {
      const out = [], seen = new Set();
      for (const value of values || []) {
        const text = String(value || '').trim();
        if (text && !seen.has(text)) {
          seen.add(text);
          out.push(text);
        }
      }
      return out;
    }
    function modelId(item) {
      if (typeof item === 'string') return item.trim();
      if (item && typeof item === 'object') return String(item.id || item.model || item.name || '').trim();
      return '';
    }
    function normalizeProviderType(value) {
      const raw = String(value || '').trim().toLowerCase().replace(/-/g, '_');
      const aliases = {
        openai_image: 'openai',
        openai_images: 'openai',
        openai_chat: 'openai_chat',
        chat_completions: 'openai_chat',
        openai_compatible: 'gemini_openai',
        google: 'gemini',
        google_gemini: 'gemini',
        zimage: 'z_image_gitee',
        z_image: 'z_image_gitee',
        gitee: 'z_image_gitee',
        jimeng: 'jimeng2api',
        jimeng2: 'jimeng2api',
        nai: 'novelai',
        novel_ai: 'novelai',
        novelai: 'novelai',
        nai2api: 'novelai',
        bestnai: 'novelai',
        ppnai: 'novelai',
        xai: 'grok',
        x_ai: 'grok',
        hy_image: 'hunyuan',
        hunyuan_image: 'hunyuan'
      };
      const normalized = aliases[raw] || raw;
      return PROVIDERS.includes(normalized) ? normalized : '';
    }
    function inferProviderTypeFromModel(model) {
      const compact = String(model || '').trim().toLowerCase().replace(/[\s_]+/g, '-');
      if (!compact) return '';
      if (compact.startsWith('hy-image') || compact.startsWith('hunyuan-image')) return 'hunyuan';
      if (compact.includes('agnes')) return 'agnes';
      if (compact.includes('z-image') || compact.startsWith('zimage')) return 'z_image_gitee';
      if (compact.includes('jimeng') || compact.includes('seedream') || compact.includes('doubao-seedream')) return 'jimeng2api';
      if (compact.includes('grok') || compact.includes('xai') || compact.includes('x-ai')) return 'grok';
      if (compact.includes('gpt-image') || compact.includes('dall-e') || compact.includes('dalle')) return 'openai';
      if (compact.includes('nai-diffusion') || compact.startsWith('nai-') || compact.includes('novelai') || /^nai[45](?:\b|[.-])/.test(compact)) return 'novelai';
      if (compact.includes('gemini') || compact.includes('nano-banana')) return 'gemini';
      return '';
    }
    function resolveModelProviderType(model, defaultProviderType, manualProviderType = '') {
      const manual = normalizeProviderType(manualProviderType);
      const def = normalizeProviderType(defaultProviderType) || 'openai';
      const inferred = inferProviderTypeFromModel(model);
      const strong = new Set(['grok','novelai','agnes','jimeng2api','z_image_gitee','hunyuan']);
      if (manual) {
        if (strong.has(inferred) && manual === def && inferred !== manual) return inferred;
        return manual;
      }
      if (strong.has(inferred)) return inferred;
      if (def === 'openai' || def === 'openai_chat' || def === 'gemini_openai') {
        return def;
      }
      return inferred || def || 'openai';
    }
    function collectModelDownloadProxyIds(ch, enabled) {
      const enabledSet = new Set(enabled || []);
      const out = {};
      const sources = [ch.model_download_proxy_ids, ch.modelDownloadProxyIds, ch.download_proxy_ids, ch.downloadProxyIds];
      for (const source of sources) {
        if (!source || typeof source !== 'object' || Array.isArray(source)) continue;
        for (const [model, proxyId] of Object.entries(source)) {
          const name = String(model || '').trim();
          const pid = String(proxyId || '').trim();
          if (name && enabledSet.has(name) && pid) out[name] = pid;
        }
      }
      return out;
    }
    function compactModelDownloadProxyIds(ch) {
      const enabledSet = new Set(ch.enabled_models || []);
      const out = {};
      for (const [model, proxyId] of Object.entries(ch.model_download_proxy_ids || {})) {
        const pid = String(proxyId || '').trim();
        if (enabledSet.has(model) && pid) out[model] = pid;
      }
      ch.model_download_proxy_ids = out;
      return ch;
    }
    function collectModelImageToTextModels(ch, enabled) {
      const enabledSet = new Set(enabled || []);
      const out = new Set();
      const sources = [ch.image_to_text_models, ch.imageToTextModels, ch.ocr_models, ch.ocrModels];
      for (const source of sources) {
        if (Array.isArray(source)) {
          for (const item of source) {
            const name = modelId(item);
            if (name && enabledSet.has(name) && (typeof item !== 'object' || item.enabled !== false)) out.add(name);
          }
        } else if (source && typeof source === 'object') {
          for (const [name, value] of Object.entries(source)) {
            if (enabledSet.has(name) && !!value) out.add(name);
          }
        }
      }
      for (const item of ch.enabled_models || ch.enabledModels || []) {
        if (!item || typeof item !== 'object') continue;
        const name = modelId(item);
        if (name && enabledSet.has(name) && !!(item.image_to_text ?? item.imageToText ?? item.ocr)) out.add(name);
      }
      return Array.from(out);
    }
    function compactModelImageToTextModels(ch) {
      const enabledSet = new Set(ch.enabled_models || []);
      ch.image_to_text_models = uniq((ch.image_to_text_models || []).filter(model => enabledSet.has(model)));
      return ch;
    }
    function collectModelPromptEnModels(ch, enabled) {
      const enabledSet = new Set(enabled || []);
      const out = new Set();
      const sources = [ch.prompt_en_models, ch.promptEnModels, ch.translate_prompt_models, ch.translatePromptModels];
      for (const source of sources) {
        if (Array.isArray(source)) {
          for (const item of source) {
            const name = modelId(item);
            if (name && enabledSet.has(name) && (typeof item !== 'object' || item.enabled !== false)) out.add(name);
          }
        } else if (source && typeof source === 'object') {
          for (const [name, value] of Object.entries(source)) {
            if (enabledSet.has(name) && !!value) out.add(name);
          }
        }
      }
      for (const item of ch.enabled_models || ch.enabledModels || []) {
        if (!item || typeof item !== 'object') continue;
        const name = modelId(item);
        if (name && enabledSet.has(name) && !!(item.prompt_en ?? item.promptEn ?? item.translate_prompt)) out.add(name);
      }
      return Array.from(out);
    }
    function compactModelPromptEnModels(ch) {
      const enabledSet = new Set(ch.enabled_models || []);
      ch.prompt_en_models = uniq((ch.prompt_en_models || []).filter(model => enabledSet.has(model)));
      return ch;
    }
    function collectModelProviderTypes(ch, enabled) {
      const enabledSet = new Set(enabled || []);
      const out = {};
      const sources = [ch.model_provider_types, ch.modelProviderTypes, ch.provider_types, ch.providerTypes];
      for (const source of sources) {
        if (!source || typeof source !== 'object' || Array.isArray(source)) continue;
        for (const [model, provider] of Object.entries(source)) {
          const name = String(model || '').trim();
          const resolved = normalizeProviderType(provider);
          if (name && enabledSet.has(name) && resolved) out[name] = resolved;
        }
      }
      for (const item of ch.enabled_models || ch.enabledModels || []) {
        if (!item || typeof item !== 'object') continue;
        const name = modelId(item);
        const raw = item.provider_type || item.providerType || item.api_type || item.apiType;
        const resolved = normalizeProviderType(raw);
        if (name && enabledSet.has(name) && resolved) out[name] = resolved;
      }
      return out;
    }
    function compactModelProviderTypes(ch) {
      const enabledSet = new Set(ch.enabled_models || []);
      const out = {};
      for (const [model, provider] of Object.entries(ch.model_provider_types || {})) {
        const resolved = normalizeProviderType(provider);
        if (enabledSet.has(model) && resolved) out[model] = resolved;
      }
      ch.model_provider_types = out;
      return ch;
    }
    function normalizeChannel(ch) {
      ch = ch && typeof ch === 'object' ? ch : {};
      const enabled = uniq((ch.enabled_models || ch.enabledModels || (ch.model ? [ch.model] : [])).map(modelId));
      const cache = uniq((ch.models_cache || ch.modelsCache || ch.available_models || ch.availableModels || []).map(modelId));
      const rawType = ch.provider_type || ch.providerType || ch.api_type || ch.apiType;
      const providerType = normalizeProviderType(rawType) || 'openai';
      const normalized = {
        id: String(ch.id || ch.channel_id || ch.channelId || '').trim(),
        name: String(ch.name || ch.id || 'new-channel').trim(),
        provider_type: providerType,
        base_url: String(ch.base_url || ch.baseUrl || '').trim(),
        api_key: String(ch.api_key || ch.apiKey || '').trim(),
        model: String(ch.model || enabled[0] || '').trim(),
        enabled: !(ch.enabled === false || ch.enabled === 'false' || ch.enabled === 0),
        enabled_models: enabled,
        image_to_text_models: collectModelImageToTextModels(ch, enabled),
        prompt_en_models: collectModelPromptEnModels(ch, enabled),
        model_download_proxy_ids: collectModelDownloadProxyIds(ch, enabled),
        model_provider_types: collectModelProviderTypes(Object.assign({}, ch, {provider_type: providerType}), enabled),
        models_cache: cache,
        proxy_id: String(ch.proxy_id || '').trim(),
        proxy: String(ch.proxy || '').trim(),
        extra: ch.extra && typeof ch.extra === 'object' ? ch.extra : {}
      };
      // API keys are masked by the server. Preserve the marker while loading
      // and saving; the server restores the original secret for existing rows.
      if (ch.api_key === '******' || ch.api_key === '[REDACTED]') normalized.api_key = ch.api_key;
      if (Array.isArray(ch.api_keys)) normalized.api_keys = ch.api_keys.slice();
      return compactModelPromptEnModels(compactModelProviderTypes(applyProviderDefaults(normalized)));
    }
    function normalizeChannels() {
      CONFIG.image_channels = (CONFIG.image_channels || []).map(normalizeChannel);
    }
    function normalizeAuditChannels() {
      CONFIG.audit_channels = (CONFIG.audit_channels || []).map(ch => {
        ch = normalizeChannel(ch);
        if (!AUDIT_PROVIDERS.includes(ch.provider_type)) ch.provider_type = 'openai';
        return ch;
      });
    }
    function channelListFor(kind) {
      if (kind === 'audit') return CONFIG.audit_channels;
      return CONFIG.image_channels;
    }
    function setChannelListFor(kind, list) {
      if (kind === 'audit') CONFIG.audit_channels = list;
      else CONFIG.image_channels = list;
    }
    function setChannelEnabledModels(ch, list) {
      ch.enabled_models = uniq(list);
      ch.model = ch.enabled_models[0] || '';
      compactModelImageToTextModels(ch);
      compactModelProviderTypes(ch);
      softDisableChannelIfNoModels(ch);
    }
    function softDisableChannelIfNoModels(ch) {
      const models = (ch.enabled_models && ch.enabled_models.length) ? ch.enabled_models : (ch.model ? [ch.model] : []);
      if ((!models || !models.length) && ch.enabled !== false) {
        ch.enabled = false;
        return true;
      }
      return false;
    }
    function isChannelModalOpen() {
      return !!(
        $('channelModal')
        && $('channelModal').classList.contains('show')
        && (EDITING_CHANNEL_IS_NEW || EDITING_CHANNEL_INDEX >= 0)
      );
    }
    function channelFactory(kind = 'image') {
      if (kind === 'audit') return newAuditChannel;
      return newChannel;
    }
    function newChannelId() {
      return 'ch_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 12);
    }
    function newChannel() {
      return normalizeChannel({id:newChannelId(), name:'new-channel', provider_type:'openai', base_url:'https://api.openai.com', api_key:'', model:'', enabled_models:[], enabled:false, models_cache:[]});
    }
    function newAuditChannel() {
      return normalizeChannel({id:newChannelId(), name:'audit-channel', provider_type:'openai', base_url:'https://api.openai.com', api_key:'', model:'', enabled_models:[], enabled:false, models_cache:[]});
    }
    function applyProviderDefaults(ch, force = false, kind = 'image') {
      ch = ch && typeof ch === 'object' ? ch : {};
      const provider = ch.provider_type || 'openai';
      ch.provider_type = provider;
      const def = defaultBaseUrlForProvider(provider);
      const cur = String(ch.base_url || '').trim();
      // Only fill empty base_url, or force restore; never clobber custom non-default URLs unless force.
      if (force || !cur) {
        ch.base_url = def;
      } else {
        // keep custom
      }
      if (provider === 'agnes') {
        if (!Array.isArray(ch.enabled_models) || !ch.enabled_models.length || force) ch.enabled_models = ['agnes-image-2.1-flash'];
        if (!Array.isArray(ch.models_cache) || !ch.models_cache.length || force) ch.models_cache = ['agnes-image-2.1-flash'];
        if (!ch.model) ch.model = (ch.enabled_models && ch.enabled_models[0]) || 'agnes-image-2.1-flash';
      }
      return ch;
    }
    function addChannel() {
      ensureConfig();
      normalizeChannels();
      switchChannelPane('image');
      openChannelModal(-1, 'image', { isNew: true });
    }
    function addAuditChannel() {
      ensureConfig();
      normalizeAuditChannels();
      switchChannelPane('audit');
      openChannelModal(-1, 'audit', { isNew: true });
    }
    function removeChannel(index, kind = 'image') {
      // AstrBot iframe may block native confirmation dialogs; use two-click page button instead.
      const list = channelListFor(kind);
      if (!list || index < 0 || index >= list.length) return;
      const key = `${kind}:${index}`;
      if (PENDING_DELETE.key !== key) {
        clearTimeout(PENDING_DELETE.timer);
        PENDING_DELETE = {
          key,
          timer: setTimeout(() => {
            PENDING_DELETE = { key: '', timer: null };
            renderAllChannelLists();
          }, 4000),
        };
        renderAllChannelLists();
        showToast('再点一次「确认删除」才会删除渠道', 'info');
        return;
      }
      clearTimeout(PENDING_DELETE.timer);
      PENDING_DELETE = { key: '', timer: null };
      list.splice(index, 1);
      renderAllChannelLists();
      refreshModelSelectors();
      scheduleChannelListAutoSave();
      showToast('渠道已删除', 'ok');
    }
    function duplicateChannel(index, kind = 'image') {
      const list = channelListFor(kind);
      const factory = kind === 'audit' ? newAuditChannel : newChannel;
      const copy = JSON.parse(JSON.stringify(list[index] || factory()));
      copy.name = (copy.name || 'channel') + '-copy';
      list.splice(index + 1, 0, normalizeChannel(copy));
      renderAllChannelLists();
      refreshModelSelectors();
      scheduleChannelListAutoSave();
    }
    function renderAllChannelLists() {
      renderChannels();
      renderAuditChannels();
    }
    function renderChannelCards(boxId, list, kind, emptyText) {
      const box = $(boxId);
      if (!box) return;
      box.innerHTML = '';
      if (!list.length) {
        box.innerHTML = `<div class="card soft muted">${emptyText}</div>`;
        return;
      }
      list.forEach((ch, i) => {
        const card = document.createElement('div');
        card.className = 'card';
        const enabledCount = Number((ch.enabled_models || []).length);
        const cacheCount = Number((ch.models_cache || []).length);
        card.innerHTML = `
          <div class="channel-row">
            <div>
              <b>${escapeHtml(ch.name || '未命名')}</b>
              <div class="actions" style="margin-top:6px">
                <span class="pill gray">缓存 ${cacheCount}</span>
                <span class="pill green">启用模型 ${enabledCount}</span>
                ${ch.enabled === false ? '<span class="pill gray">已停用</span>' : '<span class="pill">使用中</span>'}
              </div>
            </div>
            <div class="muted" style="font-size:12px">协议随模型</div>
            <label class="checkline"><input type="checkbox" ${ch.enabled !== false ? 'checked' : ''}>启用</label>
            <div class="actions" style="margin-top:0">
              <button class="secondary" type="button" data-act="edit">编辑</button>
              <button class="secondary" type="button" data-act="dup">复制</button>
              <button class="danger" type="button" data-act="del">${PENDING_DELETE.key === `${kind}:${i}` ? '确认删除' : '删除'}</button>
            </div>
          </div>
        `;
        const enableInput = card.querySelector('input[type="checkbox"]');
        enableInput.onchange = async event => {
          // Rendering selectors normalizes lists in place by replacing the
          // array; resolve the row again so the toggle never edits a stale list.
          const currentList = channelListFor(kind) || [];
          const target = currentList[i];
          if (!target) return;
          const previous = target.enabled !== false;
          const want = !!event.target.checked;
          target.enabled = want;
          if (want && softDisableChannelIfNoModels(target)) {
            event.target.checked = false;
            target.enabled = false;
            setStatus('channelStatus', `${target.name || '渠道'} 还没有启用模型，已保持关闭`);
            return;
          }
          // Only refresh this card status; avoid full list rebuild.
          const pills = card.querySelector('.actions');
          if (pills) {
            const cacheCount = Number((target.models_cache || []).length);
            const enabledCount = Number((target.enabled_models || []).length);
            pills.innerHTML = `
                <span class="pill gray">缓存 ${cacheCount}</span>
                <span class="pill green">启用模型 ${enabledCount}</span>
                ${target.enabled === false ? '<span class="pill gray">已停用</span>' : '<span class="pill">使用中</span>'}
            `;
          }
          refreshModelSelectors();
          const saved = await scheduleChannelListAutoSave();
          if (!saved) {
            const rollbackList = channelListFor(kind) || [];
            if (rollbackList[i]) rollbackList[i].enabled = previous;
            enableInput.checked = previous;
            renderAllChannelLists();
            showToast('渠道开关保存失败，已恢复原状态', 'bad');
          }
        };
        card.querySelector('[data-act="edit"]').onclick = () => openChannelModal(i, kind);
        card.querySelector('[data-act="dup"]').onclick = () => duplicateChannel(i, kind);
        card.querySelector('[data-act="del"]').onclick = () => removeChannel(i, kind);
        box.appendChild(card);
      });
    }
    function renderChannels() {
      normalizeChannels();
      renderChannelCards('channelList', CONFIG.image_channels || [], 'image', '还没有生图渠道。');
    }
    function renderAuditChannels() {
      normalizeAuditChannels();
      renderChannelCards('auditChannelList', CONFIG.audit_channels || [], 'audit', '还没有辅助渠道。');
    }
    function collectChannels() {
      normalizeChannels();
      normalizeAuditChannels();
      for (const ch of [...(CONFIG.image_channels || []), ...(CONFIG.audit_channels || [])]) {
        softDisableChannelIfNoModels(ch);
      }
    }
    function removeAllEnabledModels() {
      const ch = currentModalChannel();
      setChannelEnabledModels(ch, []);
      $('modalEnabled').checked = false;
      ch.enabled = false;
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
      refreshModelSelectors();
    }
    function collectModalChannel() {
      const factory = channelFactory(EDITING_CHANNEL_KIND);
      let source;
      if (EDITING_CHANNEL_IS_NEW) {
        source = normalizeChannel(CHANNEL_DRAFT || factory());
      } else {
        const list = channelListFor(EDITING_CHANNEL_KIND);
        source = normalizeChannel(list[EDITING_CHANNEL_INDEX] || factory());
      }
      const enabled = Array.from(document.querySelectorAll('#enabledModels .model-row .name')).map(el => el.textContent || '');
      const ch = normalizeChannel(Object.assign({}, source, {
        name: $('modalChannelName').value.trim(),
        provider_type: $('modalProvider').value || source.provider_type || 'openai',
        base_url: $('modalBaseUrl').value.trim(),
        api_key: $('modalApiKey').value.trim(),
        proxy_id: ($('modalProxyId') && $('modalProxyId').value) || '',
        proxy: '',
        model: (enabled[0] || source.model || ''),
        enabled: $('modalEnabled').checked,
        enabled_models: enabled,
        image_to_text_models: collectModalImageToTextModels(enabled),
        prompt_en_models: collectModalPromptEnModels(enabled),
        model_provider_types: collectModalProviderTypes(enabled),
        model_download_proxy_ids: collectModalDownloadProxyIds(enabled),
        models_cache: source.models_cache || []
      }));
      // Editing a masked channel without touching the field must not submit an
      // empty credential and accidentally clear the stored key.
      if (!ch.api_key && source.api_key) ch.api_key = source.api_key;
      if (Array.isArray(source.api_keys) && source.api_keys.length && !ch.api_keys) ch.api_keys = source.api_keys.slice();
      softDisableChannelIfNoModels(ch);
      if ($('modalEnabled')) $('modalEnabled').checked = ch.enabled !== false;
      return ch;
    }
    function collectModalDownloadProxyIds(enabled) {
      const enabledSet = new Set(enabled || []);
      const out = {};
      document.querySelectorAll('#enabledModels .model-row').forEach(row => {
        const model = String(row.querySelector('.name')?.textContent || '').trim();
        const pid = String(row.querySelector('.model-download-proxy')?.value || '').trim();
        if (model && enabledSet.has(model) && pid) out[model] = pid;
      });
      return out;
    }
    function collectModalImageToTextModels(enabled) {
      const enabledSet = new Set(enabled || []);
      const out = [];
      document.querySelectorAll('#enabledModels .model-row').forEach(row => {
        const model = String(row.querySelector('.name')?.textContent || '').trim();
        const checkbox = row.querySelector('.model-image-to-text');
        if (model && enabledSet.has(model) && checkbox?.checked) out.push(model);
      });
      return out;
    }
    function collectModalPromptEnModels(enabled) {
      const enabledSet = new Set(enabled || []);
      const out = [];
      document.querySelectorAll('#enabledModels .model-row').forEach(row => {
        const model = String(row.querySelector('.name')?.textContent || '').trim();
        const checkbox = row.querySelector('.model-prompt-en');
        if (model && enabledSet.has(model) && checkbox?.checked) out.push(model);
      });
      return out;
    }
    function collectModalProviderTypes(enabled) {
      const enabledSet = new Set(enabled || []);
      const out = {};
      document.querySelectorAll('#enabledModels .model-row').forEach(row => {
        const model = String(row.querySelector('.name')?.textContent || '').trim();
        const raw = row.querySelector('.model-provider')?.value || '';
        const provider = normalizeProviderType(raw);
        if (model && enabledSet.has(model) && provider) out[model] = provider;
      });
      return out;
    }
    function storeModalChannel(ch) {
      if (EDITING_CHANNEL_IS_NEW) {
        CHANNEL_DRAFT = ch;
        return;
      }
      if (EDITING_CHANNEL_INDEX >= 0) {
        channelListFor(EDITING_CHANNEL_KIND)[EDITING_CHANNEL_INDEX] = ch;
      }
    }
    function openChannelModal(index, kind = 'image', options = {}) {
      normalizeChannels();
      normalizeAuditChannels();
      const isNew = !!options.isNew || index < 0;
      EDITING_CHANNEL_IS_NEW = isNew;
      EDITING_CHANNEL_KIND = kind === 'audit' ? 'audit' : 'image';
      EDITING_CHANNEL_INDEX = isNew ? -1 : index;
      CHANNEL_MODAL_DIRTY = false;
      const factory = channelFactory(EDITING_CHANNEL_KIND);
      let ch;
      if (isNew) {
        ch = normalizeChannel(factory());
        applyProviderDefaults(ch, false, EDITING_CHANNEL_KIND);
        CHANNEL_DRAFT = ch;
      } else {
        const list = channelListFor(EDITING_CHANNEL_KIND);
        ch = normalizeChannel(list[index] || factory());
        applyProviderDefaults(ch, false, EDITING_CHANNEL_KIND);
        list[index] = ch;
        CHANNEL_DRAFT = null;
      }
      const titles = {
        image: isNew ? '新建生图渠道' : '编辑生图渠道',
        audit: isNew ? '新建辅助渠道' : '编辑辅助渠道',
      };
      $('channelModalTitle').textContent = titles[EDITING_CHANNEL_KIND] || (isNew ? '新建渠道' : '编辑渠道');
      const providerChoices = EDITING_CHANNEL_KIND === 'audit' ? AUDIT_PROVIDERS : PROVIDERS;
      const defaultProvider = ch.provider_type || 'openai';
      setSelectOptions('modalProvider', providerChoices, defaultProvider);
      const sel = $('modalProvider');
      if (EDITING_CHANNEL_KIND === 'image') {
        Array.from(sel.options || []).forEach(opt => {
          opt.textContent = IMAGE_PROVIDER_LABELS[opt.value] || opt.value;
        });
      }
      // 渠道可设置默认类型，模型旁仍可覆盖为自动或手动类型。
      if ($('modalProviderWrap')) {
        $('modalProviderWrap').style.display = '';
      }
      $('modalChannelName').value = ch.name || '';
      $('modalProvider').value = defaultProvider;
      $('modalProvider').dataset.prevProvider = defaultProvider;
      $('modalBaseUrl').value = ch.base_url || '';
      $('modalApiKey').value = ch.api_key || '';
      fillChannelProxyOptions(ch.proxy_id || '');
      $('modalEnabled').checked = ch.enabled !== false;
      $('cacheSearch').value = '';
      $('manualModel').value = '';
      $('modalStatus').textContent = isNew ? '填写后点「保存渠道」才会加入列表；关闭则不添加。' : '';
      if ($('modalProviderHint')) {
        $('modalProviderHint').textContent = EDITING_CHANNEL_KIND === 'image'
          ? '选择渠道默认类型；模型旁可改协议，下载代理仅影响结果图拉取。'
          : '渠道默认协议会按模型名自动识别；也可在下方已启用模型旁手动切换协议。';
      }
      renderModalModels(ch);
      $('channelModal').classList.add('show');
    }
    function modalProviderChanged() {
      const ch = currentModalChannel();
      const previousModels = (ch.enabled_models || []).join('\u0000');
      const prev = String(($('modalProvider').dataset.prevProvider || ch.provider_type || '')).trim();
      const next = $('modalProvider').value || 'openai';
      const curBase = String($('modalBaseUrl').value || ch.base_url || '').trim();
      const prevDefault = defaultBaseUrlForProvider(prev);
      const nextDefault = defaultBaseUrlForProvider(next);
      ch.provider_type = next;
      // Only auto-switch base_url when empty or still on previous type default (don't clobber custom).
      if (!curBase || normalizeBaseUrlCompare(curBase) === normalizeBaseUrlCompare(prevDefault)) {
        ch.base_url = nextDefault;
        $('modalBaseUrl').value = nextDefault;
      } else {
        ch.base_url = curBase;
      }
      applyProviderDefaults(ch, false, EDITING_CHANNEL_KIND);
      $('modalProvider').dataset.prevProvider = next;
      CHANNEL_MODAL_DIRTY = true;
      storeModalChannel(ch);
      renderModalModels(ch);
      // Provider changes normally do not change the priority candidates. Agnes
      // is the exception because it supplies a default model for an empty row.
      if ((ch.enabled_models || []).join('\u0000') !== previousModels) refreshModelSelectors();
    }
    function restoreModalBaseUrlDefault() {
      const provider = $('modalProvider').value || 'openai';
      const def = defaultBaseUrlForProvider(provider);
      $('modalBaseUrl').value = def;
      const ch = currentModalChannel();
      ch.base_url = def;
      CHANNEL_MODAL_DIRTY = true;
      storeModalChannel(ch);
      if ($('modalStatus')) $('modalStatus').textContent = '已恢复为当前类型默认 Base URL';
    }
    function closeChannelModal() {
      $('channelModal').classList.remove('show');
      EDITING_CHANNEL_INDEX = -1;
      EDITING_CHANNEL_KIND = 'image';
      EDITING_CHANNEL_IS_NEW = false;
      CHANNEL_DRAFT = null;
      CHANNEL_MODAL_DIRTY = false;
    }
    function renderModalModels(ch) {
      const factory = channelFactory(EDITING_CHANNEL_KIND);
      if (!ch) {
        if (EDITING_CHANNEL_IS_NEW) ch = CHANNEL_DRAFT || factory();
        else ch = channelListFor(EDITING_CHANNEL_KIND)[EDITING_CHANNEL_INDEX] || factory();
      }
      ch = normalizeChannel(ch);
      const enabled = ch.enabled_models || [];
      const search = $('cacheSearch').value.trim().toLowerCase();
      const cacheItems = (ch.models_cache || []).filter(item => !search || item.toLowerCase().includes(search));
      $('cacheCount').textContent = String((ch.models_cache || []).length);
      $('enabledCount').textContent = String(enabled.length);

      const cacheModelsEl = $('cacheModels');
      if ((ch.models_cache || []).length > 10) {
        cacheModelsEl.classList.add('collapsed');
      } else {
        cacheModelsEl.classList.remove('collapsed');
      }

      cacheModelsEl.innerHTML = cacheItems.map(item => {
        const active = enabled.includes(item);
        return `<div class="model-row" data-model="${escapeHtml(item)}"><div class="name">${escapeHtml(item)}</div><div class="actions"><button class="secondary mini" type="button" onclick="${active ? `removeEnabledModel('${escapeJs(item)}')` : `addEnabledModel('${escapeJs(item)}')`}">${active ? '取消' : '启用'}</button></div></div>`;
      }).join('') || '<div class="muted">没有匹配的缓存模型。</div>';
      $('enabledModels').innerHTML = enabled.map((item, i) => `
        <div class="model-row with-provider with-provider-download">
          <div class="model-name-stack">
            <div class="name" title="${escapeHtml(item)}">${escapeHtml(item)}</div>
            ${EDITING_CHANNEL_KIND === 'image' ? `<label class="model-feature"><input class="model-image-to-text" type="checkbox" ${((ch.image_to_text_models || []).includes(item)) ? 'checked' : ''} onchange="setModelImageToText(${i}, this.checked)">图转文</label><label class="model-feature"><input class="model-prompt-en" type="checkbox" ${((ch.prompt_en_models || []).includes(item)) ? 'checked' : ''} onchange="setModelPromptEn(${i}, this.checked)">词转英</label>` : ''}
          </div>
          <div class="model-controls">
            ${modelProviderSelectHtml(ch, item, i)}
            ${modelDownloadProxySelectHtml(ch, item, i)}
          </div>
          <div class="actions">
            <button class="secondary mini" type="button" onclick="moveEnabledModel(${i}, -1)">上移</button>
            <button class="secondary mini" type="button" onclick="moveEnabledModel(${i}, 1)">下移</button>
            <button class="danger mini" type="button" onclick="removeEnabledModel('${escapeJs(item)}')">移除</button>
          </div>
        </div>`).join('') || '<div class="muted">还没有启用模型。</div>';
    }
    function setModelImageToText(index, enabled) {
      const ch = currentModalChannel();
      const model = (ch.enabled_models || [])[index] || '';
      if (!model) return;
      ch.image_to_text_models ||= [];
      if (enabled) {
        if (!ch.image_to_text_models.includes(model)) ch.image_to_text_models.push(model);
      } else {
        ch.image_to_text_models = ch.image_to_text_models.filter(item => item !== model);
      }
      compactModelImageToTextModels(ch);
      storeModalChannel(ch);
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
    }
    function setModelPromptEn(index, enabled) {
      const ch = currentModalChannel();
      const model = (ch.enabled_models || [])[index] || '';
      if (!model) return;
      ch.prompt_en_models ||= [];
      if (enabled) {
        if (!ch.prompt_en_models.includes(model)) ch.prompt_en_models.push(model);
      } else {
        ch.prompt_en_models = ch.prompt_en_models.filter(item => item !== model);
      }
      compactModelPromptEnModels(ch);
      storeModalChannel(ch);
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
    }
    function modelDownloadProxySelectHtml(ch, model, index) {
      const selected = String(((ch.model_download_proxy_ids || {})[model]) || '').trim();
      const opts = ['<option value="">下载代理：跟随渠道</option>'];
      for (const p of enabledProxyOptions()) {
        const label = `${p.name || p.id} · ${(p.protocol || 'http').toUpperCase()}`;
        opts.push(`<option value="${escapeHtml(p.id)}" ${selected === p.id ? 'selected' : ''}>${escapeHtml(label)}</option>`);
      }
      if (selected && !opts.some(x => x.includes(`value="${escapeHtml(selected)}"`))) {
        opts.push(`<option value="${escapeHtml(selected)}" selected>已选（未启用/已删）</option>`);
      }
      return `<select class="model-download-proxy" title="仅用于模型返回结果的下载；请求仍走渠道代理" onchange="setModelDownloadProxy(${index}, this.value)">${opts.join('')}</select>`;
    }
    function setModelDownloadProxy(index, proxyId) {
      const ch = currentModalChannel();
      const model = (ch.enabled_models || [])[index] || '';
      if (!model) return;
      ch.model_download_proxy_ids ||= {};
      const pid = String(proxyId || '').trim();
      if (pid) ch.model_download_proxy_ids[model] = pid;
      else delete ch.model_download_proxy_ids[model];
      compactModelDownloadProxyIds(ch);
      storeModalChannel(ch);
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
    }
    function modelProviderSelectHtml(ch, model, index) {
      const manual = normalizeProviderType((ch.model_provider_types || {})[model] || '');
      const choices = EDITING_CHANNEL_KIND === 'audit' ? AUDIT_PROVIDERS.slice() : PROVIDERS.slice();
      if (manual && !choices.includes(manual)) choices.unshift(manual);
      const auto = resolveModelProviderType(model, ch.provider_type, '');
      const autoLabel = IMAGE_PROVIDER_LABELS[auto] || auto;
      const options = [`<option value="" ${manual ? '' : 'selected'}>自动：${escapeHtml(autoLabel)}</option>`]
        .concat(choices.map(provider => {
          const label = IMAGE_PROVIDER_LABELS[provider] || provider;
          return `<option value="${escapeHtml(provider)}" ${manual === provider ? 'selected' : ''}>${escapeHtml(label)}</option>`;
        }));
      return `<select class="model-provider" title="模型类型：自动按模型名识别，也可手动指定" onchange="setModelProviderType(${index}, this.value)">${options.join('')}</select>`;
    }
    function currentModalChannel() {
      const ch = collectModalChannel();
      storeModalChannel(ch);
      return ch;
    }
    function setModelProviderType(index, provider) {
      const ch = currentModalChannel();
      const model = (ch.enabled_models || [])[index] || '';
      if (!model) return;
      ch.model_provider_types ||= {};
      const resolved = normalizeProviderType(provider);
      if (resolved) ch.model_provider_types[model] = resolved;
      else delete ch.model_provider_types[model];
      compactModelProviderTypes(ch);
      storeModalChannel(ch);
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
    }
    function addEnabledModel(name) {
      const ch = currentModalChannel();
      setChannelEnabledModels(ch, (ch.enabled_models || []).concat([name]));
      storeModalChannel(ch);
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
      refreshModelSelectors();
    }
    function removeEnabledModel(name) {
      const ch = currentModalChannel();
      setChannelEnabledModels(ch, (ch.enabled_models || []).filter(item => item !== name));
      if ($('modalEnabled')) $('modalEnabled').checked = ch.enabled !== false;
      storeModalChannel(ch);
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
      refreshModelSelectors();
    }
    function moveEnabledModel(index, delta) {
      const ch = currentModalChannel();
      const next = index + delta;
      if (next < 0 || next >= ch.enabled_models.length) return;
      const list = ch.enabled_models.slice();
      const item = list.splice(index, 1)[0];
      list.splice(next, 0, item);
      setChannelEnabledModels(ch, list);
      storeModalChannel(ch);
      CHANNEL_MODAL_DIRTY = true;
      renderModalModels(ch);
      refreshModelSelectors();
    }
    async function refreshChannelModels(index = EDITING_CHANNEL_INDEX) {
      const ch = (EDITING_CHANNEL_IS_NEW || index === EDITING_CHANNEL_INDEX)
        ? currentModalChannel()
        : normalizeChannel(channelListFor(EDITING_CHANNEL_KIND)[index]);
      $('modalStatus').textContent = `正在刷新 ${ch.name || '渠道'} 模型...`;
      try {
        const refreshPayload = Object.assign({}, ch, {media_type: EDITING_CHANNEL_KIND});
        const res = await api('/api/refresh-image-models', {method:'POST', body: JSON.stringify({channel: refreshPayload})});
        ch.models_cache = res.data || [];
        if (EDITING_CHANNEL_IS_NEW || index === EDITING_CHANNEL_INDEX) {
          storeModalChannel(ch);
          CHANNEL_MODAL_DIRTY = true;
          renderModalModels(ch);
        } else {
          channelListFor(EDITING_CHANNEL_KIND)[index] = ch;
          renderAllChannelLists();
        }
        refreshModelSelectors();
        $('modalStatus').textContent = `刷新成功：${ch.models_cache.length} 个模型（点「保存渠道」后生效）`;
      } catch (e) { $('modalStatus').textContent = e.message; }
    }
    async function saveChannelModal() {
      const ch = currentModalChannel();
      if (!ch.name) {
        $('modalStatus').textContent = '渠道名不能为空';
        return;
      }
      if (!ch.model && ch.enabled_models.length) ch.model = ch.enabled_models[0];
      // New channel with enabled models: turn channel on.
      if (EDITING_CHANNEL_IS_NEW && (ch.enabled_models || []).length && ch.enabled === false) {
        ch.enabled = true;
        if ($('modalEnabled')) $('modalEnabled').checked = true;
      }
      softDisableChannelIfNoModels(ch);
      const list = channelListFor(EDITING_CHANNEL_KIND);
      if (EDITING_CHANNEL_IS_NEW) {
        list.push(ch);
      } else if (EDITING_CHANNEL_INDEX >= 0) {
        list[EDITING_CHANNEL_INDEX] = ch;
      } else {
        $('modalStatus').textContent = '没有可保存的渠道';
        return;
      }
      renderAllChannelLists();
      refreshModelSelectors();
      $('modalStatus').textContent = '保存中...';
      const ok = await persistConfig(false, EDITING_CHANNEL_IS_NEW ? '渠道已添加' : '渠道已保存', { toast: true });
      $('modalStatus').textContent = ok
        ? (ch.enabled === false && !(ch.enabled_models || []).length
          ? (EDITING_CHANNEL_IS_NEW ? '已添加（无启用模型，渠道已自动关闭）' : '已保存（无启用模型，渠道已自动关闭）')
          : (EDITING_CHANNEL_IS_NEW ? '已添加' : '已保存'))
        : '保存失败，请检查上方提示';
      if (ok) {
        CHANNEL_MODAL_DIRTY = false;
        closeChannelModal();
      } else if (EDITING_CHANNEL_IS_NEW) {
        // 保存失败时撤回本次添加
        const pos = list.lastIndexOf(ch);
        if (pos >= 0) list.splice(pos, 1);
        CHANNEL_DRAFT = ch;
        renderAllChannelLists();
        refreshModelSelectors();
      }
    }

    function modelLabelsFromChannels(channels) {
      const labels = [];
      for (const ch of channels || []) {
        if (ch.enabled === false) continue;
        for (const model of (ch.enabled_models?.length ? ch.enabled_models : [ch.model]).filter(Boolean)) labels.push(`${ch.name}/${model}`);
      }
      return uniq(labels);
    }
    function allModelLabels(channels = null) {
      if (!channels) {
        collectChannels();
        channels = CONFIG.image_channels;
      }
      return modelLabelsFromChannels(channels);
    }
    function activeImageModelKeys() {
      collectChannels();
      const keys = [];
      for (const ch of CONFIG.image_channels || []) {
        if (ch.enabled === false || !ch.name) continue;
        for (const model of (ch.enabled_models?.length ? ch.enabled_models : [ch.model]).filter(Boolean)) {
          keys.push(`${ch.name}/${model}`, `${ch.name}:${model}`, model);
        }
      }
      return uniq(keys);
    }
    function auditModelLabels(channels = null) {
      if (!channels) {
        collectChannels();
        channels = CONFIG.audit_channels;
      }
      return modelLabelsFromChannels(channels);
    }
    function refreshModelSelectors() {
      // Normalize once. The previous implementation recalculated every list
      // through allModelLabels/auditModelLabels while rebuilding each picker.
      collectChannels();
      const labels = allModelLabels(CONFIG.image_channels);
      const audits = auditModelLabels(CONFIG.audit_channels);
      const preserveSelection = (id, values, fallback = '') => {
        const el = $(id);
        const previous = String(el?.value || '');
        setSelectOptions(id, values, values.includes(previous) ? previous : fallback);
      };
      prunePriorityList('image', labels);
      prunePriorityList('audit', audits);
      preserveSelection('priorityPicker', labels, labels[0] || '');
      preserveSelection('auditPriorityPicker', audits, audits[0] || '');
      const auditLabels = [''].concat(audits);
      preserveSelection('promptAuditModel', auditLabels, CONFIG.image?.prompt_audit_model || '');
      preserveSelection('outputAuditModel', auditLabels, CONFIG.image?.output_audit_model || '');
      preserveSelection('ocrModel', auditLabels, CONFIG.image?.ocr_model || '');
      preserveSelection('promptEnModel', auditLabels, CONFIG.image?.prompt_en_model || '');
      const testChannels = (CONFIG.image_channels || []).filter(c => c.enabled !== false && c.name);
      const currentTestChannel = $('testChannel').value;
      const selectedTestChannel = testChannels.some(c => c.name === currentTestChannel) ? currentTestChannel : (testChannels[0]?.name || '');
      preserveSelection('testChannel', testChannels.map(c => c.name), selectedTestChannel);
      refreshTestModels();
    }
    function refreshTestModels() {
      const name = $('testChannel').value;
      const ch = (CONFIG.image_channels || []).find(c => c.enabled !== false && c.name === name) || {};
      const models = (ch.enabled_models?.length ? ch.enabled_models : [ch.model]).filter(Boolean);
      setSelectOptions('testModel', models, models.includes(ch.model) ? ch.model : (models[0] || ''));
    }
    const PRIORITY_UI = {
      image: {picker:'priorityPicker', list:'priorityList', rows:'priorityRows'},
      audit: {picker:'auditPriorityPicker', list:'auditPriorityList', rows:'auditPriorityRows'},
    };
    function priorityUi(kind = 'image') { return PRIORITY_UI[kind] || PRIORITY_UI.image; }
    function priorityLabels(kind) {
      return kind === 'audit' ? auditModelLabels() : allModelLabels();
    }
    function priorityEntryMatchesLabel(entry, label) {
      const item = String(entry || '').trim();
      const value = String(label || '').trim();
      if (!item || !value) return false;
      const [channel, ...rest] = value.split('/');
      const model = rest.join('/');
      return item === value || item === `${channel}:${model}` || item === model;
    }
    function refreshPriorityPicker(kind = 'image') {
      return refreshPriorityPickerWithLabels(kind, null);
    }
    function refreshPriorityPickerWithLabels(kind = 'image', labels = null) {
      const ui = priorityUi(kind), picker = $(ui.picker);
      if (!picker) return;
      const selected = textList(ui.list);
      const available = uniq(labels || priorityLabels(kind)).filter(label =>
        !selected.some(item => priorityEntryMatchesLabel(item, label))
      );
      setSelectOptions(ui.picker, available, available[0] || '');
      if (!available.length) {
        const empty = document.createElement('option');
        empty.value = '';
        empty.textContent = '暂无可加入模型';
        empty.disabled = true;
        empty.selected = true;
        picker.appendChild(empty);
      }
    }
    function addPriority(kind = 'image') {
      const ui = priorityUi(kind), value = $(ui.picker).value;
      if (!value) return;
      const current = textList(ui.list);
      if (!current.includes(value)) current.push(value);
      $(ui.list).value = current.join('\n');
      renderPriorityRows(kind);
      refreshPriorityPicker(kind);
      scheduleChannelListAutoSave();
    }
    function clearPriority(kind = 'image') {
      const ui = priorityUi(kind);
      $(ui.list).value = '';
      renderPriorityRows(kind);
      refreshPriorityPicker(kind);
      scheduleChannelListAutoSave();
    }
    function setPriorityItems(kind, items) {
      const ui = priorityUi(kind);
      $(ui.list).value = uniq(items).join('\n');
      renderPriorityRows(kind);
      refreshPriorityPicker(kind);
      scheduleChannelListAutoSave();
    }
    function prunePriorityList(kind = 'image', labels = null) {
      const ui = priorityUi(kind), source = labels || priorityLabels(kind), allowed = new Set(source.flatMap(label => {
        const [channel, ...rest] = label.split('/'), model = rest.join('/');
        return [label, `${channel}:${model}`, model];
      }));
      const current = textList(ui.list), next = current.filter(item => allowed.has(item));
      if (next.length !== current.length || next.some((item, i) => item !== current[i])) {
        $(ui.list).value = next.join('\n');
        renderPriorityRows(kind);
      }
      refreshPriorityPickerWithLabels(kind, source);
      return next;
    }
    function movePriority(kind, index, delta) {
      const ui = priorityUi(kind), items = textList(ui.list), next = index + delta;
      if (next < 0 || next >= items.length) return;
      const item = items.splice(index, 1)[0];
      items.splice(next, 0, item);
      setPriorityItems(kind, items);
    }
    function movePriorityToTop(kind, index) {
      const ui = priorityUi(kind), items = textList(ui.list);
      if (index <= 0 || index >= items.length) return;
      const item = items.splice(index, 1)[0];
      items.unshift(item);
      setPriorityItems(kind, items);
    }
    function removePriority(kind, index) {
      const ui = priorityUi(kind), items = textList(ui.list);
      items.splice(index, 1);
      setPriorityItems(kind, items);
    }
    function onImageModelCallModeChange() {
      CONFIG.image_model_call_mode = ($('imageModelCallMode') && $('imageModelCallMode').value) || 'sequence';
      renderPriorityRows('image');
      scheduleChannelListAutoSave();
    }
    function renderPriorityRows(kind = 'image') {
      const ui = priorityUi(kind), items = textList(ui.list), box = $(ui.rows);
      if (!box) return;
      box.innerHTML = items.map((item, i) => `
        <div class="model-row"><div class="name">${escapeHtml(item)}</div><div class="actions">
          <button class="secondary mini" type="button" onclick="movePriority('${kind}', ${i}, -1)" ${i === 0 ? 'disabled' : ''}>上移</button>
          <button class="secondary mini" type="button" onclick="movePriorityToTop('${kind}', ${i})" ${i === 0 ? 'disabled' : ''}>置顶</button>
          <button class="secondary mini" type="button" onclick="movePriority('${kind}', ${i}, 1)">下移</button>
          <button class="danger mini" type="button" onclick="removePriority('${kind}', ${i})">移除</button>
        </div></div>`).join('') || `<div class="muted">未设置${kind === 'image' ? '生图' : '辅助'}优先级时按启用顺序尝试。</div>`;
    }
    function renderAllPriorityRows() {
      for (const kind of ['image','audit']) {
        prunePriorityList(kind);
        renderPriorityRows(kind);
      }
    }

    let QQ_GROUP_ROWS = [];
    let QQ_GROUP_MODELS = [];
    let QQ_BOT_IDS = [];
    let QQ_GROUP_MODAL = null;
    const QQ_GROUP_CHAT_TYPES = [['none', '无', '不额外限制群聊'], ['black', '黑', '禁止此群使用生图'], ['white', '白', '允许此群使用生图']];
    const QQ_GROUP_MODEL_MODES = [['default', '默认', '跟随全局默认调用模式'], ['sequence', '顺序', '按全局模型优先级顺序尝试'], ['random', '随机', '按全局模型优先级随机选择'], ['fixed', '固定', '固定使用指定模型']];
