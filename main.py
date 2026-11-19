import asyncio
import logging
import os
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
BOT_TOKEN = "9024954594:AAGj3lihxCTSGgCMaeE_mCi2519paMgIYms"
BOT_USERNAME = "@MobiusSearchBot"

MAIN_ADMIN_ID = 8394631079
ADMIN_USERNAME = "@youranonglobal"

CHANNEL_ID = -1004330390390
CHANNEL_URL = "https://t.me/+pzsfx9fWdXdkYmFl"

# Единственный API-ключ Black-Box
BLACKBOX_KEY = "8jdsYcPZiQMTpYNCluXg9scNhc7y"

TON_WALLET = "UQBlTLz8MaKDtNhYo1qeDsOXtVjN1upRp2uwsf0NWWE9p5cx"
WEBHOOK_HOST = "https://listopadov-search.onrender.com"
USERS_DB = "users.db"

# API Endpoint (только Black-Box)
API_URL_BLACKBOX = "https://api.black-box.cc/v1.1/search"

# Прокси для обхода 403 по IP дата-центра (если понадобится, укажи "http://user:pass@ip:port")
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


# --- ЗАПРОСЫ К BLACK-BOX API ---
async def fetch_blackbox(q: str, api_key: str = BLACKBOX_KEY, return_debug: bool = False):
    headers = {
        **DEFAULT_HEADERS,
        "X-API-Key": api_key
    }
    params = {"q": q.strip(), "type": "auto"}
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)

    last_error = "No response"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(API_URL_BLACKBOX, headers=headers, params=params, proxy=PROXY_URL) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        return json.loads(text)
                    last_error = f"HTTP {resp.status}: {text[:300]}"
                    log.warning(f"[BLACKBOX] Попытка {attempt}/{MAX_RETRIES} -> {last_error}")
                    if resp.status in (502, 503, 504) and attempt < MAX_RETRIES:
                        await asyncio.sleep(3)
                        continue
                    break
        except Exception as e:
            last_error = f"Exception: {e}"
            log.error(f"[BLACKBOX ERROR] Попытка {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(3)

    return {"_error": last_error} if return_debug else None


async def check_blackbox_health():
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    headers = {
        **DEFAULT_HEADERS,
        "X-API-Key": BLACKBOX_KEY
    }
    params = {"q": "79825207224", "type": "phone"}
    start = asyncio.get_event_loop().time()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL_BLACKBOX, headers=headers, params=params, proxy=PROXY_URL) as resp:
                elapsed = int((asyncio.get_event_loop().time() - start) * 1000)
                return [{"name": "Black-Box", "healthy": 200 <= resp.status < 300, "status": resp.status, "latency_ms": elapsed}]
    except Exception as e:
        return [{"name": "Black-Box", "healthy": False, "status": f"ERR ({type(e).__name__})", "latency_ms": 0}]


# --- ПАРСЕР И АГРЕГАТОР ---
async def search_everywhere(q: str):
    resp = await fetch_blackbox(q, BLACKBOX_KEY)

    final_data = {
        "query": q,
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "fio": set(),
        "dob": set(),
        "snils": set(),
        "phones": set(),
        "operator": "Нет",
        "region": "Нет",
        "country": "Россия",
        "databases": {},
        "tg_info": {},
        "sources_count": 0
    }

    if isinstance(resp, dict) and "_error" not in resp:
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

    final_data["fio"] = ", ".join(final_data["fio"]) if final_data["fio"] else "НЕ НАЙДЕНО"
    final_data["dob"] = ", ".join(final_data["dob"]) if final_data["dob"] else "Нет"
    final_data["snils"] = ", ".join(final_data["snils"]) if final_data["snils"] else "Нет"
    final_data["sources_count"] = len(final_data["databases"]) or (1 if final_data["fio"] != "НЕ НАЙДЕНО" else 0)

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
    if tg and isinstance(tg, dict) and tg.get("id"):
        has_data = True
        parts.append("✈️ <b>Telegram:</b>\n")
        parts.append(tree("ID", tg.get("id")))
        if tg.get("username"):
            parts.append(tree("Username", f"@{tg.get('username')}"))
        parts.append("\n")

    dbs = data.get("databases")
    if dbs:
        has_data = True
        parts.append(f"🗄 <b>Найдено в базах ({len(dbs)}):</b>\n")
        for db_name, records in list(dbs.items())[:5]:
            parts.append(f"• <b>{db_name}</b> ({len(records)} зап.)\n")
            if records and isinstance(records[0], dict):
                for k, v in list(records[0].items())[:3]:
                    parts.append(f"  ├ {k}: <code>{v}</code>\n")

    if not has_data:
        parts.append("❌ Данные не найдены в Black-Box API.")

    return "".join(parts)


# --- ГЕНЕРАЦИЯ HTML ОТЧЕТА С VIS.JS ГРАФОМ ---
def generate_html_report(data: dict, report_id: str) -> str:
    query = data.get('query', '—')
    fio = data.get('fio', '—')
    dob = data.get('dob', '—')
    snils = data.get('snils', '—')
    operator = data.get('operator', '—')
    region = data.get('region', '—')

    nodes = [
        {"id": 1, "label": f"Запрос\n{query}", "color": "#c084fc", "shape": "box"},
        {"id": 2, "label": f"ФИО\n{fio}", "color": "#4ade80", "shape": "box"},
        {"id": 3, "label": f"Регион\n{region}", "color": "#38bdf8", "shape": "box"}
    ]
    edges = [
        {"from": 1, "to": 2, "color": "#334155"},
        {"from": 1, "to": 3, "color": "#334155"}
    ]
    graph_json = json.dumps({"nodes": nodes, "edges": edges})

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
    <head>
        <meta charset="UTF-8">
        <title>Oasis Digital — Отчет по {query}</title>
        <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ font-family: 'Inter', sans-serif; background-color: #0f1015; color: #e2e8f0; height: 100vh; display: flex; overflow: hidden; }}
            .sidebar {{ width: 260px; background-color: #0b0c10; border-right: 1px solid #1f2232; display: flex; flex-direction: column; padding: 20px 10px; }}
            .brand {{ padding: 0 10px 20px 10px; font-size: 18px; font-weight: 700; color: #fff; border-bottom: 1px solid #1f2232; margin-bottom: 15px; }}
            .nav-item {{ padding: 12px 15px; border-radius: 8px; cursor: pointer; color: #94a3b8; font-size: 14px; margin-bottom: 4px; }}
            .nav-item.active {{ background-color: #8b5cf6; color: #fff; }}
            .main-content {{ flex: 1; display: flex; flex-direction: column; padding: 20px; overflow-y: auto; background-color: #0f1015; }}
            .tab-content {{ display: none; flex-direction: column; height: 100%; }}
            .tab-content.active {{ display: flex; }}
            .graph-container {{ flex: 1; background-color: #0b0c10; border: 1px solid #1f2232; border-radius: 12px; position: relative; min-height: 500px; }}
            #mynetwork {{ width: 100%; height: 100%; position: absolute; }}
            .card {{ background-color: #0b0c10; border: 1px solid #1f2232; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
            .data-row {{ display: flex; padding: 10px 0; border-bottom: 1px solid #1f2232; }}
            .data-label {{ width: 160px; color: #94a3b8; }}
            .data-value {{ color: #fff; }}
        </style>
    </head>
    <body>
        <div class="sidebar">
            <div class="brand">Oasis Digital</div>
            <div class="nav-item active" onclick="openTab('tab-graph', this)">🕸️ Граф связей</div>
            <div class="nav-item" onclick="openTab('tab-summary', this)">📋 Сводка данных</div>
        </div>
        <div class="main-content">
            <div id="tab-graph" class="tab-content active">
                <div class="graph-container"><div id="mynetwork"></div></div>
            </div>
            <div id="tab-summary" class="tab-content">
                <div class="card">
                    <div class="data-row"><div class="data-label">Цель</div><div class="data-value">{query}</div></div>
                    <div class="data-row"><div class="data-label">ФИО</div><div class="data-value">{fio}</div></div>
                    <div class="data-row"><div class="data-label">Дата рожд.</div><div class="data-value">{dob}</div></div>
                    <div class="data-row"><div class="data-label">СНИЛС</div><div class="data-value">{snils}</div></div>
                    <div class="data-row"><div class="data-label">Оператор</div><div class="data-value">{operator}</div></div>
                    <div class="data-row"><div class="data-label">Регион</div><div class="data-value">{region}</div></div>
                </div>
                {dbs_html}
            </div>
        </div>
        <script>
            function openTab(tabId, el) {{
                document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
                document.getElementById(tabId).classList.add('active');
                el.classList.add('active');
            }}
            var graphData = {graph_json};
            var network = new vis.Network(document.getElementById('mynetwork'), {{
                nodes: new vis.DataSet(graphData.nodes),
                edges: new vis.DataSet(graphData.edges)
            }}, {{
                nodes: {{ font: {{ color: '#fff' }}, borderWidth: 0 }},
                edges: {{ color: '#334155' }}
            }});
        </script>
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
    await cb.message.edit_text("📡 <b>Тест Black-Box API (Raw)</b>\n\nВведите запрос (таймаут 90 сек):", parse_mode="HTML", reply_markup=back_kb())
    await state.set_state(SearchStates.wait_admin_raw)
    await cb.answer()


@dp.message(SearchStates.wait_admin_raw)
async def process_admin_raw(msg: Message, state: FSMContext):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return
    await state.clear()
    q = msg.text.strip()
    note = await msg.answer(f"📡 Опрашиваем Black-Box API (до 90с): <b>{q}</b>...", parse_mode="HTML")

    res_bb = await fetch_blackbox(q, BLACKBOX_KEY, return_debug=True)
    bb_str = json.dumps(res_bb, ensure_ascii=False, indent=2)[:2500] if isinstance(res_bb, dict) else str(res_bb)

    text = f"<b>BLACK-BOX:</b>\n<pre>{bb_str}</pre>"
    await note.edit_text(text[:4000], parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_check_apis")
async def cb_adm_check_apis(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    note = await cb.message.edit_text("🔌 Проверяем Black-Box API...", parse_mode="HTML")
    results = await check_blackbox_health()
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
    note = await msg.answer(f"🔍 <b>[Oasis Digital]</b> Идет поиск по Black-Box (до 90 сек)...\n\n<b>Цель:</b> <code>{q}</code>", parse_mode="HTML")

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
