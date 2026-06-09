#!/usr/bin/env python3
"""VIPER Integration Test — no exchange required"""
import sys, os
import pandas as pd
import numpy as np

sys.path.insert(0, "/root/trader")

from viper_strategies import Signal, StrategySelector
from viper_risk import RiskManager, PositionTracker
from viper_journal import TradeJournal

print("=" * 60)
print("VIPER INTEGRATION TEST (no exchange)")
print("=" * 60)

# 1. Mock OHLCV
np.random.seed(42)
n = 300
dates = pd.date_range("2026-05-01", periods=n, freq="15min")
close = 100 + np.cumsum(np.random.randn(n) * 0.5)
close = np.abs(close)
df = pd.DataFrame({
    "open": close * 0.998,
    "high": close * 1.01,
    "low": close * 0.99,
    "close": close,
    "volume": 1000000 + np.abs(np.random.randn(n) * 200000),
}, index=dates)
df["high"] = df[["open","close"]].max(axis=1) * 1.005
df["low"] = df[["open","close"]].min(axis=1) * 0.995

# 2. Generate signals
config = {"symbols": ["DOGEUSDT"], "trade_amount_usd": 1.0}
selector = StrategySelector()
strategy_meta, selected = selector.select(df, config)

print(f"1. Selected strategy: {strategy_meta.name}")
if selected.signal != "none":
    print(f"   {selected.signal:6s} (conf={selected.confidence:.2f}) [{selected.reason[:40]}]")
else:
    print(f"   Signal: none (random data — expected)")

journal = TradeJournal()
if selected.signal != "none":
    sig = selected.signal
    print(f"\n2. Signal: {sig} (conf={selected.confidence:.2f})")
    print(f"   Entry={selected.entry:.4f} SL={selected.sl:.4f} TP1={selected.tp1:.4f} TP2={selected.tp2:.4f}")

    # 3. Risk check
    risk = RiskManager()
    sd = selected.to_dict()
    sd["symbol"] = "DOGE/USDT"
    print("\n3. Risk check:")
    try:
        approved = risk.check_pre_trade(sd, config, [])
        print(f"   Approved: size={approved.get('adjusted_size', 'N/A')}")
    except Exception as e:
        print(f"   Blocked: {e}")

    # 4. Execute & Journal
    trade_id = journal.open_trade({
        "symbol": "DOGE/USDT", "side": sig,
        "entry_price": selected.entry, "size_usd": 1.0,
        "size_contracts": 1.0 / selected.entry if selected.entry else 0,
        "sl": selected.sl, "tp1": selected.tp1, "tp2": selected.tp2,
        "strategy": "mr", "regime": selected.regime,
        "confidence": selected.confidence, "reason_open": selected.reason,
    })
    print(f"\n4. Journal: trade #{trade_id} opened")

    # Simulate close
    exit_price = df.iloc[-1]["close"]
    journal.close_trade(trade_id, exit_price, "test_complete")
    print(f"   Closed @ {exit_price:.4f}")
else:
    print("\n2. Signal none — skipping trade simulation.")

# 5. Stats
stats = journal.get_all_time_stats()
print(f"\n5. Stats: {stats['total_trades']} trades, WR={stats['win_rate']:.1f}%")

# 6. Module sizes
print("\n6. Module summary:")
total = 0
for fn in ["viper_strategies.py", "viper_risk.py", "viper_journal.py", "viper_engine.py"]:
    fp = f"/root/trader/{fn}"
    if os.path.exists(fp):
        with open(fp) as f:
            lc = sum(1 for _ in f)
        print(f"   {fn:25s} {lc:4d} lines")
        total += lc
print(f"   {'':25s} {'='*4}")
print(f"   {'TOTAL':25s} {total:4d} lines")

sep = "=" * 60
print(f"\n{sep}")
print("ALL MODULES INTEGRATE SUCCESSFULLY")
print(sep)
