#!/bin/bash
# 8012 Codex 守夜监控（每晚 00:00 由 cron 拉起，循环到 05:00，每 30min 一检）。
# 成功率 = 真成功(ok且非skip) / 已跑(len results)：
#   rate<2%(任何一次,含运行中) → 全量重试(先 pkill 在跑的批跑，等端口释放+进程确认已杀，
#     并临时注释 cron 防自愈重复拉起，绝不并发)
#   2%≤rate<75% 且已跑完 → 定向重试 失败/跳过/数据不可用 的 asin
#   rate≥75% → 正常；运行中继续监测，跑完即停
#
# cron: 0 0 * * * /bin/bash /opt/ad_agent_codex/batch_night_monitor_codex.sh >> /opt/ad_agent_codex/logs/night_monitor.log 2>&1

ROOT=/opt/ad_agent_codex
PY=/opt/ad_agent_github_chenv31/ad-direction-agent/.venv/bin/python3.11
BATCH_SCRIPT="$ROOT/batch_via_api_codex.py"
CRON_PATTERN='crontab_schedule_codex\.sh'

mkdir -p "$ROOT/logs/$(date +%Y%m%d)"
MON="$ROOT/logs/$(date +%Y%m%d)/night_monitor.log"
log(){ echo "[$(date '+%F %T')] $*" >> "$MON"; }

log "=== 8012 守夜监控启动 (00:00~05:00, 每30min, codex版) ==="
STOP=$(date -d '05:00' +%s)

_kill_batch_and_wait() {
    # 步骤1: pkill 批跑进程
    pkill -9 -f 'batch_via_api_codex.py' 2>/dev/null || true
    log "已发送 pkill -9 给 batch_via_api_codex.py，等待进程真正退出..."

    # 步骤2: 死等进程消失（最多 60s）
    for i in $(seq 1 60); do
        if ! pgrep -f 'batch_via_api_codex.py' >/dev/null 2>&1; then
            log "进程已确认被杀 (${i}s)"
            break
        fi
        sleep 1
    done
    if pgrep -f 'batch_via_api_codex.py' >/dev/null 2>&1; then
        log "⚠ 进程 60s 仍未退出，强制继续但不保证无并发"
    fi

    # 步骤3: 临时注释 cron 防自愈（30min 循环内 cron 不会触发，但防 22:30 的残留）
    local bak="/tmp/crontab.bak.$$"
    crontab -l 2>/dev/null > "$bak"
    if grep -q "$CRON_PATTERN" "$bak" 2>/dev/null; then
        sed -i "s/^\(.*$CRON_PATTERN.*\)/#NIGHT_MONITOR_DISABLED \1/" "$bak"
        crontab "$bak"
        log "已临时注释 batch cron，防止自愈重复拉起"
    fi
    rm -f "$bak"
}

_restore_cron() {
    local bak="/tmp/crontab.bak.$$"
    crontab -l 2>/dev/null > "$bak"
    if grep -q 'NIGHT_MONITOR_DISABLED' "$bak" 2>/dev/null; then
        sed -i 's/^#NIGHT_MONITOR_DISABLED //' "$bak"
        crontab "$bak"
        log "已恢复 batch cron"
    fi
    rm -f "$bak"
}

while :; do
  D=$(date +%Y%m%d)
  STATUS="$ROOT/logs/$D/batch_status.json"
  RETRYLOG="$ROOT/logs/$D/batch_retry.log"
  HC=$(curl -s -m 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:8012/health)
  if pgrep -f 'batch_via_api_codex.py' >/dev/null 2>&1; then R=1; else R=0; fi

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
rate=ok/run if run>0 else 0
retry=[str(r.get("asin")) for r in rows if r.get("asin") and not (r.get("ok") and not r.get("skipped"))]
if run<20 and running: out("WAIT",run,ok)
if rate<0.02: out("RETRY_FULL",run,ok)
if running: out("WAIT",run,ok)
if rate>=0.75: out("HEALTHY",run,ok)
out("RETRY_PARTIAL",run,ok,",".join(retry))
PY
  )
  IFS='|' read V RUN OK ASINS <<< "$verdict"
  log "health=$HC 批跑在跑=$R | 判定=$V 已跑=$RUN 真成功=$OK 成功率=$(awk "BEGIN {printf \"%.1f%%\", $OK/$RUN*100}" 2>/dev/null || echo '?')"

  case "$V" in
    HEALTHY)
      log "成功率≥80%(真成功=$OK/已跑=$RUN) → 正常，结束监控"
      _restore_cron
      break;;
    WAIT)      log "等待(运行中或样本不足)，下次再判";;
    NORESULT)  log "暂无批跑结果，等待";;
    RETRY_FULL)
      log "rate<2%(真成功=$OK/已跑=$RUN) → 触发全量重试"
      _kill_batch_and_wait
      if [ "$HC" = "200" ]; then
        nohup "$PY" "$BATCH_SCRIPT" --output-dir "$ROOT/logs" >> "$RETRYLOG" 2>&1 &
        log "全量重试已启动 pid=$!，等待跑完后恢复 cron"
        # 等重试跑完再恢复 cron
        wait $! 2>/dev/null || true
        _restore_cron
        log "全量重试完成，cron 已恢复"
      else
        log "rate<2% 但 8012 /health=$HC 异常，跳过重试"
        _restore_cron
      fi;;
    RETRY_PARTIAL)
      n=$(echo "$ASINS" | tr ',' '\n' | grep -c .)
      if [ "$HC" = "200" ] && [ "$n" -gt 0 ]; then
        nohup "$PY" "$BATCH_SCRIPT" --asins "$ASINS" --output-dir "$ROOT/logs" >> "$RETRYLOG" 2>&1 &
        log "2%≤rate<80% 且已跑完 → 定向重试 $n 个失败/跳过/数据不可用 asin pid=$!"
      else
        log "拟定向重试 $n 个，但 /health=$HC 异常或无可重试，跳过"
      fi;;
  esac

  next=$(( $(date +%s) + 1800 ))
  [ "$next" -gt "$STOP" ] && { log "已到 05:00 → 结束监控"; _restore_cron; break; }
  sleep 1800
done
log "=== 8012 守夜监控结束 ==="
