# AD Assistant Agent v3.23

亚马逊广告活动调整 Agent — 四层分析向导（战略 → 策略 → 诊断 → 执行）+ Campaign 活动分析 + 新增活动建议 + 淘汰复评 + ERP 自动写入。

**Campaign 分析采用三股子 Agent 并行架构**：精准流（EXACT）、广泛流（BROAD/PHRASE/AUTO）、新增流（候选词发现→新建活动），共享策略上下文和 posture_brief，`return_exceptions` 隔离故障。

## 快速开始

```bash
cd ad-direction-agent
pip install -r requirements.txt
cp .env.example .env          # 填入 DB / DeepSeek / ERP 连接信息
python start_server.py --port 8020 --no-reload
```

- 主看板：**http://localhost:8020/demo/ad-asisitant-agent.html**
- API 文档：**http://localhost:8020/docs**

数据源：纯 MCP StarRocks 网关（2026-06-30 已切除 Doris，失败返零值不回落）。`DATA_SOURCE` 可选 `mcp` / `db` / `mock` / `csv`（见 `.env.example`）。

## 系统架构

```
ad-direction-agent/
├── app/
│   ├── api/                   # FastAPI 路由
│   │   ├── campaign.py        # Campaign 分析/确认/执行/组合预算执行
│   │   ├── decision.py        # 决策批次管理（new-event / cancel-event / context / preset）
│   │   ├── strategy.py        # 战略层（产品阶段/经营模式/广告目的）
│   │   ├── tactics.py         # 策略层（关键词分类/广告位策略）
│   │   ├── execution.py       # 执行层（P3 推荐/执行确认）
│   │   ├── wizard.py          # 四层分析向导
│   │   ├── core_keyword.py    # 核心词管理 API
│   │   ├── config_mirror.py   # 配置保存镜像（state DB + ERP 双写）
│   │   └── long_term_config.py
│   ├── config/                # 配置
│   │   ├── settings.py        # 全局配置（LLM/MCP/Campaign 参数）
│   │   ├── layer_options.toml # 战略/策略/执行层可选项（含经营模式 radio 组）
│   │   └── thresholds.toml    # 出价/预算/ACOS 阈值
│   ├── core/                  # 核心引擎
│   │   ├── recommender.py     # 目标 ACOS / 预算推荐器
│   │   ├── acos_constraints.py # ACOS 约束硬计算
│   │   ├── scenario_analyzer.py
│   │   ├── validation_engine.py
│   │   ├── workflow_orchestrator.py
│   │   └── core_keyword_policy.py
│   ├── data/                  # 数据层
│   │   ├── mcp_adapter.py     # MCP 适配器（连接池/重试/退避）
│   │   ├── mcp_mapping.py     # MCP 工具注册/入参构造
│   │   ├── mcp_registry.py    # MCP 工具集中注册（v3.21）
│   │   ├── campaign_fetcher.py # Campaign 数据编排（1433 行）
│   │   ├── campaign_prefilter.py # 硬过滤（多词/淘汰池/冲突检测）
│   │   ├── new_keyword_fetcher.py # 多源候选词发现（v3.21）
│   │   ├── core_keyword_fetcher.py # 核心词数据编排
│   │   ├── decision_config_reader.py
│   │   └── starrocks_retry.py # StarRocks BE 重试
│   ├── llm/                   # LLM 层
│   │   ├── reasoner.py        # Prompt 构建 + 全部 LLM 调用（2295 行）
│   │   ├── kb_loader.py       # KB 切片加载（13 个 preset）
│   │   ├── client.py          # DeepSeek API 客户端 + KeyPool
│   │   └── purpose_adapter.py # 广告目的子模块适配
│   ├── models/                # Pydantic 数据模型
│   │   ├── campaign.py        # Campaign 全量模型（336 行）
│   │   └── layers.py          # 战略/策略/执行层模型 + OperatingMode 枚举
│   ├── persistence/           # 持久化
│   │   ├── mysql_state_manager.py # State DB 读写
│   │   ├── state_manager.py       # 缓存/DB 双后端抽象
│   │   ├── schema.sql             # State DB DDL
│   │   └── erp_writer/            # ERP 写入
│   │       ├── repository.py      # ERP 读写仓库
│   │       ├── mappers.py         # 模型映射/规范化
│   │       ├── advert_exec_mapper.py # 执行硬护栏/请求映射
│   │       └── text_utils.py      # 枚举中英文映射
│   ├── rules/                 # 规则引擎（5 条广告方向规则）
│   └── workflow/
│       ├── analysis_run_guard.py  # 分析运行闸门（防重复/取消态, v3.23）
│       └── steps/
│           ├── campaign.py              # ★编排引擎（2846 行）
│           ├── campaign_guardrails.py   # 护栏（12 条规则 P0-P11, 504 行）
│           ├── campaign_exact_transition.py # 精准升降级（v3.22）
│           ├── campaign_portfolio.py    # 组合分类/路由
│           ├── campaign_budget_summary.py    # 预算汇总
│           ├── campaign_budget_reallocation.py # 预算回算
│           ├── campaign_new.py          # 新增活动分析线
│           ├── advert_execution.py      # MCP 真实执行
│           ├── portfolio_execution.py   # 组合预算执行
│           ├── core_keyword.py          # 核心词语义判定
│           ├── strategy/tactics/diagnosis/execution/p3/wizard.py
├── tests/                    # 测试（42 个 .py 文件，覆盖 API/data/persistence/rules/workflow）
│   ├── api/                  # API 测试（取消事件/产品身份/配置镜像）
│   ├── data/                 # 数据层测试（prefilter/config reader/operating_mode）
│   ├── persistence/          # 持久化测试（灰度卡/复评池/operating_mode/身份写入）
│   └── workflow/             # 工作流测试（护栏/复评/缓存/新增词/组合匹配）
├── demo/                     # 前端
│   ├── ad-asisitant-agent.html  # 主看板（含第5 tab Campaign, 3853 行）
│   ├── access-guard.js       # 入口守卫（v3.22）
│   └── campaign-panel/       # Campaign 前端模块（ES module, 6 文件）
├── scripts/                  # 运维/调试脚本
│   ├── erp_db/               # ERP 数据库脚本（DDL/迁移/健康检查）
│   ├── state_db/             # State 数据库脚本
│   ├── apply_strategy_preset.py
│   └── batch_core_keyword.py 等批跑脚本
├── start_server.py           # 一键启动（端口 8020）
└── .env.example              # 环境变量模板
ad-purpose-agent/              # 策略层 AI 子模块（广告目的/词类）
docs/
├── knowledge_base/           # 知识库（00-32 号 KB + 执行规则 + ontology + 部署方案）
│   ├── 00-知识库总纲与切片覆盖矩阵.md   # KB 全景索引 + 权威 preset 定义（v3.4.8）
│   ├── 01-14,20,25,30.md    # 根级 KB
│   ├── ontology/              # 本体定义（7 个 YAML + 部署方案）
│   └── 执行规则/              # 15-19,21-24,28,29,31,32
├── AD-Agent Project Knowledge Graph/  # 项目知识图谱（16 篇）
├── Campaign分析引擎交接文档.md        # ★ 技术交接主文档（v3.28, 最全）
├── Campaign切除双轮投票方案.md（已删除，过时方案）
├── announcements/            # 前端版本公告
├── sql/                      # SQL 迁移脚本
└── superpowers/plans/        # 实施方案文档
```

## API 端点

### 四层分析向导

| 能力 | 端点 |
|------|------|
| 战略层（场景/目的/经营模式/方向） | `POST /strategy/analyze`、`GET /strategy/{asin}` |
| 策略层（关键词/广告位/出价） | `POST /tactics/analyze`、`GET /tactics/{asin}` |
| 诊断层（问题检测/优先级） | `POST /diagnosis` |
| 执行层（P3 推荐、执行确认） | `POST /execution/p3`、`POST /execution/submit` |

### Campaign 分析

| 能力 | 端点 |
|------|------|
| 分析现有活动 | `POST /campaign/analyze` |
| 实时操作台（分析→落库→回读快照） | `POST /campaign/viewmodel` |
| 读历史批次快照 | `GET /campaign/snapshot` |
| 确认调整（双路审核） | `POST /campaign/confirm` |
| 执行已确认调整 | `POST /campaign/execute` |
| 组合预算调整执行 | `POST /campaign/execute-portfolio-budget` |

### 决策批次

| 能力 | 端点 |
|------|------|
| 批次上下文（A/B/C 三态） | `GET /decision/context` |
| 新建分析事件 | `POST /decision/new-event` |
| 取消分析事件 | `POST /decision/cancel-event` |
| 前置配置快照 | `GET /decision/{id}/preset` |

### 辅助

| 能力 | 端点 |
|------|------|
| 核心词管理 | `POST /core-keyword/analyze`、`GET /core-keyword/status`、`/groups` |
| 长期配置 | `POST /long-term-config/save`、`GET /long-term-config/{asin}` |
| AI 对话 | `POST /chat` |
| 健康检查 | `GET /health` |

## 核心设计

### 经营模式（Operating Mode）

6 种经营模式 → 3 级广告权限（`layers.py`）：立即退出/控制清货/限时修复/稳定经营/积极推进/获取利润。权限推导：`IMMEDIATE_EXIT` → STOP / `CONTROLLED_CLEARANCE` → CLEARANCE_ONLY / 其余 → NORMAL。

### Campaign 分析流程

```
MCP 上下文 → 预过滤 → basic_info + product_report 并行
  → 组装 CampaignUnit[] → 核心词标签注入 → 组合预分类（4 组）
  → 拉取组合预算 → 三股子 Agent 并行（共享策略上下文/posture_brief）:
  │   ├─ 精准流子 Agent: EXACT 活动 → placement 预取 → 单轮 LLM 直判
  │   ├─ 广泛流子 Agent: BROAD/PHRASE/AUTO → search_term 预取 → 单轮 LLM 直判
  │   └─ 新增流子 Agent: 候选词发现→硬过滤→LLM 相关性判定→组装
  → action 归一化 → 护栏裁决 → 组合预算汇总/回算 → AI 汇总合成
```

### 护栏系统

`campaign_guardrails.py`：12 条规则（P0-P11），含 retry_instruction 回灌。P3 硬淘汰 > P1 样本保护。护栏拦截后 R3/R4 LLM 重试最多两轮。

### 淘汰多环节阈值（有意不同）

| 环节 | 判据 | 常量 |
|------|------|------|
| 预过滤 | bid ≤ 0.20 AND budget ≤ 1.00 | `LOW_BID_MAX` / `LOW_BUDGET_MAX` |
| 归组/强制修正 | bid ≤ 0.10 OR budget ≤ 1.00 | `LOW_BID_MIN` / `LOW_BUDGET_MAX` |

### 新增活动分析线

候选词发现（flow/own/竞品三源）→ 硬过滤（搜索量≥100）→ 长尾优先排序 → LLM 相关性判定 → 双轮取交集 → 代码补齐 bid/budget/placement。数据编排委托 `new_keyword_fetcher.py`。

### KB 知识库预设

13 个 preset 由 `kb_loader.py` 加载，**权威定义见 `00-知识库总纲与切片覆盖矩阵.md` §4.1**。大部分预设使用节级切片（如 `18:1,3`）。详见交接文档 §3.1。

## 配置要点

- 敏感值（`.env`）：DB 连接、DeepSeek API Key、ERP 账密
- 运行时 TOML（`app/config/`）：`thresholds.toml` / `layer_options.toml`
- `DATA_SOURCE` 数据源切换（默认 MCP）
- `ERP_AUTO_WRITE` 控制自动写入 ERP（默认关）
- `campaign_portfolio_fetch_enabled` 控制组合预算 MCP 拉取（默认开）
- `advert_mcp_enabled` + `advert_exec_dry_run` 控制 MCP 真实执行（默认关 + 空跑）

## 维护文档

| 文档 | 说明 |
|------|------|
| [docs/Campaign分析引擎交接文档.md](docs/Campaign分析引擎交接文档.md) | ★ Campaign 引擎技术交接（v3.28，最全） |
| [docs/knowledge_base/00-知识库总纲与切片覆盖矩阵.md](docs/knowledge_base/00-知识库总纲与切片覆盖矩阵.md) | KB 全景索引 + preset 权威定义（v3.4.8） |
| [docs/精准组合确定性升降级实施方案.md](docs/精准组合确定性升降级实施方案.md) | 精准组合升降级方案 |
| [docs/精准组合确定性升降级实施方案.md](docs/精准组合确定性升降级实施方案.md) | 精准组合升降级方案 |
| [docs/复盘记忆系统实现方案.md](docs/复盘记忆系统实现方案.md) | 复盘记忆系统设计 |
| [docs/MCP工具接入架构范式.md](docs/MCP工具接入架构范式.md) | MCP 工具接入代码范式 |
| [docs/AD-Agent Project Knowledge Graph/](docs/AD-Agent Project Knowledge Graph/) | 项目知识图谱（16 篇） |
