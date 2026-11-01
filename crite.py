# crite.py — чистый бот для Render
import asyncio
import json
import html
import os
import logging
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# --- НАСТРОЙКИ ---
BOT_TOKEN = "9042470492:AAGrM2pNdx6XaZqETRSZQh-G90VbMh23Jqo"
API_URL = "https://mobiusnet.cc.cd/api/v1/search"
API_KEY = "MOBIUS-IO-IW5w3b4xKEY_dS8Kbt4FJswFEsTR97aGKhSEgfrJFjk"

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

async def query_api(q: str) -> tuple[bool, dict | str]:
    headers = {"X-API-Key": API_KEY, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=25)
    
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL, headers=headers, params={"q": q}, ssl=False) as resp:
                raw = await resp.text()
                if resp.status != 200: 
                    return False, f"HTTP {resp.status}: {raw[:200]}"
                try: 
                    return True, json.loads(raw)
                except: 
                    return False, f"Не JSON: {raw[:200]}"
    except asyncio.TimeoutError:
        return False, "Таймаут (25 сек)."
    except Exception as e:
        return False, f"Ошибка: {type(e).__name__}"

def build_html_report(data: dict, query: str) -> tuple[str, int]:
    content = ""
    results = data.get("results", {})
    records_count = 0
    
    if not isinstance(results, dict):
        results = {}

    for source_name, source_data in results.items():
        content += f'<div class="source-header">📦 {html.escape(str(source_name).upper())}</div>'
        
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

@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer(WELCOME_TEXT, parse_mode="HTML")

async def process_search(msg: Message, q: str):
    note = await msg.answer(f"🔍 Ищу <code>{html.escape(q)}</code>…", parse_mode="HTML")
    ok, result = await query_api(q)
    
    if not ok:
        return await note.edit_text(f"❌ Ошибка: {html.escape(result)}")
    
    html_code, count = build_html_report(result, q)
    file = BufferedInputFile(html_code.encode('utf-8'), filename=f"oasis_{q[:10]}.html")
    
    await msg.answer_document(
        document=file, 
        caption=f"✅ Отчет: <code>{html.escape(q)}</code>\n📊 Найдено записей: <b>{count}</b>", 
        parse_mode="HTML"
    )
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
