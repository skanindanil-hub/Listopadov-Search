import asyncio
import logging
import os
import re
import json
import uuid
import sqlite3
import aiohttp
from datetime import datetime

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

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9273738144:AAF_C8mNW5yRqpCcACaqLbvXLFdecy-t1cQ")
MAIN_ADMIN_ID = 8394631079
WEBHOOK_HOST = "https://listopadov-search.onrender.com"
DB_FILE = "oasis.db"
USERS_DB = "users.db"

TON_WALLET = "UQA7k7FbUJXC1ko40uBYfvv-jZ4WGnEiEIVsFcffXtFOU3pM"
STICKER_ID = "CAACAgIAAxkBAAFXzjhq-itNMdvQX03_x8hhB9NUezaSiAACFyUAAtkoAAFL79-L7Fw-Jlc9BA"

# Mobius
API_URL_MOBIUS = "https://mobiusnet.cc.cd/api/v1/search"
API_KEY_MOBIUS = os.getenv("MOBIUS_API_KEY", "MOBIUS-IO-IW5w3b4xKEY_dS8Kbt4FJswFEsTR97aGKhSEgfrJFjk")

# Black-Box
API_URL_BLACKBOX = "https://api.black-box.cc"
API_KEY_BLACKBOX = os.getenv("BLACKBOX_API_KEY", "8jdsYcPZiQMTpYNCluXg9scNhc7y")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("oasis")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
REPORTS_CACHE = {}

TG_USERNAME_RE = re.compile(r"[a-zA-Z0-9_]{5,32}")
TG_ID_RE = re.compile(r"\d{5,12}")


class SearchStates(StatesGroup):
    wait_partial_field = State()
    wait_support_msg = State()
    wait_mirror_token = State()
    wait_admin_give = State()
    wait_admin_take = State()
    wait_admin_ban = State()
    wait_admin_broadcast = State()
    wait_admin_raw = State()


# --- БАЗА ЮЗЕРОВ И ЗЕРКАЛ ---
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
        CREATE TABLE IF NOT EXISTS mirrors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_username TEXT UNIQUE,
            added_by INTEGER,
            added_at TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
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


# --- ТЕКСТЫ ---
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

PARTIAL_TEXT = """📄 <b>Поиск по неполным данным</b>
Вы можете указать любое количество данных.
Нажмите на поле и введите значение. Когда всё готово — нажмите «Поиск»."""

SUPPORT_TEXT = """🆘 <b>Связь с нами</b>
Пожалуйста, напишите ваше сообщение прямо в чат."""

MIRROR_TEXT = """🔧 <b>Инструкция по созданию бота:</b>
1. Перейди в @BotFather
2. Нажми «Start» или напиши /newbot
3. Введи имя для бота
4. Придумай username, оканчивающийся на Bot
5. Скопируй API Token и отправь его сюда."""

HIDE_DATA_TEXT = """🛡️ <b>Скрытие данных</b>

Скрытие данных позволяет навсегда исключить ваши персональные данные (телефон, паспорт, авто, username и т.д.) из поисковой выдачи бота.

💳 <b>Стоимость скрытия данных: $7 (навсегда)</b>"""


# --- КЛАВИАТУРЫ ---
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поиск", callback_data="search_info", style="primary")],
        [InlineKeyboardButton(text="📄 Поиск по неполным данным", callback_data="search_partial")],
        [InlineKeyboardButton(text="💎 Купить запросы", callback_data="buy_requests", style="primary")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🔗 Рефералка", callback_data="ref_link"), InlineKeyboardButton(text="🤖 Зеркало", callback_data="create_mirror")],
        [InlineKeyboardButton(text="🆘 Связь с нами", callback_data="support")]
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]])


def result_kb(report_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Веб-отчет (Граф)", url=f"{WEBHOOK_HOST}/report/{report_id}", style="primary")],
        [
            InlineKeyboardButton(text="📄 Скачать TXT", callback_data=f"get_txt_{report_id}"),
            InlineKeyboardButton(text="📦 Скачать JSON", callback_data=f"get_json_{report_id}")
        ],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")]
    ])


def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 Тест API (Raw)", callback_data="adm_test_api")],
        [InlineKeyboardButton(text="🔌 Проверка API", callback_data="adm_check_apis", style="primary")],
        [InlineKeyboardButton(text="➕ Выдать", callback_data="adm_give", style="success"), InlineKeyboardButton(text="➖ Забрать", callback_data="adm_take", style="danger")],
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="adm_stats"), InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm_promo")],
        [InlineKeyboardButton(text="🚫 Бан", callback_data="adm_ban"), InlineKeyboardButton(text="🛠 Тех. перерыв", callback_data="adm_maintenance")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")]
    ])


def partial_search_kb(data: dict):
    def short(val):
        if not val:
            return "..."
        return val[:10] + "..." if len(val) > 10 else val

    country = data.get("country") or "Не выбрана"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Фамилия: {short(data.get('surname'))}", callback_data="field_surname"), InlineKeyboardButton(text=f"Имя: {short(data.get('name'))}", callback_data="field_name")],
        [InlineKeyboardButton(text=f"Отчество: {short(data.get('patronymic'))}", callback_data="field_patronymic")],
        [InlineKeyboardButton(text=f"Дата рожд.: {short(data.get('birth_date'))}", callback_data="field_birth_date"), InlineKeyboardButton(text=f"Страна: {country}", callback_data="select_country")],
        [InlineKeyboardButton(text="Очистить", callback_data="clear_fields", style="danger"), InlineKeyboardButton(text="Поиск", callback_data="do_partial_search", style="primary")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]
    ])


def country_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 RU", callback_data="set_country_RU"), InlineKeyboardButton(text="🇺🇦 UA", callback_data="set_country_UA"), InlineKeyboardButton(text="🇰🇿 KZ", callback_data="set_country_KZ"), InlineKeyboardButton(text="🇧🇾 BY", callback_data="set_country_BY")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_partial")]
    ])


# --- API ЗАПРОСЫ ---
async def fetch_mobius(q: str):
    headers = {"X-API-Key": API_KEY_MOBIUS, "Content-Type": "application/json"}
    payload = {"query": q, "type": "auto"}
    timeout = aiohttp.ClientTimeout(total=90)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(API_URL_MOBIUS, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                body = await resp.text()
                log.warning("Mobius HTTP %s: %s", resp.status, body[:500])
    except Exception:
        log.exception("Mobius request failed for query=%r", q)
    return None


async def fetch_blackbox(q: str):
    headers = {"X-API-Key": API_KEY_BLACKBOX, "Accept": "application/json"}
    params = {"q": q, "type": "auto"}
    timeout = aiohttp.ClientTimeout(total=90)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{API_URL_BLACKBOX}/v1.1/search", headers=headers, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                body = await resp.text()
                log.warning("BlackBox HTTP %s: %s", resp.status, body[:500])
    except Exception:
        log.exception("BlackBox request failed for query=%r", q)
    return None


# --- ПРОВЕРКА API ---
async def check_api_health(name, url, method="GET", headers=None, payload=None):
    timeout = aiohttp.ClientTimeout(total=20)
    start = asyncio.get_event_loop().time()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if method == "GET":
                req = session.get(url, headers=headers, params=payload)
            else:
                req = session.post(url, headers=headers, json=payload)
            async with req as resp:
                elapsed = int((asyncio.get_event_loop().time() - start) * 1000)
                status = resp.status
                text = await resp.text()
                preview = text[:200].replace("\n", " ")
                return {"name": name, "healthy": 200 <= status < 300, "status": status, "latency_ms": elapsed, "preview": preview}
    except asyncio.TimeoutError:
        return {"name": name, "healthy": False, "status": "TIMEOUT", "latency_ms": int((asyncio.get_event_loop().time() - start) * 1000), "preview": "Превышено время ожидания"}
    except Exception as e:
        return {"name": name, "healthy": False, "status": "ERROR", "latency_ms": int((asyncio.get_event_loop().time() - start) * 1000), "preview": str(e)[:200]}


async def run_all_api_checks():
    checks = [
        check_api_health(
            "Mobius",
            API_URL_MOBIUS,
            method="POST",
            headers={"X-API-Key": API_KEY_MOBIUS, "Content-Type": "application/json"},
            payload={"query": "79999999999", "type": "auto"}
        ),
        check_api_health(
            "Black-Box",
            f"{API_URL_BLACKBOX}/v1.1/search",
            method="GET",
            headers={"X-API-Key": API_KEY_BLACKBOX},
            payload={"q": "+79999999999", "type": "phone"}
        ),
    ]
    return await asyncio.gather(*checks)


# --- СУПЕР-ПАРСЕР ---
async def search_everywhere(q: str):
    clean_q = q.strip().replace("@", "")

    is_tg_id = bool(TG_ID_RE.fullmatch(clean_q))
    is_tg_username = bool(TG_USERNAME_RE.fullmatch(clean_q)) and not clean_q.isdigit()

    if os.path.exists(DB_FILE) and (is_tg_id or is_tg_username):
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            if is_tg_id:
                cursor.execute("SELECT tg_id, username, phone, fio FROM tg_entities WHERE tg_id = ? LIMIT 1", (clean_q,))
            else:
                cursor.execute("SELECT tg_id, username, phone, fio FROM tg_entities WHERE LOWER(username) = ? LIMIT 1", (clean_q.lower(),))
            row = cursor.fetchone()
            conn.close()
            if row:
                return {
                    "query": q, "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
                    "fio": row[3] or "Нет", "phone_main": row[2] or "Нет", "username": row[1] or "Нет",
                    "operator": "-", "region": "-", "country": "-",
                    "emails": [], "addresses": [], "phones": [row[2]] if row[2] else [],
                    "dob": "-", "passport": "-", "inn": "-", "snils": "-",
                    "sources_list": [{"name": "Local DB", "count": 1}],
                    "stats": {"sources": 1, "phones": 1, "records": 1},
                    "completeness": "20%"
                }
        except Exception:
            log.exception("Local DB lookup failed for %r", clean_q)

    mobius_task = fetch_mobius(q)
    blackbox_task = fetch_blackbox(q)
    res_mobius, res_blackbox = await asyncio.gather(mobius_task, blackbox_task)

    final_data = {
        "query": q, "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "fio": "Нет", "phone_main": q, "username": "Нет",
        "operator": "Нет", "region": "Нет", "country": "Нет",
        "emails": [], "addresses": [], "phones": [],
        "dob": "Нет", "passport": "Нет", "inn": "Нет", "snils": "Нет",
        "activity": {},
        "sources_list": [], "stats": {"sources": 0, "phones": 0, "records": 0}
    }
    found_something = False
    debug_info = []

    # --- MOBIUS ---
    if not res_mobius:
        debug_info.append("Mobius: нет ответа")
    elif not res_mobius.get("success"):
        debug_info.append(f"Mobius: {res_mobius.get('message', 'Unknown')}")
    else:
        results = res_mobius.get("results", {})

        kratos_data = results.get("mobius-kratos-phone", {}).get("result", {}).get("data", {}).get("results", [])
        if kratos_data:
            found_something = True
            final_data["stats"]["records"] = len(kratos_data)
            final_data["stats"]["sources"] = len(kratos_data)

            for record in kratos_data:
                for k, v in record.items():
                    k_low = k.lower()
                    val_str = str(v).strip("'")
                    if not val_str or val_str == "None":
                        continue

                    if "фио" in k_low or k_low in ("имя", "фамилия", "отчество"):
                        if final_data["fio"] == "Нет" or len(val_str) > len(final_data["fio"]):
                            final_data["fio"] = val_str
                    elif "паспорт" in k_low and "серия" not in k_low and final_data["passport"] == "Нет":
                        final_data["passport"] = val_str
                    elif "снилс" in k_low and final_data["snils"] == "Нет":
                        final_data["snils"] = val_str
                    elif "инн" in k_low and final_data["inn"] == "Нет":
                        final_data["inn"] = val_str
                    elif "дата рождения" in k_low and final_data["dob"] == "Нет":
                        final_data["dob"] = val_str
                    elif "адрес" in k_low:
                        if val_str not in final_data["addresses"]:
                            final_data["addresses"].append(val_str)
                    elif "телефон" in k_low and final_data["phone_main"] == q:
                        final_data["phone_main"] = val_str
                        if val_str not in final_data["phones"]:
                            final_data["phones"].append(val_str)

            final_data["sources_list"].append({"name": "Mobius (Kratos)", "count": len(kratos_data)})

        nebula_raw = results.get("mobius-nebula-phone", {}).get("result", {}).get("data", {}).get("results", {})
        if nebula_raw:
            found_something = True

            phone_info = nebula_raw.get("phone_info", {}) or {}
            if phone_info.get("operator"):
                final_data["operator"] = phone_info["operator"]
            if phone_info.get("region"):
                final_data["region"] = phone_info["region"]
            if phone_info.get("country"):
                final_data["country"] = phone_info["country"]

            if final_data["phone_main"] in (q, "Нет") and phone_info.get("phone"):
                final_data["phone_main"] = phone_info["phone"]
            if final_data["phone_main"] and final_data["phone_main"] not in final_data["phones"]:
                final_data["phones"].insert(0, final_data["phone_main"])

            for em in nebula_raw.get("emails", []) or []:
                if em and em not in final_data["emails"]:
                    final_data["emails"].append(em)

            for ad in nebula_raw.get("addresses", []) or []:
                if isinstance(ad, dict):
                    ad = ad.get("address") or ad.get("value") or str(ad)
                if ad and ad not in final_data["addresses"]:
                    final_data["addresses"].append(ad)

            for pb in nebula_raw.get("phonebooks", []) or []:
                name = pb.get("name") if isinstance(pb, dict) else str(pb)
                if name:
                    if final_data["fio"] in ("Нет", final_data["fio"]):
                        final_data["fio"] = name
                    elif name not in final_data["fio"]:
                        final_data["fio"] = f"{final_data['fio']}; {name}"

            if nebula_raw.get("dob") and final_data["dob"] == "Нет":
                final_data["dob"] = nebula_raw["dob"]

            act = nebula_raw.get("activity") or {}
            if isinstance(act, dict):
                final_data["activity"].update(act)

            final_data["sources_list"].append({"name": "Mobius (Nebula)", "count": 1})

    # --- BLACK-BOX ---
    if res_blackbox and res_blackbox.get("status") == "success":
        found_something = True
        profile = res_blackbox.get("profile") or {}
        telecom = res_blackbox.get("telecom") or {}
        identities = res_blackbox.get("identities") or {}
        tg = res_blackbox.get("telegram") or {}
        financial = res_blackbox.get("financial") or {}
        stats_bb = res_blackbox.get("stats") or {}

        if profile.get("primary_fio") and final_data["fio"] in ("Нет", "НЕ НАЙДЕНО"):
            final_data["fio"] = profile["primary_fio"]

        if profile.get("birth_date") and final_data["dob"] in ("Нет", "-"):
            final_data["dob"] = profile["birth_date"]

        for p in profile.get("phones", []) or []:
            if p and p not in final_data["phones"]:
                final_data["phones"].append(p)

        if profile.get("passports") and final_data["passport"] in ("Нет", "-"):
            final_data["passport"] = profile["passports"][0]

        if telecom.get("operator") and final_data["operator"] in ("Нет", "-"):
            final_data["operator"] = telecom["operator"]
        if telecom.get("region") and final_data["region"] in ("Нет", "-"):
            final_data["region"] = telecom["region"]
        if telecom.get("country") and final_data["country"] in ("Нет", "-"):
            final_data["country"] = telecom["country"]

        if tg.get("id") and final_data["username"] in ("Нет", "-"):
            final_data["username"] = f"tg_id:{tg['id']}"

        others_names = identities.get("detected_names", []) or []
        others_dobs = identities.get("detected_dobs", []) or []

        if len(others_names) > 1:
            extra = "; ".join(n for n in others_names if n != final_data["fio"])
            if extra:
                final_data["fio"] = f"{final_data['fio']} (также: {extra})"

        if len(others_dobs) > 1 and final_data["dob"] not in ("Нет", "-"):
            extra_dob = "; ".join(d for d in others_dobs if d != final_data["dob"])
            if extra_dob:
                final_data["dob"] = f"{final_data['dob']} (также: {extra_dob})"

        fin_records = financial.get("records", []) or []
        if fin_records:
            cards = []
            banks = set()
            for rec in fin_records:
                f = (rec.get("field") or "").lower()
                v = rec.get("value") or ""
                if "creditcard" in f:
                    cards.append(v)
                elif f == "bank":
                    banks.add(v)
            if cards:
                final_data["activity"]["cards"] = f"{len(cards)} шт."
            if banks:
                final_data["activity"]["banks"] = "; ".join(sorted(banks))[:200]

        final_data["sources_list"].append({
            "name": "Black-Box",
            "count": stats_bb.get("total_records", 1)
        })
        final_data["stats"]["records"] = max(final_data["stats"]["records"], stats_bb.get("total_records", 0))
    else:
        debug_info.append("Black-Box: нет ответа")

    final_data["stats"]["phones"] = len(final_data["phones"])
    final_data["stats"]["sources"] = len(final_data["sources_list"])

    if found_something:
        filled = sum(1 for k in ("fio", "phone_main", "dob") if final_data[k] not in ("Нет", "-", ""))
        if final_data["emails"]:
            filled += 1
        if final_data["addresses"]:
            filled += 1
        final_data["completeness"] = f"{int((filled / 5) * 100)}%"
        if debug_info:
            final_data["debug"] = "; ".join(debug_info)
        return final_data

    return {
        "query": q, "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "fio": "НЕ НАЙДЕНО", "phone_main": q, "username": "Нет",
        "operator": "-", "region": "-", "country": "-",
        "emails": [], "addresses": [], "phones": [],
        "dob": "-", "passport": "-", "inn": "-", "snils": "-",
        "activity": {},
        "sources_list": [], "stats": {"sources": 0, "phones": 0, "records": 0},
        "completeness": "0%", "debug": "; ".join(debug_info)
    }


# --- ФОРМАТ ОТЧЕТА ---
def format_report_text(data: dict) -> str:
    def tree(label: str, value: str, last: bool = False) -> str:
        branch = "└" if last else "├"
        return f"{branch} {label}: {value}\n"

    parts = []
    q = data.get("query", "—")
    parts.append(f"🔍 <b>Отчёт по запросу:</b> <code>{q}</code>\n")

    phone = data.get("phone_main") or "—"
    operator = data.get("operator") or "—"
    region = data.get("region") or "—"
    country = data.get("country") or "—"
    tg_id = data.get("username", "").replace("tg_id:", "") if data.get("username", "").startswith("tg_id:") else ""
    if phone != "—" or tg_id:
        block = "📞\n"
        if phone != "—":
            block += tree("Телефон", phone)
            block += tree("Оператор", operator)
            block += tree("Регион", region)
            if tg_id:
                block += tree("Страна", country)
                block += tree("Telegram ID", tg_id, last=True)
            else:
                block += tree("Страна", country, last=True)
        elif tg_id:
            block += tree("Telegram ID", tg_id, last=True)
        parts.append(block)

    fio = data.get("fio") or "Нет"
    dob = data.get("dob") or "Нет"
    if fio not in ("Нет", "НЕ НАЙДЕНО"):
        block = "👤 <b>Основные данные</b>\n"
        block += tree("ФИО", fio)
        block += tree("Дата рождения", dob, last=True)
        parts.append(block)
        parts.append(f"🔍 <b>Телефонные книги:</b> {fio}\n")

    emails = data.get("emails") or []
    if emails:
        joined = ", ".join(f'<a href="mailto:{e}">{e}</a>' for e in emails)
        parts.append(f"📧 <b>E-mail:</b> {joined}\n")

    addresses = data.get("addresses") or []
    if addresses:
        parts.append("🏠 <b>Адреса:</b> " + "; ".join(addresses) + "\n")

    cards = data.get("activity", {}).get("cards")
    banks = data.get("activity", {}).get("banks")
    if cards or banks:
        block = "💳 <b>Финансы</b>\n"
        if cards:
            block += tree("Карт найдено", cards, last=not banks)
        if banks:
            block += tree("Банки", banks, last=True)
        parts.append(block)

    activity = data.get("activity") or {}
    extras = {k: v for k, v in activity.items() if k not in ("cards", "banks")}
    if extras:
        block = "📊 <b>Активность:</b>\n"
        items = list(extras.items())
        for i, (k, v) in enumerate(items):
            label = k.replace("_", " ").capitalize()
            block += tree(label, str(v), last=(i == len(items) - 1))
        parts.append(block)

    if not parts[1:]:
        parts.append("❌ Данные не найдены.\n")

    footer = f"\n<i>Источники: {', '.join(s['name'] for s in data.get('sources_list', [])) or '—'}</i>"
    return "".join(parts) + footer


# --- HTML ---
def generate_html_report(data: dict, report_id: str) -> str:
    nodes = [{"id": 1, "label": f"Цель: {data['query']}", "color": "#38bdf8", "font": {"color": "white"}}]
    edges = []
    node_id = 2
    if data.get('fio') and data['fio'] != "Нет":
        nodes.append({"id": node_id, "label": "ФИО", "color": "#4ade80"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('phone_main') and data['phone_main'] != "Нет":
        nodes.append({"id": node_id, "label": "Тел", "color": "#38bdf8"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('operator') and data['operator'] != "Нет":
        nodes.append({"id": node_id, "label": "Оператор", "color": "#facc15"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('emails'):
        nodes.append({"id": node_id, "label": "Email/Банк", "color": "#a78bfa"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('passport') and data['passport'] != "Нет":
        nodes.append({"id": node_id, "label": "Паспорт", "color": "#f87171"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('inn') and data['inn'] != "Нет":
        nodes.append({"id": node_id, "label": "ИНН", "color": "#fbbf24"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    graph_json = json.dumps({"nodes": nodes, "edges": edges})

    email_display = ", ".join(data.get('emails', [])) or "-"
    addr_display = "; ".join(data.get('addresses', [])) or "-"
    cards = data.get('activity', {}).get('cards', '-')
    banks = data.get('activity', {}).get('banks', '-')

    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Oasis Digital - {data['query']}</title>
        <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
            body {{ font-family: 'Inter', sans-serif; background-color: #f5f6f8; color: #1a1a1a; margin: 0; padding: 20px; line-height: 1.5; }}
            .container {{ max-width: 600px; margin: 0 auto; }}
            .header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 20px; color: #888; font-size: 14px; }}
            .header-logo {{ font-weight: 700; color: #000; font-size: 18px; }}
            .profile-card {{ display: flex; align-items: center; gap: 20px; background: #fff; border-radius: 16px; padding: 24px; border: 1px solid #e0e0e0; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.02); }}
            .avatar {{ width: 70px; height: 70px; border-radius: 50%; background: #333; color: #fff; display: flex; align-items: center; justify-content: center; font-size: 24px; font-weight: 600; flex-shrink: 0; }}
            .profile-info h2 {{ margin: 0 0 5px 0; font-size: 22px; font-weight: 700; text-transform: uppercase; }}
            .sub-info {{ color: #888; font-size: 14px; }}
            .badges {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; }}
            .badge {{ background: #fff; border: 1px solid #e0e0e0; padding: 8px 14px; border-radius: 20px; font-size: 14px; color: #333; font-weight: 500; }}
            .completeness {{ margin-bottom: 25px; }}
            .c-header {{ display: flex; justify-content: space-between; font-size: 14px; margin-bottom: 8px; color: #555; }}
            .c-bar {{ height: 8px; background: #e0e0e0; border-radius: 4px; overflow: hidden; }}
            .c-fill {{ height: 100%; background: #333; border-radius: 4px; width: {data['completeness']}; }}
            .graph-section {{ height: 400px; background: #0f172a; border-radius: 12px; margin: 20px 0; border: 1px solid #334155; }}
            .data-section {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 16px; margin-bottom: 15px; padding: 10px 20px; }}
            .data-item {{ display: flex; align-items: center; gap: 15px; padding: 15px 0; border-bottom: 1px solid #f0f0f0; }}
            .data-item:last-child {{ border-bottom: none; }}
            .d-icon {{ font-size: 20px; color: #888; width: 30px; text-align: center; }}
            .d-content {{ flex: 1; }}
            .d-label {{ font-size: 11px; text-transform: uppercase; color: #888; letter-spacing: 1px; margin-bottom: 4px; }}
            .d-value {{ font-size: 16px; font-weight: 500; color: #000; word-break: break-word; }}
            .sources-section {{ margin-top: 30px; }}
            .sources-section h3 {{ font-size: 13px; text-transform: uppercase; color: #888; letter-spacing: 1px; margin-bottom: 15px; }}
            .source-item {{ display: flex; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid #e0e0e0; font-size: 14px; }}
            .source-item:last-child {{ border-bottom: none; }}
            .source-count {{ color: #888; }}
            .footer {{ text-align: center; margin-top: 40px; font-size: 12px; color: #aaa; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="header-logo">OASIS DIGITAL</div>
                <div>/ Report</div>
            </div>

            <div class="profile-card">
                <div class="avatar">{data['fio'][:2] if data['fio'] != 'Нет' else '??'}</div>
                <div class="profile-info">
                    <h2>{data['fio']}</h2>
                    <div class="sub-info">{data['query']} • {data['date']}</div>
                </div>
            </div>

            <div class="badges">
                <div class="badge">{data['stats']['sources']} источников</div>
                <div class="badge">{data['stats']['phones']} телефонов</div>
                <div class="badge">{data['stats']['records']} записей</div>
            </div>

            <div class="completeness">
                <div class="c-header"><span>Полнота профиля</span><span>{data['completeness']}</span></div>
                <div class="c-bar"><div class="c-fill"></div></div>
            </div>

            <div id="mynetwork" class="graph-section"></div>

            <div class="data-section">
                <div class="data-item"><div class="d-icon">📞</div><div class="d-content"><div class="d-label">Телефон</div><div class="d-value">{data['phone_main']}</div></div></div>
                <div class="data-item"><div class="d-icon">📡</div><div class="d-content"><div class="d-label">Оператор / Регион</div><div class="d-value">{data['operator']} / {data['region']}</div></div></div>
                <div class="data-item"><div class="d-icon">🎂</div><div class="d-content"><div class="d-label">Дата рождения</div><div class="d-value">{data['dob']}</div></div></div>
                <div class="data-item"><div class="d-icon">📧</div><div class="d-content"><div class="d-label">Email</div><div class="d-value">{email_display}</div></div></div>
                <div class="data-item"><div class="d-icon">🏠</div><div class="d-content"><div class="d-label">Адрес</div><div class="d-value">{addr_display}</div></div></div>
                <div class="data-item"><div class="d-icon">📄</div><div class="d-content"><div class="d-label">Паспорт</div><div class="d-value">{data['passport']}</div></div></div>
                <div class="data-item"><div class="d-icon">🆔</div><div class="d-content"><div class="d-label">ИНН</div><div class="d-value">{data['inn']}</div></div></div>
                <div class="data-item"><div class="d-icon">🆔</div><div class="d-content"><div class="d-label">СНИЛС</div><div class="d-value">{data['snils']}</div></div></div>
                <div class="data-item"><div class="d-icon">💳</div><div class="d-content"><div class="d-label">Карты / Банки</div><div class="d-value">{cards} / {banks}</div></div></div>
            </div>

            <div class="sources-section">
                <h3>Источники</h3>
                {''.join([f'<div class="source-item"><span>{src["name"]}</span><span class="source-count">{src["count"]}</span></div>' for src in data.get('sources_list', [])])}
            </div>

            <div class="footer">Сгенерировано автоматически • Oasis Digital</div>
        </div>

        <script type="text/javascript">
            var graphData = {graph_json};
            var nodes = new vis.DataSet(graphData.nodes);
            var edges = new vis.DataSet(graphData.edges);
            var container = document.getElementById('mynetwork');
            var data = {{ nodes: nodes, edges: edges }};
            var options = {{
                nodes: {{ shape: 'dot', size: 20, font: {{ size: 14, color: '#ffffff' }}, borderWidth: 2 }},
                edges: {{ color: '#475569', width: 2, smooth: {{ type: 'continuous' }} }},
                physics: {{ barnesHut: {{ gravitationalConstant: -3000, centralGravity: 0.3, springLength: 150 }} }},
                interaction: {{ hover: true }}
            }};
            var network = new vis.Network(container, data, options);
        </script>
    </body>
    </html>
    """


# --- FASTAPI ---
web_app = FastAPI()

web_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@web_app.get("/api/mirrors")
async def get_mirrors():
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT bot_username FROM mirrors WHERE is_active = 1 ORDER BY added_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return {"mirrors": [row[0] for row in rows]}

@web_app.get("/report/{report_id}", response_class=HTMLResponse)
async def web_report(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data:
        return HTMLResponse("<h1>404 Отчет не найден</h1>", status_code=404)
    return HTMLResponse(content=generate_html_report(data, report_id))


async def run_web():
    config = uvicorn.Config(web_app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)), log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


# --- БОТ ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    if is_banned(message.from_user.id):
        return await message.answer("🚫 Вы заблокированы.")

    user = get_user(message.from_user.id)
    referrer_id = None
    if not user:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].isdigit():
            referrer_id = int(parts[1])
        add_user(message.from_user.id, message.from_user.username, referrer_id)

    try:
        await message.answer_sticker(STICKER_ID)
    except Exception:
        pass

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
        f"👤 <b>Мой профиль</b>\n\n"
        f"ID: <code>{cb.from_user.id}</code>\n"
        f"Доступно запросов: <b>{user[2] if user else 0}</b>\n"
        f"Баланс: $0.00\n"
        f"Реферальный баланс: $0.00\n"
        f"Дата регистрации: {user[4] if user else '-'}\n"
        f"<i>(Вы агент уже: {days_str})</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡️ Скрытие данных (5$)", callback_data="hide_data_menu")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]
    ])

    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "hide_data_menu")
async def cb_hide_data_menu(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Оплатить Tonkeeper", callback_data="pay_tonkeeper", style="primary")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="profile")]
    ])
    await cb.message.edit_text(HIDE_DATA_TEXT, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "pay_tonkeeper")
async def cb_pay_tonkeeper(cb: CallbackQuery):
    memo = f"Oasis_Digital_{cb.from_user.id}"
    text = (
        f"💎 <b>Счёт на оплату через USDT (TON) / TON</b>\n\n"
        f"📦 <b>Тариф:</b> Скрытие личных данных из поиска\n"
        f"📝 <b>Описание:</b> Скрытие личных данных из поиска за $14.0 (скидка 0%)\n"
        f"💵 <b>Сумма к отправке:</b> 14.0$ USDT (или ~13.04 TON)\n\n"
        f"📍 <b>Куда отправлять (кошелёк TON):</b>\n<code>{TON_WALLET}</code>\n\n"
        f"⚠️ <b>ОБЯЗАТЕЛЬНО укажите подпись (комментарий / memo) к переводу:</b>\n"
        f"<code>{memo}</code> (или число {cb.from_user.id})\n\n"
        f"📲 Самый удобный способ: нажмите кнопку «📲 Оплатить в 1 клик» ниже — кошелёк Tonkeeper / Telegram Wallet откроется с уже введённой суммой и memo!\n\n"
        f"⚡️ После отправки бот автоматически обнаружит платёж за 10–30 секунд и начислит покупки."
    )

    tonkeeper_link = f"ton://transfer/{TON_WALLET}?amount=13040000000&text={memo}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 Оплатить в 1 клик", url=tonkeeper_link, style="primary")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="hide_data_menu")]
    ])

    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data == "buy_requests")
async def cb_buy_requests(cb: CallbackQuery):
    await cb.message.edit_text(
        "💎 <b>Покупка запросов</b>\n\nДля покупки запросов напишите в поддержку или оплатите через профиль (функция в разработке).",
        parse_mode="HTML",
        reply_markup=back_kb()
    )
    await cb.answer()


@dp.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.wait_support_msg)
    await cb.message.edit_text(
        SUPPORT_TEXT, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_start")]])
    )
    await cb.answer()


@dp.message(SearchStates.wait_support_msg)
async def process_support_msg(msg: Message, state: FSMContext):
    await state.clear()
    admin_text = f"📩 <b>Новое сообщение в поддержку</b>\n\nОт: {msg.from_user.full_name} (@{msg.from_user.username})\nID: <code>{msg.from_user.id}</code>\n\nТекст: {msg.text}"
    try:
        await bot.send_message(MAIN_ADMIN_ID, admin_text, parse_mode="HTML")
        await msg.answer("✅ Ваше сообщение отправлено администрации.", reply_markup=main_kb())
    except Exception:
        log.exception("Support forward failed")
        await msg.answer("❌ Ошибка отправки.", reply_markup=main_kb())


# --- НЕПОЛНЫЙ ПОИСК ---
@dp.callback_query(F.data == "search_partial")
async def cb_search_partial(cb: CallbackQuery, state: FSMContext):
    await state.update_data(surname=None, name=None, patronymic=None, birth_date=None, country=None)
    await cb.message.edit_text(PARTIAL_TEXT, parse_mode="HTML", reply_markup=partial_search_kb(await state.get_data()))
    await cb.answer()


@dp.callback_query(F.data.startswith("field_"))
async def cb_select_field(cb: CallbackQuery, state: FSMContext):
    field = cb.data.replace("field_", "")
    await state.update_data(editing_field=field)
    field_names = {"surname": "Фамилию", "name": "Имя", "patronymic": "Отчество", "birth_date": "Дату рождения"}
    await cb.message.edit_text(
        f"Введите {field_names.get(field, 'значение')}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_partial")]])
    )
    await state.set_state(SearchStates.wait_partial_field)
    await cb.answer()


@dp.message(SearchStates.wait_partial_field)
async def process_field_input(msg: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("editing_field")
    if field and msg.text:
        await state.update_data({field: msg.text.strip()})
    await state.set_state(None)
    await msg.answer(PARTIAL_TEXT, parse_mode="HTML", reply_markup=partial_search_kb(await state.get_data()))


@dp.callback_query(F.data == "clear_fields")
async def cb_clear(cb: CallbackQuery, state: FSMContext):
    await state.update_data(surname=None, name=None, patronymic=None, birth_date=None, country=None)
    await cb.answer("Поля очищены")
    await cb.message.edit_reply_markup(reply_markup=partial_search_kb(await state.get_data()))


@dp.callback_query(F.data == "select_country")
async def cb_select_country(cb: CallbackQuery):
    await cb.message.edit_text("🌍 Выберите страну поиска:", reply_markup=country_kb())
    await cb.answer()


@dp.callback_query(F.data.startswith("set_country_"))
async def cb_set_country(cb: CallbackQuery, state: FSMContext):
    country = cb.data.replace("set_country_", "")
    await state.update_data(country=country)
    await cb.message.edit_text(PARTIAL_TEXT, parse_mode="HTML", reply_markup=partial_search_kb(await state.get_data()))
    await cb.answer()


@dp.callback_query(F.data == "back_to_partial")
async def cb_back_to_partial(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await cb.message.edit_text(PARTIAL_TEXT, parse_mode="HTML", reply_markup=partial_search_kb(await state.get_data()))
    await cb.answer()


@dp.callback_query(F.data == "do_partial_search")
async def cb_do_partial(cb: CallbackQuery, state: FSMContext):
    if is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)

    data = await state.get_data()
    query_parts = []
    if data.get('surname'):
        query_parts.append(f"Фамилия: {data['surname']}")
    if data.get('name'):
        query_parts.append(f"Имя: {data['name']}")
    if data.get('patronymic'):
        query_parts.append(f"Отчество: {data['patronymic']}")
    if data.get('birth_date'):
        query_parts.append(f"Дата: {data['birth_date']}")
    if data.get('country'):
        query_parts.append(f"Страна: {data['country']}")

    if not query_parts:
        return await cb.answer("Заполните хотя бы одно поле.", show_alert=True)

    if not deduct_request(cb.from_user.id):
        return await cb.answer("❌ У вас закончились запросы. Пригласите друга или купите в профиле.", show_alert=True)

    query_str = ", ".join(query_parts)
    note = await cb.message.edit_text(
        f"🔍 <b>[OASIS DIGITAL]</b> Идет поиск по базам данных...\n\n<b>Цель:</b> {query_str}\n<b>Тип:</b> partial",
        parse_mode="HTML"
    )

    data_result = await search_everywhere(query_str)
    report_id = str(uuid.uuid4())[:8]
    REPORTS_CACHE[report_id] = data_result

    if data_result.get("fio") == "НЕ НАЙДЕНО":
        await note.edit_text("❌ <b>Данные не найдены.</b>", parse_mode="HTML", reply_markup=main_kb())
    else:
        body = format_report_text(data_result)
        chunks = [body[i:i+3900] for i in range(0, len(body), 3900)]
        await note.edit_text(chunks[0], parse_mode="HTML", reply_markup=result_kb(report_id) if len(chunks) == 1 else None)
        for extra in chunks[1:-1]:
            await cb.message.answer(extra, parse_mode="HTML")
        if len(chunks) > 1:
            await cb.message.answer(chunks[-1], parse_mode="HTML", reply_markup=result_kb(report_id))
    await cb.answer()


@dp.callback_query(F.data == "ref_link")
async def cb_ref_link(cb: CallbackQuery):
    ref_link = f"https://t.me/{(await bot.get_me()).username}?start={cb.from_user.id}"
    text = f"🔗 <b>Ваша реферальная ссылка:</b>\n\n<code>{ref_link}</code>\n\n<i>За каждого приглашенного вы получаете +1 запрос!</i>"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()


@dp.callback_query(F.data == "create_mirror")
async def cb_create_mirror(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text(MIRROR_TEXT, parse_mode="HTML", reply_markup=back_kb())
    await state.set_state(SearchStates.wait_mirror_token)
    await cb.answer()


@dp.message(SearchStates.wait_mirror_token)
async def process_mirror_token(msg: Message, state: FSMContext):
    await state.clear()
    token = msg.text.strip() if msg.text else ""

    if ":" not in token:
        return await msg.answer("❌ Это не похоже на токен.", reply_markup=main_kb())

    test_bot = Bot(token=token)
    try:
        bot_info = await test_bot.get_me()
        conn = sqlite3.connect(USERS_DB)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO mirrors (bot_username, added_by, added_at) VALUES (?, ?, ?)",
            (bot_info.username, msg.from_user.id, datetime.now().strftime("%d.%m.%Y %H:%M"))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.exception("Mirror token validation failed")
        return await msg.answer("❌ Неверный токен.", reply_markup=main_kb())
    finally:
        await test_bot.session.close()

    await msg.answer(
        f"🕵‍♂ <b>Зеркало @{bot_info.username} добавлено в базу!</b>\n\nОно появится на сайте автоматически.",
        parse_mode="HTML", reply_markup=main_kb()
    )


# --- ОБЫЧНЫЙ ПОИСК ---
@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return
    if msg.from_user.is_bot:
        return

    if is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы.")

    if not deduct_request(msg.from_user.id):
        return await msg.answer("❌ У вас закончились запросы.")

    q = msg.text.strip()

    note = await msg.answer(
        f"🔍 <b>[OASIS DIGITAL]</b> Идет поиск по базам данных...\n\n<b>Цель:</b> {q}\n<b>Тип:</b> auto",
        parse_mode="HTML"
    )
    data = await search_everywhere(q)

    if not data or data.get("fio") == "НЕ НАЙДЕНО":
        return await note.edit_text("❌ <b>Данные не найдены.</b>", parse_mode="HTML", reply_markup=main_kb())

    report_id = str(uuid.uuid4())[:8]
    REPORTS_CACHE[report_id] = data

    body = format_report_text(data)
    chunks = [body[i:i+3900] for i in range(0, len(body), 3900)]
    await note.edit_text(chunks[0], parse_mode="HTML", reply_markup=result_kb(report_id) if len(chunks) == 1 else None)
    for extra in chunks[1:-1]:
        await msg.answer(extra, parse_mode="HTML")
    if len(chunks) > 1:
        await msg.answer(chunks[-1], parse_mode="HTML", reply_markup=result_kb(report_id))


# --- СКАЧИВАНИЕ ---
@dp.callback_query(F.data.startswith("get_txt_"))
async def cb_send_txt(cb: CallbackQuery):
    report_id = cb.data.replace("get_txt_", "")
    data = REPORTS_CACHE.get(report_id)
    if not data:
        return await cb.answer("Отчет устарел", show_alert=True)

    text = f"OASIS DIGITAL\nЗапрос: {data['query']}\nФИО: {data['fio']}\nТел: {data['phone_main']}\nОператор: {data['operator']}\nРегион: {data['region']}\nEmail: {', '.join(data.get('emails', [])) or '-'}\nАдрес: {'; '.join(data.get('addresses', [])) or '-'}\nПаспорт: {data['passport']}\nИНН: {data['inn']}\nСНИЛС: {data['snils']}"
    file = BufferedInputFile(text.encode('utf-8'), filename=f"oasis_{report_id}.txt")
    await cb.message.answer_document(document=file, caption="📄 <b>TXT Отчет</b>", parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("get_json_"))
async def cb_send_json(cb: CallbackQuery):
    report_id = cb.data.replace("get_json_", "")
    data = REPORTS_CACHE.get(report_id)
    if not data:
        return await cb.answer("Отчет устарел", show_alert=True)

    json_str = json.dumps(data, ensure_ascii=False, indent=2)
    file = BufferedInputFile(json_str.encode('utf-8'), filename=f"oasis_{report_id}.json")
    await cb.message.answer_document(document=file, caption="📦 <b>JSON Отчет</b>", parse_mode="HTML")
    await cb.answer()


# --- АДМИНКА ---
@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    await msg.answer("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_check_apis")
async def cb_adm_check_apis(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)

    note = await cb.message.edit_text("🔌 <b>Проверяю API...</b>\n\nЭто может занять до 20 секунд.", parse_mode="HTML")
    await cb.answer()

    results = await run_all_api_checks()

    lines = ["🔌 <b>Статус API</b>\n"]
    for r in results:
        icon = "🟢" if r["healthy"] else "🔴"
        lines.append(f"{icon} <b>{r['name']}</b>")
        lines.append(f"├ Код: <code>{r['status']}</code>")
        lines.append(f"├ Задержка: <code>{r['latency_ms']} ms</code>")
        lines.append(f"└ Ответ: <code>{r['preview']}</code>\n")

    healthy_count = sum(1 for r in results if r["healthy"])
    lines.append(f"\n📊 <b>Итого:</b> {healthy_count}/{len(results)} работают")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "..."

    await note.edit_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить ещё раз", callback_data="adm_check_apis", style="primary")],
            [InlineKeyboardButton(text="⬅️ В админку", callback_data="adm_back")]
        ])
    )


@dp.callback_query(F.data == "adm_back")
async def cb_adm_back(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_menu_kb())
    await cb.answer()


@dp.callback_query(F.data == "adm_give")
async def cb_adm_give(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text(
        "Введите <code>ID количество</code>:", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_start")]])
    )
    await state.set_state(SearchStates.wait_admin_give)
    await cb.answer()


@dp.message(SearchStates.wait_admin_give)
async def process_admin_give(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    try:
        parts = msg.text.strip().split()
        uid, amount = int(parts[0]), int(parts[1])
        add_requests(uid, amount)
        await msg.answer(f"✅ Выдано {amount} для {uid}.", reply_markup=admin_menu_kb())
    except Exception:
        await msg.answer("❌ Ошибка формата.")
    await state.clear()


@dp.callback_query(F.data == "adm_take")
async def cb_adm_take(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text(
        "Введите <code>ID количество</code> для списания:", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_start")]])
    )
    await state.set_state(SearchStates.wait_admin_take)
    await cb.answer()


@dp.message(SearchStates.wait_admin_take)
async def process_admin_take(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    try:
        parts = msg.text.strip().split()
        uid, amount = int(parts[0]), int(parts[1])
        add_requests(uid, -amount)
        await msg.answer(f"✅ Забрано {amount} у {uid}.", reply_markup=admin_menu_kb())
    except Exception:
        await msg.answer("❌ Ошибка формата.")
    await state.clear()


@dp.callback_query(F.data == "adm_ban")
async def cb_adm_ban(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text(
        "Введите <code>ID</code> для бана/разбана:", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_start")]])
    )
    await state.set_state(SearchStates.wait_admin_ban)
    await cb.answer()


@dp.message(SearchStates.wait_admin_ban)
async def process_admin_ban(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    try:
        uid = int(msg.text.strip())
        conn = sqlite3.connect(USERS_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (uid,))
        row = cursor.fetchone()
        if row:
            new_status = 0 if row[0] == 1 else 1
            cursor.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (new_status, uid))
            conn.commit()
            status_text = "ЗАБАНЕН 🚫" if new_status == 1 else "РАЗБАНЕН ✅"
            await msg.answer(f"Пользователь <code>{uid}</code>: {status_text}", parse_mode="HTML", reply_markup=admin_menu_kb())
        else:
            await msg.answer("❌ Пользователь не найден.", reply_markup=admin_menu_kb())
        conn.close()
    except Exception:
        await msg.answer("❌ Ошибка формата.")
    await state.clear()


@dp.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text(
        "📢 <b>Рассылка</b>\n\nОтправьте текст сообщения:", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_start")]])
    )
    await state.set_state(SearchStates.wait_admin_broadcast)
    await cb.answer()


@dp.message(SearchStates.wait_admin_broadcast)
async def process_admin_broadcast(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()

    if not msg.text:
        return await msg.answer("❌ Текст пустой.")

    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE is_banned = 0")
    users = cursor.fetchall()
    conn.close()

    sent = 0
    failed = 0
    status_msg = await msg.answer(f"📢 Рассылка на {len(users)} юзеров...")

    for user in users:
        try:
            await bot.send_message(user[0], msg.text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n📬 Доставлено: {sent}\n❌ Ошибок: {failed}",
        reply_markup=admin_menu_kb()
    )


@dp.callback_query(F.data == "adm_test_api")
async def cb_adm_test_api(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text(
        "📡 <b>Тест API</b>\n\nВведите запрос (например, номер телефона, ФИО или ник):", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_start")]])
    )
    await state.set_state(SearchStates.wait_admin_raw)
    await cb.answer()


@dp.message(SearchStates.wait_admin_raw)
async def process_admin_raw(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()

    q = msg.text.strip()
    note = await msg.answer(f"📡 <b>Отправляю запросы к API:</b> {q}...", parse_mode="HTML")

    res_mb, res_bb = await asyncio.gather(fetch_mobius(q), fetch_blackbox(q))
    mb_str = json.dumps(res_mb, ensure_ascii=False)[:1500] if res_mb else "None/Ошибка (см. логи)"
    bb_str = json.dumps(res_bb, ensure_ascii=False)[:1500] if res_bb else "None/Ошибка (см. логи)"

    text = f"<b>MOBIUS:</b>\n<pre>{mb_str}</pre>\n\n<b>BLACK-BOX:</b>\n<pre>{bb_str}</pre>"
    if len(text) > 4000:
        text = text[:3990] + "..."

    await note.edit_text(text, parse_mode="HTML")
    await msg.answer("Возврат в админку", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)

    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), SUM(requests_left) FROM users")
    total, remaining = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    banned = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE joined_at = ?", (datetime.now().strftime("%d.%m.%Y"),))
    today = cursor.fetchone()[0]
    conn.close()

    text = (
        f"📊 <b>Аналитика</b>\n\n"
        f"👥 Всего юзеров: <b>{total or 0}</b>\n"
        f"🚫 Забанено: <b>{banned or 0}</b>\n"
        f"🆕 Сегодня: <b>{today or 0}</b>\n"
        f"🎟 Остаток запросов: <b>{remaining or 0}</b>\n"
        f"📁 Отчетов в кэше: <b>{len(REPORTS_CACHE)}</b>"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=admin_menu_kb())
    await cb.answer()


@dp.callback_query(F.data == "adm_promo")
async def cb_adm_promo(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.answer("В разработке", show_alert=True)


@dp.callback_query(F.data == "adm_maintenance")
async def cb_adm_maintenance(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await cb.answer("В разработке", show_alert=True)


async def main():
    log.info("Бот запускается на Polling...")
    await bot.delete_webhook(drop_pending_updates=True)

    asyncio.create_task(run_web())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
