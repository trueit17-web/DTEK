#!/usr/bin/env bash
# Установка и запуск бота одной командой:
#   curl -fsSL https://raw.githubusercontent.com/trueit17-web/DTEK/main/install.sh | sudo bash
# или из клонированной папки:  sudo ./install.sh
set -euo pipefail

REPO_URL="https://github.com/trueit17-web/DTEK.git"
INSTALL_DIR="${INSTALL_DIR:-/opt/dtek-bot}"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запустите от root (через sudo)."

# 1. Docker
if ! command -v docker >/dev/null 2>&1; then
    say "Устанавливаю Docker..."
    curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version >/dev/null 2>&1; then
    say "Устанавливаю плагин docker compose..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq || true
        apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 \
            || apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q docker-compose-plugin >/dev/null 2>&1 || true
    elif command -v yum >/dev/null 2>&1; then
        yum install -y -q docker-compose-plugin >/dev/null 2>&1 || true
    fi
fi
if ! docker compose version >/dev/null 2>&1; then
    say "Пакета нет в репозитории, скачиваю docker compose с GitHub..."
    mkdir -p /usr/local/lib/docker/cli-plugins
    curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi
docker compose version >/dev/null 2>&1 || die "не удалось установить docker compose."
command -v git >/dev/null 2>&1 || { say "Устанавливаю git..."; (apt-get update && apt-get install -y git) || die "установите git вручную."; }

# 2. Код
if [ -f "$(dirname "$0")/docker-compose.yml" ] && [ -f "$(dirname "$0")/bot.py" ]; then
    INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
    say "Использую текущую папку: $INSTALL_DIR"
elif [ -d "$INSTALL_DIR/.git" ]; then
    say "Обновляю код в $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only
else
    say "Клонирую репозиторий в $INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# 3. Настройки (.env)
ask() {  # ask VAR "Вопрос" [default]
    local var="$1" prompt="$2" def="${3:-}" val=""
    if [ -n "$def" ]; then prompt="$prompt [$def]"; fi
    read -r -p "$prompt: " val </dev/tty || true
    val="${val:-$def}"
    printf '%s=%s\n' "$var" "$val" >> .env
}

if [ ! -f .env ]; then
    say "Настройка бота (значения сохранятся в $INSTALL_DIR/.env)"
    : > .env
    ask TELEGRAM_BOT_TOKEN  "Токен Telegram-бота от @BotFather (пусто — без Telegram)"
    ask ECOFLOW_ACCESS_KEY  "EcoFlow Access Key (developer-eu.ecoflow.com)"
    ask ECOFLOW_SECRET_KEY  "EcoFlow Secret Key"
    ask ECOFLOW_DEVICE_SN   "Серийный номер станции DELTA Pro 3 (MR51...)"
    ask ALLOWED_USER_IDS    "Ваш Telegram ID (через запятую, если несколько; пусто = бот доступен всем)"
    ask DTEK_GROUP          "Группа отключений ДТЭК" "3.1"
    cat >> .env <<'ENV'
DTEK_REGION=dnipro
ECOFLOW_API_HOST=https://api-e.ecoflow.com
DELTA_PRO_3_CAPACITY_WH=4096
NOTIFY_CHAT_IDS=
MONITOR_INTERVAL_SEC=60
LOW_BATTERY_SOC=20
NOTIFY_SCHEDULE_CHANGES=1
ENV
    read -r -p "Подключить WhatsApp (уведомления в группу)? [y/N]: " wa </dev/tty || true
    if [[ "${wa:-n}" =~ ^[YyДд] ]]; then
        key="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        {
            echo "COMPOSE_PROFILES=whatsapp"
            echo "WHATSAPP_ENABLED=1"
            echo "WAHA_API_KEY=$key"
            echo "WHATSAPP_CHAT_IDS="
        } >> .env
    else
        printf 'COMPOSE_PROFILES=\nWHATSAPP_ENABLED=0\nWAHA_API_KEY=\nWHATSAPP_CHAT_IDS=\n' >> .env
    fi
    chmod 600 .env
else
    say "Файл .env уже есть, оставляю его как есть."
fi

# 4. Запуск
say "Собираю и запускаю контейнер..."
docker compose up -d --build

say "Готово! Логи: cd $INSTALL_DIR && docker compose logs -f"

if grep -q '^WHATSAPP_ENABLED=1' .env; then
    cat <<'TXT'

==================== WhatsApp ====================
1. На телефоне с ОТДЕЛЬНЫМ номером для бота откройте WhatsApp →
   Настройки → Связанные устройства → Привязать устройство.
2. Отсканируйте QR-код, который появится ниже (он обновляется каждые ~20 с).
   Если QR в терминале не читается — отправьте Telegram-боту команду /wa_qr.
3. Добавьте этот номер в нужную группу WhatsApp и напишите в группе «id».
   Бот ответит ID группы — впишите его в .env:  WHATSAPP_CHAT_IDS=...@g.us
   и выполните:  docker compose up -d
После сканирования нажмите Ctrl+C (бот продолжит работать).
==================================================
TXT
    sleep 5
    docker compose logs -f --tail 60 waha || true
fi
