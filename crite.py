# crite.py — мульти-API бот с красивым HTML отчетом
import asyncio
import json
import html
import os
import re
import logging
import aiohttp
import cloudscraper
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# --- НАСТРОЙКИ API ---
API_URL_1 = "https://api.lego.new.tg/v1/search"
API_KEY_1 = "LEGO-LORDKEY-p2TA1DqD98RUHGibdm"

API_URL_2 = "https://api.devhydra.info/v1/search"
API_KEY_2 = "HYDRA-LordKEY-2eWKbyuQUJz6XQK5Pv"

API_URL_3 = "https://mobiusnet.cc.cd/api/v1/search"
API_KEY_3 = "MOBIUS-IO-IW5w3b4xKEY_dS8Kbt4FJswFEsTR97aGKhSEgfrJFjk"

# --- ОБЩИЕ НАСТРОЙКИ ---
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

def sync_fetch(url: str, params: dict) -> tuple[bool, dict | str]:
    try:
        scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows'})
        resp = scraper.get(url, params=params, timeout=20)
        if resp.status_code == 200 and "Just a moment" not in resp.text:
            return True, resp.json()
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, str(e)

async def fetch_api_1(q: str, q_type: str):
    params = {"q": q, "type": q_type, "key": API_KEY_1}
    return await asyncio.to_thread(sync_fetch, API_URL_1, params)

async def fetch_api_2(q: str, q_type: str):
    params = {"q": q, "type": q_type, "key": API_KEY_2}
    return await asyncio.to_thread(sync_fetch, API_URL_2, params)

async def fetch_api_3(q: str, q_type: str):
    try:
        headers = {"X-API-Key": API_KEY_3, "Accept": "application/json"}
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(API_URL_3, headers=headers, params={"q": q}, ssl=False) as resp:
                if resp.status == 200:
                    return True, await resp.json()
        return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, str(e)

async def query_all_apis(q: str) -> list[dict]:
    q_type = detect_type(q)
    # Запускаем все три запроса ОДНОВРЕМЕННО
    tasks = [
        fetch_api_1(q, q_type),
        fetch_api_2(q, q_type),
        fetch_api_3(q, q_type)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    combined = []
    sources = ["LEGO API", "DevHydra API", "Mobius API"]
    
    for i, res in enumerate(results):
        if isinstance(res, tuple) and res[0]:
            combined.append({"source": sources[i], "data": res[1]})
        elif isinstance(res, tuple):
            logging.warning(f"{sources[i]} failed: {res[1]}")
        else:
            logging.error(f"{sources[i]} exception: {res}")
            
    return combined

def format_value(v):
    if isinstance(v, str):
        if v.startswith("http"):
            return f'<a href="{html.escape(v)}" target="_blank">{html.escape(v)}</a>'
        return html.escape(v)
    if isinstance(v, bool):
        return "Да" if v else "Нет"
    return html.escape(str(v))

def render_data_block(data) -> str:
    html_out = ""
    if isinstance(data, dict):
        # Сначала выводим простые поля
        for k, v in data.items():
            if not isinstance(v, (dict, list)) and k not in ("id", "profile_url"):
                key_name = k.replace("_", " ").capitalize()
                html_out += f'<div class="row"><div class="key">{html.escape(key_name)}</div><div class="val">{format_value(v)}</div></div>'
        
        # Потом вложенные списки и словари
        for k, v in data.items():
            if isinstance(v, list) and len(v) > 0:
                html_out += f'<div class="section-title">{k.replace("_", " ").capitalize()}</div>'
                for item in v:
                    html_out += '<div class="nested-card">'
                    html_out += render_data_block(item)
                    html_out += '</div>'
            elif isinstance(v, dict) and len(v) > 0:
                html_out += f'<div class="section-title">{k.replace("_", " ").capitalize()}</div>'
                html_out += '<div class="nested-card">'
                html_out += render_data_block(v)
                html_out += '</div>'
    elif isinstance(data, list):
        for item in data:
            html_out += render_data_block(item)
            
    return html_out

def build_html_report(api_results: list[dict], query: str) -> tuple[str, int]:
    content = ""
    total_records = 0
    
    if not api_results:
        content = '<div class="error">❌ Данных не найдено ни в одном из API.</div>'
    else:
        for res in api_results:
            source_name = res["source"]
            data = res["data"]
            
            content += f'<div class="source-header">📦 {html.escape(source_name)}</div>'
            
            # Пытаемся найти стандартную структуру ответа (results -> result -> data -> results)
            results_dict = data.get("results", {}) if isinstance(data, dict) else {}
            
            if not results_dict:
                content += '<div class="card">'
                content += render_data_block(data)
                content += '</div>'
                total_records += 1
                continue
                
            for src, src_data in results_dict.items():
                inner_results = src_data.get("result", {}).get("data", {}).get("results", {}) if isinstance(src_data, dict) else {}
                if not inner_results:
                    continue
                
                content += f'<div class="card"><div class="card-title">{html.escape(str(src))}</div>'
                if isinstance(inner_results, dict):
                    for srv_name, srv_data in inner_results.items():
                        if isinstance(srv_data, dict):
                            content += f'<div class="service-name">📌 {srv_name.upper()}</div>'
                            content += render_data_block(srv_data)
                            total_records += 1
                elif isinstance(inner_results, list):
                    for item in inner_results:
                        content += render_data_block(item)
                        total_records += 1
                content += '</div>'

    html_doc = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OASIS REPORT: {html.escape(query)}</title>
    <style>
        :root {{
            --bg: #0d1117;
            --card-bg: #161b22;
            --border: #30363d;
            --text: #c9d1d9;
            --text-muted: #8b949e;
            --accent: #58a6ff;
            --accent-hover: #79c0ff;
            --purple: #d2a8ff;
            --green: #3fb950;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 20px;
            line-height: 1.5;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
        }}
        .header {{
            text-align: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
        }}
        h1 {{
            color: var(--accent);
            margin: 0 0 10px 0;
            font-size: 28px;
        }}
        .query {{
            color: var(--text-muted);
            font-size: 16px;
        }}
        .query b {{
            color: var(--text);
            background: var(--card-bg);
            padding: 2px 8px;
            border-radius: 4px;
            border: 1px solid var(--border);
        }}
        .source-header {{
            background: linear-gradient(90deg, var(--card-bg) 0%, var(--bg) 100%);
            border-left: 4px solid var(--green);
            padding: 12px 15px;
            font-size: 18px;
            font-weight: bold;
            margin: 30px 0 15px 0;
            border-radius: 0 6px 6px 0;
        }}
        .card {{
            background-color: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3);
        }}
        .card-title {{
            color: var(--purple);
            font-size: 18px;
            margin-bottom: 15px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 10px;
        }}
        .service-name {{
            color: var(--accent);
            font-weight: bold;
            margin: 15px 0 10px 0;
            font-size: 15px;
        }}
        .row {{
            display: flex;
            border-bottom: 1px dashed #21262d;
            padding: 8px 0;
        }}
        .row:last-child {{
            border-bottom: none;
        }}
        .key {{
            width: 40%;
            color: var(--text-muted);
            font-size: 14px;
            font-weight: 500;
        }}
        .val {{
            width: 60%;
            color: var(--text);
            font-size: 14px;
            word-break: break-word;
        }}
        .section-title {{
            color: var(--text-muted);
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin: 20px 0 10px 0;
        }}
        .nested-card {{
            background: #0d1117;
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 10px 15px;
            margin-bottom: 10px;
        }}
        a {{
            color: var(--accent);
            text-decoration: none;
        }}
        a:hover {{
            color: var(--accent-hover);
            text-decoration: underline;
        }}
        .error {{
            background: rgba(248, 81, 73, 0.1);
            border: 1px solid #f85149;
            color: #f85149;
            padding: 15px;
            border-radius: 6px;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🕵️ OASIS SEARCH</h1>
            <div class="query">Результат по запросу: <b>{html.escape(query)}</b></div>
        </div>
        {content}
    </div>
</body>
</html>"""
    
    return html_doc, total_records

@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer(WELCOME_TEXT, parse_mode="HTML")

async def process_search(msg: Message, q: str):
    note = await msg.answer(f"🔍 Ищу во всех базах <code>{html.escape(q)}</code>…", parse_mode="HTML")
    
    api_results = await query_all_apis(q)
    
    if not api_results:
        return await note.edit_text("❌ Все API вернули ошибку или ничего не нашли.")
    
    html_code, count = build_html_report(api_results, q)
    file = BufferedInputFile(html_code.encode('utf-8'), filename=f"oasis_{q[:10]}.html")
    
    await msg.answer_document(
        document=file, 
        caption=f"✅ Отчет: <code>{html.escape(q)}</code>\n📊 Найдено записей: <b>{count}</b>\n📡 Источников ответило: <b>{len(api_results)}/3</b>", 
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
