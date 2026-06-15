/**
 * campaign-panel/events.js
 * 事件委托 — 在 #camp-root 上挂一组监听器，通过 [data-action] 分发。
 *
 * 验证：主看板和 campaign_test.html 均无 data-action 属性，无命名冲突。
 */

import { toggleDetail } from './render.js';

/**
 * 挂载事件委托，返回 cleanup 函数。
 */
export function mountEventDelegation(rootEl, state) {
  if (!rootEl) return () => {};

  // dispatch 表
  function dispatch(action, data, el, st) {
    switch (action) {
      case 'camp-switch-tab':
        st._activeTab = data.tab;
        break;
      case 'camp-toggle-portfolio':
        st.togglePortfolioFilter(data.portfolio);
        return;
      case 'camp-toggle-select': {
        const cb = el;
        if (cb && cb.dataset && cb.dataset.key) st.toggleSelection(cb.dataset.key);
        return;
      }
      case 'camp-toggle-detail':
        toggleDetail(el);
        return;
      case 'camp-select-all':
        st.selectAllVisible();
        return;
      case 'camp-clear-selection':
        st.clearSelection();
        return;
      case 'camp-batch-approve':
        st.batchConfirm('approve');
        return;
      case 'camp-batch-reject':
        st.batchConfirm('reject');
        return;
      case 'camp-select-group':
        st.selectGroup(parseInt(data.groupIndex));
        return;
      case 'camp-jump-special':
        st.jumpToSpecial(parseInt(data.specialIndex));
        return;
      case 'camp-jump-group-member':
        st.jumpToGroupMember(parseInt(data.groupIndex), parseInt(data.keyIndex));
        return;
      case 'camp-edit-constraint':
        st.startEditConstraint(data.portfolio);
        return;
      case 'camp-save-constraint':
        st.saveConstraint(data.portfolio);
        return;
      case 'camp-exec-constraint':
        st.execConstraint(data.portfolio);
        return;
      case 'camp-reset-constraint':
        st.resetConstraint(data.portfolio);
        return;
      default:
        break;
    }
    // 需要重渲染的 action 在此统一触发
    st.applyFilters();
  }

  // click 委托（覆盖大部分交互）
  function onClick(e) {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    dispatch(el.dataset.action, el.dataset, el, state);
  }

  // mousedown 委托（仅 camp-save-constraint：必须抢在 blur 前执行）
  function onMouseDown(e) {
    const el = e.target.closest('[data-action="camp-save-constraint"]');
    if (el) {
      e.preventDefault();
      e.stopPropagation();
      dispatch('camp-save-constraint', el.dataset, el, state);
    }
  }

  // change 委托（筛选下拉 + 卡片 checkbox）
  function onChange(e) {
    const t = e.target;
    if (t.matches('#camp-filter-action, #camp-filter-status, #camp-filter-match')) {
      state.applyFilters();
    }
  }

  // input 委托（搜索）
  function onInput(e) {
    if (e.target.matches('#camp-search')) {
      state.applyFilters();
    }
  }

  // keydown 委托（约束编辑 Enter 键）
  function onKeyDown(e) {
    if (e.target.matches('.camp-pp-edit-input') && e.key === 'Enter') {
      e.preventDefault();
      const pill = e.target.closest('[data-portfolio]');
      if (pill) state.saveConstraint(pill.dataset.portfolio);
    }
  }

  // blur 捕获（blur 不冒泡但可捕获）
  function onBlurCapture(e) {
    if (e.target.matches('.camp-pp-edit-input')) {
      state.onConstraintBlur();
    }
  }

  rootEl.addEventListener('click', onClick);
  rootEl.addEventListener('mousedown', onMouseDown);
  rootEl.addEventListener('change', onChange);
  rootEl.addEventListener('input', onInput);
  rootEl.addEventListener('keydown', onKeyDown);
  rootEl.addEventListener('blur', onBlurCapture, true);

  return function cleanup() {
    rootEl.removeEventListener('click', onClick);
    rootEl.removeEventListener('mousedown', onMouseDown);
    rootEl.removeEventListener('change', onChange);
    rootEl.removeEventListener('input', onInput);
    rootEl.removeEventListener('keydown', onKeyDown);
    rootEl.removeEventListener('blur', onBlurCapture, true);
  };
}
