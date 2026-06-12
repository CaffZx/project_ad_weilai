/**
 * campaign-panel/state.js
 * 响应式状态工厂 — 所有状态 + 突变方法收进 module 闭包。
 */

export function createCampaignState() {
  const state = {
    asin: '',
    days: 7,
    mode: null,
    executable: true,  // 决策批次驱动: false 时隐藏批量栏+禁勾选
    _campaignItems: [],
    _filteredItems: [],
    _currentRunId: '',
    _reviewState: {},
    _selection: new Set(),
    _budgetSummary: null,
    _portfolioFilter: '',
    _portfolioOverride: {},
    _editingConstraint: null,
    _synthesisGroups: [],
    _synthesisSpecials: [],
    _activeTab: 'detail',
  };

  // 外部 render 函数引用（由 panel.js 注入）
  let _render = null;
  function _reRender() { if (_render) _render(state); }

  state.setRenderer = (fn) => { _render = fn; };

  // ── 审核态 localStorage ──
  function _reviewStorageKey() {
    return `camp_review_${state.asin}_${state.days}_${state._currentRunId}`;
  }

  function _loadReviewState() {
    try {
      const raw = localStorage.getItem(_reviewStorageKey());
      if (raw) state._reviewState = JSON.parse(raw);
    } catch (_) {}
  }

  function _saveReviewState() {
    try {
      localStorage.setItem(_reviewStorageKey(), JSON.stringify(state._reviewState));
    } catch (_) {}
  }

  function _gcOldReviewKeys() {
    try {
      const now = Date.now();
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k && k.startsWith('camp_review_')) {
          const parts = k.split('_');
          const ts = parseInt(parts[parts.length - 1]);
          if (!isNaN(ts) && (now - ts > 7 * 86400 * 1000)) localStorage.removeItem(k);
        }
      }
    } catch (_) {}
  }

  // ── 选项 ──
  state.toggleSelection = function (key) {
    if (!state.executable) return;  // 决策批次不可执行时禁用操作
    if (state._selection.has(key)) state._selection.delete(key);
    else state._selection.add(key);
    _reRender();
  };

  state.updateSelectionCount = function () {
    return state._selection.size;
  };

  function _visibleNonSkippedKeys() {
    return state._filteredItems
      .filter(it => it.item_type !== 'prefiltered' && it.item_type !== 'lost')
      .map(it => it.item_id);
  }

  state.selectAllVisible = function () {
    if (!state.executable) return;  // 决策批次不可执行时禁用操作
    _visibleNonSkippedKeys().forEach(k => state._selection.add(k));
    _reRender();
  };

  state.clearSelection = function () {
    state._selection.clear();
    _reRender();
  };

  // ── 组合筛选 ──
  state.togglePortfolioFilter = function (name) {
    state._portfolioFilter = (state._portfolioFilter === name) ? '' : name;
    state.applyFilters();
  };

  state._resetFilters = function () {
    state._portfolioFilter = '';
    const els = ['camp-filter-action', 'camp-filter-status', 'camp-filter-match'];
    els.forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    const search = document.getElementById('camp-search');
    if (search) search.value = '';
  };

  // ── 筛选 ──
  state.applyFilters = function () {
    const actionEl = document.getElementById('camp-filter-action');
    const statusEl = document.getElementById('camp-filter-status');
    const matchEl  = document.getElementById('camp-filter-match');
    const searchEl = document.getElementById('camp-search');

    const fa = (actionEl && actionEl.value) || '';
    const fs = (statusEl && statusEl.value) || '';
    const fm = (matchEl && matchEl.value) || '';
    const q  = ((searchEl && searchEl.value) || '').toLowerCase();

    state._filteredItems = state._campaignItems.filter(it => {
      if (fa && it.action_klass !== fa) return false;
      if (fs && it.conf_klass !== fs) return false;
      if (fm && it.match_type !== fm) return false;
      if (state._portfolioFilter && it.ai_portfolio_class !== state._portfolioFilter) return false;
      if (q) {
        const hay = (it.keyword_text + ' ' + it.campaign_name + ' ' + (it.keyword_class || '') + ' ' + it.child_asin).toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
    _reRender();
  };

  // ── 批量确认 / 导出 ──
  state.batchConfirm = async function (decision) {
    if (!state.executable) return;  // 决策批次不可执行时禁用操作
    if (state._selection.size === 0) {
      _toast('请先勾选至少一项');
      return;
    }

    const selectedKeys = Array.from(state._selection);
    try {
      const decisions = selectedKeys.map(key => ({ campaign_key: key, decision }));
      const resp = await fetch((window.location.origin || '') + '/api/v1/agent/ad-direction/campaign/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          asin: state.asin,
          days: state.days,
          run_id: state._currentRunId,
          decisions,
          operator: 'tab5',
        }),
      });
      let body = null;
      try { body = await resp.json(); } catch (_) {}
      if (!resp.ok) {
        throw new Error((body && body.error) || `HTTP ${resp.status}`);
      }
      if (!body || body.ok === false) {
        throw new Error((body && body.error) || '审核写回失败');
      }
      selectedKeys.forEach(key => { state._reviewState[key] = decision; });
      _saveReviewState();
      _reRender();
      const applied = body.applied ?? selectedKeys.length;
      const skipped = body.skipped ?? 0;
      _toast(`已写回 ${applied} 项${skipped ? `，跳过 ${skipped} 项` : ''}`);
    } catch (e) {
      console.warn('[campaign-panel] /campaign/confirm 失败:', e.message);
      _toast(`审核写回失败：${e.message || '未知错误'}`);
    }
  };

  state.exportReview = function () {
    const items = [];
    state._campaignItems.forEach(it => {
      if (it.item_type === 'prefiltered' || it.item_type === 'lost') return;
      const dec = state._reviewState[it.item_id] || 'pending';
      items.push({ campaign_key: it.campaign_key, campaign_name: it.campaign_name, decision: dec });
    });
    const blob = new Blob([JSON.stringify({ asin: state.asin, days: state.days, run_id: state._currentRunId, decisions: items }, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `campaign_review_${state.asin}_${state._currentRunId || 'unknown'}.json`;
    a.click();
  };

  // ── 汇总联动 ──
  state.selectGroup = function (gi) {
    if (!state.executable) return;  // 决策批次不可执行时禁用操作
    const group = state._synthesisGroups[gi];
    if (!group) return;
    group.campaign_keys.forEach(k => state._selection.add(k));
    _reRender();
    _toast(`已全选「${group.title}」(${group.campaign_keys.length} 项)`);
  };

  state.jumpToSpecial = function (si) {
    const sc = state._synthesisSpecials[si];
    if (!sc) return;
    // 找到匹配的 item
    const item = state._campaignItems.find(it => it.campaign_key === sc.campaign_key);
    if (item) {
      state._resetFilters();
      state.applyFilters();
      state.jumpToDetail(item.item_id);
    }
  };

  state.jumpToGroupMember = function (gi, ki) {
    const group = state._synthesisGroups[gi];
    if (!group) return;
    const key = group.campaign_keys[ki];
    if (key) {
      state._resetFilters();
      state.applyFilters();
      state.jumpToDetail(key);
    }
  };

  state.jumpToDetail = function (key) {
    state._activeTab = 'detail';
    // 自动加入勾选
    if (state.mode === 'interactive') state._selection.add(key);
    _reRender();
    // scroll + flash
    requestAnimationFrame(() => {
      let card = document.querySelector(`.camp-adjustment-card[data-key="${CSS.escape(key)}"]`);
      // 如果被筛选藏住，重置筛选后重找
      if (!card) {
        state._resetFilters();
        state.applyFilters();
        card = document.querySelector(`.camp-adjustment-card[data-key="${CSS.escape(key)}"]`);
      }
      if (card) {
        // reflow 保证重复跳转同卡片也触发动画
        void card.offsetWidth;
        card.classList.add('flash');
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        setTimeout(() => card.classList.remove('flash'), 1300);
      }
    });
  };

  // ── 预算约束编辑 ──
  state._curConstraint = function (name) {
    if (state._portfolioOverride[name] != null) return state._portfolioOverride[name];
    if (state._budgetSummary && state._budgetSummary.portfolio_constraints) {
      return state._budgetSummary.portfolio_constraints[name];
    }
    return null;
  };

  state.startEditConstraint = function (name) {
    state._editingConstraint = name;
    _reRender();
    setTimeout(() => {
      const input = document.querySelector('.camp-pp-edit-input');
      if (input) { input.focus(); input.select(); }
    }, 0);
  };

  state.onConstraintBlur = function () {
    setTimeout(() => {
      if (state._editingConstraint != null) {
        state._editingConstraint = null;
        _reRender();
      }
    }, 150);
  };

  state.saveConstraint = function (name) {
    const input = document.querySelector('.camp-pp-edit-input');
    if (!input) return;
    const num = parseFloat(input.value);
    if (isNaN(num) || num < 0) { alert('请输入有效金额'); return; }
    state._portfolioOverride[name] = num;
    state._editingConstraint = null;
    _reRender();
    _toast(`已修改「${name}」约束 $${num.toFixed(0)}（占位，未持久化）`);
  };

  state.execConstraint = function (name) {
    _toast(`「${name}」执行：占位，执行流待接入`);
  };

  state.resetConstraint = function (name) {
    delete state._portfolioOverride[name];
    _reRender();
    _toast(`已恢复「${name}」推荐约束`);
  };

  // ── 设置数据 ──
  state.setData = function (vm) {
    state.mode = vm.mode;
    state._currentRunId = vm.run_id || '';
    state._campaignItems = vm.items || [];
    state._filteredItems = [...state._campaignItems];
    if (vm.synthesis) {
      state._synthesisGroups = vm.synthesis.groups || [];
      state._synthesisSpecials = vm.synthesis.special_cases || [];
    } else {
      state._synthesisGroups = [];
      state._synthesisSpecials = [];
    }
    state._budgetSummary = vm.budget_summary || null;
    state.overview = vm.overview || null;
    state.summary = vm.summary || {};
    state.warnings = vm.warnings || [];
    state._portfolioFilter = '';
    state._selection.clear();
    _loadReviewState();
    _gcOldReviewKeys();
  };

  // ── toast ──
  function _toast(msg) {
    let el = document.getElementById('camp-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'camp-toast';
      el.className = 'camp-toast';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(el._tid);
    el._tid = setTimeout(() => el.classList.remove('show'), 1800);
  }

  return state;
}
