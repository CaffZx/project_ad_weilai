# Campaign KB Granular Slicing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Campaign exact, broad, and new-campaign LLM prompts receive only their applicable KB rules, with KB taking precedence over Prompt wording.

**Architecture:** Extend the local KB loader from H2-only slices to hierarchical numbered H2/H3 slices, then derive the exact/broad action-matrix section from the supplied campaign direction. Keep all workflow, data, guardrail, persistence, and execution behavior unchanged. Prompt changes remove the low-Bid/low-Budget hard-elimination instruction and any citations to rules absent from the preset.

**Tech Stack:** Python, pytest, local Markdown KB files.

---

### Task 1: Define granular KB slicing regression behavior

**Files:**
- Modify: `ad-direction-agent/tests/test_kb_slicing.py`
- Modify: `ad-direction-agent/app/llm/kb_loader.py`

- [ ] **Step 1: Write failing tests**

Add tests proving that `KnowledgeBase.build()` can select a numbered H3 section such as `17:3.2`, preserves a selected H2 section such as `17:1`, and returns only the exact/broad KB21 sections `0` through `4`.

- [ ] **Step 2: Run the focused test file**

Run: `py -m pytest tests/test_kb_slicing.py -q`

Expected: FAIL because the loader only recognizes numbered H2 sections and cannot resolve `3.2`.

- [ ] **Step 3: Implement hierarchical numbered heading selection**

Extend the loader section index to recognize numbered `## N.` and `### N.N` headings. For a selected H2, return its full range. For a selected H3, return only that subsection. Keep the existing missing-section warning behavior.

- [ ] **Step 4: Re-run the focused test file**

Run: `py -m pytest tests/test_kb_slicing.py -q`

Expected: PASS for the new slicing cases.

### Task 2: Make exact and broad injection direction-specific

**Files:**
- Modify: `ad-direction-agent/app/llm/kb_loader.py`
- Modify: `ad-direction-agent/app/llm/reasoner.py`
- Modify: `ad-direction-agent/tests/test_kb_slicing.py`

- [ ] **Step 1: Write failing tests**

Add tests for exact and broad presets asserting the shared diagnosis sections remain, exactly one selected KB17 action subsection is present for a supplied direction, and KB21 contains neither its budget-reallocation nor restart-review sections.

- [ ] **Step 2: Run the focused test file**

Run: `py -m pytest tests/test_kb_slicing.py -q`

Expected: FAIL because current presets include all KB17 action matrices and all KB21 sections.

- [ ] **Step 3: Implement per-direction Campaign KB construction**

Add a loader method that builds `campaign_adjustment_exact` or `campaign_adjustment_broad` with KB17 `3.1` through `3.4` selected only for the current `ad_directions`. Use the approved exact/broad section lists and retain a safe fallback to all four action sections only when the direction is unavailable or not recognized.

- [ ] **Step 4: Pass direction into the Campaign system-prompt builder**

Change `_build_campaign_system_prompt()` to accept `ad_directions`, and have `recommend_campaign_batch()` pass `strategy_context["ad_directions"]`.

- [ ] **Step 5: Re-run the focused test file**

Run: `py -m pytest tests/test_kb_slicing.py -q`

Expected: PASS.

### Task 3: Align Prompt wording and new-campaign injection scope

**Files:**
- Modify: `ad-direction-agent/app/llm/kb_loader.py`
- Modify: `ad-direction-agent/app/llm/reasoner.py`
- Modify: `ad-direction-agent/tests/test_kb_slicing.py`

- [ ] **Step 1: Write failing tests**

Add assertions that the new-campaign preset excludes KB08 and KB28 sections `0` and `3`, and that exact/broad presets exclude KB21 sections after section `4`.

- [ ] **Step 2: Run the focused test file**

Run: `py -m pytest tests/test_kb_slicing.py -q`

Expected: FAIL against the existing preset definitions.

- [ ] **Step 3: Update presets and Prompt precedence language**

Use `new_campaign = ["16:1,6", "06", "02:1,2,3,4,5,6", "28:2"]`. Replace the low-Bid/low-Budget hard-elimination blocks with a KB-first precedence instruction. Remove references to non-injected KB18, KB19, and KB22 sections. Add the shared rule that absent input cannot be treated as a positive or negative fact.

- [ ] **Step 4: Re-run the focused test file**

Run: `py -m pytest tests/test_kb_slicing.py -q`

Expected: PASS.

### Task 4: Verify prompt construction contracts

**Files:**
- Modify: `ad-direction-agent/tests/test_kb_slicing.py`
- Modify: `ad-direction-agent/app/llm/reasoner.py`

- [ ] **Step 1: Write failing tests**

Add a test that builds exact and broad prompts and asserts they contain no former low-Bid/low-Budget hard-elimination language and no references to non-injected sections.

- [ ] **Step 2: Run the focused test file**

Run: `py -m pytest tests/test_kb_slicing.py -q`

Expected: FAIL until Prompt text is aligned.

- [ ] **Step 3: Make the smallest Prompt-only corrections needed to pass**

Do not change JSON output schemas, workflow orchestration, guardrails, models, or execution behavior.

- [ ] **Step 4: Run targeted and full KB tests**

Run: `py -m pytest tests/test_kb_slicing.py -q`

Expected: PASS with no failures.

