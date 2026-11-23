#!/usr/bin/env python3
"""
Бот для Telegram и WhatsApp: статус станций EcoFlow (DELTA Pro 3) и график отключений ДТЭК.

Поддерживает несколько станций с разных аккаунтов EcoFlow. Станции описываются в файле
config/stations.yaml (пример — config/stations.example.yaml): у каждой свои ключи EcoFlow,
свои получатели уведомлений в Telegram/WhatsApp и своя настройка графика ДТЭК.
Если файла нет, используется одна станция из переменных окружения (.env).
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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiohttp import web
import yaml

# =====================================================================
# КОНФИГУРАЦИЯ (Днепр, группа 3.1 + EcoFlow DELTA Pro 3)
# =====================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")

# Необязательно: список Telegram ID через запятую, кому разрешено пользоваться ботом.
# Пусто — бот отвечает всем.
ALLOWED_USER_IDS: Set[int] = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

# --- Станция по умолчанию из .env (используется, если нет config/stations.yaml) ---
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
DTEK_ENABLED = os.getenv("DTEK_ENABLED", "1").lower() in ("1", "true", "yes", "on")
STATIONS_FILE = os.getenv("STATIONS_FILE", "/app/config/stations.yaml")
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


@dataclass
class Station:
    """Станция EcoFlow со своими ключами, получателями уведомлений и настройкой ДТЭК."""
    name: str
    access_key: str
    secret_key: str
    sn: str
    api_host: str = "https://api-e.ecoflow.com"
    capacity_wh: float = 4096.0
    telegram_ids: Set[int] = field(default_factory=set)
    whatsapp_chats: List[str] = field(default_factory=list)
    whatsapp_notify: bool = True  # False — в WhatsApp не слать уведомления (ответ на «статус» остается)
    dtek: bool = True
    dtek_region: str = "dnipro"
    dtek_group: str = "3.1"
    low_battery_soc: float = 20.0

    @property
    def key(self) -> str:
        return self.sn or self.name


def _as_bool(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "да")


def _station_from_env() -> Station:
    return Station(
        name="EcoFlow",
        access_key=ECOFLOW_ACCESS_KEY,
        secret_key=ECOFLOW_SECRET_KEY,
        sn=ECOFLOW_DEVICE_SN,
        api_host=ECOFLOW_API_HOST,
        capacity_wh=DELTA_PRO_3_CAPACITY_WH,
        telegram_ids=set(NOTIFY_CHAT_IDS),
        whatsapp_chats=list(WHATSAPP_CHAT_IDS),
        dtek=DTEK_ENABLED,
        dtek_region=DTEK_REGION,
        dtek_group=DTEK_GROUP,
        low_battery_soc=LOW_BATTERY_SOC,
    )


def _parse_stations(raw: Any) -> List[Station]:
    items = (raw or {}).get("stations") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ValueError("в файле нет списка stations")
    result: List[Station] = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise ValueError(f"станция №{i}: ожидается набор полей")
        missing = [k for k in ("access_key", "secret_key", "sn") if not str(it.get(k) or "").strip()]
        if missing:
            raise ValueError(f"станция №{i}: не заполнено {', '.join(missing)}")
        tg = it.get("telegram_ids") or []
        wa = it.get("whatsapp_chats") or []
        result.append(Station(
            name=str(it.get("name") or f"Станция {i}"),
            access_key=str(it["access_key"]).strip(),
            secret_key=str(it["secret_key"]).strip(),
            sn=str(it["sn"]).strip(),
            api_host=str(it.get("api_host") or "https://api-e.ecoflow.com").rstrip("/"),
            capacity_wh=float(it.get("capacity_wh") or 4096),
            telegram_ids={int(x) for x in (tg if isinstance(tg, list) else [tg])},
            whatsapp_chats=[str(x).strip() for x in (wa if isinstance(wa, list) else [wa])],
            whatsapp_notify=_as_bool(it.get("whatsapp_notify"), True),
            dtek=_as_bool(it.get("dtek"), True),
            dtek_region=str(it.get("dtek_region") or "dnipro"),
            dtek_group=str(it.get("dtek_group") or "3.1"),
            low_battery_soc=float(it.get("low_battery_soc") or 20),
        ))
    return result


_stations_cache: Tuple[Optional[float], List[Station]] = (None, [])


def get_stations() -> List[Station]:
    """
    Список станций. Файл config/stations.yaml перечитывается при изменении —
    станции можно добавлять без перезапуска бота.
    """
    global _stations_cache
    try:
        mtime = os.path.getmtime(STATIONS_FILE)
    except OSError:
        return [_station_from_env()]
    if _stations_cache[0] == mtime:
        return _stations_cache[1]
    try:
        with open(STATIONS_FILE, encoding="utf-8") as f:
            stations = _parse_stations(yaml.safe_load(f))
        logger.info("Загружено станций: %s (%s)", len(stations), ", ".join(s.name for s in stations))
    except Exception as exc:
        logger.error("Ошибка в %s: %s — оставляю прежний список станций", STATIONS_FILE, exc)
        if _stations_cache[1]:
            return _stations_cache[1]
        stations = [_station_from_env()]
    _stations_cache = (mtime, stations)
    return stations


def is_admin(user_id: Optional[int]) -> bool:
    return user_id is not None and (not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS)


def stations_for_telegram(user_id: Optional[int]) -> List[Station]:
    """Админы (ALLOWED_USER_IDS) видят все станции, остальные — только свои."""
    stations = get_stations()
    if is_admin(user_id):
        return stations
    return [s for s in stations if user_id in s.telegram_ids]


def stations_for_whatsapp(chat_id: str) -> List[Station]:
    return [s for s in get_stations() if chat_id in s.whatsapp_chats]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# =====================================================================
# 1. КЛИЕНТ ECOFLOW DELTA PRO 3 (Developer API / Quota All)
# =====================================================================
def _generate_ecoflow_headers(st: Station, params: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
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
    auth_str = f"accessKey={st.access_key}&nonce={nonce}&timestamp={timestamp}"
    sign_str = f"{query_str}&{auth_str}" if query_str else auth_str

    signature = hmac.new(
        st.secret_key.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return {
        "accessKey": st.access_key,
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


async def fetch_device_online(session: aiohttp.ClientSession, st: Station) -> Optional[bool]:
    """
    Проверяет через GET /iot-open/sign/device/list, в сети ли станция.
    Квоты (quota/all) отдаются облаком даже для офлайн-устройства (последние известные значения),
    поэтому без этой проверки бот показывал бы устаревшие данные как актуальные.
    Возвращает None, если узнать не удалось.
    """
    url = f"{st.api_host}/iot-open/sign/device/list"
    try:
        async with session.get(
            url, headers=_generate_ecoflow_headers(st), timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            payload = await resp.json(content_type=None)
    except Exception:
        logger.warning("Не удалось получить список устройств EcoFlow", exc_info=True)
        return None
    if str(payload.get("code")) != "0":
        return None
    for dev in payload.get("data") or []:
        if str(dev.get("sn")) == st.sn:
            return str(dev.get("online")) == "1"
    return None


async def fetch_delta_pro_3_status(session: aiohttp.ClientSession, st: Station) -> Dict[str, Any]:
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
    url = f"{st.api_host}/iot-open/sign/device/quota/all"
    params = {"sn": st.sn}
    headers = _generate_ecoflow_headers(st, params)

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
    if not q or await fetch_device_online(session, st) is False:
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
    capacity_wh = _get_quota_float(q, ["cmsBattFullEnergy"], None) or st.capacity_wh
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


async def _safe_station_status(session: aiohttp.ClientSession, st: Station) -> Dict[str, Any]:
    try:
        return await fetch_delta_pro_3_status(session, st)
    except Exception as exc:
        logger.warning("EcoFlow «%s»: %s", st.name, exc)
        return {"error": str(exc) or exc.__class__.__name__}


async def _safe_schedule(session: aiohttp.ClientSession, region: str, group: str) -> str:
    try:
        return await fetch_dtek_dnipro_schedule(session, region, group)
    except Exception as exc:
        logger.warning("ДТЭК %s/%s: %s", region, group, exc)
        return f"⚠️ График ДТЭК ({group}) не загрузился ({html.escape(str(exc) or exc.__class__.__name__)})"


async def build_status_report(stations: Optional[List[Station]] = None) -> str:
    """Короткий отчет по станциям и графикам ДТЭК (только для станций с dtek: true)."""
    stations = get_stations() if stations is None else stations
    if not stations:
        return "У вас нет станций. Попросите администратора добавить ваш Telegram ID к станции (ID: /id)."
    groups: List[Tuple[str, str]] = []
    for st in stations:
        if st.dtek and (st.dtek_region, st.dtek_group) not in groups:
            groups.append((st.dtek_region, st.dtek_group))

    async with aiohttp.ClientSession() as session:
        ef_list, schedules = await asyncio.gather(
            asyncio.gather(*(_safe_station_status(session, st) for st in stations)),
            asyncio.gather(*(_safe_schedule(session, r, g) for r, g in groups)),
        )

    now_str = datetime.datetime.now(KYIV_TZ).strftime("%d.%m %H:%M")
    blocks = []
    for st, ef in zip(stations, ef_list):
        section = format_ecoflow_section(ef)
        blocks.append(f"<b>{html.escape(st.name)}</b>\n{section}" if len(stations) > 1 else section)
    text = f"🕒 {now_str}\n" + "\n\n".join(blocks)
    if schedules:
        text += "\n\n" + "\n\n".join(schedules)
    return text


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
        stations = stations_for_whatsapp(chat_id)
        if not stations:
            logger.info("WhatsApp: запрос статуса из чужого чата %s — игнорирую", chat_id)
            return
        try:
            report = await build_status_report(stations)
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
                bot, ALLOWED_USER_IDS,
                f"⚠️ WhatsApp отключился (статус {status}). "
                "Подключите заново: /wa_code НОМЕР (или /wa_qr), либо на сервере ./wa-link.sh",
            )
        elif status == "WORKING" and _wa_last_status not in (None, "WORKING"):
            await _notify_telegram(bot, ALLOWED_USER_IDS, "✅ WhatsApp подключен.")
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


def _uid(message: types.Message) -> Optional[int]:
    return message.from_user.id if message.from_user else None


def _is_allowed(message: types.Message) -> bool:
    """Доступ есть у админов (ALLOWED_USER_IDS) и у получателей хотя бы одной станции."""
    return bool(stations_for_telegram(_uid(message)))


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
    chats = sorted({c for s in get_stations() for c in s.whatsapp_chats})
    await message.answer(f"WhatsApp: {status}\nГруппы для уведомлений: {', '.join(chats) or 'не заданы'}")


@dp.message(Command("stations"))
async def cmd_stations(message: types.Message) -> None:
    """Список станций и их получателей (только для админов)."""
    if not ALLOWED_USER_IDS or not is_admin(_uid(message)):
        await message.answer("⛔ Команда доступна только администраторам (ALLOWED_USER_IDS).")
        return
    lines = []
    for i, st in enumerate(get_stations(), 1):
        lines.append(
            f"{i}. <b>{html.escape(st.name)}</b> · SN …{html.escape(st.sn[-4:])}\n"
            f"   Telegram: {', '.join(map(str, sorted(st.telegram_ids))) or '—'}\n"
            f"   WhatsApp: {', '.join(st.whatsapp_chats) or '—'}"
            f"{'' if st.whatsapp_notify else ' (уведомления выключены)'}\n"
            f"   ДТЭК: {('группа ' + st.dtek_group) if st.dtek else 'выключен'}"
        )
    src = STATIONS_FILE if os.path.exists(STATIONS_FILE) else ".env (файла stations.yaml нет)"
    await message.answer("\n".join(lines) + f"\n\nНастройки: <code>{html.escape(src)}</code>",
                         parse_mode=ParseMode.HTML)


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
        "Привет! Отправьте <b>статус</b> (или нажмите кнопку ниже), чтобы узнать состояние "
        "станции EcoFlow и график отключений ДТЭК.",
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
    wait_msg = await message.answer("⏳ Опрашиваю станцию...")
    try:
        report = await build_status_report(stations_for_telegram(_uid(message)))
    except Exception as exc:
        logger.exception("Ошибка формирования отчета")
        report = f"⚠️ Не удалось сформировать отчет: <code>{html.escape(str(exc))}</code>"
    await wait_msg.edit_text(report, parse_mode=ParseMode.HTML)


# =====================================================================
# 5. ФОНОВЫЙ МОНИТОРИНГ И УВЕДОМЛЕНИЯ
# =====================================================================
async def _notify_telegram(bot: Optional[Bot], chat_ids: Any, text: str) -> None:
    if bot is None:
        return
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.warning("Не удалось отправить уведомление в чат %s", chat_id, exc_info=True)


async def _notify(bot: Optional[Bot], tg_ids: Any, wa_chats: Any, text: str) -> None:
    """Рассылает уведомление в указанные чаты Telegram и группы WhatsApp."""
    await _notify_telegram(bot, tg_ids, text)
    if WHATSAPP_ENABLED:
        for chat_id in wa_chats:
            try:
                await wa_send(chat_id, text)
            except Exception:
                logger.warning("Не удалось отправить уведомление в WhatsApp %s", chat_id, exc_info=True)


@dataclass
class _StationState:
    mode: Optional[str] = None
    online: Optional[bool] = None
    low_sent: bool = False


async def _check_station(bot: Optional[Bot], session: aiohttp.ClientSession, st: Station,
                         state: _StationState, show_name: bool) -> None:
    try:
        ef = await fetch_delta_pro_3_status(session, st)
    except Exception as exc:
        logger.warning("Мониторинг «%s»: ошибка EcoFlow API: %s", st.name, exc)
        return

    async def send(text: str) -> None:
        prefix = f"<b>{html.escape(st.name)}</b>: " if show_name else ""
        await _notify(bot, st.telegram_ids, st.whatsapp_chats if st.whatsapp_notify else [], prefix + text)

    online = bool(ef.get("online"))
    if state.online is not None and online != state.online:
        await send("🟢 станция снова на связи" if online else "⚪ станция <b>пропала из сети</b>")
    state.online = online
    if not online:
        return

    mode = ef["mode"]
    now_str = datetime.datetime.now(KYIV_TZ).strftime("%H:%M")
    if state.mode is not None and mode != state.mode:
        logger.info("«%s»: смена режима %s → %s (flowInfoAcIn=%s, AC in=%s Вт)",
                    st.name, state.mode, mode, ef.get("flow_ac_in"), ef.get("ac_in_watts"))
        if mode == "station":
            rem = format_duration_minutes(ef["dsg_rem_min"], ef["calculated_rem_min"])
            tail = f" · хватит на <b>{rem}</b>" if ef["pow_out_sum"] > 0 else ""
            await send(f"🔴 <b>{now_str} свет пропал</b>\n🔋 {_fmt_soc(ef['soc'])} · {ef['pow_out_sum']} Вт{tail}")
        else:
            await send(f"🟢 <b>{now_str} свет появился</b> · 🔋 {_fmt_soc(ef['soc'])}")
    state.mode = mode

    if mode == "station" and ef["soc"] <= st.low_battery_soc and not state.low_sent:
        rem = format_duration_minutes(ef["dsg_rem_min"], ef["calculated_rem_min"])
        await send(f"🪫 <b>Низкий заряд: {_fmt_soc(ef['soc'])}</b> · осталось {rem}")
        state.low_sent = True
    elif ef["soc"] > st.low_battery_soc + 5 or mode == "grid":
        state.low_sent = False


async def monitor_loop(bot: Optional[Bot]) -> None:
    """
    Раз в MONITOR_INTERVAL_SEC опрашивает все станции и графики и шлет уведомления
    получателям конкретной станции:
      - пропал свет / свет появился;
      - низкий заряд при работе от батареи;
      - станция пропала из сети EcoFlow / снова на связи;
      - ДТЭК изменил график (только станциям с dtek: true).
    """
    states: Dict[str, _StationState] = {}
    schedules: Dict[Tuple[str, str], str] = {}
    logger.info("Мониторинг включен: интервал %s с", MONITOR_INTERVAL_SEC)

    while True:
        try:
            stations = get_stations()
            show_name = len(stations) > 1
            async with aiohttp.ClientSession() as session:
                await asyncio.gather(*(
                    _check_station(bot, session, st, states.setdefault(st.key, _StationState()), show_name)
                    for st in stations
                ))

                if NOTIFY_SCHEDULE_CHANGES:
                    groups: Dict[Tuple[str, str], List[Station]] = {}
                    for st in stations:
                        if st.dtek:
                            groups.setdefault((st.dtek_region, st.dtek_group), []).append(st)
                    for (region, group), sts in groups.items():
                        try:
                            fp = await fetch_dtek_group_fingerprint(session, region, group)
                        except Exception:
                            logger.warning("Мониторинг: ошибка графика ДТЭК %s", group, exc_info=True)
                            continue
                        if fp is None:
                            continue
                        prev = schedules.get((region, group))
                        if prev is not None and fp != prev:
                            text = "🔔 <b>ДТЭК обновил график</b>\n" + await fetch_dtek_dnipro_schedule(session, region, group)
                            tg = {i for s in sts for i in s.telegram_ids}
                            wa = {c for s in sts if s.whatsapp_notify for c in s.whatsapp_chats}
                            await _notify(bot, tg, wa, text)
                        schedules[(region, group)] = fp
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
    stations = get_stations()
    logger.info(
        "Бот запущен | станций: %s | Telegram: %s | WhatsApp: %s",
        len(stations), "да" if bot else "нет", "да" if WHATSAPP_ENABLED else "нет",
    )

    if WHATSAPP_ENABLED:
        await start_webhook_server(bot)

    asyncio.create_task(monitor_loop(bot))

    if bot:
        await dp.start_polling(bot)
    else:
        await asyncio.Event().wait()  # только WhatsApp: просто держим процесс


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "wa-link":
        sys.exit(asyncio.run(cli_wa_link(sys.argv[2] if len(sys.argv) > 2 else None)))
    asyncio.run(main())
