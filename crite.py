# OASIS SEARCH (Mobius + Nebula + Clean Parser + Dark HTML)
import asyncio
import json
import html
import re
import os
import time
import logging
import random
import asyncpg
import aiohttp
from aiohttp import web
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# --- НАСТРОЙКИ API ---
API_URL_MOBIUS = "https://mobiusnet.cc.cd/api/v1/search"
API_KEY_MOBIUS = "MOBIUS-IO-IW5w3b4xKEY_dS8Kbt4FJswFEsTR97aGKhSEgfrJFjk"

API_URL_NEBULA = "https://nebula.bot.cd/api/v1/search"
API_KEY_NEBULA = "NEBULA-A11ACE72F6559A2E268CE1CBA39AA881"

# --- НАСТРОЙКИ БОТА ---
BOT_TOKEN = "9042470492:AAGrM2pNdx6XaZqETRSZQh-G90VbMh23Jqo"
BOT_USERNAME = "searchosintlistopadov_bot"
MAIN_ADMIN_ID = 8394631079
DATABASE_URL = "postgresql://admin:RRxOapzxaKxIHvMAgyRTOwDZWaVAHXLX@dpg-dbltbtegekts73cc59fg-a/oasis_rl3m"

# --- НАСТРОЙКИ RENDER ---
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://listopadov-search-1.onrender.com")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

logging.basicConfig(level=logging.INFO)
dp = Dispatcher(storage=MemoryStorage())

db_pool = None
html_cache = {} 
report_cache = {} 

class BuyStates(StatesGroup):
    wait_custom_amount = State()
    wait_hash = State()

class AdminStates(StatesGroup):
    give_reqs = State()
    take_reqs = State()
    promo_create = State()
    add_admin = State()

class PromoStates(StatesGroup):
    wait_code = State()

# --- БАЗА ДАННЫХ ---
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                requests_left INTEGER DEFAULT 3,
                referrer_id BIGINT,
                joined_at INTEGER
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                code TEXT PRIMARY KEY,
                req_amount INTEGER,
                max_uses INTEGER,
                current_uses INTEGER DEFAULT 0,
                expires_at BIGINT,
                created_at BIGINT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_uses (
                user_id BIGINT,
                code TEXT,
                used_at BIGINT,
                PRIMARY KEY (user_id, code)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY,
                added_at INTEGER
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

async def is_admin(user_id):
    if user_id == MAIN_ADMIN_ID: return True
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM admins WHERE user_id=$1", user_id)
        return bool(row)

async def get_maintenance() -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key='maintenance'")
        return row and row['value'] == '1'

async def set_maintenance(value: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO settings (key, value) VALUES ('maintenance', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", value)

async def add_user(user_id, username, referrer_id=None):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1", user_id)
        if row: return False
        await conn.execute("""
            INSERT INTO users (user_id, username, requests_left, referrer_id, joined_at)
            VALUES ($1, $2, 3, $3, $4)
        """, user_id, username, referrer_id, int(time.time()))
        return True

async def get_balance(user_id) -> int:
    if await is_admin(user_id): return 999999
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT requests_left FROM users WHERE user_id=$1", user_id)
        return row['requests_left'] if row else 0

async def deduct_request(user_id) -> bool:
    if await is_admin(user_id): return True
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT requests_left FROM users WHERE user_id=$1", user_id)
        if not row or row['requests_left'] <= 0: return False
        await conn.execute("UPDATE users SET requests_left = requests_left - 1 WHERE user_id=$1", user_id)
        return True

async def add_requests(user_id, amount):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1", user_id)
        if not user:
            await conn.execute("INSERT INTO users (user_id, username, requests_left, joined_at) VALUES ($1, 'unknown', 0, $2)", user_id, int(time.time()))
        await conn.execute("UPDATE users SET requests_left = requests_left + $1 WHERE user_id=$2", amount, user_id)

async def get_invited_count(user_id) -> int:
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users WHERE referrer_id=$1", user_id)

async def get_total_users() -> int:
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")

# --- ТЕКСТЫ ---
WELCOME_TEXT = """ℹ️ <b>Oasis Search — Примеры команд</b>

🕵️ <b>Личность:</b>
<code>Фамилия Имя Отчество 01.01.1990</code>

📲 <b>Контакты:</b>
<code>79123456789</code> – номер телефона
<code>example@example.com</code> – email

🚘 <b>Транспорт:</b>
<code>А123БВ777</code> – номер автомобиля
<code>XTA00000000000000</code> – VIN

💬 <b>Соцсети:</b>
<code>vk.com/username</code>, <code>tiktok.com/@username</code>

📟 <b>Telegram:</b>
<code>@username</code>, <code>tg123456789</code>

📄 <b>Документы:</b>
/vu, /passport, /snils, /inn

🏚 <b>Недвижимость:</b>
/adr Город, Улица, 1, кадастровый номер"""

def detect_type(query: str) -> str:
    q = query.strip()
    if re.match(r"^\+?\d{10,15}$", q): return "phone"
    if "@" in q and "." in q.split("@")[-1]: return "email"
    if q.startswith("@") or q.startswith("t.me/") or q.startswith("https://t.me/"): return "telegram"
    if re.match(r"^[А-ЯA-Z]\d{3}[А-ЯA-Z]{2}\d{2,3}$", q.upper()): return "auto"
    return "name"

async def fetch_api(url: str, params: dict, headers: dict = None) -> tuple[bool, dict | str]:
    timeout = aiohttp.ClientTimeout(total=25)
    raw = ""
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    raw = await resp.text()
                    if "Just a moment" not in raw:
                        return True, json.loads(raw)
                return False, f"HTTP {resp.status} - {raw[:100]}"
    except asyncio.TimeoutError:
        return False, "Таймаут"
    except Exception as e:
        return False, str(e)

async def fetch_nebula(q: str) -> tuple[bool, dict | str]:
    timeout = aiohttp.ClientTimeout(total=25)
    headers = {"X-API-Key": API_KEY_NEBULA}
    params = {"query": q}
    
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL_NEBULA, params=params, headers=headers) as resp:
                if resp.status == 200:
                    raw = await resp.text()
                    return True, json.loads(raw)
                return False, f"HTTP {resp.status} - {raw[:100]}"
    except asyncio.TimeoutError:
        return False, "Таймаут"
    except Exception as e:
        return False, str(e)

async def query_all_apis(q: str) -> list[dict]:
    q_type = detect_type(q)
    
    tasks = [
        fetch_api(API_URL_MOBIUS, {"q": q}, headers={"X-API-Key": API_KEY_MOBIUS}),
        fetch_nebula(q)
    ]
    
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)
    except asyncio.TimeoutError:
        return []
    
    combined = []
    sources = ["Mobius", "Nebula"]
    
    for i, res in enumerate(results):
        if isinstance(res, Exception): continue
        success, data = res
        if not success: continue
        if isinstance(data, dict):
            combined.append({"source": sources[i], "data": data})
                
    return combined

def clean_data(obj):
    if isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            if k in ("id", "profile_url", "success", "query", "elapsed_seconds", "results_count", "status", "count", "total_records", "latency_ms", "type", "kind", "target", "kind_tag", "stype"):
                continue
            if v is None or v == "" or v == {} or v == []: continue
            res[k] = clean_data(v)
        return res
    elif isinstance(obj, list):
        return [clean_data(v) for v in obj if v]
    return obj

# --- ПАРСЕР (Вариант 1: Карточка профиля) ---
def render_text_results(api_results: list[dict]) -> str:
    fio, genders, dates, ages = set(), set(), set(), set()
    phones, operators, regions, countries = set(), set(), set(), set()
    emails, socials, docs, addresses, others = set(), set(), set(), set(), set()

    IGNORE_WORDS = ("service", "nebula", "spectrum", "mobius", "result", "data", "status", "error", "source", "kind", "target")

    def extract_recursive(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_lower = str(k).lower()
                if k_lower in ("id", "profile_url", "success", "query", "elapsed_seconds", "results_count", "status", "count", "total_records", "latency_ms", "type", "kind", "target", "kind_tag", "stype", "error", "source", "mobius-spectrum-phone", "result"):
                    continue
                if isinstance(v, (str, int)):
                    val_str = str(v)
                    if not val_str.strip(): continue
                    if any(word in val_str.lower() for word in IGNORE_WORDS): continue
                    
                    if k_lower in ("name", "names", "first_name", "last_name", "surname", "fathername", "fio", "full_name"): 
                        if len(val_str) > 2: fio.add(val_str)
                    elif k_lower in ("phone", "phone_2", "mobile", "number"): phones.add(val_str)
                    elif k_lower == "operator": operators.add(val_str)
                    elif k_lower == "region": regions.add(val_str)
                    elif k_lower == "country": countries.add(val_str)
                    elif k_lower in ("email", "mail"): emails.add(val_str)
                    elif k_lower in ("vk", "telegram", "instagram", "tiktok"): socials.add(f"{k.capitalize()}: {val_str}")
                    elif k_lower in ("birthday", "birth_date", "date", "dob"): dates.add(val_str)
                    elif k_lower == "age": ages.add(val_str)
                    elif k_lower in ("gender", "sex"): genders.add("Мужской" if val_str.lower() in ("m", "male", "мужской", "м") else "Женский")
                    elif k_lower in ("inn", "snils", "passport", "ogrn", "passport_series", "passport_number"): docs.add(f"{k.capitalize()}: {val_str}")
                    elif k_lower == "address": addresses.add(val_str)
                    else: others.add(f"{k.capitalize()}: {val_str}")
                else:
                    extract_recursive(v)
        elif isinstance(obj, list):
            for item in obj: extract_recursive(item)

    for res in api_results:
        extract_recursive(res.get("data", {}))

    lines = []
    if fio or phones:
        main_name = list(fio)[0] if fio else "Неизвестно"
        lines.append(f"🪪 <b>{main_name}</b>\n")
        
    lines.append("━━━━━━━━━━━━━━━")

    if phones or operators or regions:
        lines.append("📱 <b>Контакты</b>")
        if phones: lines.append(f"├ Телефон: <code>{', '.join(phones)}</code>")
        if operators: lines.append(f"├ Оператор: {', '.join(operators)}")
        if regions: lines.append(f"└ Регион: {', '.join(regions)}")
        lines.append("")
        
    if fio or genders or dates or ages:
        lines.append("👤 <b>Личные данные</b>")
        if fio: lines.append(f"├ ФИО: {', '.join(fio)}")
        if dates: lines.append(f"├ Дата рождения: {', '.join(dates)}")
        if ages: lines.append(f"├ Возраст: {', '.join(ages)}")
        if genders: lines.append(f"└ Пол: {', '.join(genders)}")
        lines.append("")

    if socials:
        lines.append("🌐 <b>Соцсети</b>")
        for i, s in enumerate(socials):
            prefix = "└" if i == len(socials) - 1 else "├"
            lines.append(f"{prefix} {s}")
        lines.append("")

    if emails:
        lines.append("📧 <b>E-mail</b>")
        for i, e in enumerate(emails):
            prefix = "└" if i == len(emails) - 1 else "├"
            lines.append(f"{prefix} {e}")
        lines.append("")

    if docs:
        lines.append("📄 <b>Документы</b>")
        for i, d in enumerate(docs):
            prefix = "└" if i == len(docs) - 1 else "├"
            lines.append(f"{prefix} {d}")
        lines.append("")
        
    if addresses:
        lines.append("🏠 <b>Адреса</b>")
        for i, a in enumerate(addresses):
            prefix = "└" if i == len(addresses) - 1 else "├"
            lines.append(f"{prefix} {a}")
        lines.append("")

    clean_others = [o for o in others if not any(word in o.lower() for word in IGNORE_WORDS)]
    if clean_others:
        lines.append("🗂 <b>Прочие данные</b>")
        for i, o in enumerate(clean_others[:10]):
            prefix = "└" if i == len(clean_others[:10]) - 1 else "├"
            lines.append(f"{prefix} {o}")
        lines.append("")

    if not lines or len(lines) < 3: 
        return "🔍 <b>Данных не найдено.</b>"

    return "\n".join(lines).strip()

# --- HTML ОТЧЕТ (Темный дизайн со скрина) ---
def build_html_report(api_results: list[dict], query: str) -> str:
    all_items = []
    for res in api_results:
        data = clean_data(res["data"])
        results_dict = data.get("results", {})
        items = results_dict if isinstance(results_dict, list) else list(results_dict.values()) if isinstance(results_dict, dict) else []
        for src_data in items:
            if not isinstance(src_data, dict): continue
            inner = src_data.get("result", {}).get("data", {}).get("results", {})
            if isinstance(inner, dict): all_items.append(inner)
            elif isinstance(inner, list): all_items.extend([i for i in inner if isinstance(i, dict)])

    fio, genders, dates, ages = set(), set(), set(), set()
    phones, operators, regions, countries = set(), set(), set(), set()
    emails, socials, docs, addresses, others = set(), set(), set(), set(), set()

    TRANSLATIONS = {"surname": "Фамилия", "name": "Имя", "fathername": "Отчество", "first_name": "Имя", "last_name": "Фамилия", "middle_name": "Отчество", "phone": "Телефон", "phone_2": "Доп. телефон", "birthday": "Дата рождения", "birth_date": "Дата рождения", "address": "Адрес", "login": "Логин", "region": "Регион", "snils": "СНИЛС", "inn": "ИНН", "passport": "Паспорт", "email": "Email", "date": "Дата", "password": "Пароль", "hash": "Хэш", "full_name": "ФИО", "operator": "Оператор", "age": "Возраст"}

    for item in all_items:
        for k, v in item.items():
            k_lower = k.lower()
            if isinstance(v, list):
                for i in v:
                    val_str = str(i)
                    if k_lower in ("phone", "phone_2", "mobile"): phones.add(val_str)
                    elif k_lower in ("email", "mail"): emails.add(val_str)
                    elif k_lower in ("vk", "telegram", "instagram", "tiktok"): socials.add(f"{TRANSLATIONS.get(k_lower, k_lower).capitalize()}: {val_str}")
                    elif k_lower in ("inn", "snils", "passport", "ogrn"): docs.add(f"{TRANSLATIONS.get(k_lower, k_lower)}: {val_str}")
                    elif k_lower in ("address", "region"): addresses.add(val_str)
                    else: others.add(f"{TRANSLATIONS.get(k_lower, k.capitalize())}: {val_str}")
            elif isinstance(v, dict): continue
            else:
                if v is None or v == "": continue
                val_str = str(v)
                if k_lower in ("name", "names", "first_name", "last_name", "surname", "fathername", "fio", "full_name"): 
                    if len(val_str) > 2: fio.add(val_str)
                elif k_lower in ("phone", "phone_2", "mobile"): phones.add(val_str)
                elif k_lower in ("email", "mail"): emails.add(val_str)
                elif k_lower in ("vk", "telegram", "instagram", "tiktok"): socials.add(f"{TRANSLATIONS.get(k_lower, k_lower).capitalize()}: {val_str}")
                elif k_lower in ("birthday", "birth_date", "date", "dob"): dates.add(val_str)
                elif k_lower == "age": ages.add(val_str)
                elif k_lower == "operator": operators.add(val_str)
                elif k_lower == "region": regions.add(val_str)
                elif k_lower == "country": countries.add(val_str)
                elif k_lower in ("gender", "sex"): genders.add("Мужской" if val_str.lower() in ("m", "male", "мужской", "м") else "Женский")
                elif k_lower in ("inn", "snils", "passport", "ogrn"): docs.add(f"{TRANSLATIONS.get(k_lower, k_lower)}: {val_str}")
                elif k_lower in ("address", "region"): addresses.add(val_str)
                else: others.add(f"{TRANSLATIONS.get(k_lower, k.capitalize())}: {val_str}")

    main_name = list(fio)[0] if fio else query
    initial = main_name[0].upper() if main_name else "?"

    def make_card(title, icon, data_set):
        if not data_set: return ""
        rows = "".join([f'<div class="data-row"><span class="d-icon">{icon}</span><span class="d-val">{html.escape(str(val))}</span></div>' for val in data_set])
        return f"""
        <div class="card">
            <div class="card-header">{title}</div>
            <div class="card-body">{rows}</div>
        </div>"""

    cards_html = ""
    cards_html += make_card("Контакты", "📱", phones)
    cards_html += make_card("Основные данные", "👤", [f"ФИО: {x}" for x in fio] + [f"ДР: {x}" for x in dates] + [f"Возраст: {x}" for x in ages])
    cards_html += make_card("Соцсети", "🌐", socials)
    cards_html += make_card("Почта", "📧", emails)
    cards_html += make_card("Документы", "📄", docs)
    cards_html += make_card("Адреса", "🏠", addresses)
    cards_html += make_card("Прочее", "🗂", others)

    details_html = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Oasis Search - {html.escape(query)}</title>
        <style>
            :root {{ --bg: #121212; --card: #1e1e1e; --text: #e0e0e0; --accent: #ffffff; --border: #333333; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: var(--bg); color: var(--text); margin: 0; padding: 20px; line-height: 1.6; }}
            .container {{ max-width: 600px; margin: 0 auto; }}
            .header {{ text-align: center; margin-bottom: 30px; }}
            .logo {{ font-size: 14px; letter-spacing: 2px; color: #888; text-transform: uppercase; margin-bottom: 10px; }}
            .title {{ font-size: 28px; font-weight: 800; color: var(--accent); margin: 0; line-height: 1.2; }}
            .query {{ font-size: 16px; color: #888; margin-top: 10px; font-family: monospace; }}
            .avatar {{ width: 60px; height: 60px; border-radius: 50%; background: #333; color: #fff; display: flex; align-items: center; justify-content: center; font-size: 24px; font-weight: bold; margin: 0 auto 15px auto; border: 2px solid #444; }}
            .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 15px; overflow: hidden; }}
            .card-header {{ background: #252525; padding: 12px 20px; font-weight: 600; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; color: #aaa; border-bottom: 1px solid var(--border); }}
            .card-body {{ padding: 10px 20px; }}
            .data-row {{ display: flex; align-items: flex-start; padding: 10px 0; border-bottom: 1px dashed #333; }}
            .data-row:last-child {{ border-bottom: none; }}
            .d-icon {{ margin-right: 12px; opacity: 0.7; }}
            .d-val {{ font-family: monospace; font-size: 14px; color: #fff; word-break: break-word; }}
            .footer {{ text-align: center; margin-top: 30px; font-size: 12px; color: #555; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div class="logo">OASIS SEARCH</div>
                <div class="avatar">{html.escape(initial)}</div>
                <h1 class="title">{html.escape(main_name)}</h1>
                <div class="query">Запрос: {html.escape(query)}</div>
            </div>
            {cards_html}
            <div class="footer">Сгенерировано автоматически &bull; {len(api_results)} источников</div>
        </div>
    </body>
    </html>
    """
    return details_html

# --- ВЕБ-ХЕНДЛЕР ДЛЯ ОТЧЕТОВ ---
async def web_report_handler(request):
    report_id = request.match_info.get('report_id')
    if report_id not in report_cache:
        return web.Response(text="<h1>404 Отчет не найден или устарел</h1><p>Сгенерируйте новый в боте.</p>", content_type="text/html", status=404)
    data = report_cache[report_id]
    html_content = build_html_report(data["results"], data["query"])
    return web.Response(text=html_content, content_type="text/html")

# --- МЕНЮ ---
def main_menu(user_id=None, is_admin=False):
    kb = [
        [InlineKeyboardButton(text="🔍 Search", callback_data="search_menu")],
        [InlineKeyboardButton(text="💎 Приобрести запросы", callback_data="buy_menu"), InlineKeyboardButton(text="🎟 Промокод", callback_data="enter_promo")],
        [InlineKeyboardButton(text="👤 Мой аккаунт", callback_data="profile"), InlineKeyboardButton(text="🆘 Поддержка", url="https://t.me/anodoxx")],
        [InlineKeyboardButton(text="👥 Рефералка", callback_data="ref_link"), InlineKeyboardButton(text="📄 Условия", callback_data="terms")],
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def search_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Обычный поиск", callback_data="search_normal")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")],
    ])

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выдать запросы", callback_data="adm_give"), InlineKeyboardButton(text="➖ Забрать", callback_data="adm_take")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm_promo")],
        [InlineKeyboardButton(text="📡 Статус API", callback_data="adm_api_status")],
        [InlineKeyboardButton(text="🛠 Тех. перерыв", callback_data="adm_maintenance")],
        [InlineKeyboardButton(text="🛡 Добавить админа", callback_data="adm_add_admin")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_menu")]
    ])

def promo_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="promo_create")],
        [InlineKeyboardButton(text="📋 Список промокодов", callback_data="promo_list")],
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="admin_panel")]
    ])

# --- ОБРАБОТЧИКИ ---
@dp.message(CommandStart())
async def start(msg: Message, command: CommandStart):
    referrer_id = None
    if command.args and command.args.isdigit():
        referrer_id = int(command.args)
        if referrer_id == msg.from_user.id: referrer_id = None
    is_new = await add_user(msg.from_user.id, msg.from_user.username, referrer_id)
    if is_new and referrer_id:
        await add_requests(referrer_id, 2) 
        try: await msg.bot.send_message(referrer_id, f"🎉 По вашей ссылке пришел новый пользователь! Вам начислено <b>+2 запроса</b>.", parse_mode="HTML")
        except: pass

    total_users = await get_total_users()
    is_adm = await is_admin(msg.from_user.id)
    welcome_msg = f"{WELCOME_TEXT}\n\n📊 Нас уже: <b>{total_users}</b> пользователей."
    await msg.answer(welcome_msg, parse_mode="HTML", reply_markup=main_menu(msg.from_user.id, is_adm))

@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): 
        return await cb.answer("⛔ Нет доступа", show_alert=True)
    await cb.message.edit_text("👑 <b>Админ-панель</b>\nВыберите действие:", parse_mode="HTML", reply_markup=admin_menu())
    await cb.answer()

@dp.callback_query(F.data == "adm_maintenance")
async def cb_adm_maintenance(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    
    current_status = await get_maintenance()
    new_status = "0" if current_status else "1"
    await set_maintenance(new_status)
    
    status_text = "ВКЛЮЧЕН 🔴" if new_status == "1" else "ВЫКЛЮЧЕН 🟢"
    btn_text = "Выключить (Разрешить поиск) 🟢" if new_status == "1" else "Включить (Заблокировать поиск) 🔴"
    
    await cb.message.edit_text(
        f"🛠 <b>Технический перерыв</b>\n\n"
        f"Текущий статус: <b>{status_text}</b>\n\n"
        f"<i>Когда перерыв включен, обычные юзеры не могут делать запросы. У админов поиск работает всегда.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn_text, callback_data="adm_maintenance")],
            [InlineKeyboardButton(text="⬅️ В админку", callback_data="admin_panel")]
        ])
    )
    await cb.answer()

@dp.callback_query(F.data == "adm_api_status")
async def cb_adm_api_status(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    
    await cb.message.edit_text("📡 <b>Проверяю статус API...</b>\n<i>Это займет пару секунд.</i>", parse_mode="HTML")
    
    test_q = "79825207224"
    statuses = []
    
    start = time.time()
    succ_m, err_m = await fetch_api(API_URL_MOBIUS, {"q": test_q}, headers={"X-API-Key": API_KEY_MOBIUS})
    t_m = round(time.time() - start, 2)
    statuses.append(f"{'🟢' if succ_m else '🔴'} <b>Mobius:</b> {'OK' if succ_m else html.escape(str(err_m))[:50]} ({t_m}с)")
    
    start = time.time()
    succ_n, err_n = await fetch_nebula(test_q)
    t_n = round(time.time() - start, 2)
    statuses.append(f"{'🟢' if succ_n else '🔴'} <b>Nebula:</b> {'OK' if succ_n else html.escape(str(err_n))[:50]} ({t_n}с)")
    
    text = "📡 <b>Статус API на текущий момент:</b>\n\n" + "\n".join(statuses)
    text += "\n\n<i>🟢 - Отвечает. 🔴 - Ошибка (в скобках причина).</i>"
    
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=admin_menu())
    await cb.answer()

@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    bal = await get_balance(cb.from_user.id)
    ref_count = await get_invited_count(cb.from_user.id)
    total_users = await get_total_users()
    uname = f"@{cb.from_user.username}" if cb.from_user.username else "нет"
    status = "👑 Админ" if await is_admin(cb.from_user.id) else "👤 Юзер"
    text = (f"👤 <b>Профиль</b>\n\n🆔 ID: <code>{cb.from_user.id}</code>\n📛 Юзер: {uname}\n🎖 Статус: {status}\n💳 Баланс: <code>{bal}</code> запросов\n👥 Приглашено: <b>{ref_count}</b>\n\n📈 Всего в боте: <b>{total_users}</b> юзеров")
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]))
    await cb.answer()

@dp.callback_query(F.data == "search_menu")
async def cb_search_menu(cb: CallbackQuery):
    await cb.message.edit_text("Выберите тип поиска:", reply_markup=search_menu())
    await cb.answer()

@dp.callback_query(F.data == "search_normal")
async def cb_search_normal(cb: CallbackQuery):
    await cb.message.edit_text("🔍 Отправь запрос в чат (номер, email, ФИО и т.д.)", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]))
    await cb.answer()

@dp.callback_query(F.data == "ref_link")
async def cb_ref_link(cb: CallbackQuery):
    ref_link = f"https://t.me/{BOT_USERNAME}?start={cb.from_user.id}"
    share_text = f"Попробуй этот OSINT-бот! Ссылка: {ref_link}"
    text = f"👥 <b>Пригласи друга</b>\n\nЗа каждого, кто зайдет по ссылке, ты получишь <b>+2 запроса</b>.\n\n🔗 <code>{ref_link}</code>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url={ref_link}&text={share_text}")],[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data == "terms")
async def cb_terms(cb: CallbackQuery):
    text = "📄 <b>Условия</b>\n\n1. Бот агрегирует открытые данные.\n2. Запрещено использовать для травли.\n3. Администрация не несет ответственности."
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]))
    await cb.answer()

@dp.callback_query(F.data == "back_to_menu")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    is_adm = await is_admin(cb.from_user.id)
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu(cb.from_user.id, is_adm))
    await cb.answer()

# --- ПОКУПКА ЗАПРОСОВ ---
@dp.callback_query(F.data == "buy_menu")
async def cb_buy_menu(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10 запросов", callback_data="buy_10"), InlineKeyboardButton(text="50 запросов", callback_data="buy_50")],
        [InlineKeyboardButton(text="100 запросов", callback_data="buy_100"), InlineKeyboardButton(text="1000 запросов", callback_data="buy_1000")],
        [InlineKeyboardButton(text="✏️ Ввести свое количество", callback_data="buy_custom")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])
    await cb.message.edit_text("💎 <b>Выберите пакет запросов:</b>", parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("buy_"))
async def cb_buy_package(cb: CallbackQuery, state: FSMContext):
    if cb.data == "buy_custom": return
    amount = int(cb.data.split("_")[1])
    stars = amount * 7
    await state.update_data(buy_amount=amount, stars=stars)
    
    text = (
        f"💳 <b>Оплата: {amount} запросов</b>\n\n"
        f"Вам нужно перевести <b>{stars} ⭐ (Telegram Stars)</b> пользователю @anodoxx.\n"
        f"<i>(1 запрос = 7 звезд)</i>\n\n"
        f"1. Откройте @anodoxx\n"
        f"2. Отправьте {stars} звезд\n"
        f"3. После перевода нажмите кнопку «✅ Я оплатил» и отправьте скриншот/чек."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid_stars_{amount}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]
    ])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(BuyStates.wait_hash)
    await cb.answer()

@dp.callback_query(F.data == "buy_custom")
async def cb_buy_custom(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("✏️ Введите количество запросов:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]]))
    await state.set_state(BuyStates.wait_custom_amount)
    await cb.answer()

@dp.message(BuyStates.wait_custom_amount)
async def process_custom_amount(msg: Message, state: FSMContext):
    try:
        amount = int(msg.text)
        if amount <= 0: raise ValueError
    except ValueError:
        return await msg.answer("❌ Введите целое число.")
    
    stars = amount * 7
    await state.update_data(buy_amount=amount, stars=stars)
    
    text = (
        f"💳 <b>Оплата: {amount} запросов</b>\n\n"
        f"Вам нужно перевести <b>{stars} ⭐ (Telegram Stars)</b> пользователю @anodoxx.\n"
        f"<i>(1 запрос = 7 звезд)</i>\n\n"
        f"1. Откройте @anodoxx\n"
        f"2. Отправьте {stars} звезд\n"
        f"3. После перевода нажмите кнопку «✅ Я оплатил» и отправьте скриншот/чек."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid_stars_{amount}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]
    ])
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(BuyStates.wait_hash)

@dp.callback_query(F.data.startswith("paid_"))
async def cb_paid(cb: CallbackQuery, state: FSMContext):
    amount = int(cb.data.split("_")[2])
    await state.update_data(buy_amount=amount, currency="STARS")
    
    await cb.message.edit_text(
        "📩 <b>Отправь чек</b>\n\n"
        "Пришли сюда скриншот перевода звезд пользователю @anodoxx одним сообщением."
    )
    await state.set_state(BuyStates.wait_hash)
    await cb.answer()

@dp.message(BuyStates.wait_hash)
async def process_tx_hash(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    amount = data.get("buy_amount")
    
    await state.clear()
    
    await msg.answer("⏳ Чек получен. Отправлен админу на проверку. Ожидайте.", reply_markup=main_menu(msg.from_user.id, False))
    
    admin_text = (
        f"💰 <b>Новая заявка на оплату!</b>\n\n"
        f"👤 @{msg.from_user.username} (ID: <code>{msg.from_user.id}</code>)\n"
        f"📦 {amount} запросов\n"
        f"💵 Оплата: STARS ({amount * 7} ⭐)\n"
        f"🔗 Сообщение с чеком переслано ниже."
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"appr_{msg.from_user.id}_{amount}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej_{msg.from_user.id}")
    ]])
    await bot.send_message(MAIN_ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=admin_kb)
    
    try:
        await bot.forward_message(chat_id=MAIN_ADMIN_ID, from_chat_id=msg.chat.id, message_id=msg.message_id)
    except:
        pass

@dp.callback_query(F.data.startswith("appr_"))
async def admin_approve(cb: CallbackQuery, bot: Bot):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    _, uid, amount = cb.data.split("_")
    await add_requests(int(uid), int(amount))
    await cb.message.edit_text(f"✅ Начислено {amount} запросов для {uid}.")
    try: await bot.send_message(int(uid), f"✅ <b>Оплата подтверждена!</b>\nНачислено <b>+{amount}</b> запросов.", parse_mode="HTML", reply_markup=main_menu(int(uid), False))
    except: pass
    await cb.answer()

@dp.callback_query(F.data.startswith("rej_"))
async def admin_reject(cb: CallbackQuery, bot: Bot):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    _, uid = cb.data.split("_")
    await cb.message.edit_text(f"❌ Отклонено для {uid}.")
    try: await bot.send_message(int(uid), "❌ Оплата отклонена. Свяжитесь с поддержкой.", reply_markup=main_menu(int(uid), False))
    except: pass
    await cb.answer()

@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if not await is_admin(msg.from_user.id): return await msg.answer("⛔ Нет доступа.")
    await msg.answer("👑 <b>Админ-панель</b>\nВыберите действие:", parse_mode="HTML", reply_markup=admin_menu())

@dp.callback_query(F.data == "adm_give")
async def cb_adm_give(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Введите <code>ID количество</code>:", parse_mode="HTML")
    await state.set_state(AdminStates.give_reqs)
    await cb.answer()

@dp.callback_query(F.data == "adm_take")
async def cb_adm_take(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Введите <code>ID количество</code> для списания:", parse_mode="HTML")
    await state.set_state(AdminStates.take_reqs)
    await cb.answer()

@dp.callback_query(F.data == "adm_add_admin")
async def cb_adm_add_admin(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID: return await cb.answer("Только главный админ может выдавать права", show_alert=True)
    await cb.message.edit_text("Введите Telegram ID пользователя, чтобы сделать его админом:", parse_mode="HTML")
    await state.set_state(AdminStates.add_admin)
    await cb.answer()

@dp.message(AdminStates.add_admin)
async def process_add_admin(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID: return
    try:
        uid = int(msg.text.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO admins (user_id, added_at) VALUES ($1, $2) ON CONFLICT DO NOTHING", uid, int(time.time()))
        await msg.answer(f"✅ Пользователь {uid} теперь админ.", reply_markup=admin_menu())
    except:
        await msg.answer("❌ Ошибка формата.", reply_markup=admin_menu())
    await state.clear()

@dp.message(AdminStates.give_reqs)
async def process_give(msg: Message, state: FSMContext, bot: Bot):
    if not await is_admin(msg.from_user.id): return
    try:
        parts = msg.text.strip().split()
        if len(parts) != 2: raise ValueError()
        uid, amount = int(parts[0]), int(parts[1])
        
        await add_requests(uid, amount)
        new_bal = await get_balance(uid)
        
        await msg.answer(f"✅ Выдано {amount} для {uid}. Новый баланс: {new_bal}", reply_markup=admin_menu())
        
        try: 
            await bot.send_message(uid, f"🎁 Вам начислено <b>+{amount}</b> запросов. Баланс: <code>{new_bal}</code>", parse_mode="HTML", reply_markup=main_menu(uid, False))
        except: 
            pass
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}. Введи: ID количество")
    await state.clear()

@dp.message(AdminStates.take_reqs)
async def process_take(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id): return
    try:
        uid, amount = map(int, msg.text.split())
        await add_requests(uid, -amount)
        await msg.answer(f"✅ Забрано {amount} у {uid}.", reply_markup=admin_menu())
    except:
        await msg.answer("❌ Ошибка формата.")
    await state.clear()

# --- ПРОМОКОДЫ ---
@dp.callback_query(F.data == "enter_promo")
async def cb_enter_promo(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("🎟 <b>Активация промокода</b>\n\nОтправь промокод одним сообщением в чат:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]]))
    await state.set_state(PromoStates.wait_code)
    await cb.answer()

@dp.message(PromoStates.wait_code)
async def process_promo_code(msg: Message, state: FSMContext, bot: Bot):
    raw_text = msg.text or ""
    code = re.sub(r'[^A-Za-z0-9]', '', raw_text).upper()
    await state.clear()
    
    try:
        await bot.send_message(
            MAIN_ADMIN_ID, 
            f"🔍 <b>Ввод промокода</b>\nЮзер: @{msg.from_user.username} (<code>{msg.from_user.id}</code>)\nОчищено: <code>{html.escape(code)}</code>", 
            parse_mode="HTML"
        )
    except: pass
    
    async with db_pool.acquire() as conn:
        used = await conn.fetchrow("SELECT 1 FROM promo_uses WHERE user_id=$1 AND code=$2", msg.from_user.id, code)
        if used:
            return await msg.answer("❌ Ты уже активировал этот промокод.", reply_markup=main_menu(msg.from_user.id, await is_admin(msg.from_user.id)))
            
        promo = await conn.fetchrow("SELECT * FROM promocodes WHERE code=$1", code)
        if not promo:
            return await msg.answer(f"❌ Промокод <code>{html.escape(code)}</code> не найден.", parse_mode="HTML", reply_markup=main_menu(msg.from_user.id, await is_admin(msg.from_user.id)))
            
        if promo['expires_at'] > 0 and promo['expires_at'] < int(time.time()):
            return await msg.answer("❌ Срок действия истек.", reply_markup=main_menu(msg.from_user.id, await is_admin(msg.from_user.id)))
        if promo['current_uses'] >= promo['max_uses']:
            return await msg.answer("❌ Лимит активаций исчерпан.", reply_markup=main_menu(msg.from_user.id, await is_admin(msg.from_user.id)))
            
        await add_requests(msg.from_user.id, promo['req_amount'])
        await conn.execute("UPDATE promocodes SET current_uses = current_uses + 1 WHERE code=$1", code)
        await conn.execute("INSERT INTO promo_uses (user_id, code, used_at) VALUES ($1, $2, $3)", msg.from_user.id, code, int(time.time()))
    
    new_bal = await get_balance(msg.from_user.id)
    await msg.answer(f"✅ <b>Промокод активирован!</b>\nНачислено <b>+{promo['req_amount']}</b> запросов.\nБаланс: <code>{new_bal}</code>", parse_mode="HTML", reply_markup=main_menu(msg.from_user.id, await is_admin(msg.from_user.id)))
    
    try:
        await bot.send_message(MAIN_ADMIN_ID, f"✅ <b>Успешная активация!</b>\nЮзер: {msg.from_user.id}\nКод: {code}\nНовый баланс: {new_bal}", parse_mode="HTML")
    except: pass

@dp.callback_query(F.data == "adm_promo")
async def cb_adm_promo(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("🎟 <b>Управление промокодами</b>", parse_mode="HTML", reply_markup=promo_menu())
    await cb.answer()

@dp.callback_query(F.data == "promo_list")
async def cb_promo_list(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    async with db_pool.acquire() as conn:
        promos = await conn.fetch("SELECT * FROM promocodes ORDER BY created_at DESC LIMIT 20")
    
    if not promos:
        return await cb.message.edit_text("Список промокодов пуст.", reply_markup=promo_menu())
        
    text = "🎟 <b>Последние промокоды:</b>\n\n"
    for p in promos:
        exp = datetime.fromtimestamp(p['expires_at']).strftime('%d.%m.%Y') if p['expires_at'] > 0 else "Бессрочно"
        status = "🔴" if p['expires_at'] > 0 and p['expires_at'] < int(time.time()) or p['current_uses'] >= p['max_uses'] else "🟢"
        text += f"{status} <code>{p['code']}</code> | +{p['req_amount']} req | {p['current_uses']}/{p['max_uses']} | до {exp}\n"
        
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=promo_menu())
    await cb.answer()

@dp.callback_query(F.data == "promo_create")
async def cb_promo_create(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Введи данные промокода в формате:\n<code>КОД КОЛВО_ЗАПРОСОВ ЛИМИТ_АКТИВАЦИЙ ДНЕЙ</code>\n\nПример:\n<code>OASIS 10 50 7</code>\n(Код OASIS, 10 запросов, 50 активаций, действует 7 дней)\n\n<i>Для бессрочного введи 0 в конце.</i>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm_promo")]]))
    await state.set_state(AdminStates.promo_create)
    await cb.answer()

@dp.message(AdminStates.promo_create)
async def process_promo_create(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id): return
    try:
        parts = msg.text.strip().split()
        if len(parts) != 4: raise ValueError()
        
        code = re.sub(r'[^A-Za-z0-9]', '', parts[0]).upper()
        req_amount, max_uses, days = int(parts[1]), int(parts[2]), int(parts[3])
        expires_at = int(time.time()) + (days * 86400) if days > 0 else 0
        
        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT code FROM promocodes WHERE code=$1", code)
            if existing:
                await msg.answer("❌ Такой промокод уже существует. Придумай другой код.", reply_markup=promo_menu())
                await state.clear()
                return
                
            await conn.execute("""
                INSERT INTO promocodes (code, req_amount, max_uses, expires_at, created_at)
                VALUES ($1, $2, $3, $4, $5)
            """, code, req_amount, max_uses, expires_at, int(time.time()))
            
        await msg.answer(f"✅ Промокод <code>{code}</code> создан!\n\n📦 Запросов: {req_amount}\n👥 Лимит: {max_uses}\n⏳ Срок: {days if days > 0 else 'Бессрочно'}", parse_mode="HTML", reply_markup=promo_menu())
    except Exception as e:
        await msg.answer(f"❌ Ошибка формата. Введи: КОД КОЛВО_ЗАПРОСОВ ЛИМИТ ДНЕЙ\n(Пример: TEST123 10 5 1)", reply_markup=promo_menu())
    await state.clear()

# --- ПОИСК И ГЕНЕРАЦИЯ ---
async def process_search(msg: Message, q: str, bot: Bot):
    is_adm = await is_admin(msg.from_user.id)
    
    if not is_adm and await get_maintenance():
        return await msg.answer("🛠 <b>Бот на техническом перерыве.</b>\n\nМы обновляем базы данных. Пожалуйста, зайдите позже.", parse_mode="HTML", reply_markup=main_menu(msg.from_user.id, is_adm))

    if not is_adm and not await deduct_request(msg.from_user.id):
        return await msg.answer("❌ Закончились запросы. Купите в меню.", reply_markup=main_menu(msg.from_user.id, is_adm))

    note = await msg.answer(f"🔍 <b>Запуск поиска:</b> {html.escape(q)}\n\n<i>Опрашиваем базы...</i>", parse_mode="HTML")
    api_results = await query_all_apis(q)
    
    if not api_results:
        if not is_adm: await add_requests(msg.from_user.id, 1)
        return await note.edit_text("❌ Все базы вернули ошибку или ничего не найдено.")
    
    text_report = render_text_results(api_results)
    bal = await get_balance(msg.from_user.id)
    
    report_id = str(random.randint(100000, 999999))
    html_cache[report_id] = q
    
    web_id = str(random.randint(1000000, 9999999))
    report_cache[web_id] = {"query": q, "results": api_results}
    web_link = f"{WEBHOOK_HOST}/report/{web_id}"
    
    raw_id = str(random.randint(10000, 99999))
    html_cache[raw_id] = json.dumps(api_results, indent=2, ensure_ascii=False)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Веб-отчет", url=web_link)],
        [InlineKeyboardButton(text="📄 HTML-файл", callback_data=f"html_{report_id}")],
        [InlineKeyboardButton(text="📝 Сырой JSON", callback_data=f"raw_{raw_id}")]
    ])
    
    await note.edit_text(
        f"<b>Oasis Search</b>\n"
        f"├ Запрос: {html.escape(q)}\n"
        f"└ Тип: Nonfull\n\n"
        f"{text_report}\n\n"
        f"💰 Остаток: {bal}", 
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb
    )

@dp.message()
async def auto_search(msg: Message, state: FSMContext, bot: Bot):
    if msg.text and not msg.text.startswith("/"):
        current_state = await state.get_state()
        if current_state is not None: return
        
        code = re.sub(r'[^A-Za-z0-9]', '', msg.text).upper()
        async with db_pool.acquire() as conn:
            promo = await conn.fetchrow("SELECT * FROM promocodes WHERE code=$1", code)
            if promo:
                return await process_promo_code(msg, state, bot)
        
        await process_search(msg, msg.text.strip(), bot)

@dp.callback_query(F.data.startswith("html_"))
async def send_html_file(cb: CallbackQuery):
    report_id = cb.data.replace("html_", "")
    q = html_cache.get(report_id)
    
    if not q:
        return await cb.answer("Отчёт устарел, сделайте новый запрос.", show_alert=True)
        
    await cb.answer("Генерирую...")
    note = await cb.message.reply("⏳ Собираю...")
    api_results = await query_all_apis(q)
    if not api_results: return await note.edit_text("❌ Данные устарели или ошибка.")
    file = BufferedInputFile(build_html_report(api_results, q).encode('utf-8'), filename=f"oasis_{q[:10]}.html")
    await cb.message.answer_document(document=file, caption=f"📄 Отчет: <code>{html.escape(q)}</code>", parse_mode="HTML")
    await note.delete()

@dp.callback_query(F.data.startswith("raw_"))
async def send_raw_json(cb: CallbackQuery):
    raw_id = cb.data.replace("raw_", "")
    raw_text = html_cache.get(raw_id)
    if not raw_text:
        return await cb.answer("Устарело", show_alert=True)
    
    if len(raw_text) > 3000:
        raw_text = raw_text[:3000] + "\n\n... (обрезано)"
        
    await cb.message.answer(f"<pre>{html.escape(raw_text)}</pre>", parse_mode="HTML")
    await cb.answer()

async def on_startup(bot: Bot):
    await init_db()
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook: {WEBHOOK_URL}")

async def health_check(request):
    return web.Response(text="OK")

def main():
    bot = Bot(BOT_TOKEN)
    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/report/{report_id}", web_report_handler)
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
