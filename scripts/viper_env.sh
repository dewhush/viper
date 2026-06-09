#!/bin/bash
# Unlock/lock Viper env keys
# Usage: source viper_env.sh unlock || lock

ACTION="${1:-status}"
VAULT="/root/trader/.env.vault"
ENV_FILE="/root/trader/.env"

case "$ACTION" in
  unlock)
    if [ -f "$VAULT" ]; then
      openssl aes-256-cbc -d -pbkdf2 -in "$VAULT" -out "$ENV_FILE" 2>/dev/null
      chmod 600 "$ENV_FILE"
      echo "🔓 Env unlocked"
    elif [ -f "$ENV_FILE" ] && [ -s "$ENV_FILE" ]; then
      echo "🔓 Already unlocked"
    else
      echo "❌ Nothing to unlock"
      return 1
    fi
    ;;
  lock)
    if [ -f "$ENV_FILE" ] && [ -s "$ENV_FILE" ]; then
      openssl aes-256-cbc -salt -pbkdf2 -in "$ENV_FILE" -out "$VAULT" 2>/dev/null
      chmod 600 "$VAULT"
      > "$ENV_FILE"
      echo "🔒 Env locked"
    else
      echo "🔒 Already locked"
    fi
    ;;
  status)
    if [ -f "$VAULT" ]; then echo "🔒 Locked ($(wc -c < "$VAULT") bytes)"
    elif [ -f "$ENV_FILE" ] && [ -s "$ENV_FILE" ]; then echo "🔓 Unlocked"
    else echo "❓ No env"
    fi
    ;;
esac
