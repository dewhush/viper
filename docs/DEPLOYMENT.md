# VIPER Deployment Guide

**Bot:** @dewviper_bot | **Exchange:** Bybit Perpetual Futures (USDT)  
**Version:** 2.0 | **Python:** 3.11+

---

## 1. Prerequisites

### System Requirements
- **OS:** Linux (Debian 12 / Ubuntu 22.04+ tested)
- **CPU:** 1 vCPU minimum
- **RAM:** 512 MB minimum (1 GB recommended)
- **Disk:** 10 GB minimum
- **Network:** Stable internet, WARP+ Cloudflare tunnel recommended

### Software Dependencies
```bash
# Python 3.11+
python3 --version  # Must be >= 3.11

# System packages
apt-get update
apt-get install -y python3 python3-pip openssl curl sqlite3

# WARP for proxy (recommended)
curl -fsSL https://pkg.cloudflareclient.com/install.sh | bash
systemctl enable warp-svc
warp-cli register
warp-cli connect
```

### Python Packages
```
ccxt>=4.0.0         # Exchange API (Bybit, KuCoin, MEXC)
pandas>=2.0.0       # OHLCV data manipulation
requests>=2.28.0    # HTTP client
python-dotenv>=1.0.0# .env loading
websocket-client>=1.6.0  # WebSocket client (for real-time feed)
websockets>=12.0    # Async WebSocket (viper_ws.py)
python-telegram-bot>=20.0  # Telegram bot interfaces
```

---

## 2. Installation

### Clone Repository
```bash
git clone <repo-url> /root/viper-public
cd /root/viper-public
```

### Install Python Dependencies
```bash
# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# Install requirements
pip install -r requirements.txt
pip install ccxt pandas requests python-dotenv websocket-client websockets
pip install "python-telegram-bot[job-queue]"
```

### Directory Structure
```
/root/viper-public/
├── src/
│   ├── viper_engine.py        # Main orchestrator
│   ├── viper_strategies.py    # 5 strategies + RegimeDetector + StrategySelector
│   ├── viper_risk.py          # Risk manager, Kelly, circuit breaker
│   ├── viper_journal.py       # SQLite trade journal
│   ├── viper_sniper.py        # ATR SL/TP + sniper entry
│   ├── viper_exchange.py      # Multi-exchange adapter (Bybit/KuCoin/MEXC)
│   ├── viper_llm.py           # LLM fallback chain
│   ├── viper_notify.py        # Telegram notification templates
│   ├── viper_telegram.py      # Telegram command listener
│   ├── viper_ws.py            # WebSocket real-time feed
│   ├── viper_shutdown.py      # Graceful shutdown handler
│   ├── viper_env.py           # AES-256 .env encryption
│   └── viper_backtest.py      # Backtester
├── scripts/
│   ├── viper-daemon.sh        # 24/7 daemon loop
│   ├── viper_env.sh           # .env encryption helper
│   ├── viper_ws_daemon.sh     # WebSocket standalone daemon
│   └── watchdog.py            # Auto-recovery watchdog
├── config/
│   ├── config.json.example    # Bot configuration template
│   └── .env.example           # API credentials template
├── docs/
│   ├── ARCHITECTURE.md        # This document
│   ├── STRATEGIES.md          # Strategy documentation
│   └── DEPLOYMENT.md          # Deployment guide
└── requirements.txt
```

---

## 3. Configuration

### 3.1 `config.json`

Copy from template and edit:
```bash
cp config/config.json.example config.json
```

Key fields:

```json
{
  "symbols": [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "DOGE/USDT:USDT"
  ],
  "timeframe": "5m",
  "backtest_timeframe": "15m",
  "backtest_limit": 500,
  "leverage": 50,
  "testnet": false,
  "risk_per_trade_pct": 50,
  "max_positions": 2,
  "max_trades_per_cycle": 3,
  "confidence_threshold": 0.15,
  "stop_loss_pct": 4,
  "take_profit_pct": 5,
  "tp1_pct": 5,
  "tp2_pct": 10,
  "tp1_close_pct": 50,
  "trailing_stop": true,
  "trailing_activation_pct": 8,
  "trailing_distance_pct": 3,
  "ws_fast_move_threshold": 2.0,
  "ws_imbalance_threshold": 0.65,
  "ws_promote_min_confidence": 0.6,
  "cycle_interval_minutes": 15,
  "backtest_every_n_cycles": 4,
  "regime": "semi_aggressive",
  "llm_validation": true,
  "llm_market_analysis": true
}
```

### 3.2 `.env` — API Credentials

Copy from template:
```bash
cp config/.env.example .env
```

Edit with your Bybit API key and Telegram bot token:
```
VIPER_API_KEY=your_bybit_api_key
VIPER_API_SECRET=your_bybit_api_secret
HERMES_BOT_SECRET=your_telegram_bot_token
HERMES_CHAT_ID=your_telegram_chat_id
```

**API Key Requirements (Bybit):**
- Exchange: Bybit
- Account: Spot / Derivatives
- Permissions: Trade + Read
- IP whitelist: Add your server IP
- Do NOT enable withdrawal

### 3.3 Encryption (Recommended)

```bash
# Encrypt .env → .env.enc, wipe plaintext
python3 src/viper_env.py encrypt

# Or lock it fully (remove plaintext)
python3 src/viper_env.py lock

# Decrypt when needed
python3 src/viper_env.py decrypt

# Check status
python3 src/viper_env.py status
```

The encrypted file (`.env.enc`) and key (`.env.key`) use AES-256-CBC with PBKDF2. The key file has 0600 permissions.

---

## 4. Running

### 4.1 Quick Start (Test One Cycle)

```bash
cd /root/viper-public/src
python3 viper_engine.py cycle
```

### 4.2 Daemon Mode (24/7)

```bash
# Start background daemon
cd /root/viper-public
bash scripts/viper-daemon.sh start

# Stop gracefully
bash scripts/viper-daemon.sh stop

# Restart
bash scripts/viper-daemon.sh restart

# Check status
bash scripts/viper-daemon.sh status

# Run one cycle manually
bash scripts/viper-daemon.sh cycle

# Emergency kill (close all positions)
bash scripts/viper-daemon.sh kill

# Reset kill switch
bash scripts/viper-daemon.sh reset

# Run backtest
bash scripts/viper-daemon.sh backtest
```

### 4.3 WebSocket Feed (Standalone)

```bash
# Run WebSocket daemon in background
cd /root/viper-public/src
python3 viper_ws.py &
```

### 4.4 LLM Service (Required for LLM Validation)

VIPER expects a local LLM endpoint at `http://localhost:20128/v1/chat/completions`.

Options:
- **Ollama:** `ollama serve` on port 20128
- **LocalAI:** Configured on port 20128
- **Custom proxy:** Any OpenAI-compatible endpoint

The LLM manager uses:
- Primary: `oc/deepseek-v4-flash-free` (non-streaming)
- Fallback: `ollama/minimax-m2.5` (SSE streaming)

---

## 5. systemd Service Setup

Create `/etc/systemd/system/viper.service`:

```ini
[Unit]
Description=VIPER Trading Bot Daemon
After=network.target warp-svc.service
Wants=warp-svc.service

[Service]
Type=forking
ExecStart=/root/viper-public/scripts/viper-daemon.sh start
ExecStop=/root/viper-public/scripts/viper-daemon.sh stop
ExecReload=/root/viper-public/scripts/viper-daemon.sh restart
PIDFile=/run/viper-daemon.pid
Restart=on-failure
RestartSec=30
User=root
WorkingDirectory=/root/viper-public
Environment=PYTHONPATH=/root/viper-public/src
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
systemctl daemon-reload
systemctl enable viper.service
systemctl start viper.service
systemctl status viper.service
```

---

## 6. Log Rotation

Create `/etc/logrotate.d/viper`:

```
/root/viper-public/src/viper.log
/root/viper-public/src/viper-daemon.log
/root/viper-public/src/viper-ws.log
{
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

Test:
```bash
logrotate -d /etc/logrotate.d/viper
```

---

## 7. Database Backup

The SQLite journal DB lives at `/root/trader/viper.db`. Set up automated backups:

```bash
# Create backup script
cat > /root/viper-public/scripts/backup-db.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/root/viper-backups"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%Y%m%d-%H%M)
cp /root/trader/viper.db "$BACKUP_DIR/viper-$DATE.db"
# Keep only last 30 backups
ls -t "$BACKUP_DIR"/viper-*.db | tail -n +31 | xargs -r rm
EOF
chmod +x /root/viper-public/scripts/backup-db.sh
```

Add to crontab:
```bash
crontab -e
# Add: 0 */6 * * * /root/viper-public/scripts/backup-db.sh
```

---

## 8. Watchdog Auto-Recovery

The watchdog script (`scripts/watchdog.py`) monitors VIPER and PHANTOM for errors and auto-recovers.

### What It Checks
1. **VIPER process** — Is the daemon alive?
2. **VIPER logs** — Recent ERROR/CRITICAL entries
3. **WARP connection** — Is Cloudflare tunnel connected?
4. **PHANTOM process** — Is the orchestrator alive?

### Auto-Fix Actions
| Issue | Action |
|-------|--------|
| WARP disconnected | `warp-cli disconnect; sleep 3; warp-cli connect` |
| VIPER daemon dead | `pkill viper-daemon; restart viper-daemon.sh` |
| PHANTOM dead | `pkill mnemonic-orchestrator; restart orchestrator` |
| Unfixable errors | Telegram notification to operator |

### Run as Cron (every 5 minutes)
```bash
crontab -e
*/5 * * * * /usr/bin/python3 /root/viper-public/scripts/watchdog.py
```

### State
Watchdog state persists to `/tmp/watchdog_state.json`. It tracks error counts and last-fix timestamps.

---

## 9. Telegram Bot Setup

### Bot Creation
1. Open Telegram, search for `@BotFather`
2. Send `/newbot` and follow prompts
3. Bot name: `dewviper_bot` (or your choice)
4. Copy the bot token

### Configuration
```bash
# Add to .env
HERMES_BOT_SECRET=<your_bot_token>
HERMES_CHAT_ID=<your_telegram_user_id>
```

To find your chat ID:
1. Start a conversation with your bot
2. Visit: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Look for `"chat":{"id": YOUR_ID}`

### Bot Commands

| Command | Description | Access |
|---------|-------------|--------|
| `/status` | Current bot state (balance, positions, regime, health) | Authorized only |
| `/kill` | Emergency stop — closes all positions | Authorized only |
| `/resume` | Resume trading after kill | Authorized only |
| `/pnl [days]` | PnL report for last N days (default: 1) | Authorized only |
| `/backtest [strategy]` | Run backtest (strategy: mr/tf/momentum/funding_arb/stat_arb/all) | Authorized only |
| `/audit [n]` | Last N audit trail entries (default: 5) | Authorized only |
| `/help` | List all commands | Public |

### Notification Channels

| Template | Type | Behavior |
|----------|------|----------|
| `position_opened` | SEND new | New trade entry alert |
| `trade_closed` | SEND new | Trade exit with PnL |
| `kill_switch` | SEND new | Critical emergency alert |
| `alert` | SEND new | System alerts (warn/error) |
| `system_status` | EDIT in place | Online/offline status |
| `notify_balance` | EDIT in place | Balance updates each cycle |
| `cycle_report` | EDIT in place | Cycle summary (non-spam) |
| `daily_summary` | EDIT in place | 24h performance report |
| `positions_list` | EDIT in place | Open positions snapshot |

---

## 10. Dashboard Setup

### Log-Based Dashboard
The daemon writes events to a dashboard events log:
```bash
# Location
/root/bounties/dashboard-events.log

# Format
[HH:MM:SS] [VIPER] BOOT "message"
[HH:MM:SS] [VIPER] HALT "message"
[HH:MM:SS] [VIPER] TRADE "message"
```

### Events Log Format
```
[10:30:45] [VIPER] BOOT "VIPER daemon v2 started (PID 12345)"
[10:30:50] [VIPER] TRADE "BTC/USDT:USDT LONG @ $50000"
[11:30:45] [VIPER] HALT "VIPER daemon stopped"
```

---

## 11. Security Best Practices

### API Key Management
- **Never** commit `.env` or `.env.enc` to git (`.gitignore` includes them)
- Use AES-256 encryption via `viper_env.py lock`
- Bybit API keys should have IP whitelisting
- Use separate API keys for trading vs monitoring
- Enable 2FA on exchange account

### File Permissions
```bash
# Restrict sensitive files
chmod 600 /root/viper-public/src/.env
chmod 600 /root/viper-public/src/.env.enc
chmod 600 /root/viper-public/src/.env.key
chmod 600 /root/trader/viper.db
chmod 644 /root/viper-public/src/viper.log
```

### Network Security
- Run behind WARP+ tunnel (Cloudflare) for DDoS protection
- Use a firewall: `ufw allow 22/tcp; ufw enable`
- The LLM endpoint should be localhost-only (`127.0.0.1:20128`)
- No open ports required for trading (outbound only)

### Environment Isolation
```bash
# Use a dedicated system user
useradd -m -s /bin/bash viper
# Or run in Docker container
```

### Monitoring & Alerting
- Watchdog sends Telegram alerts for unfixable errors
- Circuit breaker auto-halts on excessive drawdown
- Kill switch can be triggered remotely via Telegram
- Logs rotate daily (7-day retention)
- Database backed up every 6 hours

---

## 12. Troubleshooting

### Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `No exchange keys` | `.env` not configured or encrypted | Decrypt .env or check API keys |
| `Bybit init failed` | Network issue or API key invalid | Check WARP, verify API key permissions |
| `LLM unavailable` | Local LLM endpoint not running | Start Ollama/LocalAI on port 20128 |
| `Rate limit exceeded` | Too many API calls | Reduce symbols or increase cycle interval |
| `Position not closing` | Missing reduceOnly param | Check Bybit conditional order params |
| `WebSocket disconnecting` | Network issues | Exponential backoff handles reconnection |

### Logs

| Log File | Location | Contents |
|----------|----------|----------|
| Engine log | `src/viper.log` | All cycles, trades, errors |
| Daemon log | `src/viper-daemon.log` | Daemon lifecycle, health checks |
| WS log | `src/viper-ws.log` | WebSocket connection, signals |
| Watchdog log | `src/watchdog.log` | Auto-recovery actions |

### Graceful Shutdown

```bash
# systemd
systemctl stop viper.service

# Direct
kill -TERM $(cat /run/viper-daemon.pid)
# or
bash scripts/viper-daemon.sh stop
```

VIPER handles SIGTERM/SIGINT by saving state, closing WebSocket feeds, and exiting cleanly.

---

## 13. Architecture Configuration Matrix

| Config Key | CLI Flag | Env Var | Default | Description |
|-----------|----------|---------|---------|-------------|
| `leverage` | — | — | `50` | Bybit leverage multiplier |
| `risk_per_trade_pct` | — | — | `50` | % of free balance per trade |
| `max_positions` | — | — | `2` | Max concurrent open positions |
| `cycle_interval_minutes` | — | — | `15` | Minutes between cycles |
| `confidence_threshold` | — | — | `0.15` | Min signal confidence |
| `llm_validation` | — | — | `true` | Enable LLM signal validation |
| `testnet` | — | `VIPER_TESTNET` | `false` | Use Bybit testnet |
| API Key | — | `VIPER_API_KEY` | — | Bybit API key |
| API Secret | — | `VIPER_API_SECRET` | — | Bybit API secret |
| Bot Token | — | `HERMES_BOT_SECRET` | — | Telegram bot token |
| Chat ID | — | `HERMES_CHAT_ID` | — | Authorized Telegram user |

---

## 14. Quick Reference

```bash
# ── First time setup ──
git clone <repo> /root/viper-public
cd /root/viper-public
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config/config.json.example src/config.json
cp config/.env.example src/.env
nano src/.env       # Add API keys
python3 src/viper_env.py encrypt
python3 src/viper_env.py lock

# ── Running ──
cd /root/viper-public
bash scripts/viper-daemon.sh start        # Start 24/7 daemon
bash scripts/viper-daemon.sh status       # Verify running

# ── Monitoring ──
tail -f src/viper.log                     # Watch live trading
python3 scripts/watchdog.py               # Run watchdog check

# ── Maintenance ──
bash scripts/backup-db.sh                 # Backup SQLite DB
python3 src/viper_env.py status           # Check encryption status
```
