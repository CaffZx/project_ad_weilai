# AD Assistant Agent v2.4

亚马逊广告方向决策辅助 Agent：四层向导（战略 → 策略 → 诊断 → 执行）+ 规则引擎 + LLM 报告。

## 快速开始

```bash
cd ad-direction-agent
pip install -r requirements.txt
cp ../.env.example ../.env   # 填入 DB、DeepSeek（协作可用 mock 免 DB）
python start_server.py --data-source mock --port 8010 --no-reload
```

浏览器打开：**http://localhost:8010/demo/ad-asisitant-agent.html**

生产/联调数仓：

```bash
python start_server.py --data-source db --port 8010 --no-reload
```

## 仓库结构

| 目录 | 说明 |
|------|------|
| [ad-direction-agent/](ad-direction-agent/) | 主服务：FastAPI、规则、数仓、Demo |
| [ad-purpose-agent/](ad-purpose-agent/) | 策略层 AI（广告目的/词类），由主服务 PYTHONPATH 引用 |
| [docs/](docs/) | 设计文档、版本公告、协作指南 |

## 配置说明

- **运行时只读 TOML**：`ad-direction-agent/app/config/tags.toml`、`thresholds.toml`、`layer_options.toml`
- 敏感项：项目根目录 `.env` 或 `ad-direction-agent/.env`（见 `.env.example`）

## API

- **当前 Demo 使用**：`/api/v1/agent/ad-direction/*`（战略/策略/诊断/执行/向导、聊天、反馈）
- **LangGraph（可选）**：设置 `USE_LANGGRAPH=true` 后走图编排（默认 `false`，行为与经典路径一致）

## 文档索引

见 [docs/README.md](docs/README.md)。

## 协作

见 [docs/协作指南.md](docs/协作指南.md)。
