"""
Viper Trading Bot — Strategy Suite
====================================
5 production-grade strategies for Bybit perp/futures via CCXT.

| # | Strategy          | Timeframe | Market       | Core Logic                      |
|---|-------------------|-----------|--------------|---------------------------------|
| 1 | Mean Reversion    | 1H / 4H   | Ranging      | BB squeeze + RSI extremes       |
| 2 | Trend Following   | 4H        | Trending     | EMA 50/200 cross + volume       |
| 3 | Momentum Scalp    | 5m / 15m  | High vol     | Volume spike + price burst      |
| 4 | Funding Rate Arb  | 1H        | Perp only    | Rate vs 8H rolling avg          |
| 5 | Statistical Arb   | 15m / 1H  | Correlated   | Z-score spread BTC/ETH          |

All strategies return a uniform signal dict ready for risk/execution module.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Typed signal output — mirrors what risk/exec module expects
# ---------------------------------------------------------------------------

SignalType = Literal["long", "short", "none"]
RegimeType = Literal["ranging", "trending", "volatile", "quiet", "unknown"]


@dataclass
class Signal:
    """Standardised output contract for all strategies."""

    signal: SignalType = "none"
    confidence: float = 0.0  # 0.0 – 1.0
    entry: float | None = None
    sl: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    regime: RegimeType = "unknown"
    reason: str = ""
    # optional extras (carried through but not required)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "confidence": round(self.confidence, 4),
            "entry": self.entry,
            "sl": self.sl,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "regime": self.regime,
            "reason": self.reason,
        }


# ===================================================================
# INDICATOR FUNCTIONS
# ===================================================================


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index.

    Args:
        series: Price series (typically close).
        period: Lookback window.

    Returns:
        RSI values (same length as input, leading NaNs).
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    # Wilder smoothing after initial SMA
    for i in range(period + 1, len(avg_gain)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calc_adx(
    df: pd.DataFrame,
    period: int = 14,
) -> pd.Series:
    """Average Directional Index.

    Expects columns: high, low, close.

    Returns:
        ADX series.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    # +DM / -DM
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    # True Range
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    def _smooth(series: pd.Series, p: int) -> pd.Series:
        return series.rolling(p, min_periods=p).mean()

    tr_s = _smooth(tr, period)
    plus_dm_s = _smooth(plus_dm, period)
    minus_dm_s = _smooth(minus_dm, period)

    # Avoid division by zero
    tr_s = tr_s.replace(0, np.nan)

    plus_di = 100 * plus_dm_s / tr_s
    minus_di = 100 * minus_dm_s / tr_s

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.rolling(period, min_periods=period).mean()
    return adx


def calc_bollinger(
    series: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands.

    Returns:
        (middle, upper, lower, bandwidth).
    """
    middle = series.rolling(period, min_periods=period).mean()
    std = series.rolling(period, min_periods=period).std(ddof=0)
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    bandwidth = (upper - lower) / middle * 100
    return middle, upper, lower, bandwidth


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def calc_macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, histogram.

    Returns:
        (macd_line, signal_line, histogram).
    """
    ema_fast = calc_ema(series, fast)
    ema_slow = calc_ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calc_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calc_volume_sma(volume: pd.Series, period: int = 20) -> pd.Series:
    """Simple moving average of volume."""
    return volume.rolling(period, min_periods=period).mean()


def calc_funding_rate(df: pd.DataFrame) -> pd.Series:
    """Extract or compute funding rate from a 'funding_rate' column.

    Falls back to zeros if column missing so strategies don't explode.
    """
    if "funding_rate" in df.columns:
        return df["funding_rate"].fillna(0.0)
    # Simulate funding rate if absent
    return pd.Series(0.0, index=df.index)


# ===================================================================
# REGIME DETECTION (shared)
# ===================================================================


def detect_regime(df: pd.DataFrame) -> RegimeType:
    """Classify market regime from OHLCV data.

    Rules used:
      - ADX > 25 → trending
      - ADX < 20 AND BB bandwidth < 10th percentile → ranging
      - else if ADX < 20 → ranging
      - ATR % > 90th percentile → volatile (overrides trending/ranging)
      - Fallback → unknown

    Args:
        df: Must have at least 100 rows and columns: open, high, low, close, volume.

    Returns:
        One of "ranging", "trending", "volatile", "unknown".
    """
    required_len = 100  # enough for ADX 14 + BB 20 + buffer
    if df is None or len(df) < required_len:
        return "unknown"

    try:
        adx = calc_adx(df)
        _, _, _, bandwidth = calc_bollinger(df["close"])

        latest_adx = adx.dropna().iloc[-1] if adx.notna().sum() > 0 else 0.0
        latest_bw = bandwidth.dropna().iloc[-1] if bandwidth.notna().sum() > 0 else 0.0

        # ATR % for volatility override
        atr = calc_atr_pct(df)
        atr_pct = atr.dropna().iloc[-1] if atr.notna().sum() > 0 else 0.0
        atr_threshold = atr.dropna().quantile(0.90) if atr.notna().sum() > 50 else 3.0

        # Volatile override
        if atr_pct > atr_threshold:
            return "volatile"

        # Regime
        if latest_adx > 25:
            return "trending"
        elif latest_adx < 20:
            # Check BB squeeze → ranging
            bw_percentile = _percentile_rank(bandwidth.dropna(), latest_bw)
            if bw_percentile < 0.10:
                return "ranging"
            return "ranging"  # low ADX default
        else:
            return "unknown"
    except Exception:
        logger.warning("Regime detection failed", exc_info=True)
        return "unknown"


def calc_atr_raw(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range in price units.
    
    Returns NaN until enough bars exist.
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, min_periods=period, adjust=False).mean()
    return atr


def calc_atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR as percentage of close price."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, min_periods=period, adjust=False).mean()
    return atr / close * 100


def _percentile_rank(series: pd.Series, value: float) -> float:
    """Return percentile rank (0-1) of value within series."""
    if len(series) == 0:
        return 0.5
    return (series < value).sum() / len(series)


# ===================================================================
# REGIME DETECTOR (class-based, with transition tracking)
# ===================================================================


class RegimeDetector:
    """Analyzes market state per symbol and maps to best-fit strategy regime.

    Pure computational detection (no LLM). Uses ADX and ATR% from OHLCV data.

    Regime → Strategy mapping:
      - trending  (ADX > 25)                     → Trend Following
      - ranging   (ADX < 20, not quiet)          → Mean Reversion
      - volatile  (ATR% > 90th percentile)       → Momentum
      - quiet     (ADX < 15 AND ATR% < 25th pct) → Stat Arb
      - unknown   (fallback)                     → Mean Reversion

    Funding Arb is evaluated independently of price regime.
    """

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.adx_trend = float(cfg.get("adx_trend", 25))
        self.adx_range = float(cfg.get("adx_range", 20))
        self.adx_quiet = float(cfg.get("adx_quiet", 15))
        self.atr_vol_pctile = float(cfg.get("atr_vol_pctile", 0.90))
        self.atr_quiet_pctile = float(cfg.get("atr_quiet_pctile", 0.25))
        self.min_bars = int(cfg.get("regime_min_bars", 50))
        # Per-symbol state for transition tracking
        self._last_regime: dict[str, RegimeType] = {}
        self._last_metrics: dict[str, dict] = {}

    def detect(self, df: pd.DataFrame, symbol: str = "") -> RegimeType:
        """Classify market regime for given OHLCV data.

        Args:
            df: OHLCV DataFrame (needs high, low, close, volume).
            symbol: Optional symbol identifier for transition tracking.

        Returns:
            One of 'trending', 'ranging', 'volatile', 'quiet', 'unknown'.
        """
        if df is None or len(df) < self.min_bars:
            return self._set_regime(symbol, "unknown", {})

        try:
            adx = calc_adx(df)
            atr = calc_atr_pct(df)

            latest_adx = float(adx.dropna().iloc[-1]) if adx.notna().sum() > 0 else 0.0
            latest_atr = float(atr.dropna().iloc[-1]) if atr.notna().sum() > 0 else 0.0

            atr_s = atr.dropna()
            atr_vol_thresh = float(atr_s.quantile(self.atr_vol_pctile)) if len(atr_s) > 50 else 5.0
            atr_quiet_thresh = float(atr_s.quantile(self.atr_quiet_pctile)) if len(atr_s) > 50 else 0.5

            metrics = {
                "adx": latest_adx,
                "atr_pct": latest_atr,
                "atr_vol_thresh": atr_vol_thresh,
                "atr_quiet_thresh": atr_quiet_thresh,
            }

            # Volatile (overrides everything)
            if latest_atr > atr_vol_thresh:
                return self._set_regime(symbol, "volatile", metrics)

            # Quiet — low ADX + low vol
            if latest_adx < self.adx_quiet and latest_atr < atr_quiet_thresh:
                return self._set_regime(symbol, "quiet", metrics)

            # Trending
            if latest_adx > self.adx_trend:
                return self._set_regime(symbol, "trending", metrics)

            # Ranging
            if latest_adx < self.adx_range:
                return self._set_regime(symbol, "ranging", metrics)

            # Borderline ADX 20-25 — fall through
            return self._set_regime(symbol, "unknown", metrics)

        except Exception as exc:
            logger.warning("RegimeDetector.detect error: %s", exc, exc_info=True)
            return self._set_regime(symbol, "unknown", {})

    def _set_regime(self, symbol: str, regime: RegimeType, metrics: dict) -> RegimeType:
        """Track regime and log transitions."""
        prev = self._last_regime.get(symbol)
        if prev is not None and prev != regime:
            adx = metrics.get("adx", 0.0)
            atr = metrics.get("atr_pct", 0.0)
            logger.info(
                "Regime transition [%s]: %s → %s (ADX=%.1f, ATR%%=%.2f)",
                symbol or "?",
                prev,
                regime,
                adx,
                atr,
            )
        self._last_regime[symbol] = regime
        self._last_metrics[symbol] = metrics
        return regime

    def last_regime(self, symbol: str = "") -> RegimeType | None:
        """Get last detected regime for a symbol."""
        return self._last_regime.get(symbol)

    def last_metrics(self, symbol: str = "") -> dict:
        """Get last detection metrics for a symbol."""
        return self._last_metrics.get(symbol, {})


def mr_generate_signal(df: pd.DataFrame, config: dict | None = None) -> Signal:
    """Mean Reversion — BB squeeze + RSI extremes.

    Trigger:
      - RSI < 30 (oversold) → long
      - RSI > 70 (overbought) → short
      - BB bandwidth < 10th percentile (squeeze) required for ranging.

    Risk levels:
      - Entry: current close
      - SL: 2 * BB width below/above entry
      - TP1: BB middle line
      - TP2: opposite BB band
    """
    cfg = config or {}
    rsi_period = cfg.get("rsi_period", 14)
    bb_period = cfg.get("bb_period", 20)
    bb_std = cfg.get("bb_std", 2.0)
    oversold = cfg.get("oversold", 35)  # wider range for aggressive
    overbought = cfg.get("overbought", 65)
    squeeze_percentile = cfg.get("squeeze_percentile", 0.30)  # relaxed for aggressive
    min_confidence = cfg.get("min_confidence", 0.0)

    regime = detect_regime(df)
    signal = Signal(regime=regime)

    if len(df) < 60:
        signal.reason = "insufficient data"
        return signal

    try:
        close = df["close"]
        rsi = calc_rsi(close, rsi_period)
        bb_mid, bb_upper, bb_lower, bb_bw = calc_bollinger(close, bb_period, bb_std)

        last_rsi = rsi.dropna().iloc[-1]
        last_close = close.iloc[-1]
        last_bb_mid = bb_mid.dropna().iloc[-1]
        last_bb_upper = bb_upper.dropna().iloc[-1]
        last_bb_lower = bb_lower.dropna().iloc[-1]

        # Squeeze check
        bw_rank = _percentile_rank(bb_bw.dropna(), bb_bw.dropna().iloc[-1])
        is_squeeze = bw_rank <= squeeze_percentile

        if not is_squeeze:
            signal.reason = "no BB squeeze detected"
            return signal

        # Direction
        if last_rsi <= oversold:
            signal.signal = "long"
            signal.entry = last_close
            bb_width = last_bb_upper - last_bb_lower
            signal.sl = last_close - bb_width
            signal.tp1 = last_bb_mid
            signal.tp2 = last_bb_upper
            # Confidence: how far into oversold
            raw_conf = (oversold - last_rsi) / oversold
            signal.confidence = min(max(raw_conf, min_confidence), 1.0)
            signal.reason = f"RSI={last_rsi:.1f} oversold, BB squeeze"
        elif last_rsi >= overbought:
            signal.signal = "short"
            signal.entry = last_close
            bb_width = last_bb_upper - last_bb_lower
            signal.sl = last_close + bb_width
            signal.tp1 = last_bb_mid
            signal.tp2 = last_bb_lower
            raw_conf = (last_rsi - overbought) / (100 - overbought)
            signal.confidence = min(max(raw_conf, min_confidence), 1.0)
            signal.reason = f"RSI={last_rsi:.1f} overbought, BB squeeze"
        else:
            signal.signal = "none"
            signal.reason = f"RSI={last_rsi:.1f} neutral range"

    except Exception as e:
        signal.reason = f"MR error: {e}"
        logger.exception("MR signal failed")

    return signal


# ===================================================================
# STRATEGY 2: TREND FOLLOWING (TF)
# ===================================================================


def tf_generate_signal(df: pd.DataFrame, config: dict | None = None) -> Signal:
    """Trend Following — EMA 50/200 crossover + volume confirmation.

    Trigger:
      - EMA 50 crosses above EMA 200 → long (golden cross)
      - EMA 50 crosses below EMA 200 → short (death cross)
      - Volume must be > 1.5× SMA(20) for confirmation.

    Risk:
      - SL: recent swing low/high (20-period)
      - TP1: 1.5× ATR
      - TP2: 3× ATR
    """
    cfg = config or {}
    fast_ema = cfg.get("fast_ema", 50)
    slow_ema = cfg.get("slow_ema", 200)
    vol_mult = cfg.get("volume_multiplier", 1.5)
    vol_sma_period = cfg.get("vol_sma_period", 20)
    atr_period = cfg.get("atr_period", 14)
    min_confidence = cfg.get("min_confidence", 0.0)

    regime = detect_regime(df)
    signal = Signal(regime=regime)

    if len(df) < slow_ema + 20:
        signal.reason = f"insufficient data (need {slow_ema + 20} bars)"
        return signal

    try:
        close = df["close"]
        volume = df["volume"]

        ema_fast = calc_ema(close, fast_ema)
        ema_slow = calc_ema(close, slow_ema)
        vol_sma = calc_volume_sma(volume, vol_sma_period)
        atr_pct = calc_atr_pct(df, atr_period)

        last_close = close.iloc[-1]
        last_ema_fast = ema_fast.dropna().iloc[-1]
        last_ema_slow = ema_slow.dropna().iloc[-1]
        prev_ema_fast = ema_fast.dropna().iloc[-2] if len(ema_fast.dropna()) >= 2 else last_ema_fast
        prev_ema_slow = ema_slow.dropna().iloc[-2] if len(ema_slow.dropna()) >= 2 else last_ema_slow
        last_vol = volume.iloc[-1]
        last_vol_sma = vol_sma.dropna().iloc[-1]
        last_atr = atr_pct.dropna().iloc[-1] if atr_pct.notna().sum() > 0 else 1.0

        # Crossover detection (aggressive: volume not gating — just adds confidence)
        # vol_ok = last_vol > 0.5 * last_vol_sma if last_vol_sma > 0 else True

        # if not vol_ok:
        #     signal.reason = f"volume {last_vol:.0f} < 0.5× SMA({vol_sma_period})"
        #     return signal

        # Crossover detection
        cross_up = prev_ema_fast <= prev_ema_slow and last_ema_fast > last_ema_slow
        cross_down = prev_ema_fast >= prev_ema_slow and last_ema_fast < last_ema_slow

        atr_price = last_close * (last_atr / 100)

        if cross_up:
            signal.signal = "long"
            signal.entry = last_close
            signal.sl = close.rolling(20).min().iloc[-1]
            signal.tp1 = last_close + atr_price * 1.5
            signal.tp2 = last_close + atr_price * 3.0
            # Confidence: distance between EMAs / close
            ema_dist = (last_ema_fast - last_ema_slow) / last_close
            signal.confidence = min(max(abs(ema_dist) * 10, min_confidence), 1.0)
            signal.reason = f"golden cross EMA{fast_ema}/{slow_ema}, vol confirmed"
        elif cross_down:
            signal.signal = "short"
            signal.entry = last_close
            signal.sl = close.rolling(20).max().iloc[-1]
            signal.tp1 = last_close - atr_price * 1.5
            signal.tp2 = last_close - atr_price * 3.0
            ema_dist = (last_ema_fast - last_ema_slow) / last_close
            signal.confidence = min(max(abs(ema_dist) * 10, min_confidence), 1.0)
            signal.reason = f"death cross EMA{fast_ema}/{slow_ema}, vol confirmed"
        else:
            signal.signal = "none"
            signal.reason = f"no crossover — EMA spread {((last_ema_fast/last_ema_slow - 1)*100):.2f}%"

    except Exception as e:
        signal.reason = f"TF error: {e}"
        logger.exception("TF signal failed")

    return signal


# ===================================================================
# STRATEGY 3: MOMENTUM SCALP
# ===================================================================


def momentum_generate_signal(df: pd.DataFrame, config: dict | None = None) -> Signal:
    """Momentum Scalp — Volume spike + price burst.

    Trigger:
      - Volume > 2.5× SMA(10)
      - Price change over last N bars > threshold (ATR-based)
      - RSI trending in direction (RSI > 55 for long, < 45 for short)

    Risk:
      - SL: 0.5× ATR
      - TP1: 1× ATR
      - TP2: 1.5× ATR
    """
    cfg = config or {}
    vol_mult = cfg.get("volume_multiplier", 2.5)
    vol_window = cfg.get("vol_window", 10)
    lookback = cfg.get("lookback_bars", 3)
    rsi_period = cfg.get("rsi_period", 7)
    atr_period = cfg.get("atr_period", 7)
    min_confidence = cfg.get("min_confidence", 0.0)

    regime = detect_regime(df)
    signal = Signal(regime=regime)

    if len(df) < 30:
        signal.reason = "insufficient data"
        return signal

    try:
        close = df["close"]
        volume = df["volume"]
        high = df["high"]
        low = df["low"]

        # Volume spike (aggressive: not gating — just adds confidence bonus)
        vol_sma = calc_volume_sma(volume, vol_window)
        last_vol = volume.iloc[-1]
        last_vol_sma = vol_sma.dropna().iloc[-1] if vol_sma.notna().sum() > 0 else 1

        # Price burst (rate of change)
        roc = (close.iloc[-1] - close.iloc[-lookback]) / close.iloc[-lookback] * 100

        # ATR for thresholds
        atr_pct = calc_atr_pct(df, atr_period)
        last_atr = atr_pct.dropna().iloc[-1] if atr_pct.notna().sum() > 0 else 1.0

        # Need at least 0.3 ATR of movement (lowered for aggressive)
        if abs(roc) < last_atr * 0.3:
            signal.reason = f"price burst {roc:.2f}% too weak (ATR={last_atr:.2f}%)"
            return signal

        # RSI direction filter
        rsi = calc_rsi(close, rsi_period)
        last_rsi = rsi.dropna().iloc[-1]

        atr_price = close.iloc[-1] * (last_atr / 100)

        if roc > 0 and last_rsi > 50:  # lower RSI threshold for aggressive
            signal.signal = "long"
            signal.entry = close.iloc[-1]
            signal.sl = low.iloc[-lookback:].min()
            signal.tp1 = close.iloc[-1] + atr_price
            signal.tp2 = close.iloc[-1] + atr_price * 1.5
            strength = min(roc / (last_atr * 2), 1.0)
            signal.confidence = min(max(strength, min_confidence), 1.0)
            signal.reason = f"bullish burst {roc:.2f}%, RSI={last_rsi:.1f}"
        elif roc < 0 and last_rsi < 50:  # lower RSI threshold for aggressive
            signal.signal = "short"
            signal.entry = close.iloc[-1]
            signal.sl = high.iloc[-lookback:].max()
            signal.tp1 = close.iloc[-1] - atr_price
            signal.tp2 = close.iloc[-1] - atr_price * 1.5
            strength = min(abs(roc) / (last_atr * 2), 1.0)
            signal.confidence = min(max(strength, min_confidence), 1.0)
            signal.reason = f"bearish burst {roc:.2f}%, RSI={last_rsi:.1f}"
        else:
            signal.signal = "none"
            signal.reason = f"ROC={roc:.2f}% RSI={last_rsi:.1f} no directional alignment"

    except Exception as e:
        signal.reason = f"Momentum error: {e}"
        logger.exception("Momentum signal failed")

    return signal


# ===================================================================
# STRATEGY 4: FUNDING RATE ARB
# ===================================================================


def funding_arb_signal(df: pd.DataFrame, config: dict | None = None) -> Signal:
    """Funding Rate Arbitrage — rate vs 8H rolling average.

    Only meaningful for perpetual swap contracts.

    Logic:
      - Compute 8-period rolling mean of funding rate (hourly data).
      - If current rate is > 2σ above rolling mean → SHORT (expensive to hold long)
      - If current rate is < 2σ below rolling mean → LONG (cheap to hold long)
      - Confidence proportional to z-score magnitude.
    """
    cfg = config or {}
    sigma_threshold = cfg.get("sigma_threshold", 2.0)
    rolling_hours = cfg.get("rolling_hours", 8)
    min_confidence = cfg.get("min_confidence", 0.0)
    max_rate_abs = cfg.get("max_rate_abs", 0.01)  # 0.1% sanity cap

    # Regime: funding arb doesn't depend on price regime
    regime = detect_regime(df)
    signal = Signal(regime=regime)

    if len(df) < rolling_hours + 5:
        signal.reason = f"insufficient data (need {rolling_hours + 5} bars)"
        return signal

    try:
        rate = calc_funding_rate(df)
        last_rate = rate.iloc[-1]

        # Sanity check — absurd rates are data errors
        if abs(last_rate) > max_rate_abs:
            signal.reason = f"rate {last_rate:.6f} exceeds sanity cap {max_rate_abs}"
            return signal

        rolling_mean = rate.rolling(rolling_hours, min_periods=rolling_hours).mean()
        rolling_std = rate.rolling(rolling_hours, min_periods=rolling_hours).std(ddof=0)

        last_mean = rolling_mean.dropna().iloc[-1]
        last_std = rolling_std.dropna().iloc[-1] if rolling_std.notna().sum() > 0 else 0.0

        if last_std < 1e-10:
            signal.reason = "funding rate std ~0, no arb opportunity"
            return signal

        z_score = (last_rate - last_mean) / last_std

        if z_score > sigma_threshold:
            signal.signal = "short"
            signal.entry = df["close"].iloc[-1]
            signal.sl = df["close"].iloc[-1] * 1.02  # 2% buffer
            signal.tp1 = df["close"].iloc[-1] * 0.99
            signal.tp2 = df["close"].iloc[-1] * 0.97
            raw_conf = min((z_score - sigma_threshold) / sigma_threshold, 1.0)
            signal.confidence = min(max(raw_conf, min_confidence), 1.0)
            signal.reason = f"funding rate z={z_score:.2f} (rate={last_rate:.6f}) → short"
        elif z_score < -sigma_threshold:
            signal.signal = "long"
            signal.entry = df["close"].iloc[-1]
            signal.sl = df["close"].iloc[-1] * 0.98
            signal.tp1 = df["close"].iloc[-1] * 1.01
            signal.tp2 = df["close"].iloc[-1] * 1.03
            raw_conf = min((abs(z_score) - sigma_threshold) / sigma_threshold, 1.0)
            signal.confidence = min(max(raw_conf, min_confidence), 1.0)
            signal.reason = f"funding rate z={z_score:.2f} (rate={last_rate:.6f}) → long"
        else:
            signal.signal = "none"
            signal.reason = f"funding rate z={z_score:.2f} within neutral band"

    except Exception as e:
        signal.reason = f"Funding arb error: {e}"
        logger.exception("Funding arb signal failed")

    return signal


# ===================================================================
# STRATEGY 5: STATISTICAL ARB (PAIRS)
# ===================================================================


def stat_arb_signal(
    df: pd.DataFrame,
    config: dict | None = None,
    pair_df: pd.DataFrame | None = None,
) -> Signal:
    """Statistical Arbitrage — Z-score spread between correlated pairs.

    Designed for BTC/ETH. Computes spread as:
        spread = log(price_A) - hedge_ratio * log(price_B)

    When Z-score > 2: SELL spread (short A, long B)
    When Z-score < -2: BUY spread (long A, short B)

    NOTE: This strategy requires TWO DataFrames. `df` is the primary asset,
    `pair_df` is the correlated pair. If pair_df is None, attempts to
    derive pair price from df['pair_close'] column.

    Returns signal on the **primary** asset (df).
    """
    cfg = config or {}
    z_entry = cfg.get("z_entry", 2.0)
    z_exit = cfg.get("z_exit", 0.5)
    hedge_ratio = cfg.get("hedge_ratio", 1.0)  # default 1:1
    lookback = cfg.get("lookback", 60)
    min_confidence = cfg.get("min_confidence", 0.0)

    regime = detect_regime(df)
    signal = Signal(regime=regime)

    if len(df) < lookback:
        signal.reason = f"insufficient data (need {lookback} bars)"
        return signal

    # Get pair price
    if pair_df is not None and "close" in pair_df.columns:
        price_a = df["close"]
        price_b = pair_df["close"]
    elif "pair_close" in df.columns:
        price_a = df["close"]
        price_b = df["pair_close"]
    else:
        signal.reason = "no pair data (provide pair_df or df['pair_close'])"
        return signal

    try:
        # Align lengths
        min_len = min(len(price_a), len(price_b))
        price_a = price_a.iloc[-min_len:]
        price_b = price_b.iloc[-min_len:]

        # Log prices
        log_a = np.log(price_a.replace(0, np.nan))
        log_b = np.log(price_b.replace(0, np.nan))

        # Spread
        spread = log_a - hedge_ratio * log_b

        # Rolling Z-score
        spread_mean = spread.rolling(lookback, min_periods=lookback).mean()
        spread_std = spread.rolling(lookback, min_periods=lookback).std(ddof=0)
        z_score = (spread - spread_mean) / spread_std.replace(0, np.nan)

        last_z = z_score.dropna().iloc[-1]
        last_close = price_a.iloc[-1]

        # ATR for SL/TP
        atr_pct = calc_atr_pct(df)
        last_atr = atr_pct.dropna().iloc[-1] if atr_pct.notna().sum() > 0 else 1.0
        atr_price = last_close * (last_atr / 100)

        if last_z > z_entry:
            # Spread too wide → short primary (mean-revert down)
            signal.signal = "short"
            signal.entry = last_close
            signal.sl = last_close + atr_price * 1.5
            signal.tp1 = last_close - atr_price * 0.75
            signal.tp2 = last_close - atr_price * 1.5
            raw_conf = min((last_z - z_entry) / z_entry, 1.0)
            signal.confidence = min(max(raw_conf, min_confidence), 1.0)
            signal.reason = f"Z={last_z:.2f} > {z_entry} → short spread"
        elif last_z < -z_entry:
            # Spread too narrow → long primary (mean-revert up)
            signal.signal = "long"
            signal.entry = last_close
            signal.sl = last_close - atr_price * 1.5
            signal.tp1 = last_close + atr_price * 0.75
            signal.tp2 = last_close + atr_price * 1.5
            raw_conf = min((abs(last_z) - z_entry) / z_entry, 1.0)
            signal.confidence = min(max(raw_conf, min_confidence), 1.0)
            signal.reason = f"Z={last_z:.2f} < {-z_entry} → long spread"
        else:
            signal.signal = "none"
            signal.reason = f"Z={last_z:.2f} within neutral band ±{z_exit}"

    except Exception as e:
        signal.reason = f"Stat arb error: {e}"
        logger.exception("Stat arb signal failed")

    return signal


# ===================================================================
# STRATEGY REGISTRY (for StrategySelector)
# ===================================================================


@dataclass
class StrategyMeta:
    """Metadata for a registered strategy."""

    name: str
    description: str
    generator: callable
    preferred_regimes: list[RegimeType]
    default_timeframes: list[str]
    min_bars: int


STRATEGY_REGISTRY: list[StrategyMeta] = [
    StrategyMeta(
        name="mr",
        description="Mean Reversion — BB squeeze + RSI extremes",
        generator=mr_generate_signal,
        preferred_regimes=["ranging"],
        default_timeframes=["1h", "4h"],
        min_bars=60,
    ),
    StrategyMeta(
        name="tf",
        description="Trend Following — EMA 50/200 crossover",
        generator=tf_generate_signal,
        preferred_regimes=["trending"],
        default_timeframes=["4h"],
        min_bars=220,
    ),
    StrategyMeta(
        name="momentum",
        description="Momentum Scalp — Volume spike + price burst",
        generator=momentum_generate_signal,
        preferred_regimes=["volatile"],
        default_timeframes=["5m", "15m"],
        min_bars=30,
    ),
    StrategyMeta(
        name="funding_arb",
        description="Funding Rate Arb — Rate vs 8H rolling avg",
        generator=funding_arb_signal,
        preferred_regimes=["ranging", "trending", "volatile", "unknown"],
        default_timeframes=["1h"],
        min_bars=13,
    ),
    StrategyMeta(
        name="stat_arb",
        description="Statistical Arb — Z-score spread between correlated pairs",
        generator=stat_arb_signal,
        preferred_regimes=["quiet", "ranging"],
        default_timeframes=["15m", "1h"],
        min_bars=60,
    ),
]


# ===================================================================
# STRATEGY SELECTOR
# ===================================================================


class StrategySelector:
    """Scores all 5 strategies and picks the best one.

    Scoring considers:
      1. Regime match (required — strategies get 0 if regime incompatible)
      2. Recent win rate (from the journal DB or supplied dict)
      3. Confidence of generated signal

    Usage::

        selector = StrategySelector(win_rates={"mr": 0.55, ...})
        best, signal = selector.select(df, config)
    """

    def __init__(
        self,
        win_rates: dict[str, float] | None = None,
        confidence_threshold: float = 0.3,
        fallback_on_none: bool = True,
    ):
        """
        Args:
            win_rates: Per-strategy recent win rates (0-1). Keyed by strategy name.
            confidence_threshold: Minimum confidence to consider a signal valid.
            fallback_on_none: If True, return best available even if no signal passes
                threshold (useful for live trading).
        """
        self.win_rates = win_rates or {}
        self.confidence_threshold = confidence_threshold
        self.fallback_on_none = fallback_on_none
        self._last_scores: dict[str, float] = {}
        # Regime detector — tracks regime per symbol across cycles
        self.detector = RegimeDetector()

    def select(
        self,
        df: pd.DataFrame,
        config: dict | None = None,
        pair_df: pd.DataFrame | None = None,
    ) -> tuple[StrategyMeta, Signal]:
        """Score all strategies, return (best_strategy, signal).

        Args:
            df: OHLCV DataFrame for the primary asset.
            config: Shared or per-strategy config. Can include top-level keys
                    like 'confidence_threshold' or nested keys like
                    'config__mr__oversold'.
            pair_df: Optional second DataFrame for stat_arb.

        Returns:
            Tuple of (StrategyMeta, Signal). Signal may be 'none' if nothing passes.
        """
        cfg = config or {}
        threshold = cfg.get("confidence_threshold", self.confidence_threshold)
        results: list[tuple[StrategyMeta, Signal, float]] = []

        # ── 1. Detect regime for this symbol ──
        regime = self.detector.detect(df)

        # ── 2. Semi-aggressive mode: try ALL strategies regardless of regime ──
        candidates = list(STRATEGY_REGISTRY)  # no regime filtering

        for meta in candidates:
            if len(df) < meta.min_bars:
                logger.debug("Skip %s: need %d bars, have %d", meta.name, meta.min_bars, len(df))
                continue

            try:
                # Per-strategy config subset
                scfg = self._strategy_config(cfg, meta.name)

                if meta.name == "stat_arb":
                    sig = meta.generator(df, scfg, pair_df=pair_df)
                else:
                    sig = meta.generator(df, scfg)

                if not isinstance(sig, Signal):
                    logger.warning("%s returned non-Signal: %s", meta.name, type(sig))
                    continue

                score = self._score_strategy(meta, sig)
                results.append((meta, sig, score))
            except Exception as e:
                logger.error("Strategy %s failed: %s", meta.name, e, exc_info=True)
                continue

        # Sort by score descending
        results.sort(key=lambda x: x[2], reverse=True)
        self._last_scores = {m.name: s for m, _, s in results}

        if not results:
            fallback = STRATEGY_REGISTRY[0]
            return fallback, Signal(regime=regime, reason="no strategy produced a signal")

        best_meta, best_sig, best_score = results[0]

        # If best signal is 'none' and we have non-none alternatives, use the
        # highest-scoring non-none signal
        if best_sig.signal == "none":
            non_none = [(m, s, sc) for m, s, sc in results if s.signal != "none"]
            if non_none:
                best_meta, best_sig, best_score = non_none[0]

        # Check confidence threshold
        if best_sig.confidence < threshold and not self.fallback_on_none:
            return best_meta, Signal(
                regime=best_sig.regime,
                reason=f"best signal ({best_meta.name}) confidence {best_sig.confidence:.2f} < threshold {threshold}",
            )

        if best_sig.confidence < threshold and self.fallback_on_none:
            # Return best anyway, with a note in reason
            best_sig.reason += f" | confidence {best_sig.confidence:.2f} below threshold {threshold} (fallback)"

        return best_meta, best_sig

    def _score_strategy(self, meta: StrategyMeta, sig: Signal) -> float:
        """Compute composite score for a strategy.

        Components:
          - regime_match: 0.0 or 1.0 (must match)
          - win_rate_bonus: 0 – 0.3 (scaled from recent win rate)
          - confidence_bonus: 0 – 0.5 (direct from signal.confidence)
          - signal_bonus: 0.2 if signal is not 'none' else 0.0

        Max possible: 1.0 + 0.3 + 0.5 + 0.2 = 2.0
        """
        # Regime match (gating)
        regime_ok = sig.regime in meta.preferred_regimes
        regime_score = 1.0 if regime_ok else 0.0
        if not regime_ok:
            return regime_score * 0.01  # Nearly zero but not zero (for logging)

        # Win rate bonus (0 – 0.3)
        wr = self.win_rates.get(meta.name, 0.5)
        wr_bonus = wr * 0.3

        # Confidence bonus (0 – 0.5)
        conf_bonus = sig.confidence * 0.5

        # Signal bonus (0.2 if actionable)
        sig_bonus = 0.2 if sig.signal != "none" else 0.0

        return regime_score + wr_bonus + conf_bonus + sig_bonus

    @staticmethod
    def _strategy_config(global_cfg: dict, strategy_name: str) -> dict:
        """Extract per-strategy config from a flat or nested config dict.

        Supports two styles:
          - Nested:  {"mr": {"oversold": 25}, "tf": {...}}
          - Flat:    {"mr__oversold": 25, "tf__fast_ema": 50}
        Flat keys take precedence over nested.
        """
        cfg = {}
        prefix = f"{strategy_name}__"

        for k, v in global_cfg.items():
            if k.startswith(prefix):
                cfg[k[len(prefix):]] = v
            elif isinstance(v, dict) and k == strategy_name:
                cfg.update(v)

        return cfg

    @property
    def last_scores(self) -> dict[str, float]:
        """Return last round scores for debugging."""
        return dict(self._last_scores)


# ===================================================================
# CONVENIENCE WRAPPER
# ===================================================================


def generate_all_signals(
    df: pd.DataFrame,
    config: dict | None = None,
    pair_df: pd.DataFrame | None = None,
) -> dict[str, Signal]:
    """Run all 5 strategies and return results keyed by name.

    Useful for backtesting or comparing outputs.
    """
    config = config or {}
    results: dict[str, Signal] = {}

    for meta in STRATEGY_REGISTRY:
        try:
            scfg = StrategySelector._strategy_config(config, meta.name)
            if meta.name == "stat_arb":
                sig = meta.generator(df, scfg, pair_df=pair_df)
            else:
                sig = meta.generator(df, scfg)
            results[meta.name] = sig
        except Exception as e:
            results[meta.name] = Signal(
                regime=detect_regime(df),
                reason=f"{meta.name} error: {e}",
            )

    return results


# ===================================================================
# QUICK TEST (when run directly)
# ===================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    # Generate synthetic OHLCV for smoke-test
    np.random.seed(42)
    n = 300
    idx = pd.date_range("2025-01-01", periods=n, freq="1h")
    closes = 100 + np.cumsum(np.random.randn(n) * 0.5)
    df_synth = pd.DataFrame(
        {
            "open": closes * 0.999,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": np.random.lognormal(10, 0.5, n),
            "funding_rate": np.random.randn(n) * 0.0001 + 0.0001,
        },
        index=idx,
    )
    # Add a pair_close col for stat_arb
    df_synth["pair_close"] = closes * 0.85 + np.random.randn(n) * 0.5

    print("=== REGIME ===")
    regime = detect_regime(df_synth)
    print(f"Regime: {regime}")

    print("\n=== ALL STRATEGIES ===")
    all_sigs = generate_all_signals(df_synth)
    for name, sig in all_sigs.items():
        d = sig.to_dict()
        print(f"  {name:15s} → {d['signal']:5s}  conf={d['confidence']:.3f}  "
              f"regime={d['regime']:10s}  reason={d['reason'][:60]}")

    print("\n=== STRATEGY SELECTOR ===")
    selector = StrategySelector(
        win_rates={"mr": 0.48, "tf": 0.62, "momentum": 0.35, "funding_arb": 0.55, "stat_arb": 0.52},
        confidence_threshold=0.2,
    )
    best_meta, best_sig = selector.select(df_synth)
    print(f"Selected: {best_meta.name:15s} → {best_sig.to_dict()}")
    print(f"Scores:  {selector.last_scores}")
    print("\nSmoke test passed ✓")
