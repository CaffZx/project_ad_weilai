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

function _actionLabel(a) {
  return {
    eliminate_to_low_bid_pool: '淘汰',
    adjust_bid: '调出价',
    adjust_budget: '调预算',
    adjust_placement: '调广告位',
    keep: '保持',
    create_campaign: '新增',
    prefiltered: '预过滤',
    skipped: '丢失',
  }[a] || a || '?';
}

function _badgeKlass(action) {
  if (action === 'eliminate_to_low_bid_pool') return 'eliminate';
  if (action === 'create_campaign') return 'create';
  if ((action || '').startsWith('adjust')) return 'adjust';
  return 'keep';
}

// ── 主渲染入口 ──
function _fullRender(state) {
  const vm = state;
  const { mode } = vm;

  // 告警
  _renderWarnings(vm);

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
    _show('camp-synthesis');
    _renderSynthesis(vm);
  } else {
    _show('camp-filters');
    _show('camp-list');
    _hide('camp-synthesis');
    _renderPortfolioFilterPills(state);
    _renderBatchToolbar(state);
    _renderCards(state);
  }

  // 预算汇总（固定区）
  _renderBudgetSummary(state);
  _renderSummaryStats(vm);
  _renderTabButtons(state);
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

// ── 告警 ──
function _renderWarnings(vm) {
  const warnings = vm.warnings || [];
  const el = _$('camp-warnings');
  if (!el) return;
  if (!warnings.length) { _hide('camp-warnings'); return; }
  el.textContent = `⚠ ${warnings.length} 条告警`;
  el.title = warnings.join('\n');
  el.classList.remove('hidden');
  el.onclick = () => { alert(warnings.join('\n\n')); };
}

// ── 概览统计 ──
function _renderSummaryStats(vm) {
  const s = vm.summary || {};
  _$('camp-sum-total').textContent = s.total ?? '-';
  _$('camp-sum-elim').textContent = s.eliminate ?? 0;
  _$('camp-sum-adj').textContent = s.adjust ?? 0;
  _$('camp-sum-keep').textContent = s.keep ?? 0;
  _$('camp-sum-new').textContent = s.create ?? 0;

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
function _renderTabButtons(state) {
  const tabs = _$('camp-tabs');
  if (!tabs) return;
  tabs.innerHTML = `
    <button class="camp-ctab-btn ${state._activeTab === 'detail' ? 'active' : ''}" data-action="camp-switch-tab" data-tab="detail">明细</button>
    <button class="camp-ctab-btn ${state._activeTab === 'summary' ? 'active' : ''}" data-action="camp-switch-tab" data-tab="summary">汇总</button>
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

// ── 卡片列表 ──
function _renderCards(state) {
  const items = state._filteredItems;
  const el = _$('camp-list');
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
            <div class="meta">
              × ${_esc(adj.child_asin || '?')}
              | ${_esc(adj.match_type || '?')}
              | 关键词: ${_esc(adj.keyword_text || '?')}
              ${adj.keyword_count ? '| 词数: ' + adj.keyword_count : ''}
            </div>
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
    if (klass === 'create') {
      extras = _renderNewCampaignExtras(adj);
    } else {
      if (adj.placement_adjustments && adj.placement_adjustments.length > 0) {
        extras += _renderPlacements(adj.placement_adjustments);
      }
      if (adj.negative_keywords && adj.negative_keywords.length > 0) {
        extras += _renderNegKeywords(adj.negative_keywords);
      }
    }

    return `
      <div class="camp-adjustment-card ${klass}" data-key="${_esc(adj.item_id)}">
        ${checkboxHtml}
        <div style="flex:1;min-width:0;">
          ${reviewBadge}
          <div class="head">
            <span class="camp-badge camp-badge-${klass}">${_actionLabel(adj.action)}</span>
            <span class="camp-badge camp-badge-${confKlass}">${_esc(adj.confidence || 'medium')}</span>
            <strong style="word-break:break-all;">${_esc(adj.campaign_name || '?')}</strong>
          </div>
          <div class="meta">
            × ${_esc(adj.child_asin || '?')}
            | ${_esc(adj.match_type || '?')}
            | 关键词: ${_esc(adj.keyword_text || '?')}
            ${adj.keyword_class ? '| ' + _esc(adj.keyword_class) : ''}
            ${adj.match_type === 'EXACT' && adj.natural_rank != null
              ? '| 自然排名: 第' + adj.natural_rank + '位' + _rankArrow(adj.rank_change) : ''}
            ${adj.match_type === 'EXACT' && adj.natural_rank == null && adj.near_natural_rank != null
              ? '| <span style="color:#D97706;">⚠ 已掉榜(上次第' + adj.near_natural_rank + '位)</span>' : ''}
            ${adj.is_core ? '| <span style="color:#DC2626;">⚠ 核心词</span>' : ''}
            ${adj.triggered_rule ? '| 触发规则: <code>' + _esc(adj.triggered_rule) + '</code>' : ''}
            ${adj.review_level ? '| 审核: ' + _esc(adj.review_level) : ''}
          </div>
          <div class="values">${vals}</div>
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

// ── 否词 ──
function _renderNegKeywords(negKws) {
  if (!negKws || !negKws.length) return '';
  const decl = negKws.filter(n => n.is_declaration);
  const real = negKws.filter(n => !n.is_declaration);
  let h = '';
  if (decl.length) {
    h += '<div style="margin-top:8px;font-size:12px;">';
    h += '<strong>否词策略：</strong>';
    h += decl.map(n => _esc(n.keyword || '')).join('，');
    h += '</div>';
  }
  if (real.length) {
    const rec = real.filter(n => (n.vote || n.recommend) !== 'optional');
    const opt = real.filter(n => (n.vote || n.recommend) === 'optional');
    h += '<div style="margin-top:8px;font-size:12px;color:var(--camp-muted-fg);"><strong>否定关键词：</strong></div>';
    if (rec.length) {
      h += '<div style="font-size:11px;margin-left:12px;margin-top:2px;">推荐：';
      h += rec.map(n => _esc(n.keyword || '?')).join('、');
      h += '</div>';
    }
    if (opt.length) {
      h += '<div style="font-size:11px;margin-left:12px;margin-top:2px;">可选：';
      h += opt.map(n => _esc(n.keyword || '?')).join('、');
      h += '</div>';
    }
  }
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

  const constraints = bs.portfolio_constraints || {};
  const countByName = {};
  state._campaignItems.forEach(it => {
    const name = it.ai_portfolio_class || '';
    if (name) countByName[name] = (countByName[name] || 0) + 1;
  });

  const editing = state._editingConstraint;

  const pills = PORTFOLIO_NAMES.map(name => {
    const esc = name.replace(/'/g, "\\'");
    const active = state._portfolioFilter === name ? ' active' : '';
    const isElim = name === '低价捡漏组';

    // 金额行
    let amt = '—';
    if (!isElim && bs.target_budget != null) {
      const constraint = constraints[name];
      if (constraint != null) amt = '$' + constraint.toFixed(0);
    }
    const amtLabel = isElim ? '' : (constraints[name] != null ? '约束' : '');

    // 约束编辑行（仅在交互模式 + 非淘汰组 + 目标预算存在时显示）
    const canEdit = state.executable !== false && !isElim && bs.target_budget != null;
    const actLine = !canEdit ? ''
      : (editing === name
        ? `<span class="pp-acts">
             <input class="camp-pp-edit-input" type="number" step="1" min="0" value="${state._curConstraint(name) || ''}">
             <a class="pp-act" data-action="camp-save-constraint" data-portfolio="${esc}">保存</a>
           </span>`
        : `<span class="pp-acts">
             <a class="pp-act" data-action="camp-edit-constraint" data-portfolio="${esc}">改</a>
             <a class="pp-act" data-action="camp-exec-constraint" data-portfolio="${esc}">执行</a>
             ${state._portfolioOverride[name] != null ? `<a class="pp-act" data-action="camp-reset-constraint" data-portfolio="${esc}">恢复</a>` : ''}
           </span>`);

    return `<button class="camp-portfolio-pill${active}" type="button" data-action="camp-toggle-portfolio" data-portfolio="${esc}">
      <span class="pp-name">${_esc(name)} (${countByName[name] || 0})</span>
      <span class="pp-amount">${amtLabel} ${amt}</span>
      ${actLine}
    </button>`;
  }).join('');

  const src = bs.target_budget_source || '';
  const hint = (src === 'fallback_spend_x1.15')
    ? '<span class="camp-portfolio-fallback-hint">⚠ 按日均花费×1.15 兜底</span>'
    : '';

  el.innerHTML = pills + hint;
  _show('camp-portfolio-pills-row');
}

// ── 预算汇总卡 ──
function _renderBudgetSummary(state) {
  const el = _$('camp-budget-summary');
  if (!el) return;
  const bs = state._budgetSummary;
  if (!bs) { _hide('camp-budget-summary'); return; }

  let selTotal = 0;
  // 遍历全集 _campaignItems（非 _filteredItems）：勾选后再切筛选时，
  // 被藏住的已勾选项仍须计入"已勾选合计"，与"执行后预计总预算"语义一致。
  state._campaignItems.forEach(it => {
    if (it.item_type === 'prefiltered' || it.item_type === 'lost') return;
    if (it.proposed_budget != null && state._selection.has(it.item_id)) {
      selTotal += it.proposed_budget;
    }
  });

  el.innerHTML = `
    <div class="camp-card">
      <div class="camp-card-title">执行后预计总预算</div>
      <div class="camp-budget-summary-totals">
        <span class="stat">预算约束 <strong>$${_fmtMoney(bs.target_budget)}</strong></span>
        <span class="stat">已勾选合计 <strong>$${selTotal.toFixed(2)}</strong></span>
      </div>
    </div>`;
  _show('camp-budget-summary');
}

// ── 导出（供 panel.js 调用） ──
export { _fullRender, toggleDetail, _actionLabel, _fmtMoney, _esc };
