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

# --- Конфиг ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9024954594:AAGj3lihxCTSGgCMaeE_mCi2519paMgIYms")
BOT_USERNAME = "@MobiusSearchBot"

MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "8394631079"))
ADMIN_USERNAME = "@anodoxx"

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1004311841781"))
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/+BqWOF7S3AqQ1YzI6")

BLACKBOX_KEY = os.getenv("BLACKBOX_KEY", "8jdsYcPZiQMTpYNCluXg9scNhc7y")
ARGUS_KEY = os.getenv("ARGUS_KEY", "ARGUS-IO-M1b_3fSDQM5qp5TU5bDMOLAITOluLLtKGrtLSHD2GGE")

TON_WALLET = os.getenv("TON_WALLET", "UQBlTLz8MaKDtNhYo1qeDsOXtVjN1upRp2uwsf0NWWE9p5cx")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://listopadov-search-1.onrender.com").rstrip("/")
DB_PATH = os.getenv("USERS_DB", "users.db")

URL_BB = "https://api.black-box.cc/v1.1/search"
URL_ARGUS = "https://argus.cc.cd/api/v1/search"

PROXY = os.getenv("PROXY_URL", None)
TIMEOUT_SEC = 90
RETRIES = 3

HEADERS = {
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


class CacheLRU(OrderedDict):
    def __init__(self, limit: int = 2000, *args, **kwargs):
        self.limit = limit
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: str, value: Any):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.limit:
            oldest = next(iter(self))
            del self[oldest]


cache = CacheLRU(limit=2000)


class States(StatesGroup):
    support_msg = State()
    promo_code = State()
    adm_raw = State()
    adm_promo = State()
    adm_check = State()


async def ack_cb(cb: CallbackQuery, text: str | None = None, alert: bool = False):
    try:
        await cb.answer(text=text, show_alert=alert)
    except TelegramBadRequest:
        pass


# --- БД ---
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
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
        cur.execute(
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_activations (
                user_id INTEGER,
                code TEXT,
                PRIMARY KEY (user_id, code)
            )
            """
        )
        cur.execute(
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


db_init()


def to_json_safe(obj: Any) -> Any:
    if isinstance(obj, set):
        return sorted(str(x) for x in obj)
    if isinstance(obj, dict):
        return {str(k): to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json_safe(x) for x in obj]
    return obj


def store_report(report_id: str, payload: dict):
    db_init()
    safe = to_json_safe(payload)
    cache[report_id] = safe
    with db_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO reports (report_id, query, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                report_id,
                str(safe.get("query") or ""),
                json.dumps(safe, ensure_ascii=False),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()


def read_report(report_id: str) -> dict | None:
    rid = (report_id or "").strip().split("?")[0].strip("/")
    if not rid:
        return None
    if rid in cache:
        return cache[rid]
    db_init()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT payload_json FROM reports WHERE report_id = ?",
            (rid,),
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload_json"])
        cache[rid] = data
        return data
    except Exception:
        return None


def fetch_user(user_id: int):
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return tuple(row) if row else None


def create_user(user_id: int, username: str | None, ref_id: int | None = None) -> bool:
    with db_conn() as conn:
        if conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone():
            return False
        conn.execute(
            "INSERT INTO users (user_id, username, requests_left, referrer_id, joined_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, 3, ref_id, datetime.now().strftime("%d.%m.%Y")),
        )
        conn.commit()
    if ref_id and ref_id != user_id:
        give_requests(ref_id, 1)
    return True


def give_requests(user_id: int, amount: int):
    with db_conn() as conn:
        conn.execute(
            "UPDATE users SET requests_left = requests_left + ? WHERE user_id = ?",
            (amount, user_id),
        )
        conn.commit()


def spend_request(user_id: int) -> bool:
    if user_id == MAIN_ADMIN_ID:
        return True
    with db_conn() as conn:
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


def check_banned(user_id: int) -> bool:
    with db_conn() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row["is_banned"] == 1)


def get_tenure(joined_str: str) -> str:
    try:
        dt = datetime.strptime(joined_str, "%d.%m.%Y")
        days = max((datetime.now() - dt).days, 0)
        months = days // 30
        rem = days % 30
        return f"{months} мес. {rem} дн." if months > 0 else f"{rem} дн."
    except Exception:
        return "0 дн."


# --- Тексты и кнопки ---
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

SUPPORT_TEXT = "<b>Связь с нами</b>\nНапишите ваше сообщение в чат."
HIDE_DATA_TEXT = "<b>Скрытие данных</b>\nСтоимость скрытия данных: $7 (навсегда)"


def kb_main():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Поиск", callback_data="search_info")],
            [InlineKeyboardButton(text="Купить запросы", callback_data="buy_requests")],
            [InlineKeyboardButton(text="Профиль", callback_data="profile")],
            [InlineKeyboardButton(text="Активировать промокод", callback_data="use_promo")],
            [
                InlineKeyboardButton(text="Рефералка", callback_data="ref_link"),
                InlineKeyboardButton(text="Связь с нами", callback_data="support"),
            ],
        ]
    )


def kb_back():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="back_to_start")]]
    )


def kb_result(report_id: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Анализатор", callback_data=f"ai_analyze_{report_id}")],
            [InlineKeyboardButton(text="Веб-отчет", url=f"{WEBHOOK_HOST}/report/{report_id}")],
            [
                InlineKeyboardButton(text="TXT", callback_data=f"get_txt_{report_id}"),
                InlineKeyboardButton(text="JSON", callback_data=f"get_json_{report_id}"),
            ],
            [InlineKeyboardButton(text="В меню", callback_data="back_to_start")],
        ]
    )


def kb_admin():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Тест API (Raw)", callback_data="adm_test_api")],
            [InlineKeyboardButton(text="Проверка API", callback_data="adm_check_apis")],
            [
                InlineKeyboardButton(text="Создать промокод", callback_data="adm_promo"),
                InlineKeyboardButton(text="Создать чек", callback_data="adm_check"),
            ],
            [InlineKeyboardButton(text="В меню", callback_data="back_to_start")],
        ]
    )


# --- Типы запросов и вспомогательные функции ---
def detect_qtype(query: str) -> str:
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


def qtype_title(qtype: str) -> str:
    return {
        "phone": "ТЕЛЕФОН",
        "telegram": "TELEGRAM",
        "email": "EMAIL",
        "vin": "VIN",
        "vehicle": "АВТО",
        "ip": "IP-АДРЕС",
        "general": "ПОИСК",
    }.get(qtype, "ПОИСК")


def approx_tg_reg(tg_id_str: str) -> str | None:
    if not re.fullmatch(r"\d{6,11}", tg_id_str):
        return None
    uid = int(tg_id_str)
    bounds = [
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
    for limit_val, year in bounds:
        if uid <= limit_val:
            return f"~{year} (по ID)"
    return "~2026 (по ID)"


def calc_age(dob_str: str) -> str | None:
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


# --- Внешние API ---
async def req_json(
    url: str,
    api_key: str,
    tag: str,
    params: dict,
    debug: bool = False,
):
    hdrs = {
        **HEADERS,
        "X-API-Key": api_key,
        "Authorization": f"Bearer {api_key}",
    }
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
    ssl_ctx = ssl.create_default_context()

    err_msg = "No response"
    for attempt in range(1, RETRIES + 1):
        conn = aiohttp.TCPConnector(ssl=ssl_ctx)
        try:
            async with aiohttp.ClientSession(timeout=timeout, connector=conn) as sess:
                async with sess.get(url, headers=hdrs, params=params, proxy=PROXY) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        try:
                            return json.loads(body)
                        except Exception:
                            return {"raw_text": body}
                    err_msg = f"HTTP {resp.status}: {body[:300]}"
                    log.warning("[%s] %s/%s -> %s", tag, attempt, RETRIES, err_msg)
                    if resp.status in (502, 503, 504) and attempt < RETRIES:
                        await asyncio.sleep(3)
                        continue
                    break
        except Exception as exc:
            err_msg = f"Exception: {exc}"
            log.error("[%s] %s/%s err: %s", tag, attempt, RETRIES, exc)
            if attempt < RETRIES:
                await asyncio.sleep(3)
        finally:
            if not conn.closed:
                await conn.close()

    return {"_error": err_msg} if debug else None


async def query_blackbox(q: str, api_key: str = BLACKBOX_KEY, debug: bool = False):
    qt = detect_qtype(q)
    bb_type = "phone" if qt == "phone" else ("email" if qt == "email" else "auto")
    return await req_json(
        URL_BB,
        api_key=api_key,
        tag="BLACKBOX",
        params={"q": q.strip(), "type": bb_type},
        debug=debug,
    )


async def query_argus(q: str, api_key: str = ARGUS_KEY, debug: bool = False):
    return await req_json(
        URL_ARGUS,
        api_key=api_key,
        tag="ARGUS",
        params={"q": q.strip(), "key": api_key},
        debug=debug,
    )


async def ping_api(name: str, url: str, api_key: str, params: dict):
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
    hdrs = {**HEADERS, "X-API-Key": api_key}
    t0 = asyncio.get_event_loop().time()
    conn = aiohttp.TCPConnector(ssl=ssl.create_default_context())
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=conn) as sess:
            async with sess.get(url, headers=hdrs, params=params, proxy=PROXY) as resp:
                ms = int((asyncio.get_event_loop().time() - t0) * 1000)
                return {"name": name, "ok": 200 <= resp.status < 300, "status": resp.status, "ms": ms}
    except Exception as exc:
        return {"name": name, "ok": False, "status": f"ERR ({type(exc).__name__})", "ms": 0}
    finally:
        if not conn.closed:
            await conn.close()


async def check_apis():
    return await asyncio.gather(
        ping_api("Black-Box", URL_BB, BLACKBOX_KEY, {"q": "79825207224", "type": "phone"}),
        ping_api("Argus", URL_ARGUS, ARGUS_KEY, {"q": "79991234567", "key": ARGUS_KEY}),
    )


# --- Парсинг ответов ---
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
REG_KEYS = {
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
SKIP_KEYS = {
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


def push_unique(target: list[str], val: str):
    s = str(val).strip()
    if s and s not in target:
        target.append(s)


def walk_tree(node: Any, path: str = ""):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            nxt = f"{path}.{k}" if path else str(k)
            yield from walk_tree(v, nxt)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            nxt = f"{path}[{idx}]"
            yield from walk_tree(item, nxt)


def parse_text_block(text: str, out: dict):
    for raw in text.splitlines():
        clean = re.sub(r"<[^>]+>", "", raw).strip(" -*|\t")
        if ":" not in clean:
            continue
        k_part, v_part = clean.split(":", 1)
        lk = k_part.strip().lower()
        val = v_part.strip()
        if not val or val.lower() in ("none", "null", "не найдено", "-"):
            continue

        if any(w in lk for w in ("фио", "fio", "full_name", "имя", "владелец")):
            out["fio_set"].add(val)
        elif any(w in lk for w in ("дата рождения", "birth", "dob", "bday")):
            out["dob_set"].add(val)
        elif any(w in lk for w in ("возраст", "age")):
            out["age"] = val
        elif any(w in lk for w in ("телефон", "phone", "номер", "msisdn")):
            digits = re.sub(r"\D", "", val)
            if 7 <= len(digits) <= 15:
                out["phones_set"].add(val)
                if out["phone_number"] == "Не найдено":
                    out["phone_number"] = val
        elif any(w in lk for w in ("оператор", "operator")):
            if out["operator"] == "Нет":
                out["operator"] = val
        elif any(w in lk for w in ("регион", "город", "region", "city", "адрес")):
            if out["region"] == "Нет":
                out["region"] = val
        elif any(w in lk for w in ("регистрац", "created", "reg_date", "first_seen")):
            out["reg_date"] = val
        elif lk in ("id", "telegram id", "tg id", "user_id", "tg_id"):
            if not out["tg_info"].get("id"):
                out["tg_info"]["id"] = val
        elif any(w in lk for w in ("username", "юзернейм", "ник", "login")):
            uname = f"@{val.lstrip('@')}"
            push_unique(out["usernames_set"], uname)
            if not out["tg_info"].get("username"):
                out["tg_info"]["username"] = uname
        elif "@" in val and "." in val.split("@")[-1]:
            out["emails_set"].add(val)

        if len(k_part.strip()) <= 40 and len(val) <= 200:
            out["extra_kv"].append((k_part.strip(), val))


def absorb_response(resp: Any, out: dict, src: str):
    if not isinstance(resp, dict) or "_error" in resp:
        return

    for path, obj in walk_tree(resp):
        items: dict[str, str] = {}
        fname = ""
        lname = ""

        for k, v in obj.items():
            lk = str(k).lower().strip()
            if lk in SKIP_KEYS:
                continue

            if isinstance(v, str) and ("\n" in v or ":" in v) and lk in {
                "text",
                "raw_text",
                "report",
                "content",
                "output",
                "answer",
            }:
                parse_text_block(v, out)
                continue

            if isinstance(v, list):
                for el in v:
                    if el is None or isinstance(el, (dict, list)):
                        continue
                    sel = str(el).strip()
                    if not sel or sel.lower() in ("none", "null", "nan", "не найдено", "unknown", "-"):
                        continue
                    if lk in PHONE_KEYS:
                        digits = re.sub(r"\D", "", sel)
                        if 7 <= len(digits) <= 15:
                            out["phones_set"].add(sel)
                            if out["phone_number"] == "Не найдено":
                                out["phone_number"] = sel
                    elif lk in EMAIL_KEYS and "@" in sel:
                        out["emails_set"].add(sel)
                    elif any(tok in lk for tok in ("history", "name_history", "changes", "nicknames", "usernames")):
                        push_unique(out["name_history"], sel)
                    elif any(tok in lk for tok in ("phonebook", "contacts", "tags", "saved_as", "book")):
                        push_unique(out["phonebooks"], sel)
                    else:
                        out["extra_kv"].append((str(k), sel))
                continue

            if v is None or isinstance(v, dict):
                continue

            sval = str(v).strip()
            if not sval or sval.lower() in ("none", "null", "nan", "не найдено", "unknown", "-"):
                continue

            items[str(k)] = sval

            if lk in {"first_name", "имя"}:
                fname = sval
            elif lk in {"last_name", "фамилия"}:
                lname = sval

            if lk in FIO_KEYS and len(sval) >= 2 and not re.fullmatch(r"\+?[\d\s\-()]+", sval):
                out["fio_set"].add(sval)
            elif lk in DOB_KEYS:
                out["dob_set"].add(sval)
            elif lk in AGE_KEYS and out["age"] == "Не найдено":
                out["age"] = sval
            elif lk in SNILS_KEYS:
                out["snils_set"].add(sval)
            elif lk in PHONE_KEYS:
                digits = re.sub(r"\D", "", sval)
                if 7 <= len(digits) <= 15:
                    out["phones_set"].add(sval)
                    if out["phone_number"] == "Не найдено":
                        out["phone_number"] = sval
            elif lk in OPERATOR_KEYS and out["operator"] == "Нет":
                out["operator"] = sval
            elif lk in REGION_KEYS and out["region"] == "Нет":
                out["region"] = sval
            elif lk in REG_KEYS:
                if out["reg_date"] == "Не найдено" or "tg" in lk or "reg" in lk:
                    out["reg_date"] = sval
            elif lk in EMAIL_KEYS and "@" in sval:
                out["emails_set"].add(sval)
            elif lk in {"id", "user_id", "tg_id", "telegram_id", "chat_id"}:
                if not out["tg_info"].get("id"):
                    out["tg_info"]["id"] = sval
            elif lk in {"username", "tg_username", "login", "nick", "юзернейм"}:
                uname = f"@{sval.lstrip('@')}"
                push_unique(out["usernames_set"], uname)
                if not out["tg_info"].get("username"):
                    out["tg_info"]["username"] = uname
            elif any(tok in lk for tok in ("phonebook", "contacts", "tags", "saved_as")):
                for pb in sval.split(","):
                    push_unique(out["phonebooks"], pb)
            elif any(tok in lk for tok in ("bio", "about", "status_text", "о себе")):
                out["tg_info"]["bio"] = sval
            elif lk in {"dc_id", "dc", "datacenter"}:
                out["tg_info"]["dc"] = sval
            elif lk in {"is_premium", "premium"}:
                out["tg_info"]["premium"] = sval
            else:
                out["extra_kv"].append((str(k), sval))

        if fname or lname:
            full = f"{lname} {fname}".strip()
            if full:
                out["fio_set"].add(full)

        if items and path:
            bucket = f"{src} · {path}"
            out["databases"].setdefault(bucket, [])
            if items not in out["databases"][bucket]:
                out["databases"][bucket].append(items)


async def run_search(q: str) -> dict:
    res_bb, res_argus = await asyncio.gather(
        query_blackbox(q, BLACKBOX_KEY),
        query_argus(q, ARGUS_KEY),
        return_exceptions=True,
    )

    q_clean = q.strip()
    q_type = detect_qtype(q_clean)

    data: dict[str, Any] = {
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
        absorb_response(res_bb, data, "Black-Box")
    if isinstance(res_argus, dict):
        absorb_response(res_argus, data, "Argus")

    if q_type == "telegram":
        if q_clean.startswith("@") and not data["tg_info"].get("username"):
            data["tg_info"]["username"] = q_clean
        elif re.fullmatch(r"\d{6,11}", q_clean) and not data["tg_info"].get("id"):
            data["tg_info"]["id"] = q_clean

    tg_id_val = str(data["tg_info"].get("id") or "")
    if data["reg_date"] == "Не найдено" and tg_id_val:
        est = approx_tg_reg(tg_id_val)
        if est:
            data["reg_date"] = est

    data["fio"] = ", ".join(sorted(data["fio_set"])) if data["fio_set"] else "Не найдено"
    data["dob"] = ", ".join(sorted(data["dob_set"])) if data["dob_set"] else "Нет"
    data["snils"] = ", ".join(sorted(data["snils_set"])) if data["snils_set"] else "Нет"

    if data["age"] == "Не найдено" and data["dob"] != "Нет":
        age_calc = calc_age(data["dob"])
        if age_calc:
            data["age"] = age_calc

    if data["phone_number"] == "Не найдено" and data["phones_set"]:
        data["phone_number"] = sorted(data["phones_set"])[0]

    total_recs = sum(len(v) for v in data["databases"].values() if isinstance(v, list))
    data["sources_count"] = len(data["databases"])
    if data["views_count"] is None:
        data["views_count"] = max(total_recs, len(data["extra_kv"]))

    return to_json_safe(data)


# --- Формирование текста отчета ---
def has_findings(data: dict) -> bool:
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


def build_text_report(data: dict) -> str:
    query = str(data.get("query") or "").strip()
    qtype = data.get("query_type") or detect_qtype(query)

    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    tg_id = tg.get("id") or tg.get("user_id")
    raw_uname = tg.get("username") or (query if query.startswith("@") else "")
    tg_uname = f"@{str(raw_uname).lstrip('@')}" if raw_uname else ""

    fio = str(data.get("fio") or "Не найдено").strip()
    dob = str(data.get("dob") or "Нет").strip()
    age = str(data.get("age") or "Не найдено").strip()
    phone = str(data.get("phone_number") or "Не найдено").strip()
    operator = str(data.get("operator") or "Нет").strip()
    region = str(data.get("region") or "Нет").strip()
    reg_date = str(data.get("reg_date") or "Не найдено").strip()

    pbooks = data.get("phonebooks") or []
    pbooks_txt = ", ".join(str(x) for x in pbooks) if pbooks else "Не найдено"

    hist = data.get("name_history") or []
    hist_lines = [f"История изменения имени ({len(hist)}):"]
    if hist:
        for item in hist[:12]:
            hist_lines.append(html.escape(str(item)))
        if len(hist) > 12:
            hist_lines.append(f"... еще {len(hist) - 12}")
    else:
        hist_lines.append("Не найдено")

    extra_lines = []
    seen = set()
    for pair in data.get("extra_kv") or []:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        k_s, v_s = str(pair[0]).strip(), str(pair[1]).strip()
        sig = f"{k_s.lower()}:{v_s.lower()}"
        if sig in seen:
            continue
        seen.add(sig)
        extra_lines.append(f"- {html.escape(k_s)}: <code>{html.escape(v_s)}</code>")
        if len(extra_lines) >= 12:
            break

    views = data.get("views_count")
    if views is None:
        views = sum(len(v) for v in (data.get("databases") or {}).values() if isinstance(v, list))

    if qtype == "phone":
        digits = re.sub(r"\D", "", query)
        shown_ph = phone if phone != "Не найдено" else (digits or query or "Не найдено")
        loc = " / ".join(x for x in (operator if operator != "Нет" else "", region if region != "Нет" else "") if x) or "Не найдено"
        dob_txt = f"{dob} (возраст: {age})" if age != "Не найдено" else dob
        parts = [
            f"<b>Номер</b> · <code>{html.escape(str(shown_ph))}</code>\n"
            f"- Оператор / Регион: {html.escape(loc)}\n"
            f"- ФИО: {html.escape(fio)}\n"
            f"- Дата рождения: {html.escape(dob_txt)}",
            f"Телефонные книги: {html.escape(pbooks_txt)}",
            "\n".join(hist_lines),
        ]
        if tg_id or (tg_uname and not re.fullmatch(r"@\+?\d+", tg_uname)):
            parts.append(
                f"Telegram: {html.escape(tg_uname or '-')} (ID: <code>{html.escape(str(tg_id or '-'))}</code>)\n"
                f"Регистрация TG: {html.escape(reg_date)}"
            )
    elif qtype == "telegram":
        shown_tg = tg_uname or query or "Не найдено"
        shown_id = str(tg_id) if tg_id else (query if re.fullmatch(r"\d{6,11}", query) else "Не найдено")
        tg_lines = [
            f"<b>Telegram</b> · {html.escape(str(shown_tg))}",
            f"- ID: <code>{html.escape(shown_id)}</code>",
            f"- Имя / ФИО: {html.escape(fio)}",
            f"- Номер: {html.escape(phone)}",
            f"- Регистрация: {html.escape(reg_date)}",
        ]
        if tg.get("dc"):
            tg_lines.append(f"- DC: {html.escape(str(tg['dc']))}")
        if tg.get("premium"):
            tg_lines.append(f"- Premium: {html.escape(str(tg['premium']))}")
        if tg.get("bio"):
            tg_lines.append(f"- О себе: {html.escape(str(tg['bio']))}")
        parts = [
            "\n".join(tg_lines),
            f"Телефонные книги: {html.escape(pbooks_txt)}",
            "\n".join(hist_lines),
        ]
    elif qtype == "email":
        parts = [
            f"<b>Email</b> · <code>{html.escape(query)}</code>\n"
            f"- ФИО: {html.escape(fio)}\n"
            f"- Номер: {html.escape(phone)}\n"
            f"- Дата рождения: {html.escape(dob)}",
            f"Телефонные книги: {html.escape(pbooks_txt)}",
            "\n".join(hist_lines),
        ]
    elif qtype in ("vin", "vehicle"):
        label = "VIN" if qtype == "vin" else "Госномер"
        parts = [
            f"<b>{label}</b> · <code>{html.escape(query.upper())}</code>\n"
            f"- Владелец / ФИО: {html.escape(fio)}\n"
            f"- Номер: {html.escape(phone)}\n"
            f"- Регион: {html.escape(region)}",
            "\n".join(hist_lines),
        ]
    elif qtype == "ip":
        loc = " / ".join(x for x in (operator if operator != "Нет" else "", region if region != "Нет" else "") if x) or "Не найдено"
        parts = [
            f"<b>IP-адрес</b> · <code>{html.escape(query)}</code>\n"
            f"- Провайдер / Регион: {html.escape(loc)}\n"
            f"- ФИО / Владелец: {html.escape(fio)}\n"
            f"- Контакты: {html.escape(phone)}",
            "\n".join(hist_lines),
        ]
    else:
        parts = [
            f"<b>Запрос</b> · {html.escape(query or 'Не найдено')}\n"
            f"- ФИО: {html.escape(fio)}\n"
            f"- Номер: {html.escape(phone)}\n"
            f"- Дата рождения: {html.escape(dob)}",
            f"Телефонные книги: {html.escape(pbooks_txt)}",
            "\n".join(hist_lines),
        ]

    if extra_lines:
        parts.append("<b>Поля:</b>\n" + "\n".join(extra_lines))

    if not has_findings(data):
        parts.append("По открытым источникам и базам совпадений не найдено.")

    parts.append(f"Найдено записей / совпадений: <b>{html.escape(str(views))}</b>")
    parts.append("Веб-отчет и выгрузки доступны по кнопкам ниже.")
    return "\n\n".join(parts)


def make_local_analysis(data: dict) -> str:
    if data.get("local_analysis"):
        return str(data["local_analysis"])
    if not has_findings(data):
        return "Недостаточно данных для анализа — совпадений не найдено."

    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    tg_id = tg.get("id") or tg.get("user_id")
    raw_uname = tg.get("username") or ""
    tg_uname = f"@{str(raw_uname).lstrip('@')}" if raw_uname else ""

    phone = str(data.get("phone_number") or "").strip()
    fio = str(data.get("fio") or "").strip()
    reg_date = str(data.get("reg_date") or "").strip()
    region = str(data.get("region") or "").strip()
    operator = str(data.get("operator") or "").strip()
    pbooks = [str(x).strip() for x in (data.get("phonebooks") or []) if str(x).strip()]
    hist = [str(x).strip() for x in (data.get("name_history") or []) if str(x).strip()]
    dbs = data.get("databases") if isinstance(data.get("databases"), dict) else {}

    unames: list[str] = []
    seen_u: set[str] = set()
    for cand in [tg_uname] + list(data.get("usernames_set") or []):
        if cand and cand.lower() not in seen_u:
            seen_u.add(cand.lower())
            unames.append(cand)
    for h in hist:
        for m in re.findall(r"@[A-Za-z0-9_]{3,}", h):
            if m.lower() not in seen_u:
                seen_u.add(m.lower())
                unames.append(m)

    facts = []
    if tg_uname or tg_id:
        facts.append(f"Профиль TG: {tg_uname or '-'} (ID: {tg_id or '-'})")
    if reg_date and reg_date != "Не найдено":
        facts.append(f"Регистрация TG: {reg_date}")
    if phone and phone != "Не найдено":
        loc = " / ".join(x for x in (operator if operator != "Нет" else "", region if region != "Нет" else "") if x)
        facts.append(f"Телефон: {phone}" + (f" ({loc})" if loc else ""))
    if fio and fio not in ("Не найдено", "НЕ НАЙДЕНО"):
        facts.append(f"ФИО/Имя: {fio}")
    if pbooks:
        facts.append(f"Записные книги: {', '.join(pbooks[:6])}")

    patterns = []
    if hist:
        patterns.append(f"Изменений имени/ника зафиксировано: {len(hist)}")
    if unames:
        patterns.append(f"Связанные никнеймы ({len(unames)}): {', '.join(unames[:8])}")
    if dbs:
        total = sum(len(v) for v in dbs.values() if isinstance(v, list))
        patterns.append(f"Источников с данными: {len(dbs)} ({total} записей)")

    lines = ["<b>Анализ связей и фактов</b>\n"]
    if facts:
        lines.append("<b>Факты:</b>")
        for item in facts:
            lines.append(f"* {html.escape(item)}")
    if patterns:
        lines.append("\n<b>Активность и источники:</b>")
        for item in patterns:
            lines.append(f"* {html.escape(item)}")

    res = "\n".join(lines)
    data["local_analysis"] = res
    return res


# --- Веб-отчет ---
def collect_blocks(data: dict) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    phones = list(data.get("phones_set") or [])
    if data.get("phone_number") and data.get("phone_number") != "Не найдено":
        push_unique(phones, str(data["phone_number"]))
    if phones:
        blocks.append({"title": "ТЕЛЕФОНЫ", "items": [("Телефон", p) for p in phones]})

    fios = list(data.get("fio_set") or [])
    if data.get("fio") and data.get("fio") not in ("Не найдено", "НЕ НАЙДЕНО"):
        for f in str(data["fio"]).split(","):
            push_unique(fios, f)
    if fios:
        blocks.append({"title": "ФИО", "items": [("ФИО", x) for x in fios]})

    dobs = list(data.get("dob_set") or [])
    if data.get("dob") and data.get("dob") not in ("Нет", "Не найдено"):
        for d in str(data["dob"]).split(","):
            push_unique(dobs, d)
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
        tg_items.append(("DC", str(tg["dc"])))
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
        seen = set()
        extra_items: list[tuple[str, str]] = []
        for pair in extra_kv:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            k_s, v_s = str(pair[0]).strip(), str(pair[1]).strip()
            sig = f"{k_s.lower()}:{v_s.lower()}"
            if sig in seen:
                continue
            seen.add(sig)
            extra_items.append((k_s, v_s))
            if len(extra_items) >= 30:
                break
        if extra_items:
            blocks.append({"title": "ДОПОЛНИТЕЛЬНЫЕ ПОЛЯ", "items": extra_items})

    return blocks


def render_html_report(data: dict, report_id: str) -> str:
    raw_q = str(data.get("query") or "—")
    q_esc = html.escape(raw_q)
    qtype = data.get("query_type") or detect_qtype(raw_q)
    type_lbl = html.escape(qtype_title(qtype))

    blocks = collect_blocks(data)
    blocks_cnt = len(blocks)

    export_obj = {
        "report_id": report_id,
        "query": data.get("query"),
        "query_type": qtype,
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
    json_raw = json.dumps(export_obj, ensure_ascii=False, indent=2)
    chars_cnt = len(json_raw)
    json_esc = html.escape(json_raw)

    cards = ""
    for blk in blocks:
        t_esc = html.escape(str(blk["title"]))
        rows = "".join(
            f"<div class='kv-item'><span class='dot'>▸</span>"
            f"<span class='kv-key'>{html.escape(str(k))}:</span> "
            f"<span class='kv-val'>{html.escape(str(v))}</span></div>"
            for k, v in blk["items"]
        )
        cards += f"""
        <div class="info-card">
            <div class="card-head"><span class="green-dot"></span> {t_esc}</div>
            <div class="card-body">{rows}</div>
        </div>
        """

    if not cards:
        cards = """
        <div class="info-card">
            <div class="card-head"><span class="green-dot"></span> РЕЗУЛЬТАТЫ</div>
            <div class="card-body">
                <div class="kv-item"><span class="dot">▸</span><span class="kv-val">Записи не найдены — проверьте вкладку JSON.</span></div>
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
    <title>Oasis Digital — Отчет по {q_esc}</title>
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
            <div class="hero-sub">Отчет по запросу <code>{q_esc}</code></div>
            <div class="badges-row">
                <span class="meta-badge">{host_clean}</span>
                <span class="meta-badge">{bot_clean}</span>
            </div>
        </div>

        <div class="stats-grid">
            <div class="stat-box">
                <div class="stat-label">ТИП</div>
                <div class="stat-val">{type_lbl}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">БЛОКОВ</div>
                <div class="stat-val">{blocks_cnt}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">СИМВОЛОВ</div>
                <div class="stat-val">{chars_cnt}</div>
            </div>
        </div>

        <div class="tabs-bar">
            <button class="tab-btn active" onclick="switchTab('cards', this)">Карточки</button>
            <button class="tab-btn" onclick="switchTab('json', this)">JSON</button>
        </div>

        <div id="panel-cards" class="tab-panel active">
            {cards}
        </div>
        <div id="panel-json" class="tab-panel">
            <pre class="json-pre">{json_esc}</pre>
        </div>
    </div>

    <script>
        function switchTab(name, btn) {{
            document.querySelectorAll('.tab-panel').forEach(function (el) {{ el.classList.remove('active'); }});
            document.querySelectorAll('.tab-btn').forEach(function (el) {{ el.classList.remove('active'); }});
            document.getElementById('panel-' + name).classList.add('active');
            btn.classList.add('active');
        }}

        (function drawBg() {{
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
                        z: Math.sin(theta) * Math.sin(phi)
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
                    return {{ sx: cx + x1 * radius, sy: cy + y2 * radius, z: z2 }};
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


# --- FastAPI ---
web_app = FastAPI()
web_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@web_app.get("/")
async def index():
    return {"status": "ok", "service": "Oasis Digital"}


@web_app.get("/report/{report_id}", response_class=HTMLResponse)
async def report_page(report_id: str):
    data = read_report(report_id)
    if not data:
        return HTMLResponse("<h1>404 Отчет не найден</h1>", status_code=404)
    return HTMLResponse(content=render_html_report(data, report_id))


async def serve_web():
    port = int(os.getenv("PORT", "10000"))
    cfg = uvicorn.Config(web_app, host="0.0.0.0", port=port, log_level="info")
    await uvicorn.Server(cfg).serve()


# --- Хендлеры бота ---
@dp.message(CommandStart())
async def on_start(msg: Message, state: FSMContext):
    await state.clear()
    if check_banned(msg.from_user.id):
        return await msg.answer("Доступ ограничен.")
    create_user(msg.from_user.id, msg.from_user.username)
    await msg.answer(START_TEXT, parse_mode="HTML", reply_markup=kb_main())


@dp.callback_query(F.data == "back_to_start")
async def on_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(START_TEXT, parse_mode="HTML", reply_markup=kb_main())
    await ack_cb(cb)


@dp.callback_query(F.data == "search_info")
async def on_search_info(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(START_TEXT, parse_mode="HTML", reply_markup=kb_back())
    await ack_cb(cb)


@dp.callback_query(F.data == "profile")
async def on_profile(cb: CallbackQuery):
    user = fetch_user(cb.from_user.id)
    tenure = get_tenure(user[4]) if user else "0 дн."
    txt = (
        f"<b>Профиль</b>\n\nID: <code>{cb.from_user.id}</code>\n"
        f"Запросов: <b>{user[2] if user else 0}</b>\n"
        f"Дата регистрации: {user[4] if user else '-'}\n"
        f"<i>В системе: {tenure}</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Скрытие данных ($5)", callback_data="hide_data_menu")],
            [InlineKeyboardButton(text="Назад", callback_data="back_to_start")],
        ]
    )
    await cb.message.edit_text(txt, parse_mode="HTML", reply_markup=kb)
    await ack_cb(cb)


@dp.callback_query(F.data == "hide_data_menu")
async def on_hide_menu(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить Tonkeeper", callback_data="pay_tonkeeper")],
            [InlineKeyboardButton(text="Назад", callback_data="profile")],
        ]
    )
    await cb.message.edit_text(HIDE_DATA_TEXT, parse_mode="HTML", reply_markup=kb)
    await ack_cb(cb)


@dp.callback_query(F.data == "pay_tonkeeper")
async def on_pay_ton(cb: CallbackQuery):
    memo = f"Oasis_{cb.from_user.id}"
    url = f"ton://transfer/{TON_WALLET}?amount=13040000000&text={memo}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить в 1 клик", url=url)],
            [InlineKeyboardButton(text="Назад", callback_data="hide_data_menu")],
        ]
    )
    await cb.message.edit_text(
        f"<b>Оплата через TON</b>\n\nКошелек: <code>{TON_WALLET}</code>\nMemo: <code>{memo}</code>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await ack_cb(cb)


@dp.callback_query(F.data == "buy_requests")
async def on_buy(cb: CallbackQuery):
    await cb.message.edit_text(
        "<b>Покупка запросов</b>\nПо вопросам пополнения: " + ADMIN_USERNAME,
        parse_mode="HTML",
        reply_markup=kb_back(),
    )
    await ack_cb(cb)


@dp.callback_query(F.data == "ref_link")
async def on_ref(cb: CallbackQuery):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
    await cb.message.edit_text(
        f"<b>Реферальная ссылка:</b>\n\n<code>{link}</code>",
        parse_mode="HTML",
        reply_markup=kb_back(),
    )
    await ack_cb(cb)


@dp.callback_query(F.data == "support")
async def on_support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(States.support_msg)
    await cb.message.edit_text(SUPPORT_TEXT, parse_mode="HTML", reply_markup=kb_back())
    await ack_cb(cb)


@dp.message(States.support_msg)
async def handle_support(msg: Message, state: FSMContext):
    await state.clear()
    try:
        await bot.send_message(
            MAIN_ADMIN_ID,
            f"Сообщение от {html.escape(msg.from_user.full_name)} (<code>{msg.from_user.id}</code>):\n\n{html.escape(msg.text or '')}",
            parse_mode="HTML",
        )
        await msg.answer("Отправлено.", reply_markup=kb_main())
    except Exception:
        await msg.answer("Ошибка отправки.", reply_markup=kb_main())


@dp.callback_query(F.data == "use_promo")
async def on_use_promo(cb: CallbackQuery, state: FSMContext):
    await state.set_state(States.promo_code)
    await cb.message.edit_text(
        "<b>Активация промокода / чека</b>\n\nВведите код сообщением:",
        parse_mode="HTML",
        reply_markup=kb_back(),
    )
    await ack_cb(cb)


@dp.message(States.promo_code)
async def handle_promo(msg: Message, state: FSMContext):
    await state.clear()
    code = (msg.text or "").strip()
    uid = msg.from_user.id

    with db_conn() as conn:
        promo = conn.execute(
            "SELECT reward, max_uses, uses_count FROM promos WHERE code = ?",
            (code,),
        ).fetchone()
        if not promo:
            return await msg.answer("Промокод не найден.", reply_markup=kb_main())

        if conn.execute(
            "SELECT 1 FROM promo_activations WHERE user_id = ? AND code = ?",
            (uid, code),
        ).fetchone():
            return await msg.answer("Промокод уже активирован.", reply_markup=kb_main())

        if promo["uses_count"] >= promo["max_uses"]:
            return await msg.answer("Лимит использований исчерпан.", reply_markup=kb_main())

        conn.execute("UPDATE promos SET uses_count = uses_count + 1 WHERE code = ?", (code,))
        conn.execute(
            "INSERT INTO promo_activations (user_id, code) VALUES (?, ?)",
            (uid, code),
        )
        conn.commit()

    give_requests(uid, promo["reward"])
    await msg.answer(
        f"Начислено <b>+{promo['reward']} запросов</b>.",
        parse_mode="HTML",
        reply_markup=kb_main(),
    )


# --- Админка ---
@dp.message(Command("admin"))
async def on_admin(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return await msg.answer("Нет доступа.")
    await msg.answer("<b>Админ-панель</b>", parse_mode="HTML", reply_markup=kb_admin())


@dp.callback_query(F.data == "adm_promo")
async def on_adm_promo(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    await cb.message.edit_text(
        "<b>Создание промокода</b>\n\nФормат: <code>КОД НАГРАДА ЛИМИТ</code>",
        parse_mode="HTML",
        reply_markup=kb_back(),
    )
    await state.set_state(States.adm_promo)
    await ack_cb(cb)


@dp.message(States.adm_promo)
async def handle_adm_promo(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    parts = (msg.text or "").strip().split()
    if len(parts) < 2:
        return await msg.answer("Неверный формат.", reply_markup=kb_admin())
    try:
        code, reward = parts[0], int(parts[1])
        limit_uses = int(parts[2]) if len(parts) > 2 else 1
    except ValueError:
        return await msg.answer("Награда и лимит должны быть числами.", reply_markup=kb_admin())

    with db_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO promos (code, reward, max_uses, uses_count, created_by) VALUES (?, ?, ?, 0, ?)",
                (code, reward, limit_uses, msg.from_user.id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return await msg.answer("Такой код уже существует.", reply_markup=kb_admin())

    await msg.answer(
        f"Промокод <code>{html.escape(code)}</code> на +{reward} запросов создан.",
        parse_mode="HTML",
        reply_markup=kb_admin(),
    )


@dp.callback_query(F.data == "adm_check")
async def on_adm_check(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    await cb.message.edit_text(
        "<b>Создание чека</b>\n\nВведите номинал (запросов):",
        parse_mode="HTML",
        reply_markup=kb_back(),
    )
    await state.set_state(States.adm_check)
    await ack_cb(cb)


@dp.message(States.adm_check)
async def handle_adm_check(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    try:
        reward = int((msg.text or "").strip())
    except ValueError:
        return await msg.answer("Введите число.", reply_markup=kb_admin())

    code = f"CHECK-{uuid.uuid4().hex[:8].upper()}"
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO promos (code, reward, max_uses, uses_count, created_by) VALUES (?, ?, 1, 0, ?)",
            (code, reward, msg.from_user.id),
        )
        conn.commit()

    await msg.answer(
        f"Чек создан: <code>{code}</code> на <b>{reward}</b> запросов.",
        parse_mode="HTML",
        reply_markup=kb_admin(),
    )


@dp.callback_query(F.data == "adm_test_api")
async def on_adm_test_api(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    await cb.message.edit_text(
        "<b>Тест API (Raw)</b>\n\nВведите запрос (таймаут 90 сек):",
        parse_mode="HTML",
        reply_markup=kb_back(),
    )
    await state.set_state(States.adm_raw)
    await ack_cb(cb)


@dp.message(States.adm_raw)
async def handle_adm_raw(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    q = (msg.text or "").strip()
    note = await msg.answer(
        f"Запрос к Black-Box и Argus (до 90с): <b>{html.escape(q)}</b>...",
        parse_mode="HTML",
    )

    res_bb, res_argus = await asyncio.gather(
        query_blackbox(q, BLACKBOX_KEY, debug=True),
        query_argus(q, ARGUS_KEY, debug=True),
        return_exceptions=True,
    )

    bb_s = json.dumps(res_bb, ensure_ascii=False, indent=2)[:1400] if isinstance(res_bb, dict) else str(res_bb)
    arg_s = json.dumps(res_argus, ensure_ascii=False, indent=2)[:1400] if isinstance(res_argus, dict) else str(res_argus)

    txt = f"<b>BLACK-BOX:</b>\n<pre>{html.escape(bb_s)}</pre>\n\n<b>ARGUS:</b>\n<pre>{html.escape(arg_s)}</pre>"
    await note.edit_text(txt[:4000], parse_mode="HTML", reply_markup=kb_admin())


@dp.callback_query(F.data == "adm_check_apis")
async def on_adm_check_apis(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    note = await cb.message.edit_text("Проверка Black-Box и Argus...", parse_mode="HTML")
    results = await check_apis()
    lines = [
        f"* <b>{r['name']}</b>: {'OK' if r['ok'] else 'FAIL'} ({r['status']}, {r['ms']}ms)"
        for r in results
    ]
    await note.edit_text("<b>Статус API:</b>\n\n" + "\n".join(lines), parse_mode="HTML", reply_markup=kb_admin())
    await ack_cb(cb)


# --- Анализатор и экспорт ---
@dp.callback_query(F.data.startswith("ai_analyze_"))
async def on_analyze(cb: CallbackQuery):
    rid = cb.data.replace("ai_analyze_", "", 1)
    data = read_report(rid)
    if not data:
        return await ack_cb(cb, "Отчет не найден", alert=True)

    await ack_cb(cb, "Анализ...")
    summary = make_local_analysis(data)
    store_report(rid, data)
    txt = f"<b>Цель:</b> <code>{html.escape(str(data.get('query') or ''))}</code>\n\n{summary}"
    await cb.message.answer(txt[:4000], parse_mode="HTML")


@dp.callback_query(F.data.startswith("get_txt_"))
async def on_export_txt(cb: CallbackQuery):
    rid = cb.data.replace("get_txt_", "", 1)
    data = read_report(rid)
    if not data:
        return await ack_cb(cb, "Отчет не найден", alert=True)

    txt = (
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
    doc = BufferedInputFile(txt.encode("utf-8"), filename=f"oasis_{rid}.txt")
    await cb.message.answer_document(document=doc, caption="<b>TXT Отчет</b>", parse_mode="HTML")
    await ack_cb(cb)


@dp.callback_query(F.data.startswith("get_json_"))
async def on_export_json(cb: CallbackQuery):
    rid = cb.data.replace("get_json_", "", 1)
    data = read_report(rid)
    if not data:
        return await ack_cb(cb, "Отчет не найден", alert=True)

    obj = {
        "report_id": rid,
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
    raw = json.dumps(obj, ensure_ascii=False, indent=2)
    doc = BufferedInputFile(raw.encode("utf-8"), filename=f"oasis_{rid}.json")
    await cb.message.answer_document(document=doc, caption="<b>JSON Отчет</b>", parse_mode="HTML")
    await ack_cb(cb)


# --- Поиск ---
@dp.message(F.text & ~F.text.startswith("/"))
async def on_search(msg: Message, state: FSMContext):
    if msg.from_user.is_bot or await state.get_state() is not None:
        return
    if check_banned(msg.from_user.id):
        return await msg.answer("Доступ ограничен.")

    q = (msg.text or "").strip()
    if not q:
        return

    if not spend_request(msg.from_user.id):
        return await msg.answer("Закончились запросы.")

    note = await msg.answer(
        f"<b>[Oasis Digital]</b> Поиск по Black-Box и Argus (до 90 сек)...\n\n<b>Цель:</b> <code>{html.escape(q)}</code>",
        parse_mode="HTML",
    )

    try:
        data = await run_search(q)
        rid = uuid.uuid4().hex[:8]
        store_report(rid, data)

        body = build_text_report(data)
        chunks = [body[i : i + 3900] for i in range(0, len(body), 3900)]
        await note.edit_text(
            chunks[0],
            parse_mode="HTML",
            reply_markup=kb_result(rid) if len(chunks) == 1 else None,
        )
        for part in chunks[1:-1]:
            await msg.answer(part, parse_mode="HTML")
        if len(chunks) > 1:
            await msg.answer(chunks[-1], parse_mode="HTML", reply_markup=kb_result(rid))
    except Exception as exc:
        log.exception("Ошибка поиска '%s': %s", q, exc)
        await note.edit_text(
            f"Ошибка при формировании отчета: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
            reply_markup=kb_main(),
        )


async def main():
    db_init()
    log.info("Старт бота и веб-сервера...")
    await bot.delete_webhook(drop_pending_updates=True)
    web_task = asyncio.create_task(serve_web())
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        web_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено.")
