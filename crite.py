# crite.py — финальная версия с умным парсингом и красивым HTML
import asyncio
import json
import html
import os
import re
import logging
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# --- НАСТРОЙКИ API ---
API_URL_1 = "https://api.lego.new.tg/v1/search"
API_KEY_1 = "LEGO-LORDKEY-p2TA1DqD98RUHGibdm"
API_URL_2 = "https://api.devhydra.info/v1/search"
API_KEY_2 = "HYDRA-LordKEY-2eWKbyuQUJz6XQK5Pv"
API_URL_3 = "https://mobiusnet.cc.cd/api/v1/search"
API_KEY_3 = "MOBIUS-IO-IW5w3b4xKEY_dS8Kbt4FJswFEsTR97aGKhSEgfrJFjk"

BOT_TOKEN = "9042470492:AAGrM2pNdx6XaZqETRSZQh-G90VbMh23Jqo"
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

dp = Dispatcher()

WELCOME_TEXT = """🕵️ <b>LISTOPADOV SEARCH</b>
<i>OSINT-бот для поиска информации</i>

ℹ️ <b>Примеры:</b>
📲 <code>79991234567</code>
📧 <code>test@mail.ru</code>
📟 <code>@username</code>
🚘 <code>А123БВ777</code>

Просто отправь запрос в чат."""

def detect_type(query: str) -> str:
    q = query.strip()
    if re.match(r"^\+?\d{10,15}$", q): return "phone"
    if "@" in q and "." in q.split("@")[-1]: return "email"
    if q.startswith("@") or q.startswith("t.me/") or q.startswith("https://t.me/"): return "telegram"
    if re.match(r"^[А-ЯA-Z]\d{3}[А-ЯA-Z]{2}\d{2,3}$", q.upper()): return "auto"
    return "name"

async def fetch_api(url: str, params: dict, headers: dict = None) -> tuple[bool, dict | str]:
    timeout = aiohttp.ClientTimeout(total=45)
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
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=45)
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

# --- ТЕКСТОВЫЙ РЕНДЕР (Только важное) ---
def render_text_results(api_results: list[dict]) -> str:
    lines = []
    for res in api_results:
        data = clean_data(res["data"])
        results_dict = data.get("results", {})
        if not results_dict: continue

        for src, src_data in results_dict.items():
            inner_results = src_data.get("result", {}).get("data", {}).get("results", {})
            if not inner_results: continue
            
            # Ищем ключевые поля по всем доступным местам
            def find_val(keys):
                for k in keys:
                    if k in inner_results: return inner_results[k]
                return None

            # ФИО
            names = find_val(["NAMES", "names", "name"])
            if names:
                n_str = ", ".join([str(n) for n in names]) if isinstance(names, list) else str(names)
                lines.append(f"👤 <b>ФИО:</b> <code>{html.escape(n_str)}</code>")
            
            # Адреса
            addrs = find_val(["ADDRESSES", "addresses", "address"])
            if addrs:
                lines.append(f"🏠 <b>Адреса:</b>")
                for a in (addrs if isinstance(addrs, list) else [addrs]):
                    lines.append(f"  └ {html.escape(str(a))}")

            # Соцсети / Telegram
            tg = find_val(["TELEGRAM", "telegram"])
            if tg and isinstance(tg, dict):
                lines.append(f"📟 <b>Telegram:</b>")
                for k, v in tg.items():
                    lines.append(f"  ├ {k.capitalize()}: <code>{html.escape(str(v))}</code>")

            # Emails
            emails = find_val(["EMAILS", "emails", "email"])
            if emails:
                e_str = ", ".join([str(e) for e in emails]) if isinstance(emails, list) else str(emails)
                lines.append(f"📧 <b>Email:</b> <code>{html.escape(e_str)}</code>")
                
            # Остальное (Базы, Авто)
            dbs = find_val(["databases", "DATABASES", "breaches", "BREACHES"])
            if dbs and isinstance(dbs, list):
                lines.append(f"🗂 <b>Базы/Утечки:</b> Найдено {len(dbs)} записей")

            # Телефон инфо
            ph = find_val(["PHONE_INFO", "phone"])
            if ph and isinstance(ph, dict):
                lines.append(f"📞 <b>Телефон:</b> {html.escape(str(ph.get('phone', '')))} ({html.escape(str(ph.get('operator', '')))})")

    res_str = "\n".join(lines)
    if not res_str: return "🔍 Данных не найдено."
    if len(res_str) > 3800: res_str = res_str[:3800] + "\n… [обрезано]"
    return res_str

# --- КРАСИВЫЙ HTML ---
def build_html_report(api_results: list[dict], query: str) -> str:
    content = ""
    for res in api_results:
        source = res["source"]
        data = clean_data(res["data"])
        
        results_dict = data.get("results", {})
        if not results_dict: continue

        for src, src_data in results_dict.items():
            inner_results = src_data.get("result", {}).get("data", {}).get("results", {})
            if not inner_results: continue

            content += f'<div class="card"><div class="card-title">📦 {html.escape(source)} ({html.escape(src)})</div>'

            # Функция для красивого вывода
            def render_block(val, title):
                out = f'<div class="section"><div class="section-title">{html.escape(title)}</div><div class="section-content">'
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict):
                            for k, v in item.items():
                                out += f'<div class="grid-item"><span class="grid-key">{k.capitalize()}</span><span class="grid-val">{html.escape(str(v))}</span></div>'
                        else:
                            out += f'<div class="list-item">• {html.escape(str(item))}</div>'
                elif isinstance(val, dict):
                    for k, v in val.items():
                        out += f'<div class="grid-item"><span class="grid-key">{k.capitalize()}</span><span class="grid-val">{html.escape(str(v))}</span></div>'
                else:
                    out += f'<div class="list-item">{html.escape(str(val))}</div>'
                out += '</div></div>'
                return out

            # Выводим то, что есть
            for k, v in inner_results.items():
                # Пропускаем пустые и системные
                if not v or k in ("id", "profile_url"): continue
                if k.upper() in ("PHONE_INFO", "PHONE", "TELEGRAM", "NAMES", "NAME", "ADDRESSES", "EMAILS", "DATABASES", "BREACHES", "VEHICLES"):
                    content += render_block(v, k.upper())
                else:
                    # Всё остальное (если есть)
                    if isinstance(v, (dict, list)):
                        content += render_block(v, k.upper())

            content += '</div>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OASIS REPORT</title>
    <style>
        :root {{
            --bg: #0d1117;
            --card-bg: #161b22;
            --border: #30363d;
            --text: #c9d1d9;
            --text-muted: #8b949e;
            --accent: #58a6ff;
            --green: #3fb950;
            --purple: #bc8cff;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 20px;
            line-height: 1.6;
        }}
        .container {{ max-width: 800px; margin: 0 auto; }}
        .header {{ text-align: center; margin-bottom: 30px; padding-bottom: 20px; border-bottom: 1px solid var(--border); }}
        h1 {{ color: var(--accent); margin: 0 0 10px 0; font-size: 28px; }}
        .query {{ color: var(--text-muted); }}
        .query b {{ color: var(--green); background: rgba(63, 185, 80, 0.1); padding: 4px 10px; border-radius: 6px; border: 1px solid rgba(63, 185, 80, 0.3); }}
        
        .card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 4px 10px rgba(0,0,0,0.2); }}
        .card-title {{ color: var(--purple); font-size: 16px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 10px; font-weight: 600; }}
        
        .section {{ margin-bottom: 20px; }}
        .section-title {{ color: var(--accent); font-size: 14px; font-weight: 600; margin-bottom: 10px; text-transform: uppercase; }}
        .section-content {{ background: rgba(0,0,0,0.2); border: 1px solid var(--border); border-radius: 8px; padding: 15px; }}
        
        .grid-item {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed rgba(255,255,255,0.05); }}
        .grid-item:last-child {{ border-bottom: none; }}
        .grid-key {{ color: var(--text-muted); font-size: 14px; }}
        .grid-val {{ color: var(--text); font-size: 14px; text-align: right; font-weight: 500; word-break: break-word; max-width: 60%; }}
        
        .list-item {{ padding: 4px 0; color: var(--text); font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🕵️ OASIS SEARCH</h1>
            <div class="query">Отчет по запросу: <b>{html.escape(query)}</b></div>
        </div>
        {content}
    </div>
</body>
</html>"""

# --- ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer(WELCOME_TEXT, parse_mode="HTML")

async def process_search(msg: Message, q: str):
    note = await msg.answer(f"🔍 Ищу во всех базах <code>{html.escape(q)}</code>…", parse_mode="HTML")
    
    api_results = await query_all_apis(q)
    
    if not api_results:
        return await note.edit_text("❌ Все API вернули ошибку или не ответили.")
    
    text_report = render_text_results(api_results)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 HTML", callback_data=f"html_{q[:40]}")]
    ])
    
    try:
        await note.edit_text(
            f"✅ <b>Результаты поиска:</b>\n\n{text_report}",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb
        )
    except Exception as e:
        # Если текст слишком длинный, отправляем без HTML
        await note.edit_text(f"✅ Результаты:\n\n{text_report[:3500]}", reply_markup=kb)

@dp.callback_query(F.data.startswith("html_"))
async def send_html_file(cb: CallbackQuery):
    q = cb.data.replace("html_", "")
    await cb.answer("Генерирую файл...")
    
    note = await cb.message.reply("⏳ Собираю данные для HTML...")
    api_results = await query_all_apis(q)
    
    if not api_results:
        return await note.edit_text("❌ Данные устарели, попробуйте поиск заново.")
        
    html_code = build_html_report(api_results, q)
    file = BufferedInputFile(html_code.encode('utf-8'), filename=f"oasis_{q[:10]}.html")
    
    await cb.message.answer_document(document=file, caption=f"📄 HTML-отчет по запросу: <code>{html.escape(q)}</code>", parse_mode="HTML")
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

# --- ЗАПУСК RENDER ---
async def on_startup(bot: Bot):
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Webhook: {WEBHOOK_URL}")

async def health_check(request):
    return web.Response(text="OK")

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
