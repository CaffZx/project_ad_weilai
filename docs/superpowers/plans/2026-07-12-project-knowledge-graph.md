# Project Knowledge Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a durable, agent-readable project knowledge graph documentation set for AD-Agent itself.

**Architecture:** Documentation is created in a dedicated folder under `docs/AD-Agent Project Knowledge Graph`. Each document explains one stable subsystem, names the code truth sources, records current data/control flow, and ends with an update checklist. Existing handoff documents are source material, not the final structure.

**Tech Stack:** Markdown, FastAPI/Python source references, MySQL schemas, Redis cache notes, browser frontend modules.

---

### Task 1: Create Navigation And Documentation Norms

**Files:**
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/README.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/00-项目知识图谱总览.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/15-项目知识图谱维护规则.md`

- [ ] **Step 1: Write the reader map**

Add a README that lists every document, preferred reading orders for humans and coding agents, and the rule that this folder documents project architecture rather than business knowledge-base content.

- [ ] **Step 2: Write the overview**

Add a subsystem map covering backend API, workflow engine, MCP data layer, persistence, frontend, batch jobs, and project-internal Codex review.

- [ ] **Step 3: Write maintenance rules**

Add the required document template, source-of-truth policy, stale-risk checklist, and review commands.

### Task 2: Document Core Data And Persistence

**Files:**
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/01-核心数据对象与领域模型.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/02-数据库表结构说明-ERP库与State库.md`

- [ ] **Step 1: Document domain objects**

Describe `ASINData`, four-layer strategy/tactics/diagnosis/execution models, `CampaignData`, `CampaignAnalysisResult`, and `decision_id`/snapshot semantics with code truth sources.

- [ ] **Step 2: Document ERP and state DB split**

Describe ERP as business display/execution persistence and state DB as agent-owned long-term state, listing project-touched tables and responsibilities.

### Task 3: Document MCP And Runtime Infrastructure

**Files:**
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/03-MCP工具原始Schema说明.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/04-广告Agent-MCP实际调用链路说明.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/05-网关连接池信号量与超时设置.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/06-缓存机制说明.md`

- [ ] **Step 1: Document project-side MCP schemas**

List active data MCP tools and advert execution MCP tools, including project-side argument builders, known return consumption, and source files.

- [ ] **Step 2: Document actual call chains**

Extract the durable parts of `docs/MCP工具调用链路文档.md` and correct stale tool names against `app/data/mcp_mapping.py`, `app/data/mcp_adapter.py`, and `app/data/advert_mcp_client.py`.

- [ ] **Step 3: Document runtime limits**

Describe MCP/LLM/Campaign/Advert MCP timeouts, connection pools, semaphores, and fail-open behavior from `settings.py` and data clients.

- [ ] **Step 4: Document cache behavior**

Describe Redis and LRU cache keys, TTLs, meta_filter suffixing, fallback behavior, and invalidation points.

### Task 4: Document Workflows And UI

**Files:**
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/07-广告策略推荐工作流引擎架构.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/08-Campaign分析引擎架构说明.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/09-知识库切片加载说明.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/10-定时分析机制说明.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/11-前端设计说明.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/12-广告执行链路与安全开关.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/13-Codex复核组件说明.md`
- Create: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/14-测试与验证地图.md`

- [ ] **Step 1: Document the pre-Campaign strategy workflow**

Describe ASIN data consumption, strategy/tactics/diagnosis/execution layers, P3 recommendation, confirmation, and ERP writeback.

- [ ] **Step 2: Document Campaign architecture**

Describe discovery, effect data, LLM decision, guardrails, budget reallocation, new campaign creation, restart review, and ERP/viewmodel outputs.

- [ ] **Step 3: Document KB slicing**

Describe `kb_loader.py` presets, section syntax, enum translation, and missing-section behavior.

- [ ] **Step 4: Document scheduled analysis**

Describe `batch_via_api.py` inputs, API target, retry/timeout/status model, and exit code policy.

- [ ] **Step 5: Document frontend design**

Describe the single HTML shell, Campaign panel modules, state ownership, API interactions, and UX constraints.

- [ ] **Step 6: Document execution and Codex review**

Describe advert MCP execution dry-run/enabled switches, pending mapping, portfolio resolution, and current Codex review component boundary.

- [ ] **Step 7: Document verification map**

Map relevant tests to subsystems and name recommended targeted verification commands.

### Task 5: Validate Documentation Set

**Files:**
- Inspect: `AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph/*.md`

- [ ] **Step 1: List generated files**

Run: `Get-ChildItem "AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph" | Select-Object Name,Length`

- [ ] **Step 2: Scan for placeholders**

Run: `rg -n "TODO|TBD|待补|最后更新|工作日志" "AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph"`

- [ ] **Step 3: Scan for accidental business KB conflation**

Run: `rg -n "知识库" "AD_assistant_agent-v3.2/docs/AD-Agent Project Knowledge Graph"`

- [ ] **Step 4: Review git diff**

Run: `git -C AD_assistant_agent-v3.2 diff -- docs/AD-Agent\ Project\ Knowledge\ Graph docs/superpowers/plans/2026-07-12-project-knowledge-graph.md`
