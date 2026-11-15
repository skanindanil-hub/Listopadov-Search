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
from fastapi.responses import HTMLResponse, Response
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

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

START_TEXT = ("<b>Oasis Digital</b>\n\n<blockquote>Бот для поиска и агрегации информации.</blockquote>")

def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Поиск", callback_data="search_info")],
        [InlineKeyboardButton(text="Профиль", callback_data="profile")]
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_start")]])

def result_kb(report_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Веб-отчет", url=f"{WEBHOOK_HOST}/report/{report_id}")],
        [InlineKeyboardButton(text="📄 TXT", callback_data=f"get_txt_{report_id}"), InlineKeyboardButton(text="📦 JSON", callback_data=f"get_json_{report_id}")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")]
    ])

def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 Тест API (Raw)", callback_data="adm_raw_test")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")]
    ])

# --- API ---
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
                return {"fio": row[3] or "Нет", "phone_main": row[2] or "Нет", "username": row[1] or "Нет", "sources_list": [{"name": "Local DB", "count": 1}]}
        except: pass

    tasks = [fetch_blackbox(q), fetch_mobius(q), fetch_nebula(q)]
    results = await asyncio.gather(*tasks)
    
    for res in results:
        if res and isinstance(res, dict):
            tg_info = res.get("telegram", {})
            profile = res.get("profile", {})
            
            # Проверка на реальные данные (расширенная)
            if tg_info.get("id") or tg_info.get("username") or profile.get("full_name") or profile.get("phone") or tg_info.get("first_name") or res.get("status") == "success":
                return {
                    "fio": tg_info.get("first_name", profile.get("full_name", "Нет")),
                    "phone_main": tg_info.get("phone", profile.get("phone", q)),
                    "username": tg_info.get("username", "Нет"),
                    "sources_list": [{"name": "OSINT API", "count": 1}]
                }
    return None

def generate_html_report(data: dict, report_id: str) -> str:
    return f"""<html><body><h1>OASIS DIGITAL</h1><p>{data['query']}</p><img src="/report/{report_id}/graph"></body></html>"""

def generate_graph_png(data: dict) -> bytes:
    G = nx.Graph()
    center = data.get('phone_main', 'Нет')
    G.add_node(center, color='#38bdf8', label="ТЕЛЕФОН")
    plt.figure(figsize=(8, 6), facecolor='#f5f6f8')
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', facecolor='#f5f6f8')
    buf.seek(0); plt.close()
    return buf.read()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/report/{report_id}", response_class=HTMLResponse)
async def web_report(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data: return HTMLResponse("404")
    return HTMLResponse(content=generate_html_report(data, report_id))

@app.get("/report/{report_id}/graph")
async def web_graph(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data: raise HTTPException(404)
    return Response(content=generate_graph_png(data), media_type="image/png")

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(START_TEXT, parse_mode="HTML", reply_markup=start_kb())

@dp.callback_query(F.data == "back_to_start")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(START_TEXT, parse_mode="HTML", reply_markup=start_kb())
    await cb.answer()

@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    await cb.message.edit_text(f"👤 ID: <code>{cb.from_user.id}</code>", parse_mode="HTML", reply_markup=back_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("get_txt_"))
async def cb_send_txt(cb: CallbackQuery):
    report_id = cb.data.replace("get_txt_", "")
    data = REPORTS_CACHE.get(report_id)
    if not data: return await cb.answer("Устарел", show_alert=True)
    text = f"OASIS DIGITAL\n{data['fio']}\n{data['phone_main']}"
    await cb.message.answer_document(BufferedInputFile(text.encode(), filename=f"report_{report_id}.txt"))
    await cb.answer()

@dp.callback_query(F.data.startswith("get_json_"))
async def cb_send_json(cb: CallbackQuery):
    report_id = cb.data.replace("get_json_", "")
    data = REPORTS_CACHE.get(report_id)
    if not data: return await cb.answer("Устарел", show_alert=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    await cb.message.answer_document(BufferedInputFile(text.encode(), filename=f"report_{report_id}.json"))
    await cb.answer()

@dp.callback_query(F.data == "search_info")
async def cb_search_info(cb: CallbackQuery):
    await cb.message.edit_text("Отправьте запрос (номер, ФИО или ник).", reply_markup=back_kb())
    await cb.answer()

@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message, state: FSMContext):
    q = msg.text.strip()
    note = await msg.answer(f"🔍 <b>Ищу:</b> {q}...", parse_mode="HTML")
    data = await search_everywhere(q)
    
    if not data:
        data = {"query": q, "date": datetime.now().strftime("%d.%m.%Y"), "fio": "НЕ НАЙДЕНО", "phone_main": q, "username": "Нет", "sources_list": []}
    
    report_id = str(uuid.uuid4())[:8]
    REPORTS_CACHE[report_id] = data
    await note.edit_text(f"✅ ФИО: <b>{data['fio']}</b>\nТел: <b>{data['phone_main']}</b>", parse_mode="HTML", reply_markup=result_kb(report_id))

@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID: return
    await msg.answer("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_menu_kb())

# --- RAW ТЕСТ ---
@dp.callback_query(F.data == "adm_raw_test")
async def cb_adm_raw_test(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID: return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("📡 <b>Отправляю тестовый запрос (79825207224)...</b>", parse_mode="HTML")
    
    res_bb = await fetch_blackbox("79825207224")
    res_mb = await fetch_mobius("79825207224")
    
    # Обрезаем, чтобы влезло в сообщение
    bb_str = json.dumps(res_bb, ensure_ascii=False)[:1500] if res_bb else "None/Ошибка"
    mb_str = json.dumps(res_mb, ensure_ascii=False)[:1500] if res_mb else "None/Ошибка"
    
    text = f"<b>BLACKBOX:</b>\n<pre>{bb_str}</pre>\n\n<b>MOBIUS:</b>\n<pre>{mb_str}</pre>"
    await cb.message.answer(text, parse_mode="HTML")
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
