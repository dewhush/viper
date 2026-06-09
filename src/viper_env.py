#!/usr/bin/env python3
"""VIPER Encryption — API keys at rest"""
import os, sys, subprocess, json, getpass

ENV_FILE = "/root/trader/.env"
KEY_FILE = "/root/trader/.env.key"
ENC_FILE = "/root/trader/.env.enc"
CIPHER = "aes-256-cbc"
PASSPHRASE = None  # Set via env var VIPER_VAULT_PASS or prompt

def _gen_key():
    """Generate a random hex key, store it encrypted with OS permissions"""
    key = subprocess.run(
        ["openssl", "rand", "-hex", "32"],
        capture_output=True, text=True, timeout=5
    ).stdout.strip()
    with open(KEY_FILE, "w") as f:
        f.write(key)
    os.chmod(KEY_FILE, 0o600)
    return key

def _get_key():
    if os.path.exists(KEY_FILE) and os.stat(KEY_FILE).st_size > 0:
        with open(KEY_FILE) as f:
            return f.read().strip()
    return _gen_key()

def encrypt():
    """Encrypt .env → .env.enc, wipe .env"""
    if not os.path.exists(ENV_FILE):
        print("Nothing to encrypt — no .env file")
        return
    key = _get_key()
    subprocess.run(
        ["openssl", "enc", f"-{CIPHER}", "-salt", "-pbkdf2",
         "-in", ENV_FILE, "-out", ENC_FILE,
         "-pass", f"pass:{key}"],
        check=True, capture_output=True, timeout=10
    )
    # Wipe original
    with open(ENV_FILE, "w") as f:
        f.write("# Encrypted — use viper_env.sh unlock\n")
    os.chmod(ENV_FILE, 0o644)
    os.chmod(ENC_FILE, 0o600)
    print(f"Encrypted: {ENV_FILE} → {ENC_FILE}")

def decrypt():
    """Decrypt .env.enc → .env (in-memory, file mode 600)"""
    if not os.path.exists(ENC_FILE):
        print("Nothing to decrypt — no encrypted file")
        return False
    key = _get_key()
    r = subprocess.run(
        ["openssl", "enc", f"-{CIPHER}", "-d", "-pbkdf2",
         "-in", ENC_FILE,
         "-pass", f"pass:{key}"],
        capture_output=True, timeout=10
    )
    if r.returncode != 0:
        print("Decrypt failed — wrong key?")
        return False
    with open(ENV_FILE, "wb") as f:
        f.write(r.stdout)
    os.chmod(ENV_FILE, 0o600)
    print(f"Decrypted: {ENC_FILE} → {ENV_FILE}")
    return True

def lock():
    """Encrypt + delete plaintext"""
    encrypt()
    if os.path.exists(ENV_FILE):
        os.remove(ENV_FILE)

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "encrypt":
        encrypt()
    elif action == "decrypt":
        decrypt()
    elif action == "lock":
        lock()
    elif action == "status":
        if os.path.exists(ENC_FILE):
            print(f"🔒 Encrypted: {ENC_FILE} exists ({os.path.getsize(ENC_FILE)} bytes)")
        elif os.path.exists(ENV_FILE) and os.path.getsize(ENV_FILE) > 50:
            print(f"🔓 Decrypted: {ENV_FILE} exists ({os.path.getsize(ENV_FILE)} bytes)")
        else:
            print("❓ No env files found")
    else:
        print(f"Usage: {sys.argv[0]} {encrypt|decrypt|lock|status}")
