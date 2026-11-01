# crite.py — финальная версия с красивым текстом и кнопкой HTML
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
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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
    return await asyncio.to_thread(sync_fetch, API_URL_1, {"q": q, "type": q_type, "key": API_KEY_1})

async def fetch_api_2(q: str, q_type: str):
    return await asyncio.to_thread(sync_fetch, API_URL_2, {"q": q, "type": q_type, "key": API_KEY_2})

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
    tasks = [fetch_api_1(q, q_type), fetch_api_2(q, q_type), fetch_api_3(q, q_type)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    combined = []
    sources = ["LEGO", "DevHydra", "Mobius"]
    
    for i, res in enumerate(results):
        if isinstance(res, tuple) and res[0]:
            combined.append({"source": sources[i], "data": res[1]})
    return combined

# --- ОЧИСТКА ДАННЫХ ---
def clean_data(obj):
    """Рекурсивно удаляет мусорные ключи и пустые значения"""
    if isinstance(obj, dict):
        return {
            k: clean_data(v) for k, v in obj.items()
            if k not in ("id", "profile_url", "success", "query", "elapsed_seconds", "results_count", "status", "count", "total_records", "latency_ms")
            and v is not None and v != "" and v != {} and v != []
        }
    elif isinstance(obj, list):
        return [clean_data(v) for v in obj if v]
    return obj

# --- ТЕКСТОВЫЙ РЕНДЕР ---
def render_text_results(api_results: list[dict]) -> str:
    lines = []
    for res in api_results:
        source = res["source"]
        data = clean_data(res["data"])
        
        results_dict = data.get("results", {})
        if not results_dict:
            continue

        for src, src_data in results_dict.items():
            inner_results = src_data.get("result", {}).get("data", {}).get("results", {})
            if not inner_results:
                continue
                
            lines.append(f"<b>📦 {source} ({src})</b>")
            
            # Функция для вывода дерева
            def print_tree(d, prefix="├"):
                out = []
                if isinstance(d, dict):
                    for k, v in d.items():
                        key_name = k.replace("_", " ").capitalize()
                        if isinstance(v, (dict, list)):
                            out.append(f"{prefix} <b>{key_name}:</b>")
                            out.extend(print_tree(v, prefix + "─"))
                        else:
                            if isinstance(v, str) and v.startswith("http"):
                                out.append(f"{prefix} {key_name}: <a href='{v}'>ссылка</a>")
                            else:
                                out.append(f"{prefix} {key_name}: <code>{html.escape(str(v))}</code>")
                elif isinstance(d, list):
                    for item in d:
                        if isinstance(item, (dict, list)):
                            out.extend(print_tree(item, prefix + "─"))
                        else:
                            out.append(f"{prefix} <code>{html.escape(str(item))}</code>")
                return out

            if isinstance(inner_results, dict):
                for srv_name, srv_data in inner_results.items():
                    lines.append(f"└─ <b>{srv_name.upper()}</b>")
                    lines.extend(print_tree(srv_data, "   ├"))
            lines.append("")
            
    res_str = "\n".join(lines)
    if not res_str:
        return "🔍 Данных не найдено."
    if len(res_str) > 3800:
        res_str = res_str[:3800] + "\n… [ответ обрезан]"
    return res_str

# --- HTML РЕНДЕР ---
def build_html_report(api_results: list[dict], query: str) -> str:
    content = ""
    for res in api_results:
        source = res["source"]
        data = clean_data(res["data"])
        
        content += f'<div class="source-header">📦 {html.escape(source)}</div>'
        
        results_dict = data.get("results", {})
        if not results_dict:
            content += '<div class="card"><span class="text-muted">Нет данных</span></div>'
            continue
            
        def render_html_block(d):
            out = ""
            if isinstance(d, dict):
                for k, v in d.items():
                    key_name = k.replace("_", " ").capitalize()
                    if isinstance(v, (dict, list)):
                        out += f'<div class="nested-title">{key_name}</div>'
                        out += f'<div class="nested-card">{render_html_block(v)}</div>'
                    else:
                        val_html = f'<a href="{v}" target="_blank">{html.escape(v)}</a>' if isinstance(v, str) and v.startswith("http") else html.escape(str(v))
                        out += f'<div class="row"><div class="key">{key_name}</div><div class="val">{val_html}</div></div>'
            elif isinstance(d, list):
                for item in d:
                    out += render_html_block(item)
            return out

        for src, src_data in results_dict.items():
            inner_results = src_data.get("result", {}).get("data", {}).get("results", {})
            if not inner_results:
                continue
                
            content += f'<div class="card"><div class="card-title">{html.escape(src)}</div>'
            if isinstance(inner_results, dict):
                for srv_name, srv_data in inner_results.items():
                    content += f'<div class="service-name">📌 {srv_name.upper()}</div>'
                    content += render_html_block(srv_data)
            elif isinstance(inner_results, list):
                for item in inner_results:
                    content += render_html_block(item)
            content += '</div>'

    return f"""<!DOCTYPE html>
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
            --green: #3fb950;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 20px;
            line-height: 1.6;
        }}
        .container {{ max-width: 800px; margin: 0 auto; }}
        .header {{ text-align: center; margin-bottom: 30px; padding-bottom: 20px; border-bottom: 1px solid var(--border); }}
        h1 {{ color: var(--accent); margin: 0 0 10px 0; font-size: 28px; }}
        .query {{ color: var(--text-muted); font-size: 16px; }}
        .query b {{ color: var(--text); background: var(--card-bg); padding: 2px 8px; border-radius: 4px; border: 1px solid var(--border); }}
        .source-header {{ background: linear-gradient(90deg, var(--card-bg) 0%, var(--bg) 100%); border-left: 4px solid var(--green); padding: 12px 15px; font-size: 18px; font-weight: bold; margin: 30px 0 15px 0; border-radius: 0 6px 6px 0; }}
        .card {{ background-color: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
        .card-title {{ color: #d2a8ff; font-size: 18px; margin-bottom: 15px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }}
        .service-name {{ color: var(--accent); font-weight: bold; margin: 15px 0 10px 0; font-size: 15px; }}
        .row {{ display: flex; border-bottom: 1px dashed #21262d; padding: 8px 0; }}
        .row:last-child {{ border-bottom: none; }}
        .key {{ width: 40%; color: var(--text-muted); font-size: 14px; font-weight: 500; }}
        .val {{ width: 60%; color: var(--text); font-size: 14px; word-break: break-word; }}
        .nested-title {{ color: var(--text-muted); font-size: 13px; text-transform: uppercase; margin: 15px 0 5px 0; }}
        .nested-card {{ background: #0d1117; border: 1px solid var(--border); border-radius: 6px; padding: 10px 15px; margin-bottom: 10px; }}
        a {{ color: var(--accent); text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .text-muted {{ color: var(--text-muted); font-style: italic; }}
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

# --- ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer(WELCOME_TEXT, parse_mode="HTML")

async def process_search(msg: Message, q: str):
    note = await msg.answer(f"🔍 Ищу во всех базах <code>{html.escape(q)}</code>…", parse_mode="HTML")
    
    api_results = await query_all_apis(q)
    
    if not api_results:
        return await note.edit_text("❌ Все API вернули ошибку или ничего не нашли.")
    
    # 1. Отправляем красивый текст
    text_report = render_text_results(api_results)
    
    # 2. Создаем инлайн-кнопку для скачивания HTML
    # В callback_data передаем запрос, чтобы потом сгенерировать файл
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Скачать HTML отчет", callback_data=f"html_{q[:40]}")]
    ])
    
    await note.edit_text(
        f"✅ <b>Результаты поиска:</b>\n\n{text_report}",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("html_"))
async def send_html_file(cb: CallbackQuery):
    q = cb.data.replace("html_", "")
    await cb.answer("Генерирую файл...")
    
    # Повторно запрашиваем данные для HTML (или можно было сохранить в FSM, но для простоты запросим снова)
    note = await cb.message.reply("⏳ Собираю данные для HTML...")
    api_results = await query_all_apis(q)
    
    if not api_results:
        await note.edit_text("❌ Данные устарели, попробуйте поиск заново.")
        return
        
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
