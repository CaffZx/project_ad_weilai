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
    _detailUserToggled: new Set(),   // 用户手动展开/收起的卡片，密度切换不覆盖
    _budgetSummary: null,
    _portfolioFilter: '',
    _processFilter: '',
    _portfolioOverride: {},
    _reallocOpen: false,
    _confirm: null,            // {kind:'approve'|'reject'|'exec', title, msg} 确认弹窗
    _synthesisGroups: [],
    _synthesisSpecials: [],
    _activeTab: 'detail',
    _density: _loadDensity(),
  };

  // 外部 render 函数引用（由 panel.js 注入）
  let _render = null;
  function _reRender() { if (_render) _render(state); }

  state.setRenderer = (fn) => { _render = fn; };

  function _loadDensity() {
    try {
      const v = localStorage.getItem('camp_density');
      return ['compact', 'comfy', 'detailed'].includes(v) ? v : 'compact';
    } catch (_) {
      return 'compact';
    }
  }

  state.toggleDensity = function (mode) {
    if (!['compact', 'comfy', 'detailed'].includes(mode)) return;
    state._density = mode;
    state._detailUserToggled.clear();   // 密度切换是用户主动意图，重置所有手动标记
    try { localStorage.setItem('camp_density', mode); } catch (_) {}
    _reRender();
  };

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

  state._selectedItems = function () {
    return state._campaignItems.filter(it => state._selection.has(it.item_id));
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

    const pf = state._processFilter || '';
    const _isProcessed = (it) =>
      !!state._reviewState[it.item_id] ||
      ['CONFIRMED', 'REJECTED'].includes(String(it.confirm_status || '').toUpperCase());

    state._filteredItems = state._campaignItems.filter(it => {
      if (fa && it.action_klass !== fa) return false;
      if (fs && it.conf_klass !== fs) return false;
      if (fm && it.match_type !== fm) return false;
      if (state._portfolioFilter && it.ai_portfolio_class !== state._portfolioFilter) return false;
      if (pf) {
        // 预过滤/丢失非可复核项，待处理/已处理均不纳入（仅在「全部」下可见）
        if (it.item_type === 'prefiltered' || it.item_type === 'lost') return false;
        const done = _isProcessed(it);
        if (pf === 'done' && !done) return false;
        if (pf === 'pending' && done) return false;
      }
      if (q) {
        const hay = (it.keyword_text + ' ' + it.campaign_name + ' ' + (it.keyword_class || '') + ' ' + it.child_asin).toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
    _reRender();
  };

  // 处理状态分段筛选（全部/待处理/已处理）。#camp-filters 静态不重渲，active 类在此手动切。
  state.setProcessFilter = function (val, el) {
    state._processFilter = val || '';
    if (el && el.parentElement) {
      el.parentElement.querySelectorAll('[data-action="camp-set-process"]')
        .forEach(b => b.classList.toggle('active', b === el));
    }
    state.applyFilters();
  };

  // ── 执行前确认弹窗（同意所选 / 不同意所选 / 回算执行 三按钮共用）──
  // 校验通过才弹窗；执行流绑定在「确认」(runConfirm) 上，「取消」(cancelConfirm) 不执行。
  state.askConfirm = function (kind) {
    if (!state.executable) return;
    if (kind === 'approve' || kind === 'reject') {
      if (state._selection.size === 0) { _toast('请先勾选至少一项'); return; }
      const n = state._selection.size;
      if (kind === 'approve') {
        state._confirm = {
          kind,
          title: '确认同意并下发所选调整',
          msg: '将直接通过ERP对亚马逊广告进行调整，不可撤销，是否确认',
          items: state._selectedItems(),
        };
      } else {
        state._confirm = {
          kind,
          title: '确认不同意所选调整',
          msg: `将把已勾选的 ${n} 项标记为不同意（REJECTED），不会调用 MCP。`,
        };
      }
    } else if (kind === 'exec') {
      state._confirm = {
        kind,
        title: '确认组合预算调整执行',
        msg: '将通过 MCP 真实下发 3 个活动组的预算调整到亚马逊广告。',
      };
    } else { return; }
    _reRender();
  };

  state.cancelConfirm = function () { state._confirm = null; _reRender(); };

  state.runConfirm = function () {
    const c = state._confirm;
    state._confirm = null;
    _reRender();
    if (!c) return;
    if (c.kind === 'approve') state.batchConfirm('approve');
    else if (c.kind === 'reject') state.batchConfirm('reject');
    else if (c.kind === 'exec') state.execConstraints();
  };

  // ── 批量确认 / 导出 ──
  state.batchConfirm = async function (decision) {
    if (!state.executable) return;  // 决策批次不可执行时禁用操作
    if (state._selection.size === 0) {
      _toast('请先勾选至少一项');
      return;
    }

    const selectedKeys = Array.from(state._selection);
    // 即时反馈：MCP 下发慢（§21.3），点确认后先弹轻量提示，避免"点了没反应"；
    // 2400ms 自动消失，结果回来的常驻 toast 会覆盖它。
    _toast(decision === 'approve'
      ? `正在下发 ${selectedKeys.length} 个调整到 MCP，请稍候…`
      : `正在提交 ${selectedKeys.length} 项审核…`);
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
      if (!body) {
        throw new Error('响应为空');
      }
      // 后端返回结构：
      //   approve 路径：{ ok, ops, task_ids[], errors[] }  — ok=true 全成功；ok=false 但 ops>0/task_ids 非空 = 部分成功
      //   reject 路径： { ok, applied, skipped }
      const ops = body.ops || 0;
      const taskIds = body.task_ids || [];
      const errs = body.errors || [];
      const applied = body.applied ?? 0;
      const skipped = body.skipped ?? 0;

      if (decision === 'approve') {
        // approve：调 MCP，不写本地审核态（已不写库），只提示
        if (body.ok === true) {
          selectedKeys.forEach(key => { state._reviewState[key] = decision; });
          _saveReviewState(); _reRender();
          _toast(`已下发 ${ops} 个调整到 MCP${taskIds.length ? `（task=${(taskIds[0]||'').slice(0,8)}…）` : ''}`, {sticky:true});
        } else if (ops > 0 || taskIds.length > 0) {
          // 部分成功
          selectedKeys.forEach(key => { state._reviewState[key] = decision; });
          _saveReviewState(); _reRender();
          _toast(`部分下发成功：${ops} 个调用 / ${errs.length} 个失败 — ${(errs[0]||'').slice(0,80)}`, {sticky:true});
        } else {
          throw new Error((errs[0]) || body.error || '全部下发失败');
        }
      } else {
        // reject：原写库链路
        if (body.ok === false) {
          throw new Error(body.error || '审核写回失败');
        }
        selectedKeys.forEach(key => { state._reviewState[key] = decision; });
        _saveReviewState(); _reRender();
        _toast(`已写回 ${applied} 项${skipped ? `，跳过 ${skipped} 项` : ''}`, {sticky:true});
      }
    } catch (e) {
      console.warn('[campaign-panel] /campaign/confirm 失败:', e.message);
      _toast(`审核写回失败：${e.message || '未知错误'}`, {sticky:true});
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
  // ── 回算修改弹窗（一次改 3 个活动组；低价捡漏固定 $1 不参与）──
  state.openRealloc = function () {
    if (!state.executable) return;  // 仅最新已完成批次可改
    state._reallocOpen = true;
    _reRender();
    setTimeout(() => {
      const input = document.querySelector('.camp-realloc-input:not([disabled])');
      if (input) { input.focus(); input.select(); }
    }, 0);
  };

  state.closeRealloc = function () {
    state._reallocOpen = false;
    _reRender();
  };

  state.saveRealloc = function () {
    const inputs = document.querySelectorAll('.camp-realloc-input[data-portfolio]');
    let n = 0;
    inputs.forEach((inp) => {
      const name = inp.dataset.portfolio;
      const num = parseFloat(inp.value);
      if (name && !isNaN(num) && num >= 0) { state._portfolioOverride[name] = num; n++; }
    });
    state._reallocOpen = false;
    _reRender();
    _toast(`已暂存 ${n} 组预算，点「执行」下发到 MCP`);
  };

  // 「执行」（组合预算调整）：把暂存的 _portfolioOverride 通过 MCP 真实下发组合预算。
  // 后端走 /campaign/execute-portfolio-budget → 实时查 portfolioId → advert_mcp_client
  // dry-run 由后端 advert_exec_dry_run 开关控制（.env）。两步模型：先「组合预算调整」暂存，再「执行」下发。
  state.execConstraints = async function () {
    if (!state.executable) return;
    const ov = state._portfolioOverride || {};
    if (!Object.keys(ov).length) {
      _toast('请先用「组合预算调整」设置各组预算再执行');
      return;
    }
    const _API = (window.location.origin || '') + '/api/v1/agent/ad-direction';
    try {
      _toast('正在下发组合预算调整到 MCP...');
      const resp = await fetch(_API + '/campaign/execute-portfolio-budget', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          asin: state.asin,
          decision_id: state._currentRunId,
          operator: ((window._erpParams || {}).userId) || 'tab5',
          portfolio_overrides: ov,
        }),
      });
      let body = null;
      try { body = await resp.json(); } catch (_) {}
      if (!resp.ok || !body || body.ok === false) {
        throw new Error((body && body.error) || `HTTP ${resp.status}`);
      }
      const isDryRun = body.dry_run === true;
      const applied = body.applied ?? 0;
      const recordId = (body.record_id || '').slice(0, 8);
      const warn = (body.warnings && body.warnings.length) ? `（${body.warnings.length} 项提示）` : '';
      _toast(
        isDryRun
          ? `已 DRY-RUN 落库 ${applied} 组预算调整（未真改广告）record=${recordId}${warn}`
          : `已下发 ${applied} 组预算调整到 MCP record=${recordId}${warn}`,
        {sticky:true}
      );
    } catch (e) {
      console.warn('[campaign-panel] /campaign/execute-portfolio-budget 失败:', e.message);
      _toast(`组合预算执行失败：${e.message || '未知错误'}`, {sticky:true});
    }
  };

  state.resetConstraints = function () {
    state._portfolioOverride = {};
    state._reallocOpen = false;
    _reRender();
    _toast('已恢复 AI 回算推荐');
  };


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
    state.synthesis = vm.synthesis || null;
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
  // 轻量提示（默认）：2400ms 自动消失；执行结果（opts.sticky）：常驻 + 右上角「×」可关闭。
  function _toast(msg, opts = {}) {
    const text = (msg == null ? '' : String(msg)).trim();
    let el = document.getElementById('camp-toast');
    if (!text) {                         // 无内容不展示（防空壳/残留空框）
      if (el) el.classList.remove('show');
      return;
    }
    if (!el) {
      el = document.createElement('div');
      el.id = 'camp-toast';
      el.className = 'camp-toast';
      document.body.appendChild(el);
    }
    clearTimeout(el._tid);
    if (opts.sticky === true) {
      el.classList.add('sticky');
      el.innerHTML = '';
      const span = document.createElement('span');
      span.textContent = text;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'camp-toast-close';
      btn.textContent = '×';
      btn.setAttribute('aria-label', '关闭');
      btn.onclick = () => el.classList.remove('show');
      el.appendChild(span);
      el.appendChild(btn);
      el.classList.add('show');           // 常驻：不设自动消失 timer
    } else {
      el.classList.remove('sticky');
      el.textContent = text;              // 清掉上一条 sticky 残留的子节点
      el.classList.add('show');
      el._tid = setTimeout(() => el.classList.remove('show'), 2400);
    }
  }

  return state;
}
