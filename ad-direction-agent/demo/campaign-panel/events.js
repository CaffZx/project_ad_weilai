/**
 * campaign-panel/events.js
 * Event delegation for the campaign panel root.
 */

import { toggleDetail } from './render.js';

export function mountEventDelegation(rootEl, state) {
  if (!rootEl) return () => {};

  function dispatch(action, data, el, state) {
    switch (action) {
      case 'camp-switch-tab':
        state._activeTab = data.tab;
        break;
      case 'camp-toggle-portfolio':
        state.togglePortfolioFilter(data.portfolio);
        return;
      case 'camp-set-process':
        state.setProcessFilter(data.process, el);
        return;
      case 'camp-toggle-select': {
        const cb = el;
        if (cb && cb.dataset && cb.dataset.key) state.toggleSelection(cb.dataset.key);
        return;
      }
      case 'camp-toggle-detail': {
        // 记录用户手动操作，密度切换不再覆盖此卡
        const card = el.closest('.camp-adjustment-card');
        const key = card && card.dataset.key;
        if (key) state._detailUserToggled.add(key);
        toggleDetail(el);
        return;
      }
      case 'camp-select-all':
        state.selectAllVisible();
        return;
      case 'camp-clear-selection':
        state.clearSelection();
        return;
      case 'camp-batch-approve':
        state.askConfirm('approve');
        return;
      case 'camp-batch-reject':
        state.askConfirm('reject');
        return;
      case 'camp-confirm-ok':
        state.runConfirm();
        return;
      case 'camp-confirm-cancel':
        state.cancelConfirm();
        return;
      case 'camp-select-group':
        state.selectGroup(parseInt(data.groupIndex, 10));
        return;
      case 'camp-jump-special':
        state.jumpToSpecial(parseInt(data.specialIndex, 10));
        return;
      case 'camp-jump-group-member':
        state.jumpToGroupMember(parseInt(data.groupIndex, 10), parseInt(data.keyIndex, 10));
        return;
      case 'camp-open-realloc':
        state.openRealloc();
        return;
      case 'camp-realloc-save':
        state.saveRealloc();
        return;
      case 'camp-realloc-cancel':
        state.closeRealloc();
        return;
      case 'camp-exec-all':
        state.askConfirm('exec');
        return;
      case 'camp-reset-all':
        state.resetConstraints();
        return;
      case 'camp-toggle-controls':
        try {
          const cur = localStorage.getItem('camp_controls_collapsed') === '1';
          localStorage.setItem('camp_controls_collapsed', cur ? '0' : '1');
        } catch (e) {
          // Ignore unavailable localStorage.
        }
        break;
      case 'camp-toggle-density':
        state.toggleDensity(data.density);
        return;
      default:
        break;
    }

    state.applyFilters();
  }

  function onClick(e) {
    const el = e.target.closest('[data-action]');
    if (el) {
      dispatch(el.dataset.action, el.dataset, el, state);
      return;
    }

    // 用户正在选中文字（拖拽复制）→ 不触发勾选
    const sel = window.getSelection();
    if (sel && sel.toString().trim().length > 0) return;

    const card = e.target.closest('.camp-adjustment-card');
    if (!card) return;
    if (e.target.closest('button, a, input, select, textarea, .review-badge, .detail')) return;
    const cb = card.querySelector('input[type="checkbox"][data-key]');
    if (!cb || cb.disabled) return;
    cb.click();
    e.preventDefault();
    e.stopPropagation();
  }

  function onChange(e) {
    const t = e.target;
    if (t.matches('#camp-filter-action, #camp-filter-status, #camp-filter-match')) {
      t.classList.remove('camp-filter-flash');
      void t.offsetWidth;
      t.classList.add('camp-filter-flash');
      setTimeout(() => t.classList.remove('camp-filter-flash'), 650);
      state.applyFilters();
    }
  }

  function onInput(e) {
    if (e.target.matches('#camp-search')) {
      state.applyFilters();
    }
  }

  function onKeyDown(e) {
    if (e.target.matches('.camp-realloc-input') && e.key === 'Enter') {
      e.preventDefault();
      state.saveRealloc();
    }
  }

  rootEl.addEventListener('click', onClick);
  rootEl.addEventListener('change', onChange);
  rootEl.addEventListener('input', onInput);
  rootEl.addEventListener('keydown', onKeyDown);

  return function cleanup() {
    rootEl.removeEventListener('click', onClick);
    rootEl.removeEventListener('change', onChange);
    rootEl.removeEventListener('input', onInput);
    rootEl.removeEventListener('keydown', onKeyDown);
  };
}
