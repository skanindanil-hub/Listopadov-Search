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

class SearchStates(StatesGroup):
    wait_partial_field = State()

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

Отправьте запрос сообщением."""

PARTIAL_TEXT = """📄 <b>Поиск по неполным данным</b>

Вы можете указать любое количество данных: фамилию, имя, отчество, дату или год рождения, возраст, место рождения и т.д.

Достаточно заполнить то, что у вас есть — все поля необязательны.
Нажмите на поле и введите значение. Когда всё готово — нажмите «Поиск»."""

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
            InlineKeyboardButton(text="📄 Скачать TXT", callback_data=f"get_txt_{report_id}"),
            InlineKeyboardButton(text="📦 Скачать JSON", callback_data=f"get_json_{report_id}")
        ],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_start")]
    ])

def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выдать", callback_data="adm_give"), InlineKeyboardButton(text="➖ Забрать", callback_data="adm_take")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm_promo"), InlineKeyboardButton(text="📊 Аналитика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="📡 Статус API", callback_data="adm_api_status"), InlineKeyboardButton(text="🚫 Бан", callback_data="adm_ban")],
        [InlineKeyboardButton(text="🛠 Тех. перерыв", callback_data="adm_maintenance"), InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
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

# --- ЛОГИКА ПОИСКА (ЖДЕМ ВСЕХ) ---
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
                    "operator": "-", "region": "-", "email": "-", "address": "-", "dob": "-", "passport": "-", "inn": "-",
                    "sources_list": [{"name": "Local DB", "count": 1}]
                }
        except: pass

    # Ждем ВСЕ API (до 45 секунд)
    tasks = [fetch_blackbox(q), fetch_mobius(q), fetch_nebula(q)]
    results = await asyncio.gather(*tasks)
    
    final_data = {"fio": "Нет", "phone_main": q, "username": "Нет", "operator": "Нет", "region": "Нет", "email": "Нет", "address": "Нет", "dob": "Нет", "passport": "Нет", "inn": "Нет", "sources_list": []}
    found_something = False

    for res in results:
        if not res or not isinstance(res, dict): continue

        # BlackBox
        if "profile" in res and res.get("status") == "success":
            found_something = True
            prof = res.get("profile", {})
            tel = res.get("telecom", {})
            locs = res.get("locations", [])
            
            if prof.get("primary_fio"): final_data["fio"] = prof["primary_fio"]
            if prof.get("birth_date"): final_data["dob"] = prof["birth_date"]
            if prof.get("passports") and isinstance(prof["passports"], list): final_data["passport"] = prof["passports"][0]
            if prof.get("inn"): final_data["inn"] = prof["inn"]
            
            if tel.get("query_phone"): final_data["phone_main"] = tel["query_phone"]
            if tel.get("operator"): final_data["operator"] = tel["operator"]
            if tel.get("region"): final_data["region"] = tel["region"]
            if locs: final_data["address"] = locs[0].get("address", "Нет")
            final_data["sources_list"].append({"name": "BlackBox", "count": 1})

        # Mobius/Nebula
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
            final_data["sources_list"].append({"name": "Mobius (Nebula)", "count": 1})

    return final_data if found_something else None

# --- HTML С ЧЕРНЫМ ДИЗАЙНОМ И ГРАФОМ ---
def generate_html_report(data: dict, report_id: str) -> str:
    nodes = [{"id": 1, "label": f"Цель: {data['query']}", "color": "#38bdf8", "font": {"color": "white"}}]
    edges = []
    node_id = 2
    
    if data.get('fio') and data['fio'] != "Нет":
        nodes.append({"id": node_id, "label": f"ФИО: {data['fio']}", "color": "#4ade80"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('phone_main') and data['phone_main'] != "Нет":
        nodes.append({"id": node_id, "label": f"Тел: {data['phone_main']}", "color": "#38bdf8"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('operator') and data['operator'] != "Нет":
        nodes.append({"id": node_id, "label": f"Оператор: {data['operator']}", "color": "#facc15"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('email') and data['email'] != "Нет":
        nodes.append({"id": node_id, "label": f"Email: {data['email']}", "color": "#a78bfa"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('passport') and data['passport'] != "Нет":
        nodes.append({"id": node_id, "label": f"Паспорт: {data['passport']}", "color": "#f87171"}); edges.append({"from": 1, "to": node_id}); node_id += 1
    if data.get('inn') and data['inn'] != "Нет":
        nodes.append({"id": node_id, "label": f"ИНН: {data['inn']}", "color": "#fbbf24"}); edges.append({"from": 1, "to": node_id}); node_id += 1

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
            body {{ font-family: sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 20px; }}
            .container {{ max-width: 800px; margin: 0 auto; background: #1e293b; padding: 20px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }}
            h1 {{ color: #38bdf8; font-size: 24px; margin-bottom: 5px; }}
            .subtitle {{ color: #94a3b8; font-size: 14px; margin-bottom: 20px; }}
            .graph-section {{ height: 500px; background: #0f172a; border-radius: 12px; margin: 20px 0; border: 1px solid #334155; }}
            .data-item {{ padding: 12px 0; border-bottom: 1px solid #334155; }}
            .d-label {{ font-size: 11px; color: #64748b; text-transform: uppercase; }}
            .d-value {{ font-size: 16px; font-weight: 500; color: #f8fafc; }}
            .legend {{ display: flex; gap: 15px; flex-wrap: wrap; margin-bottom: 15px; }}
            .legend-item {{ display: flex; align-items: center; gap: 5px; font-size: 12px; }}
            .dot {{ width: 10px; height: 10px; border-radius: 50%; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>OASIS DIGITAL</h1>
            <p class="subtitle">Отчет по запросу: {data['query']}</p>
            
            <div class="legend">
                <div class="legend-item"><div class="dot" style="background:#38bdf8;"></div>Цель/Тел</div>
                <div class="legend-item"><div class="dot" style="background:#4ade80;"></div>ФИО</div>
                <div class="legend-item"><div class="dot" style="background:#facc15;"></div>Оператор</div>
                <div class="legend-item"><div class="dot" style="background:#a78bfa;"></div>Email</div>
                <div class="legend-item"><div class="dot" style="background:#f87171;"></div>Паспорт</div>
            </div>

            <div id="mynetwork" class="graph-section"></div>

            <div class="data-item"><div class="d-label">ФИО</div><div class="d-value">{data['fio']}</div></div>
            <div class="data-item"><div class="d-label">Телефон</div><div class="d-value">{data['phone_main']}</div></div>
            <div class="data-item"><div class="d-label">Оператор / Регион</div><div class="d-value">{data['operator']} / {data['region']}</div></div>
            <div class="data-item"><div class="d-label">Email</div><div class="d-value">{data['email']}</div></div>
            <div class="data-item"><div class="d-label">Адрес</div><div class="d-value">{data['address']}</div></div>
            <div class="data-item"><div class="d-label">Паспорт</div><div class="d-value">{data['passport']}</div></div>
            <div class="data-item"><div class="d-label">Дата рождения</div><div class="d-value">{data['dob']}</div></div>
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
                interaction: {{ hover: true, tooltipDelay: 200 }}
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

# --- НЕПОЛНЫЙ ПОИСК ---
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
    data = await state.get_data()
    query_parts = []
    if data.get('surname'): query_parts.append(f"Фамилия: {data['surname']}")
    if data.get('name'): query_parts.append(f"Имя: {data['name']}")
    if data.get('patronymic'): query_parts.append(f"Отчество: {data['patronymic']}")
    if data.get('birth_date'): query_parts.append(f"Дата: {data['birth_date']}")
    if data.get('country'): query_parts.append(f"Страна: {data['country']}")
    
    query_str = ", ".join(query_parts) if query_parts else "Пустой запрос"
    
    note = await cb.message.edit_text(f"🔍 <b>[OASIS DIGITAL]</b> Идет поиск по базам данных...\n\n<b>Цель:</b> {query_str}\n<b>Тип:</b> partial", parse_mode="HTML")
    data_result = {"query": query_str, "date": datetime.now().strftime("%d.%m.%Y"), "fio": "НЕ НАЙДЕНО", "phone_main": "-", "username": "-", "operator": "-", "region": "-", "email": "-", "address": "-", "dob": "-", "passport": "-", "inn": "-", "sources_list": []}
    
    report_id = str(uuid.uuid4())[:8]
    REPORTS_CACHE[report_id] = data_result
    
    await note.edit_text(f"✅ <b>Отчет готов!</b>\nФИО: <b>{data_result['fio']}</b>", parse_mode="HTML", reply_markup=result_kb(report_id))
    await cb.answer()

# --- ОБЫЧНЫЙ ПОИСК ---
@dp.message(F.text & ~F.text.startswith("/"))
async def process_search(msg: Message, state: FSMContext):
    q = msg.text.strip()
    
    note = await msg.answer(f"🔍 <b>[OASIS DIGITAL]</b> Идет поиск по базам данных...\n\n<b>Цель:</b> {q}\n<b>Тип:</b> auto", parse_mode="HTML")
    
    data = await search_everywhere(q)
    
    if not data:
        data = {
            "query": q, "date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "fio": "НЕ НАЙДЕНО", "phone_main": q, "username": "Нет",
            "operator": "-", "region": "-", "email": "-", "address": "-", "dob": "-", "passport": "-", "inn": "-", "sources_list": []
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

# --- АДМИНКА ---
@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID: return await msg.answer("⛔ Нет доступа.")
    await msg.answer("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_menu_kb())

@dp.callback_query(F.data == "adm_api_status")
async def cb_adm_api_status(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID: return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("📡 <b>Проверяю API...</b>", parse_mode="HTML")
    res_bb = await fetch_blackbox("79825207224")
    res_mb = await fetch_mobius("79825207224")
    res_nb = await fetch_nebula("79825207224")
    text = f"📡 <b>Статус API:</b>\n\n{'🟢 OK' if res_bb else '🔴 Ошибка'} — BlackBox\n{'🟢 OK' if res_mb else '🔴 Ошибка'} — Mobius\n{'🟢 OK' if res_nb else '🔴 Ошибка'} — Nebula"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=admin_menu_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("adm_"))
async def cb_adm_stub(cb: CallbackQuery):
    if cb.from_user.id != MAIN_ADMIN_ID: return await cb.answer("Нет доступа", show_alert=True)
    await cb.answer("Функция в разработке (требует БД)", show_alert=True)

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request):
    from aiogram.types import Update
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
