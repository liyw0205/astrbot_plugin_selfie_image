    function monitorQueryPath(page = MONITOR_PAGE) {
      const params = new URLSearchParams();
      const media = $('monitorMedia').value.trim();
      const source = $('monitorSource').value.trim();
      const model = $('monitorModel').value.trim();
      const success = $('monitorSuccess').value;
      if (media) params.set('media_type', media);
      if (source) params.set('source', source);
      if (model) params.set('model', model);
      if (success) params.set('success', success);
      const asset = $('monitorAsset')?.value || '';
      if (asset === 'favorite') params.set('favorite', 'true');
      if (asset === 'pinned') params.set('pinned', 'true');
      const tag = $('monitorTag')?.value.trim() || '';
      if (tag) params.set('tag', tag);
      const keyword = $('monitorKeyword')?.value.trim() || '';
      if (keyword) params.set('q', keyword);
      params.set('limit', String(MONITOR_PAGE_SIZE));
      params.set('offset', String((Math.max(1, page) - 1) * MONITOR_PAGE_SIZE));
      return '/api/records?' + params.toString();
    }
    async function loadMetricsNow() {
      const root = $('monitorMetricButtons');
      if (!root) return;
      try {
        const windowKey = $('monitorMetricsWindow')?.value || 'all';
        const res = await api('/api/metrics?window=' + encodeURIComponent(windowKey));
        const d = res.data || {};
        const windowLabel = $('monitorMetricsWindow')?.selectedOptions?.[0]?.textContent || '全部保留记录';
        const statusLabels = {succeeded:'成功', failed:'失败', partial_success:'部分成功', cancelled:'已取消', delivery_failed:'发送失败', queued:'排队中', running:'进行中', expired:'已过期'};
        const errorLabels = {auth:'鉴权失败', not_found:'接口或模型不存在', safety:'审核拦截', quota:'额度不足', param:'参数不兼容', timeout:'超时', network:'网络错误', server:'上游服务错误', rate_limit:'请求限流', unknown:'无法分类', fatal:'不可重试'};
        const renderCountChips = (entries, labels = {}) => {
          const rows = Object.entries(entries || {});
          if (!rows.length) return '<span class="metrics-empty">暂无数据</span>';
          return `<div class="metrics-chip-list">${rows.map(([key, value]) => `<span class="metrics-chip"><span>${escapeHtml(labels[key] || key)}</span><strong>${escapeHtml(String(value))}</strong></span>`).join('')}</div>`;
        };
        const renderBreakdown = (rows, formatter) => {
          if (!rows.length) return '<span class="metrics-empty">暂无数据</span>';
          return `<div class="metrics-breakdown">${rows.map(([name, value]) => `<div class="metrics-breakdown-row"><strong>${escapeHtml(name)}</strong><span>${formatter(value)}</span></div>`).join('')}</div>`;
        };
        const e = d.elapsed_seconds || {};
        const channelRows = Object.entries(d.channels || {});
        const modelRows = Object.entries(d.models || {});
        const retries = d.retries || {};
        const quality = d.quality || {};
        const recommendations = Array.isArray(d.recommendations) ? d.recommendations : [];
        const metric = (label, value, note = '') => `<div class="metrics-kpi"><span class="metrics-kpi-label">${label}</span><strong class="metrics-kpi-value">${value}</strong>${note ? `<span class="metrics-kpi-note">${note}</span>` : ''}</div>`;
        const percent = value => `${(Number(value || 0) * 100).toFixed(1)}%`;
        const recordCount = escapeHtml(String(d.retained_records ?? 0));
        const totalRecordCount = escapeHtml(String(d.total_retained_records ?? d.retained_records ?? 0));
        const categories = [
          {
            key: 'output', title: '产出统计', summary: `${escapeHtml(String(d.requested_images ?? 0))} 张请求`,
            html: `<div class="metrics-section"><h3 class="metrics-section-title">窗口与产出</h3><div class="metrics-kpi-grid">
              ${metric('纳入记录', `${recordCount} / ${totalRecordCount}`, '当前窗口 / 全部保留')}
              ${metric('图片请求', escapeHtml(String(d.requested_images ?? 0)), '总请求数')}
              ${metric('成功图片', escapeHtml(String(d.succeeded_images ?? 0)), '已生成')}
              ${metric('失败图片', escapeHtml(String(d.failed_images ?? 0)), '未成功生成')}
            </div></div>`,
          },
          {
            key: 'quality', title: '质量与耗时', summary: `${escapeHtml(percent(quality.record_success_rate))} 记录成功率`,
            html: `<div class="metrics-section"><h3 class="metrics-section-title">质量指标</h3><div class="metrics-kpi-grid">
              ${metric('记录成功率', escapeHtml(percent(quality.record_success_rate)), '按生成记录统计')}
              ${metric('图片成功率', escapeHtml(percent(quality.image_success_rate)), '按图片数量统计')}
              ${metric('重试率', escapeHtml(percent(quality.retry_rate)), '发生过重试的记录')}
              ${metric('回退率', escapeHtml(percent(quality.fallback_rate)), '切换到后备渠道')}
            </div></div><div class="metrics-section"><h3 class="metrics-section-title">重试与耗时</h3><div class="metrics-kpi-grid">
              ${metric('重试总数', escapeHtml(String(retries.total ?? 0)), `涉及 ${escapeHtml(String(retries.records_with_retries ?? 0))} 条记录`)}
              ${metric('P50 耗时', `${escapeHtml(String(e.p50 ?? 0))}s`, '中位数')}
              ${metric('P95 耗时', `${escapeHtml(String(e.p95 ?? 0))}s`, '长耗时参考')}
              ${metric('最长耗时', `${escapeHtml(String(e.max ?? 0))}s`)}
            </div></div>`,
          },
          {
            key: 'errors', title: '状态与错误', summary: `${Object.keys(d.error_categories || {}).length} 类错误`,
            html: `<div class="metrics-split"><div class="metrics-section"><h3 class="metrics-section-title">状态分布</h3>${renderCountChips(d.status_counts, statusLabels)}</div>
              <div class="metrics-section"><h3 class="metrics-section-title">错误类型</h3>${renderCountChips(d.error_categories, errorLabels)}</div></div>`,
          },
          {
            key: 'providers', title: '渠道与模型', summary: `${channelRows.length} 渠道 · ${modelRows.length} 模型`,
            html: `<div class="metrics-split"><div class="metrics-section"><h3 class="metrics-section-title">渠道表现</h3>
              ${renderBreakdown(channelRows, value => `${escapeHtml(String(value.success ?? 0))} / ${escapeHtml(String(value.attempts ?? 0))} 次成功 · 回退 ${escapeHtml(String(value.fallbacks ?? 0))} · 成功率 ${escapeHtml(percent(value.success_rate))}`)}
              </div><div class="metrics-section"><h3 class="metrics-section-title">模型表现</h3>
              ${renderBreakdown(modelRows, value => `成功率 ${escapeHtml(percent(value.success_rate))}`)}
              </div></div>`,
          },
          {
            key: 'recommendations', title: '运行建议', summary: `${recommendations.length} 条建议`,
            html: recommendations.length
              ? `<div class="metrics-breakdown">${recommendations.map(item => `<div class="metrics-recommendation">${escapeHtml(String(item.message || item.code || ''))}</div>`).join('')}</div>`
              : '<span class="metrics-empty">暂无建议</span>',
          },
        ];
        METRICS_CONTENTS = Object.fromEntries(categories.map(category => [category.key, category]));
        $('monitorMetricsWindowLabel').textContent = `统计范围：${windowLabel}`;
        root.classList.remove('metrics-error');
        root.innerHTML = categories.map(category => `<button class="secondary metrics-launcher" type="button" data-metrics-category="${category.key}"><strong>${category.title}</strong><span>${category.summary}</span></button>`).join('');
        if (ACTIVE_METRICS_CATEGORY && $('metricsModal')?.classList.contains('show')) renderMetricsCategory(ACTIVE_METRICS_CATEGORY);
      } catch (e) {
        METRICS_CONTENTS = {};
        root.classList.add('metrics-error');
        root.textContent = e.message || '统计读取失败';
      }
    }
    function renderMetricsCategory(key) {
      const category = METRICS_CONTENTS[key];
      if (!category) return;
      ACTIVE_METRICS_CATEGORY = key;
      $('metricsModalTitle').textContent = category.title;
      $('metricsModalScope').textContent = $('monitorMetricsWindowLabel').textContent;
      $('metricsModalBody').innerHTML = category.html;
    }
    function openMetricsCategory(key) {
      renderMetricsCategory(key);
      if (METRICS_CONTENTS[key]) $('metricsModal').classList.add('show');
    }
    function closeMetricsModal() {
      $('metricsModal')?.classList.remove('show');
      ACTIVE_METRICS_CATEGORY = '';
    }
    async function loadMetrics() {
      const key = String($('monitorMetricsWindow')?.value || 'all');
      const existing = METRICS_REQUESTS.get(key);
      if (existing) return existing;
      let request;
      request = loadMetricsNow().finally(() => {
        if (METRICS_REQUESTS.get(key) === request) METRICS_REQUESTS.delete(key);
      });
      METRICS_REQUESTS.set(key, request);
      return request;
    }
    function taskQueueStatusLabel(status) {
      return ({queued:'排队', running:'进行中', succeeded:'完成', partial_success:'部分完成', failed:'失败', delivery_failed:'生成成功但发送失败', cancelled:'已取消', expired:'已过期'})[status] || status || '未知';
    }
    function taskQueueProgress(task) {
      const requested = Number(task?.requested_count || 0);
      if (!requested) return '';
      const completed = Math.max(0, Number(task?.completed_count || 0));
      const percent = Math.max(0, Math.min(100, Number(task?.progress_percent ?? Math.round(completed * 100 / requested))));
      return `${completed}/${requested}（${percent}%）`;
    }
    function taskQueueStage(task) {
      return String(task?.generation_stage_label || '').trim();
    }
    function taskQueueDeliveryLabel(task) {
      if (task?.delivery_unknown === true) return '发送状态未知（QQ 回执超时）';
      if (task?.delivery_success === true) return '已发送';
      if (task?.delivery_success === false) return '发送失败';
      return '';
    }
    function taskQueueTiming(task) {
      return '';
      const queueSeconds = Number(task?.queue_wait_seconds || 0);
      const runningSeconds = Number(task?.running_seconds || 0);
      const parts = [];
      if (task?.queue_waiting || queueSeconds > 0) parts.push(`已等 ${queueSeconds.toFixed(1)}s`);
      if (runningSeconds > 0) parts.push(`已用 ${runningSeconds.toFixed(1)}s`);
      return parts.join(' · ');
    }
    function taskQueueScope() {
      return $('taskQueueScope')?.value === 'recent' ? 'recent' : 'active';
    }
    function taskQueueMedia() {
      const value = String($('taskQueueMedia')?.value || '').trim();
      return value === 'image' ? value : '';
    }
    function taskQueueViewChanged() {
      TASK_PAGE = 1;
      loadTasks(false);
    }
    function taskQueueQueryPath() {
      const scope = taskQueueScope();
      const params = new URLSearchParams({limit: String(TASK_PAGE_SIZE), offset: String((Math.max(1, TASK_PAGE) - 1) * TASK_PAGE_SIZE)});
      if (scope === 'recent') params.set('include_finished', 'true');
      const mediaType = taskQueueMedia();
      if (mediaType) params.set('media_type', mediaType);
      const fields = [
        ['status', 'taskQueueStatus'], ['source', 'taskQueueSource'], ['model', 'taskQueueModel'],
        ['q', 'taskQueueKeyword'], ['start_date', 'taskQueueStartDate'], ['end_date', 'taskQueueEndDate']
      ];
      fields.forEach(([name, id]) => {
        const value = String($(id)?.value || '').trim();
        if (value) params.set(name, value);
      });
      return '/api/tasks?' + params.toString();
    }
    function taskQueueFilterChanged() {
      clearTimeout(TASK_FILTER_TIMER);
      TASK_PAGE = 1;
      TASK_FILTER_TIMER = setTimeout(() => loadTasks(false), 260);
    }
    function setTaskPage(page) {
      const total = Number($('taskQueuePager')?.dataset.total || 0);
      const pages = Math.max(1, Math.ceil(total / TASK_PAGE_SIZE));
      TASK_PAGE = Math.min(Math.max(1, Number(page) || 1), pages);
      loadTasks(false);
    }
    function selectedTaskIds() {
      return Array.from(TASK_SELECTION).filter(Boolean);
    }
    function updateTaskSelectionStatus(rows = []) {
      const visibleIds = new Set((Array.isArray(rows) ? rows : []).map(item => String(item?.task_id || '').trim()).filter(Boolean));
      const visibleSelected = Array.from(visibleIds).filter(id => TASK_SELECTION.has(id)).length;
      const total = selectedTaskIds().length;
      if ($('taskSelectionStatus')) $('taskSelectionStatus').textContent = total
        ? `已选择 ${total} 项${visibleSelected !== total ? `（当前页 ${visibleSelected}）` : ''}`
        : '未选择任务';
    }
    function toggleTaskSelection(id, checked) {
      const value = String(id || '').trim();
      if (!value) return;
      if (checked) TASK_SELECTION.add(value); else TASK_SELECTION.delete(value);
      updateTaskSelectionStatus();
    }
    function selectVisibleTasks() {
      document.querySelectorAll('#taskQueueTable input[data-task-select]').forEach(input => {
        const id = String(input.getAttribute('data-task-select') || '').trim();
        if (id) TASK_SELECTION.add(id);
        input.checked = true;
      });
      updateTaskSelectionStatus();
    }
    function clearTaskSelection() {
      TASK_SELECTION.clear();
      document.querySelectorAll('#taskQueueTable input[data-task-select]').forEach(input => { input.checked = false; });
      updateTaskSelectionStatus();
    }
    async function deleteSelectedTasks() {
      const ids = selectedTaskIds();
      if (!ids.length) return showToast('请先选择任务', 'bad');
      if (!await confirmAction(`确定删除选中的 ${ids.length} 项任务记录？生成记录和缓存不会受影响。`, {title:'删除任务记录', confirmText:'删除任务'})) return;
      try {
        const res = await api('/api/tasks/delete', {method:'POST', body: JSON.stringify({ids})});
        const data = res.data || {};
        (data.deleted || []).concat(data.missing || []).forEach(id => TASK_SELECTION.delete(String(id || '')));
        await loadTasks(false);
        showToast(`已删除 ${Number(data.deleted_count || 0)} 项任务`, 'ok');
      } catch (e) { showToast(e.message || '任务删除失败', 'bad'); }
    }
    async function retrySelectedTasks() {
      const ids = selectedTaskIds();
      if (!ids.length) return showToast('请先选择任务', 'bad');
      const strategy = String($('taskRetryStrategy')?.value || 'full');
      if (!await confirmAction(`确定重试选中的 ${ids.length} 项任务？\n策略：${retryStrategyLabel(strategy)}\n只有已关联生成记录的任务可以重试。`, {title:'批量重试任务', confirmText:'开始重试', danger:false})) return;
      try {
        const res = await api('/api/tasks/retry', {method:'POST', body: JSON.stringify({ids, strategy})});
        const data = res.data || {};
        showToast(`已提交 ${Number(data.submitted_count || 0)} 项重试`, data.submitted_count ? 'ok' : 'bad');
        clearTaskSelection();
        await loadTasks(false);
      } catch (e) { showToast(e.message || '批量重试失败', 'bad'); }
    }
    function retryStrategyLabel(strategy) {
      return ({full:'完整保留参数', model_only:'只换模型', channel_only:'只换渠道', lower_resolution:'降低分辨率'})[String(strategy || 'full')] || '完整保留参数';
    }
    async function exportSelectedTasks() {
      try {
        const ids = selectedTaskIds();
        const suffix = ids.length ? `?ids=${encodeURIComponent(ids.join(','))}` : '';
        const res = await api('/api/tasks/export' + suffix);
        const payload = res.data || {};
        const blob = new Blob([JSON.stringify(payload, null, 2)], {type:'application/json;charset=utf-8'});
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `selfie-image-tasks-${new Date().toISOString().slice(0, 10)}.json`;
        document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        showToast(`已导出 ${Number(payload.count || 0)} 项任务`, 'ok');
      } catch (e) { showToast(e.message || '任务导出失败', 'bad'); }
    }
    function closeTaskDetail() {
      $('taskModal')?.classList.remove('show');
    }
    function taskDetailPrompt(label, value) {
      const text = String(value || '').trim();
      return text ? `<div><label>${escapeHtml(label)}</label><pre class="status" style="white-space:pre-wrap;overflow-wrap:anywhere">${escapeHtml(text)}</pre></div>` : '';
    }
    function taskUsesPersonaReference(task) {
      const params = task?.parameters && typeof task.parameters === 'object' ? task.parameters : {};
      const selection = params.reference_selection && typeof params.reference_selection === 'object' ? params.reference_selection : {};
      const roles = selection.roles && typeof selection.roles === 'object' ? selection.roles : {};
      return Boolean(
        params.use_selfie_reference
        || Number(params.persona_reference_count || 0) > 0
        || selection.used_persona
        || Number(roles.persona || 0) > 0
        || Number(roles.persona_extra || 0) > 0
        || String(params.identity_reference_source || '').trim() === 'persona'
      );
    }
    async function retryTaskFromRecord(recordId, strategy = 'full') {
      if (!recordId) return;
      try {
        const res = await api('/api/records/' + encodeURIComponent(recordId) + '/retry', {method:'POST', body:JSON.stringify({strategy})});
        const task = res.data || {};
        showToast(task.task_id ? '已提交重试任务' : '重试已提交', 'ok');
        await loadTasks(false);
      } catch (e) { showToast(e.message || '重试提交失败', 'bad'); }
    }
    async function openTaskDetail(taskId) {
      const modal = $('taskModal');
      const body = $('taskDetailBody');
      if (!modal || !body || !taskId) return;
      body.innerHTML = '<div class="muted">正在读取任务详情…</div>';
      modal.classList.add('show');
      try {
        const res = await api('/api/tasks/' + encodeURIComponent(taskId));
        const task = res.data || {};
        const params = task.parameters || {};
        const requestImagePaths = Array.isArray(task.request_image_paths) ? task.request_image_paths : [];
        const personaReference = taskUsesPersonaReference(task)
          ? `<div style="margin-top:14px"><div class="detail-title"><h3>形象图</h3></div><div class="images">${personaReferenceThumb()}</div></div>`
          : '';
        const attempts = Array.isArray(task.attempts) ? task.attempts : [];
        const attemptRows = attempts.length
          ? `<div style="overflow:auto;margin-top:12px"><table class="table"><thead><tr><th>#</th><th>渠道/模型</th><th>结果</th><th>耗时</th><th>原因</th></tr></thead><tbody>${attempts.map(item => `<tr><td>${escapeHtml(String(item.attempt || ''))}</td><td>${escapeHtml(String(item.model || item.channel || ''))}</td><td>${item.success ? '成功' : '失败'}</td><td>${escapeHtml(String(item.elapsed_seconds ?? '-'))}s</td><td>${escapeHtml(String(item.error || item.error_category || ''))}</td></tr>`).join('')}</tbody></table></div>`
          : '<div class="muted" style="margin-top:12px">尚无渠道尝试明细</div>';
        const parameterRows = Object.entries(params).map(([key, value]) => `<div><label>${escapeHtml(key)}</label><div class="status">${escapeHtml(typeof value === 'object' ? JSON.stringify(value) : String(value))}</div></div>`).join('');
        const recordId = String(task.retry_record_id || task.record_id || '').trim();
        const recordIds = Array.isArray(task.record_ids) ? task.record_ids.map(item => String(item || '').trim()).filter(Boolean) : [];
        const linkedRecordIds = [...new Set(recordIds)];
        const deliveryNote = task.status === 'delivery_failed'
          ? '<div class="status" style="margin-top:12px">图片已经生成，但发送到会话失败；可在关联记录中重新执行。</div>'
          : '';
        body.innerHTML = `
          <div class="grid">
            <div><label>任务</label><div class="status mono">${escapeHtml(String(task.task_id || ''))}</div></div>
            <div><label>状态</label><div class="status">${escapeHtml(taskQueueStatusLabel(task.status))}</div></div>
            <div><label>来源</label><div class="status">${escapeHtml(displaySourceLabel(task))}</div></div>
            <div><label>媒体</label><div class="status">图片</div></div>
            <div><label>模型</label><div class="status">${escapeHtml(String(task.used_model || '未记录'))}</div></div>
          <div><label>进度</label><div class="status">${escapeHtml([taskQueueProgress(task), taskQueueTiming(task)].filter(Boolean).join(' · ') || '-')}</div></div>
            <div><label>生成阶段</label><div class="status">${escapeHtml(taskQueueStage(task) || '-')}</div></div>
            <div><label>生成/发送</label><div class="status">${task.generation_success === true ? '生成成功' : task.generation_success === false ? '生成失败' : '-'}${taskQueueDeliveryLabel(task) ? ` · ${escapeHtml(taskQueueDeliveryLabel(task))}` : ''}</div></div>
          </div>
          ${task.error ? `<div style="margin-top:12px"><label>失败原因</label><div class="status">${escapeHtml(String(task.error))}</div></div>` : ''}
          ${deliveryNote}
          ${taskDetailPrompt('原始提示词', task.original_prompt)}
          ${taskDetailPrompt('最终提示词', task.final_prompt)}
          ${personaReference}
          ${requestImagePaths.length ? `<div style="margin-top:14px">${imageThumbs(requestImagePaths, '请求图')}</div>` : ''}
          ${parameterRows ? `<div class="grid" style="margin-top:12px">${parameterRows}</div>` : ''}
          <div class="detail-title" style="margin-top:14px"><h3>关联记录</h3>${linkedRecordIds.length ? linkedRecordIds.map(id => `<button class="secondary mini task-record-open" data-record-id="${escapeHtml(id)}" type="button">查看 ${escapeHtml(id)}</button>`).join(' ') : recordId ? `<button class="secondary mini task-record-open" data-record-id="${escapeHtml(recordId)}" type="button">查看来源记录</button>` : '<span class="muted">生成记录尚未写入</span>'}${recordId ? `<button class="ok mini task-record-retry" data-record-id="${escapeHtml(recordId)}" type="button">重新执行来源</button>` : ''}</div>
          ${attemptRows}`;
        body.querySelectorAll('.task-record-open').forEach(button => button.addEventListener('click', () => { closeTaskDetail(); openRecordDetail(button.dataset.recordId || ''); }));
        body.querySelector('.task-record-retry')?.addEventListener('click', () => retryTaskFromRecord(recordId, String($('taskRetryStrategy')?.value || 'full')));
        loadProtectedImages(body);
        loadPersonaReferenceThumbs(body);
      } catch (e) {
        body.innerHTML = `<div class="status">${escapeHtml(e.message || '任务详情读取失败')}</div>`;
      }
    }
    async function loadTasksNow(schedule = true) {
      try {
        const scope = taskQueueScope();
        const res = await api(taskQueueQueryPath());
        const data = res.data || {};
        const summary = data.summary || {};
        const prefix = scope === 'recent' ? '最近任务' : '活动任务';
        const filteredTotal = Number(data.filtered_total ?? summary.filtered_total ?? (Array.isArray(data.tasks) ? data.tasks.length : 0));
        const pageCount = Math.max(1, Math.ceil(filteredTotal / TASK_PAGE_SIZE));
        if (TASK_PAGE > pageCount) {
          TASK_PAGE = pageCount;
          if (filteredTotal && !(Array.isArray(data.tasks) && data.tasks.length)) return loadTasks(false);
        }
        $('taskQueueSummary').textContent = `${prefix} · 第 ${TASK_PAGE}/${pageCount} 页 · 匹配 ${filteredTotal} · 排队 ${Number(summary.queued || 0)} · 进行中 ${Number(summary.running || 0)} · 图片槽位 ${Number(summary.image_active_slots || 0)}/${Number(summary.image_max_concurrent_tasks || 0)}`;
        const rows = Array.isArray(data.tasks) ? data.tasks : [];
        $('taskQueueTable').innerHTML = rows.length ? `<thead><tr><th></th><th>任务</th><th>来源</th><th>类型</th><th>状态</th><th>提示词</th><th>模型/原因</th><th></th></tr></thead><tbody>${rows.map(task => {
          const id = escapeHtml(String(task.task_id || ''));
          const rawId = String(task.task_id || '');
          const type = '图片';
          const canCancel = task.can_cancel === true || (
            !Object.prototype.hasOwnProperty.call(task, 'can_cancel') &&
            (task.status === 'queued' || task.status === 'running') && !task.cancel_requested
          );
          const info = task.error || task.used_model || task.timeout_warning || '-';
          const stage = taskQueueStage(task);
          const progress = taskQueueProgress(task);
          const statusMeta = [stage, progress, taskQueueTiming(task), task.timeout_warning].filter(Boolean).join(' · ');
          const prompt = String(task.prompt || task.prompt_summary || '').trim();
          const promptPreview = String(task.prompt_summary || prompt || '-').trim();
          const promptCell = prompt
            ? `<details><summary><span class="task-prompt-preview">${escapeHtml(promptPreview)}</span></summary><div class="task-prompt-full">${escapeHtml(prompt)}</div></details>`
            : '-';
          return `<tr class="task-detail-row" data-task-id="${id}" style="cursor:pointer"><td><input type="checkbox" data-task-select="${id}" aria-label="选择任务 ${id}" ${TASK_SELECTION.has(rawId) ? 'checked' : ''}></td><td class="mono">${id}</td><td>${escapeHtml(displaySourceLabel(task))}</td><td>${type}</td><td>${taskQueueStatusLabel(task.status)}${statusMeta ? `<br><span class="muted">${escapeHtml(statusMeta)}</span>` : ''}</td><td class="task-prompt-cell">${promptCell}</td><td>${escapeHtml(String(info))}</td><td>${canCancel ? `<button class="danger mini task-cancel-btn" data-task-id="${id}" type="button">取消</button>` : '<button class="secondary mini task-detail-btn" data-task-id="' + id + '" type="button">详情</button>'}</td></tr>`;
        }).join('')}</tbody>` : `<tbody><tr><td colspan="8" class="muted">${scope === 'recent' ? '没有最近任务' : '当前没有活动任务'}</td></tr></tbody>`;
        const pager = $('taskQueuePager');
        if (pager) {
          pager.dataset.total = String(filteredTotal);
          const start = filteredTotal ? ((TASK_PAGE - 1) * TASK_PAGE_SIZE) + 1 : 0;
          const end = Math.min(filteredTotal, (TASK_PAGE - 1) * TASK_PAGE_SIZE + rows.length);
          pager.innerHTML = `<span class="muted">${start}-${end} / ${filteredTotal}</span><button class="secondary mini" type="button" onclick="setTaskPage(${TASK_PAGE - 1})" ${TASK_PAGE <= 1 ? 'disabled' : ''}>上一页</button><button class="secondary mini" type="button" onclick="setTaskPage(${TASK_PAGE + 1})" ${TASK_PAGE >= pageCount ? 'disabled' : ''}>下一页</button>`;
        }
        $('taskQueueTable').querySelectorAll('input[data-task-select]').forEach(input => input.onchange = event => {
          event.stopPropagation();
          toggleTaskSelection(input.getAttribute('data-task-select') || '', input.checked);
        });
        $('taskQueueTable').querySelectorAll('.task-cancel-btn').forEach(btn => btn.onclick = async event => {
          event.stopPropagation();
          btn.disabled = true;
          try { await api('/api/tasks/' + encodeURIComponent(btn.dataset.taskId) + '/cancel', {method:'POST', body:'{}'}); showToast('任务已取消', 'ok'); await loadTasks(false); }
          catch (e) { btn.disabled = false; showToast(e.message || '取消失败', 'bad'); }
        });
        $('taskQueueTable').querySelectorAll('.task-detail-btn').forEach(btn => btn.onclick = event => { event.stopPropagation(); openTaskDetail(btn.dataset.taskId); });
        $('taskQueueTable').querySelectorAll('.task-detail-row').forEach(row => row.onclick = event => {
          if (!event.target.closest('button,input,details,summary,.media-zoom-shell')) openTaskDetail(row.dataset.taskId);
        });
        updateTaskSelectionStatus(rows);
      } catch (e) {
        if ($('taskQueueSummary')) $('taskQueueSummary').textContent = e.message || '任务队列读取失败';
      } finally {
        if (schedule) {
          clearTimeout(TASK_LOAD_TIMER);
          TASK_LOAD_TIMER = setTimeout(() => loadTasks(true), 4000);
        }
      }
    }
    async function loadTasks(schedule = true) {
      const key = taskQueueQueryPath();
      const existing = TASK_REQUESTS.get(key);
      if (existing) return existing;
      let request;
      request = loadTasksNow(schedule).finally(() => {
        if (TASK_REQUESTS.get(key) === request) TASK_REQUESTS.delete(key);
      });
      TASK_REQUESTS.set(key, request);
      return request;
    }
