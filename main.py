import asyncio
import logging
import os
import json
import uuid
import sqlite3
import aiohttp
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, Response
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8704046668:AAGqPaGJntSegYzKBWspTFfj1GQxDBWeN2E"
MAIN_ADMIN_ID = 8394631079 
WEBHOOK_HOST = "https://listopadov-search.onrender.com" 
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
DB_FILE = "oasis.db" 
USERS_DB = "users.db" 

# --- СТИКЕР ---
STICKER_ID = "CAACAgIAAxkBAAFXzjhq-itNMdvQX03_x8hhB9NUezaSiAACFyUAAtkoAAFL79-L7Fw-Jlc9BA"

# --- API КЛЮЧИ ---
API_URL_MOBIUS = "https://mobiusnet.cc.cd/api/v1/search"
API_KEY_MOBIUS = "MOBIUS-IO-IW5w3b4xKEY_dS8Kbt4FJswFEsTR97aGKhSEgfrJFjk"
API_URL_NEBULA = "https://nebula.bot.cd/api/v1/search"
API_KEY_NEBULA = "NEBULA-A11ACE72F6559A2E268CE1CBA39AA881"
API_URL_BLACKBOX = "https://api.black-box.cc/v1.1/search"
API_KEY_BLACKBOX = "8jdsYcPZiQMTpYNCluXg9scNhc7y"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
REPORTS_CACHE = {}
MIRROR_BOTS = []

class SearchStates(StatesGroup):
    wait_partial_field = State()
    wait_support_msg = State()
    wait_mirror_token = State()

# --- БАЗА ЮЗЕРОВ ---
def init_users_db():
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            requests_left INTEGER DEFAULT 3,
            referrer_id INTEGER,
            joined_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mirrors (
            bot_username TEXT PRIMARY KEY,
            owner_id INTEGER,
            created_at TEXT
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
    return True

def add_requests(user_id, amount):
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET requests_left = requests_left + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def deduct_request(user_id):
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT requests_left FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row and row[0] > 0:
        cursor.execute("UPDATE users SET requests_left = requests_left - 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

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

<i>Отправьте запрос сообщением. Если подходят несколько типов, бот предложит выбрать нужный.</i>"""

PARTIAL_TEXT = """📄 <b>Поиск по неполным данным</b>

Вы можете указать любое количество данных: фамилию, имя, отчество, дату или год рождения, возраст, место рождения и т.д.

Достаточно заполнить то, что у вас есть — все поля необязательны.
Нажмите на поле и введите значение. Когда всё готово — нажмите «Поиск»."""

SUPPORT_TEXT = """🆘 <b>Связь с нами</b>

Пожалуйста, напишите ваше сообщение прямо в чат. Мы получим его и ответим вам в ближайшее время.
Если это предложение о покупке запросов или сотрудничестве — укажите это в тексте."""

MIRROR_TEXT = """🔧 <b>Инструкция по созданию бота:</b>

1. Перейди в Telegram-бота 👉 @BotFather
2. Нажми кнопку «Start» или напиши команду /start
3. Отправь команду: /newbot
4. Введи любое имя для своего бота (например, «Мой Первый Бот»)
5. Придумай уникальный username для бота, который должен оканчиваться на Bot (например: IUzZDe_RgdW_bot)
6. @BotFather пришлёт тебе сообщение с API Token — он выглядит вот так:
<code>1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11</code>

<i>Скопируй этот токен и отправь его сюда — и всё готово!</i>"""

# --- КЛАВИАТУРЫ ---
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Поиск по неполным данным", callback_data="search_partial")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🔗 Рефералка / Зеркало", callback_data="ref_link")],
        [InlineKeyboardButton(text="🆘 Связь с нами", callback_data="support")]
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]])

def ref_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Создать зеркало бота", callback_data="create_mirror")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]
    ])

def result_kb(report_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Веб-отчет (Граф)", url=f"{WEBHOOK_HOST}/report/{report_id}")],
        [
            InlineKeyboardButton(text="📄 Скачать TXT", callback_data=f"get_txt_{report_id}"),
            InlineKeyboardButton(text="📦 Скачать JSON", callback_data=f"get_json_{report_id}")
        ],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")]
    ])

def partial_search_kb(data: dict):
    def short(val):
        if not val: return "..."
        return val[:10] + "..." if len(val) > 10 else val
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Фамилия: {short(data.get('surname'))}", callback_data="field_surname"), InlineKeyboardButton(text=f"Имя: {short(data.get('name'))}", callback_data="field_name")],
        [InlineKeyboardButton(text=f"Отчество: {short(data.get('patronymic'))}", callback_data="field_patronymic")],
        [InlineKeyboardButton(text=f"Дата рожд.: {short(data.get('birth_date'))}", callback_data="field_birth_date"), InlineKeyboardButton(text=f"Страна: {data.get('country', 'Не выбрана')}", callback_data="select_country")],
        [InlineKeyboardButton(text="Очистить", callback_data="clear_fields"), InlineKeyboardButton(text="Поиск", callback_data="do_partial_search")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]
    ])

def country_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 RU", callback_data="set_country_RU"), InlineKeyboardButton(text="🇺🇦 UA", callback_data="set_country_UA"), InlineKeyboardButton(text="🇰🇿 KZ", callback_data="set_country_KZ"), InlineKeyboardButton(text="🇧🇾 BY", callback_data="set_country_BY")],
        [InlineKeyboardButton(text="🇰🇬 KG", callback_data="set_country_KG"), InlineKeyboardButton(text="🇦🇲 AM", callback_data="set_country_AM"), InlineKeyboardButton(text="🇺🇿 UZ", callback_data="set_country_UZ")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_partial")]
    ])

# --- API ЗАПРОСЫ ---
async def fetch_blackbox(q: str):
    headers = {"X-API-Key": API_KEY_BLACKBOX}
    params = {"q": q, "type": "auto"}
    timeout = aiohttp.ClientTimeout(total=45)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL_BLACKBOX, params=params, headers=headers) as resp:
                if resp.status == 200: return await resp.json()
    except: pass
    return None

async def fetch_mobius(q: str):
    headers = {"X-API-Key": API_KEY_MOBIUS}
    payload = {"query": q, "type": "auto"}
    timeout = aiohttp.ClientTimeout(total=45)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(API_URL_MOBIUS, json=payload, headers=headers) as resp:
                if resp.status == 200: return await resp.json()
    except: pass
    return None

async def fetch_nebula(q: str):
    headers = {"X-API-Key": API_KEY_NEBULA}
    params = {"query": q, "type": "auto"}
    timeout = aiohttp.ClientTimeout(total=45)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL_NEBULA, params=params, headers=headers) as resp:
                if resp.status == 200: return await resp.json()
    except: pass
    return None

# --- СУПЕР-ПАРСЕР ---
async def search_everywhere(q: str):
    clean_q = q.strip().replace("@", "")
    is_tg_query = (not clean_q.isdigit() and clean_q.isalpha()) or (clean_q.isdigit() and len(clean_q) <= 12)
    
    if os.path.exists(DB_FILE) and is_tg_query:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            if clean_q.isdigit():
                cursor.execute("SELECT tg_id, username, phone, fio FROM tg_entities WHERE tg_id = ? LIMIT 1", (clean_q,))
            else:
                cursor.execute("SELECT tg_id, username, phone, fio FROM tg_entities WHERE LOWER(username) = ? LIMIT 1", (clean_q.lower(),))
            row = cursor.fetchone()
            conn.close()
            if row:
                return {
                    "fio": row[3] or "Нет", "phone_main": row[2] or "Нет", "username": row[1] or "Нет", 
                    "operator": "-", "region": "-", "email": "-", "address": "-", "dob": "-", "passport": "-", "inn": "-", "snils": "-",
                    "sources_list": [{"name": "Local DB", "count": 1}], "stats": {"sources": 1, "phones": 1, "records": 1}, "completeness": "20%"
                }
        except: pass

    tasks = [fetch_blackbox(q), fetch_mobius(q), fetch_nebula(q)]
    results = await asyncio.gather(*tasks)
    
    final_data = {
        "fio": "Нет", "phone_main": q, "username": "Нет", "operator": "Нет", "region": "Нет", 
        "email": "Нет", "address": "Нет", "dob": "Нет", "passport": "Нет", "inn": "Нет", "snils": "Нет",
        "sources_list": [], "stats": {"sources": 0, "phones": 0, "records": 0}
    }
    found_something = False

    for res in results:
        if not res or not isinstance(res, dict): continue

        if "profile" in res and res.get("status") == "success":
            found_something = True
            prof = res.get("profile", {})
            tel = res.get("telecom", {})
            locs = res.get("locations", [])
            fin = res.get("financial", {})
            
            if prof.get("primary_fio"): final_data["fio"] = prof["primary_fio"]
            if prof.get("birth_date"): final_data["dob"] = prof["birth_date"]
            if prof.get("passports") and isinstance(prof["passports"], list): final_data["passport"] = prof["passports"][0]
            if prof.get("inn"): final_data["inn"] = prof["inn"]
            if prof.get("snils"): final_data["snils"] = prof["snils"]
            
            if tel.get("query_phone"): final_data["phone_main"] = tel["query_phone"]
            if tel.get("operator"): final_data["operator"] = tel["operator"]
            if tel.get("region"): final_data["region"] = tel["region"]
            
            if locs: final_data["address"] = locs[0].get("address", "Нет")
            if fin.get("records"): final_data["email"] = str(fin["records"])
                
            final_data["stats"]["sources"] += res.get("stats", {}).get("sources_count", 1)
            final_data["stats"]["phones"] += len(prof.get("phones", []))
            final_data["stats"]["records"] += res.get("stats", {}).get("total_records", 1)
            
            final_data["sources_list"].append({"name": "BlackBox", "count": res.get("stats", {}).get("sources_count", 1)})

        elif res.get("success") and "results" in res:
            found_something = True
            for key, val in res["results"].items():
                if isinstance(val, dict) and "result" in val and isinstance(val["result"], dict):
                    inner_data = val["result"].get("data", {}).get("results", {})
                    if inner_data:
                        if inner_data.get("names") and final_data["fio"] == "Нет": final_data["fio"] = inner_data["names"][0]
                        if inner_data.get("emails") and final_data["email"] == "Нет": final_data["email"] = inner_data["emails"][0]
                        if inner_data.get("addresses") and final_data["address"] == "Нет": final_data["address"] = inner_data["addresses"][0]
                        if inner_data.get("phone_info") and final_data["operator"] == "Нет":
                            final_data["operator"] = inner_data["phone_info"].get("operator", "Нет")
                            final_data["region"] = inner_data["phone_info"].get("region", "Нет")
                            
            final_data["stats"]["sources"] += 1
            final_data["sources_list"].append({"name": "Mobius (Nebula)", "count": res.get("results_count", 1)})

    filled = sum(1 for k in ["fio", "phone_main", "email", "address", "dob"] if final_data[k] != "Нет")
    final_data["completeness"] = f"{int((filled / 5) * 100)}%"

    return final_data if found_something else None

# --- HTML ---
def generate_html_report(data: dict, report_id: str) -> str:
    nodes = [{"id": 1, "label": f"Цель: {data['query']}", "color": "#38bdf8", "font": {"color": "white"}}]
    edges = []
    node_id = 2
    if data.get('fio') and data['fio'] != "Нет": nodes.append({"id": node_id, "label": "ФИО", "color": "#4ade80"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('phone_main') and data['phone_main'] != "Нет": nodes.append({"id": node_id, "label": "Тел", "color": "#38bdf8"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('operator') and data['operator'] != "Нет": nodes.append({"id": node_id, "label": "Оператор", "color": "#facc15"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('email') and data['email'] != "Нет": nodes.append({"id": node_id, "label": "Email/Банк", "color": "#a78bfa"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('passport') and data['passport'] != "Нет": nodes.append({"id": node_id, "label": "Паспорт", "color": "#f87171"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('inn') and data['inn'] != "Нет": nodes.append({"id": node_id, "label": "ИНН", "color": "#fbbf24"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    graph_json = json.dumps({"nodes": nodes, "edges": edges})

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
                <div class="data-item"><div class="d-icon">📧</div><div class="d-content"><div class="d-label">Email / Банк</div><div class="d-value">{data['email']}</div></div></div>
                <div class="data-item"><div class="d-icon">🏠</div><div class="d-content"><div class="d-label">Адрес</div><div class="d-value">{data['address']}</div></div></div>
                <div class="data-item"><div class="d-icon">📄</div><div class="d-content"><div class="d-label">Паспорт</div><div class="d-value">{data['passport']}</div></div></div>
                <div class="data-item"><div class="d-icon">🆔</div><div class="d-content"><div class="d-label">ИНН</div><div class="d-value">{data['inn']}</div></div></div>
                <div class="data-item"><div class="d-icon">🆔</div><div class="d-content"><div class="d-label">СНИЛС</div><div class="d-value">{data['snils']}</div></div></div>
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
@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    yield
    await bot.delete_webhook()
    await bot.session.close()

app = FastAPI(title="Oasis Digital API", lifespan=lifespan)

@app.get("/report/{report_id}", response_class=HTMLResponse)
async def web_report(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data: return HTMLResponse("<h1>404 Отчет не найден</h1>", status_code=404)
    return HTMLResponse(content=generate_html_report(data, report_id))

@app.get("/mirrors", response_class=HTMLResponse)
async def show_mirrors():
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT bot_username, created_at FROM mirrors ORDER BY created_at DESC")
    bots = cursor.fetchall()
    conn.close()
    
    bots_html = ""
    for b in bots:
        uname = b[0]
        date = b[1]
        bots_html += f'''
        <div class="bot-card">
            <div class="bot-icon">🤖</div>
            <div class="bot-info">
                <h3>@{uname}</h3>
                <p>Создан: {date}</p>
            </div>
            <a href="https://t.me/{uname}" target="_blank" class="btn">Запустить</a>
        </div>
        '''

    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Oasis Digital - Зеркала</title>
        <style>
            body {{ font-family: 'Inter', sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 40px 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; }}
            h1 {{ color: #38bdf8; text-align: center; margin-bottom: 10px; }}
            p.sub {{ text-align: center; color: #94a3b8; margin-bottom: 40px; }}
            .bot-card {{ display: flex; align-items: center; background: #1e293b; padding: 15px 20px; border-radius: 12px; margin-bottom: 15px; border: 1px solid #334155; }}
            .bot-icon {{ font-size: 24px; margin-right: 15px; }}
            .bot-info {{ flex: 1; }}
            .bot-info h3 {{ margin: 0 0 5px 0; color: #f8fafc; font-size: 16px; }}
            .bot-info p {{ margin: 0; color: #64748b; font-size: 12px; }}
            .btn {{ background: #38bdf8; color: #0f172a; padding: 8px 16px; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 14px; transition: 0.2s; }}
            .btn:hover {{ background: #0ea5e9; }}
            .footer {{ text-align: center; color: #475569; font-size: 12px; margin-top: 50px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>OASIS DIGITAL</h1>
            <p class="sub">Каталог активных зеркал бота</p>
            {bots_html if bots else '<p style="text-align:center;">Пока нет созданных зеркал.</p>'}
            <div class="footer">Сгенерировано автоматически • Oasis Digital</div>
        </div>
    </body>
    </html>
    """

# --- ЗЕРКАЛО ---
async def run_mirror_bot(token: str, admin_id: int):
    mirror_bot = Bot(token=token)
    mirror_dp = Dispatcher(storage=MemoryStorage())
    
    @mirror_dp.message(CommandStart())
    async def mirror_start(message: Message):
        user = get_user(message.from_user.id)
        if not user: add_user(message.from_user.id, message.from_user.username)
        
        try: await message.answer_sticker(STICKER_ID)
        except: pass
        await message.answer(START_TEXT, parse_mode="HTML", reply_markup=main_kb())
        
    @mirror_dp.callback_query(F.data == "profile")
    async def mirror_profile(cb: CallbackQuery):
        user = get_user(cb.from_user.id)
        text = f"👤 <b>Профиль</b>\n\nID: <code>{cb.from_user.id}</code>\nЗапросов: <b>{user[2] if user else 0}</b>"
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())
        await cb.answer()
        
    @mirror_dp.callback_query(F.data == "ref_link")
    async def mirror_ref(cb: CallbackQuery):
        ref_link = f"https://t.me/{(await mirror_bot.get_me()).username}?start={cb.from_user.id}"
        text = f"🔗 <b>Ваша реферальная ссылка:</b>\n\n<code>{ref_link}</code>\n\n<i>За каждого приглашенного вы получаете +1 запрос!</i>"
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())
        await cb.answer()

    @mirror_dp.callback_query(F.data == "support")
    async def mirror_support(cb: CallbackQuery, state: FSMContext):
        await state.set_state(SearchStates.wait_support_msg)
        await cb.message.edit_text(SUPPORT_TEXT, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_start")]]))
        await cb.answer()

    @mirror_dp.callback_query(F.data == "back_to_start")
    async def mirror_back(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.message.edit_text(START_TEXT, parse_mode="HTML", reply_markup=main_kb())
        await cb.answer()

    @mirror_dp.message(SearchStates.wait_support_msg)
    async def mirror_process_support(msg: Message, state: FSMContext):
        await state.clear()
        admin_text = f"📩 <b>Новое сообщение в поддержку</b>\n\nОт: {msg.from_user.full_name} (@{msg.from_user.username})\nID: <code>{msg.from_user.id}</code>\n\nТекст: {msg.text}"
        try:
            await bot.send_message(admin_id, admin_text, parse_mode="HTML")
            await msg.answer("✅ Ваше сообщение отправлено администрации. Ожидайте ответа.", reply_markup=main_kb())
        except:
            await msg.answer("❌ Ошибка отправки.", reply_markup=main_kb())

    @mirror_dp.callback_query(F.data == "search_partial")
    async def mirror_search_partial(cb: CallbackQuery, state: FSMContext):
        await state.update_data(surname=None, name=None, patronymic=None, birth_date=None, country=None)
        await cb.message.edit_text(PARTIAL_TEXT, parse_mode="HTML", reply_markup=partial_search_kb(await state.get_data()))
        await cb.answer()

    @mirror_dp.callback_query(F.data.startswith("field_"))
    async def mirror_select_field(cb: CallbackQuery, state: FSMContext):
        field = cb.data.replace("field_", "")
        await state.update_data(editing_field=field)
        field_names = {"surname": "Фамилию", "name": "Имя", "patronymic": "Отчество", "birth_date": "Дату рождения"}
        await cb.message.edit_text(f"Введите {field_names.get(field, 'значение')}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_partial")]]))
        await state.set_state(SearchStates.wait_partial_field)
        await cb.answer()

    @mirror_dp.message(SearchStates.wait_partial_field)
    async def mirror_process_field_input(msg: Message, state: FSMContext):
        data = await state.get_data()
        field = data.get("editing_field")
        if field and msg.text: await state.update_data({field: msg.text.strip()})
        await state.set_state(None)
        await msg.answer(PARTIAL_TEXT, parse_mode="HTML", reply_markup=partial_search_kb(await state.get_data()))

    @mirror_dp.callback_query(F.data == "clear_fields")
    async def mirror_clear(cb: CallbackQuery, state: FSMContext):
        await state.update_data(surname=None, name=None, patronymic=None, birth_date=None, country=None)
        await cb.answer("Поля очищены")
        await cb.message.edit_reply_markup(reply_markup=partial_search_kb(await state.get_data()))

    @mirror_dp.callback_query(F.data == "select_country")
    async def mirror_select_country(cb: CallbackQuery):
        await cb.message.edit_text("🌍 Выберите страну поиска:", reply_markup=country_kb())
        await cb.answer()

    @mirror_dp.callback_query(F.data.startswith("set_country_"))
    async def mirror_set_country(cb: CallbackQuery, state: FSMContext):
        country = cb.data.replace("set_country_", "")
        await state.update_data(country=country)
        await cb.message.edit_text(PARTIAL_TEXT, parse_mode="HTML", reply_markup=partial_search_kb(await state.get_data()))
        await cb.answer()

    @mirror_dp.callback_query(F.data == "back_to_partial")
    async def mirror_back_to_partial(cb: CallbackQuery, state: FSMContext):
        await state.set_state(None)
        await cb.message.edit_text(PARTIAL_TEXT, parse_mode="HTML", reply_markup=partial_search_kb(await state.get_data()))
        await cb.answer()

    @mirror_dp.callback_query(F.data == "do_partial_search")
    async def mirror_do_partial(cb: CallbackQuery, state: FSMContext):
        if not deduct_request(cb.from_user.id):
            return await cb.answer("❌ У вас закончились запросы.", show_alert=True)
            
        data = await state.get_data()
        query_parts = []
        if data.get('surname'): query_parts.append(f"Фамилия: {data['surname']}")
        if data.get('name'): query_parts.append(f"Имя: {data['name']}")
        if data.get('patronymic'): query_parts.append(f"Отчество: {data['patronymic']}")
        if data.get('birth_date'): query_parts.append(f"Дата: {data['birth_date']}")
        if data.get('country'): query_parts.append(f"Страна: {data['country']}")
        
        query_str = ", ".join(query_parts) if query_parts else "Пустой запрос"
        
        note = await cb.message.edit_text(f"🔍 <b>[OASIS DIGITAL]</b> Идет поиск по базам данных...\n\n<b>Цель:</b> {query_str}\n<b>Тип:</b> partial", parse_mode="HTML")
        data_result = {"query": query_str, "date": datetime.now().strftime("%d.%m.%Y"), "fio": "НЕ НАЙДЕНО", "phone_main": "-", "username": "-", "operator": "-", "region": "-", "email": "-", "address": "-", "dob": "-", "passport": "-", "inn": "-", "snils": "-", "sources_list": [], "stats": {"sources": 0, "phones": 0, "records": 0}, "completeness": "0%"}
        
        report_id = str(uuid.uuid4())[:8]
        REPORTS_CACHE[report_id] = data_result
        
        await note.edit_text(f"✅ <b>Отчет готов!</b>\nФИО: <b>{data_result['fio']}</b>", parse_mode="HTML", reply_markup=result_kb(report_id))
        await cb.answer()

    @mirror_dp.message(F.text & ~F.text.startswith("/"))
    async def mirror_process_search(msg: Message, state: FSMContext):
        if msg.from_user.is_bot: return
        if not deduct_request(msg.from_user.id):
            return await msg.answer("❌ У вас закончились запросы.")
        
        q = msg.text.strip()
        note = await msg.answer(f"🔍 <b>[OASIS DIGITAL]</b> Идет поиск по базам данных...\n\n<b>Цель:</b> {q}\n<b>Тип:</b> auto", parse_mode="HTML")
        data = await search_everywhere(q)
        
        if not data:
            data = {"query": q, "date": datetime.now().strftime("%d.%m.%Y %H:%M"), "fio": "НЕ НАЙДЕНО", "phone_main": q, "username": "Нет", "operator": "-", "region": "-", "email": "-", "address": "-", "dob": "-", "passport": "-", "inn": "-", "snils": "-", "sources_list": [], "stats": {"sources": 0, "phones": 0, "records": 0}, "completeness": "0%"}
        else:
            data["query"] = q
            data["date"] = datetime.now().strftime("%d.%m.%Y %H:%M")

        report_id = str(uuid.uuid4())[:8]
        REPORTS_CACHE[report_id] = data
        
        await note.edit_text(
            f"✅ <b>Отчет готов!</b>\nФИО: <b>{data['fio']}</b>\nТел: <b>{data['phone_main']}</b>\nОператор: {data['operator']}\n\n<i>Выберите формат отчета:</i>", 
            parse_mode="HTML", reply_markup=result_kb(report_id)
        )

    try:
        await mirror_dp.start_polling(mirror_bot)
    except Exception as e:
        logging.error(f"Ошибка клона: {e}")
    finally:
        await mirror_bot.session.close()


# --- БОТ ХЕНДЛЕРЫ ---
@dp.message(CommandStart(deep_link=True))
async def cmd_start_ref(message: Message, command: CommandStart, state: FSMContext):
    await state.clear()
    referrer_id = int(command.args) if command.args and command.args.isdigit() else None
    
    user = get_user(message.from_user.id)
    if not user:
        add_user(message.from_user.id, message.from_user.username, referrer_id)
        if referrer_id:
            add_requests(referrer_id, 1)
            try: await bot.send_message(referrer_id, "🎉 По вашей ссылке зашел новый пользователь! Вам начислен +1 запрос.")
            except: pass
    
    try: await message.answer_sticker(STICKER_ID)
    except: pass
    await message.answer(START_TEXT, parse_mode="HTML", reply_markup=main_kb())

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = get_user(message.from_user.id)
    if not user: add_user(message.from_user.id, message.from_user.username)
    
    try: await message.answer_sticker(STICKER_ID)
    except: pass
    await message.answer(START_TEXT, parse_mode="HTML", reply_markup=main_kb())

@dp.callback_query(F.data == "back_to_start")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(START_TEXT, parse_mode="HTML", reply_markup=main_kb())
    await cb.answer()

@dp.callback_query(F.data == "ref_link")
async def cb_ref_link(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    ref_link = f"https://t.me/{(await bot.get_me()).username}?start={cb.from_user.id}"
    text = (
        f"🔗 <b>Ваша реферальная ссылка:</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"<i>За каждого приглашенного вы получаете +1 запрос!</i>\n\n"
        f"<b>Также вы можете создать своего собственного бота с этим функционалом!</b>"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=ref_kb())
    await cb.answer()

@dp.callback_query(F.data == "create_mirror")
async def cb_create_mirror(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text(MIRROR_TEXT, parse_mode="HTML", reply_markup=back_kb())
    await state.set_state(SearchStates.wait_mirror_token)
    await cb.answer()

@dp.message(SearchStates.wait_mirror_token)
async def process_mirror_token(msg: Message, state: FSMContext):
    await state.clear()
    token = msg.text.strip()
    
    if ":" not in token:
        return await msg.answer("❌ Это не похоже на токен. Скопируй его из BotFather.", reply_markup=main_kb())
        
    try:
        test_bot = Bot(token=token)
        bot_info = await test_bot.get_me()
        await test_bot.session.close()
    except Exception:
        return await msg.answer("❌ Неверный токен. Проверь и попробуй снова.", reply_markup=main_kb())
        
    conn = sqlite3.connect(USERS_DB)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO mirrors (bot_username, owner_id, created_at) VALUES (?, ?, ?)", 
                   (bot_info.username, msg.from_user.id, datetime.now().strftime("%d.%m.%Y")))
    conn.commit()
    conn.close()
        
    await msg.answer("✅ Токен верный! Бот-клон запускается в фоне. Пожалуйста, подождите...", reply_markup=main_kb())
    
    task = asyncio.create_task(run_mirror_bot(token, msg.from_user.id))
    MIRROR_BOTS.append(task)

@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    text = f"👤 <b>Профиль</b>\n\nID: <code>{cb.from_user.id}</code>\nЗапросов: <b>{user[2] if user else 0}</b>"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()

@dp.callback_query(F.data == "support")
async def cb_support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.wait_support_msg)
    await cb.message.edit_text(SUPPORT_TEXT, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_start")]]))
    await cb.answer()

@dp.message(SearchStates.wait_support_msg)
async def process_support_msg(msg: Message, state: FSMContext):
    await state.clear()
    admin_text = f"📩 <b>Новое сообщение в поддержку</b>\n\nОт: {msg.from_user.full_name} (@{msg.from_user.username})\nID: <code>{msg.from_user.id}</code>\n\nТекст: {msg.text}"
    try:
        await bot.send_message(MAIN_ADMIN_ID, admin_text, parse_mode="HTML")
        await msg.answer("✅ Ваше сообщение отправлено администрации. Ожидайте ответа.", reply_markup=main_kb())
    except:
        await msg.answer("❌ Ошибка отправки.", reply_markup=main_kb())

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
    await cb.message.edit_text(f"Введите {field_names.get(field, 'значение')}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_partial")]]))
    await state.set_state(SearchStates.wait_partial_field)
    await cb.answer()

@dp.message(SearchStates.wait_partial_field)
async def process_field_input(msg: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("editing_field")
    if field and msg.text: await state.update_data({field: msg.text.strip()})
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
    if not deduct_request(cb.from_user.id):
        return await cb.answer("❌ У вас закончились запросы. Пригласите друга или купите в профиле.", show_alert=True)
        
    data = await state.get_data()
    query_parts = []
    if data.get('surname'): query_parts.append(f"Фамилия: {data['surname']}")
    if data.get('name'): query_parts.append(f"Имя: {data['name']}")
    if data.get('patronymic'): query_parts.append(f"Отчество: {data['patronymic']}")
    if data.get('birth_date'): query_parts.append(f"Дата: {data['birth_date']}")
    if data.get('country'): query_parts.append(f"Страна: {data['country']}")
    
    query_str = ", ".join(query_parts) if query_parts else "Пустой запрос"
    
    note = await cb.message.edit_text(f"🔍 <b>[OASIS DIGITAL]</b> Идет поиск по базам данных...\n\n<b>Цель:</b> {query_str}\n<b>Тип:</b> partial", parse_mode="HTML")
    data_result = {"query": query_str, "date": datetime.now().strftime("%d.%m.%Y"), "fio": "НЕ НАЙДЕНО", "phone_main": "-", "username": "-", "operator": "-", "region": "-", "email": "-", "address": "-", "dob": "-", "passport": "-", "inn": "-", "snils": "-", "sources_list": [], "stats": {"sources": 0, "phones": 0, "records": 0}, "completeness": "0%"}
    
    report_id = str(uuid.uuid4())[:8]
    REPORTS_CACHE[report_id] = data_result
    
    await note.edit_text(f"✅ <b>Отчет готов!</b>\nФИО: <b>{data_result['fio']}</b>", parse_mode="HTML", reply_markup=result_kb(report_id))
    await cb.answer()

@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message, state: FSMContext):
    if msg.from_user.is_bot: return
    
    if not deduct_request(msg.from_user.id):
        return await msg.answer("❌ У вас закончились запросы. Пригласите друга (кнопка Рефералка) или купите в профиле.")
    
    q = msg.text.strip()
    
    note = await msg.answer(f"🔍 <b>[OASIS DIGITAL]</b> Идет поиск по базам данных...\n\n<b>Цель:</b> {q}\n<b>Тип:</b> auto", parse_mode="HTML")
    
    data = await search_everywhere(q)
    
    if not data:
        data = {
            "query": q, "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "fio": "НЕ НАЙДЕНО", "phone_main": q, "username": "Нет",
            "operator": "-", "region": "-", "email": "-", "address": "-", "dob": "-", "passport": "-", "inn": "-", "snils": "-", 
            "sources_list": [], "stats": {"sources": 0, "phones": 0, "records": 0}, "completeness": "0%"
        }
    else:
        data["query"] = q
        data["date"] = datetime.now().strftime("%d.%m.%Y %H:%M")

    report_id = str(uuid.uuid4())[:8]
    REPORTS_CACHE[report_id] = data
    
    await note.edit_text(
        f"✅ <b>Отчет готов!</b>\n"
        f"ФИО: <b>{data['fio']}</b>\n"
        f"Тел: <b>{data['phone_main']}</b>\n"
        f"Оператор: {data['operator']}\n\n"
        f"<i>Выберите формат отчета:</i>", 
        parse_mode="HTML", reply_markup=result_kb(report_id)
    )

@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID: return await msg.answer("⛔ Нет доступа.")
    await msg.answer("👑 <b>Админ-панель</b>", parse_mode="HTML")

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request):
    from aiogram.types import Update
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
