#!/usr/bin/env python3
"""
Telegram-бот для мониторинга графика отключений ДТЭК (г. Днепр, группа 3.1)
и состояния зарядной станции EcoFlow DELTA Pro 3.

По текстовому сообщению «статус» (или команде /status) бот сообщает:
1. Актуальный график отключений ДТЭК Днепровские электросети для группы 3.1
   на сегодня и завтра, а также текущий статус по графику (есть свет или отключение).
2. Режим работы станции EcoFlow DELTA Pro 3: «Сеть (байпас / зарядка)» или «Станция (от батареи)».
3. Если текущий режим работы — от станции EcoFlow DELTA Pro 3:
   - Основные параметры заряда: уровень заряда (SoC, %), доступный запас энергии (Вт·ч),
     лимит разряда (нижний порог SoC), здоровье АКБ (SoH, %) и температура ячеек (°C)
   - Примерное время до полного разряда (ч и мин)
   - Текущую потребляемую мощность нагрузки (общая в Вт + разбивка AC / DC)

Все настройки задаются переменными окружения (см. .env.example).
Запуск на сервере: см. README.md (docker compose up -d --build).
"""

import asyncio
import datetime
import hashlib
import hmac
import html
import json
import logging
import os
import random
import re
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiohttp import web

# =====================================================================
# КОНФИГУРАЦИЯ (Днепр, группа 3.1 + EcoFlow DELTA Pro 3)
# =====================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")

# Необязательно: список Telegram ID через запятую, кому разрешено пользоваться ботом.
# Пусто — бот отвечает всем.
ALLOWED_USER_IDS: Set[int] = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

# Ключи EcoFlow Developer Platform (https://developer-eu.ecoflow.com/)
ECOFLOW_ACCESS_KEY = os.getenv("ECOFLOW_ACCESS_KEY", "YOUR_ECOFLOW_ACCESS_KEY")
ECOFLOW_SECRET_KEY = os.getenv("ECOFLOW_SECRET_KEY", "YOUR_ECOFLOW_SECRET_KEY")
# Серийный номер EcoFlow DELTA Pro 3 (обычно начинается с MR5...)
ECOFLOW_DEVICE_SN = os.getenv("ECOFLOW_DEVICE_SN", "YOUR_DELTA_PRO_3_SN")
ECOFLOW_API_HOST = os.getenv("ECOFLOW_API_HOST", "https://api-e.ecoflow.com").rstrip("/")

# Паспортная емкость EcoFlow DELTA Pro 3 (4096 Вт·ч; при подключении доп. батарей можно увеличить до 8192 или 12288)
DELTA_PRO_3_CAPACITY_WH = float(os.getenv("DELTA_PRO_3_CAPACITY_WH", "4096"))

# Настройки ДТЭК Днепровские электросети — г. Днепр, группа 3.1
DTEK_REGION = os.getenv("DTEK_REGION", "dnipro")
DTEK_CITY_TITLE = "г. Днепр (ДТЭК Днепровские электросети)"
DTEK_GROUP = os.getenv("DTEK_GROUP", "3.1")
KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Фоновые уведомления (пропал/появился свет, низкий заряд, изменение графика)
# Кому слать: NOTIFY_CHAT_IDS, а если не задано — пользователям из ALLOWED_USER_IDS.
NOTIFY_CHAT_IDS: Set[int] = {
    int(x) for x in os.getenv("NOTIFY_CHAT_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()
} or set(ALLOWED_USER_IDS)
MONITOR_INTERVAL_SEC = max(30, int(os.getenv("MONITOR_INTERVAL_SEC", "60")))
LOW_BATTERY_SOC = float(os.getenv("LOW_BATTERY_SOC", "20"))
NOTIFY_SCHEDULE_CHANGES = os.getenv("NOTIFY_SCHEDULE_CHANGES", "1").lower() in ("1", "true", "yes", "on")

# WhatsApp через шлюз WAHA (https://waha.devlike.pro), запускается рядом в docker compose
WHATSAPP_ENABLED = os.getenv("WHATSAPP_ENABLED", "0").lower() in ("1", "true", "yes", "on")
WAHA_URL = os.getenv("WAHA_URL", "http://waha:3000").rstrip("/")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")
WAHA_SESSION = os.getenv("WAHA_SESSION", "default")
# ID групп/чатов WhatsApp через запятую (вида 120363...@g.us). Узнать: написать «id» в группе.
WHATSAPP_CHAT_IDS: List[str] = [
    x.strip() for x in os.getenv("WHATSAPP_CHAT_IDS", "").split(",") if x.strip()
]
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))

STATUS_WORDS = {"статус", "status", "свет", "график", "экофлоу", "/status"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# =====================================================================
# 1. КЛИЕНТ ECOFLOW DELTA PRO 3 (Developer API / Quota All)
# =====================================================================
def _generate_ecoflow_headers(params: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """
    Формирует заголовки HMAC-SHA256 для EcoFlow Quota API (GET /iot-open/sign/device/quota/all).
    Документация: https://developer-eu.ecoflow.com/
    """
    nonce = str(random.randint(100000, 999999))
    timestamp = str(int(time.time() * 1000))

    query_parts: List[str] = []
    if params:
        for key in sorted(params.keys()):
            query_parts.append(f"{key}={params[key]}")

    query_str = "&".join(query_parts)
    auth_str = f"accessKey={ECOFLOW_ACCESS_KEY}&nonce={nonce}&timestamp={timestamp}"
    sign_str = f"{query_str}&{auth_str}" if query_str else auth_str

    signature = hmac.new(
        ECOFLOW_SECRET_KEY.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return {
        "accessKey": ECOFLOW_ACCESS_KEY,
        "nonce": nonce,
        "timestamp": timestamp,
        "sign": signature,
        # Content-Type не передаем: на GET-запросах с параметрами EcoFlow из-за него
        # отвечает «8521 signature is wrong».
    }


def _get_quota_float(data: Dict[str, Any], keys: List[str], default: Optional[float] = None) -> Optional[float]:
    """
    Ищет значение в плоском словаре квот EcoFlow DELTA Pro 3 как по прямому имени ключа
    (например, 'cmsBattSoc'), так и с учетом возможных префиксов модулей ('*.cmsBattSoc').
    """
    for k in keys:
        if k in data and data[k] is not None:
            try:
                return float(data[k])
            except (ValueError, TypeError):
                continue

    # Поиск по суффиксу, если API возвращает префиксированные ключи (например, hs_yj751_...cmsBattSoc)
    for k in keys:
        suffix = f".{k}"
        for full_key, val in data.items():
            if full_key.endswith(suffix) and val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue

    return default


async def fetch_device_online(session: aiohttp.ClientSession) -> Optional[bool]:
    """
    Проверяет через GET /iot-open/sign/device/list, в сети ли станция.
    Квоты (quota/all) отдаются облаком даже для офлайн-устройства (последние известные значения),
    поэтому без этой проверки бот показывал бы устаревшие данные как актуальные.
    Возвращает None, если узнать не удалось.
    """
    url = f"{ECOFLOW_API_HOST}/iot-open/sign/device/list"
    try:
        async with session.get(
            url, headers=_generate_ecoflow_headers(), timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            payload = await resp.json(content_type=None)
    except Exception:
        logger.warning("Не удалось получить список устройств EcoFlow", exc_info=True)
        return None
    if str(payload.get("code")) != "0":
        return None
    for dev in payload.get("data") or []:
        if str(dev.get("sn")) == ECOFLOW_DEVICE_SN:
            return str(dev.get("online")) == "1"
    return None


async def fetch_delta_pro_3_status(session: aiohttp.ClientSession) -> Dict[str, Any]:
    """
    Опрашивает станцию EcoFlow DELTA Pro 3 через GET /iot-open/sign/device/quota/all
    и извлекает телеметрию:
      - cmsBattSoc: общий уровень заряда (%)
      - cmsBattSoh: состояние здоровья батареи (%)
      - cmsDsgRemTime: оставшееся время до разряда (мин, 15999 = нет разряда)
      - cmsChgRemTime: оставшееся время до полного заряда (мин)
      - cmsChgDsgState: статус (0 = ожидание, 1 = разряд, 2 = заряд)
      - powInSumW: общая входная мощность (Вт)
      - powOutSumW: общая выходная потребляемая мощность (Вт)
      - powGetAcIn: входная мощность от сети AC (Вт)
      - powGetAc / powGetAcLvOut / powGetAcHvOut: выходная мощность AC (Вт)
      - powGet_12v: выходная мощность DC 12V (Вт)
      - powGetPv / powGetPvH / powGetPvL: мощность солнечных панелей (Вт)
      - cmsMinDsgSoc: установленный лимит разряда (%)
      - bmsMinCellTemp / bmsMaxCellTemp: температура ячеек АКБ (°C)
    """
    url = f"{ECOFLOW_API_HOST}/iot-open/sign/device/quota/all"
    params = {"sn": ECOFLOW_DEVICE_SN}
    headers = _generate_ecoflow_headers(params)

    async with session.get(
        url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=12)
    ) as resp:
        payload = await resp.json(content_type=None)

    code = str(payload.get("code"))
    if code == "8512":
        raise RuntimeError(
            "станция не привязана к аккаунту разработчика EcoFlow (8512 no permission). "
            "Проверьте, что ключи созданы в том же аккаунте EcoFlow, где станция добавлена в приложении, "
            "и что серийный номер указан верно"
        )
    if code != "0":
        raise RuntimeError(
            f"EcoFlow API вернул код {payload.get('code')}: {payload.get('message')}"
        )

    q: Dict[str, Any] = payload.get("data") or {}
    if not q or await fetch_device_online(session) is False:
        return {"online": False}

    # Основные параметры заряда DELTA Pro 3
    soc = _get_quota_float(q, ["cmsBattSoc", "bmsBattSoc", "soc", "f32ShowSoc"], 0.0) or 0.0
    soh = _get_quota_float(q, ["cmsBattSoh", "bmsBattSoh", "soh"], None)
    min_dsg_soc = _get_quota_float(q, ["cmsMinDsgSoc", "minDsgSoc"], 0.0) or 0.0
    max_chg_soc = _get_quota_float(q, ["cmsMaxChgSoc", "maxChgSoc"], 100.0) or 100.0
    backup_reserve_soc = _get_quota_float(
        q, ["energyBackupStartSoc", "backupReverseSoc"], None
    )

    # Мощности входа и выхода (Вт)
    pow_in_sum = _get_quota_float(q, ["powInSumW", "wattsInSum", "inv.inputWatts"], 0.0) or 0.0
    pow_out_sum = abs(
        _get_quota_float(q, ["powOutSumW", "wattsOutSum", "inv.outputWatts"], 0.0) or 0.0
    )
    ac_in_watts = abs(_get_quota_float(q, ["powGetAcIn", "inv.acInWatts"], 0.0) or 0.0)
    ac_out_watts = abs(
        _get_quota_float(q, ["powGetAc", "powGetAcLvOut", "powGetAcHvOut"], 0.0) or 0.0
    )
    dc_12v_watts = abs(_get_quota_float(q, ["powGet12v", "powGet_12v", "carWatts"], 0.0) or 0.0)
    pv_watts = (
        abs(_get_quota_float(q, ["powGetPv", "powGetPvH"], 0.0) or 0.0)
        + abs(_get_quota_float(q, ["powGetPv2", "powGetPvL"], 0.0) or 0.0)
    )

    # Флаг и напряжение входа переменного тока (AC Grid)
    ac_in_flag = _get_quota_float(q, ["plugInInfoAcInFlag"], 0.0) or 0.0
    ac_in_vol = _get_quota_float(q, ["plugInInfoAcInVol", "inv.acInVol"], 0.0) or 0.0
    if ac_in_vol > 1000:
        ac_in_vol /= 1000.0

    # Оставшееся время разряда / заряда (в минутах; на DELTA Pro 3 максимум 15999 означает отсутствие разряда)
    dsg_rem_min = int(
        round(_get_quota_float(q, ["cmsDsgRemTime", "ems.dsgRemainTime", "remainTime"], 0.0) or 0.0)
    )
    chg_rem_min = int(
        round(_get_quota_float(q, ["cmsChgRemTime", "ems.chgRemainTime"], 0.0) or 0.0)
    )
    chg_dsg_state = int(round(_get_quota_float(q, ["cmsChgDsgState"], 0.0) or 0.0))

    # Температура ячеек батареи (°C)
    temp_min = _get_quota_float(q, ["bmsMinCellTemp", "minCellTemp"], None)
    temp_max = _get_quota_float(q, ["bmsMaxCellTemp", "maxCellTemp"], None)

    # Определяем режим работы EcoFlow DELTA Pro 3:
    # «grid» (Сеть) — если на входе AC есть мощность (ac_in_watts > 15 Вт),
    # либо присутствует сетевое напряжение AC >= 100 В, либо общий вход pow_in_sum > 15 Вт (без учета только солнечных панелей при 0 AC).
    # flowInfoAcIn у DELTA Pro 3: 0 — сети на входе нет, >0 — сеть подключена
    # (остается >0 даже при полной батарее и нулевой нагрузке, когда входная мощность 0 Вт).
    flow_ac_in = _get_quota_float(q, ["flowInfoAcIn"], None)
    has_grid_power = (
        (flow_ac_in is not None and flow_ac_in > 0)
        or (ac_in_watts > 15.0)
        or (ac_in_vol >= 100.0)
        or (ac_in_flag == 1.0 and pow_in_sum > 15.0)
        or (pow_in_sum > 15.0 and pv_watts == 0.0)
    )
    mode = "grid" if has_grid_power else "station"

    # Расчет доступного запаса энергии (Вт·ч) с учетом нижнего порога разряда cmsMinDsgSoc
    usable_soc_pct = max(0.0, soc - min_dsg_soc)
    capacity_wh = _get_quota_float(q, ["cmsBattFullEnergy"], None) or DELTA_PRO_3_CAPACITY_WH
    remaining_wh = int(round((usable_soc_pct / 100.0) * capacity_wh))

    # Если станция отдает нагрузку, а cmsDsgRemTime равен 0 или техническому максимуму (5999 / 15999),
    # рассчитываем время до разряда аналитически по текущей мощности и доступной емкости DELTA Pro 3
    calculated_rem_min: Optional[int] = None
    if (dsg_rem_min <= 0 or dsg_rem_min >= 5900) and pow_out_sum > 5.0:
        # Учитываем КПД инвертора ~90%
        effective_wh = remaining_wh * 0.90
        calculated_rem_min = int(round((effective_wh / pow_out_sum) * 60.0))

    return {
        "online": True,
        "mode": mode,
        "soc": round(soc, 1),
        "soh": int(round(soh)) if soh is not None else None,
        "min_dsg_soc": int(round(min_dsg_soc)),
        "max_chg_soc": int(round(max_chg_soc)),
        "backup_reserve_soc": int(round(backup_reserve_soc)) if backup_reserve_soc is not None else None,
        "remaining_wh": remaining_wh,
        "pow_in_sum": int(round(pow_in_sum)),
        "pow_out_sum": int(round(pow_out_sum)),
        "ac_in_watts": int(round(ac_in_watts)),
        "ac_out_watts": int(round(ac_out_watts)),
        "dc_12v_watts": int(round(dc_12v_watts)),
        "pv_watts": int(round(pv_watts)),
        "dsg_rem_min": dsg_rem_min,
        "chg_rem_min": chg_rem_min,
        "calculated_rem_min": calculated_rem_min,
        "chg_dsg_state": chg_dsg_state,
        "flow_ac_in": flow_ac_in,
        "temp_min": int(round(temp_min)) if temp_min is not None else None,
        "temp_max": int(round(temp_max)) if temp_max is not None else None,
    }


def format_duration_minutes(minutes: int, calculated_fallback: Optional[int] = None) -> str:
    """Время до разряда; сервисные значения 5999/15999 мин отбрасываются (тогда берется расчет)."""
    valid_min = minutes if (0 < minutes < 5900) else (calculated_fallback or 0)
    if valid_min <= 0:
        return "нагрузки нет"
    hours, mins = divmod(valid_min, 60)
    return f"~{hours} ч {mins} мин" if hours else f"~{mins} мин"


# =====================================================================
# 2. КЛИЕНТ ГРАФИКОВ ОТКЛЮЧЕНИЙ ДТЭК (г. Днепр, группа 3.1)
# =====================================================================
def _idx_to_time(idx: int) -> str:
    """Переводит индекс 30-минутного интервала (0..48) в формат ЧЧ:ММ."""
    h, m = divmod(idx * 30, 60)
    return f"{h:02d}:{m:02d}"


def _parse_dtek_day_slots(day_slots: Dict[str, str]) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    """
    Разбирает 24 часовых слота ДТЭК ('1'..'24') в список 48 получасовых состояний
    и склеивает соседние слоты отключений в интервалы (начало, конец, тип).
    """
    half_hours: List[str] = []
    for hour in range(1, 25):
        state = str(day_slots.get(str(hour), "yes")).lower()
        if state == "no":
            half_hours.extend(["off", "off"])
        elif state == "first":
            half_hours.extend(["off", "on"])
        elif state == "second":
            half_hours.extend(["on", "off"])
        elif state in ("maybe", "possible"):
            half_hours.extend(["maybe", "maybe"])
        elif state == "mfirst":
            half_hours.extend(["maybe", "on"])
        elif state == "msecond":
            half_hours.extend(["on", "maybe"])
        else:
            half_hours.extend(["on", "on"])

    intervals: List[Tuple[str, str, str]] = []
    current_type: Optional[str] = None
    start_idx = 0

    for idx, slot_state in enumerate(half_hours):
        if slot_state in ("off", "maybe"):
            if current_type is None:
                current_type = slot_state
                start_idx = idx
            elif current_type != slot_state:
                intervals.append((_idx_to_time(start_idx), _idx_to_time(idx), current_type))
                current_type = slot_state
                start_idx = idx
        else:
            if current_type is not None:
                intervals.append((_idx_to_time(start_idx), _idx_to_time(idx), current_type))
                current_type = None

    if current_type is not None:
        intervals.append((_idx_to_time(start_idx), _idx_to_time(48), current_type))

    return intervals, half_hours


async def fetch_dtek_dnipro_schedule(
    session: aiohttp.ClientSession, region: str = DTEK_REGION, group: str = DTEK_GROUP
) -> str:
    """
    Компактный график отключений ДТЭК для группы (данные: Baskerville42/outage-data-ua).
    Показывает, есть ли свет по графику сейчас, когда ближайшее отключение
    и оставшиеся интервалы на сегодня и завтра.
    """
    url = f"https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/{region}.json"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        data = await resp.json(content_type=None)

    fact_data: Dict[str, Any] = (data.get("fact") or {}).get("data") or {}
    group_key = group if group.upper().startswith("GPV") else f"GPV{group}"
    now = datetime.datetime.now(KYIV_TZ)
    today = now.date()
    tomorrow = today + datetime.timedelta(days=1)
    now_idx = min(47, (now.hour * 60 + now.minute) // 30)

    days: Dict[datetime.date, List[str]] = {}
    for ts, groups in fact_data.items():
        if not str(ts).isdigit():
            continue
        day = datetime.datetime.fromtimestamp(int(ts), tz=KYIV_TZ).date()
        slots = (groups or {}).get(group_key) or (groups or {}).get(group)
        if day in (today, tomorrow) and slots:
            days[day] = _parse_dtek_day_slots(slots)[1]

    header = f"📅 <b>ДТЭК, группа {group}</b>"
    if today not in days:
        return f"{header}\n⚠️ График на сегодня не опубликован"

    def intervals(half: List[str], from_idx: int = 0) -> List[Tuple[int, int, str]]:
        res: List[Tuple[int, int, str]] = []
        i = from_idx
        while i < 48:
            st = half[i]
            if st in ("off", "maybe"):
                j = i
                while j < 48 and half[j] == st:
                    j += 1
                res.append((i, j, st))
                i = j
            else:
                i += 1
        return res

    def fmt(items: List[Tuple[int, int, str]]) -> str:
        return ", ".join(
            f"{_idx_to_time(a)}–{_idx_to_time(b)}" + (" (возможно)" if st == "maybe" else "")
            for a, b, st in items
        )

    half_today = days[today]
    cur = half_today[now_idx]
    lines = [header]

    if cur in ("off", "maybe"):
        end = now_idx
        while end < 48 and half_today[end] == cur:
            end += 1
        what = "🔴 Сейчас отключение" if cur == "off" else "🟡 Сейчас возможно отключение"
        lines.append(f"{what} до <b>{_idx_to_time(end)}</b>")
    else:
        upcoming = intervals(half_today, now_idx)
        if upcoming:
            nxt = f"отключение в <b>{_idx_to_time(upcoming[0][0])}</b>"
        elif tomorrow in days and intervals(days[tomorrow]):
            nxt = f"отключение завтра в <b>{_idx_to_time(intervals(days[tomorrow])[0][0])}</b>"
        else:
            nxt = "отключений не ожидается"
        lines.append(f"🟢 Сейчас свет есть · {nxt}")

    # Сегодня — только еще не закончившиеся интервалы
    left_today = [it for it in intervals(half_today) if it[1] > now_idx]
    lines.append(f"Сегодня: {fmt(left_today) if left_today else 'больше без отключений'}")
    if tomorrow in days:
        tm = intervals(days[tomorrow])
        lines.append(f"Завтра: {fmt(tm) if tm else 'без отключений'}")
    return "\n".join(lines)


async def fetch_dtek_group_fingerprint(
    session: aiohttp.ClientSession, region: str = DTEK_REGION, group: str = DTEK_GROUP
) -> Optional[str]:
    """Возвращает «отпечаток» графика группы на сегодня и следующие дни (для отслеживания изменений)."""
    url = f"https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/{region}.json"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status != 200:
            return None
        data = await resp.json(content_type=None)
    fact_data: Dict[str, Any] = (data.get("fact") or {}).get("data") or {}
    group_key = group if group.upper().startswith("GPV") else f"GPV{group}"
    today = datetime.datetime.now(KYIV_TZ).date()
    result = {}
    for ts, groups in fact_data.items():
        if not str(ts).isdigit():
            continue
        if datetime.datetime.fromtimestamp(int(ts), tz=KYIV_TZ).date() < today:
            continue
        result[ts] = (groups or {}).get(group_key) or (groups or {}).get(group)
    return json.dumps(result, sort_keys=True) if result else None


# =====================================================================
# 3. СБОРКА ИТОГОВОГО ОТЧЕТА ПО КОМАНДЕ «СТАТУС»
# =====================================================================
def _fmt_soc(soc: float) -> str:
    return f"{int(soc)}%"  # 99.9 → 99%, без «округления до 100»


def format_ecoflow_section(ef: Dict[str, Any]) -> str:
    """Короткий блок о станции: режим, заряд, нагрузка, время работы."""
    if "error" in ef:
        return f"⚠️ EcoFlow: {html.escape(ef['error'])}"
    if not ef.get("online"):
        return "⚪ Станция EcoFlow не на связи"

    soc = _fmt_soc(ef["soc"])
    load = ef["pow_out_sum"]
    if ef["mode"] == "grid":
        parts = [f"🔌 Сеть есть · 🔋 {soc}"]
        chg = ef["chg_rem_min"]
        if ef["pow_in_sum"] > 15 and 0 < chg < 5900 and ef["soc"] < ef["max_chg_soc"]:
            h, m = divmod(chg, 60)
            parts.append(f"заряд еще {h} ч {m} мин" if h else f"заряд еще {m} мин")
        if load > 0:
            parts.append(f"нагрузка {load} Вт")
        return " · ".join(parts)

    rem = format_duration_minutes(ef["dsg_rem_min"], calculated_fallback=ef["calculated_rem_min"])
    line2 = f"🔋 <b>{soc}</b> · " + (f"{load} Вт · хватит на <b>{rem}</b>" if load > 0 else "нагрузки нет")
    if ef["pv_watts"] > 0:
        line2 += f" · ☀️ {ef['pv_watts']} Вт"
    return "🪫 <b>Света нет — работаем от станции</b>\n" + line2


async def build_status_report() -> str:
    """Короткий отчет: станция EcoFlow + график ДТЭК."""
    async with aiohttp.ClientSession() as session:
        dtek_task = asyncio.create_task(fetch_dtek_dnipro_schedule(session, DTEK_REGION, DTEK_GROUP))
        ecoflow_task = asyncio.create_task(fetch_delta_pro_3_status(session))
        try:
            dtek_section = await dtek_task
        except Exception as exc:
            logger.exception("Ошибка получения графика ДТЭК")
            dtek_section = f"⚠️ График ДТЭК не загрузился ({html.escape(str(exc) or exc.__class__.__name__)})"
        try:
            ef = await ecoflow_task
        except Exception as exc:
            logger.exception("Ошибка получения данных EcoFlow DELTA Pro 3")
            ef = {"error": str(exc) or exc.__class__.__name__}

    now_str = datetime.datetime.now(KYIV_TZ).strftime("%d.%m %H:%M")
    return f"🕒 {now_str}\n{format_ecoflow_section(ef)}\n\n{dtek_section}"


# =====================================================================
# 3.1. WHATSAPP (WAHA)
# =====================================================================
def html_to_whatsapp(text: str) -> str:
    """Переводит HTML-разметку Telegram в разметку WhatsApp (*жирный*, _курсив_, `код`)."""
    text = re.sub(r"</?b>", "*", text)
    text = re.sub(r"</?i>", "_", text)
    text = re.sub(r"</?code>", "`", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _wa_error_text(exc: Exception) -> str:
    """Понятное описание ошибки связи со шлюзом WhatsApp."""
    if isinstance(exc, aiohttp.ClientConnectorError):
        return (
            "шлюз WhatsApp не запущен. На сервере выполните: "
            "cd /opt/dtek-bot && sudo ./wa-link.sh НОМЕР_БОТА"
        )
    return str(exc)


def _waha_headers() -> Dict[str, str]:
    return {"X-Api-Key": WAHA_API_KEY} if WAHA_API_KEY else {}


async def wa_send(chat_id: str, html_text: str) -> None:
    """Отправляет сообщение в чат/группу WhatsApp через WAHA."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{WAHA_URL}/api/sendText",
            json={"session": WAHA_SESSION, "chatId": chat_id, "text": html_to_whatsapp(html_text)},
            headers=_waha_headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"WAHA sendText HTTP {resp.status}: {(await resp.text())[:200]}")


async def wa_session_status() -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{WAHA_URL}/api/sessions/{WAHA_SESSION}",
            headers=_waha_headers(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
    return str(data.get("status", "UNKNOWN"))


async def wa_request_code(phone: str) -> str:
    """
    Запрашивает код привязки по номеру телефона (вместо сканирования QR).
    Код вводится в WhatsApp: Связанные устройства → Привязать устройство →
    «Привязать по номеру телефона».
    """
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 10:
        raise ValueError("укажите номер полностью, с кодом страны, например 380501234567")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{WAHA_URL}/api/{WAHA_SESSION}/auth/request-code",
            json={"phoneNumber": digits},
            headers=_waha_headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 300 or not data.get("code"):
                raise RuntimeError(f"WAHA HTTP {resp.status}: {str(data)[:200]}")
    return str(data["code"])


async def wa_wait_ready_for_link(timeout_sec: int = 300) -> str:
    """Ждет, пока шлюз запустится и будет готов к привязке (или уже подключен)."""
    deadline = time.monotonic() + timeout_sec
    status = "UNKNOWN"
    while time.monotonic() < deadline:
        try:
            status = await wa_session_status()
            if status in ("SCAN_QR_CODE", "WORKING"):
                return status
        except Exception:
            pass
        await asyncio.sleep(3)
    return status


WA_CODE_HELP = (
    "На телефоне с номером бота откройте WhatsApp → Настройки → Связанные устройства → "
    "Привязать устройство → внизу «Привязать по номеру телефона» и введите код."
)


async def cli_wa_link(phone: Optional[str]) -> int:
    """Консольная привязка WhatsApp: python bot.py wa-link 380XXXXXXXXX"""
    if not WHATSAPP_ENABLED:
        print("WhatsApp выключен: в .env нужно WHATSAPP_ENABLED=1 и COMPOSE_PROFILES=whatsapp")
        return 1
    print("Жду запуска шлюза WhatsApp (первый запуск может занять 1–3 минуты)...", flush=True)
    status = await wa_wait_ready_for_link()
    if status == "WORKING":
        print("✅ WhatsApp уже подключен.")
        return 0
    if status != "SCAN_QR_CODE":
        print(f"⚠️ Шлюз не готов (статус {status}). Посмотрите логи: docker compose logs waha")
        return 1
    while not phone:
        phone = input("Номер телефона бота с кодом страны (например 380501234567): ").strip()
    try:
        code = await wa_request_code(phone)
    except Exception as exc:
        print(f"⚠️ Не удалось получить код: {exc}")
        return 1
    print(f"\n   КОД ПРИВЯЗКИ:  {code}\n")
    print(WA_CODE_HELP)
    print("Жду подключения (до 3 минут)...", flush=True)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        try:
            if await wa_session_status() == "WORKING":
                print("✅ WhatsApp подключен! Добавьте номер бота в группу и напишите там «id».")
                return 0
        except Exception:
            pass
    print("Подключение не подтвердилось. Запустите команду еще раз, чтобы получить новый код.")
    return 1


async def wa_get_qr_png() -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{WAHA_URL}/api/{WAHA_SESSION}/auth/qr",
            headers={**_waha_headers(), "Accept": "image/png"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"QR недоступен (HTTP {resp.status}): {(await resp.text())[:200]}")
            return await resp.read()


async def _wa_handle_message(payload: Dict[str, Any]) -> None:
    chat_id = str(payload.get("from") or "")
    body = str(payload.get("body") or "").strip().lower()
    if not chat_id or not body:
        return

    if body in ("id", "/id", "айди"):
        await wa_send(chat_id, f"ID этого чата: <code>{chat_id}</code>")
        return

    if body in STATUS_WORDS:
        if WHATSAPP_CHAT_IDS and chat_id not in WHATSAPP_CHAT_IDS:
            logger.info("WhatsApp: запрос статуса из чужого чата %s — игнорирую", chat_id)
            return
        try:
            report = await build_status_report()
        except Exception as exc:
            logger.exception("Ошибка формирования отчета для WhatsApp")
            report = f"⚠️ Не удалось сформировать отчет: {html.escape(str(exc))}"
        await wa_send(chat_id, report)


_wa_last_status: Optional[str] = None


async def waha_webhook(request: web.Request) -> web.Response:
    """Принимает события от WAHA: входящие сообщения и смену статуса сессии."""
    global _wa_last_status
    try:
        event = await request.json()
    except Exception:
        return web.Response(status=400)

    kind = event.get("event")
    payload = event.get("payload") or {}

    if kind == "message" and not payload.get("fromMe"):
        asyncio.create_task(_safe(_wa_handle_message(payload)))
    elif kind == "session.status":
        status = str(payload.get("status"))
        logger.info("WhatsApp: статус сессии %s", status)
        bot: Optional[Bot] = request.app.get("bot")
        if _wa_last_status == "WORKING" and status in ("SCAN_QR_CODE", "FAILED", "STOPPED"):
            await _notify_telegram(
                bot,
                f"⚠️ WhatsApp отключился (статус {status}). "
                "Подключите заново: /wa_code НОМЕР (или /wa_qr), либо на сервере ./wa-link.sh",
            )
        elif status == "WORKING" and _wa_last_status not in (None, "WORKING"):
            await _notify_telegram(bot, "✅ WhatsApp подключен.")
        _wa_last_status = status

    return web.Response(text="ok")


async def _safe(coro: Any) -> None:
    try:
        await coro
    except Exception:
        logger.exception("Ошибка обработки WhatsApp-сообщения")


async def start_webhook_server(bot: Optional[Bot]) -> web.AppRunner:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/waha", waha_webhook)
    app.router.add_get("/health", lambda _: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT).start()
    logger.info("Webhook для WhatsApp слушает порт %s", WEBHOOK_PORT)
    return runner


# =====================================================================
# 4. ОБРАБОТЧИКИ TELEGRAM-БОТА
# =====================================================================
dp = Dispatcher()


def _is_allowed(message: types.Message) -> bool:
    """Если задан ALLOWED_USER_IDS, пропускает только перечисленных пользователей."""
    if not ALLOWED_USER_IDS:
        return True
    return bool(message.from_user and message.from_user.id in ALLOWED_USER_IDS)


@dp.message(Command("id"))
async def cmd_id(message: types.Message) -> None:
    """Показывает Telegram ID пользователя (нужен для ALLOWED_USER_IDS)."""
    user_id = message.from_user.id if message.from_user else "неизвестно"
    await message.answer(f"Ваш Telegram ID: <code>{user_id}</code>", parse_mode=ParseMode.HTML)


@dp.message(Command("wa_qr"))
async def cmd_wa_qr(message: types.Message) -> None:
    """Присылает QR-код для подключения WhatsApp (только разрешенным пользователям)."""
    if not ALLOWED_USER_IDS or not _is_allowed(message):
        await message.answer("⛔ Команда доступна только пользователям из ALLOWED_USER_IDS.")
        return
    if not WHATSAPP_ENABLED:
        await message.answer("WhatsApp выключен (WHATSAPP_ENABLED=0).")
        return
    try:
        status = await wa_session_status()
        if status == "WORKING":
            await message.answer("✅ WhatsApp уже подключен.")
            return
        png = await wa_get_qr_png()
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось получить QR: {_wa_error_text(exc)}")
        return
    await message.answer_photo(
        types.BufferedInputFile(png, filename="qr.png"),
        caption=(
            "Откройте WhatsApp на телефоне с номером для бота → Настройки → "
            "Связанные устройства → Привязать устройство и отсканируйте код. "
            "Код живет ~20 секунд; если не успели — отправьте /wa_qr еще раз."
        ),
    )


@dp.message(Command("wa_code"))
async def cmd_wa_code(message: types.Message) -> None:
    """Код привязки WhatsApp по номеру: /wa_code 380501234567"""
    if not ALLOWED_USER_IDS or not _is_allowed(message):
        await message.answer("⛔ Команда доступна только пользователям из ALLOWED_USER_IDS.")
        return
    if not WHATSAPP_ENABLED:
        await message.answer("WhatsApp выключен (WHATSAPP_ENABLED=0).")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите номер бота с кодом страны: /wa_code 380501234567")
        return
    try:
        status = await wa_session_status()
        if status == "WORKING":
            await message.answer("✅ WhatsApp уже подключен.")
            return
        code = await wa_request_code(parts[1])
    except Exception as exc:
        await message.answer(f"⚠️ Не удалось получить код: {html.escape(_wa_error_text(exc))}")
        return
    await message.answer(
        f"Код привязки: <code>{code}</code>\n\n{WA_CODE_HELP}", parse_mode=ParseMode.HTML
    )


@dp.message(Command("wa_status"))
async def cmd_wa_status(message: types.Message) -> None:
    if not _is_allowed(message):
        return
    if not WHATSAPP_ENABLED:
        await message.answer("WhatsApp выключен (WHATSAPP_ENABLED=0).")
        return
    try:
        status = await wa_session_status()
    except Exception as exc:
        status = f"нет связи со шлюзом: {_wa_error_text(exc)}"
    chats = ", ".join(WHATSAPP_CHAT_IDS) or "не заданы (WHATSAPP_CHAT_IDS)"
    await message.answer(f"WhatsApp: {status}\nГруппы для уведомлений: {chats}")


@dp.message(Command("start", "help"))
async def cmd_start(message: types.Message) -> None:
    """Приветствие и кнопка быстрого вызова «Статус»."""
    if not _is_allowed(message):
        await message.answer("⛔ Доступ к боту закрыт. Узнать свой ID: /id")
        return
    kb = types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="Статус")]],
        resize_keyboard=True,
    )
    await message.answer(
        "Привет! Отправьте <b>статус</b> (или нажмите кнопку ниже), чтобы получить "
        f"график отключений ДТЭК (г. Днепр, группа {DTEK_GROUP}) и параметры вашей станции "
        "<b>EcoFlow DELTA Pro 3</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


@dp.message(Command("status"))
@dp.message(F.text.lower().in_({"статус", "status", "свет", "график", "экофлоу"}))
async def handle_status(message: types.Message) -> None:
    """Отвечает на сообщение «статус» в любом регистре."""
    if not _is_allowed(message):
        await message.answer("⛔ Доступ к боту закрыт. Узнать свой ID: /id")
        return
    wait_msg = await message.answer(
        f"⏳ Опрашиваю EcoFlow DELTA Pro 3 и график ДТЭК (группа {DTEK_GROUP})..."
    )
    try:
        report = await build_status_report()
    except Exception as exc:
        logger.exception("Ошибка формирования отчета")
        report = f"⚠️ Не удалось сформировать отчет: <code>{html.escape(str(exc))}</code>"
    await wait_msg.edit_text(report, parse_mode=ParseMode.HTML)


# =====================================================================
# 5. ФОНОВЫЙ МОНИТОРИНГ И УВЕДОМЛЕНИЯ
# =====================================================================
async def _notify_telegram(bot: Optional[Bot], text: str) -> None:
    if bot is None:
        return
    for chat_id in NOTIFY_CHAT_IDS:
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.warning("Не удалось отправить уведомление в чат %s", chat_id, exc_info=True)


async def _notify(bot: Optional[Bot], text: str) -> None:
    """Рассылает уведомление в Telegram и в группы WhatsApp."""
    await _notify_telegram(bot, text)
    if WHATSAPP_ENABLED:
        for chat_id in WHATSAPP_CHAT_IDS:
            try:
                await wa_send(chat_id, text)
            except Exception:
                logger.warning("Не удалось отправить уведомление в WhatsApp %s", chat_id, exc_info=True)


async def monitor_loop(bot: Optional[Bot]) -> None:
    """
    Раз в MONITOR_INTERVAL_SEC опрашивает станцию и график и присылает уведомления:
      - пропал свет (станция перешла на батарею) / свет появился;
      - заряд упал ниже LOW_BATTERY_SOC при работе от батареи;
      - станция пропала из сети EcoFlow / снова на связи;
      - ДТЭК изменил график для группы.
    """
    last_mode: Optional[str] = None
    last_online: Optional[bool] = None
    low_battery_sent = False
    last_schedule: Optional[str] = None
    logger.info(
        "Мониторинг включен: интервал %s с, Telegram: %s, WhatsApp: %s",
        MONITOR_INTERVAL_SEC, sorted(NOTIFY_CHAT_IDS) if bot else "-",
        WHATSAPP_CHAT_IDS if WHATSAPP_ENABLED else "-",
    )

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                # --- EcoFlow ---
                try:
                    ef = await fetch_delta_pro_3_status(session)
                except Exception:
                    logger.warning("Мониторинг: ошибка EcoFlow API", exc_info=True)
                    ef = None

                if ef is not None:
                    online = bool(ef.get("online"))
                    if last_online is not None and online != last_online:
                        await _notify(
                            bot,
                            "🟢 Станция EcoFlow снова на связи" if online
                            else "⚪ Станция EcoFlow <b>пропала из сети</b> (нет связи с облаком)",
                        )
                    last_online = online

                    if online:
                        mode = ef["mode"]
                        now_str = datetime.datetime.now(KYIV_TZ).strftime("%H:%M")
                        if last_mode is not None and mode != last_mode:
                            logger.info(
                                "Смена режима: %s → %s (flowInfoAcIn=%s, AC in=%s Вт)",
                                last_mode, mode, ef.get("flow_ac_in"), ef.get("ac_in_watts"),
                            )
                            if mode == "station":
                                rem = format_duration_minutes(ef["dsg_rem_min"], ef["calculated_rem_min"])
                                tail = f" · хватит на <b>{rem}</b>" if ef["pow_out_sum"] > 0 else ""
                                await _notify(
                                    bot,
                                    f"🔴 <b>{now_str} свет пропал</b>\n"
                                    f"🔋 {_fmt_soc(ef['soc'])} · {ef['pow_out_sum']} Вт{tail}",
                                )
                            else:
                                await _notify(
                                    bot, f"🟢 <b>{now_str} свет появился</b> · 🔋 {_fmt_soc(ef['soc'])}"
                                )
                        last_mode = mode

                        if mode == "station" and ef["soc"] <= LOW_BATTERY_SOC and not low_battery_sent:
                            rem = format_duration_minutes(ef["dsg_rem_min"], ef["calculated_rem_min"])
                            await _notify(
                                bot,
                                f"🪫 <b>Низкий заряд: {_fmt_soc(ef['soc'])}</b> · осталось {rem}",
                            )
                            low_battery_sent = True
                        elif ef["soc"] > LOW_BATTERY_SOC + 5 or mode == "grid":
                            low_battery_sent = False

                # --- График ДТЭК ---
                if NOTIFY_SCHEDULE_CHANGES:
                    try:
                        fp = await fetch_dtek_group_fingerprint(session)
                    except Exception:
                        logger.warning("Мониторинг: ошибка загрузки графика ДТЭК", exc_info=True)
                        fp = None
                    if fp is not None:
                        if last_schedule is not None and fp != last_schedule:
                            schedule_text = await fetch_dtek_dnipro_schedule(session)
                            await _notify(bot, "🔔 <b>ДТЭК обновил график</b>\n" + schedule_text)
                        last_schedule = fp
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Мониторинг: непредвиденная ошибка")

        await asyncio.sleep(MONITOR_INTERVAL_SEC)


async def main() -> None:
    has_telegram = TELEGRAM_BOT_TOKEN not in ("", "YOUR_TELEGRAM_BOT_TOKEN")
    if not has_telegram and not WHATSAPP_ENABLED:
        logger.error("Задайте TELEGRAM_BOT_TOKEN и/или включите WhatsApp (WHATSAPP_ENABLED=1)!")
        raise SystemExit(1)

    bot = Bot(token=TELEGRAM_BOT_TOKEN) if has_telegram else None
    logger.info(
        "Бот запущен (Днепр, группа %s | EcoFlow DELTA Pro 3) | Telegram: %s | WhatsApp: %s",
        DTEK_GROUP, "да" if bot else "нет", "да" if WHATSAPP_ENABLED else "нет",
    )

    if WHATSAPP_ENABLED:
        await start_webhook_server(bot)
        if not WHATSAPP_CHAT_IDS:
            logger.warning("WHATSAPP_CHAT_IDS не задан: напишите «id» в нужной группе WhatsApp и впишите ID в .env")

    if (bot and NOTIFY_CHAT_IDS) or (WHATSAPP_ENABLED and WHATSAPP_CHAT_IDS):
        asyncio.create_task(monitor_loop(bot))
    else:
        logger.info("Уведомления выключены: задайте ALLOWED_USER_IDS/NOTIFY_CHAT_IDS или WHATSAPP_CHAT_IDS")

    if bot:
        await dp.start_polling(bot)
    else:
        await asyncio.Event().wait()  # только WhatsApp: просто держим процесс


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "wa-link":
        sys.exit(asyncio.run(cli_wa_link(sys.argv[2] if len(sys.argv) > 2 else None)))
    asyncio.run(main())
