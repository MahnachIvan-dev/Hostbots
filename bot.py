"""
🤖 BotHost — хостинг Telegram-ботов (ИСПРАВЛЕННАЯ ВЕРСИЯ)

✅ Все FSM состояния работают
✅ Хост ботов работает
✅ Админы — безлимит
✅ Данные сохраняются в SQLite
✅ Рассылка обычным текстом
"""

import os
import sys
import asyncio
import subprocess
import sqlite3
import logging
import signal
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ═══════════════════════════════════════════════════════════════
# 🔧 КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_TELEGRAM_ID"])
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "")
OWNER_PHONE = os.environ.get("OWNER_PHONE")
OWNER_API_ID = os.environ.get("OWNER_API_ID")
OWNER_API_HASH = os.environ.get("OWNER_API_HASH")

DATA_DIR = Path("./data")
BOTS_DIR = DATA_DIR / "bots"
DB_PATH = DATA_DIR / "bot.db"
WELCOME_IMAGE = DATA_DIR / "welcome.jpg"
DATA_DIR.mkdir(exist_ok=True)
BOTS_DIR.mkdir(exist_ok=True)

PLANS = {
    "week": {"name": "Неделя", "stars": 15, "days": 7, "emoji": "📅"},
    "2weeks": {"name": "2 недели", "stars": 25, "days": 14, "emoji": "📅"},
    "month": {"name": "Месяц", "stars": 50, "days": 30, "emoji": "🗓"},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("BotHost")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
running_bots: Dict[int, subprocess.Popen] = {}


# ═══════════════════════════════════════════════════════════════
# 💾 БАЗА ДАННЫХ (SQLite — всё сохраняется после перезапуска)
# ═══════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            is_admin INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan TEXT,
            expires_at TEXT,
            created_at TEXT,
            gift_id TEXT
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT,
            bot_token TEXT,
            status TEXT DEFAULT 'stopped',
            created_at TEXT
        )
    """)
    
    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_gifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            gift_value INTEGER,
            gift_id TEXT UNIQUE,
            status TEXT DEFAULT 'pending',
            plan TEXT,
            created_at TEXT
        )
    """)
    
    # Миграции
    for col, sql in [
        ("is_banned", "ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0"),
        ("full_name", "ALTER TABLE users ADD COLUMN full_name TEXT"),
    ]:
        try:
            c.execute(f"SELECT {col} FROM users LIMIT 1")
        except sqlite3.OperationalError:
            try:
                c.execute(sql)
            except: pass
    
    conn.commit()
    conn.close()
    logger.info("💾 БД инициализирована")


def get_db():
    return sqlite3.connect(DB_PATH)


# ─── Пользователи ─────────────────────────────────────────

def create_user(user_id: int, username: str, full_name: str = ""):
    conn = get_db()
    c = conn.cursor()
    # Обновляем username и full_name при каждом входе
    c.execute(
        "INSERT INTO users (user_id, username, full_name, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username = ?, full_name = ?",
        (user_id, username, full_name, datetime.now().isoformat(), username, full_name)
    )
    conn.commit()
    conn.close()


def get_all_users():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return rows


def is_user_banned(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return False  # Владелец не может быть забанен
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])


def ban_user(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def unban_user(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ─── Админы ───────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])


def add_admin(user_id: int):
    conn = get_db()
    c = conn.cursor()
    # Создаём пользователя если его нет
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)",
        (user_id, datetime.now().isoformat())
    )
    c.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def remove_admin(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_all_admins():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, username, full_name FROM users WHERE is_admin = 1 AND user_id != ?", (OWNER_ID,))
    rows = c.fetchall()
    conn.close()
    return rows


# ─── Слоты ────────────────────────────────────────────────

def has_active_slot(user_id: int) -> bool:
    """Проверить есть ли активный слот. Владелец и админы — безлимит"""
    if user_id == OWNER_ID:
        return True
    if is_admin(user_id):
        return True  # Админы — безлимит
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM slots WHERE user_id = ? AND expires_at > ?",
        (user_id, datetime.now().isoformat())
    )
    count = c.fetchone()[0]
    conn.close()
    return count > 0


def get_active_slots(user_id: int):
    if user_id == OWNER_ID:
        return [(0, OWNER_ID, "owner", "2099-12-31", datetime.now().isoformat(), "owner")]
    if is_admin(user_id):
        return [(0, user_id, "admin", "2099-12-31", datetime.now().isoformat(), "admin")]
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM slots WHERE user_id = ? AND expires_at > ?",
        (user_id, datetime.now().isoformat())
    )
    rows = c.fetchall()
    conn.close()
    return rows


def create_slot(user_id: int, plan: str, gift_id: str = ""):
    days = PLANS[plan]["days"]
    expires_at = datetime.now() + timedelta(days=days)
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO slots (user_id, plan, expires_at, created_at, gift_id) VALUES (?, ?, ?, ?, ?)",
        (user_id, plan, expires_at.isoformat(), datetime.now().isoformat(), gift_id)
    )
    conn.commit()
    conn.close()


# ─── Боты ─────────────────────────────────────────────────

def save_bot(user_id: int, filename: str, user_bot_token: str):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO bots (user_id, filename, bot_token, created_at) VALUES (?, ?, ?, ?)",
        (user_id, filename, user_bot_token, datetime.now().isoformat())
    )
    bot_id = c.lastrowid
    conn.commit()
    conn.close()
    return bot_id


def get_user_bots(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bots WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_bot(bot_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bots WHERE id = ?", (bot_id,))
    row = c.fetchone()
    conn.close()
    return row


def delete_bot_record(bot_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
    conn.commit()
    conn.close()


def get_all_bots():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bots")
    rows = c.fetchall()
    conn.close()
    return rows


# ─── Подарки ──────────────────────────────────────────────

def save_pending_gift(user_id: int, username: str, gift_value: int, gift_id: str) -> bool:
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO pending_gifts (user_id, username, gift_value, gift_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, gift_value, gift_id, datetime.now().isoformat())
        )
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False
    conn.close()
    return success


def get_pending_gift_for_plan(user_id: int, plan: str):
    required = PLANS[plan]["stars"]
    conn = get_db()
    c = conn.cursor()
    c.execute(
        """SELECT id, gift_value, gift_id FROM pending_gifts 
           WHERE user_id = ? AND status = 'pending' AND gift_value >= ?
           ORDER BY created_at DESC LIMIT 1""",
        (user_id, required)
    )
    row = c.fetchone()
    conn.close()
    return row


def activate_gift(gift_db_id: int, plan: str):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "UPDATE pending_gifts SET status = 'activated', plan = ? WHERE id = ?",
        (plan, gift_db_id)
    )
    conn.commit()
    conn.close()


# ─── Статистика ───────────────────────────────────────────

def get_stats():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    banned = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1")
    admins = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM slots WHERE expires_at > ?", (datetime.now().isoformat(),))
    active_slots = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM bots")
    total_bots = c.fetchone()[0]
    c.execute("SELECT COUNT(*), COALESCE(SUM(gift_value), 0) FROM pending_gifts WHERE status = 'activated'")
    gifts_row = c.fetchone()
    conn.close()
    return {
        "total_users": total_users,
        "banned": banned,
        "admins": admins,
        "active_slots": active_slots,
        "total_bots": total_bots,
        "running_bots": len(running_bots),
        "total_gifts": gifts_row[0],
        "total_stars": gifts_row[1],
    }


# ═══════════════════════════════════════════════════════════════
# 🚀 СИСТЕМА ХОСТА (ИСПРАВЛЕННАЯ)
# ═══════════════════════════════════════════════════════════════

async def start_user_bot(bot_id: int, code: str, user_bot_token: str) -> bool:
    """
    Запускает бота пользователя.
    
    ВАЖНО: код пользователя выполняется напрямую через exec(),
    чтобы все импорты работали (aiogram, telebot и т.д.)
    """
    try:
        bot_dir = BOTS_DIR / f"bot_{bot_id}"
        bot_dir.mkdir(exist_ok=True)
        
        # Сохраняем код пользователя
        (bot_dir / "user_bot.py").write_text(code, encoding="utf-8")
        
        # Wrapper который запускает код пользователя
        # Использует exec() чтобы все импорты работали
        wrapper = '''#!/usr/bin/env python3
"""Wrapper для запуска бота пользователя"""
import os
import sys

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("[BotHost] ERROR: BOT_TOKEN не установлен")
    sys.exit(1)

print(f"[BotHost] Запуск бота, токен: {BOT_TOKEN[:10]}...")

# Читаем код пользователя
with open("user_bot.py", "r", encoding="utf-8") as f:
    user_code = f.read()

# Выполняем код в глобальной области
try:
    exec(user_code, {"__name__": "__main__", "__file__": "user_bot.py"})
    print("[BotHost] Код пользователя выполнен")
except SystemExit as e:
    print(f"[BotHost] Бот завершился с кодом: {e.code}")
    sys.exit(e.code if e.code else 0)
except Exception as e:
    print(f"[BotHost] ОШИБКА: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
'''
        (bot_dir / "wrapper.py").write_text(wrapper, encoding="utf-8")
        
        # Файл логов
        log_file = bot_dir / "bot.log"
        
        # Окружение с токеном
        env = os.environ.copy()
        env["BOT_TOKEN"] = user_bot_token
        env["PYTHONUNBUFFERED"] = "1"
        # Убираем только секретные переменные владельца
        # GROQ_API_KEY, OPENAI_API_KEY и другие API ключи оставляем — они могут быть нужны ботам пользователей
        for k in ["OWNER_TELEGRAM_ID", "OWNER_PHONE", "OWNER_API_ID", "OWNER_API_HASH", "OWNER_USERNAME"]:
            env.pop(k, None)
        
        # Запускаем процесс
        with open(log_file, "w", encoding="utf-8") as lf:
            process = subprocess.Popen(
                [sys.executable, "-u", "wrapper.py"],
                cwd=str(bot_dir),
                env=env,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
        
        running_bots[bot_id] = process
        
        # Ждём 5 секунд и проверяем что бот не упал
        # Даём время на инициализацию и возможные ошибки импорта
        await asyncio.sleep(5)
        
        if process.poll() is not None:
            # Бот упал — читаем логи
            logs = log_file.read_text(encoding="utf-8", errors="ignore")
            
            # Если логи пустые — ждём ещё немного
            if not logs.strip():
                await asyncio.sleep(2)
                logs = log_file.read_text(encoding="utf-8", errors="ignore")
            
            logger.error(f"❌ Бот #{bot_id} упал при запуске:\n{logs}")
            
            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE bots SET status = 'error' WHERE id = ?", (bot_id,))
            conn.commit()
            conn.close()
            
            # Удаляем из running_bots
            if bot_id in running_bots:
                del running_bots[bot_id]
            
            return False
        
        # Бот работает — обновляем статус
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE bots SET status = 'running' WHERE id = ?", (bot_id,))
        conn.commit()
        conn.close()
        
        logger.info(f"✅ Бот #{bot_id} запущен (PID {process.pid})")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка запуска #{bot_id}: {e}")
        return False


async def stop_user_bot(bot_id: int):
    if bot_id in running_bots:
        proc = running_bots[bot_id]
        try:
            # Отправляем SIGTERM всей группе процессов
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.error(f"Ошибка остановки #{bot_id}: {e}")
        finally:
            del running_bots[bot_id]
    
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE bots SET status = 'stopped' WHERE id = ?", (bot_id,))
    conn.commit()
    conn.close()
    logger.info(f"⏹ Бот #{bot_id} остановлен")


def get_bot_logs(bot_id: int, lines: int = 50) -> str:
    log_file = BOTS_DIR / f"bot_{bot_id}" / "bot.log"
    if not log_file.exists():
        return "📭 Логи пока пустые"
    try:
        content = log_file.read_text(encoding="utf-8", errors="ignore")
        log_lines = content.strip().split("\n")
        return "\n".join(log_lines[-lines:]) or "📭 Логи пока пустые"
    except Exception as e:
        return f"❌ Ошибка: {e}"


async def monitor_bots():
    """Мониторинг запущенных ботов"""
    logger.info("🔄 Мониторинг запущен")
    while True:
        try:
            for bot_id, proc in list(running_bots.items()):
                if proc.poll() is not None:
                    code = proc.returncode
                    del running_bots[bot_id]
                    
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("UPDATE bots SET status = 'error' WHERE id = ?", (bot_id,))
                    c.execute("SELECT user_id FROM bots WHERE id = ?", (bot_id,))
                    row = c.fetchone()
                    conn.commit()
                    conn.close()
                    
                    if row and row[0] != OWNER_ID:
                        logs = get_bot_logs(bot_id, 10)
                        try:
                            await bot.send_message(
                                row[0],
                                f"⚠️ <b>Бот #{bot_id} упал</b>\n\n"
                                f"Код: {code}\n<pre>{logs[:500]}</pre>",
                                parse_mode="HTML"
                            )
                        except: pass
                else:
                    # Проверка подписки/блокировки (но не для владельца и админов)
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("SELECT user_id FROM bots WHERE id = ?", (bot_id,))
                    row = c.fetchone()
                    conn.close()
                    
                    if row:
                        uid = row[0]
                        if uid != OWNER_ID and not is_admin(uid):
                            if is_user_banned(uid) or not has_active_slot(uid):
                                await stop_user_bot(bot_id)
        except Exception as e:
            logger.error(f"Мониторинг: {e}")
        
        await asyncio.sleep(30)


async def restore_running_bots():
    """Восстанавливает запущенных ботов после перезапуска"""
    logger.info("🔄 Восстановление ботов после перезапуска...")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, user_id, bot_token, status FROM bots WHERE status = 'running'")
    bots_to_restore = c.fetchall()
    conn.close()
    
    restored = 0
    for bot_id, user_id, user_bot_token, status in bots_to_restore:
        # Проверяем что пользователь не заблокирован и имеет слот
        if is_user_banned(user_id):
            continue
        if user_id != OWNER_ID and not is_admin(user_id) and not has_active_slot(user_id):
            continue
        
        code_file = BOTS_DIR / f"bot_{bot_id}" / "user_bot.py"
        if code_file.exists():
            code = code_file.read_text(encoding="utf-8")
            if await start_user_bot(bot_id, code, user_bot_token):
                restored += 1
    
    logger.info(f"✅ Восстановлено ботов: {restored}")


# ═══════════════════════════════════════════════════════════════
# 🎁 ПРОВЕРКА ПОДАРКОВ
# ═══════════════════════════════════════════════════════════════

async def check_gifts():
    if not all([OWNER_PHONE, OWNER_API_ID, OWNER_API_HASH]):
        logger.warning("⚠️ Не настроены данные для подарков (OWNER_PHONE/API_ID/API_HASH)")
        return
    
    try:
        from telethon import TelegramClient, events
        
        client = TelegramClient("owner_session", int(OWNER_API_ID), OWNER_API_HASH)
        await client.start(phone=OWNER_PHONE)
        logger.info(f"✅ Userbot подключен: {OWNER_PHONE}")
        
        @client.on(events.NewMessage(incoming=True))
        async def on_msg(event):
            try:
                if hasattr(event.message, 'action'):
                    action = event.message.action
                    action_type = str(type(action)).lower()
                    if 'gift' in action_type:
                        sender = await event.get_sender()
                        if sender and sender.id != OWNER_ID:
                            value = getattr(action, 'stars', 0) or getattr(action, 'cost', 0) or 0
                            gift_id = f"{event.id}_{event.chat_id}_{int(datetime.now().timestamp())}"
                            await register_gift(sender.id, sender.username or sender.first_name, value, gift_id)
            except Exception as e:
                logger.error(f"Ошибка подарка: {e}")
        
        await client.run_until_disconnected()
    except ImportError:
        logger.warning("Telethon не установлен — отслеживание подарков отключено")
    except Exception as e:
        logger.error(f"Userbot: {e}")


async def register_gift(user_id: int, username: str, value: int, gift_id: str):
    if not save_pending_gift(user_id, username, value, gift_id):
        return
    
    if value >= 50:
        plan_name = "Месяц"
    elif value >= 25:
        plan_name = "2 недели"
    elif value >= 15:
        plan_name = "Неделя"
    else:
        plan_name = None
    
    try:
        if plan_name:
            await bot.send_message(
                user_id,
                f"🎁 <b>Подарок получен!</b>\n\n"
                f"💎 Стоимость: <b>{value}⭐</b>\n\n"
                f"✅ Вернись в бота и нажми <b>«💎 Купить слот»</b>,\n"
                f"выбери тариф <b>{plan_name}</b> и нажми <b>«✅ Я отправил подарок»</b>",
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                user_id,
                f"🎁 Спасибо за подарок ({value}⭐), но для слота нужно минимум 15⭐"
            )
    except Exception as e:
        logger.error(f"Не удалось уведомить {user_id}: {e}")
    
    try:
        await bot.send_message(
            OWNER_ID,
            f"🎁 <b>Новый подарок!</b>\n\n"
            f"👤 От: @{username or '—'} (<code>{user_id}</code>)\n"
            f"💎 Стоимость: {value}⭐\n"
            f"🆔 ID: <code>{gift_id}</code>",
            parse_mode="HTML"
        )
    except: pass
    
    logger.info(f"🎁 Подарок: {user_id} → {value}⭐")


# ═══════════════════════════════════════════════════════════════
# 💬 ИНТЕРФЕЙС
# ═══════════════════════════════════════════════════════════════

def get_profile_link() -> str:
    if OWNER_USERNAME:
        return f"https://t.me/{OWNER_USERNAME}"
    return f"tg://user?id={OWNER_ID}"


WELCOME_TEXT = """
👋 <b>Привет, {name}!</b>

Я — <b>BotHost</b>, хостинг для Telegram-ботов.

━━━━━━━━━━━━━━━━━━━━━━━

🎁 <b>Как начать?</b>
1️⃣ Нажми «💎 Купить слот»
2️⃣ Выбери тариф
3️⃣ Отправь подарок владельцу
4️⃣ Подтверди оплату
5️⃣ Загрузи своего бота ✨

━━━━━━━━━━━━━━━━━━━━━━━

Выбери действие ниже 👇
"""


def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="💎 Купить слот", callback_data="buy")],
        [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
        [InlineKeyboardButton(text="🤖 Мои боты", callback_data="mybots")],
        [InlineKeyboardButton(text="📊 Мои слоты", callback_data="myslots")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")],
    ]
    
    if is_admin(user_id):
        buttons.append([InlineKeyboardButton(text="🔐 Админ-панель", callback_data="admin")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_welcome(target, edit: bool = False):
    """Отправить приветствие с картинкой если есть"""
    user_id = target.from_user.id if hasattr(target, 'from_user') else target.chat.id
    name = target.from_user.first_name if hasattr(target, 'from_user') else "друг"
    text = WELCOME_TEXT.format(name=name)
    kb = main_menu_kb(user_id)
    
    # Определяем chat_id
    chat_id = target.chat.id if hasattr(target, 'chat') else user_id
    
    # Если есть картинка и пользователь не владелец — отправляем с фото
    if WELCOME_IMAGE.exists() and user_id != OWNER_ID:
        try:
            # При edit=True удаляем старое сообщение
            if edit:
                try:
                    await target.delete()
                except Exception as e:
                    logger.debug(f"Не удалось удалить сообщение: {e}")
            
            # Отправляем новое сообщение с фото в чат
            photo = FSInputFile(WELCOME_IMAGE)
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=text,
                reply_markup=kb,
                parse_mode="HTML"
            )
            logger.info(f"✅ Отправлено приветствие с фото для {user_id}")
            return
        except Exception as e:
            logger.error(f"❌ Ошибка отправки фото для {user_id}: {e}")
            # Падение на текстовое сообщение ниже
    
    # Текстовое приветствие
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception as e:
            logger.debug(f"Не удалось отредактировать: {e}")
    
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"❌ Ошибка отправки приветствия: {e}")


# ═══════════════════════════════════════════════════════════════
# 📝 FSM СОСТОЯНИЯ
# ═══════════════════════════════════════════════════════════════

class UploadStates(StatesGroup):
    waiting_file = State()
    waiting_token = State()


class AdminStates(StatesGroup):
    broadcast = State()
    ban = State()
    unban = State()
    addadmin = State()
    welcome_photo = State()


# ═══════════════════════════════════════════════════════════════
# 📱 ХЕНДЛЕРЫ (порядок важен: FSM хендлеры ДО общих)
# ═══════════════════════════════════════════════════════════════

# ─── СТАРТ ─────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()  # Очищаем любое состояние
    
    if is_user_banned(message.from_user.id):
        await message.answer(
            "🚫 <b>Вы заблокированы</b>\n\nВаш аккаунт заблокирован администрацией.",
            parse_mode="HTML"
        )
        return
    
    create_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.full_name or ""
    )
    await send_welcome(message)


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено")


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id):
        return
    await show_admin_panel(message)


# ═══════════════════════════════════════════════════════════════
# 🔐 FSM ХЕНДЛЕРЫ (ДОЛЖНЫ БЫТЬ ДО ОБЩИХ!)
# ═══════════════════════════════════════════════════════════════

# ─── Загрузка бота ────────────────────────────────────────

@dp.message(AdminStates.broadcast)
async def handle_broadcast(message: types.Message, state: FSMContext):
    """Рассылка — обычный текст БЕЗ HTML"""
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("❌ Отменено")
    
    text = message.text or message.caption or ""
    if not text:
        return
    
    await message.answer("⏳ Рассылаю...")
    ok, fail = 0, 0
    for u in get_all_users():
        if u[4]: continue  # banned
        try:
            # ВАЖНО: БЕЗ parse_mode="HTML" — обычный текст!
            await bot.send_message(u[0], text)
            ok += 1
            await asyncio.sleep(0.05)
        except:
            fail += 1
    
    await message.answer(
        f"✅ Готово!\n✓ Доставлено: {ok}\n✗ Ошибок: {fail}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


@dp.message(AdminStates.ban)
async def handle_ban(message: types.Message, state: FSMContext):
    """Блокировка пользователя"""
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("❌ Отменено")
    
    text = message.text.strip()
    if not text:
        return await message.answer("❌ Отправь ID или @username")
    
    if text.startswith("@"):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE username = ?", (text[1:],))
        row = c.fetchone()
        conn.close()
        if not row:
            return await message.answer(
                "❌ Пользователь не найден в БД",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="« Админка", callback_data="admin")]
                ])
            )
        uid = row[0]
    else:
        try:
            uid = int(text)
        except:
            return await message.answer("❌ Неверный формат. Нужен ID (число) или @username")
    
    if uid == OWNER_ID:
        return await message.answer("❌ Нельзя заблокировать владельца")
    
    ban_user(uid)
    # Остановить его ботов
    for b in get_user_bots(uid):
        await stop_user_bot(b[0])
    
    await message.answer(
        f"🚫 <b>Заблокирован:</b> <code>{uid}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )
    await state.clear()


@dp.message(AdminStates.unban)
async def handle_unban(message: types.Message, state: FSMContext):
    """Разблокировка"""
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("❌ Отменено")
    
    try:
        uid = int(message.text.strip())
    except:
        return await message.answer("❌ Неверный ID (нужно число)")
    
    unban_user(uid)
    await message.answer(
        f"✅ <b>Разблокирован:</b> <code>{uid}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )
    await state.clear()


@dp.message(AdminStates.addadmin)
async def handle_addadmin(message: types.Message, state: FSMContext):
    """Добавление админа"""
    if message.from_user.id != OWNER_ID:
        await state.clear()
        return
    
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("❌ Отменено")
    
    text = message.text.strip()
    if not text:
        return await message.answer("❌ Отправь ID или @username")
    
    if text.startswith("@"):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE username = ?", (text[1:],))
        row = c.fetchone()
        conn.close()
        if not row:
            return await message.answer("❌ Не найден. Пусть напишет /start")
        uid = row[0]
    else:
        try:
            uid = int(text)
        except:
            return await message.answer("❌ Неверный формат")
    
    if uid == OWNER_ID:
        return await message.answer("⚠️ Это уже владелец")
    
    add_admin(uid)
    await message.answer(
        f"🛡 <b>Админ добавлен:</b> <code>{uid}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )
    await state.clear()


@dp.message(AdminStates.welcome_photo, F.photo)
async def handle_welcome_photo(message: types.Message, state: FSMContext):
    """Установка картинки приветствия"""
    if message.from_user.id != OWNER_ID:
        await state.clear()
        return
    
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, WELCOME_IMAGE)
    
    await message.answer(
        "✅ <b>Картинка установлена!</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )
    await state.clear()


@dp.message(AdminStates.welcome_photo, F.text)
async def handle_welcome_text(message: types.Message, state: FSMContext):
    """Удаление картинки или отмена"""
    if message.from_user.id != OWNER_ID:
        await state.clear()
        return
    
    if message.text.strip().lower() == "delete":
        if WELCOME_IMAGE.exists():
            WELCOME_IMAGE.unlink()
            await message.answer(
                "🗑 <b>Картинка удалена</b>",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="« Админка", callback_data="admin")]
                ]),
                parse_mode="HTML"
            )
        else:
            await message.answer("📭 Картинки и так нет")
        await state.clear()
    elif message.text == "/cancel":
        await message.answer("❌ Отменено")
        await state.clear()
    else:
        await message.answer("❌ Отправь фото или напиши 'delete'")


# ─── Загрузка файла бота (FSM) ────────────────────────────

@dp.message(UploadStates.waiting_file, F.document)
async def handle_file(message: types.Message, state: FSMContext):
    doc = message.document
    if not doc.file_name.endswith(".py"):
        return await message.answer("❌ Нужен <b>.py-файл</b>", parse_mode="HTML")
    
    if doc.file_size and doc.file_size > 200 * 1024:
        return await message.answer("❌ Файл больше 200 КБ")
    
    file_info = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file_info.file_path)
    code = file_bytes.read().decode("utf-8")
    
    # Валидация
    errors = []
    tg_libs = ["aiogram", "telebot", "pyrogram", "telegram", "telethon"]
    if not any(lib in code for lib in tg_libs):
        errors.append("• Нет импортов Telegram-библиотек")
    if "os.environ" not in code and "os.getenv" not in code:
        errors.append("• Токен должен браться из os.environ.get('BOT_TOKEN')")
    
    if errors:
        return await message.answer(
            "❌ <b>Код не прошёл проверку:</b>\n\n" + "\n".join(errors),
            parse_mode="HTML"
        )
    
    await state.update_data(code=code, filename=doc.file_name)
    await message.answer(
        "✅ <b>Файл принят!</b>\n\n"
        f"📁 {doc.file_name}\n\n"
        "Теперь отправь <b>токен этого бота</b>.\n"
        "🔒 Токен будет удалён из чата для безопасности.",
        parse_mode="HTML"
    )
    await state.set_state(UploadStates.waiting_token)


@dp.message(UploadStates.waiting_token, F.text)
async def handle_token(message: types.Message, state: FSMContext):
    token = message.text.strip()
    if not token or ":" not in token or len(token) < 40:
        return await message.answer("❌ Неверный формат токена")
    
    # Удаляем сообщение с токеном
    try:
        await message.delete()
    except: pass
    
    data = await state.get_data()
    bot_id = save_bot(message.from_user.id, data['filename'], token)
    
    bot_dir = BOTS_DIR / f"bot_{bot_id}"
    bot_dir.mkdir(exist_ok=True)
    (bot_dir / "user_bot.py").write_text(data['code'], encoding="utf-8")
    
    await message.answer(
        f"✅ <b>Бот сохранён!</b>\n\n"
        f"🆔 ID: <code>{bot_id}</code>\n"
        f"📁 Файл: {data['filename']}\n\n"
        "Запусти через <b>«🤖 Мои боты»</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 К моим ботам", callback_data="mybots")]
        ]),
        parse_mode="HTML"
    )
    await state.clear()


# ═══════════════════════════════════════════════════════════════
# 📸 ОБЩИЙ ОБРАБОТЧИК ФОТО (после FSM!)
# ═══════════════════════════════════════════════════════════════

@dp.message(F.photo)
async def handle_random_photo(message: types.Message):
    """Если пользователь просто кидает фото"""
    await message.answer(
        "📸 <b>Получил фото!</b>\n\n"
        "Чтобы <b>загрузить своего бота</b>, нужен <b>.py-файл</b>.\n"
        "Используй кнопку ниже:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
            [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════
# 🔘 CALLBACK ХЕНДЛЕРЫ
# ═══════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "back_main")
async def cb_back_main(call: types.CallbackQuery, state: FSMContext):
    await state.clear()  # Очищаем состояние
    
    if is_user_banned(call.from_user.id):
        return await call.answer("🚫 Вы заблокированы", show_alert=True)
    
    # Удаляем старое сообщение
    try:
        await call.message.delete()
    except Exception as e:
        logger.debug(f"Не удалось удалить сообщение: {e}")
    
    # Отправляем новое приветствие
    await send_welcome(call.message)


# ─── Покупка слота ────────────────────────────────────────

@dp.callback_query(F.data == "buy")
async def cb_buy(call: types.CallbackQuery, state: FSMContext):
    await state.clear()  # Очищаем состояние
    
    if is_user_banned(call.from_user.id):
        return await call.answer("🚫 Вы заблокированы", show_alert=True)
    
    if call.from_user.id == OWNER_ID:
        await call.message.edit_text(
            "👑 <b>Ты — владелец!</b>\n\n"
            "У тебя <b>бесплатный безлимит</b>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
                [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
            ]),
            parse_mode="HTML"
        )
        return
    
    if is_admin(call.from_user.id):
        await call.message.edit_text(
            "🛡 <b>Ты — админ!</b>\n\n"
            "У тебя <b>бесплатный безлимит</b>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
                [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
            ]),
            parse_mode="HTML"
        )
        return
    
    text = (
        "💎 <b>Выбери тариф</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Оплата <b>подарком</b> владельцу.\n"
        "После отправки — подтверди в боте.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📅 Неделя — {PLANS['week']['stars']}⭐", callback_data="plan:week")],
        [InlineKeyboardButton(text=f"📅 2 недели — {PLANS['2weeks']['stars']}⭐", callback_data="plan:2weeks")],
        [InlineKeyboardButton(text=f"🗓 Месяц — {PLANS['month']['stars']}⭐", callback_data="plan:month")],
        [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
    ])
    
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: types.CallbackQuery, state: FSMContext):
    await state.clear()  # Очищаем состояние
    plan_id = call.data.split(":")[1]
    plan = PLANS[plan_id]
    link = get_profile_link()
    
    text = (
        f"{plan['emoji']} <b>Тариф: {plan['name']}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💎 <b>Стоимость:</b> {plan['stars']}⭐\n"
        f"📅 <b>Длительность:</b> {plan['days']} дней\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>🎁 Как оплатить:</b>\n\n"
        f"1️⃣ Нажми <b>«🎁 Отправить подарок»</b>\n\n"
        f"2️⃣ В профиле нажми <b>⋮ → 🎁 Подарить</b>\n\n"
        f"3️⃣ Выбери подарок <b>от {plan['stars']}⭐</b>\n\n"
        f"4️⃣ Отправь подарок\n\n"
        f"5️⃣ Вернись и нажми <b>«✅ Я отправил подарок»</b>"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Отправить подарок", url=link)],
        [InlineKeyboardButton(text="✅ Я отправил подарок", callback_data=f"confirm:{plan_id}")],
        [InlineKeyboardButton(text="« Другие тарифы", callback_data="buy")],
        [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
    ])
    
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("confirm:"))
async def cb_confirm_payment(call: types.CallbackQuery, state: FSMContext):
    await state.clear()  # Очищаем состояние
    plan_id = call.data.split(":")[1]
    plan = PLANS[plan_id]
    user_id = call.from_user.id
    
    await call.answer("⏳ Проверяю...")
    
    gift = get_pending_gift_for_plan(user_id, plan_id)
    
    if not gift:
        await call.message.edit_text(
            "❌ <b>Подарок не найден!</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Мы не получили подарок <b>от {plan['stars']}⭐</b>.\n\n"
            "<b>Что делать:</b>\n"
            f"1️⃣ Убедись что отправил <b>от {plan['stars']}⭐</b>\n"
            "2️⃣ Подожди 1-2 минуты\n"
            "3️⃣ Попробуй ещё раз",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить ещё раз", callback_data=f"confirm:{plan_id}")],
                [InlineKeyboardButton(text="🎁 Отправить подарок", url=get_profile_link())],
                [InlineKeyboardButton(text="« Другие тарифы", callback_data="buy")],
                [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
            ]),
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        return
    
    gift_id, gift_value, gift_uid = gift
    activate_gift(gift_id, plan_id)
    create_slot(user_id, plan_id, gift_uid)
    
    await call.message.edit_text(
        f"✅ <b>Оплата прошла!</b>\n\n"
        f"🎁 Подарок: <b>{gift_value}⭐</b>\n"
        f"{plan['emoji']} Тариф: <b>{plan['name']}</b>\n"
        f"📅 Длительность: <b>{plan['days']} дней</b>\n\n"
        f"🎉 Теперь можешь загрузить бота!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
            [InlineKeyboardButton(text="📊 Мои слоты", callback_data="myslots")],
            [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )
    
    try:
        admin_msg = (
            f"💰 <b>Оплата!</b>\n\n"
            f"👤 @{call.from_user.username or '—'} (<code>{user_id}</code>)\n"
            f"🎁 {gift_value}⭐ → {plan['name']}"
        )
        await bot.send_message(OWNER_ID, admin_msg, parse_mode="HTML")
    except: pass


# ─── Загрузка ─────────────────────────────────────────────

@dp.callback_query(F.data == "upload")
async def cb_upload(call: types.CallbackQuery, state: FSMContext):
    # НЕ очищаем состояние здесь — мы сами его устанавливаем
    
    if is_user_banned(call.from_user.id):
        await state.clear()
        return await call.answer("🚫 Вы заблокированы", show_alert=True)
    
    if not has_active_slot(call.from_user.id):
        await state.clear()
        await call.answer("❌ Нет активного слота!", show_alert=True)
        return
    
    await call.message.edit_text(
        "📤 <b>Загрузка бота</b>\n\n"
        "Отправь <b>.py-файл</b>.\n\n"
        "⚠️ <b>Важно:</b>\n"
        "• Токен: <code>os.environ.get('BOT_TOKEN')</code>\n"
        "• НЕ хардкодь токен!\n"
        "• aiogram, telebot, pyrogram\n"
        "• Макс. 200 КБ",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="back_main")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(UploadStates.waiting_file)


# ─── Мои боты ─────────────────────────────────────────────

@dp.callback_query(F.data == "mybots")
async def cb_mybots(call: types.CallbackQuery, state: FSMContext):
    await state.clear()  # Очищаем состояние
    bots = get_user_bots(call.from_user.id)
    if not bots:
        await call.message.edit_text(
            "🤖 <b>У тебя пока нет ботов</b>\n\n"
            "Загрузи первого!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
                [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
            ]),
            parse_mode="HTML"
        )
        return
    
    buttons = []
    for b in bots:
        status = "🟢" if b[0] in running_bots else "🔴"
        buttons.append([InlineKeyboardButton(
            text=f"{status} #{b[0]} • {b[2]}",
            callback_data=f"bot:{b[0]}"
        )])
    buttons.append([InlineKeyboardButton(text="« Главное меню", callback_data="back_main")])
    
    await call.message.edit_text(
        f"🤖 <b>Твои боты ({len(bots)})</b>\n\n"
        "🟢 работает • 🔴 остановлен",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("bot:"))
async def cb_bot_detail(call: types.CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    if not b or b[1] != call.from_user.id:
        if not is_admin(call.from_user.id):
            return await call.answer("❌ Нет доступа", show_alert=True)
    
    status = "🟢 Работает" if bot_id in running_bots else "🔴 Остановлен"
    
    await call.message.edit_text(
        f"🤖 <b>Бот #{bot_id}</b>\n\n"
        f"📁 Файл: <code>{b[2]}</code>\n"
        f"📊 Статус: <b>{status}</b>\n"
        f"📅 Создан: {b[5][:10]}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Запустить", callback_data=f"start:{bot_id}"),
                InlineKeyboardButton(text="⏹ Стоп", callback_data=f"stop:{bot_id}"),
            ],
            [InlineKeyboardButton(text="📄 Логи", callback_data=f"logs:{bot_id}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{bot_id}")],
            [InlineKeyboardButton(text="« К списку", callback_data="mybots")],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("start:"))
async def cb_start_bot(call: types.CallbackQuery, state: FSMContext):
    await state.clear()  # Очищаем состояние
    
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    if not b or b[1] != call.from_user.id:
        return await call.answer("❌ Нет доступа", show_alert=True)
    
    if not has_active_slot(call.from_user.id):
        return await call.answer("❌ Нет активного слота", show_alert=True)
    
    if bot_id in running_bots:
        return await call.answer("⚠️ Уже запущен", show_alert=True)
    
    await call.answer("⏳ Запускаю (это займёт ~5 секунд)...")
    
    code_file = BOTS_DIR / f"bot_{bot_id}" / "user_bot.py"
    if not code_file.exists():
        return await call.answer("❌ Файл не найден", show_alert=True)
    
    code = code_file.read_text(encoding="utf-8")
    success = await start_user_bot(bot_id, code, b[3])
    
    if success:
        await call.message.answer(
            f"✅ <b>Бот #{bot_id} запущен!</b>\n\n"
            f"🟢 Статус: работает\n"
            f"⏱ Мониторинг активен",
            parse_mode="HTML"
        )
    else:
        # Бот упал — показываем подробную ошибку
        logs = get_bot_logs(bot_id, 25)
        
        # Определяем типичные проблемы
        error_hint = ""
        if "GROQ_API_KEY" in logs:
            error_hint = "\n\n💡 <b>Подсказка:</b> Добавь <code>GROQ_API_KEY</code> в Railway Variables"
        elif "OPENAI_API_KEY" in logs:
            error_hint = "\n\n💡 <b>Подсказка:</b> Добавь <code>OPENAI_API_KEY</code> в Railway Variables"
        elif "No module named" in logs:
            error_hint = "\n\n💡 <b>Подсказка:</b> Недостающая библиотека. Пересдеплой Railway."
        elif "BOT_TOKEN" in logs:
            error_hint = "\n\n💡 <b>Подсказка:</b> Используй <code>os.environ.get('BOT_TOKEN')</code> в коде"
        elif "SyntaxError" in logs or "IndentationError" in logs:
            error_hint = "\n\n💡 <b>Подсказка:</b> Синтаксическая ошибка в коде"
        elif "Invalid token" in logs or "Unauthorized" in logs:
            error_hint = "\n\n💡 <b>Подсказка:</b> Неверный токен бота"
        
        await call.message.answer(
            f"❌ <b>Бот #{bot_id} не запустился</b>\n\n"
            f"<b>Причина:</b>\n"
            f"<pre>{logs[:1500]}</pre>"
            f"{error_hint}\n\n"
            f"🔧 Исправь ошибку и попробуй снова.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"start:{bot_id}")],
                [InlineKeyboardButton(text="📄 Логи", callback_data=f"logs:{bot_id}")],
                [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bot_id}")],
            ])
        )


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop_bot(call: types.CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    if not b or b[1] != call.from_user.id:
        return await call.answer("❌ Нет доступа", show_alert=True)
    
    await stop_user_bot(bot_id)
    await call.answer("⏹ Остановлен")


@dp.callback_query(F.data.startswith("logs:"))
async def cb_logs(call: types.CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    
    logs = get_bot_logs(bot_id, 30)
    await call.message.answer(
        f"📄 <b>Логи бота #{bot_id}</b>\n\n<pre>{logs[:2500]}</pre>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bot_id}")]
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete_bot(call: types.CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    if not b or b[1] != call.from_user.id:
        return await call.answer("❌ Нет доступа", show_alert=True)
    
    await stop_user_bot(bot_id)
    delete_bot_record(bot_id)
    await call.answer("🗑 Удалён")
    await cb_mybots(call)


# ─── Мои слоты ────────────────────────────────────────────

@dp.callback_query(F.data == "myslots")
async def cb_myslots(call: types.CallbackQuery, state: FSMContext):
    await state.clear()  # Очищаем состояние
    if call.from_user.id == OWNER_ID:
        await call.message.edit_text(
            "👑 <b>Твой слот:</b>\n\n"
            "🎫 <b>Безлимитный</b> (бесплатно)\n"
            "♾ Действует: всегда",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")]
            ]),
            parse_mode="HTML"
        )
        return
    
    if is_admin(call.from_user.id):
        await call.message.edit_text(
            "🛡 <b>Твой слот:</b>\n\n"
            "🎫 <b>Безлимитный</b> (админ)\n"
            "♾ Действует: всегда",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")]
            ]),
            parse_mode="HTML"
        )
        return
    
    slots = get_active_slots(call.from_user.id)
    if not slots:
        await call.message.edit_text(
            "💳 <b>У тебя нет активных слотов</b>\n\n"
            "Купи через подарок!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить слот", callback_data="buy")],
                [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
            ]),
            parse_mode="HTML"
        )
        return
    
    text = f"💳 <b>Твои активные слоты ({len(slots)})</b>\n\n"
    for s in slots:
        exp = datetime.fromisoformat(s[3])
        days_left = (exp - datetime.now()).days
        text += f"• <b>{PLANS[s[2]]['name']}</b> — ещё {days_left} дн. (до {exp.strftime('%d.%m')})\n"
    
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Купить ещё", callback_data="buy")],
            [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )


# ─── Помощь ───────────────────────────────────────────────

@dp.callback_query(F.data == "help")
async def cb_help(call: types.CallbackQuery, state: FSMContext):
    await state.clear()  # Очищаем состояние
    link = get_profile_link()
    text = (
        "❓ <b>Помощь</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>🎁 Как купить слот?</b>\n"
        "1. Нажми «💎 Купить слот»\n"
        "2. Выбери тариф (15/25/50⭐)\n"
        "3. Нажми «🎁 Отправить подарок»\n"
        "4. Отправь подарок владельцу\n"
        "5. Вернись и нажми «✅ Я отправил подарок»\n\n"
        "<b>📤 Как загрузить бота?</b>\n"
        "1. Купи слот\n"
        "2. Нажми «📤 Загрузить бота»\n"
        "3. Отправь .py-файл\n"
        "4. Отправь токен бота\n"
        "5. Запусти через «🤖 Мои боты»\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Написать владельцу", url=link)],
            [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
        ]),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


# ═══════════════════════════════════════════════════════════════
# 🔐 АДМИН-ПАНЕЛЬ
# ═══════════════════════════════════════════════════════════════

async def show_admin_panel(message: types.Message, edit: bool = False):
    stats = get_stats()
    text = (
        "🔐 <b>Админ-панель</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Пользователей: <b>{stats['total_users']}</b>\n"
        f"🚫 Заблок.: <b>{stats['banned']}</b>\n"
        f"🛡 Админов: <b>{stats['admins']}</b>\n"
        f"💳 Слотов: <b>{stats['active_slots']}</b>\n"
        f"🤖 Ботов: <b>{stats['total_bots']}</b> (🟢 {stats['running_bots']})\n"
        f"🎁 Подарков: <b>{stats['total_gifts']}</b> ({stats['total_stars']}⭐)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:users")],
        [
            InlineKeyboardButton(text="🚫 Бан", callback_data="adm:ban"),
            InlineKeyboardButton(text="✅ Разбан", callback_data="adm:unban"),
        ],
        [
            InlineKeyboardButton(text="🛡 +Админ", callback_data="adm:addadmin"),
            InlineKeyboardButton(text="❌ -Админ", callback_data="adm:remadmin"),
        ],
        [InlineKeyboardButton(text="🖼 Приветствие", callback_data="adm:welcome")],
        [InlineKeyboardButton(text="🔄 Рестарт всех", callback_data="adm:restart")],
        [InlineKeyboardButton(text="« Главное меню", callback_data="back_main")],
    ])
    
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "admin")
async def cb_admin(call: types.CallbackQuery, state: FSMContext):
    await state.clear()  # Очищаем состояние
    if not is_admin(call.from_user.id):
        return await call.answer("🔐 Нет прав", show_alert=True)
    await show_admin_panel(call.message, edit=True)


@dp.callback_query(F.data == "adm:stats")
async def cb_adm_stats(call: types.CallbackQuery):
    if not is_admin(call.from_user.id): return
    s = get_stats()
    text = (
        "📊 <b>Подробная статистика</b>\n\n"
        f"👥 Пользователей: {s['total_users']}\n"
        f"🚫 Заблок.: {s['banned']}\n"
        f"🛡 Админов: {s['admins']}\n\n"
        f"💳 Слотов: {s['active_slots']}\n\n"
        f"🤖 Ботов всего: {s['total_bots']}\n"
        f"🤖 Работает: {s['running_bots']}\n\n"
        f"🎁 Подарков: {s['total_gifts']}\n"
        f"💎 Звёзд: {s['total_stars']}⭐"
    )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm:broadcast")
async def cb_adm_broadcast(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await call.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Отправь сообщение.\n"
        "⚠️ Отправится как <b>обычный текст</b> (без HTML).\n\n"
        "Отмена: /cancel"
    )
    await state.set_state(AdminStates.broadcast)


@dp.callback_query(F.data == "adm:users")
async def cb_adm_users(call: types.CallbackQuery):
    if not is_admin(call.from_user.id): return
    users = get_all_users()[:15]
    if not users:
        text = "👥 Пользователей нет"
    else:
        text = f"👥 <b>Пользователи ({len(get_all_users())})</b>\n\n"
        for u in users:
            ban = "🚫" if u[4] else "✓"
            adm = "🛡" if u[3] else ""
            uname = f"@{u[1]}" if u[1] else "—"
            text += f"{ban}{adm} <code>{u[0]}</code> {uname}\n"
    
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm:ban")
async def cb_adm_ban(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await call.message.edit_text(
        "🚫 <b>Блокировка</b>\n\n"
        "Отправь ID или @username.\n"
        "Пример: <code>123456789</code> или <code>@user</code>\n\n"
        "Отмена: /cancel",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.ban)


@dp.callback_query(F.data == "adm:unban")
async def cb_adm_unban(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await call.message.edit_text(
        "✅ <b>Разблокировка</b>\n\n"
        "Отправь ID пользователя.\n\n"
        "Отмена: /cancel"
    )
    await state.set_state(AdminStates.unban)


@dp.callback_query(F.data == "adm:addadmin")
async def cb_addadmin(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⚠️ Только владелец", show_alert=True)
    await call.message.edit_text(
        "🛡 <b>Добавить админа</b>\n\n"
        "Отправь ID или @username.\n\n"
        "Отмена: /cancel"
    )
    await state.set_state(AdminStates.addadmin)


@dp.callback_query(F.data == "adm:remadmin")
async def cb_remadmin(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⚠️ Только владелец", show_alert=True)
    
    admins = get_all_admins()
    if not admins:
        return await call.answer("📭 Админов нет", show_alert=True)
    
    buttons = [[InlineKeyboardButton(
        text=f"❌ {a[1] or a[2] or a[0]}",
        callback_data=f"remadm:{a[0]}"
    )] for a in admins]
    buttons.append([InlineKeyboardButton(text="« Отмена", callback_data="admin")])
    
    await call.message.edit_text(
        "❌ <b>Удалить админа:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@dp.callback_query(F.data.startswith("remadm:"))
async def cb_remadm_confirm(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID: return
    uid = int(call.data.split(":")[1])
    remove_admin(uid)
    await call.answer("✅ Удалён")
    await show_admin_panel(call.message, edit=True)


@dp.callback_query(F.data == "adm:welcome")
async def cb_welcome(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⚠️ Только владелец", show_alert=True)
    
    status = "✅ Установлена" if WELCOME_IMAGE.exists() else "📭 Не установлена"
    await call.message.edit_text(
        f"🖼 <b>Приветствие</b>\n\n"
        f"Картинка: {status}\n\n"
        f"Отправь <b>фото</b> или напиши <code>delete</code>.\n"
        f"Отмена: /cancel",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.welcome_photo)


@dp.callback_query(F.data == "adm:restart")
async def cb_restart_all(call: types.CallbackQuery):
    if not is_admin(call.from_user.id): return
    await call.answer("⏳ Перезапускаю...")
    
    for bid in list(running_bots.keys()):
        await stop_user_bot(bid)
    
    all_bots = get_all_bots()
    restarted = 0
    for b in all_bots:
        bid, uid, _, user_bot_token, status, _ = b
        if is_user_banned(uid):
            continue
        if uid != OWNER_ID and not is_admin(uid) and not has_active_slot(uid):
            continue
        
        code_file = BOTS_DIR / f"bot_{bid}" / "user_bot.py"
        if code_file.exists():
            code = code_file.read_text(encoding="utf-8")
            if await start_user_bot(bid, code, user_bot_token):
                restarted += 1
    
    await call.message.answer(
        f"🔄 <b>Перезапущено:</b> {restarted}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════
# 🎯 MAIN
# ═══════════════════════════════════════════════════════════════

async def main():
    init_db()
    logger.info("=" * 50)
    logger.info("🤖 BotHost запущен")
    logger.info(f"👤 Владелец: {OWNER_ID}")
    logger.info("=" * 50)
    
    # Восстанавливаем запущенных ботов
    await restore_running_bots()
    
    # Фоновые задачи
    asyncio.create_task(check_gifts())
    asyncio.create_task(monitor_bots())
    
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
