"""
Сервер для мини-приложения «Колесо фортуны».
Работает вместе с ботом (aiogram 3) и отдаёт API для index.html.

Установка:  pip install "aiogram>=3.13" aiohttp
Запуск:     BOT_TOKEN=123:ABC WEBAPP_ORIGIN=https://твой-сайт.com python bot.py
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import time
import uuid
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
    Message, PreCheckoutQuery, WebAppInfo,
)
from aiohttp import web

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")  # адрес сайта (на Render подставляется сам)
WEBAPP_ORIGIN = os.environ.get("WEBAPP_ORIGIN", "*")   # откуда разрешены запросы (домен страницы)
PORT = int(os.environ.get("PORT", "8080"))

SPIN_PRICE = 10  # должно совпадать с SPIN_PRICE в index.html

# Порядок и веса секторов ДОЛЖНЫ совпадать с PRIZES в index.html
# (stars — стоимость подарка, emojis — какие подарки Telegram подходят)
PRIZES = [
    {"name": "Мишка",   "stars": 15, "emojis": ["🧸"],       "w": 5},
    {"name": "Пусто",   "stars": 0,  "emojis": [],          "w": 44},
    {"name": "Сердце",  "stars": 15, "emojis": ["💝", "❤️"], "w": 5},
    {"name": "Пусто",   "stars": 0,  "emojis": [],          "w": 44},
    {"name": "Подарок", "stars": 25, "emojis": ["🎁"],       "w": 2},
]

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# Хранилище в памяти. Для продакшена лучше заменить на SQLite/Redis.
spins: dict[str, dict] = {}      # payload -> {user_id, paid, index, event}
links: dict[str, str] = {}       # ссылка на счёт -> payload


# ---------- проверка, что запрос действительно из Telegram ----------
def validate_init_data(init_data: str, max_age: int = 24 * 3600) -> int | None:
    try:
        data = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        if time.time() - int(data["auth_date"]) > max_age:
            return None
        return int(json.loads(data["user"])["id"])
    except Exception:
        return None


def roll() -> int:
    return random.choices(range(len(PRIZES)), weights=[p["w"] for p in PRIZES])[0]


async def send_prize(user_id: int, prize: dict):
    """Отправляет пользователю настоящий подарок Telegram за звёзды бота."""
    gifts = (await bot.get_available_gifts()).gifts
    for g in gifts:
        if g.star_count == prize["stars"] and g.sticker.emoji in prize["emojis"]:
            await bot.send_gift(user_id=user_id, gift_id=g.id,
                                text=f"Выигрыш в колесе фортуны: {prize['name']}!")
            return
    logging.error("Не найден подарок для %s (%s звёзд)", prize["name"], prize["stars"])
    await bot.send_message(user_id, f"Ты выиграл: {prize['name']}! Свяжись с поддержкой, чтобы получить приз.")


# ---------- бот ----------
@dp.message(CommandStart())
async def start(m: Message):
    if not WEBAPP_URL:
        return await m.answer("Задай WEBAPP_URL, чтобы появилась кнопка.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎡 Крутить колесо", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await m.answer("Крути колесо и выигрывай подарки!", reply_markup=kb)


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    spin = spins.get(q.invoice_payload)
    await q.answer(ok=bool(spin and not spin["paid"] and spin["user_id"] == q.from_user.id),
                   error_message="Счёт недействителен, попробуй ещё раз")


@dp.message(F.successful_payment)
async def paid(m: Message):
    payload = m.successful_payment.invoice_payload
    spin = spins.get(payload)
    if not spin or spin["paid"]:
        return
    spin["paid"] = True
    spin["index"] = roll()                      # приз определяется ТОЛЬКО здесь, на сервере
    spin["event"].set()
    prize = PRIZES[spin["index"]]
    if prize["stars"] > 0:
        # даём анимации докрутиться, потом отправляем подарок
        await asyncio.sleep(6)
        try:
            await send_prize(spin["user_id"], prize)
        except Exception:
            logging.exception("Не удалось отправить подарок")
            await bot.send_message(spin["user_id"], f"Ты выиграл: {prize['name']}! Подарок отправим вручную.")


# ---------- API для мини-приложения ----------
@web.middleware
async def cors(request: web.Request, handler):
    resp = web.Response() if request.method == "OPTIONS" else await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = WEBAPP_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


async def api_spin(request: web.Request):
    body = await request.json()
    user_id = validate_init_data(body.get("initData", ""))
    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    payload = uuid.uuid4().hex
    link = await bot.create_invoice_link(
        title="Вращение колеса",
        description="Одно вращение колеса фортуны",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label="Вращение", amount=SPIN_PRICE)],
    )
    spins[payload] = {"user_id": user_id, "paid": False, "index": None, "event": asyncio.Event()}
    links[link] = payload
    return web.json_response({"invoice": link})


async def api_result(request: web.Request):
    body = await request.json()
    user_id = validate_init_data(body.get("initData", ""))
    payload = links.get(body.get("invoice", ""))
    spin = spins.get(payload) if payload else None
    if not user_id or not spin or spin["user_id"] != user_id:
        return web.json_response({"error": "not found"}, status=404)
    try:
        await asyncio.wait_for(spin["event"].wait(), timeout=15)   # ждём подтверждения оплаты от Telegram
    except asyncio.TimeoutError:
        return web.json_response({"error": "payment not confirmed"}, status=402)
    return web.json_response({"index": spin["index"]})


async def index(request: web.Request):
    return web.FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))


async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application(middlewares=[cors])
    app.router.add_get("/", index)                 # сервер сам отдаёт страницу приложения
    app.router.add_route("*", "/spin", lambda r: api_spin(r) if r.method == "POST" else web.Response())
    app.router.add_route("*", "/result", lambda r: api_result(r) if r.method == "POST" else web.Response())
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
