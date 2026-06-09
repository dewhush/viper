#!/usr/bin/env python3
"""
VIPER Telegram Notification — with message editing for non-spam updates.
- Balance, status, cycle reports → EDIT last message (no spam)
- Trade signals, alerts, kill switch → SEND new messages (important events)
- State persisted in viper_notify_state.json
"""
import urllib.request, json, os, time
from datetime import datetime, timezone

BOT_ID = "8789332699"
BOT_SECRET = os.environ.get("HERMES_BOT_SECRET", "***")
CHAT_ID = os.environ.get("HERMES_CHAT_ID", "5673885457")
BASE_URL = f"https://api.telegram.org/bot{BOT_ID}:{BOT_SECRET}"
STATE_FILE = "/root/trader/viper_notify_state.json"

_lock = {"state": None, "ts": 0}

def _load_state():
    now = time.time()
    if _lock["state"] and now - _lock["ts"] < 2:
        return _lock["state"]
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
            _lock["state"] = s
            _lock["ts"] = now
            return s
    except Exception:
        return {}

def _save_state(key, val):
    s = _load_state()
    s[key] = val
    s["_updated"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
        _lock["state"] = s
        _lock["ts"] = time.time()
    except Exception:
        pass

def _api(method, payload):
    url = f"{BASE_URL}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"[notify] HTTP {e.code}: {body[:200]}")
        return None
    except Exception as e:
        print(f"[notify] {method} failed: {e}")
        return None

# ─── SEND / EDIT ──────────────────────────────────

def _send(text, parse_mode="Markdown"):
    """Send new message. Returns message_id or None."""
    r = _api("sendMessage", {"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode, "link_preview_options": {"is_disabled": True}})
    if r and r.get("ok"):
        return r["result"]["message_id"]
    return None

def _edit(text, msg_id, parse_mode="Markdown"):
    """Edit existing message. Falls back to send on failure."""
    r = _api("editMessageText", {"chat_id": CHAT_ID, "message_id": msg_id, "text": text, "parse_mode": parse_mode, "link_preview_options": {"is_disabled": True}})
    if r and r.get("ok"):
        return msg_id
    # If edit fails (deleted / too old), send new
    return _send(text, parse_mode)

def _edit_or_send(text, key, parse_mode="Markdown"):
    """Edit if we have a saved msg_id for this key, else send new."""
    state = _load_state()
    msg_id = state.get(key)
    if msg_id:
        new_id = _edit(text, msg_id, parse_mode)
        if new_id == msg_id:
            return msg_id  # Edit successful
        # Edit failed — new_id is the new message id
        if new_id:
            _save_state(key, new_id)
        return new_id
    # No saved msg — send new
    new_id = _send(text, parse_mode)
    if new_id:
        _save_state(key, new_id)
    return new_id

# ─── CLEANUP ──────────────────────────────────────

def clear_state(key=None):
    """Delete saved message state so next call sends new instead of editing."""
    if key:
        s = _load_state()
        s.pop(key, None)
        _save_state("_clear", True)
    else:
        try:
            os.remove(STATE_FILE)
        except Exception:
            pass

# ═══════════════════════════════════════════════════
#  PUBLIC API (same names as hermes_notify)
# ═══════════════════════════════════════════════════

# ─── These SEND new messages (important events) ───

def position_opened(pair, side, entry, size_usd, size_contracts, sl, tp, strategy="", confidence=0.0, leverage=1):
    side_icon = "🗻" if side.upper() == "LONG" else "⛰️"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = f"""🗻 *VIPER — POSITION OPENED*
━━━━━━━━━━━━━━━━━━━
{side_icon} *{pair}* | {strategy}
*Entry:* `{entry}`
*Size:* `{size_usd:.2f}` USDT ({size_contracts} cont. @ x{leverage})
━━━━━━━━━━━━━━━━━━━
🎯 *TP:* `{tp}`
🛡 *SL:* `{sl}`
{('*Conf:* ' + f'{confidence:.0%}' if confidence else '')}
━━━━━━━━━━━━━━━━━━━
⏱ {now}"""
    return bool(_send(text))

def trade_closed(pair, side, entry, exit_px, size, pnl, pnl_pct, reason, duration="", strategy=""):
    emoji = "🟢" if pnl >= 0 else "🔴"
    text = f"""{emoji} *VIPER — TRADE CLOSED*
━━━━━━━━━━━━━━━━━━━
*{pair}* | {side.upper()}
*Entry:* `{entry}` → *Exit:* `{exit_px}`
{('*Duration:* ' + duration if duration else '')}
{('*Strategy:* ' + strategy if strategy else '')}
━━━━━━━━━━━━━━━━━━━
*PnL:* `{'+' if pnl >= 0 else ''}${pnl:.2f} ({pnl_pct:.2f}%)`
*Size:* `${size:.2f}`
*Reason:* {reason}
━━━━━━━━━━━━━━━━━━━
⏱ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"""
    return bool(_send(text))

def kill_switch(reason, balance=0, positions=0):
    text = f"""🛑 *VIPER — KILL SWITCH ACTIVE*
━━━━━━━━━━━━━━━━━━━
*Reason:* {reason}
*Positions closed:* {positions}
*Balance:* `${balance:.2f}`
━━━━━━━━━━━━━━━━━━━
_All trading halted until /resume_
⏱ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"""
    return bool(_send(text))

def alert(title, message, severity="info"):
    icons = {"info": "ℹ️", "warn": "⚠️", "error": "🚨", "critical": "💀"}
    icon = icons.get(severity, "ℹ️")
    text = f"""{icon} *VIPER — {severity.upper()}*
━━━━━━━━━━━━━━━━━━━
*{title}*
{message}
━━━━━━━━━━━━━━━━━━━
⏱ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"""
    return bool(_send(text))

# ─── These EDIT existing messages (no spam) ───────

def system_status(status, details=""):
    icons = {"online": "🟢", "offline": "🔴", "restarting": "🔄", "halted": "🛑"}
    icon = icons.get(status.lower(), "⚪")
    tagline = {"online": "_Viper is awake. Coffee not required._",
               "halted": "_Viper is sleeping. Don't poke._"}.get(status.lower(), "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = f"""{icon} *VIPER — {status.upper()}*
━━━━━━━━━━━━━━━━━━━
{tagline}
{details}
━━━━━━━━━━━━━━━━━━━
⏱ {now}
_Updated automatically_"""
    return bool(_edit_or_send(text, "status_msg"))

def notify_balance(balance, pnl_24h=0, pnl_7d=0, positions=0, last_signal=""):
    """Balance update — EDIT in place, not spam."""
    pnl_icon = "🟢" if pnl_24h >= 0 else "🔴"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = f"""💰 *VIPER — BALANCE*
━━━━━━━━━━━━━━━━━━━
*Balance:* `${balance:.4f}`
{pnl_icon} *24h PnL:* `{'+' if pnl_24h >= 0 else ''}${pnl_24h:.4f}`
*7d PnL:* `{'+' if pnl_7d >= 0 else ''}${pnl_7d:.4f}`
*Open positions:* {positions}
{'*Last signal:* ' + last_signal if last_signal else ''}
━━━━━━━━━━━━━━━━━━━
⏱ {now}
_Updates auto — no spam ✨_"""
    return bool(_edit_or_send(text, "balance_msg"))

def cycle_report(scanned, signals, trades, duration, balance, errors=0):
    """Cycle summary — EDIT same message each cycle."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = f"""🔄 *VIPER — CYCLE REPORT*
━━━━━━━━━━━━━━━━━━━
*Scanned:* {scanned} pairs
*Signals:* {signals}
*Trades:* {trades}
*Duration:* {duration:.1f}s
*Balance:* `${balance:.4f}`
{('⚠️ Errors: '+str(errors) if errors else '')}
━━━━━━━━━━━━━━━━━━━
⏱ {now}
_Latest cycle — previous replaced_"""
    return bool(_edit_or_send(text, "cycle_msg"))

def daily_summary(pnl_usd, trades, wins, win_rate, balance, drawdown, sharpe, regime=""):
    """Daily report — EDIT same message each day."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pnl_icon = "🟢" if pnl_usd >= 0 else "🔴"
    vibe = "Solid." if pnl_usd > 0 and win_rate > 60 else ("Green is green." if pnl_usd > 0 else ("Flat day." if pnl_usd == 0 else "Rough one."))
    text = f"""📊 *VIPER — DAILY REPORT*
━━━━━━━━━━━━━━━━━━━
{now}
{regime or 'N/A'} regime

*Trades:* {trades} | *Wins:* {wins} ({win_rate:.0f}%)
{pnl_icon} *PnL:* `{'+' if pnl_usd >= 0 else ''}${pnl_usd:.4f}`

*Balance:* `${balance:.4f}`
*Drawdown:* {drawdown:.2f}% | *Sharpe:* {sharpe:.2f}

_{vibe}_
━━━━━━━━━━━━━━━━━━━
🐍 _Viper keeps the books._"""
    return bool(_edit_or_send(text, "daily_msg"))


def positions_list(positions: list, balance: float = 0):
    """Open positions list — EDIT in place, updates each cycle."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not positions:
        text = f"""📋 *VIPER — OPEN POSITIONS*
━━━━━━━━━━━━━━━━━━━
No open positions.
*Balance:* `${balance:.4f}`
━━━━━━━━━━━━━━━━━━━
⏱ {now}
_Updates each cycle_"""
        return bool(_edit_or_send(text, "positions_msg"))

    lines = []
    for p in positions:
        sym = p.get("symbol", "?")
        side = p.get("side", "?").upper()
        contracts = float(p.get("contracts", 0))
        entry = float(p.get("entryPrice", 0))
        mark = float(p.get("markPrice", 0))
        liq = float(p.get("liquidationPrice", 0))
        upnl = float(p.get("unrealizedPnl", 0))
        sl = p.get("stopLossPrice", "—")
        tp = p.get("takeProfitPrice", "—")
        if sl == 0 or sl == "0":
            sl = "—"
        if tp == 0 or tp == "0":
            tp = "—"

        pnl_icon = "🟢" if upnl >= 0 else "🔴"
        pnl_pct = ((mark - entry) / entry * 100) if side == "LONG" else ((entry - mark) / entry * 100)
        sl_dist = ((entry - liq) / entry * 100) if liq else 0

        icon = "🗻" if side == "LONG" else "⛰️"
        lines.append(f"""{icon} *{sym}* | {side} x{contracts}
  Entry: `{entry}` → Mark: `{mark}`
  {pnl_icon} PnL: `{'+' if upnl >= 0 else ''}{upnl:.4f}` ({pnl_pct:.2f}%)
  🛡 SL: `{sl}` 🎯 TP: `{tp}`
  ⚠️ Liq: `{liq}` ({sl_dist:.1f}% away)""")

    text = f"""📋 *VIPER — OPEN POSITIONS ({len(positions)})*
━━━━━━━━━━━━━━━━━━━
{chr(10).join(lines)}
━━━━━━━━━━━━━━━━━━━
*Balance:* `${balance:.4f}`
⏱ {now}
_Updates each cycle_"""
    return bool(_edit_or_send(text, "positions_msg"))
