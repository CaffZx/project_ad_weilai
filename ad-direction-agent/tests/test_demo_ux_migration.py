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
