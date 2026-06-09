# VIPER Strategies

**5 production-grade strategies + RegimeDetector + Sniper Entry + ATR SL/TP**

---

## 1. Strategy Overview

| # | Strategy | ID | Timeframe | Best Regime | Core Logic |
|---|----------|----|-----------|-------------|------------|
| 1 | Mean Reversion | `mr` | 1H / 4H | Ranging | BB squeeze + RSI extremes |
| 2 | Trend Following | `tf` | 4H | Trending | EMA 50/200 cross + volume |
| 3 | Momentum Scalp | `momentum` | 5m / 15m | Volatile | Volume spike + price burst |
| 4 | Funding Rate Arb | `funding_arb` | 1H | Any (perp only) | Rate vs 8H rolling avg |
| 5 | Statistical Arb | `stat_arb` | 15m / 1H | Quiet / Ranging | Z-score spread BTC/ETH |

All strategies return a unified `Signal` dataclass:

```python
@dataclass
class Signal:
    signal: SignalType    # "long" | "short" | "none"
    confidence: float     # 0.0 – 1.0
    entry: float | None
    sl: float | None
    tp1: float | None
    tp2: float | None
    regime: RegimeType    # "ranging" | "trending" | "volatile" | "quiet" | "unknown"
    reason: str
    metadata: dict
```

---

## 2. Strategy Details

### 2.1 Mean Reversion (`mr_generate_signal`)

**When:** Ranging markets (ADX < 20, BB squeeze)

**Entry Logic:**
- Bollinger Bands (20,2) bandwidth < 30th percentile → squeeze detected
- RSI < 35 (oversold) → **long** signal
- RSI > 65 (overbought) → **short** signal

**Exit Logic:**
- TP1: BB middle line (partial close 50%)
- TP2: Opposite BB band (full exit)
- SL: 1 full BB width away from entry

**Confidence:**
- Long: `(oversold - RSI) / oversold`
- Short: `(RSI - overbought) / (100 - overbought)`
- Clamped to [min_confidence, 1.0]

**Configurable params:** `rsi_period`, `bb_period`, `bb_std`, `oversold` (default: 35), `overbought` (default: 65), `squeeze_percentile` (default: 0.30)

---

### 2.2 Trend Following (`tf_generate_signal`)

**When:** Trending markets (ADX > 25)

**Entry Logic:**
- EMA 50 crosses above EMA 200 → **golden cross → long**
- EMA 50 crosses below EMA 200 → **death cross → short**
- Volume confirmation: last volume > 1.5× SMA(20) (relaxed in aggressive mode)

**Exit Logic:**
- SL: Recent 20-period swing low (long) / swing high (short)
- TP1: Entry + 1.5× ATR (long)
- TP2: Entry + 3.0× ATR (long)

**Confidence:**
- Based on EMA spread: `abs(ema_fast - ema_slow) / close * 10`

**Configurable params:** `fast_ema` (50), `slow_ema` (200), `volume_multiplier` (1.5), `vol_sma_period` (20)

---

### 2.3 Momentum Scalp (`momentum_generate_signal`)

**When:** Volatile markets (ATR% > 90th percentile)

**Entry Logic:**
- Price rate-of-change over last 3 bars > 0.3× ATR → movement detected
- RSI > 50 + positive ROC → **long**
- RSI < 50 + negative ROC → **short**

**Exit Logic:**
- SL: Lookback period low (long) / high (short)
- TP1: Entry + 1× ATR
- TP2: Entry + 1.5× ATR

**Confidence:**
- `min(ROC / (ATR * 2), 1.0)` — movement strength relative to volatility

**Configurable params:** `volume_multiplier` (2.5), `vol_window` (10), `lookback_bars` (3), `rsi_period` (7), `atr_period` (7)

---

### 2.4 Funding Rate Arbitrage (`funding_arb_signal`)

**When:** Any regime (perp contracts only). Evaluated independently of price regime.

**Entry Logic:**
- Compute rolling mean and standard deviation of funding rate over last 8 hours
- Current rate > 2σ above mean → **short** (expensive to hold long)
- Current rate < 2σ below mean → **long** (cheap to hold long)

**Exit Logic:**
- SL: 2% buffer from entry
- TP1: 1% (long) / 1% (short)
- TP2: 3% (long) / 3% (short)

**Confidence:**
- `min((|z-score| - sigma) / sigma, 1.0)` — proportional to z-score extremity

**Configurable params:** `sigma_threshold` (2.0), `rolling_hours` (8), `max_rate_abs` (0.01)

---

### 2.5 Statistical Arb — Pairs (`stat_arb_signal`)

**When:** Quiet / Ranging regimes. Designed for correlated pairs (BTC/ETH).

**Entry Logic:**
- Compute spread = `log(price_A) - hedge_ratio * log(price_B)`
- Rolling Z-score of spread over 60 periods
- Z-score > 2: **short** primary asset (spread too wide → mean-revert down)
- Z-score < -2: **long** primary asset (spread too narrow → mean-revert up)

**Exit Logic:**
- SL: Entry ± 1.5× ATR
- TP1: Entry ∓ 0.75× ATR
- TP2: Entry ∓ 1.5× ATR

**Confidence:**
- `min((|Z| - z_entry) / z_entry, 1.0)` — proportional to Z-score extremity

**Configurable params:** `z_entry` (2.0), `z_exit` (0.5), `hedge_ratio` (1.0), `lookback` (60)

**Requires pair data:** Either a second DataFrame (`pair_df`) or a `pair_close` column in the primary DataFrame.

---

## 3. RegimeDetector

The `RegimeDetector` class classifies market regime per symbol using ADX (Average Directional Index) and ATR% (Average True Range as % of close).

### Detection Rules

```
                    ┌──────────────────┐
                    │   OHLCV Input    │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │  calc_adx(df)    │
                    │  calc_atr_pct(df)│
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────────────────────────┐
                    │  ATR% > 90th percentile?             │
                    │  ──► volatile (overrides all else)   │
                    └──────────────────────────────────────┘
                             │
                             ▼
                    ┌──────────────────────────────────────┐
                    │  ADX < 15 AND ATR% < 25th pct?       │
                    │  ──► quiet                           │
                    └──────────────────────────────────────┘
                             │
                             ▼
                    ┌──────────────────────────────────────┐
                    │  ADX > 25?                           │
                    │  ──► trending                        │
                    └──────────────────────────────────────┘
                             │
                             ▼
                    ┌──────────────────────────────────────┐
                    │  ADX < 20?                           │
                    │  ──► ranging                         │
                    └──────────────────────────────────────┘
                             │
                             ▼
                    ┌──────────────────────────────────────┐
                    │  ADX 20-25 (borderline)              │
                    │  ──► unknown                         │
                    └──────────────────────────────────────┘
```

### Regime → Strategy Mapping

| Regime | Primary Strategy | Secondary |
|--------|-----------------|-----------|
| `ranging` | Mean Reversion | Stat Arb |
| `trending` | Trend Following | — |
| `volatile` | Momentum Scalp | — |
| `quiet` | Stat Arb | Mean Reversion |
| `unknown` | Mean Reversion (fallback) | — |

Funding Arb is evaluated independently of price regime (it checks funding rate z-score).

### Transition Tracking
The detector logs regime transitions with ADX/ATR metrics:
```
Regime transition [BTC/USDT]: trending → volatile (ADX=28.3, ATR%=4.52)
```

---

## 4. StrategySelector

The `StrategySelector` class scores all 5 strategies and picks the best one per symbol each cycle.

### Scoring Formula

```
total_score = regime_match  +  win_rate_bonus  +  confidence_bonus  +  signal_bonus

regime_match:    1.0 if regime is in strategy's preferred_regimes, else ~0
win_rate_bonus:  recent_win_rate * 0.3    (range: 0 – 0.3)
confidence_bonus: signal.confidence * 0.5 (range: 0 – 0.5)
signal_bonus:    0.2 if signal != "none", else 0.0
─────────────────────────────────────────────────────────────
Max possible:    2.0
```

### Selection Process

1. Detect regime for symbol via `RegimeDetector`
2. Run ALL strategies (semi-aggressive mode — no regime filtering)
3. Score each strategy-sig pair
4. Sort by descending score
5. If best signal is "none" but non-none alternatives exist, pick highest-scoring non-none
6. If confidence < threshold AND `fallback_on_none=True`, return best anyway with warning note

---

## 5. Sniper Entry System

The sniper module (`viper_sniper.py`) applies confirmation filters before any signal is accepted.

### `sniper_confirm()` — Entry Filter

Two checks (only 1 required to pass, aggressive mode):

| Check | Rule | Pass Condition |
|-------|------|----------------|
| **Candle Close** | Entry price ≈ last candle close | `abs(entry - last_close) / last_close <= 0.02` (2% deviation) |
| **Volume** | Non-zero trading volume | `last_vol > 0` |

Returns `(approved: bool, passed: list[str], reasons: list[str])`.

### `sniper_confirm_mtf()` — Multi-Timeframe Alignment

Checks EMA 9/21 alignment across multiple timeframes:

- **Long:** `EMA9 > EMA21 AND close > EMA9` on each timeframe
- **Short:** `EMA9 < EMA21 AND close < EMA9` on each timeframe

Returns `(aligned: list[tf_names], against: list[tf_names])`.

---

## 6. ATR-Based Dynamic SL/TP

`calc_atr_sl_tp()` overrides fixed % SL/TP with ATR-based values.

### Calculation
```python
atr = calc_atr_raw(df, period=14).iloc[-1]
sl_dist = 1.5 * atr     # 1.5x ATR stop loss
tp_dist = 3.0 * atr     # 3.0x ATR take profit (1:2 risk/reward)

# Long
sl  = entry - sl_dist
tp1 = entry + tp_dist
tp2 = entry + 2 * tp_dist

# Short
sl  = entry + sl_dist
tp1 = entry - tp_dist
tp2 = entry - 2 * tp_dist
```

### Fallback
If ATR series is empty or zero, falls back to fixed % from config (`stop_loss_pct`, `take_profit_pct`). Metadata field `sl_tp_method` reports either `"atr"` or `"fixed"`.

### Exit Management

| Trigger | Behavior |
|---------|----------|
| **TP1** reached | Close 50% of position, move SL to breakeven |
| **TP2** reached | Close remaining 50% |
| **SL** hit | Close full position |
| **Trailing stop** activated (at `trailing_activation_pct` PnL) | Stop trails at `trailing_distance_pct` below highest (long) / above lowest (short) |

---

## 7. Backtest Methodology

### Walk-Forward Testing

Backtesting runs on 4H OHLCV data (500 candles) with:

```
Config:
  initial_balance:    $100
  trade_size_pct:     20% of balance
  slippage:           5 bps
  warmup_bars:        50 (for indicator computation)
  leverage:           1x (no leverage in backtest)

Metrics computed:
  • Total trades
  • Win rate (%)
  • Total PnL (USD)
  • Profit factor (gross profit / gross loss)
  • Sharpe ratio (annualised, risk-free = 0)
  • Max drawdown (peak-to-trough %)
```

### Auto-Backtest Schedule

Every `backtest_every_n_cycles` (default: 4) the engine runs a backtest against the current config, outputting results to log.

### Command-Line Backtest

```bash
./viper-daemon.sh backtest
# Or via Telegram:
# /backtest all
# /backtest mr
# /backtest momentum
```

### Performance Metrics (from journal)

The `TradeJournal.get_all_time_stats()` computes:

| Metric | Calculation |
|--------|-------------|
| **Win Rate** | `wins / total_closed_trades * 100` |
| **Total PnL** | Sum of all `pnl_usd` from closed trades |
| **Avg PnL** | Mean of per-trade PnL |
| **Max Drawdown** | Peak-to-trough on cumulative PnL curve |
| **Sharpe Ratio** | `(mean(pnl) / stdev(pnl)) * sqrt(252)` |

### Risk-Reward Profile

| Strategy | Risk:Reward (TP1) | Risk:Reward (TP2) |
|----------|-------------------|--------------------|
| Mean Reversion | 1:1 (BB mid) | 1:2 (opposite BB) |
| Trend Following | 1:1.5 (ATR-based) | 1:3 (ATR-based) |
| Momentum Scalp | 1:1 (ATR) | 1:1.5 (ATR) |
| Funding Arb | 1:0.5 (fixed %) | 1:1.5 (fixed %) |
| Stat Arb | 1:0.5 (ATR) | 1:1 (ATR) |
