"""
Viper — Sniper Entry + Dynamic SL/TP Module
============================================
Sniper confirmation filters + ATR-based adaptive SL/TP.
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  Dynamic SL/TP (ATR-based)
# ═══════════════════════════════════════════════════════════════

def calc_atr_sl_tp(signal: dict, df: pd.DataFrame) -> dict:
    """Override signal SL/TP with ATR-based values.

    ATR multiplier:
      SL = 1.5 × ATR (tight to survive wicks)
      TP = 3.0 × ATR (1:2 risk/reward)

    Falls back to fixed config % if ATR unavailable.
    """
    entry = signal.get("entry", 0)
    if not entry:
        return signal

    if "metadata" not in signal:
        signal["metadata"] = {}

    # calc_atr_raw is now in viper_strategies
    from viper_strategies import calc_atr_raw

    atr_series = calc_atr_raw(df, period=14)
    if atr_series.dropna().empty:
        return signal  # fallback to fixed %

    atr = atr_series.dropna().iloc[-1]
    if atr <= 0:
        return signal

    sl_dist = 1.5 * atr
    tp_dist = 3.0 * atr

    is_long = signal["signal"] == "long"
    signal["sl"] = entry - sl_dist if is_long else entry + sl_dist
    signal["tp1"] = entry + tp_dist if is_long else entry - tp_dist
    signal["tp2"] = entry + (2 * tp_dist) if is_long else entry - (2 * tp_dist)

    signal["metadata"]["atr"] = round(atr, 8)
    signal["metadata"]["atr_sl_dist"] = round(sl_dist, 8)
    signal["metadata"]["atr_tp_dist"] = round(tp_dist, 8)
    signal["metadata"]["sl_tp_method"] = "atr"
    return signal


# ═══════════════════════════════════════════════════════════════
#  Sniper Entry — Confirmation Filters
# ═══════════════════════════════════════════════════════════════

def sniper_confirm(signal: dict, df: pd.DataFrame) -> tuple:
    """Sniper entry validation — aggressive mode (main cepet).

    Checks:
      1. Candle close — entry ≈ last candle close
      2. Volume — any volume > 0 (no strict spike req)

    Returns:
      (approved: bool, passed: list[str], reasons: list[str])
    """
    reasons = []
    passed = []

    # ── 1. Candle close ──
    last_close = float(df["close"].iloc[-1])
    entry = signal.get("entry", 0)
    if entry and abs(entry - last_close) / (last_close or 0.01) <= 0.02:
        passed.append("candle_closed")
    else:
        reasons.append("entry != last close")

    # ── 2. Volume (lenient: just check there IS volume) ──
    vol = df["volume"]
    last_vol = float(vol.iloc[-1]) if not vol.empty else 0
    if last_vol > 0:
        passed.append(f"vol_{last_vol:.0f}")
    else:
        reasons.append("zero vol")

    approved = len(passed) >= 1  # Only need 1 check
    return approved, passed, reasons


def sniper_confirm_mtf(signal: dict, dataframes: dict) -> tuple:
    """Multi-timeframe alignment check.

    Args:
      signal: the signal dict
      dataframes: {tf_name: pd.DataFrame} e.g. {"1m": df1, "5m": df5}

    Returns:
      (aligned: bool, tfs_aligned: list[str], tfs_against: list[str])
    """
    is_long = signal["signal"] == "long"
    aligned = []
    against = []

    for tf_name, df in dataframes.items():
        if df is None or df.empty or len(df) < 20:
            continue

        close = df["close"]
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()

        if ema9.dropna().empty or ema21.dropna().empty:
            continue

        last_ema9 = float(ema9.dropna().iloc[-1])
        last_ema21 = float(ema21.dropna().iloc[-1])
        last_close = float(close.iloc[-1])

        if is_long:
            if last_ema9 > last_ema21 and last_close > last_ema9:
                aligned.append(tf_name)
            else:
                against.append(tf_name)
        else:
            if last_ema9 < last_ema21 and last_close < last_ema9:
                aligned.append(tf_name)
            else:
                against.append(tf_name)

    return aligned, against
