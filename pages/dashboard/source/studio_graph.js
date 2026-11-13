    function studioGraph() { return STUDIO.canvas && typeof STUDIO.canvas === 'object' ? STUDIO.canvas : {nodes: [], edges: [], viewport: {x: 120, y: 80, zoom: 1}}; }
    function studioNode(id) { return (studioGraph().nodes || []).find(item => String(item.id || '') === String(id || '')) || null; }
    function studioNodeHasMissingParent(node) {
      return !!(node && node.parent_id && !studioNode(node.parent_id));
    }
    function studioNodeDescendants(nodeId) {
      const children = new Map();
      for (const node of studioGraph().nodes || []) {
        const parentId = String(node.parent_id || '').trim();
        const childId = String(node.id || '').trim();
        if (parentId && childId) {
          const list = children.get(parentId) || [];
          list.push(childId);
          children.set(parentId, list);
        }
      }
      const descendants = new Set();
      const pending = [...(children.get(String(nodeId || '').trim()) || [])];
      while (pending.length) {
        const current = pending.pop();
        if (!current || descendants.has(current)) continue;
        descendants.add(current);
        pending.push(...(children.get(current) || []));
      }
      return descendants;
    }
    function studioConnectCandidates(nodeId) {
      const target = studioNode(nodeId);
      if (!target) return [];
      const targetId = String(target.id || '');
      const descendants = studioNodeDescendants(targetId);
      const targetPosition = target.position || {x: 0, y: 0};
      const nodes = (studioGraph().nodes || []).filter(node => {
        const id = String(node.id || '');
        return id && id !== targetId && !descendants.has(id);
      });
      const earlier = nodes.filter(node => Number(node.position?.x || 0) < Number(targetPosition.x || 0));
      const pool = earlier.length ? earlier : nodes;
      return pool.sort((a, b) => {
        const ax = Number(a.position?.x || 0), bx = Number(b.position?.x || 0);
        const ay = Number(a.position?.y || 0), by = Number(b.position?.y || 0);
        return bx - ax || ay - by || String(a.title || a.id).localeCompare(String(b.title || b.id), 'zh');
      });
    }
    function closeStudioConnectModal() {
      STUDIO.connectNodeId = '';
      $('studioConnectModal')?.classList.remove('show');
    }
    function openStudioConnectModal(nodeId) {
      const node = studioNode(nodeId);
      if (!node || !STUDIO.current) return;
      const list = $('studioConnectList');
      const hint = $('studioConnectHint');
      const status = $('studioConnectStatus');
      STUDIO.connectNodeId = String(node.id || '');
      if (hint) hint.textContent = `为「${node.title || '当前画面'}」选择参考画面，生成时将使用所选画面的图片。`;
      if (status) status.textContent = '';
      const candidates = studioConnectCandidates(node.id);
      if (list) {
        list.innerHTML = '';
        if (!candidates.length) {
          list.innerHTML = '<div class="muted">没有可用的参考画面。</div>';
        } else {
          for (const candidate of candidates) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'secondary';
            button.dataset.connectParent = String(candidate.id || '');
            const position = candidate.position || {x: 0, y: 0};
            button.innerHTML = `<span><strong>${escapeHtml(candidate.title || candidate.template_id || '画面')}</strong><span class="muted">${escapeHtml(studioNodeLabel(candidate))}</span></span><span class="muted">${Math.round(Number(position.x) || 0)}, ${Math.round(Number(position.y) || 0)}</span>`;
            button.onclick = () => studioConnectNode(candidate.id);
            list.appendChild(button);
          }
        }
      }
      $('studioConnectModal')?.classList.add('show');
    }
    async function studioConnectNode(parentId) {
      const nodeId = String(STUDIO.connectNodeId || '');
      if (!nodeId || !STUDIO.current || !parentId) return;
      const status = $('studioConnectStatus');
      const list = $('studioConnectList');
      if (status) status.textContent = '正在连接…';
      list?.querySelectorAll('button').forEach(button => { button.disabled = true; });
      try {
        const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/canvas/nodes/' + encodeURIComponent(nodeId) + '/connect', {
          method: 'POST', body: JSON.stringify({parent_id: String(parentId)})
        });
        if (!res.success) throw new Error(res.message || '连接失败');
        if (res.data?.canvas) STUDIO.canvas = res.data.canvas;
        else if (res.data) STUDIO.canvas = res.data;
        STUDIO.selectedNodeId = nodeId;
        closeStudioConnectModal();
        studioRenderCanvas();
        showToast('参考画面已更新', 'ok');
      } catch (error) {
        if (status) status.textContent = error.message || '连接失败';
        list?.querySelectorAll('button').forEach(button => { button.disabled = false; });
      }
    }
    function studioNodeMedia(node) { return node && node.result && (node.result.thumbnail_path || node.result.media_path) || ''; }
    function studioSetStatus(message, kind = '') {
      const el = $('studioStatus');
      if (el) { el.textContent = message || ''; el.className = 'studio-status-line' + (kind ? ' ' + kind : ''); }
    }
    function studioClampZoom(value) { return Math.max(.35, Math.min(1.8, Number(value) || 1)); }
    function studioFitViewport(force = false) {
      const area = $('studioCanvasViewport');
      const graph = studioGraph();
      const nodes = (graph.nodes || []).filter(node => node && node.position);
      if (!area || !nodes.length || (!force && STUDIO.viewportFittedForSession)) return;
      const width = Math.max(240, area.clientWidth || 640);
      const height = Math.max(240, area.clientHeight || 520);
      const xs = nodes.map(node => Number(node.position.x) || 0);
      const ys = nodes.map(node => Number(node.position.y) || 0);
      const minX = Math.min(...xs) - 120, maxX = Math.max(...xs) + 120;
      const minY = Math.min(...ys) - 95, maxY = Math.max(...ys) + 95;
      const spanX = Math.max(240, maxX - minX), spanY = Math.max(190, maxY - minY);
      const mobile = window.matchMedia?.('(max-width: 680px)').matches;
      const readableMin = mobile ? 0.72 : 0.5;
      const savedZoom = Number(STUDIO.viewport?.zoom || 1);
      const fitZoom = Math.min(width * 0.86 / spanX, height * 0.82 / spanY, 1.15);
      const zoom = Math.max(readableMin, Math.min(1.8, Math.max(fitZoom, savedZoom < readableMin ? readableMin : savedZoom)));
      const centerX = (minX + maxX) / 2, centerY = (minY + maxY) / 2;
      STUDIO.viewport = {x: width / 2 - centerX * zoom, y: height / 2 - centerY * zoom, zoom};
      STUDIO.viewportFittedForSession = true;
    }
    function studioApplyViewport() {
      const viewport = STUDIO.viewport || {x:120, y:80, zoom:1};
      const grid = $('studioCanvasGrid');
      if (grid) grid.style.transform = `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})`;
      const label = $('studioZoomLabel');
      if (label) label.textContent = Math.round(viewport.zoom * 100) + '%';
    }
    function studioCanvasPoint(event) {
      const area = $('studioCanvasViewport');
      const rect = area.getBoundingClientRect();
      return {x: event.clientX - rect.left, y: event.clientY - rect.top};
    }
    function studioPersistCanvas(delay = 260) {
      clearTimeout(STUDIO.canvasSaveTimer);
      STUDIO.canvasSaveTimer = setTimeout(async () => {
        if (!STUDIO.current || !STUDIO.canvas) return;
        const positions = {};
        for (const node of STUDIO.canvas.nodes || []) positions[node.id] = node.position || {x:0, y:0};
        try {
          const nodes = (STUDIO.canvas.nodes || []).map(node => ({
            id: node.id,
            title: node.title || '画面',
            template_id: node.template_id || '',
            params: Object.assign({}, node.params || {}),
          }));
          const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/canvas', {
            method:'POST', body: JSON.stringify({viewport: STUDIO.viewport, positions, nodes})
          });
          if (res.success && res.data) STUDIO.canvas = res.data;
        } catch (error) { console.warn('[studio-canvas] save failed', error); }
      }, delay);
    }
    function studioRenderEdges() {
      const svg = $('studioCanvasEdges');
      if (!svg) return;
      const graph = studioGraph();
      const byId = new Map((graph.nodes || []).map(node => [String(node.id), node]));
      svg.innerHTML = (graph.edges || []).map(edge => {
        const source = byId.get(String(edge.source));
        const target = byId.get(String(edge.target));
        if (!source || !target) return '';
        const a = source.position || {x:0,y:0};
        const b = target.position || {x:0,y:0};
        const dx = Math.max(80, Math.abs(b.x - a.x) * .46);
        const active = String(STUDIO.selectedNodeId || '') === String(source.id) || String(STUDIO.selectedNodeId || '') === String(target.id);
        return `<path class="studio-canvas-edge${active ? ' active' : ''}" d="M ${a.x + 102} ${a.y} C ${a.x + 102 + dx} ${a.y}, ${b.x - 102 - dx} ${b.y}, ${b.x - 102} ${b.y}"></path>`;
      }).join('');
    }
    function studioNodeLabel(node) {
      if (node.status === 'draft') return '待生成';
      if (node.status === 'running' || node.status === 'queued') return '生成中';
      if (node.status === 'failed') return '失败';
      if (node.status === 'cancelled') return '已取消';
      return String(node.template_id || '生成结果');
    }
    function studioNodeTitle(node) {
      const explicit = String(node?.title || '').trim();
      if (explicit && !['生成结果', '新画面', '画面'].includes(explicit)) return explicit;
      const template = (STUDIO.templates || []).find(item => String(item.id || '') === String(node?.template_id || ''));
      const index = Math.max(1, (studioGraph().nodes || []).findIndex(item => String(item.id) === String(node?.id)) + 1);
      return `${template?.title || node?.template_id || '画面'} ${index}`;
    }
    function studioRenderNodes() {
      const wrap = $('studioCanvasNodes');
      if (!wrap) return;
      const query = String(STUDIO.nodeSearch || '').trim().toLowerCase();
      wrap.innerHTML = '';
      for (const node of studioGraph().nodes || []) {
      const title = studioNodeTitle(node);
        const prompt = String(node.params && node.params.prompt || '');
        if (query && !(title + ' ' + prompt + ' ' + studioNodeLabel(node)).toLowerCase().includes(query)) continue;
        const pos = node.position || {x:0,y:0};
        const card = document.createElement('article');
        card.className = 'studio-canvas-node' + (studioNodeHasMissingParent(node) ? ' dangling' : '') + (String(node.id) === String(STUDIO.selectedNodeId) ? ' selected' : '');
        card.dataset.nodeId = node.id;
        card.style.left = `${Number(pos.x) || 0}px`;
        card.style.top = `${Number(pos.y) || 0}px`;
        const media = studioNodeMedia(node);
        card.innerHTML = `<div class="node-media">${media ? `<img data-cache-path="${escapeHtml(media)}" data-cache-variant="thumbnail" alt="">` : `<span class="node-placeholder">${escapeHtml(node.status === 'draft' ? '等待生成' : '暂无图片')}</span>`}</div>
          <div class="node-copy"><div class="node-title">${escapeHtml(title)}</div><div class="node-meta"><span>${escapeHtml(studioNodeLabel(node))}</span><span>${escapeHtml(String(node.created_at || '').slice(0, 10))}</span></div></div>
          <div class="node-actions">${studioNodeHasMissingParent(node) ? '<button class="studio-node-action connect" data-node-action="connect" title="选择参考画面" aria-label="选择参考画面">+</button>' : ''}<button class="studio-node-action" data-node-action="focus" title="定位" aria-label="定位">⌖</button><button class="studio-node-action" data-node-action="delete" title="删除画面" aria-label="删除画面">×</button></div>`;
        card.addEventListener('click', event => { if (event.target.closest('.studio-node-action')) return; studioSelectNode(node.id); });
        card.addEventListener('dblclick', event => { event.stopPropagation(); studioFocusNode(node.id); });
        card.addEventListener('pointerdown', event => studioStartNodeDrag(event, node.id));
        card.querySelector('[data-node-action="connect"]')?.addEventListener('click', event => { event.stopPropagation(); openStudioConnectModal(node.id); });
        card.querySelector('[data-node-action="focus"]').onclick = event => { event.stopPropagation(); studioFocusNode(node.id); };
        card.querySelector('[data-node-action="delete"]').onclick = async event => { event.stopPropagation(); await studioDeleteNode(node.id); };
        wrap.appendChild(card);
      }
      wrap.dataset.protectedMediaQueued = 'true';
      loadProtectedImages(wrap);
    }
    function studioRenderSidebar() {
      const graph = studioGraph();
      const title = $('studioGraphTitle');
      const meta = $('studioGraphMeta');
      if (title) title.textContent = STUDIO.current?.title || '未命名画布';
      if (meta) meta.textContent = `${(graph.nodes || []).length} 个画面 · ${(graph.edges || []).length} 条关联`;
      const list = $('studioNodeList');
      if (!list) return;
      const query = String(STUDIO.nodeSearch || '').trim().toLowerCase();
      const rows = (graph.nodes || []).filter(node => {
        if (!query) return true;
        const searchable = `${studioNodeTitle(node)} ${node.title || ''} ${node.template_id || ''} ${(node.params || {}).prompt || ''} ${studioNodeLabel(node)}`;
        return searchable.toLowerCase().includes(query);
      });
      list.innerHTML = rows.map(node => {
        const media = studioNodeMedia(node);
        return `<button type="button" class="studio-node-list-item${String(node.id) === String(STUDIO.selectedNodeId) ? ' selected' : ''}" data-sidebar-node="${escapeHtml(node.id)}">
          ${media ? `<img data-cache-path="${escapeHtml(media)}" data-cache-variant="thumbnail" alt="">` : '<span style="width:42px;height:42px;border-radius:7px;background:#e8edf0"></span>'}
          <span class="node-list-copy"><strong>${escapeHtml(studioNodeTitle(node))}</strong><span>${escapeHtml(studioNodeLabel(node))}</span></span></button>`;
      }).join('') || '<div class="studio-node-empty">没有匹配画面</div>';
      list.querySelectorAll('[data-sidebar-node]').forEach(button => button.onclick = () => studioSelectNode(button.dataset.sidebarNode));
      list.dataset.protectedMediaQueued = 'true';
      loadProtectedImages(list);
    }
    function studioRenderPanel() {
      const panel = $('studioNodePanel');
      const body = $('studioNodePanelBody');
      const node = studioNode(STUDIO.selectedNodeId);
      if (!panel || !body) return;
      panel.hidden = !node;
      const panelTitle = $('studioNodePanelTitle');
      if (panelTitle) panelTitle.textContent = node ? studioNodeTitle(node) : '选择一个画面';
      if (!node) return;
      const params = node.params && typeof node.params === 'object' ? node.params : {};
      const media = studioNodeMedia(node);
      const templates = STUDIO.templates || [];
      body.innerHTML = `${media ? `<img class="studio-node-result" data-cache-path="${escapeHtml(media)}" alt="画面结果">` : ''}
        <div class="studio-node-field"><label>模板</label><select id="studioNodeTemplate">${templates.map(item => `<option value="${escapeHtml(item.id || '')}"${String(item.id || '') === String(node.template_id || '') ? ' selected' : ''}>${escapeHtml(item.title || item.id || '')}</option>`).join('')}</select></div>
        <div class="studio-node-field"><label>画面描述</label><textarea id="studioNodePrompt">${escapeHtml(params.prompt || '')}</textarea></div>
        <div class="studio-node-picker-actions"><button class="secondary" type="button" id="studioNodePresetBtn" data-panel-action="presets">提示词预设</button><button class="secondary" type="button" id="studioNodeCosBtn" data-panel-action="cos">COS 池</button></div>
        <div id="studioNodePresetPanel" class="preset-panel studio-node-picker"></div><div id="studioNodeCosPanel" class="preset-panel studio-node-picker"></div>
        <div class="grid2"><div class="studio-node-field"><label>比例</label><select id="studioNodeAspect">${['自动','1:1','2:3','3:2','3:4','4:3','4:5','5:4','9:16','16:9','21:9'].map(item => `<option${item === (params.aspect_ratio || '自动') ? ' selected' : ''}>${item}</option>`).join('')}</select></div><div class="studio-node-field"><label>清晰度</label><select id="studioNodeResolution">${['1K','2K','4K'].map(item => `<option${item === (params.resolution || '1K') ? ' selected' : ''}>${item}</option>`).join('')}</select></div></div>
        <div class="studio-node-field"><label>状态</label><div class="status">${escapeHtml(studioNodeLabel(node))}${node.parent_id ? ' · 沿用上一张图' : ''}</div>${(() => {
          const task = STUDIO.taskProgress || {};
          const active = node.status === 'running' || (task.task_id && String(task.task_id) === String(STUDIO.taskId || '') && String(node.id) === String(STUDIO.taskNodeId || ''));
          if (!active) return '';
          const requested = Number(task.requested_count || 0);
          const completed = Number(task.completed_count || 0);
          const percent = Math.max(0, Math.min(100, Number(task.progress_percent || 0)));
          const progress = requested ? `${completed}/${requested}（${percent}%）` : `${percent}%`;
          const stage = taskStageText(task) || '排队中';
          return `<div class="studio-node-progress"><div class="between"><span>${escapeHtml(stage)}</span><strong>${escapeHtml(progress)}</strong></div><progress max="100" value="${percent}"></progress></div>`;
        })()}</div>
        <div class="studio-node-actions"><button class="secondary" type="button" data-panel-action="new">继续创作</button><button class="ok" type="button" data-panel-action="run" ${node.status === 'running' ? 'disabled' : ''}>${node.status === 'draft' ? '生成图片' : '重新生成'}</button><button class="danger" type="button" data-panel-action="delete" ${!node.parent_id ? 'disabled' : ''}>删除画面</button></div>`;
      body.querySelector('[data-panel-action="new"]').onclick = () => studioCreateChildNode(node);
      body.querySelector('[data-panel-action="run"]').onclick = () => studioRunNode(node);
      body.querySelector('[data-panel-action="delete"]').onclick = () => studioDeleteNode(node.id);
      body.querySelector('[data-panel-action="presets"]').onclick = () => studioToggleNodePresets();
      body.querySelector('[data-panel-action="cos"]').onclick = () => studioToggleNodeCos();
      body.querySelector('#studioNodeTemplate').onchange = () => studioPanelChanged(node);
      body.querySelector('#studioNodePrompt').oninput = () => studioPanelChanged(node);
      body.querySelector('#studioNodeAspect').onchange = () => studioPanelChanged(node);
      body.querySelector('#studioNodeResolution').onchange = () => studioPanelChanged(node);
      renderPresetPanel('studioNodePresetPanel', 'studioNodePrompt', !!STUDIO.nodePresetOpen, 'studioNodePresetBtn');
      renderCosPanel('studioNodeCosPanel', 'studioNodePrompt', !!STUDIO.nodeCosOpen, 'studioNodeCosBtn');
      loadProtectedImages(body);
    }
    async function studioToggleNodePresets() {
      STUDIO.nodePresetOpen = !STUDIO.nodePresetOpen;
      STUDIO.nodeCosOpen = false;
      if (STUDIO.nodePresetOpen) {
        const state = catalogPickerState('studioNodePresetPanel');
        await loadCatalogPickerPage('studioNodePresetPanel', 'image', state.page);
      }
      renderPresetPanel('studioNodePresetPanel', 'studioNodePrompt', STUDIO.nodePresetOpen, 'studioNodePresetBtn');
      renderCosPanel('studioNodeCosPanel', 'studioNodePrompt', false, 'studioNodeCosBtn');
    }
    async function studioToggleNodeCos() {
      STUDIO.nodeCosOpen = !STUDIO.nodeCosOpen;
      STUDIO.nodePresetOpen = false;
      if (STUDIO.nodeCosOpen) {
        const state = catalogPickerState('studioNodeCosPanel');
        await loadCatalogPickerPage('studioNodeCosPanel', 'cos', state.page);
      }
      renderPresetPanel('studioNodePresetPanel', 'studioNodePrompt', false, 'studioNodePresetBtn');
      renderCosPanel('studioNodeCosPanel', 'studioNodePrompt', STUDIO.nodeCosOpen, 'studioNodeCosBtn');
    }
    function studioPanelChanged(node) {
      if (!node) return;
      node.template_id = $('studioNodeTemplate')?.value || node.template_id || '';
      node.params = Object.assign({}, node.params || {}, {
        prompt: $('studioNodePrompt')?.value || '',
        aspect_ratio: $('studioNodeAspect')?.value || '自动',
        resolution: $('studioNodeResolution')?.value || '1K',
      });
      studioPersistCanvas();
    }
    function studioRenderCanvas() {
      studioFitViewport();
      studioApplyViewport();
      studioRenderEdges();
      studioRenderNodes();
      studioRenderSidebar();
      studioRenderPanel();
    }
    function studioSelectNode(nodeId, focus = false) {
      if (!studioNode(nodeId)) return;
      STUDIO.selectedNodeId = String(nodeId);
      if (STUDIO.current?.id) safeStorageSet(`selfieStudioSelected:${STUDIO.current.id}`, STUDIO.selectedNodeId);
      $('studioEditor')?.classList.remove('mobile-node-list-open');
      studioRenderCanvas();
      if (focus) studioFocusNode(nodeId);
    }
    function studioFocusNode(nodeId) {
      const node = studioNode(nodeId);
      const area = $('studioCanvasViewport');
      if (!node || !area) return;
      const pos = node.position || {x:0,y:0};
      STUDIO.viewport = {x: area.clientWidth / 2 - Number(pos.x || 0) * STUDIO.viewport.zoom, y: area.clientHeight / 2 - Number(pos.y || 0) * STUDIO.viewport.zoom, zoom: STUDIO.viewport.zoom};
      studioRenderCanvas();
      studioPersistCanvas();
    }
    function studioZoomAt(factor, event) {
      const old = STUDIO.viewport.zoom;
      const next = studioClampZoom(old * factor);
      if (next === old) return;
      const area = $('studioCanvasViewport');
      const point = event ? studioCanvasPoint(event) : {x: area.clientWidth / 2, y: area.clientHeight / 2};
      const world = {x: (point.x - STUDIO.viewport.x) / old, y: (point.y - STUDIO.viewport.y) / old};
      STUDIO.viewport = {x: point.x - world.x * next, y: point.y - world.y * next, zoom: next};
      STUDIO.viewportFittedForSession = true;
      studioRenderCanvas();
      studioPersistCanvas();
    }
    function studioStartNodeDrag(event, nodeId) {
      if (event.button !== 0 || event.target.closest('.studio-node-action')) return;
      event.stopPropagation();
      studioSelectNode(nodeId);
      const node = studioNode(nodeId);
      const point = studioCanvasPoint(event);
      STUDIO.drag = {type:'node', nodeId, x:point.x, y:point.y, origin:Object.assign({}, node.position || {x:0,y:0})};
      $('studioCanvasViewport')?.setPointerCapture?.(event.pointerId);
    }
    function studioBindCanvasInteractions() {
      const area = $('studioCanvasViewport');
      if (!area || area.dataset.bound === 'true') return;
      area.dataset.bound = 'true';
      const pointers = new Map();
      let pinch = null;
      const pointFor = event => {
        const point = studioCanvasPoint(event);
        return {x: point.x, y: point.y};
      };
      const pinchPoints = () => Array.from(pointers.values()).slice(0, 2);
      const startPinch = () => {
        const points = pinchPoints();
        if (points.length < 2) return;
        const a = points[0];
        const b = points[1];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const center = {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2};
        const viewport = Object.assign({}, STUDIO.viewport);
        pinch = {
          distance,
          center,
          viewport,
          world: {x: (center.x - viewport.x) / viewport.zoom, y: (center.y - viewport.y) / viewport.zoom},
        };
        STUDIO.drag = null;
        area.classList.remove('is-panning');
      };
      const updatePinch = () => {
        if (!pinch || pointers.size < 2) return false;
        const points = pinchPoints();
        const a = points[0];
        const b = points[1];
        const distance = Math.max(1, Math.hypot(b.x - a.x, b.y - a.y));
        const center = {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2};
        const zoom = studioClampZoom(pinch.viewport.zoom * distance / pinch.distance);
        STUDIO.viewport = {x: center.x - pinch.world.x * zoom, y: center.y - pinch.world.y * zoom, zoom};
        studioApplyViewport();
        studioRenderEdges();
        studioRenderNodes();
        return true;
      };
      area.addEventListener('pointerdown', event => {
        if (event.pointerType === 'touch') {
          pointers.set(event.pointerId, pointFor(event));
          if (pointers.size === 2) startPinch();
          if (pointers.size >= 2) {
            event.preventDefault();
            area.setPointerCapture?.(event.pointerId);
            return;
          }
        }
        if (event.button !== 0 || event.target.closest('.studio-canvas-node') || event.target.closest('.studio-canvas-controls')) return;
        const point = studioCanvasPoint(event);
        STUDIO.drag = {type:'pan', x:point.x, y:point.y, origin:Object.assign({}, STUDIO.viewport)};
        area.classList.add('is-panning');
        area.setPointerCapture?.(event.pointerId);
      });
      area.addEventListener('pointermove', event => {
        if (event.pointerType === 'touch' && pointers.has(event.pointerId)) {
          pointers.set(event.pointerId, pointFor(event));
          if (updatePinch()) {
            event.preventDefault();
            return;
          }
        }
        const drag = STUDIO.drag;
        if (!drag) return;
        const point = studioCanvasPoint(event);
        if (drag.type === 'pan') {
          STUDIO.viewport = {x:drag.origin.x + point.x - drag.x, y:drag.origin.y + point.y - drag.y, zoom:drag.origin.zoom};
          STUDIO.viewportFittedForSession = true;
        } else {
          const node = studioNode(drag.nodeId);
          if (!node) return;
          node.position = {x:drag.origin.x + (point.x - drag.x) / STUDIO.viewport.zoom, y:drag.origin.y + (point.y - drag.y) / STUDIO.viewport.zoom};
        }
        studioApplyViewport();
        studioRenderEdges();
        studioRenderNodes();
      });
      const end = event => {
        if (event?.pointerType === 'touch') {
          pointers.delete(event.pointerId);
          if (pointers.size < 2) pinch = null;
        }
        if (STUDIO.drag) studioPersistCanvas(80);
        STUDIO.drag = null;
        area.classList.remove('is-panning');
      };
      area.addEventListener('pointerup', end); area.addEventListener('pointercancel', end); area.addEventListener('pointerleave', event => { if (event.buttons === 0) end(event); });
      area.addEventListener('wheel', event => { event.preventDefault(); studioZoomAt(event.deltaY > 0 ? .9 : 1.1, event); }, {passive:false});
    }
    async function studioCreateChildNode(parent) {
      if (!STUDIO.current || !parent) return;
      if (String(STUDIO.selectedNodeId || '') === String(parent.id || '')) studioPanelChanged(parent);
      const params = Object.assign({}, parent.params || {});
      const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/canvas/nodes', {method:'POST', body:JSON.stringify({parent_id:parent.id, template_id:parent.template_id, params, title:'新画面'})});
      if (!res.success) return showToast(res.message || '创建画面失败', 'bad');
      const graph = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/canvas');
      if (graph.success) STUDIO.canvas = graph.data;
      studioSelectNode(res.data.id, true); showToast('已开始新的创作，参数已继承', 'ok');
    }
    async function studioDeleteNode(nodeId) {
      const node = studioNode(nodeId);
      if (!node || !node.parent_id) return;
      if (!await confirmAction(`确定删除「${node.title || '画面'}」吗？图片记录不会删除。`, {title:'删除画面', confirmText:'删除画面'})) return;
      const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/canvas/nodes/' + encodeURIComponent(nodeId), {method:'DELETE'});
      if (!res.success) return showToast(res.message || '删除画面失败', 'bad');
      STUDIO.canvas = res.data.canvas || STUDIO.canvas;
      STUDIO.selectedNodeId = STUDIO.canvas.root_node_id || '';
      studioRenderCanvas(); showToast('画面已删除', 'ok');
    }
    async function studioRunNode(node) {
      if (!STUDIO.current || !node || STUDIO.running) return;
      studioPanelChanged(node);
      const params = Object.assign({}, node.params || {});
      const isRoot = !node.parent_id;
      const parentNodeId = isRoot ? '' : (node.status === 'draft' ? node.parent_id : node.id);
      const targetNodeId = node.status === 'draft' || isRoot ? node.id : '';
      const payload = {parent_node_id: parentNodeId, target_node_id: targetNodeId, template_id: node.template_id || STUDIO.current.template || '', node_params: Object.assign({}, params), graph: {prompt:params.prompt || '', mode:params.mode || STUDIO.current.graph?.mode || 'group', aspect_ratio:params.aspect_ratio || '自动', resolution:params.resolution || '1K', count:Number(params.count || 1), use_persona_identity:STUDIO.current.graph?.use_persona_identity !== false}};
      const optimisticDraft = node.status === 'draft';
      if (optimisticDraft) { node.status = 'running'; node.error = ''; }
      STUDIO.running = true; STUDIO.taskNodeId = String(node.id || ''); STUDIO.taskProgress = null; studioSetStatus('正在提交生成…'); studioRenderCanvas();
      try {
        const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/run', {method:'POST', body:JSON.stringify(payload)});
        if (!res.success) throw new Error(res.message || '生成提交失败');
        STUDIO.taskId = res.data && res.data.task_id || '';
        STUDIO.taskProgress = res.data || null;
        studioPollTask(STUDIO.taskId);
      } catch (error) {
        if (optimisticDraft) node.status = 'draft';
        STUDIO.running = false; STUDIO.taskId = ''; STUDIO.taskNodeId = ''; STUDIO.taskProgress = null; studioSetStatus(error.message || '生成提交失败', 'bad'); studioRenderCanvas();
      }
    }
    async function studioPollTask(taskId) {
      studioStopPoll();
      const poll = async () => {
        try {
          const res = await studioApi('/api/studio/tasks/' + encodeURIComponent(taskId));
          if (!res.success) throw new Error(res.message || '状态读取失败');
          const task = res.data || {};
          if (task.status === 'queued' || task.status === 'running') { STUDIO.taskProgress = task; studioSetStatus('生成中… ' + (taskStageText(task) || taskProgressText(task) || '')); studioRenderPanel(); STUDIO.pollTimer = setTimeout(poll, 1200); return; }
          STUDIO.taskProgress = task;
          STUDIO.running = false; STUDIO.taskId = ''; STUDIO.taskNodeId = '';
          const sessionRes = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id));
          if (sessionRes.success) STUDIO.current = sessionRes.data;
          const graphRes = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/canvas');
          if (graphRes.success) STUDIO.canvas = graphRes.data;
          studioSetStatus(task.success ? '生成完成，画布已更新' : (task.error || '生成失败'), task.success ? 'ok' : 'bad');
          studioRenderCanvas();
        } catch (error) { studioSetStatus(error.message || '状态读取失败', 'bad'); STUDIO.pollTimer = setTimeout(poll, 2600); }
      };
      STUDIO.pollTimer = setTimeout(poll, 900);
    }
    function studioStopPoll() { if (STUDIO.pollTimer) { clearTimeout(STUDIO.pollTimer); STUDIO.pollTimer = null; } }
    function renderStudioSessionSelect() {
      const select = $('studioSessionSelect');
      if (!select) return;
      select.innerHTML = '<option value="">选择画布…</option>' + (STUDIO.sessions || []).map(item => {
        const id = String(item.id || '');
        const suffix = id.slice(-4).toUpperCase();
        const template = item.template || item.template_id || '';
        const status = item.last_status || '';
        const detail = [template, Number(item.result_count || 0) ? `${Number(item.result_count)} 图` : '', status, suffix ? `#${suffix}` : ''].filter(Boolean).join(' · ');
        return `<option value="${escapeHtml(id)}">${escapeHtml(item.title || '未命名')} · ${escapeHtml(detail)}</option>`;
      }).join('');
      if (STUDIO.current?.id) select.value = STUDIO.current.id;
    }
    function syncStudioEditorVisibility() {
      const has = !!STUDIO.current?.id;
      if ($('studioEditor')) $('studioEditor').hidden = !has;
      if ($('studioEmptyState')) $('studioEmptyState').hidden = has;
      if ($('studioDeleteBtn')) $('studioDeleteBtn').disabled = !has;
    }
    function renderStudio() { syncStudioEditorVisibility(); renderStudioSessionSelect(); studioBindCanvasInteractions(); if (!STUDIO.current) return; studioRenderCanvas(); }
    function openStudioCreateModal() { renderStudioTemplateSelect(); $('studioCreateTitle').value = ''; $('studioCreateStatus').textContent = ''; if ($('studioCreateConfirmBtn')) $('studioCreateConfirmBtn').disabled = false; $('studioCreateModal').classList.add('show'); setTimeout(() => $('studioCreateTitle')?.focus(), 0); }
    function closeStudioCreateModal() { $('studioCreateModal')?.classList.remove('show'); }
    async function loadStudioSessionList(force = false) {
      const fresh = !force && STUDIO.sessionsLoadedAt && (Date.now() - STUDIO.sessionsLoadedAt) < CANVAS_SESSION_CACHE_TTL_MS;
      if (fresh) return {success:true, data:{sessions:STUDIO.sessions, templates:STUDIO.templates}};
      if (STUDIO.sessionsRequest) return STUDIO.sessionsRequest;
      const request = studioApi('/api/studio/sessions').then(res => {
        if (!res.success) return res;
        STUDIO.sessions = res.data?.sessions || [];
        STUDIO.templates = res.data?.templates || STUDIO.templates || [];
        STUDIO.sessionsLoadedAt = Date.now();
        return res;
      }).finally(() => { STUDIO.sessionsRequest = null; });
      STUDIO.sessionsRequest = request;
      return request;
    }
    async function loadStudioList(selectId, options = {}) {
      const force = options === true || options?.force === true;
      const id = String(selectId || STUDIO.current?.id || '').trim();
      if (id && STUDIO.current?.id && id !== String(STUDIO.current.id)) {
        studioStopPoll();
        STUDIO.running = false;
        STUDIO.taskId = '';
        STUDIO.taskNodeId = '';
        STUDIO.taskProgress = null;
      }
      const loadSeq = ++STUDIO.loadSeq;
      const needsDetail = Boolean(id) && (force || STUDIO.current?.id !== id || !STUDIO.canvas);
      const detailRequest = needsDetail ? Promise.all([
        studioApi('/api/studio/sessions/' + encodeURIComponent(id)),
        studioApi('/api/studio/sessions/' + encodeURIComponent(id) + '/canvas')
      ]) : Promise.resolve(null);
      const [res, detailPair] = await Promise.all([loadStudioSessionList(force), detailRequest]);
      if (!res.success) return showToast(res.message || '画布列表加载失败', 'bad');
      if (id && needsDetail) {
        // Detail and graph are independent endpoints; fetch them together so opening
        // a session waits for the slower request only once.
        const [detail, canvas] = detailPair;
        if (loadSeq !== STUDIO.loadSeq) return;
        if (detail.success) STUDIO.current = detail.data;
        if (canvas.success) {
          STUDIO.canvas = canvas.data;
          STUDIO.viewport = Object.assign({x:120,y:80,zoom:1}, canvas.data.viewport || {});
        }
        STUDIO.viewportFittedForSession = false;
        const rememberedNode = safeStorageGet(`selfieStudioSelected:${id}`);
        STUDIO.selectedNodeId = (rememberedNode && studioNode(rememberedNode))
          ? rememberedNode
          : String(canvas?.data?.root_node_id || '');
      } else if (!id) {
        STUDIO.current = null;
        STUDIO.canvas = null;
        STUDIO.selectedNodeId = '';
        STUDIO.viewportFittedForSession = false;
      }
      if (loadSeq !== STUDIO.loadSeq) return;
      renderStudio();
      const lastRun = STUDIO.current?.last_run;
      if (lastRun?.status === 'running' && lastRun.task_id) {
        STUDIO.taskId = String(lastRun.task_id);
        STUDIO.taskNodeId = String(lastRun.summary?.target_node_id || lastRun.summary?.parent_node_id || '');
        STUDIO.running = true;
        studioPollTask(STUDIO.taskId);
      }
    }
    async function studioCreate() {
      const template = $('studioTemplateSelect')?.value || 'duo';
      const title = $('studioCreateTitle')?.value?.trim() || '';
      const confirm = $('studioCreateConfirmBtn');
      const status = $('studioCreateStatus');
      if (confirm) confirm.disabled = true;
      if (status) status.textContent = '创建中…';
      try {
        const res = await studioApi('/api/studio/sessions', {method:'POST', body:JSON.stringify({title, template})});
        if (!res.success) throw new Error(res.message || '创建失败');
        STUDIO.current = res.data;
        STUDIO.canvas = res.data?.canvas || null;
        STUDIO.viewport = Object.assign({x:120, y:80, zoom:1}, STUDIO.canvas?.viewport || {});
        STUDIO.viewportFittedForSession = false;
        STUDIO.selectedNodeId = String(STUDIO.canvas?.root_node_id || '');
        upsertStudioSessionSummary(res.data);
        STUDIO.sessionsLoadedAt = Date.now();
        closeStudioCreateModal();
        renderStudio();
        studioSelectNode(STUDIO.canvas?.root_node_id, true);
        showToast('已创建无限画布', 'ok');
      } catch (error) {
        if (status) status.textContent = error.message || '创建失败';
        if (confirm) confirm.disabled = false;
      }
    }
    async function studioSave() { if (STUDIO.current) studioPersistCanvas(0); }
    async function studioDelete() {
      if (!STUDIO.current || !await confirmAction(`确定删除画布「${STUDIO.current.title || '当前画布'}」吗？`, {title:'删除画布', confirmText:'删除画布'})) return;
      const res = await studioApi('/api/studio/sessions/' + encodeURIComponent(STUDIO.current.id) + '/delete', {method:'POST', body:'{}'});
      if (!res.success) return showToast(res.message || '删除失败', 'bad');
      STUDIO.current = null; STUDIO.canvas = null; await loadStudioList('', {force:true}); showToast('画布已删除', 'ok');
    }
