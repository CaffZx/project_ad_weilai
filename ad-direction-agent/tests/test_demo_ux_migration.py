from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo" / "ad-asisitant-agent.html"
PANEL_CSS = ROOT / "demo" / "campaign-panel" / "panel.css"
PANEL_EVENTS = ROOT / "demo" / "campaign-panel" / "events.js"
PANEL_RENDER = ROOT / "demo" / "campaign-panel" / "render.js"
PANEL_STATE = ROOT / "demo" / "campaign-panel" / "state.js"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_demo_callapi_owns_fast_fallback_for_upper_layers():
    html = read(DEMO)

    assert "FAST_FALLBACK_PATHS" in html
    assert "makeFastFallback" in html
    assert "fastFallbackTimeout" in html
    assert "r.status === 502" in html
    assert "mcp_gateway_unavailable" in html


def test_demo_cancel_event_remounts_latest_campaign_snapshot():
    html = read(DEMO)

    assert "onCancelEventClick" in html
    assert "window._mountCampaignBatch({ decision_id: latestId" in html
    assert "showToast('已放弃本次分析，已恢复最近一次历史数据'" in html


def test_cancel_event_immediately_restores_local_b_state_without_waiting_for_session_exit():
    html = read(DEMO)
    cancel = html[
        html.index("async function onCancelEventClick"):
        html.index("async function _restoreAfterCancel")
    ]

    assert "in_progress: null" in cancel
    assert "renderBatchBar('B', _decisionContext)" in cancel
    assert "await _restoreAfterCancel({ refresh: false })" in cancel
    assert "/decision/session-status" not in cancel


def test_cancelled_realtime_run_cannot_render_a_late_completion():
    html = read(DEMO)
    realtime_start = html.index("window._mountCampaignRealtime")
    waiting_start = html.index("window._mountCampaignWaiting")
    realtime = html[realtime_start:waiting_start]
    complete_start = realtime.index("onComplete: async (vm) =>")
    complete = realtime[complete_start:]

    assert "window._campaignPanelAPI.unmount()" in html[html.index("async function onCancelEventClick"):html.index("async function _restoreAfterCancel")]
    assert "window._cancelledCampaignRunIds.has(run_id)" in complete
    assert "window._cancelledCampaignRunIds.has(run_id)" in realtime[realtime.index("catch (e)"):]


def test_campaign_panel_has_native_density_and_card_selection():
    css = read(PANEL_CSS)
    events = read(PANEL_EVENTS)
    render = read(PANEL_RENDER)

    assert "camp-density-toggle" in css
    assert "camp-density-compact" in css
    assert "camp-adjustment-card.selected" in css
    assert "camp-toggle-density" in events
    assert "state.toggleDensity" in events
    assert "closest('.camp-adjustment-card')" in events
    assert "selected" in render


def test_approve_confirm_details_are_generated_from_state_items():
    state = read(PANEL_STATE)
    render = read(PANEL_RENDER)

    assert "_selectedItems" in state
    assert "state._confirm.items" in render
    assert "确认执行以下调整" in render
    assert "campaign_name" in render
    assert "placement_adjustments" in render


def test_core_keyword_management_never_silently_ignores_a_click():
    state = read(PANEL_STATE)

    assert "if (!state.executable) { _toast('当前不是最新可执行批次，无法管理核心词'); return; }" in state
    assert "if (!state._coreKeywordIdentity) { _toast('产品身份尚未加载，无法读取核心词'); return; }" in state


def test_core_keyword_management_modal_uses_visible_overlay_and_empty_state():
    render = read(PANEL_RENDER)

    assert '<div class="camp-modal-overlay camp-core-overlay"><section class="camp-modal camp-core-modal"' in render
    assert "暂无核心词分析记录" in render
    assert 'data-action="camp-core-keyword-add"' in render


def test_core_keyword_management_modal_matches_v3_layout_contract():
    css = read(PANEL_CSS)
    render = read(PANEL_RENDER)

    assert 'class="camp-modal-overlay camp-core-overlay"' in render
    assert 'class="camp-core-header"' in render
    assert 'class="camp-core-count"' in render
    assert 'class="camp-core-empty"' in render
    assert 'class="camp-core-footer"' in render
    assert ".camp-core-overlay" in css
    assert ".camp-core-type.semantic" in css
    assert ".camp-core-type.data" in css
    assert ".camp-core-type.manual" in css


def test_core_keyword_state_changes_have_frontend_inflight_guard():
    css = read(PANEL_CSS)
    render = read(PANEL_RENDER)
    state = read(PANEL_STATE)

    assert "state._coreKeywordPending = state._coreKeywordPending || new Set();" in state
    assert "if (state._coreKeywordPending.has(key)) return;" in state
    assert "state._coreKeywordPending.delete(key);" in state
    assert "const minPendingMs = 800;" in state
    assert "await new Promise(resolve => setTimeout(resolve, waitMs));" in state
    assert "camp-core-menu is-pending" in render
    assert "camp-core-add-button" in render and "disabled" in render
    assert ".camp-core-menu.is-pending" in css


def test_core_keyword_modal_uses_search_placeholder_without_redundant_tips():
    css = read(PANEL_CSS)
    render = read(PANEL_RENDER)

    assert 'placeholder="搜索本轮离线任务词池（选择后默认锁定）"' in render
    assert "<span>从词池中选择后默认锁定</span>" not in render
    assert "竖三点始终显示除当前状态外的三种状态" not in render
    assert "justify-content:flex-end" in css


def test_core_keyword_evidence_formatter_hides_empty_ai_evidence_arrays():
    render = read(PANEL_RENDER)

    assert "if (Array.isArray(value) && value.length === 0) return '';" in render
    assert "if (parsed.length === 0) return '';" in render


def test_portfolio_budget_keeps_arrow_when_current_budget_is_unavailable():
    render = read(PANEL_RENDER)

    assert "const curStr = cur != null ? '$' + Number(cur).toFixed(0) : '—';" in render
    assert "const propStr = prop != null ? '$' + Number(prop).toFixed(0) : '—';" in render
    assert "amt = curStr + ' → ' + propStr;" in render


def test_demo_native_analyzing_overlay_and_request_hints():
    html = read(DEMO)

    assert "campaignAnalysisOverlay" in html
    assert "showCampaignAnalysisOverlay" in html
    assert "hideCampaignAnalysisOverlay" in html
    assert "AI 分析中，请勿刷新" in html
    assert "onCancelEventClick({ skipConfirm: true })" in html
    assert "showRequestHint" in html
    assert "hideRequestHint" in html
    assert "SLOW_HINT_PATHS" in html


def test_demo_native_unified_strategy_save_and_preserves_manual_inputs():
    html = read(DEMO)

    assert "confirmTactics()" in html
    assert "saveP3AcosLeft()" in html
    assert "saveP3BudgetLeft()" in html
    assert "saveDirectionsLeft()" in html
    assert "_manualTouched" in html
    assert "preserveManualInputs" in html
    assert "restoreManualInputs" in html
    assert "btnStrategy" in html
    assert "保存配置" in html


def test_one_click_config_loads_downstream_without_tactics_confirmation():
    html = read(DEMO)
    start = html.index("async function loadTactics()")
    end = html.index("async function confirmTactics()", start)
    load_tactics = html[start:end]

    assert "await enableTabsForReturnVisit();" in load_tactics
    assert "if (returnVisit)" not in load_tactics
    assert 'id="btnTactics" class="btn btn-primary btn-sm hidden"' in load_tactics
    load_all_start = html.index("async function loadAll(")
    load_all_end = html.index("// ═══════════════ 战略层", load_all_start)
    assert "if (!tact.returnVisit)" not in html[load_all_start:load_all_end]


def test_one_click_config_latest_read_uses_identity_query_and_only_runs_in_editable_path():
    html = read(DEMO)
    start = html.index("async function loadAll(")
    end = html.index("// ═══════════════ 战略层", start)
    load_all = html[start:end]

    assert "shouldUseSnapshotMode()" in load_all
    assert "shop_id=${encodeURIComponent(_erpParams.shopId)}" in load_all
    assert "parent_seller_sku=${encodeURIComponent(_erpParams.parentSellerSku)}" in load_all
    assert "applyConfigSnapshotToEditable(snap)" in load_all


def test_one_click_save_bar_is_explicitly_gated_to_c_state():
    html = read(DEMO)
    start = html.index("function _syncSaveAllBar()")
    end = html.index("// MutationObserver", start)
    sync = html[start:end]

    assert "_decisionContext" in sync
    assert ".in_progress" in sync
    assert "!shouldUseSnapshotMode()" in sync


def test_one_click_save_uses_canonical_erp_page_identity_names():
    html = read(DEMO)
    start = html.index("function _collectSaveAllBody()")
    end = html.index("// 统一回读", start)
    collect = html[start:end]

    assert "_erpParams?.shopId" in collect
    assert "_erpParams?.parentSellerSku" in collect
    assert "_erpParams?.shopAccount" in collect
    assert "_erpParams?.siteCode" in collect
    assert "_erpParams?.shop_id" not in collect


def test_one_click_snapshot_resets_strategy_dirty_baseline_and_can_render_empty():
    html = read(DEMO)
    start = html.index("function applyConfigSnapshotToEditable(snap)")
    end = html.index("function _applyPersistedMulti", start)
    apply_snapshot = html[start:end]
    strategy_start = html.index("function applyPersistedStrategySelection(config)")
    strategy_end = html.index("function refreshStrategyContext", strategy_start)
    strategy = html[strategy_start:strategy_end]

    assert "_setSavedStrategyBaseline(snap)" in apply_snapshot
    assert "text.textContent = '请选择'" in strategy
    assert "option.classList.remove('selected')" in strategy


def test_one_click_feedback_keeps_ai_values_before_snapshot_replaces_p3_data():
    html = read(DEMO)
    save_start = html.index("async function saveAllConfig()")
    save_end = html.index("// 一键保存只属于 C 态", save_start)
    save = html[save_start:save_end]
    side_start = html.index("function applySaveAllSideEffects")
    side_end = html.index("async function saveAllConfig()", side_start)
    side = html[side_start:side_end]

    assert save.index("const aiSnapshot") < save.index("applyConfigSnapshotToEditable(snap)")
    assert "applySaveAllSideEffects(snap, body, aiSnapshot)" in save
    assert "aiSnapshot.target_acos" in side
    assert "aiSnapshot.daily_budget" in side


def test_one_click_save_does_not_wait_for_insight_panel_refresh():
    html = read(DEMO)
    start = html.index("function applySaveAllSideEffects")
    end = html.index("async function saveAllConfig()", start)
    side = html[start:end]
    save = html[html.index("async function saveAllConfig()"):]

    assert "await loadMainInsightPanels()" not in side
    assert "_ensureInsightPanelsLoaded()" in side
    assert "await applySaveAllSideEffects" not in save


def test_return_visit_and_save_share_one_insight_panel_load_promise():
    html = read(DEMO)
    helper_start = html.index("function _ensureInsightPanelsLoaded")
    helper_end = html.index("function applySaveAllSideEffects", helper_start)
    helper = html[helper_start:helper_end]
    return_start = html.index("async function enableTabsForReturnVisit()")
    return_end = html.index("// ═══════════════ 诊断层", return_start)
    return_visit = html[return_start:return_end]
    side_start = html.index("function applySaveAllSideEffects")
    side_end = html.index("async function saveAllConfig()", side_start)
    side = html[side_start:side_end]

    assert "if (!_insightPanelsLoadPromise)" in helper
    assert "return _insightPanelsLoadPromise" in helper
    # 2026-08-05:回访路径改后台加载,不 await 面板(否则配置表回读被 LLM 链阻塞)
    assert "await _ensureInsightPanelsLoaded()" not in return_visit
    assert "_ensureInsightPanelsLoaded()" in return_visit
    assert "_ensureInsightPanelsLoaded()" in side


def test_one_click_snapshot_trusts_backend_per_field_manual_override_flags():
    html = read(DEMO)
    start = html.index("function applyConfigSnapshotToEditable(snap)")
    end = html.index("function _applyPersistedMulti", start)
    apply_snapshot = html[start:end]

    assert "window._p3data = snap.p3" in apply_snapshot
    assert "manual_override = (snap.target_acos != null)" not in apply_snapshot
    assert "manual_override = (snap.daily_budget != null)" not in apply_snapshot


def test_editable_readback_owns_inputs_no_gray_preview_overwrite():
    """2026-08-05:进入即回读,灰值预览已切除,后续加载不得覆盖回读值。"""
    html = read(DEMO)
    # 灰值预览函数已整体切除
    assert "function autoFillLeftInputs" not in html
    assert "function _setP3SavedFlags" not in html
    # latest 回读存 snap,供后续防覆盖
    latest_idx = html.index("const latestPath")
    latest_block = html[latest_idx:latest_idx + 600]
    assert "_latestConfigSnap = snap" in latest_block
    # 回访路径不 await 面板(否则 latest 被 LLM 链阻塞)
    return_start = html.index("async function enableTabsForReturnVisit()")
    return_visit = html[return_start:return_start + 700]
    assert "await _ensureInsightPanelsLoaded()" not in return_visit
    # loadExecution 方向勾选按配置表真源重做,不再读 state selected_directions
    exec_start = html.index("async function loadExecution()")
    exec_block = html[exec_start:exec_start + 3000]
    assert "_latestConfigSnap.directions.includes(cb.value)" in exec_block
    assert "const savedDirs = data && data.selected_directions" not in exec_block


def test_demo_native_error_retry_button():
    html = read(DEMO)

    assert "attachGlobalErrorRetry" in html
    assert "errRetryBtn" in html
    assert "刷新重试" in html
    assert "loadAll(true)" in html


def test_remaining_patch_visual_modules_are_native():
    html = read(DEMO)
    css = read(PANEL_CSS)
    events = read(PANEL_EVENTS)

    assert "sidebarCollapseBtn" in html
    assert "toggleSidebarCollapsed" in html
    assert "sidebar-collapsed" in html
    assert "btn-loading-spinner" in html
    assert "setButtonLoading" in html
    assert "codex-btn-pulse" in html
    assert "filter-flash" in html
    assert "batchFlash" in html
    assert "生成调整建议(AI 分析)" in html
    assert "button[data-action=\"camp-batch-approve\"]" in css
    assert "button[data-action=\"camp-batch-reject\"]" in css
    assert "camp-portfolio-pill" in css
    assert "camp-meta-chip" in css
    assert "camp-tab5-space" in css
    assert "camp-list" in css
    assert "camp-filter-flash" in css
    assert "camp-btn-pulse" in css
    assert "camp-batch-flash" in css
    assert "camp-toggle-detail" in events
