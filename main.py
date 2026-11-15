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
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# --- ГРАФЫ ---
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import networkx as nx

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8704046668:AAGqPaGJntSegYzKBWspTFfj1GQxDBWeN2E"
MAIN_ADMIN_ID = 8394631079 
WEBHOOK_HOST = "https://listopadov-search.onrender.com" 
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

# Кнопки результата (теперь без ссылок на файлы, они будут появляться после нажатия)
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
        [InlineKeyboardButton(text="📡 Статус API", callback_data="adm_api_status")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")]
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

async def search_everywhere(q: str):
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

    tasks = [fetch_blackbox(q), fetch_mobius(q), fetch_nebula(q)]
    
    for coro in asyncio.as_completed(tasks):
        res = await coro
        if res:
            tg_info = res.get("telegram", {}) if isinstance(res, dict) else {}
            profile = res.get("profile", {}) if isinstance(res, dict) else {}
            return {
                "fio": tg_info.get("first_name", profile.get("full_name", "Нет")),
                "phone_main": tg_info.get("phone", profile.get("phone", q)),
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

# --- ГЕНЕРАЦИЯ HTML ---
def generate_html_report(data: dict, report_id: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Oasis Digital - {data['query']}</title>
        <style>
            body {{ font-family: sans-serif; background: #f5f6f8; color: #1a1a1a; margin: 0; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: #fff; padding: 20px; border-radius: 12px; }}
            .graph-section img {{ width: 100%; border-radius: 12px; margin: 20px 0; }}
            .data-item {{ padding: 12px 0; border-bottom: 1px solid #eee; }}
            .d-label {{ font-size: 11px; color: #888; text-transform: uppercase; }}
            .d-value {{ font-size: 16px; font-weight: 500; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>OASIS DIGITAL</h1>
            <p>Отчет по запросу: {data['query']}</p>
            <div class="graph-section"><img src="/report/{report_id}/graph"></div>
            <div class="data-item"><div class="d-label">ФИО</div><div class="d-value">{data['fio']}</div></div>
            <div class="data-item"><div class="d-label">Телефон</div><div class="d-value">{data['phone_main']}</div></div>
        </div>
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

@app.get("/report/{report_id}/graph")
async def web_graph(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data: raise HTTPException(status_code=404)
    return Response(content=generate_graph_png(data), media_type="image/png")

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

# --- ОТПРАВКА ФАЙЛОВ В ЧАТ ---
@dp.callback_query(F.data.startswith("get_txt_"))
async def cb_send_txt(cb: CallbackQuery):
    report_id = cb.data.replace("get_txt_", "")
    data = REPORTS_CACHE.get(report_id)
    if not data:
        return await cb.answer("Отчет устарел", show_alert=True)
        
    text = f"OASIS DIGITAL\nЗапрос: {data['query']}\nФИО: {data['fio']}\nТел: {data['phone_main']}\nTelegram: {data['username']}\n\nИсточники: {', '.join([s['name'] for s in data['sources_list']])}"
    
    # Создаем файл в памяти и отправляем
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
    
    # Создаем файл в памяти и отправляем
    file = BufferedInputFile(json_str.encode('utf-8'), filename=f"oasis_{report_id}.json")
    await cb.message.answer_document(document=file, caption="📦 <b>JSON Отчет</b>", parse_mode="HTML")
    await cb.answer()

@dp.callback_query(F.data.in_({"buy", "support", "search_partial"}))
async def cb_stub(cb: CallbackQuery):
    await cb.answer("В разработке", show_alert=True)

# --- ПОИСК ---
@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message, state: FSMContext):
    q = msg.text.strip()
    note = await msg.answer(f"🔍 <b>Ищу:</b> {q}\n<i>(Опрос баз, до 45 сек...)</i>", parse_mode="HTML")
    
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

# --- АДМИНКА ---
@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID: return await msg.answer("⛔ Нет доступа.")
    await msg.answer("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_menu_kb())

@dp.callback_query(F.data == "adm_api_status")
async def cb_adm_api_status(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID: return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("📡 <b>Проверяю API (до 45 сек)...</b>", parse_mode="HTML")
    res_bb = await fetch_blackbox("79825207224")
    res_mb = await fetch_mobius("79825207224")
    res_nb = await fetch_nebula("79825207224")
    text = "📡 <b>Статус API:</b>\n\n"
    text += f"{'🟢 OK' if res_bb else '🔴 Ошибка'} — BlackBox\n"
    text += f"{'🟢 OK' if res_mb else '🔴 Ошибка'} — Mobius\n"
    text += f"{'🟢 OK' if res_nb else '🔴 Ошибка'} — Nebula"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=admin_menu_kb())
    await cb.answer()

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request):
    from aiogram.types import Update
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
