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
from aiogram.exceptions import TelegramBadRequest
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
    def __init__(self, maxlen: int = 2000, *args, **kwargs):
        self.maxlen = maxlen
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: str, value: Any):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxlen:
            oldest = next(iter(self))
            del self[oldest]


REPORTS_CACHE = BoundedCache(maxlen=2000)


class SearchStates(StatesGroup):
    wait_support_msg = State()
    wait_promo_input = State()
    wait_admin_raw = State()
    wait_admin_create_promo = State()
    wait_admin_create_check = State()


async def safe_cb_answer(cb: CallbackQuery, text: str | None = None, show_alert: bool = False):
    try:
        await cb.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest:
        pass


# ====================== БАЗА ДАННЫХ (SQLITE) ======================
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(USERS_DB, timeout=20, check_same_thread=False)
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


def _make_json_safe(obj: Any) -> Any:
    if isinstance(obj, set):
        return sorted(str(x) for x in obj)
    if isinstance(obj, dict):
        return {str(k): _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(x) for x in obj]
    return obj


def save_report(report_id: str, data: dict):
    init_db()
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
    clean_id = (report_id or "").strip().split("?")[0].strip("/")
    if not clean_id:
        return None
    if clean_id in REPORTS_CACHE:
        return REPORTS_CACHE[clean_id]
    init_db()
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM reports WHERE report_id = ?",
            (clean_id,),
        ).fetchone()
    if not row:
        return None
    try:
        parsed = json.loads(row["payload_json"])
        REPORTS_CACHE[clean_id] = parsed
        return parsed
    except Exception:
        return None


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
                    text="🌐 Веб-отчет",
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
    if re.fullmatch(
        r"[АВЕКМНОРСТУХABEKMHOPCTYX]{1,2}\d{3}[АВЕКМНОРСТУХABEKMHOPCTYX]{2,3}\d{2,3}",
        re.sub(r"\s+", "", q.upper()),
    ):
        return "vehicle"
    return "general"


def query_type_label(q_type: str) -> str:
    return {
        "phone": "ТЕЛЕФОН",
        "telegram": "TELEGRAM",
        "email": "EMAIL",
        "vin": "VIN",
        "vehicle": "АВТО",
        "ip": "IP-АДРЕС",
        "general": "ПОИСК",
    }.get(q_type, "ПОИСК")


def estimate_tg_reg_by_id(tg_id_str: str) -> str | None:
    if not re.fullmatch(r"\d{6,11}", tg_id_str):
        return None
    uid = int(tg_id_str)
    anchors = [
        (100_000_000, "2015"),
        (300_000_000, "2016"),
        (500_000_000, "2017"),
        (700_000_000, "2018"),
        (1_000_000_000, "2019"),
        (1_500_000_000, "2020"),
        (2_100_000_000, "2021"),
        (5_000_000_000, "2022"),
        (6_000_000_000, "2023"),
        (7_000_000_000, "2024"),
        (7_600_000_000, "2025"),
        (8_200_000_000, "2026"),
    ]
    for threshold, year in anchors:
        if uid <= threshold:
            return f"~{year} (оценка по ID)"
    return "~2026 (оценка по ID)"


def calc_age_from_dob(dob_str: str) -> str | None:
    m = re.search(r"(\d{2})[.\-/](\d{2})[.\-/](\d{4})", dob_str)
    if not m:
        return None
    try:
        bday = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        now = datetime.now()
        age = now.year - bday.year - ((now.month, now.day) < (bday.month, bday.day))
        if 0 < age < 120:
            return str(age)
    except Exception:
        pass
    return None


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


# ====================== ГЛУБОКИЙ ПАРСИНГ ОТВЕТА API ======================
FIO_KEYS = {
    "fio",
    "full_name",
    "fullname",
    "name",
    "primary_fio",
    "owner",
    "holder",
    "first_name",
    "last_name",
    "фио",
    "имя",
    "фамилия",
    "отчество",
    "владелец",
}
DOB_KEYS = {"dob", "birth_date", "birthdate", "birthday", "date_of_birth", "bday", "дата_рождения", "дата рождения"}
AGE_KEYS = {"age", "возраст"}
SNILS_KEYS = {"snils", "снилс"}
PHONE_KEYS = {"phone", "phone_number", "mobile", "msisdn", "number", "tel", "phones", "телефон", "номер"}
OPERATOR_KEYS = {"operator", "carrier", "provider", "оператор"}
REGION_KEYS = {"region", "city", "area", "location", "address", "регион", "город", "адрес"}
REG_DATE_KEYS = {
    "reg_date",
    "registration",
    "registration_date",
    "registered_at",
    "created_at",
    "created",
    "first_seen",
    "date_reg",
    "tg_reg",
    "дата_регистрации",
    "дата регистрации",
    "регистрация",
}
EMAIL_KEYS = {"email", "e_mail", "mail", "emails", "почта"}
META_IGNORE_KEYS = {
    "status",
    "success",
    "ok",
    "code",
    "message",
    "msg",
    "error",
    "_error",
    "query",
    "type",
    "count",
    "total",
    "elapsed",
    "time",
}


def _add_unique(lst: list[str], val: str):
    s = str(val).strip()
    if s and s not in lst:
        lst.append(s)


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


def _parse_text_dump(text: str, final_data: dict):
    """Вытаскивает пары Ключ: Значение из текстовых ответов/дампов API."""
    for raw_line in text.splitlines():
        line = re.sub(r"<[^>]+>", "", raw_line).strip(" •▸-*|\t")
        if ":" not in line:
            continue
        k_raw, v_raw = line.split(":", 1)
        lk = k_raw.strip().lower()
        sval = v_raw.strip()
        if not sval or sval.lower() in ("none", "null", "не найдено", "-"):
            continue

        if any(w in lk for w in ("фио", "fio", "full_name", "имя", "владелец")):
            final_data["fio_set"].add(sval)
        elif any(w in lk for w in ("дата рождения", "birth", "dob", "bday")):
            final_data["dob_set"].add(sval)
        elif any(w in lk for w in ("возраст", "age")):
            final_data["age"] = sval
        elif any(w in lk for w in ("телефон", "phone", "номер", "msisdn")):
            digits = re.sub(r"\D", "", sval)
            if 7 <= len(digits) <= 15:
                final_data["phones_set"].add(sval)
                if final_data["phone_number"] == "Не найдено":
                    final_data["phone_number"] = sval
        elif any(w in lk for w in ("оператор", "operator")):
            if final_data["operator"] == "Нет":
                final_data["operator"] = sval
        elif any(w in lk for w in ("регион", "город", "region", "city", "адрес")):
            if final_data["region"] == "Нет":
                final_data["region"] = sval
        elif any(w in lk for w in ("регистрац", "created", "reg_date", "first_seen")):
            final_data["reg_date"] = sval
        elif lk in ("id", "telegram id", "tg id", "user_id", "tg_id"):
            if not final_data["tg_info"].get("id"):
                final_data["tg_info"]["id"] = sval
        elif any(w in lk for w in ("username", "юзернейм", "ник", "login")):
            clean_u = f"@{sval.lstrip('@')}"
            _add_unique(final_data["usernames_set"], clean_u)
            if not final_data["tg_info"].get("username"):
                final_data["tg_info"]["username"] = clean_u
        elif "@" in sval and "." in sval.split("@")[-1]:
            final_data["emails_set"].add(sval)

        if len(k_raw.strip()) <= 40 and len(sval) <= 200:
            final_data["extra_kv"].append((k_raw.strip(), sval))


def _merge_single_response(resp: Any, final_data: dict, source_label: str):
    if not isinstance(resp, dict) or "_error" in resp:
        return

    for sub_path, obj in _walk_records(resp):
        low_path = sub_path.lower()
        clean_obj_items: dict[str, str] = {}

        first_name_part = ""
        last_name_part = ""

        for k, v in obj.items():
            lk = str(k).lower().strip()
            if lk in META_IGNORE_KEYS:
                continue

            if isinstance(v, str) and ("\n" in v or ":" in v) and lk in {"text", "raw_text", "report", "content", "output", "answer"}:
                _parse_text_dump(v, final_data)
                continue

            if isinstance(v, list):
                for item in v:
                    if item is None or isinstance(item, (dict, list)):
                        continue
                    sitem = str(item).strip()
                    if not sitem or sitem.lower() in ("none", "null", "nan", "не найдено", "unknown", "-"):
                        continue
                    if lk in PHONE_KEYS:
                        digits = re.sub(r"\D", "", sitem)
                        if 7 <= len(digits) <= 15:
                            final_data["phones_set"].add(sitem)
                            if final_data["phone_number"] == "Не найдено":
                                final_data["phone_number"] = sitem
                    elif lk in EMAIL_KEYS and "@" in sitem:
                        final_data["emails_set"].add(sitem)
                    elif any(tok in lk for tok in ("history", "name_history", "changes", "nicknames", "usernames")):
                        _add_unique(final_data["name_history"], sitem)
                    elif any(tok in lk for tok in ("phonebook", "contacts", "tags", "saved_as", "book")):
                        _add_unique(final_data["phonebooks"], sitem)
                    else:
                        final_data["extra_kv"].append((str(k), sitem))
                continue

            if v is None or isinstance(v, dict):
                continue

            sval = str(v).strip()
            if not sval or sval.lower() in ("none", "null", "nan", "не найдено", "unknown", "-"):
                continue

            clean_obj_items[str(k)] = sval

            if lk in {"first_name", "имя"}:
                first_name_part = sval
            elif lk in {"last_name", "фамилия"}:
                last_name_part = sval

            if lk in FIO_KEYS and len(sval) >= 2 and not re.fullmatch(r"\+?[\d\s\-()]+", sval):
                final_data["fio_set"].add(sval)
            elif lk in DOB_KEYS:
                final_data["dob_set"].add(sval)
            elif lk in AGE_KEYS and final_data["age"] == "Не найдено":
                final_data["age"] = sval
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
            elif lk in REG_DATE_KEYS:
                if final_data["reg_date"] == "Не найдено" or "tg" in lk or "reg" in lk:
                    final_data["reg_date"] = sval
            elif lk in EMAIL_KEYS and "@" in sval:
                final_data["emails_set"].add(sval)
            elif lk in {"id", "user_id", "tg_id", "telegram_id", "chat_id"}:
                if not final_data["tg_info"].get("id"):
                    final_data["tg_info"]["id"] = sval
            elif lk in {"username", "tg_username", "login", "nick", "юзернейм"}:
                clean_u = f"@{sval.lstrip('@')}"
                _add_unique(final_data["usernames_set"], clean_u)
                if not final_data["tg_info"].get("username"):
                    final_data["tg_info"]["username"] = clean_u
            elif any(tok in lk for tok in ("phonebook", "contacts", "tags", "saved_as")):
                for pb in sval.split(","):
                    _add_unique(final_data["phonebooks"], pb)
            elif any(tok in lk for tok in ("bio", "about", "status_text", "о себе")):
                final_data["tg_info"]["bio"] = sval
            elif lk in {"dc_id", "dc", "datacenter"}:
                final_data["tg_info"]["dc"] = sval
            elif lk in {"is_premium", "premium"}:
                final_data["tg_info"]["premium"] = sval
            else:
                final_data["extra_kv"].append((str(k), sval))

        if first_name_part or last_name_part:
            combined_name = f"{last_name_part} {first_name_part}".strip()
            if combined_name:
                final_data["fio_set"].add(combined_name)

        if len(clean_obj_items) >= 1 and sub_path:
            bucket = f"{source_label} · {sub_path}"
            final_data["databases"].setdefault(bucket, [])
            if clean_obj_items not in final_data["databases"][bucket]:
                final_data["databases"][bucket].append(clean_obj_items)


async def search_everywhere(q: str) -> dict:
    res_bb, res_argus = await asyncio.gather(
        fetch_blackbox(q, BLACKBOX_KEY),
        fetch_argus(q, ARGUS_KEY),
        return_exceptions=True,
    )

    q_clean = q.strip()
    q_type = detect_query_type(q_clean)

    final_data: dict[str, Any] = {
        "query": q_clean,
        "query_type": q_type,
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "fio_set": set(),
        "dob_set": set(),
        "snils_set": set(),
        "phones_set": set(),
        "emails_set": set(),
        "usernames_set": [],
        "extra_kv": [],
        "fio": "Не найдено",
        "dob": "Нет",
        "age": "Не найдено",
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
        _merge_single_response(res_bb, final_data, source_label="Black-Box")
    if isinstance(res_argus, dict):
        _merge_single_response(res_argus, final_data, source_label="Argus")

    if q_type == "telegram":
        if q_clean.startswith("@") and not final_data["tg_info"].get("username"):
            final_data["tg_info"]["username"] = q_clean
        elif re.fullmatch(r"\d{6,11}", q_clean) and not final_data["tg_info"].get("id"):
            final_data["tg_info"]["id"] = q_clean

    tg_id_cand = str(final_data["tg_info"].get("id") or "")
    if final_data["reg_date"] == "Не найдено" and tg_id_cand:
        est = estimate_tg_reg_by_id(tg_id_cand)
        if est:
            final_data["reg_date"] = est

    final_data["fio"] = ", ".join(sorted(final_data["fio_set"])) if final_data["fio_set"] else "Не найдено"
    final_data["dob"] = ", ".join(sorted(final_data["dob_set"])) if final_data["dob_set"] else "Нет"
    final_data["snils"] = ", ".join(sorted(final_data["snils_set"])) if final_data["snils_set"] else "Нет"

    if final_data["age"] == "Не найдено" and final_data["dob"] != "Нет":
        calc_age = calc_age_from_dob(final_data["dob"])
        if calc_age:
            final_data["age"] = calc_age

    if final_data["phone_number"] == "Не найдено" and final_data["phones_set"]:
        final_data["phone_number"] = sorted(final_data["phones_set"])[0]

    total_records = sum(len(v) for v in final_data["databases"].values() if isinstance(v, list))
    final_data["sources_count"] = len(final_data["databases"])
    if final_data["views_count"] is None:
        final_data["views_count"] = max(total_records, len(final_data["extra_kv"]))

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
        or bool(data.get("extra_kv"))
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
    age = str(data.get("age") or "Не найдено").strip()
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

    extra_kv = data.get("extra_kv") or []
    extra_lines = []
    seen_kv = set()
    for pair in extra_kv:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        k_s, v_s = str(pair[0]).strip(), str(pair[1]).strip()
        sig = f"{k_s.lower()}:{v_s.lower()}"
        if sig in seen_kv:
            continue
        seen_kv.add(sig)
        extra_lines.append(f"▸ {html.escape(k_s)}: <code>{html.escape(v_s)}</code>")
        if len(extra_lines) >= 12:
            break

    views_val = data.get("views_count")
    if views_val is None:
        views_val = sum(len(v) for v in (data.get("databases") or {}).values() if isinstance(v, list))

    if q_type == "phone":
        digits = re.sub(r"\D", "", query)
        shown_phone = phone_val if phone_val != "Не найдено" else (digits or query or "Не найдено")
        loc_parts = [x for x in (operator if operator != "Нет" else "", region if region != "Нет" else "") if x]
        loc_line = " / ".join(loc_parts) if loc_parts else "Не найдено"
        dob_line = f"{dob} (возраст: {age})" if age != "Не найдено" else dob

        blocks = [
            f"📲 <b>Номер</b> · <code>{html.escape(str(shown_phone))}</code>\n"
            f"▸ Оператор / Регион: {html.escape(loc_line)}\n"
            f"▸ ФИО: {html.escape(fio)}\n"
            f"▸ Дата рождения: {html.escape(dob_line)}",
            f"🔎 Телефонные книги: {html.escape(phonebooks_str)}",
            "\n".join(history_lines),
        ]
        if tg_id or (tg_username and not re.fullmatch(r"@\+?\d+", tg_username)):
            blocks.append(
                f"✈️ Telegram: {html.escape(tg_username or '—')} (ID: <code>{html.escape(str(tg_id or '—'))}</code>)\n"
                f"📅 Регистрация TG: {html.escape(reg_date)}"
            )
    elif q_type == "telegram":
        shown_tg = tg_username or query or "Не найдено"
        shown_id = str(tg_id) if tg_id else (query if re.fullmatch(r"\d{6,11}", query) else "Не найдено")
        tg_block = [
            f"✈️ <b>Telegram</b> · {html.escape(str(shown_tg))}",
            f"▸ ID: <code>{html.escape(shown_id)}</code>",
            f"▸ Имя / ФИО: {html.escape(fio)}",
            f"▸ Номер: {html.escape(phone_val)}",
            f"📅 Регистрация: {html.escape(reg_date)}",
        ]
        if tg.get("dc"):
            tg_block.append(f"▸ Дата-центр (DC): {html.escape(str(tg['dc']))}")
        if tg.get("premium"):
            tg_block.append(f"▸ Premium: {html.escape(str(tg['premium']))}")
        if tg.get("bio"):
            tg_block.append(f"▸ О себе: {html.escape(str(tg['bio']))}")
        blocks = [
            "\n".join(tg_block),
            f"🔎 Телефонные книги: {html.escape(phonebooks_str)}",
            "\n".join(history_lines),
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

    if extra_lines:
        blocks.append("📋 <b>Найденные поля:</b>\n" + "\n".join(extra_lines))

    if not _has_report_findings(data):
        blocks.append("⚠️ По открытым источникам и базам совпадений не найдено.")

    blocks.append(f"👁 Найдено записей / совпадений: <b>{html.escape(str(views_val))}</b>")
    blocks.append("Полный веб-отчёт и выгрузки доступны по кнопкам ниже ⬇️")
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
    if reg_date and reg_date != "Не найдено":
        facts.append(f"Регистрация TG: {reg_date}")
    if phone and phone != "Не найдено":
        loc_part = " / ".join(x for x in (operator if operator != "Нет" else "", region if region != "Нет" else "") if x)
        facts.append(f"Телефон: {phone}" + (f" ({loc_part})" if loc_part else ""))
    if fio and fio not in ("Не найдено", "НЕ НАЙДЕНО"):
        facts.append(f"ФИО/Имя: {fio}")
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


# ====================== WEB-ОТЧЁТ В СТИЛЕ СКРИНА (OASIS DIGITAL) ======================
def _build_report_blocks(data: dict) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    phones = list(data.get("phones_set") or [])
    if data.get("phone_number") and data.get("phone_number") != "Не найдено":
        _add_unique(phones, str(data["phone_number"]))
    if phones:
        blocks.append({"title": "ТЕЛЕФОНЫ", "items": [("Телефон", p) for p in phones]})

    fios = list(data.get("fio_set") or [])
    if data.get("fio") and data.get("fio") not in ("Не найдено", "НЕ НАЙДЕНО"):
        for f in str(data["fio"]).split(","):
            _add_unique(fios, f)
    if fios:
        blocks.append({"title": "ФИО", "items": [("ФИО", x) for x in fios]})

    dobs = list(data.get("dob_set") or [])
    if data.get("dob") and data.get("dob") not in ("Нет", "Не найдено"):
        for d in str(data["dob"]).split(","):
            _add_unique(dobs, d)
    if dobs:
        blocks.append({"title": "ДАТА РОЖДЕНИЯ", "items": [("Дата рождения", x) for x in dobs]})

    if data.get("age") and data.get("age") != "Не найдено":
        blocks.append({"title": "ВОЗРАСТ", "items": [("Возраст", str(data["age"]))]})

    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    tg_items: list[tuple[str, str]] = []
    if tg.get("username"):
        tg_items.append(("Username", str(tg["username"])))
    if tg.get("id"):
        tg_items.append(("Telegram ID", str(tg["id"])))
    if data.get("reg_date") and data.get("reg_date") != "Не найдено":
        tg_items.append(("Дата регистрации", str(data["reg_date"])))
    if tg.get("dc"):
        tg_items.append(("Дата-центр (DC)", str(tg["dc"])))
    if tg.get("premium"):
        tg_items.append(("Premium", str(tg["premium"])))
    if tg.get("bio"):
        tg_items.append(("О себе", str(tg["bio"])))
    if tg_items:
        blocks.append({"title": "TELEGRAM", "items": tg_items})

    loc_items: list[tuple[str, str]] = []
    if data.get("operator") and data.get("operator") != "Нет":
        loc_items.append(("Оператор", str(data["operator"])))
    if data.get("region") and data.get("region") != "Нет":
        loc_items.append(("Регион", str(data["region"])))
    if loc_items:
        blocks.append({"title": "ОПЕРАТОР И РЕГИОН", "items": loc_items})

    emails = list(data.get("emails_set") or [])
    if emails:
        blocks.append({"title": "EMAIL", "items": [("Email", e) for e in emails]})

    pbs = list(data.get("phonebooks") or [])
    if pbs:
        blocks.append({"title": "ТЕЛЕФОННЫЕ КНИГИ", "items": [("Запись", p) for p in pbs]})

    hist = list(data.get("name_history") or [])
    if hist:
        blocks.append({"title": "ИСТОРИЯ ИЗМЕНЕНИЙ", "items": [("Изменение", h) for h in hist]})

    extra_kv = data.get("extra_kv") or []
    if extra_kv:
        seen_e = set()
        extra_items: list[tuple[str, str]] = []
        for pair in extra_kv:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            k_s, v_s = str(pair[0]).strip(), str(pair[1]).strip()
            sig = f"{k_s.lower()}:{v_s.lower()}"
            if sig in seen_e:
                continue
            seen_e.add(sig)
            extra_items.append((k_s, v_s))
            if len(extra_items) >= 30:
                break
        if extra_items:
            blocks.append({"title": "ДОПОЛНИТЕЛЬНЫЕ ПОЛЯ", "items": extra_items})

    return blocks


def generate_html_report(data: dict, report_id: str) -> str:
    raw_query = str(data.get("query") or "—")
    query_esc = html.escape(raw_query)
    q_type = data.get("query_type") or detect_query_type(raw_query)
    type_badge = html.escape(query_type_label(q_type))

    blocks = _build_report_blocks(data)
    blocks_count = len(blocks)

    export_dict = {
        "report_id": report_id,
        "query": data.get("query"),
        "query_type": q_type,
        "date": data.get("date"),
        "telegram": {
            "username": (data.get("tg_info") or {}).get("username"),
            "id": (data.get("tg_info") or {}).get("id"),
            "reg_date": data.get("reg_date"),
        },
        "phone_number": data.get("phone_number"),
        "fio": data.get("fio"),
        "dob": data.get("dob"),
        "age": data.get("age"),
        "operator": data.get("operator"),
        "region": data.get("region"),
        "phonebooks": data.get("phonebooks"),
        "name_history": data.get("name_history"),
        "databases": data.get("databases"),
        "raw_response": data.get("raw_response"),
    }
    raw_json_text = json.dumps(export_dict, ensure_ascii=False, indent=2)
    chars_count = len(raw_json_text)
    raw_json_esc = html.escape(raw_json_text)

    cards_html = ""
    for blk in blocks:
        title_esc = html.escape(str(blk["title"]))
        rows = "".join(
            f"<div class='kv-item'><span class='dot'>▸</span>"
            f"<span class='kv-key'>{html.escape(str(k))}:</span> "
            f"<span class='kv-val'>{html.escape(str(v))}</span></div>"
            for k, v in blk["items"]
        )
        cards_html += f"""
        <div class="info-card">
            <div class="card-head"><span class="green-dot"></span> {title_esc}</div>
            <div class="card-body">{rows}</div>
        </div>
        """

    if not cards_html:
        cards_html = """
        <div class="info-card">
            <div class="card-head"><span class="green-dot"></span> РЕЗУЛЬТАТЫ</div>
            <div class="card-body">
                <div class="kv-item"><span class="dot">▸</span><span class="kv-val">По данному запросу записи не найдены — откройте вкладку JSON.</span></div>
            </div>
        </div>
        """

    host_clean = html.escape(WEBHOOK_HOST.replace("https://", "").replace("http://", ""))
    bot_clean = html.escape(BOT_USERNAME)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Oasis Digital — Отчет по {query_esc}</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background-color: #050608;
            color: #e6edf7;
            min-height: 100vh;
            position: relative;
            overflow-x: hidden;
            padding: 30px 16px 72px 16px;
        }}
        canvas#bg-mesh {{
            position: fixed;
            inset: 0;
            width: 100%;
            height: 100%;
            z-index: 0;
            pointer-events: none;
        }}
        .container {{
            position: relative;
            z-index: 2;
            max-width: 780px;
            margin: 0 auto;
        }}
        .top-pill-wrap {{
            display: flex;
            justify-content: center;
            margin-bottom: 16px;
        }}
        .top-pill {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 14px;
            border-radius: 999px;
            background: rgba(16, 185, 129, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.14);
            color: #cbd5e1;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.1em;
            text-transform: uppercase;
            backdrop-filter: blur(8px);
        }}
        .hero {{
            text-align: center;
            margin-bottom: 24px;
        }}
        .hero-title {{
            font-size: 46px;
            font-weight: 800;
            letter-spacing: -0.02em;
            color: #ffffff;
            line-height: 1.08;
            margin-bottom: 12px;
            text-shadow: 0 4px 28px rgba(0, 0, 0, 0.75);
        }}
        .hero-sub {{
            font-size: 14px;
            color: #94a3b8;
            margin-bottom: 14px;
        }}
        .hero-sub code {{
            color: #f8fafc;
            background: rgba(15, 23, 42, 0.75);
            border: 1px solid rgba(148, 163, 184, 0.2);
            padding: 4px 10px;
            border-radius: 7px;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        }}
        .badges-row {{
            display: flex;
            justify-content: center;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 20px;
        }}
        .meta-badge {{
            display: inline-flex;
            align-items: center;
            gap: 7px;
            padding: 6px 12px;
            border-radius: 8px;
            background: rgba(13, 16, 24, 0.86);
            border: 1px solid rgba(148, 163, 184, 0.18);
            color: #cbd5e1;
            font-size: 12px;
            font-weight: 500;
            backdrop-filter: blur(8px);
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
            margin-bottom: 18px;
        }}
        .stat-box {{
            background: rgba(13, 16, 24, 0.88);
            border: 1px solid rgba(148, 163, 184, 0.16);
            border-radius: 12px;
            padding: 15px 18px;
            text-align: left;
            backdrop-filter: blur(10px);
        }}
        .stat-label {{
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.08em;
            color: #64748b;
            text-transform: uppercase;
            margin-bottom: 6px;
        }}
        .stat-val {{
            font-size: 21px;
            font-weight: 700;
            color: #ffffff;
        }}
        .tabs-bar {{
            display: flex;
            justify-content: flex-end;
            gap: 8px;
            margin-bottom: 12px;
        }}
        .tab-btn {{
            padding: 7px 13px;
            border-radius: 8px;
            border: 1px solid rgba(148, 163, 184, 0.18);
            background: rgba(13, 16, 24, 0.85);
            color: #94a3b8;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }}
        .tab-btn.active {{
            background: rgba(56, 189, 248, 0.14);
            border-color: rgba(56, 189, 248, 0.45);
            color: #ffffff;
        }}
        .tab-panel {{ display: none; }}
        .tab-panel.active {{ display: block; }}
        .info-card {{
            background: rgba(13, 16, 24, 0.9);
            border: 1px solid rgba(148, 163, 184, 0.16);
            border-radius: 14px;
            padding: 16px 18px;
            margin-bottom: 12px;
            backdrop-filter: blur(10px);
        }}
        .card-head {{
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.08em;
            color: #cbd5e1;
            text-transform: uppercase;
            margin-bottom: 12px;
        }}
        .green-dot {{
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: #22c55e;
            box-shadow: 0 0 8px #22c55e;
            display: inline-block;
        }}
        .kv-item {{
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 6px;
            padding: 9px 12px;
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.025);
            border: 1px solid rgba(255, 255, 255, 0.03);
            margin-bottom: 6px;
            font-size: 13px;
            line-height: 1.45;
            word-break: break-word;
        }}
        .kv-item:last-child {{ margin-bottom: 0; }}
        .dot {{ color: #64748b; font-weight: 700; }}
        .kv-key {{ color: #94a3b8; }}
        .kv-val {{
            color: #7dd3fc;
            background: rgba(56, 189, 248, 0.1);
            padding: 2px 7px;
            border-radius: 5px;
            font-weight: 500;
        }}
        .json-pre {{
            background: rgba(13, 16, 24, 0.94);
            border: 1px solid rgba(148, 163, 184, 0.16);
            border-radius: 14px;
            padding: 16px;
            font-size: 12px;
            line-height: 1.5;
            color: #cbd5e1;
            overflow-x: auto;
            white-space: pre-wrap;
            word-break: break-word;
        }}
        @media (max-width: 620px) {{
            .hero-title {{ font-size: 34px; }}
            .stats-grid {{ grid-template-columns: 1fr; gap: 8px; }}
        }}
    </style>
</head>
<body>
    <canvas id="bg-mesh"></canvas>

    <div class="container">
        <div class="top-pill-wrap">
            <div class="top-pill"><span class="green-dot"></span> OASIS DIGITAL SEARCH ENGINE</div>
        </div>

        <div class="hero">
            <h1 class="hero-title">Oasis Digital</h1>
            <div class="hero-sub">Отчёт по запросу <code>{query_esc}</code></div>
            <div class="badges-row">
                <span class="meta-badge">🌐 {host_clean}</span>
                <span class="meta-badge">🤖 {bot_clean}</span>
            </div>
        </div>

        <div class="stats-grid">
            <div class="stat-box">
                <div class="stat-label">ТИП</div>
                <div class="stat-val">{type_badge}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">БЛОКОВ</div>
                <div class="stat-val">{blocks_count}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">СИМВОЛОВ</div>
                <div class="stat-val">{chars_count}</div>
            </div>
        </div>

        <div class="tabs-bar">
            <button class="tab-btn active" onclick="switchTab('cards', this)">Карточки</button>
            <button class="tab-btn" onclick="switchTab('json', this)">JSON</button>
        </div>

        <div id="panel-cards" class="tab-panel active">
            {cards_html}
        </div>
        <div id="panel-json" class="tab-panel">
            <pre class="json-pre">{raw_json_esc}</pre>
        </div>
    </div>

    <script>
        function switchTab(name, btn) {{
            document.querySelectorAll('.tab-panel').forEach(function (el) {{ el.classList.remove('active'); }});
            document.querySelectorAll('.tab-btn').forEach(function (el) {{ el.classList.remove('active'); }});
            document.getElementById('panel-' + name).classList.add('active');
            btn.classList.add('active');
        }}

        (function drawGlobeAndStars() {{
            var canvas = document.getElementById('bg-mesh');
            if (!canvas) return;
            var ctx = canvas.getContext('2d');
            var w = canvas.width = window.innerWidth;
            var h = canvas.height = window.innerHeight;

            var stars = [];
            for (var i = 0; i < 140; i++) {{
                stars.push({{
                    x: Math.random() * w,
                    y: Math.random() * h,
                    r: Math.random() * 1.3 + 0.3,
                    a: Math.random() * 0.6 + 0.2
                }});
            }}

            var pts = [];
            var latSteps = 10;
            var lonSteps = 18;
            for (var lat = 0; lat <= latSteps; lat++) {{
                var theta = (lat / latSteps) * Math.PI;
                for (var lon = 0; lon < lonSteps; lon++) {{
                    var phi = (lon / lonSteps) * Math.PI * 2;
                    pts.push({{
                        x: Math.sin(theta) * Math.cos(phi),
                        y: Math.cos(theta),
                        z: Math.sin(theta) * Math.sin(phi),
                        lat: lat,
                        lon: lon
                    }});
                }}
            }}

            var angle = 0;
            function render() {{
                ctx.clearRect(0, 0, w, h);

                var cx = w * 0.5;
                var cy = Math.min(h * 0.31, 270);
                var radius = Math.min(w * 0.34, 240);

                var grad = ctx.createRadialGradient(cx, cy + radius * 0.45, 10, cx, cy + radius * 0.3, radius * 1.25);
                grad.addColorStop(0, 'rgba(217, 179, 108, 0.16)');
                grad.addColorStop(0.55, 'rgba(56, 189, 248, 0.05)');
                grad.addColorStop(1, 'rgba(5, 6, 8, 0)');
                ctx.fillStyle = grad;
                ctx.fillRect(0, 0, w, h);

                for (var s = 0; s < stars.length; s++) {{
                    var st = stars[s];
                    ctx.fillStyle = 'rgba(226, 232, 240,' + st.a + ')';
                    ctx.beginPath();
                    ctx.arc(st.x, st.y, st.r, 0, Math.PI * 2);
                    ctx.fill();
                }}

                angle += 0.0022;
                var cosA = Math.cos(angle);
                var sinA = Math.sin(angle);
                var tilt = 0.32;
                var cosT = Math.cos(tilt);
                var sinT = Math.sin(tilt);

                var proj = pts.map(function (p) {{
                    var x1 = p.x * cosA - p.z * sinA;
                    var z1 = p.x * sinA + p.z * cosA;
                    var y2 = p.y * cosT - z1 * sinT;
                    var z2 = p.y * sinT + z1 * cosT;
                    return {{
                        sx: cx + x1 * radius,
                        sy: cy + y2 * radius,
                        z: z2
                    }};
                }});

                ctx.lineWidth = 0.7;
                for (var i = 0; i < proj.length; i++) {{
                    var p1 = proj[i];
                    if (p1.z < -0.25) continue;
                    var alpha = Math.max(0.05, (p1.z + 1) * 0.14);
                    ctx.strokeStyle = 'rgba(148, 163, 184,' + alpha.toFixed(3) + ')';

                    var nextLon = (i % lonSteps === lonSteps - 1) ? i - (lonSteps - 1) : i + 1;
                    var p2 = proj[nextLon];
                    if (p2) {{
                        ctx.beginPath();
                        ctx.moveTo(p1.sx, p1.sy);
                        ctx.lineTo(p2.sx, p2.sy);
                        ctx.stroke();
                    }}
                    if (i + lonSteps < proj.length) {{
                        var p3 = proj[i + lonSteps];
                        ctx.beginPath();
                        ctx.moveTo(p1.sx, p1.sy);
                        ctx.lineTo(p3.sx, p3.sy);
                        ctx.stroke();
                    }}
                }}

                requestAnimationFrame(render);
            }}

            window.addEventListener('resize', function () {{
                w = canvas.width = window.innerWidth;
                h = canvas.height = window.innerHeight;
            }});
            render();
        }})();
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
    await safe_cb_answer(cb)


@dp.callback_query(F.data == "search_info")
async def cb_search_info(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(START_TEXT, parse_mode="HTML", reply_markup=back_kb())
    await safe_cb_answer(cb)


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
    await safe_cb_answer(cb)


@dp.callback_query(F.data == "hide_data_menu")
async def cb_hide_data_menu(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Оплатить Tonkeeper", callback_data="pay_tonkeeper")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="profile")],
        ]
    )
    await cb.message.edit_text(HIDE_DATA_TEXT, parse_mode="HTML", reply_markup=kb)
    await safe_cb_answer(cb)


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
    await safe_cb_answer(cb)


@dp.callback_query(F.data == "buy_requests")
async def cb_buy_requests(cb: CallbackQuery):
    await cb.message.edit_text(
        "💎 <b>Покупка запросов</b>\nОбратитесь к администратору: " + ADMIN_USERNAME,
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await safe_cb_answer(cb)


@dp.callback_query(F.data == "ref_link")
async def cb_ref_link(cb: CallbackQuery):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={cb.from_user.id}"
    await cb.message.edit_text(
        f"🔗 <b>Реферальная ссылка:</b>\n\n<code>{ref_link}</code>",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await safe_cb_answer(cb)


@dp.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.wait_support_msg)
    await cb.message.edit_text(SUPPORT_TEXT, parse_mode="HTML", reply_markup=back_kb())
    await safe_cb_answer(cb)


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
    await safe_cb_answer(cb)


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
        return await safe_cb_answer(cb, "Нет доступа", show_alert=True)
    await cb.message.edit_text(
        "🎟 <b>Создание промокода</b>\n\nФормат: <code>КОД НАГРАДА ЛИМИТ</code>",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await state.set_state(SearchStates.wait_admin_create_promo)
    await safe_cb_answer(cb)


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
        return await safe_cb_answer(cb, "Нет доступа", show_alert=True)
    await cb.message.edit_text(
        "💳 <b>Создание чека</b>\n\nВведите номинал (запросов):",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await state.set_state(SearchStates.wait_admin_create_check)
    await safe_cb_answer(cb)


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
        return await safe_cb_answer(cb, "Нет доступа", show_alert=True)
    await cb.message.edit_text(
        "📡 <b>Тест API (Raw)</b>\n\nВведите запрос (таймаут 90 сек):",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await state.set_state(SearchStates.wait_admin_raw)
    await safe_cb_answer(cb)


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
        return await safe_cb_answer(cb, "Нет доступа", show_alert=True)
    note = await cb.message.edit_text("🔌 Проверяем Black-Box и Argus...", parse_mode="HTML")
    results = await check_all_apis_health()
    lines = [
        f"• <b>{r['name']}</b>: {'✅ OK' if r['healthy'] else '❌ FAIL'} ({r['status']}, {r['latency_ms']}ms)"
        for r in results
    ]
    await note.edit_text("🔌 <b>Статус API:</b>\n\n" + "\n".join(lines), parse_mode="HTML", reply_markup=admin_menu_kb())
    await safe_cb_answer(cb)


# ====================== АНАЛИЗАТОР И ЭКСПОРТЫ ======================
@dp.callback_query(F.data.startswith("ai_analyze_"))
async def cb_ai_analyze(cb: CallbackQuery):
    report_id = cb.data.replace("ai_analyze_", "", 1)
    data = load_report(report_id)
    if not data:
        return await safe_cb_answer(cb, "Отчет не найден", show_alert=True)

    await safe_cb_answer(cb, "⚡ Анализирую...")
    summary = analyze_locally(data)
    save_report(report_id, data)
    text = f"🔍 <b>Цель:</b> <code>{html.escape(str(data.get('query') or ''))}</code>\n\n{summary}"
    await cb.message.answer(text[:4000], parse_mode="HTML")


@dp.callback_query(F.data.startswith("get_txt_"))
async def cb_send_txt(cb: CallbackQuery):
    report_id = cb.data.replace("get_txt_", "", 1)
    data = load_report(report_id)
    if not data:
        return await safe_cb_answer(cb, "Отчет не найден", show_alert=True)

    text = (
        f"OASIS DIGITAL\n"
        f"Запрос: {data.get('query')}\n"
        f"Тип: {data.get('query_type')}\n"
        f"Номер: {data.get('phone_number', 'Не найдено')}\n"
        f"ФИО: {data.get('fio', 'Не найдено')}\n"
        f"Дата рождения: {data.get('dob', 'Нет')}\n"
        f"Возраст: {data.get('age', 'Не найдено')}\n"
        f"Регистрация: {data.get('reg_date', 'Не найдено')}\n"
        f"СНИЛС: {data.get('snils', 'Нет')}\n"
        f"Оператор / Регион: {data.get('operator', 'Нет')} / {data.get('region', 'Нет')}"
    )
    file = BufferedInputFile(text.encode("utf-8"), filename=f"oasis_{report_id}.txt")
    await cb.message.answer_document(document=file, caption="📄 <b>TXT Отчет</b>", parse_mode="HTML")
    await safe_cb_answer(cb)


@dp.callback_query(F.data.startswith("get_json_"))
async def cb_send_json(cb: CallbackQuery):
    report_id = cb.data.replace("get_json_", "", 1)
    data = load_report(report_id)
    if not data:
        return await safe_cb_answer(cb, "Отчет не найден", show_alert=True)

    export_obj = {
        "report_id": report_id,
        "query": data.get("query"),
        "query_type": data.get("query_type"),
        "date": data.get("date"),
        "phone_number": data.get("phone_number"),
        "fio": data.get("fio"),
        "dob": data.get("dob"),
        "age": data.get("age"),
        "reg_date": data.get("reg_date"),
        "phonebooks": data.get("phonebooks"),
        "name_history": data.get("name_history"),
        "tg_info": data.get("tg_info"),
        "databases": data.get("databases"),
        "raw_response": data.get("raw_response"),
    }
    json_str = json.dumps(export_obj, ensure_ascii=False, indent=2)
    file = BufferedInputFile(json_str.encode("utf-8"), filename=f"oasis_{report_id}.json")
    await cb.message.answer_document(document=file, caption="📦 <b>JSON Отчет</b>", parse_mode="HTML")
    await safe_cb_answer(cb)


# ====================== ОСНОВНОЙ ПОИСК ======================
@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message, state: FSMContext):
    if msg.from_user.is_bot:
        return
    if await state.get_state() is not None:
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
    init_db()
    log.info("Бот Oasis Digital запускается на Polling + FastAPI...")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(run_web())
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
