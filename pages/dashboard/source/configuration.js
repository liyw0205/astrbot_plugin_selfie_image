    function groupConfigValue(index, field) {
      return String(QQ_GROUP_ROWS[index]?.config?.[field] || (field === 'chat_type' ? 'none' : field === 'model_priority_mode' ? 'default' : ''));
    }
    function globalUsableGateActive() {
      return !!$('groupUsableEnabled')?.checked && textList('groupUsableUsers').length > 0;
    }
    function updateGlobalUsableGateUi() {
      const active = globalUsableGateActive();
      const panel = $('groupUserAccessPanel');
      panel?.classList.toggle('group-policy-overridden', active);
      for (const id of ['groupBlockedUsers', 'groupWhitelistUsers']) {
        const input = $(id);
        if (input) input.disabled = active;
      }
      const note = $('groupUsableNote');
      if (note) note.hidden = !active;
      const save = $('saveGroupsBtn');
      if (save) save.disabled = false;
      if (active && QQ_GROUP_MODAL?.field === 'chat_type') closeGroupConfigModal();
      renderGroupAccessSummary();
      renderQQGroups();
    }
    function renderGroupAccessSummary() {
      const target = $('groupAccessSummary');
      if (!target) return;
      const permission = CONFIG.permission || {};
      const format = (ids, kind, reasons = {}) => ids.map(id => {
        const reason = String(reasons?.[id] || '').trim();
        return `${id}(${kind}${reason ? `：${reason}` : ''})`;
      });
      const groups = [
        QQ_BOT_IDS.map(id => `${id}(机器人)`),
        format(textList('groupUsableUsers'), '可用'),
        format(textList('groupBlockedUsers'), '黑名单', permission.blocked_user_reasons),
        format(textList('groupWhitelistUsers'), '白名单', permission.whitelist_user_reasons),
      ].filter(items => items.length).map(items => items.join(','));
      target.textContent = groups.length ? groups.join(' | ') : '暂无名单条目';
    }
    function groupConfigLabel(field, value) {
      const choices = field === 'chat_type' ? QQ_GROUP_CHAT_TYPES : QQ_GROUP_MODEL_MODES;
      return choices.find(item => item[0] === value)?.[1] || '未设置';
    }
    function closeGroupConfigModal() {
      $('groupConfigModal')?.classList.remove('show');
      QQ_GROUP_MODAL = null;
    }
    function openGroupConfigModal(index, field) {
      if (field === 'chat_type' && globalUsableGateActive()) return;
      const row = QQ_GROUP_ROWS[index];
      if (!row) return;
      QQ_GROUP_MODAL = {index, field};
      const modal = $('groupConfigModal');
      const title = field === 'chat_type' ? '群聊类型' : field === 'model_priority_mode' ? '模型模式' : '固定模型';
      $('groupConfigModalTitle').textContent = title;
      $('groupConfigModalHint').textContent = `${row.group_id}${row.group_name ? ` · ${row.group_name}` : ''}`;
      $('groupConfigSearchWrap').style.display = field === 'fixed_model' ? '' : 'none';
      $('groupConfigSearch').value = '';
      renderGroupConfigModalOptions();
      modal?.classList.add('show');
      modal?.querySelector('.group-config-option')?.focus();
    }
    function renderGroupConfigModalOptions() {
      const state = QQ_GROUP_MODAL;
      const body = $('groupConfigModalBody');
      if (!state || !body) return;
      const current = groupConfigValue(state.index, state.field);
      if (state.field === 'fixed_model') {
        const query = String($('groupConfigSearch')?.value || '').trim().toLowerCase();
        const models = QQ_GROUP_MODELS.filter(model => !query || model.toLowerCase().includes(query));
        body.innerHTML = models.length ? `<div class="group-config-model-list">${models.map(model => `<button class="group-config-option" type="button" aria-pressed="${model === current}" onclick="selectGroupConfigValue('${escapeJs(model)}')"><strong>${escapeHtml(model)}</strong><span>${model === current ? '当前使用' : '点击设为固定模型'}</span></button>`).join('')}</div>` : '<div class="group-config-empty">暂无匹配的已启用模型。</div>';
        return;
      }
      const choices = state.field === 'chat_type' ? QQ_GROUP_CHAT_TYPES : QQ_GROUP_MODEL_MODES;
      body.innerHTML = `<div class="group-config-options">${choices.map(([value, label, hint]) => `<button class="group-config-option" type="button" aria-pressed="${value === current}" onclick="selectGroupConfigValue('${value}')"><strong>${label}</strong><span>${hint}</span></button>`).join('')}</div>`;
    }
    function selectGroupConfigValue(value) {
      const state = QQ_GROUP_MODAL;
      const config = state && QQ_GROUP_ROWS[state.index]?.config;
      if (!state || !config) return;
      config[state.field] = value;
      if (state.field === 'model_priority_mode' && value !== 'fixed') config.fixed_model = '';
      closeGroupConfigModal();
      renderQQGroups();
    }
    function renderQQGroups() {
      const body = $('groupConfigRows');
      if (!body) return;
      if (!QQ_GROUP_ROWS.length) {
        body.innerHTML = '<div class="group-config-empty">没有读取到 QQ 群，或当前没有可用 QQ 平台。</div>';
        return;
      }
      body.innerHTML = QQ_GROUP_ROWS.map((row, index) => {
        const config = row.config || {};
        const mode = config.model_priority_mode || 'default';
        const fixed = mode === 'fixed' ? (config.fixed_model || '未选择') : '不适用';
        return `<div class="group-config-row" data-group-index="${index}" role="row">
          <div class="group-config-identity"><strong>${escapeHtml(row.group_id)}</strong><span title="${escapeHtml(row.group_name || '')}">${escapeHtml(row.group_name || '未命名群')}</span></div>
          <button class="group-config-trigger${globalUsableGateActive() ? ' policy-overridden' : ''}" data-config-field="chat_type" type="button" ${globalUsableGateActive() ? 'disabled title="由全局可用名单覆盖"' : `onclick="openGroupConfigModal(${index}, 'chat_type')"`}><span>${escapeHtml(groupConfigLabel('chat_type', config.chat_type || 'none'))}</span></button>
          <button class="group-config-trigger" type="button" onclick="openGroupConfigModal(${index}, 'model_priority_mode')"><span>${escapeHtml(groupConfigLabel('model_priority_mode', mode))}</span></button>
          <button class="group-config-trigger" type="button" ${mode === 'fixed' ? `onclick="openGroupConfigModal(${index}, 'fixed_model')"` : 'disabled'}><span>${escapeHtml(fixed)}</span></button>
        </div>`;
      }).join('');
    }
    async function loadQQGroups(force = false) {
      try {
        setStatus('groupsStatus', '正在读取 QQ 群列表…');
        const res = await api('/api/qq-groups' + (force ? '?refresh=1' : ''));
        const data = res.data || {};
        QQ_GROUP_MODELS = Array.isArray(data.models) ? uniq(data.models) : [];
        QQ_BOT_IDS = Array.isArray(data.bot_ids) ? uniq(data.bot_ids.map(String)) : [];
        QQ_GROUP_ROWS = (Array.isArray(data.groups) ? data.groups : []).map(row => ({...row, config: {
          chat_type: row.config?.chat_type || 'none',
          model_priority_mode: row.config?.model_priority_mode || 'default',
          fixed_model: row.config?.fixed_model || '',
        }}));
        $('saveGroupsBtn').disabled = false;
        const resultText = data.available ? `已读取 ${QQ_GROUP_ROWS.length} 个群` : (data.message || 'QQ 平台不可用');
        setStatus('groupsStatus', force ? '' : resultText);
        if (force) showToast(resultText, data.available ? 'ok' : 'info');
        renderGroupAccessSummary();
        renderQQGroups();
      } catch (error) {
        const message = error.message || '读取群列表失败';
        setStatus('groupsStatus', force ? '' : message);
        if (force) showToast(message, 'bad');
      }
    }
    async function saveQQGroups() {
      try {
        const rows = QQ_GROUP_ROWS.map(row => ({group_id: row.group_id, config: row.config}));
        const blockedUsers = textList('groupBlockedUsers');
        const whitelistUsers = textList('groupWhitelistUsers');
        const blockedReasons = CONFIG.permission?.blocked_user_reasons || {};
        const whitelistReasons = CONFIG.permission?.whitelist_user_reasons || {};
        const permission = {
          usable_users: textList('groupUsableUsers'),
          usable_users_enabled: $('groupUsableEnabled').checked,
          blocked_users: blockedUsers,
          blocked_user_reasons: Object.fromEntries(Object.entries(blockedReasons).filter(([id]) => blockedUsers.includes(id))),
          whitelist_users: whitelistUsers,
          whitelist_user_reasons: Object.fromEntries(Object.entries(whitelistReasons).filter(([id]) => whitelistUsers.includes(id))),
        };
        const result = await api('/api/qq-groups/config', {method:'POST', body: JSON.stringify({groups: rows, permission})});
        if (rows.length) CONFIG.group_chat_configs = Object.fromEntries(rows.map(row => [String(row.group_id), row.config]));
        CONFIG.permission = {...(CONFIG.permission || {}), ...permission};
        if (result.data?.permission) CONFIG.permission = {...CONFIG.permission, ...result.data.permission};
        updateGlobalUsableGateUi();
        showToast('群聊配置和用户名单已保存', 'ok');
      } catch (error) { showToast(error.message || '保存群聊配置失败', 'bad'); }
    }

    async function loadConfig() {
      try {
        const res = await api('/api/config');
        CONFIG = res.data || {};
        fillForms();
        renderProxyList();
      } catch (e) {
        showToast(e.message || '配置读取失败', 'bad');
      }
    }
    async function exportJsonConfig() {
      const modal = $('configExportModal');
      if (!modal) return;
      modal.classList.add('show');
      const mode = await new Promise(resolve => {
        const close = value => {
          modal.classList.remove('show');
          resolve(value);
        };
        $('configExportMaskedBtn').onclick = () => close('masked');
        $('configExportRawBtn').onclick = () => close('raw');
        $('configExportCancelBtn').onclick = () => close('');
        $('configExportCloseBtn').onclick = () => close('');
        modal.onclick = event => { if (event.target === modal) close(''); };
      });
      if (!mode) return;
      try {
        const res = await api(`/api/config/export?mode=${encodeURIComponent(mode)}`);
        const parsed = res.data || {};
        const blob = new Blob([JSON.stringify(parsed, null, 2) + '\n'], {type: 'application/json;charset=utf-8'});
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = 'selfie_image_config.json';
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
        showToast(mode === 'raw' ? '原文配置 JSON 已导出，请妥善保管' : '脱敏配置 JSON 已导出', 'ok');
      } catch (e) {
        showToast(`配置 JSON 导出失败：${e.message}`, 'bad');
      }
    }
    async function importJsonConfigFile(file) {
      try {
        const text = await file.text();
        const parsed = JSON.parse(text);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          throw new Error('配置必须是 JSON 对象');
        }
        $('configText').value = JSON.stringify(parsed, null, 2);
        showToast('配置 JSON 已导入编辑区，请点击保存 JSON', 'ok');
      } catch (e) {
        showToast(`配置 JSON 导入失败：${e.message}`, 'bad');
      }
    }
    document.getElementById('configImportFile')?.addEventListener('change', (event) => {
      const file = event.target.files && event.target.files[0];
      if (file) importJsonConfigFile(file);
      event.target.value = '';
    });
    let PERSIST_TAIL = Promise.resolve(true);
    async function persistConfig(renderAfterSave = false, okText = '配置已保存', options = {}) {
      const run = () => persistConfigNow(renderAfterSave, okText, options);
      const next = PERSIST_TAIL.then(run, run);
      PERSIST_TAIL = next.then(() => true, () => true);
      return next;
    }
    async function persistConfigNow(renderAfterSave = false, okText = '配置已保存', options = {}) {
      const toastOnOk = options.toast === true;
      const toastOnError = options.toastError !== false;
      const quiet = options.quiet === true;
      try {
        collectForms();
        const res = await api('/api/config', {method:'POST', body: JSON.stringify({config: CONFIG})});
        CONFIG = res.data || CONFIG;
        ensureConfig();
        $('configText').value = JSON.stringify(CONFIG, null, 2);
        if (renderAfterSave) fillForms();
        else if (!quiet) {
          // Manual save may refresh lists.
          renderAllChannelLists();
          refreshModelSelectors();
        } else {
          // Keep selectors in sync without rebuilding cards.
          refreshModelSelectors();
        }
        if (toastOnOk && okText) setMultiStatus(okText, 'ok');
        return true;
      } catch (e) {
        if (toastOnError) setMultiStatus(e.message, 'bad');
        return false;
      }
    }
    async function saveAll() {
      await persistConfig(true, '配置已保存', { toast: true });
    }
    function scheduleAutoSave(okText = '', options = {}) {
      if (IS_FILLING || SUPPRESS_AUTOSAVE || !document.body.classList.contains('authed')) return;
      // 渠道弹窗未保存前不自动写入
      if (isChannelModalOpen() && options.allowWhileModal !== true) return;
      clearTimeout(AUTO_SAVE_TIMER);
      const reason = options.reason || 'form';
      // 输入框自动保存不刷「正在保存…」，避免每敲一字全页状态抖动
      AUTO_SAVE_TIMER = setTimeout(() => {
        persistConfig(false, okText || '', {
          toast: false,
          toastError: true,
          // Quiet autosave for form and channel-list changes
          quiet: reason === 'form' || reason === 'channel-list',
        });
      }, options.delay != null ? options.delay : (reason === 'form' ? 900 : 650));
    }
    /** 主表单字段变更：仅配置页，不负责渠道弹窗/试画/记录。 */
    function scheduleFormAutoSave() {
      scheduleAutoSave('', { reason: 'form', silentStatus: true, toast: false });
    }
    /** 渠道列表结构化变更（删/复制/启停、优先级）：静默落盘。 */

    function enabledProxyOptions() {
      ensureConfig();
      const rows = Array.isArray(CONFIG.proxies) ? CONFIG.proxies : [];
      return rows.filter(p => p && p.enabled !== false && p.id);
    }
    function fillChannelProxyOptions(selectedId = '') {
      const sel = $('modalProxyId');
      if (!sel) return;
      const cur = selectedId || sel.value || '';
      const opts = ['<option value="">直连（不使用代理）</option>'];
      for (const p of enabledProxyOptions()) {
        const label = `${p.name || p.id} · ${(p.protocol || 'http').toUpperCase()} ${p.host}:${p.port}`;
        opts.push(`<option value="${escapeHtml(p.id)}">${escapeHtml(label)}</option>`);
      }
      // If selected is disabled/missing, still show it once
      if (cur && !opts.some(x => x.includes(`value="${escapeHtml(cur)}"`))) {
        const all = Array.isArray(CONFIG.proxies) ? CONFIG.proxies : [];
        const hit = all.find(p => p && p.id === cur);
        if (hit) {
          opts.push(`<option value="${escapeHtml(cur)}">${escapeHtml((hit.name || cur) + '（已停用）')}</option>`);
        }
      }
      sel.innerHTML = opts.join('');
      sel.value = cur;
    }
    function newProxyDraft() {
      return {id:'', name:'', protocol:'http', host:'', port:7890, username:'', password:'', enabled:true};
    }
    let EDITING_PROXY_INDEX = -1;
    let PROXY_DRAFT = null;
    function openProxyModal(index = -1) {
      ensureConfig();
      CONFIG.proxies = Array.isArray(CONFIG.proxies) ? CONFIG.proxies : [];
      EDITING_PROXY_INDEX = index;
      const isNew = index < 0;
      const row = isNew ? newProxyDraft() : Object.assign(newProxyDraft(), CONFIG.proxies[index] || {});
      PROXY_DRAFT = row;
      $('proxyModalTitle').textContent = isNew ? '添加代理' : '编辑代理';
      $('proxyName').value = row.name || '';
      $('proxyProtocol').value = row.protocol || 'http';
      $('proxyHost').value = row.host || '';
      $('proxyPort').value = row.port || 7890;
      $('proxyUser').value = row.username || '';
      $('proxyPass').value = '';
      $('proxyPass').placeholder = isNew ? '可空' : (row.has_auth || row.password ? '留空表示不修改密码' : '可空');
      $('proxyEnabled').checked = row.enabled !== false;
      $('proxyModalStatus').textContent = '';
      $('proxyModal').classList.add('show');
    }
    function closeProxyModal() {
      $('proxyModal').classList.remove('show');
      EDITING_PROXY_INDEX = -1;
      PROXY_DRAFT = null;
    }
    function collectProxyModal() {
      const isNew = EDITING_PROXY_INDEX < 0;
      const prev = (!isNew && CONFIG.proxies[EDITING_PROXY_INDEX]) ? CONFIG.proxies[EDITING_PROXY_INDEX] : newProxyDraft();
      const protocol = $('proxyProtocol').value || 'http';
      const host = $('proxyHost').value.trim();
      const port = Number($('proxyPort').value || 0);
      const username = $('proxyUser').value.trim();
      let password = $('proxyPass').value;
      if (!isNew && !password && (prev.password || prev.has_auth)) {
        // keep previous password if masked/blank on edit
        password = prev.password && prev.password !== '******' ? prev.password : (PROXY_DRAFT && PROXY_DRAFT._plain_password) || '';
        if (!password && prev.has_auth && prev.password === '******') {
          // need server-side keep: stash flag
          password = '';
        }
      }
      const name = $('proxyName').value.trim();
      const enabled = $('proxyEnabled').checked;
      if (!host) throw new Error('请填写 IP / 主机');
      if (!port || port < 1 || port > 65535) throw new Error('端口无效');
      if (Boolean(username) !== Boolean(password) && isNew) throw new Error('账号和密码需同时填写，或都留空');
      if (Boolean(username) && !password && isNew) throw new Error('填写了账号时需要密码');
      const id = prev.id || ('px_' + Math.random().toString(16).slice(2, 10));
      const row = {
        id,
        name: name || `${protocol}://${host}:${port}`,
        protocol,
        host,
        port,
        username,
        password: password || (prev.password && prev.password !== '******' ? prev.password : ''),
        enabled,
        has_auth: Boolean(username),
      };
      // On edit with masked password and empty input: keep has_auth and omit password change by reusing sentinel
      if (!isNew && prev.has_auth && !$('proxyPass').value) {
        row.password = prev.password || '******';
        row.has_auth = true;
        row._keep_password = true;
      }
      return row;
    }
    async function saveProxyModal() {
      try {
        ensureConfig();
        CONFIG.proxies = Array.isArray(CONFIG.proxies) ? CONFIG.proxies : [];
        const row = collectProxyModal();
        if (row._keep_password) {
          // The server restores the password by the existing proxy ID.
          delete row._keep_password;
        }
        if (EDITING_PROXY_INDEX < 0) CONFIG.proxies.push(row);
        else CONFIG.proxies[EDITING_PROXY_INDEX] = row;
        $('proxyModalStatus').textContent = '保存中…';
        const ok = await persistConfig(false, '代理已保存', { toast: true, quiet: true });
        if (!ok) {
          $('proxyModalStatus').textContent = '保存失败';
          return;
        }
        closeProxyModal();
        renderProxyList();
        fillChannelProxyOptions($('modalProxyId') && $('modalProxyId').value);
      } catch (e) {
        $('proxyModalStatus').textContent = e.message || String(e);
      }
    }
    function proxyStatusCodeClass(code) {
      const n = Number(code);
      if (!Number.isFinite(n) || n <= 0) return 'mute';
      if (n >= 200 && n < 400) return 'ok';
      if (n === 401 || n === 403 || n === 404 || n === 429) return 'warn';
      if (n >= 500) return 'bad';
      return 'mute';
    }
    function formatProxyLocation(d) {
      if (!d || typeof d !== 'object') return '';
      if (d.location) return String(d.location);
      const parts = [d.city, d.region, d.country].map(x => String(x || '').trim()).filter(Boolean);
      let text = parts.join(' · ');
      const code = String(d.country_code || '').trim().toUpperCase();
      if (code && !text.includes(code)) text = text ? `${text} (${code})` : code;
      return text;
    }
    function renderProxyConnectSummary(index, d) {
      const box = $(`proxyResult-${index}`);
      if (!box) return;
      const ok = !!d.success;
      const code = d.http_status != null ? d.http_status : '';
      const ms = d.latency_ms != null ? `${d.latency_ms} ms` : '—';
      const ip = d.ip_address || '—';
      const loc = formatProxyLocation(d) || '—';
      const org = d.org || '';
      box.classList.remove('empty');
      box.innerHTML = `
        <div class="proxy-kpi">
          <div class="cell"><div class="k">状态</div><div class="v">${ok ? '连通' : '失败'}${code !== '' ? ` · <span class="badge-code ${proxyStatusCodeClass(code)}">${escapeHtml(String(code))}</span>` : ''}</div></div>
          <div class="cell"><div class="k">延迟</div><div class="v">${escapeHtml(ms)}</div></div>
          <div class="cell"><div class="k">出口 IP</div><div class="v">${escapeHtml(String(ip))}</div></div>
        </div>
        <div class="proxy-kpi">
          <div class="cell" style="grid-column:1/-1"><div class="k">地理位置</div><div class="v">${escapeHtml(loc)}${org ? `<div class="meta" style="margin-top:4px;font-weight:500">${escapeHtml(org)}</div>` : ''}</div></div>
        </div>
        ${d.message ? `<div class="meta">${escapeHtml(d.message)}</div>` : ''}
      `;
    }
    function renderProxyQualitySummary(index, d) {
      const box = $(`proxyResult-${index}`);
      if (!box) return;
      const rows = Array.isArray(d.results) ? d.results : [];
      const ip = d.ip_address || '';
      const loc = formatProxyLocation(d);
      const items = rows.map(r => {
        const code = r.http_status != null ? r.http_status : null;
        const codeHtml = code != null
          ? `<span class="badge-code ${proxyStatusCodeClass(code)}">${escapeHtml(String(code))}</span>`
          : `<span class="badge-code mute">无响应</span>`;
        const ms = r.latency_ms != null ? `${r.latency_ms}ms` : '';
        const ok = r.reachable || r.success;
        return `<div class="proxy-qitem">
          <div class="name">${escapeHtml(r.label || r.id || '')}</div>
          <div class="line">${codeHtml}<span>${ok ? '可达' : '不可达'}</span>${ms ? `<span>${escapeHtml(ms)}</span>` : ''}</div>
        </div>`;
      }).join('');
      box.classList.remove('empty');
      box.innerHTML = `
        <div class="proxy-kpi">
          <div class="cell"><div class="k">评分</div><div class="v">${escapeHtml(String(d.quality_grade || '-'))} · ${escapeHtml(String(d.quality_score ?? '-'))}</div></div>
          <div class="cell"><div class="k">可达</div><div class="v">${escapeHtml(String(d.ok_count ?? 0))}/${escapeHtml(String(d.total ?? rows.length))}</div></div>
          <div class="cell"><div class="k">出口 IP</div><div class="v">${escapeHtml(ip || '—')}</div></div>
        </div>
        ${loc ? `<div class="meta">位置 ${escapeHtml(loc)}${d.org ? ' · ' + escapeHtml(d.org) : ''}</div>` : ''}
        <div class="proxy-qgrid">${items || '<div class="meta">无结果</div>'}</div>
      `;
    }
    function renderProxyList() {
      ensureConfig();
      const box = $('proxyList');
      if (!box) return;
      const rows = Array.isArray(CONFIG.proxies) ? CONFIG.proxies : [];
      if (!rows.length) {
        box.innerHTML = '<div class="card soft muted">还没有代理。点右上角「添加代理」。</div>';
        return;
      }
      // Preserve previous probe results across re-render.
      const prev = {};
      box.querySelectorAll('[data-proxy-index]').forEach(card => {
        const i = card.getAttribute('data-proxy-index');
        const result = card.querySelector('.proxy-result');
        if (result && !result.classList.contains('empty')) prev[i] = result.innerHTML;
      });
      box.innerHTML = rows.map((p, i) => {
        const proto = String(p.protocol || 'http').toUpperCase();
        const auth = p.has_auth || p.username;
        const en = p.enabled !== false;
        const addr = `${p.host || ''}:${p.port || ''}`;
        return `<div class="proxy-card" data-proxy-index="${i}">
          <div class="proxy-card-head">
            <div class="proxy-title">
              <b>${escapeHtml(p.name || p.id || '未命名')}</b>
              <div class="proxy-chips">
                <span class="proxy-chip proto">${escapeHtml(proto)}</span>
                <span class="proxy-chip ${en ? 'on' : 'off'}">${en ? '已启用' : '已停用'}</span>
                ${auth ? '<span class="proxy-chip auth">认证</span>' : '<span class="proxy-chip">无认证</span>'}
              </div>
            </div>
          </div>
          <div class="proxy-addr">${escapeHtml(addr)}</div>
          <div class="proxy-actions">
            <label class="checkline"><input type="checkbox" data-act="en" ${en ? 'checked' : ''}>启用</label>
            <button class="secondary mini" type="button" data-act="edit">编辑</button>
            <button class="secondary mini" type="button" data-act="test">连通</button>
            <button class="secondary mini" type="button" data-act="quality">质量</button>
            <button class="danger mini" type="button" data-act="del" style="grid-column:1/-1">${PENDING_DELETE.key === `proxy:${i}` ? '确认删除' : '删除'}</button>
          </div>
          <div class="proxy-result ${prev[i] ? '' : 'empty'}" id="proxyResult-${i}">${prev[i] || ''}</div>
        </div>`;
      }).join('');
      box.querySelectorAll('.proxy-card').forEach(card => {
        const idx = Number(card.getAttribute('data-proxy-index'));
        card.querySelector('[data-act="en"]').onchange = async (ev) => {
          CONFIG.proxies[idx].enabled = !!ev.target.checked;
          const chip = card.querySelector('.proxy-chip.on, .proxy-chip.off');
          if (chip) {
            chip.className = `proxy-chip ${ev.target.checked ? 'on' : 'off'}`;
            chip.textContent = ev.target.checked ? '已启用' : '已停用';
          }
          fillChannelProxyOptions($('modalProxyId') && $('modalProxyId').value);
          await persistConfig(false, '', { quiet: true, toast: false, toastError: true });
        };
        card.querySelector('[data-act="edit"]').onclick = () => openProxyModal(idx);
        card.querySelector('[data-act="del"]').onclick = async () => {
          const key = `proxy:${idx}`;
          if (PENDING_DELETE.key !== key) {
            clearTimeout(PENDING_DELETE.timer);
            PENDING_DELETE = {
              key,
              timer: setTimeout(() => {
                PENDING_DELETE = { key: '', timer: null };
                renderProxyList();
              }, 4000),
            };
            renderProxyList();
            showToast('再点一次「确认删除」才会删除代理', 'info');
            return;
          }
          clearTimeout(PENDING_DELETE.timer);
          PENDING_DELETE = { key: '', timer: null };
          const removed = CONFIG.proxies.splice(idx, 1)[0];
          const rid = removed && removed.id;
          if (rid) {
            for (const list of [CONFIG.image_channels, CONFIG.audit_channels]) {
              for (const ch of list || []) {
                if (ch && ch.proxy_id === rid) ch.proxy_id = '';
              }
            }
          }
          renderProxyList();
          fillChannelProxyOptions();
          await persistConfig(false, '代理已删除', { quiet: true, toast: true });
        };
        card.querySelector('[data-act="test"]').onclick = () => testProxyRow(idx, 'connect');
        card.querySelector('[data-act="quality"]').onclick = () => testProxyRow(idx, 'quality');
      });
    }
    async function testProxyRow(index, kind = 'connect') {
      const row = (CONFIG.proxies || [])[index];
      if (!row) return;
      const box = $(`proxyResult-${index}`);
      const body = row.id ? { id: row.id } : {
        protocol: row.protocol, host: row.host, port: row.port,
        username: row.username, password: row.password === '******' ? '' : row.password,
        name: row.name, enabled: row.enabled !== false,
      };
      if (kind === 'connect') {
        if (box) {
          box.classList.remove('empty');
          box.innerHTML = '<div class="meta">正在检测连通与出口信息…</div>';
        }
        try {
          const res = await api('/api/proxies/test', { method: 'POST', body: JSON.stringify(body) });
          const d = res.data || {};
          renderProxyConnectSummary(index, d);
          showToast(d.success ? '连通正常' : (d.message || '连通失败'), d.success ? 'ok' : 'bad');
        } catch (e) {
          if (box) {
            box.classList.remove('empty');
            box.innerHTML = `<div class="meta">${escapeHtml(e.message || '测试失败')}</div>`;
          }
          showToast(e.message || '测试失败', 'bad');
        }
        return;
      }
      if (box) {
        box.classList.remove('empty');
        box.innerHTML = '<div class="meta">正在质量测试（各站点真实状态码）…</div>';
      }
      try {
        const res = await api('/api/proxies/quality-check', { method: 'POST', body: JSON.stringify(body) });
        const d = res.data || {};
        renderProxyQualitySummary(index, d);
        showToast(d.message || '质量测试完成', d.success ? 'ok' : 'bad');
      } catch (e) {
        if (box) {
          box.classList.remove('empty');
          box.innerHTML = `<div class="meta">${escapeHtml(e.message || '质量测试失败')}</div>`;
        }
        showToast(e.message || '质量测试失败', 'bad');
      }
    }

    function channelListSaveSnapshot() {
      ensureConfig();
      normalizeChannels();
      normalizeAuditChannels();
      for (const ch of [...(CONFIG.image_channels || []), ...(CONFIG.audit_channels || [])]) {
        softDisableChannelIfNoModels(ch);
      }
      const copy = value => JSON.parse(JSON.stringify(value || []));
      return {
        image_channels: copy(CONFIG.image_channels),
        audit_channels: copy(CONFIG.audit_channels),
        enabled_image_model_priority: $('priorityList') ? textList('priorityList') : copy(CONFIG.enabled_image_model_priority),
        enabled_audit_model_priority: $('auditPriorityList') ? textList('auditPriorityList') : copy(CONFIG.enabled_audit_model_priority),
        image_model_call_mode: ($('imageModelCallMode') && $('imageModelCallMode').value) || CONFIG.image_model_call_mode || 'sequence',
      };
    }
    async function persistChannelListSnapshot(snapshot) {
      try {
        const res = await api('/api/config', {method:'POST', body: JSON.stringify({config: snapshot})});
        CONFIG = res.data || CONFIG;
        ensureConfig();
        // Channel-list mutations already update their local card. Rebuilding
        // every card after a priority-only autosave makes mobile WebViews hitch.
        refreshModelSelectors();
        return true;
      } catch (e) {
        // A failed autosave must leave the visible priority order aligned with
        // the last known server config, so a retry cannot submit a partial or
        // reordered snapshot by accident.
        ensureConfig();
        if ($('priorityList')) $('priorityList').value = (CONFIG.enabled_image_model_priority || []).join('\n');
        if ($('auditPriorityList')) $('auditPriorityList').value = (CONFIG.enabled_audit_model_priority || []).join('\n');
        renderAllChannelLists();
        refreshModelSelectors();
        renderAllPriorityRows();
        setMultiStatus(e.message, 'bad');
        return false;
      }
    }
    async function scheduleChannelListAutoSave() {
      if (isChannelModalOpen()) return false;
      const snapshot = channelListSaveSnapshot();
      const run = () => persistChannelListSnapshot(snapshot);
      const next = PERSIST_TAIL.then(run, run);
      PERSIST_TAIL = next.then(() => true, () => true);
      return await next;
    }
    async function saveJsonConfig() {
      try {
        CONFIG = JSON.parse($('configText').value || '{}');
        ensureConfig();
        const res = await api('/api/config', {method:'POST', body: JSON.stringify({config: CONFIG})});
        CONFIG = res.data || CONFIG;
        fillForms();
        showToast('JSON 配置已保存', 'ok');
      } catch (e) { showToast(e.message || 'JSON 配置保存失败', 'bad'); }
    }

    async function checkHealth() {
      try {
        const res = await api('/api/health');
        const d = res.data || {};
        const cachePreview = d.cache_cleanup_preview || {};
        const statusText = d.status === 'ok' ? '运行正常' : d.status === 'degraded' ? '配置需要检查' : String(d.status || '状态未知');
        const healthItems = [
          ['插件状态', statusText],
          ['缓存占用', `${String(d.cache_file_count ?? 0)} / ${String(d.cache_limit_count ?? 1000)} 张 · ${String(d.cache_size_mb ?? 0)} / ${String(d.cache_limit_mb ?? 2048)} MB`],
          ['可回收预览', `${String(cachePreview.would_delete_count ?? 0)} 张 · ${String(Math.round(Number(cachePreview.would_delete_bytes || 0) / 1024 / 1024))} MB`],
          ['缓存目录', d.cache_dir || '-'],
          ['记录文件', d.records_path || '-'],
          ['配置文件', d.config_path || '-'],
        ];
        const healthWarnings = [
          d.cache_limit_warning,
          ...(Array.isArray(d.config_preflight?.errors) ? d.config_preflight.errors.map(item => item?.message) : []),
        ].map(value => String(value || '').trim()).filter(Boolean);
        $('health').innerHTML = `
          ${healthItems.map(([label, value]) => `<div class="health-item"><span class="health-item-label">${escapeHtml(label)}</span><span class="health-item-value">${escapeHtml(String(value))}</span></div>`).join('')}
          ${healthWarnings.map(message => `<div class="health-alert">${escapeHtml(message)}</div>`).join('')}
        `;
        $('healthPill').textContent = d.status === 'ok' ? '已连接' : '已连接 · 需检查';
        $('healthPill').className = `pill ${d.status === 'ok' ? 'green' : 'warning'}`;
      } catch (e) {
        $('health').innerHTML = `<div class="health-message">${escapeHtml(e.message || '连接失败')}</div>`;
        $('healthPill').textContent = '未连接';
        $('healthPill').className = 'pill gray';
      }
    }
    async function cleanupCache() {
      const button = $('cleanupCacheBtn');
      if (button) button.disabled = true;
      try {
        const previewResponse = await api('/api/cache/cleanup', {method:'POST', body:'{}'});
        const previewData = previewResponse.data || {};
        const preview = previewData.preview || {};
        const count = Number(preview.would_delete_count || 0);
        const bytes = Number(preview.would_delete_bytes || 0);
        if (!count) {
          showToast('当前没有超限且可回收的缓存', 'info');
          return;
        }
        const mb = (bytes / 1024 / 1024).toFixed(1);
        const accepted = await confirmAction(
          `将清理 ${count} 个超出缓存上限的旧缓存文件（约 ${mb} MB）。\n仍被生成记录、收藏、置顶或存活画布会话引用的媒体不会被清理。`,
          {title:'清理可回收缓存', confirmText:'确认清理'}
        );
        if (!accepted) return;
        const resultResponse = await api('/api/cache/cleanup', {
          method:'POST',
          body: JSON.stringify({confirm:true, plan_token: preview.plan_token || ''})
        });
        const result = resultResponse.data?.result || {};
        const deleted = Array.isArray(result.deleted) ? result.deleted.length : Number(result.deleted_count || 0);
        showToast(`已清理 ${deleted} 个缓存文件`, 'ok');
        await checkHealth();
      } catch (e) {
        showToast(e.message || '缓存清理失败', 'bad');
        await checkHealth();
      } finally {
        if (button) button.disabled = false;
      }
    }
