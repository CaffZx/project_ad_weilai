/**
 * campaign-panel/viewmodel.js
 * ViewModel 契约 + 兜底归一化（仅防御，不做语义转换）。
 *
 * 白名单：
 *   1. undefined/null → 默认值填充
 *   2. campaign_sample.json 老格式兼容（压平 adjustments/new/skipped → items）
 *   3. console.warn 上报后端漏字段
 *
 * 禁止：字段重命名、类型转换、派生字段计算、mode 语义判断。
 */

export const ViewMode = Object.freeze({
  INTERACTIVE: 'interactive',
  READONLY: 'readonly',
});

export const PORTFOLIO_NAMES = [
  '精准主力组',
  '精准测试组',
  '自动广泛组',
  '低价捡漏组',
];

const ACTION_KLASS_MAP = {
  // 暂停与淘汰同为退出/关停类，共用中性样式名 eliminate_or_paused（防误显，非美化）
  'eliminate_to_low_bid_pool': 'eliminate_or_paused',
  'paused': 'eliminate_or_paused',
  'adjust_bid': 'adjust',
  'adjust_budget': 'adjust',
  'adjust_placement': 'adjust',
  'keep': 'keep',
  'create_campaign': 'create',
  'prefiltered': 'skipped',
  'skipped': 'skipped',
};

function _def(v, fallback) {
  return (v != null) ? v : fallback;
}

function _itemDefaults(item) {
  return {
    // 先铺原始 item，保留后端透传但未列入下方白名单的字段
    // （natural_rank/near_natural_rank/rank_change、confirm_status/execute_status、
    //  keyword_count 等）；下方归一化字段再覆盖，确保默认值不丢。
    ...item,
    item_type: _def(item.item_type, 'existing'),
    item_id: _def(item.item_id, _def(item.campaign_key, item.campaign_name || '')),
    campaign_key: _def(item.campaign_key, ''),
    campaign_name: _def(item.campaign_name, ''),
    child_asin: _def(item.child_asin, ''),
    keyword_text: _def(item.keyword_text, ''),
    match_type: _def(item.match_type, ''),
    keyword_class: _def(item.keyword_class, ''),
    action: _def(item.action, ''),
    reason: _def(item.reason, ''),
    evidence: _def(item.evidence, []),
    confidence: _def(item.confidence, ''),
    current_budget: _def(item.current_budget, null),
    proposed_budget: _def(item.proposed_budget, null),
    current_bid: _def(item.current_bid, null),
    proposed_bid: _def(item.proposed_bid, null),
    placement_adjustments: _def(item.placement_adjustments, []),
    negative_keywords: _def(item.negative_keywords, []),
    ai_portfolio_class: _def(item.ai_portfolio_class, ''),
    triggered_rule: _def(item.triggered_rule, ''),
    review_level: _def(item.review_level, ''),
    is_core: _def(item.is_core, false),
    action_klass: _def(item.action_klass, ACTION_KLASS_MAP[item.action] || 'skipped'),
    conf_klass: _def(item.conf_klass, item.confidence || ''),
  };
}

/**
 * 兜底归一化：只做防御填充 + sample.json 老格式兼容。
 * 不做字段重命名、不做派生字段计算。
 */
export function normalizeViewModel(raw) {
  // 如果后端已返回 items 数组（新格式），直接做默认值填充
  if (raw && Array.isArray(raw.items)) {
    return {
      ...raw,
      mode: raw.mode || ViewMode.INTERACTIVE,
      items: raw.items.map(_itemDefaults),
      summary: raw.summary || {},
      overview: raw.overview || null,
      budget_summary: raw.budget_summary || null,
      synthesis: raw.synthesis || null,
      warnings: raw.warnings || [],
    };
  }

  // 老格式兼容 (campaign_sample.json / 旧 API)：压平 adjustments + new_campaigns + skipped_campaigns
  if (raw && (raw.adjustments || raw.new_campaigns || raw.skipped_campaigns)) {
    if (typeof console !== 'undefined') {
      console.warn('[campaign-panel] normalizeViewModel: 收到老格式数据，执行 sample.json 兼容压平。请确认后端已接入 /campaign/viewmodel。');
    }
    const items = [];

    (raw.adjustments || []).forEach(a => {
      items.push(_itemDefaults({ ...a,
        item_type: 'existing',
        item_id: a.campaign_key || a.campaign_name || '',
        action_klass: ACTION_KLASS_MAP[a.action] || 'adjust',
      }));
    });

    (raw.new_campaigns || []).forEach(n => {
      // 兜底归一 = 跟 backend _item_from_new 一样：primary_placement→is_declaration, negative_strategy→is_declaration
      const primary = n.primary_placement || '';
      const negStrat = n.negative_strategy || '';
      const pa = (primary && primary !== 'N/A')
        ? [{ placement: primary, is_declaration: true, note: '首轮仅声明主投位，不加价' }] : [];
      const nk = negStrat
        ? [{ keyword: negStrat, is_declaration: true }] : [];

      items.push(_itemDefaults({ ...n,
        item_type: 'new',
        item_id: n.campaign_name || n.keyword_text || '',
        campaign_key: n.campaign_name || n.keyword_text || '',
        action: 'create_campaign',
        action_klass: 'create',
        current_budget: 0,
        current_bid: 0,
        proposed_budget: n.proposed_daily_budget ?? n.proposed_budget ?? 3.0,
        proposed_bid: n.proposed_base_bid ?? n.proposed_bid ?? 0.30,
        triggered_rule: n.trigger_scene ?? n.triggered_rule ?? '',
        placement_adjustments: pa,
        negative_keywords: nk,
        is_core: false,
      }));
    });

    const allSkipped = raw.skipped_campaigns || [];
    allSkipped.forEach(s => {
      const isPrefiltered = s.__prefiltered;
      items.push(_itemDefaults({ ...s,
        item_type: isPrefiltered ? 'prefiltered' : 'lost',
        item_id: s.campaign_key || s.campaign_name || '',
        action: isPrefiltered ? 'prefiltered' : 'skipped',
        action_klass: 'skipped',
        confidence: '',
        placement_adjustments: [],
        negative_keywords: [],
        is_core: false,
      }));
    });

    const s = raw.summary || {};
    return {
      mode: raw.mode || ViewMode.INTERACTIVE,
      parent_asin: raw.parent_asin || '',
      days: raw.days || 7,
      run_id: raw.run_id || '',
      snapshot_time: raw.snapshot_time || null,
      summary: {
        total: raw.total_campaigns ?? 0,
        eliminate: s.to_eliminate ?? 0,
        paused: s.to_paused ?? 0,
        adjust: s.to_adjust ?? 0,
        keep: s.to_keep ?? 0,
        create: (raw.new_campaigns || []).length,
        reactivate: s.to_reactivate ?? 0,
        prefiltered: allSkipped.filter(x => x.__prefiltered).length,
        lost: allSkipped.filter(x => !x.__prefiltered).length,
        confidence_high: s.confidence_high ?? 0,
        confidence_medium: s.confidence_medium ?? 0,
        confidence_low: s.confidence_low ?? 0,
        budget_impact: s.estimated_budget_impact ?? null,
        sanity_check_passed: raw.sanity_check_passed ?? null,
      },
      overview: raw.strategic_overview ? {
        facts: raw.strategic_overview.facts || {},
        assessment_text: raw.strategic_overview.assessment_text || '',
        direction_text: raw.strategic_overview.direction_text || '',
        generated_by: raw.strategic_overview.generated_by || 'ai',
      } : null,
      budget_summary: raw.budget_summary || null,
      synthesis: raw.synthesis || null,
      items,
      warnings: raw.warnings || [],
    };
  }

  // 完全空数据
  return {
    mode: ViewMode.READONLY,
    parent_asin: '',
    days: 7,
    run_id: '',
    snapshot_time: null,
    summary: { total: 0, eliminate: 0, paused: 0, adjust: 0, keep: 0, create: 0, reactivate: 0, prefiltered: 0, lost: 0,
               confidence_high: 0, confidence_medium: 0, confidence_low: 0,
               budget_impact: null, sanity_check_passed: null },
    overview: null,
    budget_summary: null,
    synthesis: null,
    items: [],
    warnings: ['无有效数据'],
  };
}
