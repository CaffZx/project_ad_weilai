/**
 * campaign-panel/state.js
 * 响应式状态工厂 — 所有状态 + 突变方法收进 module 闭包。
 */

export function createCampaignState() {
  const state = {
    asin: '',
    days: 7,
    mode: null,
    executable: false,  // 由 setData 依 vm.is_latest 自决；默认关闭（未确认最新批次前不显示执行控件）
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

  // run_id 形如 "20260612T031045Z"（UTC），解析为毫秒时间戳；非该格式返回 null。
  function _runIdToMs(runId) {
    const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(runId || '');
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null;
  }

  function _gcOldReviewKeys() {
    try {
      const now = Date.now();
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k && k.startsWith('camp_review_')) {
          // key = camp_review_{asin}_{days}_{run_id}；末段是 run_id（UTC 时间串）。
          // 原先 parseInt("20260612T031045Z")=20260612，与 Date.now()(~1.7e12) 比较恒为旧
          // → 每次都把刚写入的当前审核态一并删掉。改为正确解析；解析失败则跳过删除（保守）。
          const parts = k.split('_');
          const ms = _runIdToMs(parts[parts.length - 1]);
          if (ms != null && (now - ms > 7 * 86400 * 1000)) localStorage.removeItem(k);
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
          operator: ((window._erpParams || {}).userId) || 'tab5',
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
    // 自动加入勾选（门禁与全模块统一用 executable：B 态 readonly+executable 也应自动勾选）
    if (state.executable) state._selection.add(key);
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

  // ── 真实执行（Part 6）──
  const _API = (window.location.origin || '') + '/api/v1/agent/ad-direction';
  function _operator() { return ((window._erpParams || {}).userId) || 'tab5'; }

  state.executeConfirmed = async function () {
    if (!state.executable) return;  // 仅最新已完成批次可执行
    const did = state._currentRunId;
    if (!did) { _toast('无可执行批次'); return; }
    if (!confirm('确认对【已同意】的调整执行真实广告调整？\n将调用广告调整工具（dry-run 模式下不会真实修改）。')) return;
    _toast('执行中…');
    try {
      const resp = await fetch(_API + '/campaign/execute', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ asin: state.asin, decision_id: did, operator: _operator() }),
      });
      const body = await resp.json().catch(() => null);
      if (!resp.ok || !body || body.ok === false) {
        throw new Error((body && body.error) || `HTTP ${resp.status}`);
      }
      if (body.dry_run) _toast(`空跑完成：构造 ${body.ops || 0} 项（dry_run，未真实调整）`);
      else _toast(`已提交 ${body.ops || 0} 项${(body.task_ids && body.task_ids.length) ? `，任务 ${body.task_ids.join(',')}` : ''}`);
      await state.loadExecutionRecords();
      if (body.task_ids && body.task_ids.length) setTimeout(() => state.pollExecStatus(did), 4000);
    } catch (e) {
      _toast('执行失败：' + (e.message || '未知错误'));
    }
  };

  state.pollExecStatus = async function (did) {
    try {
      await fetch(_API + '/campaign/execute/status?decision_id=' + encodeURIComponent(did || state._currentRunId));
      await state.loadExecutionRecords();
    } catch (_) {}
  };

  state.loadExecutionRecords = async function () {
    const did = state._currentRunId;
    if (!did) return;
    try {
      const resp = await fetch(_API + '/campaign/execution-records?decision_id=' + encodeURIComponent(did));
      const body = await resp.json().catch(() => null);
      state._execRecords = (body && body.records) || [];
    } catch (_) { state._execRecords = []; }
    _renderExecRecords();
  };

  function _renderExecRecords() {
    const box = document.getElementById('camp-exec-records');
    if (!box) return;
    const recs = state._execRecords || [];
    if (!recs.length) { box.classList.add('hidden'); box.innerHTML = ''; return; }
    const esc = (s) => { const d = document.createElement('div'); d.textContent = String(s == null ? '' : s); return d.innerHTML; };
    const KIND = { campaign: '活动', keyword: '关键词', placement: '广告位' };
    const rows = [];
    recs.forEach((r) => {
      (r.items || []).forEach((it) => {
        const kind = it._kind;
        let chg = '';
        if (kind === 'campaign') chg = `预算 ${it.old_budget ?? '—'} → ${it.new_budget ?? '—'}` + (it.new_state ? ` / 状态 ${esc(it.new_state)}` : '');
        else if (kind === 'keyword') chg = `竞价 ${it.old_bid ?? '—'} → ${it.new_bid ?? '—'}` + (it.keyword_text ? `（${esc(it.keyword_text)}）` : '');
        else if (kind === 'placement') chg = `${esc(it.placement_type)} ${it.old_percent ?? '—'}% → ${it.new_percent ?? '—'}%`;
        const res = it.modify_result || 'PENDING';
        const color = res === 'SUCCESS' ? '#16A34A' : (res === 'FAIL' ? '#DC2626' : (res === 'DRY_RUN' ? '#6B7280' : '#D97706'));
        rows.push(`<tr><td>${esc(r.create_time || '')}</td><td>${esc(it.campaign_id || '')}</td><td>${KIND[kind] || kind}</td><td>${chg}</td><td style="color:${color};font-weight:600">${esc(res)}</td><td>${esc(it.error_msg || '')}</td></tr>`);
      });
    });
    box.classList.remove('hidden');
    box.innerHTML = `<div class="camp-card-title">调整记录（${recs.length} 批 / ${rows.length} 项）</div>
      <table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead><tr style="text-align:left;color:var(--camp-muted-fg)"><th>时间</th><th>活动ID</th><th>类型</th><th>变更</th><th>结果</th><th>错误</th></tr></thead>
      <tbody>${rows.join('')}</tbody></table>`;
  }

  // ── 设置数据 ──
  state.setData = function (vm) {
    state.mode = vm.mode;
    // 显隐门禁（勾选框/批量栏/执行按钮）唯一由「展示中的批次是否为已完成的最新批次」决定：
    // vm.is_latest = read_snapshot 读 decision.is_latest（finalize_batch 维护，每 ASIN 唯一最新）。
    // 不再依赖页面层传入 executable / /decision/context —— 解除与 in_progress、ERP 可达性的耦合。
    state.executable = vm.is_latest === true;
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
