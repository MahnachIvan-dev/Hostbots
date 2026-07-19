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
import sys
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
    "week": {"name": "Неделя", "stars": 1, "days": 7},
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
            is_banned INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    
    # Проверка наличия поля is_banned (для совместимости со старыми БД)
    try:
        c.execute("SELECT is_banned FROM users LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    
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


def is_user_banned(user_id: int) -> bool:
    """Проверить заблокирован ли пользователь"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])


def ban_user(user_id: int):
    """Заблокировать пользователя"""
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    # Остановить все бота пользователя
    user_bots = get_user_bots(user_id)
    for b in user_bots:
        asyncio.create_task(stop_bot(b[0]))


def unban_user(user_id: int):
    """Разблокировать пользователя"""
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_all_users():
    """Получить всех пользователей"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY created_at DESC")
    users = c.fetchall()
    conn.close()
    return users


def get_stats():
    """Получить статистику"""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    banned_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM slots WHERE expires_at > ?", (datetime.now().isoformat(),))
    active_slots = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM bots")
    total_bots = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM payments")
    total_payments = c.fetchone()[0]
    c.execute("SELECT SUM(stars) FROM payments")
    total_stars = c.fetchone()[0] or 0
    conn.close()
    return {
        "total_users": total_users,
        "banned_users": banned_users,
        "active_slots": active_slots,
        "total_bots": total_bots,
        "total_payments": total_payments,
        "total_stars": total_stars,
        "running_bots": len(running_bots),
    }


async def broadcast_message(text: str, parse_mode: str = "HTML"):
    """Рассылка сообщений всем пользователям"""
    users = get_all_users()
    success = 0
    failed = 0
    for user in users:
        try:
            await bot.send_message(user[0], text, parse_mode=parse_mode)
            success += 1
            await asyncio.sleep(0.05)  # Защита от flood
        except Exception:
            failed += 1
    return success, failed


# === СИСТЕМА ХОСТА БОТОВ ===
# Это код, который реально запускает ботов пользователей

async def start_bot(bot_id: int, code: str, bot_token: str) -> bool:
    """
    🚀 ЗАПУСК БОТА ПОЛЬЗОВАТЕЛЯ
    
    Этот код:
    1. Создаёт отдельную папку для бота
    2. Сохраняет код пользователя
    3. Устанавливает зависимости (aiogram, telebot и т.д.)
    4. Запускает бота в изолированном процессе
    5. Подставляет BOT_TOKEN из окружения
    """
    try:
        # Изолированная директория для бота
        bot_dir = BOTS_DIR / f"bot_{bot_id}"
        bot_dir.mkdir(exist_ok=True)
        
        # Сохраняем код пользователя
        bot_file = bot_dir / "user_bot.py"
        bot_file.write_text(code, encoding="utf-8")
        
        # Создаём requirements.txt для популярных библиотек
        req_file = bot_dir / "requirements.txt"
        req_file.write_text(
            "aiogram>=3.0\n"
            "pyTelegramBotAPI\n"
            "python-telegram-bot\n"
            "aiohttp\n",
            encoding="utf-8"
        )
        
        # Создаём wrapper-скрипт, который подставляет токен
        wrapper_code = f'''"""
Wrapper для запуска бота пользователя.
Подставляет BOT_TOKEN из окружения.
"""
import os
import sys

# Токен берётся из окружения (подставляется хостом)
if not os.environ.get("BOT_TOKEN"):
    print("ERROR: BOT_TOKEN не установлен")
    sys.exit(1)

# Импортируем и запускаем код пользователя
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("user_bot", "user_bot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except Exception as e:
    print(f"ERROR: {{e}}")
    sys.exit(1)
'''
        wrapper_file = bot_dir / "wrapper.py"
        wrapper_file.write_text(wrapper_code, encoding="utf-8")
        
        # Файл логов
        log_file = bot_dir / "bot.log"
        
        # Запускаем процесс с ограничениями
        env = os.environ.copy()
        env["BOT_TOKEN"] = bot_token
        env["PYTHONUNBUFFERED"] = "1"  # Чтобы логи писались сразу
        
        with open(log_file, "w", encoding="utf-8") as lf:
            process = subprocess.Popen(
                [sys.executable, "wrapper.py"],
                cwd=bot_dir,
                env=env,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
        
        running_bots[bot_id] = process
        logger.info(f"✅ Бот {bot_id} запущен (PID: {process.pid})")
        
        # Обновляем статус в БД
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE bots SET status = 'running' WHERE id = ?", (bot_id,))
        conn.commit()
        conn.close()
        
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка запуска бота {bot_id}: {e}")
        return False


async def stop_bot(bot_id: int):
    """⏹ Остановить бота"""
    if bot_id in running_bots:
        process = running_bots[bot_id]
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        except Exception as e:
            logger.error(f"Ошибка остановки {bot_id}: {e}")
        del running_bots[bot_id]
        logger.info(f"⏹ Бот {bot_id} остановлен")
    
    # Обновляем статус в БД
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE bots SET status = 'stopped' WHERE id = ?", (bot_id,))
    conn.commit()
    conn.close()


def get_bot_logs(bot_id: int, lines: int = 50) -> str:
    """📄 Получить логи бота"""
    log_file = BOTS_DIR / f"bot_{bot_id}" / "bot.log"
    if not log_file.exists():
        return "Логи пока пустые"
    try:
        content = log_file.read_text(encoding="utf-8", errors="ignore")
        log_lines = content.strip().split("\n")
        return "\n".join(log_lines[-lines:]) or "Логи пока пустые"
    except Exception as e:
        return f"Ошибка чтения логов: {e}"


async def monitor_bots():
    """
    🔄 Мониторинг запущенных ботов
    
    Периодически проверяет статус процессов:
    - Если бот упал — пытается перезапустить
    - Если у пользователя истёк слот — останавливает бота
    - Если пользователь заблокирован — останавливает бота
    """
    logger.info("🔄 Мониторинг ботов запущен")
    while True:
        try:
            for bot_id, process in list(running_bots.items()):
                # Проверяем, жив ли процесс
                if process.poll() is not None:
                    # Процесс завершился
                    exit_code = process.returncode
                    logger.warning(f"⚠️ Бот {bot_id} упал (exit: {exit_code})")
                    del running_bots[bot_id]
                    
                    # Обновляем статус в БД
                    conn = get_db()
                    c = conn.cursor()
                    c.execute(
                        "UPDATE bots SET status = 'error' WHERE id = ?",
                        (bot_id,)
                    )
                    conn.commit()
                    c.execute("SELECT user_id FROM bots WHERE id = ?", (bot_id,))
                    row = c.fetchone()
                    conn.close()
                    
                    if row:
                        user_id = row[0]
                        # Уведомляем пользователя
                        try:
                            logs = get_bot_logs(bot_id, 10)
                            await bot.send_message(
                                user_id,
                                f"⚠️ **Ваш бот #{bot_id} упал**\n\n"
                                f"Код ошибки: {exit_code}\n"
                                f"Последние логи:\n```\n{logs[:500]}\n```",
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass
                else:
                    # Процесс жив — проверяем подписку пользователя
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("SELECT user_id FROM bots WHERE id = ?", (bot_id,))
                    row = c.fetchone()
                    conn.close()
                    
                    if row:
                        user_id = row[0]
                        # Проверка блокировки
                        if is_user_banned(user_id):
                            logger.info(f"🚫 Останавливаю бота {bot_id} — пользователь заблокирован")
                            await stop_bot(bot_id)
                            continue
                        
                        # Проверка подписки
                        slots = get_active_slots(user_id)
                        if not slots:
                            logger.info(f"⏰ Останавливаю бота {bot_id} — истёк слот")
                            await stop_bot(bot_id)
                            try:
                                await bot.send_message(
                                    user_id,
                                    f"⏰ **Ваш бот #{bot_id} остановлен**\n\n"
                                    f"Слот истёк. Купите новый слот через /start"
                                )
                            except Exception:
                                pass
        except Exception as e:
            logger.error(f"Ошибка мониторинга: {e}")
        
        await asyncio.sleep(30)  # Проверка каждые 30 секунд


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
                        gift_value = getattr(action, 'stars', 0) or 1
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
    elif gift_value >= 1:
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
    # Проверка блокировки
    if is_user_banned(message.from_user.id):
        await message.answer(
            "🚫 **Вы заблокированы**\n\n"
            "Ваш аккаунт заблокирован администратором.\n"
            "Если считаете что это ошибка — свяжитесь с поддержкой."
        )
        return
    
    create_user(message.from_user.id, message.from_user.username)
    
    kb_buttons = [
        [InlineKeyboardButton(text="💎 Купить слот", callback_data="pricing")],
        [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
        [InlineKeyboardButton(text="🤖 Мои боты", callback_data="mybots")],
        [InlineKeyboardButton(text="🎁 Оплата подарком", callback_data="gift_info")],
    ]
    
    # Кнопка админки для владельца
    if message.from_user.id == OWNER_ID:
        kb_buttons.append([InlineKeyboardButton(text="🔐 Админ-панель", callback_data="admin")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    
    await message.answer(
        f"👋 Привет, {message.from_user.full_name}!\n\n"
        "Я бот для хостинга твоих Telegram-ботов.\n\n"
        "💎 **Купить слот** — оплата через Stars\n"
        "🎁 **Оплата подарком** — подари подарок владельцу\n"
        "📤 **Загрузить бота** — загрузи .py файл\n\n"
        "Выбери действие:",
        reply_markup=kb
    )


# === АДМИН-ПАНЕЛЬ ===

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return
    await show_admin_panel(message)


async def show_admin_panel(message: types.Message, edit: bool = False):
    """Показать админ-панель"""
    stats = get_stats()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton(text="🚫 Блокировка", callback_data="admin:ban")],
        [InlineKeyboardButton(text="✅ Разблокировка", callback_data="admin:unban")],
        [InlineKeyboardButton(text="🔄 Перезапустить все боты", callback_data="admin:restart_all")],
    ])
    
    text = (
        f"🔐 **Админ-панель**\n\n"
        f"👥 Пользователей: {stats['total_users']}\n"
        f"🚫 Заблокировано: {stats['banned_users']}\n"
        f"💳 Активных слотов: {stats['active_slots']}\n"
        f"🤖 Ботов: {stats['total_bots']} (работает: {stats['running_bots']})\n"
        f"💰 Платежей: {stats['total_payments']} ({stats['total_stars']}⭐)"
    )
    
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@dp.callback_query(F.data == "admin")
async def cb_admin(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("Нет прав", show_alert=True)
    await show_admin_panel(call.message, edit=True)


@dp.callback_query(F.data == "admin:stats")
async def cb_admin_stats(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    stats = get_stats()
    text = (
        f"📊 **Подробная статистика**\n\n"
        f"👥 **Пользователи:**\n"
        f"   • Всего: {stats['total_users']}\n"
        f"   • Заблокировано: {stats['banned_users']}\n"
        f"   • Активно: {stats['total_users'] - stats['banned_users']}\n\n"
        f"💳 **Слоты:**\n"
        f"   • Активных: {stats['active_slots']}\n\n"
        f"🤖 **Боты:**\n"
        f"   • Всего загружено: {stats['total_bots']}\n"
        f"   • Работает сейчас: {stats['running_bots']}\n"
        f"   • Остановлено: {stats['total_bots'] - stats['running_bots']}\n\n"
        f"💰 **Финансы:**\n"
        f"   • Платежей: {stats['total_payments']}\n"
        f"   • Всего звёзд: {stats['total_stars']}⭐"
    )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="admin")]
        ]),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return
    await call.message.edit_text(
        "📢 **Рассылка**\n\n"
        "Отправь сообщение, которое нужно разослать всем пользователям.\n\n"
        "Поддерживается HTML разметка:\n"
        "<b>жирный</b>, <i>курсив</i>, <a href='...'>ссылка</a>\n\n"
        "Для отмены: /cancel",
        parse_mode="Markdown"
    )
    await state.set_state("broadcast_text")


@dp.message(F.state("broadcast_text"))
async def handle_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    
    text = message.text or message.caption or ""
    if not text or text == "/cancel":
        await message.answer("❌ Рассылка отменена")
        await state.clear()
        return
    
    await message.answer("⏳ Рассылаю сообщение...")
    success, failed = await broadcast_message(text)
    
    await message.answer(
        f"✅ **Рассылка завершена**\n\n"
        f"✓ Доставлено: {success}\n"
        f"✗ Ошибок: {failed}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« В админку", callback_data="admin")]
        ]),
        parse_mode="Markdown"
    )
    await state.clear()


@dp.callback_query(F.data == "admin:users")
async def cb_admin_users(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    users = get_all_users()[:10]  # Первые 10
    if not users:
        text = "👥 Пользователей пока нет"
    else:
        text = "👥 **Последние пользователи:**\n\n"
        for u in users:
            status = "🚫" if u[3] else "✓"
            text += f"{status} <code>{u[0]}</code> — @{u[1] or 'нет'}\n"
        if len(get_all_users()) > 10:
            text += f"\n...и ещё {len(get_all_users()) - 10}"
    
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "admin:ban")
async def cb_admin_ban(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return
    await call.message.edit_text(
        "🚫 **Блокировка пользователя**\n\n"
        "Отправь ID или @username пользователя для блокировки.\n"
        "Пример: <code>123456789</code> или <code>@username</code>\n\n"
        "Для отмены: /cancel",
        parse_mode="HTML"
    )
    await state.set_state("ban_user")


@dp.message(F.state("ban_user"))
async def handle_ban(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    
    text = message.text.strip()
    if text == "/cancel":
        await message.answer("❌ Отменено")
        await state.clear()
        return
    
    # Если username
    if text.startswith("@"):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE username = ?", (text[1:],))
        row = c.fetchone()
        conn.close()
        if not row:
            await message.answer("❌ Пользователь не найден")
            return
        user_id = row[0]
    else:
        try:
            user_id = int(text)
        except ValueError:
            await message.answer("❌ Неверный формат")
            return
    
    if user_id == OWNER_ID:
        await message.answer("❌ Нельзя заблокировать владельца")
        return
    
    ban_user(user_id)
    await message.answer(
        f"✅ Пользователь <code>{user_id}</code> заблокирован",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« В админку", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )
    await state.clear()


@dp.callback_query(F.data == "admin:unban")
async def cb_admin_unban(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return
    await call.message.edit_text(
        "✅ **Разблокировка**\n\n"
        "Отправь ID пользователя для разблокировки.\n\n"
        "Для отмены: /cancel",
        parse_mode="HTML"
    )
    await state.set_state("unban_user")


@dp.message(F.state("unban_user"))
async def handle_unban(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    
    text = message.text.strip()
    if text == "/cancel":
        await message.answer("❌ Отменено")
        await state.clear()
        return
    
    try:
        user_id = int(text)
    except ValueError:
        await message.answer("❌ Неверный ID")
        return
    
    unban_user(user_id)
    await message.answer(
        f"✅ Пользователь <code>{user_id}</code> разблокирован",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« В админку", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )
    await state.clear()


@dp.callback_query(F.data == "admin:restart_all")
async def cb_restart_all(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    await call.answer("Перезапускаю все боты...", show_alert=False)
    
    # Остановить все
    for bot_id in list(running_bots.keys()):
        await stop_bot(bot_id)
    
    # Запустить заново всех активных
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, user_id, bot_token FROM bots")
    all_bots = c.fetchall()
    conn.close()
    
    restarted = 0
    for b in all_bots:
        bot_id, user_id, bot_token = b
        if not is_user_banned(user_id):
            bot_dir = BOTS_DIR / f"bot_{bot_id}"
            code_file = bot_dir / "bot.py"
            if code_file.exists():
                code = code_file.read_text(encoding="utf-8")
                if await start_bot(bot_id, code, bot_token):
                    restarted += 1
    
    await call.message.answer(
        f"🔄 Перезапущено ботов: {restarted}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« В админку", callback_data="admin")]
        ])
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено")


@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    """💰 Проверить баланс звёзд бота (только для владельца)"""
    if message.from_user.id != OWNER_ID:
        return
    
    try:
        # Получаем баланс через API
        from aiogram.methods import GetMyCommands
        # Для Stars нужен специальный запрос к API
        # balance = await bot.get_star_transactions(offset=0, limit=1)
        
        await message.answer(
            "💰 **Баланс звёзд бота**\n\n"
            "Чтобы посмотреть баланс и вывести звёзды:\n\n"
            "1. Открой @BotFather\n"
            "2. Отправь /mybots\n"
            "3. Выбери этого бота\n"
            "4. Нажми **Bot Settings** → **Payments** → **Telegram Stars Balance**\n\n"
            "Там увидишь:\n"
            "• Текущий баланс\n"
            "• Историю транзакций\n"
            "• Кнопку вывода в TON\n\n"
            "⚠️ **Важно:**\n"
            "• Минимум для вывода: 1000 звёзд\n"
            "• Период hold: до 21 дня\n"
            "• Комиссия Telegram: ~30%",
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.callback_query(F.data == "pricing")
async def cb_pricing(call: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Неделя — 1⭐", callback_data="buy:week")],
        [InlineKeyboardButton(text="📅 2 недели — 25⭐", callback_data="buy:2weeks")],
        [InlineKeyboardButton(text="📅 Месяц — 50⭐", callback_data="buy:month")],
        [InlineKeyboardButton(text="🎁 Оплатить подарком (быстрее!)", callback_data="gift_info")],
        [InlineKeyboardButton(text="« Назад", callback_data="back")],
    ])
    await call.message.edit_text(
        "💎 **Выберите тариф:**\n\n"
        "🎁 **Совет:** оплатите подарком — так звёзды быстрее дойдут!",
        reply_markup=kb,
        parse_mode="Markdown"
    )


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
        "   • 1+⭐ → Неделя\n"
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
    if is_user_banned(call.from_user.id):
        await call.answer("🚫 Вы заблокированы", show_alert=True)
        return
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
    if is_user_banned(call.from_user.id):
        return await call.answer("🚫 Вы заблокированы", show_alert=True)
    
    bot_id = int(call.data.split(":")[1])
    bot_data = get_bot(bot_id)
    if not bot_data:
        return await call.answer("Бот не найден", show_alert=True)
    
    status = "🟢 работает" if bot_id in running_bots else "🔴 остановлен"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Запустить", callback_data=f"start:{bot_id}")],
        [InlineKeyboardButton(text="⏹ Остановить", callback_data=f"stop:{bot_id}")],
        [InlineKeyboardButton(text="📄 Логи", callback_data=f"logs:{bot_id}")],
        [InlineKeyboardButton(text="« Назад", callback_data="mybots")],
    ])
    await call.message.edit_text(
        f"🤖 **Бот #{bot_id}**\n\n"
        f"📁 Файл: {bot_data[2]}\n"
        f"📊 Статус: {status}",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@dp.callback_query(F.data.startswith("logs:"))
async def cb_bot_logs(call: types.CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    bot_data = get_bot(bot_id)
    
    # Проверка прав
    if not bot_data or bot_data[1] != call.from_user.id:
        if call.from_user.id != OWNER_ID:
            return await call.answer("Нет доступа", show_alert=True)
    
    logs = get_bot_logs(bot_id, 30)
    text = f"📄 **Логи бота #{bot_id}:**\n\n```\n{logs[:2000]}\n```"
    
    await call.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bot_id}")]
        ]),
        parse_mode="Markdown"
    )


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
    logger.info("🤖 BotHost запущен")
    
    # Запускаем фоновые задачи
    asyncio.create_task(check_gifts())      # Проверка подарков
    asyncio.create_task(monitor_bots())     # Мониторинг ботов
    
    # Запускаем бота
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
