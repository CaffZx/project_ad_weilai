#!/bin/bash
# 核心词离线判定 — 每周日早上 7 点执行（8012 vendor 版本）
# crontab: 0 7 * * 0 /bin/bash /opt/ad_agent_codex/batch_core_keyword.sh >> /opt/ad_agent_codex/logs/core_keyword_batch.log 2>&1

set -e

CODE_DIR=/opt/ad_agent_codex/vendor/ad-direction-agent
PY=/opt/ad_agent_github_chenv31/ad-direction-agent/.venv/bin/python3.11
export PYTHONPATH="$CODE_DIR:/opt/ad_agent_codex"

export BATCH_ERP_HOST="${BATCH_ERP_HOST:-192.168.0.34}"
export BATCH_ERP_PORT="${BATCH_ERP_PORT:-3306}"
export BATCH_ERP_USER="${BATCH_ERP_USER:-erp_agentadvert}"
export BATCH_ERP_PASSWORD="${BATCH_ERP_PASSWORD:-WAzx)erp2312whIT666&}"
export BATCH_ERP_DATABASE="${BATCH_ERP_DATABASE:-erp_agentadvert}"
export CORE_KEYWORD_API="${CORE_KEYWORD_API:-http://127.0.0.1:8015/api/v1/agent/ad-direction/core-keyword/analyze}"
export CORE_KEYWORD_WORKERS="${CORE_KEYWORD_WORKERS:-2}"
export CORE_KEYWORD_MCP_TIMEOUT="${CORE_KEYWORD_MCP_TIMEOUT:-420}"

"$PY" "$CODE_DIR/batch_core_keyword.py" "$@"
