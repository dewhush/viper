#!/usr/bin/env python3
"""
VIPER — Telegram Command Listener
Interactive bot commands using python-telegram-bot Application class.
Shares bot token (@dewviper_bot) with hermes_notify.py.
Non-blocking — runs in background thread.

Commands:
  /status     — Viper current state (balance, positions, regimes, health)
  /kill       — Kill switch (close all, stop trading)
  /resume     — Resume trading after kill
  /pnl [days] — PnL for last N days (default 1)
  /backtest [strategy] — Quick backtest report
  /audit [n]  — Last N audit log entries
  /help       — List commands
"""

import os
import sys
import json
import asyncio
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ── Path setup ──
BASE = os.path.dirname(__file__) or "."
sys.path.insert(0, BASE)

# ── Config ──
BOT_ID = "8789332699"
BOT_SECRET = os.environ.get("HERMES_BOT_SECRET", "")
BOT_TOKEN = f"{BOT_ID}:{BOT_SECRET}"
ALLOWED_CHAT_ID = 5673885457

STATE_FILE = os.path.join(BASE, "viper_state.json")
LOG_FILE = os.path.join(BASE, "viper.log")

# ── Globals ──
_ENGINE: Optional[object] = None
_APPLICATION: Optional[Application] = None
_THREAD: Optional[threading.Thread] = None
_READY = threading.Event()
_STOP = threading.Event()


# ═══════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════

def _authorized(update: Update) -> bool:
    """Only respond to Dew's DM."""
    return (
        update.effective_chat is not None
        and update.effective_chat.id == ALLOWED_CHAT_ID
    )


def _load_state() -> dict:
    """Load viper_state.json, return dict or defaults."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _journal():
    """Lazy-load TradeJournal."""
    from viper_journal import TradeJournal
    return TradeJournal()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _read_log_lines(n: int = 10) -> list[str]:
    """Return last n lines from viper.log."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        return [l.rstrip("\n") for l in lines[-n:]]
    except OSError:
        return []


# ═══════════════════════════════════════════════════
#  REPLY HELPERS
# ═══════════════════════════════════════════════════

async def _reply(update: Update, text: str, parse_mode: str = "Markdown"):
    """Send reply to chat."""
    try:
        await update.message.reply_text(text, parse_mode=parse_mode)
    except Exception:
        try:
            await update.message.reply_text(text)
        except Exception:
            pass  # give up


async def _err(update: Update, e: Exception = None):
    """Indonesian error message — santai."""
    msg = "Waduh, error nih 🙏"
    if e:
        err_str = str(e)[:200]
        msg += f"\n`{err_str}`"
    msg += "\nCoba lagi nanti ya, atau lapor Dew."
    try:
        await _reply(update, msg)
    except Exception:
        pass


# ═══════════════════════════════════════════════════
#  COMMAND HANDLERS
# ═══════════════════════════════════════════════════

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    text = """
🐍 *VIPER — BANTUAN*
━━━━━━━━━━━━━━━━━━━
Perintah yang tersedia:

`/status` — Status Viper saat ini
  balance, posisi, siklus, regime, kesehatan

`/kill` — Matikan trading darurat
  Tutup semua posisi, stop trading

`/resume` — Lanjutkan trading setelah kill

`/pnl [hari]` — Lihat PnL N hari terakhir
  Contoh: `/pnl 7` (default: 1)

`/backtest [strategi]` — Jalanin backtest
  Strategi: mr, tf, momentum, funding_arb, stat_arb
  Contoh: `/backtest mr` (default: semua)

`/audit [n]` — Lihat N log audit terakhir
  Contoh: `/audit 10` (default: 5)

`/help` — Tampilkan pesan ini
━━━━━━━━━━━━━━━━━━━
_Ada yang bisa dibantu? 🐍_
"""
    await _reply(update, text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    try:
        state = _load_state()

        # Balance — try engine first, fallback state
        bal = {"total": 0, "free": 0}
        if _ENGINE and hasattr(_ENGINE, "_fetch_balance"):
            try:
                bal = _ENGINE._fetch_balance()
            except Exception:
                pass
        if not bal.get("total"):
            bal = state.get("balance", {"total": 0, "free": 0})

        # Open positions — engine vs journal
        open_positions = []
        if _ENGINE and hasattr(_ENGINE, "positions"):
            try:
                open_positions = _ENGINE.positions.all()
            except Exception:
                pass
        if not open_positions:
            try:
                j = _journal()
                open_positions = j.get_open_trades()
            except Exception:
                pass

        pos_lines = []
        for p in open_positions[:5]:
            sym = p.get("symbol", p.get("pair", "?"))
            side = p.get("side", "?").upper()
            entry = p.get("entry_price", 0)
            pos_lines.append(f"  • `{sym}` {side} @ ${entry:.4f}")

        # Last cycle time
        last_cycle = state.get("last_cycle", "N/A")

        # Market regime per symbol — from state or config
        regimes = state.get("regimes", {})
        if not regimes and _ENGINE and hasattr(_ENGINE, "config"):
            regimes = _ENGINE.config.get("regime", "auto")

        # System health
        kill_active = state.get("kill_switch", False) or (
            _ENGINE and getattr(_ENGINE, "_kill_signalled", False)
        )
        cb_halted = state.get("circuit_breaker_halted", False)
        ws_feed = state.get("ws_feed", False)

        health = []
        if kill_active:
            health.append("🔴 KILL")
        elif cb_halted:
            health.append("🟡 CB")
        else:
            health.append("🟢 OK")
        health.append("WS: 🟢" if ws_feed else "WS: ⚪")
        health_str = " | ".join(health)

        total_bal = bal.get("total", 0)

        text = f"""🐍 *VIPER — STATUS*
━━━━━━━━━━━━━━━━━━━
💰 *Balance:* `${total_bal:.2f}`
📊 *Open Positions:* {len(open_positions)}
{chr(10).join(pos_lines) or "  _(kosong)_"}
⏱ *Last Cycle:* `{last_cycle}`
📈 *Regime:* `{regimes}`
🩺 *Health:* {health_str}
━━━━━━━━━━━━━━━━━━━
⏱ {_utcnow()}
"""
        await _reply(update, text)
    except Exception as e:
        await _err(update, e)


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    try:
        if _ENGINE and hasattr(_ENGINE, "activate_kill_switch"):
            _ENGINE.activate_kill_switch()
            text = f"""🛑 *KILL SWITCH DIAKTIFKAN*
━━━━━━━━━━━━━━━━━━━
Semua posisi ditutup.
Trading dihentikan.

⚠️ Pake `/resume` kalo mau jalan lagi.
━━━━━━━━━━━━━━━━━━━
⏱ {_utcnow()}
"""
            await _reply(update, text)
        else:
            # Fallback: write state directly
            state = _load_state()
            state["kill_switch"] = True
            try:
                with open(STATE_FILE, "w") as f:
                    json.dump(state, f, indent=2)
            except Exception:
                pass
            text = f"""🛑 *KILL SWITCH* (state only — engine not attached)
━━━━━━━━━━━━━━━━━━━
Flag ditulis ke state file.
Posisi tidak ditutup otomatis (no engine ref).
━━━━━━━━━━━━━━━━━━━
⏱ {_utcnow()}
"""
            await _reply(update, text)
    except Exception as e:
        await _err(update, e)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    try:
        if _ENGINE and hasattr(_ENGINE, "reset_kill_switch"):
            _ENGINE.reset_kill_switch()
            text = f"""🟢 *TRADING DILANJUTKAN*
━━━━━━━━━━━━━━━━━━━
Kill switch dicabut.
Viper bisa trading lagi.
━━━━━━━━━━━━━━━━━━━
⏱ {_utcnow()}
"""
            await _reply(update, text)
        else:
            # Fallback state write
            state = _load_state()
            state["kill_switch"] = False
            state["circuit_breaker_halted"] = False
            state["circuit_breaker_reason"] = ""
            try:
                with open(STATE_FILE, "w") as f:
                    json.dump(state, f, indent=2)
            except Exception:
                pass
            text = f"""🟢 *TRADING DILANJUTKAN* (state only)
━━━━━━━━━━━━━━━━━━━
Reset via state file.
━━━━━━━━━━━━━━━━━━━
⏱ {_utcnow()}
"""
            await _reply(update, text)
    except Exception as e:
        await _err(update, e)


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    try:
        days = 1
        if context.args and context.args[0].isdigit():
            days = int(context.args[0])
        days = max(1, min(365, days))  # sanitize

        j = _journal()

        # Aggregate PnL over last N days
        from datetime import date as date_type

        today = date_type.today()
        total_pnl = 0.0
        total_trades = 0
        total_wins = 0
        pnl_by_day = {}

        for i in range(days):
            d = today - timedelta(days=i)
            d_str = d.isoformat()
            try:
                stats = j.get_daily_stats(d_str)
                day_pnl = stats.get("pnl_usd", 0)
                total_pnl += day_pnl
                total_trades += stats.get("trades_count", 0)
                total_wins += stats.get("wins", 0)
                if stats.get("trades_count", 0) > 0:
                    pnl_by_day[d_str] = day_pnl
            except Exception:
                pass

        # Also get all-time stats
        all_time = j.get_all_time_stats()

        emoji = "🟢" if total_pnl >= 0 else "🔴"
        emoji_at = "🟢" if all_time.get("total_pnl", 0) >= 0 else "🔴"

        # Show last few days breakdown
        day_lines = []
        for d_str in sorted(pnl_by_day.keys(), reverse=True)[:7]:
            val = pnl_by_day[d_str]
            ico = "🟢" if val >= 0 else "🔴"
            day_lines.append(f"  {d_str}: {ico} `${val:+.4f}`")

        text = f"""📊 *VIPER — PNL REPORT*
━━━━━━━━━━━━━━━━━━━
*Periode:* {days} hari terakhir
{emoji} *PnL:* `{'+' if total_pnl >= 0 else ''}${total_pnl:.4f}`
*Trades:* {total_trades} | *Wins:* {total_wins}

{chr(10).join(day_lines) or "  _(no data)_"}

*All-Time:*
{emoji_at} Total: `${all_time.get('total_pnl', 0):+.2f}`
Win Rate: {all_time.get('win_rate', 0):.1f}%
Sharpe: {all_time.get('sharpe', 0):.3f}
Max DD: ${all_time.get('max_drawdown', 0):.2f}
━━━━━━━━━━━━━━━━━━━
⏱ {_utcnow()}
"""
        await _reply(update, text)
    except Exception as e:
        await _err(update, e)


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    try:
        from viper_backtest import Backtester, _STRATEGY_MAP

        # Determine strategy(ies)
        requested = context.args[0].lower() if context.args else "all"
        if requested == "all":
            strategies = list(_STRATEGY_MAP.keys())
        elif requested in _STRATEGY_MAP:
            strategies = [requested]
        else:
            valid = ", ".join(_STRATEGY_MAP.keys())
            await _reply(
                update,
                f"Hmm, strategy `{requested}` gak ada 🙏\n"
                f"Pilih salah satu: {valid}\n"
                f"Atau `/backtest all` buat semua.",
            )
            return

        # Fetch data — use engine exchange if available, else try directly
        symbols = ["DOGEUSDT", "PEPEUSDT"]
        if _ENGINE and hasattr(_ENGINE, "config"):
            symbols = _ENGINE.config.get("symbols", symbols)

        results = {}
        for raw_sym in symbols[:2]:  # max 2 symbols for speed
            sym = raw_sym
            if "/" not in sym:
                if sym.endswith("USDT"):
                    sym = sym[:-4] + "/USDT"
                else:
                    sym = sym + "/USDT"

            # Fetch OHLCV
            df = None
            if _ENGINE and hasattr(_ENGINE, "_fetch_ohlcv"):
                try:
                    df = _ENGINE._fetch_ohlcv(sym, "4h", 300)
                except Exception:
                    pass
            if df is None or df.empty:
                # Try direct exchange fetch
                try:
                    from viper_exchange import ExchangeManager
                    import os as _os

                    env_file = os.path.join(BASE, ".env")
                    env = {}
                    if os.path.exists(env_file):
                        with open(env_file) as _f:
                            for _l in _f:
                                _l = _l.strip()
                                if _l and not _l.startswith("#") and "=" in _l:
                                    k, v = _l.split("=", 1)
                                    env[k.strip()] = v.strip().strip("'\"")
                    config = {"symbols": [sym]}
                    ex = ExchangeManager(env, config)
                    ohlcv = ex.fetch_ohlcv(sym, "4h", limit=300)
                    import pandas as pd
                    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                    df.set_index("timestamp", inplace=True)
                except Exception as e:
                    results[sym] = f"Data error: {e}"
                    continue

            if df.empty or len(df) < 100:
                results[sym] = "Data insufficient (<100 bars)"
                continue

            # Run backtest for each strategy
            sym_results = {}
            for strat in strategies:
                try:
                    bt = Backtester(
                        strategy_name=strat,
                        initial_balance=100.0,
                        trade_size_pct=5.0,
                    )
                    res = bt.run(df, {"slippage_bps": 5, "warmup_bars": 50})
                    sym_results[strat] = {
                        "trades": res.total_trades,
                        "win_rate": res.win_rate,
                        "pnl": res.total_pnl_usd,
                        "pf": res.profit_factor,
                        "sharpe": res.sharpe_ratio,
                        "dd": res.max_drawdown_pct,
                    }
                except Exception as e:
                    sym_results[strat] = f"Error: {e}"

            results[sym] = sym_results

        # Build report
        report_parts = [f"📊 *VIPER — BACKTEST REPORT*"]
        report_parts.append("━" * 25)

        for sym, sym_res in results.items():
            report_parts.append(f"📈 *{sym}*")
            if isinstance(sym_res, str):
                report_parts.append(f"  _{sym_res}_")
                continue
            for strat, r in sym_res.items():
                if isinstance(r, str):
                    report_parts.append(f"  `{strat:12s}` → {r}")
                    continue
                emoji_pnl = "🟢" if r["pnl"] >= 0 else "🔴"
                report_parts.append(
                    f"  `{strat:12s}` {r['trades']:3d} tr  "
                    f"WR={r['win_rate']:.0%}  "
                    f"{emoji_pnl}${r['pnl']:+.2f}  "
                    f"PF={r['pf']:.2f}  "
                    f"Sharpe={r['sharpe']:.2f}"
                )
            report_parts.append("")

        report_parts.append(f"⏱ {_utcnow()}")
        await _reply(update, "\n".join(report_parts))
    except Exception as e:
        await _err(update, e)


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    try:
        n = 5
        if context.args and context.args[0].isdigit():
            n = int(context.args[0])
        n = max(1, min(100, n))  # sanitize

        j = _journal()
        entries = j.get_recent_audit(n)

        if not entries:
            await _reply(update, "Belum ada audit log 📭")
            return

        lines = [f"📋 *VIPER — AUDIT LOG* ({len(entries)} terbaru)", "━" * 25]
        for entry in entries:
            eid = entry.get("id", "?")
            ts = entry.get("ts", "?")
            event = entry.get("event", "?")
            details = entry.get("details", "")
            if details:
                details_s = details[:80]
            else:
                details_s = ""
            lines.append(f"`#{eid}` {ts}")
            lines.append(f"  *{event}* {details_s}")
        lines.append("")
        lines.append(f"⏱ {_utcnow()}")

        await _reply(update, "\n".join(lines))
    except Exception as e:
        await _err(update, e)


# ═══════════════════════════════════════════════════
#  APPLICATION SETUP
# ═══════════════════════════════════════════════════

def _build_application() -> Application:
    """Create Application with all command handlers."""
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("audit", cmd_audit))

    return app


# ═══════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════

def init_commands(engine: object = None):
    """Initialize command listener with optional engine reference.

    Call before start_command_poller(). Sets up Application handlers
    and stores engine reference for kill/resume/status commands.
    """
    global _ENGINE, _APPLICATION
    _ENGINE = engine
    _APPLICATION = _build_application()


def start_command_poller(engine: object = None):
    """Start Telegram polling in a background daemon thread.

    Non-blocking — returns immediately. Polling runs in a separate
    thread managed by python-telegram-bot's internal event loop.

    Args:
        engine: Optional ViperEngine instance for kill/resume/status.
    """
    global _THREAD, _READY, _ENGINE, _APPLICATION

    if _APPLICATION is None:
        init_commands(engine)
    elif engine is not None:
        _ENGINE = engine

    if _THREAD and _THREAD.is_alive():
        return  # Already running

    _READY.clear()
    _STOP.clear()

    def _run():
        """Run Application polling (blocking, in thread)."""
        app = _APPLICATION
        if app is None:
            return

        try:
            # Start the application in the thread's own event loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # Run polling in a way that can be stopped
            loop.run_until_complete(_poll_forever(app))
        except Exception as e:
            print(f"[viper_telegram] Poller error: {e}")
            traceback.print_exc()
        finally:
            _READY.clear()

    _THREAD = threading.Thread(target=_run, daemon=True, name="viper-telegram")
    _THREAD.start()
    _READY.wait(timeout=10)  # Wait for startup


async def _poll_forever(app: Application):
    """Run application polling until stopped."""
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    _READY.set()
    try:
        # Keep running until stop event
        while not _STOP.is_set():
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def stop_command_poller():
    """Gracefully stop the command poller.

    Signals the poller loop to exit, then cleans up.
    Safe to call multiple times.
    """
    global _THREAD, _APPLICATION, _READY

    _STOP.set()
    _READY.clear()

    if _THREAD and _THREAD.is_alive():
        _THREAD.join(timeout=5)

    _THREAD = None
    _APPLICATION = None
    print("[viper_telegram] Command poller stopped.")


# ═══════════════════════════════════════════════════
#  STANDALONE ENTRY POINT
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    print("🐍 VIPER Telegram Command Listener")
    print(f"Bot: @dewviper_bot | Chat: {ALLOWED_CHAT_ID}")
    print("Starting polling... (Ctrl+C to stop)")

    init_commands()
    start_command_poller()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        stop_command_poller()
        print("Done.")
