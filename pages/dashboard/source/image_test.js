    async function readFileDataUrl(file) {
      return await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    }
    function testTemplateValues() {
      const map = {
        role: 'testTemplateRole', outfit: 'testTemplateOutfit', scene: 'testTemplateScene',
        pose: 'testTemplatePose', shot: 'testTemplateShot', view: 'testTemplateView',
        lighting: 'testTemplateLighting'
      };
      const values = {};
      Object.entries(map).forEach(([key, id]) => {
        const value = String($(id)?.value || '').trim();
        if (value) values[key] = value;
      });
      return values;
    }
    const TEST_TEMPLATE_RANDOM_POOLS = {
      role: ['成年女性', '成年男性', '东方女性', '短发女孩', '年轻男生'],
      outfit: ['白色衬衫与牛仔裤', '红色旗袍', '黑色西装', '浅色针织衫', '简洁的夏日连衣裙'],
      scene: ['窗边客厅', '安静庭院', '城市街角', '简洁影棚', '黄昏海边'],
      pose: ['自然站姿', '轻步转身', '侧身回看', '双手自然放在身侧', '坐姿看向镜头'],
      shot: ['竖屏半身', '三分之四侧面', '环境人像', '近距离人像', '全身构图'],
      view: ['自拍视角', '他拍视角', '平视视角', '轻微低机位'],
      lighting: ['窗光', '柔和室内光', '阴天散射光', '暖色环境光', '夕阳轮廓光']
    };
    function randomTemplateValue(values, current = '') {
      const options = Array.isArray(values) ? values.filter(Boolean) : [];
      if (!options.length) return '';
      const available = options.filter(value => value !== current);
      return (available.length ? available : options)[Math.floor(Math.random() * (available.length || options.length))];
    }
    function randomizeTestTemplateValues() {
      const fields = {
        role: 'testTemplateRole', outfit: 'testTemplateOutfit', scene: 'testTemplateScene',
        pose: 'testTemplatePose', shot: 'testTemplateShot', view: 'testTemplateView', lighting: 'testTemplateLighting'
      };
      Object.entries(fields).forEach(([key, id]) => {
        const input = $(id);
        if (!input) return;
        input.value = randomTemplateValue(TEST_TEMPLATE_RANDOM_POOLS[key], String(input.value || '').trim());
        input.dispatchEvent(new Event('input', {bubbles:true}));
      });
      const preview = $('testTemplatePreview');
      if (preview) {
        preview.style.display = '';
        preview.textContent = '已填充一组随机模板变量，点击“预览模板”查看组合结果。';
      }
    }
    function testVariationFields() {
      return Array.from(document.querySelectorAll('[data-test-vary]:checked')).map(el => String(el.value || '').trim()).filter(Boolean);
    }
    function creativeTestPayload() {
      const variationEnabled = !!$('testVariationEnabled')?.checked;
      return {
        template_values: testTemplateValues(),
        template_randomize: !!$('testTemplateRandomize')?.checked,
        variation_enabled: variationEnabled,
        variation_fields: variationEnabled ? testVariationFields() : []
      };
    }
    async function previewTestTemplate() {
      const target = $('testTemplatePreview');
      if (!target) return;
      const body = {prompt: String($('testPrompt')?.value || ''), ...creativeTestPayload()};
      target.style.display = '';
      target.textContent = '正在预览…';
      try {
        const res = await api('/api/creative/template/render', {method:'POST', body: JSON.stringify(body)});
        const data = res.data || {};
        const unresolved = Array.isArray(data.unresolved) && data.unresolved.length ? `；未填写：${data.unresolved.join('、')}` : '';
        const randomized = data.randomized && Object.keys(data.randomized).length ? `；随机：${Object.entries(data.randomized).map(([k,v]) => `${k}=${v}`).join('、')}` : '';
        target.textContent = `预览：${data.prompt || body.prompt || ''}${unresolved}${randomized}`;
      } catch (e) { target.textContent = e.message || '模板预览失败'; }
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
      if ($('promptEnhanceWrap')) $('promptEnhanceWrap').style.display = '';
      $('useSelfieWrap').style.display = '';
      $('useSelfie').disabled = false;
      $('testRefs').multiple = true;
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
        showTestPanel('response');
        return;
      }
      const generatedCount = Number(data.succeeded_count || data.generated_image_paths?.length || 0);
      const prefix = partial ? '部分完成' : '成功';
      $('testStatus').textContent = `${prefix}：${data.used_model || ''}，耗时 ${data.elapsed_seconds}s，${generatedCount || 1} 张，参考图 ${data.reference_images || 0} 张${partial && data.error ? `；${data.error}` : ''}`;
      $('testImages').innerHTML = imageThumbs(data.generated_image_paths || [], '生成图', data.generated_image_sources || []);
      loadProtectedImages($('testImages'));
      showTestPanel('result');
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
        showTestPanel('response');
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
        for (const file of $('testRefs').files) {
          images.push(await readFileDataUrl(file));
        }
        const payload = {
          media_type: 'image',
          channel: $('testChannel').value,
          model: $('testModel').value,
          prompt: $('testPrompt').value.trim(),
          aspect_ratio: $('testAspect').value,
          resolution: $('testResolution').value,
          count: Number($('testCount')?.value || 1),
          prompt_enhance: $('promptEnhance').checked,
          use_selfie_reference: $('useSelfie').checked,
          images,
          ...creativeTestPayload()
        };
        $('testRequestData').textContent = JSON.stringify({...payload, images: `[${images.length} images]`}, null, 2);
        showTestPanel('request');
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
        showTestPanel('response');
      }
    }
    async function runImageTest() { return runMediaTest(); }
    function showTestPanel(name) {
      ['request','response','result'].forEach(key => $('test' + key[0].toUpperCase() + key.slice(1) + 'Panel').classList.toggle('active', key === name));
    }
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
      showTestPanel('result');
    }
