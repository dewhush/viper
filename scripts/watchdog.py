#!/usr/bin/env python3
"""VIPER + PHANTOM Auto-Watchdog: detects errors, auto-fixes, escalates."""
import os, sys, subprocess, json, time
from datetime import datetime, timezone

TRADER_DIR = "/root/trader"
BOUNTIES_DIR = "/root/bounties"
STATE_FILE = "/tmp/watchdog_state.json"

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(TRADER_DIR, "watchdog.log")

def state():
    s = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
        except: pass
    return s

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [WATCHDOG] {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def notify(msg):
    """Send telegram alert for unfixable errors"""
    try:
        import urllib.request
        # Get token from bashrc
        r = subprocess.run(
            ["bash", "-c", "source /root/.bashrc; echo -n $HERMES_BOT_SECRET"],
            capture_output=True, timeout=10
        )
        token = r.stdout.decode().strip()
        if not token:
            return
        chat_id = "5673885457"
        text = f"⚠️ [WATCHDOG] {msg[:200]}"
        data = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Notify fail: {e}")

def fix_warp():
    log("Fix: WARP reconnect")
    subprocess.run(["warp-cli", "--accept-tos", "disconnect"], capture_output=True, timeout=10)
    time.sleep(3)
    r = subprocess.run(["warp-cli", "--accept-tos", "connect"], capture_output=True, timeout=15)
    time.sleep(5)
    return r.returncode == 0

def fix_viper():
    log("Fix: restart viper daemon")
    subprocess.run(["pkill", "-f", "viper-daemon"], capture_output=True, timeout=5)
    subprocess.run(["pkill", "-f", "viper_engine"], capture_output=True, timeout=5)
    time.sleep(3)
    p = subprocess.Popen(
        ["bash", os.path.join(TRADER_DIR, "viper-daemon.sh"), "_daemon"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return p.pid > 0

def fix_phantom():
    log("Fix: restart Phantom orchestrator")
    subprocess.run(["pkill", "-f", "mnemonic-orchestrator"], capture_output=True, timeout=5)
    time.sleep(3)
    p = subprocess.Popen(
        ["bash", os.path.join(BOUNTIES_DIR, "mnemonic-orchestrator.sh")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return p.pid > 0

# --- Scanners ---
def scan_log(path, process_name, errors):
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            lines = f.readlines()
    except:
        errors.append(f"Cannot read {process_name} log")
        return
    recent = lines[-50:] if len(lines) > 50 else lines
    for line in recent:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip known non-errors
        if any(kw in stripped for kw in ["Cooldown", "cooldown", "remaining", "urllib3", "RequestsDependencyWarning"]):
            continue
        if "ERROR" in stripped or "CRITICAL" in stripped or "FAIL" in stripped:
            errors.append(f"{process_name}: {stripped[:150]}")

def scan_phantom():
    errors = []
    scan_log(os.path.join(BOUNTIES_DIR, "orchestrator.log"), "Phantom", errors)
    r = subprocess.run(["pgrep", "-f", "mnemonic-orchestrator"], capture_output=True, timeout=5)
    if r.returncode != 0:
        errors.append("Phantom: process dead (pgrep returned empty)")
        # Try pgrep broader
        r2 = subprocess.run(["pgrep", "-f", "orchestrator"], capture_output=True, timeout=5)
        if r2.returncode == 0:
            errors.append("Phantom: process may exist as 'orchestrator'")
    return errors

def scan_viper():
    errors = []
    scan_log(os.path.join(TRADER_DIR, "viper.log"), "Viper", errors)
    scan_log(os.path.join(TRADER_DIR, "viper-daemon.log"), "ViperDaemon", errors)
    # WARP health
    r = subprocess.run(["warp-cli", "--accept-tos", "status"], capture_output=True, timeout=10)
    status = (r.stdout or b"").decode()
    if "Connected" not in status and "Connecting" not in status:
        errors.append("WARP: not connected")
    # Viper process
    r = subprocess.run(["pgrep", "-f", "viper-daemon"], capture_output=True, timeout=5)
    if r.returncode != 0:
        errors.append("Viper: daemon process dead")
    return errors

# --- Main ---
def main():
    s = state()
    log("Watchdog scan starting...")

    phantom_errors = scan_phantom()
    viper_errors = scan_viper()
    all_errors = phantom_errors + viper_errors

    if not all_errors:
        log("All clear.")
        s["last_ok"] = datetime.now(timezone.utc).isoformat()
        save_state(s)
        return

    log(f"Detected {len(all_errors)} issue(s):")
    for e in all_errors:
        log(f"  - {e}")

    fixed, unfixable = [], []
    for err in all_errors:
        el = err.lower()
        if "warp" in el and "not connect" in el:
            if fix_warp():
                fixed.append("WARP reconnected")
            else:
                unfixable.append(err)
        elif "viper" in el and "process dead" in el:
            if fix_viper():
                fixed.append("Viper restarted")
            else:
                unfixable.append(err)
        elif "phantom" in el and "process dead" in el:
            if fix_phantom():
                fixed.append("Phantom restarted")
            else:
                unfixable.append(err)
        else:
            unfixable.append(err)

    if fixed:
        log(f"Fixed: {', '.join(fixed)}")
    if unfixable:
        log(f"Unfixable: {len(unfixable)} issue(s)")
        for u in unfixable[:3]:
            log(f"  - {u}")
        notify("Unfixable: " + "; ".join(unfixable[:3]))

    s["last_fix"] = {"time": datetime.now(timezone.utc).isoformat(), "fixed": fixed}
    s.setdefault("error_count", {})
    for e in all_errors:
        key = e.split(":")[0] if ":" in e else e[:20]
        s["error_count"][key] = s["error_count"].get(key, 0) + 1
    save_state(s)

if __name__ == "__main__":
    main()
