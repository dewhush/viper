#!/bin/bash
# =============================================================================
# VIPER DAEMON v2 — 24/7 Autonomous Crypto Trading Bot
# Usage:
#   ./viper-daemon.sh start          # Start background daemon
#   ./viper-daemon.sh stop           # Graceful stop
#   ./viper-daemon.sh status         # Check status
#   ./viper-daemon.sh restart        # Restart daemon
#   ./viper-daemon.sh cycle          # Run one cycle (manual)
#   ./viper-daemon.sh backtest       # Run backtest (manual)
#   ./viper-daemon.sh kill           # Emergency kill switch
#   ./viper-daemon.sh reset          # Reset kill switch
# =============================================================================
set -euo pipefail

TRADER_DIR="/root/trader"
ENGINE="$TRADER_DIR/viper_engine.py"
LOG="$TRADER_DIR/viper-daemon.log"
EVENTS_LOG="${EVENTS_LOG:-/root/bounties/dashboard-events.log}"
PID_FILE="/run/viper-daemon.pid"
CONFIG="$TRADER_DIR/config.json"
ENV_FILE="$TRADER_DIR/.env"

# Source credentials
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Also source from bashrc for Telegram bot token
if grep -q "HERMES_BOT_SECRET" /root/.bashrc 2>/dev/null; then
    source /root/.bashrc
fi

# Override from env if not set in .env
export VIPER_API_KEY="${VIPER_API_KEY:-}"
export VIPER_API_SECRET="${VIPER_API_SECRET:-}"

log()   { echo "[$(TZ=UTC date '+%Y-%m-%d %H:%M:%S')] [VIPER] $*" | tee -a "$LOG"; }
dash()  { echo "[$(TZ=Asia/Jakarta date '+%H:%M:%S')] [VIPER] $1 $2" >> "$EVENTS_LOG" 2>/dev/null || true; }

# ─── Health check — can we reach Bybit + WARP? ───
health_check() {
    # Check WARP
    if ! warp-cli --accept-tos status 2>/dev/null | grep -q "Connected"; then
        log "WARP disconnected. Reconnecting..."
        warp-cli --accept-tos connect 2>/dev/null
        sleep 5
    fi

    # Check Bybit
    if curl -s --connect-timeout 15 --max-time 20 "https://api.bybit.com/v5/market/time" >/dev/null 2>&1; then
        return 0
    fi

    log "⚠️ Bybit UNREACHABLE after WARP reconnect."
    return 1
}

# ─── Commands ───
cmd_start() {
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        log "Already running (PID $(cat "$PID_FILE"))"
        return 0
    fi

    log "🐍 Starting VIPER Daemon v2..."
    nohup bash "$0" _daemon > "$LOG" 2>&1 &
    echo $! > "$PID_FILE"
    log "Started PID $(cat "$PID_FILE")"
    dash info BOOT "VIPER daemon v2 started (PID $(cat \"$PID_FILE\"))"
}

cmd_stop() {
    if [[ ! -f "$PID_FILE" ]]; then
        log "Not running (no PID file)"
        return 0
    fi
    local pid
    pid=$(cat "$PID_FILE")
    log "Stopping PID $pid..."
    kill "$pid" 2>/dev/null || true
    sleep 3
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    log "Stopped"
    dash info HALT "VIPER daemon stopped"
}

cmd_status() {
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        local pid
        pid=$(cat "$PID_FILE")
        local uptime
        uptime=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')
        echo "✅ VIPER is RUNNING (PID $pid, up $uptime)"
        echo ""
        echo "=== Last 5 log lines ==="
        tail -5 "$LOG" 2>/dev/null
        echo ""
        echo "=== Open positions ==="
        python3 -c "
from viper_journal import TradeJournal
j = TradeJournal()
trades = j.get_open_trades()
if trades:
    for t in trades:
        print(f\"  {t['symbol']} {t['side']} @ \${t['entry_price']:.4f} (ID:{t['id']})\")
else:
    print('  None')
" 2>/dev/null || echo "  (journal unavailable)"
    else
        echo "❌ VIPER is STOPPED"
    fi
}

cmd_kill() {
    log "🛑 EMERGENCY KILL SWITCH"
    cd "$TRADER_DIR"
    python3 -c "
from viper_engine import ViperEngine
ViperEngine().activate_kill_switch()
" 2>&1 | tee -a "$LOG"
    dash critical KILL "VIPER kill switch activated"
}

cmd_reset() {
    cd "$TRADER_DIR"
    python3 -c "
from viper_engine import ViperEngine
ViperEngine().reset_kill_switch()
" 2>&1 | tee -a "$LOG"
}

cmd_cycle() {
    cd "$TRADER_DIR"
    python3 -c "
from viper_engine import ViperEngine
import traceback
try:
    engine = ViperEngine()
    if engine._init_exchange():
        result = engine.run_cycle()
        print(f'Done: {result}')
    else:
        print('Exchange not available - missing API keys?')
except Exception as e:
    traceback.print_exc()
" 2>&1 | tee -a "$LOG"
}

cmd_backtest() {
    cd "$TRADER_DIR"
    python3 -c "
from viper_engine import ViperEngine
from viper_backtest import Backtester
from viper_strategies import *
import pandas as pd, numpy as np, json
engine = ViperEngine()
result = engine.run_backtest()
print(json.dumps(result, indent=2))
" 2>&1 | tee -a "$LOG"
}

# ─── Daemon — internal (started by cmd_start) ───
cmd_daemon() {
    log "🐍 VIPER Daemon v2 — Internal loop starting"
    dash info BOOT "VIPER daemon (internal loop)"

    local interval=1  # minutes between cycles (aggressive testing)
    local cycle=0

    # Graceful shutdown on SIGTERM/SIGINT
    trap 'log "🛑 Received shutdown signal. Stopping..."; exit 0' SIGTERM SIGINT

    while true; do
        cycle=$((cycle + 1))
        log "═══════════════════════════════════"
        log "🚀 Internal Cycle #$cycle"

        # Health check
        if ! health_check; then
            log "❌ Health check failed. Retrying in 60s..."
            sleep 60
            continue
        fi

        # Run cycle
        cd "$TRADER_DIR"
        python3 "$ENGINE" cycle 2>&1 | tee -a "$LOG" || {
            log "⚠️ Cycle failed. Continuing..."
        }

        # Backtest every 5 cycles (~5 min with 1-min interval)
        if (( cycle % 5 == 0 )); then
            log "📊 Scheduled backtest (~37s)..."
            python3 "$ENGINE" backtest 2>&1 | tee -a "$LOG" || true
        fi

        # Cooldown (interruptible sleep)
        log "💤 Cooldown ${interval}m..."
        for ((i=interval; i>0; i--)); do
            sleep 60 &
            wait $! 2>/dev/null || break  # wait is interruptible
        done
    done
}


# ═══════════════════════════════════════════════════
#  Main dispatch
# ═══════════════════════════════════════════════════
case "${1:-help}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_stop; sleep 2; cmd_start ;;
    status)  cmd_status ;;
    cycle)   cmd_cycle ;;
    backtest) cmd_backtest ;;
    kill)    cmd_kill ;;
    reset)   cmd_reset ;;
    _daemon) cmd_daemon ;;
    *)
        echo "🐍 VIPER Daemon v2"
        echo "Usage: $(basename "$0") {start|stop|restart|status|cycle|backtest|kill|reset}"
        ;;
esac
