/**
 * campaign-panel/render.js
 * 全部渲染函数 — ViewModel → DOM。
 * 所有模板字符串使用 .camp- 前缀 CSS class + data-action 事件委托。
 */

import { PORTFOLIO_NAMES } from './viewmodel.js';

// ── 工具函数 ──
function _$(id) { return document.getElementById(id); }
function _show(id) { const el = _$(id); if (el) el.classList.remove('hidden'); }
function _hide(id) { const el = _$(id); if (el) el.classList.add('hidden'); }
function _esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}
function _fmtMoney(v) {
  if (v == null || isNaN(v)) return '—';
  return Number(v).toFixed(2);
}
function _fmtChange(cur, prop) {
  if (cur == null && prop == null) return '—';
  if (cur == null) return `$${_fmtMoney(prop)}`;   // 无原值（如新增活动）→ 单值
  if (prop == null) return `$${_fmtMoney(cur)}`;
  const d = prop - cur;
  if (Math.abs(d) < 0.005) return `$${_fmtMoney(cur)}`;   // 无变化 → 单值，不画箭头
  const sign = d >= 0 ? '+' : '';
  const color = d >= 0 ? '#16A34A' : '#DC2626';   // 涨=绿/降=红（对齐 campaign_test.html / 范例图）
  // 调整前 → 调整后 (变化)
  return `$${_fmtMoney(cur)} <span style="color:var(--camp-muted-fg);">→</span> $${_fmtMoney(prop)}`
       + ` <span style="color:${color};font-size:11px;">(${sign}$${d.toFixed(2)})</span>`;
}

// 真实 bid = 基础 bid ×(1 + 加价比例%/100)。广告位实际出价并非基础 bid。
function _realBid(bid, pct) {
  if (bid == null || isNaN(bid)) return null;
  const p = Number(pct) || 0;
  return Number(bid) * (1 + p / 100);
}

// ── 广告位加价 常驻简要渲染（与 预算/出价 同级常驻）──
// 不改动 .detail 里的 _renderPlacements（展开详情原样保留）；仅追加一个只读简表。
// 真实 bid：当前 = current_bid×(1+current_pct)；调整后 =(proposed_bid ?? current_bid)×(1+proposed_pct)。
// bid 与广告位比例同时变时，右值体现复合后的真实出价。
const _PLACEMENT_ORDER = { '头部': 0, '商品': 1, '其他': 2 };
function _renderPlacementsBrief(adj) {
  const placements = (adj.placement_adjustments || []).filter(p => !p.is_declaration);
  if (!placements.length) return '';
  const baseCur = adj.current_bid;
  const baseProp = (adj.proposed_bid != null) ? adj.proposed_bid : adj.current_bid;
  if (baseCur == null && baseProp == null) return '';

  const sorted = placements.slice().sort((a, b) =>
    (_PLACEMENT_ORDER[a.placement] ?? 9) - (_PLACEMENT_ORDER[b.placement] ?? 9));

  const rows = sorted.map(p => {
    const cp = p.current_pct;
    const pp = (p.proposed_pct != null) ? p.proposed_pct : p.current_pct;
    const rbCur = _realBid(baseCur, cp);
    const rbProp = _realBid(baseProp, pp);
    const name = _esc(p.placement || '?');
    const curStr = `${cp ?? '?'}%（${rbCur != null ? _fmtMoney(rbCur) : '—'}）`;
    // 无变化（比例与真实 bid 均未变）→ 单值
    const changed = (Number(cp) !== Number(pp))
      || (rbCur != null && rbProp != null && Math.abs(rbProp - rbCur) >= 0.005);
    if (!changed) {
      return `<div class="camp-placement-brief-row">${name}：${curStr}</div>`;
    }
    const propStr = `${pp ?? '?'}%（${rbProp != null ? _fmtMoney(rbProp) : '—'}）`;
    const d = (rbProp != null && rbCur != null) ? (rbProp - rbCur) : null;
    const color = d == null ? 'var(--camp-muted-fg)' : (d >= 0 ? '#16A34A' : '#DC2626');
    return `<div class="camp-placement-brief-row">${name}：${curStr}`
         + ` <span style="color:${color};">→</span> ${propStr}</div>`;
  }).join('');

  return `<div class="camp-placement-brief" style="margin-top:4px;font-size:12px;line-height:1.7;">`
       + `<span class="k" style="color:var(--camp-muted-fg);">广告位加价</span>`
       + ` <span style="color:var(--camp-muted-fg);font-size:11px;">(括号内为真实bid)</span>`
       + rows + `</div>`;
}

function _actionLabel(a) {
  return {
    eliminate_to_low_bid_pool: '淘汰',
    paused: '暂停',
    adjust: '调整',
    adjust_bid: '调出价',
    adjust_budget: '调预算',
    adjust_placement: '调广告位',
    reactivate_budget_only: '复评',
    reactivate_with_calibrated_bid: '复评',
    keep: '保持',
    create_campaign: '新增',
    prefiltered: '预过滤',
    skipped: '丢失',
  }[a] || a || '?';
}

function _badgeKlass(action) {
  // 暂停与淘汰同为退出/关停类，共用红色警示样式；中性名避免"暂停挂 eliminate"误导维护
  if (action === 'eliminate_to_low_bid_pool' || action === 'paused') return 'eliminate_or_paused';
  if ((action || '').startsWith('reactivate')) return 'reactivate';
  if (action === 'create_campaign') return 'create';
  if ((action || '').startsWith('adjust')) return 'adjust';
  return 'keep';
}

// ── 主渲染入口 ──
function _fullRender(state) {
  const vm = state;
  const { mode } = vm;
  const root = document.querySelector('.camp-root');
  if (root) {
    root.classList.remove('camp-density-compact', 'camp-density-comfy', 'camp-density-detailed');
    root.classList.add(`camp-density-${state._density || 'compact'}`);
    root.classList.add('camp-tab5-space');
  }

  // sanity 校验未通过警示（sanity 失败照常落库，仅警示不阻断执行）
  _renderSanityNotice(vm);

  // 概览
  _renderOverview(vm);

  // Tab 区
  _show('camp-tabs');

  // 切换 tab
  if (state._activeTab === 'summary') {
    _hide('camp-filters');
    _hide('camp-portfolio-pills-row');
    _hide('camp-batch-toolbar');
    _hide('camp-list');
    _hide('camp-warnings-panel');
    _show('camp-synthesis');
    _renderSynthesis(vm);
  } else if (state._activeTab === 'warnings') {
    _hide('camp-filters');
    _hide('camp-portfolio-pills-row');
    _hide('camp-batch-toolbar');
    _hide('camp-list');
    _hide('camp-synthesis');
    _show('camp-warnings-panel');
    _renderWarningsPanel(vm);
  } else {
    _show('camp-filters');
    _show('camp-list');
    _hide('camp-synthesis');
    _hide('camp-warnings-panel');
    _renderPortfolioFilterPills(state);
    _renderBatchToolbar(state);
    _renderCards(state);
    // 密度模式联动详情区（跳过用户手动操作过的卡片）
    _syncDensityDetails(state);
    // 折叠：隐藏筛选器 + 广告组合栏（执行工具栏 camp-batch-toolbar 保持常驻）
    if (_controlsCollapsed()) {
      _hide('camp-filters');
      _hide('camp-portfolio-pills-row');
    }
  }

  // 预算汇总（固定区）
  _renderBudgetSummary(state);
  _renderSummaryStats(vm);
  _renderTabButtons(state);
  _renderReallocModal(state);
  _renderCoreKeywordModal(state);
  _renderConfirmModal(state);
}

// ── 总览 ──
function _renderOverview(vm) {
  const ov = vm.overview;
  if (!ov) { _hide('camp-overview'); return; }
  const generatedBy = ov.generated_by || (ov.facts && Object.keys(ov.facts).length ? 'fallback' : '');
  if (generatedBy === 'ai' && (ov.assessment_text || ov.direction_text)) {
    let body = '<div class="camp-root text-sm" style="line-height:1.7;white-space:pre-wrap;">';
    if (ov.assessment_text) body += `<div><strong>判断：</strong>${_esc(ov.assessment_text)}</div>`;
    if (ov.direction_text) body += `<div style="margin-top:8px;"><strong>方向：</strong>${_esc(ov.direction_text)}</div>`;
    body += '</div>';
    _$('camp-overview-body').innerHTML = body;
  } else if (generatedBy === 'snapshot') {
    _$('camp-overview-body').innerHTML = `<div class="text-sm muted">${_esc(ov.assessment_text || '')}</div>`;
  } else {
    _$('camp-overview-body').innerHTML = '<div class="text-xs muted">AI 执行总纲未生成（数据不足或超时），请参考下方明细。</div>';
  }
  _show('camp-overview');
}

// ── sanity 校验未通过警示 ──
// validation_passed=0（sanity 旁路 LLM 失败/发现矛盾）→ 数据照常可见可执行，
// 仅复用 .camp-disclaimer（琥珀色）提示人工复核。sanity_check_passed===false 才显示，
// null/undefined（无低置信项跳过校验，或快照无该列）不显示。
function _renderSanityNotice(vm) {
  const el = _$('camp-disclaimer');
  if (!el) return;
  const passed = vm.summary && vm.summary.sanity_check_passed;
  if (passed === false) {
    el.textContent = '⚠ 一致性校验未通过（可能为 sanity 步骤 LLM 抖动，或发现决策矛盾）。数据仍可查看与执行，请人工复核后再确认。';
    el.classList.remove('hidden');
  } else {
    el.classList.add('hidden');
  }
}

// ── 告警（「告警」tab 内容，常驻；空态显示「暂无告警」）──
function _renderWarningsPanel(vm) {
  const warnings = vm.warnings || [];
  const el = _$('camp-warnings-panel');
  if (!el) return;
  if (!warnings.length) {
    el.innerHTML = '<div class="camp-card text-sm muted" style="text-align:center;padding:20px;">暂无告警</div>';
    return;
  }
  el.innerHTML = warnings.map(w =>
    `<div class="camp-card text-sm" style="padding:8px 12px;margin-bottom:6px;background:#FEF2F2;border:1px solid #FECACA;color:#991B1B;">⚠ ${_esc(w)}</div>`
  ).join('');
}

// ── 概览统计 ──
function _renderSummaryStats(vm) {
  const s = vm.summary || {};
  _$('camp-sum-total').textContent = s.total ?? '-';
  _$('camp-sum-elim').textContent = (s.eliminate ?? 0) + (s.paused ?? 0);
  _$('camp-sum-adj').textContent = s.adjust ?? 0;
  _$('camp-sum-keep').textContent = s.keep ?? 0;
  // 「新增/复评」合并位（KB16 新增 + KB21§7 复评）
  _$('camp-sum-new').textContent = (s.create ?? 0) + (s.reactivate ?? 0);

  const conf = `高 ${s.confidence_high || 0} / 中 ${s.confidence_medium || 0} / 低 ${s.confidence_low || 0}`;
  const budget = s.budget_impact ?? 0;
  const budgetStr = budget >= 0 ? `+$${budget.toFixed(2)}` : `-$${Math.abs(budget).toFixed(2)}`;
  const sanity = s.sanity_check_passed ? '✓' : '✗';
  const skippedNote =
    (s.prefiltered ? `  |  预过滤: ${s.prefiltered}` : '') +
    (s.lost ? `  |  丢失: ${s.lost}` : '');
  _$('camp-sum-meta').textContent =
    `置信: ${conf}  |  预算影响: ${budgetStr}  |  校验: ${sanity}${skippedNote}`;
  _show('camp-summary');
}

// ── Tab 按钮 ──
// 折叠筛选器/广告组合栏 的持久化标记（localStorage，跨重渲染/刷新保持）
function _controlsCollapsed() {
  try { return localStorage.getItem('camp_controls_collapsed') === '1'; } catch (e) { return false; }
}

function _renderTabButtons(state) {
  const tabs = _$('camp-tabs');
  if (!tabs) return;
  const warnN = (state.warnings || []).length;
  const collapsed = _controlsCollapsed();
  const density = state._density || 'compact';
  tabs.innerHTML = `
    <button class="camp-ctab-btn ${state._activeTab === 'detail' ? 'active' : ''}" data-action="camp-switch-tab" data-tab="detail">明细</button>
    <button class="camp-ctab-btn ${state._activeTab === 'summary' ? 'active' : ''}" data-action="camp-switch-tab" data-tab="summary">汇总</button>
    <button class="camp-ctab-btn ${state._activeTab === 'warnings' ? 'active' : ''}" data-action="camp-switch-tab" data-tab="warnings">告警${warnN ? ` (${warnN})` : ''}</button>
    ${state._activeTab === 'detail' ? `
    <span class="camp-tab-right">
      <span class="camp-density-toggle" title="调整卡片密度">
        <button type="button" class="${density === 'compact' ? 'active' : ''}" data-action="camp-toggle-density" data-density="compact">紧凑</button>
        <button type="button" class="${density === 'comfy' ? 'active' : ''}" data-action="camp-toggle-density" data-density="comfy">舒适</button>
        <button type="button" class="${density === 'detailed' ? 'active' : ''}" data-action="camp-toggle-density" data-density="detailed">详细</button>
      </span>
      <button class="camp-ctab-fold" data-action="camp-toggle-controls" title="折叠/展开 筛选器与广告组合栏（执行工具栏保持常驻）">${collapsed ? '▸ 展开筛选栏' : '▾ 折叠筛选栏'}</button>
    </span>` : ''}
  `;
}

// ── 批量工具栏 ──
function _renderBatchToolbar(state) {
  if (!state.executable) { _hide('camp-batch-toolbar'); return; }
  _show('camp-batch-toolbar');
  const cnt = state._selection.size;
  const total = state._filteredItems.filter(it => it.item_type !== 'prefiltered' && it.item_type !== 'lost').length;
  _$('camp-batch-count').textContent = `已选 ${cnt} / ${total}`;
}

// ── 密度联动详情区（对齐补丁 codex-ux:tab5-density）──
function _syncDensityDetails(state) {
  const mode = state._density;
  if (!mode || mode === 'comfy') return;  // 舒适模式不动
  _$('camp-list').querySelectorAll('.camp-adjustment-card').forEach(card => {
    const key = card.dataset.key;
    // 用户手动操作过 → 密度切换不覆盖
    if (key && state._detailUserToggled.has(key)) {
      card.dataset.codexDetailUser = '1';
      return;
    }
    const detail = card.querySelector('.detail');
    const btn = card.querySelector('[data-action="camp-toggle-detail"]');
    if (!detail) return;
    if (mode === 'compact') {
      if (!detail.classList.contains('hidden')) {
        detail.classList.add('hidden');
        detail.style.display = 'none';
        if (btn) btn.textContent = '展开详情';
      }
    } else if (mode === 'detailed') {
      if (detail.classList.contains('hidden')) {
        detail.classList.remove('hidden');
        detail.style.display = 'block';
        if (btn) btn.textContent = '收起详情';
      }
    }
  });
}

// ── 卡片列表 ──
// 卡片数达到此阈值才切两列瀑布（低于则单列满宽，避免筛选后仅剩 1 张时半宽孤卡）
const _TWO_COL_MIN = 2;

function _renderMetaChips(adj) {
  const chips = [];
  if (adj.child_asin) chips.push(['asin', _esc(adj.child_asin)]);
  if (adj.match_type) chips.push(['match', _esc(adj.match_type)]);
  if (adj.keyword_text) chips.push(['kw', '关键词: <b>' + _esc(adj.keyword_text) + '</b>']);
  if (adj.keyword_class) chips.push(['class', _esc(adj.keyword_class)]);
  if (adj.match_type === 'EXACT' && adj.natural_rank != null) {
    chips.push(['rank', '自然排名: <b>第' + adj.natural_rank + '位</b>' + _rankArrow(adj.rank_change)]);
  } else if (adj.match_type === 'EXACT' && adj.near_natural_rank != null) {
    chips.push(['warn', '⚠ 已掉榜(上次第' + adj.near_natural_rank + '位)']);
  }
  if (adj.is_core) chips.push(['warn', '⚠ 核心词']);
  if (adj.triggered_rule) chips.push(['rule', '触发规则: <b>' + _esc(adj.triggered_rule) + '</b>']);
  if (adj.review_level) chips.push(['audit', '审核: <b>' + _esc(adj.review_level) + '</b>']);
  return chips.map(([cls, html]) =>
    '<span class="camp-meta-chip camp-meta-' + cls + '">' + html + '</span>'
  ).join('');
}

function _renderCards(state) {
  const items = state._filteredItems;
  const el = _$('camp-list');
  // 两列瀑布：卡片数 ≥ 阈值时启用（浏览器自动均衡两列高度）；1 张或空时单列满宽
  if (el) el.classList.toggle('camp-2col', items.length >= _TWO_COL_MIN);
  if (!items.length) {
    el.innerHTML = '<div class="camp-card" style="text-align:center;color:var(--camp-muted-fg);padding:30px;">无匹配活动</div>';
    return;
  }

  el.innerHTML = items.map(adj => {
    // 预过滤（灰卡）
    if (adj.item_type === 'prefiltered') {
      return `
        <div class="camp-adjustment-card skipped" data-key="${_esc(adj.item_id)}">
          <div style="flex:1;min-width:0;">
            <div class="head">
              <span class="camp-badge camp-badge-skipped">预过滤</span>
              <strong style="word-break:break-all;">${_esc(adj.campaign_name || '?')}</strong>
            </div>
            <div class="meta">${_renderMetaChips(adj)}</div>
            <div class="text-sm muted" style="margin-top:6px;">
              过滤原因：${_esc(adj.reason || '')}
            </div>
          </div>
        </div>`;
    }

    // 丢失（红卡 + ⚠）
    if (adj.item_type === 'lost') {
      return `
        <div class="camp-adjustment-card skipped" data-key="${_esc(adj.item_id)}">
          <div class="card-checkbox">
            <input type="checkbox" disabled title="丢失活动无法审核">
          </div>
          <div style="flex:1;min-width:0;">
            <div class="head">
              <span class="camp-badge camp-badge-skipped">丢失</span>
              | 关键词: ${_esc(adj.keyword_text || '?')}
            </div>
            <div class="text-sm" style="color:#DC2626;margin-top:6px;">
              ⚠ ${_esc(adj.reason || '未被分析')}
            </div>
          </div>
        </div>`;
    }

    // 正常卡片（existing / new）
    const klass = adj.action_klass || _badgeKlass(adj.action);
    const confKlass = adj.conf_klass || adj.confidence || 'medium';
    const fullReason = adj.reason || '';
    const reviewMark = state._reviewState[adj.item_id];
    const reviewBadge = reviewMark
      ? `<span class="review-badge review-${reviewMark}">${reviewMark === 'approve' ? '✓ 已同意' : '✗ 已拒绝'}</span>`
      : '';
    const interactive = state.executable !== false;
    const checkboxHtml = interactive
      ? `<div class="card-checkbox"><input type="checkbox" data-key="${_esc(adj.item_id)}" ${state._selection.has(adj.item_id) ? 'checked' : ''} data-action="camp-toggle-select"></div>`
      : '';
    const selected = state._selection.has(adj.item_id) ? ' selected' : '';

    // values 行
    let vals = '';
    if (adj.proposed_budget != null || adj.current_budget != null) {
      vals += `<span class="camp-kv"><span class="k">预算</span> <span class="v">${_fmtChange(adj.current_budget, adj.proposed_budget)}</span></span>`;
    }
    if (adj.proposed_bid != null || adj.current_bid != null) {
      vals += `<span class="camp-kv"><span class="k">出价</span> <span class="v">${_fmtChange(adj.current_bid, adj.proposed_bid)}</span></span>`;
    }

    // 广告位/否词 专用渲染
    let extras = '';
    let placementBrief = '';
    let negBrief = '';
    if (klass === 'create') {
      extras = _renderNewCampaignExtras(adj);
    } else {
      // 常驻广告位加价简表（真实 bid），不进 .detail
      placementBrief = _renderPlacementsBrief(adj);
      // 常驻否词简表（按类型分组词列表），不进 .detail
      negBrief = _renderNegKeywordsBrief(adj);
      if (adj.placement_adjustments && adj.placement_adjustments.length > 0) {
        extras += _renderPlacements(adj.placement_adjustments);
      }
      if (adj.neg_details && adj.neg_details.length > 0) {
        extras += _renderNegKeywords(adj.neg_details);
      }
    }

    return `
      <div class="camp-adjustment-card ${klass}${selected}" data-key="${_esc(adj.item_id)}">
        ${checkboxHtml}
        <div style="flex:1;min-width:0;">
          ${reviewBadge}
          <div class="head">
            <span class="camp-badge camp-badge-${klass}">${_actionLabel(adj.action)}</span>
            <span class="camp-badge camp-badge-${confKlass}">${_esc(adj.confidence || 'medium')}</span>
            <strong style="word-break:break-all;">${_esc(adj.campaign_name || '?')}</strong>
          </div>
          <div class="meta">${_renderMetaChips(adj)}</div>
          <div class="values">${vals}</div>
          ${placementBrief}
          ${negBrief}
          <div class="reason" style="white-space:pre-wrap;">${_esc(fullReason)}</div>
          <div class="detail hidden" style="display:none;">
            ${_renderEvidence(adj)}
            ${extras}
          </div>
          <div style="margin-top:6px;">
            <button class="camp-btn camp-btn-ghost text-xs" data-action="camp-toggle-detail" data-key="${_esc(adj.item_id)}">展开详情</button>
          </div>
        </div>
      </div>`;
  }).join('');
}

function toggleDetail(btn) {
  const card = btn.closest('.camp-adjustment-card');
  if (!card) return;
  const detail = card.querySelector('.detail');
  if (!detail) return;
  if (detail.classList.contains('hidden')) {
    detail.classList.remove('hidden');
    detail.style.display = 'block';
    btn.textContent = '收起详情';
  } else {
    detail.classList.add('hidden');
    detail.style.display = 'none';
    btn.textContent = '展开详情';
  }
}

// ── 自然排名箭头（周变化）──
function _rankArrow(chg) {
  if (chg == null) return '';
  if (chg === 0) return ' <span style="color:#6B7280;">(持平)</span>';
  return chg > 0
    ? ' <span style="color:#059669;">(↑' + chg + ')</span>'
    : ' <span style="color:#DC2626;">(↓' + Math.abs(chg) + ')</span>';
}

// ── evidence ──
function _renderEvidence(adj) {
  if (!adj.evidence || !adj.evidence.length) return '';
  return `<div style="margin-top:8px;font-size:12px;color:var(--camp-muted-fg);">
    <strong>证据：</strong><ul style="margin:4px 0 0 18px;padding:0;">${adj.evidence.map(e => `<li>${_esc(e)}</li>`).join('')}</ul></div>`;
}

// ── 否词常驻简表（卡片表面，按类型分组展示词列表）──
function _renderNegKeywordsBrief(adj) {
  const brief = adj.neg_keywords_brief;
  if (!brief) return '';
  const parts = [];
  if (brief.exact && brief.exact.length) {
    parts.push('<span style="font-weight:500;">否定精准</span> ' + brief.exact.map(k => _esc(k)).join(' · '));
  }
  if (brief.phrase && brief.phrase.length) {
    parts.push('<span style="font-weight:500;">否定词组</span> ' + brief.phrase.map(k => _esc(k)).join(' · '));
  }
  if (!parts.length) return '';
  return '<div style="margin-top:4px;font-size:11px;color:var(--camp-muted-fg);">' + parts.join(' &nbsp;|&nbsp; ') + '</div>';
}

// ── 广告位调整 ──
function _renderPlacements(placements) {
  if (!placements || !placements.length) return '';
  // is_declaration = 新增活动声明式（"首轮仅声明主投位"）
  const decl = placements.filter(p => p.is_declaration);
  const real = placements.filter(p => !p.is_declaration);
  let h = '';
  if (decl.length) {
    h += '<div style="margin-top:8px;font-size:12px;">';
    h += '<strong>主投广告位：</strong>';
    h += decl.map(p => `${_esc(p.placement)}（${_esc(p.note || '')}）`).join('，');
    h += '</div>';
  }
  if (real.length) {
    h += '<div style="margin-top:8px;font-size:12px;color:var(--camp-muted-fg);"><strong>广告位调整：</strong></div>';
    real.forEach(p => {
      h += `<div style="font-size:12px;margin-left:12px;margin-top:4px;">
        ${_esc(p.placement || '?')}: ${p.current_pct ?? '?'}% → ${p.proposed_pct ?? '?'}%
        ${p.action ? ' (' + _esc(p.action) + ')' : ''}
        ${p.evidence ? '<div style="font-size:11px;color:var(--camp-muted-fg);">' + _esc(p.evidence) + '</div>' : ''}
      </div>`;
    });
  }
  return h;
}

// ── 新增活动主投位/否词专用渲染 ──
function _renderNewCampaignExtras(adj) {
  let h = '<div style="margin-top:8px;font-size:12px;">';

  // 从 placement_adjustments 或 negative_keywords 读 is_declaration
  const placements = adj.placement_adjustments || [];
  const negKws = adj.negative_keywords || [];
  const declPlacements = placements.filter(p => p.is_declaration);
  const declNegKws = negKws.filter(n => n.is_declaration);

  if (declPlacements.length) {
    h += `<strong>主投广告位：</strong>`;
    h += declPlacements.map(p => `${_esc(p.placement)}（${_esc(p.note || '首轮仅声明主投位，不加价')}）`).join('，');
  }

  if (declNegKws.length) {
    h += `<div style="margin-top:6px;"><strong>否词策略：</strong>`;
    h += declNegKws.map(n => _esc(n.keyword || n.negative_strategy || '')).join('，');
    h += '</div>';
  }

  h += '</div>';
  return h;
}

// ── 否词展开明细（逐词逐证据，对齐广告位渲染模式）──
function _renderNegKeywords(negDetails) {
  if (!negDetails || !negDetails.length) return '';
  let h = '<div style="margin-top:8px;font-size:12px;color:var(--camp-muted-fg);"><strong>否词明细：</strong></div>';
  negDetails.forEach(n => {
    const mt = n.match_type || '';
    const mtLabel = mt === 'NEGATIVE_EXACT' ? '精准' : (mt === 'NEGATIVE_PHRASE' ? '词组' : (mt === 'NEGATIVE' ? '否词' : mt));
    h += '<div style="font-size:12px;margin-left:12px;margin-top:4px;">'
      + _esc(n.keyword || '?') + ' <span style="color:var(--camp-muted-fg);">(' + _esc(mtLabel) + ')</span>'
      + (n.evidence ? '<div style="font-size:11px;color:var(--camp-muted-fg);">' + _esc(n.evidence) + '</div>' : '')
      + '</div>';
  });
  return h;
}

// ── 汇总 Synthesis ──
function _renderSynthesis(vm) {
  const sy = vm.synthesis;
  if (!sy || !sy.groups || !sy.groups.length) {
    _$('camp-synthesis').innerHTML = '<div class="camp-card text-sm muted" style="text-align:center;padding:20px;">暂无 AI 汇总</div>';
    return;
  }

  let html = '';
  // groups
  (sy.groups || []).forEach((g, gi) => {
    html += `<div class="camp-synthesis-group">
      <div class="group-title">
        ${gi + 1}. ${_esc(g.title || '')}
        <span class="camp-badge camp-badge-${_badgeKlass(g.action)}">${_actionLabel(g.action)} ×${g.count || (g.campaign_keys || []).length}</span>
        <button class="camp-group-select-btn" data-action="camp-select-group" data-group-index="${gi}" ${!vm.executable ? 'disabled' : ''}>☑ 全选本组</button>
      </div>
      <div class="group-narrative">${_esc(g.narrative || '')}</div>
      <div class="group-keys">
        <details><summary>查看成员活动（${(g.campaign_keys || []).length} 条）</summary>
          <ul>${(g.campaign_keys || []).map((k, ki) =>
            `<li><span class="camp-special-jump" data-action="camp-jump-group-member" data-group-index="${gi}" data-key-index="${ki}">${_esc(k)}</span></li>`
          ).join('')}</ul>
        </details>
      </div>
    </div>`;
  });

  // special cases
  if (sy.special_cases && sy.special_cases.length) {
    html += '<div class="camp-synthesis-special">';
    html += '<div style="font-size:12px; font-weight:600; margin-bottom:4px;">⚠ 特殊调整</div>';
    sy.special_cases.forEach((sc, si) => {
      html += `<div style="font-size:12px; margin-top:2px;">
        ${si + 1}. <span class="camp-special-jump" data-action="camp-jump-special" data-special-index="${si}">${_esc(sc.campaign_key || '?')}</span>
        — ${_esc(sc.why_special || '')}
      </div>`;
    });
    html += '</div>';
  }

  _$('camp-synthesis').innerHTML = html;
}

// ── 组合筛选气泡 + 约束编辑 ──
function _renderPortfolioFilterPills(state) {
  const el = _$('camp-portfolio-pills-row');
  if (!el) return;
  const bs = state._budgetSummary;
  if (!bs) { _hide('camp-portfolio-pills-row'); return; }

  const LOW_BID = '低价捡漏组';
  const constraints = bs.portfolio_constraints || {};
  const money0 = v => '$' + (Number(v) || 0).toFixed(0);

  // 每组活动数 + 调整前/后预算统计（统计值=活动预算之和，前端遍历算，随勾选动态；
  // 与"执行后预计总预算"同口径：勾选→(低价捡漏$1/否则proposed)，未勾选→current）。
  const countByName = {};
  const budgetByName = {};
  PORTFOLIO_NAMES.forEach(n => { countByName[n] = 0; budgetByName[n] = { cur: 0, prop: 0 }; });
  state._campaignItems.forEach(it => {
    if (it.item_type === 'prefiltered' || it.item_type === 'lost') return;
    const p = it.ai_portfolio_class || '自动广泛组';
    if (budgetByName[p] == null) return;
    countByName[p]++;
    budgetByName[p].cur += Number(it.current_budget) || 0;
    budgetByName[p].prop += state._selection.has(it.item_id)
      ? (p === LOW_BID ? 1.0 : (Number(it.proposed_budget) || Number(it.current_budget) || 0))
      : (Number(it.current_budget) || 0);
  });

  const pills = PORTFOLIO_NAMES.map(name => {
    const esc = name.replace(/'/g, "\\'");
    const active = state._portfolioFilter === name ? ' active' : '';
    const isElim = name === LOW_BID;
    const ov = state._portfolioOverride[name];

    // 广告组合预算行：当前原始预算 → AI建议值
    // 低价捡漏组硬编码 $1，不显示 current
    const curBudgets = bs.portfolio_current_budget || {};
    let amt, amtLabel;
    if (isElim) { amt = '$1'; amtLabel = '广告组合预算'; }
    else {
      amtLabel = ov != null ? '组合预算(覆盖)' : '组合预算';
      const cur = curBudgets[name];
      const prop = ov != null ? ov : constraints[name];
      const curStr = cur != null ? '$' + Number(cur).toFixed(0) : '-';
      const propStr = prop != null ? '$' + Number(prop).toFixed(0) : '-';
      amt = curStr + ' → ' + propStr;
    }

    // 1/3/7 天组合实际花费（看板数据，不会为 NULL 时用 — 占位）
    const spend1 = (bs.portfolio_spend_1d || {})[name];
    const spend3 = (bs.portfolio_spend_3d || {})[name];
    const spend7 = (bs.portfolio_spend_7d || {})[name];
    const moneyOrDash = v => v != null ? '$' + Number(v).toFixed(2) : '-';
    const acos1 = (bs.portfolio_acos_1d || {})[name];
    const acos3 = (bs.portfolio_acos_3d || {})[name];
    const acos7 = (bs.portfolio_acos_7d || {})[name];
    const pctOrDash = v => v != null ? (v * 100).toFixed(0) + '%' : '-';
    const spendLine = `<span class="pp-spend">1/3/7天花费：${moneyOrDash(spend1)} / ${moneyOrDash(spend3)} / ${moneyOrDash(spend7)}</span>`;
    const acosLine = `<span class="pp-acos">ACOS：${pctOrDash(acos1)} / ${pctOrDash(acos3)} / ${pctOrDash(acos7)}</span>`;

    // 调整前/后预算（统计值）。低价捡漏组淘汰活动预算固定 $1、统计无意义 → 占位 "—"（美观对齐）
    const b = budgetByName[name];
    const budgetLine = isElim
      ? `<span class="pp-budget" style="opacity:.4;">活动预算之和 调整前 — | 调整后 —</span>`
      : `<span class="pp-budget">活动预算之和 调整前 ${money0(b.cur)} | 调整后 ${money0(b.prop)}</span>`;

    return `<button class="camp-portfolio-pill${active}" type="button" data-action="camp-toggle-portfolio" data-portfolio="${esc}">
      <span class="pp-name">${_esc(name)} (${countByName[name] || 0})</span>
      <span class="pp-amount">${amtLabel} ${amt}</span>
      ${spendLine}
      ${acosLine}
      ${budgetLine}
    </button>`;
  }).join('');

  // 统一操作区（行末，不在气泡内）：回算修改（弹窗内 3 组同改）/ 执行 / 恢复。
  // 门禁 = 可执行 且 有目标预算（提到行级，与单组无关）。低价捡漏组固定 $1 不参与。
  const canEdit = state.executable !== false && bs.target_budget != null;
  const hasOverride = Object.keys(state._portfolioOverride || {}).length > 0;
  const actionsHtml = !canEdit ? '' : `<span class="camp-pp-actions">
      <a class="pp-act" data-action="camp-open-realloc">组合预算调整</a>
      <a class="pp-act" data-action="camp-exec-all">执行</a>
      ${hasOverride ? '<a class="pp-act" data-action="camp-reset-all">恢复</a>' : ''}
    </span>`;

  const src = bs.target_budget_source || '';
  const hint = (src === 'fallback_spend_x1.15')
    ? '<span class="camp-portfolio-fallback-hint">⚠ 按日均花费×1.15 兜底</span>'
    : '';

  el.innerHTML = pills + actionsHtml + hint;
  _show('camp-portfolio-pills-row');
}

function _renderCoreKeywordModal(state) {
  const mount = _$('camp-modal-mount');
  if (!mount || !state._coreKeywordManagement) return;
  const data = state._coreKeywordManagement;
  const labels = { LOCKED: '锁定', ENABLED: '本周启用', DISABLED: '本周不启用', VETOED: '否决' };
  const typeLabels = { semantic: '语义核心', data: '数据核心', manual: '人工' };
  const pendingKeys = state._coreKeywordPending || new Set();
  const keywordKey = (value) => String(value || '').trim().replace(/\s+/g, ' ').toLowerCase();
  const formatEvidence = (value) => {
    if (!value) return '';
    if (Array.isArray(value) && value.length === 0) return '';
    if (typeof value !== 'string') return JSON.stringify(value);
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) {
        if (parsed.length === 0) return '';
        return parsed.map(item => {
          if (typeof item === 'string') return item;
          if (item && typeof item === 'object') {
            return [item.condition, item.value, item.threshold].filter(Boolean).join(' · ');
          }
          return String(item || '');
        }).filter(Boolean).join('\n');
      }
      return parsed && typeof parsed === 'object' ? JSON.stringify(parsed) : String(parsed || '');
    } catch (_) {
      return value;
    }
  };
  const rows = (data.rows || []).map(row => {
    const evidence = [row.semantic_evidence, row.data_evidence, row.manual_reason]
      .map(formatEvidence).filter(Boolean).join('\n');
    const types = (row.types || []).map(type => `<span class="camp-core-type ${_esc(type)}">${_esc(typeLabels[type] || type)}</span>`).join('');
    const isPending = pendingKeys.has(keywordKey(row.keyword_text));
    const choices = isPending
      ? `<button disabled>保存中…</button>`
      : Object.keys(labels).filter(key => key !== row.state).map(key => `<button data-action="camp-core-keyword-state" data-keyword="${_esc(row.keyword_text)}" data-state="${key}">${labels[key]}</button>`).join('');
    const menuClass = isPending ? 'camp-core-menu is-pending' : 'camp-core-menu';
    return `<tr><td class="camp-core-keyword"><strong>${_esc(row.keyword_text)}</strong></td><td class="camp-core-types">${types || '<span class="camp-core-type manual">人工</span>'}</td><td class="camp-core-reason">${_esc(evidence || '无可用 AI 证据')}</td><td><span class="camp-core-state state-${row.state}">${labels[row.state] || row.state}</span></td><td class="camp-core-action"><details class="${menuClass}"><summary aria-label="${isPending ? '保存中' : '切换核心词状态'}">⋮</summary><div class="camp-core-menu-list">${choices}</div></details></td></tr>`;
  }).join('');
  const options = (data.word_pool || []).map(word => `<option value="${_esc(word)}"></option>`).join('');
  const emptyState = data.latest_task ? '本轮没有可管理的核心词' : '暂无核心词分析记录';
  const emptyHtml = `<div class="camp-core-empty"><strong>${emptyState}</strong><span>离线核心词任务完成后，AI 推荐词会自动显示在这里。</span></div>`;
  const addDisabled = pendingKeys.size ? ' disabled' : '';
  mount.innerHTML = `<div class="camp-modal-overlay camp-core-overlay"><section class="camp-modal camp-core-modal" role="dialog" aria-modal="true" aria-label="核心词管理"><header class="camp-core-header"><div class="camp-core-heading"><h3>核心词管理</h3><p>每周进行一次核心词判定AI分析；被人工锁定/否决的词不会参与到AI对核心词的判定分析；设置本周启用或本周不启用的词将被下一次AI分析结果自动覆盖。</p></div><span class="camp-core-count">当前有效核心词 ${data.effective_core_count || 0} / ${data.limit || 30}</span><button class="camp-core-close" data-action="camp-core-keyword-close" aria-label="关闭">×</button></header><div class="camp-core-body"><div class="camp-core-add"><div class="camp-core-search"><input id="camp-core-keyword-input" list="camp-core-keyword-pool" placeholder="搜索本轮离线任务词池（选择后默认锁定）"><datalist id="camp-core-keyword-pool">${options}</datalist></div><button class="camp-core-add-button" data-action="camp-core-keyword-add"${addDisabled}>＋ 添加核心词</button></div>${rows ? `<div class="camp-core-table-wrap"><table class="camp-core-table"><thead><tr><th>核心词</th><th>核心类型</th><th>核心原因</th><th>状态</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div>` : emptyHtml}</div><footer class="camp-core-footer"><button data-action="camp-core-keyword-close">关闭</button></footer></section></div>`;
  const add = mount.querySelector('[data-action="camp-core-keyword-add"]');
  const input = mount.querySelector('#camp-core-keyword-input');
  if (add && input) add.addEventListener('click', () => { add.dataset.keyword = input.value; });
}

// ── 回算修改弹窗（一次改 3 个活动组；低价捡漏固定 $1 不可改）──
// 挂载于 camp-root 内的 #camp-modal-mount，以保事件委托命中。遮罩/对话框均无 data-action，
// 点输入框/空白不触发任何分发；仅底部「取消/保存」按钮有 data-action。
function _renderReallocModal(state) {
  const mount = _$('camp-modal-mount');
  if (!mount) return;
  if (!state._reallocOpen) { mount.innerHTML = ''; return; }

  const bs = state._budgetSummary || {};
  const constraints = bs.portfolio_constraints || {};
  const LOW_BID = '低价捡漏组';
  const money0 = v => '$' + (Number(v) || 0).toFixed(0);

  const heads = PORTFOLIO_NAMES.map(n => `<th>${_esc(n)}</th>`).join('');
  const aiRow = PORTFOLIO_NAMES.map(n => {
    const v = (n === LOW_BID) ? '$1' : (constraints[n] != null ? money0(constraints[n]) : '—');
    return `<td>${v}</td>`;
  }).join('');
  const editRow = PORTFOLIO_NAMES.map(n => {
    if (n === LOW_BID) {
      return `<td><input type="number" value="1" disabled class="camp-realloc-input camp-realloc-fixed"></td>`;
    }
    const esc = _esc(n);
    const init = (state._portfolioOverride[n] != null)
      ? state._portfolioOverride[n]
      : (constraints[n] != null ? Math.round(constraints[n]) : '');
    return `<td><input type="number" step="1" min="0" class="camp-realloc-input" data-portfolio="${esc}" value="${init}"></td>`;
  }).join('');

  mount.innerHTML = `
    <div class="camp-modal-overlay">
      <div class="camp-modal">
        <div class="camp-modal-header">组合预算调整 · 广告组合预算（3 组同改，低价捡漏固定 $1）</div>
        <div class="camp-modal-body">
          <table class="camp-realloc-table">
            <thead><tr><th></th>${heads}</tr></thead>
            <tbody>
              <tr><td class="rowlab">AI 回算推荐</td>${aiRow}</tr>
              <tr><td class="rowlab">运营修改</td>${editRow}</tr>
            </tbody>
          </table>
          <div class="camp-modal-note">保存为临时占位（未持久化），仅改各组「广告组合预算」显示，不影响顶部父目标预算与执行后预计总预算。</div>
        </div>
        <div class="camp-modal-footer">
          <button class="pp-act" data-action="camp-realloc-cancel">取消</button>
          <button class="pp-act primary" data-action="camp-realloc-save">保存</button>
        </div>
      </div>
    </div>`;
}

// ── 执行前确认弹窗（同意/不同意/回算执行）──
// 渲染于独立挂载点 camp-confirm-mount（与回算弹窗互不覆盖）。仅底部「取消/确认」有 data-action。
function _renderConfirmItems(items) {
  if (!items || !items.length) return '';
  const rows = items.map(it => {
    const placements = (it.placement_adjustments || [])
      .filter(p => !p.is_declaration)
      .map(p => {
        const from = p.current_pct != null ? `${p.current_pct}%` : '-';
        const to = p.proposed_pct != null ? `${p.proposed_pct}%` : from;
        return `${_esc(p.placement || '?')}: ${from} -> ${to}`;
      }).join('<br>');
    const budget = (it.current_budget != null || it.proposed_budget != null)
      ? `${_fmtMoney(it.current_budget)} -> ${_fmtMoney(it.proposed_budget)}`
      : '-';
    const bid = (it.current_bid != null || it.proposed_bid != null)
      ? `${_fmtMoney(it.current_bid)} -> ${_fmtMoney(it.proposed_bid)}`
      : '-';
    return `<tr>
      <td title="${_esc(it.campaign_name || '')}">${_esc(it.campaign_name || '-')}</td>
      <td>${_esc(it.keyword_text || '-')}</td>
      <td>${_actionLabel(it.action)}</td>
      <td>${budget}</td>
      <td>${bid}</td>
      <td>${placements || '-'}</td>
    </tr>`;
  }).join('');
  return `
    <div class="camp-confirm-detail">
      <div class="camp-confirm-warn">确认执行以下调整后，将通过 ERP/MCP 下发到亚马逊广告。</div>
      <table class="camp-confirm-table">
        <thead><tr><th>活动</th><th>关键词</th><th>动作</th><th>预算</th><th>出价</th><th>广告位</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function _renderConfirmModal(state) {
  const mount = _$('camp-confirm-mount');
  if (!mount) return;
  const c = state._confirm;
  if (!c) { mount.innerHTML = ''; return; }
  const detail = c.kind === 'approve' ? _renderConfirmItems(c.items || state._confirm.items) : '';
  mount.innerHTML = `
    <div class="camp-modal-overlay">
      <div class="camp-modal" style="width:${c.kind === 'approve' ? 'min(820px,94vw)' : 'min(420px,92vw)'}">
        <div class="camp-modal-header">${c.kind === 'approve' ? '确认执行以下调整' : _esc(c.title || '请确认')}</div>
        <div class="camp-modal-body">
          <div class="text-sm" style="line-height:1.6;color:var(--camp-foreground);">${_esc(c.msg || '')}</div>
          ${detail}
        </div>
        <div class="camp-modal-footer">
          <button class="pp-act" data-action="camp-confirm-cancel">取消</button>
          <button class="pp-act primary" data-action="camp-confirm-ok">${c.kind === 'approve' ? '确认执行' : '确认'}</button>
        </div>
      </div>
    </div>`;
}

// ── 预算汇总卡 ──
function _renderBudgetSummary(state) {
  const el = _$('camp-budget-summary');
  if (!el) return;
  const bs = state._budgetSummary;
  if (!bs) { _hide('camp-budget-summary'); return; }

  const LOW_BID = '低价捡漏组';
  // 执行后预计总预算 = Σ(已勾选→(低价捡漏$1/否则proposed)，未勾选→current)，随勾选动态（统计值）。
  // 遍历全集 _campaignItems（非 _filteredItems）：被筛选藏住的已勾选项仍须计入。
  let execTotal = 0, selectedCount = 0, totalCount = 0;
  state._campaignItems.forEach(it => {
    if (it.item_type === 'prefiltered' || it.item_type === 'lost') return;
    totalCount++;
    const p = it.ai_portfolio_class || '自动广泛组';
    if (state._selection.has(it.item_id)) {
      execTotal += (p === LOW_BID) ? 1.0 : (Number(it.proposed_budget) || Number(it.current_budget) || 0);
      selectedCount++;
    } else {
      execTotal += Number(it.current_budget) || 0;
    }
  });

  const target = bs.target_budget;
  const over = (target != null && execTotal > target);
  const selStyle = over ? 'color:#DC2626;' : '';
  const src = bs.target_budget_source || '';
  const srcHint =
      src === 'override'   ? '<span style="font-size:11px;color:var(--camp-muted-fg)">来源: 运营 override</span>'
    : src === 'asin_data'  ? '<span style="font-size:11px;color:var(--camp-muted-fg)">来源: ASIN 数据</span>'
    : src === 'fallback_spend_x1.15' ? '<span style="font-size:11px;color:#D97706">⚠ 按日均花费×1.15 兜底</span>'
    : '';

  el.innerHTML = `
    <div class="camp-card">
      <div class="camp-card-title">组合预算汇总（预算约束 + 运营同意动态汇总）</div>
      <div class="camp-budget-summary-totals">
        <span class="stat">预算约束 <strong>$${_fmtMoney(target)}</strong> ${srcHint}</span>
        <span class="stat">执行后预计总预算 <strong style="${selStyle}">$${execTotal.toFixed(2)}</strong>
          <span style="font-size:11px;color:var(--camp-muted-fg)">(${selectedCount}/${totalCount} 个已勾选${over ? '，超约束' : ''})</span>
        </span>
      </div>
    </div>`;
  _show('camp-budget-summary');
}

// ── 导出（供 panel.js 调用） ──
export { _fullRender, toggleDetail, _actionLabel, _fmtMoney, _esc };
