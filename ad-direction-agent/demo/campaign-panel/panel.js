/**
 * campaign-panel/panel.js
 * 唯一入口 — mountCampaignPanel(root, options)
 *
 * 创建骨架 → 创建 state → 挂载事件 → 请求数据 → 渲染。
 * 返回 { refresh, getReviewState, unmount } 控制 API。
 */

import { normalizeViewModel, ViewMode } from './viewmodel.js';
import { createCampaignState } from './state.js';
import { mountEventDelegation } from './events.js';
import { _fullRender } from './render.js';

const API = (() => {
  try { return window.location.origin + '/api/v1/agent/ad-direction'; }
  catch (_) { return '/api/v1/agent/ad-direction'; }
})();

const API_TIMEOUT = {
  campaign: 1800,
  snapshot: 30,
};

/**
 * @param {HTMLElement} containerEl
 * @param {Object} options
 * @param {string} options.asin
 * @param {number} [options.days=7]
 * @param {string} [options.mode='interactive']
 * @param {number} [options.temperature]
 */
export async function mountCampaignPanel(containerEl, options = {}) {
  const {
    asin,
    days = 7,
    mode = ViewMode.INTERACTIVE,
    temperature,
    executable = true,
    decision_id = '',
    run_id = '',
    write_erp = false,
    analysis_mode = 'REALTIME',
    onComplete = null,
  } = options;

  // 1. DOM 骨架
  containerEl.innerHTML = `
    <div class="camp-root">
      <div class="camp-app-layout">
        <div class="camp-main-area">

          <!-- 免责声明 -->
          <div class="camp-disclaimer hidden" id="camp-disclaimer">
            ⚠ AI 建议仅供参考，请结合运营经验判断。执行操作前请二次确认。
          </div>

          <!-- 告警 -->
          <div id="camp-warnings" class="hidden" style="margin-bottom:8px;padding:6px 12px;background:#FEF2F2;border:1px solid #FECACA;border-radius:6px;font-size:12px;color:#991B1B;cursor:pointer;" title="点击查看详情"></div>

          <!-- 策略总览 -->
          <div id="camp-overview" class="camp-card hidden" style="margin-bottom:12px;">
            <div class="camp-card-title">策略总览（执行总纲）</div>
            <div id="camp-overview-body"></div>
          </div>

          <!-- 分析概览统计 -->
          <div id="camp-summary" class="camp-summary-grid hidden">
            <div class="stat"><div class="num" id="camp-sum-total">-</div><div class="lab">总活动</div></div>
            <div class="stat"><div class="num" id="camp-sum-elim">0</div><div class="lab">淘汰</div></div>
            <div class="stat"><div class="num" id="camp-sum-adj">0</div><div class="lab">调整</div></div>
            <div class="stat"><div class="num" id="camp-sum-keep">0</div><div class="lab">保持</div></div>
            <div class="stat"><div class="num" id="camp-sum-new">0</div><div class="lab">新增</div></div>
            <div style="grid-column:1/-1;font-size:11px;color:var(--camp-muted-fg);" id="camp-sum-meta"></div>
          </div>

          <!-- 预算汇总 -->
          <div id="camp-budget-summary" style="margin-bottom:8px;" class="hidden"></div>

          <!-- 始终可见的操作控制区（明细/汇总 + 筛选 + 组合气泡 + 批量栏）：sticky 钉顶，卡片列表在其下独立滚动 -->
          <div class="camp-sticky-controls">

          <!-- 明细/汇总 Tab -->
          <div id="camp-tabs" class="camp-tab-bar hidden"></div>

          <!-- 加载器 -->
          <div id="camp-loader" style="text-align:center;padding:24px;color:var(--camp-muted-fg);font-size:13px;">
            <span class="camp-spinner"></span> 正在初始化...
          </div>

          <!-- 多维筛选下拉 -->
          <div id="camp-filters" class="hidden" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 12px;margin-bottom:8px;">
            <div class="filter-group">
              <span class="filter-label">动作</span>
              <select id="camp-filter-action">
                <option value="">全部</option>
                <option value="eliminate">淘汰</option>
                <option value="adjust">调整</option>
                <option value="reactivate">复评</option>
                <option value="keep">保持</option>
                <option value="create">新增</option>
                <option value="skipped">预过滤/丢失</option>
              </select>
            </div>
            <div class="filter-group">
              <span class="filter-label">置信度</span>
              <select id="camp-filter-status">
                <option value="">全部</option>
                <option value="high">高</option>
                <option value="medium">中</option>
                <option value="low">低</option>
              </select>
            </div>
            <div class="filter-group">
              <span class="filter-label">匹配</span>
              <select id="camp-filter-match">
                <option value="">全部</option>
                <option value="EXACT">EXACT</option>
                <option value="BROAD">BROAD</option>
                <option value="PHRASE">PHRASE</option>
              </select>
            </div>
            <input type="text" id="camp-search" placeholder="搜索关键词/活动名/ASIN...">
            <!-- 处理状态分段筛选：全部 / 待处理 / 已处理（已处理 = 已复核/已确认） -->
            <div class="camp-seg" id="camp-process-seg">
              <button type="button" data-action="camp-set-process" data-process="" class="active">全部</button>
              <button type="button" data-action="camp-set-process" data-process="pending">待处理</button>
              <button type="button" data-action="camp-set-process" data-process="done">已处理</button>
            </div>
          </div>

          <!-- 组合筛选气泡 -->
          <div id="camp-portfolio-pills-row" class="camp-portfolio-pills-row hidden"></div>

          <!-- 批量工具栏 -->
          <div id="camp-batch-toolbar" class="camp-batch-toolbar hidden">
            <button data-action="camp-select-all">全选可见</button>
            <button data-action="camp-clear-selection">清空选择</button>
            <span class="sep"></span>
            <span id="camp-batch-count" style="font-size:12px;color:var(--camp-muted-fg);">已选 0 / 0</span>
            <span class="sep"></span>
            <button class="primary" data-action="camp-batch-approve">同意所选</button>
            <button class="danger" data-action="camp-batch-reject">不同意所选</button>
            <button class="exec" data-action="camp-batch-execute" title="把所有已同意(CONFIRMED)的调整通过 MCP 真实下发到亚马逊广告">执行已同意</button>
            <button data-action="camp-view-records">调整记录</button>
          </div>

          </div><!-- /camp-sticky-controls -->

          <!-- 卡片列表 -->
          <div id="camp-list"></div>

          <!-- 汇总区 -->
          <div id="camp-synthesis" class="hidden"></div>

          <!-- 调整记录（真实执行回写）-->
          <div id="camp-exec-records" class="camp-card hidden" style="margin-top:12px;"></div>

          <!-- 回算修改弹窗挂载点（_renderReallocModal 渲染于此；在 camp-root 内以保事件委托） -->
          <div id="camp-modal-mount"></div>
          <!-- 确认弹窗挂载点（同意/不同意/回算执行 前的确认） -->
          <div id="camp-confirm-mount"></div>

        </div>
      </div>
    </div>
  `;

  const root = containerEl.querySelector('.camp-root');

  // 2. 创建状态
  const state = createCampaignState();
  // executable 不再由调用方传入决定显隐；改由 state.setData 依据 vm.is_latest 自决
  // （「已完成且最新批次」）。options.executable 保留接收但忽略，避免页面层与 ERP 上下文耦合。
  Object.assign(state, { asin, days, mode });

  // 3. 注入渲染回调
  state.setRenderer((st) => { _fullRender(st); });

  // 4. 挂载事件委托
  const cleanupEvents = mountEventDelegation(root, state);

  // 5. 加载数据（mode 由页面级批次选择器驱动，不再内嵌模式条）
  const loader = document.getElementById('camp-loader');
  try {
    if (mode === ViewMode.READONLY) {
      // 快照
      const vm = await _fetchSnapshot(asin, decision_id);
      state.setData(vm);
    } else {
      // 实时
      if (loader) loader.innerHTML = '<span class="camp-spinner"></span> 正在运行分析（可能需数分钟）...';
      const vm = await _fetchRealtime(asin, days, temperature, { run_id, write_erp, analysis_mode });
      state.setData(vm);
      if (typeof onComplete === 'function') onComplete(vm);
    }
  } catch (e) {
    if (loader) { loader.textContent = '分析失败：' + (e.message || '未知错误'); loader.style.cssText = 'color:#DC2626;padding:20px;'; }
    state.setData({
      mode, parent_asin: asin, days, run_id: '', snapshot_time: null,
      summary: { total: 0, eliminate: 0, adjust: 0, keep: 0, create: 0, prefiltered: 0, lost: 0,
                 confidence_high: 0, confidence_medium: 0, confidence_low: 0,
                 budget_impact: null, sanity_check_passed: false },
      overview: null, budget_summary: null, synthesis: null, items: [],
      warnings: [e.message],
    });
  }

  if (loader) loader.classList.add('hidden');

  // 首次渲染
  state._campaignItems = state._campaignItems || [];
  state._filteredItems = [...state._campaignItems];
  state.applyFilters();

  // 6. 返回控制 API
  return {
    refresh: async () => {
      if (loader) { loader.classList.remove('hidden'); loader.innerHTML = '<span class="camp-spinner"></span> 正在运行分析...'; }
      try {
        const vm = mode === ViewMode.READONLY
          ? await _fetchSnapshot(asin, decision_id)
          : await _fetchRealtime(asin, days, temperature, { run_id, write_erp, analysis_mode });
        state.setData(vm);
        state.applyFilters();
        if (mode !== ViewMode.READONLY && typeof onComplete === 'function') onComplete(vm);
      } catch (e) {
        /* 静默降级 */
      }
      if (loader) loader.classList.add('hidden');
    },
    getReviewState: () => state._reviewState,
    unmount: () => {
      cleanupEvents();
      containerEl.innerHTML = '';
    },
  };
}

// ── 内部 fetch ──
function _esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

async function _callAPI(path, body, timeout = 60, method = 'POST') {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeout * 1000);
  try {
    const opts = { method, headers: { 'Content-Type': 'application/json' }, signal: ctrl.signal };
    if (body != null) opts.body = JSON.stringify(body);
    const r = await fetch(`${API}${path}`, opts);
    if (!r.ok) {
      const txt = await r.text();
      throw new Error(`HTTP ${r.status}: ${txt.slice(0, 300)}`);
    }
    return await r.json();
  } catch (e) {
    if (e.name === 'AbortError') throw new Error(`请求超时（${timeout} 秒）`);
    throw e;
  } finally {
    clearTimeout(t);
  }
}

// ERP URL 上下文（panel 有独立 _callAPI，不经主页 callAPI 注入，故在此自取）
function _erpCtx() {
  try {
    const p = new URLSearchParams(window.location.search);
    const o = {};
    if (p.get('shopId')) o._shopId = p.get('shopId');
    if (p.get('shopAccount')) o._shopAccount = p.get('shopAccount');
    if (p.get('parentSellerSku')) o._parentSellerSku = p.get('parentSellerSku');
    if (p.get('userId')) o._userId = p.get('userId');
    return o;
  } catch (_) { return {}; }
}

async function _fetchRealtime(asin, days, temperature, extra = {}) {
  const raw = await _callAPI('/campaign/viewmodel', {
    asin, days,
    temperature: temperature != null ? temperature : undefined,
    run_id: extra.run_id || undefined,
    write_erp: !!extra.write_erp,
    analysis_mode: extra.analysis_mode || 'REALTIME',
    ..._erpCtx(),
  }, API_TIMEOUT.campaign);
  return normalizeViewModel(raw);
}

async function _fetchSnapshot(asin, decision_id) {
  const params = { asin };
  if (decision_id) params.decision_id = decision_id;
  const raw = await _callAPI('/campaign/snapshot?' + new URLSearchParams(params), null, API_TIMEOUT.snapshot, 'GET');
  return normalizeViewModel(raw);
}
