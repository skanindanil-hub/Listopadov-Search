# crite.py — основной бот Listopadov Search для Render (с API для сайта)
import asyncio
import json
import html
import re
import os
import time
import logging
import aiosqlite
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# --- НАСТРОЙКИ API ---
API_URL_1 = "https://api.lego.new.tg/v1/search"
API_KEY_1 = "LEGO-LORDKEY-p2TA1DqD98RUHGibdm"
API_URL_2 = "https://api.devhydra.info/v1/search"
API_KEY_2 = "HYDRA-LordKEY-2eWKbyuQUJz6XQK5Pv"
API_URL_3 = "https://mobiusnet.cc.cd/api/v1/search"
API_KEY_3 = "MOBIUS-IO-IW5w3b4xKEY_dS8Kbt4FJswFEsTR97aGKhSEgfrJFjk"

# --- НАСТРОЙКИ БОТА ---
BOT_TOKEN = "9042470492:AAGrM2pNdx6XaZqETRSZQh-G90VbMh23Jqo"
BOT_USERNAME = "ListopadovSearchBot"
MAIN_ADMIN_ID = 8394631079
DB_PATH = "bot_data.db"

# --- НАСТРОЙКИ ОПЛАТЫ ---
CRYPTO_WALLET = "UQA7k7FbUJXC1ko40uBYfvv-jZ4WGnEiEIVsFcffXtFOU3pM"
PRICE_PER_REQ_TON = 0.03
RATE_TON_TO_USDT = 6.5 

# --- НАСТРОЙКИ RENDER ---
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://listopadov-search-1.onrender.com")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

logging.basicConfig(level=logging.INFO)
dp = Dispatcher(storage=MemoryStorage())

class BuyStates(StatesGroup):
    wait_custom_amount = State()
    wait_currency = State()
    wait_hash = State()

class AdminStates(StatesGroup):
    give_reqs = State()
    take_reqs = State()

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                requests_left INTEGER DEFAULT 3,
                referrer_id INTEGER,
                joined_at INTEGER
            )
        """)
        await db.commit()

async def add_user(user_id, username, referrer_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)) as cur:
            if await cur.fetchone(): return False
        await db.execute("""
            INSERT INTO users (user_id, username, requests_left, referrer_id, joined_at)
            VALUES (?, ?, 3, ?, ?)
        """, (user_id, username, referrer_id, int(time.time())))
        await db.commit()
        return True

async def get_balance(user_id) -> int:
    if user_id == MAIN_ADMIN_ID: return 999999
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT requests_left FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def deduct_request(user_id) -> bool:
    if user_id == MAIN_ADMIN_ID: return True
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

async def get_invited_count(user_id) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

# --- ТЕКСТЫ ---
WELCOME_TEXT = """ℹ️ <b>Примеры для ввода команд</b>

🕵️ <b>Личность:</b>
<code>Фамилия Имя Отчество 01.01.1990</code>

📲 <b>Контакты:</b>
<code>79123456789</code> – номер телефона
<code>example@example.com</code> – email

🚘 <b>Транспорт:</b>
<code>А123БВ777</code> – номер автомобиля
<code>XTA00000000000000</code> – VIN автомобиля

💬 <b>Социальные сети:</b>
<code>vk.com/username</code> – Вконтакте
<code>tiktok.com/@username</code> – TikTok
<code>instagram.com/username</code> – Instagram
<code>ok.ru/profile/123456789</code> – Одноклассники

📟 <b>Telegram:</b>
<code>@username</code>, <code>tg123456789</code> – логин или ID

📄 <b>Документы:</b>
<code>/vu 1234567890</code> – водительские права
<code>/passport 1234567890</code> – паспорт
<code>/snils 12345678901</code> – СНИЛС
<code>/inn 123456789012</code> – ИНН

🌐 <b>Онлайн-следы:</b>
<code>/tag профессия город</code> – поиск по телефонным книгам
<code>example.com</code> или <code>192.0.2.1</code> – домен или IP

🏚 <b>Недвижимость:</b>
<code>/adr Город, Улица, 1</code>
<code>77:01:0000000:1234</code> – кадастровый номер

🏢 <b>Юридическое лицо:</b>
<code>/inn 1234567890</code> – ИНН
<code>1234567890123</code> – ОГРН или ОГРНИП

📸 <i>Отправьте лицо человека, чтобы попробовать найти его.</i>"""

HELP_TEXT = """✅ <b>Подписка успешно подтверждена!</b>\n\n""" + WELCOME_TEXT

def detect_type(query: str) -> str:
    q = query.strip()
    if re.match(r"^\+?\d{10,15}$", q): return "phone"
    if "@" in q and "." in q.split("@")[-1]: return "email"
    if q.startswith("@") or q.startswith("t.me/") or q.startswith("https://t.me/"): return "telegram"
    if re.match(r"^[А-ЯA-Z]\d{3}[А-ЯA-Z]{2}\d{2,3}$", q.upper()): return "auto"
    return "name"

async def fetch_api(url: str, params: dict, headers: dict = None) -> tuple[bool, dict | str]:
    timeout = aiohttp.ClientTimeout(total=25)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    raw = await resp.text()
                    if "Just a moment" not in raw:
                        return True, json.loads(raw)
                return False, f"HTTP {resp.status}"
    except asyncio.TimeoutError:
        return False, "Таймаут"
    except Exception as e:
        return False, str(e)

async def query_all_apis(q: str) -> list[dict]:
    q_type = detect_type(q)
    tasks = [
        fetch_api(API_URL_1, {"q": q, "type": q_type, "key": API_KEY_1}),
        fetch_api(API_URL_2, {"q": q, "type": q_type, "key": API_KEY_2}),
        fetch_api(API_URL_3, {"q": q}, headers={"X-API-Key": API_KEY_3})
    ]
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)
    except asyncio.TimeoutError:
        return []
    
    combined = []
    sources = ["LEGO", "DevHydra", "Mobius"]
    for i, res in enumerate(results):
        if isinstance(res, tuple) and res[0]:
            combined.append({"source": sources[i], "data": res[1]})
    return combined

def clean_data(obj):
    if isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            if k in ("id", "profile_url", "success", "query", "elapsed_seconds", "results_count", "status", "count", "total_records", "latency_ms", "type", "kind", "target", "kind_tag", "stype"):
                continue
            if v is None or v == "" or v == {} or v == []:
                continue
            res[k] = clean_data(v)
        return res
    elif isinstance(obj, list):
        return [clean_data(v) for v in obj if v]
    return obj

def render_text_results(api_results: list[dict]) -> str:
    lines = []
    for res in api_results:
        source = res["source"]
        data = clean_data(res["data"])
        results_dict = data.get("results", {})
        
        items = []
        if isinstance(results_dict, list): items = results_dict
        elif isinstance(results_dict, dict): items = list(results_dict.values())

        for src_data in items:
            if not isinstance(src_data, dict): continue
            inner_results = src_data.get("result", {}).get("data", {}).get("results", {})
            if not inner_results: continue
                
            if f"📦 <b>{source.upper()}</b>" not in lines:
                lines.append(f"📦 <b>{source.upper()}</b>")

            def find_val(keys, data_dict):
                for k in keys:
                    if k in data_dict: return data_dict[k]
                return None

            items_to_process = inner_results if isinstance(inner_results, list) else [inner_results]
            
            for item in items_to_process:
                if not isinstance(item, dict): continue
                main_info = []
                ph = find_val(["PHONE_INFO", "phone"], item)
                if ph and isinstance(ph, dict):
                    if ph.get('phone'): main_info.append(f"<b>Телефон:</b> <code>{html.escape(str(ph.get('phone')))}</code>")
                    if ph.get('operator'): main_info.append(f"<b>Оператор:</b> {html.escape(str(ph.get('operator')))}")
                    if ph.get('region'): main_info.append(f"<b>Регион:</b> {html.escape(str(ph.get('region')))}")
                
                names = find_val(["NAMES", "names", "name"], item)
                if names:
                    n_str = ", ".join([str(n) for n in names if n]) if isinstance(names, list) else str(names)
                    if n_str: main_info.append(f"<b>Имя:</b> <code>{html.escape(n_str)}</code>")
                
                if main_info:
                    lines.append("<blockquote>" + "\n".join(main_info) + "</blockquote>")

                socials = []
                tg = find_val(["TELEGRAM", "telegram"], item)
                if tg and isinstance(tg, dict):
                    for k, v in tg.items():
                        if v: socials.append(f"<b>{k.capitalize()}:</b> <code>{html.escape(str(v))}</code>")
                
                emails = find_val(["EMAILS", "emails", "email"], item)
                if emails:
                    e_str = ", ".join([str(e) for e in emails if e]) if isinstance(emails, list) else str(emails)
                    if e_str: socials.append(f"<b>Email:</b> <code>{html.escape(e_str)}</code>")

                if socials:
                    lines.append("📱 <b>Соцсети:</b>\n<blockquote>" + "\n".join(socials) + "</blockquote>")

                addrs = find_val(["ADDRESSES", "addresses", "address"], item)
                if addrs:
                    addr_lines = [f"• {html.escape(str(a))}" for a in (addrs if isinstance(addrs, list) else [addrs]) if a]
                    if addr_lines:
                        lines.append("🏠 <b>Адреса:</b>\n<blockquote>" + "\n".join(addr_lines) + "</blockquote>")

                dbs = find_val(["databases", "DATABASES", "breaches", "BREACHES"], item)
                if dbs and isinstance(dbs, list):
                    db_names = {}
                    for db in dbs:
                        if isinstance(db, dict):
                            name = db.get("name") or db.get("source") or db.get("db_name") or "Неизвестная база"
                            db_names[name] = db_names.get(name, 0) + 1
                    if db_names:
                        db_lines = [f"• <b>{html.escape(str(name))}</b> ({count} зап.)" for name, count in db_names.items()]
                        lines.append("🗂 <b>Базы данных:</b>\n<blockquote>" + "\n".join(db_lines) + "</blockquote>")

    res_str = "\n\n".join(lines)
    if not res_str: return "🔍 Данных не найдено."
    if len(res_str) > 3800: res_str = res_str[:3800] + "\n… [обрезано]"
    return res_str

# --- МЕНЮ ---
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"), InlineKeyboardButton(text="💎 Купить запросы", callback_data="buy_menu")],
        [InlineKeyboardButton(text="👥 Пригласить друга", callback_data="ref_link")]
    ])

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выдать запросы", callback_data="adm_give"), InlineKeyboardButton(text="➖ Забрать", callback_data="adm_take")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_menu")]
    ])

# --- ХЭНДЛЕРЫ БОТА ---
@dp.message(CommandStart())
async def start(msg: Message, command: CommandStart):
    referrer_id = None
    if command.args and command.args.isdigit():
        referrer_id = int(command.args)
        if referrer_id == msg.from_user.id: referrer_id = None

    is_new = await add_user(msg.from_user.id, msg.from_user.username, referrer_id)
    
    if is_new and referrer_id:
        await add_requests(referrer_id, 2) 
        try:
            await msg.bot.send_message(referrer_id, f"🎉 По вашей ссылке пришел новый пользователь! Вам начислено <b>+2 запроса</b>.", parse_mode="HTML")
        except: pass

    await msg.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu())

@dp.callback_query(F.data == "profile")
async def cb_profile(cb: CallbackQuery):
    bal = await get_balance(cb.from_user.id)
    ref_count = await get_invited_count(cb.from_user.id)
    uname = f"@{cb.from_user.username}" if cb.from_user.username else "нет"
    status = "👑 Админ" if cb.from_user.id == MAIN_ADMIN_ID else "👤 Юзер"
    
    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{cb.from_user.id}</code>\n"
        f"📛 Юзер: {uname}\n"
        f"🎖 Статус: {status}\n"
        f"💳 Баланс: <code>{bal}</code> запросов\n"
        f"👥 Приглашено: <b>{ref_count}</b>"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]))
    await cb.answer()

@dp.callback_query(F.data == "ref_link")
async def cb_ref_link(cb: CallbackQuery):
    ref_link = f"https://t.me/{BOT_USERNAME}?start={cb.from_user.id}"
    share_text = f"Попробуй этот OSINT-бот! Ссылка: {ref_link}"
    
    text = (
        f"👥 <b>Пригласи друга</b>\n\n"
        f"За каждого, кто зайдет по ссылке, ты получишь <b>+2 запроса</b>.\n\n"
        f"🔗 <code>{ref_link}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url={ref_link}&text={share_text}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data == "back_to_menu")
async def cb_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu())
    await cb.answer()

# --- ПОКУПКА ЗАПРОСОВ ---
@dp.callback_query(F.data == "buy_menu")
async def cb_buy_menu(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10 запросов", callback_data="buy_10"), InlineKeyboardButton(text="50 запросов", callback_data="buy_50")],
        [InlineKeyboardButton(text="100 запросов", callback_data="buy_100"), InlineKeyboardButton(text="1000 запросов", callback_data="buy_1000")],
        [InlineKeyboardButton(text="✏️ Ввести свое количество", callback_data="buy_custom")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])
    await cb.message.edit_text("💎 <b>Выберите пакет запросов:</b>", parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("buy_"))
async def cb_buy_package(cb: CallbackQuery, state: FSMContext):
    if cb.data == "buy_custom": return
    amount = int(cb.data.split("_")[1])
    total_ton = amount * PRICE_PER_REQ_TON
    await state.update_data(buy_amount=amount, total_ton=total_ton)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="curr_TON")],
        [InlineKeyboardButton(text="💵 USDT (TRC-20)", callback_data="curr_USDT")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]
    ])
    await cb.message.edit_text(f"Вы выбрали <b>{amount}</b> запросов.\nК оплате: <b>{total_ton:.2f} TON</b>\n\nВыберите валюту:", parse_mode="HTML", reply_markup=kb)
    await state.set_state(BuyStates.wait_currency)
    await cb.answer()

@dp.callback_query(F.data == "buy_custom")
async def cb_buy_custom(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("✏️ Введите количество запросов:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]]))
    await state.set_state(BuyStates.wait_custom_amount)
    await cb.answer()

@dp.message(BuyStates.wait_custom_amount)
async def process_custom_amount(msg: Message, state: FSMContext):
    try:
        amount = int(msg.text)
        if amount <= 0: raise ValueError
    except ValueError:
        return await msg.answer("❌ Введите целое число.")
    
    total_ton = amount * PRICE_PER_REQ_TON
    await state.update_data(buy_amount=amount, total_ton=total_ton)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 TON", callback_data="curr_TON")],
        [InlineKeyboardButton(text="💵 USDT (TRC-20)", callback_data="curr_USDT")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]
    ])
    await msg.answer(f"Вы хотите купить <b>{amount}</b> запросов.\nК оплате: <b>{total_ton:.2f} TON</b>\n\nВыберите валюту:", parse_mode="HTML", reply_markup=kb)
    await state.set_state(BuyStates.wait_currency)

@dp.callback_query(BuyStates.wait_currency, F.data.startswith("curr_"))
async def process_currency(cb: CallbackQuery, state: FSMContext):
    currency = cb.data.split("_")[1]
    data = await state.get_data()
    amount = data.get("buy_amount")
    total_ton = data.get("total_ton")
    
    if currency == "TON":
        curr_text = f"Сумма: <b>{total_ton:.2f} TON</b>"
        nano_amount = int(total_ton * 1_000_000_000)
        tonkeeper_link = f"ton://transfer/{CRYPTO_WALLET}?amount={nano_amount}"
    else:
        total_usdt = total_ton * RATE_TON_TO_USDT
        curr_text = f"Сумма: <b>~{total_usdt:.2f} USDT</b>"
        tonkeeper_link = f"ton://transfer/{CRYPTO_WALLET}?amount=0"

    text = f"💳 <b>Оплата: {amount} запросов</b>\n\nВалюта: <b>{currency}</b>\n{curr_text}\nКошелек: <code>{CRYPTO_WALLET}</code>\n\nОтправьте <b>хеш транзакции</b> сюда."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть TONKEEPER", url=tonkeeper_link)],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_to_menu")]
    ])
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
    
    if len(tx_hash) < 10:
        return await msg.answer("❌ Это не похоже на хеш. Попробуйте снова.")
        
    await msg.answer("⏳ Хеш получен. Отправлен админу на проверку.", reply_markup=main_menu())
    
    admin_text = f"💰 <b>Заявка на оплату!</b>\n\n👤 @{msg.from_user.username} (ID: <code>{msg.from_user.id}</code>)\n📦 {amount} запросов\n💵 {total_ton:.2f} TON ({currency})\n🔗 <code>{html.escape(tx_hash)}</code>"
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"appr_{msg.from_user.id}_{amount}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej_{msg.from_user.id}")]])
    
    await bot.send_message(MAIN_ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=admin_kb)

@dp.callback_query(F.data.startswith("appr_"))
async def admin_approve(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != MAIN_ADMIN_ID: return await cb.answer("Нет доступа", show_alert=True)
    _, uid, amount = cb.data.split("_")
    await add_requests(int(uid), int(amount))
    await cb.message.edit_text(f"✅ Начислено {amount} запросов для {uid}.")
    try:
        await bot.send_message(int(uid), HELP_TEXT, parse_mode="HTML", reply_markup=main_menu())
    except: pass
    await cb.answer()

@dp.callback_query(F.data.startswith("rej_"))
async def admin_reject(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != MAIN_ADMIN_ID: return await cb.answer("Нет доступа", show_alert=True)
    _, uid = cb.data.split("_")
    await cb.message.edit_text(f"❌ Отклонено для {uid}.")
    try: await bot.send_message(int(uid), "❌ Оплата отклонена.", reply_markup=main_menu())
    except: pass
    await cb.answer()

# --- АДМИНКА ---
@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id != MAIN_ADMIN_ID: return await msg.answer("⛔ Нет доступа.")
    await msg.answer("👑 <b>Админ-панель</b>\nВыберите действие:", parse_mode="HTML", reply_markup=admin_menu())

@dp.callback_query(F.data == "adm_give")
async def cb_adm_give(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID: return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Введите `ID количество` (например: `123456789 50`):", parse_mode="Markdown")
    await state.set_state(AdminStates.give_reqs)
    await cb.answer()

@dp.callback_query(F.data == "adm_take")
async def cb_adm_take(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != MAIN_ADMIN_ID: return await cb.answer("Нет доступа", show_alert=True)
    await cb.message.edit_text("Введите `ID количество` для списания:", parse_mode="Markdown")
    await state.set_state(AdminStates.take_reqs)
    await cb.answer()

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

# --- ПОИСК В БОТЕ ---
async def process_search(msg: Message, q: str):
    if not await deduct_request(msg.from_user.id):
        return await msg.answer("❌ Закончились запросы. Купите в меню.", reply_markup=main_menu())
        
    note = await msg.answer(f"🔍 <b>Начинаю поиск...</b>\nЗапрос: <code>{html.escape(q)}</code>\n\n📡 <b>Опрашиваемые базы:</b>\n├ 🔸 LEGO\n├ 🔸 DevHydra\n└ 🔸 Mobius", parse_mode="HTML")
    api_results = await query_all_apis(q)
    
    if not api_results:
        await add_requests(msg.from_user.id, 1)
        return await note.edit_text("❌ Все API вернули ошибку или не ответили.")
    
    text_report = render_text_results(api_results)
    bal = await get_balance(msg.from_user.id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Скачать HTML", callback_data=f"html_{q[:40]}")]
    ])
    
    final_text = (
        f"✅ <b>Результаты поиска:</b>\n\n"
        f"{text_report}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Остаток запросов:</b> <code>{bal}</code>"
    )
    
    try:
        await note.edit_text(final_text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
    except Exception:
        clean_text = re.sub(r'<[^>]+>', '', text_report)
        await note.edit_text(f"✅ Результаты:\n\n{clean_text[:3500]}\n\n💰 Остаток: {bal}", reply_markup=kb)

@dp.callback_query(F.data.startswith("html_"))
async def send_html_file(cb: CallbackQuery):
    q = cb.data.replace("html_", "")
    await cb.answer("Генерирую файл...")
    note = await cb.message.reply("⏳ Собираю данные для HTML...")
    api_results = await query_all_apis(q)
    
    if not api_results:
        return await note.edit_text("❌ Данные устарели, попробуйте поиск заново.")
        
    html_code = build_html_report(api_results, q)
    file = BufferedInputFile(html_code.encode('utf-8'), filename=f"listopadov_{q[:10]}.html")
    await cb.message.answer_document(document=file, caption=f"📄 HTML-отчет: <code>{html.escape(q)}</code>", parse_mode="HTML")
    await note.delete()

@dp.message(Command("search"))
async def cmd_search(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2: return await msg.answer("Укажи запрос.")
    await process_search(msg, parts[1].strip())

@dp.message()
async def auto_search(msg: Message, state: FSMContext):
    if msg.text and not msg.text.startswith("/"):
        current_state = await state.get_state()
        if current_state is not None: return
        await process_search(msg, msg.text.strip())

# --- API ДЛЯ САЙТА ---
async def api_search_handler(request):
    query = request.query.get("q")
    if not query:
        return web.json_response({"error": "Нет запроса"}, status=400)

    api_results = await query_all_apis(query)
    
    if not api_results:
        return web.json_response({"error": "Ничего не найдено или API недоступны"}, status=404)

    text_report = render_text_results(api_results)
    
    return web.json_response({
        "query": query,
        "result": text_report,
        "sources": len(api_results)
    }, headers={"Access-Control-Allow-Origin": "*"})

# --- ЗАПУСК ДЛЯ RENDER ---
async def on_startup(bot: Bot):
    await init_db()
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook установлен: {WEBHOOK_URL}")

async def health_check(request):
    return web.Response(text="OK")

def main():
    bot = Bot(BOT_TOKEN)
    dp.startup.register(on_startup)
    
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    # Добавляем API для сайта
    app.router.add_get("/api/search", api_search_handler)
    
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
