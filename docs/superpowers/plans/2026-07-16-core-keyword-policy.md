# 核心词人工状态管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 建立独立人工策略表、管理接口和面板，并使锁定/否决词在离线任务的性能、排名 MCP 与语义 LLM 前被预过滤。

**Architecture:** 最新 DONE 任务的 label.is_core=1 是唯一 AI 基础集合。策略表只覆盖 LOCKED、DISABLED、VETOED；离线编排读取 LOCKED/VETOED 规范词并在 fetcher Step3 后、Step4 前过滤。

**Tech Stack:** Python 3, FastAPI, PyMySQL, pytest, 原生 ES modules, MySQL 8.

---

### Task 1: 策略表、归一化和状态解析

**Files:**
- Create: ad-direction-agent/scripts/erp_db/migrate_core_keyword_policy.sql
- Modify: ad-direction-agent/app/persistence/erp_writer/repository.py
- Modify: ad-direction-agent/tests/workflow/test_core_keyword.py

- [ ] **Step 1: Write the failing test**

    def test_resolve_effective_core_keywords_applies_policy_with_normalized_matching():
        result = resolve_effective_core_keywords(
            {"AI Dress", "disabled kw"},
            {" locked kw ": "LOCKED", "DISABLED KW": "DISABLED", "ai dress": "VETOED"},
        )
        assert result == {"locked kw"}

- [ ] **Step 2: Verify it fails**

Run: python -m pytest tests/workflow/test_core_keyword.py::test_resolve_effective_core_keywords_applies_policy_with_normalized_matching -q

Expected: import failure for the missing resolver.

- [ ] **Step 3: Implement minimal resolver and DDL**

    def normalize_core_keyword(value: str) -> str:
        return " ".join(str(value or "").strip().split()).casefold()

    def resolve_effective_core_keywords(ai_keywords, policy_by_norm):
        effective = {normalize_core_keyword(k) for k in ai_keywords}
        for norm, state in policy_by_norm.items():
            if state == "LOCKED":
                effective.add(norm)
            elif state in {"DISABLED", "VETOED"}:
                effective.discard(norm)
        return effective

DDL uses the product triple plus keyword_norm as the unique key and stores state, task version, operator and timestamps.

- [ ] **Step 4: Verify green**

Run: python -m pytest tests/workflow/test_core_keyword.py::test_resolve_effective_core_keywords_applies_policy_with_normalized_matching -q

Expected: PASS.

### Task 2: Offline prefilter and task-commit state rotation

**Files:**
- Modify: ad-direction-agent/app/data/core_keyword_fetcher.py
- Modify: ad-direction-agent/app/workflow/steps/core_keyword.py
- Modify: ad-direction-agent/app/persistence/erp_writer/repository.py
- Modify: ad-direction-agent/app/api/core_keyword.py
- Modify: ad-direction-agent/tests/workflow/test_core_keyword.py

- [ ] **Step 1: Write the failing prefilter tests**

    @pytest.mark.asyncio
    async def test_fetcher_prefilters_locked_and_vetoed_before_perf_and_rank():
        result = await fetcher.fetch(
            "B0TEST", "SKU-1", 1622,
            excluded_keyword_norms={"locked kw", "vetoed kw"},
        )
        assert [item.keyword_text for item in result.campaigns] == ["enabled kw"]
        ranking_keywords = [
            args["keyword"] for name, args in fake_starrocks.calls
            if name == "keyword_child_asins"
        ]
        assert ranking_keywords == ["enabled kw"]

Also test that run_core_keyword_analysis obtains LOCKED/VETOED from Repository and no excluded term appears in the LLM batch.

- [ ] **Step 2: Verify red**

Run: python -m pytest tests/workflow/test_core_keyword.py -k "prefilter or excluded" -q

Expected: fetch does not accept excluded_keyword_norms or the assertions fail.

- [ ] **Step 3: Implement minimum behavior**

Add excluded_keyword_norms to CoreKeywordFetcher.fetch. After Step3 returns candidates and before Step4 performance calls, remove normalized terms in that set. Run_core_keyword_analysis reads exclusions before fetch. write_core_keyword_task synchronizes policies in the same transaction: preserve LOCKED/VETOED, remove old ENABLED/DISABLED, then upsert current is_core=1 labels as ENABLED unless protected.

- [ ] **Step 4: Verify green**

Run: python -m pytest tests/workflow/test_core_keyword.py -k "prefilter or excluded" -q

Expected: PASS.

### Task 3: Management queries, policy writes and Campaign effective core set

**Files:**
- Modify: ad-direction-agent/app/persistence/erp_writer/repository.py
- Modify: ad-direction-agent/app/api/core_keyword.py
- Modify: ad-direction-agent/app/workflow/steps/campaign.py
- Modify: ad-direction-agent/tests/workflow/test_core_keyword.py

- [ ] **Step 1: Write failing API/repository tests**

    def test_fetch_core_keyword_set_uses_latest_done_ai_base_then_policy(monkeypatch):
        result = ErpDualWriterRepository.fetch_core_keyword_set("B0TEST", "SKU-1", 1622)
        assert result == {"locked kw", "current core"}

    @pytest.mark.asyncio
    async def test_management_api_returns_only_current_task_label_pool(monkeypatch):
        out = await core_keyword_api.get_core_keyword_management("B0TEST", "SKU-1", 1622)
        assert out["word_pool"] == ["current non-core", "current core"]

- [ ] **Step 2: Verify red**

Run: python -m pytest tests/workflow/test_core_keyword.py -k "management or policy" -q

Expected: missing endpoint/repository behavior or incorrect result.

- [ ] **Step 3: Implement minimum interfaces**

GET management uses only the latest DONE task: core labels form AI rows and all labels form the selectable pool. It appends policy exceptions that lack a current label. POST policy performs task version validation, admits new words only from the current pool, and permits existing LOCKED/VETOED exceptions to change state. fetch_core_keyword_set resolves AI base plus policies. Campaign normalizes both the set and CampaignUnit keyword before membership checks.

- [ ] **Step 4: Verify green**

Run: python -m pytest tests/workflow/test_core_keyword.py -k "management or policy or fetch_core_keyword_set" -q

Expected: PASS.

### Task 4: Panel entry and single-list modal

**Files:**
- Modify: ad-direction-agent/demo/campaign-panel/panel.js
- Modify: ad-direction-agent/demo/campaign-panel/render.js
- Modify: ad-direction-agent/demo/campaign-panel/events.js
- Modify: ad-direction-agent/demo/campaign-panel/state.js
- Modify: ad-direction-agent/demo/campaign-panel/panel.css

- [ ] **Step 1: Add a minimal state-level red test/manual harness**

Construct executable and non-latest state. Assert openCoreKeywordManagement does not call the API for non-latest state and requests GET management for latest state.

- [ ] **Step 2: Verify red**

Run: node --check demo/campaign-panel/state.js

Expected: static syntax passes, but the new harness cannot find the entry method.

- [ ] **Step 3: Implement the button and modal**

Put 核心词管理 left of 同意所选. Render it only when state.executable and vm.is_latest. Use one list with columns 核心词、核心类型、核心原因、状态、操作. Color LOCKED purple, ENABLED green, DISABLED grey, VETOED red. Add a searchable select-only pool; its default POST state is LOCKED.

- [ ] **Step 4: Verify syntax**

Run: node --check demo/campaign-panel/panel.js; node --check demo/campaign-panel/render.js; node --check demo/campaign-panel/events.js; node --check demo/campaign-panel/state.js

Expected: all commands exit 0.

### Task 5: Regression and documentation

**Files:**
- Modify: docs/superpowers/specs/2026-07-16-core-keyword-management-design.md
- Create: docs/superpowers/plans/2026-07-16-core-keyword-policy.md

- [ ] **Step 1: Mark the spec implemented and retain the Step3-before-Step4 prefilter boundary.**

- [ ] **Step 2: Run verification**

Run: python -m pytest tests/workflow/test_core_keyword.py -q; node --check demo/campaign-panel/panel.js; node --check demo/campaign-panel/render.js; node --check demo/campaign-panel/events.js; node --check demo/campaign-panel/state.js; git diff --check

Expected: all new tests pass. Report separately the known baseline fixture failures in Step3 if they remain.

- [ ] **Step 3: Commit only feature files**

    git add ad-direction-agent docs/superpowers
    git commit -m "feat: add core keyword policy management"

