# AD Assistant Agent v3.0.2

亚马逊广告方向决策 Agent：四层向导（战略 → 策略 → 诊断 → 执行）+ Campaign 活动分析 + ERP 写入。

## 快速开始

```bash
cd ad-direction-agent
pip install -r requirements.txt
cp .env.example .env   # 填入 DB、DeepSeek、ERP（可选）
python start_server.py --port 8010 --no-reload
```

- Demo：**http://localhost:8010/demo/ad-asisitant-agent.html**
- Campaign 调试：**http://localhost:8010/demo/campaign_test.html**

联调数仓：`DATA_SOURCE=mcp` 或 `db`（见 `.env.example`）。

## 仓库结构

| 目录 | 说明 |
|------|------|
| [ad-direction-agent/](ad-direction-agent/) | 主服务：FastAPI、规则、数仓、Campaign、ERP writer |
| [ad-purpose-agent/](ad-purpose-agent/) | 策略层 AI（广告目的/词类） |
| [docs/](docs/) | 文档索引、知识库、版本公告 |
| [ERP数据库接入文档1.md](ERP数据库接入文档1.md) / [2_campaign.md](ERP数据库接入文档2_campaign.md) | ERP 表结构 |
| [交接文档.md](交接文档.md) | 部署与数仓交接 |

## API（前缀 `/api/v1/agent/ad-direction`）

| 能力 | 端点 |
|------|------|
| 四层向导 | `/strategy/*`、`/tactics/*`、`/diagnosis`、`/execution/*`、`/wizard/*` |
| Campaign 分析 | `POST /campaign/analyze`（可选 `write_erp: true` 或 `ERP_AUTO_WRITE=true` 写 ERP） |
| 健康检查 | `GET /health` |

## 配置要点

- 运行时 TOML：`ad-direction-agent/app/config/*.toml`
- Skill（MCP）：`ad-direction-agent/app/config/skills/mcp-query/`（勿使用仓库根 `.claude/`，已弃用）
- 本地实验输出：写入 `cursor临时文件/`（已在 `.gitignore`，勿提交）

## 文档

见 [docs/README.md](docs/README.md)。
