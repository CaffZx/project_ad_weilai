#!/bin/bash
# 核心词判定服务启动（本地开发用）
# 生产部署用 systemd: systemctl start ad-core-keyword
# 用法: bash start_core_keyword.sh [--port 8015] [--workers 2]
set -e
cd "$(dirname "$0")"

PORT="${1:-8015}"
# --port 8015 → PORT=8015; 数字直接 → PORT=$1
if [[ "$1" == --port ]]; then PORT="$2"; fi

WORKERS="${CORE_KEYWORD_WORKERS:-5}"
export CORE_KEYWORD_ANALYZE_ENABLED=true

echo "[+] 核心词判定服务: port=$PORT workers=$WORKERS"
.venv/bin/python3.11 -m uvicorn app.start_core_keyword_server:app \
    --host 0.0.0.0 --port "$PORT" \
    --workers "$WORKERS" \
    --log-level info
