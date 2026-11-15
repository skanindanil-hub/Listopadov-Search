import asyncio
import logging
import os
import io
import json
import uuid
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# --- МАТПЛОТЛИБ ДЛЯ ГРАФОВ ---
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import networkx as nx

# --- НАСТРОЙКИ ---
BOT_TOKEN = "8704046668:AAGqPaGJntSegYzKBWspTFfj1GQxDBWeN2E"
WEBHOOK_HOST = "https://oasis-digital.onrender.com" # <-- ПОМЕНЯЙ НА СВОЙ URL RENDER
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

REPORTS_CACHE = {}
templates = Jinja2Templates(directory="templates")

# --- ИМИТАЦИЯ БАЗЫ ДАННЫХ ---
async def search_in_db(q: str):
    return {
        "query": q,
        "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "fio": "ВЕРЯСКИНА ОЛЬГА АЛЕКСАНДРОВНА",
        "phone_main": "+7 (932) 249-95-55",
        "completeness": "88%",
        "stats": {"sources": 11, "phones": 2, "records": 18, "emails": 1, "addresses": 2},
        "operator": "МегаФон",
        "region": "г.Москва и Московская область",
        "country": "Россия",
        "passport": "6720 945700",
        "inn": "861200554128",
        "snils": "06911632967",
        "email": "veryaskina.olya@gmail.com",
        "dob": "30.08.1975",
        "age": "51 лет",
        "address": "Ханты-Мансийский Автономный округ - Югра АО, г Пыть-Ях, мкр 2а Лесников, ул Советская,2,2",
        "sources_list": [
            {"name": "Красное&Белое 2019", "count": 1},
            {"name": "Russia Tax 2023", "count": 1},
            {"name": "Facebook Россия 2021", "count": 1},
            {"name": "ФНС 2022", "count": 3},
            {"name": "ФОМС 2025", "count": 2},
            {"name": "Телефонные книги РФ 2026", "count": 3},
            {"name": "Пользователи gosuslugi.ru 2023", "count": 1}
        ]
    }

# --- ГЕНЕРАЦИЯ ГРАФА (PNG) ---
def generate_graph_png(data: dict) -> bytes:
    G = nx.Graph()
    center = data['phone_main']
    G.add_node(center, color='#38bdf8', label="ТЕЛЕФОН")
    
    if data.get('fio'):
        G.add_node(data['fio'], color='#4ade80', label="ФИО")
        G.add_edge(center, data['fio'])
    
    if data.get('email'):
        G.add_node(data['email'], color='#facc15', label="EMAIL")
        G.add_edge(center, data['email'])
        
    if data.get('address'):
        G.add_node("АДРЕС", color='#f87171', label="АДРЕС")
        G.add_edge(center, "АДРЕС")

    plt.figure(figsize=(8, 6), facecolor='#f5f6f8')
    ax = plt.gca()
    ax.set_facecolor('#f5f6f8')
    pos = nx.spring_layout(G, seed=42)
    node_colors = [nx.get_node_attributes(G, 'color').get(n, '#fff') for n in G.nodes()]
    
    nx.draw_networkx_nodes(G, pos, node_color=node_colors, node_size=3000, alpha=0.9)
    nx.draw_networkx_edges(G, pos, width=2, edge_color='#94a3b8')
    nx.draw_networkx_labels(G, pos, labels=nx.get_node_attributes(G, 'label'), font_size=10, font_color='black', font_weight='bold')
    
    plt.axis('off')
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', facecolor='#f5f6f8')
    buf.seek(0)
    plt.close()
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
    if not data:
        return HTMLResponse("<h1>404 Отчет не найден или устарел</h1>", status_code=404)
    return templates.TemplateResponse("report.html", {"request": request, "d": data, "report_id": report_id})

@app.get("/report/{report_id}/graph")
async def web_graph(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data:
        raise HTTPException(status_code=404, detail="Not found")
    img_bytes = generate_graph_png(data)
    return Response(content=img_bytes, media_type="image/png")

@app.get("/export/{report_id}/json")
async def export_json(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json")

@app.get("/export/{report_id}/txt")
async def export_txt(report_id: str):
    data = REPORTS_CACHE.get(report_id)
    if not data:
        raise HTTPException(status_code=404, detail="Not found")
    text = f"ОТЧЕТ OASIS DIGITAL ПО ЗАПРОСУ: {data['query']}\n"
    text += f"ФИО: {data['fio']}\nТЕЛЕФОН: {data['phone_main']}\nEmail: {data['email']}\n"
    text += f"Паспорт: {data['passport']}\nИНН: {data['inn']}\nАдрес: {data['address']}\n"
    return PlainTextResponse(content=text, headers={"Content-Disposition": f"attachment; filename=oasis_report_{report_id}.txt"})

# --- БОТ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("<b>Oasis Digital</b>\nОтправьте запрос (номер, ФИО).", parse_mode="HTML")

@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message):
    q = msg.text.strip()
    note = await msg.answer(f"🔍 <b>Oasis Digital ищет:</b> {q}...", parse_mode="HTML")
    
    data = await search_in_db(q)
    report_id = str(uuid.uuid4())[:8]
    REPORTS_CACHE[report_id] = data
    
    web_link = f"{WEBHOOK_HOST}/report/{report_id}"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Веб-отчет (Граф)", url=web_link)],
        [
            InlineKeyboardButton(text="📄 Скачать TXT", url=f"{WEBHOOK_HOST}/export/{report_id}/txt"),
            InlineKeyboardButton(text="📦 Скачать JSON", url=f"{WEBHOOK_HOST}/export/{report_id}/json")
        ]
    ])
    
    await note.edit_text(
        f"✅ <b>Отчет Oasis Digital готов!</b>\n"
        f"ФИО: <b>{data['fio']}</b>\n"
        f"Тел: <b>{data['phone_main']}</b>\n\n"
        f"<i>Нажмите на кнопку ниже, чтобы открыть веб-отчет с графом связей.</i>",
        parse_mode="HTML",
        reply_markup=kb
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
