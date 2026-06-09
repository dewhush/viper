"""
Viper Trade Journal — SQLite-based PnL tracking module for @dewviper_bot.

Thread-safe via SQLite WAL mode. All timestamps in UTC ISO format.
Production-grade with type hints, error handling, docstrings.
"""

import sqlite3
import threading
from datetime import datetime, timezone, date as date_type
from typing import Any, List, Optional, Dict
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = Path("/root/trader/viper.db")
_STATUS_OPEN = "open"
_STATUS_CLOSED = "closed"
_STATUS_CANCELLED = "cancelled"

def _utcnow() -> str:
    """Return current UTC time in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")




# ---------------------------------------------------------------------------
# Helper: format a trade record as a Telegram notification string
# ---------------------------------------------------------------------------
def format_trade_report(trade: dict) -> str:
    """Return a single formatted Telegram message for a trade dict."""
    symbol = trade.get("symbol", "???")
    side = trade.get("side", "???").upper()
    status = trade.get("status", "?")

    lines = [f"*Trade Report — {symbol} ({side})*"]
    lines.append(f"Status: {status}")

    if trade.get("entry_price"):
        lines.append(f"Entry: ${trade['entry_price']:.4f}")
    if trade.get("exit_price"):
        lines.append(f"Exit:  ${trade['exit_price']:.4f}")

    if trade.get("size_usd"):
        lines.append(f"Size:  ${trade['size_usd']:.2f}")
    if trade.get("size_contracts"):
        lines.append(f"Qty:   {trade['size_contracts']:.4f}")

    if trade.get("pnl_usd") is not None:
        emoji = "🟢" if trade["pnl_usd"] >= 0 else "🔴"
        lines.append(f"PnL:   {emoji} ${trade['pnl_usd']:.2f} ({trade.get('pnl_pct', 0):.2f}%)")

    if trade.get("fee_usd"):
        lines.append(f"Fee:   ${trade['fee_usd']:.4f}")
    if trade.get("slippage_pct"):
        lines.append(f"Slippage: {trade['slippage_pct']:.3f}%")

    if trade.get("strategy"):
        lines.append(f"Strategy: `{trade['strategy']}`")
    if trade.get("regime"):
        lines.append(f"Regime: {trade['regime']}")

    if trade.get("reason_open"):
        lines.append(f"Reason open: {trade['reason_open']}")
    if trade.get("reason_close"):
        lines.append(f"Reason close: {trade['reason_close']}")

    duration = trade.get("duration_sec")
    if duration is not None and duration > 0:
        m, s = divmod(int(duration), 60)
        h, m = divmod(m, 60)
        dur_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"
        lines.append(f"Duration: {dur_str}")

    if trade.get("entry_time"):
        lines.append(f"Opened: {trade['entry_time']}")
    if trade.get("exit_time"):
        lines.append(f"Closed: {trade['exit_time']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TradeJournal class
# ---------------------------------------------------------------------------
class TradeJournal:
    """SQLite-backed trade journal with PnL tracking, daily summaries, and
    balance snapshots.  Thread-safe via WAL mode and per-connection locks."""

    # ── schema ──────────────────────────────────────────────────────────
    _SQL_CREATE_TRADES = """
    CREATE TABLE IF NOT EXISTS trades (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol          TEXT    NOT NULL,
        side            TEXT    NOT NULL,       -- long / short
        entry_price     REAL,
        entry_time      TEXT    NOT NULL,       -- UTC ISO
        exit_price      REAL,
        exit_time       TEXT,
        size_usd        REAL,
        size_contracts  REAL,
        pnl_usd         REAL,
        pnl_pct         REAL,
        fee_usd         REAL    DEFAULT 0,
        slippage_pct    REAL    DEFAULT 0,
        strategy        TEXT,
        regime          TEXT,
        confidence      REAL,
        reason_open     TEXT,
        reason_close    TEXT,
        duration_sec    INTEGER,
        status          TEXT    NOT NULL DEFAULT 'open'  -- open / closed / cancelled
    );
    """

    _SQL_CREATE_DAILY_PNL = """
    CREATE TABLE IF NOT EXISTS daily_pnl (
        date            TEXT PRIMARY KEY,       -- YYYY-MM-DD
        trades_count    INTEGER DEFAULT 0,
        wins            INTEGER DEFAULT 0,
        losses          INTEGER DEFAULT 0,
        pnl_usd         REAL    DEFAULT 0,
        balance_start   REAL,
        balance_end     REAL,
        max_drawdown    REAL    DEFAULT 0,
        sharpe          REAL,
        regime          TEXT,
        notes           TEXT
    );
    """

    _SQL_CREATE_BALANCE = """
    CREATE TABLE IF NOT EXISTS balance_snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp       TEXT    NOT NULL,       -- UTC ISO
        exchange        TEXT    NOT NULL,
        total_balance   REAL,
        free_balance    REAL,
        unrealized_pnl  REAL,
        positions_count INTEGER DEFAULT 0
    );
    """

    _SQL_CREATE_CYCLE = """
    CREATE TABLE IF NOT EXISTS cycle_logs (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp         TEXT    NOT NULL,     -- UTC ISO
        symbols_scanned   INTEGER DEFAULT 0,
        signals_generated INTEGER DEFAULT 0,
        trades_executed   INTEGER DEFAULT 0,
        backtest_run      INTEGER DEFAULT 0,
        duration_sec      REAL,
        errors            INTEGER DEFAULT 0
    );
    """


    # ── audit trail ─────────────────────────────────────────────────────
    def audit(self, event: str, details: str = "", metadata: dict = None) -> None:
        """Write structured audit entry."""
        import json as _json
        try:
            self._execute(
                "INSERT INTO audit_log (ts, event, details, metadata) VALUES (datetime('now'), ?, ?, ?)",
                (event, details[:500], _json.dumps(metadata or {})),
            )
        except Exception:
            pass  # Don't crash on audit fail

    def get_recent_audit(self, n: int = 20, event: str = None) -> list:
        """Return recent audit entries."""
        if event:
            return self._fetchall(
                "SELECT * FROM audit_log WHERE event=? ORDER BY id DESC LIMIT ?",
                (event, n),
            )
        return self._fetchall("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (n,))


    _SQL_CREATE_AUDIT = """
    CREATE TABLE IF NOT EXISTS audit_log (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       TEXT    NOT NULL DEFAULT (CURRENT_TIMESTAMP),
        event    TEXT    NOT NULL,
        details  TEXT    DEFAULT "",
        metadata TEXT    DEFAULT "{}"
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
    """

    __slots__ = ("_db_path", "_local", "_lock")

    # ── lifecycle ───────────────────────────────────────────────────────
    def __init__(self, db_path: str | Path = DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # Thread-local connections to avoid sharing across threads
        self._local = threading.local()
        self._lock = threading.Lock()
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Return a thread-local connection, creating it if necessary."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,   # we use our own lock
                isolation_level=None,       # autocommit mode
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=5000;")
            self._local.conn = conn
        return conn

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Thread-safe execute with our own lock."""
        with self._lock:
            return self._get_connection().execute(sql, params)

    def _executemany(self, sql: str, seq: list[tuple]) -> sqlite3.Cursor:
        """Thread-safe executemany."""
        with self._lock:
            return self._get_connection().executemany(sql, seq)

    def _fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._get_connection().execute(sql, params)
            return cur.fetchone()

    def _fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._get_connection().execute(sql, params)
            return cur.fetchall()

    def init_db(self) -> None:
        """Create all tables if they do not exist."""
        conn = self._get_connection()
        with self._lock:
            conn.executescript(self._SQL_CREATE_TRADES)
            conn.executescript(self._SQL_CREATE_DAILY_PNL)
            conn.executescript(self._SQL_CREATE_BALANCE)
            conn.executescript(self._SQL_CREATE_CYCLE)
            conn.executescript(self._SQL_CREATE_AUDIT)

    # ── trades ──────────────────────────────────────────────────────────
    def open_trade(self, trade_data: dict) -> int:
        """Insert a new open trade record and return its trade_id.

        Required keys: symbol, side.
        Optional: entry_price, entry_time (defaults to now UTC), size_usd,
        size_contracts, strategy, regime, confidence, reason_open.
        """
        now = _utcnow()
        row = (
            trade_data.get("symbol"),
            trade_data.get("side"),
            trade_data.get("entry_price"),
            trade_data.get("entry_time", now),
            trade_data.get("size_usd"),
            trade_data.get("size_contracts"),
            trade_data.get("strategy"),
            trade_data.get("regime"),
            trade_data.get("confidence"),
            trade_data.get("reason_open"),
        )
        sql = """
            INSERT INTO trades
                (symbol, side, entry_price, entry_time,
                 size_usd, size_contracts, strategy, regime,
                 confidence, reason_open, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """
        cur = self._execute(sql, row)
        return cur.lastrowid  # type: ignore[return-value]

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        reason: str,
        fee: float = 0.0,
        slippage: float = 0.0,
    ) -> dict:
        """Close an open trade, compute PnL, and return the updated record.

        Raises ValueError if trade not found or already closed.
        """
        trade = self.get_trade(trade_id)
        if not trade:
            raise ValueError(f"Trade #{trade_id} not found.")
        if trade["status"] != _STATUS_OPEN:
            raise ValueError(
                f"Trade #{trade_id} is already '{trade['status']}', cannot close."
            )

        now = _utcnow()
        entry_price = trade["entry_price"]
        side = trade["side"]

        # ── compute PnL ─────────────────────────────────────────────
        if entry_price and entry_price > 0:
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
            if side == "short":
                pnl_pct = -pnl_pct
            size_usd = trade.get("size_usd") or 0.0
            pnl_usd = (pnl_pct / 100.0) * size_usd

            # If we have contracts, recompute from contracts for precision
            size_contracts = trade.get("size_contracts")
            if size_contracts:
                raw_pnl = (exit_price - entry_price) * size_contracts
                if side == "short":
                    raw_pnl = -raw_pnl
                pnl_usd = raw_pnl
                if size_usd and size_usd > 0:
                    pnl_pct = (pnl_usd / size_usd) * 100.0
                else:
                    pnl_pct = 0.0 if entry_price == 0 else (pnl_usd / (entry_price * size_contracts)) * 100.0
        else:
            pnl_usd = 0.0
            pnl_pct = 0.0

        pnl_usd -= fee  # subtract fee from realised PnL

        # ── duration ─────────────────────────────────────────────────
        try:
            entry_dt = datetime.fromisoformat(trade["entry_time"])
            exit_dt = datetime.fromisoformat(now)
            duration_sec = int((exit_dt - entry_dt).total_seconds())
        except Exception:
            duration_sec = None

        sql = """
            UPDATE trades
               SET exit_price    = ?,
                   exit_time     = ?,
                   pnl_usd       = ?,
                   pnl_pct       = ?,
                   fee_usd       = ?,
                   slippage_pct  = ?,
                   reason_close  = ?,
                   duration_sec  = ?,
                   status        = 'closed'
             WHERE id = ?
        """
        self._execute(
            sql,
            (exit_price, now, pnl_usd, pnl_pct, fee, slippage,
             reason, duration_sec, trade_id),
        )
        return self.get_trade(trade_id)  # type: ignore[return-value]

    def get_open_trades(self) -> List[dict]:
        """Return all trades with status == 'open'."""
        rows = self._fetchall("SELECT * FROM trades WHERE status = 'open' ORDER BY id")
        return [_row_to_dict(r) for r in rows]

    def get_trade(self, trade_id: int) -> Optional[dict]:
        """Return a single trade record by id, or None."""
        row = self._fetchone("SELECT * FROM trades WHERE id = ?", (trade_id,))
        return _row_to_dict(row) if row else None

    # ── stats ───────────────────────────────────────────────────────────
    def get_daily_stats(self, date: Optional[str] = None) -> dict:
        """Return aggregated stats for a given calendar date (YYYY-MM-DD).

        If *date* is None, uses today's date.  Returns a dict with keys:
        date, trades_count, wins, losses, pnl_usd, pnl_pct_avg.
        """
        target = date or _today_str()
        rows = self._fetchall(
            "SELECT * FROM trades WHERE date(entry_time) = ?", (target,)
        )
        trades = [_row_to_dict(r) for r in rows]
        total = len(trades)
        wins = sum(
            1 for t in trades
            if t["status"] == _STATUS_CLOSED and (t.get("pnl_usd") or 0) > 0
        )
        losses = sum(
            1 for t in trades
            if t["status"] == _STATUS_CLOSED and (t.get("pnl_usd") or 0) <= 0
        )
        closed_pnls = [
            t["pnl_usd"] for t in trades
            if t["status"] == _STATUS_CLOSED and t.get("pnl_usd") is not None
        ]
        total_pnl = sum(closed_pnls) if closed_pnls else 0.0
        avg_pnl = total_pnl / len(closed_pnls) if closed_pnls else 0.0

        return {
            "date": target,
            "trades_count": total,
            "wins": wins,
            "losses": losses,
            "pnl_usd": total_pnl,
            "pnl_pct_avg": avg_pnl,
        }

    def get_all_time_stats(self) -> dict:
        """Return overall performance statistics as a dict.

        Keys: total_trades, win_rate, total_pnl, avg_pnl, max_drawdown, sharpe.
        """
        closed = self._fetchall(
            "SELECT pnl_usd, pnl_pct FROM trades WHERE status = 'closed'"
        )
        total_trades = len(closed)
        if total_trades == 0:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
                "max_drawdown": 0.0,
                "sharpe": 0.0,
            }

        pnls = [r["pnl_usd"] or 0.0 for r in closed]
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = sum(pnls)
        avg_pnl = total_pnl / total_trades
        win_rate = (wins / total_trades) * 100.0

        # ── max drawdown (peak-to-trough on cumulative) ──────────────
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd

        # ── Sharpe (annualised, assuming risk-free = 0) ──────────────
        # Sortino-style: we use daily returns via daily_pnl if available,
        # else approximate from per-trade returns.
        if len(pnls) >= 2:
            import statistics
            mean_pnl = statistics.mean(pnls)
            std_pnl = statistics.pstdev(pnls)  # population std
            sharpe = (mean_pnl / std_pnl) * (252 ** 0.5) if std_pnl > 0 else 0.0
        else:
            sharpe = 0.0

        return {
            "total_trades": total_trades,
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
            "max_drawdown": round(max_dd, 2),
            "sharpe": round(sharpe, 4),
        }

    # ── daily summary ───────────────────────────────────────────────────
    def save_daily_summary(self, stats: dict) -> None:
        """Upsert a daily summary row.

        Expected keys: date (YYYY-MM-DD), trades_count, wins, losses,
        pnl_usd, balance_start, balance_end, max_drawdown, sharpe,
        regime (optional), notes (optional).
        """
        sql = """
            INSERT INTO daily_pnl
                (date, trades_count, wins, losses, pnl_usd,
                 balance_start, balance_end, max_drawdown, sharpe,
                 regime, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                trades_count   = excluded.trades_count,
                wins           = excluded.wins,
                losses         = excluded.losses,
                pnl_usd        = excluded.pnl_usd,
                balance_start  = excluded.balance_start,
                balance_end    = excluded.balance_end,
                max_drawdown   = excluded.max_drawdown,
                sharpe         = excluded.sharpe,
                regime         = excluded.regime,
                notes          = excluded.notes
        """
        self._execute(
            sql,
            (
                stats["date"],
                stats.get("trades_count", 0),
                stats.get("wins", 0),
                stats.get("losses", 0),
                stats.get("pnl_usd", 0.0),
                stats.get("balance_start"),
                stats.get("balance_end"),
                stats.get("max_drawdown", 0.0),
                stats.get("sharpe"),
                stats.get("regime"),
                stats.get("notes"),
            ),
        )

    # ── balance snapshots ───────────────────────────────────────────────
    def log_balance(
        self,
        exchange: str,
        total: float,
        free: float,
        positions: int = 0,
    ) -> None:
        """Record a balance snapshot."""
        self._execute(
            """INSERT INTO balance_snapshots
               (timestamp, exchange, total_balance, free_balance, positions_count)
               VALUES (?, ?, ?, ?, ?)""",
            (_utcnow(), exchange, total, free, positions),
        )

    # ── cycle logs ──────────────────────────────────────────────────────
    def log_cycle(self, stats: dict) -> None:
        """Record a bot cycle execution log.

        Expected keys: symbols_scanned, signals_generated, trades_executed,
        backtest_run, duration_sec, errors (all optional, default 0).
        """
        self._execute(
            """INSERT INTO cycle_logs
               (timestamp, symbols_scanned, signals_generated, trades_executed,
                backtest_run, duration_sec, errors)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                _utcnow(),
                stats.get("symbols_scanned", 0),
                stats.get("signals_generated", 0),
                stats.get("trades_executed", 0),
                stats.get("backtest_run", 0),
                stats.get("duration_sec"),
                stats.get("errors", 0),
            ),
        )

    # ── audit trail ─────────────────────────────────────────────────────
    def audit(self, event: str, details: str = "", metadata: dict = None) -> None:
        import json as _json
        try:
            self._execute(
                "INSERT INTO audit_log (ts, event, details, metadata) VALUES (datetime('now'), ?, ?, ?)",
                (event, details[:500], _json.dumps(metadata or {})),
            )
        except Exception:
            pass

    def get_recent_audit(self, n: int = 20, event: str = None) -> list:
        if event:
            return self._fetchall(
                "SELECT * FROM audit_log WHERE event=? ORDER BY id DESC LIMIT ?",
                (event, n),
            )
        return self._fetchall("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (n,))
