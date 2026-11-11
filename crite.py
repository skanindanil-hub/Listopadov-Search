# OASIS SEARCH (Final Sync Version)
import asyncio
import json
import html
import re
import os
import time
import logging
import random
import csv
import io
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

# --- НАСТРОЙКИ ОПЛАТЫ ---
CRYPTO_WALLET = "UQA7k7FbUJXC1ko40uBYfvv-jZ4WGnEiEIVsFcffXtFOU3pM"
STARS_PRICE = 300
CRYPTO_PRICE = 5.0

# --- НАСТРОЙКИ RENDER ---
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://listopadov-search-1.onrender.com")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

logging.basicConfig(level=logging.INFO)
dp = Dispatcher(storage=MemoryStorage())

db_pool = None
html_cache = {} 
report_cache = {} 
unlocked_reports = set()
IS_MAINTENANCE = False 
LAST_SEARCH_TIME = {} 

class BuyStates(StatesGroup):
    wait_custom_amount = State()
    wait_hash = State()

class AdminStates(StatesGroup):
    give_reqs = State()
    take_reqs = State()
    promo_create = State()
    ban_user = State()

class PromoStates(StatesGroup):
    wait_code = State()

class SearchStates(StatesGroup):
    wait_query = State()

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
                joined_at INTEGER,
                last_bonus_time BIGINT DEFAULT 0,
                is_banned BOOLEAN DEFAULT FALSE
            )
        """)
        try:
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_bonus_time BIGINT DEFAULT 0")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE")
        except: pass

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
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value BIGINT DEFAULT 0
            )
        """)
        await conn.execute("INSERT INTO stats (key, value) VALUES ('total_searches', 0) ON CONFLICT DO NOTHING")

async def is_admin(user_id):
    if user_id == MAIN_ADMIN_ID: return True
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM admins WHERE user_id=$1", user_id)
        return bool(row)

async def is_banned(user_id):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_banned FROM users WHERE user_id=$1", user_id)
        return row and row['is_banned']

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
        await conn.execute("UPDATE stats SET value = value + 1 WHERE key = 'total_searches'")
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
    if q.startswith("@") or q.startswith("t.me/") or q.startswith("https://t.me/"): return "tg"
    if re.match(r"^[А-ЯA-Z]\d{3}[А-ЯA-Z]{2}\d{2,3}$", q.upper()): return "auto"
    return "fio"

def get_operator(phone: str) -> str:
    clean_phone = re.sub(r'\D', '', phone)
    if len(clean_phone) == 11 and clean_phone.startswith('8'):
        clean_phone = '7' + clean_phone[1:]
    elif len(clean_phone) == 10:
        clean_phone = '7' + clean_phone

    if len(clean_phone) != 11 or not clean_phone.startswith('7'):
        return ""

    prefix = clean_phone[1:4]
    if prefix in ['910', '911', '912', '913', '914', '915', '916', '917', '918', '919', '980', '981', '982', '983', '984', '985', '986', '987', '988', '989', '901', '903']: return "МТС"
    if prefix in ['905', '906', '909', '962', '963', '964', '965', '966', '967', '968', '969']: return "Билайн"
    if prefix in ['902', '904', '908', '950', '951', '952', '953', '955', '956', '958', '960', '961']: return "МегаФон"
    if prefix in ['900', '901', '902', '904', '908', '950', '951', '952', '953', '958', '977', '991', '992', '993', '994', '995', '996', '997', '999']: return "Т2 (Tele2)"
    return ""

# --- API ЗАПРОСЫ ---
async def fetch_mobius(q: str, q_type: str) -> tuple[bool, dict | str]:
    timeout = aiohttp.ClientTimeout(total=25)
    headers = {"X-API-Key": API_KEY_MOBIUS, "Content-Type": "application/json"}
    payload = {"query": q, "type": q_type}
    try:
        await asyncio.sleep(2) 
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(API_URL_MOBIUS, json=payload, headers=headers) as resp:
                if resp.status == 200: return True, json.loads(await resp.text())
                return False, f"HTTP {resp.status}"
    except Exception as e: return False, str(e)

async def fetch_nebula(q: str, q_type: str) -> tuple[bool, dict | str]:
    timeout = aiohttp.ClientTimeout(total=25)
    headers = {"X-API-Key": API_KEY_NEBULA}
    n_type = "auto" if q_type == "fio" else q_type
    params = {"query": q, "type": n_type}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL_NEBULA, params=params, headers=headers) as resp:
                if resp.status == 200: return True, json.loads(await resp.text())
                return False, f"HTTP {resp.status}"
    except Exception as e: return False, str(e)

async def query_all_apis(q: str, q_type: str) -> list[dict]:
    tasks = [fetch_mobius(q, q_type), fetch_nebula(q, q_type)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    combined = []
    sources = ["Mobius", "Nebula"]
    for i, res in enumerate(results):
        if isinstance(res, Exception): continue
        success, data = res
        if success and isinstance(data, dict):
            combined.append({"source": sources[i], "data": data})
    return combined

# --- ПАРСЕР (СИНХРОНИЗИРОВАННЫЙ) ---
def render_text_results(api_results: list[dict]) -> str:
    fio, phones, socials, emails, docs, others = set(), set(), set(), set(), set(), set()
    addresses = set()
    operators, tg_data = set(), set()

    IGNORE_WORDS = ("service", "nebula", "spectrum", "mobius", "result", "data", "status", "error", "source", "kind", "target", "limit", "cache", "remaining", "cached", "used", "is_premium", "search_id", "report_url")

    def extract_recursive(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_lower = str(k).lower()
                
                if k_lower in ("id", "profile_url", "success", "query", "elapsed_seconds", "results_count", "status", "count", "total_records", "latency_ms", "type", "kind", "target", "kind_tag", "stype", "error", "source", "mobius-spectrum-phone", "result", "groups", "records"):
                    continue
                    
                if isinstance(v, (str, int, bool)):
                    val_str = str(v)
                    if not val_str.strip(): continue
                    if any(word in k_lower for word in IGNORE_WORDS): continue 
                    if any(word in val_str.lower() for word in IGNORE_WORDS): continue 
                    
                    if k_lower in ("username", "tg_username", "telegram", "tg", "tg_id"):
                        if len(val_str) > 1 and val_str != "None": 
                            if k_lower == "username" and not val_str.startswith("@"): val_str = f"@{val_str}"
                            tg_data.add(f"{k.capitalize()}: {val_str}")
                    elif k_lower in ("first_name", "last_name"):
                        if val_str.strip(): tg_data.add(f"{k.capitalize()}: {val_str}")
                    elif k_lower == "bio":
                        if val_str.strip(): tg_data.add(f"Bio: {val_str}")
                    elif k_lower in ("name", "names", "first_name", "last_name", "surname", "fathername", "fio", "full_name", "фио"): 
                        if len(val_str) > 2 and not re.search(r'\d', val_str) and re.search(r'[А-Яа-я]', val_str): fio.add(val_str)
                    elif k_lower in ("phone", "phone_2", "mobile", "number", "телефон"): 
                        phones.add(val_str)
                        op = get_operator(val_str)
                        if op: operators.add(op)
                    elif k_lower in ("operator", "оператор"): 
                        if val_str.lower() not in ("мтс", "билайн", "мегафон", "т2"): operators.add(val_str)
                    elif k_lower in ("region", "регион", "город", "city"): others.add(f"Регион: {val_str}")
                    elif k_lower in ("country", "страна"): others.add(f"Страна: {val_str}")
                    elif k_lower in ("email", "mail", "почта"): emails.add(val_str)
                    elif k_lower in ("vk", "instagram", "tiktok", "facebook"): socials.add(f"{k.capitalize()}: {val_str}")
                    elif k_lower in ("birthday", "birth_date", "date", "dob", "дата рождения"): 
                        if "T" in val_str:
                            parts = val_str.split("T")[0].split("-")
                            if len(parts) == 3: val_str = f"{parts[2]}.{parts[1]}.{parts[0]}"
                        others.add(f"Дата рождения: {val_str}")
                    elif k_lower in ("age", "возраст"): others.add(f"Возраст: {val_str}")
                    elif k_lower in ("gender", "sex", "пол"): others.add(f"Пол: {'Мужской' if val_str.lower() in ('m', 'male', 'мужской', 'м') else 'Женский'}")
                    elif k_lower in ("inn", "snils", "passport", "ogrn", "passport_series", "passport_number", "инн", "снилс", "паспорт"): docs.add(f"{k.capitalize()}: {val_str}")
                    elif k_lower in ("address", "адрес"): addresses.add(val_str)
                    else: others.add(f"{k.capitalize()}: {val_str}")
                else:
                    extract_recursive(v)
        elif isinstance(obj, list):
            for item in obj: extract_recursive(item)

    for res in api_results:
        extract_recursive(res.get("data", {}))

    lines = []
    if fio:
        main_name = list(fio)[0]
        lines.append(f"<b>🪪 {main_name}</b>\n")
    
    if tg_data:
        lines.append("<b>📟 Telegram:</b>")
        for tg in list(tg_data)[:5]: lines.append(f"> {tg}")
        lines.append("")

    if phones or operators:
        lines.append("<b>📱 Связь:</b>")
        if phones: lines.append(f"> Телефон: {', '.join(phones)}")
        if operators: lines.append(f"> Оператор: {', '.join(operators)}")
        lines.append("")
        
    if others:
        lines.append("<b>👤 Основные данные:</b>")
        for o in list(others)[:6]: lines.append(f"> {o}")
        lines.append("")

    if emails:
        lines.append("<b>📧 E-mail:</b>")
        for e in emails: lines.append(f"> {e}")
        lines.append("")

    if docs:
        lines.append("<b>📄 Документы:</b>")
        for d in docs: lines.append(f"> {d}")
        lines.append("")

    if addresses:
        lines.append("<b>🏠 Адреса:</b>")
        for a in addresses: lines.append(f"> {a}")
        lines.append("")

    if socials:
        lines.append("<b>🌐 Соцсети:</b>")
        for s in socials: lines.append(f"> {s}")
        lines.append("")

    if len(lines) <= 1: return "🔍 <b>Данных не найдено.</b>"

    return "\n".join(lines).strip()

# --- HTML ОТЧЕТ (BlackBox Style) ---
def build_html_report(api_results: list[dict], query: str, is_locked: bool = False, report_id: str = None) -> str:
    fio, phones, socials, emails, docs, others = set(), set(), set(), set(), set(), set()
    addresses, sources = set(), set()
    
    def extract(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_l = k.lower()
                if isinstance(v, str) and len(v) > 1:
                    if k_l in ("name", "fio", "full_name", "фио"): fio.add(v)
                    elif k_l in ("phone", "phone_2", "mobile", "телефон"): phones.add(v)
                    elif k_l in ("email", "mail", "почта"): emails.add(v)
                    elif k_l in ("passport", "snils", "inn", "паспорт", "снилс", "инн"): docs.add(f"{k}: {v}")
                    elif k_l in ("address", "адрес", "region", "регион"): addresses.add(v)
                    elif k_l in ("vk", "telegram", "instagram", "tiktok", "facebook"): socials.add(f"{k.capitalize()}: {v}")
                    elif k_l in ("source", "database", "источник"): sources.add(v)
                elif isinstance(v, (dict, list)): extract(v)
        elif isinstance(obj, list):
            for i in obj: extract(i)
            
    for r in api_results: extract(r.get("data", {}))
    
    total_fields = len(fio) + len(phones) + len(emails) + len(docs) + len(addresses)
    completeness = min(100, total_fields * 15) 

    if is_locked:
        content_html = f"""
        <div style="text-align: center; padding: 50px 20px;">
            <h2 style="color: #ff4444;">🔒 ДАННЫЕ СКРЫТЫ</h2>
            <p style="color: #888; margin-top: 20px;">Этот отчет защищен. Чтобы получить доступ к данным, свяжитесь с администратором: <b>@anodoxx</b></p>
            <p style="color: #555; font-size: 12px; margin-top: 40px;">ID отчета: {report_id}</p>
        </div>
        """
    else:
        main_fio = list(fio)[0] if fio else query
        initials = "".join([w[0].upper() for w in main_fio.split()[:2]]) if main_fio else "??"

        phone_html = "".join([f'<div class="data-item"><div class="d-icon">📞</div><div class="d-content"><div class="d-label">Телефон</div><div class="d-value">{html.escape(p)}</div></div></div>' for p in phones])
        email_html = "".join([f'<div class="data-item"><div class="d-icon">✉️</div><div class="d-content"><div class="d-label">Почта</div><div class="d-value">{html.escape(e)}</div></div></div>' for e in emails])
        doc_html = "".join([f'<div class="data-item"><div class="d-icon">📄</div><div class="d-content"><div class="d-label">Документ</div><div class="d-value">{html.escape(d)}</div></div></div>' for d in docs])
        addr_html = "".join([f'<div class="data-item"><div class="d-icon">📍</div><div class="d-content"><div class="d-label">Адрес</div><div class="d-value">{html.escape(a)}</div></div></div>' for a in addresses])
        soc_html = "".join([f'<div class="data-item"><div class="d-icon">🌐</div><div class="d-content"><div class="d-label">Соцсеть</div><div class="d-value">{html.escape(s)}</div></div></div>' for s in socials])
        
        sources_html = "".join([f'<div class="source-item"><span>{html.escape(s)}</span><span class="source-count">1</span></div>' for s in sources])

        content_html = f"""
        <div class="profile-card">
            <div class="avatar">{html.escape(initials)}</div>
            <div class="profile-info">
                <h2>{html.escape(main_fio)}</h2>
                <div class="sub-info">{html.escape(query)}</div>
            </div>
        </div>

        <div class="badges">
            <div class="badge">📊 {len(sources)} источников</div>
            <div class="badge">📞 {len(phones)} телефонов</div>
            <div class="badge">📧 {len(emails)} почт</div>
            <div class="badge">📍 {len(addresses)} адресов</div>
        </div>

        <div class="completeness">
            <div class="c-header"><span>Полнота профиля</span><span>{completeness}%</span></div>
            <div class="c-bar"><div class="c-fill" style="width: {completeness}%"></div></div>
        </div>

        {f'<div class="data-section">{phone_html}</div>' if phone_html else ''}
        {f'<div class="data-section">{email_html}</div>' if email_html else ''}
        {f'<div class="data-section">{addr_html}</div>' if addr_html else ''}
        {f'<div class="data-section">{doc_html}</div>' if doc_html else ''}
        {f'<div class="data-section">{soc_html}</div>' if soc_html else ''}

        {f'<div class="sources-section"><h3>ИСТОЧНИКИ</h3>{sources_html}</div>' if sources_html else ''}
        """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Oasis Search - {html.escape(query)}</title>
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
            .c-fill {{ height: 100%; background: #333; border-radius: 4px; }}
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
                <div class="header-logo">OASIS SEARCH</div>
                <div>/ Report</div>
            </div>
            {content_html}
            <div class="footer">Сгенерировано автоматически &bull; {len(api_results)} источников</div>
        </div>
    </body>
    </html>
    """
    return html_content

# --- ВЕБ-ХЕНДЛЕР ---
async def web_report_handler(request):
    report_id = request.match_info.get('report_id')
    if report_id not in report_cache:
        return web.Response(text="<h1>404 Отчет не найден</h1>", content_type="text/html", status=404)
    data = report_cache[report_id]
    is_locked = data.get("locked", False) and (report_id not in unlocked_reports)
    html_content = build_html_report(data["results"], data["query"], is_locked=is_locked, report_id=report_id)
    return web.Response(text=html_content, content_type="text/html")

# --- МЕНЮ ---
def main_menu(user_id=None, is_admin=False):
    kb = [
        [InlineKeyboardButton(text="🔍 Search", callback_data="search_menu")],
        [InlineKeyboardButton(text="💎 Купить запросы", callback_data="buy_menu"), InlineKeyboardButton(text="🎟 Промокод", callback_data="enter_promo")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"), InlineKeyboardButton(text="🆘 Поддержка", url="https://t.me/anodoxx")],
        [InlineKeyboardButton(text="🔒 Скрыть данные (300⭐ / 5$)", callback_data="hide_data_menu")],
        [InlineKeyboardButton(text="🎁 Бонус", callback_data="daily_bonus")],
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выдать", callback_data="adm_give"), InlineKeyboardButton(text="➖ Забрать", callback_data="adm_take")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm_promo"), InlineKeyboardButton(text="📊 Аналитика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="📡 Статус API", callback_data="adm_api_status"), InlineKeyboardButton(text="🚫 Бан", callback_data="adm_ban")],
        [InlineKeyboardButton(text="🛠 Тех. перерыв", callback_data="adm_maintenance")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_menu")]
    ])

def promo_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="promo_create")],
        [InlineKeyboardButton(text="📋 Список промокодов", callback_data="promo_list")],
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="admin_panel")]
    ])

def buy_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10 запросов", callback_data="buy_10"), InlineKeyboardButton(text="50 запросов", callback_data="buy_50")],
        [InlineKeyboardButton(text="100 запросов", callback_data="buy_100"), InlineKeyboardButton(text="1000 запросов", callback_data="buy_1000")],
        [InlineKeyboardButton(text="✏️ Свое количество", callback_data="buy_custom")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])

def hide_data_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Оплатить 300 Stars", callback_data="pay_hide_stars")],
        [InlineKeyboardButton(text="💎 Оплатить 5$ (Crypto)", callback_data="pay_hide_crypto")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])

# --- ОБРАБОТЧИКИ ---
@dp.message(CommandStart())
async def start(msg: Message, command: CommandStart):
    if await is_banned(msg.from_user.id):
        return await msg.answer("Пошел нахуй")
        
    referrer_id = None
    if command.args and command.args.isdigit():
        referrer_id = int(command.args)
        if referrer_id == msg.from_user.id: referrer_id = None
    is_new = await add_user(msg.from_user.id, msg.from_user.username, referrer_id)
    if is_new and referrer_id:
        await add_requests(referrer_id, 2) 

    total_users = await get_total_users()
    is_adm = await is_admin(msg.from_user.id)
    welcome_msg = f"{WELCOME_TEXT}\n\n📊 Нас уже: <b>{total_users}</b> пользователей."
    await msg.answer(welcome_msg, parse_mode="HTML", reply_markup=main_menu(msg.from_user.id, is_adm))

@dp.callback_query(F.data == "hide_data_menu")
async def cb_hide_data_menu(cb: CallbackQuery):
    try:
        await cb.message.edit_text(
            "🔒 <b>Скрытие данных</b>\n\n"
            "Эта функция позволяет скрыть данные. После оплаты бот пришлет вам отчет в чат.\n\n"
            "Выберите способ оплаты:",
            parse_mode="HTML", reply_markup=hide_data_menu()
        )
        await cb.answer()
    except Exception as e:
        logging.error(f"Ошибка в hide_data_menu: {e}")
        await cb.answer("Ошибка. Попробуйте позже.", show_alert=True)

@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        banned = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = TRUE")
        total_searches = await conn.fetchval("SELECT value FROM stats WHERE key='total_searches'")
    
    await cb.message.edit_text(
        f"📊 <b>Аналитика</b>\n\n"
        f"👥 Всего юзеров: {total_users}\n"
        f"🚫 Забанено: {banned}\n"
        f"🔍 Всего поисков: {total_searches}",
        parse_mode="HTML", reply_markup=admin_menu()
    )
    await cb.answer()

@dp.callback_query(F.data == "adm_ban")
async def cb_adm_ban(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Введите <code>ID</code> пользователя для бана/разбана:", parse_mode="HTML")
    await state.set_state(AdminStates.ban_user)
    await cb.answer()

@dp.message(AdminStates.ban_user)
async def process_ban(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id): return
    try:
        uid = int(msg.text.strip())
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT is_banned FROM users WHERE user_id=$1", uid)
            if not row:
                return await msg.answer("❌ Пользователь не найден.", reply_markup=admin_menu())
            
            new_status = not row['is_banned']
            await conn.execute("UPDATE users SET is_banned=$1 WHERE user_id=$2", new_status, uid)
            
        status_text = "ЗАБАНЕН 🚫" if new_status else "РАЗБАНЕН ✅"
        await msg.answer(f"Пользователь <code>{uid}</code>: {status_text}", parse_mode="HTML", reply_markup=admin_menu())
    except:
        await msg.answer("❌ Ошибка формата.")
    await state.clear()

@dp.callback_query(F.data == "adm_maintenance")
async def cb_adm_maintenance(cb: CallbackQuery):
    global IS_MAINTENANCE
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    IS_MAINTENANCE = not IS_MAINTENANCE
    status_text = "ВКЛЮЧЕН 🔴" if IS_MAINTENANCE else "ВЫКЛЮЧЕН 🟢"
    btn_text = "Выключить 🟢" if IS_MAINTENANCE else "Включить 🔴"
    await cb.message.edit_text(f"🛠 <b>Тех. перерыв:</b> {status_text}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_text, callback_data="adm_maintenance")],
        [InlineKeyboardButton(text="⬅️ В админку", callback_data="admin_panel")]
    ]))
    await cb.answer()

@dp.callback_query(F.data == "adm_api_status")
async def cb_adm_api_status(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("📡 <b>Проверяю API...</b>", parse_mode="HTML")
    start = time.time()
    succ_m, err_m = await fetch_mobius("79825207224", "phone")
    t_m = round(time.time() - start, 2)
    start = time.time()
    succ_n, err_n = await fetch_nebula("79825207224", "phone")
    t_n = round(time.time() - start, 2)
    text = (
        f"📡 <b>Статус API:</b>\n\n"
        f"{'🟢' if succ_m else '🔴'} <b>Mobius:</b> {'OK' if succ_m else html.escape(str(err_m))[:30]} ({t_m}с)\n"
        f"{'🟢' if succ_n else '🔴'} <b>Nebula:</b> {'OK' if succ_n else html.escape(str(err_n))[:30]} ({t_n}с)"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=admin_menu())
    await cb.answer()

@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    bal = await get_balance(cb.from_user.id)
    uname = f"@{cb.from_user.username}" if cb.from_user.username else "нет"
    status = "👑 Админ" if await is_admin(cb.from_user.id) else "👤 Юзер"
    text = f"👤 <b>Профиль</b>\n\n🆔 ID: <code>{cb.from_user.id}</code>\n📛 Юзер: {uname}\n🎖 Статус: {status}\n💳 Баланс: <code>{bal}</code> запросов"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=main_menu(cb.from_user.id, await is_admin(cb.from_user.id)))
    await cb.answer()

@dp.callback_query(F.data == "daily_bonus")
async def cb_daily_bonus(cb: CallbackQuery):
    user_id = cb.from_user.id
    if await is_admin(user_id): return await cb.answer("У админов безлимит.", show_alert=True)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_bonus_time FROM users WHERE user_id=$1", user_id)
        last_time = row['last_bonus_time'] if row else 0
    now = int(time.time())
    if now - last_time < 86400:
        remaining = 86400 - (now - last_time)
        return await cb.answer(f"❌ Ждать: {remaining // 3600} ч. {(remaining % 3600) // 60} мин.", show_alert=True)
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET last_bonus_time=$1 WHERE user_id=$2", now, user_id)
    await add_requests(user_id, 2)
    await cb.answer("🎁 +2 запроса!", show_alert=True)

@dp.callback_query(F.data == "search_menu")
async def cb_search_menu(cb: CallbackQuery):
    await cb.message.edit_text("Выберите тип поиска:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Обычный", callback_data="search_normal")],
        [InlineKeyboardButton(text="📄 Документы", callback_data="search_docs")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")],
    ]))
    await cb.answer()

@dp.callback_query(F.data == "search_normal")
async def cb_search_normal(cb: CallbackQuery):
    await cb.message.edit_text("🔍 Отправь запрос в чат:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]))
    await cb.answer()

@dp.callback_query(F.data == "search_docs")
async def cb_search_docs(cb: CallbackQuery):
    await cb.message.edit_text("📄 Выберите тип документа:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Паспорт РФ", callback_data="doc_passport")],
        [InlineKeyboardButton(text="СНИЛС", callback_data="doc_snils")],
        [InlineKeyboardButton(text="ИНН", callback_data="doc_inn")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="search_menu")],
    ]))
    await cb.answer()

@dp.callback_query(F.data.startswith("doc_"))
async def cb_doc_selected(cb: CallbackQuery, state: FSMContext):
    doc_type = cb.data.replace("doc_", "")
    await state.update_data(doc_type=doc_type)
    await cb.message.edit_text(f"Введите данные документа ({doc_type}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]]))
    await state.set_state(SearchStates.wait_query)
    await cb.answer()

@dp.message(SearchStates.wait_query)
async def process_doc_search(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    doc_type = data.get("doc_type", "auto")
    await state.clear()
    await process_search(msg, msg.text.strip(), bot, forced_type=doc_type)

@dp.callback_query(F.data == "back_to_menu")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    is_adm = await is_admin(cb.from_user.id)
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu(cb.from_user.id, is_adm))
    await cb.answer()

# --- ПОКУПКА ЗАПРОСОВ ---
@dp.callback_query(F.data == "buy_menu")
async def cb_buy_menu(cb: CallbackQuery):
    await cb.message.edit_text("💎 <b>Выберите пакет запросов:</b>", parse_mode="HTML", reply_markup=buy_menu())
    await cb.answer()

@dp.callback_query(F.data.startswith("buy_"))
async def cb_buy_package(cb: CallbackQuery, state: FSMContext):
    if cb.data == "buy_custom": return
    amount = int(cb.data.split("_")[1])
    stars = amount * 7
    await state.update_data(buy_amount=amount, stars=stars)
    text = f"💳 <b>Оплата: {amount} запросов</b>\n\nВам нужно перевести <b>{stars} ⭐ (Telegram Stars)</b> пользователю @anodoxx.\n<i>(1 запрос = 7 звезд)</i>\n\n1. Откройте @anodoxx\n2. Отправьте {stars} звезд\n3. После перевода нажмите кнопку «✅ Я оплатил» и отправьте скриншот/чек."
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
    text = f"💳 <b>Оплата: {amount} запросов</b>\n\nВам нужно перевести <b>{stars} ⭐ (Telegram Stars)</b> пользователю @anodoxx.\n<i>(1 запрос = 7 звезд)</i>\n\n1. Откройте @anodoxx\n2. Отправьте {stars} звезд\n3. После перевода нажмите кнопку «✅ Я оплатил» и отправьте скриншот/чек."
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
    await cb.message.edit_text("📩 <b>Отправь чек</b>\n\nПришли сюда скриншот перевода звезд пользователю @anodoxx одним сообщением.")
    await state.set_state(BuyStates.wait_hash)
    await cb.answer()

@dp.message(BuyStates.wait_hash)
async def process_tx_hash(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    amount = data.get("buy_amount")
    pay_type = data.get("pay_type")
    
    if pay_type != "hide_crypto":
        await state.clear()
        await msg.answer("⏳ Чек получен. Отправлен админу на проверку. Ожидайте.", reply_markup=main_menu(msg.from_user.id, False))
        admin_text = f"💰 <b>Новая заявка на оплату!</b>\n\n👤 @{msg.from_user.username} (ID: <code>{msg.from_user.id}</code>)\n📦 {amount} запросов\n💵 Оплата: STARS ({amount * 7} ⭐)\n🔗 Сообщение с чеком переслано ниже."
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"appr_{msg.from_user.id}_{amount}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej_{msg.from_user.id}")
        ]])
        await bot.send_message(MAIN_ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=admin_kb)
        try: await bot.forward_message(chat_id=MAIN_ADMIN_ID, from_chat_id=msg.chat.id, message_id=msg.message_id)
        except: pass

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
        try: await bot.send_message(uid, f"🎁 Вам начислено <b>+{amount}</b> запросов. Баланс: <code>{new_bal}</code>", parse_mode="HTML", reply_markup=main_menu(uid, False))
        except: pass
    except: await msg.answer("❌ Ошибка формата.")
    await state.clear()

@dp.message(AdminStates.take_reqs)
async def process_take(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id): return
    try:
        uid, amount = map(int, msg.text.split())
        await add_requests(uid, -amount)
        await msg.answer(f"✅ Забрано {amount} у {uid}.", reply_markup=admin_menu())
    except: await msg.answer("❌ Ошибка формата.")
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
    
    try: await bot.send_message(MAIN_ADMIN_ID, f"🔍 <b>Ввод промокода</b>\nЮзер: @{msg.from_user.username} (<code>{msg.from_user.id}</code>)\nОчищено: <code>{html.escape(code)}</code>", parse_mode="HTML")
    except: pass
    
    async with db_pool.acquire() as conn:
        used = await conn.fetchrow("SELECT 1 FROM promo_uses WHERE user_id=$1 AND code=$2", msg.from_user.id, code)
        if used: return await msg.answer("❌ Ты уже активировал этот промокод.", reply_markup=main_menu(msg.from_user.id, await is_admin(msg.from_user.id)))
        promo = await conn.fetchrow("SELECT * FROM promocodes WHERE code=$1", code)
        if not promo: return await msg.answer(f"❌ Промокод <code>{html.escape(code)}</code> не найден.", parse_mode="HTML", reply_markup=main_menu(msg.from_user.id, await is_admin(msg.from_user.id)))
        if promo['expires_at'] > 0 and promo['expires_at'] < int(time.time()): return await msg.answer("❌ Срок действия истек.", reply_markup=main_menu(msg.from_user.id, await is_admin(msg.from_user.id)))
        if promo['current_uses'] >= promo['max_uses']: return await msg.answer("❌ Лимит активаций исчерпан.", reply_markup=main_menu(msg.from_user.id, await is_admin(msg.from_user.id)))
        await add_requests(msg.from_user.id, promo['req_amount'])
        await conn.execute("UPDATE promocodes SET current_uses = current_uses + 1 WHERE code=$1", code)
        await conn.execute("INSERT INTO promo_uses (user_id, code, used_at) VALUES ($1, $2, $3)", msg.from_user.id, code, int(time.time()))
    
    new_bal = await get_balance(msg.from_user.id)
    await msg.answer(f"✅ <b>Промокод активирован!</b>\nНачислено <b>+{promo['req_amount']}</b> запросов.\nБаланс: <code>{new_bal}</code>", parse_mode="HTML", reply_markup=main_menu(msg.from_user.id, await is_admin(msg.from_user.id)))
    try: await bot.send_message(MAIN_ADMIN_ID, f"✅ <b>Успешная активация!</b>\nЮзер: {msg.from_user.id}\nКод: {code}\nНовый баланс: {new_bal}", parse_mode="HTML")
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
    if not promos: return await cb.message.edit_text("Список промокодов пуст.", reply_markup=promo_menu())
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
    await cb.message.edit_text("Введи данные промокода в формате:\n<code>КОД КОЛВО_ЗАПРОСОВ ЛИМИТ_АКТИВАЦИЙ ДНЕЙ</code>\n\nПример:\n<code>OASIS 10 50 7</code>\n<i>Для бессрочного введи 0 в конце.</i>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm_promo")]]))
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
                await msg.answer("❌ Такой промокод уже существует.", reply_markup=promo_menu())
                await state.clear()
                return
            await conn.execute("INSERT INTO promocodes (code, req_amount, max_uses, expires_at, created_at) VALUES ($1, $2, $3, $4, $5)", code, req_amount, max_uses, expires_at, int(time.time()))
        await msg.answer(f"✅ Промокод <code>{code}</code> создан!\n\n📦 Запросов: {req_amount}\n👥 Лимит: {max_uses}\n⏳ Срок: {days if days > 0 else 'Бессрочно'}", parse_mode="HTML", reply_markup=promo_menu())
    except: await msg.answer("❌ Ошибка формата.", reply_markup=promo_menu())
    await state.clear()

# --- ПОИСК И ГЕНЕРАЦИЯ ---
async def process_search(msg: Message, q: str, bot: Bot, forced_type: str = None):
    if await is_banned(msg.from_user.id): return await msg.answer("Пошел нахуй")
    
    is_adm = await is_admin(msg.from_user.id)
    
    if not is_adm and IS_MAINTENANCE:
        return await msg.answer("🛠 <b>Бот на техническом перерыве.</b>", parse_mode="HTML", reply_markup=main_menu(msg.from_user.id, is_adm))

    if not is_adm:
        now = time.time()
        last_time = LAST_SEARCH_TIME.get(msg.from_user.id, 0)
        if now - last_time < 3:
            return await msg.answer("⏳ Подожди пару секунд перед следующим запросом.")
        LAST_SEARCH_TIME[msg.from_user.id] = now

    if not is_adm and not await deduct_request(msg.from_user.id):
        return await msg.answer("❌ Закончились запросы. Купите в меню.", reply_markup=main_menu(msg.from_user.id, is_adm))

    q_type = forced_type if forced_type else detect_type(q)
    note = await msg.answer(f"🔍 <b>Запуск поиска:</b> {html.escape(q)}\n\n<i>Опрашиваем базы (Тип: {q_type})...</i>", parse_mode="HTML")
    
    api_results = await query_all_apis(q, q_type)
    
    if not api_results:
        if not is_adm: await add_requests(msg.from_user.id, 1)
        return await note.edit_text("❌ Все базы вернули ошибку или ничего не найдено.")
    
    text_report = render_text_results(api_results)
    bal = await get_balance(msg.from_user.id)
    
    report_id = str(random.randint(100000, 999999))
    html_cache[report_id] = q
    
    web_id = str(random.randint(1000000, 9999999))
    report_cache[web_id] = {"query": q, "results": api_results, "locked": False, "text_report": text_report}
    web_link = f"{WEBHOOK_HOST}/report/{web_id}"
    
    raw_id = str(random.randint(10000, 99999))
    html_cache[raw_id] = json.dumps(api_results, indent=2, ensure_ascii=False)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Веб-отчет", url=web_link)],
        [InlineKeyboardButton(text="📄 HTML-файл", callback_data=f"html_{report_id}")],
        [InlineKeyboardButton(text="📊 Скачать CSV", callback_data=f"csv_{raw_id}")],
        [InlineKeyboardButton(text="📝 Сырой JSON", callback_data=f"raw_{raw_id}")]
    ])
    
    await note.edit_text(
        f"<b>Oasis Search</b>\n"
        f"├ Запрос: {html.escape(q)}\n"
        f"└ Тип: {q_type}\n\n"
        f"{text_report}\n\n"
        f"💰 Остаток: {bal}", 
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb
    )

@dp.message()
async def auto_search(msg: Message, state: FSMContext, bot: Bot):
    if await is_banned(msg.from_user.id): return await msg.answer("Пошел нахуй")
    
    if msg.text and not msg.text.startswith("/"):
        current_state = await state.get_state()
        if current_state is not None: return
        
        code = re.sub(r'[^A-Za-z0-9]', '', msg.text).upper()
        async with db_pool.acquire() as conn:
            promo = await conn.fetchrow("SELECT * FROM promocodes WHERE code=$1", code)
            if promo: return await process_promo_code(msg, state, bot)
        
        await process_search(msg, msg.text.strip(), bot)

@dp.callback_query(F.data.startswith("html_"))
async def send_html_file(cb: CallbackQuery):
    report_id = cb.data.replace("html_", "")
    q = html_cache.get(report_id)
    if not q: return await cb.answer("Отчёт устарел.", show_alert=True)
    await cb.answer("Генерирую...")
    note = await cb.message.reply("⏳ Собираю...")
    api_results = await query_all_apis(q, "auto")
    if not api_results: return await note.edit_text("❌ Данные устарели.")
    file = BufferedInputFile(build_html_report(api_results, q, is_locked=False).encode('utf-8'), filename=f"oasis_{q[:10]}.html")
    await cb.message.answer_document(document=file, caption=f"📄 Отчет: <code>{html.escape(q)}</code>", parse_mode="HTML")
    await note.delete()

@dp.callback_query(F.data.startswith("csv_"))
async def send_csv_file(cb: CallbackQuery):
    raw_id = cb.data.replace("csv_", "")
    raw_data = html_cache.get(raw_id)
    if not raw_data: return await cb.answer("Устарело", show_alert=True)
    
    try:
        data = json.loads(raw_data)
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';')
        writer.writerow(["Источник", "Данные"])
        
        def extract_csv(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, (str, int)): writer.writerow([k, v])
                    else: extract_csv(v)
            elif isinstance(obj, list):
                for item in obj: extract_csv(item)
                
        extract_csv(data)
        
        file = BufferedInputFile(output.getvalue().encode('utf-8-sig'), filename=f"oasis_{raw_id}.csv")
        await cb.message.answer_document(document=file, caption="📊 Данные в CSV (Excel)")
    except:
        await cb.answer("Ошибка генерации CSV", show_alert=True)
    await cb.answer()

@dp.callback_query(F.data.startswith("raw_"))
async def send_raw_json(cb: CallbackQuery):
    raw_id = cb.data.replace("raw_", "")
    raw_text = html_cache.get(raw_id)
    if not raw_text: return await cb.answer("Устарело", show_alert=True)
    if len(raw_text) > 3000: raw_text = raw_text[:3000] + "\n\n... (обрезано)"
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
