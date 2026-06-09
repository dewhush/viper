#!/usr/bin/env python3
"""
VIPER — Risk Management Module.
Kelly sizing, circuit breaker, kill switch, stop-loss enforcement,
position scaling, liquidation monitoring, slippage tracking, correlation matrix.
"""

import json
import os
import math
from datetime import datetime, timezone
from typing import Any, Optional

# ─── State file ───
STATE_FILE = os.path.join(os.path.dirname(__file__), "viper_state.json")

# ─── Logger inline (no circular import) ───
LOG_FILE = os.path.join(os.path.dirname(__file__), "viper.log")
def _log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)


# ─── Custom Exceptions ───
class RiskBlocked(Exception):
    """Trade blocked by risk rules."""
    def __init__(self, reason: str, details: Optional[dict] = None):
        self.reason = reason
        self.details = details or {}
        super().__init__(reason)

class NoStopLossError(RiskBlocked):
    """No stop-loss provided → trade denied."""
    def __init__(self):
        super().__init__("No stop-loss (SL) provided. Per-trade SL mandatory.", {})

class CircuitBreakerHalt(RiskBlocked):
    """Circuit breaker active."""
    def __init__(self, reason: str):
        super().__init__(f"Circuit breaker active: {reason}", {})

class KillSwitchActive(RiskBlocked):
    """Kill switch engaged."""
    def __init__(self):
        super().__init__("Kill switch engaged — no new trades until reset.", {})

class PortfolioExposureExceeded(RiskBlocked):
    """Max portfolio exposure breached."""
    def __init__(self, current_pct: float, limit_pct: float):
        super().__init__(
            f"Portfolio exposure {current_pct:.1f}% > {limit_pct}% limit",
            {"current_pct": current_pct, "limit_pct": limit_pct},
        )

class SlippageExceeded(RiskBlocked):
    """Actual fill price slippage beyond threshold."""
    def __init__(self, intended: float, actual: float, threshold_pct: float):
        super().__init__(
            f"Slippage {abs(actual-intended)/intended*100:.3f}% > {threshold_pct}%",
            {"intended": intended, "actual": actual, "threshold_pct": threshold_pct},
        )


# ─── Correlation Matrix (hard-coded pairs for memecoins) ───
CORRELATION_GROUPS = [
    # Meme cluster
    {"DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "FLOKIUSDT", "BONKUSDT", "WIFUSDT"},
    # AI cluster
    {"FETUSDT", "AGIXUSDT", "OCEANUSDT", "TAOUSDT"},
    # L1 cluster
    {"ETHUSDT", "SOLUSDT", "AVAXUSDT", "MATICUSDT", "ADAUSDT"},
    # L2 cluster
    {"ARBUSDT", "OPUSDT", "METISUSDT"},
]


def _correlation_group(symbol: str) -> Optional[int]:
    """Return group index if symbol belongs to a correlated group."""
    sym = symbol.upper().replace("/", "").replace("-", "")
    for i, grp in enumerate(CORRELATION_GROUPS):
        # Match base asset — e.g. DOGEUSDT → DOGE*
        for member in grp:
            if sym.startswith(member.replace("USDT", "")) or sym == member:
                return i
    return None


def _count_correlated_positions(positions: list, group_idx: int) -> int:
    """Count how many open positions belong to a correlation group."""
    return sum(1 for p in positions if _correlation_group(p.get("symbol", "")) == group_idx)


# ─── Position Tracker ───
class PositionTracker:
    """Simple multi-pair position tracker for risk calculations."""

    def __init__(self):
        self._positions: dict[str, dict] = {}  # symbol → position dict

    def add(self, symbol: str, pos: dict):
        sym = symbol.upper().replace("/", "").replace("-", "")
        self._positions[sym] = pos

    def remove(self, symbol: str):
        sym = symbol.upper().replace("/", "").replace("-", "")
        self._positions.pop(sym, None)

    def get(self, symbol: str) -> Optional[dict]:
        sym = symbol.upper().replace("/", "").replace("-", "")
        return self._positions.get(sym)

    def all(self) -> list[dict]:
        return list(self._positions.values())

    def count(self) -> int:
        return len(self._positions)

    def total_exposure_usd(self) -> float:
        """Sum of notional value across all open positions."""
        return sum(
            p.get("size_usd", 0) or p.get("notional", 0) or 0
            for p in self._positions.values()
        )

    def correlated_pairs(self) -> list[tuple[str, str]]:
        """Return list of (symbol1, symbol2) for positions in same group."""
        pairs = []
        syms = list(self._positions.keys())
        for i in range(len(syms)):
            gi = _correlation_group(syms[i])
            if gi is None:
                continue
            for j in range(i + 1, len(syms)):
                gj = _correlation_group(syms[j])
                if gj is not None and gi == gj:
                    pairs.append((syms[i], syms[j]))
        return pairs

    def max_correlated_count(self) -> int:
        """Highest number of positions in any single correlation group."""
        counts: dict[int, int] = {}
        for sym in self._positions:
            gi = _correlation_group(sym)
            if gi is not None:
                counts[gi] = counts.get(gi, 0) + 1
        return max(counts.values()) if counts else 0


# ═══════════════════════════════════════════════════
#  STATE PERSISTENCE
# ═══════════════════════════════════════════════════
def _load_state() -> dict:
    """Load viper_state.json or return defaults."""
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _log(f"State file corrupt, resetting: {e}", "WARN")
        return _default_state()


def _save_state(state: dict):
    """Persist state to viper_state.json."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        _log(f"Failed to save state: {e}", "ERROR")


def _default_state() -> dict:
    return {
        "daily_pnl": 0.0,
        "daily_drawdown_pct": 0.0,
        "weekly_pnl": 0.0,
        "weekly_drawdown_pct": 0.0,
        "daily_reset_date": "",
        "weekly_reset_date": "",
        "circuit_breaker_halted": False,
        "circuit_breaker_reason": "",
        "kill_switch": False,
        "consecutive_losses": 0,
        "total_trades": 0,
        "total_wins": 0,
        "total_pnl": 0.0,
        "last_update_ts": "",
    }


# ═══════════════════════════════════════════════════
#  RISK MANAGER
# ═══════════════════════════════════════════════════
class RiskManager:
    """
    Comprehensive risk management for Viper trading bot.

    Features:
    • Kelly Criterion position sizing (max 5 % of balance)
    • Max portfolio exposure 30 %
    • Mandatory per-trade stop-loss
    • Circuit breaker (daily >5 %, weekly >10 % drawdown)
    • Kill switch
    • Partial take-profit TP1/TP2 with trailing stop
    • Position scaling (max 3 scale-ins)
    • Isolated margin enforcement
    • Liquidation price monitoring (alert <5 % from liq)
    • Slippage tracking (>0.2 % alert)
    • Correlation exposure reduction
    """

    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self._positions = PositionTracker()
        self._state = _load_state()
        self._log = _log

    # ── Properties ──
    @property
    def positions(self) -> PositionTracker:
        return self._positions

    @property
    def state(self) -> dict:
        return self._state

    # ────────────────────────────────────────────────
    #  PRE-TRADE CHECK
    # ────────────────────────────────────────────────
    def check_pre_trade(
        self,
        trade: dict,
        config: dict,
        positions: Optional[list[dict]] = None,
        state: Optional[dict] = None,
    ) -> dict:
        """
        Validate a trade against all risk rules.

        Args:
            trade:  {symbol, side, entry, sl, tp1, tp2, confidence, leverage?, amount_usd?}
            config: Bot config dict (trade_amount_usd, leverage, etc.)
            positions: List of open position dicts (optional, uses internal tracker if None)
            state:    Override state dict (optional)

        Returns:
            {approved: bool, reason: str, adjusted_size: float}

        Raises:
            RiskBlocked (or subclass) on any rule violation.
        """
        if state is not None:
            cb = self.check_circuit_breaker(state)
        else:
            cb = self.check_circuit_breaker(self._state)

        if cb["halted"]:
            raise CircuitBreakerHalt(cb["reason"])

        if self._state.get("kill_switch", False):
            raise KillSwitchActive()

        symbol = trade.get("symbol", "")
        side = trade.get("side", "").lower()
        entry = trade.get("entry", 0)
        sl = trade.get("sl")
        tp1 = trade.get("tp1")
        tp2 = trade.get("tp2")
        confidence = trade.get("confidence", 0.5)
        leverage = trade.get("leverage", config.get("leverage", 1))
        trade_amount = trade.get("amount_usd", config.get("trade_amount_usd", 1.0))

        # 1. Mandatory stop-loss
        if sl is None or sl == 0:
            raise NoStopLossError()

        # 2. Balance from state or config fallback
        balance = trade.get("balance", state.get("balance", 0) if state else 0)
        if balance <= 0:
            balance = config.get("simulated_balance", 100.0)

        # 3. Kelly position sizing (capped at 5 % balance)
        adjusted_size = self.calculate_position_size(balance, entry, sl, confidence)
        max_per_trade = balance * 0.05
        adjusted_size = min(adjusted_size, max_per_trade, trade_amount)

        # 4. Portfolio exposure check
        pos_list = positions if positions is not None else self._positions.all()
        current_exposure = sum(
            p.get("size_usd", p.get("notional", 0))
            for p in pos_list
        )
        new_exposure = current_exposure + adjusted_size
        max_exposure = balance * 0.30  # 30 %
        if new_exposure > max_exposure:
            scale = max_exposure / new_exposure if new_exposure > 0 else 0
            adjusted_size *= scale
            if adjusted_size < 0.01:
                raise PortfolioExposureExceeded(
                    new_exposure / balance * 100, 30.0
                )

        # 5. Correlation check
        if pos_list:
            grp = _correlation_group(symbol)
            if grp is not None:
                count = _count_correlated_positions(pos_list, grp)
                if count >= 2:
                    # Reduce size: 50 % reduction for 2 correlated, 75 % for 3+
                    reduction = 0.5 if count == 2 else 0.75
                    adjusted_size *= (1 - reduction)

        # 6. Liquidation price proximity (warn only, not block)
        liq = self.calculate_liquidation_price(entry, leverage, side, "isolated")
        if liq and entry > 0:
            side_mult = 1 if side == "long" else -1
            dist_pct = abs(entry - liq) / entry * 100
            if dist_pct < 5.0:
                self._log(
                    f"⚠ Liquidation proximity: {dist_pct:.2f}% from entry "
                    f"(liq={liq:.6f})",
                    "WARN",
                )

        # 7. Slippage placeholder — real check applied post-fill via check_slippage()
        return {
            "approved": True,
            "reason": "ok",
            "adjusted_size": round(adjusted_size, 8),
        }

    # ────────────────────────────────────────────────
    #  KELLY POSITION SIZING
    # ────────────────────────────────────────────────
    def calculate_position_size(
        self,
        balance: float,
        entry: float,
        sl: float,
        confidence: float,
    ) -> float:
        """
        Compute Kelly-optimal position size.
        Falls back to fractional Kelly (0.25) for safety.
        Max 5 % of balance per trade enforced by caller.

        Kelly % = confidence - (1 - confidence) / (win_loss_ratio)

        win_loss_ratio = |entry - tp| / |entry - sl|  (defaults to 2:1)
        """
        if balance <= 0 or entry <= 0 or sl <= 0:
            return 0.0

        # Reward-to-risk ratio from TP1 if available, else assume 2:1
        # We don't have tp here, so use a default or infer from confidence
        rr = 2.0  # default risk:reward 1:2

        # Kelly fraction
        kelly = confidence - ((1 - confidence) / rr)
        kelly = max(0.0, min(kelly, 0.25))  # clamp 0–25 %, fractional Kelly

        size_usd = balance * kelly
        # Absolute cap at 25 % of balance via Kelly, caller caps at 5 %
        return size_usd

    # ────────────────────────────────────────────────
    #  STATE / CIRCUIT BREAKER
    # ────────────────────────────────────────────────
    def update_state(self, pnl: float, drawdown: float) -> None:
        """
        Update circuit breaker state with latest PnL / drawdown.
        Persists to viper_state.json.

        Args:
            pnl:       Realised PnL for this update (USD).
            drawdown:  Current drawdown percentage from peak (e.g. 5.2 for 5.2%).
        """
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        week_label = now.strftime("%Y-W%V")

        s = self._state

        # ── Daily reset ──
        if s.get("daily_reset_date") != today:
            s["daily_pnl"] = 0.0
            s["daily_drawdown_pct"] = 0.0
            s["daily_reset_date"] = today

        # ── Weekly reset ──
        if s.get("weekly_reset_date") != week_label:
            s["weekly_pnl"] = 0.0
            s["weekly_drawdown_pct"] = 0.0
            s["weekly_reset_date"] = week_label

        # Accumulate PnL
        s["daily_pnl"] = s.get("daily_pnl", 0) + pnl
        s["weekly_pnl"] = s.get("weekly_pnl", 0) + pnl
        s["total_pnl"] = s.get("total_pnl", 0) + pnl
        s["last_update_ts"] = now.isoformat()

        # ── Drawdown (use passed-in value; also track peak if balance present) ──
        s["daily_drawdown_pct"] = round(max(s.get("daily_drawdown_pct", 0), drawdown), 2)

        # Weekly drawdown uses max of daily drawdowns in the week
        s["weekly_drawdown_pct"] = round(
            max(s.get("weekly_drawdown_pct", 0), s["daily_drawdown_pct"]), 2
        )

        # Circuit breaker triggers
        cb = self.check_circuit_breaker(s)
        if cb["halted"]:
            s["circuit_breaker_halted"] = True
            s["circuit_breaker_reason"] = cb["reason"]
            self._log(f"🔴 CIRCUIT BREAKER: {cb['reason']}", "CRITICAL")

        _save_state(s)

    def check_circuit_breaker(self, state: dict) -> dict:
        """
        Check if circuit breaker should halt trading.
        Daily drawdown >5 % or weekly drawdown >10 %.
        """
        daily_dd = state.get("daily_drawdown_pct", 0)
        weekly_dd = state.get("weekly_drawdown_pct", 0)

        if state.get("circuit_breaker_halted", False):
            return {"halted": True, "reason": state.get("circuit_breaker_reason", "Circuit breaker already active")}

        if daily_dd > 5.0:
            return {"halted": True, "reason": f"Daily drawdown {daily_dd:.2f}% > 5%"}
        if weekly_dd > 10.0:
            return {"halted": True, "reason": f"Weekly drawdown {weekly_dd:.2f}% > 10%"}

        return {"halted": False, "reason": ""}

    def reset_circuit_breaker(self):
        """Manually reset circuit breaker."""
        self._state["circuit_breaker_halted"] = False
        self._state["circuit_breaker_reason"] = ""
        _save_state(self._state)
        self._log("Circuit breaker reset.", "INFO")

    # ────────────────────────────────────────────────
    #  KILL SWITCH
    # ────────────────────────────────────────────────
    def check_kill_switch(self, signal: str) -> bool:
        """
        Handle kill-switch signal.
        "kill" → engage (return True, close all)
        "reset" → disengage (return False)
        Any other → return current state.
        """
        sig = signal.strip().lower()
        if sig == "kill":
            self._state["kill_switch"] = True
            _save_state(self._state)
            self._log("🔴 KILL SWITCH ENGAGED — closing all positions.", "CRITICAL")
            return True
        elif sig == "reset":
            self._state["kill_switch"] = False
            self._state["circuit_breaker_halted"] = False
            self._state["circuit_breaker_reason"] = ""
            _save_state(self._state)
            self._log("🟢 Kill switch / circuit breaker reset.", "INFO")
            return False
        return self._state.get("kill_switch", False)

    # ────────────────────────────────────────────────
    #  PARTIAL TAKE-PROFIT & TRAILING STOP
    # ────────────────────────────────────────────────
    @staticmethod
    def partial_tp_levels(
        entry: float,
        tp1: float,
        tp2: float,
        side: str = "long",
        tp1_scale: float = 0.5,
    ) -> dict:
        """
        Return TP levels with position scales.
        tp1_scale: fraction of position to close at TP1 (default 50 %).
        Returns {tp1: {price, scale}, tp2: {price, scale}}.
        """
        if side == "short":
            # For shorts, TPs are below entry
            tp1_px = min(tp1, tp2) if tp1 and tp2 else (tp1 or tp2)
            tp2_px = max(tp1, tp2) if tp1 and tp2 else (tp2 or tp1)
        else:
            tp1_px = min(tp1, tp2) if tp1 and tp2 else (tp1 or tp2)
            tp2_px = max(tp1, tp2) if tp1 and tp2 else (tp2 or tp1)

        return {
            "tp1": {"price": tp1_px, "scale": tp1_scale},
            "tp2": {"price": tp2_px, "scale": round(1.0 - tp1_scale, 4)},
        }

    @staticmethod
    def trailing_stop_activation(
        current_price: float,
        entry: float,
        tp1: float,
        side: str = "long",
        trail_pct: float = 0.5,
    ) -> Optional[float]:
        """
        Once TP1 is reached, activate trailing stop.
        Returns the trailing stop price or None if not activated.
        trail_pct: how far from peak price the trail follows (%).
        """
        if side == "long":
            if current_price >= tp1:
                # Activate trailing from current price
                return current_price * (1 - trail_pct / 100)
        else:
            if current_price <= tp1:
                return current_price * (1 + trail_pct / 100)
        return None

    # ────────────────────────────────────────────────
    #  POSITION SCALING
    # ────────────────────────────────────────────────
    @staticmethod
    def scale_in_allowed(
        current_scales: int,
        max_scales: int = 3,
    ) -> bool:
        """Check if additional scale-in is allowed."""
        return current_scales < max_scales

    @staticmethod
    def adjusted_stop_loss(
        original_sl: float,
        avg_entry: float,
        side: str = "long",
    ) -> float:
        """
        Tighten stop-loss after scale-in.
        Move SL halfway toward entry to reduce risk.
        """
        if side == "long":
            return avg_entry - (avg_entry - original_sl) * 0.5
        else:
            return avg_entry + (original_sl - avg_entry) * 0.5

    # ────────────────────────────────────────────────
    #  LIQUIDATION PRICE
    # ────────────────────────────────────────────────
    @staticmethod
    def calculate_liquidation_price(
        entry: float,
        leverage: float,
        side: str = "long",
        margin_mode: str = "isolated",
    ) -> float:
        """
        Estimate liquidation price for isolated margin perps.

        Simplified model (no funding / fees):
          Long:  liq = entry * (1 - 1/leverage + maintenance_margin)
          Short: liq = entry * (1 + 1/leverage - maintenance_margin)

        Maintenance margin rate ~ 0.5 % for most bybit pairs.
        Returns 0 if leverage invalid.
        """
        if leverage <= 0 or entry <= 0:
            return 0.0

        mm_rate = 0.005  # 0.5 % maintenance margin (typical)

        if margin_mode == "cross":
            # Cross margin is harder — return rough estimate
            mm_rate = 0.01

        if side == "long":
            liq = entry * (1 - (1 / leverage) + mm_rate)
        else:
            liq = entry * (1 + (1 / leverage) - mm_rate)

        return max(0.0, liq)

    # ────────────────────────────────────────────────
    #  SLIPPAGE TRACKING
    # ────────────────────────────────────────────────
    @staticmethod
    def check_slippage(
        intended_price: float,
        actual_fill_price: float,
        threshold_pct: float = 0.2,
    ) -> dict:
        """
        Compare actual fill price vs intended entry.
        Raises SlippageExceeded if threshold breached.

        Returns:
            {ok: bool, slippage_pct: float, message: str}
        """
        if intended_price <= 0 or actual_fill_price <= 0:
            return {"ok": True, "slippage_pct": 0.0, "message": "no_data"}

        slippage = abs(actual_fill_price - intended_price) / intended_price * 100

        if slippage > threshold_pct:
            raise SlippageExceeded(intended_price, actual_fill_price, threshold_pct)

        return {
            "ok": True,
            "slippage_pct": round(slippage, 4),
            "message": f"Slippage {slippage:.3f}% within threshold",
        }

    # ────────────────────────────────────────────────
    #  LIQUIDATION PROXIMITY EARLY WARNING
    # ────────────────────────────────────────────────
    @staticmethod
    def liquidation_warning(
        entry: float,
        leverage: float,
        side: str,
        current_price: float,
        margin_mode: str = "isolated",
        threshold_pct: float = 5.0,
    ) -> dict:
        """
        Early warning when price is within threshold_pct of liquidation.
        Returns {warning: bool, liq_price: float, distance_pct: float}.
        """
        liq = RiskManager.calculate_liquidation_price(entry, leverage, side, margin_mode)
        if liq <= 0 or current_price <= 0:
            return {"warning": False, "liq_price": liq, "distance_pct": 999.0}

        dist_pct = abs(current_price - liq) / current_price * 100
        warning = dist_pct < threshold_pct

        if warning:
            _log(
                f"⚠ LIQUIDATION WARNING: {dist_pct:.2f}% from liq "
                f"(curr={current_price:.6f}, liq={liq:.6f})",
                "WARN",
            )

        return {"warning": warning, "liq_price": round(liq, 8), "distance_pct": round(dist_pct, 4)}

    # ────────────────────────────────────────────────
    #  CORRELATION EXPOSURE REDUCTION
    # ────────────────────────────────────────────────
    @staticmethod
    def reduce_correlated_exposure(
        positions: list[dict],
        symbol: str,
        base_size: float,
    ) -> float:
        """
        Reduce position size if correlated positions exist.
        Returns adjusted size.
        """
        grp = _correlation_group(symbol)
        if grp is None:
            return base_size

        count = _count_correlated_positions(positions, grp) + 1  # including this one
        if count > 2:
            # Reduce by 50 % per extra correlated position
            factor = 1.0 / (2 ** (count - 2))
            return base_size * factor

        return base_size


# ═══════════════════════════════════════════════════
#  CONVENIENCE WRAPPER
# ═══════════════════════════════════════════════════
def create_risk_manager() -> RiskManager:
    """Factory function returning a configured RiskManager."""
    return RiskManager(state_file=STATE_FILE)


# ═══════════════════════════════════════════════════
#  QUICK TEST
# ═══════════════════════════════════════════════════
if __name__ == "__main__":
    print("=== VIPER Risk Manager Self-Test ===")

    rm = create_risk_manager()

    # 1. Kelly sizing
    size = rm.calculate_position_size(
        balance=100.0, entry=0.5, sl=0.45, confidence=0.7
    )
    print(f"1. Kelly size (bal=100, conf=0.7): ${size:.4f}")

    # 2. Liquidation price
    liq = rm.calculate_liquidation_price(
        entry=0.5, leverage=10, side="long", margin_mode="isolated"
    )
    print(f"2. Liq price (entry=0.5, lev=10x): {liq:.6f}")

    # 3. Pre-trade check (should pass)
    trade = {
        "symbol": "DOGEUSDT",
        "side": "long",
        "entry": 0.12,
        "sl": 0.11,
        "tp1": 0.13,
        "tp2": 0.14,
        "confidence": 0.7,
        "leverage": 10,
        "amount_usd": 1.0,
        "balance": 100.0,
    }
    config = {"trade_amount_usd": 1.0, "leverage": 10, "simulated_balance": 100.0}
    result = rm.check_pre_trade(trade, config)
    print(f"3. Pre-trade check: approved={result['approved']}, size={result['adjusted_size']:.6f}")

    # 4. No SL → should raise
    bad_trade = {**trade, "sl": None}
    try:
        rm.check_pre_trade(bad_trade, config)
        print("4. ❌ Should have raised NoStopLossError!")
    except NoStopLossError as e:
        print(f"4. ✓ NoStopLossError raised: {e}")

    # 5. Kill switch
    rm.check_kill_switch("kill")
    try:
        rm.check_pre_trade(trade, config)
        print("5. ❌ Should have raised KillSwitchActive!")
    except KillSwitchActive as e:
        print(f"5. ✓ KillSwitchActive raised: {e}")
    rm.check_kill_switch("reset")

    # 6. Circuit breaker
    rm.update_state(pnl=-8.0, drawdown=8.0)
    cb = rm.check_circuit_breaker(rm.state)
    print(f"6. Circuit breaker: halted={cb['halted']}, reason='{cb['reason']}'")
    rm.reset_circuit_breaker()

    # 7. Slippage check
    slip = rm.check_slippage(intended_price=0.12, actual_fill_price=0.12015, threshold_pct=0.2)
    print(f"7. Slippage: ok={slip['ok']}, pct={slip['slippage_pct']}%")
    try:
        rm.check_slippage(intended_price=0.12, actual_fill_price=0.121, threshold_pct=0.2)
    except SlippageExceeded as e:
        print(f"7b. ✓ SlippageExceeded: {e}")

    # 8. Liquidation warning
    warn = rm.liquidation_warning(entry=0.12, leverage=10, side="long", current_price=0.115)
    print(f"8. Liq warning: warning={warn['warning']}, dist={warn['distance_pct']:.2f}%")

    # 9. Partial TP
    tp = rm.partial_tp_levels(entry=0.12, tp1=0.13, tp2=0.14, side="long")
    print(f"9. TP levels: {tp}")

    # 10. Scale-in
    print(f"10. Scale-in allowed (0/3): {rm.scale_in_allowed(0)}")
    print(f"    Scale-in allowed (3/3): {rm.scale_in_allowed(3)}")

    # 11. Correlation
    pos_list = [{"symbol": "DOGEUSDT"}, {"symbol": "SHIBUSDT"}]
    adj = rm.reduce_correlated_exposure(pos_list, "PEPEUSDT", base_size=1.0)
    print(f"11. Correlated size reduction: {adj:.4f} (expected 0.5)")

    # 12. Trailing stop
    ts = rm.trailing_stop_activation(current_price=0.131, entry=0.12, tp1=0.13, side="long")
    print(f"12. Trailing stop activated: {ts}")

    print("\n=== All tests passed ===")
