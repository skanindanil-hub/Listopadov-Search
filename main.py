# language: Python, file: oasis_digital_full.py, runtime: Python 3.11+, target: aiogram v3 + FastAPI + SQLite
import asyncio
import base64
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import ssl
import time
import uuid
import zlib
from collections import OrderedDict, defaultdict
from datetime import datetime
from typing import Any

import aiohttp
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
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
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

BOT_TOKEN = os.getenv("BOT_TOKEN", "9040601874:AAFN29zWh8G-Ghvm6at8tH7Ct_hh1HgDR7k").strip()
BOT_USERNAME = "@OasisDigitalro_bot"

MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "8394631079"))
ADMIN_USERNAME = "@anodoxx"

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1004311841781"))
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/+BqWOF7S3AqQ1YzI6").strip()

BLACKBOX_KEY = os.getenv("BLACKBOX_KEY", "8jdsYcPZiQMTpYNCluXg9scNhc7y").strip()
ARGUS_KEY = os.getenv("ARGUS_KEY", "ARGUS-IO-M1b_3fSDQM5qp5TU5bDMOLAITOluLLtKGrtLSHD2GGE").strip()

TON_WALLET = os.getenv("TON_WALLET", "UQBlTLz8MaKDtNhYo1qeDsOXtVjN1upRp2uwsf0NWWE9p5cx").strip()
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://listopadov-search-1.onrender.com").rstrip("/")
DB_PATH = os.getenv("USERS_DB", "/tmp/users.db")

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
RATE_BUCKETS: dict[str, list[float]] = defaultdict(list)


class States(StatesGroup):
    support_msg = State()
    promo_code = State()
    search_typed = State()
    adm_raw = State()
    adm_promo = State()
    adm_check = State()
    adm_make_api = State()
    adm_add_sub = State()
    adm_del_sub = State()


SEARCH_MODES: dict[str, tuple[str, str, str]] = {
    "phone": ("Phone", "phone", "Введите номер телефона (например 79104813955):"),
    "email": ("Email", "email", "Введите email (например user@example.com):"),
    "username": ("Username", "telegram", "Введите @username (например @durov):"),
    "ip": ("IP", "ip", "Введите IP-адрес (например 78.26.242.244):"),
    "mac": ("MAC Address", "general", "Введите MAC-адрес (например 00:1A:2B:3C:4D:5E):"),
    "metadata": ("Metadata", "general", "Введите метаданные, хэш, ссылку или домен:"),
    "inn": ("ИНН", "general", "Введите ИНН физлица (12 цифр) или организации (10 цифр):"),
    "snils": ("СНИЛС", "general", "Введите СНИЛС (например 112-233-445 95):"),
    "passport": ("ПАСПОРТ", "general", "Введите серию и номер паспорта (например 4510 123456):"),
    "universal": ("Universal Search", "general", "Введите любой запрос (ФИО, телефон, email, авто, ID, домен):"),
}


def red_btn(label: str, **kwargs) -> InlineKeyboardButton:
    text = label.strip()[:64]
    try:
        return InlineKeyboardButton(text=text, style="danger", **kwargs)
    except TypeError:
        return InlineKeyboardButton(text=text, **kwargs)


async def ack_cb(cb: CallbackQuery, text: str | None = None, alert: bool = False):
    try:
        await cb.answer(text=text, show_alert=alert)
    except TelegramBadRequest:
        pass


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_api_keys_table():
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('api_enabled', '1')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('sub_enabled', '1')"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS required_channels (
                chat_id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT
            )
            """
        )
        cnt = conn.execute("SELECT COUNT(*) AS c FROM required_channels").fetchone()["c"]
        if cnt == 0 and CHANNEL_ID and CHANNEL_URL:
            conn.execute(
                """
                INSERT OR IGNORE INTO required_channels (chat_id, url, title, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    int(CHANNEL_ID),
                    CHANNEL_URL,
                    "Основной канал",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                api_key TEXT PRIMARY KEY,
                owner TEXT DEFAULT 'admin',
                requests_limit INTEGER DEFAULT 0,
                requests_used INTEGER DEFAULT 0,
                rate_limit INTEGER DEFAULT 70,
                expires_at TEXT DEFAULT '2099-12-31 23:59:59',
                is_active INTEGER DEFAULT 1,
                created_at TEXT
            )
            """
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
        if "requests_limit" not in cols:
            conn.execute("ALTER TABLE api_keys ADD COLUMN requests_limit INTEGER DEFAULT 0")
        if "requests_used" not in cols:
            conn.execute("ALTER TABLE api_keys ADD COLUMN requests_used INTEGER DEFAULT 0")
        conn.commit()


def is_api_enabled() -> bool:
    init_api_keys_table()
    with db_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'api_enabled'").fetchone()
    return str(row["value"] if row else "1") == "1"


def set_api_enabled(enabled: bool):
    init_api_keys_table()
    val = "1" if enabled else "0"
    with db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('api_enabled', ?)",
            (val,),
        )
        conn.commit()


def is_sub_enabled() -> bool:
    init_api_keys_table()
    with db_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'sub_enabled'").fetchone()
    return str(row["value"] if row else "1") == "1"


def set_sub_enabled(enabled: bool):
    init_api_keys_table()
    with db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('sub_enabled', ?)",
            ("1" if enabled else "0",),
        )
        conn.commit()


def list_required_channels() -> list[dict]:
    init_api_keys_table()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT chat_id, url, title, created_at FROM required_channels ORDER BY rowid ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_required_channel(chat_id: int, url: str, title: str):
    init_api_keys_table()
    clean_title = (title or "Обязательный канал").strip()[:50]
    clean_url = url.strip()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO required_channels (chat_id, url, title, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (int(chat_id), clean_url, clean_title, now_str),
        )
        conn.commit()


def delete_required_channel(chat_id: int) -> bool:
    init_api_keys_table()
    with db_conn() as conn:
        cur = conn.execute("DELETE FROM required_channels WHERE chat_id = ?", (int(chat_id),))
        conn.commit()
        return cur.rowcount > 0


async def get_missing_channels(user_id: int) -> list[dict]:
    if user_id == MAIN_ADMIN_ID or not is_sub_enabled():
        return []
    channels = list_required_channels()
    if not channels:
        return []
    missing: list[dict] = []
    for ch in channels:
        chat_id = int(ch["chat_id"])
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            ok = member.status in {
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            }
        except Exception as exc:
            log.warning("Ошибка проверки подписки user_id=%s chat_id=%s: %s", user_id, chat_id, exc)
            ok = False
        if not ok:
            missing.append(ch)
    return missing


def kb_sub_required(missing: list[dict] | None = None) -> InlineKeyboardMarkup:
    targets = missing if missing else list_required_channels()
    rows: list[list[InlineKeyboardButton]] = []
    for idx, ch in enumerate(targets, start=1):
        btn_txt = ch.get("title") or f"Канал #{idx}"
        rows.append([red_btn(f"Подписаться: {btn_txt}", url=ch["url"])])
    rows.append([red_btn("Я подписался", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def is_user_subscribed(user_id: int) -> bool:
    return len(await get_missing_channels(user_id)) == 0


async def require_sub_msg(msg: Message) -> bool:
    if not msg.from_user:
        return False
    missing = await get_missing_channels(msg.from_user.id)
    if not missing:
        return True
    await msg.answer(
        "🔒 <b>Доступ закрыт без подписки</b>\n\n"
        "Подпишись на обязательные каналы по кнопкам ниже и нажми <b>«Я подписался»</b>.",
        parse_mode="HTML",
        reply_markup=kb_sub_required(missing),
    )
    return False


async def require_sub_cb(cb: CallbackQuery) -> bool:
    if not cb.from_user:
        return False
    missing = await get_missing_channels(cb.from_user.id)
    if not missing:
        return True
    await ack_cb(cb, "Сначала подпишись на обязательные каналы!", alert=True)
    try:
        await cb.message.edit_text(
            "🔒 <b>Доступ закрыт без подписки</b>\n\n"
            "Подпишись на обязательные каналы по кнопкам ниже и нажми <b>«Я подписался»</b>.",
            parse_mode="HTML",
            reply_markup=kb_sub_required(missing),
        )
    except Exception:
        pass
    return False


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
    init_api_keys_table()


db_init()


def generate_oasis_key() -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_"
    body = "".join(secrets.choice(alphabet) for _ in range(38))
    return f"OASIS-IO-{body}"


def create_oasis_api_key(
    owner: str = "admin",
    requests_limit: int = 0,
    rate_limit: int = 70,
    expires_at: str = "2099-12-31 23:59:59",
) -> dict:
    init_api_keys_table()
    key = generate_oasis_key()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    req_lim = max(0, int(requests_limit))
    rpm = max(1, int(rate_limit))
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO api_keys (
                api_key, owner, requests_limit, requests_used, rate_limit, expires_at, is_active, created_at
            )
            VALUES (?, ?, ?, 0, ?, ?, 1, ?)
            """,
            (key, owner, req_lim, rpm, expires_at, now_str),
        )
        conn.commit()
    return {
        "api_key": key,
        "owner": owner,
        "requests_limit": req_lim,
        "requests_used": 0,
        "rate_limit": rpm,
        "expires_at": expires_at,
        "is_active": 1,
        "created_at": now_str,
    }


def list_oasis_api_keys() -> list[dict]:
    init_api_keys_table()
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT api_key, owner, requests_limit, requests_used, rate_limit, expires_at, is_active, created_at
            FROM api_keys ORDER BY rowid DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_oasis_api_key(api_key: str) -> bool:
    init_api_keys_table()
    k = (api_key or "").strip()
    if not k:
        return False
    with db_conn() as conn:
        cur = conn.execute("UPDATE api_keys SET is_active = 0 WHERE api_key = ?", (k,))
        conn.commit()
        return cur.rowcount > 0


def format_api_key_card(item: dict) -> str:
    key = html.escape(str(item["api_key"]))
    exp = html.escape(str(item.get("expires_at") or "2099-12-31 23:59:59"))
    rpm = int(item.get("rate_limit") or 70)
    req_lim = int(item.get("requests_limit") or 0)
    quota_txt = "Безлимитный" if req_lim <= 0 else f"{req_lim} запросов"
    domain = html.escape(WEBHOOK_HOST)
    return (
        "🔑 <b>API-ключ успешно создан!</b>\n\n"
        "Код ключа (нажмите для копирования):\n"
        f"<code>{key}</code>\n\n"
        f"⏳ <b>Срок действия:</b> Бессрочно (до {exp})\n"
        f"📦 <b>Лимит запросов:</b> {quota_txt}\n"
        "⚡️ <b>Доступ:</b> Full Data REST API (без фильтрации полей)\n"
        f"🚀 <b>Лимит скорости:</b> {rpm} запросов в минуту\n"
        f"🌐 <b>Домен API:</b> {domain}\n\n"
        "Пример запроса cURL:\n"
        f'<code>curl -H "X-API-Key: {key}" "{domain}/api/v1/search?q=79991234567"</code>\n\n'
        "Список всех ключей: /apilist\n"
        f"Отозвать: /delapi {key}"
    )


def verify_oasis_key(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict:
    if not is_api_enabled():
        raise HTTPException(status_code=503, detail="Oasis API is disabled")

    init_api_keys_table()
    token = (x_api_key or "").strip()
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if not token:
        raise HTTPException(status_code=401, detail="Missing X-API-Key")

    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT api_key, owner, requests_limit, requests_used, rate_limit, expires_at, is_active
            FROM api_keys WHERE api_key = ?
            """,
            (token,),
        ).fetchone()

        if not row or int(row["is_active"]) != 1:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key")

        req_lim = int(row["requests_limit"] or 0)
        req_used = int(row["requests_used"] or 0)
        if req_lim > 0 and req_used >= req_lim:
            raise HTTPException(status_code=429, detail="API request quota exhausted")

        rpm = int(row["rate_limit"] or 70)
        now_ts = time.monotonic()
        bucket = RATE_BUCKETS[token]
        bucket[:] = [ts for ts in bucket if now_ts - ts < 60.0]
        if len(bucket) >= rpm:
            raise HTTPException(status_code=429, detail=f"Rate limit exceeded ({rpm}/min)")
        bucket.append(now_ts)

        conn.execute(
            "UPDATE api_keys SET requests_used = requests_used + 1 WHERE api_key = ?",
            (token,),
        )
        conn.commit()

    info = dict(row)
    info["requests_used"] = req_used + 1
    return info


def to_json_safe(obj: Any) -> Any:
    if isinstance(obj, set):
        return sorted(str(x) for x in obj)
    if isinstance(obj, dict):
        return {str(k): to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json_safe(x) for x in obj]
    return obj


def normalize_rid(report_id: str | None) -> str:
    s = (report_id or "").strip().split("?")[0].strip("/")
    if s.startswith("report/"):
        s = s.split("/", 1)[1]
    return re.sub(r"[^A-Za-z0-9_-]", "", s)


def store_report(report_id: str, payload: dict):
    rid = normalize_rid(report_id) or uuid.uuid4().hex[:8]
    db_init()
    safe = to_json_safe(payload)
    safe["report_id"] = rid
    cache[rid] = safe
    cache[rid.lower()] = safe
    with db_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO reports (report_id, query, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                rid,
                str(safe.get("query") or ""),
                json.dumps(safe, ensure_ascii=False),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()


def read_report(report_id: str | None) -> dict | None:
    rid = normalize_rid(report_id)
    if not rid:
        return None
    if rid in cache:
        return cache[rid]
    if rid.lower() in cache:
        return cache[rid.lower()]
    db_init()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT payload_json FROM reports WHERE LOWER(TRIM(report_id)) = LOWER(?)",
            (rid,),
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload_json"])
        cache[rid] = data
        cache[rid.lower()] = data
        return data
    except Exception:
        return None


def pack_report_token(data: dict) -> str:
    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    compact = {
        "q": data.get("query") or "",
        "qt": data.get("query_type") or "general",
        "dt": data.get("date") or "",
        "fio": data.get("fio") or "Не найдено",
        "dob": data.get("dob") or "Нет",
        "age": data.get("age") or "Не найдено",
        "inn": data.get("inn") or "Нет",
        "passport": data.get("passport") or "Нет",
        "snils": data.get("snils") or "Нет",
        "addr": (data.get("addresses") or [])[:10],
        "veh": (data.get("vehicles") or [])[:10],
        "soc": (data.get("social_links") or [])[:10],
        "ph": data.get("phone_number") or "Не найдено",
        "op": data.get("operator") or "Нет",
        "reg": data.get("region") or "Нет",
        "tgr": data.get("reg_date") or "Не найдено",
        "tgi": tg.get("id") or "",
        "tgu": tg.get("username") or "",
        "pb": (data.get("phonebooks") or [])[:15],
        "nh": (data.get("name_history") or [])[:15],
        "ex": (data.get("extra_kv") or [])[:30],
    }
    raw_bytes = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(raw_bytes, level=9)
    return base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")


def unpack_report_token(token: str) -> dict | None:
    if not token:
        return None
    s = token.strip().split("?")[0].strip("/")
    if s.startswith("r/"):
        s = s.split("/", 1)[1]
    pad = "=" * ((4 - len(s) % 4) % 4)
    try:
        compressed = base64.urlsafe_b64decode((s + pad).encode("ascii"))
        raw_bytes = zlib.decompress(compressed)
        c = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(c, dict):
            return None
        return {
            "query": c.get("q") or "",
            "query_type": c.get("qt") or "general",
            "date": c.get("dt") or "",
            "fio": c.get("fio") or "Не найдено",
            "dob": c.get("dob") or "Нет",
            "age": c.get("age") or "Не найдено",
            "inn": c.get("inn") or "Нет",
            "passport": c.get("passport") or "Нет",
            "snils": c.get("snils") or "Нет",
            "addresses": c.get("addr") or [],
            "vehicles": c.get("veh") or [],
            "social_links": c.get("soc") or [],
            "phone_number": c.get("ph") or "Не найдено",
            "operator": c.get("op") or "Нет",
            "region": c.get("reg") or "Нет",
            "reg_date": c.get("tgr") or "Не найдено",
            "tg_info": {
                "id": c.get("tgi") or "",
                "username": c.get("tgu") or "",
            },
            "phonebooks": c.get("pb") or [],
            "name_history": c.get("nh") or [],
            "extra_kv": c.get("ex") or [],
            "databases": {},
            "raw_response": {},
        }
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


PIN_BANNER_TEXT = '🕵‍♂<a href="https://oasissearchcom.localhost.cc">oasis.cc</a> - бот для поиска информации'

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

<i>Отправьте запрос сообщением или выберите тип в разделе «Поиск».</i>"""

SUPPORT_TEXT = "💬 <b>Связь с нами</b>\nНапишите ваше сообщение в чат."
HIDE_DATA_TEXT = "🛡️ <b>Скрытие данных</b>\nСтоимость скрытия данных: $7 (навсегда)"


def kb_main():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [red_btn("Поиск", callback_data="search_info")],
            [red_btn("Купить запросы", callback_data="buy_requests")],
            [red_btn("Профиль", callback_data="profile")],
            [red_btn("Активировать промокод", callback_data="use_promo")],
            [
                red_btn("Рефералка", callback_data="ref_link"),
                red_btn("Связь с нами", callback_data="support"),
            ],
        ]
    )


def kb_search_types():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                red_btn("Phone", callback_data="stype_phone"),
                red_btn("Email", callback_data="stype_email"),
            ],
            [
                red_btn("Username", callback_data="stype_username"),
                red_btn("IP", callback_data="stype_ip"),
            ],
            [
                red_btn("MAC Address", callback_data="stype_mac"),
                red_btn("Metadata", callback_data="stype_metadata"),
            ],
            [
                red_btn("ИНН", callback_data="stype_inn"),
                red_btn("СНИЛС", callback_data="stype_snils"),
            ],
            [red_btn("ПАСПОРТ", callback_data="stype_passport")],
            [red_btn("Universal Search", callback_data="stype_universal")],
            [red_btn("Back", callback_data="back_to_start")],
        ]
    )


def kb_back():
    return InlineKeyboardMarkup(
        inline_keyboard=[[red_btn("Back", callback_data="back_to_start")]]
    )


def kb_result(report_id: str, data: dict | None = None):
    rid = normalize_rid(report_id)
    token = pack_report_token(data) if data else ""
    web_url = f"{WEBHOOK_HOST}/r/{token}" if token else f"{WEBHOOK_HOST}/report/{rid}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [red_btn("Анализатор", callback_data=f"ai_analyze_{rid}")],
            [red_btn("Веб-отчет", url=web_url)],
            [
                red_btn("TXT", callback_data=f"get_txt_{rid}"),
                red_btn("JSON", callback_data=f"get_json_{rid}"),
            ],
            [red_btn("В меню", callback_data="back_to_start")],
        ]
    )


def kb_admin():
    sub_txt = "ОП: ВКЛ (выкл)" if is_sub_enabled() else "ОП: ВЫКЛ (вкл)"
    api_txt = "API: ВКЛ (выкл)" if is_api_enabled() else "API: ВЫКЛ (вкл)"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [red_btn(sub_txt, callback_data="adm_toggle_sub")],
            [
                red_btn("Добавить канал", callback_data="adm_add_sub"),
                red_btn("Удалить канал", callback_data="adm_del_sub"),
            ],
            [red_btn("Список каналов", callback_data="adm_list_sub")],
            [red_btn("Сделать API", callback_data="adm_make_api")],
            [red_btn(api_txt, callback_data="adm_toggle_api")],
            [
                red_btn("Тест API (Raw)", callback_data="adm_test_api"),
                red_btn("Проверка API", callback_data="adm_check_apis"),
            ],
            [
                red_btn("Создать промокод", callback_data="adm_promo"),
                red_btn("Создать чек", callback_data="adm_check"),
            ],
            [red_btn("В меню", callback_data="back_to_start")],
        ]
    )


JUNK_VALUES = {
    "",
    "-",
    "--",
    "---",
    ".",
    "..",
    "none",
    "null",
    "nan",
    "n/a",
    "na",
    "undefined",
    "false",
    "true",
    "не найдено",
    "неизвестно",
    "unknown",
    "нет данных",
    "отсутствует",
    "скрыто",
    "hidden",
    "private",
    "0",
}


def is_junk_value(val: str) -> bool:
    s = (val or "").strip()
    low = s.lower()
    if low in JUNK_VALUES:
        return True
    if "*" in s or "•" in s or "█" in s or "■" in s:
        return True
    if re.fullmatch(r"[*•_\-?#.]+", s):
        return True
    if re.fullmatch(r"(?:не\s+найдено|нет\s+данных|неизвестно|unknown|null|none)", low):
        return True
    return False


def detect_qtype(query: str) -> str:
    q = (query or "").strip()
    low = q.lower()
    digits = re.sub(r"\D", "", q)

    if "vk.com/" in low or "vk.me/" in low or low.startswith("vk"):
        return "vk"
    if re.fullmatch(r"\+?\d[\d\s\-()]{6,17}", q) and 10 <= len(digits) <= 15:
        return "phone"
    if re.fullmatch(r"\d{6,11}", q):
        return "telegram"
    if q.startswith("@") or "t.me/" in low or "telegram" in low:
        return "telegram"
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
    if re.fullmatch(r"(?:https?://)?(?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:/.*)?", q):
        return "domain"
    return "general"


def qtype_title(qtype: str) -> str:
    return {
        "phone": "ТЕЛЕФОН",
        "telegram": "TELEGRAM",
        "vk": "VK",
        "domain": "ДОМЕН",
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


def split_html_lines(text: str, limit: int = 3800) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


async def safe_edit_or_send(
    msg: Message,
    note: Message,
    text: str,
    reply_markup=None,
    edit_first: bool = False,
):
    try:
        if edit_first:
            await note.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await msg.answer(text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "can't parse entities" in str(exc).lower():
            plain = re.sub(r"<[^>]+>", "", text)
            if edit_first:
                await note.edit_text(plain, reply_markup=reply_markup)
            else:
                await msg.answer(plain, reply_markup=reply_markup)
        else:
            raise


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
    "middle_name",
    "patronymic",
    "фио",
    "имя",
    "фамилия",
    "отчество",
    "владелец",
}
DOB_KEYS = {
    "dob",
    "birth_date",
    "birthdate",
    "birthday",
    "date_of_birth",
    "bday",
    "born",
    "дата_рождения",
    "дата рождения",
    "др",
}
AGE_KEYS = {"age", "возраст"}
INN_KEYS = {"inn", "tin", "tax_id", "инн", "инн_фл", "инн_юл"}
PASSPORT_KEYS = {
    "passport",
    "passport_number",
    "passport_series",
    "pass_num",
    "pass_ser",
    "doc_number",
    "document",
    "паспорт",
    "серия_паспорта",
    "номер_паспорта",
    "паспортные_данные",
}
SNILS_KEYS = {"snils", "снилс"}
ADDRESS_KEYS = {
    "address",
    "addr",
    "registration_address",
    "reg_address",
    "living_address",
    "fact_address",
    "home_address",
    "street",
    "city",
    "region",
    "area",
    "location",
    "адрес",
    "прописка",
    "адрес_регистрации",
    "фактический_адрес",
    "город",
    "регион",
}
VEHICLE_KEYS = {
    "car",
    "cars",
    "vehicle",
    "vehicles",
    "auto",
    "vin",
    "plate",
    "gosnomer",
    "license_plate",
    "sts",
    "pts",
    "авто",
    "автомобиль",
    "госномер",
    "вин",
}
SOCIAL_KEYS = {
    "vk",
    "vk_id",
    "vkontakte",
    "ok",
    "odnoklassniki",
    "instagram",
    "ig",
    "facebook",
    "fb",
    "tiktok",
    "github",
    "linkedin",
    "discord",
    "skype",
    "whatsapp",
    "viber",
    "url",
    "profile_url",
    "link",
    "links",
    "socials",
    "social",
    "accounts",
    "соцсети",
    "ссылка",
    "ссылки",
    "профиль",
}
PHONE_KEYS = {"phone", "phone_number", "mobile", "msisdn", "number", "tel", "phones", "телефон", "номер"}
OPERATOR_KEYS = {"operator", "carrier", "provider", "оператор"}
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
    "request_id",
    "trace_id",
    "version",
}


def push_unique(target: list[str], val: str, allow_masked: bool = False):
    s = str(val).strip()
    if not s:
        return
    if not allow_masked and is_junk_value(s):
        return
    if s not in target:
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


def extract_regex_entities(text: str, out: dict):
    if not text or is_junk_value(text):
        return
    for m in re.findall(r"\b\d{10}(?:\d{2})?\b", text):
        if not re.fullmatch(r"\d{8}", m):
            push_unique(out["inn_list"], m)
    for m in re.findall(r"\b\d{4}\s?\d{6}\b", text):
        push_unique(out["passport_list"], re.sub(r"\s+", " ", m).strip())
    for m in re.findall(r"\b\d{3}-\d{3}-\d{3}[\s-]\d{2}\b", text):
        push_unique(out["snils_list"], m)
    for m in re.findall(r"https?://[^\s<>\"']+", text):
        push_unique(out["social_links"], m.rstrip(".,);]"))


def parse_text_block(text: str, out: dict):
    extract_regex_entities(text, out)
    for raw in text.splitlines():
        clean = re.sub(r"<[^>]+>", "", raw).strip(" -*|\t")
        if ":" not in clean:
            continue
        k_part, v_part = clean.split(":", 1)
        lk = k_part.strip().lower()
        val = v_part.strip()
        if is_junk_value(val):
            continue

        extract_regex_entities(val, out)

        if any(w in lk for w in ("фио", "fio", "full_name", "имя", "владелец")):
            out["fio_set"].add(val)
        elif any(w in lk for w in ("дата рождения", "birth", "dob", "bday")):
            out["dob_set"].add(val)
        elif any(w in lk for w in ("возраст", "age")):
            out["age"] = val
        elif any(w in lk for w in ("инн", "inn", "tin")):
            push_unique(out["inn_list"], val)
        elif any(w in lk for w in ("паспорт", "passport", "серия", "document")):
            push_unique(out["passport_list"], val)
        elif any(w in lk for w in ("снилс", "snils")):
            push_unique(out["snils_list"], val)
        elif any(w in lk for w in ("адрес", "прописк", "address", "addr", "город", "регион")):
            push_unique(out["addresses"], val)
            if lk in ("регион", "region", "город", "city") and out["region"] == "Нет":
                out["region"] = val
        elif any(w in lk for w in ("авто", "vin", "госномер", "vehicle", "car", "стс", "птс")):
            push_unique(out["vehicles"], val)
        elif any(w in lk for w in ("vk", "inst", "соц", "профиль", "ссылк", "url", "link", "account")):
            push_unique(out["social_links"], val)
        elif any(w in lk for w in ("телефон", "phone", "номер", "msisdn")):
            digits = re.sub(r"\D", "", val)
            if 7 <= len(digits) <= 15:
                out["phones_set"].add(val)
                if out["phone_number"] == "Не найдено":
                    out["phone_number"] = val
        elif any(w in lk for w in ("оператор", "operator")):
            if out["operator"] == "Нет":
                out["operator"] = val
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

        if len(k_part.strip()) <= 50 and len(val) <= 300:
            out["extra_kv"].append((k_part.strip(), val))


def absorb_response(resp: Any, out: dict, src: str):
    if not isinstance(resp, dict) or "_error" in resp:
        return

    for path, obj in walk_tree(resp):
        items: dict[str, str] = {}
        fname = ""
        lname = ""
        mname = ""

        for k, v in obj.items():
            lk = str(k).lower().strip()
            if lk in SKIP_KEYS:
                continue

            if isinstance(v, str) and ("\n" in v or ":" in v or "http" in v) and lk in {
                "text",
                "raw_text",
                "report",
                "content",
                "output",
                "answer",
                "description",
                "bio",
                "about",
            }:
                parse_text_block(v, out)

            if isinstance(v, list):
                for el in v:
                    if el is None:
                        continue
                    if isinstance(el, (dict, list)):
                        nested_txt = json.dumps(el, ensure_ascii=False)
                        extract_regex_entities(nested_txt, out)
                        continue
                    sel = str(el).strip()
                    if is_junk_value(sel):
                        continue
                    extract_regex_entities(sel, out)
                    if lk in PHONE_KEYS:
                        digits = re.sub(r"\D", "", sel)
                        if 7 <= len(digits) <= 15:
                            out["phones_set"].add(sel)
                            if out["phone_number"] == "Не найдено":
                                out["phone_number"] = sel
                    elif lk in EMAIL_KEYS and "@" in sel:
                        out["emails_set"].add(sel)
                    elif lk in INN_KEYS:
                        push_unique(out["inn_list"], sel)
                    elif lk in PASSPORT_KEYS:
                        push_unique(out["passport_list"], sel)
                    elif lk in SNILS_KEYS:
                        push_unique(out["snils_list"], sel)
                    elif lk in ADDRESS_KEYS:
                        push_unique(out["addresses"], sel)
                    elif lk in VEHICLE_KEYS:
                        push_unique(out["vehicles"], sel)
                    elif lk in SOCIAL_KEYS or sel.startswith(("http://", "https://")):
                        push_unique(out["social_links"], sel)
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
            if is_junk_value(sval):
                continue

            extract_regex_entities(sval, out)
            items[str(k)] = sval

            if lk in {"first_name", "имя"}:
                fname = sval
            elif lk in {"last_name", "фамилия"}:
                lname = sval
            elif lk in {"middle_name", "patronymic", "отчество"}:
                mname = sval

            if lk in FIO_KEYS and len(sval) >= 2 and not re.fullmatch(r"\+?[\d\s\-()]+", sval):
                out["fio_set"].add(sval)
            elif lk in DOB_KEYS:
                out["dob_set"].add(sval)
            elif lk in AGE_KEYS and out["age"] == "Не найдено":
                out["age"] = sval
            elif lk in INN_KEYS:
                push_unique(out["inn_list"], sval)
            elif lk in PASSPORT_KEYS:
                push_unique(out["passport_list"], sval)
            elif lk in SNILS_KEYS:
                push_unique(out["snils_list"], sval)
                out["snils_set"].add(sval)
            elif lk in ADDRESS_KEYS:
                push_unique(out["addresses"], sval)
                if lk in {"region", "city", "регион", "город"} and out["region"] == "Нет":
                    out["region"] = sval
            elif lk in VEHICLE_KEYS:
                push_unique(out["vehicles"], sval)
            elif lk in SOCIAL_KEYS or sval.startswith(("http://", "https://")):
                push_unique(out["social_links"], sval)
            elif lk in PHONE_KEYS:
                digits = re.sub(r"\D", "", sval)
                if 7 <= len(digits) <= 15:
                    out["phones_set"].add(sval)
                    if out["phone_number"] == "Не найдено":
                        out["phone_number"] = sval
            elif lk in OPERATOR_KEYS and out["operator"] == "Нет":
                out["operator"] = sval
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

        if fname or lname or mname:
            full = " ".join(p for p in (lname, fname, mname) if p).strip()
            if full and not is_junk_value(full):
                out["fio_set"].add(full)

        if items and path:
            bucket = f"{src} · {path}"
            out["databases"].setdefault(bucket, [])
            if items not in out["databases"][bucket]:
                out["databases"][bucket].append(items)


async def run_search(q: str, forced_qtype: str | None = None) -> dict:
    res_bb, res_argus = await asyncio.gather(
        query_blackbox(q, BLACKBOX_KEY),
        query_argus(q, ARGUS_KEY),
        return_exceptions=True,
    )

    q_clean = q.strip()
    q_type = forced_qtype or detect_qtype(q_clean)

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
        "inn_list": [],
        "passport_list": [],
        "snils_list": [],
        "addresses": [],
        "vehicles": [],
        "social_links": [],
        "extra_kv": [],
        "fio": "Не найдено",
        "dob": "Нет",
        "age": "Не найдено",
        "inn": "Нет",
        "passport": "Нет",
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

    clean_fios = [x for x in sorted(data["fio_set"]) if not is_junk_value(x)]
    clean_dobs = [x for x in sorted(data["dob_set"]) if not is_junk_value(x)]
    clean_snils = [x for x in dict.fromkeys(list(data["snils_list"]) + sorted(data["snils_set"])) if not is_junk_value(x)]
    clean_inn = [x for x in dict.fromkeys(data["inn_list"]) if not is_junk_value(x)]
    clean_pass = [x for x in dict.fromkeys(data["passport_list"]) if not is_junk_value(x)]
    clean_addr = [x for x in dict.fromkeys(data["addresses"]) if not is_junk_value(x)]
    clean_veh = [x for x in dict.fromkeys(data["vehicles"]) if not is_junk_value(x)]
    clean_soc = [x for x in dict.fromkeys(data["social_links"]) if not is_junk_value(x)]
    clean_phones = [x for x in sorted(data["phones_set"]) if not is_junk_value(x)]
    clean_emails = [x for x in sorted(data["emails_set"]) if not is_junk_value(x)]

    data["fio_set"] = set(clean_fios)
    data["dob_set"] = set(clean_dobs)
    data["snils_set"] = set(clean_snils)
    data["phones_set"] = set(clean_phones)
    data["emails_set"] = set(clean_emails)
    data["inn_list"] = clean_inn
    data["passport_list"] = clean_pass
    data["snils_list"] = clean_snils
    data["addresses"] = clean_addr
    data["vehicles"] = clean_veh
    data["social_links"] = clean_soc

    data["fio"] = ", ".join(clean_fios) if clean_fios else "Не найдено"
    data["dob"] = ", ".join(clean_dobs) if clean_dobs else "Нет"
    data["inn"] = ", ".join(clean_inn) if clean_inn else "Нет"
    data["passport"] = ", ".join(clean_pass) if clean_pass else "Нет"
    data["snils"] = ", ".join(clean_snils) if clean_snils else "Нет"

    if data["age"] == "Не найдено" and data["dob"] != "Нет":
        age_calc = calc_age(data["dob"])
        if age_calc:
            data["age"] = age_calc

    if data["phone_number"] == "Не найдено" and clean_phones:
        data["phone_number"] = clean_phones[0]

    total_recs = sum(len(v) for v in data["databases"].values() if isinstance(v, list))
    data["sources_count"] = len(data["databases"])
    if data["views_count"] is None:
        data["views_count"] = max(
            total_recs,
            len(clean_fios)
            + len(clean_phones)
            + len(clean_emails)
            + len(clean_inn)
            + len(clean_pass)
            + len(clean_snils)
            + len(clean_addr)
            + len(clean_veh)
            + len(clean_soc)
            + len(data["extra_kv"]),
        )

    return to_json_safe(data)


def has_findings(data: dict) -> bool:
    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    fio_val = str(data.get("fio") or "")
    return bool(
        tg.get("id")
        or tg.get("username")
        or (data.get("phone_number") and data.get("phone_number") != "Не найдено")
        or (fio_val and fio_val not in ("Не найдено", "НЕ НАЙДЕНО") and not is_junk_value(fio_val))
        or bool(data.get("inn_list"))
        or bool(data.get("passport_list"))
        or bool(data.get("snils_list"))
        or bool(data.get("addresses"))
        or bool(data.get("vehicles"))
        or bool(data.get("social_links"))
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

    raw_fio = str(data.get("fio") or "Не найдено").strip()
    fio = "Не найдено" if is_junk_value(raw_fio) else raw_fio
    dob = str(data.get("dob") or "Нет").strip()
    age = str(data.get("age") or "Не найдено").strip()
    inn = str(data.get("inn") or "Нет").strip()
    passport = str(data.get("passport") or "Нет").strip()
    snils = str(data.get("snils") or "Нет").strip()
    phone = str(data.get("phone_number") or "Не найдено").strip()
    operator = str(data.get("operator") or "Нет").strip()
    region = str(data.get("region") or "Нет").strip()
    reg_date = str(data.get("reg_date") or "Не найдено").strip()

    phones_all = [str(x).strip() for x in (data.get("phones_set") or []) if not is_junk_value(str(x))]
    emails_all = [str(x).strip() for x in (data.get("emails_set") or []) if not is_junk_value(str(x))]
    addrs_all = [str(x).strip() for x in (data.get("addresses") or []) if not is_junk_value(str(x))]
    vehs_all = [str(x).strip() for x in (data.get("vehicles") or []) if not is_junk_value(str(x))]
    socs_all = [str(x).strip() for x in (data.get("social_links") or []) if not is_junk_value(str(x))]

    pbooks = [x for x in (data.get("phonebooks") or []) if not is_junk_value(str(x))]
    pbooks_txt = ", ".join(str(x) for x in pbooks) if pbooks else "Не найдено"

    hist = [x for x in (data.get("name_history") or []) if not is_junk_value(str(x))]
    hist_lines = [f"📝 <b>История изменений ({len(hist)}):</b>"]
    if hist:
        for item in hist[:15]:
            hist_lines.append(f"• {html.escape(str(item))}")
        if len(hist) > 15:
            hist_lines.append(f"• ... еще {len(hist) - 15}")
    else:
        hist_lines.append("• Не найдено")

    extra_lines = []
    seen = set()
    for pair in data.get("extra_kv") or []:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        k_s, v_s = str(pair[0]).strip(), str(pair[1]).strip()
        if is_junk_value(v_s):
            continue
        sig = f"{k_s.lower()}:{v_s.lower()}"
        if sig in seen:
            continue
        seen.add(sig)
        extra_lines.append(f"• {html.escape(k_s)}: <code>{html.escape(v_s)}</code>")
        if len(extra_lines) >= 20:
            break

    views = data.get("views_count")
    if views is None:
        views = sum(len(v) for v in (data.get("databases") or {}).values() if isinstance(v, list))

    sep = "━━━━━━━━━━━━━━━━━━━━"
    dob_txt = f"{dob} ({age} лет)" if age != "Не найдено" else dob

    docs_block = (
        f"🪪 <b>Документы и идентификаторы:</b>\n"
        f"• ИНН: <code>{html.escape(inn)}</code>\n"
        f"• Паспорт: <code>{html.escape(passport)}</code>\n"
        f"• СНИЛС: <code>{html.escape(snils)}</code>"
    )

    if qtype == "phone":
        digits = re.sub(r"\D", "", query)
        shown_ph = phone if phone != "Не найдено" else (digits or query or "Не найдено")
        loc = " / ".join(x for x in (operator if operator != "Нет" else "", region if region != "Нет" else "") if x) or "Не найдено"
        parts = [
            f"📲 <b>Номер</b> · <code>{html.escape(str(shown_ph))}</code>\n"
            f"{sep}\n"
            f"📡 <b>Оператор / Регион:</b> {html.escape(loc)}\n"
            f"🕵️ <b>ФИО:</b> {html.escape(fio)}\n"
            f"🎂 <b>Дата рождения:</b> {html.escape(dob_txt)}",
            docs_block,
            f"📒 <b>Телефонные книги:</b> {html.escape(pbooks_txt)}",
            "\n".join(hist_lines),
        ]
        if tg_id or (tg_uname and not re.fullmatch(r"@\+?\d+", tg_uname)):
            parts.append(
                f"💬 <b>Telegram:</b> {html.escape(tg_uname or '-')} (ID: <code>{html.escape(str(tg_id or '-'))}</code>)\n"
                f"🗓️ <b>Регистрация TG:</b> {html.escape(reg_date)}"
            )
    elif qtype == "telegram":
        shown_tg = tg_uname or query or "Не найдено"
        shown_id = str(tg_id) if tg_id else (query if re.fullmatch(r"\d{6,11}", query) else "Не найдено")
        tg_lines = [
            f"💬 <b>Telegram</b> · {html.escape(str(shown_tg))}",
            sep,
            f"🆔 <b>ID:</b> <code>{html.escape(shown_id)}</code>",
            f"🕵️ <b>Имя / ФИО:</b> {html.escape(fio)}",
            f"🎂 <b>Дата рождения:</b> {html.escape(dob_txt)}",
            f"📲 <b>Номер:</b> {html.escape(phone)}",
            f"🗓️ <b>Регистрация:</b> {html.escape(reg_date)}",
        ]
        if tg.get("dc"):
            tg_lines.append(f"🌐 <b>DC:</b> {html.escape(str(tg['dc']))}")
        if tg.get("premium"):
            tg_lines.append(f"⭐ <b>Premium:</b> {html.escape(str(tg['premium']))}")
        if tg.get("bio") and not is_junk_value(str(tg["bio"])):
            tg_lines.append(f"ℹ️ <b>О себе:</b> {html.escape(str(tg['bio']))}")
        parts = [
            "\n".join(tg_lines),
            docs_block,
            f"📒 <b>Телефонные книги:</b> {html.escape(pbooks_txt)}",
            "\n".join(hist_lines),
        ]
    elif qtype == "email":
        parts = [
            f"📧 <b>Email</b> · <code>{html.escape(query)}</code>\n"
            f"{sep}\n"
            f"🕵️ <b>ФИО:</b> {html.escape(fio)}\n"
            f"📲 <b>Номер:</b> {html.escape(phone)}\n"
            f"🎂 <b>Дата рождения:</b> {html.escape(dob_txt)}",
            docs_block,
            f"📒 <b>Телефонные книги:</b> {html.escape(pbooks_txt)}",
            "\n".join(hist_lines),
        ]
    elif qtype in ("vin", "vehicle"):
        icon = "🔑" if qtype == "vin" else "🚘"
        label = "VIN" if qtype == "vin" else "Госномер"
        parts = [
            f"{icon} <b>{label}</b> · <code>{html.escape(query.upper())}</code>\n"
            f"{sep}\n"
            f"🕵️ <b>Владелец / ФИО:</b> {html.escape(fio)}\n"
            f"📲 <b>Номер:</b> {html.escape(phone)}\n"
            f"📍 <b>Регион:</b> {html.escape(region)}",
            docs_block,
            "\n".join(hist_lines),
        ]
    elif qtype == "ip":
        loc = " / ".join(x for x in (operator if operator != "Нет" else "", region if region != "Нет" else "") if x) or "Не найдено"
        parts = [
            f"🌐 <b>IP-адрес</b> · <code>{html.escape(query)}</code>\n"
            f"{sep}\n"
            f"📡 <b>Провайдер / Регион:</b> {html.escape(loc)}\n"
            f"🕵️ <b>ФИО / Владелец:</b> {html.escape(fio)}\n"
            f"📲 <b>Контакты:</b> {html.escape(phone)}",
            docs_block,
            "\n".join(hist_lines),
        ]
    else:
        parts = [
            f"🔎 <b>Запрос ({html.escape(qtype_title(qtype))})</b> · {html.escape(query or 'Не найдено')}\n"
            f"{sep}\n"
            f"🕵️ <b>ФИО:</b> {html.escape(fio)}\n"
            f"📲 <b>Номер:</b> {html.escape(phone)}\n"
            f"🎂 <b>Дата рождения:</b> {html.escape(dob_txt)}",
            docs_block,
            f"📒 <b>Телефонные книги:</b> {html.escape(pbooks_txt)}",
            "\n".join(hist_lines),
        ]

    if len(phones_all) > 1:
        parts.append("📞 <b>Все телефоны:</b>\n" + "\n".join(f"• <code>{html.escape(p)}</code>" for p in phones_all[:15]))
    if emails_all:
        parts.append("✉️ <b>Emails:</b>\n" + "\n".join(f"• <code>{html.escape(e)}</code>" for e in emails_all[:15]))
    if addrs_all:
        parts.append("🏠 <b>Адреса:</b>\n" + "\n".join(f"• {html.escape(a)}" for a in addrs_all[:10]))
    if vehs_all:
        parts.append("🚗 <b>Транспорт / VIN:</b>\n" + "\n".join(f"• <code>{html.escape(v)}</code>" for v in vehs_all[:10]))
    if socs_all:
        parts.append("🔗 <b>Соцсети и ссылки:</b>\n" + "\n".join(f"• {html.escape(s)}" for s in socs_all[:12]))
    if extra_lines:
        parts.append("🧩 <b>Дополнительные поля:</b>\n" + "\n".join(extra_lines))

    if not has_findings(data):
        parts.append("⚠️ По открытым источникам и базам совпадений не найдено.")

    parts.append(f"{sep}\n📊 <b>Найдено записей / совпадений:</b> <b>{html.escape(str(views))}</b>")
    parts.append("🌐 Веб-отчет и выгрузки доступны по кнопкам ниже.")
    return "\n\n".join(parts)


def make_local_analysis(data: dict) -> str:
    if data.get("local_analysis"):
        return str(data["local_analysis"])
    if not has_findings(data):
        return "⚠️ Недостаточно данных для анализа — совпадений не найдено."

    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    tg_id = tg.get("id") or tg.get("user_id")
    raw_uname = tg.get("username") or ""
    tg_uname = f"@{str(raw_uname).lstrip('@')}" if raw_uname else ""

    phone = str(data.get("phone_number") or "").strip()
    raw_fio = str(data.get("fio") or "").strip()
    fio = "" if is_junk_value(raw_fio) else raw_fio
    reg_date = str(data.get("reg_date") or "").strip()
    region = str(data.get("region") or "").strip()
    operator = str(data.get("operator") or "").strip()
    pbooks = [str(x).strip() for x in (data.get("phonebooks") or []) if str(x).strip() and not is_junk_value(str(x))]
    hist = [str(x).strip() for x in (data.get("name_history") or []) if str(x).strip() and not is_junk_value(str(x))]
    dbs = data.get("databases") if isinstance(data.get("databases"), dict) else {}

    unames: list[str] = []
    seen_u: set[str] = set()
    for cand in [tg_uname] + list(data.get("usernames_set") or []):
        if cand and not is_junk_value(cand) and cand.lower() not in seen_u:
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
    if data.get("inn") and data.get("inn") != "Нет":
        facts.append(f"ИНН: {data['inn']}")
    if data.get("passport") and data.get("passport") != "Нет":
        facts.append(f"Паспорт: {data['passport']}")
    if data.get("snils") and data.get("snils") != "Нет":
        facts.append(f"СНИЛС: {data['snils']}")
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

    lines = ["🧠 <b>Анализ связей и фактов</b>\n"]
    if facts:
        lines.append("📌 <b>Факты:</b>")
        for item in facts:
            lines.append(f"• {html.escape(item)}")
    if patterns:
        lines.append("\n📈 <b>Активность и источники:</b>")
        for item in patterns:
            lines.append(f"• {html.escape(item)}")

    res = "\n".join(lines)
    data["local_analysis"] = res
    return res


def collect_blocks(data: dict) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []

    phones = list(data.get("phones_set") or [])
    if data.get("phone_number") and data.get("phone_number") != "Не найдено":
        push_unique(phones, str(data["phone_number"]))
    if phones:
        blocks.append({"title": "ТЕЛЕФОНЫ", "items": [("Телефон", p) for p in phones]})

    fios = []
    for f_src in list(data.get("fio_set") or []) + (
        str(data["fio"]).split(",") if data.get("fio") and data.get("fio") not in ("Не найдено", "НЕ НАЙДЕНО") else []
    ):
        push_unique(fios, f_src)
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

    doc_items: list[tuple[str, str]] = []
    for x in data.get("inn_list") or []:
        if not is_junk_value(str(x)):
            doc_items.append(("ИНН", str(x)))
    for x in data.get("passport_list") or []:
        if not is_junk_value(str(x)):
            doc_items.append(("Паспорт", str(x)))
    for x in data.get("snils_list") or []:
        if not is_junk_value(str(x)):
            doc_items.append(("СНИЛС", str(x)))
    if doc_items:
        blocks.append({"title": "ДОКУМЕНТЫ (ИНН / ПАСПОРТ / СНИЛС)", "items": doc_items})

    tg = data.get("tg_info") if isinstance(data.get("tg_info"), dict) else {}
    tg_items: list[tuple[str, str]] = []
    if tg.get("username") and not is_junk_value(str(tg["username"])):
        tg_items.append(("Username", str(tg["username"])))
    if tg.get("id") and not is_junk_value(str(tg["id"])):
        tg_items.append(("Telegram ID", str(tg["id"])))
    if data.get("reg_date") and data.get("reg_date") != "Не найдено":
        tg_items.append(("Дата регистрации", str(data["reg_date"])))
    if tg.get("dc") and not is_junk_value(str(tg["dc"])):
        tg_items.append(("DC", str(tg["dc"])))
    if tg.get("premium") and not is_junk_value(str(tg["premium"])):
        tg_items.append(("Premium", str(tg["premium"])))
    if tg.get("bio") and not is_junk_value(str(tg["bio"])):
        tg_items.append(("О себе", str(tg["bio"])))
    if tg_items:
        blocks.append({"title": "TELEGRAM", "items": tg_items})

    loc_items: list[tuple[str, str]] = []
    if data.get("operator") and data.get("operator") != "Нет":
        loc_items.append(("Оператор", str(data["operator"])))
    if data.get("region") and data.get("region") != "Нет":
        loc_items.append(("Регион", str(data["region"])))
    for a in (data.get("addresses") or [])[:10]:
        if not is_junk_value(str(a)):
            loc_items.append(("Адрес", str(a)))
    if loc_items:
        blocks.append({"title": "ОПЕРАТОР, РЕГИОН И АДРЕСА", "items": loc_items})

    vehs = [v for v in (data.get("vehicles") or []) if not is_junk_value(str(v))]
    if vehs:
        blocks.append({"title": "ТРАНСПОРТ / VIN", "items": [("Транспорт", str(v)) for v in vehs]})

    socs = [s for s in (data.get("social_links") or []) if not is_junk_value(str(s))]
    if socs:
        blocks.append({"title": "СОЦСЕТИ И ССЫЛКИ", "items": [("Ссылка", str(s)) for s in socs]})

    emails = [e for e in (data.get("emails_set") or []) if not is_junk_value(str(e))]
    if emails:
        blocks.append({"title": "EMAIL", "items": [("Email", e) for e in emails]})

    pbs = [p for p in (data.get("phonebooks") or []) if not is_junk_value(str(p))]
    if pbs:
        blocks.append({"title": "ТЕЛЕФОННЫЕ КНИГИ", "items": [("Запись", p) for p in pbs]})

    hist = [h for h in (data.get("name_history") or []) if not is_junk_value(str(h))]
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
            if is_junk_value(v_s):
                continue
            sig = f"{k_s.lower()}:{v_s.lower()}"
            if sig in seen:
                continue
            seen.add(sig)
            extra_items.append((k_s, v_s))
            if len(extra_items) >= 40:
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
        "brand": "Oasis Digital",
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
        "phones": list(data.get("phones_set") or []),
        "emails": list(data.get("emails_set") or []),
        "fio": data.get("fio"),
        "dob": data.get("dob"),
        "age": data.get("age"),
        "inn": data.get("inn"),
        "passport": data.get("passport"),
        "snils": data.get("snils"),
        "addresses": data.get("addresses"),
        "vehicles": data.get("vehicles"),
        "social_links": data.get("social_links"),
        "operator": data.get("operator"),
        "region": data.get("region"),
        "phonebooks": data.get("phonebooks"),
        "name_history": data.get("name_history"),
        "extra_kv": data.get("extra_kv"),
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


def build_full_api_payload(query: str, rid: str, data: dict) -> dict:
    token = pack_report_token(data)
    return {
        "ok": True,
        "provider": "Oasis Digital",
        "full_dump": True,
        "report_id": rid,
        "web_report": f"{WEBHOOK_HOST}/r/{token}",
        "query": query,
        "query_type": data.get("query_type"),
        "date": data.get("date"),
        "summary": {
            "fio": data.get("fio"),
            "dob": data.get("dob"),
            "age": data.get("age"),
            "inn": data.get("inn"),
            "passport": data.get("passport"),
            "snils": data.get("snils"),
            "phone_number": data.get("phone_number"),
            "operator": data.get("operator"),
            "region": data.get("region"),
            "reg_date": data.get("reg_date"),
            "tg_info": data.get("tg_info"),
            "phones": list(data.get("phones_set") or []),
            "emails": list(data.get("emails_set") or []),
            "addresses": data.get("addresses") or [],
            "vehicles": data.get("vehicles") or [],
            "social_links": data.get("social_links") or [],
            "phonebooks": data.get("phonebooks") or [],
            "name_history": data.get("name_history") or [],
            "extra_kv": data.get("extra_kv") or [],
            "sources_count": data.get("sources_count", 0),
            "records_count": data.get("views_count", 0),
        },
        "data": data,
    }


class OasisSearchPayload(BaseModel):
    q: str


web_app = FastAPI()
web_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@web_app.get("/")
async def index():
    return {"status": "ok", "service": "Oasis Digital", "api_enabled": is_api_enabled()}


@web_app.get("/api/v1/search")
async def oasis_api_search_get(q: str, _auth: dict = Depends(verify_oasis_key)):
    query = (q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required")
    data = await run_search(query)
    rid = uuid.uuid4().hex[:8]
    store_report(rid, data)
    return build_full_api_payload(query, rid, data)


@web_app.post("/api/v1/search")
async def oasis_api_search_post(payload: OasisSearchPayload, _auth: dict = Depends(verify_oasis_key)):
    query = (payload.q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Field 'q' is required")
    data = await run_search(query)
    rid = uuid.uuid4().hex[:8]
    store_report(rid, data)
    return build_full_api_payload(query, rid, data)


@web_app.get("/r/{token:path}", response_class=HTMLResponse)
async def report_stateless(token: str):
    data = unpack_report_token(token)
    if not data:
        return HTMLResponse("<h1>404 Отчет не найден</h1>", status_code=404)
    return HTMLResponse(content=render_html_report(data, "oasis"))


@web_app.get("/report/{report_id:path}", response_class=HTMLResponse)
async def report_page(report_id: str):
    rid = normalize_rid(report_id)
    data = read_report(rid)
    if not data:
        data = unpack_report_token(report_id)
    if not data:
        return HTMLResponse("<h1>404 Отчет не найден</h1>", status_code=404)
    return HTMLResponse(content=render_html_report(data, rid or "oasis"))


@web_app.get("/report", response_class=HTMLResponse)
async def report_query_page(request: Request):
    raw_id = request.query_params.get("id") or request.query_params.get("report_id") or ""
    rid = normalize_rid(raw_id)
    data = read_report(rid) or unpack_report_token(raw_id)
    if not data:
        return HTMLResponse("<h1>404 Отчет не найден</h1>", status_code=404)
    return HTMLResponse(content=render_html_report(data, rid or "oasis"))


async def serve_web():
    port = int(os.getenv("PORT", "10000"))
    cfg = uvicorn.Config(web_app, host="0.0.0.0", port=port, log_level="info")
    await uvicorn.Server(cfg).serve()


async def execute_search_flow(msg: Message, q: str, forced_qtype: str | None = None):
    if not msg.from_user:
        return
    if check_banned(msg.from_user.id):
        await msg.answer("⛔ Доступ ограничен.")
        return
    if not await require_sub_msg(msg):
        return
    if not spend_request(msg.from_user.id):
        await msg.answer("⚠️ Закончились запросы.")
        return

    note = await msg.answer(
        f"[Oasis Digital] Начался Поиск информации\n(Время поиска от 10-90 сек)\nЦель : <code>{html.escape(q)}</code>",
        parse_mode="HTML",
    )

    try:
        data = await run_search(q, forced_qtype=forced_qtype)
        rid = uuid.uuid4().hex[:8]
        store_report(rid, data)
        markup = kb_result(rid, data)

        body = build_text_report(data)
        chunks = split_html_lines(body, limit=3800)
        await safe_edit_or_send(
            msg,
            note,
            chunks[0],
            reply_markup=markup if len(chunks) == 1 else None,
            edit_first=True,
        )
        for part in chunks[1:-1]:
            await safe_edit_or_send(msg, note, part)
        if len(chunks) > 1:
            await safe_edit_or_send(msg, note, chunks[-1], reply_markup=markup)
    except Exception as exc:
        log.exception("Ошибка поиска '%s': %s", q, exc)
        await note.edit_text(
            f"❌ Ошибка при формировании отчета: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
            reply_markup=kb_main(),
        )


@dp.callback_query(F.data == "check_sub")
async def on_check_sub(cb: CallbackQuery, state: FSMContext):
    if not cb.from_user:
        return
    if await is_user_subscribed(cb.from_user.id):
        await ack_cb(cb, "Подписка подтверждена ✅")
        create_user(cb.from_user.id, cb.from_user.username)
        await cb.message.edit_text(START_TEXT, parse_mode="HTML", reply_markup=kb_main())
    else:
        await ack_cb(cb, "Подписка не найдена. Вступи в канал и нажми снова.", alert=True)


@dp.message(CommandStart())
async def on_start(msg: Message, state: FSMContext):
    await state.clear()
    if check_banned(msg.from_user.id):
        return await msg.answer("⛔ Доступ ограничен.")
    if not await require_sub_msg(msg):
        return
    create_user(msg.from_user.id, msg.from_user.username)
    banner = await msg.answer(PIN_BANNER_TEXT, parse_mode="HTML", disable_web_page_preview=True)
    try:
        await bot.pin_chat_message(chat_id=msg.chat.id, message_id=banner.message_id, disable_notification=True)
    except Exception:
        pass
    await asyncio.sleep(0.5)
    await msg.answer(START_TEXT, parse_mode="HTML", reply_markup=kb_main())


@dp.callback_query(F.data == "search_info")
async def on_search_info(cb: CallbackQuery, state: FSMContext):
    if not await require_sub_cb(cb):
        return
    await state.clear()
    await cb.message.edit_text(
        "Select an analysis type.",
        reply_markup=kb_search_types(),
    )
    await ack_cb(cb)


@dp.callback_query(F.data.startswith("stype_"))
async def on_select_search_type(cb: CallbackQuery, state: FSMContext):
    if not await require_sub_cb(cb):
        return
    mode_key = cb.data.replace("stype_", "", 1)
    mode_info = SEARCH_MODES.get(mode_key)
    if not mode_info:
        return await ack_cb(cb, "Неизвестный тип", alert=True)

    label, _, prompt_txt = mode_info
    await state.set_state(States.search_typed)
    await state.update_data(search_mode=mode_key)
    await cb.message.edit_text(
        f"🔎 <b>{html.escape(label)}</b>\n\n{html.escape(prompt_txt)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[red_btn("Back", callback_data="search_info")]]
        ),
    )
    await ack_cb(cb)


@dp.message(States.search_typed)
async def handle_typed_search(msg: Message, state: FSMContext):
    data = await state.get_data()
    mode_key = data.get("search_mode")
    await state.clear()
    q = (msg.text or "").strip()
    if not q:
        return await msg.answer("❌ Пустой запрос.", reply_markup=kb_main())

    mode_info = SEARCH_MODES.get(str(mode_key))
    forced_qt = mode_info[1] if mode_info else None

    if mode_key == "username" and not q.startswith("@") and not re.fullmatch(r"\d+", q):
        q = "@" + q
    elif mode_key in ("inn", "snils", "passport"):
        q = f"{mode_key.upper()}: {q}"

    await execute_search_flow(msg, q, forced_qtype=forced_qt)


@dp.message(Command("newapi"))
async def on_cmd_newapi(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    parts = (msg.text or "").strip().split()
    req_lim = 0
    rpm = 70
    if len(parts) > 1:
        try:
            req_lim = max(0, int(parts[1]))
        except ValueError:
            return await msg.answer("❌ Формат: <code>/newapi [лимит_запросов] [запросов_в_мин]</code>", parse_mode="HTML")
    if len(parts) > 2:
        try:
            rpm = max(1, int(parts[2]))
        except ValueError:
            return await msg.answer("❌ Лимит в минуту должен быть числом.", parse_mode="HTML")
    item = create_oasis_api_key(owner=f"tg:{msg.from_user.id}", requests_limit=req_lim, rate_limit=rpm)
    await msg.answer(format_api_key_card(item), parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("apilist"))
async def on_cmd_apilist(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    items = list_oasis_api_keys()
    if not items:
        return await msg.answer("📭 Ключей Oasis API пока нет. Создайте через /newapi")
    state_str = "ВКЛ ✅" if is_api_enabled() else "ВЫКЛ ⛔"
    lines = [f"🔑 <b>Ключи Oasis API</b> (общий статус API: <b>{state_str}</b>):\n"]
    for it in items[:30]:
        st = "✅ Активен" if int(it.get("is_active", 1)) == 1 else "⛔ Отозван"
        lim = int(it.get("requests_limit") or 0)
        used = int(it.get("requests_used") or 0)
        lim_txt = "∞" if lim <= 0 else f"{used}/{lim}"
        lines.append(
            f"• <code>{html.escape(str(it['api_key']))}</code>\n"
            f"  Статус: {st} | Запросы: {lim_txt} | Скорость: {it.get('rate_limit', 70)}/мин"
        )
    await msg.answer("\n".join(lines)[:4000], parse_mode="HTML")


@dp.message(Command("delapi"))
async def on_cmd_delapi(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    parts = (msg.text or "").strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return await msg.answer("❌ Укажите ключ: <code>/delapi OASIS-IO-...</code>", parse_mode="HTML")
    key_to_revoke = parts[1].strip()
    if revoke_oasis_api_key(key_to_revoke):
        await msg.answer(f"🗑️ Ключ отозван:\n<code>{html.escape(key_to_revoke)}</code>", parse_mode="HTML")
    else:
        await msg.answer("⚠️ Ключ не найден.", parse_mode="HTML")


@dp.callback_query(F.data == "adm_make_api")
async def on_adm_make_api(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    await cb.message.edit_text(
        "🔑 <b>Создание ключа Oasis API</b>\n\n"
        "Введите: <code>ЛИМИТ_ЗАПРОСОВ ЛИМИТ_В_МИНУТУ</code>\n"
        "• <code>0</code> — безлимит запросов\n"
        "• Пример: <code>500 70</code> (500 запросов, 70/мин)\n"
        "• Пример: <code>0 70</code> (безлимит, 70/мин)",
        parse_mode="HTML",
        reply_markup=kb_back(),
    )
    await state.set_state(States.adm_make_api)
    await ack_cb(cb)


@dp.message(States.adm_make_api)
async def handle_adm_make_api(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    parts = (msg.text or "").strip().split()
    if not parts:
        return await msg.answer("❌ Укажите лимит запросов.", reply_markup=kb_admin())
    try:
        req_lim = max(0, int(parts[0]))
        rpm = max(1, int(parts[1])) if len(parts) > 1 else 70
    except ValueError:
        return await msg.answer("❌ Введите числа. Пример: <code>100 70</code> или <code>0 70</code>", parse_mode="HTML", reply_markup=kb_admin())

    item = create_oasis_api_key(owner=f"tg:{msg.from_user.id}", requests_limit=req_lim, rate_limit=rpm)
    await msg.answer(format_api_key_card(item), parse_mode="HTML", disable_web_page_preview=True)
    await msg.answer("🛠️ <b>Админ-панель</b>", parse_mode="HTML", reply_markup=kb_admin())


@dp.callback_query(F.data == "adm_toggle_api")
async def on_adm_toggle_api(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    new_state = not is_api_enabled()
    set_api_enabled(new_state)
    status_msg = "Oasis API включён ✅" if new_state else "Oasis API отключён ⛔"
    await cb.message.edit_text(f"🛠️ <b>Админ-панель</b>\n\n{status_msg}", parse_mode="HTML", reply_markup=kb_admin())
    await ack_cb(cb, status_msg)


@dp.callback_query(F.data == "adm_toggle_sub")
async def on_adm_toggle_sub(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    new_state = not is_sub_enabled()
    set_sub_enabled(new_state)
    msg_txt = "Обязательная подписка включена ✅" if new_state else "Обязательная подписка выключена ⛔"
    await cb.message.edit_text(f"🛠️ <b>Админ-панель</b>\n\n{msg_txt}", parse_mode="HTML", reply_markup=kb_admin())
    await ack_cb(cb, msg_txt)


@dp.callback_query(F.data == "adm_add_sub")
async def on_adm_add_sub(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    await cb.message.edit_text(
        "➕ <b>Добавление канала в обязательную подписку</b>\n\n"
        "Отправь строку в формате:\n"
        "<code>ID_КАНАЛА | ССЫЛКА | НАЗВАНИЕ</code>\n\n"
        "Пример:\n"
        "<code>-1002233445566 | https://t.me/+InviteLink | Канал друга</code>",
        parse_mode="HTML",
        reply_markup=kb_back(),
    )
    await state.set_state(States.adm_add_sub)
    await ack_cb(cb)


@dp.message(States.adm_add_sub)
async def handle_adm_add_sub(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    raw = (msg.text or "").strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2:
        return await msg.answer(
            "❌ Неверный формат. Нужно: <code>ID_КАНАЛА | ССЫЛКА | НАЗВАНИЕ</code>",
            parse_mode="HTML",
            reply_markup=kb_admin(),
        )
    try:
        chat_id = int(parts[0])
    except ValueError:
        return await msg.answer("❌ ID канала должен быть числом (например -1001234567890).", reply_markup=kb_admin())

    url = parts[1]
    if not url.startswith(("https://t.me/", "http://t.me/", "https://telegram.me/")):
        return await msg.answer("❌ Ссылка должна вести на Telegram (https://t.me/...).", reply_markup=kb_admin())

    title = parts[2] if len(parts) >= 3 and parts[2] else "Обязательный канал"
    add_required_channel(chat_id=chat_id, url=url, title=title)
    await msg.answer(
        f"✅ Канал добавлен:\n• Название: <b>{html.escape(title)}</b>\n• ID: <code>{chat_id}</code>\n• Ссылка: {html.escape(url)}",
        parse_mode="HTML",
        reply_markup=kb_admin(),
    )


@dp.callback_query(F.data == "adm_list_sub")
async def on_adm_list_sub(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    channels = list_required_channels()
    state_txt = "ВКЛ ✅" if is_sub_enabled() else "ВЫКЛ ⛔"
    if not channels:
        txt = f"📋 <b>Каналы подписки</b> (ОП: <b>{state_txt}</b>)\n\nСписок пуст."
    else:
        lines = [f"📋 <b>Каналы подписки</b> (ОП: <b>{state_txt}</b>)\n"]
        for ch in channels:
            lines.append(
                f"• <b>{html.escape(str(ch['title']))}</b>\n"
                f"  ID: <code>{ch['chat_id']}</code> | {html.escape(str(ch['url']))}"
            )
        txt = "\n".join(lines)
    await cb.message.edit_text(txt[:4000], parse_mode="HTML", reply_markup=kb_admin())
    await ack_cb(cb)


@dp.callback_query(F.data == "adm_del_sub")
async def on_adm_del_sub(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    channels = list_required_channels()
    hint = "\n".join(f"• <code>{c['chat_id']}</code> — {html.escape(str(c['title']))}" for c in channels) or "Список пуст"
    await cb.message.edit_text(
        f"🗑️ <b>Удаление канала из подписки</b>\n\nОтправь <code>ID_КАНАЛА</code> для удаления:\n\n{hint}",
        parse_mode="HTML",
        reply_markup=kb_back(),
    )
    await state.set_state(States.adm_del_sub)
    await ack_cb(cb)


@dp.message(States.adm_del_sub)
async def handle_adm_del_sub(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    raw = (msg.text or "").strip()
    try:
        chat_id = int(raw)
    except ValueError:
        return await msg.answer("❌ Введи числовой ID канала.", reply_markup=kb_admin())

    if delete_required_channel(chat_id):
        await msg.answer(f"🗑️ Канал <code>{chat_id}</code> удалён из обязательной подписки.", parse_mode="HTML", reply_markup=kb_admin())
    else:
        await msg.answer("⚠️ Канал с таким ID не найден в списке.", reply_markup=kb_admin())


@dp.callback_query(F.data == "back_to_start")
async def on_back(cb: CallbackQuery, state: FSMContext):
    if not await require_sub_cb(cb):
        return
    await state.clear()
    await cb.message.edit_text(START_TEXT, parse_mode="HTML", reply_markup=kb_main())
    await ack_cb(cb)


@dp.callback_query(F.data == "profile")
async def on_profile(cb: CallbackQuery):
    if not await require_sub_cb(cb):
        return
    user = fetch_user(cb.from_user.id)
    tenure = get_tenure(user[4]) if user else "0 дн."
    txt = (
        f"👤 <b>Профиль</b>\n\n🆔 ID: <code>{cb.from_user.id}</code>\n"
        f"🔎 Запросов: <b>{user[2] if user else 0}</b>\n"
        f"📅 Дата регистрации: {user[4] if user else '-'}\n"
        f"⏳ <i>В системе: {tenure}</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [red_btn("Скрытие данных ($5)", callback_data="hide_data_menu")],
            [red_btn("Back", callback_data="back_to_start")],
        ]
    )
    await cb.message.edit_text(txt, parse_mode="HTML", reply_markup=kb)
    await ack_cb(cb)


@dp.callback_query(F.data == "hide_data_menu")
async def on_hide_menu(cb: CallbackQuery):
    if not await require_sub_cb(cb):
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [red_btn("Оплатить Tonkeeper", callback_data="pay_tonkeeper")],
            [red_btn("Back", callback_data="profile")],
        ]
    )
    await cb.message.edit_text(HIDE_DATA_TEXT, parse_mode="HTML", reply_markup=kb)
    await ack_cb(cb)


@dp.callback_query(F.data == "pay_tonkeeper")
async def on_pay_ton(cb: CallbackQuery):
    if not await require_sub_cb(cb):
        return
    memo = f"Oasis_{cb.from_user.id}"
    url = f"ton://transfer/{TON_WALLET}?amount=13040000000&text={memo}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [red_btn("Оплатить в 1 клик", url=url)],
            [red_btn("Back", callback_data="hide_data_menu")],
        ]
    )
    await cb.message.edit_text(
        f"💎 <b>Оплата через TON</b>\n\n👛 Кошелек: <code>{TON_WALLET}</code>\n📝 Memo: <code>{memo}</code>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await ack_cb(cb)


@dp.callback_query(F.data == "buy_requests")
async def on_buy(cb: CallbackQuery):
    if not await require_sub_cb(cb):
        return
    await cb.message.edit_text(
        "💳 <b>Покупка запросов</b>\nПо вопросам пополнения: " + ADMIN_USERNAME,
        parse_mode="HTML",
        reply_markup=kb_back(),
    )
    await ack_cb(cb)


@dp.callback_query(F.data == "ref_link")
async def on_ref(cb: CallbackQuery):
    if not await require_sub_cb(cb):
        return
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={cb.from_user.id}"
    await cb.message.edit_text(
        f"👥 <b>Реферальная ссылка:</b>\n\n<code>{link}</code>",
        parse_mode="HTML",
        reply_markup=kb_back(),
    )
    await ack_cb(cb)


@dp.callback_query(F.data == "support")
async def on_support(cb: CallbackQuery, state: FSMContext):
    if not await require_sub_cb(cb):
        return
    await state.set_state(States.support_msg)
    await cb.message.edit_text(SUPPORT_TEXT, parse_mode="HTML", reply_markup=kb_back())
    await ack_cb(cb)


@dp.message(States.support_msg)
async def handle_support(msg: Message, state: FSMContext):
    await state.clear()
    try:
        await bot.send_message(
            MAIN_ADMIN_ID,
            f"📩 Сообщение от {html.escape(msg.from_user.full_name)} (<code>{msg.from_user.id}</code>):\n\n{html.escape(msg.text or '')}",
            parse_mode="HTML",
        )
        await msg.answer("✅ Отправлено.", reply_markup=kb_main())
    except Exception:
        await msg.answer("❌ Ошибка отправки.", reply_markup=kb_main())


@dp.callback_query(F.data == "use_promo")
async def on_use_promo(cb: CallbackQuery, state: FSMContext):
    if not await require_sub_cb(cb):
        return
    await state.set_state(States.promo_code)
    await cb.message.edit_text(
        "🎟️ <b>Активация промокода / чека</b>\n\nВведите код сообщением:",
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
            return await msg.answer("❌ Промокод не найден.", reply_markup=kb_main())

        if conn.execute(
            "SELECT 1 FROM promo_activations WHERE user_id = ? AND code = ?",
            (uid, code),
        ).fetchone():
            return await msg.answer("⚠️ Промокод уже активирован.", reply_markup=kb_main())

        if promo["uses_count"] >= promo["max_uses"]:
            return await msg.answer("⚠️ Лимит использований исчерпан.", reply_markup=kb_main())

        conn.execute("UPDATE promos SET uses_count = uses_count + 1 WHERE code = ?", (code,))
        conn.execute(
            "INSERT INTO promo_activations (user_id, code) VALUES (?, ?)",
            (uid, code),
        )
        conn.commit()

    give_requests(uid, promo["reward"])
    await msg.answer(
        f"✅ Начислено <b>+{promo['reward']} запросов</b>.",
        parse_mode="HTML",
        reply_markup=kb_main(),
    )


@dp.message(Command("admin"))
async def on_admin(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    await msg.answer("🛠️ <b>Админ-панель</b>", parse_mode="HTML", reply_markup=kb_admin())


@dp.callback_query(F.data == "adm_promo")
async def on_adm_promo(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    await cb.message.edit_text(
        "🎟️ <b>Создание промокода</b>\n\nФормат: <code>КОД НАГРАДА ЛИМИТ</code>",
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
        return await msg.answer("❌ Неверный формат.", reply_markup=kb_admin())
    try:
        code, reward = parts[0], int(parts[1])
        limit_uses = int(parts[2]) if len(parts) > 2 else 1
    except ValueError:
        return await msg.answer("❌ Награда и лимит должны быть числами.", reply_markup=kb_admin())

    with db_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO promos (code, reward, max_uses, uses_count, created_by) VALUES (?, ?, ?, 0, ?)",
                (code, reward, limit_uses, msg.from_user.id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return await msg.answer("⚠️ Такой код уже существует.", reply_markup=kb_admin())

    await msg.answer(
        f"✅ Промокод <code>{html.escape(code)}</code> на +{reward} запросов создан.",
        parse_mode="HTML",
        reply_markup=kb_admin(),
    )


@dp.callback_query(F.data == "adm_check")
async def on_adm_check(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    await cb.message.edit_text(
        "🧾 <b>Создание чека</b>\n\nВведите номинал (запросов):",
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
        return await msg.answer("❌ Введите число.", reply_markup=kb_admin())

    code = f"CHECK-{uuid.uuid4().hex[:8].upper()}"
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO promos (code, reward, max_uses, uses_count, created_by) VALUES (?, ?, 1, 0, ?)",
            (code, reward, msg.from_user.id),
        )
        conn.commit()

    await msg.answer(
        f"✅ Чек создан: <code>{code}</code> на <b>{reward}</b> запросов.",
        parse_mode="HTML",
        reply_markup=kb_admin(),
    )


@dp.callback_query(F.data == "adm_test_api")
async def on_adm_test_api(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await ack_cb(cb, "Нет доступа", alert=True)
    await cb.message.edit_text(
        "🧪 <b>Тест API (Raw)</b>\n\nВведите запрос (таймаут 90 сек):",
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
        f"⏳ Запрос к Black-Box и Argus (до 90с): <b>{html.escape(q)}</b>...",
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
    note = await cb.message.edit_text("📡 Проверка Black-Box и Argus...", parse_mode="HTML")
    results = await check_apis()
    lines = [
        f"• <b>{r['name']}</b>: {'✅ OK' if r['ok'] else '❌ FAIL'} ({r['status']}, {r['ms']}ms)"
        for r in results
    ]
    await note.edit_text("📡 <b>Статус API:</b>\n\n" + "\n".join(lines), parse_mode="HTML", reply_markup=kb_admin())
    await ack_cb(cb)


@dp.callback_query(F.data.startswith("ai_analyze_"))
async def on_analyze(cb: CallbackQuery):
    if not await require_sub_cb(cb):
        return
    rid = normalize_rid(cb.data.replace("ai_analyze_", "", 1))
    data = read_report(rid)
    if not data:
        return await ack_cb(cb, "Отчет не найден", alert=True)

    await ack_cb(cb, "Анализ...")
    summary = make_local_analysis(data)
    store_report(rid, data)
    txt = f"🎯 <b>Цель:</b> <code>{html.escape(str(data.get('query') or ''))}</code>\n\n{summary}"
    await cb.message.answer(txt[:4000], parse_mode="HTML")


@dp.callback_query(F.data.startswith("get_txt_"))
async def on_export_txt(cb: CallbackQuery):
    if not await require_sub_cb(cb):
        return
    rid = normalize_rid(cb.data.replace("get_txt_", "", 1))
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
        f"ИНН: {data.get('inn', 'Нет')}\n"
        f"Паспорт: {data.get('passport', 'Нет')}\n"
        f"СНИЛС: {data.get('snils', 'Нет')}\n"
        f"Адреса: {', '.join(data.get('addresses') or []) or 'Нет'}\n"
        f"Транспорт: {', '.join(data.get('vehicles') or []) or 'Нет'}\n"
        f"Соцсети: {', '.join(data.get('social_links') or []) or 'Нет'}\n"
        f"Регистрация: {data.get('reg_date', 'Не найдено')}\n"
        f"Оператор / Регион: {data.get('operator', 'Нет')} / {data.get('region', 'Нет')}"
    )
    doc = BufferedInputFile(txt.encode("utf-8"), filename=f"oasis_{rid}.txt")
    await cb.message.answer_document(document=doc, caption="📄 <b>TXT Отчет</b>", parse_mode="HTML")
    await ack_cb(cb)


@dp.callback_query(F.data.startswith("get_json_"))
async def on_export_json(cb: CallbackQuery):
    if not await require_sub_cb(cb):
        return
    rid = normalize_rid(cb.data.replace("get_json_", "", 1))
    data = read_report(rid)
    if not data:
        return await ack_cb(cb, "Отчет не найден", alert=True)

    obj = {
        "report_id": rid,
        "query": data.get("query"),
        "query_type": data.get("query_type"),
        "date": data.get("date"),
        "phone_number": data.get("phone_number"),
        "phones": list(data.get("phones_set") or []),
        "emails": list(data.get("emails_set") or []),
        "fio": data.get("fio"),
        "dob": data.get("dob"),
        "age": data.get("age"),
        "inn": data.get("inn"),
        "passport": data.get("passport"),
        "snils": data.get("snils"),
        "addresses": data.get("addresses"),
        "vehicles": data.get("vehicles"),
        "social_links": data.get("social_links"),
        "reg_date": data.get("reg_date"),
        "phonebooks": data.get("phonebooks"),
        "name_history": data.get("name_history"),
        "extra_kv": data.get("extra_kv"),
        "tg_info": data.get("tg_info"),
        "databases": data.get("databases"),
        "raw_response": data.get("raw_response"),
    }
    raw = json.dumps(obj, ensure_ascii=False, indent=2)
    doc = BufferedInputFile(raw.encode("utf-8"), filename=f"oasis_{rid}.json")
    await cb.message.answer_document(document=doc, caption="🗂️ <b>JSON Отчет</b>", parse_mode="HTML")
    await ack_cb(cb)


@dp.message(F.text & ~F.text.startswith("/"))
async def on_search(msg: Message, state: FSMContext):
    if msg.from_user.is_bot or await state.get_state() is not None:
        return
    q = (msg.text or "").strip()
    if not q:
        return
    await execute_search_flow(msg, q)


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
