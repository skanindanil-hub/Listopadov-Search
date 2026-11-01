
# crite.py — каскадный OSINT-бот (LEGO -> DevHydra -> Mobius)
import asyncio
import json
import html
import time
import os
import re
import logging
import aiohttp
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
    BufferedInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# --- НАСТРОЙКИ API ---
# API 1 (LEGO)
API_URL_1 = "https://api.lego.new.tg/v1/search"
API_KEY_1 = "LEGO-LORDKEY-p2TA1DqD98RUHGibdm"

# API 2 (DevHydra)
API_URL_2 = "https://api.devhydra.info/v1/search"
API_KEY_2 = "HYDRA-LordKEY-2eWKbyuQUJz6XQK5Pv"

# API 3 (Mobius - твой личный бессрочный)
API_URL_3 = "https://mobiusnet.cc.cd/api/v1/search"
API_KEY_3 = "MOBIUS-IO-IW5w3b4xKEY_dS8Kbt4FJswFEsTR97aGKhSEgfrJFjk"

# --- ОБЩИЕ НАСТРОЙКИ ---
BOT_TOKEN = "9042470492:AAGrM2pNdx6XaZqETRSZQh-G90VbMh23Jqo"
DB_PATH = "bot_data.db"
MAIN_ADMIN_ID = 8394631079
ADMIN_USERNAME = "anodoxx"

WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

CRYPTO_WALLET = "UQA7k7FbUJXC1ko40uBYfvv-jZ4WGnEiEIVsFcffXtFOU3pM"
PRICE_PER_REQ = 0.3

dp = Dispatcher(storage=MemoryStorage())

class BuyStates(StatesGroup):
    wait_amount = State()
    wait_currency = State()
    wait_hash = State()

class AdminStates(StatesGroup):
    give_reqs = State()
    take_reqs = State()
    broadcast = State()
    give_admin = State()
    take_admin = State()

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                requests_left INTEGER DEFAULT 3,
                joined_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        """)
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (MAIN_ADMIN_ID,))
        await db.commit()

async def is_admin(user_id) -> bool:
    if user_id == MAIN_ADMIN_ID: return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)) as cur:
            return await cur.fetchone() is not None

async def add_admin_db(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def del_admin_db(user_id):
    if user_id == MAIN_ADMIN_ID: return False
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        await db.commit()
    return True

async def add_user(user_id, username, first_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, username, first_name, requests_left, joined_at)
            VALUES (?, ?, ?, 3, ?)
        """, (user_id, username, first_name, int(time.time())))
        await db.commit()

async def get_balance(user_id) -> int:
    if await is_admin(user_id): return 999999
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT requests_left FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def deduct_request(user_id) -> bool:
    if await is_admin(user_id): return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT requests_left FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
        if not row or row[0] <= 0: return False
        await db.execute("UPDATE users SET requests_left = requests_left - 1 WHERE user_id=?", (user_id,))
        await db.commit()
        return True

async def add_requests(user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET requests_left = requests_left + ? WHERE user_id=?", (amount, user_id))
        await db.commit()

async def get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            return [row[0] for row in await cur.fetchall()]

# --- ТЕКСТЫ И API ---
WELCOME_TEXT = """🕵️ <b>LISTOPADOV SEARCH</b>
<i>OSINT-бот для поиска информации</i>

ℹ️ <b>Примеры для ввода команд</b>

🕵️ <b>Личность:</b>
<code>Фамилия Имя Отчество 01.01.1990</code>

📲 <b>Контакты:</b>
<code>79123456789</code> – телефон
<code>example@example.com</code> – email

🚘 <b>Транспорт:</b>
<code>А123БВ777</code> – авто
<code>XTA00000000000000</code> – VIN

💬 <b>Соцсети:</b>
<code>vk.com/username</code> – VK
<code>tiktok.com/@username</code> – TikTok
<code>instagram.com/username</code> – Inst
<code>ok.ru/profile/123</code> – OK

📟 <b>Telegram:</b>
<code>@username</code>, <code>tg123456789</code>

📄 <b>Документы:</b>
<code>/passport 1234567890</code>
<code>/snils 12345678901</code>
<code>/inn 123456789012</code>

🌐 <b>Онлайн-следы:</b>
<code>/tag профессия город</code>
<code>example.com</code> или <code>192.0.2.1</code>

🏚 <b>Недвижимость:</b>
<code>/adr Город, Улица, 1</code>
<code>77:01:0000000:1234</code>

📸 <i>Отправьте лицо, чтобы найти его.</i>"""

def detect_type(query: str) -> str:
    q = query.strip()
    if re.match(r"^\+?\d{10,15}$", q): return "phone"
    if "@" in q and "." in q.split("@")[-1]: return "email"
    if q.startswith("@") or q.startswith("t.me/") or q.startswith("https://t.me/"): return "telegram"
    if re.match(r"^[А-ЯA-Z]\d{3}[А-ЯA-Z]{2}\d{2,3}$", q.upper()): return "auto"
    return "name"

async def query_api(q: str) -> tuple[bool, dict | str]:
    q_type = detect_type(q)
    
    # Попытка 1: LEGO
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession() as session:
            resp = await session.get(API_URL_1, params={"q": q, "type": q_type, "key": API_KEY_1}, impersonate="chrome", timeout=15)
            if resp.status_code == 200 and "Just a moment" not in resp.text:
                return True, resp.json()
    except Exception as e:
        logging.warning(f"LEGO (curl) failed: {e}")

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL_1, params={"q": q, "type": q_type, "key": API_KEY_1}, ssl=False) as resp:
                if resp.status == 200:
                    raw = await resp.text()
                    if "Just a moment" not in raw:
                        return True, json.loads(raw)
    except Exception as e:
        logging.warning(f"LEGO (aiohttp) failed: {e}")

    # Попытка 2: DevHydra
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession() as session:
            resp = await session.get(API_URL_2, params={"q": q, "type": q_type, "key": API_KEY_2}, impersonate="chrome", timeout=15)
            if resp.status_code == 200 and "Just a moment" not in resp.text:
                return True, resp.json()
    except Exception as e:
        logging.warning(f"DevHydra (curl) failed: {e}")

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL_2, params={"q": q, "type": q_type, "key": API_KEY_2}, ssl=False) as resp:
                if resp.status == 200:
                    raw = await resp.text()
                    if "Just a moment" not in raw:
                        return True, json.loads(raw)
    except Exception as e:
        logging.warning(f"DevHydra (aiohttp) failed: {e}")

    # Попытка 3: Mobius (Резерв)
    logging.info("Переключение на резервный API Mobius...")
    try:
        headers = {"X-API-Key": API_KEY_3, "Accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL_3, headers=headers, params={"q": q}, ssl=False) as resp:
                if resp.status == 200:
                    return True, json.loads(await resp.text())
    except Exception as e:
        logging.warning(f"Mobius failed: {e}")

    return False, "Все три API сейчас недоступны (ошибка сети или блокировка Cloudflare)."

def build_html_report(data: dict, query: str) -> tuple[str, int]:
    content = ""
    results = data.get("results", {})
    records_count = 0
    
    if not isinstance(results, dict):
        results = {}

    for source_name, source_data in results.items():
        display_source_name = str(source_name).replace("mobius", "OASIS").replace("Mobius", "OASIS")
        content += f'<div class="source-header">📦 {html.escape(display_source_name.upper())}</div>'
        
        if not isinstance(source_data, dict):
            content += f'<div class="card"><pre>{html.escape(json.dumps(source_data, ensure_ascii=False, indent=2))}</pre></div>'
            continue
            
        inner_results = source_data.get("result", {}).get("data", {}).get("results", {})
        if not inner_results:
            content += '<div class="card"><span class="error">Нет данных</span></div>'
            continue
            
        if isinstance(inner_results, dict):
            for service_name, service_data in inner_results.items():
                if not isinstance(service_data, dict): continue
                items = ""
                for k, v in service_data.items():
                    if k in ("id", "profile_url") or isinstance(v, (dict, list)): continue
                    val_str = str(v)
                    val_html = f'<a href="{val_str}">{html.escape(val_str)}</a>' if val_str.startswith("http") else html.escape(val_str)
                    items += f'<div class="row"><div class="key">{k.capitalize()}:</div><div class="val">{val_html}</div></div>'
                if items: 
                    content += f'<div class="card"><h3>{service_name.upper()}</h3>{items}</div>'
                    records_count += 1
                    
    if not content: 
        content = f'<div class="card"><pre>{html.escape(json.dumps(data, ensure_ascii=False, indent=2))}</pre></div>'
        records_count = 0

    html_doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>OASIS</title>
    <style>body{{background:#0d1117;color:#c9d1d9;font-family:sans-serif;padding:20px}}
    .container{{max-width:600px;margin:0 auto}} h1{{color:#58a6ff;text-align:center}}
    .source-header{{background:#161b22;border-left:4px solid #58a6ff;padding:10px;margin:20px 0 10px;font-weight:bold}}
    .card{{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:15px;margin-bottom:15px}}
    .row{{display:flex;margin-bottom:8px}} .key{{width:40%;color:#8b949e}} .val{{width:60%;color:#c9d1d9;word-break:break-all}}
    a{{color:#58a6ff;text-decoration:none}} pre{{white-space:pre-wrap;font-size:12px}}</style></head>
    <body><div class="container"><h1>🕵️ OASIS SEARCH</h1><div style="text-align:center;margin-bottom:20px">Результат: {html.escape(query)}</div>
    {content}</div></body></html>"""
    
    return html_doc, records_count

# --- КЛАВИАТУРЫ ---
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile"), InlineKeyboardButton(text="💎 Купить запросы", callback_data="buy_menu")],
        [InlineKeyboardButton(text="📄 Оферта", callback_data="offer"), InlineKeyboardButton(text="🆘 Связь с админом", url=f"https://t.me/{ADMIN_USERNAME}")]
    ])

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выдать запросы", callback_data="adm_give"), InlineKeyboardButton(text="➖ Забрать запросы", callback_data="adm_take")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="👑 Выдать админку", callback_data="adm_set"), InlineKeyboardButton(text="🚫 Забрать админку", callback_data="adm_unset")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_menu")]
    ])

# --- ХЭНДЛЕРЫ (ПОЛЬЗОВАТЕЛИ) ---
@dp.message(Command("start"))
async def start(msg: Message):
    await add_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    await msg.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu())

@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    bal = await get_balance(cb.from_user.id)
    uname = f"@{cb.from_user.username}" if cb.from_user.username else "не задан"
    status = "👑 Администратор" if await is_admin(cb.from_user.id) else "👤 Пользователь"
    
    text = (
        f"╭┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈╮\n"
        f"   👤 <b>Личный кабинет</b>\n"
        f"╰┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈╯\n\n"
        f"🆔 <b>ID:</b> <code>{cb.from_user.id}</code>\n"
        f"📛 <b>Юзернейм:</b> {uname}\n"
        f"🎖 <b>Статус:</b> {status}\n"
        f"💳 <b>Баланс:</b> <code>{bal}</code> запросов"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]))
    await cb.answer()

@dp.callback_query(F.data == "offer")
async def cb_offer(cb: CallbackQuery):
    text = (
        "📄 <b>Публичная оферта</b>\n\n"
        "1. Бот предоставляет доступ к агрегатору открытых данных.\n"
        "2. Оплата запросов является добровольным взносом.\n"
        "3. Администрация не несет ответственности за использование данных.\n"
        "4. Возврат средств не предусмотрен."
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]))
    await cb.answer()

@dp.callback_query(F.data == "back_to_menu")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu())
    await cb.answer()

# --- ПОКУПКА ---
@dp.callback_query(F.data == "buy_menu")
async def cb_buy_menu(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text(
        f"💎 <b>Покупка запросов</b>\n\nЦена: <b>{PRICE_PER_REQ} TON</b> за 1 запрос.\nВведите количество:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]])
    )
    await state.set_state(BuyStates.wait_amount)
    await cb.answer()

@dp.message(BuyStates.wait_amount)
async def process_amount(msg: Message, state: FSMContext):
    try:
        amount = int(msg.text)
        if amount <= 0: raise ValueError
    except ValueError:
        return await msg.answer("❌ Введите целое число.")
    total_ton = amount * PRICE_PER_REQ
    await state.update_data(buy_amount=amount, total_ton=total_ton)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="curr_TON")],
        [InlineKeyboardButton(text="💠 GRAM", callback_data="curr_GRAM")],
        [InlineKeyboardButton(text="💵 USDT", callback_data="curr_USDT")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]
    ])
    await msg.answer(f"Вы хотите купить <b>{amount}</b> запросов.\nК оплате: <b>{total_ton} TON</b>.\nВыберите валюту:", parse_mode="HTML", reply_markup=kb)
    await state.set_state(BuyStates.wait_currency)

@dp.callback_query(BuyStates.wait_currency, F.data.startswith("curr_"))
async def process_currency(cb: CallbackQuery, state: FSMContext):
    currency = cb.data.split("_")[1]
    data = await state.get_data()
    amount = data.get("buy_amount")
    total_ton = data.get("total_ton")
    nano_amount = int(total_ton * 1_000_000_000)
    tonkeeper_link = f"ton://transfer/{CRYPTO_WALLET}?amount={nano_amount}"
    if currency == "TON": curr_text = f"Сумма: <b>{total_ton} TON</b>"
    elif currency == "GRAM": curr_text = f"Сумма: <b>{total_ton} GRAM</b>"
    else: curr_text = f"Сумма: <b>~{round(total_ton * 6.5, 2)} USDT</b>"
    text = f"💳 <b>Оплата: {amount} запросов</b>\n\nВалюта: <b>{currency}</b>\n{curr_text}\nКошелек: <code>{CRYPTO_WALLET}</code>\n\nОтправьте <b>хеш транзакции</b> сюда."
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 TONKEEPER", url=tonkeeper_link)],[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.update_data(currency=currency)
    await state.set_state(BuyStates.wait_hash)
    await cb.answer()

@dp.message(BuyStates.wait_hash)
async def process_tx_hash(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    amount, total_ton, currency = data.get("buy_amount"), data.get("total_ton"), data.get("currency", "TON")
    tx_hash = msg.text.strip()
    await state.clear()
    await msg.answer("⏳ Хеш получен. Отправлен на проверку.", reply_markup=main_menu())
    admin_text = f"💰 <b>Заявка!</b>\n👤 @{msg.from_user.username} (ID: <code>{msg.from_user.id}</code>)\n📦 {amount} запр.\n💵 {total_ton} TON ({currency})\n🔗 <code>{html.escape(tx_hash)}</code>"
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"appr_{msg.from_user.id}_{amount}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej_{msg.from_user.id}")]])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins") as cur:
            admins = [row[0] for row in await cur.fetchall()]
    for admin in admins:
        try: await bot.send_message(admin, admin_text, parse_mode="HTML", reply_markup=admin_kb)
        except: pass

@dp.callback_query(F.data.startswith("appr_"))
async def admin_approve(cb: CallbackQuery, bot: Bot):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    _, uid, amount = cb.data.split("_")
    await add_requests(int(uid), int(amount))
    await cb.message.edit_text(f"✅ Начислено {amount} запросов для {uid}.")
    try: await bot.send_message(int(uid), f"✅ Оплата подтверждена! Начислено {amount} запросов.", reply_markup=main_menu())
    except: pass
    await cb.answer()

@dp.callback_query(F.data.startswith("rej_"))
async def admin_reject(cb: CallbackQuery, bot: Bot):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    _, uid = cb.data.split("_")
    await cb.message.edit_text(f"❌ Отклонено для {uid}.")
    try: await bot.send_message(int(uid), "❌ Оплата отклонена.", reply_markup=main_menu())
    except: pass
    await cb.answer()

# --- АДМИН ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if not await is_admin(msg.from_user.id):
        return await msg.answer("⛔ У вас нет доступа.")
    await msg.answer("👑 <b>Админ-панель</b>\nВыберите действие:", parse_mode="HTML", reply_markup=admin_menu())

@dp.callback_query(F.data == "adm_give")
async def cb_adm_give(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Введите `ID количество` (например: `8394631079 50`):", parse_mode="Markdown")
    await state.set_state(AdminStates.give_reqs)
    await cb.answer()

@dp.callback_query(F.data == "adm_take")
async def cb_adm_take(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Введите `ID количество` для списания (например: `8394631079 10`):", parse_mode="Markdown")
    await state.set_state(AdminStates.take_reqs)
    await cb.answer()

@dp.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Отправьте сообщение для рассылки (текст или фото с подписью).\nДля отмены напишите /start")
    await state.set_state(AdminStates.broadcast)
    await cb.answer()

@dp.callback_query(F.data == "adm_set")
async def cb_adm_set(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Введите ID пользователя, которому нужно выдать админку:")
    await state.set_state(AdminStates.give_admin)
    await cb.answer()

@dp.callback_query(F.data == "adm_unset")
async def cb_adm_unset(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Введите ID пользователя, у которого нужно забрать админку:")
    await state.set_state(AdminStates.take_admin)
    await cb.answer()

# --- ОБРАБОТКА СОСТОЯНИЙ АДМИНА ---
@dp.message(AdminStates.give_reqs)
async def process_give(msg: Message, state: FSMContext):
    try:
        uid, amount = map(int, msg.text.split())
        await add_requests(uid, amount)
        await msg.answer(f"✅ Пользователю <code>{uid}</code> выдано <b>{amount}</b> запросов.", parse_mode="HTML", reply_markup=admin_menu())
    except ValueError:
        await msg.answer("❌ Ошибка формата. Введи: <code>ID количество</code>")
    await state.clear()

@dp.message(AdminStates.take_reqs)
async def process_take(msg: Message, state: FSMContext):
    try:
        uid, amount = map(int, msg.text.split())
        await add_requests(uid, -amount)
        await msg.answer(f"✅ У пользователя <code>{uid}</code> забрано <b>{amount}</b> запросов.", parse_mode="HTML", reply_markup=admin_menu())
    except ValueError:
        await msg.answer("❌ Ошибка формата. Введи: <code>ID количество</code>")
    await state.clear()

@dp.message(AdminStates.broadcast)
async def process_broadcast(msg: Message, state: FSMContext, bot: Bot):
    await state.clear()
    users = await get_all_users()
    status = await msg.answer(f"⏳ Начинаю рассылку на {len(users)} чел...")
    success, fail = 0, 0
    for uid in users:
        try:
            if msg.text: await bot.send_message(uid, msg.text, parse_mode="HTML")
            elif msg.photo: await bot.send_photo(uid, msg.photo[-1].file_id, caption=msg.caption, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05) 
        except Exception:
            fail += 1
    await status.edit_text(f"✅ Рассылка завершена.\nДоставлено: {success}\nОшибок: {fail}", reply_markup=admin_menu())

@dp.message(AdminStates.give_admin)
async def process_give_admin(msg: Message, state: FSMContext, bot: Bot):
    try:
        uid = int(msg.text)
        await add_admin_db(uid)
        await msg.answer(f"✅ Пользователю <code>{uid}</code> выдана админка.", parse_mode="HTML", reply_markup=admin_menu())
        try: await bot.send_message(uid, "👑 Вам выданы права администратора.", reply_markup=main_menu())
        except: pass
    except ValueError:
        await msg.answer("❌ Ошибка формата. Введи ID.")
    await state.clear()

@dp.message(AdminStates.take_admin)
async def process_take_admin(msg: Message, state: FSMContext, bot: Bot):
    try:
        uid = int(msg.text)
        if await del_admin_db(uid):
            await msg.answer(f"✅ У <code>{uid}</code> забрана админка.", parse_mode="HTML", reply_markup=admin_menu())
            try: await bot.send_message(uid, "🚫 Ваши права администратора отозваны.", reply_markup=main_menu())
            except: pass
        else:
            await msg.answer("❌ Нельзя забрать админку у главного администратора.")
    except ValueError:
        await msg.answer("❌ Ошибка формата. Введи ID.")
    await state.clear()

# --- ПОИСК ---
async def process_search(msg: Message, q: str):
    if not await deduct_request(msg.from_user.id):
        return await msg.answer("❌ Закончились запросы. Купите в /start", reply_markup=main_menu())
    note = await msg.answer(f"🔍 Ищу <code>{html.escape(q)}</code>…", parse_mode="HTML")
    ok, result = await query_api(q)
    if not ok:
        await add_requests(msg.from_user.id, 1)
        return await note.edit_text(f"❌ Ошибка: {html.escape(result)}")
    
    html_code, count = build_html_report(result, q)
    file = BufferedInputFile(html_code.encode('utf-8'), filename=f"oasis_{q[:10]}.html")
    bal = await get_balance(msg.from_user.id)
    
    await msg.answer_document(
        document=file, 
        caption=f"✅ Отчет: <code>{html.escape(q)}</code>\n📊 Найдено записей: <b>{count}</b>\n💰 Осталось: {bal}", 
        parse_mode="HTML", 
        reply_markup=main_menu()
    )
    await note.delete()

@dp.message(Command("search"))
async def cmd_search(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2: return await msg.answer("Укажи запрос.")
    await process_search(msg, parts[1].strip())

@dp.message()
async def auto_search(msg: Message, state: FSMContext):
    if msg.text and msg.text.startswith("/"): return
    current_state = await state.get_state()
    if current_state is not None: return
    if msg.text:
        await process_search(msg, msg.text.strip())

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
async def on_startup(bot: Bot):
    await init_db()
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook установлен: {WEBHOOK_URL}")

async def health_check(request):
    return web.Response(text="Bot is running")

def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    dp.startup.register(on_startup)
    
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
