# VIPER Architecture

**Version:** 2.0  
**Type:** Autonomous Algorithmic Trading Bot — Bybit Perpetual Futures  
**Bot:** @dewviper_bot (Telegram)  
**Language:** Python 3.11+

---

## 1. High-Level Overview

VIPER is a fully autonomous, 24/7 trading bot that scans Bybit perpetual markets across 5 strategies, selects the best signal via a composite scoring system, validates trades through an LLM fallback chain, executes with ATR-based dynamic SL/TP and sniper entry filters, journals everything to SQLite, and pushes Telegram notifications — all without human intervention.

### Data Flow (Conceptual)

```
Exchange OHLCV ──► StrategySelector ──► LLM Validation ──► Risk Check ──► Execute ──► Journal ──► Notify
     ▲                               │                       │             │            │           │
     │                               ▼                       ▼             ▼            ▼           ▼
     └── WebSocket ──► Real-time    Sniper Filter         Kelly Size    Bybit v5    SQLite DB   Telegram
                      Signals       ATR SL/TP             Circuit Brkr  API         (SQLite)    (@dewviper_bot)
```

---

## 2. Cycle Lifecycle

Each trading cycle runs at a configurable interval (default: `cycle_interval_minutes: 15`):

```
┌─────────────────────────────────────────────────────────────────┐
│  1. SCAN                                                       │
│     ├─ Fetch OHLCV for each configured symbol                  │
│     ├─ Run RegimeDetector (ADX + ATR% → market regime)         │
│     ├─ Run all 5 strategies via StrategySelector                │
│     │  (MR, TF, Momentum, Funding Arb, Stat Arb)               │
│     ├─ Apply Sniper entry filters (candle close + volume)      │
│     ├─ Apply ATR-based dynamic SL/TP (1.5x ATR SL, 3x ATR TP) │
│     └─ Collect pending WebSocket signals (fast-move, imbalance)│
│                                                                │
│  2. LLM VALIDATE                                               │
│     ├─ Skip signals with confidence >= 0.40 (aggressive mode)  │
│     └─ Send sub-threshold signals to LLM chain                 │
│        Primary model → Secondary model → Deterministic fallback│
│                                                                │
│  3. RISK CHECK                                                 │
│     ├─ Circuit breaker (daily dd > 5%, weekly > 10%)           │
│     ├─ Kill switch check                                       │
│     ├─ Kelly position sizing (cap 5% of balance)               │
│     ├─ Portfolio exposure (max 30%)                            │
│     ├─ Correlation group reduction                             │
│     ├─ Max positions enforcement                               │
│     ├─ Mandatory stop-loss enforcement                         │
│     └─ Liquidation proximity warning                           │
│                                                                │
│  4. EXECUTE                                                    │
│     ├─ Place limit order on Bybit (USDT perpetual)             │
│     ├─ Set Stop-Loss via Bybit conditional order               │
│     ├─ Set Take-Profit via Bybit conditional order              │
│     └─ Cancel stale orders                                     │
│                                                                │
│  5. POSITION MONITOR                                           │
│     ├─ Partial TP1 close (50% at TP1)                          │
│     ├─ Move SL to breakeven after TP1                          │
│     ├─ Trailing stop activation (at trail_activate % PnL)      │
│     └─ TP2 full exit                                           │
│                                                                │
│  6. JOURNAL                                                    │
│     ├─ Log cycle metadata (scanned, signals, trades, duration) │
│     ├─ Open/close trade records in SQLite                      │
│     ├─ Update daily PnL summary                                │
│     ├─ Balance snapshots                                       │
│     └─ Structured audit trail                                  │
│                                                                │
│  7. NOTIFY                                                     │
│     ├─ Balance → EDIT in place (no spam)                       │
│     ├─ Cycle report → EDIT in place                            │
│     ├─ Positions list → EDIT in place                          │
│     ├─ Trade open/close → SEND new                             │
│     ├─ Alerts → SEND new                                       │
│     └─ Daily summary → EDIT in place                           │
│                                                                │
│  8. BACKTEST (every N cycles)                                  │
│     └─ Walk-forward backtest on 4h data                        │
│                                                                │
│  9. COOLDOWN (sleep for interval)                              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Component Descriptions

### `viper_engine.py` — Main Orchestrator
- `ViperEngine` class: One instance per process
- Methods: `run_cycle()`, `daemon_loop()`, `_scan_symbols()`, `_execute_trade()`, `_close_position()`, `_protect_existing_positions()`, `_llm_validate_signals()`, `_llm_market_analysis()`, `activate_kill_switch()`, `reset_kill_switch()`, `run_backtest()`
- Integrates all sub-modules in a sequential pipeline
- Handles SIGTERM/SIGINT for graceful shutdown
- Loads config from `config.json` and env from `.env`

### `viper_strategies.py` — Strategy Suite
- 5 strategy functions: `mr_generate_signal`, `tf_generate_signal`, `momentum_generate_signal`, `funding_arb_signal`, `stat_arb_signal`
- `RegimeDetector` class: ADX + ATR% market regime classifier
- `StrategySelector` class: Composite scoring to pick best strategy per symbol
- `detect_regime()` function: ADX < 20 → ranging, ADX > 25 → trending, ATR% > 90th percentile → volatile
- `Signal` dataclass: Unified output contract (signal, confidence, entry, sl, tp1, tp2, regime, reason)
- `STRATEGY_REGISTRY`: Metadata for all 5 strategies with preferred regimes

### `viper_risk.py` — Risk Management
- `RiskManager` class: Kelly sizing, circuit breaker, kill switch, portfolio exposure, correlation reduction, slippage tracking, liquidation monitoring
- `PositionTracker` class: Multi-pair position tracker
- `CORRELATION_GROUPS`: Meme cluster, AI cluster, L1 cluster, L2 cluster
- Circuit breaker: Daily drawdown > 5% OR weekly > 10% → halt
- Kelly: `fraction = confidence - ((1 - confidence) / RR)`, clamped 0-25%, cap 5% of balance

### `viper_journal.py` — Trade Journal & Audit Trail
- `TradeJournal` class: SQLite-backed with WAL mode for thread safety
- Tables: `trades`, `daily_pnl`, `balance_snapshots`, `cycle_logs`, `audit_log`
- Methods: `open_trade()`, `close_trade()`, `get_open_trades()`, `get_all_time_stats()`, `get_daily_stats()`, `audit()`, `save_daily_summary()`
- PnL calculation: Computes from `(exit - entry) * contracts * direction - fee`

### `viper_sniper.py` — Sniper Entry + Dynamic SL/TP
- `calc_atr_sl_tp()`: ATR-based SL = entry - 1.5×ATR (long), TP1 = entry + 3.0×ATR, TP2 = entry + 6.0×ATR. Falls back to fixed % if ATR unavailable.
- `sniper_confirm()`: Candle close proximity (< 2% deviation) + non-zero volume filter
- `sniper_confirm_mtf()`: Multi-timeframe EMA alignment (9/21 EMA check)

### `viper_exchange.py` — Multi-Exchange Adapter
- `ExchangeManager` class: Primary = Bybit (API keys), Fallbacks = KuCoin → MEXC (public data)
- Public methods (fetch_ohlcv, fetch_ticker, fetch_order_book): Auto-fallback through Bybit → KuCoin → MEXC
- Private methods (create_order, fetch_balance, etc.): Bybit only — no cross-exchange order routing
- CCXT-based, USDT perpetual (`defaultType: "swap"`), sandbox mode support

### `viper_llm.py` — LLM Validation Chain
- `LLMManager` class: Dual-model fallback chain
- Primary: `oc/deepseek-v4-flash-free` (non-streaming JSON, 5s timeout)
- Secondary: `ollama/minimax-m2.5` (SSE streaming, 10s timeout)
- Deterministic fallback: Regex-based canned analysis
- Endpoint: `http://localhost:20128/v1/chat/completions`
- Health state persisted to `viper_llm_state.json`
- Auto-skip for signals with confidence >= 0.40

### `viper_ws.py` — Bybit v5 WebSocket Real-Time Feed
- SharedState: Lock-protected dict shared between WS daemon thread and engine
- OrderBookManager: Full orderbook rebuild from snapshot + incremental deltas (200 levels)
- TradeTracker: Rolling 60-second trade window for fast-move detection
- SignalDetector: Evaluates fast moves (>2%) and orderbook imbalances (>65%)
- Exponential backoff reconnection (2s → 60s cap)
- Pushes alerts to SharedState; engine drains via `get_pending_signals()`

### `viper_notify.py` — Telegram Notification Templates
- 7 notification templates: position_opened, trade_closed, kill_switch, alert, system_status, notify_balance, cycle_report, daily_summary, positions_list
- Hybrid pattern: Important events → SEND new; status/balance/cycle/positions → EDIT in place (no spam)
- State persisted in `viper_notify_state.json`

### `viper_telegram.py` — Interactive Telegram Commands
- 6 commands: `/status`, `/kill`, `/resume`, `/pnl [days]`, `/backtest [strategy]`, `/audit [n]`, `/help`
- Non-blocking background thread using `python-telegram-bot` Application class
- Bot token: `8789332699:${HERMES_BOT_SECRET}` — bot @dewviper_bot

### `viper_shutdown.py` — Graceful Shutdown Handler
- Registers SIGTERM/SIGINT handlers
- Calls all registered shutdown callbacks in sequence
- Prevents double-trigger

### `viper_env.py` — AES-256 .env Encryption
- Encrypts `.env` → `.env.enc` using `openssl enc -aes-256-cbc -pbkdf2`
- Key stored in `.env.key` with 0600 permissions
- Supports encrypt, decrypt, lock, status commands

---

## 4. Module Interaction Diagram

```
                            ┌──────────────────────┐
                            │   viper_engine.py     │
                            │   (ViperEngine)       │
                            └───────┬──────────────┘
                                    │
            ┌───────────────────────┼───────────────────────────┐
            │                       │                           │
            ▼                       ▼                           ▼
   ┌────────────────┐   ┌────────────────────┐   ┌─────────────────────┐
   │ viper_strategies│   │   viper_risk.py    │   │ viper_exchange.py   │
   │ • 5 strategies │   │ • Kelly sizing     │   │ • Bybit (primary)   │
   │ • RegimeDetect │   │ • Circuit breaker  │   │ • KuCoin (fallback) │
   │ • StrategySel  │   │ • Kill switch      │   │ • MEXC (fallback)   │
   └────────┬───────┘   │ • Correlation      │   └─────────┬───────────┘
            │           └────────────────────┘             │
            ▼                                               ▼
   ┌────────────────┐                            ┌─────────────────────┐
   │ viper_sniper.py│                            │   viper_ws.py       │
   │ • ATR SL/TP    │                            │ • Orderbook stream  │
   │ • Sniper entry │                            │ • Trade tracker     │
   └────────────────┘                            │ • Signal detector   │
                                                  └─────────────────────┘
            ▲                                               ▲
            │                                               │
   ┌────────┴──────────┐                     ┌─────────────┴────────────┐
   │   viper_llm.py    │                     │    viper_journal.py      │
   │ • Deepseek v4     │                     │ • SQLite DB (WAL)        │
   │ • Minimax m2.5    │                     │ • Trades / PnL / Audit  │
   │ • Deterministic   │                     └──────────────────────────┘
   └───────────────────┘
                                   
            ┌─────────────────────────────────────────────┐
            │              viper_notify.py                 │
            │         + viper_telegram.py                  │
            │     Telegram Bot (@dewviper_bot)             │
            └─────────────────────────────────────────────┘
                        ▲
                        │
   ┌──────────────────────────────────────────────┐
   │            Telegram User (Dew)               │
   │   Commands: /status /kill /resume /pnl       │
   │             /backtest /audit /help            │
   └──────────────────────────────────────────────┘
```

---

## 5. LLM Chain Architecture

```
                     ┌─────────────────────────────┐
                     │    _llm_validate_signals()   │
                     │    (viper_engine.py)         │
                     └────────────┬────────────────┘
                                  │
                    ┌─────────────┴──────────────┐
                    │                            │
            conf >= 0.40                   conf < 0.40
                    │                            │
                    ▼                            ▼
           ┌──────────────┐          ┌──────────────────────┐
           │ Auto-approve │          │  LLMManager          │
           │ (skip LLM)   │          │  (viper_llm.py)      │
           └──────────────┘          └──────────┬───────────┘
                                                │
                                ┌───────────────┼───────────────┐
                                │               │               │
                                ▼               ▼               ▼
                    ┌──────────────────┐  ┌──────────────┐  ┌──────────────┐
                    │ Primary Model    │  │ Secondary    │  │ Deterministic│
                    │ deepseek-v4-flash│  │ minimax-m2.5 │  │ Fallback     │
                    │ (JSON, 5s tout)  │  │ (SSE, 10s)   │  │ (regex)      │
                    └──────────────────┘  └──────────────┘  └──────────────┘
                                                │
                                                ▼
                                    ┌──────────────────────┐
                                    │  _llm_market_analysis│
                                    │  (cycle summary)     │
                                    └──────────────────────┘
```

### LLM Health State
Persisted to `viper_llm_state.json`:
```json
{
  "primary_available": true,
  "secondary_available": true,
  "updated_ts": "2025-01-01T00:00:00Z"
}
```

If a model fails, its health flag is set to `false` and it is skipped on subsequent calls. `reset_health()` re-enables both.

---

## 6. WebSocket Data Flow

```
   Bybit v5 Public WebSocket (wss://stream.bybit.com/v5/public/spot)
                        │
                        ▼
              ┌─────────────────────┐
              │  ViperWSClient      │  (async, runs in daemon thread)
              │  (viper_ws.py)      │
              └─────────┬───────────┘
                        │
            ┌───────────┴───────────────┐
            │                           │
            ▼                           ▼
   ┌─────────────────┐       ┌─────────────────┐
   │ OrderBookManager│       │  TradeTracker   │
   │ • Snapshot      │       │ • Rolling 60s   │
   │ • Delta apply   │       │ • Window trades │
   │ • Imbalance calc│       │ • Fast-move pct │
   └────────┬────────┘       └────────┬────────┘
            │                         │
            └──────────┬──────────────┘
                       ▼
              ┌─────────────────┐
              │ SignalDetector  │
              │ • fast_move     │
              │ • imbalance     │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  SharedState    │  (thread-safe, RLock)
              │  push_signal()  │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────────┐
              │  get_pending_signals│  ← engine drains each cycle
              │  (viper_engine.py)  │
              └─────────────────────┘
                       │
                       ▼
              ┌──────────────────────────────┐
              │  Promoted to trade signal if │
              │  fast_move >= 2% OR          │
              │  imbalance >= 65%            │
              │  AND confidence >= 0.60      │
              └──────────────────────────────┘
```

### WS Signal Types
1. **Fast Move** (>2% price change in 60s): Promoted to trade signal at confidence = `min(0.85, 0.5 + |move| * 0.12)`
2. **Orderbook Imbalance** (>65% depth on one side): Promoted at confidence = `min(0.75, 0.5 + |imbalance| * 0.2)`

Both have 30-second cooldown per symbol to prevent alert spam.

---

## 7. Configuration

### `config.json` (key fields)
| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `symbols` | `string[]` | `["BTC/USDT:USDT", ...]` | USDT perpetual pairs |
| `timeframe` | `string` | `"5m"` | Candle interval for scanning |
| `leverage` | `int` | `50` | Bybit leverage |
| `risk_per_trade_pct` | `float` | `50` | % of free balance per trade |
| `max_positions` | `int` | `2` | Max concurrent positions |
| `confidence_threshold` | `float` | `0.15` | Minimum signal confidence |
| `stop_loss_pct` | `float` | `4` | Fixed SL% (fallback) |
| `take_profit_pct` | `float` | `5` | Fixed TP% (fallback) |
| `trailing_stop` | `bool` | `true` | Enable trailing stop |
| `cycle_interval_minutes` | `int` | `15` | Time between cycles |
| `llm_validation` | `bool` | `true` | Enable LLM signal validation |

### `.env` (credentials)
```
VIPER_API_KEY=your_bybit_api_key
VIPER_API_SECRET=your_bybit_api_secret
HERMES_BOT_SECRET=your_telegram_bot_token
HERMES_CHAT_ID=your_telegram_chat_id
```
