    const $ = id => document.getElementById(id);
    const ASPECTS = ['自动','1:1','2:3','3:2','3:4','4:3','4:3','4:5','5:4','9:16','16:9','21:9'].filter((v,i,a)=>a.indexOf(v)===i);
    const PROVIDERS = ['openai','openai_chat','gemini','gemini_openai','z_image_gitee','jimeng2api','grok','agnes','novelai','hunyuan'];
    const IMAGE_PROVIDER_LABELS = {
      openai: 'openai',
      openai_chat: 'openai_chat',
      gemini: 'gemini',
      gemini_openai: 'gemini_openai',
      z_image_gitee: 'z_image_gitee',
      jimeng2api: 'jimeng2api',
      grok: 'grok',
      agnes: 'agnes',
      novelai: 'NovelAI',
      hunyuan: 'Hunyuan / 混元'
    };
    const PROVIDER_DEFAULT_BASE_URLS = {
      openai: 'https://api.openai.com',
      openai_chat: 'https://api.openai.com',
      gemini: 'https://generativelanguage.googleapis.com',
      gemini_openai: 'https://generativelanguage.googleapis.com',
      z_image_gitee: 'https://ai.gitee.com',
      jimeng2api: 'http://localhost:5100',
      grok: 'https://api.x.ai',
      agnes: 'https://apihub.agnes-ai.com',
      novelai: 'https://image.novelai.net',
      hunyuan: 'https://your-company.copilot.qq.com'
    };
    function defaultBaseUrlForProvider(provider, kind = 'image') {
      const p = String(provider || '').trim();
      return PROVIDER_DEFAULT_BASE_URLS[p] || PROVIDER_DEFAULT_BASE_URLS.openai || 'https://api.openai.com';
    }
    function normalizeBaseUrlCompare(url) {
      return String(url || '').trim().replace(/\/+$/, '').toLowerCase();
    }
    const AUDIT_PROVIDERS = ['openai','gemini','gemini_openai'];
    const MONITOR_PAGE_SIZE = 20;
    const ASSET_PAGE_SIZE = 24;
    // AstrBot plugin iframe is sandboxed without allow-same-origin.
    // Bare storage access throws and aborts the whole script before login bindings.
    function safeStorageGet(key, fallback = '') {
      try { return window.localStorage?.getItem(key) ?? fallback; } catch (_) { return fallback; }
    }
    function safeStorageSet(key, value) {
      try { window.localStorage?.setItem(key, value); } catch (_) {}
    }
    function safeStorageRemove(key) {
      try { window.localStorage?.removeItem(key); } catch (_) {}
    }
    const CANVAS_SESSION_CACHE_TTL_MS = 15000;
    // Prompt/COS/tag catalogs refresh periodically; the shared gallery cache has no TTL.
    const PICKER_CATALOG_CACHE_TTL_MS = 30000;
    const PICKER_PAGE_SIZE = 30;
    const PAGE_PREFETCH_AHEAD = 3;
    const PICKER_PAGE_CACHE = new Map();
    const MANAGED_PAGE_CACHE = new Map();
    let PICKER_CATALOG_CACHE_VERSION = 0;
    const PICKER_PANEL_STATES = new Map();
    const PICKER_SEARCH_TIMERS = new Map();
    const PICKER_LOADING = new Set();
    // Redacted client-side request trace for page diagnostics.
    const REQUEST_TRACE = [];
    const REQUEST_TRACE_LIMIT = 80;
    function traceRequest(entry) {
      const item = Object.assign({startedAt: Date.now()}, entry || {});
      delete item.token; delete item.cookie; delete item.prompt; delete item.body;
      REQUEST_TRACE.push(item);
      if (REQUEST_TRACE.length > REQUEST_TRACE_LIMIT) REQUEST_TRACE.splice(0, REQUEST_TRACE.length - REQUEST_TRACE_LIMIT);
      return item;
    }
    function requestTraceSnapshot() { return REQUEST_TRACE.map(item => Object.assign({}, item)); }
    let CONFIG = {};
    let SELFIE_REFERENCE_DATA_URL = '';
    let SELFIE_REFERENCE_REQUEST = null;
    let STUDIO = { sessions: [], current: null, canvas: null, selectedNodeId: "", connectNodeId: "", prompts: [], templates: [], promptPresets: [], promptPresetsByKind: {image: []}, cosLooks: [], cosPools: null, pollTimer: null, pollFailures: 0, taskId: "", taskNodeId: "", taskProgress: null, uploadSlotId: "", presetOpen: false, cosOpen: false, nodePresetOpen: false, nodeCosOpen: false, running: false, galleryTargetSlotId: "", nodeSearch: "", viewport: {x: 120, y: 80, zoom: 1}, drag: null, sessionsLoadedAt: 0, sessionsRequest: null, loadSeq: 0, viewportFittedForSession: false };
    let CREATIVE_CANVAS = { sessions: [], current: null, templates: [], prompts: [], promptPresets: [], cosLooks: [], taskId: "", running: false, pollTimer: null, uploadSlotId: "", galleryTargetSlotId: "", galleryRequestSeq: 0, presetOpen: false, cosOpen: false, sessionsLoadedAt: 0, sessionsRequest: null, loadSeq: 0 };
    const PICKER_CACHE = {
      tags: {data: [], loadedAt: 0, request: null, version: 0},
    };

    let RECORDS = [];
    let RECORD_META = {total: 0, filtered: 0, offset: 0, limit: MONITOR_PAGE_SIZE};
    let MONITOR_PAGE = 1;
    let ASSET_RECORDS = [];
    let ASSET_META = {total: 0, filtered: 0, offset: 0, limit: ASSET_PAGE_SIZE};
    let ASSET_PAGE = 1;
    const ASSET_SELECTION = new Set();
    const TASK_SELECTION = new Set();
    const TASK_PAGE_SIZE = 20;
    const RECORD_SELECTION = new Set();
    let ASSET_LOAD_TIMER = null;
    let RECORD_LOAD_SEQ = 0;
    let ASSET_LOAD_SEQ = 0;
    const METRICS_REQUESTS = new Map();
    let METRICS_CONTENTS = {};
    let ACTIVE_METRICS_CATEGORY = '';
    const TASK_REQUESTS = new Map();
    let RECORD_LOADING = false;
    let ASSET_LOADING = false;
    const PAGE_CACHE_MAX_ENTRIES = 12;
    const PAGE_CACHE_TTL_MS = 30000;
    const RECORD_PAGE_CACHE = new Map();
    const ASSET_PAGE_CACHE = new Map();
    const RECORD_PAGE_REQUESTS = new Map();
    const ASSET_PAGE_REQUESTS = new Map();
    let RECORD_PAGE_CACHE_VERSION = 0;
    let ASSET_PAGE_CACHE_VERSION = 0;
    let ASSET_STUDIO_PRESETS = [];
    let ASSET_STUDIO_PRESET_PAGE = 1;
    let ASSET_STUDIO_PRESET_SEARCH_TIMER = null;
    let AUTH_TOKEN = safeStorageGet('selfieImageToken') || safeStorageGet('aicatToken') || '';
    let IS_FILLING = false;
    let AUTO_SAVE_TIMER = null;
    let MONITOR_LOAD_TIMER = null;
    let TASK_LOAD_TIMER = null;
    let TASK_FILTER_TIMER = null;
    let TASK_PAGE = 1;
    let ACTIVE_CHANNEL_PANE = 'image';
    let EDITING_CHANNEL_INDEX = -1;
    let EDITING_CHANNEL_KIND = 'image';
    let EDITING_CHANNEL_IS_NEW = false;
    let PENDING_DELETE = { key: '', timer: null };
    let CHANNEL_DRAFT = null;
    let CHANNEL_MODAL_DIRTY = false;
    let SUPPRESS_AUTOSAVE = false;
    let CURRENT_RECORD = null;
    const RECORD_MEDIA_SOURCE_CACHE = new Map();
    const RECORD_MEDIA_SOURCE_REQUESTS = new Map();
    // Keep authenticated media in memory and in a short-lived browser cache so the
    // asset grid, detail modal and download action share one response across refreshes.
    const PROTECTED_MEDIA_CACHE = new Map();
    const PROTECTED_MEDIA_REQUESTS = new Map();
    let PROTECTED_MEDIA_OBSERVER = null;
    const PROTECTED_MEDIA_LOAD_QUEUE = [];
    const PROTECTED_MEDIA_LOAD_CONCURRENCY = 2;
    let PROTECTED_MEDIA_LOAD_ACTIVE = 0;
    const PROTECTED_MEDIA_CACHE_MAX_ENTRIES = 100;
    const PROTECTED_MEDIA_CACHE_MAX_BYTES = 200 * 1024 * 1024;
    const PROTECTED_MEDIA_PERSIST_CACHE_NAME = 'selfie-image-protected-v1';
    const PROTECTED_MEDIA_PERSIST_META_KEY = 'selfieImageProtectedMediaMeta:v1';
    const PROTECTED_MEDIA_PERSIST_TTL_MS = 3 * 24 * 60 * 60 * 1000;
    const PROTECTED_MEDIA_PERSIST_MAX_ENTRIES = 150;
    let PROTECTED_MEDIA_CACHE_BYTES = 0;
    let PROTECTED_MEDIA_CACHE_SCOPE = '';
    let PROTECTED_MEDIA_CACHE_VERSION = 0;
    let PROTECTED_MEDIA_LAST_PURGE = 0;
    const GALLERY_PICKER_CACHE_MAX_ENTRIES = 100;
    const GALLERY_PICKER_CACHE = {items: [], loaded: false, request: null, version: 0};
    let TEST_TASK_POLL_TIMER = null;
    let TEST_TASK_ID = '';
    let TEST_MODE = 'image';
    let DASHBOARD_BRIDGE = null;
    let PRESET_MANAGE_ROWS = [];
    let COS_MANAGE_ROWS = [];
    let PRESET_EDIT_ORIGINAL = '';
    let PRESET_EDIT_KIND = 'image';
    let PRESET_TRANSFER_KIND = 'image';
    let PRESET_KIND = 'image';
    let PRESET_LOAD_SEQ = 0;
    const PRESET_MANAGER_PAGES = {image: 1, cos: 1, nai: 1};
    const PRESET_MANAGER_TOTALS = {image: 0, cos: 0, nai: 0};
    const PRESET_MANAGER_SEARCH_TIMERS = {image: null, cos: null, nai: null};
    const PRESET_MANAGER_QUERIES = {image: '', cos: '', nai: ''};
    let CURRENT_COMPARE = null;

    function isEmbeddedFrame() {
      try {
        return window.parent && window.parent !== window;
      } catch (_) {
        return true;
      }
    }
    function isDashboardPage() {
      // Prefer live bridge when available.
      return Boolean(window.AstrBotPluginPage || DASHBOARD_BRIDGE)
        || isEmbeddedFrame()
        || String(window.location.pathname || '').startsWith('/api/plugin/page/content/');
    }
    function dashboardBridge() {
      return window.AstrBotPluginPage || DASHBOARD_BRIDGE || null;
    }
    async function waitForDashboardBridge(timeout = 12000) {
      const existing = dashboardBridge();
      if (existing) return existing;
      const started = Date.now();
      while (Date.now() - started < timeout) {
        if (window.AstrBotPluginPage) {
          DASHBOARD_BRIDGE = window.AstrBotPluginPage;
          return DASHBOARD_BRIDGE;
        }
        await new Promise(resolve => setTimeout(resolve, 25));
      }
      throw new Error('管理页连接未就绪');
    }
    function bridgeEndpoint(path) {
      // Match telegram forwarder api.js:
      // "/api/config" -> "config"
      // parent then calls /api/v1/plugins/extensions/<plugin>/config
      const value = String(path || '').trim();
      const noQuery = value.split('?', 1)[0];
      if (noQuery.startsWith('/api/')) return noQuery.slice('/api/'.length);
      return noQuery.replace(/^\/+/, '');
    }
    function bridgePayload(payload) {
      const isObject = value => value && typeof value === 'object';
      const isEnvelope = value => isObject(value) && (
        ((('success' in value) || ('ok' in value) || ('status' in value)) && ('data' in value))
        || (isObject(value.data) && ('success' in value.data || 'ok' in value.data || 'status' in value.data) && ('data' in value.data))
      );
      const normalize = value => {
        if (!isObject(value)) return { success: true, data: value || {} };
        if (value.ok === false || value.status === 'error' || value.success === false) {
          throw new Error(value.message || value.error || '请求失败');
        }
        // Dashboard versions differ on whether the outer response.data wrapper
        // is removed before postMessage. Normalize both forms here.
        if (value.ok === true && 'data' in value) return normalize(value.data);
        if (value.success === true && 'data' in value) {
          if (isEnvelope(value.data)) {
            const nested = normalize(value.data);
            return Object.assign({}, nested, Object.fromEntries(Object.entries(value).filter(([key]) => !['success', 'data'].includes(key))));
          }
          return value;
        }
        if ('data' in value && isEnvelope(value.data)) return normalize(value.data);
        if ('data' in value) return { success: true, data: value.data, message: value.message || '' };
        return { success: true, data: value };
      };
      return normalize(payload);
    }
    if ($('loginToken')) $('loginToken').value = AUTH_TOKEN;

    function headers() {
      const result = {'Content-Type':'application/json'};
      if (AUTH_TOKEN) result['X-Selfie-Image-Token'] = AUTH_TOKEN;
      return result;
    }
    function apiErrorMessage(status, data) {
      return String(data?.error || ("HTTP " + status));
    }
    async function api(path, options = {}) {
      const method = String(options.method || 'GET').toUpperCase();
      const endpoint = bridgeEndpoint(path);
      const trace = traceRequest({
        page: document.querySelector('nav.page-nav button.active')?.dataset.tab || 'base',
        requestKey: `${method} ${endpoint}`,
        sessionId: STUDIO?.current?.id || CREATIVE_CANVAS?.current?.id || '',
        startedAt: Date.now(), status: 'pending'
      });
      const finish = status => { trace.finishedAt = Date.now(); trace.status = status; };
      try {
        if (isDashboardPage()) {
          const bridge = await waitForDashboardBridge();
          if (typeof bridge.ready === 'function') await bridge.ready();
          let body = null;
          if (options.body) {
            try { body = typeof options.body === 'string' ? JSON.parse(options.body) : options.body; }
            catch (_) { body = {}; }
          } else if (method === 'GET' && path.includes('?')) {
            body = Object.fromEntries(new URLSearchParams(path.split('?', 2)[1]));
          }
          const raw = method === 'GET'
            ? await bridge.apiGet(endpoint, body || {})
            : await bridge.apiPost(endpoint, body || {});
          const result = bridgePayload(raw);
          finish('success');
          return result;
        }
        const opts = Object.assign({}, options, {headers: Object.assign({}, headers(), options.headers || {})});
        let res;
        try {
          res = await fetch(path, opts);
        } catch (_) {
          finish('network_error');
          throw new Error("网络请求失败，请检查 Web 服务连接");
        }
        let data;
        try {
          data = await res.json();
        } catch (_) {
          finish(`http_${res.status}`);
          throw new Error("接口返回了无效响应（HTTP " + res.status + "）");
        }
        if (!res.ok || data.success === false) {
          if (res.status === 401) {
            clearProtectedMediaCache();
            document.body.classList.remove("authed");
          }
          finish(`http_${res.status}`);
          throw new Error(apiErrorMessage(res.status, data));
        }
        finish(`http_${res.status}`);
        return data;
      } catch (error) {
        if (trace.status === 'pending') finish('error');
        throw error;
      }
    }
    function textList(id) { return ($(id).value || '').split(/[\n,|]+/).map(s => s.trim()).filter(Boolean); }
    function setTextList(id, value) { $(id).value = Array.isArray(value) ? value.join('\n') : String(value || ''); }
    function setSelectOptions(id, values, selected = '') {
      const el = $(id);
      el.innerHTML = '';
      for (const value of values) {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = value || '留空';
        if (value === selected) opt.selected = true;
        el.appendChild(opt);
      }
    }
    function showToast(text, type = 'info') {
      const wrap = $('toastWrap');
      if (!wrap || !text) return;
      const node = document.createElement('div');
      node.className = 'toast' + (type === 'ok' ? ' ok' : type === 'bad' ? ' bad' : '');
      node.textContent = text;
      wrap.appendChild(node);
      setTimeout(() => node.remove(), 2600);
    }
    let CONFIRM_RESOLVER = null;
    let CONFIRM_PREVIOUS_FOCUS = null;
    function closeConfirmAction(accepted = false) {
      const modal = $('confirmModal');
      if (modal) modal.classList.remove('show');
      const resolver = CONFIRM_RESOLVER;
      CONFIRM_RESOLVER = null;
      const previousFocus = CONFIRM_PREVIOUS_FOCUS;
      CONFIRM_PREVIOUS_FOCUS = null;
      if (resolver) resolver(Boolean(accepted));
      if (previousFocus && typeof previousFocus.focus === 'function' && document.contains(previousFocus)) {
        setTimeout(() => previousFocus.focus(), 0);
      }
    }
    function confirmAction(message, options = {}) {
      const modal = $('confirmModal');
      if (!modal) return Promise.resolve(false);
      if (CONFIRM_RESOLVER) closeConfirmAction(false);
      $('confirmTitle').textContent = String(options.title || '请确认操作');
      $('confirmMessage').textContent = String(message || '确定继续吗？');
      $('confirmProceedBtn').textContent = String(options.confirmText || '确认');
      $('confirmProceedBtn').className = options.danger === false ? 'ok' : 'danger';
      CONFIRM_PREVIOUS_FOCUS = document.activeElement && typeof document.activeElement.focus === 'function'
        ? document.activeElement
        : null;
      modal.classList.add('show');
      const proceed = $('confirmProceedBtn');
      if (proceed) proceed.focus();
      return new Promise(resolve => { CONFIRM_RESOLVER = resolve; });
    }
    function switchChannelPane(kind = 'image') {
      ACTIVE_CHANNEL_PANE = kind === 'audit' ? 'audit' : 'image';
      const map = { image: 'channelTabImage', audit: 'channelTabAudit' };
      const panes = { image: 'channelPaneImage', audit: 'channelPaneAudit' };
      for (const key of Object.keys(map)) {
        const tab = $(map[key]);
        const pane = $(panes[key]);
        if (tab) tab.classList.toggle('active', ACTIVE_CHANNEL_PANE === key);
        if (pane) pane.classList.toggle('active', ACTIVE_CHANNEL_PANE === key);
      }
      document.querySelectorAll('[data-priority-kind]').forEach(block => {
        block.style.display = block.getAttribute('data-priority-kind') === ACTIVE_CHANNEL_PANE ? '' : 'none';
      });
      document.querySelectorAll('[data-channel-add]').forEach(button => {
        button.hidden = button.getAttribute('data-channel-add') !== ACTIVE_CHANNEL_PANE;
      });
    }
    const DEFAULT_IMAGE_PROMPT_EN_TEMPLATE = `Translate the image-generation prompt into natural English.
Goal: faithful language conversion only. Do not rewrite creative intent.

Rules:
1. Keep the same meaning, detail order, style tags, and constraints.
2. Do not add, remove, or strengthen subjects, clothing, poses, scenes, or bans.
3. Do not summarize, beautify, or restructure into a new prompt.
4. Proper nouns, model tags, and bracket markers may stay as-is when needed.
5. If already English, return it with only minimal grammar fixes.

Return ONLY one JSON object (no markdown, no extra text):
{"ok":true,"en":"<english prompt>"}
On failure still return JSON: {"ok":false,"en":""}

Source prompt:
{prompt}`;
    function ensureConfig() {
      delete CONFIG.web;
      delete CONFIG.webHost;
      delete CONFIG.webPort;
      delete CONFIG.webToken;
      delete CONFIG.webEnable;
      delete CONFIG.bot_name;
      delete CONFIG.botName;
      delete CONFIG.personality;
      CONFIG.permission ??= {};
      CONFIG.image ??= {};
      CONFIG.proxies ??= [];
      CONFIG.image_channels ??= [];
      CONFIG.audit_channels ??= [];
      CONFIG.enabled_image_model_priority ??= [];
      CONFIG.enabled_audit_model_priority ??= [];
      CONFIG.image_model_call_mode ??= CONFIG.random_image_model ? 'random' : 'sequence';
      delete CONFIG.random_image_model;
      const img = CONFIG.image;
      img.enable_llm_tool ??= true;
      img.default_aspect_ratio ??= '9:16';
      img.default_resolution ??= '1K';
      img.max_concurrent_tasks ??= 5;
      const configuredConcurrent = Number(img.max_concurrent_tasks);
      img.max_concurrent_tasks = Number.isInteger(configuredConcurrent) && configuredConcurrent >= 1 && configuredConcurrent <= 5 ? configuredConcurrent : 5;
      img.global_timeout ??= 280;
      img.max_image_size_mb ??= 10;
      img.cache_limit_mb ??= 200;
      img.cache_limit_count ??= 100;
      img.max_batch_count ??= 10;
      img.batch_on_failure ??= 'skip';
      img.batch_skip_max ??= 2;
      img.rate_limit_seconds ??= 0;
      img.enable_daily_limit ??= false;
      img.daily_limit_count ??= 10;
      img.show_generation_info ??= false;
      img.show_model_info ??= false;
      img.use_logo_when_no_persona ??= true;
      img.blocked_words ??= [];
      img.enable_prompt_audit ??= false;
      img.enable_output_audit ??= false;
      img.prompt_audit_threshold ??= 60;
      img.output_audit_threshold ??= 60;
      img.prompt_audit_model ??= '';
      img.output_audit_model ??= '';
      img.ocr_model ??= '';
      img.prompt_audit_template ??= '你是生图安全审核员。请评估以下提示词的违规风险。提示词：{prompt}。type 是主要风险类型，只能填写 sex、violence、drugs、illegal 或 other；percentage 是违规风险百分比（0=无风险，100=确定违规）；根据阈值 {threshold}% 填写 allow（percentage 大于等于阈值时为 false，否则为 true）。仅输出 JSON：{"allow":true/false,"type":"sex","percentage":0-100,"reason":"原因"}';
      img.output_audit_template ??= '你是图像安全审核员。请评估以下图片不适合普通用户的风险。type 是主要风险类型，只能填写 sex、violence、drugs、illegal 或 other；percentage 是违规风险百分比（0=无风险，100=确定违规）；根据阈值 {threshold}% 填写 allow（percentage 大于等于阈值时为 false，否则为 true）。仅输出 JSON：{"allow":true/false,"type":"sex","percentage":0-100,"reason":"原因"}';
      if (img.prompt_audit_template === '你是生图安全审核员。请判断以下提示词是否安全。提示词：{prompt}。仅输出 JSON：{"allow":true/false,"reason":"原因"}') img.prompt_audit_template = '你是生图安全审核员。请评估以下提示词的违规风险。提示词：{prompt}。type 是主要风险类型，只能填写 sex、violence、drugs、illegal 或 other；percentage 是违规风险百分比（0=无风险，100=确定违规）；根据阈值 {threshold}% 填写 allow（percentage 大于等于阈值时为 false，否则为 true）。仅输出 JSON：{"allow":true/false,"type":"sex","percentage":0-100,"reason":"原因"}';
      if (img.output_audit_template === '你是图像安全审核员。请判断以下图片是否适合普通用户。仅输出 JSON：{"allow":true/false,"reason":"原因"}') img.output_audit_template = '你是图像安全审核员。请评估以下图片不适合普通用户的风险。type 是主要风险类型，只能填写 sex、violence、drugs、illegal 或 other；percentage 是违规风险百分比（0=无风险，100=确定违规）；根据阈值 {threshold}% 填写 allow（percentage 大于等于阈值时为 false，否则为 true）。仅输出 JSON：{"allow":true/false,"type":"sex","percentage":0-100,"reason":"原因"}';
      img.enable_image_prompt_en ??= false;
      img.prompt_en_mode ??= 'if_cjk';
      img.prompt_en_model ??= '';
      img.image_prompt_en_template = String(img.image_prompt_en_template || '').trim() || DEFAULT_IMAGE_PROMPT_EN_TEMPLATE;
      if (String(img.image_prompt_en_template||'').includes('Task: rewrite the user prompt') || !String(img.image_prompt_en_template||'').includes('"ok":true')) img.image_prompt_en_template = DEFAULT_IMAGE_PROMPT_EN_TEMPLATE;
    }

    function fillForms() {
      IS_FILLING = true;
      try {
        ensureConfig();
        normalizeChannels();
        normalizeAuditChannels();
        const p = CONFIG.permission, img = CONFIG.image;
        setTextList('groupUsableUsers', p.usable_users);
        $('groupUsableEnabled').checked = !!p.usable_users_enabled;
        setTextList('groupBlockedUsers', p.blocked_users);
        setTextList('groupWhitelistUsers', p.whitelist_users);
        updateGlobalUsableGateUi();

        for (const id of ['defaultAspect','selfieAspect','testAspect']) setSelectOptions(id, ASPECTS, img.default_aspect_ratio || '9:16');
        $('defaultResolution').value = img.default_resolution || '1K';
        $('testResolution').value = img.default_resolution || '1K';
        if ($('testCount')) {
          const testCountLimit = Math.max(1, Math.min(100, Number(img.max_batch_count || 10)));
          $('testCount').max = String(testCountLimit);
          const currentTestCount = Number($('testCount').value || 1);
          $('testCount').value = String(Math.max(1, Math.min(testCountLimit, currentTestCount)));
        }
        $('maxConcurrent').value = img.max_concurrent_tasks;
        $('globalTimeout').value = img.global_timeout;
        $('maxImageSize').value = img.max_image_size_mb;
        $('cacheLimitMB').value = img.cache_limit_mb;
        $('cacheLimitCount').value = img.cache_limit_count;
        if ($('recordKeepLimit')) $('recordKeepLimit').value = img.record_keep_limit ?? 1000;
        $('maxBatchCount').value = img.max_batch_count;
         if ($('batchOnFailure')) {
           const m = String(img.batch_on_failure || 'skip');
           $('batchOnFailure').value = ['stop','skip','skip_max'].includes(m) ? m : 'skip';
         }
         if ($('batchSkipMax')) $('batchSkipMax').value = img.batch_skip_max ?? 2;
        $('rateLimitSeconds').value = img.rate_limit_seconds;
        $('dailyLimitCount').value = img.daily_limit_count;
        $('enableLLMTool').checked = !!img.enable_llm_tool;
        $('showGenerationInfo').checked = !!img.show_generation_info;
        $('showModelInfo').checked = !!img.show_model_info;
        if ($('useLogoWhenNoPersona')) $('useLogoWhenNoPersona').checked = img.use_logo_when_no_persona !== false;
        $('enableDailyLimit').checked = !!img.enable_daily_limit;

        $('selfieAspect').value = img.default_aspect_ratio || '9:16';

        $('enablePromptAudit').checked = !!img.enable_prompt_audit;
        $('enableOutputAudit').checked = !!img.enable_output_audit;
        $('promptAuditThreshold').value = img.prompt_audit_threshold ?? 60;
        $('outputAuditThreshold').value = img.output_audit_threshold ?? 60;
        setTextList('blockedWords', img.blocked_words);
        $('promptAuditTemplate').value = img.prompt_audit_template || '';
        $('outputAuditTemplate').value = img.output_audit_template || '';
        if ($('enableImagePromptEn')) $('enableImagePromptEn').checked = !!img.enable_image_prompt_en;
        if ($('promptEnMode')) $('promptEnMode').value = (img.prompt_en_mode === 'always') ? 'always' : 'if_cjk';
        if ($('imagePromptEnTemplate')) $('imagePromptEnTemplate').value = String(img.image_prompt_en_template || '').trim() || DEFAULT_IMAGE_PROMPT_EN_TEMPLATE;

        $('priorityList').value = (CONFIG.enabled_image_model_priority || []).join('\n');
        $('auditPriorityList').value = (CONFIG.enabled_audit_model_priority || []).join('\n');
        if ($('imageModelCallMode')) $('imageModelCallMode').value = ['sequence','random','fixed'].includes(CONFIG.image_model_call_mode) ? CONFIG.image_model_call_mode : 'sequence';
        renderAllChannelLists();
        refreshModelSelectors();
        renderAllPriorityRows();
        switchChannelPane(ACTIVE_CHANNEL_PANE);
        $('configText').value = JSON.stringify(CONFIG, null, 2);
      } finally {
        IS_FILLING = false;
      }
    }

    function collectForms() {
      ensureConfig();
      CONFIG.permission = {...(CONFIG.permission || {})};
      CONFIG.image.default_aspect_ratio = $('defaultAspect').value || $('selfieAspect').value || '9:16';
      CONFIG.image.default_resolution = $('defaultResolution').value || '1K';
      const maxConcurrent = Number($('maxConcurrent').value || 5);
      CONFIG.image.max_concurrent_tasks = Number.isInteger(maxConcurrent) && maxConcurrent >= 1 && maxConcurrent <= 5 ? maxConcurrent : 5;
      CONFIG.image.global_timeout = Number($('globalTimeout').value || 280);
      CONFIG.image.max_image_size_mb = Number($('maxImageSize').value || 10);
      CONFIG.image.cache_limit_mb = Number($('cacheLimitMB').value || 2048);
      CONFIG.image.cache_limit_count = Number($('cacheLimitCount').value || 1000);
      if ($('recordKeepLimit')) CONFIG.image.record_keep_limit = Number($('recordKeepLimit').value || 1000);
      const maxBatchCount = Number($('maxBatchCount').value || 10);
      CONFIG.image.max_batch_count = Number.isInteger(maxBatchCount) && maxBatchCount >= 1 && maxBatchCount <= 100 ? maxBatchCount : 100;
       CONFIG.image.batch_on_failure = ($('batchOnFailure') && $('batchOnFailure').value) || 'skip';
       if (!['stop','skip','skip_max'].includes(CONFIG.image.batch_on_failure)) CONFIG.image.batch_on_failure = 'skip';
       CONFIG.image.batch_skip_max = Number(($('batchSkipMax') && $('batchSkipMax').value) || 2);
      CONFIG.image.rate_limit_seconds = Number($('rateLimitSeconds').value || 0);
      CONFIG.image.daily_limit_count = Number($('dailyLimitCount').value || 10);
      CONFIG.image.enable_llm_tool = $('enableLLMTool').checked;
      CONFIG.image.show_generation_info = $('showGenerationInfo').checked;
      CONFIG.image.show_model_info = $('showModelInfo').checked;
      if ($('useLogoWhenNoPersona')) CONFIG.image.use_logo_when_no_persona = $('useLogoWhenNoPersona').checked;
      CONFIG.image.enable_daily_limit = $('enableDailyLimit').checked;
      CONFIG.image.enable_prompt_audit = $('enablePromptAudit').checked;
      CONFIG.image.enable_output_audit = $('enableOutputAudit').checked;
      const promptAuditThreshold = Number($('promptAuditThreshold').value);
      const outputAuditThreshold = Number($('outputAuditThreshold').value);
      CONFIG.image.prompt_audit_threshold = String($('promptAuditThreshold').value).trim() && Number.isFinite(promptAuditThreshold) ? Math.min(100, Math.max(0, Math.round(promptAuditThreshold))) : 60;
      CONFIG.image.output_audit_threshold = String($('outputAuditThreshold').value).trim() && Number.isFinite(outputAuditThreshold) ? Math.min(100, Math.max(0, Math.round(outputAuditThreshold))) : 60;
      CONFIG.image.prompt_audit_model = $('promptAuditModel').value || '';
      CONFIG.image.output_audit_model = $('outputAuditModel').value || '';
      CONFIG.image.ocr_model = $('ocrModel').value || '';
      delete CONFIG.image.audit_whitelist;
      CONFIG.image.blocked_words = textList('blockedWords');
      CONFIG.image.prompt_audit_template = $('promptAuditTemplate').value;
      CONFIG.image.output_audit_template = $('outputAuditTemplate').value;
       CONFIG.image.enable_image_prompt_en = !!( $('enableImagePromptEn') && $('enableImagePromptEn').checked );
       CONFIG.image.prompt_en_mode = ($('promptEnMode') && $('promptEnMode').value === 'always') ? 'always' : 'if_cjk';
       CONFIG.image.prompt_en_model = ($('promptEnModel') && $('promptEnModel').value) || '';
       CONFIG.image.image_prompt_en_template = String(($('imagePromptEnTemplate') && $('imagePromptEnTemplate').value) || '').trim() || DEFAULT_IMAGE_PROMPT_EN_TEMPLATE;
      collectChannels();
      for (const kind of ['image','audit']) prunePriorityList(kind);
      CONFIG.enabled_image_model_priority = textList('priorityList');
      CONFIG.enabled_audit_model_priority = textList('auditPriorityList');
      CONFIG.image_model_call_mode = ($('imageModelCallMode') && $('imageModelCallMode').value) || 'sequence';
      return CONFIG;
    }
