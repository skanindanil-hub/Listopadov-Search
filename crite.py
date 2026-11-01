import asyncio
import json
import html
import time
import os
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

# --- НАСТРОЙКИ ---
# На Render токены лучше задавать через Environment Variables, но для теста можно оставить тут
BOT_TOKEN = os.getenv("BOT_TOKEN", "9042470492:AAGrM2pNdx6XaZqETRSZQh-G90VbMh23Jqo")
API_KEY = os.getenv("API_KEY", "MOBIUS-IO-m8CwLHrwIBHnTWCW_pSYPjYgdGD9rsJ31S-RPUnAmro")
API_URL = "https://mobiusnet.cc.cd/api/v1/search"
DB_PATH = "bot_data.db"
ADMIN_IDS = {8394631079}

# Render автоматически выдает URL в переменную RENDER_EXTERNAL_URL
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
        await db.commit()

async def add_user(user_id, username, first_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, username, first_name, requests_left, joined_at)
            VALUES (?, ?, ?, 3, ?)
        """, (user_id, username, first_name, int(time.time())))
        await db.commit()

async def get_balance(user_id) -> int:
    if user_id in ADMIN_IDS: return 999999
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT requests_left FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def deduct_request(user_id) -> bool:
    if user_id in ADMIN_IDS: return True
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

# --- ТЕКСТЫ И API ---
WELCOME_TEXT = """🕵️ <b>OASIS SEARCH</b>
<i>OSINT-бот для поиска информации</i>

📲 <b>Примеры:</b> <code>79991234567</code>, <code>@username</code>, <code>ФИО</code>"""

async def query_api(q: str) -> tuple[bool, dict | str]:
    headers = {"X-API-Key": API_KEY, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=60)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL, headers=headers, params={"q": q}, ssl=False) as resp:
                raw = await resp.text()
                if resp.status != 200: return False, f"HTTP {resp.status}"
                try: return True, json.loads(raw)
                except: return False, "Не JSON"
    except asyncio.TimeoutError: return False, "Таймаут API (60с)"
    except Exception as e: return False, f"Ошибка: {type(e).__name__}"

def format_html_report(data: dict, query: str) -> str:
    content = ""
    results = data.get("results", {})
    for source_name, source_data in results.items():
        display_source_name = source_name.replace("mobius", "OASIS").replace("Mobius", "OASIS")
        content += f'<div class="source-header">📦 {html.escape(display_source_name.upper())}</div>'
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
                if items: content += f'<div class="card"><h3>{service_name.upper()}</h3>{items}</div>'
    if not content: content = f'<div class="card"><pre>{html.escape(json.dumps(data, ensure_ascii=False, indent=2))}</pre></div>'
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>OASIS</title>
    <style>body{{background:#0d1117;color:#c9d1d9;font-family:sans-serif;padding:20px}}
    .container{{max-width:600px;margin:0 auto}} h1{{color:#58a6ff;text-align:center}}
    .source-header{{background:#161b22;border-left:4px solid #58a6ff;padding:10px;margin:20px 0 10px;font-weight:bold}}
    .card{{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:15px;margin-bottom:15px}}
    .row{{display:flex;margin-bottom:8px}} .key{{width:40%;color:#8b949e}} .val{{width:60%;color:#c9d1d9;word-break:break-all}}
    a{{color:#58a6ff;text-decoration:none}} pre{{white-space:pre-wrap;font-size:12px}}</style></head>
    <body><div class="container"><h1>🕵️ OASIS SEARCH</h1><div style="text-align:center;margin-bottom:20px">Результат: {html.escape(query)}</div>
    {content}</div></body></html>"""

# --- КЛАВИАТУРЫ ---
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton(text="💎 Купить запросы", callback_data="buy_menu")]
    ])

# --- ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(msg: Message):
    await add_user(msg.from_user.id, msg.from_user.username, msg.from_user.first_name)
    await msg.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu())

@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    bal = await get_balance(cb.from_user.id)
    uname = f"@{cb.from_user.username}" if cb.from_user.username else "нет"
    text = f"👤 <b>Профиль</b>\n\n🆔 ID: <code>{cb.from_user.id}</code>\n📛 Юзер: {uname}\n💳 Запросов: <code>{bal}</code>"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]))
    await cb.answer()

@dp.callback_query(F.data == "back_to_menu")
async def cb_back(cb: CallbackQuery):
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu())
    await cb.answer()

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
    for admin in ADMIN_IDS:
        try: await bot.send_message(admin, admin_text, parse_mode="HTML", reply_markup=admin_kb)
        except: pass

@dp.callback_query(F.data.startswith("appr_"))
async def admin_approve(cb: CallbackQuery, bot: Bot):
    _, uid, amount = cb.data.split("_")
    await add_requests(int(uid), int(amount))
    await cb.message.edit_text(f"✅ Начислено {amount} запросов для {uid}.")
    try: await bot.send_message(int(uid), f"✅ Оплата подтверждена! Начислено {amount} запросов.", reply_markup=main_menu())
    except: pass
    await cb.answer()

@dp.callback_query(F.data.startswith("rej_"))
async def admin_reject(cb: CallbackQuery, bot: Bot):
    _, uid = cb.data.split("_")
    await cb.message.edit_text(f"❌ Отклонено для {uid}.")
    try: await bot.send_message(int(uid), "❌ Оплата отклонена.", reply_markup=main_menu())
    except: pass
    await cb.answer()

async def process_search(msg: Message, q: str):
    if not await deduct_request(msg.from_user.id):
        return await msg.answer("❌ Закончились запросы. Купите в /start", reply_markup=main_menu())
    note = await msg.answer(f"🔍 Ищу <code>{html.escape(q)}</code>…", parse_mode="HTML")
    ok, result = await query_api(q)
    if not ok:
        await add_requests(msg.from_user.id, 1)
        return await note.edit_text(f"❌ Ошибка: {html.escape(result)}")
    html_code = format_html_report(result, q)
    file = BufferedInputFile(html_code.encode('utf-8'), filename=f"oasis_{q[:10]}.html")
    bal = await get_balance(msg.from_user.id)
    await msg.answer_document(document=file, caption=f"✅ Отчет: <code>{html.escape(q)}</code>\n💰 Осталось: {bal}", parse_mode="HTML", reply_markup=main_menu())
    await note.delete()

@dp.message(Command("search"))
async def cmd_search(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2: return await msg.answer("Укажи запрос.")
    await process_search(msg, parts[1].strip())

@dp.message()
async def auto_search(msg: Message):
    if msg.text and not msg.text.startswith("/"):
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
    
    # Роут для проверки статуса (чтобы Render не усыплял бота)
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    # Обработчик вебхуков от Telegram
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    
    setup_application(app, dp, bot=bot)
    
    # Render сам передает порт через переменную PORT
    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()