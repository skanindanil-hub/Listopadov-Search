# crite.py — версия с таймаутом 45 сек и защитой от падения HTML
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
        logging.error("Общий таймаут запросов к API (45 сек)")
        return []
    
    combined = []
    sources = ["LEGO", "DevHydra", "Mobius"]
    
    for i, res in enumerate(results):
        if isinstance(res, tuple) and res[0]:
            combined.append({"source": sources[i], "data": res[1]})
        elif isinstance(res, tuple):
            logging.warning(f"{sources[i]} failed: {res[1]}")
            
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
        if not results_dict: continue

        for src, src_data in results_dict.items():
            inner_results = src_data.get("result", {}).get("data", {}).get("results", {})
            if not inner_results: continue
            
            lines.append(f"<b>📦 {source} ({src})</b>")
            
            def print_tree(d, prefix="├"):
                out = []
                if isinstance(d, dict):
                    for k, v in d.items():
                        if isinstance(v, (dict, list)):
                            out.append(f"{prefix} <b>{k.capitalize()}:</b>")
                            out.extend(print_tree(v, prefix + "─"))
                        else:
                            if isinstance(v, str) and v.startswith("http"):
                                out.append(f"{prefix} {k.capitalize()}: <a href='{v}'>ссылка</a>")
                            else:
                                out.append(f"{prefix} {k.capitalize()}: <code>{html.escape(str(v))}</code>")
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
    if not res_str: return "🔍 Данных не найдено."
    if len(res_str) > 3800: res_str = res_str[:3800] + "\n… [ответ обрезан]"
    return res_str

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
                out += '<div class="grid">'
                for k, v in d.items():
                    if isinstance(v, (dict, list)):
                        out += f'<div class="grid-title">{html.escape(k.capitalize())}</div>'
                        out += f'<div class="grid-content">{render_html_block(v)}</div>'
                    else:
                        val_html = f'<a href="{v}" target="_blank">{html.escape(v)}</a>' if isinstance(v, str) and v.startswith("http") else html.escape(str(v))
                        out += f'<div class="grid-item"><span class="grid-key">{k.capitalize()}</span><span class="grid-val">{val_html}</span></div>'
                out += '</div>'
            elif isinstance(d, list):
                for item in d:
                    out += render_html_block(item)
            return out

        for src, src_data in results_dict.items():
            inner_results = src_data.get("result", {}).get("data", {}).get("results", {})
            if not inner_results: continue
                
            content += f'<div class="card"><div class="card-title">{html.escape(src)}</div>'
            if isinstance(inner_results, dict):
                for srv_name, srv_data in inner_results.items():
                    content += f'<div class="service-badge">{srv_name.upper()}</div>'
                    content += render_html_block(srv_data)
            elif isinstance(inner_results, list):
                for item in inner_results:
                    content += render_html_block(item)
            content += '</div>'

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>OASIS</title>
    <style>
        body {{ background: #0b0f19; color: #e2e8f0; font-family: sans-serif; padding: 20px; }}
        .container {{ max-width: 800px; margin: 0 auto; }}
        .source-header {{ background: rgba(34,197,94,0.1); color: #22c55e; padding: 12px 20px; font-weight: bold; margin: 30px 0 15px; border-radius: 8px; border: 1px solid rgba(34,197,94,0.3); }}
        .card {{ background: #151b26; border: 1px solid #2a3441; border-radius: 12px; padding: 24px; margin-bottom: 20px; }}
        .grid-item {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px dashed #2a3441; }}
        .grid-key {{ color: #94a3b8; }} .grid-val {{ color: #e2e8f0; text-align: right; }}
        .service-badge {{ background: rgba(56,189,248,0.1); color: #38bdf8; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; display: inline-block; margin: 10px 0; }}
        .grid-title {{ color: #38bdf8; font-size: 15px; margin: 15px 0 5px; border-bottom: 1px solid #2a3441; }}
        a {{ color: #38bdf8; text-decoration: none; }}
    </style></head><body><div class="container">
    <h1>🕵️ OASIS SEARCH</h1>
    <div style="text-align:center; color:#94a3b8; margin-bottom:30px;">Запрос: <b>{html.escape(query)}</b></div>
    {content}</div></body></html>"""

# --- ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer(WELCOME_TEXT, parse_mode="HTML")

async def process_search(msg: Message, q: str):
    note = await msg.answer(f"🔍 Ищу во всех базах <code>{html.escape(q)}</code>…", parse_mode="HTML")
    
    api_results = await query_all_apis(q)
    
    if not api_results:
        return await note.edit_text("❌ Все API вернули ошибку или не ответили (таймаут 45 сек).")
    
    text_report = render_text_results(api_results)
    
    # Очищаем от HTML-тегов, чтобы Телеграм не падал
    safe_report = html.escape(text_report)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Открыть красивый HTML", callback_data=f"html_{q[:40]}")]
    ])
    
    try:
        await note.edit_text(
            f"✅ <b>Результаты поиска:</b>\n\n{safe_report}",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb
        )
    except Exception as e:
        logging.error(f"Ошибка отправки: {e}")
        # Если Телеграм снова ругнется на разметку, отправим текст без HTML
        await note.edit_text(f"✅ Результаты:\n\n{text_report[:3500]}", reply_markup=kb)

@dp.callback_query(F.data.startswith("html_"))
async def send_html_file(cb: CallbackQuery):
    q = cb.data.replace("html_", "")
    await cb.answer("Генерирую файл...")
    
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
