#!/bin/bash
# 核心词离线判定 — 每周一凌晨 2 点执行
# crontab: 0 2 * * 1 /opt/ad-agent/batch_core_keyword.sh >> /var/log/ad-agent/core_keyword.log 2>&1

set -e
cd "$(dirname "$0")"

export BATCH_ERP_HOST="${BATCH_ERP_HOST:-192.168.0.29}"
export BATCH_ERP_PORT="${BATCH_ERP_PORT:-3306}"
export BATCH_ERP_USER="${BATCH_ERP_USER:-erp_agentadvert}"
export BATCH_ERP_PASSWORD="${BATCH_ERP_PASSWORD}"
export BATCH_ERP_DATABASE="${BATCH_ERP_DATABASE:-erp_agentadvert}"
export CORE_KEYWORD_API="${CORE_KEYWORD_API:-http://127.0.0.1:8015/api/v1/agent/ad-direction/core-keyword/analyze}"
export CORE_KEYWORD_MCP_TIMEOUT="${CORE_KEYWORD_MCP_TIMEOUT:-420}"

python3.11 batch_core_keyword.py "$@"
