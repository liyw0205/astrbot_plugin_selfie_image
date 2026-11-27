    async function readFileDataUrl(file) {
      return await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    }
    function clearTestTaskPoll() {
      if (TEST_TASK_POLL_TIMER) {
        clearTimeout(TEST_TASK_POLL_TIMER);
        TEST_TASK_POLL_TIMER = null;
      }
    }
    async function prepareActiveTestTask(actionLabel) {
      const taskId = String(TEST_TASK_ID || '').trim();
      if (!taskId) return true;
      const cancel = await confirmAction(
        `任务 ${taskId} 仍在后台运行。选择“取消并${actionLabel}”会停止它；取消则保留任务继续运行。`,
        {title: '处理正在运行的任务', confirmText: `取消并${actionLabel}`}
      );
      if (cancel) {
        await cancelTestTask();
        return !TEST_TASK_ID;
      }
      const keep = await confirmAction(
        `是否保留任务 ${taskId} 在后台运行并${actionLabel}？\n\n保留后可在任务历史中查看；当前面板将清空展示。`,
        {title: '保留后台任务', confirmText: `保留并${actionLabel}`, danger: false}
      );
      if (!keep) return false;
      clearTestTaskPoll();
      TEST_TASK_ID = '';
      safeStorageRemove('selfieImageLastTestTaskId');
      safeStorageRemove('selfieImageLastTestMode');
      clearTestData(true);
      showToast(`已保留后台任务 ${taskId}`, 'ok');
      return true;
    }
    async function switchTestMode() {
      TEST_MODE = 'image';
      if (TEST_TASK_ID && !(await prepareActiveTestTask('刷新试画'))) return;
      $('testResolutionWrap').style.display = '';
      if ($('testCountWrap')) $('testCountWrap').style.display = '';
      $('useSelfieWrap').style.display = '';
      $('useSelfie').disabled = false;
      renderTestExtraReferences();
      setSelectOptions('testAspect', ASPECTS, CONFIG.image?.default_aspect_ratio || '9:16');
      refreshModelSelectors();
      clearTestData(false);
      if (window.__TEST_PRESET_OPEN) {
        const state = catalogPickerState('testPresetPanel');
        void loadCatalogPickerPage('testPresetPanel', 'image', state.page);
      }
    }
    function setTestBusy(busy) {
      $('testImageBtn').disabled = !!busy;
      $('testCancelBtn').style.display = busy && TEST_TASK_ID ? '' : 'none';
      $('testRetryStatusBtn').style.display = 'none';
      $('testImageBtn').textContent = busy ? '正在画…' : '开始试画';
    }
    function taskProgressText(task) {
      const requested = Number(task?.requested_count || 0);
      if (!requested) return '';
      const completed = Math.max(0, Number(task?.completed_count || 0));
      const percent = Math.max(0, Math.min(100, Number(task?.progress_percent ?? Math.round(completed * 100 / requested))));
      return `${completed}/${requested}（${percent}%）`;
    }
    function taskStageText(task) {
      return String(task?.generation_stage_label || '').trim();
    }
    function displaySourceLabel(item) {
      const source = String(item?.source || '').trim().toLowerCase();
      const request = item?.request_data && typeof item.request_data === 'object' ? item.request_data : {};
      const title = String(
        item?.studio_session_title ||
        request.studio_session_title ||
        item?.task_summary?.studio_session_title ||
        item?.summary?.studio_session_title ||
        ''
      ).trim();
      if (source === 'studio-run') return `Web/${title || '画布'}`;
      if (source === 'web-test') return 'Web/快速试画';
      const group = String(item?.group_id || '').trim(), user = String(item?.user_id || '').trim();
      if (user) return group ? `${group}/${user}` : user;
      const label = String(item?.source_label || '').trim();
      const legacy = label.match(/^(?:群\s*(\d+)\s*\/\s*)?QQ\s*(\d+)$/);
      if (legacy) return legacy[1] ? `${legacy[1]}/${legacy[2]}` : legacy[2];
      return String(item?.source_label || item?.source || '未知来源');
    }
    async function recoverTestTaskAfterCancelFailure(taskId) {
      const id = String(taskId || '').trim();
      if (!id || TEST_TASK_ID !== id) return;
      try {
        const res = await api(testTaskStatusPath(id));
        const task = res.data || {};
        const active = task.status === 'queued' || task.status === 'running';
        if (active) {
          setTestBusy(true);
          $('testStatus').textContent = `取消未完成，任务仍在${task.status === 'queued' ? '排队' : '生成'}，继续等待…`;
          TEST_TASK_POLL_TIMER = setTimeout(() => pollImageTestTask(id, 0), 1000);
          return;
        }
        clearTestTaskPoll();
        TEST_TASK_ID = '';
        safeStorageRemove('selfieImageLastTestTaskId');
        safeStorageRemove('selfieImageLastTestMode');
        setTestBusy(false);
        renderImageTestResult(testTaskResult(task));
      } catch (statusError) {
        clearTestTaskPoll();
        TEST_TASK_ID = '';
        safeStorageRemove('selfieImageLastTestTaskId');
        safeStorageRemove('selfieImageLastTestMode');
        setTestBusy(false);
        $('testStatus').textContent = statusError.message || '取消后任务状态读取失败，已解除试画锁定';
        $('testResponseData').textContent = JSON.stringify({success:false, error:statusError.message || String(statusError)}, null, 2);
      }
    }
    async function cancelTestTask() {
      const taskId = String(TEST_TASK_ID || '').trim();
      if (!taskId) return;
      clearTestTaskPoll();
      $('testCancelBtn').disabled = true;
      $('testStatus').textContent = '正在取消任务…';
      try {
        const res = await api('/api/tasks/' + encodeURIComponent(taskId) + '/cancel', {method:'POST', body: JSON.stringify({})});
        const task = res.data || {};
        const active = task.status === 'queued' || task.status === 'running' || task.cancel_requested;
        setTestBusy(active);
        if (active) {
          $('testStatus').textContent = `已请求取消任务 ${taskId}，等待安全停止…`;
          TEST_TASK_POLL_TIMER = setTimeout(() => pollImageTestTask(taskId, 0), 1000);
        } else {
          TEST_TASK_ID = '';
          safeStorageRemove('selfieImageLastTestTaskId');
          safeStorageRemove('selfieImageLastTestMode');
          renderImageTestResult(testTaskResult(task));
        }
      } catch (e) {
        $('testCancelBtn').disabled = false;
        $('testStatus').textContent = e.message || '取消失败';
        await recoverTestTaskAfterCancelFailure(taskId);
      }
    }
    function testTaskResult(task) {
      const raw = task && task.result && typeof task.result === 'object' ? {...task.result} : {};
      const response = raw.response_data && typeof raw.response_data === 'object' ? raw.response_data : {};
      const taskError = (task && (task.error || (task.status === 'failed' ? response.error : ''))) || '';
      const error = raw.error || response.error || taskError || '';
      const terminalFailure = task && (task.success === false || ['failed', 'cancelled', 'expired'].includes(String(task.status || '').toLowerCase()));
      const terminalSuccess = task && (task.success === true || String(task.status || '').toLowerCase() === 'succeeded');
      if (task?.status && !raw.status) raw.status = String(task.status);
      if (raw.success === false || terminalFailure) raw.success = false;
      else if (raw.success !== true && terminalSuccess) raw.success = true;
      if (error) raw.error = error;
      return raw;
    }
    function renderImageTestResult(data) {
      $('testResponseData').textContent = JSON.stringify(data || {}, null, 2);
      const imagePaths = Array.isArray(data?.generated_image_paths) ? data.generated_image_paths : [];
      const partial = String(data?.status || '').toLowerCase() === 'partial_success';
      const hasMedia = imagePaths.length > 0;
      if (!data || (data.success !== true && !(partial && hasMedia))) {
        $('testStatus').textContent = `失败：${(data && (data.error || data.response_data?.error)) || '这次没顺好'}`;
        showTestPanel('response', false);
        return;
      }
      const generatedCount = Number(data.succeeded_count || data.generated_image_paths?.length || 0);
      const prefix = partial ? '部分完成' : '成功';
      $('testStatus').textContent = `${prefix}：${data.used_model || ''}，耗时 ${data.elapsed_seconds}s，${generatedCount || 1} 张，参考图 ${data.reference_images || 0} 张${partial && data.error ? `；${data.error}` : ''}`;
      $('testImages').innerHTML = imageThumbs(data.generated_image_paths || [], '生成图', data.generated_image_sources || []);
      loadProtectedImages($('testImages'));
      showTestPanel('result', false);
    }
    function testTaskStatusPath(taskId) {
      return `/api/test-image-channel/tasks/${encodeURIComponent(taskId)}`;
    }
    async function pollImageTestTask(taskId, failStreak = 0) {
      clearTestTaskPoll();
      TEST_TASK_ID = taskId || '';
      if (!TEST_TASK_ID) return;
      try {
        const res = await api(testTaskStatusPath(TEST_TASK_ID));
        if (TEST_TASK_ID !== taskId) return;
        const task = res.data || {};
        $('testResponseData').textContent = JSON.stringify(task, null, 2);
        if (task.request_data && !$('testRequestData').textContent.trim()) {
          $('testRequestData').textContent = JSON.stringify(task.request_data, null, 2);
        }
        if (task.status === 'queued' || task.status === 'running') {
          setTestBusy(true);
          const label = task.status === 'queued' ? '排队中' : '生成中';
          const seconds = Number(task.running_seconds || 0);
          const hint = seconds >= 20
            ? ' 有的中转要 15–60 秒，先等等；超过设定超时才会判失败。'
            : '';
          const progress = taskProgressText(task);
          const stage = taskStageText(task);
          $('testStatus').textContent = `任务 ${task.task_id || TEST_TASK_ID} ${label}${stage ? ` · ${stage}` : ''}${progress ? ` ${progress}` : ''}，已用 ${seconds}s。${hint}关掉页面也不会停。`;
          TEST_TASK_POLL_TIMER = setTimeout(() => pollImageTestTask(TEST_TASK_ID, 0), 2000);
          return;
        }
        setTestBusy(false);
        TEST_TASK_ID = '';
        safeStorageRemove('selfieImageLastTestTaskId');
        safeStorageRemove('selfieImageLastTestMode');
        renderImageTestResult(testTaskResult(task));
        clearRecordPageCache();
        clearAssetPageCache();
        clearGalleryPickerCache();
        try { await loadRecords(false); } catch (_) {}
      } catch (e) {
        if (TEST_TASK_ID !== taskId) return;
        const errorText = String(e.message || e);
        if (/\b401\b/.test(errorText)) {
          clearTestTaskPoll();
          setTestBusy(false);
          $('testStatus').textContent = '登录已失效，请重新登录后查询任务。';
          $('testRetryStatusBtn').style.display = TEST_TASK_ID ? '' : 'none';
          $('testResponseData').textContent = JSON.stringify({success:false, error:errorText}, null, 2);
          return;
        }
        if (/\b404\b/.test(errorText)) {
          clearTestTaskPoll();
          setTestBusy(false);
          TEST_TASK_ID = '';
          safeStorageRemove('selfieImageLastTestTaskId');
          safeStorageRemove('selfieImageLastTestMode');
          $('testStatus').textContent = '任务不存在或已清理，请从任务历史查看。';
          $('testResponseData').textContent = JSON.stringify({success:false, error:errorText}, null, 2);
          return;
        }
        // Ignore transient poll errors while task is still running.
        const nextFail = failStreak + 1;
        if (nextFail < 8) {
          setTestBusy(true);
          $('testStatus').textContent = `轮询暂时失败（${nextFail}/8）：${e.message || e}。正在重试...`;
          TEST_TASK_POLL_TIMER = setTimeout(() => pollImageTestTask(TEST_TASK_ID, nextFail), 2000);
          return;
        }
        setTestBusy(false);
        $('testStatus').textContent = errorText;
        $('testRetryStatusBtn').style.display = TEST_TASK_ID ? '' : 'none';
        $('testResponseData').textContent = JSON.stringify({success:false, error:errorText}, null, 2);
        showTestPanel('response', false);
      }
    }
    async function resumeImageTestTask() {
      const taskId = safeStorageGet('selfieImageLastTestTaskId') || '';
      if (!taskId) return;
      await switchTestMode();
      try {
        const res = await api(testTaskStatusPath(taskId));
        const task = res.data || {};
        if (task.status === 'queued' || task.status === 'running') {
          TEST_TASK_ID = taskId;
          $('testResponseData').textContent = JSON.stringify(task, null, 2);
          if (task.request_data) $('testRequestData').textContent = JSON.stringify(task.request_data, null, 2);
          pollImageTestTask(taskId);
        } else {
          TEST_TASK_ID = '';
          safeStorageRemove('selfieImageLastTestTaskId');
          safeStorageRemove('selfieImageLastTestMode');
          $('testResponseData').textContent = JSON.stringify(task, null, 2);
          if (task.request_data) $('testRequestData').textContent = JSON.stringify(task.request_data, null, 2);
          renderImageTestResult(testTaskResult(task));
          clearRecordPageCache();
          clearAssetPageCache();
          try { await loadRecords(false); } catch (_) {}
        }
      } catch (_) {
        safeStorageRemove('selfieImageLastTestTaskId');
      safeStorageRemove('selfieImageLastTestMode');
      }
    }
    async function runMediaTest() {
      TEST_MODE = 'image';
      if (TEST_TASK_ID && !(await prepareActiveTestTask('开始新任务'))) return;
      collectForms();
      clearTestData(false);
      setTestBusy(true);
      $('testStatus').textContent = '正在提交试画…';
      try {
        if (!$('testChannel').value) throw new Error('还没有可用渠道，先去「渠道」里启用');
        if (!$('testModel').value) throw new Error('这个渠道还没有可用模型，先启用模型');
        const images = [];
        for (const reference of TEST_EXTRA_REFS) {
          if (reference.image) images.push(reference.image);
          else if (reference.image_path) images.push((await getProtectedMedia(reference.image_path)).dataUrl);
        }
        const payload = {
          media_type: 'image',
          channel: $('testChannel').value,
          model: $('testModel').value,
          prompt: $('testPrompt').value.trim(),
          aspect_ratio: $('testAspect').value,
          resolution: $('testResolution').value,
          count: Number($('testCount')?.value || 1),
          prompt_enhance: false,
          use_selfie_reference: $('useSelfie').checked,
          images
        };
        $('testRequestData').textContent = JSON.stringify({...payload, images: `[${images.length} images]`}, null, 2);
        showTestPanel('request', false);
        const res = await api('/api/test-image-channel/tasks', {method:'POST', body: JSON.stringify(payload)});
        const task = res.data || {};
        TEST_TASK_ID = task.task_id || '';
        if (!TEST_TASK_ID) throw new Error('后台任务提交失败：未返回 task_id');
        safeStorageSet('selfieImageLastTestTaskId', TEST_TASK_ID);
        safeStorageSet('selfieImageLastTestMode', TEST_MODE);
        $('testResponseData').textContent = JSON.stringify(task, null, 2);
        $('testStatus').textContent = `后台任务 ${TEST_TASK_ID} 已提交，关闭页面不会停止任务。`;
        pollImageTestTask(TEST_TASK_ID);
      } catch (e) {
        setTestBusy(false);
        $('testStatus').textContent = e.message;
        $('testResponseData').textContent = JSON.stringify({success:false, error:e.message}, null, 2);
        showTestPanel('response', false);
      }
    }
    async function runImageTest() { return runMediaTest(); }
    function showTestPanel(name, open = true) {
      ['request','response','result'].forEach(key => $('test' + key[0].toUpperCase() + key.slice(1) + 'Panel').classList.toggle('active', key === name));
      $('testDataTitle').textContent = {request:'请求数据', response:'响应数据', result:'生成结果'}[name] || '试画数据';
      if (open) $('testDataModal').classList.add('show');
    }
    function closeTestPanel() { $('testDataModal').classList.remove('show'); }
    function clearTestData(clearStatus = true) {
      const active = !!String(TEST_TASK_ID || '').trim();
      if (!active) {
        clearTestTaskPoll();
        TEST_TASK_ID = '';
        safeStorageRemove('selfieImageLastTestTaskId');
        safeStorageRemove('selfieImageLastTestMode');
      }
      setTestBusy(active);
      $('testImages').innerHTML = '';
      $('testRequestData').textContent = '';
      $('testResponseData').textContent = '';
      if (clearStatus) $('testStatus').textContent = active ? `任务 ${TEST_TASK_ID} 仍在后台运行，展示已清空。` : '';
      showTestPanel('result', false);
    }
