    function cacheImageUrl(path) {
      return `/api/cache-image?path=${encodeURIComponent(path)}`;
    }
    function cacheImagePreviewUrl(path) {
      return `/api/cache-image-preview?path=${encodeURIComponent(path)}`;
    }
    function cacheImageThumbnailUrl(path) {
      return `/api/cache-image-thumbnail?path=${encodeURIComponent(path)}`;
    }
    function protectedMediaScope() {
      return isDashboardPage() ? 'dashboard' : `token:${AUTH_TOKEN || 'anonymous'}`;
    }
    function estimateDataUrlBytes(value) {
      const text = String(value || '');
      const comma = text.indexOf(',');
      if (comma < 0) return text.length;
      return Math.max(0, Math.floor((text.length - comma - 1) * 0.75));
    }
    function clearProtectedMediaCache() {
      PROTECTED_MEDIA_CACHE_VERSION += 1;
      PROTECTED_MEDIA_REQUESTS.clear();
      for (const entry of PROTECTED_MEDIA_CACHE.values()) {
        if (entry?.objectUrl) URL.revokeObjectURL(entry.objectUrl);
      }
      PROTECTED_MEDIA_CACHE.clear();
      PROTECTED_MEDIA_CACHE_BYTES = 0;
      void clearPersistentProtectedMedia();
    }
    function clearGalleryPickerCache() {
      GALLERY_PICKER_CACHE.version += 1;
      GALLERY_PICKER_CACHE.request = null;
      GALLERY_PICKER_CACHE.items = [];
      GALLERY_PICKER_CACHE.loaded = false;
    }
    function normalizeGalleryPickerItems(items) {
      const seen = new Set();
      const result = [];
      for (const item of Array.isArray(items) ? items : []) {
        const path = String(item?.path || '').trim();
        if (!path || seen.has(path)) continue;
        seen.add(path);
        result.push(Object.assign({}, item, {path}));
        if (result.length >= GALLERY_PICKER_CACHE_MAX_ENTRIES) break;
      }
      return result;
    }
    async function loadGalleryPickerItems() {
      if (GALLERY_PICKER_CACHE.loaded) return {success:true, data:{items:GALLERY_PICKER_CACHE.items}};
      if (GALLERY_PICKER_CACHE.request) return GALLERY_PICKER_CACHE.request;
      const version = GALLERY_PICKER_CACHE.version;
      const request = studioApi('/api/studio/gallery?limit=' + GALLERY_PICKER_CACHE_MAX_ENTRIES).then(res => {
        if (res.success && version === GALLERY_PICKER_CACHE.version) {
          GALLERY_PICKER_CACHE.items = normalizeGalleryPickerItems(res.data?.items);
          GALLERY_PICKER_CACHE.loaded = true;
        }
        return res;
      }).finally(() => {
        if (GALLERY_PICKER_CACHE.request === request) GALLERY_PICKER_CACHE.request = null;
      });
      GALLERY_PICKER_CACHE.request = request;
      return request;
    }
    function renderGalleryPage(panel, items, onPick) {
      const grid = panel?.querySelector('.studio-gallery-grid');
      if (!grid) return;
      const offset = Number(panel.dataset.galleryOffset || 0);
      const page = (items || []).slice(offset, offset + PICKER_PAGE_SIZE);
      page.forEach(item => {
        const cell = document.createElement('div');
        cell.className = 'studio-gallery-item';
        cell.innerHTML = `<div class="studio-gallery-thumb"><div class="status" data-media-placeholder>加载缩略图…</div><img data-cache-path="${escapeHtml(item.path || '')}" data-cache-variant="thumbnail" alt=""></div><div class="cap">${escapeHtml(item.prompt || item.model || item.path || '')}</div>`;
        cell.onclick = () => onPick(item, cell);
        grid.appendChild(cell);
      });
      panel.dataset.galleryOffset = String(offset + page.length);
      panel.querySelector('[data-gallery-more]')?.remove();
      if (offset + page.length < (items || []).length) {
        const more = document.createElement('button');
        more.type = 'button'; more.className = 'secondary mini'; more.dataset.galleryMore = 'true';
        more.textContent = `加载更多（${offset + page.length}/${items.length}）`;
        more.onclick = () => { more.remove(); renderGalleryPage(panel, items, onPick); loadProtectedImages(grid); };
        panel.appendChild(more);
      }
      loadProtectedImages(grid);
    }
    function ensureProtectedMediaScope() {
      const scope = protectedMediaScope();
      if (PROTECTED_MEDIA_CACHE_SCOPE && PROTECTED_MEDIA_CACHE_SCOPE !== scope) clearProtectedMediaCache();
      PROTECTED_MEDIA_CACHE_SCOPE = scope;
      if (Date.now() - PROTECTED_MEDIA_LAST_PURGE > 300000) {
        PROTECTED_MEDIA_LAST_PURGE = Date.now();
        void purgePersistentProtectedMedia(scope);
      }
      return scope;
    }
    function protectedMediaKey(path, variant = 'full') {
      const value = String(path || '').trim();
      if (!value) return '';
      return variant === 'thumbnail' ? `thumbnail:${value}` : value;
    }
    function protectedMediaVariant(key) {
      return String(key || '').startsWith('thumbnail:') ? 'thumbnail' : 'full';
    }
    function protectedMediaPath(key) {
      const value = String(key || '');
      return protectedMediaVariant(value) === 'thumbnail' ? value.slice('thumbnail:'.length) : value;
    }
    function protectedMediaPersistentUrl(key) {
      const path = protectedMediaPath(key);
      return protectedMediaVariant(key) === 'thumbnail' ? cacheImageThumbnailUrl(path) : cacheImagePreviewUrl(path);
    }
    function readProtectedMediaMeta() {
      try {
        const value = JSON.parse(safeStorageGet(PROTECTED_MEDIA_PERSIST_META_KEY, '{}'));
        return value && typeof value === 'object' ? value : {};
      } catch (_) {
        return {};
      }
    }
    function writeProtectedMediaMeta(meta) {
      try { safeStorageSet(PROTECTED_MEDIA_PERSIST_META_KEY, JSON.stringify(meta || {})); } catch (_) {}
    }
    function touchPersistentProtectedMedia(key, scope) {
      const meta = readProtectedMediaMeta();
      const item = meta[key];
      if (!item || item.scope !== scope) return;
      item.lastAccessed = Date.now();
      meta[key] = item;
      writeProtectedMediaMeta(meta);
    }
    function mediaPersistentSupported() {
      try { return typeof window.caches?.open === 'function' && typeof window.Response === 'function'; } catch (_) { return false; }
    }
    async function deletePersistentProtectedMedia(key) {
      const meta = readProtectedMediaMeta();
      delete meta[key];
      writeProtectedMediaMeta(meta);
      if (!mediaPersistentSupported()) return;
      try {
        const cache = await caches.open(PROTECTED_MEDIA_PERSIST_CACHE_NAME);
        await cache.delete(protectedMediaPersistentUrl(key));
      } catch (_) {}
    }
    async function clearPersistentProtectedMedia() {
      writeProtectedMediaMeta({});
      if (!mediaPersistentSupported()) return;
      try { await caches.delete(PROTECTED_MEDIA_PERSIST_CACHE_NAME); } catch (_) {}
    }
    async function purgePersistentProtectedMedia(scope = protectedMediaScope()) {
      if (!mediaPersistentSupported()) return;
      const now = Date.now();
      const meta = readProtectedMediaMeta();
      const cache = await caches.open(PROTECTED_MEDIA_PERSIST_CACHE_NAME).catch(() => null);
      if (!cache) return;
      let changed = false;
      for (const [key, item] of Object.entries(meta)) {
        const expired = !item || item.scope !== scope || now - Number(item.lastAccessed || item.cachedAt || 0) > PROTECTED_MEDIA_PERSIST_TTL_MS;
        if (!expired) continue;
        delete meta[key];
        changed = true;
        await cache.delete(protectedMediaPersistentUrl(key)).catch(() => {});
      }
      if (changed) writeProtectedMediaMeta(meta);
    }
    async function readPersistentProtectedMedia(key, scope) {
      if (!mediaPersistentSupported()) return null;
      const meta = readProtectedMediaMeta();
      const now = Date.now();
      try {
        const cache = await caches.open(PROTECTED_MEDIA_PERSIST_CACHE_NAME);
        const response = await cache.match(protectedMediaPersistentUrl(key));
        if (!response) {
          if (meta[key]) await deletePersistentProtectedMedia(key);
          return null;
        }
        const headers = response.headers;
        const item = meta[key] || {
          scope: String(headers.get('x-selfie-cache-scope') || ''),
          cachedAt: Number(headers.get('x-selfie-cache-at') || 0),
          lastAccessed: Number(headers.get('x-selfie-cache-last-accessed') || 0),
        };
        if (item.scope !== scope || now - Number(item.lastAccessed || item.cachedAt || 0) > PROTECTED_MEDIA_PERSIST_TTL_MS) {
          await deletePersistentProtectedMedia(key);
          return null;
        }
        const payload = await response.json();
        if (!payload?.data_url) throw new Error('媒体缓存无效');
        item.lastAccessed = now;
        meta[key] = item;
        writeProtectedMediaMeta(meta);
        return {dataUrl: String(payload.data_url), mimeType: String(payload.mime_type || ''), size: Number(payload.size || 0)};
      } catch (_) {
        await deletePersistentProtectedMedia(key);
        return null;
      }
    }
    async function persistProtectedMedia(key, entry, scope) {
      if (!mediaPersistentSupported() || !entry?.dataUrl) return;
      try {
        const cache = await caches.open(PROTECTED_MEDIA_PERSIST_CACHE_NAME);
        const now = Date.now();
        await cache.put(protectedMediaPersistentUrl(key), new Response(JSON.stringify({
          data_url: entry.dataUrl, mime_type: entry.mimeType || '', size: Number(entry.size || 0)
        }), {headers: {
          'content-type': 'application/json',
          'x-selfie-cache-scope': scope,
          'x-selfie-cache-at': String(now),
          'x-selfie-cache-last-accessed': String(now),
        }}));
        const meta = readProtectedMediaMeta();
        meta[key] = {scope, cachedAt: Number(meta[key]?.cachedAt || now), lastAccessed: now};
        const keys = Object.entries(meta).sort((a, b) => Number(a[1]?.lastAccessed || 0) - Number(b[1]?.lastAccessed || 0));
        while (keys.length > PROTECTED_MEDIA_PERSIST_MAX_ENTRIES) {
          const [oldKey] = keys.shift();
          delete meta[oldKey];
          await cache.delete(protectedMediaPersistentUrl(oldKey)).catch(() => {});
        }
        writeProtectedMediaMeta(meta);
      } catch (_) {}
    }
    function touchProtectedMedia(key, entry) {
      entry.lastUsed = Date.now();
      PROTECTED_MEDIA_CACHE.delete(key);
      PROTECTED_MEDIA_CACHE.set(key, entry);
      return entry;
    }
    function forgetProtectedMedia(key) {
      const entry = PROTECTED_MEDIA_CACHE.get(key);
      if (!entry) return;
      if (entry.objectUrl) URL.revokeObjectURL(entry.objectUrl);
      PROTECTED_MEDIA_CACHE.delete(key);
      PROTECTED_MEDIA_CACHE_BYTES = Math.max(0, PROTECTED_MEDIA_CACHE_BYTES - Number(entry.size || 0));
    }
    function rememberProtectedMedia(key, entry) {
      forgetProtectedMedia(key);
      entry.size = Math.max(0, Number(entry.size || entry.blob?.size || estimateDataUrlBytes(entry.dataUrl)));
      PROTECTED_MEDIA_CACHE.set(key, entry);
      PROTECTED_MEDIA_CACHE_BYTES += entry.size;
      while (
        PROTECTED_MEDIA_CACHE.size > PROTECTED_MEDIA_CACHE_MAX_ENTRIES
        || (PROTECTED_MEDIA_CACHE_BYTES > PROTECTED_MEDIA_CACHE_MAX_BYTES && PROTECTED_MEDIA_CACHE.size > 1)
      ) {
        const oldest = PROTECTED_MEDIA_CACHE.keys().next().value;
        if (!oldest) break;
        forgetProtectedMedia(oldest);
      }
      return entry;
    }
    function invalidateProtectedMediaPath(path) {
      for (const variant of ['full', 'thumbnail']) {
        const key = protectedMediaKey(path, variant);
        if (key) {
          forgetProtectedMedia(key);
          void deletePersistentProtectedMedia(key);
        }
      }
    }
    function protectedMediaUrl(entry) {
      return String(entry?.objectUrl || entry?.dataUrl || '');
    }
    function mediaRetryIconSvg() {
      return '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 0 0-14.8-4L3 10"></path><path d="M3 5v5h5"></path><path d="M4 13a8 8 0 0 0 14.8 4L21 14"></path><path d="M21 19v-5h-5"></path></svg>';
    }
    function bindProtectedMediaRetry(root = document) {
      root?.querySelectorAll?.('[data-media-retry]').forEach(button => {
        if (button.dataset.bound === 'true') return;
        button.dataset.bound = 'true';
        button.onclick = async event => {
          event.preventDefault();
          event.stopPropagation();
          const path = button.getAttribute('data-media-retry') || '';
          const variant = button.getAttribute('data-media-retry-variant') || 'full';
          const frame = button.closest('.studio-gallery-thumb, .asset-thumb');
          const placeholder = frame?.querySelector('[data-media-placeholder]');
          let img = frame?.querySelector('img');
          if (!path || !frame) return;
          button.disabled = true;
          invalidateProtectedMediaPath(path);
          if (!img) {
            img = document.createElement('img');
            img.alt = '';
            const shell = frame.querySelector('.media-zoom-shell');
            (shell || frame).appendChild(img);
          }
          img.hidden = false;
          if (placeholder) {
            placeholder.hidden = false;
            placeholder.textContent = '正在重试…';
          }
          await loadProtectedImage(img, path, variant);
        };
      });
    }
    function showProtectedMediaError(img, path, message, variant = 'full') {
      if (/已清理|不存在|无效/.test(String(message || ''))) invalidateProtectedMediaPath(path);
      const frame = img.closest('.studio-gallery-thumb, .asset-thumb');
      const placeholder = frame?.querySelector('[data-media-placeholder]');
      img.hidden = true;
      if (placeholder) {
        placeholder.hidden = false;
        placeholder.innerHTML = `<span>${escapeHtml(message || '图片加载失败')}</span><button class="secondary mini media-retry" type="button" data-media-retry="${escapeHtml(path)}" data-media-retry-variant="${escapeHtml(variant)}" title="重新加载图片" aria-label="重新加载图片">${mediaRetryIconSvg()}</button>`;
        bindProtectedMediaRetry(frame);
        return;
      }
      if (frame) {
        const div = document.createElement('div');
        div.className = 'status';
        div.innerHTML = `<span>${escapeHtml(message || '图片加载失败')}</span><button class="secondary mini media-retry" type="button" data-media-retry="${escapeHtml(path)}" data-media-retry-variant="${escapeHtml(variant)}" title="重新加载图片" aria-label="重新加载图片">${mediaRetryIconSvg()}</button>`;
        frame.appendChild(div);
        bindProtectedMediaRetry(frame);
        return;
      }
      img.replaceWith(Object.assign(document.createElement('div'), {className: 'status', textContent: message || '图片加载失败'}));
    }
    async function getProtectedMedia(path) {
      return getProtectedMediaVariant(path, 'full');
    }
    async function getFullProtectedMedia(path) {
      const media = await getProtectedMedia(path);
      return media;
    }
    async function getProtectedMediaVariant(path, variant = 'full') {
      const key = protectedMediaKey(path, variant);
      if (!key) throw new Error('媒体路径为空');
      const scope = ensureProtectedMediaScope();
      const cached = PROTECTED_MEDIA_CACHE.get(key);
      if (cached) {
        touchPersistentProtectedMedia(key, scope);
        return touchProtectedMedia(key, cached);
      }
      const pending = PROTECTED_MEDIA_REQUESTS.get(key);
      if (pending) return pending;
      const version = PROTECTED_MEDIA_CACHE_VERSION;
      const request = (async () => {
        let entry;
        const persisted = await readPersistentProtectedMedia(key, scope);
        if (persisted) return rememberProtectedMedia(key, persisted);
        const mediaPath = protectedMediaPath(key);
        const previewUrl = variant === 'thumbnail' ? cacheImageThumbnailUrl(mediaPath) : cacheImagePreviewUrl(mediaPath);
        if (isDashboardPage()) {
          const res = await api(previewUrl);
          const data = res.data || {};
          if (!data.data_url) throw new Error('媒体已清理');
          entry = {
            dataUrl: String(data.data_url),
            mimeType: String(data.mime_type || ''),
            size: Number(data.size || 0),
          };
        } else {
          const res = variant === 'thumbnail'
            ? await fetch(cacheImageThumbnailUrl(mediaPath), {headers: headers()})
            : await fetch(cacheImagePreviewUrl(key), {headers: headers()});
          let data;
          try { data = await res.json(); } catch (_) { data = null; }
          if (!res.ok) {
            const error = new Error(res.status === 404 ? '媒体已清理' : `媒体读取失败 HTTP ${res.status}`);
            error.status = res.status;
            throw error;
          }
          const preview = data?.data || {};
          if (preview.available === false || !preview.data_url) throw new Error(preview.message || '媒体已清理');
          entry = {
            dataUrl: String(preview.data_url),
            mimeType: String(preview.mime_type || ''),
            size: Number(preview.size || 0),
          };
        }
        if (version !== PROTECTED_MEDIA_CACHE_VERSION || scope !== protectedMediaScope()) {
          if (entry.objectUrl) URL.revokeObjectURL(entry.objectUrl);
          return entry;
        }
        void persistProtectedMedia(key, entry, scope);
        return rememberProtectedMedia(key, entry);
      })();
      PROTECTED_MEDIA_REQUESTS.set(key, request);
      try {
        return await request;
      } finally {
        if (PROTECTED_MEDIA_REQUESTS.get(key) === request) PROTECTED_MEDIA_REQUESTS.delete(key);
      }
    }
    async function loadProtectedImage(img, path, variant = 'full') {
      try {
        const media = variant === 'full'
          ? await getFullProtectedMedia(path)
          : await getProtectedMediaVariant(path, variant);
        const url = protectedMediaUrl(media);
        if (!url) throw new Error('图片已清理');
        const thumb = img.closest('.studio-gallery-thumb, .asset-thumb');
        const placeholder = thumb?.querySelector('[data-media-placeholder]');
        img.addEventListener('load', () => {
          if (thumb) thumb.classList.add('loaded');
          if (placeholder) placeholder.hidden = true;
          img.hidden = false;
        }, {once: true});
        img.addEventListener('error', () => {
          showProtectedMediaError(img, path, '图片加载失败', variant);
        }, {once: true});
        img.hidden = false;
        img.src = url;
      } catch (e) {
        showProtectedMediaError(img, path, e.message || '图片已清理', variant);
      }
    }
    function protectedMediaViewportPriority(element) {
      try {
        const rect = element.getBoundingClientRect();
        const viewport = Number(window.innerHeight || 800);
        return rect.bottom >= -80 && rect.top <= viewport + 160 ? 10 : 0;
      } catch (_) {
        return 0;
      }
    }
    function observeProtectedMedia(element, path) {
      return observeProtectedMediaVariant(element, path, 'full');
    }
    function observeProtectedMediaVariant(element, path, variant = 'full') {
      if (!element || !path) return;
      if (typeof window.IntersectionObserver !== 'function') {
        enqueueProtectedMediaLoad(element, path, variant, protectedMediaViewportPriority(element));
        return;
      }
      if (!PROTECTED_MEDIA_OBSERVER) {
        PROTECTED_MEDIA_OBSERVER = new IntersectionObserver(entries => {
          entries.forEach(entry => {
            if (!entry.isIntersecting) return;
            const target = entry.target;
            PROTECTED_MEDIA_OBSERVER?.unobserve(target);
            const deferredPath = target.getAttribute('data-deferred-cache-path') || '';
            const deferredVariant = target.getAttribute('data-deferred-cache-variant') || 'full';
            target.removeAttribute('data-deferred-cache-path');
            target.removeAttribute('data-deferred-cache-variant');
            if (target.isConnected && deferredPath) {
              enqueueProtectedMediaLoad(target, deferredPath, deferredVariant, protectedMediaViewportPriority(target));
            }
          });
        }, {rootMargin: '220px 0px', threshold: 0});
      }
      element.setAttribute('data-deferred-cache-path', path);
      element.setAttribute('data-deferred-cache-variant', variant);
      PROTECTED_MEDIA_OBSERVER.observe(element);
    }
    function drainProtectedMediaLoadQueue() {
      while (PROTECTED_MEDIA_LOAD_QUEUE.length && PROTECTED_MEDIA_LOAD_ACTIVE < PROTECTED_MEDIA_LOAD_CONCURRENCY) {
        const task = PROTECTED_MEDIA_LOAD_QUEUE.shift();
        if (!task || task.element?.isConnected === false) continue;
        PROTECTED_MEDIA_LOAD_ACTIVE += 1;
        Promise.resolve(task.loader()).catch(() => {}).finally(() => {
          PROTECTED_MEDIA_LOAD_ACTIVE = Math.max(0, PROTECTED_MEDIA_LOAD_ACTIVE - 1);
          drainProtectedMediaLoadQueue();
        });
      }
    }
    function pruneProtectedMediaLoadQueue() {
      for (let index = PROTECTED_MEDIA_LOAD_QUEUE.length - 1; index >= 0; index -= 1) {
        if (!PROTECTED_MEDIA_LOAD_QUEUE[index]?.element?.isConnected) PROTECTED_MEDIA_LOAD_QUEUE.splice(index, 1);
      }
    }
    function zoomIconSvg() {
      return '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="6.5"></circle><path d="m16 16 4.5 4.5"></path><path d="M11 8v6M8 11h6"></path></svg>';
    }
    function attachImageLightbox(img, path) {
      if (!img || !path || img.closest('.media-zoom-shell')) return;
      const shell = document.createElement('span');
      shell.className = 'media-zoom-shell';
      img.parentNode?.insertBefore(shell, img);
      shell.appendChild(img);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'media-zoom-button';
      button.title = '放大预览';
      button.setAttribute('aria-label', '放大预览');
      button.innerHTML = zoomIconSvg();
      button.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        openMediaLightbox(path, img.alt || '图片预览');
      });
      shell.appendChild(button);
    }
    function enqueueProtectedMediaLoad(element, path, variant = 'full', priority = 0) {
      PROTECTED_MEDIA_LOAD_QUEUE.push({element, path, variant, priority, loader: () => loadProtectedImage(element, path, variant)});
      PROTECTED_MEDIA_LOAD_QUEUE.sort((a, b) => b.priority - a.priority);
      drainProtectedMediaLoadQueue();
    }
    function loadProtectedImages(root = document) {
      const queued = root?.id === 'assetGrid' || root?.dataset?.protectedMediaQueued === 'true';
      root.querySelectorAll('img[data-cache-path]').forEach(img => {
        const path = img.getAttribute('data-cache-path') || '';
        const variant = img.getAttribute('data-cache-variant') || 'full';
        img.removeAttribute('data-cache-path');
        img.removeAttribute('data-cache-variant');
        if (!path) return;
        attachImageLightbox(img, path);
        if (queued || variant === 'thumbnail') observeProtectedMediaVariant(img, path, variant);
        else loadProtectedImage(img, path, variant);
      });
    }
    function normalizeMediaSources(sources) {
      const list = Array.isArray(sources) ? sources : (sources ? [sources] : []);
      return list.map(item => {
        const isObject = item && typeof item === 'object';
        const value = typeof item === 'string' ? item.trim() : String(item?.value || '').trim();
        const placeholder = isObject && (String(item?.type || '').toLowerCase() === 'base64' || Number.isInteger(item?.index));
        if (!value && !placeholder) return null;
        const rawType = typeof item === 'object' ? String(item?.type || '').trim().toLowerCase() : '';
        const type = rawType === 'base64' || value.toLowerCase().startsWith('data:') || value.toLowerCase().startsWith('base64://') ? 'base64' : (rawType === 'url' || /^https?:\/\//i.test(value) ? 'url' : 'local');
        return {type, value, deferred: isObject && item?.deferred === true, index: isObject && Number.isInteger(item?.index) ? item.index : undefined, size: isObject ? item?.size : undefined};
      }).filter(Boolean);
    }
    function mediaSourceLabel(source) {
      const type = source?.type || 'local';
      return type === 'base64' ? 'Base64' : type === 'url' ? 'URL' : '本地缓存';
    }
    function mediaSourceBadge(source) {
      const normalized = source || {type: 'local'};
      const title = normalized.type === 'base64' ? '原始 Base64' : normalized.type === 'url' ? '原始 URL' : '仅有本地缓存文件';
      return `<span class="media-source-badge ${escapeHtml(normalized.type || 'local')}" title="${title}">${escapeHtml(mediaSourceLabel(normalized))}</span>`;
    }
    function mediaSourceSummary(sources) {
      const list = normalizeMediaSources(sources);
      if (!list.length) return {text: '本地缓存', sources: []};
      const counts = {};
      for (const source of list) counts[source.type] = (counts[source.type] || 0) + 1;
      const text = Object.entries(counts).map(([type, count]) => `${mediaSourceLabel({type})}${count > 1 ? ` ×${count}` : ''}`).join(' / ');
      return {text, sources: list};
    }
    function mediaHead(title, sources, kind = 'image') {
      const summary = mediaSourceSummary(sources);
      const sourceArg = escapeJs(JSON.stringify(summary.sources));
      const copy = summary.sources.length
        ? `<button class="copy-btn source-copy-btn" type="button" title="复制原始来源" aria-label="复制原始来源" onclick="copyMediaSourceValues(${sourceArg},'${escapeJs(kind)}')">${copyIconSvg()}<span class="media-action-label">复制</span></button>`
        : '';
      const badgeType = summary.sources.length && summary.sources.every(source => source.type === summary.sources[0].type) ? summary.sources[0].type : 'local';
      return `<div class="media-head"><h3>${escapeHtml(title)}</h3><span class="media-source-badge ${badgeType}">${escapeHtml(summary.text)}</span>${copy}</div>`;
    }
    function mediaItemSource(sources, index, kind = 'image') {
      const list = normalizeMediaSources(sources);
      const source = list[index] || null;
      return `<div class="media-source-line">${mediaSourceBadge(source || {type: 'local'})}${source ? `<button class="copy-btn source-copy-btn" type="button" title="复制原始来源" aria-label="复制原始来源" onclick="copyMediaSourceValue(${escapeJs(JSON.stringify(source))},'${escapeJs(kind)}')">${copyIconSvg()}<span class="media-action-label">复制</span></button>` : ''}</div>`;
    }
    function loadRecordMediaPreview(button, kind, path) {
      const shell = button && button.closest('.media-preview-shell');
      if (!shell || !path) return;
      shell.innerHTML = '<div class="status">正在加载预览…</div>';
      const img = document.createElement('img');
      img.alt = path;
      shell.innerHTML = '';
      shell.appendChild(img);
      attachImageLightbox(img, path);
      loadProtectedImage(img, path);
    }
    function lazyMediaPreview(kind, path) {
      return `<div class="media-preview-shell"><button class="secondary mini" type="button" onclick="loadRecordMediaPreview(this,'image','${escapeJs(path)}')">加载图片预览</button><span class="muted" style="margin-left:8px">打开详情时不自动下载</span></div>`;
    }
    function imageThumbs(paths, title = '图片', sources = [], lazy = false) {
      const items = (paths || []).filter(Boolean);
      const normalizedSources = normalizeMediaSources(sources);
      if (!items.length) return `${mediaHead(title, normalizedSources, 'image')}<div class="muted">无图片</div>`;
      const grid = `<div class="images">${items.map((path, index) => {
        const name = String(path).split('/').pop() || 'image';
        const preview = lazy ? lazyMediaPreview('image', path) : `<img data-cache-path="${escapeHtml(path)}" alt="${escapeHtml(path)}">`;
        return `<div class="thumb-card"><button class="secondary thumb-dl" type="button" title="下载" aria-label="下载" onclick="downloadCachePath('${escapeJs(path)}','${escapeJs(name)}')">${downloadIconSvg()}</button>${preview}<div class="muted">${escapeHtml(path)}</div>${mediaItemSource(normalizedSources, index, 'image')}</div>`;
      }).join('')}</div>`;
      return `${mediaHead(title, normalizedSources, 'image')}${grid}`;
    }
    function recordUsesPersonaReference(record) {
      const request = record?.request_data && typeof record.request_data === 'object' ? record.request_data : {};
      const selection = request.reference_selection && typeof request.reference_selection === 'object' ? request.reference_selection : {};
      const roles = selection.roles && typeof selection.roles === 'object' ? selection.roles : {};
      return Boolean(
        request.use_selfie_reference
        || Number(request.persona_reference_count || 0) > 0
        || selection.used_persona
        || Number(roles.persona || 0) > 0
        || Number(roles.persona_extra || 0) > 0
        || record?.identity_reference_source === 'persona'
        || Number(record?.persona_reference_count || 0) > 0
      );
    }
    function personaReferenceThumb() {
      return '<div class="thumb-card persona-reference-thumb"><div class="status persona-reference-placeholder">正在加载形象图…</div><img alt="形象图" style="display:none"><div class="muted">形象图</div></div>';
    }
    async function loadSelfieReferenceDataUrl() {
      if (SELFIE_REFERENCE_DATA_URL) return SELFIE_REFERENCE_DATA_URL;
      if (!SELFIE_REFERENCE_REQUEST) {
        SELFIE_REFERENCE_REQUEST = api('/api/selfie-reference').then(res => {
          const image = String(res?.data?.image || '').trim();
          SELFIE_REFERENCE_DATA_URL = image.startsWith('data:image/') ? image : '';
          return SELFIE_REFERENCE_DATA_URL;
        }).catch(() => '').finally(() => {
          SELFIE_REFERENCE_REQUEST = null;
        });
      }
      return SELFIE_REFERENCE_REQUEST;
    }
    async function loadPersonaReferenceThumbs(root = document) {
      const images = root?.querySelectorAll?.('.persona-reference-thumb img') || [];
      if (!images.length) return;
      const src = await loadSelfieReferenceDataUrl();
      images.forEach(img => {
        const card = img.closest('.persona-reference-thumb');
        const placeholder = card?.querySelector('.persona-reference-placeholder');
        if (!src) {
          if (placeholder) placeholder.textContent = '形象图读取失败';
          return;
        }
        img.onload = () => {
          img.style.display = 'block';
          if (placeholder) placeholder.remove();
        };
        img.onerror = () => {
          img.style.display = 'none';
          if (placeholder) placeholder.textContent = '形象图加载失败';
        };
        img.src = src;
      });
    }
    function recordRequestReferences(record) {
      const parts = [];
      if (recordUsesPersonaReference(record)) {
        parts.push(`<div class="detail-title"><h3>形象图</h3></div><div class="images">${personaReferenceThumb()}</div>`);
      }
      const paths = Array.isArray(record?.request_image_paths) ? record.request_image_paths : [];
      if (paths.length) parts.push(imageThumbs(paths, '请求图'));
      if (!parts.length) parts.push(imageThumbs([], '请求图'));
      return parts.join('');
    }
    function downloadIconSvg() {
      return '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"></path><path d="m7 10 5 5 5-5"></path><path d="M5 21h14"></path></svg>';
    }
    function triggerProtectedDownload(entry, name) {
      const url = protectedMediaUrl(entry);
      if (!url) return false;
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      a.remove();
      return true;
    }
    async function downloadStandaloneCacheFile(path, name) {
      const res = await fetch(cacheImageUrl(path), {headers: headers()});
      if (!res.ok) throw new Error(res.status === 404 ? '媒体已清理' : `媒体读取失败 HTTP ${res.status}`);
      const blob = await res.blob();
      if (!blob || !blob.size) throw new Error('下载内容为空');
      const objectUrl = URL.createObjectURL(blob);
      try {
        triggerProtectedDownload({objectUrl}, name);
      } finally {
        setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      }
    }
    async function downloadCachePath(path, filename) {
      const name = String(filename || path || 'download').split('/').pop() || 'download';
      const rel = String(path || '').trim();
      if (!rel) {
        showToast('没有可下载文件', 'bad');
        return;
      }
      try {
        showToast('正在准备下载…', 'ok');
        // Fetch through the shared cache first so a download also serves the
        // record detail preview without a second request.
        try {
          const media = await getProtectedMedia(rel);
          if (triggerProtectedDownload(media, name)) {
            showToast('已开始下载', 'ok');
            return;
          }
          throw new Error('下载内容为空');
        } catch (previewError) {
          if (!isDashboardPage() && previewError?.status === 413) {
            await downloadStandaloneCacheFile(rel, name);
            showToast('已开始下载', 'ok');
            return;
          }
          if (!isDashboardPage()) throw previewError;
          // The dashboard bridge remains a fallback for large files that the
          // preview endpoint intentionally does not inline.
          const bridge = await waitForDashboardBridge();
          if (typeof bridge.ready === 'function') await bridge.ready();
          if (typeof bridge.download !== 'function') throw previewError;
          await bridge.download('cache-image', { path: rel }, name);
          showToast('已开始下载', 'ok');
        }
      } catch (e) {
        showToast(e.message || '下载失败', 'bad');
      }
    }
