#!/usr/bin/env python3
"""
HERMES / VIPER — Telegram Notification Module
Hybrid style: Professional trades, personality for daily/system
Bot: @dewviper_bot

Usage:
    from hermes_notify import send_trade, trade_closed, daily_summary, alert, system_status, kill_switch
"""

import urllib.request
import json
import os
from datetime import datetime, timezone

# Bot config — read from env for security
BOT_ID = "8789332699"
BOT_SECRET = os.environ.get("HERMES_BOT_SECRET", "***")
CHAT_ID = os.environ.get("HERMES_CHAT_ID", "5673885457")
BASE_URL = f"https://api.telegram.org/bot{BOT_ID}:{BOT_SECRET}"


def _send(text: str, parse_mode: str = "Markdown") -> bool:
    url = f"{BASE_URL}/sendMessage"
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        print(f"[hermes_notify] Telegram send failed: {e}")
        return False


# ═══════════════════════════════════════════════════
#  TRADE SIGNAL — Professional, clean, data-driven
# ═══════════════════════════════════════════════════
def send_trade(pair: str, side: str, entry: float, size: float,
               tp1: float, tp2: float, sl: float,
               risk_pct: float, rr: str, strategy: str = "",
               confidence: float = 0.0, regime: str = "") -> bool:
    side_icon = "📈" if side.upper() == "LONG" else "📉"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    text = f"""🐍 *VIPER — TRADE SIGNAL*
━━━━━━━━━━━━━━━━━━━
*Pair:* `{pair}`
*Side:* {side_icon} {side.upper()}
*Entry:* `${entry:,.2f}`
*Size:* `{size}` USDT

*TP1:* `${tp1:,.2f}`
*TP2:* `${tp2:,.2f}`
*SL:* `${sl:,.2f}`

*Risk:* {risk_pct:.1f}% | *R:R:* 1:{rr}
*Strategy:* {strategy or 'N/A'} | *Regime:* {regime or 'N/A'}
*Confidence:* {confidence:.0%}
━━━━━━━━━━━━━━━━━━━
⏱ {now}"""
    return _send(text)


# ═══════════════════════════════════════════════════
#  TRADE CLOSED — Clean PnL report
# ═══════════════════════════════════════════════════
def trade_closed(pair: str, side: str, entry: float, exit_price: float,
                 pnl_usd: float, pnl_pct: float, duration: str = "",
                 reason: str = "") -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    emoji = "✅" if pnl_usd >= 0 else "❌"
    color = "🟢" if pnl_usd >= 0 else "🔴"
    
    text = f"""{emoji} *VIPER — TRADE CLOSED*
━━━━━━━━━━━━━━━━━━━
*Pair:* `{pair}` | *Side:* {side.upper()}
*Entry:* `${entry:,.2f}` → *Exit:* `${exit_price:,.2f}`

{color} *PnL:* `{'+' if pnl_usd >= 0 else ''}${pnl_usd:,.4f}` ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)

*Reason:* {reason or 'TP/SL hit'}
*Duration:* {duration or 'N/A'}
━━━━━━━━━━━━━━━━━━━
⏱ {now}"""
    return _send(text)


# ═══════════════════════════════════════════════════
#  DAILY PnL SUMMARY — More personality here
# ═══════════════════════════════════════════════════
def daily_summary(trades: int, wins: int, pnl_usd: float,
                  balance: float, drawdown: float = 0.0,
                  sharpe: float = 0.0, regime: str = "") -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    win_rate = (wins / trades * 100) if trades > 0 else 0
    pnl_icon = "🟢" if pnl_usd >= 0 else "🔴"
    
    # Pick a vibe line based on performance
    if pnl_usd > 0 and win_rate > 60:
        vibe = "Solid. Not bad for a snake."
    elif pnl_usd > 0:
        vibe = "Green is green."
    elif pnl_usd == 0:
        vibe = "Flat day. Could be worse."
    else:
        vibe = "Rough one. Tomorrow's another day."
    
    text = f"""📊 *VIPER — DAILY REPORT*
━━━━━━━━━━━━━━━━━━━
{now}
_{regime or 'N/A'} regime_

*Trades:* {trades} | *Wins:* {wins} ({win_rate:.0f}%)
{pnl_icon} *PnL:* `{'+' if pnl_usd >= 0 else ''}${pnl_usd:,.4f}`

*Balance:* `${balance:,.4f}`
*Drawdown:* {drawdown:.2f}% | *Sharpe:* {sharpe:.2f}

_{vibe}_
━━━━━━━━━━━━━━━━━━━
🐍 _Viper keeps the books._"""
    return _send(text)


# ═══════════════════════════════════════════════════
#  SYSTEM ALERT — Clean, contextual
# ═══════════════════════════════════════════════════
def alert(title: str, message: str, severity: str = "info") -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    icons = {"info": "ℹ️", "warn": "⚠️", "error": "🚨", "critical": "💀"}
    icon = icons.get(severity, "ℹ️")
    
    text = f"""{icon} *VIPER — {severity.upper()}*
━━━━━━━━━━━━━━━━━━━
*{title}*

{message}
━━━━━━━━━━━━━━━━━━━
⏱ {now}"""
    return _send(text)


# ═══════════════════════════════════════════════════
#  SYSTEM STATUS — Mixed professional + character
# ═══════════════════════════════════════════════════
def system_status(status: str, details: str = "") -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    icons = {"online": "🟢", "offline": "🔴", "restarting": "🔄", "halted": "🛑"}
    icon = icons.get(status.lower(), "⚪")
    
    # Character touch for online status
    tagline = ""
    if status.lower() == "online":
        tagline = "_Viper is awake. Coffee not required._"
    elif status.lower() == "halted":
        tagline = "_Standing by. Waiting for your call, boss._"
    
    text = f"""{icon} *VIPER — {status.upper()}*
━━━━━━━━━━━━━━━━━━━
{details or 'Status updated.'}
{tagline}
━━━━━━━━━━━━━━━━━━━
⏱ {now}"""
    return _send(text)


# ═══════════════════════════════════════════════════
#  KILL SWITCH — Serious, no jokes
# ═══════════════════════════════════════════════════
def kill_switch(positions_closed: int, total_pnl: float) -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pnl_color = "🟢" if total_pnl >= 0 else "🔴"
    
    text = f"""🛑 *VIPER — KILL SWITCH ACTIVATED*
━━━━━━━━━━━━━━━━━━━
All positions closed immediately.

*Positions closed:* {positions_closed}
{pnl_color} *Session PnL:* `{'+' if total_pnl >= 0 else ''}${total_pnl:,.4f}`

⚠️ Manual restart required to resume trading.
━━━━━━━━━━━━━━━━━━━
⏱ {now}"""
    return _send(text)


# ═══════════════════════════════════════════════════
#  QUICK BALANCE CHECK — For on-demand request
# ═══════════════════════════════════════════════════
def balance_check(exchange: str, balance: float, positions: int = 0,
                  daily_pnl: float = 0.0) -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pnl_icon = "🟢" if daily_pnl >= 0 else "🔴"
    
    text = f"""💰 *VIPER — BALANCE SNAPSHOT*
━━━━━━━━━━━━━━━━━━━
*Exchange:* {exchange}
*Balance:* `${balance:,.4f}`
*Open positions:* {positions}

{pnl_icon} *Today:* `{'+' if daily_pnl >= 0 else ''}${daily_pnl:,.4f}`
━━━━━━━━━━━━━━━━━━━
⏱ {now}"""
    return _send(text)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("Sending test notifications...")
        send_trade("BTC/USDT", "LONG", 64067.9, 6.40, 64800, 65500, 63500, 2.5, "2.2", "Mean Reversion", 0.82, "Ranging")
        trade_closed("DOGE/USDT", "LONG", 0.342, 0.351, 0.0026, 2.63, "4h 12m", "TP1 hit")
        daily_summary(5, 3, 0.0089, 1.2345, 1.2, 1.8, "Trending")
        alert("High Slippage", "DOGE/USDT fill was 0.23% from intended entry (threshold: 0.2%)", "warn")
        system_status("online", "All systems nominal.\\nBybit API: OK\\nWARP tunnel: Active\\nTailscale: Connected")
        kill_switch(2, -0.0045)
        balance_check("Bybit", 1.2345, 1, 0.0089)
        print("Done! Check Telegram.")
    else:
        print("Usage: python3 hermes_notify.py test")
