#!/bin/bash
# =============================================================================
# VIPER WebSocket Feed Daemon — Real-time Bybit v5 Price Stream
# =============================================================================
# Usage:
#   ./viper_ws_daemon.sh start      # Start WS feed daemon
#   ./viper_ws_daemon.sh stop       # Graceful stop (SIGTERM)
#   ./viper_ws_daemon.sh restart    # Restart daemon
#   ./viper_ws_daemon.sh status     # Check status + live state
#   ./viper_ws_daemon.sh logs       # Tail the WS log
#   ./viper_ws_daemon.sh state      # Dump current market state JSON
#   ./viper_ws_daemon.sh signals    # Show pending alert signals
# =============================================================================
set -euo pipefail

TRADER_DIR="/root/trader"
WS_SCRIPT="$TRADER_DIR/viper_ws.py"
LOG="$TRADER_DIR/viper-ws.log"
EVENTS_LOG="${EVENTS_LOG:-/root/bounties/dashboard-events.log}"
PID_FILE="/run/viper-ws-daemon.pid"
STATE_FILE="$TRADER_DIR/viper_ws_state.json"

# Source credentials / env
if [[ -f "$TRADER_DIR/.env" ]]; then
    set -a
    source "$TRADER_DIR/.env"
    set +a
fi
if grep -q "HERMES_BOT_SECRET" /root/.bashrc 2>/dev/null; then
    source /root/.bashrc
fi

log()   { echo "[$(TZ=UTC date '+%Y-%m-%d %H:%M:%S')] [VIPER-WS] $*" | tee -a "$LOG"; }
dash()  { echo "[$(TZ=Asia/Jakarta date '+%H:%M:%S')] [VIPER-WS] $1 $2" >> "$EVENTS_LOG" 2>/dev/null || true; }

# ─── Commands ────────────────────────────────────

cmd_start() {
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        log "Already running (PID $(cat "$PID_FILE"))"
        return 0
    fi

    log "🐍 Starting VIPER WebSocket Feed..."
    cd "$TRADER_DIR"
    nohup python3 "$WS_SCRIPT" >> "$LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    log "Started PID $pid"
    dash info BOOT "VIPER WS feed started (PID $pid)"

    # Quick health check — wait 5s and verify it's still alive
    sleep 5
    if kill -0 "$pid" 2>/dev/null; then
        log "✅ Process alive after 5s startup check"
    else
        log "❌ Process died within 5s — check $LOG"
        rm -f "$PID_FILE"
        dash critical FAIL "VIPER WS feed crashed on startup"
        return 1
    fi
}

cmd_stop() {
    if [[ ! -f "$PID_FILE" ]]; then
        log "Not running (no PID file)"
        return 0
    fi
    local pid
    pid=$(cat "$PID_FILE")
    log "Sending SIGTERM to PID $pid..."
    kill "$pid" 2>/dev/null || true

    # Wait up to 15s for graceful exit
    local waited=0
    while kill -0 "$pid" 2>/dev/null && (( waited < 15 )); do
        sleep 1
        waited=$((waited + 1))
    done

    if kill -0 "$pid" 2>/dev/null; then
        log "Still alive after 15s — sending SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"
    log "Stopped"
    dash info HALT "VIPER WS feed stopped"
}

cmd_restart() {
    cmd_stop
    sleep 2
    cmd_start
}

cmd_status() {
    local running=false
    local pid=""

    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        running=true
        pid=$(cat "$PID_FILE")
    fi

    if $running; then
        local uptime
        uptime=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')
        echo "✅ VIPER WS Feed is RUNNING (PID $pid, up $uptime)"
    else
        echo "❌ VIPER WS Feed is STOPPED"
    fi

    echo ""
    echo "=== WebSocket State ==="
    if [[ -f "$STATE_FILE" ]]; then
        python3 -c "
import json, sys
try:
    with open('$STATE_FILE') as f:
        s = json.load(f)
    conn = '✅ Connected' if s.get('connected') else '❌ Disconnected'
    msgs = s.get('messages_rx', 0)
    recon = s.get('reconnect_count', 0)
    last = s.get('last_update', 'never')
    sigs = len(s.get('signals', []))
    print(f'  Connection: {conn}')
    print(f'  Messages:   {msgs}')
    print(f'  Reconnects: {recon}')
    print(f'  Signals:    {sigs} pending')
    print(f'  Last update:{last}')
    print()
    for sym, sd in sorted(s.get('symbols', {}).items()):
        ob = sd.get('orderbook', {})
        tr = sd.get('trades', {})
        mid = ob.get('mid_price', 0)
        imb = ob.get('imbalance_side', '?')
        lp  = tr.get('last_price', 0)
        mv  = tr.get('fast_move_pct', 0)
        lvl = ob.get('levels', 0)
        print(f'  {sym:12s}  mid={mid:.6f}  last={lp:.6f}  '
              f'move={mv:+.2f}%  imb={imb:7s}  levels={lvl}')
except Exception as e:
    print(f'  (state read error: {e})')
" 2>/dev/null || echo "  (state file not available)"
    else
        echo "  (no state file yet)"
    fi

    echo ""
    echo "=== Last 10 log lines ==="
    tail -10 "$LOG" 2>/dev/null || echo "  (no log)"
}

cmd_logs() {
    tail -50 "$LOG" 2>/dev/null || echo "(no log file)"
}

cmd_state() {
    if [[ -f "$STATE_FILE" ]]; then
        python3 -m json.tool "$STATE_FILE" 2>/dev/null || cat "$STATE_FILE"
    else
        echo "No state file at $STATE_FILE"
    fi
}

cmd_signals() {
    if [[ -f "$STATE_FILE" ]]; then
        python3 -c "
import json
with open('$STATE_FILE') as f:
    s = json.load(f)
sigs = s.get('signals', [])
if not sigs:
    print('No pending signals')
else:
    print(f'{len(sigs)} pending signal(s):')
    for sig in sigs[-20:]:
        print(f'  [{sig.get(\"timestamp\",\"?\")}] {sig.get(\"message\",\"?\")}')
" 2>/dev/null || echo "(state read error)"
    else
        echo "No state file"
    fi
}

# ═══════════════════════════════════════════════════
#  Main dispatch
# ═══════════════════════════════════════════════════
case "${1:-help}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    state)   cmd_state ;;
    signals) cmd_signals ;;
    *)
        echo "🐍 VIPER WebSocket Feed Daemon"
        echo "Usage: $(basename "$0") {start|stop|restart|status|logs|state|signals}"
        ;;
esac
