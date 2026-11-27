    let TEST_EXTRA_REFS = [];
    let REFERENCE_GALLERY_REQUEST = null;
    function closeReferenceGallery() {
      REFERENCE_GALLERY_REQUEST = null;
      $('referenceGalleryModal').classList.remove('show');
      $('referenceGalleryBody').replaceChildren();
    }
    async function openReferenceGallery(onPick) {
      const request = {};
      REFERENCE_GALLERY_REQUEST = request;
      const panel = $('referenceGalleryBody');
      panel.innerHTML = '<div class="status" role="status">正在加载缩略图…</div>';
      $('referenceGalleryModal').classList.add('show');
      try {
        const res = await loadGalleryPickerItems();
        if (REFERENCE_GALLERY_REQUEST !== request) return;
        if (!res.success) throw new Error(res.message || '图库读取失败');
        const items = res.data?.items || [];
        panel.innerHTML = '<div class="actions"><button class="ok" type="button" data-ref-confirm disabled>确定</button></div><div class="studio-gallery-grid"></div>' + (items.length ? '' : '<div class="status">暂无可用图片</div>');
        const confirm = panel.querySelector('[data-ref-confirm]');
        let selected = null;
        panel.dataset.galleryOffset = '0';
        renderGalleryPage(panel, items, (item, cell) => {
          panel.querySelectorAll('.selected').forEach(element => element.classList.remove('selected'));
          cell.classList.add('selected'); selected = item; confirm.disabled = false;
        });
        confirm.onclick = async () => {
          if (!selected || REFERENCE_GALLERY_REQUEST !== request) return;
          confirm.disabled = true;
          try {
            const ok = await onPick({image_path:selected.path, source:'record', source_record_id:selected.record_id || ''});
            if (ok !== false && REFERENCE_GALLERY_REQUEST === request) closeReferenceGallery();
          } catch (error) { showToast(error.message || '选择图片失败', 'bad'); }
          finally { if (confirm.isConnected) confirm.disabled = false; }
        };
      } catch (error) {
        if (REFERENCE_GALLERY_REQUEST === request) panel.textContent = error.message || '图库读取失败';
      }
    }
    function renderExtraReferences(container, rows, {onChange, disabled = false}) {
      if (!container) return;
      let busy = false;
      const items = Array.isArray(rows) ? rows : [];
      container.innerHTML = `<div class="extra-refs-heading"><h3>额外参考图</h3><button class="icon-button" type="button" data-ref-add title="添加额外参考图" aria-label="添加额外参考图" ${disabled ? 'disabled' : ''}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg></button></div><div class="extra-refs-list">${items.map((item, index) => `<div class="extra-ref-item" data-ref-index="${index}"><div class="extra-ref-image">${item.image_path ? `<img data-cache-path="${escapeHtml(item.image_path)}" data-cache-variant="thumbnail" alt="额外参考图 ${index + 1}">` : item.image ? `<img src="${escapeHtml(item.image)}" alt="额外参考图 ${index + 1}">` : '<span class="muted">未选择图片</span>'}</div><div class="extra-ref-actions"><button class="secondary mini" type="button" data-ref-upload ${disabled ? 'disabled' : ''}>上传</button><button class="secondary mini" type="button" data-ref-gallery ${disabled ? 'disabled' : ''}>图库</button><button class="danger mini" type="button" data-ref-remove ${disabled ? 'disabled' : ''}>移除</button></div></div>`).join('')}</div>`;
      const change = async next => {
        if (busy || disabled || !container.isConnected) return false;
        busy = true;
        container.querySelectorAll('button').forEach(button => { button.disabled = true; });
        try {
          const ok = await onChange(next);
          if (ok !== false && container.isConnected) renderExtraReferences(container, next, {onChange, disabled});
          return ok;
        } catch (error) { showToast(error.message || '参考图保存失败', 'bad'); return false; }
        finally {
          busy = false;
          if (container.isConnected) container.querySelectorAll('button').forEach(button => { button.disabled = disabled; });
        }
      };
      container.querySelector('[data-ref-add]').onclick = () => {
        if (items.length >= 12) return showToast('最多添加 12 张额外参考图', 'info');
        void change([...items, {id:'ref_' + Math.random().toString(16).slice(2)}]);
      };
      container.querySelectorAll('[data-ref-index]').forEach(card => {
        const index = Number(card.dataset.refIndex);
        const replace = image => change(items.map((item, i) => i === index ? {id:item.id, ...image} : item));
        card.querySelector('[data-ref-remove]').onclick = () => change(items.filter((_, i) => i !== index));
        const input = document.createElement('input');
        input.type = 'file'; input.accept = 'image/*'; input.hidden = true; input.dataset.refFile = 'true';
        card.appendChild(input);
        input.onchange = async () => {
          const file = input.files?.[0];
          input.value = '';
          if (!file) return;
          try { await replace({image:await readFileDataUrl(file), source:'upload'}); }
          catch (error) { showToast(error.message || '上传失败', 'bad'); }
        };
        card.querySelector('[data-ref-upload]').onclick = () => input.click();
        card.querySelector('[data-ref-gallery]').onclick = () => openReferenceGallery(replace);
      });
      loadProtectedImages(container);
    }
    function renderTestExtraReferences() {
      renderExtraReferences($('testExtraReferences'), TEST_EXTRA_REFS, {onChange:next => { TEST_EXTRA_REFS = next; return true; }});
    }
