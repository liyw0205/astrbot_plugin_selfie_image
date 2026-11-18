    function setPagerLoading(id, loading) {
      const pager = $(id);
      if (!pager) return;
      pager.setAttribute('aria-busy', loading ? 'true' : 'false');
      pager.querySelectorAll('button').forEach(button => {
        if (loading) {
          if (button.dataset.pagerDisabled === undefined) {
            button.dataset.pagerDisabled = button.disabled ? '1' : '0';
          }
          button.disabled = true;
        } else if (button.dataset.pagerDisabled !== undefined) {
          button.disabled = button.dataset.pagerDisabled === '1';
          delete button.dataset.pagerDisabled;
        }
      });
    }

    function rememberPageCache(cache, key, value) {
      value.cachedAt = Date.now();
      cache.delete(key);
      cache.set(key, value);
      const namespace = cache === RECORD_PAGE_CACHE ? 'record-page' : cache === ASSET_PAGE_CACHE ? 'asset-page' : '';
      if (namespace && writeDashboardPersistCache(namespace, key, value)) {
        // Keep the short in-memory TTL separate from the three-day persisted
        // page cache. A failed storage write must not make this entry appear
        // durable, while a successful write can be reused after a refresh.
        value.persistedAt = Date.now();
      }
      while (cache.size > PAGE_CACHE_MAX_ENTRIES) {
        const oldest = cache.keys().next().value;
        if (!oldest) break;
        cache.delete(oldest);
        if (namespace) removeDashboardPersistCache(namespace, oldest);
      }
      return value;
    }
    function restorePersistedPageCache(cache, key) {
      const namespace = cache === RECORD_PAGE_CACHE ? 'record-page' : cache === ASSET_PAGE_CACHE ? 'asset-page' : '';
      if (!namespace) return null;
      const persisted = readDashboardPersistCacheEntry(namespace, key);
      const data = persisted?.data;
      if (!data || !Array.isArray(data.rows) || !data.meta || typeof data.meta !== 'object') return null;
      const value = Object.assign({}, data, {
        cachedAt: Date.now(),
        persistedAt: Number(persisted.storedAt || 0),
      });
      cache.delete(key);
      cache.set(key, value);
      return value;
    }
    function usablePageCache(cache, key) {
      const value = cache.get(key);
      if (!value) return null;
      if (Date.now() - Number(value.cachedAt || 0) > PAGE_CACHE_TTL_MS) {
        const restored = restorePersistedPageCache(cache, key);
        if (!restored) {
          cache.delete(key);
          return null;
        }
        return restored;
      }
      cache.delete(key);
      cache.set(key, value);
      return value;
    }
    function abortPageRequests(requests, controllers, keepPath = '') {
      for (const [path, controller] of controllers.entries()) {
        if (path === keepPath) continue;
        try { controller.abort(); } catch (_) {}
        controllers.delete(path);
        requests.delete(path);
      }
    }
    function clearPageCache(cache, requests, controllers) {
      abortPageRequests(requests, controllers);
      cache.clear();
      requests.clear();
      controllers.clear();
    }
    function requestCachedPage(path, requests, controllers, force = false) {
      if (force) {
        const previous = controllers.get(path);
        if (previous) {
          try { previous.abort(); } catch (_) {}
          controllers.delete(path);
        }
        requests.delete(path);
      }
      const existing = requests.get(path);
      if (existing) return existing;
      const controller = typeof AbortController === 'function' ? new AbortController() : null;
      const request = api(path, controller ? {signal: controller.signal} : {});
      requests.set(path, request);
      if (controller) controllers.set(path, controller);
      const cleanup = () => {
        if (requests.get(path) === request) requests.delete(path);
        if (controllers.get(path) === controller) controllers.delete(path);
      };
      request.then(
        cleanup,
        cleanup
      );
      return request;
    }
    function pagePathAt(path, page, pageSize) {
      const parts = String(path || '').split('?');
      const params = new URLSearchParams(parts[1] || '');
      params.set('offset', String((Math.max(1, page) - 1) * pageSize));
      return `${parts[0]}?${params.toString()}`;
    }
    function recordPagePayload(res) {
      const rows = Array.isArray(res.data) ? res.data : [];
      return {
        rows,
        meta: {
          total: Number(res.total ?? rows.length),
          filtered: Number(res.filtered ?? rows.length),
          offset: Number(res.offset ?? 0),
          limit: Number(res.limit ?? MONITOR_PAGE_SIZE),
          scope_stats: res.scope_stats && typeof res.scope_stats === 'object' ? res.scope_stats : null
        }
      };
    }
    function assetPagePayload(res) {
      const rows = Array.isArray(res.data) ? res.data : [];
      return {
        rows,
        meta: {
          total: Number(res.total ?? rows.length),
          filtered: Number(res.filtered ?? rows.length),
          offset: Number(res.offset ?? 0),
          limit: Number(res.limit ?? ASSET_PAGE_SIZE)
        }
      };
    }
    function applyRecordPage(payload) {
      RECORDS = payload.rows;
      RECORD_META = payload.meta;
    }
    function applyAssetPage(payload) {
      ASSET_RECORDS = payload.rows;
      ASSET_META = payload.meta;
    }
    function prefetchPages(path, page, totalPages, pageSize, cache, requests, controllers, normalize, isValid = () => true, onPage = null) {
      const targets = Array.from({length: PAGE_PREFETCH_AHEAD}, (_, index) => page + index + 1)
        .filter(item => item >= 1 && item <= totalPages);
      targets.forEach(target => {
        const targetPath = pagePathAt(path, target, pageSize);
        const existing = usablePageCache(cache, targetPath);
        if (existing) {
          if (typeof onPage === 'function') onPage(existing);
          return;
        }
        requestCachedPage(targetPath, requests, controllers)
          .then(res => {
            if (!isValid()) return;
            const payload = rememberPageCache(cache, targetPath, normalize(res));
            if (typeof onPage === 'function') onPage(payload);
          })
          .catch(() => {});
      });
    }
    function clearRecordPageCache() {
      RECORD_PAGE_CACHE_VERSION += 1;
      clearPageCache(RECORD_PAGE_CACHE, RECORD_PAGE_REQUESTS, RECORD_PAGE_CONTROLLERS);
      clearDashboardPersistNamespace('record-page');
    }
    function clearAssetPageCache() {
      ASSET_PAGE_CACHE_VERSION += 1;
      clearPageCache(ASSET_PAGE_CACHE, ASSET_PAGE_REQUESTS, ASSET_PAGE_CONTROLLERS);
      clearDashboardPersistNamespace('asset-page');
    }

    async function loadRecords(showRefreshToast = false) {
      await ensureDashboardPersistentCachesHydrated();
      if (showRefreshToast) clearGalleryPickerCache();
      const requestSeq = ++RECORD_LOAD_SEQ;
      const cacheVersion = RECORD_PAGE_CACHE_VERSION;
      const requestPath = monitorQueryPath(MONITOR_PAGE);
      abortPageRequests(RECORD_PAGE_REQUESTS, RECORD_PAGE_CONTROLLERS, requestPath);
      const cached = showRefreshToast ? null : usablePageCache(RECORD_PAGE_CACHE, requestPath);
      if (showRefreshToast) RECORD_PAGE_CACHE.delete(requestPath);
      if (showRefreshToast) removeDashboardPersistCache('record-page', requestPath);
      if (cached) {
        applyRecordPage(cached);
        RECORD_LOADING = false;
        setPagerLoading('monitorPager', false);
        renderRecords();
        const cachedPages = Math.max(1, Math.ceil((cached.meta.filtered || 0) / MONITOR_PAGE_SIZE));
        prefetchPages(requestPath, MONITOR_PAGE, cachedPages, MONITOR_PAGE_SIZE, RECORD_PAGE_CACHE, RECORD_PAGE_REQUESTS, RECORD_PAGE_CONTROLLERS, recordPagePayload, () => cacheVersion === RECORD_PAGE_CACHE_VERSION);
        return;
      } else {
        RECORD_LOADING = true;
        setPagerLoading('monitorPager', true);
      }
      try {
        // The cached equivalent of api(monitorQueryPath(MONITOR_PAGE)).
        const res = await requestCachedPage(requestPath, RECORD_PAGE_REQUESTS, RECORD_PAGE_CONTROLLERS, showRefreshToast);
        if (requestSeq !== RECORD_LOAD_SEQ) return;
        if (cacheVersion !== RECORD_PAGE_CACHE_VERSION) return;
        const payload = recordPagePayload(res);
        rememberPageCache(RECORD_PAGE_CACHE, requestPath, payload);
        applyRecordPage(payload);
        const totalPages = Math.max(1, Math.ceil((RECORD_META.filtered || 0) / MONITOR_PAGE_SIZE));
        if (!RECORDS.length && RECORD_META.filtered > 0 && MONITOR_PAGE > totalPages) {
          MONITOR_PAGE = totalPages;
          return await loadRecords(showRefreshToast);
        }
        RECORD_LOADING = false;
        renderRecords();
        prefetchPages(requestPath, MONITOR_PAGE, totalPages, MONITOR_PAGE_SIZE, RECORD_PAGE_CACHE, RECORD_PAGE_REQUESTS, RECORD_PAGE_CONTROLLERS, recordPagePayload, () => cacheVersion === RECORD_PAGE_CACHE_VERSION);
        if (showRefreshToast) showToast('记录已刷新', 'ok');
      } catch (e) {
        if (isAbortError(e)) return;
        if (requestSeq === RECORD_LOAD_SEQ && !cached) $('monitorStats').textContent = e.message;
      } finally {
        if (requestSeq === RECORD_LOAD_SEQ) {
          RECORD_LOADING = false;
          setPagerLoading('monitorPager', false);
        }
      }
    }
    async function clearRecords() {
      if (!await confirmAction('确定清空全部生成记录吗？此操作不可撤销。', {title:'清空生成记录', confirmText:'清空记录'})) return;
      try {
        await api('/api/records/clear', {method:'POST', body:'{}'});
        clearRecordPageCache();
        clearAssetPageCache();
        clearGalleryPickerCache();
        clearProtectedMediaCache();
        MONITOR_PAGE = 1;
        RECORDS = [];
        RECORD_META = {total: 0, filtered: 0, offset: 0, limit: MONITOR_PAGE_SIZE};
        renderRecords();
        await loadMetrics();
        showToast('记录已清空', 'ok');
      } catch (e) { $('monitorStats').textContent = e.message; }
    }
    function setMonitorSourceOptions(values) {
      const list = $('monitorSourceList');
      list.innerHTML = '';
      for (const value of values) {
        const opt = document.createElement('option');
        opt.value = value;
        list.appendChild(opt);
      }
    }
    function monitorSourceText(record) {
      return [
        displaySourceLabel(record),
        record.source || '',
        record.group_id || '',
        record.user_id || ''
      ].join(' ');
    }
    function setMonitorPage(page) {
      MONITOR_PAGE = Math.max(1, Math.trunc(Number(page) || 1));
      void loadRecords(false);
    }
    function monitorFilterChanged() {
      MONITOR_PAGE = 1;
      RECORD_LOAD_SEQ += 1;
      abortPageRequests(RECORD_PAGE_REQUESTS, RECORD_PAGE_CONTROLLERS);
      clearTimeout(MONITOR_LOAD_TIMER);
      MONITOR_LOAD_TIMER = setTimeout(() => loadRecords(false), 260);
    }
    function formatRecordElapsed(record) {
      const attempts = record.attempts || record.response_data?.attempts || [];
      if (record.timeout || record.error_category === 'timeout' || attempts.some(attempt => attempt?.timeout || attempt?.error_category === 'timeout')) return '超时';
      const seconds = Number(record.elapsed_seconds);
      return Number.isFinite(seconds) && seconds >= 0 ? `${seconds.toFixed(2)}秒` : '空';
    }
    function recordLifecycleStatus(record) {
      const generation = record.generation_success ?? record.success;
      if (generation !== true) return {label: generation === false ? '生成失败' : '未记录', tone: 'gray'};
      if (record.delivery_unknown === true) return {label: '回执未知', tone: 'warning'};
      if (record.delivery_failed === true || record.delivery_success === false) return {label: '发送失败', tone: 'warning'};
      if (record.delivery_success === true) return {label: '成功', tone: 'green'};
      return {label: '待确认', tone: 'gray'};
    }
    function recordDetailValue(value, fallback = '') {
      if (value === undefined || value === null || value === '') return fallback;
      if (typeof value === 'boolean') return value ? '是' : '否';
      return String(value);
    }
    function recordParameterBlock(record) {
      const r = record || {};
      const rd = r.request_data && typeof r.request_data === 'object' ? r.request_data : {};
      const composition = rd.composition && typeof rd.composition === 'object' ? rd.composition : {};
      const rows = [];
      const add = (label, value, fallback = '') => {
        const text = recordDetailValue(value, fallback);
        if (text && text !== '-') rows.push(`<div><label>${escapeHtml(label)}</label><div class="status">${escapeHtml(text)}</div></div>`);
      };
      const requested = rd.requested_count ?? rd.count ?? r.requested_count ?? r.count;
      const actual = r.count ?? r.succeeded_count ?? '';
      add('请求张数', requested);
      if (actual !== '' && String(actual) !== String(requested)) add('实际生成张数', actual);
      add('画面比例', rd.aspect_ratio || composition.aspect_ratio);
      add('清晰度', rd.resolution || composition.resolution);
      add('全局超时', rd.timeout_seconds !== undefined && rd.timeout_seconds !== null && rd.timeout_seconds !== '' ? `${rd.timeout_seconds} 秒` : '');
      add('参考图（实际）', rd.reference_image_count ?? r.reference_images);
      add('参考图（原始）', rd.raw_reference_image_count);
      add('渠道', rd.channel);
      add('模型', rd.model || r.used_model);
      const response = r.response_data && typeof r.response_data === 'object' ? r.response_data : {};
      const retryCount = r.retry_count ?? response.retry_count;
      add('模型重试次数', retryCount !== undefined && retryCount !== null && retryCount !== '' ? `${retryCount} 次` : '');
      add('记录状态', r.status || response.status);
      if (r.delivery_success === false || response.delivery_success === false) {
        add('发送失败原因', r.delivery_error || response.delivery_error || r.error);
      }
      if (response.stage === 'output_audit' || response.blocked_images_retained === true) {
        add('审核结果', response.blocked_images_retained === true ? '输出审核未通过（已保留拦截图）' : '输出审核未通过');
      }
      if (rd.prompt_en && typeof rd.prompt_en === 'object') {
        add('英文提示词', rd.prompt_en.applied ? '已翻译并发送' : (rd.prompt_en.enabled ? '已启用但未应用' : '未启用'));
      }
      if (rd.image_to_text && typeof rd.image_to_text === 'object') {
        add('参考图转文字', rd.image_to_text.applied ? '已应用描述' : (rd.image_to_text.enabled ? '已启用但未应用' : '未启用'));
      }
      add('构图策略', composition.strategy);
      add('提示词指纹', composition.prompt_hash);
      const cosLabels = {cos: 'COS 套装', cos_pose: 'COS 姿势', cos_scene: 'COS 场景', cos_framing: 'COS 镜头', cos_composition: 'COS 构图', cos_view: 'COS 视角'};
      Object.entries(cosLabels).forEach(([key, label]) => add(label, r[key] || rd[key]));
      if (!rows.length) return '';
      return `<div class="detail-title"><h3>请求参数与随机结果</h3></div><div class="grid">${rows.join('')}</div>`;
    }
    function recordModelLabel(record) {
      return String(record.label || record.model || record.target || record.used_model || '').trim() || '未记录模型';
    }
    function recordFailureReason(record) {
      const direct = String(record.failure_reason || record.error || '').trim();
      if (direct) return direct;
      const rows = record.failure_reasons || [];
      if (Array.isArray(rows) && rows.length) {
        const last = rows[rows.length - 1] || {};
        const label = String(last.label || '').trim();
        const reason = String(last.error_user_message || last.error || '').trim();
        if (label && reason) return `${label}: ${reason}`;
        return reason || label;
      }
      const attempts = record.attempts || record.response_data?.attempts || [];
      const failed = (Array.isArray(attempts) ? attempts : []).filter(a => a && a.success === false);
      if (failed.length) {
        const last = failed[failed.length - 1];
        const label = recordModelLabel(last);
        const reason = String(last.error_user_message || last.error || '').trim();
        if (label && reason) return `${label}: ${reason}`;
        return reason || label;
      }
      return '';
    }
    function recordFinalModelBadge(record) {
      const failed = record.success === false;
      const model = String(record.used_model || '').trim();
      if (failed) {
        const reason = recordFailureReason(record);
        const text = model || reason || '失败';
        const title = reason || text;
        return `<span class="model-attempt-badge model-attempt-fail" title="${escapeHtml(title)}">${escapeHtml(text)}</span>`;
      }
      if (!model) return '<span class="model-attempt-empty">空</span>';
      return `<span class="model-attempt-badge model-attempt-ok">${escapeHtml(model)}</span>`;
    }
    function recordRetryModelChain(record) {
      const attempts = record.attempts || record.response_data?.attempts || [];
      const rows = Array.isArray(attempts) ? attempts.slice() : [];
      if (!rows.length && record.used_model) {
        rows.push({ model: record.used_model, success: record.success });
      }
      if (!rows.length) return '<span class="model-attempt-empty">空</span>';
      return `<div class="model-retry-chain">${rows.map((attempt, index) => {
        const label = recordModelLabel(attempt);
        const failed = attempt.success === false || attempt.timeout || attempt.error_category === 'timeout';
        const arrow = index ? '<span class="model-retry-arrow">→</span>' : '';
        return `${arrow}<span class="model-attempt-badge ${failed ? 'model-attempt-fail' : 'model-attempt-ok'}">${escapeHtml(label)}</span>`;
      }).join('')}</div>`;
    }
    function recordFailureReasonsBlock(record) {
      let rows = Array.isArray(record.failure_reasons) ? record.failure_reasons : [];
      if (!rows.length) {
        const attempts = record.attempts || record.response_data?.attempts || [];
        rows = (Array.isArray(attempts) ? attempts : [])
          .filter(a => a && a.success === false)
          .map(a => ({
            label: recordModelLabel(a),
            error: a.error || '',
            error_user_message: a.error_user_message || '',
            error_category: a.error_category || '',
            elapsed_seconds: a.elapsed_seconds,
          }));
      }
      if (!rows.length) {
        // Terminal failure with no attempt rows: single final reason only.
        if (record.success === false) {
          const reason = recordFailureReason(record);
          if (!reason) return '';
          return `<h3>失败原因</h3><div class="status">${escapeHtml(reason)}</div>`;
        }
        return '';
      }
      const finalFailed = record.success === false;
      const title = finalFailed ? '各模型失败原因' : '过程中失败（最终成功）';
      const body = rows.map((row, index) => {
        const label = String(row.label || `尝试${index + 1}`).trim();
        const reason = String(row.error_user_message || row.error || '生成失败').trim();
        const raw = String(row.error || '').trim();
        const cat = String(row.error_category || '').trim();
        const elapsed = Number(row.elapsed_seconds);
        const meta = [
          cat ? `类型:${cat}` : '',
          Number.isFinite(elapsed) ? `${elapsed.toFixed(2)}s` : '',
        ].filter(Boolean).join(' · ');
        const rawLine = raw && raw !== reason ? `<div class="muted" style="margin-top:4px;overflow-wrap:anywhere">${escapeHtml(raw)}</div>` : '';
        return `<div class="model-row" style="grid-template-columns:1fr">
          <div><strong>${escapeHtml(label)}</strong>${meta ? ` <span class="muted">· ${escapeHtml(meta)}</span>` : ''}</div>
          <div style="margin-top:4px">${escapeHtml(reason)}</div>
          ${rawLine}
        </div>`;
      }).join('');
      return `<h3>${title}</h3><div class="model-list">${body}</div>`;
    }
    function renderRecords() {
      const model = $('monitorModel').value.trim();
      const sourceOptions = uniq(RECORDS.map(r => displaySourceLabel(r).trim()).filter(Boolean));
      const modelOptions = uniq(RECORDS.map(r => String(r.used_model || '').trim()).filter(Boolean));
      if (model && !modelOptions.includes(model)) modelOptions.unshift(model);
      setMonitorSourceOptions(sourceOptions);
      setSelectOptions('monitorModel', [''].concat(modelOptions), model);
      const assetModel = String($('assetModel')?.value || '').trim();
      setSelectOptions('assetModel', [''].concat(modelOptions), assetModel);
      const rows = RECORDS;
      const filteredCount = Number(RECORD_META.filtered ?? rows.length);
      const totalCount = Number(RECORD_META.total ?? rows.length);
      const scopeStats = RECORD_META.scope_stats && typeof RECORD_META.scope_stats === 'object'
        ? RECORD_META.scope_stats : null;
      const scopeRows = rows;
      const ok = scopeStats ? Number(scopeStats.success_count || 0) : scopeRows.filter(r=>r.success).length;
      const fail = scopeStats ? Number(scopeStats.failure_count || 0) : scopeRows.length - ok;
      const sampleCount = scopeStats ? Number(scopeStats.sample_count || 0) : scopeRows.length;
      const avg = scopeStats && scopeStats.average_elapsed_seconds != null
        ? Number(scopeStats.average_elapsed_seconds)
        : (scopeRows.length ? scopeRows.reduce((s,r)=>s+Number(r.elapsed_seconds||0),0)/scopeRows.length : 0);
      const totalPages = Math.max(1, Math.ceil(filteredCount / MONITOR_PAGE_SIZE));
      MONITOR_PAGE = Math.min(Math.max(1, MONITOR_PAGE), totalPages);
      const start = Number(RECORD_META.offset ?? ((MONITOR_PAGE - 1) * MONITOR_PAGE_SIZE));
      const pageRows = rows;
      const denom = Math.max(1, sampleCount);
      const okPct = scopeStats && scopeStats.success_rate != null ? Number(scopeStats.success_rate) : (sampleCount ? Math.round((ok / denom) * 1000) / 10 : 0);
      const failPct = scopeStats && scopeStats.failure_rate != null ? Number(scopeStats.failure_rate) : (sampleCount ? Math.round((fail / denom) * 1000) / 10 : 0);
      const scopeLabel = scopeStats ? '筛选全集' : '当前页';
      const successLabel = sampleCount ? `${ok}（${okPct}%）` : '暂无样本';
      const failureLabel = sampleCount ? `${fail}（${failPct}%）` : '暂无样本';
      const averageLabel = sampleCount ? `${avg.toFixed(2)}s` : '暂无样本';
      $('monitorStats').innerHTML = [
        ['筛选', String(filteredCount)],
        ['总计', String(totalCount)],
        ['统计范围', sampleCount ? `${scopeLabel}（${sampleCount} 条）` : '暂无样本'],
        ['成功', successLabel],
        ['失败', failureLabel],
        ['平均耗时', averageLabel],
        ['页码', `${MONITOR_PAGE}/${totalPages}`],
      ].map(([k,v]) => `<div class="stat-card"><div class="k">${k}</div><div class="v">${escapeHtml(String(v))}</div></div>`).join('');
      $('recordTable').innerHTML = '<thead><tr><th></th><th>编号</th><th>时间</th><th>类型</th><th>来源</th><th>状态</th><th>用时</th><th>模型/失败原因</th></tr></thead><tbody>' +
        (pageRows.length ? pageRows.map(r => {
          const id = String(r.id || '');
          const lifecycle = recordLifecycleStatus(r);
          return `<tr class="record-detail-row" data-record-id="${escapeHtml(id)}" style="cursor:pointer" title="点击查看详情"><td><input type="checkbox" data-record-select="${escapeHtml(id)}" aria-label="选择记录 ${escapeHtml(id)}" ${RECORD_SELECTION.has(id) ? 'checked' : ''}></td><td class="mono">${escapeHtml(r.image_code || '')}</td><td>${escapeHtml(r.time||'')}</td><td>图片</td><td>${escapeHtml(displaySourceLabel(r))}</td><td>${escapeHtml(lifecycle.label)}</td><td>${escapeHtml(formatRecordElapsed(r))}</td><td>${recordFinalModelBadge(r)}</td></tr>`;
        }).join('') : '<tr><td colspan="8" class="muted">没有匹配的监控记录</td></tr>') +
        '</tbody>';
      $('recordTable').querySelectorAll('input[data-record-select]').forEach(input => input.onchange = event => {
        event.stopPropagation();
        const id = String(input.getAttribute('data-record-select') || '').trim();
        if (input.checked) RECORD_SELECTION.add(id); else RECORD_SELECTION.delete(id);
        updateRecordSelectionStatus();
      });
      $('recordTable').querySelectorAll('.record-detail-row').forEach(row => row.onclick = event => {
        if (!event.target.closest('input,button')) openRecordDetail(row.dataset.recordId || '');
      });
      updateRecordSelectionStatus();
      $('monitorPager').innerHTML = `
        <button class="secondary mini" type="button" onclick="setMonitorPage(1)" ${MONITOR_PAGE <= 1 || RECORD_LOADING ? 'disabled' : ''}>首页</button>
        <button class="secondary mini" type="button" onclick="setMonitorPage(${MONITOR_PAGE - 1})" ${MONITOR_PAGE <= 1 || RECORD_LOADING ? 'disabled' : ''}>上一页</button>
        <span class="pill gray">第 ${MONITOR_PAGE}/${totalPages} 页 · 每页 ${MONITOR_PAGE_SIZE} 条 · 显示 ${pageRows.length ? start + 1 : 0}-${start + pageRows.length} / 共 ${filteredCount} 条</span>
        <button class="secondary mini" type="button" onclick="setMonitorPage(${MONITOR_PAGE + 1})" ${MONITOR_PAGE >= totalPages || RECORD_LOADING ? 'disabled' : ''}>下一页</button>
        <button class="secondary mini" type="button" onclick="setMonitorPage(${totalPages})" ${MONITOR_PAGE >= totalPages || RECORD_LOADING ? 'disabled' : ''}>末页</button>
      `;
    }

    function selectedRecordIds() { return Array.from(RECORD_SELECTION).filter(Boolean); }
    function updateRecordSelectionStatus() {
      const total = selectedRecordIds().length;
      if ($('recordSelectionStatus')) $('recordSelectionStatus').textContent = total ? `已选择 ${total} 条记录` : '未选择记录';
    }
    function selectVisibleRecords() {
      document.querySelectorAll('#recordTable input[data-record-select]').forEach(input => {
        const id = String(input.getAttribute('data-record-select') || '').trim();
        if (id) RECORD_SELECTION.add(id);
        input.checked = true;
      });
      updateRecordSelectionStatus();
    }
    function clearRecordSelection() {
      RECORD_SELECTION.clear();
      document.querySelectorAll('#recordTable input[data-record-select]').forEach(input => { input.checked = false; });
      updateRecordSelectionStatus();
    }
    function closeCompareModal() { $('compareModal')?.classList.remove('show'); CURRENT_COMPARE = null; }
    function comparisonMediaMarkup(item) {
      const images = Array.isArray(item.image_paths) ? item.image_paths : [];
      return images.length ? imageThumbs(images, '对比图') : '<div class="status">没有可预览的媒体</div>';
    }
    async function compareSelectedRecords() {
      const ids = selectedRecordIds();
      if (ids.length < 2) return showToast('至少选择两条记录', 'bad');
      const body = $('compareBody');
      if (!body) return;
      body.innerHTML = '<div class="status">正在加载对比结果…</div>';
      $('compareModal')?.classList.add('show');
      try {
        const res = await api('/api/records/compare', {method:'POST', body: JSON.stringify({ids, limit: 8})});
        CURRENT_COMPARE = res.data || {};
        const rows = Array.isArray(CURRENT_COMPARE.records) ? CURRENT_COMPARE.records : [];
        body.innerHTML = rows.length ? `<div class="muted" style="margin-bottom:10px">共同提示词：${escapeHtml(CURRENT_COMPARE.common_prompt || '未记录')}</div><div class="comparison-grid">${rows.map(item => `<article class="comparison-card"><div class="between"><b>${escapeHtml(item.model || '未记录模型')}</b><span class="pill ${item.success ? 'green' : 'gray'}">${item.success ? '成功' : '失败'}</span></div><div class="muted">${escapeHtml(item.channel || '未记录渠道')} · ${escapeHtml(item.elapsed_seconds == null ? '-' : String(item.elapsed_seconds))}s</div>${comparisonMediaMarkup(item)}<div class="actions"><button class="secondary mini" type="button" data-compare-reuse="${escapeHtml(item.id)}">复用参数</button><button class="secondary mini" type="button" data-compare-favorite="${escapeHtml(item.id)}">收藏最佳</button></div></article>`).join('')}</div>` : '<div class="status">没有可对比的记录</div>';
        body.querySelectorAll('[data-compare-reuse]').forEach(btn => btn.onclick = () => reuseRecordPromptById(btn.dataset.compareReuse || ''));
        body.querySelectorAll('[data-compare-favorite]').forEach(btn => btn.onclick = () => markRecordFavoriteById(btn.dataset.compareFavorite || ''));
        loadProtectedImages(body);
      } catch (e) { body.innerHTML = `<div class="status">${escapeHtml(e.message || '对比失败')}</div>`; }
    }
    async function reuseRecordPromptById(id) {
      try { const res = await api('/api/records/' + encodeURIComponent(id) + '/reuse'); await copyTextToClipboard(String(res.data?.prompt || '')); showToast('提示词已复制', 'ok'); }
      catch (e) { showToast(e.message || '复用失败', 'bad'); }
    }
    async function markRecordFavoriteById(id) {
      try {
        await api('/api/records/' + encodeURIComponent(id) + '/metadata', {method:'POST', body: JSON.stringify({favorite:true})});
        clearRecordPageCache();
        clearAssetPageCache();
        showToast('已收藏该结果', 'ok');
        await loadRecords(false);
      }
      catch (e) { showToast(e.message || '收藏失败', 'bad'); }
    }

    function assetQueryPath(page = ASSET_PAGE) {
      const params = new URLSearchParams();
      const fields = [
        ['q', 'assetKeyword'], ['source', 'assetSource'], ['model', 'assetModel'],
        ['media_type', 'assetMedia'], ['success', 'assetSuccess'], ['tag', 'assetTag'],
        ['sort', 'assetSort'], ['start_time', 'assetStartTime'], ['end_time', 'assetEndTime']
      ];
      fields.forEach(([name, id]) => {
        const value = String($(id)?.value || '').trim();
        if (value) params.set(name, value);
      });
      const mark = String($('assetMark')?.value || '').trim();
      if (mark === 'favorite' || mark === 'pinned') params.set(mark, 'true');
      params.set('limit', String(ASSET_PAGE_SIZE));
      params.set('offset', String((Math.max(1, page) - 1) * ASSET_PAGE_SIZE));
      return '/api/assets?' + params.toString();
    }
    function assetPrimaryPath(record) {
      const images = Array.isArray(record?.generated_image_paths) ? record.generated_image_paths : [];
      return {
        kind: 'image',
        path: String(images[0] || '').trim(),
        count: images.length
      };
    }
    function assetTagPills(record) {
      const tags = Array.isArray(record?.tags) ? record.tags.filter(Boolean).slice(0, 4) : [];
      return tags.map(tag => `<span class="pill gray">${escapeHtml(tag)}</span>`).join('');
    }
    function setupAssetGridInteractions() {
      const grid = $('assetGrid');
      if (!grid || grid.dataset.bound === '1') return;
      grid.dataset.bound = '1';
      grid.addEventListener('change', event => {
        const input = event.target.closest('input[data-asset-select]');
        if (!input || !grid.contains(input)) return;
        toggleAssetSelection(input.getAttribute('data-asset-select') || '', input.checked);
      });
      grid.addEventListener('click', async event => {
        const button = event.target.closest('button[data-asset-action]');
        if (button && grid.contains(button)) {
          event.preventDefault();
          event.stopPropagation();
          if (button.disabled) return;
          const action = button.getAttribute('data-asset-action') || '';
          const recordId = button.getAttribute('data-record-id') || '';
          const path = button.getAttribute('data-asset-path') || '';
          const name = button.getAttribute('data-asset-name') || '';
          button.disabled = true;
          button.classList.add('busy');
          try {
            if (action === 'detail') {
              await openRecordDetail(recordId);
            } else if (action === 'studio') {
              await addAssetToStudio(recordId);
            } else if (action === 'download') {
              await downloadCachePath(path, name);
            }
          } finally {
            button.disabled = false;
            button.classList.remove('busy');
          }
          return;
        }
        const card = event.target.closest('.asset-card[data-record-id]');
        if (card && grid.contains(card) && !event.target.closest('input,button')) {
          event.preventDefault();
          await openRecordDetail(card.getAttribute('data-record-id') || '');
        }
      });
    }
    function renderAssets() {
      const rows = Array.isArray(ASSET_RECORDS) ? ASSET_RECORDS : [];
      const filtered = Number(ASSET_META.filtered ?? rows.length);
      const total = Number(ASSET_META.total ?? rows.length);
      const totalPages = Math.max(1, Math.ceil(filtered / ASSET_PAGE_SIZE));
      ASSET_PAGE = Math.min(Math.max(1, ASSET_PAGE), totalPages);
      const start = Number(ASSET_META.offset ?? ((ASSET_PAGE - 1) * ASSET_PAGE_SIZE));
      const selectedVisible = rows.filter(row => ASSET_SELECTION.has(String(row.id || ''))).length;
      if ($('assetSelectionStatus')) $('assetSelectionStatus').textContent = selectedVisible
        ? `已选择 ${selectedVisible} 项（当前页）`
        : '当前页未选择资产';
      if ($('assetStats')) {
        const images = rows.length;
        const favorites = rows.filter(row => row.favorite).length;
        const pinned = rows.filter(row => row.pinned).length;
        $('assetStats').innerHTML = [
          ['筛选结果', filtered], ['记录总数', total], ['本页图片', images],
          ['本页收藏', favorites], ['本页置顶', pinned]
        ].map(([key, value]) => `<div class="stat-card"><div class="k">${escapeHtml(key)}</div><div class="v">${escapeHtml(String(value))}</div></div>`).join('');
      }
      if ($('assetGrid')) {
        setupAssetGridInteractions();
        $('assetGrid').innerHTML = rows.length ? rows.map(record => {
          const id = String(record.id || '');
          const media = assetPrimaryPath(record);
          const name = media.path.split('/').pop() || '图片';
          const prompt = String(record.original_prompt || record.request_prompt || '').trim();
          const lifecycle = recordLifecycleStatus(record);
          const thumb = media.path
            ? `<div class="status" data-media-placeholder>加载缩略图…</div><img data-cache-path="${escapeHtml(media.path)}" data-cache-variant="thumbnail" alt="${escapeHtml(name)}">${media.count > 1 ? `<span class="asset-count-label">图片 ×${media.count}</span>` : ''}`
            : '<div class="status">没有缓存媒体</div>';
          const flags = `${record.pinned ? '<span>置顶</span>' : ''}${record.favorite ? '<span>收藏</span>' : ''}`;
          return `<article class="asset-card" data-record-id="${escapeHtml(id)}">
            <div class="asset-thumb">
              <input class="asset-select" type="checkbox" ${ASSET_SELECTION.has(id) ? 'checked' : ''} aria-label="选择资产" data-asset-select="${escapeHtml(id)}">
              ${flags ? `<div class="asset-flag">${flags}</div>` : ''}
              ${thumb}
            </div>
            <div class="asset-card-body">
              <div class="asset-card-title"><b title="${escapeHtml(name)}">${escapeHtml(name)}</b><span class="pill ${lifecycle.tone}">${escapeHtml(lifecycle.label)}</span></div>
              <div class="muted">${escapeHtml(record.time || '')} · ${escapeHtml(record.used_model || '未记录模型')}</div>
              <div class="asset-card-prompt">${escapeHtml(prompt || record.note || '没有提示词或备注')}</div>
              <div class="asset-card-meta">${assetTagPills(record)}</div>
              <div class="asset-card-actions">
                ${media.path ? `<button class="secondary asset-action" type="button" title="下载" aria-label="下载" data-asset-action="download" data-record-id="${escapeHtml(id)}" data-asset-path="${escapeHtml(media.path)}" data-asset-name="${escapeHtml(name)}">${downloadIconSvg()}<span class="asset-action-label">下载</span></button>` : ''}
                ${media.kind === 'image' && media.path ? `<button class="secondary mini asset-action" type="button" data-asset-action="studio" data-record-id="${escapeHtml(id)}">加入画布</button>` : ''}
                <button class="secondary asset-action" type="button" data-asset-action="detail" data-record-id="${escapeHtml(id)}">详情</button>
              </div>
            </div>
          </article>`;
        }).join('') : '<div class="asset-empty muted">没有匹配的资产</div>';
        pruneProtectedMediaLoadQueue();
        loadProtectedImages($('assetGrid'));
      }
      if ($('assetPager')) {
        $('assetPager').innerHTML = `
          <button class="secondary mini" type="button" onclick="setAssetPage(1)" ${ASSET_PAGE <= 1 || ASSET_LOADING ? 'disabled' : ''}>首页</button>
          <button class="secondary mini" type="button" onclick="setAssetPage(${ASSET_PAGE - 1})" ${ASSET_PAGE <= 1 || ASSET_LOADING ? 'disabled' : ''}>上一页</button>
          <span class="pill gray">第 ${ASSET_PAGE}/${totalPages} 页 · 每页 ${ASSET_PAGE_SIZE} 条 · 显示 ${rows.length ? start + 1 : 0}-${start + rows.length} / 共 ${filtered} 条</span>
          <button class="secondary mini" type="button" onclick="setAssetPage(${ASSET_PAGE + 1})" ${ASSET_PAGE >= totalPages || ASSET_LOADING ? 'disabled' : ''}>下一页</button>
          <button class="secondary mini" type="button" onclick="setAssetPage(${totalPages})" ${ASSET_PAGE >= totalPages || ASSET_LOADING ? 'disabled' : ''}>末页</button>`;
      }
    }
    async function loadAssets(showRefreshToast = false) {
      await ensureDashboardPersistentCachesHydrated();
      const requestSeq = ++ASSET_LOAD_SEQ;
      const cacheVersion = ASSET_PAGE_CACHE_VERSION;
      const requestPath = assetQueryPath(ASSET_PAGE);
      abortPageRequests(ASSET_PAGE_REQUESTS, ASSET_PAGE_CONTROLLERS, requestPath);
      const cached = showRefreshToast ? null : usablePageCache(ASSET_PAGE_CACHE, requestPath);
      if (showRefreshToast) ASSET_PAGE_CACHE.delete(requestPath);
      if (showRefreshToast) removeDashboardPersistCache('asset-page', requestPath);
      if (cached) {
        applyAssetPage(cached);
        ASSET_LOADING = false;
        setPagerLoading('assetPager', false);
        renderAssets();
        const cachedPages = Math.max(1, Math.ceil((cached.meta.filtered || 0) / ASSET_PAGE_SIZE));
        prefetchPages(requestPath, ASSET_PAGE, cachedPages, ASSET_PAGE_SIZE, ASSET_PAGE_CACHE, ASSET_PAGE_REQUESTS, ASSET_PAGE_CONTROLLERS, assetPagePayload, () => cacheVersion === ASSET_PAGE_CACHE_VERSION);
        return;
      } else {
        ASSET_LOADING = true;
        setPagerLoading('assetPager', true);
      }
      try {
        const res = await requestCachedPage(requestPath, ASSET_PAGE_REQUESTS, ASSET_PAGE_CONTROLLERS, showRefreshToast);
        if (requestSeq !== ASSET_LOAD_SEQ) return;
        if (cacheVersion !== ASSET_PAGE_CACHE_VERSION) return;
        const payload = assetPagePayload(res);
        rememberPageCache(ASSET_PAGE_CACHE, requestPath, payload);
        applyAssetPage(payload);
        const totalPages = Math.max(1, Math.ceil((ASSET_META.filtered || 0) / ASSET_PAGE_SIZE));
        if (!ASSET_RECORDS.length && ASSET_META.filtered > 0 && ASSET_PAGE > totalPages) {
          ASSET_PAGE = totalPages;
          return await loadAssets(showRefreshToast);
        }
        ASSET_LOADING = false;
        renderAssets();
        prefetchPages(requestPath, ASSET_PAGE, totalPages, ASSET_PAGE_SIZE, ASSET_PAGE_CACHE, ASSET_PAGE_REQUESTS, ASSET_PAGE_CONTROLLERS, assetPagePayload, () => cacheVersion === ASSET_PAGE_CACHE_VERSION);
        if (showRefreshToast) showToast('资产库已刷新', 'ok');
      } catch (e) {
        if (isAbortError(e)) return;
        if (requestSeq === ASSET_LOAD_SEQ && !cached && $('assetStatus')) $('assetStatus').textContent = e.message || '资产读取失败';
      } finally {
        if (requestSeq === ASSET_LOAD_SEQ) {
          ASSET_LOADING = false;
          setPagerLoading('assetPager', false);
        }
      }
    }
    function setAssetPage(page) {
      ASSET_PAGE = Math.max(1, Math.trunc(Number(page) || 1));
      void loadAssets(false);
    }
    function assetFilterChanged() {
      ASSET_PAGE = 1;
      ASSET_LOAD_SEQ += 1;
      abortPageRequests(ASSET_PAGE_REQUESTS, ASSET_PAGE_CONTROLLERS);
      clearTimeout(ASSET_LOAD_TIMER);
      ASSET_LOAD_TIMER = setTimeout(() => loadAssets(false), 260);
    }
    async function loadAssetTags(force = false) {
      const cache = PICKER_CACHE.tags;
      try {
        const fresh = !force && cache.loadedAt > 0 && (Date.now() - cache.loadedAt) < PICKER_CATALOG_CACHE_TTL_MS;
        const rows = fresh ? cache.data : await loadAssetTagsCached(force);
        const list = $('assetTagList');
        if (!list) return;
        list.replaceChildren();
        rows.forEach(item => {
          const tag = String(item?.tag || '').trim();
          if (!tag) return;
          const option = document.createElement('option');
          option.value = tag;
          option.label = `${tag} (${Number(item?.count || 0)})`;
          list.appendChild(option);
        });
      } catch (e) {
        if ($('assetStatus')) $('assetStatus').textContent = e.message || '资产标签读取失败';
      }
    }
    async function loadAssetStudioPresets(page = ASSET_STUDIO_PRESET_PAGE) {
      const select = $('assetStudioPreset');
      if (!select) return;
      const loadSeq = ++ASSET_STUDIO_PRESET_LOAD_SEQ;
      const requestedPage = Math.max(1, Number(page) || 1);
      ASSET_STUDIO_PRESET_PAGE = requestedPage;
      const search = String($('assetStudioPresetSearch')?.value || '').trim();
      try {
        const selected = select.value;
        const requestedSearch = search;
        const result = await fetchCatalogPage('image', search, ASSET_STUDIO_PRESET_PAGE);
        if (loadSeq !== ASSET_STUDIO_PRESET_LOAD_SEQ
          || requestedPage !== ASSET_STUDIO_PRESET_PAGE
          || requestedSearch !== String($('assetStudioPresetSearch')?.value || '').trim()) return;
        ASSET_STUDIO_PRESETS = result.items;
        const pageCount = Math.max(1, Math.ceil(result.total / PICKER_PAGE_SIZE));
        if (requestedPage > pageCount) return loadAssetStudioPresets(pageCount);
        prefetchCatalogPages('image', search, requestedPage, result.total);
        select.innerHTML = '<option value="">不套预设</option>';
        ASSET_STUDIO_PRESETS.forEach(item => {
          const name = String(item?.name || item?.title || '').trim();
          if (!name) return;
          const option = document.createElement('option');
          option.value = name;
          option.textContent = `加入画布 · ${name}`;
          select.appendChild(option);
        });
        if ([...select.options].some(option => option.value === selected)) select.value = selected;
        const prev = $('assetStudioPresetPrev');
        const next = $('assetStudioPresetNext');
        if (prev) prev.disabled = requestedPage <= 1;
        if (next) next.disabled = requestedPage >= pageCount;
        select.title = `加入画布时附加的预设 · 第 ${requestedPage}/${pageCount} 页，共 ${result.total} 项`;
      } catch (e) {
        if (loadSeq === ASSET_STUDIO_PRESET_LOAD_SEQ) {
          if ($('assetStatus')) $('assetStatus').textContent = e.message || '画布预设读取失败';
        }
      }
    }
    async function addAssetToStudio(recordId) {
      const id = String(recordId || '').trim();
      if (!id) return showToast('资产 ID 为空', 'bad');
      const preset = String($('assetStudioPreset')?.value || '').trim();
      try {
        showToast('正在加入画布…', 'ok');
        const body = {preset_name: preset};
        if (STUDIO.current && STUDIO.current.id) body.session_id = STUDIO.current.id;
        const res = await api('/api/assets/' + encodeURIComponent(id) + '/studio', {method:'POST', body: JSON.stringify(body)});
        if (!res.success) throw new Error(res.message || '加入画布失败');
        const data = res.data || {};
        if (data.session_id) await loadStudioList(data.session_id);
        document.querySelector('nav.page-nav button[data-tab="studio"]')?.click();
        showToast(res.message || '资产已加入画布', 'ok');
      } catch (e) { showToast(e.message || '加入画布失败', 'bad'); }
    }
    async function addSelectedAssetToStudio() {
      const ids = selectedAssetIds();
      if (!ids.length) return showToast('请选择图片资产', 'bad');
      if (ids.length === 1) return addAssetToStudio(ids[0]);
      if (ids.length > 12) return showToast('单次最多加入 12 项图片资产', 'bad');
      const preset = String($('assetStudioPreset')?.value || '').trim();
      try {
        const body = {ids, preset_name: preset};
        if (STUDIO.current && STUDIO.current.id) body.session_id = STUDIO.current.id;
        const res = await api('/api/assets/studio', {method:'POST', body: JSON.stringify(body)});
        if (!res.success) throw new Error(res.message || '批量加入画布失败');
        const data = res.data || {};
        ASSET_SELECTION.clear();
        if (data.session_id) await loadStudioList(data.session_id);
        document.querySelector('nav.page-nav button[data-tab="studio"]')?.click();
        const failed = Number(data.error_count || 0);
        showToast(`已加入 ${Number(data.added_count || 0)} 项${failed ? `，${failed} 项失败` : ''}`, failed ? 'bad' : 'ok');
      } catch (e) { showToast(e.message || '批量加入画布失败', 'bad'); }
    }
    function toggleAssetSelection(id, checked) {
      const value = String(id || '').trim();
      if (!value) return;
      if (checked) ASSET_SELECTION.add(value); else ASSET_SELECTION.delete(value);
      renderAssets();
    }
    function selectAllAssets() {
      ASSET_RECORDS.forEach(record => { const id = String(record.id || '').trim(); if (id) ASSET_SELECTION.add(id); });
      renderAssets();
    }
    function clearAssetSelection() {
      ASSET_SELECTION.clear();
      renderAssets();
    }
    function selectedAssetIds() {
      return Array.from(ASSET_SELECTION).filter(Boolean);
    }
    function assetMetadataSnapshotRows(payload, field, ids) {
      const allowed = new Set((ids || []).map(value => String(value || '').trim()).filter(Boolean));
      const rows = Array.isArray(payload?.records) ? payload.records : [];
      return rows.filter(row => allowed.has(String(row?.id || '').trim())).map(row => {
        const id = String(row.id || '').trim();
        if (field === 'tags') return {id, tags: Array.isArray(row.tags) ? row.tags.slice() : []};
        if (field === 'note') return {id, note: String(row.note || '')};
        if (field === 'favorite' || field === 'pinned') return {id, [field]: Boolean(row[field])};
        return {id};
      });
    }
    async function captureAssetMetadataSnapshot(ids, field) {
      const suffix = `?ids=${encodeURIComponent(ids.join(','))}`;
      const res = await api('/api/assets/export' + suffix);
      return assetMetadataSnapshotRows(res.data, field, ids);
    }
    async function undoAssetMetadataSnapshot(rows) {
      if (!Array.isArray(rows) || !rows.length) throw new Error('没有可撤销的资产变更');
      const res = await api('/api/assets/import', {
        method: 'POST',
        body: JSON.stringify({version: 1, records: rows})
      });
      clearAssetPageCache();
      clearRecordPageCache();
      clearPickerCache('tags');
      await Promise.all([loadAssets(false), loadRecords(false), loadAssetTags()]);
      return res;
    }
    async function batchAssetMetadata(field, value) {
      const ids = selectedAssetIds();
      if (!ids.length) return showToast('请先选择资产', 'bad');
      let snapshot = [];
      try {
        snapshot = await captureAssetMetadataSnapshot(ids, field);
      } catch (_) {
        // Metadata update remains available when an older host lacks export.
      }
      try {
        const res = await api('/api/assets/metadata', {method: 'POST', body: JSON.stringify({ids, [field]: value})});
        clearAssetPageCache();
        clearRecordPageCache();
        if (field === 'tags') clearPickerCache('tags');
        (res.data?.missing || []).forEach(id => ASSET_SELECTION.delete(String(id)));
        await Promise.all([loadAssets(false), field === 'tags' ? loadAssetTags() : Promise.resolve()]);
        const updated = Array.isArray(res.data?.updated) ? new Set(res.data.updated.map(id => String(id))) : null;
        const undoRows = updated ? snapshot.filter(row => updated.has(String(row.id))) : snapshot;
        const updatedCount = Number(res.data?.updated_count || undoRows.length);
        showToast(res.message || '批量标记已保存', 'ok', undoRows.length ? {
          actionText: '撤销',
          duration: 8000,
          onAction: async () => {
            try {
              const restored = await undoAssetMetadataSnapshot(undoRows);
              showToast(`已撤销 ${Number(restored.data?.updated_count || undoRows.length)} 项资产变更`, 'ok');
            } catch (e) { showToast(e.message || '撤销失败', 'bad'); }
          }
        } : {});
      } catch (e) { showToast(e.message || '批量标记失败', 'bad'); }
    }
    async function applyBatchAssetFields(field) {
      if (field === 'tags') {
        const value = String($('assetBatchTags')?.value || '').split(/[,，]/).map(item => item.trim()).filter(Boolean);
        if (!value.length) return showToast('请输入要应用的标签', 'bad');
        return batchAssetMetadata('tags', value);
      }
      if (field === 'note') {
        const input = $('assetBatchNote');
        if (!input || !String(input.value || '').trim()) return showToast('请输入要应用的备注', 'bad');
        return batchAssetMetadata('note', String(input.value || '').trim());
      }
    }
    async function exportAssets() {
      try {
        const ids = selectedAssetIds();
        const suffix = ids.length ? `?ids=${encodeURIComponent(ids.join(','))}` : '';
        const res = await api('/api/assets/export' + suffix);
        const payload = res.data || {};
        const text = JSON.stringify(payload, null, 2);
        const blob = new Blob([text], {type: 'application/json;charset=utf-8'});
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `selfie-image-assets-${new Date().toISOString().slice(0, 10)}.json`;
        link.rel = 'noopener';
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        showToast(`已导出 ${Number(payload.count || 0)} 条资产元数据`, 'ok');
      } catch (e) { showToast(e.message || '导出失败', 'bad'); }
    }
    async function importAssetsFile(file) {
      if (!file) return;
      try {
        const text = await file.text();
        const payload = JSON.parse(text);
        if (!payload || typeof payload !== 'object' || !Array.isArray(payload.records)) throw new Error('导入文件必须包含 records 数组');
        const res = await api('/api/assets/import', {method: 'POST', body: JSON.stringify(payload)});
        clearAssetPageCache();
        clearRecordPageCache();
        clearPickerCache('tags');
        await Promise.all([loadAssets(false), loadAssetTags()]);
        const count = Number(res.data?.updated_count || 0);
        showToast(res.message || `已导入 ${count} 条资产元数据`, 'ok');
      } catch (e) { showToast(e.message || '导入失败', 'bad'); }
    }
    async function deleteSelectedAssets() {
      const ids = selectedAssetIds();
      const selected = ASSET_RECORDS.filter(record => ASSET_SELECTION.has(String(record.id || '')));
      if (!ids.length) return showToast('请先选择资产', 'bad');
      if (!await confirmAction(`确定删除选中的 ${ids.length} 项资产？缓存文件仅在没有其他记录引用时清理。`, {title:'删除资产', confirmText:'删除资产'})) return;
      try {
        const res = await api('/api/assets/delete', {method: 'POST', body: JSON.stringify({ids})});
        clearAssetPageCache();
        clearRecordPageCache();
        clearPickerCache('tags');
        selected.forEach(record => {
          const images = Array.isArray(record.generated_image_paths) ? record.generated_image_paths : [];
          images.forEach(invalidateProtectedMediaPath);
        });
        if (selected.length !== ids.length) clearProtectedMediaCache();
        clearGalleryPickerCache();
        ASSET_SELECTION.clear();
        await loadAssets(false);
        showToast(res.message || `已删除 ${res.data?.deleted_count || 0} 项`, 'ok');
      } catch (e) { showToast(e.message || '删除失败', 'bad'); }
    }
    async function batchDownloadAssets() {
      const selected = ASSET_RECORDS.filter(record => ASSET_SELECTION.has(String(record.id || '')));
      const files = [];
      selected.forEach(record => {
        const imagePaths = Array.isArray(record.generated_image_paths) ? record.generated_image_paths : [];
        imagePaths.filter(Boolean).forEach(path => files.push(String(path)));
      });
      if (!files.length) return showToast('选中的资产没有可下载缓存', 'bad');
      for (const path of files) await downloadCachePath(path, path.split('/').pop() || 'asset');
      showToast(`已开始下载 ${files.length} 个文件`, 'ok');
    }

    let ASSET_COLLECTIONS = [];
    let ASSET_COLLECTION_LOAD_SEQ = 0;
    let ASSET_COLLECTION_LOAD_REQUEST = null;
    let ASSET_COLLECTION_LOAD_CONTROLLER = null;
    let ASSET_COLLECTION_CACHE_AT = 0;
    let ASSET_COLLECTION_CACHE_SCOPE = '';
    const ASSET_COLLECTION_CACHE_TTL_MS = 30000;

    function selectedAssetCollectionId() {
      return String($('assetCollectionSelect')?.value || '').trim();
    }

    function renderAssetCollections() {
      const select = $('assetCollectionSelect');
      const list = $('assetCollectionList');
      if (select) {
        const selected = selectedAssetCollectionId();
        select.innerHTML = '<option value="">选择集合</option>' + ASSET_COLLECTIONS.map(item =>
          `<option value="${escapeHtml(String(item.id || ''))}">${escapeHtml(String(item.name || '未命名集合'))} · ${Number((item.record_ids || []).length)} 项</option>`
        ).join('');
        if (ASSET_COLLECTIONS.some(item => String(item.id || '') === selected)) select.value = selected;
      }
      if (list) {
        list.innerHTML = ASSET_COLLECTIONS.length ? ASSET_COLLECTIONS.map(item => {
          const id = String(item.id || '');
          const name = String(item.name || '未命名集合');
          const count = Array.isArray(item.record_ids) ? item.record_ids.length : 0;
          return `<article class="asset-collection-card">
            <div class="asset-collection-card-head"><span class="asset-collection-card-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span><span class="pill gray">${count} 项</span></div>
            <div class="asset-collection-card-note">${escapeHtml(String(item.note || '无备注'))}</div>
            <div class="asset-collection-card-actions"><button class="danger mini" type="button" data-collection-delete="${escapeHtml(id)}">删除</button></div>
          </article>`;
        }).join('') : '<div class="muted">还没有资产集合。</div>';
        if (list.dataset.bound !== '1') {
          list.dataset.bound = '1';
          list.addEventListener('click', event => {
            const button = event.target.closest('button[data-collection-delete]');
            if (button) void deleteAssetCollection(button.dataset.collectionDelete || '');
          });
        }
      }
    }

    function clearAssetCollectionsCache() {
      ASSET_COLLECTION_LOAD_SEQ += 1;
      try { ASSET_COLLECTION_LOAD_CONTROLLER?.abort(); } catch (_) {}
      ASSET_COLLECTION_LOAD_CONTROLLER = null;
      ASSET_COLLECTION_CACHE_AT = 0;
      ASSET_COLLECTION_LOAD_REQUEST = null;
      ASSET_COLLECTIONS = [];
      clearDashboardPersistNamespace('asset-collections');
    }
    async function loadAssetCollections(showRefreshToast = false, force = false) {
      await ensureDashboardPersistentCachesHydrated();
      const scope = dashboardPersistScope();
      if (ASSET_COLLECTION_CACHE_SCOPE && ASSET_COLLECTION_CACHE_SCOPE !== scope) {
        ASSET_COLLECTION_CACHE_AT = 0;
        ASSET_COLLECTIONS = [];
      }
      ASSET_COLLECTION_CACHE_SCOPE = scope;
      if (ASSET_COLLECTION_LOAD_REQUEST) {
        const pending = ASSET_COLLECTION_LOAD_REQUEST;
        return force || showRefreshToast ? pending.then(() => loadAssetCollections(showRefreshToast, true)) : pending;
      }
      if (!force && !showRefreshToast && ASSET_COLLECTION_CACHE_AT > 0
        && Date.now() - ASSET_COLLECTION_CACHE_AT < ASSET_COLLECTION_CACHE_TTL_MS) {
        renderAssetCollections();
        return;
      }
      if (!force && !showRefreshToast) {
        const persisted = readDashboardPersistCacheEntry('asset-collections', 'all');
        if (Array.isArray(persisted?.data)) {
          ASSET_COLLECTIONS = persisted.data;
          ASSET_COLLECTION_CACHE_AT = Date.now();
          renderAssetCollections();
          return;
        }
      }
      const requestSeq = ++ASSET_COLLECTION_LOAD_SEQ;
      const controller = typeof AbortController === 'function' ? new AbortController() : null;
      ASSET_COLLECTION_LOAD_CONTROLLER = controller;
      let request;
      request = (async () => {
        try {
          const res = await api('/api/assets/collections', controller ? {signal: controller.signal} : {});
          if (requestSeq !== ASSET_COLLECTION_LOAD_SEQ) return;
          ASSET_COLLECTIONS = Array.isArray(res.data) ? res.data : [];
          ASSET_COLLECTION_CACHE_AT = Date.now();
          writeDashboardPersistCache('asset-collections', 'all', ASSET_COLLECTIONS);
          renderAssetCollections();
          if (showRefreshToast) showToast('资产集合已刷新', 'ok');
        } catch (e) {
          if (requestSeq === ASSET_COLLECTION_LOAD_SEQ && $('assetCollectionStatus')) $('assetCollectionStatus').textContent = e.message || '资产集合读取失败';
        }
      })().finally(() => {
        if (ASSET_COLLECTION_LOAD_REQUEST === request) ASSET_COLLECTION_LOAD_REQUEST = null;
        if (ASSET_COLLECTION_LOAD_CONTROLLER === controller) ASSET_COLLECTION_LOAD_CONTROLLER = null;
      });
      ASSET_COLLECTION_LOAD_REQUEST = request;
      return request;
    }

    async function createAssetCollection() {
      const name = String($('assetCollectionName')?.value || '').trim();
      const note = String($('assetCollectionNote')?.value || '').trim();
      if (!name) return showToast('请输入集合名称', 'bad');
      try {
        clearAssetCollectionsCache();
        const res = await api('/api/assets/collections', {method: 'POST', body: JSON.stringify({name, note})});
        if (!res.success) throw new Error(res.message || '集合创建失败');
        if ($('assetCollectionName')) $('assetCollectionName').value = '';
        if ($('assetCollectionNote')) $('assetCollectionNote').value = '';
        await loadAssetCollections(false, true);
        const id = String(res.data?.id || '');
        if (id && $('assetCollectionSelect')) $('assetCollectionSelect').value = id;
        showToast(res.message || '资产集合已创建', 'ok');
      } catch (e) {
        await loadAssetCollections(false, true).catch(() => {});
        showToast(e.message || '集合创建失败', 'bad');
      }
    }

    async function deleteAssetCollection(collectionId) {
      const id = String(collectionId || '').trim();
      if (!id) return;
      const item = ASSET_COLLECTIONS.find(row => String(row.id || '') === id);
      if (!await confirmAction(`确定删除集合“${item?.name || id}”？集合内记录和媒体不会删除。`, {title: '删除资产集合', confirmText: '删除集合'})) return;
      try {
        clearAssetCollectionsCache();
        const res = await api('/api/assets/collections/' + encodeURIComponent(id) + '/delete', {method: 'POST'});
        if (!res.success) throw new Error(res.message || '集合删除失败');
        await loadAssetCollections(false, true);
        const token = String(res.data?.undo?.token || '');
        const ttl = Math.min(10000, Math.max(1000, Number(res.data?.undo?.ttl_seconds || 600) * 1000));
        showToast(res.message || '资产集合已删除', 'ok', token ? {
          actionText: '撤销', duration: ttl,
          onAction: async () => {
            try {
              clearAssetCollectionsCache();
              const restored = await api('/api/assets/collections/restore', {method: 'POST', body: JSON.stringify({token})});
              if (!restored.success) throw new Error(restored.message || '集合恢复失败');
              await loadAssetCollections(false, true);
              showToast(restored.message || '资产集合已恢复', 'ok');
            } catch (e) { showToast(e.message || '集合恢复失败', 'bad'); }
          }
        } : {});
      } catch (e) {
        await loadAssetCollections(false, true).catch(() => {});
        showToast(e.message || '集合删除失败', 'bad');
      }
    }

    async function updateSelectedAssetCollectionRecords(remove = false) {
      const collectionId = selectedAssetCollectionId();
      const ids = selectedAssetIds();
      if (!collectionId) return showToast('请先选择资产集合', 'bad');
      if (!ids.length) return showToast('请先选择资产', 'bad');
      try {
        const path = '/api/assets/collections/' + encodeURIComponent(collectionId) + (remove ? '/remove' : '/add');
        const res = await api(path, {method: 'POST', body: JSON.stringify({record_ids: ids})});
        if (!res.success) throw new Error(res.message || '集合内容更新失败');
        ASSET_COLLECTIONS = ASSET_COLLECTIONS.map(item => String(item.id || '') === collectionId ? (res.data || item) : item);
        writeDashboardPersistCache('asset-collections', 'all', ASSET_COLLECTIONS);
        renderAssetCollections();
        showToast(res.message || '集合内容已更新', 'ok');
      } catch (e) { showToast(e.message || '集合内容更新失败', 'bad'); }
    }

    function addSelectedAssetsToCollection() { return updateSelectedAssetCollectionRecords(false); }
    function removeSelectedAssetsFromCollection() { return updateSelectedAssetCollectionRecords(true); }
