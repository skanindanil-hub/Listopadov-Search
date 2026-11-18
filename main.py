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

# ====================== НАСТРОЙКИ ИЗ ТВОЕГО КОНФИГА ======================
BOT_TOKEN = "9024954594:AAGj3lihxCTSGgCMaeE_mCi2519paMgIYms"
BOT_USERNAME = "@MobiusSearchBot"

MAIN_ADMIN_ID = 7615546239
ADMIN_USERNAME = "@youranonglobal"

CHANNEL_ID = -1004330390390
CHANNEL_URL = "https://t.me/+pzsfx9fWdXdkYmFl"
PAYMENTS_CHANNEL_ID = -1004308412586

# API КЛЮЧИ
KRATOS_KEY = "lBGtnFGdTabFrRiFdFzZiEjUrTlqArcU"
BLACKBOX_KEY = "Y1nqXZ4tOVh4PBsUZyB2CQW6TGtn"
HYDRA_KEY = "HYDRA-LordKEY-m7N9iq8wl7JzAnjToNXCA"
ONUX_KEY = "sk-qU6CYudhVnxATab3cSJVdZSP35okBEB4"
NEBULA_KEY = "NEBULA-A0C2BC2A724EA3B5E8431B2B8C83EC9C"

TON_WALLET = "UQBlTLz8MaKDtNhYo1qeDsOXtVjN1upRp2uwsf0NWWE9p5cx"
WEBHOOK_HOST = "https://listopadov-search.onrender.com"
DB_FILE = "oasis.db"
USERS_DB = "users.db"
STICKER_ID = "CAACAgIAAxkBAAFXzjhq-itNMdvQX03_x8hhB9NUezaSiAACFyUAAtkoAAFL79-L7Fw-Jlc9BA"

# Mobius / BlackBox Endpoints
API_URL_MOBIUS = "https://mobiusnet.cc.cd/api/v1/search"
API_URL_BLACKBOX = "https://api.black-box.cc"

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
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE mirrors ADD COLUMN bot_token TEXT")
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


# --- ТЕКСТЫ И КЛАВИАТУРЫ ---
START_TEXT = """<b>ℹ️ Примеры для ввода запросов</b>

🕵️ <b>Личность</b>
<blockquote>Будашевская Елена Анатольевна</blockquote>

📲 <b>Контакты</b>
<blockquote>79825207224 — номер телефона
example@gmail.com — email</blockquote>

💬 <b>Соцсети</b>
<blockquote>@durov — Telegram</blockquote>

<i>Отправьте запрос сообщением.</i>"""

INTRO_TEXT = f'<a href="{WEBHOOK_HOST}">Mobius Search</a> - это отличный инструмент для поиска информации по открытым источникам.'
PARTIAL_TEXT = "📄 <b>Поиск по неполным данным</b>\nЗаполните необходимые поля и нажмите поиск."
SUPPORT_TEXT = "🆘 <b>Связь с нами</b>\nНапишите ваше сообщение в чат."
MIRROR_TEXT = "🔧 <b>Инструкция по созданию зеркала:</b>\nОтправьте API Token бота от @BotFather."
HIDE_DATA_TEXT = "🛡️ <b>Скрытие данных</b>\nСтоимость скрытия данных: $7 (навсегда)"


def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поиск", callback_data="search_info")],
        [InlineKeyboardButton(text="📄 Поиск по неполным данным", callback_data="search_partial")],
        [InlineKeyboardButton(text="💎 Купить запросы", callback_data="buy_requests")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🎟 Активировать промокод", callback_data="use_promo")],
        [InlineKeyboardButton(text="🔗 Рефералка", callback_data="ref_link"), InlineKeyboardButton(text="🤖 Зеркало", callback_data="create_mirror")],
        [InlineKeyboardButton(text="🆘 Связь с нами", callback_data="support")]
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
        [InlineKeyboardButton(text="➕ Выдать", callback_data="adm_give"), InlineKeyboardButton(text="➖ Забрать", callback_data="adm_take")],
        [InlineKeyboardButton(text="🎟 Создать промокод", callback_data="adm_promo"), InlineKeyboardButton(text="💳 Создать чек", callback_data="adm_check")],
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="adm_stats"), InlineKeyboardButton(text="🚫 Бан", callback_data="adm_ban")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")]
    ])


def partial_search_kb(data: dict):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Очистить", callback_data="clear_fields"), InlineKeyboardButton(text="Поиск", callback_data="do_partial_search")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]
    ])


# --- API ЗАПРОСЫ С ТВОИМИ КЛЮЧАМИ ---
async def fetch_mobius(q: str):
    clean_q = q.strip().replace("@", "")
    headers = {"X-API-Key": KRATOS_KEY, "Content-Type": "application/json"}
    payload = {"query": clean_q, "type": "auto"}
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(API_URL_MOBIUS, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        log.exception("Mobius request failed")
    return None


async def fetch_blackbox(q: str):
    headers = {"X-API-Key": BLACKBOX_KEY, "Accept": "application/json"}
    params = {"q": q, "type": "auto"}
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{API_URL_BLACKBOX}/v1.1/search", headers=headers, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        log.exception("BlackBox request failed")
    return None


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
                return {"name": name, "healthy": 200 <= resp.status < 300, "status": resp.status, "latency_ms": elapsed, "preview": (await resp.text())[:200]}
    except Exception as e:
        return {"name": name, "healthy": False, "status": "ERROR", "latency_ms": 0, "preview": str(e)[:200]}


async def run_all_api_checks():
    return await asyncio.gather(
        check_api_health("Mobius (Kratos)", API_URL_MOBIUS, method="POST", headers={"X-API-Key": KRATOS_KEY, "Content-Type": "application/json"}, payload={"query": "79825207224", "type": "auto"}),
        check_api_health("Black-Box", f"{API_URL_BLACKBOX}/v1.1/search", method="GET", headers={"X-API-Key": BLACKBOX_KEY}, payload={"q": "79825207224", "type": "phone"})
    )


# --- ПАРСЕР ---
async def search_everywhere(q: str):
    clean_q = q.strip().replace("@", "")
    res_mobius, res_blackbox = await asyncio.gather(fetch_mobius(q), fetch_blackbox(q))

    final_data = {
        "query": q, "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "fio": "Нет", "phone_main": q, "username": "Нет",
        "operator": "Нет", "region": "Нет", "country": "Нет",
        "emails": [], "addresses": [], "phones": [],
        "dob": "Нет", "passport": "Нет", "inn": "Нет", "snils": "Нет",
        "activity": {}, "tg_info": {},
        "sources_list": [], "stats": {"sources": 0, "phones": 0, "records": 0}
    }

    found = False
    for resp in [res_blackbox, res_mobius]:
        if not isinstance(resp, dict):
            continue
        profile = resp.get("profile") or {}
        telecom = resp.get("telecom") or {}
        
        fio = profile.get("primary_fio") or resp.get("fio") or ""
        if fio:
            found = True
            final_data["fio"] = fio
            final_data["dob"] = profile.get("birth_date", "Нет")
            final_data["snils"] = profile.get("snils", "Нет")
            final_data["operator"] = telecom.get("operator", "Нет")
            final_data["region"] = telecom.get("region", "Нет")
            final_data["country"] = telecom.get("country", "Россия")
            break

    if found:
        final_data["stats"]["records"] = 1
        final_data["sources_list"].append({"name": "Mobius / Black-Box Engine", "count": 1})
    else:
        final_data["fio"] = "НЕ НАЙДЕНО"

    final_data["stats"]["sources"] = len(final_data["sources_list"])
    return final_data


def format_report_text(data: dict) -> str:
    def tree(label, val, last=False):
        return f"{'└' if last else '├'} {label}: {val}\n"

    parts = [f"🔍 <b>Результат поиска по:</b> {data.get('query')}\n\n"]
    if data.get("fio") != "НЕ НАЙДЕНО":
        parts.append(tree("ФИО", data.get("fio")))
        parts.append(tree("Дата рождения", data.get("dob")))
        parts.append(tree("СНИЛС", data.get("snils")))
        parts.append(tree("Оператор", data.get("operator")))
        parts.append(tree("Регион", data.get("region"), last=True))
    else:
        parts.append("❌ Данные не найдены в базах.")
    return "".join(parts)


def generate_html_report(data: dict, report_id: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head><meta charset="UTF-8"><title>Mobius Report</title>
    <style>body{{font-family:sans-serif;background:#0f1015;color:#fff;padding:20px;}} .card{{background:#1e293b;padding:20px;border-radius:10px;}}</style>
    </head>
    <body>
        <div class="card">
            <h2>Отчет по запросу: {data.get('query')}</h2>
            <p><b>ФИО:</b> {data.get('fio')}</p>
            <p><b>Дата рождения:</b> {data.get('dob')}</p>
            <p><b>СНИЛС:</b> {data.get('snils')}</p>
            <p><b>Оператор / Регион:</b> {data.get('operator')} / {data.get('region')}</p>
        </div>
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


# --- ЗЕРКАЛА И БОТ ---
async def run_mirror_bot(token: str, bot_username: str):
    mirror_bot = Bot(token=token)
    mirror_dp = Dispatcher(storage=MemoryStorage())
    @mirror_dp.message(CommandStart())
    async def mirror_start(message: Message):
        await message.answer(f"Зеркало Mobius Search. Главный бот: {BOT_USERNAME}")
    try:
        await mirror_bot.delete_webhook(drop_pending_updates=True)
        await mirror_dp.start_polling(mirror_bot)
    finally:
        await mirror_bot.session.close()


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
    memo = f"Mobius_{cb.from_user.id}"
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
    await msg.answer("👑 <b>Админ-панель Mobius</b>", parse_mode="HTML", reply_markup=admin_menu_kb())


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
    await cb.message.edit_text("📡 <b>Тест API (Raw)</b>\n\nВведите запрос:", parse_mode="HTML", reply_markup=back_kb())
    await state.set_state(SearchStates.wait_admin_raw)
    await cb.answer()


@dp.message(SearchStates.wait_admin_raw)
async def process_admin_raw(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    q = msg.text.strip()
    note = await msg.answer(f"📡 Запрос: <b>{q}</b>...", parse_mode="HTML")
    
    res_mb, res_bb = await asyncio.gather(fetch_mobius(q), fetch_blackbox(q))
    mb_str = json.dumps(res_mb, ensure_ascii=False, indent=2)[:1500] if res_mb else "None"
    bb_str = json.dumps(res_bb, ensure_ascii=False, indent=2)[:1500] if res_bb else "None"

    await note.edit_text(f"<b>MOBIUS:</b>\n<pre>{mb_str}</pre>\n\n<b>BLACK-BOX:</b>\n<pre>{bb_str}</pre>", parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_check_apis")
async def cb_adm_check_apis(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    note = await cb.message.edit_text("🔌 Проверяю API...", parse_mode="HTML")
    results = await run_all_api_checks()
    healthy = sum(1 for r in results if r["healthy"])
    await note.edit_text(f"🔌 <b>Статус API:</b> {healthy}/{len(results)} активны", parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_back")
async def cb_adm_back(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    await state.clear()
    await cb.message.edit_text("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_menu_kb())
    await cb.answer()


# --- ОСНОВНОЙ ПОИСК ---
@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message, state: FSMContext):
    if await state.get_state() is not None or msg.from_user.is
