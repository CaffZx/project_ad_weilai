# AD Assistant Agent v3.2

亚马逊广告方向决策 Agent — 四层分析向导（战略 → 策略 → 诊断 → 执行）+ Campaign 活动分析 + 新增活动建议 + ERP 自动写入。

## 快速开始

```bash
cd ad-direction-agent
pip install -r requirements.txt
cp .env.example .env          # 填入 DB / DeepSeek / ERP
python start_server.py --port 8010 --no-reload
```

- 主看板：**http://localhost:8010/demo/ad-asisitant-agent.html**
- Campaign 调试页：**http://localhost:8010/demo/campaign_test.html**

数据源：`DATA_SOURCE=mcp`（主力）/ `db`（回落）/ `mock` / `csv`（见 `.env.example`）。

## 系统架构

```
ad-direction-agent/
├── app/
│   ├── api/              # FastAPI 路由（strategy / tactics / diagnosis / execution / campaign / decision / wizard / chat）
│   ├── config/           # settings.py + TOML 配置（layer_options / thresholds / tags）
│   ├── core/             # 核心引擎（recommender / scenario_analyzer / validation_engine / decision_package / orchestrator）
│   ├── data/             # 数据层（mcp_adapter / db_adapter / campaign_fetcher / phased_fetcher / mcp_mapping）
│   ├── llm/              # LLM 层（reasoner / kb_loader / purpose_adapter / client）
│   ├── models/           # Pydantic 数据模型（asin_data / campaign / layers）
│   ├── persistence/      # 持久化（mysql_state_manager / erp_writer）
│   ├── rules/            # 规则引擎（optimize_acos / expand_keywords / balance_maintain 等 12 条规则）
│   ├── skills/           # MCP Skill 配置
│   └── workflow/steps/   # 分析步骤实现（strategy / tactics / diagnosis / execution / campaign / campaign_new / p3 / wizard）
├── tests/                # 测试（data / persistence / rules / skills / workflow）
├── demo/                 # 前端看板（ad-asisitant-agent.html / campaign_test.html）
├── scripts/              # 运维脚本
├── logs/                 # 本地日志
└── start_server.py       # 一键启动
ad-purpose-agent/          # 策略层 AI 子模块（广告目的/词类）
docs/
├── knowledge_base/       # 知识库（01-23 号 KB，含执行规则）
├── announcements/        # 前端版本公告（v2.2-v3.1）
├── sql/                  # 数仓索引建议
├── Campaign分析引擎交接文档.md  # 技术交接主文档
└── 前后端接口文档.md
```

## API（前缀 `/api/v1/agent/ad-direction`）

### 四层分析向导

| 能力 | 端点 |
|------|------|
| 战略层（场景/目的/方向） | `POST /strategy/analyze`、`GET /strategy/{asin}` |
| 策略层（关键词/广告位/出价） | `POST /tactics/analyze`、`GET /tactics/{asin}` |
| 诊断层（问题检测/优先级） | `POST /diagnosis` |
| 执行层（P3 推荐、执行确认） | `POST /execution/p3`、`POST /execution/submit` |

### Campaign 分析

| 能力 | 端点 |
|------|------|
| 分析现有活动 | `POST /campaign/analyze`（可选 `write_erp: true`） |
| 新增活动建议 | `POST /campaign/analyze`（含 `new_campaign` 字段） |
| 执行层直接操作 | `POST /execution/direct` |

### 辅助

| 能力 | 端点 |
|------|------|
| 决策事件管理 | `POST /decision/new-event`、`GET /decision/context` |
| 长期配置 | `POST /long-term-config/save`、`GET /long-term-config/{asin}` |
| 公告 | `GET /announcements`、`GET /announcements/{filename}` |
| 健康检查 | `GET /health` |
| AI 对话 | `POST /chat` |

## 配置要点

- 敏感值（`.env`）：DB 连接、DeepSeek API Key、ERP 账密
- 运行时 TOML（`app/config/*.toml`）：
  - `thresholds.toml` — 出价/预算/ACOS 等阈值
  - `layer_options.toml` — 战略/策略/执行层可选项
  - `tags.toml` — 运营标签
- 数据源切换：`DATA_SOURCE=mcp|db|mock|csv`
- ERP 自动写入：`ERP_AUTO_WRITE=true`（env）或请求传 `write_erp: true`

## 维护文档

| 文档 | 说明 |
|------|------|
| [交接文档.md](交接文档.md) | 部署与数仓交接（prodb chenv3.1） |
| [docs/Campaign分析引擎交接文档.md](docs/Campaign分析引擎交接文档.md) | Campaign 引擎技术交接（最全） |
| [docs/前后端接口文档.md](docs/前后端接口文档.md) | 前后端 API 契约 |
| [docs/MCP工具与网关索引.md](docs/MCP工具与网关索引.md) | MCP 工具清单与会话上下文 |
| [docs/ERP数据库改造方案.md](docs/ERP数据库改造方案.md) | ERP 表结构与字段映射 |
| [docs/knowledge_base/知识库目录索引.md](docs/knowledge_base/知识库目录索引.md) | 知识库 01-23 号 KB 索引 |
| [服务器部署说明.md](服务器部署说明.md) | 服务器 `chenv31` 部署流程 |
