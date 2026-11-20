import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import ssl
import uuid
from collections import OrderedDict
from datetime import datetime
from typing import Any

import aiohttp
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

# ====================== НАСТРОЙКИ OASIS DIGITAL ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9024954594:AAGj3lihxCTSGgCMaeE_mCi2519paMgIYms")
BOT_USERNAME = "@MobiusSearchBot"

MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "8394631079"))
ADMIN_USERNAME = "@anodoxx"

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1004311841781"))
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/+BqWOF7S3AqQ1YzI6")

# API-ключи
BLACKBOX_KEY = os.getenv("BLACKBOX_KEY", "8jdsYcPZiQMTpYNCluXg9scNhc7y")
ARGUS_KEY = os.getenv("ARGUS_KEY", "ARGUS-IO-M1b_3fSDQM5qp5TU5bDMOLAITOluLLtKGrtLSHD2GGE")

TON_WALLET = os.getenv("TON_WALLET", "UQBlTLz8MaKDtNhYo1qeDsOXtVjN1upRp2uwsf0NWWE9p5cx")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://listopadov-search-1.onrender.com").rstrip("/")
USERS_DB = os.getenv("USERS_DB", "users.db")

API_URL_BLACKBOX = "https://api.black-box.cc/v1.1/search"
API_URL_ARGUS = "https://argus.cc.cd/api/v1/search"

PROXY_URL = os.getenv("PROXY_URL", None)
API_TIMEOUT = 90
MAX_RETRIES = 3

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("oasis")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class BoundedCache(OrderedDict):
    def __init__(self, maxlen: int = 1000, *args, **kwargs):
        self.maxlen = maxlen
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: str, value: Any):
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


# ====================== БАЗА ДАННЫХ (SQLITE) ======================
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(USERS_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                requests_left INTEGER DEFAULT 3,
                referrer_id INTEGER,
                joined_at TEXT,
                is_banned INTEGER DEFAULT 0
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS promos (
                code TEXT PRIMARY KEY,
                reward INTEGER,
                max_uses INTEGER DEFAULT 1,
                uses_count INTEGER DEFAULT 0,
                created_by INTEGER
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_activations (
                user_id INTEGER,
                code TEXT,
                PRIMARY KEY (user_id, code)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                report_id TEXT PRIMARY KEY,
                query TEXT,
                payload_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.commit()


init_db()


def save_report(report_id: str, data: dict):
    serializable = _make_json_safe(data)
    REPORTS_CACHE[report_id] = serializable
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO reports (report_id, query, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                report_id,
                str(serializable.get("query") or ""),
                json.dumps(serializable, ensure_ascii=False),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()


def load_report(report_id: str) -> dict | None:
    if report_id in REPORTS_CACHE:
        return REPORTS_CACHE[report_id]
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
    if not row:
        return None
    try:
        parsed = json.loads(row["payload_json"])
        REPORTS_CACHE[report_id] = parsed
        return parsed
    except Exception:
        return None


def _make_json_safe(obj: Any) -> Any:
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, dict):
        return {str(k): _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(x) for x in obj]
    return obj


def get_user(user_id: int):
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return tuple(row) if row else None


def add_user(user_id: int, username: str | None, referrer_id: int | None = None) -> bool:
    with get_db_connection() as conn:
        exists = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if exists:
            return False
        conn.execute(
            "INSERT INTO users (user_id, username, requests_left, referrer_id, joined_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, 3, referrer_id, datetime.now().strftime("%d.%m.%Y")),
        )
        conn.commit()
    if referrer_id and referrer_id != user_id:
        add_requests(referrer_id, 1)
    return True


def add_requests(user_id: int, amount: int):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET requests_left = requests_left + ? WHERE user_id = ?",
            (amount, user_id),
        )
        conn.commit()


def deduct_request(user_id: int) -> bool:
    if user_id == MAIN_ADMIN_ID:
        return True
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT requests_left, is_banned FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row and row["requests_left"] > 0 and row["is_banned"] == 0:
            conn.execute(
                "UPDATE users SET requests_left = requests_left - 1 WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
            return True
    return False


def is_banned(user_id: int) -> bool:
    with get_db_connection() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row["is_banned"] == 1)


def calculate_days(joined_str: str) -> str:
    try:
        joined_date = datetime.strptime(joined_str, "%d.%m.%Y")
        delta = datetime.now() - joined_date
        days = max(delta.days, 0)
        months = days // 30
        rem_days = days % 30
        if months > 0:
            return f"{months} мес. {rem_days} дн."
        return f"{rem_days} дн."
    except Exception:
        return "0 дн."


# ====================== ТЕКСТЫ И КЛАВИАТУРЫ ======================
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
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Поиск", callback_data="search_info")],
            [InlineKeyboardButton(text="💎 Купить запросы", callback_data="buy_requests")],
            [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
            [InlineKeyboardButton(text="🎟 Активировать промокод", callback_data="use_promo")],
            [
                InlineKeyboardButton(text="🔗 Рефералка", callback_data="ref_link"),
                InlineKeyboardButton(text="🆘 Связь с нами", callback_data="support"),
            ],
        ]
    )


def back_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]]
    )


def result_kb(report_id: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 AI-анализатор", callback_data=f"ai_analyze_{report_id}")],
            [
                InlineKeyboardButton(
                    text="🌐 Веб-отчет (Граф)",
                    url=f"{WEBHOOK_HOST}/report/{report_id}",
                )
            ],
            [
                InlineKeyboardButton(text="📄 Скачать TXT", callback_data=f"get_txt_{report_id}"),
                InlineKeyboardButton(text="📦 Скачать JSON", callback_data=f"get_json_{report_id}"),
            ],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")],
        ]
    )


def admin_menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📡 Тест API (Raw)", callback_data="adm_test_api")],
            [InlineKeyboardButton(text="🔌 Проверка API", callback_data="adm_check_apis")],
            [
                InlineKeyboardButton(text="🎟 Создать промокод", callback_data="adm_promo"),
                InlineKeyboardButton(text="💳 Создать чек", callback_data="adm_check"),
            ],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")],
        ]
    )


# ====================== ОПРЕДЕЛЕНИЕ ТИПА ЗАПРОСА ======================
def detect_query_type(query: str) -> str:
    q = (query or "").strip()
    low = q.lower()
    digits = re.sub(r"\D", "", q)

    if q.startswith("@") or "t.me/" in low or "telegram" in low or re.fullmatch(r"\d{6,11}", q):
        return "telegram"
    if re.fullmatch(r"\+?\d[\d\s\-()]{6,17}", q) and 7 <= len(digits) <= 15:
        return "phone"
    if "@" in q and "." in q.split("@")[-1]:
        return "email"
    if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", q.upper()):
        return "vin"
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", q):
        return "ip"
    if re.fullmatch(r"[АВЕКМНОРСТУХABEKMHOPCTYX]{1,2}\d{3}[АВЕКМНОРСТУХABEKMHOPCTYX]{2,3}\d{2,3}", re.sub(r"\s+", "", q.upper())):
        return "vehicle"
    return "general"


# ====================== ЗАПРОСЫ К ВНЕШНИМ API ======================
async def _get_with_retry(
    url: str,
    api_key: str,
    tag: str,
    params: dict,
    return_debug: bool = False,
):
    headers = {
        **DEFAULT_HEADERS,
        "X-API-Key": api_key,
        "Authorization": f"Bearer {api_key}",
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
                    log.warning("[%s] Попытка %s/%s -> %s", tag, attempt, MAX_RETRIES, last_error)
                    if resp.status in (502, 503, 504) and attempt < MAX_RETRIES:
                        await asyncio.sleep(3)
                        continue
                    break
        except Exception as e:
            last_error = f"Exception: {e}"
            log.error("[%s ERROR] Попытка %s/%s: %s", tag, attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(3)

    return {"_error": last_error} if return_debug else None


async def fetch_blackbox(q: str, api_key: str = BLACKBOX_KEY, return_debug: bool = False):
    q_type = detect_query_type(q)
    bb_type = "phone" if q_type == "phone" else ("email" if q_type == "email" else "auto")
    return await _get_with_retry(
        API_URL_BLACKBOX,
        api_key=api_key,
        tag="BLACKBOX",
        params={"q": q.strip(), "type": bb_type},
        return_debug=return_debug,
    )


async def fetch_argus(q: str, api_key: str = ARGUS_KEY, return_debug: bool = False):
    return await _get_with_retry(
        API_URL_ARGUS,
        api_key=api_key,
        tag="ARGUS",
        params={"q": q.strip(), "key": api_key},
        return_debug=return_debug,
    )


async def _single_health_check(name: str, url: str, api_key: str, params: dict):
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    headers = {**DEFAULT_HEADERS, "X-API-Key": api_key}
    start = asyncio.get_event_loop().time()
    try:
        connector = aiohttp.TCPConnector(ssl=ssl.create_default_context())
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(url, headers=headers, params=params, proxy=PROXY_URL) as resp:
                elapsed = int((asyncio.get_event_loop().time() - start) * 1000)
                return {
                    "name": name,
                    "healthy": 200 <= resp.status < 300,
                    "status": resp.status,
                    "latency_ms": elapsed,
                }
    except Exception as e:
        return {
            "name": name,
            "healthy": False,
            "status": f"ERR ({type(e).__name__})",
            "latency_ms": 0,
        }


async def check_all_apis_health():
    return await asyncio.gather(
        _single_health_check("Black-Box", API_URL_BLACKBOX, BLACKBOX_KEY, {"q": "79825207224", "type": "phone"}),
        _single_health_check("Argus", API_URL_ARGUS, ARGUS_KEY, {"q": "79991234567", "key": ARGUS_KEY}),
    )


# ====================== ПАРСИНГ И АГРЕГАЦИЯ ======================
FIO_KEYS = {"fio", "full_name", "fullname", "name", "primary_fio", "owner", "holder"}
DOB_KEYS = {"dob", "birth_date", "birthdate", "birthday", "date_of_birth"}
SNILS_KEYS = {"snils"}
PHONE_KEYS = {"phone", "phone_number", "mobile", "msisdn", "number", "tel"}
OPERATOR_KEYS = {"operator", "carrier", "provider"}
REGION_KEYS = {"region", "city", "area", "location"}
REG_DATE_KEYS = {"reg_date", "registration", "registered_at", "created_at", "first_seen"}
EMAIL_KEYS = {"email", "e_mail", "mail"}


def _walk_records(node: Any, path: str = ""):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            sub = f"{path}.{k}" if path else str(k)
            yield from _walk_records(v, sub)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            sub = f"{path}[{idx}]"
            yield from _walk_records(item, sub)


def _merge_single_response(resp: Any, final_data: dict, source_prefix: str = ""):
    if not isinstance(resp, dict) or "_error" in resp:
        return

    payload = resp.get("data") if isinstance(resp.get("data"), dict) else resp

    for sub_path, obj in _walk_records(payload):
        low_path = sub_path.lower()
        for k, v in obj.items():
            if v is None or isinstance(v, (dict, list)):
                continue
            lk = str(k).lower().strip()
            sval = str(v).strip()
            if not sval or sval.lower() in ("none", "null", "nan", "не найдено", "unknown", "-"):
                continue

            if lk in FIO_KEYS and len(sval) >= 2 and not re.fullmatch(r"\+?[\d\s\-()]+", sval):
                final_data["fio_set"].add(sval)
            elif lk in DOB_KEYS:
                final_data["dob_set"].add(sval)
            elif lk in SNILS_KEYS:
                final_data["snils_set"].add(sval)
            elif lk in PHONE_KEYS:
                digits = re.sub(r"\D", "", sval)
                if 7 <= len(digits) <= 15:
                    final_data["phones_set"].add(sval)
                    if final_data["phone_number"] == "Не найдено":
                        final_data["phone_number"] = sval
            elif lk in OPERATOR_KEYS and final_data["operator"] == "Нет":
                final_data["operator"] = sval
            elif lk in REGION_KEYS and final_data["region"] == "Нет":
                final_data["region"] = sval
            elif lk in REG_DATE_KEYS and final_data["reg_date"] == "Не найдено":
                final_data["reg_date"] = sval
            elif lk in EMAIL_KEYS:
                final_data["emails_set"].add(sval)
            elif lk in {"id", "user_id", "tg_id", "telegram_id"} and ("tg" in low_path or "telegram" in low_path or "user" in low_path):
                if not final_data["tg_info"].get("id"):
                    final_data["tg_info"]["id"] = sval
            elif lk in {"username", "tg_username", "login"}:
                clean_u = f"@{sval.lstrip('@')}"
                if clean_u not in final_data["usernames_set"]:
                    final_data["usernames_set"].append(clean_u)
                if not final_data["tg_info"].get("username"):
                    final_data["tg_info"]["username"] = clean_u

        if any(tok in lk for tok in ("history", "name_history", "changes", "nicknames")):
            if isinstance(v, list):
                for h in v:
                    hs = str(h).strip()
                    if hs and hs not in final_data["name_history"]:
                        final_data["name_history"].append(hs)

        if any(tok in lk for tok in ("phonebook", "contacts", "tags", "saved_as")):
            if isinstance(v, list):
                for pb in v:
                    pbs = str(pb).strip()
                    if pbs and pbs not in final_data["phonebooks"]:
                        final_data["phonebooks"].append(pbs)
            elif isinstance(v, str) and "," in v:
                for pb in v.split(","):
                    pbs = pb.strip()
                    if pbs and pbs not in final_data["phonebooks"]:
                        final_data["phonebooks"].append(pbs)

    for container_key in ("databases", "results", "sources", "leaks"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            for db_name, records in container.items():
                key = f"{source_prefix}{db_name}" if source_prefix and db_name in final_data["databases"] else str(db_name)
                if key not in final_data["databases"]:
                    final_data["databases"][key] = []
                if isinstance(records, list):
                    final_data["databases"][key].extend(records)
                elif isinstance(records, dict):
                    final_data["databases"][key].append(records)
        elif isinstance(container, list) and container:
            key = f"{source_prefix}{container_key}"
            final_data["databases"].setdefault(key, []).extend(container)


async def search_everywhere(q: str) -> dict:
    res_bb, res_argus = await asyncio.gather(
        fetch_blackbox(q, BLACKBOX_KEY),
        fetch_argus(q, ARGUS_KEY),
        return_exceptions=True,
    )

    final_data: dict[str, Any] = {
        "query": q,
        "query_type": detect_query_type(q),
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "fio_set": set(),
        "dob_set": set(),
        "snils_set": set(),
        "phones_set": set(),
        "emails_set": set(),
        "usernames_set": [],
        "fio": "Не найдено",
        "dob": "Нет",
        "snils": "Нет",
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
            "blackbox": res_bb if isinstance(res_bb, dict) else {"error": str(res_bb)},
            "argus": res_argus if isinstance(res_argus, dict) else {"error": str(res_argus)},
        },
    }

    if isinstance(res_bb, dict):
        _merge_single_response(res_bb, final_data, source_prefix="bb_")
    if isinstance(res_argus, dict):
        _merge_single_response(res_argus, final_data, source_prefix="argus_")

    final_data["fio"] = ", ".join(sorted(final_data["fio_set"])) if final_data["fio_set"] else "Не найдено"
    final_data["dob"] = ", ".join(sorted(final_data["dob_set"])) if final_data["dob_set"] else "Нет"
    final_data["snils"] = ", ".join(sorted(final_data["snils_set"])) if final_data["snils_set"] else "Нет"

    total_records = sum(len(v) for v in final_data["databases"].values() if isinstance(v, list))
    final_data["sources_count"] = len(final_data["databases"])
    if final_data["views_count"] is None:
        final_data["views_count"] = total_records

    return _make_json_safe(final_data)


# ====================== ФОРМАТИРОВАНИЕ ОТЧЁТА ПОД ТИП ЗАПРОСА ======================
def _has_report_findings(data: dict) -> bool:
    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    return bool(
        tg.get("id")
        or tg.get("username")
        or (data.get("phone_number") and data.get("phone_number") != "Не найдено")
        or (data.get("fio") and data.get("fio") not in ("Не найдено", "НЕ НАЙДЕНО"))
        or bool(data.get("phonebooks"))
        or bool(data.get("name_history"))
        or bool(data.get("databases"))
        or bool(data.get("emails_set"))
    )


def format_report_text(data: dict) -> str:
    query = str(data.get("query") or "").strip()
    q_type = data.get("query_type") or detect_query_type(query)

    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    tg_id = tg.get("id") or tg.get("user_id")
    raw_username = tg.get("username") or (query if query.startswith("@") else "")
    tg_username = f"@{str(raw_username).lstrip('@')}" if raw_username else ""

    fio = str(data.get("fio") or "Не найдено").strip()
    dob = str(data.get("dob") or "Нет").strip()
    phone_val = str(data.get("phone_number") or "Не найдено").strip()
    operator = str(data.get("operator") or "Нет").strip()
    region = str(data.get("region") or "Нет").strip()
    reg_date = str(data.get("reg_date") or "Не найдено").strip()

    phonebooks = data.get("phonebooks") or []
    phonebooks_str = ", ".join(str(x) for x in phonebooks) if phonebooks else "Не найдено"

    history = data.get("name_history") or []
    history_lines = [f"🌀 История изменения имени ({len(history)}):"]
    if history:
        for item in history[:12]:
            history_lines.append(html.escape(str(item)))
        if len(history) > 12:
            history_lines.append(f"… еще {len(history) - 12}")
    else:
        history_lines.append("Не найдено")

    views_val = data.get("views_count")
    if views_val is None:
        views_val = sum(len(v) for v in (data.get("databases") or {}).values() if isinstance(v, list))

    if q_type == "phone":
        digits = re.sub(r"\D", "", query)
        shown_phone = phone_val if phone_val != "Не найдено" else (digits or query or "Не найдено")
        loc_parts = [x for x in (operator if operator != "Нет" else "", region if region != "Нет" else "") if x]
        loc_line = " / ".join(loc_parts) if loc_parts else "Не найдено"

        blocks = [
            f"📲 <b>Номер</b> · <code>{html.escape(str(shown_phone))}</code>\n"
            f"▸ Оператор / Регион: {html.escape(loc_line)}\n"
            f"▸ ФИО: {html.escape(fio)}\n"
            f"▸ Дата рождения: {html.escape(dob)}",
            f"🔎 Телефонные книги: {html.escape(phonebooks_str)}",
            "\n".join(history_lines),
        ]
        if tg_id or (tg_username and not re.fullmatch(r"@\+?\d+", tg_username)):
            blocks.append(
                f"✈️ Telegram: {html.escape(tg_username or '—')} (ID: <code>{html.escape(str(tg_id or '—'))}</code>)"
            )
    elif q_type == "telegram":
        shown_tg = tg_username or query or "Не найдено"
        shown_id = str(tg_id) if tg_id else (query if re.fullmatch(r"\d{6,11}", query) else "Не найдено")
        blocks = [
            f"✈️ <b>Telegram</b> · {html.escape(str(shown_tg))}\n"
            f"▸ ID: <code>{html.escape(shown_id)}</code>\n"
            f"▸ Номер: {html.escape(phone_val)}\n"
            f"📅 Регистрация: {html.escape(reg_date)}",
            f"🔎 Телефонные книги: {html.escape(phonebooks_str)}",
            "\n".join(history_lines),
            f"💬 Telegram: {html.escape(str(shown_tg))} (ID: <code>{html.escape(shown_id)}</code>)",
        ]
    elif q_type == "email":
        blocks = [
            f"✉️ <b>Email</b> · <code>{html.escape(query)}</code>\n"
            f"▸ ФИО: {html.escape(fio)}\n"
            f"▸ Номер: {html.escape(phone_val)}\n"
            f"▸ Дата рождения: {html.escape(dob)}",
            f"🔎 Телефонные книги: {html.escape(phonebooks_str)}",
            "\n".join(history_lines),
        ]
    elif q_type in ("vin", "vehicle"):
        icon = "🚘 VIN" if q_type == "vin" else "🚗 Госномер"
        blocks = [
            f"{icon} · <code>{html.escape(query.upper())}</code>\n"
            f"▸ Владелец / ФИО: {html.escape(fio)}\n"
            f"▸ Номер: {html.escape(phone_val)}\n"
            f"▸ Регион: {html.escape(region)}",
            "\n".join(history_lines),
        ]
    elif q_type == "ip":
        loc_parts = [x for x in (operator if operator != "Нет" else "", region if region != "Нет" else "") if x]
        loc_line = " / ".join(loc_parts) if loc_parts else "Не найдено"
        blocks = [
            f"🌐 <b>IP-адрес</b> · <code>{html.escape(query)}</code>\n"
            f"▸ Провайдер / Регион: {html.escape(loc_line)}\n"
            f"▸ ФИО / Владелец: {html.escape(fio)}\n"
            f"▸ Контакты: {html.escape(phone_val)}",
            "\n".join(history_lines),
        ]
    else:
        blocks = [
            f"🔎 <b>Запрос</b> · {html.escape(query or 'Не найдено')}\n"
            f"▸ ФИО: {html.escape(fio)}\n"
            f"▸ Номер: {html.escape(phone_val)}\n"
            f"▸ Дата рождения: {html.escape(dob)}",
            f"🔎 Телефонные книги: {html.escape(phonebooks_str)}",
            "\n".join(history_lines),
        ]

    if not _has_report_findings(data):
        blocks.append("⚠️ По открытым источникам и базам совпадений не найдено.")

    blocks.append(f"👁 Найдено записей / совпадений: <b>{html.escape(str(views_val))}</b>")
    blocks.append("Полный отчёт, граф связей и выгрузки доступны по кнопкам ниже ⬇️")
    return "\n\n".join(blocks)


def analyze_locally(data: dict) -> str:
    if data.get("local_analysis"):
        return str(data["local_analysis"])

    if not _has_report_findings(data):
        return "❌ Недостаточно данных для анализа — по запросу совпадений не найдено."

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

    usernames_seen: list[str] = []
    seen_set: set[str] = set()
    for cand in [tg_uname] + list(data.get("usernames_set") or []):
        if cand and cand.lower() not in seen_set:
            seen_set.add(cand.lower())
            usernames_seen.append(cand)

    for h in history:
        for match in re.findall(r"@[A-Za-z0-9_]{3,}", h):
            low = match.lower()
            if low not in seen_set:
                seen_set.add(low)
                usernames_seen.append(match)

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
        facts.append(f"Записные книги: {', '.join(phonebooks[:6])}")

    patterns = []
    if history:
        patterns.append(f"Изменений имени/ника зафиксировано: {len(history)}")
    if usernames_seen:
        patterns.append(f"Связанные никнеймы ({len(usernames_seen)}): {', '.join(usernames_seen[:8])}")
    if dbs:
        total_recs = sum(len(v) for v in dbs.values() if isinstance(v, list))
        patterns.append(f"Источников с данными: {len(dbs)} ({total_recs} записей)")

    lines = ["📊 <b>Анализ связей и фактов</b>\n"]
    if facts:
        lines.append("<b>Факты:</b>")
        for f_item in facts:
            lines.append(f"• {html.escape(f_item)}")
    if patterns:
        lines.append("\n<b>Активность и источники:</b>")
        for p_item in patterns:
            lines.append(f"• {html.escape(p_item)}")

    result = "\n".join(lines)
    data["local_analysis"] = result
    return result


# ====================== АДАПТИВНЫЙ WEB-ОТЧЁТ (ПК + МОБИЛЬНЫЕ) ======================
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
        nodes.append({"id": node_id, "label": clean, "group": group})
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
    history_html = "<br>".join(html.escape(str(item)) for item in history_list) if history_list else "Не найдено"

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
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
    <title>Oasis Digital — Отчет по {query}</title>
    <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        html, body {{
            font-family: 'Inter', sans-serif;
            background-color: #0f1015;
            color: #e2e8f0;
            min-height: 100vh;
            width: 100%;
            overflow-x: hidden;
        }}
        .app-shell {{
            display: flex;
            min-height: 100vh;
            width: 100%;
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
            flex-shrink: 0;
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
        .nav-group {{
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}
        .nav-item {{
            padding: 11px 14px;
            border-radius: 8px;
            cursor: pointer;
            color: #94a3b8;
            font-size: 14px;
            font-weight: 500;
            transition: background 0.15s, color 0.15s;
            white-space: nowrap;
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
            white-space: nowrap;
        }}
        .btn-fit:hover {{
            background: #1e2334;
        }}
        .main-content {{
            flex: 1;
            min-width: 0;
            padding: 18px;
            overflow-y: auto;
        }}
        .tab-content {{
            display: none;
            width: 100%;
        }}
        .tab-content.active {{
            display: block;
        }}
        .graph-wrap {{
            width: 100%;
            height: calc(100vh - 36px);
            min-height: 460px;
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
            top: 12px;
            right: 12px;
            z-index: 10;
            background: rgba(15, 16, 21, 0.92);
            border: 1px solid #24283b;
            border-radius: 10px;
            padding: 9px 12px;
            font-size: 11px;
            color: #cbd5e1;
            line-height: 1.65;
            pointer-events: none;
        }}
        .card {{
            background-color: #0b0c10;
            border: 1px solid #1f2232;
            border-radius: 12px;
            padding: 18px;
            margin-bottom: 14px;
        }}
        .card h3 {{
            font-size: 15px;
            margin-bottom: 12px;
            color: #f8fafc;
        }}
        .data-row {{
            display: flex;
            gap: 14px;
            padding: 10px 0;
            border-bottom: 1px solid #1a1d2b;
            font-size: 14px;
        }}
        .data-row:last-child {{
            border-bottom: none;
        }}
        .data-label {{
            width: 185px;
            min-width: 140px;
            color: #94a3b8;
            flex-shrink: 0;
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
            word-break: break-word;
        }}
        .rec-row:last-child {{
            border-bottom: none;
        }}
        pre.json-box {{
            background: #0b0c10;
            border: 1px solid #1f2232;
            border-radius: 12px;
            padding: 16px;
            font-size: 12px;
            line-height: 1.5;
            overflow-x: auto;
            color: #cbd5e1;
            white-space: pre-wrap;
            word-break: break-word;
        }}
        @media (max-width: 820px) {{
            .app-shell {{ flex-direction: column; }}
            .sidebar {{
                width: 100%;
                min-width: 0;
                border-right: none;
                border-bottom: 1px solid #1f2232;
                padding: 12px;
                gap: 8px;
            }}
            .brand {{
                padding: 0 4px 10px 4px;
                margin-bottom: 2px;
                font-size: 16px;
                display: flex;
                align-items: center;
                justify-content: space-between;
            }}
            .brand span {{ margin-top: 0; }}
            .nav-group {{
                flex-direction: row;
                overflow-x: auto;
                gap: 6px;
                padding-bottom: 2px;
            }}
            .nav-item {{ padding: 8px 12px; font-size: 13px; }}
            .btn-fit {{
                margin-top: 0;
                padding: 8px 12px;
                font-size: 12px;
                align-self: flex-start;
            }}
            .main-content {{ padding: 12px; }}
            .graph-wrap {{ height: calc(100vh - 150px); min-height: 360px; }}
            .data-row {{ flex-direction: column; gap: 4px; }}
            .data-label {{ width: auto; min-width: 0; font-size: 12px; }}
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
            <div class="nav-group">
                <div class="nav-item active" onclick="openTab('tab-graph', this)">🕸️ Граф связей</div>
                <div class="nav-item" onclick="openTab('tab-summary', this)">📋 Сводка данных</div>
                <div class="nav-item" onclick="openTab('tab-json', this)">📦 JSON</div>
            </div>
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
                    margin: {{ top: 10, right: 14, bottom: 10, left: 14 }},
                    widthConstraint: {{ minimum: 120, maximum: 260 }},
                    font: {{ color: '#ffffff', size: 13, face: 'Inter, sans-serif' }},
                    borderWidth: 1.5,
                    shadow: {{ enabled: true, color: 'rgba(0,0,0,0.45)', size: 10, x: 0, y: 4 }}
                }},
                edges: {{
                    arrows: {{ to: {{ enabled: true, scaleFactor: 0.7 }} }},
                    color: {{ color: '#475569', highlight: '#a855f7' }},
                    width: 1.8,
                    smooth: {{ type: 'dynamic' }},
                    font: {{ color: '#94a3b8', size: 11, strokeWidth: 0 }}
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
                        springLength: 180,
                        springConstant: 0.06,
                        avoidOverlap: 1
                    }},
                    stabilization: {{ enabled: true, iterations: 150 }}
                }},
                interaction: {{ hover: true, navigationButtons: true, keyboard: true }}
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
            document.querySelectorAll('.tab-content').forEach(function (t) {{ t.classList.remove('active'); }});
            document.querySelectorAll('.nav-item').forEach(function (n) {{ n.classList.remove('active'); }});
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
            if (network) {{ network.redraw(); }}
        }});
    </script>
</body>
</html>"""


# ====================== FASTAPI ======================
web_app = FastAPI()
web_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@web_app.get("/")
async def root_health():
    return {"status": "ok", "service": "Oasis Digital"}


@web_app.get("/report/{report_id}", response_class=HTMLResponse)
async def web_report(report_id: str):
    data = load_report(report_id)
    if not data:
        return HTMLResponse("<h1>404 Отчет не найден или устарел</h1>", status_code=404)
    return HTMLResponse(content=generate_html_report(data, report_id))


async def run_web():
    port = int(os.getenv("PORT", "10000"))
    config = uvicorn.Config(web_app, host="0.0.0.0", port=port, log_level="info")
    await uvicorn.Server(config).serve()


# ====================== БОТ ХЕНДЛЕРЫ ======================
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
    await state.clear()
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
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛡️ Скрытие данных ($5)", callback_data="hide_data_menu")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")],
        ]
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "hide_data_menu")
async def cb_hide_data_menu(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Оплатить Tonkeeper", callback_data="pay_tonkeeper")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="profile")],
        ]
    )
    await cb.message.edit_text(HIDE_DATA_TEXT, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "pay_tonkeeper")
async def cb_pay_tonkeeper(cb: CallbackQuery):
    memo = f"Oasis_{cb.from_user.id}"
    link = f"ton://transfer/{TON_WALLET}?amount=13040000000&text={memo}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📲 Оплатить в 1 клик", url=link)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="hide_data_menu")],
        ]
    )
    await cb.message.edit_text(
        f"💎 <b>Оплата через TON</b>\n\nКошелек: <code>{TON_WALLET}</code>\nMemo: <code>{memo}</code>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await cb.answer()


@dp.callback_query(F.data == "buy_requests")
async def cb_buy_requests(cb: CallbackQuery):
    await cb.message.edit_text(
        "💎 <b>Покупка запросов</b>\nОбратитесь к администратору: " + ADMIN_USERNAME,
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data == "ref_link")
async def cb_ref_link(cb: CallbackQuery):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={cb.from_user.id}"
    await cb.message.edit_text(
        f"🔗 <b>Реферальная ссылка:</b>\n\n<code>{ref_link}</code>",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
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
        await bot.send_message(
            MAIN_ADMIN_ID,
            f"📩 Поддержка от {html.escape(msg.from_user.full_name)} (<code>{msg.from_user.id}</code>):\n\n{html.escape(msg.text or '')}",
            parse_mode="HTML",
        )
        await msg.answer("✅ Отправлено администрации.", reply_markup=main_kb())
    except Exception:
        await msg.answer("❌ Ошибка отправки.", reply_markup=main_kb())


@dp.callback_query(F.data == "use_promo")
async def cb_use_promo(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.wait_promo_input)
    await cb.message.edit_text(
        "🎟 <b>Активация промокода / чека</b>\n\nВведите код сообщением:",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await cb.answer()


@dp.message(SearchStates.wait_promo_input)
async def process_promo_input(msg: Message, state: FSMContext):
    await state.clear()
    code = (msg.text or "").strip()
    user_id = msg.from_user.id

    with get_db_connection() as conn:
        promo = conn.execute(
            "SELECT reward, max_uses, uses_count FROM promos WHERE code = ?",
            (code,),
        ).fetchone()
        if not promo:
            return await msg.answer("❌ Промокод не найден.", reply_markup=main_kb())

        used = conn.execute(
            "SELECT 1 FROM promo_activations WHERE user_id = ? AND code = ?",
            (user_id, code),
        ).fetchone()
        if used:
            return await msg.answer("⚠️ Вы уже активировали этот промокод!", reply_markup=main_kb())

        if promo["uses_count"] >= promo["max_uses"]:
            return await msg.answer("❌ Лимит активаций исчерпан.", reply_markup=main_kb())

        conn.execute("UPDATE promos SET uses_count = uses_count + 1 WHERE code = ?", (code,))
        conn.execute(
            "INSERT INTO promo_activations (user_id, code) VALUES (?, ?)",
            (user_id, code),
        )
        conn.commit()

    add_requests(user_id, promo["reward"])
    await msg.answer(
        f"🎉 <b>Успешно!</b> Начислено: <b>+{promo['reward']} запросов</b> ⚡️",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


# ====================== АДМИНКА ======================
@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    await msg.answer("👑 <b>Админ-панель Oasis Digital</b>", parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_promo")
async def cb_adm_promo(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text(
        "🎟 <b>Создание промокода</b>\n\nФормат: <code>КОД НАГРАДА ЛИМИТ</code>",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await state.set_state(SearchStates.wait_admin_create_promo)
    await cb.answer()


@dp.message(SearchStates.wait_admin_create_promo)
async def process_admin_create_promo(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    parts = (msg.text or "").strip().split()
    if len(parts) < 2:
        return await msg.answer("❌ Неверный формат.", reply_markup=admin_menu_kb())
    try:
        code, reward = parts[0], int(parts[1])
        max_uses = int(parts[2]) if len(parts) > 2 else 1
    except ValueError:
        return await msg.answer("❌ Награда и лимит должны быть числами.", reply_markup=admin_menu_kb())

    with get_db_connection() as conn:
        try:
            conn.execute(
                "INSERT INTO promos (code, reward, max_uses, uses_count, created_by) VALUES (?, ?, ?, 0, ?)",
                (code, reward, max_uses, msg.from_user.id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return await msg.answer("❌ Такой код уже есть!", reply_markup=admin_menu_kb())

    await msg.answer(
        f"✅ Промокод <code>{html.escape(code)}</code> на +{reward} запросов создан!",
        parse_mode="HTML",
        reply_markup=admin_menu_kb(),
    )


@dp.callback_query(F.data == "adm_check")
async def cb_adm_check(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text(
        "💳 <b>Создание чека</b>\n\nВведите номинал (запросов):",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await state.set_state(SearchStates.wait_admin_create_check)
    await cb.answer()


@dp.message(SearchStates.wait_admin_create_check)
async def process_admin_create_check(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    try:
        reward = int((msg.text or "").strip())
    except ValueError:
        return await msg.answer("❌ Введите число.", reply_markup=admin_menu_kb())

    check_code = f"CHECK-{uuid.uuid4().hex[:8].upper()}"
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO promos (code, reward, max_uses, uses_count, created_by) VALUES (?, ?, 1, 0, ?)",
            (check_code, reward, msg.from_user.id),
        )
        conn.commit()

    await msg.answer(
        f"✅ Чек создан: <code>{check_code}</code> на <b>{reward}</b> запросов.",
        parse_mode="HTML",
        reply_markup=admin_menu_kb(),
    )


@dp.callback_query(F.data == "adm_test_api")
async def cb_adm_test_api(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text(
        "📡 <b>Тест API (Raw)</b>\n\nВведите запрос (таймаут 90 сек):",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await state.set_state(SearchStates.wait_admin_raw)
    await cb.answer()


@dp.message(SearchStates.wait_admin_raw)
async def process_admin_raw(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    q = (msg.text or "").strip()
    note = await msg.answer(
        f"📡 Опрашиваем Black-Box и Argus (до 90с): <b>{html.escape(q)}</b>...",
        parse_mode="HTML",
    )

    res_bb, res_argus = await asyncio.gather(
        fetch_blackbox(q, BLACKBOX_KEY, return_debug=True),
        fetch_argus(q, ARGUS_KEY, return_debug=True),
        return_exceptions=True,
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
    lines = [
        f"• <b>{r['name']}</b>: {'✅ OK' if r['healthy'] else '❌ FAIL'} ({r['status']}, {r['latency_ms']}ms)"
        for r in results
    ]
    await note.edit_text("🔌 <b>Статус API:</b>\n\n" + "\n".join(lines), parse_mode="HTML", reply_markup=admin_menu_kb())


# ====================== АНАЛИЗАТОР И ЭКСПОРТЫ ======================
@dp.callback_query(F.data.startswith("ai_analyze_"))
async def cb_ai_analyze(cb: CallbackQuery):
    report_id = cb.data.replace("ai_analyze_", "")
    data = load_report(report_id)
    if not data:
        return await cb.answer("Отчет не найден или устарел", show_alert=True)

    await cb.answer("⚡ Анализирую...")
    summary = analyze_locally(data)
    save_report(report_id, data)
    text = f"🔍 <b>Цель:</b> <code>{html.escape(str(data.get('query') or ''))}</code>\n\n{summary}"
    await cb.message.answer(text[:4000], parse_mode="HTML")


@dp.callback_query(F.data.startswith("get_txt_"))
async def cb_send_txt(cb: CallbackQuery):
    report_id = cb.data.replace("get_txt_", "")
    data = load_report(report_id)
    if not data:
        return await cb.answer("Отчет не найден или устарел", show_alert=True)

    text = (
        f"OASIS DIGITAL\n"
        f"Запрос: {data.get('query')}\n"
        f"Тип: {data.get('query_type')}\n"
        f"Номер: {data.get('phone_number', 'Не найдено')}\n"
        f"ФИО: {data.get('fio', 'Не найдено')}\n"
        f"Дата рождения: {data.get('dob', 'Нет')}\n"
        f"СНИЛС: {data.get('snils', 'Нет')}\n"
        f"Оператор / Регион: {data.get('operator', 'Нет')} / {data.get('region', 'Нет')}"
    )
    file = BufferedInputFile(text.encode("utf-8"), filename=f"oasis_{report_id}.txt")
    await cb.message.answer_document(document=file, caption="📄 <b>TXT Отчет</b>", parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("get_json_"))
async def cb_send_json(cb: CallbackQuery):
    report_id = cb.data.replace("get_json_", "")
    data = load_report(report_id)
    if not data:
        return await cb.answer("Отчет не найден или устарел", show_alert=True)

    export_obj = {
        "report_id": report_id,
        "query": data.get("query"),
        "query_type": data.get("query_type"),
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


# ====================== ОСНОВНОЙ ПОИСК ======================
@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message, state: FSMContext):
    if await state.get_state() is not None or msg.from_user.is_bot:
        return
    if is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы.")

    q = (msg.text or "").strip()
    if not q:
        return

    if not deduct_request(msg.from_user.id):
        return await msg.answer("❌ У вас закончились запросы.")

    note = await msg.answer(
        f"🔍 <b>[Oasis Digital]</b> Идет поиск по Black-Box и Argus (до 90 сек)...\n\n<b>Цель:</b> <code>{html.escape(q)}</code>",
        parse_mode="HTML",
    )

    try:
        data = await search_everywhere(q)
        report_id = uuid.uuid4().hex[:8]
        save_report(report_id, data)

        body = format_report_text(data)
        chunks = [body[i : i + 3900] for i in range(0, len(body), 3900)]
        await note.edit_text(
            chunks[0],
            parse_mode="HTML",
            reply_markup=result_kb(report_id) if len(chunks) == 1 else None,
        )
        for extra in chunks[1:-1]:
            await msg.answer(extra, parse_mode="HTML")
        if len(chunks) > 1:
            await msg.answer(chunks[-1], parse_mode="HTML", reply_markup=result_kb(report_id))
    except Exception as e:
        log.exception("Ошибка при обработке поискового запроса '%s': %s", q, e)
        await note.edit_text(
            f"❌ Ошибка при формировании отчёта: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=main_kb(),
        )


async def main():
    log.info("Бот Oasis Digital запускается на Polling + FastAPI...")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(run_web())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
