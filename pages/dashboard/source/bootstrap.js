    function scrollTabIntoView(button) {
      if (!button) return;
      const tablist = button.closest('.tabs-inline');
      if (!tablist) return;
      const bounds = tablist.getBoundingClientRect(), tabBounds = button.getBoundingClientRect();
      if (tabBounds.left < bounds.left) tablist.scrollLeft += tabBounds.left - bounds.left;
      else if (tabBounds.right > bounds.right) tablist.scrollLeft += tabBounds.right - bounds.right;
    }
    function setConfigurationNavigation(collapsed, restoreFocus = false) {
      $('configurationNav').hidden = collapsed;
      const toggle = $('configurationToggle');
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.title = collapsed ? '展开配置导航' : '收起配置导航';
      toggle.setAttribute('aria-label', toggle.title);
      if (restoreFocus) toggle.focus({preventScroll: true});
    }
    $('configurationToggle').onclick = () => {
      setConfigurationNavigation(!$('configurationNav').hidden);
    };
    document.addEventListener('pointerdown', event => {
      if (!$('configurationNav').hidden && !$('configurationHeader').contains(event.target)) setConfigurationNavigation(true);
    });
    $('configurationHeader').addEventListener('keydown', event => {
      if (event.key === 'Escape' && !$('configurationNav').hidden) {
        event.preventDefault(); event.stopPropagation(); setConfigurationNavigation(true, true);
      }
    });
    function navigateToTab(tab, options = {}) {
      let target = String(tab || '').trim();
      if (target === 'configuration') {
        target = document.querySelector('#configurationNav button.active')?.dataset.configTab || 'channels';
      }
      const configButtons = [...document.querySelectorAll('[data-config-tab]')];
      const configButton = configButtons.find(item => item.dataset.configTab === target);
      const navTarget = configButton ? 'configuration' : target;
      const button = [...document.querySelectorAll('nav.page-nav button[data-tab]')].find(item => item.dataset.tab === navTarget);
      const section = target ? $(target) : null;
      if (!button || !section) return false;
      DASHBOARD_VISITED_TABS.add(target);
      const current = document.querySelector('main.app-shell > section.active')?.id || '';
      const currentNav = document.querySelector('nav.page-nav button.active')?.dataset.tab || '';
      if (current === target && options.force !== true) return false;
      document.querySelectorAll('nav.page-nav button').forEach(item => {
        item.classList.toggle('active', item === button);
        item.classList.remove('nav-switching');
        if (item === button) item.setAttribute('aria-current', 'page');
        else item.removeAttribute('aria-current');
      });
      if (currentNav !== navTarget && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) button.classList.add('nav-switching');
      $('configurationHeader').hidden = !configButton;
      if (!configButton) setConfigurationNavigation(true);
      if (configButton) configButtons.forEach(item => {
        const active = item === configButton;
        item.classList.toggle('active', active);
        item.setAttribute('aria-selected', String(active));
        item.tabIndex = active ? 0 : -1;
      });
      document.querySelectorAll('main.app-shell > section').forEach(item => item.classList.toggle('active', item === section));
      scrollTabIntoView(configButton);
      if (configButton && currentNav !== navTarget) window.scrollTo(0, 0);
      if (target === 'monitor') { loadRecords(false); loadMetrics(); loadTasks(); }
      if (target === 'assets') { loadAssets(); loadAssetTags(); loadAssetCollections(); }
      if (target === 'studio') loadStudioList(STUDIO.current && STUDIO.current.id);
      if (target === 'presets') loadManagedPromptPresets(PRESET_KIND);
      if (target === 'selfie') refreshSelfie();
      if (target === 'backup') loadBackupManager();
      if (target === 'proxies') { loadConfig().then(() => renderProxyList()).catch(() => renderProxyList()); }
      if (target === 'groups') { loadQQGroups(); }
      if (options.focus === true) (configButton || button).focus({preventScroll: true});
      return true;
    }
    document.querySelectorAll('nav.page-nav button[data-tab]').forEach(btn => {
      btn.addEventListener('click', () => {
        navigateToTab(btn.dataset.tab);
      });
    });
    document.querySelectorAll('[data-config-tab]').forEach(btn => {
      btn.addEventListener('click', () => {
        navigateToTab(btn.dataset.configTab);
        setConfigurationNavigation(true, true);
      });
    });
    document.querySelectorAll('.tabs-inline[role="tablist"]').forEach(tablist => {
      tablist.addEventListener('keydown', event => {
        if (event.defaultPrevented) return;
        const buttons = [...tablist.querySelectorAll(':scope > button[role="tab"]')];
        const index = buttons.indexOf(event.target);
        if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
          : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
        if (tablist !== $('configurationNav')) buttons[next].click();
        buttons[next].focus({preventScroll: true});
        scrollTabIntoView(buttons[next]);
      });
    });
    document.querySelectorAll('[data-nav-target]').forEach(card => {
      card.addEventListener('click', () => navigateToTab(card.dataset.navTarget));
    });
    $('loginBtn').onclick = enterApp;
    $('channelModalCloseButton')?.addEventListener('click', closeChannelModal);
    $('embeddedBootRetry')?.addEventListener('click', () => void initializeEmbeddedDashboard());
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
      else if (mask === $('cacheModelsModal')) closeCacheModels();
      else if (mask === $('modelSettingsModal')) closeModelSettings();
      else if (mask === $('proxyModal')) closeProxyModal();
      else if (mask === $('proxyInfoModal')) closeProxyInfo();
      else if (mask === $('testDataModal')) closeTestPanel();
      else if (mask === $('referenceGalleryModal')) closeReferenceGallery();
      else if (mask === $('catalogPickerModal')) closeCatalogPicker();
      else if (mask === $('recordModal')) closeRecordDetail();
      else if (mask === $('recordFieldModal')) closeRecordField();
      else if (mask === $('compareModal')) closeCompareModal();
      else if (mask === $('taskModal')) closeTaskDetail();
      else if (mask === $('taskQueueModal')) closeTaskQueue();
      else if (mask === $('metricsModal')) closeMetricsModal();
      else if (mask === $('metricAlertSettingsModal')) closeMetricAlertSettings();
      else if (mask === $('mediaSourceModal')) closeMediaSourceModal();
      else if (mask === $('mediaLightboxModal')) closeMediaLightbox();
      else if (mask === $('presetEditorModal')) closePresetEditor();
      else if (mask === $('cosPoolModal')) closeCosPoolModal();
      else if (mask === $('studioCreateModal')) closeStudioCreateModal();
      else if (mask === $('studioConnectModal')) closeStudioConnectModal();
      else if (mask === $('groupConfigModal')) closeGroupConfigModal();
      else if (mask === $('backupRestoreModal')) closeBackupRestore();
    }));
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      if ($('confirmModal')?.classList.contains('show')) { closeConfirmAction(false); return; }
      if ($('mediaLightboxModal')?.classList.contains('show')) { closeMediaLightbox(); return; }
      if ($('cosPoolModal')?.classList.contains('show')) { closeCosPoolModal(); return; }
      if ($('catalogPickerModal')?.classList.contains('show')) { closeCatalogPicker(); return; }
      if ($('referenceGalleryModal')?.classList.contains('show')) { closeReferenceGallery(); return; }
      if ($('testDataModal')?.classList.contains('show')) { closeTestPanel(); return; }
      if ($('proxyInfoModal')?.classList.contains('show')) { closeProxyInfo(); return; }
      if ($('backupRestoreModal')?.classList.contains('show')) { closeBackupRestore(); return; }
      if ($('mediaSourceModal')?.classList.contains('show')) { closeMediaSourceModal(); return; }
      if ($('mediaLightboxModal')?.classList.contains('show')) { closeMediaLightbox(); return; }
      if ($('recordFieldModal')?.classList.contains('show')) { closeRecordField(); return; }
      if ($('taskModal')?.classList.contains('show')) { closeTaskDetail(); return; }
      if ($('taskQueueModal')?.classList.contains('show')) { closeTaskQueue(); return; }
      if ($('metricsModal')?.classList.contains('show')) { closeMetricsModal(); return; }
      if ($('metricAlertSettingsModal')?.classList.contains('show')) { closeMetricAlertSettings(); return; }
      if ($('recordModal')?.classList.contains('show')) { closeRecordDetail(); return; }
      if ($('compareModal')?.classList.contains('show')) { closeCompareModal(); return; }
      if ($('modelSettingsModal')?.classList.contains('show')) { closeModelSettings(); return; }
      if ($('cacheModelsModal')?.classList.contains('show')) { closeCacheModels(); return; }
      if ($('channelModal')?.classList.contains('show')) { closeChannelModal(); return; }
      if ($('proxyModal')?.classList.contains('show')) { closeProxyModal(); return; }
      if ($('presetEditorModal')?.classList.contains('show')) { closePresetEditor(); return; }
      if ($('cosPoolModal')?.classList.contains('show')) { closeCosPoolModal(); return; }
      if ($('studioCreateModal')?.classList.contains('show')) { closeStudioCreateModal(); return; }
      if ($('studioConnectModal')?.classList.contains('show')) closeStudioConnectModal();
      if ($('groupConfigModal')?.classList.contains('show')) { closeGroupConfigModal(); return; }
    });
    if ($('cleanupCacheBtn')) $('cleanupCacheBtn').onclick = cleanupCache;
    if ($('recordFieldCopy')) $('recordFieldCopy').innerHTML = copyIconSvg();
    if ($('mediaLightboxClose')) $('mediaLightboxClose').onclick = closeMediaLightbox;
    if ($('mediaLightboxDownload')) {
      $('mediaLightboxDownload').innerHTML = downloadIconSvg();
      $('mediaLightboxDownload').onclick = () => {
        if (MEDIA_LIGHTBOX_DOWNLOAD) { void MEDIA_LIGHTBOX_DOWNLOAD(); return; }
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
    $('presetManagerFilter').onchange = event => {
      PRESET_MANAGER_FILTERS.image = String(event.target.value || '');
      PRESET_MANAGER_PAGES.image = 1;
      void loadManagedPromptPresets('image');
    };
    $('presetCosManagerSearch').oninput = event => {
      schedulePresetManagerSearch('cos', event.target.value);
    };
    $('presetCosManagerSeries').onchange = event => {
      PRESET_MANAGER_FILTERS.cos = String(event.target.value || '');
      PRESET_MANAGER_PAGES.cos = 1;
      void loadManagedCosPool();
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
    $('reloadAll').onclick = async () => { clearPickerCache(); clearGalleryPickerCache(); invalidateMetricsRequests(); await checkHealth(); await loadConfig(); await refreshSelfie(); await loadRecords(true); await loadMetrics(); await loadTasks(false); await loadAssets(true); await loadAssetTags(true); await loadAssetCollections(true); };
    $('pageSaveAll').onclick = saveAll;
    $('modalBaseUrlRestore') && ($('modalBaseUrlRestore').onclick = restoreModalBaseUrlDefault);
    $('modalSave').onclick = saveChannelModal;
    $('manualFillNai').onclick = fillNovelAIModels;
    $('modalProvider').onchange = modalProviderChanged;
    $('modalRefreshModels').onclick = () => refreshChannelModels();
    $('modalEnableAll').onclick = () => { removeAllEnabledModels(); };
    $('openCacheModels').onclick = openCacheModels;
    $('cacheSearch').oninput = () => { CACHE_MODELS_PAGE = 1; renderCacheModels(); $('cacheModels').scrollTop = 0; };
    $('manualAdd').onclick = () => {
      const value = $('manualModel').value.trim();
      if (!value) return;
      addEnabledModel(value);
      $('manualModel').value = '';
    };
    $('manualModel').onkeydown = event => { if (event.key === 'Enter') $('manualAdd').click(); };
    // Modal field edits stay local until「保存渠道」
    ['modalChannelName','modalBaseUrl','modalApiKey','modalProxyId','modalEnabled','modalNaiNegative','modalNaiStrength'].forEach(id => {
      const el = $(id);
      if (!el) return;
      const eventName = el.type === 'checkbox' || el.type === 'number' ? 'change' : 'input';
      el.addEventListener(eventName, () => { if (isChannelModalOpen()) CHANNEL_MODAL_DIRTY = true; });
    });
    $('testImageBtn').onclick = runImageTest;
    renderTestExtraReferences();
    document.querySelectorAll('[data-identity-mode]').forEach(button => {
      button.onclick = () => {
        const checkbox = $('selfiePersonaEnabled');
        if (checkbox.disabled || button.getAttribute('aria-pressed') === 'true') return;
        checkbox.checked = button.dataset.identityMode === 'persona';
        void saveSelfieIdentityMode();
      };
    });
    $('testCancelBtn').onclick = cancelTestTask;
    $('testRetryStatusBtn').onclick = () => {
      if (TEST_TASK_ID) pollImageTestTask(TEST_TASK_ID, 0);
    };
    if ($('studioCreateBtn')) $('studioCreateBtn').onclick = openStudioCreateModal;
    if ($('studioCreateConfirmBtn')) $('studioCreateConfirmBtn').onclick = studioCreate;
    if ($('studioCreateModal')) $('studioCreateModal').addEventListener('click', event => {
      if (event.target === $('studioCreateModal')) closeStudioCreateModal();
    });
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
    if ($('studioFilePick')) $('studioFilePick').onchange = async (ev) => {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      if (file) await studioUploadFile(file);
    };

    $('uploadSelfie').onclick = uploadSelfie;
    const selfieViewTabs = [...document.querySelectorAll('[data-selfie-view]')];
    selfieViewTabs.forEach((tab, index) => {
      tab.addEventListener('click', () => switchSelfieView(tab.dataset.selfieView));
      tab.addEventListener('keydown', event => {
        let nextIndex = index;
        if (event.key === 'ArrowRight') nextIndex = (index + 1) % selfieViewTabs.length;
        else if (event.key === 'ArrowLeft') nextIndex = (index + selfieViewTabs.length - 1) % selfieViewTabs.length;
        else if (event.key === 'Home') nextIndex = 0;
        else if (event.key === 'End') nextIndex = selfieViewTabs.length - 1;
        else return;
        event.preventDefault();
        selfieViewTabs[nextIndex].focus();
        switchSelfieView(selfieViewTabs[nextIndex].dataset.selfieView);
      });
    });
    const auditViewTabs = [...document.querySelectorAll('[data-audit-view]')];
    auditViewTabs.forEach((tab, index) => {
      tab.addEventListener('click', () => switchAuditView(tab.dataset.auditView));
      tab.addEventListener('keydown', event => {
        let nextIndex = index;
        if (event.key === 'ArrowRight') nextIndex = (index + 1) % auditViewTabs.length;
        else if (event.key === 'ArrowLeft') nextIndex = (index + auditViewTabs.length - 1) % auditViewTabs.length;
        else if (event.key === 'Home') nextIndex = 0;
        else if (event.key === 'End') nextIndex = auditViewTabs.length - 1;
        else return;
        event.preventDefault();
        auditViewTabs[nextIndex].focus();
        switchAuditView(auditViewTabs[nextIndex].dataset.auditView);
      });
    });
    if ($('refreshSelfieBtn')) $('refreshSelfieBtn').onclick = () => refreshSelfie(true);
    if ($('clearSelfieBtn')) $('clearSelfieBtn').onclick = clearSelfie;
    if ($('selfiePersonaEnabled')) $('selfiePersonaEnabled').onchange = saveSelfieIdentityMode;
    if ($('addSelfiePersona')) $('addSelfiePersona').onclick = addSelfiePersona;
    if ($('selfiePersonaPrompt')) $('selfiePersonaPrompt').onkeydown = event => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') addSelfiePersona();
    };
    if ($('uploadWardrobe')) $('uploadWardrobe').onclick = uploadWardrobe;
    if ($('wardrobeSelectionMode')) $('wardrobeSelectionMode').onchange = saveWardrobeMode;
    if ($('wardrobeBatchCancel')) $('wardrobeBatchCancel').onclick = cancelWardrobeBatch;
    if ($('wardrobeFile')) $('wardrobeFile').onchange = () => {
      WARDROBE_PENDING_UPLOADS = null;
      $('uploadWardrobe').textContent = '添加到衣橱';
      $('wardrobeBatchProgressWrap').hidden = true;
    };
    if ($('refreshWardrobe')) $('refreshWardrobe').onclick = () => refreshSelfie(true);
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
    $('saveMetricAlertSettings')?.addEventListener('click', saveMetricAlertSettings);
    $('openMetricAlertSettings')?.addEventListener('click', openMetricAlertSettings);
    $('closeMetricAlertSettings')?.addEventListener('click', closeMetricAlertSettings);
    $('metricAlertSettingsModal')?.addEventListener('keydown', event => {
      if (event.key !== 'Tab') return;
      const controls = event.currentTarget.querySelectorAll('button:not(:disabled), input:not(:disabled)');
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (document.activeElement !== (event.shiftKey ? first : last)) return;
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    });
    $('metricAlertSettings')?.querySelectorAll('input').forEach(input => {
      input.addEventListener('input', () => { $('metricAlertSettings').dataset.dirty = 'true'; });
      input.addEventListener('change', () => { $('metricAlertSettings').dataset.dirty = 'true'; });
    });
    if ($('monitorMetricsWindow')) $('monitorMetricsWindow').onchange = () => loadMetrics();
    if ($('monitorTag')) $('monitorTag').oninput = monitorFilterChanged;
    if ($('monitorKeyword')) $('monitorKeyword').oninput = monitorFilterChanged;
    ['assetKeyword','assetSource','assetTag'].forEach(id => {
      if ($(id)) $(id).oninput = assetFilterChanged;
    });
    ['assetModel','assetSuccess','assetMark','assetSort','assetStartTime','assetEndTime'].forEach(id => {
      if ($(id)) $(id).onchange = assetFilterChanged;
    });

    let EMBEDDED_BOOT_BUSY = false;
    async function initializeEmbeddedDashboard() {
      if (EMBEDDED_BOOT_BUSY) return;
      EMBEDDED_BOOT_BUSY = true;
      const retry = $('embeddedBootRetry');
      const message = $('embeddedBootMessage');
      if (retry) { retry.hidden = true; retry.disabled = true; }
      if (message) message.textContent = '正在连接 AstrBot 管理通道…';
      try {
        DASHBOARD_BRIDGE = await waitForDashboardBridge(120000);
        if (typeof DASHBOARD_BRIDGE.ready === 'function') await DASHBOARD_BRIDGE.ready();
        if (message) message.textContent = '通道已连接，正在读取管理配置…';
        await enterApp(true);
        if (!document.body.classList.contains('authed')) throw new Error(message?.textContent || '管理配置读取失败');
      } catch (e) {
        if (message) message.textContent = e.message || '管理通道暂不可用';
        if (retry) retry.hidden = false;
      } finally {
        EMBEDDED_BOOT_BUSY = false;
        if (retry) retry.disabled = false;
      }
    }

    (async function init() {
      for (const id of ['defaultAspect','selfieAspect','testAspect']) setSelectOptions(id, ASPECTS, '自动');
      setupAutoSave();
      // Ensure bridge is available before boot.
      // Still wait when running inside the plugin iframe so boot never races token login.
      const embedded = isEmbeddedFrame()
        || String(window.location.pathname || '').startsWith('/api/plugin/page/content/')
        || Boolean(window.AstrBotPluginPage);
      if (embedded) {
        document.documentElement.classList.add('embedded-dashboard');
        if ($('loginPage')) $('loginPage').style.display = 'none';
        void initializeEmbeddedDashboard();
        return;
      }
      // Do not probe protected endpoints before the user supplies a token.
      // Stored tokens still restore the standalone page automatically.
      if (AUTH_TOKEN) await enterApp(true);
    })();
