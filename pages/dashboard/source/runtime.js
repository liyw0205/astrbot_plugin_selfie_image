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
    const DASHBOARD_PERSIST_CACHE_TTL_MS = 3 * 24 * 60 * 60 * 1000;
    const DASHBOARD_PERSIST_CACHE_PREFIX = 'selfieImageDashboardCache:v1:';
    const DASHBOARD_PERSIST_CACHE_INDEX_KEY = `${DASHBOARD_PERSIST_CACHE_PREFIX}index`;
    const DASHBOARD_PERSIST_CACHE_MAX_ENTRIES = 160;
    const DASHBOARD_PERSIST_CACHE_MAX_BYTES = 3 * 1024 * 1024;
    const DASHBOARD_PERSIST_CACHE_ENTRY_MAX_BYTES = 700 * 1024;
    // Media payloads are data URLs and are larger than catalog JSON. Keep a
    // separate bound so an opaque embedded frame can retain normal previews
    // without allowing one unusually large catalog entry to consume the cache.
    const DASHBOARD_PERSIST_MEDIA_ENTRY_MAX_BYTES = 2 * 1024 * 1024;
    // Sandboxed AstrBot iframes have an opaque origin, so both Web Storage and
    // CacheStorage are unavailable. window.name is scoped to this browsing
    // context and survives same-page reloads; keep it bounded and namespaced.
    const DASHBOARD_WINDOW_NAME_PREFIX = '__selfie_image_dashboard_cache_v1__:';
    const DASHBOARD_WINDOW_NAME_MAX_BYTES = 2 * 1024 * 1024;
    const DASHBOARD_PERSIST_CACHE_NAME = 'selfie-image-dashboard-v1';
    const DASHBOARD_PERSIST_MEMORY = new Map();
    const DASHBOARD_PERSIST_CACHE_WRITE_SEQ = new Map();
    const DASHBOARD_PERSIST_CACHE_WRITE_OP = new Map();
    let DASHBOARD_PERSIST_STORAGE_HYDRATION = null;
    let DASHBOARD_PERSIST_STORAGE_HYDRATED = false;
    function dashboardPersistHash(value) {
      let hash = 2166136261;
      for (const char of String(value || '')) {
        hash ^= char.charCodeAt(0);
        hash = Math.imul(hash, 16777619);
      }
      return (hash >>> 0).toString(36);
    }
    function dashboardPersistScope() {
      // During standalone login AUTH_TOKEN is available before the successful
      // config response writes it to localStorage. Use that value first so the
      // first cache write and the next reload use the same scope.
      const activeToken = typeof AUTH_TOKEN !== 'undefined' ? AUTH_TOKEN : '';
      const token = String(activeToken || safeStorageGet('selfieImageToken') || safeStorageGet('aicatToken') || '').trim();
      return token ? `token:${dashboardPersistHash(token)}` : (isDashboardPage() ? 'dashboard' : 'anonymous');
    }
    function dashboardPersistKey(namespace, key, scope = dashboardPersistScope()) {
      return `${DASHBOARD_PERSIST_CACHE_PREFIX}${scope}:${namespace}:${encodeURIComponent(String(key || ''))}`;
    }
    function dashboardPersistCacheStorage() {
      try {
        return typeof window.caches?.open === 'function' && typeof window.Response === 'function'
          ? window.caches : null;
      } catch (_) {
        return null;
      }
    }
    function dashboardPersistCacheUrl(storageKey) {
      return `/__selfie_dashboard_cache__/${encodeURIComponent(storageKey)}`;
    }
    function readDashboardWindowNameCache() {
      try {
        const raw = String(window.name || '');
        if (!raw.startsWith(DASHBOARD_WINDOW_NAME_PREFIX)) return {};
        const value = JSON.parse(raw.slice(DASHBOARD_WINDOW_NAME_PREFIX.length));
        return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
      } catch (_) {
        return {};
      }
    }
    function writeDashboardWindowNameCache(value) {
      try {
        const encoded = JSON.stringify(value || {});
        if (encoded.length > DASHBOARD_WINDOW_NAME_MAX_BYTES) return false;
        window.name = DASHBOARD_WINDOW_NAME_PREFIX + encoded;
        return String(window.name || '') === DASHBOARD_WINDOW_NAME_PREFIX + encoded;
      } catch (_) {
        return false;
      }
    }
    function readDashboardWindowNameEntry(storageKey) {
      const value = readDashboardWindowNameCache()[storageKey];
      return value && typeof value === 'object' ? value : null;
    }
    function writeDashboardWindowNameEntry(storageKey, entry) {
      const cache = readDashboardWindowNameCache();
      cache[storageKey] = entry;
      return writeDashboardWindowNameCache(cache);
    }
    function removeDashboardWindowNameEntry(storageKey) {
      const cache = readDashboardWindowNameCache();
      if (!Object.prototype.hasOwnProperty.call(cache, storageKey)) return;
      delete cache[storageKey];
      writeDashboardWindowNameCache(cache);
    }
    function clearDashboardWindowNameNamespace(namespace, predicate = null) {
      const cache = readDashboardWindowNameCache();
      let changed = false;
      for (const [storageKey, value] of Object.entries(cache)) {
        if (value?.namespace !== namespace) continue;
        if (typeof predicate === 'function' && !predicate(String(value.key || ''))) continue;
        delete cache[storageKey];
        changed = true;
      }
      if (changed) writeDashboardWindowNameCache(cache);
    }
    function pruneDashboardWindowNameCache() {
      const cache = readDashboardWindowNameCache();
      const now = Date.now();
      const entries = Object.entries(cache).filter(([, value]) => {
        if (!value || !value.storedAt || now - Number(value.storedAt) > DASHBOARD_PERSIST_CACHE_TTL_MS) return false;
        return true;
      }).sort((a, b) => Number(a[1]?.lastAccessed || a[1]?.storedAt || 0) - Number(b[1]?.lastAccessed || b[1]?.storedAt || 0));
      const kept = {};
      let total = 0;
      for (const [storageKey, value] of entries.reverse()) {
        const size = Math.max(0, Number(value?.size || 0));
        if (Object.keys(kept).length >= DASHBOARD_PERSIST_CACHE_MAX_ENTRIES || total + size > DASHBOARD_WINDOW_NAME_MAX_BYTES) continue;
        kept[storageKey] = value;
        total += size;
      }
      if (Object.keys(kept).length !== Object.keys(cache).length) writeDashboardWindowNameCache(kept);
      return kept;
    }
    function scheduleDashboardPersistCacheDelete(storageKey) {
      const sequence = (DASHBOARD_PERSIST_CACHE_WRITE_SEQ.get(storageKey) || 0) + 1;
      DASHBOARD_PERSIST_CACHE_WRITE_SEQ.set(storageKey, sequence);
      DASHBOARD_PERSIST_CACHE_WRITE_OP.set(storageKey, 'delete');
      const cachesApi = dashboardPersistCacheStorage();
      if (!cachesApi) return;
      void cachesApi.open(DASHBOARD_PERSIST_CACHE_NAME)
        .then(cache => cache.delete(dashboardPersistCacheUrl(storageKey)))
        .catch(() => {});
    }
    function scheduleDashboardPersistCacheWrite(storageKey, encoded) {
      const cachesApi = dashboardPersistCacheStorage();
      if (!cachesApi) return;
      const sequence = (DASHBOARD_PERSIST_CACHE_WRITE_SEQ.get(storageKey) || 0) + 1;
      DASHBOARD_PERSIST_CACHE_WRITE_SEQ.set(storageKey, sequence);
      DASHBOARD_PERSIST_CACHE_WRITE_OP.set(storageKey, 'write');
      void cachesApi.open(DASHBOARD_PERSIST_CACHE_NAME).then(async cache => {
        if (DASHBOARD_PERSIST_CACHE_WRITE_SEQ.get(storageKey) !== sequence) return;
        await cache.put(dashboardPersistCacheUrl(storageKey), new Response(encoded, {
          headers: {'content-type': 'application/json'},
        }));
        if (DASHBOARD_PERSIST_CACHE_WRITE_SEQ.get(storageKey) !== sequence
          && DASHBOARD_PERSIST_CACHE_WRITE_OP.get(storageKey) === 'delete') {
          await cache.delete(dashboardPersistCacheUrl(storageKey));
        }
      }).catch(() => {});
    }
    async function hydrateDashboardPersistStorage() {
      if (DASHBOARD_PERSIST_STORAGE_HYDRATED) return;
      if (DASHBOARD_PERSIST_STORAGE_HYDRATION) return DASHBOARD_PERSIST_STORAGE_HYDRATION;
      const cachesApi = dashboardPersistCacheStorage();
      if (!cachesApi) {
        DASHBOARD_PERSIST_STORAGE_HYDRATED = true;
        return;
      }
      DASHBOARD_PERSIST_STORAGE_HYDRATION = (async () => {
        try {
          const cache = await cachesApi.open(DASHBOARD_PERSIST_CACHE_NAME);
          const requests = await cache.keys();
          const now = Date.now();
          for (const request of requests) {
            const response = await cache.match(request);
            if (!response) continue;
            try {
              const value = await response.json();
              const storageKey = String(value?.storageKey || '');
              const namespace = String(value?.namespace || '');
              const key = String(value?.key || '');
              const scope = String(value?.scope || '');
              const storedAt = Number(value?.storedAt || 0);
              if (!storageKey || !namespace || !scope || !storedAt || now - storedAt > DASHBOARD_PERSIST_CACHE_TTL_MS) {
                await cache.delete(request);
                continue;
              }
              DASHBOARD_PERSIST_MEMORY.set(storageKey, {
                namespace, key, scope, storedAt,
                lastAccessed: Number(value?.lastAccessed || storedAt),
                size: Number(value?.size || 0), data: value.data,
              });
            } catch (_) {
              await cache.delete(request);
            }
          }
          const entries = [...DASHBOARD_PERSIST_MEMORY.entries()]
            .sort((a, b) => Number(a[1]?.lastAccessed || 0) - Number(b[1]?.lastAccessed || 0));
          let totalBytes = entries.reduce((sum, item) => sum + Math.max(0, Number(item[1]?.size || 0)), 0);
          while (entries.length > DASHBOARD_PERSIST_CACHE_MAX_ENTRIES || totalBytes > DASHBOARD_PERSIST_CACHE_MAX_BYTES) {
            const [storageKey, item] = entries.shift() || [];
            if (!storageKey) break;
            totalBytes -= Math.max(0, Number(item?.size || 0));
            DASHBOARD_PERSIST_MEMORY.delete(storageKey);
            await cache.delete(dashboardPersistCacheUrl(storageKey)).catch(() => {});
          }
          DASHBOARD_PERSIST_STORAGE_HYDRATED = true;
        } catch (_) {
          // CacheStorage is optional in embedded/sandboxed browsers.
        }
      })().finally(() => { DASHBOARD_PERSIST_STORAGE_HYDRATION = null; });
      return DASHBOARD_PERSIST_STORAGE_HYDRATION;
    }
    function readDashboardPersistIndex() {
      try {
        const value = JSON.parse(safeStorageGet(DASHBOARD_PERSIST_CACHE_INDEX_KEY, '{}'));
        return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
      } catch (_) {
        return {};
      }
    }
    function writeDashboardPersistIndex(index) {
      safeStorageSet(DASHBOARD_PERSIST_CACHE_INDEX_KEY, JSON.stringify(index || {}));
    }
    function dashboardPersistStorageKeys() {
      const keys = [];
      try {
        const storage = window.localStorage;
        if (!storage || typeof storage.length !== 'number') return keys;
        for (let index = 0; index < storage.length; index += 1) {
          const key = storage.key(index);
          if (!key || key === DASHBOARD_PERSIST_CACHE_INDEX_KEY || !key.startsWith(DASHBOARD_PERSIST_CACHE_PREFIX)) continue;
          keys.push(key);
        }
      } catch (_) {}
      return keys;
    }
    function dashboardPersistEntryMetadata(value, storageKey = '') {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
      const key = String(value.key || '');
      const namespace = String(value.namespace || '');
      const scope = String(value.scope || '');
      const storedAt = Number(value.storedAt || 0);
      const resolvedKey = String(value.storageKey || storageKey || '');
      if (!resolvedKey || !namespace || !scope || !storedAt) return null;
      return {
        namespace, key, scope, storedAt,
        lastAccessed: Number(value.lastAccessed || storedAt),
        size: Math.max(0, Number(value.size || 0)),
      };
    }
    function recoverDashboardPersistIndex(index = readDashboardPersistIndex()) {
      let changed = false;
      for (const storageKey of dashboardPersistStorageKeys()) {
        if (index[storageKey]) continue;
        const raw = safeStorageGet(storageKey, '');
        if (!raw) continue;
        try {
          const value = JSON.parse(raw);
          const metadata = dashboardPersistEntryMetadata(value, storageKey);
          if (!metadata || value.storageKey !== storageKey) continue;
          index[storageKey] = metadata;
          changed = true;
        } catch (_) {}
      }
      if (changed) writeDashboardPersistIndex(index);
      return index;
    }
    function removeDashboardPersistEntry(storageKey, index = readDashboardPersistIndex()) {
      safeStorageRemove(storageKey);
      DASHBOARD_PERSIST_MEMORY.delete(storageKey);
      removeDashboardWindowNameEntry(storageKey);
      scheduleDashboardPersistCacheDelete(storageKey);
      if (Object.prototype.hasOwnProperty.call(index, storageKey)) {
        delete index[storageKey];
        writeDashboardPersistIndex(index);
      }
    }
    function dashboardPersistEntryExpired(item, now = Date.now()) {
      return !item || now - Number(item.storedAt || 0) > DASHBOARD_PERSIST_CACHE_TTL_MS;
    }
    function pruneDashboardPersistCache() {
      const index = recoverDashboardPersistIndex();
      const now = Date.now();
      let changed = false;
      let totalBytes = 0;
      const entries = [];
      for (const [storageKey, item] of Object.entries(index)) {
        const raw = safeStorageGet(storageKey, '');
        const missing = !raw;
        if (missing || dashboardPersistEntryExpired(item, now)) {
          safeStorageRemove(storageKey);
          DASHBOARD_PERSIST_MEMORY.delete(storageKey);
          removeDashboardWindowNameEntry(storageKey);
          scheduleDashboardPersistCacheDelete(storageKey);
          delete index[storageKey];
          changed = true;
          continue;
        }
        const size = Math.max(0, Number(item.size || raw.length || 0));
        totalBytes += size;
        entries.push({storageKey, item: Object.assign({}, item, {size})});
      }
      entries.sort((a, b) => Number(a.item.lastAccessed || a.item.storedAt || 0) - Number(b.item.lastAccessed || b.item.storedAt || 0));
      while (entries.length > DASHBOARD_PERSIST_CACHE_MAX_ENTRIES || totalBytes > DASHBOARD_PERSIST_CACHE_MAX_BYTES) {
        const oldest = entries.shift();
        if (!oldest) break;
        totalBytes -= Number(oldest.item.size || 0);
        safeStorageRemove(oldest.storageKey);
        DASHBOARD_PERSIST_MEMORY.delete(oldest.storageKey);
        removeDashboardWindowNameEntry(oldest.storageKey);
        scheduleDashboardPersistCacheDelete(oldest.storageKey);
        delete index[oldest.storageKey];
        changed = true;
      }
      if (changed) writeDashboardPersistIndex(index);
      return index;
    }
    function readDashboardPersistCacheEntry(namespace, key) {
      const scope = dashboardPersistScope();
      const storageKey = dashboardPersistKey(namespace, key, scope);
      const index = readDashboardPersistIndex();
      const raw = safeStorageGet(storageKey, '');
      if (!raw) {
        const memory = DASHBOARD_PERSIST_MEMORY.get(storageKey);
        const fallback = memory || readDashboardWindowNameEntry(storageKey);
        if (!fallback) {
          if (index[storageKey]) removeDashboardPersistEntry(storageKey, index);
          return null;
        }
        const fallbackScope = String(fallback.scope || '');
        if (dashboardPersistEntryExpired(fallback) || fallbackScope !== scope) {
          removeDashboardPersistEntry(storageKey, index);
          return null;
        }
        fallback.lastAccessed = Date.now();
        DASHBOARD_PERSIST_MEMORY.set(storageKey, fallback);
        writeDashboardWindowNameEntry(storageKey, fallback);
        return {data: fallback.data, storedAt: Number(fallback.storedAt || 0)};
      }
      try {
        const value = JSON.parse(raw);
        let item = index[storageKey];
        if (!item) {
          // A previous quota error can leave the value written while the
          // shared index write fails. Rebuild that one entry instead of
          // discarding otherwise valid cached data.
          item = dashboardPersistEntryMetadata(value, storageKey);
          if (!item || value.storageKey !== storageKey) {
            safeStorageRemove(storageKey);
            return null;
          }
          index[storageKey] = item;
          writeDashboardPersistIndex(index);
        }
        if (dashboardPersistEntryExpired(item) || value.scope !== scope) {
          removeDashboardPersistEntry(storageKey, index);
          return null;
        }
        item.lastAccessed = Date.now();
        index[storageKey] = item;
        safeStorageSet(storageKey, JSON.stringify(value));
        writeDashboardPersistIndex(index);
        DASHBOARD_PERSIST_MEMORY.set(storageKey, {
          namespace, key: String(key || ''), scope,
          storedAt: Number(value.storedAt || item.storedAt || 0),
          lastAccessed: item.lastAccessed, size: Number(item.size || raw.length || 0), data: value.data,
        });
        return {data: value.data, storedAt: Number(value.storedAt || item.storedAt || 0)};
      } catch (_) {
        removeDashboardPersistEntry(storageKey, index);
        return null;
      }
    }
    function readDashboardPersistCache(namespace, key) {
      return readDashboardPersistCacheEntry(namespace, key)?.data ?? null;
    }
    function writeDashboardPersistCache(namespace, key, data) {
      let encoded;
      try {
        const scope = dashboardPersistScope();
        const storedAt = Date.now();
        const storageKey = dashboardPersistKey(namespace, key, scope);
        const entry = {scope, namespace, key: String(key || ''), storageKey, storedAt, lastAccessed: storedAt, data};
        encoded = JSON.stringify(entry);
        const entryLimit = namespace === 'media'
          ? DASHBOARD_PERSIST_MEDIA_ENTRY_MAX_BYTES
          : DASHBOARD_PERSIST_CACHE_ENTRY_MAX_BYTES;
        if (encoded.length > entryLimit) return false;
        DASHBOARD_PERSIST_MEMORY.set(storageKey, {
          namespace, key: String(key || ''), scope, storedAt, lastAccessed: storedAt,
          size: encoded.length, data,
        });
        scheduleDashboardPersistCacheWrite(storageKey, JSON.stringify(Object.assign({}, entry, {size: encoded.length})));
        safeStorageSet(storageKey, encoded);
        const storedInLocalStorage = safeStorageGet(storageKey, '') === encoded;
        const index = readDashboardPersistIndex();
        if (storedInLocalStorage) {
          index[storageKey] = {namespace, key: String(key || ''), scope, storedAt, lastAccessed: storedAt, size: encoded.length};
          writeDashboardPersistIndex(index);
          pruneDashboardPersistCache();
        }
        const storedInWindowName = !storedInLocalStorage && writeDashboardWindowNameEntry(
          storageKey,
          Object.assign({}, entry, {size: encoded.length}),
        );
        if (storedInWindowName) pruneDashboardWindowNameCache();
        return storedInLocalStorage || storedInWindowName || Boolean(dashboardPersistCacheStorage());
      } catch (_) {
        return false;
      }
    }
    function removeDashboardPersistCache(namespace, key) {
      const scope = dashboardPersistScope();
      removeDashboardPersistEntry(dashboardPersistKey(namespace, key, scope));
    }
    function clearDashboardPersistNamespace(namespace, predicate = null) {
      const index = readDashboardPersistIndex();
      let changed = false;
      for (const [storageKey, item] of Object.entries(index)) {
        if (item.namespace !== namespace) continue;
        if (typeof predicate === 'function' && !predicate(String(item.key || ''))) continue;
        safeStorageRemove(storageKey);
        DASHBOARD_PERSIST_MEMORY.delete(storageKey);
        removeDashboardWindowNameEntry(storageKey);
        scheduleDashboardPersistCacheDelete(storageKey);
        delete index[storageKey];
        changed = true;
      }
      for (const [storageKey, item] of [...DASHBOARD_PERSIST_MEMORY.entries()]) {
        if (item.namespace !== namespace) continue;
        if (typeof predicate === 'function' && !predicate(String(item.key || ''))) continue;
        DASHBOARD_PERSIST_MEMORY.delete(storageKey);
        removeDashboardWindowNameEntry(storageKey);
        scheduleDashboardPersistCacheDelete(storageKey);
      }
      if (changed) writeDashboardPersistIndex(index);
      clearDashboardWindowNameNamespace(namespace, predicate);
    }
    function listDashboardPersistCache(namespace) {
      const index = pruneDashboardPersistCache();
      const scope = dashboardPersistScope();
      const rows = [];
      const seen = new Set();
      for (const [storageKey, item] of Object.entries(index)) {
        if (item.namespace !== namespace || item.scope !== scope) continue;
        const raw = safeStorageGet(storageKey, '');
        if (!raw) continue;
        try {
          const value = JSON.parse(raw);
          if (value.scope !== scope || dashboardPersistEntryExpired(item)) continue;
          item.lastAccessed = Date.now();
          index[storageKey] = item;
          seen.add(storageKey);
          DASHBOARD_PERSIST_MEMORY.set(storageKey, {
            namespace, key: String(item.key || ''), scope,
            storedAt: Number(value.storedAt || item.storedAt || 0),
            lastAccessed: item.lastAccessed, size: Number(item.size || raw.length || 0), data: value.data,
          });
          rows.push({key: String(item.key || ''), data: value.data, storedAt: Number(value.storedAt || 0)});
        } catch (_) {
          removeDashboardPersistEntry(storageKey, index);
        }
      }
      for (const [storageKey, item] of DASHBOARD_PERSIST_MEMORY.entries()) {
        if (seen.has(storageKey) || item.namespace !== namespace || item.scope !== scope) continue;
        if (dashboardPersistEntryExpired(item)) {
          removeDashboardPersistEntry(storageKey, index);
          continue;
        }
        item.lastAccessed = Date.now();
        seen.add(storageKey);
        rows.push({key: String(item.key || ''), data: item.data, storedAt: Number(item.storedAt || 0)});
      }
      const fallback = pruneDashboardWindowNameCache();
      for (const [storageKey, value] of Object.entries(fallback)) {
        if (seen.has(storageKey) || value?.namespace !== namespace || value?.scope !== scope) continue;
        if (dashboardPersistEntryExpired(value)) {
          removeDashboardWindowNameEntry(storageKey);
          continue;
        }
        value.lastAccessed = Date.now();
        DASHBOARD_PERSIST_MEMORY.set(storageKey, value);
        rows.push({key: String(value.key || ''), data: value.data, storedAt: Number(value.storedAt || 0)});
      }
      writeDashboardPersistIndex(index);
      return rows;
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
    // Keep the last server response separate from the editable local snapshot.
    // Writes can mutate CONFIG before the queued request starts, so diffing
    // against this baseline prevents unrelated stale domains from being sent.
    let CONFIG_SERVER_SNAPSHOT = null;
    let CONFIG_REVISION = 0;
    let CONFIG_LOAD_SEQ = 0;
    let QQ_GROUP_LOAD_SEQ = 0;
    let CONFIG_MUTATION_SEQ = 0;
    // Pages are loaded on demand; startup refreshes only tabs the user opened.
    const DASHBOARD_VISITED_TABS = new Set(['base']);
    let SELFIE_REFERENCE_DATA_URL = '';
    let SELFIE_REFERENCE_REQUEST = null;
    let STUDIO = { sessions: [], current: null, canvas: null, selectedNodeId: "", connectNodeId: "", prompts: [], templates: [], promptPresets: [], promptPresetsByKind: {image: []}, cosLooks: [], cosPools: null, pollTimer: null, pollFailures: 0, taskId: "", taskNodeId: "", taskProgress: null, uploadSlotId: "", presetOpen: false, cosOpen: false, nodePresetOpen: false, nodeCosOpen: false, running: false, galleryTargetSlotId: "", nodeSearch: "", viewport: {x: 120, y: 80, zoom: 1}, drag: null, sessionsLoadedAt: 0, sessionsRequest: null, sessionsVersion: 0, loadSeq: 0, viewportFittedForSession: false };
    let CREATIVE_CANVAS = { sessions: [], current: null, templates: [], prompts: [], promptPresets: [], cosLooks: [], taskId: "", running: false, pollTimer: null, uploadSlotId: "", galleryTargetSlotId: "", galleryRequestSeq: 0, presetOpen: false, cosOpen: false, sessionsLoadedAt: 0, sessionsRequest: null, sessionsVersion: 0, loadSeq: 0 };
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
    // A query-key cache prevents duplicate work, but different filters can
    // still overlap.  Keep a generation so an older response cannot repaint
    // the task table after the user has changed filters or pages.
    let TASK_LOAD_SEQ = 0;
    // Invalidate slower task-detail responses when the user switches or closes a detail view.
    let TASK_DETAIL_REQUEST_SEQ = 0;
    let RECORD_LOADING = false;
    let ASSET_LOADING = false;
    const PAGE_CACHE_MAX_ENTRIES = 12;
    const PAGE_CACHE_TTL_MS = 30000;
    const RECORD_PAGE_CACHE = new Map();
    const ASSET_PAGE_CACHE = new Map();
    const RECORD_PAGE_REQUESTS = new Map();
    const ASSET_PAGE_REQUESTS = new Map();
    const RECORD_PAGE_CONTROLLERS = new Map();
    const ASSET_PAGE_CONTROLLERS = new Map();
    let RECORD_PAGE_CACHE_VERSION = 0;
    let ASSET_PAGE_CACHE_VERSION = 0;
    let ASSET_STUDIO_PRESETS = [];
    let ASSET_STUDIO_PRESET_PAGE = 1;
    let ASSET_STUDIO_PRESET_LOAD_SEQ = 0;
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
    let DASHBOARD_PERSIST_HYDRATED_SCOPE = '';
    let DASHBOARD_PERSIST_HYDRATION_SCOPE = '';
    let DASHBOARD_PERSIST_HYDRATION_READY = null;

    async function hydrateDashboardPersistentCaches() {
      // Standalone login pages do not have a trustworthy scope until a token is entered.
      if (!isDashboardPage() && !AUTH_TOKEN) return;
      await hydrateDashboardPersistStorage();
      const scope = dashboardPersistScope();
      if (DASHBOARD_PERSIST_HYDRATED_SCOPE && DASHBOARD_PERSIST_HYDRATED_SCOPE !== scope) {
        for (const controller of [...RECORD_PAGE_CONTROLLERS.values(), ...ASSET_PAGE_CONTROLLERS.values()]) {
          try { controller.abort(); } catch (_) {}
        }
        RECORD_PAGE_REQUESTS.clear();
        ASSET_PAGE_REQUESTS.clear();
        PICKER_PAGE_CACHE.clear();
        MANAGED_PAGE_CACHE.clear();
        RECORD_PAGE_CACHE.clear();
        ASSET_PAGE_CACHE.clear();
        PICKER_CACHE.tags.data = [];
        PICKER_CACHE.tags.loadedAt = 0;
        GALLERY_PICKER_CACHE.items = [];
        GALLERY_PICKER_CACHE.loaded = false;
      }
      DASHBOARD_PERSIST_HYDRATED_SCOPE = scope;
      for (const entry of listDashboardPersistCache('picker-page')) {
        if (entry.key) PICKER_PAGE_CACHE.set(entry.key, {data: entry.data, loadedAt: Date.now(), request: null});
      }
      for (const entry of listDashboardPersistCache('managed-page')) {
        if (entry.key) MANAGED_PAGE_CACHE.set(entry.key, {data: entry.data, loadedAt: Date.now(), request: null});
      }
      const tags = readDashboardPersistCache('asset-tags', 'all');
      if (Array.isArray(tags)) {
        PICKER_CACHE.tags.data = tags;
        PICKER_CACHE.tags.loadedAt = Date.now();
      }
      const recordPages = listDashboardPersistCache('record-page');
      for (const entry of recordPages) {
        if (entry.key && entry.data?.rows && entry.data?.meta) RECORD_PAGE_CACHE.set(entry.key, Object.assign({}, entry.data, {cachedAt: Date.now(), persistedAt: entry.storedAt || 0}));
      }
      const assetPages = listDashboardPersistCache('asset-page');
      for (const entry of assetPages) {
        if (entry.key && entry.data?.rows && entry.data?.meta) ASSET_PAGE_CACHE.set(entry.key, Object.assign({}, entry.data, {cachedAt: Date.now(), persistedAt: entry.storedAt || 0}));
      }
      const gallery = readDashboardPersistCache('gallery-picker', 'items');
      if (Array.isArray(gallery)) {
        GALLERY_PICKER_CACHE.items = gallery;
        GALLERY_PICKER_CACHE.loaded = true;
      }
    }
    function ensureDashboardPersistentCachesHydrated() {
      // Page handlers can run as soon as the bridge becomes visible. Gate all
      // cache-aware reads on the same promise so a slow CacheStorage restore
      // cannot race the first API request and make a refresh look uncached.
      if (!isDashboardPage() && !AUTH_TOKEN) return Promise.resolve();
      const scope = dashboardPersistScope();
      if (DASHBOARD_PERSIST_HYDRATION_READY && DASHBOARD_PERSIST_HYDRATION_SCOPE === scope) {
        return DASHBOARD_PERSIST_HYDRATION_READY;
      }
      DASHBOARD_PERSIST_HYDRATION_SCOPE = scope;
      const ready = Promise.resolve()
        .then(() => hydrateDashboardPersistentCaches())
        .catch(() => {});
      DASHBOARD_PERSIST_HYDRATION_READY = ready;
      return ready;
    }
    void ensureDashboardPersistentCachesHydrated();

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
    function isAbortError(error) {
      return error?.name === 'AbortError';
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
        } catch (error) {
          if (isAbortError(error) || options.signal?.aborted) {
            finish('cancelled');
            throw error;
          }
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
          const error = new Error(apiErrorMessage(res.status, data));
          error.status = res.status;
          throw error;
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
    function showToast(text, type = 'info', options = {}) {
      const wrap = $('toastWrap');
      if (!wrap || !text) return;
      const node = document.createElement('div');
      node.className = 'toast' + (type === 'ok' ? ' ok' : type === 'bad' ? ' bad' : '');
      node.style.pointerEvents = 'auto';
      const message = document.createElement('span');
      message.textContent = text;
      node.appendChild(message);
      const action = typeof options?.onAction === 'function' ? options.onAction : null;
      let timer = null;
      const remove = () => {
        if (timer) clearTimeout(timer);
        node.remove();
      };
      if (action && String(options.actionText || '').trim()) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'secondary mini toast-action';
        button.textContent = String(options.actionText).trim();
        button.addEventListener('click', () => {
          button.disabled = true;
          remove();
          try {
            const result = action();
            if (result && typeof result.catch === 'function') result.catch(() => {});
          } catch (_) {}
        });
        node.appendChild(button);
      }
      wrap.appendChild(node);
      timer = setTimeout(remove, Math.max(1000, Number(options?.duration) || 2600));
      return {dismiss: remove, element: node};
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
      img.max_image_dimension ??= 16384;
      img.max_image_pixels ??= 64 * 1024 * 1024;
      img.max_animation_frames ??= 8;
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
    function rememberConfigServerSnapshot(value = CONFIG) {
      try {
        CONFIG_SERVER_SNAPSHOT = JSON.parse(JSON.stringify(value && typeof value === 'object' ? value : {}));
      } catch (_) {
        CONFIG_SERVER_SNAPSHOT = {};
      }
      return CONFIG_SERVER_SNAPSHOT;
    }

    function fillForms() {
      IS_FILLING = true;
      try {
        ensureConfig();
        normalizeChannels();
        normalizeAuditChannels();
        const p = CONFIG.permission, img = CONFIG.image;
        syncMetricAlertForm(true);
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
        if ($('maxImageDimension')) $('maxImageDimension').value = img.max_image_dimension;
        if ($('maxImagePixels')) $('maxImagePixels').value = img.max_image_pixels;
        if ($('maxAnimationFrames')) $('maxAnimationFrames').value = img.max_animation_frames;
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
      if ($('maxImageDimension')) CONFIG.image.max_image_dimension = Number($('maxImageDimension').value || 16384);
      if ($('maxImagePixels')) CONFIG.image.max_image_pixels = Number($('maxImagePixels').value || (64 * 1024 * 1024));
      if ($('maxAnimationFrames')) CONFIG.image.max_animation_frames = Number($('maxAnimationFrames').value || 8);
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
