import asyncio
import html
import logging
import os
import re
import json
import ssl
import uuid
import sqlite3
import aiohttp
from datetime import datetime
from collections import OrderedDict

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ====================== НАСТРОЙКИ OASIS DIGITAL ======================
BOT_TOKEN = "9024954594:AAGj3lihxCTSGgCMaeE_mCi2519paMgIYms"
BOT_USERNAME = "@MobiusSearchBot"

MAIN_ADMIN_ID = 8394631079
ADMIN_USERNAME = "@youranonglobal"

CHANNEL_ID = -1004330390390
CHANNEL_URL = "https://t.me/+pzsfx9fWdXdkYmFl"

# API-ключи
BLACKBOX_KEY = "8jdsYcPZiQMTpYNCluXg9scNhc7y"
ARGUS_KEY = "ARGUS-IO-K_JKRX"

TON_WALLET = "UQBlTLz8MaKDtNhYo1qeDsOXtVjN1upRp2uwsf0NWWE9p5cx"
WEBHOOK_HOST = "https://listopadov-search.onrender.com"
USERS_DB = "users.db"

# API Endpoints
API_URL_BLACKBOX = "https://api.black-box.cc/v1.1/search"
API_URL_ARGUS = "https://argus.cc.cd/api/v1/search"

# Прокси (если понадобится, укажи "http://user:pass@ip:port")
PROXY_URL = os.getenv("PROXY_URL", None)

API_TIMEOUT = 90  # Таймаут 90 секунд
MAX_RETRIES = 3   # Количество повторных попыток при 502/503/504

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("oasis")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class BoundedCache(OrderedDict):
    def __init__(self, maxlen=1000, *args, **kwargs):
        self.maxlen = maxlen
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxlen:
            oldest = next(iter(self))
            del self[oldest]


REPORTS_CACHE = BoundedCache(maxlen=1000)


class SearchStates(StatesGroup):
    wait_support_msg = State()
    wait_promo_input = State()
    wait_admin_raw = State()
    wait_admin_create_promo = State()
    wait_admin_create_check = State()


# --- БАЗА ДАННЫХ ---
def init_users_db():
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            requests_left INTEGER DEFAULT 3,
            referrer_id INTEGER,
            joined_at TEXT,
            is_banned INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS promos (
            code TEXT PRIMARY KEY,
            reward INTEGER,
            max_uses INTEGER DEFAULT 1,
            uses_count INTEGER DEFAULT 0,
            created_by INTEGER
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS promo_activations (
            user_id INTEGER,
            code TEXT,
            PRIMARY KEY (user_id, code)
        )
    ''')
    conn.commit()
    conn.close()


init_users_db()


def get_user(user_id):
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def add_user(user_id, username, referrer_id=None):
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cursor.fetchone():
        conn.close()
        return False
    cursor.execute(
        "INSERT INTO users (user_id, username, requests_left, referrer_id, joined_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, 3, referrer_id, datetime.now().strftime("%d.%m.%Y"))
    )
    conn.commit()
    conn.close()
    if referrer_id and referrer_id != user_id:
        add_requests(referrer_id, 1)
    return True


def add_requests(user_id, amount):
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET requests_left = requests_left + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


def deduct_request(user_id):
    if user_id == MAIN_ADMIN_ID:
        return True
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT requests_left, is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row and row[0] > 0 and row[1] == 0:
        cursor.execute("UPDATE users SET requests_left = requests_left - 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False


def is_banned(user_id):
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row and row[0] == 1)


def calculate_days(joined_str):
    try:
        joined_date = datetime.strptime(joined_str, "%d.%m.%Y")
        delta = datetime.now() - joined_date
        days = delta.days
        months = days // 30
        rem_days = days % 30
        if months > 0:
            return f"{months} мес. {rem_days} дн."
        return f"{rem_days} дн."
    except Exception:
        return "0 дн."


# --- ТЕКСТЫ И КЛАВИАТУРЫ ---
START_TEXT = """<b>ℹ️ Примеры для ввода запросов</b>

🕵️ <b>Личность</b>
<blockquote>Навальный Алексей Анатольевич</blockquote>

📲 <b>Контакты</b>
<blockquote>79104813955 — номер телефона
vania120df@gmail.com — email</blockquote>

🚘 <b>Транспорт</b>
<blockquote>РЕНО19 — госномер автомобиля
XTA210930Y2696785 — VIN автомобиля</blockquote>

💬 <b>Соцсети</b>
<blockquote>@durov 1090472807 — Telegram
vk.com/id1, 2737383 - ВКонтакте</blockquote>

🌐 <b>Онлайн-следы</b>
<blockquote>78.26.242.244, google.com</blockquote>

<i>Отправьте запрос сообщением.</i>"""

SUPPORT_TEXT = "🆘 <b>Связь с нами</b>\nНапишите ваше сообщение в чат."
HIDE_DATA_TEXT = "🛡️ <b>Скрытие данных</b>\nСтоимость скрытия данных: $7 (навсегда)"


def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поиск", callback_data="search_info")],
        [InlineKeyboardButton(text="💎 Купить запросы", callback_data="buy_requests")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🎟 Активировать промокод", callback_data="use_promo")],
        [InlineKeyboardButton(text="🔗 Рефералка", callback_data="ref_link"), InlineKeyboardButton(text="🆘 Связь с нами", callback_data="support")]
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]])


def result_kb(report_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 AI-анализатор", callback_data=f"ai_analyze_{report_id}")],
        [InlineKeyboardButton(text="🌐 Веб-отчет (Граф)", url=f"{WEBHOOK_HOST}/report/{report_id}")],
        [
            InlineKeyboardButton(text="📄 Скачать TXT", callback_data=f"get_txt_{report_id}"),
            InlineKeyboardButton(text="📦 Скачать JSON", callback_data=f"get_json_{report_id}")
        ],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")]
    ])


def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 Тест API (Raw)", callback_data="adm_test_api")],
        [InlineKeyboardButton(text="🔌 Проверка API", callback_data="adm_check_apis")],
        [InlineKeyboardButton(text="🎟 Создать промокод", callback_data="adm_promo"), InlineKeyboardButton(text="💳 Создать чек", callback_data="adm_check")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")]
    ])


# --- ЗАПРОСЫ К ВНЕШНИМ API ---
async def _get_with_retry(url: str, api_key: str, tag: str, params: dict, return_debug: bool = False):
    headers = {
        **DEFAULT_HEADERS,
        "X-API-Key": api_key
    }
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    ssl_ctx = ssl.create_default_context()

    last_error = "No response"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.get(url, headers=headers, params=params, proxy=PROXY_URL) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        try:
                            return json.loads(text)
                        except Exception:
                            return {"raw_text": text}
                    last_error = f"HTTP {resp.status}: {text[:300]}"
                    log.warning(f"[{tag}] Попытка {attempt}/{MAX_RETRIES} -> {last_error}")
                    if resp.status in (502, 503, 504) and attempt < MAX_RETRIES:
                        await asyncio.sleep(3)
                        continue
                    break
        except Exception as e:
            last_error = f"Exception: {e}"
            log.error(f"[{tag} ERROR] Попытка {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(3)

    return {"_error": last_error} if return_debug else None


async def fetch_blackbox(q: str, api_key: str = BLACKBOX_KEY, return_debug: bool = False):
    return await _get_with_retry(
        API_URL_BLACKBOX,
        api_key=api_key,
        tag="BLACKBOX",
        params={"q": q.strip(), "type": "auto"},
        return_debug=return_debug
    )


async def fetch_argus(q: str, api_key: str = ARGUS_KEY, return_debug: bool = False):
    return await _get_with_retry(
        API_URL_ARGUS,
        api_key=api_key,
        tag="ARGUS",
        params={"q": q.strip(), "key": api_key},
        return_debug=return_debug
    )


async def _single_health_check(name: str, url: str, api_key: str, params: dict):
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    headers = {
        **DEFAULT_HEADERS,
        "X-API-Key": api_key
    }
    start = asyncio.get_event_loop().time()
    try:
        connector = aiohttp.TCPConnector(ssl=ssl.create_default_context())
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(url, headers=headers, params=params, proxy=PROXY_URL) as resp:
                elapsed = int((asyncio.get_event_loop().time() - start) * 1000)
                return {"name": name, "healthy": 200 <= resp.status < 300, "status": resp.status, "latency_ms": elapsed}
    except Exception as e:
        return {"name": name, "healthy": False, "status": f"ERR ({type(e).__name__})", "latency_ms": 0}


async def check_all_apis_health():
    return await asyncio.gather(
        _single_health_check("Black-Box", API_URL_BLACKBOX, BLACKBOX_KEY, {"q": "79825207224", "type": "phone"}),
        _single_health_check("Argus", API_URL_ARGUS, ARGUS_KEY, {"q": "79991234567", "key": ARGUS_KEY})
    )


# --- ЛОКАЛЬНЫЙ АНАЛИЗАТОР БЕЗ ВНЕШНЕГО AI API ---
def _has_report_findings(data: dict) -> bool:
    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    return bool(
        (tg.get("id") or tg.get("user_id"))
        or (data.get("phone_number") and data.get("phone_number") != "Не найдено")
        or (data.get("fio") and data.get("fio") not in ("Не найдено", "НЕ НАЙДЕНО"))
        or bool(data.get("phonebooks"))
        or bool(data.get("name_history"))
        or bool(data.get("databases"))
    )


def analyze_locally(data: dict) -> str:
    if data.get("local_analysis"):
        return data["local_analysis"]

    if not _has_report_findings(data):
        return "❌ Недостаточно данных для анализа — по запросу ничего не найдено."

    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    tg_id = tg.get("id") or tg.get("user_id")
    raw_uname = tg.get("username") or ""
    tg_uname = f"@{str(raw_uname).lstrip('@')}" if raw_uname else ""

    phone = str(data.get("phone_number") or "").strip()
    fio = str(data.get("fio") or "").strip()
    reg_date = str(data.get("reg_date") or "").strip()
    region = str(data.get("region") or "").strip()
    operator = str(data.get("operator") or "").strip()
    phonebooks = [str(x).strip() for x in (data.get("phonebooks") or []) if str(x).strip()]
    history = [str(x).strip() for x in (data.get("name_history") or []) if str(x).strip()]
    dbs = data.get("databases") if isinstance(data.get("databases"), dict) else {}

    usernames_seen = []
    seen_set = set()
    if tg_uname and tg_uname.lower() not in seen_set:
        seen_set.add(tg_uname.lower())
        usernames_seen.append(tg_uname)

    for h in history:
        for match in re.findall(r"@[A-Za-z0-9_]{3,}", h):
            low = match.lower()
            if low not in seen_set:
                seen_set.add(low)
                usernames_seen.append(match)

    tags_found = []
    for h in history:
        for tag in re.findall(r"#[^\s,;]+", h):
            if tag not in tags_found:
                tags_found.append(tag)

    facts = []
    if tg_uname or tg_id:
        facts.append(f"Профиль TG: {tg_uname or '—'} (ID: {tg_id or '—'})")
    if phone and phone != "Не найдено":
        loc_part = " / ".join(x for x in (operator if operator != "Нет" else "", region if region != "Нет" else "") if x)
        facts.append(f"Телефон: {phone}" + (f" ({loc_part})" if loc_part else ""))
    if fio and fio not in ("Не найдено", "НЕ НАЙДЕНО"):
        facts.append(f"ФИО/Имя: {fio}")
    if reg_date and reg_date != "Не найдено":
        facts.append(f"Регистрация: {reg_date}")
    if phonebooks:
        facts.append(f"В записных книгах встречается как: {', '.join(phonebooks[:6])}")

    patterns = []
    if history:
        patterns.append(f"Зафиксировано изменений имени/ника: {len(history)}")
        if len(history) >= 2:
            patterns.append(f"Свежая запись: {history[0]}")
            patterns.append(f"Ранняя запись: {history[-1]}")
    if usernames_seen:
        patterns.append(f"Связанные никнеймы ({len(usernames_seen)}): {', '.join(usernames_seen[:8])}")
    if tags_found:
        patterns.append(f"Метки в истории: {', '.join(tags_found[:6])}")
    if dbs:
        total_recs = sum(len(v) for v in dbs.values() if isinstance(v, list))
        patterns.append(f"Совпадений в базах: {len(dbs)} источн. ({total_recs} записей)")

    next_steps = []
    for u in usernames_seen[:3]:
        if u.lower() != tg_uname.lower():
            next_steps.append(f"Проверить старый ник {u} отдельно")
    if phone and phone != "Не найдено":
        next_steps.append(f"Пробить номер {phone} по утечкам и мессенджерам")
    if not next_steps:
        next_steps.append("Сверить найденные имена и контакты по веб-отчету и JSON")

    lines = ["📊 <b>Анализ связей и фактов</b>\n"]
    if facts:
        lines.append("<b>Факты:</b>")
        for f_item in facts:
            lines.append(f"• {html.escape(f_item)}")
    if patterns:
        lines.append("\n<b>Активность и связи:</b>")
        for p_item in patterns:
            lines.append(f"• {html.escape(p_item)}")
    lines.append("\n<b>Что дальше:</b>")
    for s_item in next_steps:
        lines.append(f"• {html.escape(s_item)}")

    result = "\n".join(lines)
    data["local_analysis"] = result
    return result


# --- ПАРСЕР И АГРЕГАТОР ---
def _extract_phone(resp: dict, telecom: dict, tg: dict, dbs: dict) -> str:
    candidates = []
    for src in (resp, telecom, tg):
        if isinstance(src, dict):
            for key in ("phone", "phone_number", "mobile", "msisdn", "number", "tel"):
                val = src.get(key)
                if val:
                    candidates.append(str(val))
            for key in ("phones", "phone_numbers"):
                vals = src.get(key)
                if isinstance(vals, list):
                    candidates.extend(str(v) for v in vals if v)

    if isinstance(dbs, dict):
        for records in dbs.values():
            if isinstance(records, list):
                for rec in records[:15]:
                    if isinstance(rec, dict):
                        for key in ("phone", "phone_number", "mobile", "msisdn", "number", "tel"):
                            val = rec.get(key)
                            if val:
                                candidates.append(str(val))

    for item in candidates:
        cleaned = item.strip()
        if cleaned:
            return cleaned
    return "Не найдено"


def _extract_name_history(resp: dict, tg: dict, dbs: dict) -> list[str]:
    history = []
    raw_sources = []
    for src in (resp, tg):
        if isinstance(src, dict):
            for key in ("name_history", "history", "names_history", "username_history", "changes"):
                val = src.get(key)
                if isinstance(val, list):
                    raw_sources.extend(val)

    if isinstance(dbs, dict):
        for records in dbs.values():
            if isinstance(records, list):
                for rec in records:
                    if isinstance(rec, dict):
                        for key in ("name_history", "history", "names", "usernames"):
                            val = rec.get(key)
                            if isinstance(val, list):
                                raw_sources.extend(val)

    for entry in raw_sources:
        if isinstance(entry, str) and entry.strip():
            history.append(entry.strip())
        elif isinstance(entry, dict):
            dt = entry.get("date") or entry.get("time") or entry.get("updated_at") or ""
            uname = entry.get("username") or ""
            fname = entry.get("full_name") or entry.get("first_name") or entry.get("name") or ""
            if uname and not str(uname).startswith("@"):
                uname = f"@{uname}"
            target = ", ".join(x for x in (str(uname).strip(), str(fname).strip()) if x)
            line = f"{str(dt).strip()} → {target}".strip(" →")
            if line:
                history.append(line)

    return history


def _merge_single_response(resp: dict, final_data: dict, source_prefix: str = ""):
    if not isinstance(resp, dict) or "_error" in resp:
        return

    payload = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    telecom = payload.get("telecom") if isinstance(payload.get("telecom"), dict) else {}
    tg = payload.get("telegram") if isinstance(payload.get("telegram"), dict) else {}
    dbs = payload.get("databases") or payload.get("results") or {}

    if isinstance(dbs, list):
        dbs = {f"{source_prefix}results" if source_prefix else "results": dbs}

    for fio_val in (profile.get("primary_fio"), payload.get("fio"), payload.get("full_name")):
        if fio_val:
            final_data["fio"].add(str(fio_val).strip())
    if profile.get("birth_date"):
        final_data["dob"].add(str(profile["birth_date"]).strip())
    if profile.get("snils"):
        final_data["snils"].add(str(profile["snils"]).strip())

    if telecom.get("operator") and final_data["operator"] == "Нет":
        final_data["operator"] = str(telecom["operator"])
    if telecom.get("region") and final_data["region"] == "Нет":
        final_data["region"] = str(telecom["region"])
    if telecom.get("country"):
        final_data["country"] = str(telecom["country"])

    if isinstance(tg, dict) and tg:
        merged_tg = dict(final_data["tg_info"])
        for k, v in tg.items():
            if v is not None and (k not in merged_tg or not merged_tg[k]):
                merged_tg[k] = v
        final_data["tg_info"] = merged_tg
        if tg.get("full_name"):
            final_data["fio"].add(str(tg["full_name"]).strip())
        reg_tg = tg.get("reg_date") or tg.get("registration") or tg.get("created_at")
        if reg_tg and final_data["reg_date"] == "Не найдено":
            final_data["reg_date"] = str(reg_tg)

    reg_val = payload.get("reg_date") or payload.get("registration") or profile.get("reg_date")
    if reg_val and final_data["reg_date"] == "Не найдено":
        final_data["reg_date"] = str(reg_val)

    pb_raw = payload.get("phonebooks") or (tg.get("phonebooks") if isinstance(tg, dict) else None) or []
    if isinstance(pb_raw, list):
        for x in pb_raw:
            s = str(x).strip()
            if s and s not in final_data["phonebooks"]:
                final_data["phonebooks"].append(s)
    elif isinstance(pb_raw, str) and pb_raw.strip():
        for x in pb_raw.split(","):
            s = x.strip()
            if s and s not in final_data["phonebooks"]:
                final_data["phonebooks"].append(s)

    for h_item in _extract_name_history(payload, tg, dbs):
        if h_item not in final_data["name_history"]:
            final_data["name_history"].append(h_item)

    extracted_phone = _extract_phone(payload, telecom, tg, dbs)
    if extracted_phone != "Не найдено":
        final_data["phones"].add(extracted_phone)
        if final_data["phone_number"] == "Не найдено":
            final_data["phone_number"] = extracted_phone

    views_val = (
        payload.get("views")
        or payload.get("interested")
        or (tg.get("views") if isinstance(tg, dict) else None)
        or (tg.get("interested") if isinstance(tg, dict) else None)
    )
    if views_val is not None:
        final_data["views_count"] = views_val

    if isinstance(dbs, dict):
        for db_name, records in dbs.items():
            key_name = f"{source_prefix}{db_name}" if source_prefix and db_name in final_data["databases"] else db_name
            if key_name not in final_data["databases"]:
                final_data["databases"][key_name] = []
            if isinstance(records, list):
                final_data["databases"][key_name].extend(records)


async def search_everywhere(q: str):
    res_bb, res_argus = await asyncio.gather(
        fetch_blackbox(q, BLACKBOX_KEY),
        fetch_argus(q, ARGUS_KEY),
        return_exceptions=True
    )

    final_data = {
        "query": q,
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "fio": set(),
        "dob": set(),
        "snils": set(),
        "phones": set(),
        "phone_number": "Не найдено",
        "operator": "Нет",
        "region": "Нет",
        "country": "Россия",
        "databases": {},
        "tg_info": {},
        "reg_date": "Не найдено",
        "phonebooks": [],
        "name_history": [],
        "views_count": None,
        "sources_count": 0,
        "local_analysis": "",
        "raw_response": {
            "blackbox": res_bb if isinstance(res_bb, dict) else {},
            "argus": res_argus if isinstance(res_argus, dict) else {},
        }
    }

    if isinstance(res_bb, dict):
        _merge_single_response(res_bb, final_data, source_prefix="bb_")
    if isinstance(res_argus, dict):
        _merge_single_response(res_argus, final_data, source_prefix="argus_")

    final_data["fio"] = ", ".join(final_data["fio"]) if final_data["fio"] else "Не найдено"
    final_data["dob"] = ", ".join(final_data["dob"]) if final_data["dob"] else "Нет"
    final_data["snils"] = ", ".join(final_data["snils"]) if final_data["snils"] else "Нет"
    final_data["sources_count"] = len(final_data["databases"]) or (1 if final_data["fio"] != "Не найдено" else 0)

    return final_data


def format_report_text(data: dict) -> str:
    query = str(data.get("query") or "").strip()
    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}

    tg_id = tg.get("id") or tg.get("user_id") or "Не найдено"
    raw_username = tg.get("username") or (query if query.startswith("@") else "")
    tg_username = f"@{str(raw_username).lstrip('@')}" if raw_username else "Не найдено"

    reg_date = data.get("reg_date") or "Не найдено"
    phone_val = data.get("phone_number") or "Не найдено"

    phonebooks = data.get("phonebooks") or []
    phonebooks_str = ", ".join(phonebooks) if phonebooks else "Не найдено"

    history = data.get("name_history") or []
    history_header = f"🌀 История изменения имени ({len(history)}):" if history else "🌀 История изменения имени (0):"
    history_lines = [history_header]
    if history:
        for item in history[:12]:
            history_lines.append(html.escape(str(item)))
        if len(history) > 12:
            history_lines.append(f"… еще {len(history) - 12}")
    else:
        history_lines.append("Не найдено")

    if not _has_report_findings(data):
        safe_q = html.escape(query) if query else "Не найдено"
        return (
            f"✈️ Telegram · {safe_q}\n"
            f"▸ ID: Не найдено\n"
            f"▸ Номер: Не найдено\n"
            f"📅 Регистрация: Не найдено\n\n"
            f"🔎 Телефонные книги: Не найдено\n\n"
            f"🌀 История изменения имени (0):\n"
            f"Не найдено\n\n"
            f"💬 Telegram: {safe_q} (ID: Не найдено)\n\n"
            f"👁 Интересовались этим: 0\n\n\n"
            f"Полная информация доступна в файле ниже ⬇️"
        )

    views_val = data.get("views_count")
    if views_val is None:
        views_val = len(data.get("databases") or {})

    blocks = [
        f"✈️ Telegram · {html.escape(str(tg_username))}\n"
        f"▸ ID: {html.escape(str(tg_id))}\n"
        f"▸ Номер: {html.escape(str(phone_val))}\n"
        f"📅 Регистрация: {html.escape(str(reg_date))}",
        f"🔎 Телефонные книги: {html.escape(phonebooks_str)}",
        "\n".join(history_lines),
        f"💬 Telegram: {html.escape(str(tg_username))} (ID: {html.escape(str(tg_id))})",
        f"👁 Интересовались этим: {html.escape(str(views_val))}",
        "Полная информация доступна в файле ниже ⬇️"
    ]
    return "\n\n".join(blocks)


# --- ГЕНЕРАЦИЯ HTML ОТЧЕТА С КОРРЕКТНЫМ VIS.JS ГРАФОМ ---
def _build_graph_payload(data: dict) -> dict:
    nodes = []
    edges = []
    seen_labels = set()
    next_id = 1

    def add_node(label: str, group: str, parent_id: int | None = None, edge_label: str = ""):
        nonlocal next_id
        clean = label.strip()
        if not clean:
            return None
        dedup_key = f"{group}:{clean}"
        if dedup_key in seen_labels:
            return None
        seen_labels.add(dedup_key)

        node_id = next_id
        next_id += 1
        nodes.append({
            "id": node_id,
            "label": clean,
            "group": group
        })
        if parent_id is not None:
            edge = {"from": parent_id, "to": node_id}
            if edge_label:
                edge["label"] = edge_label
            edges.append(edge)
        return node_id

    query = str(data.get("query") or "").strip() or "Запрос"
    root_id = add_node(f"🔍 Запрос\n{query}", "query")

    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    tg_id = tg.get("id") or tg.get("user_id")
    raw_username = tg.get("username") or (query if query.startswith("@") else "")
    tg_username = f"@{str(raw_username).lstrip('@')}" if raw_username else ""

    tg_parts = []
    if tg_username:
        tg_parts.append(tg_username)
    if tg_id:
        tg_parts.append(f"ID: {tg_id}")
    if tg_parts:
        add_node("✈️ Telegram\n" + " · ".join(tg_parts), "telegram", root_id, "Telegram")

    phone = str(data.get("phone_number") or "").strip()
    if phone and phone != "Не найдено":
        add_node(f"📞 Телефон\n{phone}", "phone", root_id, "Номер")

    fio = str(data.get("fio") or "").strip()
    if fio and fio not in ("Не найдено", "НЕ НАЙДЕНО"):
        add_node(f"👤 ФИО\n{fio}", "person", root_id, "Личность")

    region = str(data.get("region") or "").strip()
    if region and region not in ("Нет", "Не найдено"):
        add_node(f"📍 Регион\n{region}", "region", root_id, "Локация")

    for pb in (data.get("phonebooks") or [])[:8]:
        pb_str = str(pb).strip()
        if pb_str:
            add_node(f"📒 Контакт\n{pb_str}", "contact", root_id, "Книга")

    dbs = data.get("databases") if isinstance(data.get("databases"), dict) else {}
    for db_name, records in list(dbs.items())[:12]:
        rec_count = len(records) if isinstance(records, list) else 0
        add_node(f"🗄 {db_name}\n({rec_count} зап.)", "db", root_id, "База")

    return {"nodes": nodes, "edges": edges}


def generate_html_report(data: dict, report_id: str) -> str:
    query = html.escape(str(data.get("query") or "—"))
    fio = html.escape(str(data.get("fio") or "Не найдено"))
    phone = html.escape(str(data.get("phone_number") or "Не найдено"))
    reg_date = html.escape(str(data.get("reg_date") or "Не найдено"))
    dob = html.escape(str(data.get("dob") or "Нет"))
    snils = html.escape(str(data.get("snils") or "Нет"))
    operator = html.escape(str(data.get("operator") or "Нет"))
    region = html.escape(str(data.get("region") or "Нет"))

    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    tg_id = html.escape(str(tg.get("id") or tg.get("user_id") or "Не найдено"))
    raw_username = tg.get("username") or (str(data.get("query") or "") if str(data.get("query") or "").startswith("@") else "")
    tg_username = html.escape(f"@{str(raw_username).lstrip('@')}" if raw_username else "Не найдено")

    phonebooks_list = data.get("phonebooks") or []
    phonebooks_html = html.escape(", ".join(phonebooks_list)) if phonebooks_list else "Не найдено"

    history_list = data.get("name_history") or []
    if history_list:
        history_html = "<br>".join(html.escape(str(item)) for item in history_list)
    else:
        history_html = "Не найдено"

    graph_payload = _build_graph_payload(data)
    graph_json = json.dumps(graph_payload, ensure_ascii=False).replace("</", "<\\/")

    export_dict = {
        "report_id": report_id,
        "query": data.get("query"),
        "date": data.get("date"),
        "telegram": {"username": tg_username, "id": tg_id, "reg_date": data.get("reg_date")},
        "phone_number": data.get("phone_number"),
        "fio": data.get("fio"),
        "phonebooks": data.get("phonebooks"),
        "name_history": data.get("name_history"),
        "databases": data.get("databases"),
    }
    raw_json_str = html.escape(json.dumps(export_dict, ensure_ascii=False, indent=2))

    dbs_html = ""
    for db_name, records in (data.get("databases") or {}).items():
        safe_db = html.escape(str(db_name))
        rows_html = ""
        if isinstance(records, list) and records:
            for rec in records[:30]:
                if isinstance(rec, dict):
                    items = "<br>".join(
                        f"<b>{html.escape(str(k))}:</b> {html.escape(str(v))}"
                        for k, v in rec.items()
                    )
                    rows_html += f"<div class='rec-row'>{items}</div>"
                else:
                    rows_html += f"<div class='rec-row'>{html.escape(str(rec))}</div>"
        else:
            rows_html = "<div class='rec-row'>Нет записей</div>"
        dbs_html += f"<div class='card'><h3>📁 {safe_db}</h3>{rows_html}</div>"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Oasis Digital — Отчет по {query}</title>
    <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        html, body {{
            font-family: 'Inter', sans-serif;
            background-color: #0f1015;
            color: #e2e8f0;
            width: 100%;
            height: 100%;
            overflow: hidden;
        }}
        .app-shell {{
            display: flex;
            width: 100vw;
            height: 100vh;
        }}
        .sidebar {{
            width: 260px;
            min-width: 240px;
            background-color: #0b0c10;
            border-right: 1px solid #1f2232;
            display: flex;
            flex-direction: column;
            padding: 20px 12px;
            gap: 6px;
        }}
        .brand {{
            padding: 4px 12px 18px 12px;
            font-size: 18px;
            font-weight: 700;
            color: #fff;
            border-bottom: 1px solid #1f2232;
            margin-bottom: 10px;
        }}
        .brand span {{
            display: block;
            font-size: 12px;
            font-weight: 400;
            color: #94a3b8;
            margin-top: 4px;
        }}
        .nav-item {{
            padding: 11px 14px;
            border-radius: 8px;
            cursor: pointer;
            color: #94a3b8;
            font-size: 14px;
            font-weight: 500;
            transition: background 0.15s, color 0.15s;
        }}
        .nav-item:hover {{
            background-color: #171926;
            color: #f8fafc;
        }}
        .nav-item.active {{
            background-color: #8b5cf6;
            color: #fff;
        }}
        .btn-fit {{
            margin-top: auto;
            padding: 10px 14px;
            border-radius: 8px;
            border: 1px solid #2a2f45;
            background: #141722;
            color: #e2e8f0;
            font-size: 13px;
            cursor: pointer;
        }}
        .btn-fit:hover {{
            background: #1e2334;
        }}
        .main-content {{
            flex: 1;
            width: 100%;
            height: 100%;
            position: relative;
            overflow-y: auto;
            padding: 18px;
        }}
        .tab-content {{
            display: none;
            width: 100%;
            height: 100%;
        }}
        .tab-content.active {{
            display: block;
        }}
        .graph-wrap {{
            width: 100%;
            height: 100%;
            min-height: 520px;
            background-color: #0b0c10;
            border: 1px solid #1f2232;
            border-radius: 12px;
            position: relative;
        }}
        #mynetwork {{
            width: 100%;
            height: 100%;
        }}
        .legend {{
            position: absolute;
            top: 14px;
            right: 14px;
            z-index: 10;
            background: rgba(15, 16, 21, 0.9);
            border: 1px solid #24283b;
            border-radius: 10px;
            padding: 10px 14px;
            font-size: 12px;
            color: #cbd5e1;
            line-height: 1.7;
        }}
        .card {{
            background-color: #0b0c10;
            border: 1px solid #1f2232;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 16px;
        }}
        .card h3 {{
            font-size: 15px;
            margin-bottom: 12px;
            color: #f8fafc;
        }}
        .data-row {{
            display: flex;
            gap: 16px;
            padding: 10px 0;
            border-bottom: 1px solid #1a1d2b;
            font-size: 14px;
        }}
        .data-row:last-child {{
            border-bottom: none;
        }}
        .data-label {{
            width: 190px;
            min-width: 150px;
            color: #94a3b8;
        }}
        .data-value {{
            color: #f8fafc;
            word-break: break-word;
        }}
        .rec-row {{
            border-bottom: 1px solid #1a1d2b;
            padding: 8px 0;
            font-size: 13px;
            line-height: 1.5;
        }}
        .rec-row:last-child {{
            border-bottom: none;
        }}
        pre.json-box {{
            background: #0b0c10;
            border: 1px solid #1f2232;
            border-radius: 12px;
            padding: 18px;
            font-size: 12px;
            line-height: 1.5;
            overflow-x: auto;
            color: #cbd5e1;
        }}
    </style>
</head>
<body>
    <div class="app-shell">
        <aside class="sidebar">
            <div class="brand">
                Oasis Digital
                <span>Отчет #{html.escape(report_id)}</span>
            </div>
            <div class="nav-item active" onclick="openTab('tab-graph', this)">🕸️ Граф связей</div>
            <div class="nav-item" onclick="openTab('tab-summary', this)">📋 Сводка данных</div>
            <div class="nav-item" onclick="openTab('tab-json', this)">📦 JSON</div>
            <button class="btn-fit" onclick="fitGraph()">🎯 Центрировать граф</button>
        </aside>

        <main class="main-content">
            <div id="tab-graph" class="tab-content active">
                <div class="graph-wrap">
                    <div class="legend">
                        <div>🟣 Запрос</div>
                        <div>🔵 Telegram / Контакты</div>
                        <div>🟢 Личность / Телефон</div>
                        <div>🟠 Базы данных</div>
                    </div>
                    <div id="mynetwork"></div>
                </div>
            </div>

            <div id="tab-summary" class="tab-content">
                <div class="card">
                    <h3>Основная информация</h3>
                    <div class="data-row"><div class="data-label">Запрос</div><div class="data-value">{query}</div></div>
                    <div class="data-row"><div class="data-label">Telegram</div><div class="data-value">{tg_username} (ID: {tg_id})</div></div>
                    <div class="data-row"><div class="data-label">Номер телефона</div><div class="data-value">{phone}</div></div>
                    <div class="data-row"><div class="data-label">Регистрация</div><div class="data-value">{reg_date}</div></div>
                    <div class="data-row"><div class="data-label">Телефонные книги</div><div class="data-value">{phonebooks_html}</div></div>
                    <div class="data-row"><div class="data-label">ФИО</div><div class="data-value">{fio}</div></div>
                    <div class="data-row"><div class="data-label">Дата рождения</div><div class="data-value">{dob}</div></div>
                    <div class="data-row"><div class="data-label">СНИЛС</div><div class="data-value">{snils}</div></div>
                    <div class="data-row"><div class="data-label">Оператор / Регион</div><div class="data-value">{operator} / {region}</div></div>
                </div>

                <div class="card">
                    <h3>История изменения имени</h3>
                    <div class="rec-row">{history_html}</div>
                </div>

                {dbs_html}
            </div>

            <div id="tab-json" class="tab-content">
                <pre class="json-box">{raw_json_str}</pre>
            </div>
        </main>
    </div>

    <script>
        var network = null;

        function initGraph() {{
            var container = document.getElementById('mynetwork');
            var graphData = {graph_json};
            var data = {{
                nodes: new vis.DataSet(graphData.nodes),
                edges: new vis.DataSet(graphData.edges)
            }};
            var options = {{
                autoResize: true,
                nodes: {{
                    shape: 'box',
                    margin: {{ top: 12, right: 16, bottom: 12, left: 16 }},
                    widthConstraint: {{ minimum: 140, maximum: 280 }},
                    font: {{
                        color: '#ffffff',
                        size: 14,
                        face: 'Inter, sans-serif'
                    }},
                    borderWidth: 1.5,
                    shadow: {{
                        enabled: true,
                        color: 'rgba(0,0,0,0.45)',
                        size: 10,
                        x: 0,
                        y: 4
                    }}
                }},
                edges: {{
                    arrows: {{ to: {{ enabled: true, scaleFactor: 0.7 }} }},
                    color: {{ color: '#475569', highlight: '#a855f7' }},
                    width: 1.8,
                    smooth: {{ type: 'dynamic' }},
                    font: {{
                        color: '#94a3b8',
                        size: 11,
                        strokeWidth: 0
                    }}
                }},
                groups: {{
                    query: {{ color: {{ background: '#6d28d9', border: '#a78bfa' }} }},
                    telegram: {{ color: {{ background: '#0369a1', border: '#38bdf8' }} }},
                    phone: {{ color: {{ background: '#15803d', border: '#4ade80' }} }},
                    person: {{ color: {{ background: '#047857', border: '#34d399' }} }},
                    region: {{ color: {{ background: '#1d4ed8', border: '#60a5fa' }} }},
                    contact: {{ color: {{ background: '#0e7490', border: '#22d3ee' }} }},
                    db: {{ color: {{ background: '#b45309', border: '#fbbf24' }} }}
                }},
                physics: {{
                    enabled: true,
                    solver: 'forceAtlas2Based',
                    forceAtlas2Based: {{
                        gravitationalConstant: -90,
                        centralGravity: 0.015,
                        springLength: 190,
                        springConstant: 0.06,
                        avoidOverlap: 1
                    }},
                    stabilization: {{
                        enabled: true,
                        iterations: 150
                    }}
                }},
                interaction: {{
                    hover: true,
                    navigationButtons: true,
                    keyboard: true
                }}
            }};

            network = new vis.Network(container, data, options);
            network.once('stabilized', function () {{
                network.fit({{ animation: {{ duration: 400, easingFunction: 'easeInOutQuad' }} }});
            }});
        }}

        function fitGraph() {{
            if (network) {{
                network.fit({{ animation: {{ duration: 350, easingFunction: 'easeInOutQuad' }} }});
            }}
        }}

        function openTab(tabId, el) {{
            document.querySelectorAll('.tab-content').forEach(function (t) {{
                t.classList.remove('active');
            }});
            document.querySelectorAll('.nav-item').forEach(function (n) {{
                n.classList.remove('active');
            }});
            document.getElementById(tabId).classList.add('active');
            el.classList.add('active');

            if (tabId === 'tab-graph' && network) {{
                setTimeout(function () {{
                    network.redraw();
                    network.fit({{ animation: false }});
                }}, 50);
            }}
        }}

        window.addEventListener('load', initGraph);
        window.addEventListener('resize', function () {{
            if (network) {{
                network.redraw();
            }}
        }});
    </script>
</body>
</html>"""


# --- FASTAPI ---
web_app = FastAPI()
web_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@web_app.get("/report/{report_id}", response_class=HTMLResponse)
async def web_report(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data:
        return HTMLResponse("<h1>404 Отчет не найден или устарел</h1>", status_code=404)
    return HTMLResponse(content=generate_html_report(data, report_id))


async def run_web():
    config = uvicorn.Config(web_app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)), log_level="info")
    await uvicorn.Server(config).serve()


# --- БОТ ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if is_banned(message.from_user.id):
        return await message.answer("🚫 Вы заблокированы.")
    add_user(message.from_user.id, message.from_user.username)
    await message.answer(START_TEXT, parse_mode="HTML", reply_markup=main_kb())


@dp.callback_query(F.data == "back_to_start")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(START_TEXT, parse_mode="HTML", reply_markup=main_kb())
    await cb.answer()


@dp.callback_query(F.data == "search_info")
async def cb_search_info(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text(START_TEXT, parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()


@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    days_str = calculate_days(user[4]) if user else "0 дн."
    text = (
        f"👤 <b>Мой профиль</b>\n\nID: <code>{cb.from_user.id}</code>\n"
        f"Доступно запросов: <b>{user[2] if user else 0}</b>\n"
        f"Дата регистрации: {user[4] if user else '-'}\n"
        f"<i>(Вы агент уже: {days_str})</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡️ Скрытие данных ($5)", callback_data="hide_data_menu")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]
    ])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "hide_data_menu")
async def cb_hide_data_menu(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Оплатить Tonkeeper", callback_data="pay_tonkeeper")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="profile")]
    ])
    await cb.message.edit_text(HIDE_DATA_TEXT, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "pay_tonkeeper")
async def cb_pay_tonkeeper(cb: CallbackQuery):
    memo = f"Oasis_{cb.from_user.id}"
    link = f"ton://transfer/{TON_WALLET}?amount=13040000000&text={memo}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 Оплатить в 1 клик", url=link)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="hide_data_menu")]
    ])
    await cb.message.edit_text(f"💎 <b>Оплата через TON</b>\n\nКошелек: <code>{TON_WALLET}</code>\nMemo: <code>{memo}</code>", parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "buy_requests")
async def cb_buy_requests(cb: CallbackQuery):
    await cb.message.edit_text("💎 <b>Покупка запросов</b>\nОбратитесь к администратору: " + ADMIN_USERNAME, parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()


@dp.callback_query(F.data == "ref_link")
async def cb_ref_link(cb: CallbackQuery):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={cb.from_user.id}"
    await cb.message.edit_text(f"🔗 <b>Реферальная ссылка:</b>\n\n<code>{ref_link}</code>", parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()


@dp.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.wait_support_msg)
    await cb.message.edit_text(SUPPORT_TEXT, parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()


@dp.message(SearchStates.wait_support_msg)
async def process_support_msg(msg: Message, state: FSMContext):
    await state.clear()
    try:
        await bot.send_message(MAIN_ADMIN_ID, f"📩 Поддержка от {msg.from_user.full_name} (`{msg.from_user.id}`):\n\n{msg.text}", parse_mode="HTML")
        await msg.answer("✅ Отправлено администрации.", reply_markup=main_kb())
    except Exception:
        await msg.answer("❌ Ошибка отправки.", reply_markup=main_kb())


# --- ПРОМОКОДЫ ---
@dp.callback_query(F.data == "use_promo")
async def cb_use_promo(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.wait_promo_input)
    await cb.message.edit_text("🎟 <b>Активация промокода / чека</b>\n\nВведите код сообщением:", parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()


@dp.message(SearchStates.wait_promo_input)
async def process_promo_input(msg: Message, state: FSMContext):
    await state.clear()
    code = msg.text.strip()
    user_id = msg.from_user.id

    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT reward, max_uses, uses_count FROM promos WHERE code = ?", (code,))
    promo = cursor.fetchone()

    if not promo:
        conn.close()
        return await msg.answer("❌ Промокод не найден.", reply_markup=main_kb())

    reward, max_uses, uses_count = promo
    cursor.execute("SELECT 1 FROM promo_activations WHERE user_id = ? AND code = ?", (user_id, code))
    if cursor.fetchone():
        conn.close()
        return await msg.answer("⚠️ Вы уже активировали этот промокод!", reply_markup=main_kb())

    if uses_count >= max_uses:
        conn.close()
        return await msg.answer("❌ Лимит активаций исчерпан.", reply_markup=main_kb())

    add_requests(user_id, reward)
    cursor.execute("UPDATE promos SET uses_count = uses_count + 1 WHERE code = ?", (code,))
    cursor.execute("INSERT INTO promo_activations (user_id, code) VALUES (?, ?)", (user_id, code))
    conn.commit()
    conn.close()

    await msg.answer(f"🎉 <b>Успешно!</b> Начислено: <b>+{reward} запросов</b> ⚡️", parse_mode="HTML", reply_markup=main_kb())


# --- АДМИНКА ---
@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    await msg.answer("👑 <b>Админ-панель Oasis Digital</b>", parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_promo")
async def cb_adm_promo(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("🎟 <b>Создание промокода</b>\n\nФормат: <code>КОД НАГРАДА ЛИМИТ</code>", parse_mode="HTML", reply_markup=back_kb())
    await state.set_state(SearchStates.wait_admin_create_promo)
    await cb.answer()


@dp.message(SearchStates.wait_admin_create_promo)
async def process_admin_create_promo(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    parts = msg.text.strip().split()
    if len(parts) < 2:
        return await msg.answer("❌ Неверный формат.", reply_markup=admin_menu_kb())
    code, reward = parts[0], int(parts[1])
    max_uses = int(parts[2]) if len(parts) > 2 else 1

    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO promos (code, reward, max_uses, uses_count, created_by) VALUES (?, ?, ?, 0, ?)", (code, reward, max_uses, msg.from_user.id))
        conn.commit()
    except Exception:
        conn.close()
        return await msg.answer("❌ Такой код уже есть!", reply_markup=admin_menu_kb())
    conn.close()
    await msg.answer(f"✅ Промокод <code>{code}</code> на +{reward} запросов создан!", parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_check")
async def cb_adm_check(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("💳 <b>Создание чека</b>\n\nВведите номинал (запросов):", parse_mode="HTML", reply_markup=back_kb())
    await state.set_state(SearchStates.wait_admin_create_check)
    await cb.answer()


@dp.message(SearchStates.wait_admin_create_check)
async def process_admin_create_check(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    try:
        reward = int(msg.text.strip())
    except ValueError:
        return await msg.answer("❌ Введите число.", reply_markup=admin_menu_kb())

    check_code = f"CHECK-{uuid.uuid4().hex[:8].upper()}"
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO promos (code, reward, max_uses, uses_count, created_by) VALUES (?, ?, 1, 0, ?)", (check_code, reward, msg.from_user.id))
    conn.commit()
    conn.close()

    await msg.answer(f"✅ Чек создан: <code>{check_code}</code> на <b>{reward}</b> запросов.", parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_test_api")
async def cb_adm_test_api(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("📡 <b>Тест API (Raw)</b>\n\nВведите запрос (таймаут 90 сек):", parse_mode="HTML", reply_markup=back_kb())
    await state.set_state(SearchStates.wait_admin_raw)
    await cb.answer()


@dp.message(SearchStates.wait_admin_raw)
async def process_admin_raw(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    q = msg.text.strip()
    note = await msg.answer(f"📡 Опрашиваем Black-Box и Argus (до 90с): <b>{html.escape(q)}</b>...", parse_mode="HTML")

    res_bb, res_argus = await asyncio.gather(
        fetch_blackbox(q, BLACKBOX_KEY, return_debug=True),
        fetch_argus(q, ARGUS_KEY, return_debug=True),
        return_exceptions=True
    )

    bb_str = json.dumps(res_bb, ensure_ascii=False, indent=2)[:1400] if isinstance(res_bb, dict) else str(res_bb)
    argus_str = json.dumps(res_argus, ensure_ascii=False, indent=2)[:1400] if isinstance(res_argus, dict) else str(res_argus)

    text = f"<b>BLACK-BOX:</b>\n<pre>{html.escape(bb_str)}</pre>\n\n<b>ARGUS:</b>\n<pre>{html.escape(argus_str)}</pre>"
    await note.edit_text(text[:4000], parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_check_apis")
async def cb_adm_check_apis(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    note = await cb.message.edit_text("🔌 Проверяем Black-Box и Argus...", parse_mode="HTML")
    results = await check_all_apis_health()
    lines = [f"• <b>{r['name']}</b>: {'✅ OK' if r['healthy'] else '❌ FAIL'} ({r['status']}, {r['latency_ms']}ms)" for r in results]
    await note.edit_text("🔌 <b>Статус API:</b>\n\n" + "\n".join(lines), parse_mode="HTML", reply_markup=admin_menu_kb())


# --- ЛОКАЛЬНЫЙ АНАЛИЗАТОР ПО КНОПКЕ ---
@dp.callback_query(F.data.startswith("ai_analyze_"))
async def cb_ai_analyze(cb: CallbackQuery):
    report_id = cb.data.replace("ai_analyze_", "")
    data = REPORTS_CACHE.get(report_id)
    if not data:
        return await cb.answer("Отчет устарел", show_alert=True)

    await cb.answer("⚡ Анализирую...")
    summary = analyze_locally(data)
    text = f"🔍 <b>Цель:</b> <code>{html.escape(str(data.get('query') or ''))}</code>\n\n{summary}"
    await cb.message.answer(text[:4000], parse_mode="HTML")


@dp.callback_query(F.data.startswith("get_txt_"))
async def cb_send_txt(cb: CallbackQuery):
    report_id = cb.data.replace("get_txt_", "")
    data = REPORTS_CACHE.get(report_id)
    if not data:
        return await cb.answer("Отчет устарел", show_alert=True)
    text = (
        f"OASIS DIGITAL\n"
        f"Запрос: {data.get('query')}\n"
        f"Номер: {data.get('phone_number', 'Не найдено')}\n"
        f"ФИО: {data.get('fio', 'Не найдено')}\n"
        f"Дата рождения: {data.get('dob', 'Нет')}\n"
        f"СНИЛС: {data.get('snils', 'Нет')}"
    )
    file = BufferedInputFile(text.encode("utf-8"), filename=f"oasis_{report_id}.txt")
    await cb.message.answer_document(document=file, caption="📄 <b>TXT Отчет</b>", parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("get_json_"))
async def cb_send_json(cb: CallbackQuery):
    report_id = cb.data.replace("get_json_", "")
    data = REPORTS_CACHE.get(report_id)
    if not data:
        return await cb.answer("Отчет устарел", show_alert=True)
    export_obj = {
        "query": data.get("query"),
        "date": data.get("date"),
        "phone_number": data.get("phone_number"),
        "fio": data.get("fio"),
        "reg_date": data.get("reg_date"),
        "phonebooks": data.get("phonebooks"),
        "name_history": data.get("name_history"),
        "tg_info": data.get("tg_info"),
        "databases": data.get("databases"),
    }
    json_str = json.dumps(export_obj, ensure_ascii=False, indent=2)
    file = BufferedInputFile(json_str.encode("utf-8"), filename=f"oasis_{report_id}.json")
    await cb.message.answer_document(document=file, caption="📦 <b>JSON Отчет</b>", parse_mode="HTML")
    await cb.answer()


# --- ОСНОВНОЙ ПОИСК ---
@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message, state: FSMContext):
    if await state.get_state() is not None or msg.from_user.is_bot:
        return
    if is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы.")
    if not deduct_request(msg.from_user.id):
        return await msg.answer("❌ У вас закончились запросы.")

    q = msg.text.strip()
    note = await msg.answer(f"🔍 <b>[Oasis Digital]</b> Идет поиск по Black-Box и Argus (до 90 сек)...\n\n<b>Цель:</b> <code>{html.escape(q)}</code>", parse_mode="HTML")

    data = await search_everywhere(q)
    report_id = str(uuid.uuid4())[:8]
    REPORTS_CACHE[report_id] = data

    body = format_report_text(data)
    chunks = [body[i:i + 3900] for i in range(0, len(body), 3900)]
    await note.edit_text(chunks[0], parse_mode="HTML", reply_markup=result_kb(report_id) if len(chunks) == 1 else None)
    for extra in chunks[1:-1]:
        await msg.answer(extra, parse_mode="HTML")
    if len(chunks) > 1:
        await msg.answer(chunks[-1], parse_mode="HTML", reply_markup=result_kb(report_id))


async def main():
    log.info("Бот Oasis Digital запускается на Polling...")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(run_web())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
