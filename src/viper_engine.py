#!/usr/bin/env python3
"""
VIPER — Trading Engine Core (v2)
Orchestrator: Data → StrategySelector → Risk → Execute → Journal → Notify

Depends on: viper_strategies, viper_risk, viper_journal, viper_backtest, hermes_notify
"""

import os, sys, json, time, traceback
from datetime import datetime, timezone, timedelta
from typing import Optional
import signal

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from viper_strategies import Signal, StrategySelector
from viper_risk import RiskManager, PositionTracker
from viper_journal import TradeJournal
from viper_llm import get_llm
from viper_ws import start_feed, stop_feed, get_latest_state, get_pending_signals, is_running
from viper_notify import (
    position_opened, trade_closed,
    alert, system_status,
    kill_switch as notify_kill,
    notify_balance, cycle_report, daily_summary,
    positions_list,
)
from viper_sniper import calc_atr_sl_tp, sniper_confirm, sniper_confirm_mtf

# ─── Config ───
BASE_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")
LOG_FILE = os.path.join(BASE_DIR, "viper.log")
STATE_FILE = os.path.join(BASE_DIR, "viper_state.json")

# ─── Logger ───
def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════
#  VIPER ENGINE
# ═══════════════════════════════════════════════════
class ViperEngine:
    """
    Core trading engine. One instance per process.
    Usage: engine = ViperEngine(); engine.run_cycle()  # single cycle
           engine.daemon_loop()                        # infinite 24/7
    """

    def __init__(self):
        self.config = self._load_config()
        self.env = self._load_env()
        self.ex = None            # ExchangeManager (multi-exchange, with fallback)
        self._bybit = None        # Raw CCXT Bybit instance (order execution)
        self.exchange = None      # Backward compat alias → self._bybit
        self.risk = RiskManager()
        self.journal = TradeJournal()
        self.positions = PositionTracker()
        self.selector = StrategySelector()
        self.llm = get_llm()
        self.counter = {"cycles": 0, "backtests": 0}
        self._kill_signalled = False
        self._running = False
        self._ws_started = False

        # Aggressive mode: trailing stop tracker + partial TP state
        self._trailing = {}   # trade_id -> {"activated": bool, "highest": float, "lowest": float}
        self._partial_tp = {} # trade_id -> {"tp1_hit": bool}

        # Signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    # ── Config ────────────────────────────────────
    def _load_config(self) -> dict:
        if not os.path.exists(CONFIG_FILE):
            log("No config.json. Using defaults.", "WARN")
            return {"symbols": ["DOGEUSDT","PEPEUSDT","WIFUSDT","SHIBUSDT"],
                    "trade_amount_usd": 1.0, "leverage": 10, "timeframe": "15m"}
        with open(CONFIG_FILE) as f:
            return json.load(f)

    def _load_env(self) -> dict:
        env = {}
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"): continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip().strip("'\"")
        return env

    def _save_state(self):
        """Persist engine state (circuit breaker, counters)"""
        state = {
            "last_cycle": _now_utc(),
            "cycles": self.counter["cycles"],
            "backtests": self.counter["backtests"],
            "kill_active": self._kill_signalled,
            "ws_feed": is_running() if self._ws_started else False,
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

    def _load_state(self) -> dict:
        """Load engine state from JSON"""
        if not os.path.exists(STATE_FILE):
            return {}
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    # ── Exchange ──────────────────────────────────
    def _init_exchange(self) -> bool:
        """Initialize ExchangeManager (Bybit + KuCoin + MEXC). Returns True if Bybit ready for trading."""
        from viper_exchange import ExchangeManager

        try:
            self.ex = ExchangeManager(self.env, self.config)
            self._bybit = self.ex.bybit
            self.exchange = self._bybit  # Backward compat

            if self._bybit and self.ex.is_ready:
                try:
                    ticker = self._bybit.fetch_ticker("BTC/USDT")
                    log(f"Bybit OK. BTC/USDT: ${ticker['last']}", "STARTUP")
                except Exception as e:
                    log(f"Bybit verify failed: {e} — public data via fallbacks", "WARN")

                # Set leverage for perp symbols
                lev = self.config.get("leverage", 10)
                for sym in self.config.get("symbols", []):
                    if ":USDT" in sym:
                        try:
                            self._bybit.set_leverage(lev, sym)
                        except Exception as e:
                            log(f"Leverage set {sym}@x{lev}: {e}", "WARN")

                # Protect existing positions with SL/TP
                self._protect_existing_positions()

                return True

            log("Bybit not available for trading. KuCoin/MEXC for public data only.", "WARN")
            return False

        except Exception as e:
            log(f"Exchange init failed: {e}", "CRITICAL")
            traceback.print_exc()
            return False

    def _protect_existing_positions(self, cleanup_stale=True):
        """Set SL/TP on any existing positions that don't have them.

        Args:
            cleanup_stale: If True, cancel stale orphan orders first (startup mode).
                           If False, only check and add missing SL/TP (cycle safety mode).
        """
        if not self._bybit:
            return

        # Cleanup stale orders only at startup
        if cleanup_stale:
            try:
                for sym in self.config.get("symbols", []):
                    try:
                        open_stops = self._bybit.fetch_open_orders(sym)
                    except Exception:
                        open_stops = []
                    cancelled = 0
                    for o in open_stops:
                        try:
                            self._bybit.cancel_order(o["id"], sym)
                            cancelled += 1
                        except Exception:
                            pass
                    if cancelled:
                        log(f"Cancelled {cancelled} stale conditional orders for {sym}", "STARTUP")
            except Exception as e:
                log(f"Stale order cleanup: {e}", "WARN")

        try:
            # Fetch existing conditional orders to avoid duplicates
            existing_sl = {}  # symbol -> [order_ids]
            existing_tp = {}
            for sym in self.config.get("symbols", []):
                try:
                    orders = self._bybit.fetch_open_orders(sym)
                except Exception:
                    orders = []
                for o in orders:
                    reduce = o.get("reduceOnly", False)
                    if not reduce:
                        continue
                    # Bybit v5: stop loss orders have stopLossPrice, tp orders have takeProfitPrice
                    if o.get("stopLossPrice") is not None or "StopLoss" in str(o.get("info", {}).get("orderFilter", "")):
                        existing_sl.setdefault(sym, []).append(o["id"])
                    elif o.get("takeProfitPrice") is not None or "TakeProfit" in str(o.get("info", {}).get("orderFilter", "")):
                        existing_tp.setdefault(sym, []).append(o["id"])

            positions = self._bybit.fetch_positions()
            sl_pct = self.config.get("stop_loss_pct", 4) / 100.0
            tp_pct = self.config.get("take_profit_pct", 5) / 100.0

            for pos in positions:
                qty = float(pos.get("contracts", 0))
                if qty <= 0:
                    continue
                sym = pos["symbol"]
                entry = float(pos.get("entryPrice", 0))
                is_long = float(pos.get("size", 0)) > 0

                # Check if SL already exists for this symbol
                has_sl = len(existing_sl.get(sym, [])) > 0
                has_tp = len(existing_tp.get(sym, [])) > 0
                if has_sl and has_tp:
                    continue  # Covered already

                sl_price = entry * (1 - sl_pct) if is_long else entry * (1 + sl_pct)
                tp_price = entry * (1 + tp_pct) if is_long else entry * (1 - tp_pct)

                # Round to exchange precision
                try:
                    sl_price = float(self._bybit.price_to_precision(sym, sl_price))
                    tp_price = float(self._bybit.price_to_precision(sym, tp_price))
                except Exception:
                    pass

                exit_side = "sell" if is_long else "buy"
                sl_dir = 2 if is_long else 1
                tp_dir = 1 if is_long else 2

                log(f"Protecting {sym} {qty}@${entry:.8f} — SL={'yes' if has_sl else 'NO'} TP={'yes' if has_tp else 'NO'}", "EXEC")

                # Place SL only if missing
                if not has_sl:
                    try:
                        self.ex.create_stop_loss_order(
                            symbol=sym, type="market", side=exit_side, amount=qty,
                            stopLossPrice=sl_price,
                            params={"reduceOnly": True, "triggerBy": "LastPrice",
                                    "triggerDirection": sl_dir, "orderFilter": "StopOrder"}
                        )
                        log(f"SL placed: {sym} @${sl_price:.8f} (dir={sl_dir})", "EXEC")
                    except Exception as e:
                        log(f"SL skip {sym}: {e}", "WARN")

                # Place TP only if missing
                if not has_tp:
                    try:
                        self.ex.create_take_profit_order(
                            symbol=sym, type="market", side=exit_side, amount=qty,
                            takeProfitPrice=tp_price,
                            params={"reduceOnly": True, "triggerBy": "LastPrice",
                                    "triggerDirection": tp_dir, "orderFilter": "TpSlOrder"}
                        )
                        log(f"TP placed: {sym} @${tp_price:.8f} (dir={tp_dir})", "EXEC")
                    except Exception as e:
                        log(f"TP skip {sym}: {e}", "WARN")

        except Exception as e:
            log(f"Protect positions failed: {e}", "WARN")

    # ── Data ──────────────────────────────────────
    def _fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 150) -> pd.DataFrame:
        """Fetch OHLCV, return DataFrame"""
        # Format symbol for CCXT
        if "/" not in symbol:
            if symbol.endswith("USDT"):
                symbol = symbol[:-4] + "/USDT"
            elif symbol.endswith("USDC"):
                symbol = symbol[:-4] + "/USDC"
            else:
                symbol = symbol

        try:
            ohlcv = self.ex.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            return df
        except Exception as e:
            log(f"OHLCV fail {symbol}: {e}", "WARN")
            return pd.DataFrame()

    def _fetch_balance(self) -> dict:
        try:
            bal = self.ex.fetch_balance()
            total = bal.get("free", {}).get("USDT", 0) or 0
            free = bal.get("free", {}).get("USDT", 0) or 0
            return {"total": float(total), "free": float(free)}
        except Exception as e:
            log(f"Balance fetch: {e}", "WARN")
            return {"total": 0, "free": 0}

    # ── Signal Generation ─────────────────────────
    def _scan_symbols(self) -> list:
        """Scan all configured symbols, return list of signals"""
        signals = []
        symbols = self.config.get("symbols", ["DOGEUSDT", "PEPEUSDT"])

        for raw_sym in symbols:
            # Normalise
            if "/" not in raw_sym:
                if raw_sym.endswith("USDT"):
                    sym = raw_sym[:-4] + "/USDT"
                else:
                    sym = raw_sym + "/USDT"
            else:
                sym = raw_sym

            log(f"Scanning {sym}...", "SCAN")

            # Fetch data
            df = self._fetch_ohlcv(sym, timeframe=self.config.get("timeframe", "15m"))
            if df.empty or len(df) < 50:
                log(f"  {sym}: insufficient data ({len(df)} bars)", "SCAN")
                continue

            # Generate signal via StrategySelector
            try:
                strategy_meta, selected = self.selector.select(df, self.config)
            except Exception as e:
                log(f"  {sym}: signal error: {e}", "ERROR")
                continue

            conf_threshold = self.config.get("confidence_threshold", 0.55)
            if selected and selected.signal != "none":
                sig_dict = selected.to_dict()
                if sig_dict["confidence"] >= conf_threshold:
                    sig_dict["symbol"] = sym
                    # ── Fixed SL/TP (fallback if ATR unavailable) ──
                    entry = sig_dict.get("entry", 0)
                    if entry:
                        sl_pct = self.config.get("stop_loss_pct", 4) / 100.0
                        tp_pct = self.config.get("take_profit_pct", 5) / 100.0
                        if sig_dict["signal"] == "long":
                            sig_dict["sl"] = entry * (1 - sl_pct)
                            sig_dict["tp1"] = entry * (1 + tp_pct)
                            sig_dict["tp2"] = entry * (1 + tp_pct * 2)
                        else:  # short
                            sig_dict["sl"] = entry * (1 + sl_pct)
                            sig_dict["tp1"] = entry * (1 - tp_pct)
                            sig_dict["tp2"] = entry * (1 - tp_pct * 2)
                    # ── ATR-based Dynamic SL/TP (overrides fixed) ──
                    sig_dict = calc_atr_sl_tp(sig_dict, df)
                    # ── Sniper Confirmation ──
                    sniper_ok, sniper_passed, sniper_reasons = sniper_confirm(sig_dict, df)
                    if not sniper_ok:
                        log(f"  {sym}: sniper rejected ({'; '.join(sniper_reasons)})", "SNIPER")
                        continue
                    log(f"  {sym}: sniper OK ({'; '.join(sniper_passed)})", "SNIPER")
                    signals.append(sig_dict)
                    log(f"  {sym}: {sig_dict['signal']} (conf={sig_dict['confidence']}) — {sig_dict.get('reason','')} | SL/TP: {sig_dict.get('metadata', {}).get('sl_tp_method','fixed')}", "SIGNAL")
                else:
                    log(f"  {sym}: below threshold ({sig_dict['confidence']:.2f} < {conf_threshold}) — skipped", "FILTER")

            # Check existing positions for exit conditions
            open_positions = self.positions.get(sym)
            if open_positions:
                self._check_position_exit(open_positions, df)

        # ── Real-time WS signals (instant reaction, no REST wait) ──
        ws_signals = get_pending_signals()
        if ws_signals:
            log(f"WS feed: {len(ws_signals)} pending signal(s)", "WS")
            for ws_sig in ws_signals:
                log(f"  WS: {ws_sig.get('message', '?')}", "WS-SIGNAL")
                # Augment signal list with WS-triggered fast-move alerts
                ws_fast_thresh = self.config.get("ws_fast_move_threshold", 2.0)
                ws_promote_conf = self.config.get("ws_promote_min_confidence", 0.60)
                if ws_sig.get("type") == "fast_move" and abs(ws_sig.get("value", 0)) >= ws_fast_thresh:
                    direction = "long" if ws_sig["value"] > 0 else "short"
                    conf = min(0.85, 0.5 + abs(ws_sig["value"]) * 0.12)
                    if conf >= ws_promote_conf:
                        ws_trade_sig = {
                            "signal": direction,
                            "symbol": ws_sig["symbol"][:-4] + "/" + ws_sig["symbol"][-4:],
                            "confidence": conf,
                            "entry": ws_sig["data"].get("last_price", 0),
                            "reason": ws_sig["message"],
                            "regime": "ws_realtime",
                            "metadata": {"strategy": "ws_fast_move", "source": "websocket"},
                        }
                        signals.append(ws_trade_sig)
                        log(f"  WS promoted to trade signal: {direction} {ws_trade_sig['symbol']} (conf={conf:.2f})", "WS-SIGNAL")
                    else:
                        log(f"  WS signal below promote threshold ({conf:.2f} < {ws_promote_conf}) — alert only", "WS-FILTER")
                elif ws_sig.get("type") == "imbalance":
                    imb_thresh = self.config.get("ws_imbalance_threshold", 0.65)
                    imb_val = ws_sig.get("data", {}).get("imbalance_ratio", 0)
                    if abs(imb_val) >= imb_thresh:
                        direction = "long" if imb_val > 0 else "short"
                        ws_trade_sig = {
                            "signal": direction,
                            "symbol": ws_sig["symbol"][:-4] + "/" + ws_sig["symbol"][-4:],
                            "confidence": min(0.75, 0.5 + abs(imb_val) * 0.2),
                            "entry": ws_sig["data"].get("last_price", 0),
                            "reason": ws_sig["message"],
                            "regime": "ws_realtime",
                            "metadata": {"strategy": "ws_imbalance", "source": "websocket"},
                        }
                        signals.append(ws_trade_sig)
                        log(f"  WS imbalance promoted: {direction} {ws_trade_sig['symbol']} (imb={imb_val:.2f})", "WS-SIGNAL")

        return signals

    # ── Position Monitoring ───────────────────────
    def _check_position_exit(self, positions: list, df: pd.DataFrame):
        """Check open positions: SL, TP1 (partial), TP2, trailing stop"""
        trailing_on = self.config.get("trailing_stop", True)
        trail_activate = self.config.get("trailing_activation_pct", 15)
        trail_distance = self.config.get("trailing_distance_pct", 5)
        tp1_close_pct = self.config.get("tp1_close_pct", 50)

        for pos in positions:
            current_price = df.iloc[-1]["close"]
            entry = pos.get("entry_price", 0)
            sl = pos.get("sl", 0)
            tp1 = pos.get("tp1", 0)
            tp2 = pos.get("tp2", 0)
            trade_id = pos.get("trade_id", "")

            # Direction
            is_long = pos["side"] == "long"
            direction = 1 if is_long else -1
            pnl_pct = direction * (current_price - entry) / entry * 100 if entry else 0

            # ── Trailing stop logic ──
            if trailing_on and trade_id:
                t = self._trailing.setdefault(trade_id, {"activated": False, "highest": entry, "lowest": entry})

                if is_long:
                    t["highest"] = max(t["highest"], current_price)
                    if pnl_pct >= trail_activate:
                        t["activated"] = True
                    if t["activated"]:
                        trail_sl = t["highest"] * (1 - trail_distance / 100)
                        if current_price <= trail_sl:
                            log(f"  Trailing stop hit {pos['symbol']}: highest=${t['highest']:.6f} current=${current_price:.6f}", "TRAIL")
                            self._close_position(pos, current_price, "trailing_stop")
                            continue
                else:
                    t["lowest"] = min(t["lowest"], current_price)
                    if pnl_pct >= trail_activate:
                        t["activated"] = True
                    if t["activated"]:
                        trail_sl = t["lowest"] * (1 + trail_distance / 100)
                        if current_price >= trail_sl:
                            log(f"  Trailing stop hit {pos['symbol']}: lowest=${t['lowest']:.6f} current=${current_price:.6f}", "TRAIL")
                            self._close_position(pos, current_price, "trailing_stop")
                            continue

            # ── Partial TP at TP1 ──
            partial_state = self._partial_tp.setdefault(trade_id, {"tp1_hit": False})
            if is_long:
                if not partial_state["tp1_hit"] and tp1 and current_price >= tp1:
                    partial_state["tp1_hit"] = True
                    log(f"  TP1 hit {pos['symbol']} @ ${current_price:.6f} — closing {tp1_close_pct}%", "PARTIAL-TP")
                    close_size = pos.get("size", 0) * (tp1_close_pct / 100)
                    try:
                        self.ex.create_market_order(symbol=pos["symbol"], side="sell", amount=round(close_size, 6))
                        pos["size"] = pos.get("size", 0) - close_size
                        pnl_usd = close_size * (current_price - entry)
                        log(f"  Partial TP1 closed: {close_size:.6f} contracts, PnL: ${pnl_usd:.4f}", "PARTIAL-TP")
                        # Move SL to breakeven
                        pos["sl"] = entry
                        log(f"  SL moved to breakeven: ${entry:.6f}", "TRAIL")
                    except Exception as e:
                        log(f"  Partial TP1 failed: {e}", "ERROR")
                elif sl and current_price <= sl:
                    self._close_position(pos, current_price, "sl")
                elif tp2 and current_price >= tp2:
                    self._close_position(pos, current_price, "tp2")
            else:
                if not partial_state["tp1_hit"] and tp1 and current_price <= tp1:
                    partial_state["tp1_hit"] = True
                    log(f"  TP1 hit {pos['symbol']} @ ${current_price:.6f} — closing {tp1_close_pct}%", "PARTIAL-TP")
                    close_size = pos.get("size", 0) * (tp1_close_pct / 100)
                    try:
                        self.ex.create_market_order(symbol=pos["symbol"], side="buy", amount=round(close_size, 6))
                        pos["size"] = pos.get("size", 0) - close_size
                        pnl_usd = close_size * (entry - current_price)
                        log(f"  Partial TP1 closed: {close_size:.6f} contracts, PnL: ${pnl_usd:.4f}", "PARTIAL-TP")
                        pos["sl"] = entry
                        log(f"  SL moved to breakeven: ${entry:.6f}", "TRAIL")
                    except Exception as e:
                        log(f"  Partial TP1 failed: {e}", "ERROR")
                elif sl and current_price >= sl:
                    self._close_position(pos, current_price, "sl")
                elif tp2 and current_price <= tp2:
                    self._close_position(pos, current_price, "tp2")

    # ── LLM Integration ──────────────────────────
    def _llm_validate_signals(self, signals: list) -> list:
        """Use LLM to validate/filter trade signals. Returns approved subset."""
        if not signals:
            return signals

        validated = []
        for sig in signals:
            symbol = sig.get("symbol", "?")
            side = sig.get("signal", "none")
            confidence = sig.get("confidence", 0)
            reason = sig.get("reason", "")
            regime = sig.get("regime", "unknown")

            # Skip LLM for confident signals (aggressive mode)
            if confidence >= 0.40:
                log(f"  LLM {symbol}: skip — auto-approved (conf={confidence:.2f} >= 0.40)", "LLM")
                validated.append(sig)
                continue

            prompt = (
                f"Signal: {side.upper()} {symbol}\n"
                f"Confidence: {confidence:.2f}\n"
                f"Regime: {regime}\n"
                f"Reason: {reason}\n"
                f"Should this trade be executed? Answer YES or NO."
            )
            try:
                answer = self.llm.inline_completion(prompt)
                log(f"  LLM {symbol}: {answer} (conf={confidence:.2f}, regime={regime})", "LLM")
                if answer == "yes":
                    validated.append(sig)
                else:
                    log(f"  LLM rejected {symbol} {side}", "LLM")
            except Exception as e:
                log(f"  LLM validation error {symbol}: {e} — passing through", "WARN")
                validated.append(sig)  # Permissive on error

        return validated

    def _llm_market_analysis(self, signals: list, balance: dict) -> str:
        """Generate LLM market analysis for the current cycle."""
        symbols = self.config.get("symbols", [])
        signal_summary = ", ".join(
            f"{s['symbol']}:{s['signal']}({s['confidence']:.2f})"
            for s in signals
        ) if signals else "no signals"

        prompt = (
            f"VIPER cycle report:\n"
            f"Symbols scanned: {', '.join(symbols)}\n"
            f"Signals generated: {signal_summary}\n"
            f"Balance: ${balance.get('free', 0):.2f} free / ${balance.get('total', 0):.2f} total\n"
            f"Open positions: {self.positions.count()}\n"
            f"Provide a brief market assessment and risk note."
        )
        try:
            analysis = self.llm.full_completion(prompt)
            log(f"LLM analysis: {analysis[:80]}...", "LLM")
            return analysis
        except Exception as e:
            log(f"LLM analysis error: {e}", "WARN")
            return ""

    # ── Execution ─────────────────────────────────
    def _execute_trade(self, signal: dict) -> Optional[dict]:
        """Execute a trade signal with risk checks"""
        symbol = signal["symbol"]
        side = "buy" if signal["signal"] == "long" else "sell"

        # ── Skip if existing position still open (SL/TP not yet hit) ──
        if self._bybit:
            try:
                exchange_positions = self._bybit.fetch_positions()
                for ep in exchange_positions:
                    if ep.get("symbol") == symbol and float(ep.get("contracts", 0)) > 0:
                        log(f"Skip {symbol}: existing position {ep['side']} x{ep['contracts']} still open — waiting for SL/TP", "SKIP")
                        return None
            except Exception as e:
                log(f"Position check failed for {symbol}: {e}", "WARN")

        # Risk pre-check
        try:
            approved = self.risk.check_pre_trade(
                signal, self.config, self.positions.all()
            )
        except Exception as e:
            log(f"Risk blocked {symbol}: {e}", "RISK")
            alert("Risk Blocked", f"{symbol}: {e}", "warn")
            return None

        # Balance check
        bal = self._fetch_balance()

        # Aggressive sizing: risk_per_trade_pct of balance, capped at trade_amount_usd
        risk_pct = self.config.get("risk_per_trade_pct", 20)
        max_trade = self.config.get("trade_amount_usd", 1.0)
        trade_size = min(max_trade, bal["free"] * (risk_pct / 100.0))
        trade_size = max(0.10, trade_size)  # Minimum $0.10

        # ${symbol} minimum notional check — only for spot, skip perps
        if ":USDT" not in symbol:
            sym_min_notional = {
                "PEPE": 5.0, "DOGE": 5.0, "WIF": 5.0, "SHIB": 5.0,
                "BTC": 10.0, "ETH": 10.0, "SOL": 5.0,
            }
            base = symbol.split("/")[0]
            min_req = sym_min_notional.get(base, 5.0)
            if trade_size < min_req:
                log(f"  ${trade_size:.2f} < ${min_req:.0f} min — using full balance", "EXEC")
                trade_size = min(max_trade, bal["free"])

        if bal["free"] < trade_size:
            log(f"Insufficient balance: ${bal['free']:.4f} < ${trade_size:.4f}", "ERROR")
            return None

        # Max positions check
        max_pos = self.config.get("max_positions", 2)
        if self.positions.count() >= max_pos:
            log(f"Max positions reached ({max_pos}). Skipping {symbol}.", "RISK")
            return None

        # Calculate position size — apply leverage for perps
        lev = self.config.get("leverage", 10)
        if ":USDT" in symbol:
            size_contracts = trade_size * lev / signal["entry"]
        else:
            size_contracts = trade_size / signal["entry"]
        log(f"Executing {side.upper()} {symbol}: ${trade_size:.4f} ({risk_pct}% risk) @ {signal['entry']}", "EXEC")

        try:
            # Place limit order
            order = self.ex.create_limit_order(
                symbol=symbol,
                side=side,
                amount=round(size_contracts, 6),
                price=signal["entry"],
                params={"timeInForce": "GTC"}
            )
            order_id = order.get("id", "unknown")
            log(f"Order placed: {order_id}", "EXEC")

            # Cancel old stale stop orders to avoid 110009 (max 10)
            try:
                old_stops = self._bybit.fetch_open_orders(symbol)
                for o in old_stops:
                    try:
                        self._bybit.cancel_order(o["id"], symbol)
                    except Exception:
                        pass
            except Exception:
                pass

            # ── Set Stop-Loss on exchange ──
            if signal.get("sl"):
                sl_side = "sell" if side == "buy" else "buy"
                # triggerDirection: 2=descending (price falls to SL), 1=ascending
                sl_trigger_dir = 2 if side == "buy" else 1
                try:
                    self.ex.create_stop_loss_order(
                        symbol=symbol,
                        type='market',
                        side=sl_side,
                        amount=round(size_contracts, 6),
                        stopLossPrice=signal["sl"],
                        params={
                            "reduceOnly": True,
                            "triggerBy": "LastPrice",
                            "triggerDirection": sl_trigger_dir,
                            "orderFilter": "StopOrder",
                        }
                    )
                    log(f"SL set: ${signal['sl']:.8f} (trigDir={sl_trigger_dir})", "EXEC")
                except Exception as e:
                    log(f"SL placement failed: {e}", "WARN")

            # ── Set Take-Profit on exchange ──
            if signal.get("tp1"):
                tp_side = "sell" if side == "buy" else "buy"
                # triggerDirection: 1=ascending (price rises to TP), 2=descending
                tp_trigger_dir = 1 if side == "buy" else 2
                try:
                    self.ex.create_take_profit_order(
                        symbol=symbol,
                        type='market',
                        side=tp_side,
                        amount=round(size_contracts, 6),
                        takeProfitPrice=signal["tp1"],
                        params={
                            "reduceOnly": True,
                            "triggerBy": "LastPrice",
                            "triggerDirection": tp_trigger_dir,
                            "orderFilter": "TpSlOrder",
                        }
                    )
                    log(f"TP set: ${signal['tp1']:.8f} (trigDir={tp_trigger_dir})", "EXEC")
                except Exception as e:
                    log(f"TP placement failed: {e}", "WARN")

            # Journal
            trade_id = self.journal.open_trade({
                "symbol": symbol,
                "side": signal["signal"],
                "entry_price": signal["entry"],
                "size_usd": trade_size,
                "size_contracts": size_contracts,
                "sl": signal.get("sl"),
                "tp1": signal.get("tp1"),
                "tp2": signal.get("tp2"),
                "strategy": signal.get("metadata", {}).get("strategy", "unknown"),
                "regime": signal.get("regime", "unknown"),
                "confidence": signal.get("confidence", 0),
                "reason_open": signal.get("reason", ""),
            })

            pos = {
                "trade_id": trade_id,
                "symbol": symbol,
                "side": signal["signal"],
                "entry_price": signal["entry"],
                "size": size_contracts,
                "order_id": order_id,
                "sl": signal.get("sl"),
                "tp1": signal.get("tp1"),
                "tp2": signal.get("tp2"),
            }
            self.positions.add(symbol, pos)

            # Notify
            lev = self.config.get("leverage", 10)
            position_opened(
                pair=symbol, side=signal["signal"].upper(),
                entry=signal["entry"], size_usd=trade_size,
                size_contracts=round(size_contracts, 0),
                sl=signal.get("sl", 0), tp=signal.get("tp1", 1),
                strategy=signal.get("metadata", {}).get("strategy", ""),
                confidence=signal["confidence"],
                leverage=lev,
            )

            return pos

        except Exception as e:
            log(f"Trade failed: {e}", "ERROR")
            alert("Trade Failed", f"{symbol}: {e}", "error")
            return None

    def _close_position(self, pos: dict, price: float, reason: str):
        """Close open position"""
        symbol = pos["symbol"]
        side = "sell" if pos["side"] == "long" else "buy"

        try:
            order = self.ex.create_market_order(
                symbol=symbol,
                side=side,
                amount=round(pos.get("size", 0), 6),
                params={"reduceOnly": True}
            )

            # Calculate PnL
            entry = pos["entry_price"]
            direction = 1 if pos["side"] == "long" else -1
            pnl_pct = direction * (price - entry) / entry * 100
            pnl_usd = pos.get("size", 0) * (price - entry) * direction

            # Journal
            self.journal.close_trade(
                trade_id=pos["trade_id"],
                exit_price=price,
                reason=reason,
            )

            # Remove from tracker
            self.positions.remove(pos.get("trade_id"))

            # Notify
            trade_closed(
                pair=symbol, side=pos["side"].upper(),
                entry=entry, exit_price=price,
                pnl_usd=pnl_usd, pnl_pct=pnl_pct,
                duration="", reason=reason
            )

            # Risk state update
            self.risk.update_state(pnl_usd, pnl_pct)
            self._save_state()

            log(f"Closed {symbol} @ ${price}: {pnl_usd:+.4f} ({pnl_pct:+.2f}%) [{reason}]", "CLOSE")

        except Exception as e:
            log(f"Close failed: {e}", "ERROR")

    # ── Kill Switch ───────────────────────────────
    def activate_kill_switch(self):
        """Close all positions, halt trading"""
        log("🛑 KILL SWITCH ACTIVATED", "CRITICAL")
        self._kill_signalled = True

        open_positions = self.positions.all()
        total_pnl = 0

        for pos in open_positions:
            try:
                ticker = self.ex.fetch_ticker(pos["symbol"])
                price = ticker["last"]
                self._close_position(pos, price, "kill_switch")

                direction = 1 if pos["side"] == "long" else -1
                entry = pos["entry_price"]
                pnl = pos.get("size", 0) * (price - entry) * direction
                total_pnl += pnl
            except Exception as e:
                log(f"Kill close {pos['symbol']}: {e}", "ERROR")

        notify_kill(len(open_positions), total_pnl)
        self._save_state()

    def reset_kill_switch(self):
        """Re-enable trading"""
        self._kill_signalled = False
        self.risk.check_kill_switch("reset")
        self._save_state()
        log("Kill switch reset — trading resumes", "INFO")
        system_status("online", "VIPER trading resumed after kill switch reset.")

    # ── Backtest ──────────────────────────────────
    def run_backtest(self) -> dict:
        """Run automated backtest on all available data"""
        log("Running automated backtest...", "BACKTEST")

        # Ensure exchange is initialized for OHLCV fetching
        if not self._bybit:
            if not self._init_exchange():
                log("Exchange not available for backtest", "WARN")
                return {"error": "exchange_unavailable"}

        from viper_backtest import Backtester
        results = {}
        symbols = self.config.get("symbols", ["DOGEUSDT"])

        for raw_sym in symbols[:2]:  # Max 2 for speed
            sym = raw_sym[:-4] + "/USDT" if raw_sym.endswith("USDT") and "/" not in raw_sym else raw_sym
            df = self._fetch_ohlcv(sym, "4h", 500)
            if df.empty:
                continue

            # Supplement with WS real-time data if available
            if self._ws_started:
                try:
                    state = get_latest_state()
                    if state and "data" in state:
                        for sym_state in state.get("orderbook", []):
                            pass  # WS state available for context
                except Exception:
                    pass

            # Use aggressive config for backtest
            bt_cfg = dict(self.config)
            bt_cfg["timeframe"] = self.config.get("backtest_timeframe", "5m")
            bt_cfg["confidence_threshold"] = self.config.get("confidence_threshold", 0.55)
            bt_cfg["stop_loss_pct"] = self.config.get("stop_loss_pct", 12)
            bt_cfg["take_profit_pct"] = self.config.get("take_profit_pct", 25)
            bt_cfg["trailing_stop"] = self.config.get("trailing_stop", True)

            bt = Backtester(strategy_name="momentum", initial_balance=100.0, trade_size_pct=20.0)
            try:
                result = bt.run(df, bt_cfg)
                results[sym] = {
                    "trades": result.total_trades,
                    "win_rate": result.win_rate,
                    "sharpe": result.sharpe_ratio,
                    "profit_factor": result.profit_factor,
                    "max_drawdown": result.max_drawdown_pct,
                }
                log(f"  AGGR {sym}: {result.total_trades} trades, WR={result.win_rate:.1f}%, Sharpe={result.sharpe_ratio:.2f}", "BACKTEST")
            except Exception as e:
                log(f"  {sym} backtest error: {e}", "WARN")

        self.counter["backtests"] += 1
        return results

    # ── Main Cycle ────────────────────────────────
    def run_cycle(self) -> dict:
        """One complete trading cycle"""
        cycle_start = time.time()
        result = {
            "symbols_scanned": 0,
            "signals": 0,
            "trades": 0,
            "errors": 0,
        }

        # Check kill switch
        state = self._load_state()
        cb = self.risk.check_circuit_breaker(state)
        if cb.get("halted") or self._kill_signalled:
            log(f"Circuit breaker / kill active. Skipping cycle.", "RISK")
            return result

        # Check exchange
        if not self.exchange:
            if not self._init_exchange():
                return result

        # 1. Fetch balance
        bal = self._fetch_balance()
        log(f"Balance: ${bal['free']:.4f} free / ${bal['total']:.4f} total", "INFO")

        # 2. Scan symbols → signals
        signals = self._scan_symbols()
        result["symbols_scanned"] = len(self.config.get("symbols", []))
        result["signals"] = len(signals)

        # 2b. LLM validate signals (filter with inline mode)
        llm_val = self.config.get("llm_validation", True)
        if signals and llm_val:
            pre_count = len(signals)
            signals = self._llm_validate_signals(signals)
            dropped = pre_count - len(signals)
            if dropped:
                log(f"  LLM filtered {dropped}/{pre_count} signals", "LLM")

        # 3. Execute best signals
        max_trades = self.config.get("max_trades_per_cycle", 3)
        for sig in signals[:max_trades]:
            trade = self._execute_trade(sig)
            if trade:
                result["trades"] += 1

        # 4. Update open position monitoring
        if self.positions.count() > 0:
            log(f"Monitoring {self.positions.count()} open positions...", "MONITOR")

        # 5. Journal
        self.journal.log_cycle({
            "symbols_scanned": result["symbols_scanned"],
            "signals_generated": result["signals"],
            "trades_executed": result["trades"],
            "duration_sec": round(time.time() - cycle_start, 2),
        })

        # 6. Balance notification
        notify_balance(balance=bal["total"], positions=self.positions.count())

        # 7. State save
        self.counter["cycles"] += 1
        self._save_state()

        # 8. LLM market analysis (non-blocking, silent on fail)
        self._llm_market_analysis(signals, bal)

        duration = round(time.time() - cycle_start, 2)
        log(f"Cycle done: {result['symbols_scanned']} scanned, {result['signals']} signals, {result['trades']} trades [{duration}s]", "CYCLE")

        # 8b. Telegram cycle report (edit — no spam)
        cycle_report(
            scanned=result.get("symbols_scanned", 0),
            signals=result.get("signals", 0),
            trades=result.get("trades", 0),
            duration=duration,
            balance=bal.get("total", 0),
            errors=result.get("errors", 0),
        )

        # 8c. Positions list (edit — no spam, live positions)
        # Also: re-protect any positions missing SL/TP (safety net)
        try:
            exchange_positions = self._bybit.fetch_positions() if self._bybit else []
            positions_list(positions=exchange_positions, balance=bal.get("total", 0))
            # Safety: ensure all positions have SL/TP attached (no stale cleanup)
            self._protect_existing_positions(cleanup_stale=False)
        except Exception:
            positions_list(positions=[], balance=bal.get("total", 0))

        return result

    # ── 24/7 Loop ─────────────────────────────────
    def daemon_loop(self, interval_minutes: int | None = None):
        interval_minutes = interval_minutes or self.config.get("cycle_interval_minutes", 5)
        """Run infinite loop with cooldown between cycles"""
        log("🐍 VIPER Daemon v2 — Starting 24/7", "STARTUP")
        system_status("online", "VIPER Engine v2 starting.\\nWaiting for API keys to trade.")

        if not self._init_exchange():
            log("No exchange. Will retry each cycle.", "WARN")

        self._running = True

        # Start WebSocket real-time feed (background daemon thread)
        if not self._ws_started:
            try:
                start_feed(self.config.get("symbols", None))
                self._ws_started = True
                log("WebSocket feed started in background", "STARTUP")
            except Exception as e:
                log(f"WebSocket feed start failed: {e}", "WARN")

        while self._running:
            try:
                # Check if killed
                if self._kill_signalled:
                    log("Kill switch active. Idle...", "IDLE")
                    time.sleep(30)
                    continue

                # Re-init exchange if lost
                if not self.exchange:
                    self._init_exchange()
                    if not self.exchange:
                        log("No exchange keys. Sleeping 60s.", "WARN")
                        time.sleep(60)
                        continue

                # Cycle
                self.counter["cycles"] += 1
                log(f"═════ Cycle #{self.counter['cycles']} ═════", "CYCLE")
                self.run_cycle()

                # Auto-backtest every N cycles
                bt_interval = self.config.get("backtest_every_n_cycles", 4)
                if self.counter["cycles"] % bt_interval == 0:
                    self.run_backtest()

                # Daily PnL summary
                if self.counter["cycles"] % 96 == 0:  # ~24h
                    stats = self.journal.get_all_time_stats()
                    daily_summary(
                        trades=stats.get("total_trades", 0),
                        wins=stats.get("wins", 0),
                        pnl_usd=stats.get("total_pnl", 0),
                        balance=bal.get("total", 0) if (bal := self._fetch_balance()) else 0,
                        drawdown=stats.get("max_drawdown", 0),
                        sharpe=stats.get("sharpe", 0),
                        regime=self.config.get("regime", "auto"),
                    )

            except Exception as e:
                log(f"Cycle error: {e}", "ERROR")
                traceback.print_exc()
                result["errors"] += 1

            # Cooldown
            log(f"💤 Cooldown {interval_minutes}m...", "CYCLE")
            time.sleep(interval_minutes * 60)

    # ── Signal Handler ────────────────────────────
    def _handle_signal(self, signum, frame):
        log(f"Signal {signum} received. Graceful shutdown...", "SHUTDOWN")
        self._running = False
        self._save_state()
        # Stop WebSocket feed thread
        if self._ws_started:
            stop_feed()
            self._ws_started = False


# ═══════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════
def main():
    import sys
    engine = ViperEngine()

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "cycle":
            engine.run_cycle()
        elif cmd == "daemon":
            interval = int(sys.argv[2]) if len(sys.argv) > 2 else 15
            engine.daemon_loop(interval)
        elif cmd == "backtest":
            engine.run_backtest()
        elif cmd == "kill":
            engine.activate_kill_switch()
        elif cmd == "reset":
            engine.reset_kill_switch()
        elif cmd == "balance":
            import json
            bal = engine._fetch_balance()
            print(json.dumps(bal))
        else:
            print(f"Unknown: {cmd}. Commands: cycle, daemon, backtest, kill, reset, balance")
    else:
        # Single test cycle
        print("VIPER Engine v2 — single cycle test")
        if engine._init_exchange():
            engine.run_cycle()


if __name__ == "__main__":
    main()
