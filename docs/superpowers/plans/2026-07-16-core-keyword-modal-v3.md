# Core Keyword Management Modal V3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Tab5's core-keyword management dialog visually and structurally match the approved V3 prototype while preserving the existing API and state model.

**Architecture:** Keep the existing management endpoint and state methods. Replace only `_renderCoreKeywordModal()` markup and its `.camp-core-*` CSS so the fixed overlay has a header, content area, add control, table or empty state, and footer.

**Tech Stack:** Browser ES modules, vanilla DOM templates, CSS, pytest source-contract checks.

---

### Task 1: Lock the V3 dialog structure with a regression test

**Files:**

- Modify: `ad-direction-agent/tests/test_demo_ux_migration.py`

- [ ] **Step 1: Write the failing test**

```python
def test_core_keyword_management_modal_matches_v3_layout_contract():
    render = read(PANEL_RENDER)
    assert 'class="camp-modal-overlay camp-core-overlay"' in render
    assert 'class="camp-core-header"' in render
    assert 'class="camp-core-count"' in render
    assert 'class="camp-core-empty"' in render
    assert 'class="camp-core-footer"' in render
    assert '当前有效核心词' in render
    assert '暂无核心词分析记录' in render
```

- [ ] **Step 2: Run the test before implementation**

Run: `python -m pytest tests/test_demo_ux_migration.py::test_core_keyword_management_modal_matches_v3_layout_contract -q`

Expected: FAIL because the current template uses only a flat title and a raw empty table row.

- [ ] **Step 3: Commit the red test**

```powershell
git add ad-direction-agent/tests/test_demo_ux_migration.py
git commit -m "test: define core keyword modal v3 layout"
```

### Task 2: Render the V3 information hierarchy

**Files:**

- Modify: `ad-direction-agent/demo/campaign-panel/render.js:672-688`

- [ ] **Step 1: Replace the flat modal template**

Render `camp-core-header` with title and offline-analysis hint, `camp-core-count` with `当前有效核心词 N / 30`, the existing search/add control, table cells for keyword/type/reason/state/action, a centred `camp-core-empty` for an empty response, and `camp-core-footer` with the three-dot rule plus close action.

- [ ] **Step 2: Preserve existing interaction contracts**

Retain `camp-core-keyword-close`, `camp-core-keyword-add`, and `camp-core-keyword-state` actions plus their current data attributes. The add control remains visible with an empty response; do not add free-text submission.

- [ ] **Step 3: Run the focused test**

Run: `python -m pytest tests/test_demo_ux_migration.py::test_core_keyword_management_modal_matches_v3_layout_contract -q`

Expected: PASS.

- [ ] **Step 4: Commit the render change**

```powershell
git add ad-direction-agent/demo/campaign-panel/render.js ad-direction-agent/tests/test_demo_ux_migration.py
git commit -m "feat: render core keyword modal v3 layout"
```

### Task 3: Apply the V3 visual system

**Files:**

- Modify: `ad-direction-agent/demo/campaign-panel/panel.css:525-539`

- [ ] **Step 1: Add failing visual-contract assertions**

Assert that `panel.css` contains `.camp-core-overlay`, `.camp-core-header`, `.camp-core-count`, `.camp-core-empty`, `.camp-core-footer`, `.camp-core-type.semantic`, `.camp-core-type.data`, and `.camp-core-type.manual`.

- [ ] **Step 2: Run the test before CSS implementation**

Run: `python -m pytest tests/test_demo_ux_migration.py::test_core_keyword_management_modal_matches_v3_layout_contract -q`

Expected: FAIL because V3-specific selectors do not yet exist.

- [ ] **Step 3: Implement the approved V3 CSS**

Use a 1020px maximum-width, rounded, shadowed dialog. Use a 18px title, muted hint, light-blue count badge, light table header, 13px rows, semantic/data/manual tags, purple/green/gray/red state colors, a padded centred empty panel, and a subtle footer with a secondary close button.

- [ ] **Step 4: Run source checks**

Run: `node --check demo/campaign-panel/render.js; python -m pytest tests/test_demo_ux_migration.py -q`

Expected: JavaScript syntax succeeds and all demo UX tests pass.

- [ ] **Step 5: Commit the styling**

```powershell
git add ad-direction-agent/demo/campaign-panel/panel.css ad-direction-agent/tests/test_demo_ux_migration.py
git commit -m "feat: style core keyword modal as v3"
```

### Task 4: Verify with real and empty local responses

**Files:**

- Verify only: `ad-direction-agent/demo/ad-asisitant-agent.html`

- [ ] **Step 1: Verify the real-record product**

Open `http://localhost:8010/demo/ad-asisitant-agent.html?parentAsin=B0B7S3PWWB&parentSellerSku=FS02721-3pcs&shopId=1622&siteCode=Amazon_US` and confirm the modal displays count, real rows, type pills, evidence, states, and the add search control.

- [ ] **Step 2: Verify the no-record product**

Open the supplied current product and confirm the dialog remains centred, displays `暂无核心词分析记录`, and keeps the add control.

- [ ] **Step 3: Run final regression**

Run: `python -m pytest tests/test_demo_ux_migration.py tests/workflow/test_core_keyword.py tests/workflow/test_campaign_guardrails.py -q; git diff --check`

Expected: all focused tests pass and `git diff --check` has no output.
