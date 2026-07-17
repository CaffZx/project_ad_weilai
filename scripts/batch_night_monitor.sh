#!/bin/bash
# 常驻守夜监控（每晚 01:00 由 cron 拉起，循环到 05:00，每 30min 一检）。
# 成功率 = 真成功(ok且非skip) / 已跑(len results)：
#   rate<5%(任何一次,含运行中) → 全量重试(有批跑在跑先 pkill,绝不并发)
#   5%≤rate<80% 且已跑完 → 定向重试 失败/跳过/数据不可用 的 asin
#   rate≥80% → 正常；运行中继续监测,跑完即停
# 常驻：不自删 cron；每晚自动复用。
ROOT=/opt/ad_agent_github_chenv31
PP=$ROOT/ad-direction-agent
PY=$PP/.venv/bin/python3.11
mkdir -p "$ROOT/logs/$(date +%Y%m%d)"
MON="$ROOT/logs/$(date +%Y%m%d)/batch_monitor.log"
log(){ echo "[$(date '+%F %T')] $*" >> "$MON"; }

log "=== 守夜监控启动 (01:00~05:00, 每30min, 常驻) ==="
STOP=$(date -d '05:00' +%s)

while :; do
  D=$(date +%Y%m%d)
  STATUS="$ROOT/logs/$D/batch_status.json"
  RETRYLOG="$ROOT/logs/$D/batch_retry.log"
  UV=$(ps -eo etime,cmd | grep 'uvicorn app.main' | grep -v grep | awk '{print $1}' | head -1)
  HC=$(curl -s -m 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:8010/health)
  if pgrep -f 'batch_via_api.py' >/dev/null 2>&1; then R=1; else R=0; fi

  verdict=$("$PY" - "$STATUS" "$R" <<'PY'
import sys,json,os
p=sys.argv[1]; running=(sys.argv[2]=="1")
def out(v,run=0,ok=0,asins=""):
    print(f"{v}|{run}|{ok}|{asins}"); raise SystemExit
if not os.path.exists(p): out("NORESULT")
try: d=json.load(open(p,encoding="utf-8"))
except Exception: out("NORESULT")
rows=d.get("results") or []
run=len(rows)
if run==0: out("WAIT" if running else "NORESULT")
ok=sum(1 for r in rows if r.get("ok") and not r.get("skipped"))
rate=ok/run
retry=[str(r.get("asin")) for r in rows if r.get("asin") and not (r.get("ok") and not r.get("skipped"))]
if run<20 and running: out("WAIT",run,ok)
if rate<0.05: out("RETRY_FULL",run,ok)
if running: out("WAIT",run,ok)
if rate>=0.80: out("HEALTHY",run,ok)
out("RETRY_PARTIAL",run,ok,",".join(retry))
PY
)
  IFS='|' read V RUN OK ASINS <<< "$verdict"
  log "进程: uvicorn=$UV /health=$HC 批跑在跑=$R | 判定=$V 已跑=$RUN 真成功=$OK"

  case "$V" in
    HEALTHY)
      log "成功率≥80%(真成功=$OK/已跑=$RUN) → 正常，结束监控"; break;;
    WAIT)      log "等待(运行中或样本不足)，下次再判";;
    NORESULT)  log "暂无批跑结果，等待";;
    RETRY_FULL)
      if [ "$R" = "1" ]; then
        log "rate<5%(真成功=$OK/已跑=$RUN) 且有批跑在跑 → 先 pkill 在跑的，避免并发"
        pkill -f 'batch_via_api.py'; sleep 3
      fi
      if [ "$HC" = "200" ]; then
        nohup "$PY" "$ROOT/batch_via_api.py" --output-dir "$ROOT/logs" >> "$RETRYLOG" 2>&1 &
        log "rate<5% → 已启动全量重试 pid=$!"
      else
        log "rate<5% 但 8010 /health=$HC 异常，跳过重试"
      fi;;
    RETRY_PARTIAL)
      n=$(echo "$ASINS" | tr ',' '\n' | grep -c .)
      if [ "$HC" = "200" ] && [ "$n" -gt 0 ]; then
        nohup "$PY" "$ROOT/batch_via_api.py" --asins "$ASINS" --output-dir "$ROOT/logs" >> "$RETRYLOG" 2>&1 &
        log "5%≤rate<80% 且已跑完 → 已启动定向重试 $n 个失败/跳过/数据不可用 asin pid=$!"
      else
        log "拟定向重试 $n 个，但 /health=$HC 异常或无可重试，跳过"
      fi;;
  esac

  next=$(( $(date +%s) + 1800 ))
  [ "$next" -gt "$STOP" ] && { log "已到 05:00 → 结束监控"; break; }
  sleep 1800
done
log "=== 守夜监控结束 ==="
