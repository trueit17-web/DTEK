# DTEK + EcoFlow бот

Telegram-бот, который по слову **«статус»** присылает:

- режим работы **EcoFlow DELTA Pro 3**: от сети или от батареи;
- при работе от батареи — заряд (%), доступный запас (Вт·ч), лимит разряда, SoH, температуру ячеек, время до разряда и текущую нагрузку (AC / DC);
- **график отключений ДТЭК** (г. Днепр, группа 3.1) на сегодня и завтра и что по графику сейчас.

Кроме того, бот **сам присылает уведомления**:

- 🔴 свет пропал (станция перешла на батарею) и сколько примерно хватит заряда;
- 🟢 свет появился;
- 🪫 заряд упал ниже порога (по умолчанию 20%);
- 🔔 ДТЭК изменил график для вашей группы;
- ⚪ станция пропала из сети EcoFlow / снова на связи.

## Быстрый старт (одна команда)

На сервере с Ubuntu/Debian:

```bash
curl -fsSL https://raw.githubusercontent.com/trueit17-web/DTEK/main/install.sh | sudo bash
```

Скрипт сам поставит Docker (если его нет), скачает бота в `/opt/dtek-bot`, спросит токены и запустит контейнер. Бот будет автоматически подниматься после перезагрузки сервера.

> Репозиторий приватный? Тогда сначала клонируйте его (`git clone https://github.com/trueit17-web/DTEK.git && cd DTEK`) и запустите `sudo ./install.sh` из этой папки.

### Вручную

```bash
git clone https://github.com/trueit17-web/DTEK.git
cd DTEK
cp .env.example .env
nano .env                    # впишите токены
docker compose up -d --build
```

## Что нужно подготовить

| Переменная | Где взять |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Создать бота у [@BotFather](https://t.me/BotFather) |
| `ECOFLOW_ACCESS_KEY`, `ECOFLOW_SECRET_KEY` | [developer-eu.ecoflow.com](https://developer-eu.ecoflow.com/) → войти аккаунтом EcoFlow → *Security* → создать ключи |
| `ECOFLOW_DEVICE_SN` | Серийный номер станции (приложение EcoFlow → устройство → настройки, начинается с `MR51`) |
| `ALLOWED_USER_IDS` | Ваш Telegram ID: отправьте боту `/id`. Если не задать, бот ответит любому, кто его найдет, и уведомления никуда не придут |

Остальные настройки (группа ДТЭК, интервал проверки, порог низкого заряда, емкость с доп. батареями) описаны в [`.env.example`](.env.example).

## Команды бота

- `статус` / кнопка **Статус** / `/status` — полный отчет (также понимает «свет», «график», «экофлоу»);
- `/id` — показать ваш Telegram ID;
- `/start` — приветствие и кнопка.

## Управление

```bash
cd /opt/dtek-bot             # или папка, куда вы клонировали
docker compose logs -f       # логи
docker compose restart       # перезапуск (например, после правки .env)
docker compose down          # остановить
git pull && docker compose up -d --build   # обновить до новой версии
```

## Откуда данные

- **EcoFlow**: официальный EcoFlow Developer API (`/iot-open/sign/device/quota/all`), подпись HMAC-SHA256.
- **ДТЭК**: открытый JSON [Baskerville42/outage-data-ua](https://github.com/Baskerville42/outage-data-ua), который регулярно обновляется с сайта ДТЭК.
