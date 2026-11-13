    function navigateToTab(tab, options = {}) {
      const target = String(tab || '').trim();
      const button = [...document.querySelectorAll('nav.page-nav button[data-tab]')].find(item => item.dataset.tab === target);
      const section = target ? $(target) : null;
      if (!button || !section) return false;
      const current = document.querySelector('nav.page-nav button.active')?.dataset.tab || '';
      if (current === target && options.force !== true) return false;
      document.querySelectorAll('nav.page-nav button').forEach(item => item.classList.toggle('active', item === button));
      document.querySelectorAll('main.app-shell > section').forEach(item => item.classList.toggle('active', item === section));
      if (target === 'monitor') { loadRecords(false); loadMetrics(); loadTasks(); }
      if (target === 'assets') { loadAssets(); loadAssetTags(); loadAssetStudioPresets(); }
      if (target === 'studio') loadStudioList(STUDIO.current && STUDIO.current.id);
      if (target === 'creativeCanvas') loadCreativeCanvasList(CREATIVE_CANVAS.current && CREATIVE_CANVAS.current.id);
      if (target === 'presets') loadManagedPromptPresets(PRESET_KIND);
      if (target === 'selfie') refreshSelfie();
      if (target === 'proxies') { loadConfig().then(() => renderProxyList()).catch(() => renderProxyList()); }
      if (target === 'groups') { loadQQGroups(); }
      if (options.focus === true) button.focus();
      return true;
    }
    document.querySelectorAll('nav.page-nav button[data-tab]').forEach(btn => {
      btn.addEventListener('click', () => {
        // Keep the proxy tab check explicit for embedded-page integrations.
        const isProxyTab = btn.dataset.tab === 'proxies';
        navigateToTab(btn.dataset.tab, { focus: isProxyTab });
      });
    });
    document.querySelectorAll('[data-nav-target]').forEach(card => {
      card.addEventListener('click', () => navigateToTab(card.dataset.navTarget));
    });
    $('loginBtn').onclick = enterApp;
    $('refreshGroupsBtn')?.addEventListener('click', () => loadQQGroups(true));
    $('saveGroupsBtn')?.addEventListener('click', saveQQGroups);
    $('groupUsableEnabled')?.addEventListener('change', updateGlobalUsableGateUi);
    $('groupUsableUsers')?.addEventListener('input', updateGlobalUsableGateUi);
    for (const id of ['groupBlockedUsers', 'groupWhitelistUsers']) $(id)?.addEventListener('input', renderGroupAccessSummary);
    $('groupConfigSearch')?.addEventListener('input', renderGroupConfigModalOptions);
    $('loginToken').onkeydown = event => { if (event.key === 'Enter') enterApp(); };
    $('logoutBtn').onclick = logout;
    if ($('confirmProceedBtn')) $('confirmProceedBtn').onclick = () => closeConfirmAction(true);
    if ($('confirmCancelBtn')) $('confirmCancelBtn').onclick = () => closeConfirmAction(false);
    if ($('confirmCloseBtn')) $('confirmCloseBtn').onclick = () => closeConfirmAction(false);
    if ($('confirmModal')) $('confirmModal').addEventListener('click', event => {
      if (event.target === $('confirmModal')) closeConfirmAction(false);
    });
    document.querySelectorAll('.modal-mask').forEach(mask => mask.addEventListener('click', event => {
      if (event.target !== mask || mask === $('confirmModal')) return;
      if (mask === $('channelModal')) closeChannelModal();
      else if (mask === $('proxyModal')) closeProxyModal();
      else if (mask === $('recordModal')) closeRecordDetail();
      else if (mask === $('compareModal')) closeCompareModal();
      else if (mask === $('taskModal')) closeTaskDetail();
      else if (mask === $('metricsModal')) closeMetricsModal();
      else if (mask === $('mediaSourceModal')) closeMediaSourceModal();
      else if (mask === $('mediaLightboxModal')) closeMediaLightbox();
      else if (mask === $('presetEditorModal')) closePresetEditor();
      else if (mask === $('presetTransferModal')) closePresetTransfer();
      else if (mask === $('cosPoolModal')) closeCosPoolModal();
      else if (mask === $('studioCreateModal')) closeStudioCreateModal();
      else if (mask === $('studioConnectModal')) closeStudioConnectModal();
      else if (mask === $('creativeCanvasCreateModal')) closeCreativeCanvasCreateModal();
      else if (mask === $('groupConfigModal')) closeGroupConfigModal();
    }));
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      if ($('confirmModal')?.classList.contains('show')) { closeConfirmAction(false); return; }
      if ($('mediaSourceModal')?.classList.contains('show')) { closeMediaSourceModal(); return; }
      if ($('mediaLightboxModal')?.classList.contains('show')) { closeMediaLightbox(); return; }
      if ($('taskModal')?.classList.contains('show')) { closeTaskDetail(); return; }
      if ($('metricsModal')?.classList.contains('show')) { closeMetricsModal(); return; }
      if ($('recordModal')?.classList.contains('show')) { closeRecordDetail(); return; }
      if ($('compareModal')?.classList.contains('show')) { closeCompareModal(); return; }
      if ($('channelModal')?.classList.contains('show')) { closeChannelModal(); return; }
      if ($('proxyModal')?.classList.contains('show')) { closeProxyModal(); return; }
      if ($('presetEditorModal')?.classList.contains('show')) { closePresetEditor(); return; }
      if ($('presetTransferModal')?.classList.contains('show')) { closePresetTransfer(); return; }
      if ($('cosPoolModal')?.classList.contains('show')) { closeCosPoolModal(); return; }
      if ($('studioCreateModal')?.classList.contains('show')) { closeStudioCreateModal(); return; }
      if ($('studioConnectModal')?.classList.contains('show')) closeStudioConnectModal();
      if ($('creativeCanvasCreateModal')?.classList.contains('show')) { closeCreativeCanvasCreateModal(); return; }
      if ($('groupConfigModal')?.classList.contains('show')) { closeGroupConfigModal(); return; }
    });
    if ($('cleanupCacheBtn')) $('cleanupCacheBtn').onclick = cleanupCache;
    if ($('mediaLightboxClose')) $('mediaLightboxClose').onclick = closeMediaLightbox;
    if ($('mediaLightboxDownload')) {
      $('mediaLightboxDownload').innerHTML = downloadIconSvg();
      $('mediaLightboxDownload').onclick = () => {
        if (!MEDIA_LIGHTBOX_PATH) return;
        const filename = MEDIA_LIGHTBOX_PATH.split(/[\\/]/).pop() || 'image';
        downloadCachePath(MEDIA_LIGHTBOX_PATH, filename);
      };
    }
    $('presetAddBtn').onclick = () => openPresetEditor();
    $('presetKindImage').onclick = () => switchPresetKind('image');
    $('presetKindCos').onclick = () => switchPresetKind('cos');
    $('presetKindNai').onclick = () => switchPresetKind('nai');
    $('presetManagerSearch').oninput = event => {
      schedulePresetManagerSearch(PRESET_KIND === 'nai' ? 'nai' : 'image', event.target.value);
    };
    $('presetCosManagerSearch').oninput = event => {
      schedulePresetManagerSearch('cos', event.target.value);
    };
    $('presetImportBtn').onclick = openPresetImport;
    $('presetExportBtn').onclick = openPresetExport;
    $('presetCosMaintainBtn').onclick = openCosPoolModal;
    $('presetCosImportBtn').onclick = openCosPoolImport;
    $('presetCosExportBtn').onclick = exportCosPool;
    $('presetReloadBtn').onclick = async () => {
      clearPickerCache(PRESET_KIND);
      await loadManagedPromptPresets(PRESET_KIND);
    };
    $('presetEditorSaveBtn').onclick = saveManagedPromptPreset;
    $('presetTransferCopyBtn').onclick = copyPresetTransfer;
    $('presetTransferImportBtn').onclick = importPresetTransfer;
    $('reloadAll').onclick = async () => { clearPickerCache(); clearGalleryPickerCache(); await checkHealth(); await loadConfig(); await refreshSelfie(); await loadRecords(true); await loadMetrics(); await loadTasks(false); await loadAssets(true); await loadAssetTags(true); await loadAssetStudioPresets(); };
    $('pageSaveAll').onclick = saveAll;
    $('modalBaseUrlRestore') && ($('modalBaseUrlRestore').onclick = restoreModalBaseUrlDefault);
    $('modalSave').onclick = saveChannelModal;
    $('modalProvider').onchange = modalProviderChanged;
    $('modalRefreshModels').onclick = () => refreshChannelModels();
    $('modalEnableAll').onclick = () => { removeAllEnabledModels(); };
    $('cacheSearch').oninput = () => renderModalModels(currentModalChannel());
    $('manualAdd').onclick = () => {
      const value = $('manualModel').value.trim();
      if (!value) return;
      addEnabledModel(value);
      $('manualModel').value = '';
    };
    $('manualModel').onkeydown = event => { if (event.key === 'Enter') $('manualAdd').click(); };
    // Modal field edits stay local until「保存渠道」
    ['modalChannelName','modalBaseUrl','modalApiKey','modalProxyId','modalEnabled'].forEach(id => {
      const el = $(id);
      if (!el) return;
      const eventName = el.type === 'checkbox' || el.type === 'number' ? 'change' : 'input';
      el.addEventListener(eventName, () => { if (isChannelModalOpen()) CHANNEL_MODAL_DIRTY = true; });
    });
    $('testImageBtn').onclick = runImageTest;
    $('testCancelBtn').onclick = cancelTestTask;
    $('testRetryStatusBtn').onclick = () => {
      if (TEST_TASK_ID) pollImageTestTask(TEST_TASK_ID, 0);
    };
    if ($('testTemplatePreviewBtn')) $('testTemplatePreviewBtn').onclick = previewTestTemplate;
    if ($('testTemplateRandomBtn')) $('testTemplateRandomBtn').onclick = randomizeTestTemplateValues;
    if ($('testVariationEnabled')) $('testVariationEnabled').onchange = () => { $('testVariationFieldsWrap').style.display = $('testVariationEnabled').checked ? '' : 'none'; };
    if ($('studioCreateBtn')) $('studioCreateBtn').onclick = openStudioCreateModal;
    if ($('studioCreateConfirmBtn')) $('studioCreateConfirmBtn').onclick = studioCreate;
    if ($('studioCreateModal')) $('studioCreateModal').addEventListener('click', event => {
      if (event.target === $('studioCreateModal')) closeStudioCreateModal();
    });
    if ($('studioPresetBtn')) $('studioPresetBtn').onclick = toggleStudioPresets;
    if ($('studioCosBtn')) $('studioCosBtn').onclick = toggleStudioCos;
    if ($('testPresetBtn')) $('testPresetBtn').onclick = toggleTestPresets;
    if ($('testCosBtn')) $('testCosBtn').onclick = toggleTestCos;
    if ($('cosPoolModal')) $('cosPoolModal').addEventListener('click', event => { if (event.target === $('cosPoolModal')) closeCosPoolModal(); });
    if ($('studioReloadBtn')) $('studioReloadBtn').onclick = () => loadStudioList(STUDIO.current && STUDIO.current.id, {force:true});
    if ($('studioNodeSearch')) $('studioNodeSearch').oninput = event => {
      STUDIO.nodeSearch = event.target.value || '';
      studioRenderCanvas();
    };
    if ($('studioZoomIn')) $('studioZoomIn').onclick = () => studioZoomAt(1.1);
    if ($('studioZoomOut')) $('studioZoomOut').onclick = () => studioZoomAt(.9);
    if ($('studioZoomReset')) $('studioZoomReset').onclick = () => {
      STUDIO.viewport = {x: 120, y: 80, zoom: 1};
      studioRenderCanvas();
      studioPersistCanvas(0);
    };
    if ($('studioSidebarToggle')) $('studioSidebarToggle').onclick = () => {
      const layout = $('studioEditor');
      if (!layout) return;
      const collapsed = layout.classList.toggle('sidebar-collapsed');
      $('studioSidebarToggle').textContent = collapsed ? '›' : '‹';
      $('studioSidebarToggle').title = collapsed ? '展开画面列表' : '收起画面列表';
      $('studioSidebarToggle').setAttribute('aria-label', $('studioSidebarToggle').title);
    };
    if ($('studioMobileNodes')) $('studioMobileNodes').onclick = () => {
      $('studioEditor')?.classList.toggle('mobile-node-list-open');
    };
    if ($('studioNodePanelClose')) $('studioNodePanelClose').onclick = () => {
      STUDIO.selectedNodeId = '';
      studioRenderCanvas();
    };
    if ($('studioDeleteBtn')) $('studioDeleteBtn').onclick = studioDelete;
    if ($('studioSaveBtn')) $('studioSaveBtn').onclick = studioSave;
    if ($('studioRunBtn')) $('studioRunBtn').onclick = studioRun;
    if ($('studioRerunBtn')) $('studioRerunBtn').onclick = studioRun;
    if ($('studioCancelBtn')) $('studioCancelBtn').onclick = cancelStudioTask;
    if ($('studioRetryStatusBtn')) $('studioRetryStatusBtn').onclick = async () => {
      if (!STUDIO.current?.id) return;
      $('studioRetryStatusBtn').disabled = true;
      try { await loadStudioList(STUDIO.current.id, {force:true}); }
      catch (e) { $('studioStatus').textContent = e.message || '状态读取失败'; }
      finally { $('studioRetryStatusBtn').disabled = false; }
    };
    if ($('studioPickRecordBtn')) $('studioPickRecordBtn').onclick = () => openStudioGallery('');
    if ($('studioAddSlotBtn')) $('studioAddSlotBtn').onclick = async () => {
      if (!STUDIO.current) return showToast('请先新建画布', 'bad');
      const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/slots', {method:'POST', body: JSON.stringify({role:'extra', label:'额外参考'})});
      if (!res.success) return showToast(res.message || '失败', 'bad');
      STUDIO.current = res.data; renderStudio();
    };
    if ($('studioSessionSelect')) $('studioSessionSelect').onchange = async () => {
      const id = $('studioSessionSelect').value;
      if (!id) { STUDIO.current = null; renderStudio(); return; }
      await loadStudioList(id);
    };
    if ($('creativeCanvasSessionSelect')) $('creativeCanvasSessionSelect').onchange = () =>
      loadCreativeCanvasList($('creativeCanvasSessionSelect').value || '');
    if ($('creativeCanvasReloadBtn')) $('creativeCanvasReloadBtn').onclick = () =>
      loadCreativeCanvasList(CREATIVE_CANVAS.current && CREATIVE_CANVAS.current.id, {force:true});
    if ($('creativeCanvasDeleteBtn')) $('creativeCanvasDeleteBtn').onclick = deleteCreativeCanvas;
    if ($('creativeCanvasCreateBtn')) $('creativeCanvasCreateBtn').onclick = openCreativeCanvasCreateModal;
    if ($('creativeCanvasCreateConfirmBtn')) $('creativeCanvasCreateConfirmBtn').onclick = createCreativeCanvas;
    if ($('creativeCanvasSaveBtn')) $('creativeCanvasSaveBtn').onclick = saveCreativeCanvas;
    if ($('creativeCanvasRunBtn')) $('creativeCanvasRunBtn').onclick = runCreativeCanvas;
    if ($('creativeCanvasRerunBtn')) $('creativeCanvasRerunBtn').onclick = runCreativeCanvas;
    if ($('creativeCanvasCancelBtn')) $('creativeCanvasCancelBtn').onclick = cancelCreativeCanvasTask;
    if ($('creativeCanvasAddSlotBtn')) $('creativeCanvasAddSlotBtn').onclick = addCreativeCanvasSlot;
    if ($('creativeCanvasPresetBtn')) $('creativeCanvasPresetBtn').onclick = toggleCreativeCanvasPresets;
    if ($('creativeCanvasCosBtn')) $('creativeCanvasCosBtn').onclick = toggleCreativeCanvasCos;
    if ($('creativeCanvasFilePick')) $('creativeCanvasFilePick').onchange = async event => {
      const file = event.target.files && event.target.files[0];
      event.target.value = '';
      if (file) await uploadCreativeCanvasFile(file);
    };
    if ($('studioFilePick')) $('studioFilePick').onchange = async (ev) => {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      if (file) await studioUploadFile(file);
    };
    if ($('assetImportFile')) $('assetImportFile').onchange = async (ev) => {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      if (file) await importAssetsFile(file);
    };

    $('uploadSelfie').onclick = uploadSelfie;
    if ($('selfieAppearanceType')) $('selfieAppearanceType').onchange = saveAppearanceType;
    $('testChannel').onchange = refreshTestModels;
    function mirrorValue(a, b) {
      const sync = () => { if ($(b).value !== $(a).value) $(b).value = $(a).value; };
      $(a).addEventListener('input', sync);
      $(a).addEventListener('change', sync);
    }
    mirrorValue('defaultAspect', 'selfieAspect');
    mirrorValue('selfieAspect', 'defaultAspect');
    $('monitorSource').oninput = monitorFilterChanged;
    ['monitorMedia','monitorModel','monitorSuccess','monitorAsset'].forEach(id => {
      $(id).onchange = monitorFilterChanged;
    });
    if ($('monitorMetricButtons')) $('monitorMetricButtons').addEventListener('click', event => {
      const button = event.target.closest('[data-metrics-category]');
      if (button) openMetricsCategory(button.dataset.metricsCategory);
    });
    if ($('monitorMetricsWindow')) $('monitorMetricsWindow').onchange = () => loadMetrics();
    if ($('monitorTag')) $('monitorTag').oninput = monitorFilterChanged;
    if ($('monitorKeyword')) $('monitorKeyword').oninput = monitorFilterChanged;
    ['assetKeyword','assetSource','assetTag'].forEach(id => {
      if ($(id)) $(id).oninput = assetFilterChanged;
    });
    ['assetModel','assetMedia','assetSuccess','assetMark','assetSort','assetStartTime','assetEndTime'].forEach(id => {
      if ($(id)) $(id).onchange = assetFilterChanged;
    });
    if ($('assetStudioPresetSearch')) $('assetStudioPresetSearch').oninput = () => {
      clearTimeout(ASSET_STUDIO_PRESET_SEARCH_TIMER);
      ASSET_STUDIO_PRESET_PAGE = 1;
      ASSET_STUDIO_PRESET_SEARCH_TIMER = setTimeout(() => void loadAssetStudioPresets(1), 250);
    };
    if ($('assetStudioPresetPrev')) $('assetStudioPresetPrev').onclick = () =>
      void loadAssetStudioPresets(ASSET_STUDIO_PRESET_PAGE - 1);
    if ($('assetStudioPresetNext')) $('assetStudioPresetNext').onclick = () =>
      void loadAssetStudioPresets(ASSET_STUDIO_PRESET_PAGE + 1);

    (async function init() {
      for (const id of ['defaultAspect','selfieAspect','testAspect']) setSelectOptions(id, ASPECTS, '自动');
      setupAutoSave();
      // Ensure bridge is available before boot.
      // Still wait when running inside the plugin iframe so boot never races token login.
      const embedded = isEmbeddedFrame()
        || String(window.location.pathname || '').startsWith('/api/plugin/page/content/')
        || Boolean(window.AstrBotPluginPage);
      if (embedded) {
        if ($('loginPage')) $('loginPage').style.display = 'none';
        if ($('loginStatus')) $('loginStatus').textContent = '正在连上 AstrBot 管理通道…';
        if ($('loginHint')) $('loginHint').textContent = '插件页会沿用后台登录，不必再输管理口令。';
        try {
          DASHBOARD_BRIDGE = await waitForDashboardBridge(12000);
          if (typeof DASHBOARD_BRIDGE.ready === 'function') await DASHBOARD_BRIDGE.ready();
        } catch (e) {
          if ($('loginPage')) $('loginPage').style.display = '';
          if ($('loginStatus')) $('loginStatus').textContent = e.message || '管理页连接未就绪';
          const box = $('loginPage') && $('loginPage').querySelector('.login-box');
          if (box) {
            box.innerHTML = `<h1>管理页加载失败</h1><p class="muted">请刷新页面或稍后重试。</p><div class="status">${escapeHtml(e.message || '连接未就绪')}</div>`;
          }
          return;
        }
        await enterApp(true);
        return;
      }
      // Do not probe protected endpoints before the user supplies a token.
      // Stored tokens still restore the standalone page automatically.
      if (AUTH_TOKEN) await enterApp(true);
    })();
