import asyncio
import logging
import os
import io
import json
import uuid
import sqlite3
import aiohttp
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# --- ГРАФЫ ---
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import networkx as nx

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8704046668:AAGqPaGJntSegYzKBWspTFfj1GQxDBWeN2E"
WEBHOOK_HOST = "https://listopadov-search.onrender.com" # ТВОЙ URL
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
DB_FILE = "oasis.db" 

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
templates = Jinja2Templates(directory="templates")

# --- ТЕКСТЫ ---
START_TEXT = (
    "<b>Oasis Digital</b>\n\n"
    "<blockquote>Бот для поиска и агрегации информации. "
    "Ищет данные по номеру телефона, Telegram ID, email, "
    "автомобилям и документам в открытых и приватных базах.</blockquote>"
)

GUIDE_TEXT = """<b>ℹ️ Примеры для ввода запросов</b>

🕵️ <b>Личность</b>
<blockquote>Навальный Алексей Анатольевич</blockquote>

📲 <b>Контакты</b>
<blockquote>79104813955 — номер телефона
vania120df@gmail.com — email</blockquote>

📄 <b>Документы:</b>
<blockquote>/vu 1234567890 – водительские права
/passport 1234567890 – паспорт
/ogrn 1027700067328 – ОГРН</blockquote>

🚘 <b>Транспорт</b>
<blockquote>РЕНО19 — госномер автомобиля
XTA210930Y2696785 — VIN автомобиля</blockquote>

🏢 <b>Юридическое лицо:</b>
<blockquote>/inn 1234567890 – ИНН
/ogrn 1027700067328 – ОГРН</blockquote>

💬 <b>Соцсети</b>
<blockquote>@durov 1090472807 — Telegram
vk.com/id1, 2737383 - ВКонтакте</blockquote>

🌐 <b>Онлайн-следы</b>
<blockquote>78.26.242.244, google.com</blockquote>

Отправьте запрос сообщением. Если подходят несколько типов, бот предложит выбрать нужный."""

# --- КЛАВИАТУРЫ ---
def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поиск", callback_data="search_info")],
        [InlineKeyboardButton(text="📄 Поиск по неполным данным", callback_data="search_partial")],
        [InlineKeyboardButton(text="Профиль", callback_data="profile"), InlineKeyboardButton(text="Купить запросы", callback_data="buy")],
        [InlineKeyboardButton(text="Связь с нами", callback_data="support")]
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]])

def result_kb(report_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Веб-отчет (Граф)", url=f"{WEBHOOK_HOST}/report/{report_id}")],
        [
            InlineKeyboardButton(text="📄 Скачать TXT", url=f"{WEBHOOK_HOST}/export/{report_id}/txt"),
            InlineKeyboardButton(text="📦 Скачать JSON", url=f"{WEBHOOK_HOST}/export/{report_id}/json")
        ],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")]
    ])

# --- API ЗАПРОСЫ ---
async def fetch_blackbox(q: str):
    headers = {"X-API-Key": API_KEY_BLACKBOX}
    params = {"q": q, "type": "auto"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL_BLACKBOX, params=params, headers=headers, timeout=15) as resp:
                if resp.status == 200: return await resp.json()
    except: pass
    return None

async def fetch_mobius(q: str):
    headers = {"X-API-Key": API_KEY_MOBIUS}
    payload = {"query": q, "type": "auto"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(API_URL_MOBIUS, json=payload, headers=headers, timeout=15) as resp:
                if resp.status == 200: return await resp.json()
    except: pass
    return None

async def fetch_nebula(q: str):
    headers = {"X-API-Key": API_KEY_NEBULA}
    params = {"query": q, "type": "auto"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(API_URL_NEBULA, params=params, headers=headers, timeout=15) as resp:
                if resp.status == 200: return await resp.json()
    except: pass
    return None

async def search_everywhere(q: str):
    # 1. База
    if os.path.exists(DB_FILE):
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            clean_q = q.strip().replace("@", "")
            if clean_q.isdigit():
                cursor.execute("SELECT tg_id, username, phone, fio FROM tg_entities WHERE phone LIKE ? LIMIT 1", (f"%{clean_q}%",))
            else:
                cursor.execute("SELECT tg_id, username, phone, fio FROM tg_entities WHERE username LIKE ? OR fio LIKE ? LIMIT 1", (f"%{clean_q}%", f"%{clean_q}%"))
            row = cursor.fetchone()
            conn.close()
            if row:
                return {
                    "fio": row[3] or "Нет", "phone_main": row[2] or "Нет", "username": row[1] or "Нет",
                    "sources_list": [{"name": "Local DB", "count": 1}], "is_local": True
                }
        except: pass

    # 2. API
    tasks = [fetch_blackbox(q), fetch_mobius(q), fetch_nebula(q)]
    results = await asyncio.gather(*tasks)
    
    for res in results:
        if res:
            tg_info = res.get("telegram", {}) if isinstance(res, dict) else {}
            return {
                "fio": tg_info.get("first_name", "Нет") + " " + tg_info.get("last_name", ""),
                "phone_main": tg_info.get("phone", q),
                "username": tg_info.get("username", "Нет"),
                "sources_list": [{"name": "OSINT API", "count": 1}],
                "is_api": True
            }
    return None

# --- ГРАФ ---
def generate_graph_png(data: dict) -> bytes:
    G = nx.Graph()
    center = data.get('phone_main', 'Нет')
    G.add_node(center, color='#38bdf8', label="ТЕЛЕФОН")
    if data.get('fio') and data['fio'] != "Нет":
        G.add_node(data['fio'], color='#4ade80', label="ФИО")
        G.add_edge(center, data['fio'])
    if data.get('username') and data['username'] != "Нет":
        G.add_node(data['username'], color='#a78bfa', label="TG")
        G.add_edge(center, data['username'])

    plt.figure(figsize=(8, 6), facecolor='#f5f6f8')
    ax = plt.gca(); ax.set_facecolor('#f5f6f8')
    pos = nx.spring_layout(G, seed=42)
    node_colors = [nx.get_node_attributes(G, 'color').get(n, '#fff') for n in G.nodes()]
    nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=3000, alpha=0.9)
    nx.draw_networkx_edges(G, pos, width=2, edge_color='#94a3b8')
    nx.draw_networkx_labels(G, pos, labels=nx.get_node_attributes(G, 'label'), font_size=10, font_color='black', font_weight='bold')
    plt.axis('off')
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', facecolor='#f5f6f8')
    buf.seek(0); plt.close()
    return buf.read()

# --- FASTAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    yield
    await bot.delete_webhook()
    await bot.session.close()

app = FastAPI(title="Oasis Digital API", lifespan=lifespan)

@app.get("/report/{report_id}", response_class=HTMLResponse)
async def web_report(request: Request, report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data: return HTMLResponse("<h1>404 Отчет не найден</h1>", status_code=404)
    return templates.TemplateResponse("report.html", {"request": request, "d": data, "report_id": report_id})

@app.get("/report/{report_id}/graph")
async def web_graph(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data: raise HTTPException(status_code=404)
    return Response(content=generate_graph_png(data), media_type="image/png")

@app.get("/export/{report_id}/json")
async def export_json(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data: raise HTTPException(status_code=404)
    return Response(content=json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": f"attachment; filename=oasis_{report_id}.json"})

@app.get("/export/{report_id}/txt")
async def export_txt(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data: raise HTTPException(status_code=404)
    text = f"OASIS DIGITAL\nЗапрос: {data['query']}\nФИО: {data['fio']}\nТел: {data['phone_main']}\n"
    return PlainTextResponse(content=text, headers={"Content-Disposition": f"attachment; filename=oasis_{report_id}.txt"})

# --- БОТ ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(START_TEXT, parse_mode="HTML", reply_markup=start_kb())

@dp.callback_query(F.data == "back_to_start")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(START_TEXT, parse_mode="HTML", reply_markup=start_kb())
    await cb.answer()

@dp.callback_query(F.data == "search_info")
async def cb_search_info(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text(GUIDE_TEXT, parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()

@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    text = f"👤 <b>Профиль</b>\n\nID: <code>{cb.from_user.id}</code>\nБаланс: 0 запросов"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()

@dp.callback_query(F.data.in_({"buy", "support", "search_partial"}))
async def cb_stub(cb: CallbackQuery):
    await cb.answer("В разработке", show_alert=True)

# --- ОБЫЧНЫЙ ПОИСК ---
@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message, state: FSMContext):
    q = msg.text.strip()
    note = await msg.answer(f"🔍 <b>Ищу:</b> {q}\n<i>(База + API)</i>", parse_mode="HTML")
    
    data = await search_everywhere(q)
    
    if not data:
        data = {
            "query": q, "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "fio": "НЕ НАЙДЕНО", "phone_main": q, "username": "Нет",
            "completeness": "0%", "stats": {"sources": 0, "phones": 0, "records": 0, "emails": 0, "addresses": 0},
            "operator": "-", "region": "-", "country": "-", "passport": "-", "inn": "-", "snils": "-", 
            "email": "-", "dob": "-", "age": "-", "address": "-", "sources_list": []
        }
    else:
        data.update({
            "query": q, "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "completeness": "100%", "stats": {"sources": len(data["sources_list"]), "phones": 1, "records": 1, "emails": 0, "addresses": 0},
            "operator": "-", "region": "-", "country": "-", "passport": "-", "inn": "-", "snils": "-", 
            "email": "-", "dob": "-", "age": "-", "address": "-"
        })

    report_id = str(uuid.uuid4())[:8]
    REPORTS_CACHE[report_id] = data
    
    await note.edit_text(
        f"✅ <b>Отчет готов!</b>\nФИО: <b>{data['fio']}</b>\nТел: <b>{data['phone_main']}</b>\n\n"
        f"<i>Выберите формат отчета:</i>", parse_mode="HTML", reply_markup=result_kb(report_id)
    )

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request):
    from aiogram.types import Update
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
