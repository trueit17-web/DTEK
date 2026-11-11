#!/usr/bin/env bash
# Привязка WhatsApp к боту по коду (без сканирования QR).
#   ./wa-link.sh 380501234567
cd "$(dirname "$0")" || exit 1
if [ -t 0 ]; then
    exec docker compose exec bot python bot.py wa-link "$@"
else
    exec docker compose exec -T bot python bot.py wa-link "$@"
fi
