#!/bin/bash
# 一键拉取反馈日志 → 导出 JSON + 转 Markdown
# 用法:
#   bash scripts/pull_feedback.sh                          # 导出全部，输出到 stdout
#   bash scripts/pull_feedback.sh -a B0B7S3PWWB           # 筛选 ASIN
#   bash scripts/pull_feedback.sh -o feedback_report.md    # 保存到文件
#   bash scripts/pull_feedback.sh -h 192.168.1.100:8010    # 指定服务器

set -euo pipefail

# ── 默认值 ──
HOST="${AD_HOST:-localhost:8010}"
ASIN="${AD_ASIN:-}"
OUTPUT=""

# ── 解析参数 ──
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--host) HOST="$2"; shift 2 ;;
    -a|--asin) ASIN="$2"; shift 2 ;;
    -o|--output) OUTPUT="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

API="http://${HOST}/api/v1/agent/ad-direction/feedback/export"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONVERTER="${SCRIPT_DIR}/feedback_to_md.py"

# ── 1. 拉取 JSON ──
echo ">>> 拉取反馈数据 (${HOST}) ..." >&2

if [[ -n "$ASIN" ]]; then
  JSON=$(curl -sf "${API}?asin=${ASIN}&format=json") || {
    echo "导出失败：无法连接 ${API}" >&2
    exit 1
  }
else
  JSON=$(curl -sf "${API}?format=json") || {
    echo "导出失败：无法连接 ${API}" >&2
    exit 1
  }
fi

COUNT=$(echo "$JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else 0)" 2>/dev/null || echo 0)
echo ">>> 拉取到 ${COUNT} 条反馈" >&2

# ── 2. 保存 JSON（如果需要） ──
JSON_FILE=""
if [[ -n "$OUTPUT" ]]; then
  JSON_FILE="${OUTPUT%.md}.json"
  echo "$JSON" > "$JSON_FILE"
  echo ">>> JSON 已保存: ${JSON_FILE}" >&2
else
  JSON_FILE=$(mktemp /tmp/feedback_XXXXXX.json)
  echo "$JSON" > "$JSON_FILE"
  trap "rm -f $JSON_FILE" EXIT
fi

# ── 3. 转 Markdown ──
if [[ -n "$OUTPUT" ]]; then
  python3 "$CONVERTER" "$JSON_FILE" -o "$OUTPUT"
  echo ">>> Markdown 已保存: ${OUTPUT}" >&2
else
  python3 "$CONVERTER" "$JSON_FILE"
fi
