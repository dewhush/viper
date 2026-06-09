#!/usr/bin/env python3
"""
VIPER — Multi-Exchange Manager
Primary:    Bybit (spot, API keys from .env)
Fallbacks:  KuCoin → MEXC (public data, no API keys needed)
Auto-fallback on failure. Order execution on primary only.
"""

import os, sys, time
from datetime import datetime, timezone
from typing import Optional, Any

BASE_DIR = os.path.dirname(__file__)
LOG_FILE = os.path.join(BASE_DIR, "viper.log")


def _log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] [EXCH] {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)


class ExchangeManager:
    """Multi-exchange manager with automatic fallback for public data.

    Public methods (fetch_ohlcv, fetch_ticker, fetch_order_book):
        Try Bybit → KuCoin → MEXC, first success wins. Silent fallback on failure.
    Private methods (create_order, fetch_balance, etc.):
        Primary/Bybit only — no cross-exchange order routing.
    """

    def __init__(self, env: Optional[dict] = None, config: Optional[dict] = None):
        self.env = env or {}
        self.config = config or {}
        self.bybit = None   # Raw CCXT Bybit instance (primary, w/ API keys)
        self.kucoin = None  # Raw CCXT KuCoin instance (fallback, public)
        self.mexc = None    # Raw CCXT MEXC instance (fallback, public)
        self._active = {}   # symbol (str) -> exchange instance
        self._init_exchanges()

    # ── Init ──────────────────────────────────────
    def _init_exchanges(self):
        import ccxt

        api_key = self.env.get("VIPER_API_KEY", "") or self.config.get("api_key", "")
        api_secret = self.env.get("VIPER_API_SECRET", "") or self.config.get("api_secret", "")
        testnet = str(self.env.get("VIPER_TESTNET", "")).lower() in ("true", "1", "yes")

        # -- Primary: Bybit --
        bybit_cfg = {
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {
                "defaultType": "swap",   # USDT perpetual — lower minimums for $1 challenge
                "adjustForTimeDifference": True,
            },
        }
        if api_key and api_secret:
            bybit_cfg["apiKey"] = api_key
            bybit_cfg["secret"] = api_secret

        try:
            self.bybit = ccxt.bybit(bybit_cfg)
            if testnet:
                self.bybit.set_sandbox_mode(True)
            self.bybit.load_markets()
            _log("Bybit primary exchange ready", "STARTUP")
        except Exception as e:
            _log(f"Bybit init failed: {e}", "WARN")
            self.bybit = None

        # -- Fallback: KuCoin --
        try:
            self.kucoin = ccxt.kucoin({
                "enableRateLimit": True,
                "timeout": 30000,
            })
            _log("KuCoin fallback exchange ready", "STARTUP")
        except Exception as e:
            _log(f"KuCoin init failed: {e}", "WARN")
            self.kucoin = None

        # -- Fallback: MEXC --
        try:
            self.mexc = ccxt.mexc({
                "enableRateLimit": True,
                "timeout": 30000,
            })
            _log("MEXC fallback exchange ready", "STARTUP")
        except Exception as e:
            _log(f"MEXC init failed: {e}", "WARN")
            self.mexc = None

        if not any([self.bybit, self.kucoin, self.mexc]):
            _log("No exchanges available!", "CRITICAL")

    # ── Internal fallback dispatcher ──────────────
    def _public_exchanges(self):
        """Return [Bybit, KuCoin, MEXC] — only available ones, in priority order."""
        out = []
        if self.bybit:
            out.append(self.bybit)
        if self.kucoin:
            out.append(self.kucoin)
        if self.mexc:
            out.append(self.mexc)
        return out

    def _try_public(self, method: str, symbol: str, *args, **kwargs):
        """Try *method* on each available exchange. Returns first success.
        Logs fallback at DEBUG level. Raises on total failure."""
        last_err = None
        for ex in self._public_exchanges():
            try:
                fn = getattr(ex, method, None)
                if fn is None:
                    continue
                result = fn(symbol, *args, **kwargs)
                self._active[symbol] = ex
                return result
            except Exception as e:
                _log(f"  {ex.id}.{method}({symbol}): {e}", "DEBUG")
                last_err = e
        raise RuntimeError(f"All exchanges failed for {method}({symbol})") from last_err

    # ═══ Public methods (auto-fallback) ═══

    def fetch_ohlcv(self, symbol: str, timeframe: str = "15m", limit: int = 150) -> list:
        """Fetch OHLCV candles. Falls back through available exchanges."""
        return self._try_public("fetch_ohlcv", symbol, timeframe, limit=limit)

    def fetch_ticker(self, symbol: str) -> dict:
        """Fetch ticker. Falls back through available exchanges."""
        return self._try_public("fetch_ticker", symbol)

    def fetch_order_book(self, symbol: str, limit: int = 10) -> dict:
        """Fetch order book. Falls back through available exchanges."""
        return self._try_public("fetch_order_book", symbol, limit)

    # ═══ Private methods (primary/Bybit only) ═══

    def fetch_balance(self) -> dict:
        """Fetch wallet balance. Bybit only. Returns empty dict on failure."""
        if not self.bybit:
            _log("Bybit unavailable for fetch_balance", "ERROR")
            return {"info": {}, "free": {}, "used": {}, "total": {}}
        try:
            return self.bybit.fetch_balance()
        except Exception as e:
            _log(f"fetch_balance failed: {e}", "ERROR")
            return {"info": {}, "free": {}, "used": {}, "total": {}}

    def create_order(self, symbol: str, type: str, side: str, amount: float,
                     price: Optional[float] = None, params: Optional[dict] = None) -> dict:
        """Place order. Bybit only — no fallback (don't send orders to wrong exchange)."""
        if not self.is_ready:
            raise RuntimeError("Bybit not ready for trading (missing API keys)")
        try:
            adjusted = self.bybit.amount_to_precision(symbol, amount)
        except Exception:
            adjusted = round(amount, 6)
        return self.bybit.create_order(symbol, type, side, float(adjusted), price, params or {})

    def create_limit_order(self, symbol: str, side: str, amount: float,
                           price: float, params: Optional[dict] = None) -> dict:
        """Place limit order. Bybit only — precision-adjusted."""
        if not self.is_ready:
            raise RuntimeError("Bybit not ready for trading (missing API keys)")
        try:
            adjusted = self.bybit.amount_to_precision(symbol, amount)
        except Exception:
            adjusted = round(amount, 6)
        return self.bybit.create_limit_order(symbol, side, float(adjusted), price, params or {})

    def create_market_order(self, symbol: str, side: str, amount: float,
                            params: Optional[dict] = None) -> dict:
        """Place market order. Bybit only — precision-adjusted."""
        if not self.is_ready:
            raise RuntimeError("Bybit not ready for trading (missing API keys)")
        try:
            adjusted = self.bybit.amount_to_precision(symbol, amount)
        except Exception:
            adjusted = round(amount, 6)
        return self.bybit.create_market_order(symbol, side, float(adjusted), params or {})

    def create_stop_loss_order(self, symbol: str, type: str, side: str, amount: float,
                                stopLossPrice: float, params: Optional[dict] = None) -> dict:
        """Place stop-loss order. Bybit only — precision-adjusted."""
        if not self.is_ready:
            raise RuntimeError("Bybit not ready for trading (missing API keys)")
        try:
            adjusted = self.bybit.amount_to_precision(symbol, amount)
        except Exception:
            adjusted = round(amount, 6)
        return self.bybit.create_stop_loss_order(
            symbol, type, side, float(adjusted),
            stopLossPrice=stopLossPrice, params=params or {}
        )

    def create_take_profit_order(self, symbol: str, type: str, side: str, amount: float,
                                  price: Optional[float] = None, takeProfitPrice: Optional[float] = None,
                                  params: Optional[dict] = None) -> dict:
        """Place take-profit order. Bybit only — precision-adjusted."""
        if not self.is_ready:
            raise RuntimeError("Bybit not ready for trading (missing API keys)")
        try:
            adjusted = self.bybit.amount_to_precision(symbol, amount)
        except Exception:
            adjusted = round(amount, 6)
        return self.bybit.create_take_profit_order(
            symbol, type, side, float(adjusted),
            price=price, takeProfitPrice=takeProfitPrice, params=params or {}
        )

    def fetch_open_orders(self, symbol: Optional[str] = None) -> list:
        """Fetch open orders. Bybit only. Returns [] on failure."""
        if not self.bybit:
            _log("Bybit unavailable for fetch_open_orders", "ERROR")
            return []
        try:
            return self.bybit.fetch_open_orders(symbol)
        except Exception as e:
            _log(f"fetch_open_orders failed: {e}", "ERROR")
            return []

    def cancel_order(self, id: str, symbol: str) -> dict:
        """Cancel order. Bybit only. Returns {} on failure."""
        if not self.bybit:
            _log("Bybit unavailable for cancel_order", "ERROR")
            return {}
        try:
            return self.bybit.cancel_order(id, symbol)
        except Exception as e:
            _log(f"cancel_order failed: {e}", "ERROR")
            return {}

    def fetch_position(self, symbol: str) -> Optional[dict]:
        """Fetch position. Bybit only. Returns None on failure."""
        if not self.bybit:
            _log("Bybit unavailable for fetch_position", "ERROR")
            return None
        try:
            return self.bybit.fetch_position(symbol)
        except Exception as e:
            _log(f"fetch_position({symbol}): {e}", "DEBUG")
            return None

    # ═══ Status helpers ═══

    def get_active_exchange(self, symbol: str) -> Optional[str]:
        """Return exchange ID last successfully used for this symbol's public data."""
        ex = self._active.get(symbol)
        return ex.id if ex else None

    @property
    def is_ready(self) -> bool:
        """True if Bybit primary exchange is available for trading (has API keys)."""
        if not self.bybit:
            return False
        return bool(self.bybit.apiKey and self.bybit.secret)

    @property
    def available_exchanges(self) -> list:
        """List of exchange IDs currently available."""
        return [ex.id for ex in self._public_exchanges()]


# ── Quick self-test ──────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))

    em = ExchangeManager(env=os.environ)
    print(f"Avail exchanges: {em.available_exchanges}")
    print(f"Bybit ready: {em.is_ready}")

    try:
        t = em.fetch_ticker("BTC/USDT")
        print(f"BTC/USDT ticker: ${t.get('last', '?')}")
        print(f"  Active exchange: {em.get_active_exchange('BTC/USDT')}")
    except Exception as e:
        print(f"Ticker fail: {e}")

    try:
        ob = em.fetch_order_book("BTC/USDT", 5)
        print(f"Order book: {len(ob.get('bids', []))} bids, {len(ob.get('asks', []))} asks")
    except Exception as e:
        print(f"Order book fail: {e}")

    try:
        ohlcv = em.fetch_ohlcv("BTC/USDT", "1h", 5)
        print(f"OHLCV: {len(ohlcv)} candles")
    except Exception as e:
        print(f"OHLCV fail: {e}")
