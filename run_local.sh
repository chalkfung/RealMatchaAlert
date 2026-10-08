#!/usr/bin/env bash
# Local runner for cron. Put your secrets in .env next to this file:
#   TELEGRAM_BOT_TOKEN=123456:ABC...
#   TELEGRAM_CHAT_ID=123456789
cd "$(dirname "$0")"
set -a; [ -f .env ] && . ./.env; set +a
exec python3 matcha_alert.py "$@" >> scan.log 2>&1
