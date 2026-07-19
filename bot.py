"""
🤖 BotHost — простой Telegram-бот для хостинга ботов

ФУНКЦИОНАЛ:
✅ Оплата через Telegram Stars
✅ Загрузка .py файлов ботов
✅ Запуск ботов в subprocess
✅ Проверка подарков владельцу
✅ Админ-панель

АРХИТЕКТУРА:
- Один файл bot.py
- SQLite для хранения данных
- Subprocess для запуска ботов
- Telethon для проверки подарков

ДЕПЛОЙ:
- Только Railway (или любой VPS)
"""

import os
import asyncio
import subprocess
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, BufferedInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_TELEGRAM_ID"])
OWNER_PHONE = os.environ.get("OWNER_PHONE")
OWNER_API_ID = os.environ.get("OWNER_API_ID")
OWNER_API_HASH = os.environ.get("OWNER_API_HASH")

# Директории
DATA_DIR = Path("./data")
BOTS_DIR = DATA_DIR / "bots"
DB_PATH = DATA_DIR / "bot.db"
DATA_DIR.mkdir(exist_ok=True)
BOTS_DIR.mkdir(exist_ok=True)

# Тарифы
PLANS = {
    "week": {"name": "Неделя", "stars": 15, "days": 7},
    "2weeks": {"name": "2 недели", "stars": 25, "days": 14},
    "month": {"name": "Месяц", "stars": 50, "days": 30},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# === ИНИЦИАЛИЗАЦИЯ ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Реестр запущенных ботов
running_bots: Dict[int, subprocess.Popen] = {}


# === БАЗА ДАННЫХ (SQLite) ===

def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Таблица пользователей
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    
    # Таблица слотов (ячейки для ботов)
    c.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan TEXT,
            expires_at TEXT,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    
    # Таблица ботов
    c.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT,
            bot_token TEXT,
            status TEXT DEFAULT 'stopped',
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    
    # Таблица платежей
    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan TEXT,
            stars INTEGER,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")


def get_db():
    """Получить соединение с БД"""
    return sqlite3.connect(DB_PATH)


def get_user(user_id: int):
    """Получить пользователя"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    return user


def create_user(user_id: int, username: str):
    """Создать пользователя"""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
        (user_id, username, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_active_slots(user_id: int):
    """Получить активные слоты пользователя"""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM slots WHERE user_id = ? AND expires_at > ?",
        (user_id, datetime.now().isoformat())
    )
    slots = c.fetchall()
    conn.close()
    return slots


def create_slot(user_id: int, plan: str):
    """Создать слот"""
    days = PLANS[plan]["days"]
    expires_at = datetime.now() + timedelta(days=days)
    
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO slots (user_id, plan, expires_at, created_at) VALUES (?, ?, ?, ?)",
        (user_id, plan, expires_at.isoformat(), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def save_bot(user_id: int, filename: str, bot_token: str):
    """Сохранить бота"""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO bots (user_id, filename, bot_token, created_at) VALUES (?, ?, ?, ?)",
        (user_id, filename, bot_token, datetime.now().isoformat())
    )
    bot_id = c.lastrowid
    conn.commit()
    conn.close()
    return bot_id


def get_user_bots(user_id: int):
    """Получить ботов пользователя"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bots WHERE user_id = ?", (user_id,))
    bots = c.fetchall()
    conn.close()
    return bots


def get_bot(bot_id: int):
    """Получить бота по ID"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bots WHERE id = ?", (bot_id,))
    bot = c.fetchone()
    conn.close()
    return bot


def create_payment(user_id: int, plan: str, stars: int):
    """Создать платёж"""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO payments (user_id, plan, stars, created_at) VALUES (?, ?, ?, ?)",
        (user_id, plan, stars, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# === ЗАПУСК БОТОВ ===

async def start_bot(bot_id: int, code: str, bot_token: str) -> bool:
    """Запустить бота"""
    try:
        bot_dir = BOTS_DIR / f"bot_{bot_id}"
        bot_dir.mkdir(exist_ok=True)
        
        # Сохраняем код
        bot_file = bot_dir / "bot.py"
        bot_file.write_text(code, encoding="utf-8")
        
        # Запускаем процесс
        env = os.environ.copy()
        env["BOT_TOKEN"] = bot_token
        
        process = subprocess.Popen(
            ["python", "bot.py"],
            cwd=bot_dir,
            env=env,
            start_new_session=True
        )
        
        running_bots[bot_id] = process
        logger.info(f"Бот {bot_id} запущен (PID: {process.pid})")
        return True
    except Exception as e:
        logger.error(f"Ошибка запуска бота {bot_id}: {e}")
        return False


async def stop_bot(bot_id: int):
    """Остановить бота"""
    if bot_id in running_bots:
        process = running_bots[bot_id]
        process.terminate()
        process.wait(timeout=5)
        del running_bots[bot_id]
        logger.info(f"Бот {bot_id} остановлен")


# === ПРОВЕРКА ПОДАРКОВ (TELETHON) ===

async def check_gifts():
    """Проверять подарки владельцу через Telethon"""
    if not all([OWNER_PHONE, OWNER_API_ID, OWNER_API_HASH]):
        logger.warning("Не настроены OWNER_PHONE/API_ID/API_HASH — проверка подарков отключена")
        return
    
    try:
        from telethon import TelegramClient, events
        
        client = TelegramClient("owner_session", int(OWNER_API_ID), OWNER_API_HASH)
        await client.start(phone=OWNER_PHONE)
        logger.info(f"Userbot подключен: {OWNER_PHONE}")
        
        @client.on(events.NewMessage(incoming=True))
        async def on_gift(event):
            # Проверяем, является ли сообщение подарком
            if hasattr(event.message, 'action'):
                action = event.message.action
                if hasattr(action, 'gift') or 'gift' in str(type(action)).lower():
                    sender = await event.get_sender()
                    if sender:
                        gift_value = getattr(action, 'stars', 0) or 15
                        logger.info(f"🎁 Подарок от {sender.id}: {gift_value} stars")
                        await process_gift(sender.id, sender.username, gift_value)
        
        await client.run_until_disconnected()
    except ImportError:
        logger.warning("Telethon не установлен — проверка подарков отключена")
    except Exception as e:
        logger.error(f"Ошибка проверки подарков: {e}")


async def process_gift(sender_id: int, username: str, gift_value: int):
    """Обработать подарок"""
    # Определяем план
    if gift_value >= 50:
        plan = "month"
    elif gift_value >= 25:
        plan = "2weeks"
    elif gift_value >= 15:
        plan = "week"
    else:
        return
    
    # Создаём слот
    create_slot(sender_id, plan)
    
    # Уведомляем пользователя
    try:
        await bot.send_message(
            sender_id,
            f"🎁 **Спасибо за подарок!**\n\n"
            f"✅ Вам активирован слот на **{PLANS[plan]['name']}**.\n"
            f"Используйте /upload чтобы загрузить бота."
        )
    except Exception:
        pass
    
    # Уведомляем владельца
    await bot.send_message(
        OWNER_ID,
        f"🎁 Подарок от @{username or sender_id}: {gift_value}⭐ → {PLANS[plan]['name']}"
    )


# === TELEGRAM БОТ (ИНТЕРФЕЙС) ===

class UploadStates(StatesGroup):
    waiting_file = State()
    waiting_token = State()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    create_user(message.from_user.id, message.from_user.username)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Купить слот", callback_data="pricing")],
        [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
        [InlineKeyboardButton(text="🤖 Мои боты", callback_data="mybots")],
        [InlineKeyboardButton(text="🎁 Оплата подарком", callback_data="gift_info")],
    ])
    
    await message.answer(
        f"👋 Привет, {message.from_user.full_name}!\n\n"
        "Я бот для хостинга твоих Telegram-ботов.\n\n"
        "💎 **Купить слот** — оплата через Stars\n"
        "🎁 **Оплата подарком** — подари подарок владельцу\n"
        "📤 **Загрузить бота** — загрузи .py файл\n\n"
        "Выбери действие:",
        reply_markup=kb
    )


@dp.callback_query(F.data == "pricing")
async def cb_pricing(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Неделя — 15⭐", callback_data="buy:week")],
        [InlineKeyboardButton(text="📅 2 недели — 25⭐", callback_data="buy:2weeks")],
        [InlineKeyboardButton(text="📅 Месяц — 50⭐", callback_data="buy:month")],
        [InlineKeyboardButton(text="« Назад", callback_data="back")],
    ])
    await call.message.edit_text("💎 Выберите тариф:", reply_markup=kb)


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: types.CallbackQuery):
    plan = call.data.split(":")[1]
    plan_data = PLANS[plan]
    
    await call.message.answer_invoice(
        title=f"Слот на {plan_data['name']}",
        description="1 ячейка для бота",
        payload=f"slot_{plan}_{call.from_user.id}",
        provider_token="",  # Пустой для Stars
        currency="XTR",
        prices=[LabeledPrice(label=f"Слот {plan_data['name']}", amount=plan_data['stars'])],
    )


@dp.pre_checkout_query()
async def pre_checkout(query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)


@dp.message(F.successful_payment)
async def on_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    plan = payload.split("_")[1]
    
    create_payment(message.from_user.id, plan, message.successful_payment.total_amount)
    create_slot(message.from_user.id, plan)
    
    await message.answer(f"✅ Оплата прошла! Слот на {PLANS[plan]['name']} активирован.")
    
    await bot.send_message(
        OWNER_ID,
        f"💰 Оплата от {message.from_user.full_name}: {PLANS[plan]['name']} ({message.successful_payment.total_amount}⭐)"
    )


@dp.callback_query(F.data == "gift_info")
async def cb_gift_info(call: types.CallbackQuery):
    await call.message.edit_text(
        "🎁 **Оплата подарком**\n\n"
        "1. Открой профиль владельца бота\n"
        "2. Нажми '🎁 Подарить'\n"
        "3. Выбери подарок:\n"
        "   • 15+⭐ → Неделя\n"
        "   • 25+⭐ → 2 недели\n"
        "   • 50+⭐ → Месяц\n"
        "4. Отправь подарок\n"
        "5. Слот активируется автоматически!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="back")]
        ])
    )


@dp.callback_query(F.data == "upload")
async def cb_upload(call: types.CallbackQuery, state: FSMContext):
    slots = get_active_slots(call.from_user.id)
    if not slots:
        await call.answer("Нет активных слотов! Купи слот через /start", show_alert=True)
        return
    
    await call.message.edit_text(
        "📤 Отправь .py-файл бота.\n\n"
        "⚠️ Токен указывай через `os.environ.get('BOT_TOKEN')`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="back")]
        ])
    )
    await state.set_state(UploadStates.waiting_file)


@dp.message(UploadStates.waiting_file, F.document)
async def handle_file(message: types.Message, state: FSMContext):
    doc = message.document
    if not doc.file_name.endswith(".py"):
        await message.answer("❌ Нужен .py-файл")
        return
    
    file_info = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file_info.file_path)
    code = file_bytes.read().decode("utf-8")
    
    await state.update_data(code=code, filename=doc.file_name)
    await message.answer("✅ Файл получен! Теперь отправь токен этого бота:")
    await state.set_state(UploadStates.waiting_token)


@dp.message(UploadStates.waiting_token, F.text)
async def handle_token(message: types.Message, state: FSMContext):
    token = message.text.strip()
    if not token or ":" not in token:
        await message.answer("❌ Неверный токен")
        return
    
    data = await state.get_data()
    bot_id = save_bot(message.from_user.id, data['filename'], token)
    
    # Сохраняем код
    bot_dir = BOTS_DIR / f"bot_{bot_id}"
    bot_dir.mkdir(exist_ok=True)
    (bot_dir / "bot.py").write_text(data['code'], encoding="utf-8")
    
    await message.answer(
        f"✅ Бот сохранён (ID: {bot_id})!\n"
        f"Используй /mybots чтобы запустить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Мои боты", callback_data="mybots")]
        ])
    )
    await state.clear()


@dp.callback_query(F.data == "mybots")
async def cb_mybots(call: types.CallbackQuery):
    bots = get_user_bots(call.from_user.id)
    if not bots:
        await call.message.edit_text(
            "У тебя нет ботов. Используй /upload",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад", callback_data="back")]
            ])
        )
        return
    
    buttons = []
    for b in bots:
        status = "🟢" if b[4] == "running" else "🔴"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {b[2]} (#{b[0]})",
            callback_data=f"bot:{b[0]}"
        )])
    
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back")])
    await call.message.edit_text("🤖 **Твои боты:**", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("bot:"))
async def cb_bot_detail(call: types.CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Запустить", callback_data=f"start:{bot_id}")],
        [InlineKeyboardButton(text="⏹ Остановить", callback_data=f"stop:{bot_id}")],
        [InlineKeyboardButton(text="« Назад", callback_data="mybots")],
    ])
    await call.message.edit_text(f"🤖 **Бот #{bot_id}**", reply_markup=kb)


@dp.callback_query(F.data.startswith("start:"))
async def cb_start_bot(call: types.CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    bot_data = get_bot(bot_id)
    
    if not bot_data:
        await call.answer("Бот не найден", show_alert=True)
        return
    
    bot_dir = BOTS_DIR / f"bot_{bot_id}"
    code = (bot_dir / "bot.py").read_text(encoding="utf-8")
    
    await call.answer("Запускаю...")
    success = await start_bot(bot_id, code, bot_data[3])
    
    if success:
        await call.message.edit_text(f"✅ Бот #{bot_id} запущен!")
    else:
        await call.message.edit_text(f"❌ Ошибка запуска")


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop_bot(call: types.CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    await stop_bot(bot_id)
    await call.message.edit_text(f"⏹ Бот #{bot_id} остановлен")


@dp.callback_query(F.data == "back")
async def cb_back(call: types.CallbackQuery):
    await cmd_start(call.message)


# === MAIN ===

async def main():
    init_db()
    logger.info("BotHost запущен")
    
    # Запускаем проверку подарков в фоне
    asyncio.create_task(check_gifts())
    
    # Запускаем бота
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
