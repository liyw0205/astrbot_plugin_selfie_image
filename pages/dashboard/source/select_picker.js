    let SELECT_PICKER = null;
    const SELECT_PICKER_SEARCH_THRESHOLD = 8;
    const SELECT_PICKER_OBSERVER = new MutationObserver(() => {
      if (SELECT_PICKER?.matches(':disabled')) closeSelectPicker();
      else renderSelectPickerOptions();
    });
    function closeSelectPicker() {
      const select = SELECT_PICKER;
      SELECT_PICKER = null;
      SELECT_PICKER_OBSERVER.disconnect();
      $('selectPickerModal').classList.remove('show');
      select?.setAttribute('aria-expanded', 'false');
      if (select?.isConnected && !select.matches(':disabled')) select.focus({preventScroll:true});
    }
    function renderSelectPickerOptions() {
      const select = SELECT_PICKER;
      if (!select) return;
      if (!select.isConnected) { closeSelectPicker(); return; }
      const search = $('selectPickerSearch');
      search.hidden = Array.from(select.options).filter(option => !option.hidden && !option.closest('optgroup')?.hidden).length <= SELECT_PICKER_SEARCH_THRESHOLD;
      if (search.hidden) search.value = '';
      const query = $('selectPickerSearch').value.trim().toLocaleLowerCase();
      let group = null;
      const rows = Array.from(select.options).map((option, index) => {
        const parent = option.closest('optgroup');
        if (option.hidden || parent?.hidden || (query && !`${option.text} ${parent?.label || ''}`.toLocaleLowerCase().includes(query))) return '';
        const heading = parent && parent !== group ? `<div class="select-picker-group">${escapeHtml(parent.label)}</div>` : '';
        group = parent;
        return `${heading}<button type="button" class="secondary" data-select-index="${index}" aria-pressed="${option.selected}" ${option.disabled || parent?.disabled ? 'disabled' : ''}><span>${escapeHtml(option.text || '默认')}</span><span aria-hidden="true">${option.selected ? '✓' : ''}</span></button>`;
      }).join('');
      $('selectPickerOptions').innerHTML = rows || '<div class="status">没有匹配的选项</div>';
    }
    function openSelectPicker(select) {
      if (!select || select.multiple || select.size > 1 || select.matches(':disabled')) return;
      if (SELECT_PICKER === select) return;
      if (SELECT_PICKER) closeSelectPicker();
      SELECT_PICKER = select;
      $('selectPickerTitle').textContent = select.getAttribute('aria-label') || select.labels?.[0]?.textContent.trim() || select.parentElement.querySelector('label')?.textContent.trim() || '选择选项';
      $('selectPickerSearch').value = '';
      select.setAttribute('aria-haspopup', 'dialog');
      select.setAttribute('aria-controls', 'selectPickerModal');
      select.setAttribute('aria-expanded', 'true');
      renderSelectPickerOptions();
      SELECT_PICKER_OBSERVER.observe(select, {childList:true, subtree:true, attributes:true, characterData:true});
      $('selectPickerModal').classList.add('show');
      $('selectPickerModal').querySelector('.modal-body').scrollTop = 0;
      $('selectPickerClose').focus({preventScroll:true});
    }
    const isPickerSelect = target => target instanceof HTMLSelectElement && !target.multiple && target.size <= 1;
    document.addEventListener('pointerdown', event => {
      if (isPickerSelect(event.target) && !event.target.matches(':disabled')) event.preventDefault();
    }, true);
    document.addEventListener('mousedown', event => {
      if (isPickerSelect(event.target) && !event.target.matches(':disabled')) event.preventDefault();
    }, true);
    document.addEventListener('click', event => {
      if (!isPickerSelect(event.target)) return;
      event.preventDefault();
      openSelectPicker(event.target);
    }, true);
    $('selectPickerClose').onclick = closeSelectPicker;
    $('selectPickerSearch').oninput = renderSelectPickerOptions;
    $('selectPickerModal').addEventListener('click', event => {
      if (event.target === $('selectPickerModal')) { event.stopImmediatePropagation(); closeSelectPicker(); return; }
      const button = event.target.closest('[data-select-index]');
      const select = SELECT_PICKER;
      if (!button || button.disabled || !select?.isConnected || select.matches(':disabled')) return;
      const index = Number(button.dataset.selectIndex), option = select.options[index];
      if (!option || option.disabled || option.closest('optgroup')?.disabled) return;
      const changed = select.selectedIndex !== index;
      select.selectedIndex = index;
      closeSelectPicker();
      if (changed) {
        select.dispatchEvent(new Event('input', {bubbles:true}));
        select.dispatchEvent(new Event('change', {bubbles:true}));
      }
    });
    document.addEventListener('keydown', event => {
      if (SELECT_PICKER) {
        if (event.key === 'Escape') {
          event.preventDefault(); event.stopImmediatePropagation(); closeSelectPicker(); return;
        }
        const buttons = [...$('selectPickerOptions').querySelectorAll('button:not(:disabled)')];
        if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) && (event.target !== $('selectPickerSearch') || event.key.startsWith('Arrow'))) {
          event.preventDefault(); event.stopImmediatePropagation();
          const current = buttons.indexOf(document.activeElement);
          const index = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
            : current < 0 ? (event.key === 'ArrowDown' ? 0 : buttons.length - 1)
            : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
          buttons[index]?.focus();
        } else if (event.key === 'Tab') {
          const controls = [$('selectPickerClose'), ...($('selectPickerSearch').hidden ? [] : [$('selectPickerSearch')]), ...buttons];
          const index = controls.indexOf(document.activeElement);
          if (event.shiftKey && index <= 0) { event.preventDefault(); controls.at(-1)?.focus(); }
          else if (!event.shiftKey && (index < 0 || index === controls.length - 1)) { event.preventDefault(); controls[0].focus(); }
        }
        return;
      }
      if (isPickerSelect(event.target) && ['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault(); event.stopImmediatePropagation(); openSelectPicker(event.target);
      }
    }, true);
