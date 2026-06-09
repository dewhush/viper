#!/usr/bin/env python3
"""
VIPER WebSocket Price Feed — Real-time market data via Bybit v5 Public WebSocket
==================================================================================
Provides low-latency orderbook (200 levels) + trade stream for signal detection.

Integration API:
    from viper_ws import start_feed, get_latest_state, get_pending_signals

    start_feed()                        # starts background daemon thread
    state = get_latest_state()          # dict snapshot — call from engine
    signals = get_pending_signals()     # consume alert signals

Standalone:
    python3 viper_ws.py                 # runs as standalone daemon process
"""

import os, sys, json, time, asyncio, threading, signal as sig_mod
import traceback
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from collections import deque

try:
    import websockets
except ImportError:
    print("FATAL: websockets not installed. Run: pip3 install --break-system-packages websockets")
    sys.exit(1)

# ─── Paths ───
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_FILE    = os.path.join(BASE_DIR, "viper-ws.log")
STATE_FILE  = os.path.join(BASE_DIR, "viper_ws_state.json")

# ─── Bybit WebSocket ───
WS_URL = "wss://stream.bybit.com/v5/public/spot"

# ─── Signal thresholds ───
FAST_MOVE_PCT       = 2.0    # % price change to trigger alert (aggressive: lowered from 3.0)
FAST_MOVE_WINDOW    = 60     # seconds lookback window
IMBALANCE_THRESHOLD = 0.65   # 65%+ volume on one side (aggressive: lowered from 0.70)
IMBALANCE_LEVELS    = 20     # top-N book levels to evaluate

# ─── Reconnect tuning ───
INITIAL_RECONNECT_DELAY = 2     # seconds
MAX_RECONNECT_DELAY     = 60    # cap
PING_INTERVAL           = 20    # Bybit requires < 30 s keepalive
PING_TIMEOUT            = 10    # pong deadline

# ─── Max trade buffer per symbol ───
MAX_TRADE_BUFFER = 5000


# ═══════════════════════════════════════════════════
#  LOGGER
# ═══════════════════════════════════════════════════
def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [WS-{level}] {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line)


# ═══════════════════════════════════════════════════
#  SHARED STATE  (thread-safe between WS thread ↔ engine)
# ═══════════════════════════════════════════════════
class SharedState:
    """Lock-protected dict shared between the WS daemon thread and the engine."""

    def __init__(self):
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {
            "connected":       False,
            "last_update":     None,
            "started_at":      datetime.now(timezone.utc).isoformat(),
            "symbols":         {},
            "signals":         [],
            "reconnect_count": 0,
            "messages_rx":     0,
        }

    def set(self, key: str, value):
        with self._lock:
            self._data[key] = value

    def set_symbol(self, symbol: str, key: str, value):
        with self._lock:
            syms = self._data["symbols"]
            if symbol not in syms:
                syms[symbol] = {}
            syms[symbol][key] = value

    def push_signal(self, signal_dict: dict):
        with self._lock:
            q = self._data["signals"]
            if len(q) >= 200:
                self._data["signals"] = q[-100:]
            self._data["signals"].append(signal_dict)

    def pop_signals(self) -> list:
        """Atomically drain and return all queued signals."""
        with self._lock:
            out = self._data["signals"]
            self._data["signals"] = []
            return out

    def snapshot(self) -> dict:
        """Deep-copy current state (JSON round-trip for safety)."""
        with self._lock:
            return json.loads(json.dumps(self._data))

    def inc(self, key: str, delta: int = 1):
        with self._lock:
            self._data[key] = self._data.get(key, 0) + delta


# Module-level singleton
_state = SharedState()


# ═══════════════════════════════════════════════════
#  PUBLIC API  (called by viper_engine.py)
# ═══════════════════════════════════════════════════
def get_latest_state() -> dict:
    """
    Return a full snapshot of real-time market data.

    Structure:
        {
          "connected": bool,
          "last_update": ISO timestamp,
          "messages_rx": int,
          "reconnect_count": int,
          "signals": [ ... ],           # unconsumed alert signals
          "symbols": {
              "PEPEUSDT": {
                  "orderbook": {
                      "best_bid", "best_ask", "spread", "spread_pct",
                      "mid_price", "bid_depth", "ask_depth", "total_depth",
                      "imbalance": float 0-1, "imbalance_side": "bid"|"ask"|"neutral",
                      "levels": int, "update_id": int
                  },
                  "trades": {
                      "last_price", "volume_1m", "trades_1m",
                      "fast_move_pct": float, "first_price"
                  }
              },
              ...
          }
        }
    """
    return _state.snapshot()


def get_pending_signals() -> list:
    """
    Drain and return all queued alert signals.
    Each: { symbol, type, value, message, timestamp, data }
    """
    return _state.pop_signals()


def is_running() -> bool:
    """True if the WS daemon thread is alive."""
    return _ws_thread is not None and _ws_thread.is_alive()


# ═══════════════════════════════════════════════════
#  ORDERBOOK MANAGER
# ═══════════════════════════════════════════════════
class OrderBookManager:
    """
    Maintains a local orderbook cache per symbol.
    Bybit v5 sends an initial snapshot followed by incremental deltas.
    """

    def __init__(self):
        self._books: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def init_book(self, symbol: str):
        with self._lock:
            self._books[symbol] = {
                "bids": {},          # price_str → qty (float)
                "asks": {},
                "bids_sorted": [],   # [[price, qty], ...] descending
                "asks_sorted": [],   # [[price, qty], ...] ascending
                "update_id": 0,
                "seq": 0,
                "last_update": None,
            }

    # ── snapshot ──
    def apply_snapshot(self, symbol: str, bids: list, asks: list,
                       update_id: int, seq: int = 0):
        with self._lock:
            if symbol not in self._books:
                self.init_book(symbol)
            b = self._books[symbol]
            b["bids"] = {p: float(q) for p, q in bids if float(q) > 0}
            b["asks"] = {p: float(q) for p, q in asks if float(q) > 0}
            b["update_id"] = update_id
            b["seq"] = seq
            b["last_update"] = time.time()
            self._rebuild_sorted(symbol)

    # ── delta ──
    def apply_delta(self, symbol: str, bids: list, asks: list,
                    update_id: int, seq: int = 0):
        with self._lock:
            if symbol not in self._books:
                return  # missed snapshot — will resubscribe
            b = self._books[symbol]
            for p, q in bids:
                qf = float(q)
                if qf > 0:
                    b["bids"][p] = qf
                else:
                    b["bids"].pop(p, None)
            for p, q in asks:
                qf = float(q)
                if qf > 0:
                    b["asks"][p] = qf
                else:
                    b["asks"].pop(p, None)
            b["update_id"] = update_id
            b["seq"] = seq
            b["last_update"] = time.time()
            self._rebuild_sorted(symbol)

    def _rebuild_sorted(self, symbol: str):
        """Rebuild sorted arrays. MUST hold _lock."""
        b = self._books[symbol]
        b["bids_sorted"] = sorted(
            ([float(p), q] for p, q in b["bids"].items()), reverse=True
        )
        b["asks_sorted"] = sorted(
            ([float(p), q] for p, q in b["asks"].items())
        )

    def get_summary(self, symbol: str) -> Optional[dict]:
        """
        Compute a compact orderbook summary for shared state / signals.
        """
        with self._lock:
            if symbol not in self._books:
                return None
            b = self._books[symbol]
            bids = b["bids_sorted"]
            asks = b["asks_sorted"]

            if not bids or not asks:
                return {
                    "best_bid": 0, "best_ask": 0, "spread": 0,
                    "spread_pct": 0, "mid_price": 0,
                    "bid_depth": 0, "ask_depth": 0, "total_depth": 0,
                    "imbalance": 0.5, "imbalance_side": "neutral",
                    "levels": 0, "update_id": 0, "stale": True,
                }

            best_bid = bids[0][0]
            best_ask = asks[0][0]
            mid      = (best_bid + best_ask) / 2
            spread   = best_ask - best_bid

            n = IMBALANCE_LEVELS
            bid_vol = sum(q for _, q in bids[:n])
            ask_vol = sum(q for _, q in asks[:n])
            total   = bid_vol + ask_vol
            ratio   = bid_vol / total if total > 0 else 0.5

            if ratio >= IMBALANCE_THRESHOLD:
                side = "bid"
            elif (1 - ratio) >= IMBALANCE_THRESHOLD:
                side = "ask"
            else:
                side = "neutral"

            stale = (time.time() - (b["last_update"] or 0)) > 30

            return {
                "best_bid":       best_bid,
                "best_ask":       best_ask,
                "spread":         spread,
                "spread_pct":     round(spread / mid * 100, 4) if mid else 0,
                "mid_price":      mid,
                "bid_depth":      round(bid_vol, 4),
                "ask_depth":      round(ask_vol, 4),
                "total_depth":    round(total, 4),
                "imbalance":      round(ratio, 4),
                "imbalance_side": side,
                "levels":         len(bids) + len(asks),
                "update_id":      b["update_id"],
                "stale":          stale,
            }

    def reset(self, symbol: str):
        """Wipe book for resync after disconnect."""
        with self._lock:
            self.init_book(symbol)


# ═══════════════════════════════════════════════════
#  TRADE TRACKER
# ═══════════════════════════════════════════════════
class TradeTracker:
    """Rolling window of recent trades for fast-move detection."""

    def __init__(self, window_sec: int = FAST_MOVE_WINDOW):
        self._buf: Dict[str, deque] = {}
        self._lock = threading.Lock()
        self._window = window_sec

    def record(self, symbol: str, price: float, qty: float,
               side: str, ts_ms: int):
        with self._lock:
            if symbol not in self._buf:
                self._buf[symbol] = deque(maxlen=MAX_TRADE_BUFFER)
            self._buf[symbol].append({
                "p": price, "q": qty, "s": side,
                "t": ts_ms / 1000.0, "r": time.time(),
            })

    def get_summary(self, symbol: str) -> Optional[dict]:
        with self._lock:
            if symbol not in self._buf or not self._buf[symbol]:
                return None
            buf  = self._buf[symbol]
            now  = time.time()
            cut  = now - self._window
            win  = [t for t in buf if t["r"] >= cut]

            last = buf[-1]
            if not win:
                return {
                    "last_price": last["p"], "volume_1m": 0,
                    "trades_1m": 0, "fast_move_pct": 0.0,
                    "first_price": last["p"], "window_sec": self._window,
                }

            last_p  = win[-1]["p"]
            first_p = win[0]["p"]
            vol_usd = sum(t["q"] * t["p"] for t in win)
            move    = ((last_p - first_p) / first_p * 100) if first_p else 0

            return {
                "last_price":    last_p,
                "volume_1m":     round(vol_usd, 2),
                "trades_1m":     len(win),
                "fast_move_pct": round(move, 4),
                "first_price":   first_p,
                "window_sec":    self._window,
            }


# ═══════════════════════════════════════════════════
#  SIGNAL DETECTOR
# ═══════════════════════════════════════════════════
class SignalDetector:
    """Evaluates real-time conditions per symbol; pushes alerts to SharedState."""

    COOLDOWN = 30  # seconds between repeated alerts of the same type+symbol

    def __init__(self, ob: OrderBookManager, tt: TradeTracker):
        self.ob = ob
        self.tt = tt
        self._last: Dict[str, float] = {}

    def evaluate(self, symbol: str):
        now = time.time()
        self._check_fast_move(symbol, now)
        self._check_imbalance(symbol, now)

    def _check_fast_move(self, symbol: str, now: float):
        ts = self.tt.get_summary(symbol)
        if not ts:
            return
        move = abs(ts["fast_move_pct"])
        if move < FAST_MOVE_PCT:
            return
        key = f"{symbol}:fast_move"
        if now - self._last.get(key, 0) < self.COOLDOWN:
            return
        direction = "📈 UP" if ts["fast_move_pct"] > 0 else "📉 DOWN"
        alert = {
            "symbol":    symbol,
            "type":      "fast_move",
            "value":     ts["fast_move_pct"],
            "message":   (f"🚨 {symbol} moved {ts['fast_move_pct']:+.2f}% "
                          f"in {FAST_MOVE_WINDOW}s ({direction})"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data":      ts,
        }
        _state.push_signal(alert)
        self._last[key] = now
        log(alert["message"], "SIGNAL")

    def _check_imbalance(self, symbol: str, now: float):
        obs = self.ob.get_summary(symbol)
        if not obs or obs["imbalance_side"] == "neutral":
            return
        key = f"{symbol}:imbalance"
        if now - self._last.get(key, 0) < self.COOLDOWN:
            return
        side = obs["imbalance_side"].upper()
        pct  = (obs["imbalance"] * 100
                if side == "BID"
                else (1 - obs["imbalance"]) * 100)
        alert = {
            "symbol":    symbol,
            "type":      "imbalance",
            "value":     obs["imbalance"],
            "message":   (f"📊 {symbol} orderbook {side}-heavy: "
                          f"{pct:.1f}% depth on {side} side "
                          f"({obs['levels']} levels)"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data":      obs,
        }
        _state.push_signal(alert)
        self._last[key] = now
        log(alert["message"], "SIGNAL")


# ═══════════════════════════════════════════════════
#  WEBSOCKET CLIENT  (async)
# ═══════════════════════════════════════════════════
class ViperWSClient:
    """
    Async Bybit v5 public-spot WebSocket client.
    Handles subscribe → stream → detect → reconnect.
    """

    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self.ob      = OrderBookManager()
        self.tt      = TradeTracker()
        self.det     = SignalDetector(self.ob, self.tt)
        self._ws     = None
        self._running       = False
        self._reconnect_n   = 0
        self._stop_event    = asyncio.Event()

        for sym in symbols:
            self.ob.init_book(sym)

    # ── lifecycle ──
    def request_stop(self):
        self._stop_event.set()
        self._running = False

    async def run(self):
        """Main reconnect loop."""
        self._running = True
        _state.set("connected", False)

        while self._running and not self._stop_event.is_set():
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                log("WebSocket task cancelled", "SHUTDOWN")
                break
            except Exception as exc:
                log(f"WebSocket error: {exc}", "ERROR")
                traceback.print_exc()

            if not self._running:
                break

            # Exponential backoff reconnect
            _state.set("connected", False)
            self._reconnect_n += 1
            _state.set("reconnect_count", self._reconnect_n)
            delay = min(
                INITIAL_RECONNECT_DELAY * (2 ** (self._reconnect_n - 1)),
                MAX_RECONNECT_DELAY,
            )
            log(f"Reconnecting in {delay:.0f}s "
                f"(attempt #{self._reconnect_n})...", "RECONNECT")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                break  # stop requested during sleep
            except asyncio.TimeoutError:
                pass     # timeout → retry

        _state.set("connected", False)
        _state.set("last_update", datetime.now(timezone.utc).isoformat())
        log("WebSocket client fully stopped", "SHUTDOWN")

    # ── connection ──
    async def _connect_and_stream(self):
        log(f"Connecting to {WS_URL}...", "CONNECT")

        async with websockets.connect(
            WS_URL,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            close_timeout=5,
            max_size=2 ** 22,          # 4 MB — orderbook snapshots can be large
        ) as ws:
            self._ws          = ws
            self._reconnect_n = 0
            _state.set("connected", True)
            _state.set("reconnect_count", 0)
            log(f"✅ Connected — subscribing to "
                f"{len(self.symbols)} symbols × 2 channels", "CONNECT")

            # ── subscribe ──
            args = []
            for sym in self.symbols:
                args.append(f"orderbook.200.{sym}")
                args.append(f"publicTrade.{sym}")

            await ws.send(json.dumps({"op": "subscribe", "args": args}))

            # Wait for confirmation (non-fatal if it times out)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                resp = json.loads(raw)
                if resp.get("success"):
                    log(f"Subscription confirmed ✅  ({len(args)} channels)",
                        "SUBSCRIBE")
                else:
                    log(f"Subscription NACK: {resp}", "WARN")
            except asyncio.TimeoutError:
                log("No subscription confirmation within 10 s", "WARN")

            # Reset books so we treat next snapshot as fresh
            for sym in self.symbols:
                self.ob.reset(sym)

            # ── message pump ──
            async for raw_msg in ws:
                if self._stop_event.is_set():
                    break
                log(f"RAW msg received ({len(raw_msg)}b)", "TRACE")
                try:
                    self._dispatch(raw_msg)
                except Exception as exc:
                    log(f"Dispatch error: {exc} raw={raw_msg[:100]}", "ERROR")
                    traceback.print_exc()

    # ── message dispatch ──
    def _dispatch(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Ignore keepalive
        if msg.get("op") in ("pong", "ping"):
            return
        # Subscription ack already handled
        if msg.get("op") == "subscribe":
            return

        topic = msg.get("topic", "")
        data  = msg.get("data")
        mtype = msg.get("type", "")

        if not data:
            return

        _state.inc("messages_rx")

        # ── Orderbook ──
        if topic.startswith("orderbook."):
            symbol    = data.get("s", "")
            bids      = data.get("b", [])
            asks      = data.get("a", [])
            update_id = data.get("u", 0)
            seq       = data.get("seq", 0)

            if mtype == "snapshot":
                self.ob.apply_snapshot(symbol, bids, asks, update_id, seq)
                log(f"  📖 {symbol} snapshot: "
                    f"{len(bids)}B / {len(asks)}A  u={update_id}", "OB")
            elif mtype == "delta":
                self.ob.apply_delta(symbol, bids, asks, update_id, seq)

            summary = self.ob.get_summary(symbol)
            if summary:
                _state.set_symbol(symbol, "orderbook", summary)
                _state.set_symbol(symbol, "mid_price", summary["mid_price"])
                log(f"  {symbol} mid={summary['mid_price']:.8f} imb={summary['imbalance_side']}", "OB-SUMMARY")

            # Evaluate imbalance after every book update
            self.det.evaluate(symbol)

        # ── Public trade ──
        elif topic.startswith("publicTrade."):
            if not isinstance(data, list):
                data = [data]
            for t in data:
                self.tt.record(
                    symbol = t.get("s", ""),
                    price  = float(t.get("p", 0)),
                    qty    = float(t.get("v", 0)),
                    side   = t.get("S", ""),
                    ts_ms  = int(t.get("T", 0)),
                )

            if data:
                last_sym = data[-1].get("s", "")
                ts = self.tt.get_summary(last_sym)
                if ts:
                    _state.set_symbol(last_sym, "trades", ts)
                # Evaluate fast-move after trade batch
                self.det.evaluate(last_sym)

        # Timestamp heartbeat
        _state.set("last_update", datetime.now(timezone.utc).isoformat())


# ═══════════════════════════════════════════════════
#  THREAD WRAPPER  (daemon thread hosting the asyncio loop)
# ═══════════════════════════════════════════════════
_ws_client: Optional[ViperWSClient] = None
_ws_thread: Optional[threading.Thread] = None
_shutdown   = threading.Event()


def _thread_target(symbols: List[str]):
    """Entry point for the daemon thread."""
    global _ws_client

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _ws_client = ViperWSClient(symbols)

    try:
        loop.run_until_complete(_ws_client.run())
    except Exception as exc:
        log(f"WS loop fatal: {exc}", "CRITICAL")
        traceback.print_exc()
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        log("WS asyncio event loop closed", "SHUTDOWN")


def start_feed(symbols: Optional[List[str]] = None) -> threading.Thread:
    """
    PUBLIC API — Launch the WebSocket feed in a background daemon thread.
    Safe to call multiple times; no-ops if already running.
    """
    global _ws_thread

    if _ws_thread and _ws_thread.is_alive():
        log("WS feed already running", "WARN")
        return _ws_thread

    if symbols is None:
        try:
            with open(CONFIG_FILE) as f:
                symbols = json.load(f).get(
                    "symbols",
                    ["PEPEUSDT", "DOGEUSDT", "WIFUSDT", "SHIBUSDT"],
                )
        except Exception:
            symbols = ["PEPEUSDT", "DOGEUSDT", "WIFUSDT", "SHIBUSDT"]

    log(f"🐍 WS feed starting: {', '.join(symbols)}", "STARTUP")

    _ws_thread = threading.Thread(
        target=_thread_target,
        args=(symbols,),
        daemon=True,
        name="viper-ws",
    )
    _ws_thread.start()
    _shutdown.clear()
    return _ws_thread


def stop_feed(timeout: float = 10.0):
    """PUBLIC API — Gracefully shut down the WS feed."""
    global _ws_client

    if _ws_client:
        log("Requesting WS client stop...", "SHUTDOWN")
        _ws_client.request_stop()

    if _ws_thread and _ws_thread.is_alive():
        _ws_thread.join(timeout=timeout)
        if _ws_thread.is_alive():
            log("WS thread did not exit in time — abandoning", "WARN")

    _shutdown.set()
    log("WS feed stopped", "SHUTDOWN")


# ═══════════════════════════════════════════════════
#  SIGNAL HANDLERS  (SIGTERM / SIGINT)
# ═══════════════════════════════════════════════════
_original_handlers: Dict[int, Any] = {}


def _install_signal_handlers():
    """
    Install SIGTERM/SIGINT handlers that close the WS connection cleanly.
    Preserves existing handlers so viper_engine's own handlers still fire.
    """
    def _handler(signum, frame):
        name = sig_mod.Signals(signum).name
        log(f"Received {name} — closing WS feed...", "SHUTDOWN")
        stop_feed()
        _shutdown.set()
        # Chain to previous handler if it's callable (not SIG_DFL/SIG_IGN)
        prev = _original_handlers.get(signum)
        if prev and callable(prev):
            prev(signum, frame)

    for s in (sig_mod.SIGTERM, sig_mod.SIGINT):
        _original_handlers[s] = sig_mod.getsignal(s)
        sig_mod.signal(s, _handler)


# ═══════════════════════════════════════════════════
#  STANDALONE DAEMON MODE
# ═══════════════════════════════════════════════════
def run_standalone():
    """
    Run as an independent daemon process (started by viper_ws_daemon.sh).
    Keeps the main thread alive with periodic heartbeats and auto-restarts
    the WS thread if it dies unexpectedly.
    """
    log("═" * 55, "STARTUP")
    log("🐍 VIPER WebSocket Feed — Standalone Daemon", "STARTUP")
    log("═" * 55, "STARTUP")

    try:
        with open(CONFIG_FILE) as f:
            symbols = json.load(f).get(
                "symbols",
                ["PEPEUSDT", "DOGEUSDT", "WIFUSDT", "SHIBUSDT"],
            )
    except Exception as exc:
        log(f"Config load error: {exc} — using defaults", "WARN")
        symbols = ["PEPEUSDT", "DOGEUSDT", "WIFUSDT", "SHIBUSDT"]

    _install_signal_handlers()
    start_feed(symbols)

    heartbeat_counter = 0
    try:
        while not _shutdown.is_set():
            _shutdown.wait(timeout=30)
            heartbeat_counter += 1

            if not is_running() and not _shutdown.is_set():
                log("⚠️ WS thread died — restarting...", "ERROR")
                start_feed(symbols)
                continue

            # Persist state snapshot for external monitoring
            state = get_latest_state()
            try:
                with open(STATE_FILE, "w") as f:
                    json.dump(state, f, indent=2)
            except Exception:
                pass

            # Heartbeat summary
            connected = state.get("connected", False)
            syms_live = sum(
                1 for s in state.get("symbols", {}).values()
                if s.get("orderbook", {}).get("levels", 0) > 0
            )
            pending   = len(state.get("signals", []))
            msgs      = state.get("messages_rx", 0)
            reconnects = state.get("reconnect_count", 0)

            status = "✅" if connected else "❌"
            log(
                f"💓 [{status}] {syms_live}/{len(symbols)} symbols live  "
                f"| msgs: {msgs}  | pending signals: {pending}  "
                f"| reconnects: {reconnects}",
                "HEARTBEAT",
            )

            # Detailed per-symbol heartbeat every 5th beat (~2.5 min)
            if heartbeat_counter % 5 == 0:
                for sym, sdata in state.get("symbols", {}).items():
                    ob  = sdata.get("orderbook", {})
                    tr  = sdata.get("trades", {})
                    mid = ob.get("mid_price", 0)
                    imb = ob.get("imbalance_side", "?")
                    lp  = tr.get("last_price", 0)
                    mv  = tr.get("fast_move_pct", 0)
                    if mid or lp:
                        log(
                            f"  {sym}: mid={mid:.6f}  last={lp:.6f}  "
                            f"move={mv:+.2f}%  imb={imb}  "
                            f"depth={ob.get('total_depth', 0):.0f}",
                            "DETAIL",
                        )

    except KeyboardInterrupt:
        log("KeyboardInterrupt caught", "SHUTDOWN")
    finally:
        stop_feed()
        log("VIPER WS standalone daemon exiting", "SHUTDOWN")


# ═══════════════════════════════════════════════════
#  CLI ENTRY
# ═══════════════════════════════════════════════════
if __name__ == "__main__":
    run_standalone()
