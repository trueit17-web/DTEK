#!/usr/bin/env bash
# Привязка WhatsApp к боту по коду (без сканирования QR).
#   ./wa-link.sh 380501234567
cd "$(dirname "$0")" || exit 1

if ! grep -q '^WHATSAPP_ENABLED=1' .env 2>/dev/null; then
    echo "Включаю WhatsApp в .env..."
    key="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    sed -i '/^COMPOSE_PROFILES=/d; /^WHATSAPP_ENABLED=/d; /^WAHA_API_KEY=/d' .env
    printf 'COMPOSE_PROFILES=whatsapp\nWHATSAPP_ENABLED=1\nWAHA_API_KEY=%s\n' "$key" >> .env
    grep -q '^WHATSAPP_CHAT_IDS=' .env || echo 'WHATSAPP_CHAT_IDS=' >> .env
fi

echo "Запускаю шлюз WhatsApp (первый раз скачивается ~1 ГБ, это может занять несколько минут)..."
docker compose --profile whatsapp up -d --build || exit 1

if ! docker compose --profile whatsapp ps --status running --services | grep -qx waha; then
    echo "⚠️ Шлюз WhatsApp не запустился. Логи:"
    docker compose --profile whatsapp logs --tail 40 waha
    exit 1
fi

if [ -t 0 ]; then
    exec docker compose exec bot python bot.py wa-link "$@"
else
    exec docker compose exec -T bot python bot.py wa-link "$@"
fi
