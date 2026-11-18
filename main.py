import asyncio
import logging
import os
import re
import json
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
BOT_TOKEN = "9244232488:AAFJAQ4TpJvZm3uhE1cgXsbwChSzOj2DTXY"
BOT_USERNAME = "@MobiusSearchBot"

MAIN_ADMIN_ID = 8394631079
ADMIN_USERNAME = "@youranonglobal"

CHANNEL_ID = -1004330390390
CHANNEL_URL = "https://t.me/+pzsfx9fWdXdkYmFl"
PAYMENTS_CHANNEL_ID = -1004308412586

# API КЛЮЧИ
KRATOS_KEY = "lBGtnFGdTabFrRiFdFzZiEjUrTlqArcU"
BLACKBOX_KEY = "Y1nqXZ4tOVh4PBsUZyB2CQW6TGtn"
BLACKBOX_KEY_2 = "gcGGgocfzgrHqyDPLfQpvMvrQgQ3Dnqi"
HYDRA_KEY = "HYDRA-LordKEY-m7N9iq8wl7JzAnjToNXCA"
ONUX_KEY = "sk-qU6CYudhVnxATab3cSJVdZSP35okBEB4"
NEBULA_KEY = "NEBULA-A0C2BC2A724EA3B5E8431B2B8C83EC9C"
LEAKOSINT_KEY = "0f1a043ec680d41415c6b4437f631d47aa0cd484427d7e65"

TON_WALLET = "UQBlTLz8MaKDtNhYo1qeDsOXtVjN1upRp2uwsf0NWWE9p5cx"
WEBHOOK_HOST = "https://listopadov-search.onrender.com"
DB_FILE = "oasis.db"
USERS_DB = "users.db"

# API Endpoints
API_URL_MOBIUS = "https://mobiusnet.cc.cd/api/v1/search"
API_URL_BLACKBOX = "https://api.black-box.cc/v1.1/search"
API_URL_LEAKOSINT = "https://leakosintapi.com/"

API_TIMEOUT = 90  # 90 секунд на ожидание ответа от каждого API

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
    wait_partial_field = State()
    wait_support_msg = State()
    wait_mirror_token = State()
    wait_admin_give = State()
    wait_admin_take = State()
    wait_admin_ban = State()
    wait_admin_broadcast = State()
    wait_admin_raw = State()
    wait_promo_input = State()
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
        CREATE TABLE IF NOT EXISTS mirrors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_username TEXT UNIQUE,
            bot_token TEXT,
            added_by INTEGER,
            added_at TEXT,
            is_active INTEGER DEFAULT 1
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


# --- API ЗАПРОСЫ (ТАЙМАУТ 90 СЕКУНД) ---
async def fetch_mobius(q: str):
    clean_q = q.strip().replace("@", "")
    headers = {"X-API-Key": KRATOS_KEY, "Content-Type": "application/json"}
    payload = {"query": clean_q, "type": "auto"}
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(API_URL_MOBIUS, json=payload, headers=headers) as resp:
                text = await resp.text()
                log.info(f"[MOBIUS] Status: {resp.status} | Response: {text[:200]}")
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        log.error(f"[MOBIUS ERROR] {e}")
    return None


async def fetch_blackbox(q: str, api_key: str = BLACKBOX_KEY):
    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    params = {"q": q.strip(), "type": "auto"}
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL_BLACKBOX, headers=headers, params=params) as resp:
                text = await resp.text()
                log.info(f"[BLACKBOX] Status: {resp.status} | Response: {text[:200]}")
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        log.error(f"[BLACKBOX ERROR] {e}")
    return None


async def fetch_leakosint(q: str):
    payload = {"token": LEAKOSINT_KEY, "request": q.strip(), "limit": 100, "lang": "ru"}
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(API_URL_LEAKOSINT, json=payload) as resp:
                text = await resp.text()
                log.info(f"[LEAKOSINT] Status: {resp.status} | Response: {text[:200]}")
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        log.error(f"[LEAKOSINT ERROR] {e}")
    return None


async def check_api_health(name, url, method="GET", headers=None, payload=None):
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    start = asyncio.get_event_loop().time()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if method == "GET":
                req = session.get(url, headers=headers, params=payload)
            else:
                req = session.post(url, headers=headers, json=payload)
            async with req as resp:
                elapsed = int((asyncio.get_event_loop().time() - start) * 1000)
                return {"name": name, "healthy": 200 <= resp.status < 300, "status": resp.status, "latency_ms": elapsed}
    except Exception as e:
        return {"name": name, "healthy": False, "status": "ERROR", "latency_ms": 0}


async def run_all_api_checks():
    return await asyncio.gather(
        check_api_health("Mobius (Kratos)", API_URL_MOBIUS, method="POST", headers={"X-API-Key": KRATOS_KEY, "Content-Type": "application/json"}, payload={"query": "79825207224", "type": "auto"}),
        check_api_health("Black-Box 1", API_URL_BLACKBOX, method="GET", headers={"X-API-Key": BLACKBOX_KEY}, payload={"q": "79825207224", "type": "phone"}),
        check_api_health("Black-Box 2", API_URL_BLACKBOX, method="GET", headers={"X-API-Key": BLACKBOX_KEY_2}, payload={"q": "79825207224", "type": "phone"}),
        check_api_health("LeakOsint", API_URL_LEAKOSINT, method="POST", payload={"token": LEAKOSINT_KEY, "request": "79825207224", "limit": 10})
    )


# --- АГРЕГАТОР ПО ВСЕМ API ---
async def search_everywhere(q: str):
    # Запускаем все доступные API одновременно с ожиданием до 90 секунд
    results = await asyncio.gather(
        fetch_blackbox(q, BLACKBOX_KEY),
        fetch_blackbox(q, BLACKBOX_KEY_2),
        fetch_mobius(q),
        fetch_leakosint(q),
        return_exceptions=True
    )

    final_data = {
        "query": q,
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "fio": set(),
        "dob": set(),
        "snils": set(),
        "phones": set(),
        "emails": set(),
        "operator": "Нет",
        "region": "Нет",
        "country": "Россия",
        "databases": {},
        "tg_info": {},
        "sources_list": [],
        "raw_responses": []
    }

    for idx, resp in enumerate(results):
        if not isinstance(resp, dict):
            continue

        final_data["raw_responses"].append(resp)

        # 1. Разбор формата Black-Box / Mobius
        profile = resp.get("profile") or {}
        telecom = resp.get("telecom") or {}
        tg = resp.get("telegram") or {}
        dbs = resp.get("databases") or resp.get("results") or {}

        if profile.get("primary_fio"):
            final_data["fio"].add(profile["primary_fio"])
        if resp.get("fio"):
            final_data["fio"].add(resp["fio"])
        if profile.get("birth_date"):
            final_data["dob"].add(profile["birth_date"])
        if profile.get("snils"):
            final_data["snils"].add(profile["snils"])
        if isinstance(profile.get("phones"), list):
            final_data["phones"].update(profile["phones"])

        if telecom.get("operator"):
            final_data["operator"] = telecom["operator"]
        if telecom.get("region"):
            final_data["region"] = telecom["region"]
        if telecom.get("country"):
            final_data["country"] = telecom["country"]

        if tg and isinstance(tg, dict):
            final_data["tg_info"] = tg
            if tg.get("full_name"):
                final_data["fio"].add(tg["full_name"])

        if isinstance(dbs, dict):
            for db_name, records in dbs.items():
                if db_name not in final_data["databases"]:
                    final_data["databases"][db_name] = []
                if isinstance(records, list):
                    final_data["databases"][db_name].extend(records)

        # 2. Разбор формата LeakOsint
        leak_list = resp.get("List")
        if isinstance(leak_list, dict):
            for db_name, db_content in leak_list.items():
                if db_name == "No results found":
                    continue
                data_items = db_content.get("Data", [])
                if data_items:
                    if db_name not in final_data["databases"]:
                        final_data["databases"][db_name] = []
                    final_data["databases"][db_name].extend(data_items)

    # Приводим множества к строкам/спискам для JSON и вывода
    final_data["fio"] = ", ".join(final_data["fio"]) if final_data["fio"] else "НЕ НАЙДЕНО"
    final_data["dob"] = ", ".join(final_data["dob"]) if final_data["dob"] else "Нет"
    final_data["snils"] = ", ".join(final_data["snils"]) if final_data["snils"] else "Нет"
    final_data["phones"] = list(final_data["phones"])
    final_data["emails"] = list(final_data["emails"])

    if final_data["fio"] != "НЕ НАЙДЕНО" or final_data["databases"] or final_data["tg_info"]:
        final_data["sources_list"].append({"name": "Oasis Multi-API Engine", "count": len(final_data["databases"]) or 1})

    return final_data


def format_report_text(data: dict) -> str:
    def tree(label, val, last=False):
        return f"{'└' if last else '├'} {label}: {val}\n"

    parts = [f"🔍 <b>[Oasis Digital] Результат поиска по:</b> <code>{data.get('query')}</code>\n\n"]
    has_data = False

    if data.get("fio") and data.get("fio") != "НЕ НАЙДЕНО":
        has_data = True
        parts.append(tree("ФИО", data.get("fio")))
        if data.get("dob") != "Нет":
            parts.append(tree("Дата рождения", data.get("dob")))
        if data.get("snils") != "Нет":
            parts.append(tree("СНИЛС", data.get("snils")))
        if data.get("operator") != "Нет":
            parts.append(tree("Оператор", data.get("operator")))
        if data.get("region") != "Нет":
            parts.append(tree("Регион", data.get("region"), last=True))
        parts.append("\n")

    tg = data.get("tg_info")
    if tg:
        has_data = True
        parts.append(f"✈️ <b>Telegram:</b>\n")
        if tg.get("id"):
            parts.append(tree("ID", tg.get("id")))
        if tg.get("username"):
            parts.append(tree("Username", f"@{tg.get('username')}"))
        if tg.get("reg_date"):
            parts.append(tree("Регистрация", tg.get("reg_date"), last=True))
        parts.append("\n")

    dbs = data.get("databases")
    if dbs:
        has_data = True
        parts.append(f"🗄 <b>Найдено в базах ({len(dbs)}):</b>\n")
        for db_name, records in list(dbs.items())[:5]:
            parts.append(f"• <b>{db_name}</b> ({len(records)} зап.)\n")
            if records and isinstance(records[0], dict):
                for k, v in list(records[0].items())[:4]:
                    parts.append(f"  ├ {k}: <code>{v}</code>\n")

    if not has_data:
        parts.append("❌ Данные не найдены ни в одном из подключенных API.")

    return "".join(parts)


def generate_html_report(data: dict, report_id: str) -> str:
    dbs_html = ""
    for db_name, records in data.get("databases", {}).items():
        rows = ""
        for rec in records:
            if isinstance(rec, dict):
                items = "<br>".join(f"<b>{k}:</b> {v}" for k, v in rec.items())
                rows += f"<div style='border-bottom:1px solid #334155;padding:8px 0;'>{items}</div>"
        dbs_html += f"<div class='card'><h3>📁 {db_name}</h3>{rows}</div>"

    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head><meta charset="UTF-8"><title>Oasis Digital Report</title>
    <style>body{{font-family:sans-serif;background:#0f1015;color:#fff;padding:20px;}} .card{{background:#1e293b;padding:20px;border-radius:10px;margin-bottom:15px;}}</style>
    </head>
    <body>
        <div class="card">
            <h2>Oasis Digital — Отчет по запросу: {data.get('query')}</h2>
            <p><b>ФИО:</b> {data.get('fio')}</p>
            <p><b>Дата рождения:</b> {data.get('dob')}</p>
            <p><b>СНИЛС:</b> {data.get('snils')}</p>
            <p><b>Оператор / Регион:</b> {data.get('operator')} / {data.get('region')}</p>
        </div>
        {dbs_html}
    </body>
    </html>
    """


# --- FASTAPI ---
web_app = FastAPI()
web_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@web_app.get("/report/{report_id}", response_class=HTMLResponse)
async def web_report(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data:
        return HTMLResponse("<h1>404 Отчет не найден</h1>", status_code=404)
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
    await cb.message.edit_text("📡 <b>Тест API (Raw)</b>\n\nВведите запрос (ожидание до 90 сек):", parse_mode="HTML", reply_markup=back_kb())
    await state.set_state(SearchStates.wait_admin_raw)
    await cb.answer()


@dp.message(SearchStates.wait_admin_raw)
async def process_admin_raw(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    q = msg.text.strip()
    note = await msg.answer(f"📡 Опрашиваю все API (таймаут 90с): <b>{q}</b>...", parse_mode="HTML")
    
    res_mb, res_bb1, res_bb2, res_lo = await asyncio.gather(
        fetch_mobius(q),
        fetch_blackbox(q, BLACKBOX_KEY),
        fetch_blackbox(q, BLACKBOX_KEY_2),
        fetch_leakosint(q),
        return_exceptions=True
    )

    mb_str = json.dumps(res_mb, ensure_ascii=False, indent=2)[:900] if isinstance(res_mb, dict) else "None"
    bb_str = json.dumps(res_bb1 or res_bb2, ensure_ascii=False, indent=2)[:900] if isinstance(res_bb1 or res_bb2, dict) else "None"
    lo_str = json.dumps(res_lo, ensure_ascii=False, indent=2)[:900] if isinstance(res_lo, dict) else "None"

    text = f"<b>MOBIUS:</b>\n<pre>{mb_str}</pre>\n\n<b>BLACK-BOX:</b>\n<pre>{bb_str}</pre>\n\n<b>LEAKOSINT:</b>\n<pre>{lo_str}</pre>"
    await note.edit_text(text[:4000], parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_check_apis")
async def cb_adm_check_apis(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    note = await cb.message.edit_text("🔌 Проверяю все API (ожидание до 90с)...", parse_mode="HTML")
    results = await run_all_api_checks()
    lines = [f"• <b>{r['name']}</b>: {'✅ OK' if r['healthy'] else '❌ FAIL'} ({r['status']}, {r['latency_ms']}ms)" for r in results]
    await note.edit_text("🔌 <b>Статус API:</b>\n\n" + "\n".join(lines), parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data.startswith("get_txt_"))
async def cb_send_txt(cb: CallbackQuery):
    report_id = cb.data.replace("get_txt_", "")
    data = REPORTS_CACHE.get(report_id)
    if not data:
        return await cb.answer("Отчет устарел", show_alert=True)
    text = f"OASIS DIGITAL\nЗапрос: {data['query']}\nФИО: {data['fio']}\nДата рождения: {data['dob']}\nСНИЛС: {data['snils']}"
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
    note = await msg.answer(f"🔍 <b>[Oasis Digital]</b> Идет глубокий поиск по всем базам (до 90 сек)...\n\n<b>Цель:</b> <code>{q}</code>", parse_mode="HTML")
    
    data = await search_everywhere(q)
    report_id = str(uuid.uuid4())[:8]
    REPORTS_CACHE[report_id] = data

    body = format_report_text(data)
    chunks = [body[i:i+3900] for i in range(0, len(body), 3900)]
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
