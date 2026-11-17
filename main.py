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

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9244232488:AAFJAQ4TpJvZm3uhE1cgXsbwChSzOj2DTXY")
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
            bot_token TEXT,
            added_by INTEGER,
            added_at TEXT,
            is_active INTEGER DEFAULT 1
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

INTRO_TEXT = '<a href="https://oasissearchcom.localhost.cc">Oasis Digital</a> - это отличный инструмент для поиска информации по открытым источникам.'

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
        [InlineKeyboardButton(text=f"Отчество: {short(data.get('patronymic'))}", callback_data="field_patronymic"), InlineKeyboardButton(text=f"Дата рожд.: {short(data.get('birth_date'))}", callback_data="field_birth_date")],
        [InlineKeyboardButton(text=f"Страна: {country}", callback_data="select_country")],
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
    clean_q = q.strip().replace("@", "")
    headers = {"X-API-Key": API_KEY_MOBIUS, "Content-Type": "application/json"}
    payload = {"query": clean_q, "type": "auto"}
    timeout = aiohttp.ClientTimeout(total=90)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(API_URL_MOBIUS, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        log.exception("Mobius request failed")
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
    except Exception:
        log.exception("BlackBox request failed")
    return None


# --- ПРОВЕРКА API ---
async def check_api_health(name, url, method="GET", headers=None, payload=None):
    timeout = aiohttp.ClientTimeout(total=90)
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
                return {"name": name, "healthy": 200 <= status < 300, "status": status, "latency_ms": elapsed, "preview": text[:200]}
    except Exception as e:
        return {"name": name, "healthy": False, "status": "ERROR", "latency_ms": 0, "preview": str(e)[:200]}


async def run_all_api_checks():
    return await asyncio.gather(
        check_api_health("Mobius", API_URL_MOBIUS, method="POST", headers={"X-API-Key": API_KEY_MOBIUS, "Content-Type": "application/json"}, payload={"query": "79999999999", "type": "auto"}),
        check_api_health("Black-Box", f"{API_URL_BLACKBOX}/v1.1/search", method="GET", headers={"X-API-Key": API_KEY_BLACKBOX}, payload={"q": "+79825207224", "type": "phone"})
    )


# --- СУПЕР-ПАРСЕР ---
async def search_everywhere(q: str):
    clean_q = q.strip().replace("@", "")
    is_tg_id = bool(TG_ID_RE.fullmatch(clean_q))
    is_tg_username = bool(TG_USERNAME_RE.fullmatch(clean_q)) and not clean_q.isdigit()

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

    bb_res = res_blackbox
    if bb_res and (bb_res.get("status") == "success" or "telegram" in bb_res):
        tg = bb_res.get("telegram") or {}
        if tg:
            tg_username = tg.get("username") or clean_q
            tg_first = tg.get("first_name") or "-"
            tg_last = tg.get("last_name") or ""
            
            final_data["tg_info"] = {
                "id": str(tg.get("id") or (clean_q if is_tg_id else "-")),
                "username": tg_username,
                "first_name": tg_first,
                "last_name": tg_last,
                "full_name": tg.get("full_name") or f"{tg_first} {tg_last}".strip(),
                "bio": tg.get("bio") or "-",
                "phone": (tg.get("phones")[0] if tg.get("phones") else "-"),
                "reg_date": tg.get("reg_date") or "-",
                "reg_age": tg.get("reg_age") or "-",
                "gifts_total": tg.get("gifts_total", 0),
                "gifts": tg.get("gifts", []) or [],
                "sent_gifts": tg.get("sent_gifts", []) or [],
                "names_history": tg.get("names_history", []) or [],
                "usernames_history": tg.get("usernames_history", []) or []
            }
            final_data["fio"] = final_data["tg_info"]["full_name"]
            final_data["username"] = tg_username

        stats_bb = bb_res.get("stats") or {}
        final_data["stats"]["records"] = stats_bb.get("total_records", 1)
        final_data["sources_list"].append({"name": "Black-Box (Telegram)", "count": final_data["stats"]["records"]})
    else:
        final_data["fio"] = "НЕ НАЙДЕНО"

    final_data["stats"]["sources"] = len(final_data["sources_list"])
    return final_data


# --- ФОРМАТ ОТЧЕТА ---
def format_report_text(data: dict) -> str:
    def tree(label: str, value: str, last: bool = False) -> str:
        branch = "└" if last else "├"
        return f"{branch} {label}: {value}\n"

    parts = []
    tg_data = data.get("tg_info") or {}
    
    if tg_data:
        parts.append(f"✈️ <b>Telegram</b> · {tg_data.get('id', '-')}\n\n")
        parts.append(tree("Юзернейм", f"@{tg_data.get('username', '-')}" if tg_data.get('username') != '-' else '-'))
        
        full_name = tg_data.get('full_name') or "-"
        if full_name != "-":
            parts.append(tree("Имя", full_name))
        if tg_data.get('reg_date') != '-':
            reg_str = f"{tg_data.get('reg_date')}"
            if tg_data.get('reg_age') != '-':
                reg_str += f" ({tg_data.get('reg_age')})"
            parts.append(tree("Регистрация", reg_str))
        if tg_data.get('gifts_total', 0) > 0:
            parts.append(tree("Всего подарков", str(tg_data.get('gifts_total'))))
        parts.append("\n")

        gifts = tg_data.get('gifts', [])
        if gifts:
            parts.append(f"🎁 <b>Подарки ({len(gifts)}):</b>\n")
            for g in gifts[:5]:
                from_u = g.get('from', {})
                u_str = f" от @{from_u.get('username', 'id:'+str(from_u.get('id', '')))}" if from_u else ""
                stars = f" ({g.get('stars')} ⭐)" if g.get('stars') else ""
                parts.append(f"├ Подарок{stars}{u_str}\n")
            parts.append("\n")

    if not parts:
        parts.append("❌ Данные не найдены.\n")

    footer = f"\n<i>Источники: {', '.join(s['name'] for s in data.get('sources_list', [])) or '—'}</i>"
    return "".join(parts) + footer


# --- HTML (DASHBOARD) ---
def generate_html_report(data: dict, report_id: str) -> str:
    nodes = [{"id": 1, "label": f"Цель\n{data.get('query', '—')}", "color": "#c084fc", "font": {"color": "#ffffff", "size": 16}, "shape": "box", "borderWidth": 0}]
    edges = []
    node_id = 2

    def add_branch(label, value, color="#4ade80"):
        nonlocal node_id
        if value and str(value) not in ("Нет", "-", "", "None"):
            nodes.append({"id": node_id, "label": label, "color": color, "font": {"color": "#ffffff"}, "shape": "box", "borderWidth": 0})
            edges.append({"from": 1, "to": node_id, "color": "#334155", "width": 1})
            leaf_id = node_id + 1
            val_str = str(value)[:30] + "..." if len(str(value)) > 30 else str(value)
            nodes.append({"id": leaf_id, "label": val_str, "color": "#1e293b", "font": {"color": "#94a3b8"}, "shape": "box", "borderWidth": 0})
            edges.append({"from": node_id, "to": leaf_id, "color": "#334155", "width": 1})
            node_id += 2

    add_branch("ФИО", data.get('fio'))
    tg_data = data.get('tg_info') or {}
    if tg_data:
        add_branch("Telegram", f"@{tg_data.get('username', '-')}")

    graph_json = json.dumps({"nodes": nodes, "edges": edges})
    query = data.get('query', '-')
    records_count = data.get('stats', {}).get('records', 0)

    tg_html = ""
    if tg_data:
        full_name = tg_data.get('full_name') or "-"
        reg_info = f"{tg_data.get('reg_date', '-')}"
        if tg_data.get('reg_age') != '-':
            reg_info += f" ({tg_data.get('reg_age')})"

        tg_html = f"""
        <div class="data-card">
            <div class="data-row"><div class="data-label">ID</div><div class="data-value">{tg_data.get('id')}</div></div>
            <div class="data-row"><div class="data-label">Username</div><div class="data-value">@{tg_data.get('username', '-')}</div></div>
            <div class="data-row"><div class="data-label">Имя</div><div class="data-value">{full_name}</div></div>
            <div class="data-row"><div class="data-label">Регистрация</div><div class="data-value">{reg_info}</div></div>
            <div class="data-row"><div class="data-label">Всего подарков</div><div class="data-value">{tg_data.get('gifts_total', 0)}</div></div>
        </div>
        """
    else:
        tg_html = '<div class="data-card"><div class="data-row"><div class="data-value">Telegram данные не найдены.</div></div></div>'

    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Oasis Digital — Отчет по {query}</title>
        <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ font-family: 'Inter', sans-serif; background-color: #0f1015; color: #e2e8f0; height: 100vh; display: flex; overflow: hidden; }}
            .sidebar {{ width: 260px; background-color: #0b0c10; border-right: 1px solid #1f2232; display: flex; flex-direction: column; padding: 20px 10px; flex-shrink: 0; }}
            .brand {{ padding: 0 10px 20px 10px; font-size: 18px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid #1f2232; margin-bottom: 15px; }}
            .brand-icon {{ background: #fff; color: #000; border-radius: 4px; padding: 2px 6px; font-size: 14px; }}
            .nav-item {{ padding: 12px 15px; border-radius: 8px; cursor: pointer; display: flex; align-items: center; gap: 10px; color: #94a3b8; font-size: 14px; font-weight: 500; transition: 0.2s; margin-bottom: 4px; }}
            .nav-item:hover {{ background-color: #1a1c29; color: #fff; }}
            .nav-item.active {{ background-color: #8b5cf6; color: #fff; }}
            .nav-icon {{ font-size: 18px; opacity: 0.7; }}
            .main-content {{ flex: 1; display: flex; flex-direction: column; padding: 20px; overflow-y: auto; background-color: #0f1015; }}
            .tab-content {{ display: none; flex-direction: column; }}
            .tab-content.active {{ display: flex; }}
            .header-text {{ font-size: 14px; color: #94a3b8; margin-bottom: 15px; }}
            .header-text b {{ color: #fff; }}
            .graph-container {{ flex: 1; background-color: #0b0c10; border: 1px solid #1f2232; border-radius: 12px; position: relative; overflow: hidden; min-height: 500px; }}
            #mynetwork {{ width: 100%; height: 100%; position: absolute; }}
            .data-card {{ background-color: #0b0c10; border: 1px solid #1f2232; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
            .data-row {{ display: flex; padding: 12px 0; border-bottom: 1px solid #1f2232; }}
            .data-row:last-child {{ border-bottom: none; }}
            .data-label {{ width: 180px; font-size: 13px; color: #94a3b8; flex-shrink: 0; }}
            .data-value {{ font-size: 14px; color: #fff; word-break: break-word; line-height: 1.5; }}
        </style>
    </head>
    <body>
        <div class="sidebar">
            <div class="brand"><span class="brand-icon">O</span> Oasis Report</div>
            <div class="nav-item" onclick="openTab('tab-summary', this)"><span class="nav-icon">📋</span> Сводка</div>
            <div class="nav-item active" onclick="openTab('tab-graph', this)"><span class="nav-icon">🕸️</span> Граф связей</div>
            <div class="nav-item" onclick="openTab('tab-tg', this)"><span class="nav-icon">✈️</span> Telegram</div>
        </div>
        <div class="main-content">
            <div id="tab-summary" class="tab-content">
                <div class="header-text">Обзор по запросу <b>{query}</b></div>
                <div class="data-card" style="max-width: 600px;">
                    <div class="data-row"><div class="data-label">Цель</div><div class="data-value">{query}</div></div>
                    <div class="data-row"><div class="data-label">Записей</div><div class="data-value">{records_count}</div></div>
                </div>
            </div>
            <div id="tab-graph" class="tab-content active">
                <div class="header-text">Граф связей</div>
                <div class="graph-container"><div id="mynetwork"></div></div>
            </div>
            <div id="tab-tg" class="tab-content">
                <div class="header-text">Telegram данные</div>
                <div style="max-width: 800px;">{tg_html}</div>
            </div>
        </div>
        <script type="text/javascript">
            function openTab(tabId, el) {{
                document.querySelectorAll('.tab-content').forEach(tab => tab.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active'));
                document.getElementById(tabId).classList.add('active');
                el.classList.add('active');
            }}
            var graphData = {graph_json};
            var network = new vis.Network(document.getElementById('mynetwork'), {{ nodes: new vis.DataSet(graphData.nodes), edges: new vis.DataSet(graphData.edges) }}, {{
                nodes: {{ shape: 'box', font: {{ color: '#fff' }}, borderWidth: 0 }},
                edges: {{ color: '#334155' }}
            }});
        </script>
    </body>
    </html>
    """


# --- FASTAPI ---
web_app = FastAPI()
web_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
    await uvicorn.Server(config).serve()


# --- ЗЕРКАЛА ---
async def run_mirror_bot(token: str, bot_username: str):
    log.info(f"Запуск зеркала @{bot_username}...")
    mirror_bot = Bot(token=token)
    mirror_dp = Dispatcher(storage=MemoryStorage())
    
    @mirror_dp.message(CommandStart())
    async def mirror_start(message: Message):
        main_username = (await bot.get_me()).username
        await message.answer(
            f"🕵‍♂ <b>Добро пожаловать в зеркало Oasis Digital!</b>\n\nГлавный бот: @{main_username}",
            parse_mode="HTML"
        )

    @mirror_dp.message(F.text)
    async def mirror_echo(message: Message):
        main_username = (await bot.get_me()).username
        await message.answer(
            f"🕵‍♂ <b>Это зеркало Oasis Digital.</b>\n\nПерейдите в главный бот: @{main_username}",
            parse_mode="HTML"
        )

    try:
        await mirror_bot.delete_webhook(drop_pending_updates=True)
        await mirror_dp.start_polling(mirror_bot)
    except Exception as e:
        log.exception(f"Зеркало @{bot_username} упало: {e}")
    finally:
        await mirror_bot.session.close()


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

    await asyncio.sleep(1)
    intro_msg = await message.answer(INTRO_TEXT, parse_mode="HTML", disable_web_page_preview=True)
    try:
        await bot.pin_chat_message(chat_id=message.chat.id, message_id=intro_msg.message_id)
    except Exception:
        pass
    await asyncio.sleep(1)
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
        f"📍 <b>Кошелёк TON:</b>\n<code>{TON_WALLET}</code>\n\n"
        f"⚠️ <b>Memo / Комментарий:</b> <code>{memo}</code>"
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
    await cb.message.edit_text("💎 <b>Покупка запросов</b>", parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()


@dp.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.wait_support_msg)
    await cb.message.edit_text(SUPPORT_TEXT, parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()


@dp.message(SearchStates.wait_support_msg)
async def process_support_msg(msg: Message, state: FSMContext):
    await state.clear()
    admin_text = f"📩 <b>Поддержка</b>\nОт: {msg.from_user.full_name} (`{msg.from_user.id}`)\n\n{msg.text}"
    try:
        await bot.send_message(MAIN_ADMIN_ID, admin_text, parse_mode="HTML")
        await msg.answer("✅ Отправлено администрации.", reply_markup=main_kb())
    except Exception:
        await msg.answer("❌ Ошибка отправки.", reply_markup=main_kb())


@dp.callback_query(F.data == "search_partial")
async def cb_search_partial(cb: CallbackQuery, state: FSMContext):
    await state.update_data(surname=None, name=None, patronymic=None, birth_date=None, country=None)
    await cb.message.edit_text(PARTIAL_TEXT, parse_mode="HTML", reply_markup=partial_search_kb(await state.get_data()))
    await cb.answer()


@dp.callback_query(F.data == "ref_link")
async def cb_ref_link(cb: CallbackQuery):
    ref_link = f"https://t.me/{(await bot.get_me()).username}?start={cb.from_user.id}"
    await cb.message.edit_text(f"🔗 <b>Реферальная ссылка:</b>\n\n<code>{ref_link}</code>", parse_mode="HTML", reply_markup=back_kb())
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
        return await msg.answer("❌ Неверный формат токена.", reply_markup=main_kb())

    test_bot = Bot(token=token)
    try:
        bot_info = await test_bot.get_me()
        conn = sqlite3.connect(USERS_DB)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO mirrors (bot_username, bot_token, added_by, added_at) VALUES (?, ?, ?, ?)",
            (bot_info.username, token, msg.from_user.id, datetime.now().strftime("%d.%m.%Y %H:%M"))
        )
        cursor.execute("UPDATE mirrors SET bot_token = ? WHERE bot_username = ?", (token, bot_info.username))
        conn.commit()
        conn.close()
        asyncio.create_task(run_mirror_bot(token, bot_info.username))
    except Exception:
        return await msg.answer("❌ Ошибка запуска бота-зеркала.", reply_markup=main_kb())
    finally:
        await test_bot.session.close()

    await msg.answer(f"🕵‍♂ <b>Зеркало @{bot_info.username} запущено!</b>", parse_mode="HTML", reply_markup=main_kb())


@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None or msg.from_user.is_bot:
        return
    if is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы.")
    if not deduct_request(msg.from_user.id):
        return await msg.answer("❌ У вас закончились запросы.")

    q = msg.text.strip()
    note = await msg.answer(f"🔍 <b>[OASIS DIGITAL]</b> Идет поиск...\n\n<b>Цель:</b> {q}", parse_mode="HTML")
    
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


@dp.callback_query(F.data.startswith("get_txt_"))
async def cb_send_txt(cb: CallbackQuery):
    report_id = cb.data.replace("get_txt_", "")
    data = REPORTS_CACHE.get(report_id)
    if not data:
        return await cb.answer("Отчет устарел", show_alert=True)
    text = f"OASIS DIGITAL\nЗапрос: {data['query']}\nФИО: {data['fio']}"
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


@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    await msg.answer("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_menu_kb())


@dp.callback_query(F.data == "adm_check_apis")
async def cb_adm_check_apis(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID:
        return await cb.answer("Нет доступа", show_alert=True)
    note = await cb.message.edit_text("🔌 <b>Проверяю API...</b>", parse_mode="HTML")
    results = await run_all_api_checks()
    healthy_count = sum(1 for r in results if r["healthy"])
    await note.edit_text(f"🔌 <b>Статус API:</b> {healthy_count}/{len(results)} активны", parse_mode="HTML", reply_markup=admin_menu_kb())


async def main():
    log.info("Бот запускается на Polling...")
    await bot.delete_webhook(drop_pending_updates=True)

    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT bot_username, bot_token FROM mirrors WHERE is_active = 1 AND bot_token IS NOT NULL")
    mirrors = cursor.fetchall()
    conn.close()

    for username, token in mirrors:
        if token:
            asyncio.create_task(run_mirror_bot(token, username))

    asyncio.create_task(run_web())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
