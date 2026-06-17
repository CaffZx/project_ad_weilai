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
      case 'camp-set-process':
        st.setProcessFilter(data.process, el);
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
        st.askConfirm('approve');
        return;
      case 'camp-batch-reject':
        st.askConfirm('reject');
        return;
      case 'camp-batch-execute':
        st.askConfirm('execApproved');
        return;
      case 'camp-confirm-ok':
        st.runConfirm();
        return;
      case 'camp-confirm-cancel':
        st.cancelConfirm();
        return;
      case 'camp-view-records':
        st.loadExecutionRecords();
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
      case 'camp-open-realloc':
        st.openRealloc();
        return;
      case 'camp-realloc-save':
        st.saveRealloc();
        return;
      case 'camp-realloc-cancel':
        st.closeRealloc();
        return;
      case 'camp-exec-all':
        st.askConfirm('exec');
        return;
      case 'camp-reset-all':
        st.resetConstraints();
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

  // keydown 委托（回算修改弹窗内 Enter 保存）
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
