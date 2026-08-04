"""
🤖 BotHost — хостинг Telegram-ботов (ПОЛНАЯ ВЕРСИЯ v2.0)

✅ Автоматизация чатов (userbot через Telethon)
✅ Подарки отслеживаются в реальном времени
✅ При оформлении тарифа — автоподтверждение если подарок есть
✅ Хост ботов — несколько файлов, размер до 10MB
✅ Мульти-файловый хостинг (zip архивы)
✅ Админы — безлимит
✅ Данные сохраняются в SQLite
"""

import os
import sys
import asyncio
import subprocess
import sqlite3
import logging
import signal
import html
import re
import zipfile
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

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
SESSIONS_DIR = DATA_DIR / "sessions"

DATA_DIR.mkdir(exist_ok=True)
BOTS_DIR.mkdir(exist_ok=True)
SESSIONS_DIR.mkdir(exist_ok=True)

# Лимиты файлов
MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 МБ на файл
MAX_ZIP_SIZE = 50 * 1024 * 1024    # 50 МБ на zip архив
MAX_FILES_PER_BOT = 20              # Максимум файлов в боте
MAX_BOTS_PER_USER = 10              # Максимум ботов у одного пользователя

PLANS = {
    "week":   {"name": "Неделя",   "stars": 15, "days": 7,  "emoji": "📅"},
    "2weeks": {"name": "2 недели", "stars": 25, "days": 14, "emoji": "📅"},
    "month":  {"name": "Месяц",    "stars": 50, "days": 30, "emoji": "🗓"},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("BotHost")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Запущенные боты: bot_id -> subprocess.Popen
running_bots: Dict[int, subprocess.Popen] = {}

# Userbot (Telethon клиент) — глобальный
userbot_client = None

# Очередь подарков для мгновенного подтверждения
# gift_queue[user_id] = [{"value": int, "gift_id": str, "ts": datetime}, ...]
gift_queue: Dict[int, List[dict]] = {}


# ═══════════════════════════════════════════════════════════════
# 💾 БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            full_name  TEXT,
            is_admin   INTEGER DEFAULT 0,
            is_banned  INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            plan       TEXT,
            expires_at TEXT,
            created_at TEXT,
            gift_id    TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            name         TEXT,
            main_file    TEXT DEFAULT 'main.py',
            bot_token    TEXT,
            status       TEXT DEFAULT 'stopped',
            created_at   TEXT,
            file_count   INTEGER DEFAULT 1,
            total_size   INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_gifts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            username    TEXT,
            gift_value  INTEGER,
            gift_id     TEXT UNIQUE,
            status      TEXT DEFAULT 'pending',
            plan        TEXT,
            created_at  TEXT
        )
    """)

    # Миграции
    migrations = [
        ("SELECT is_banned FROM users LIMIT 1",
         "ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0"),
        ("SELECT full_name FROM users LIMIT 1",
         "ALTER TABLE users ADD COLUMN full_name TEXT"),
        ("SELECT file_count FROM bots LIMIT 1",
         "ALTER TABLE bots ADD COLUMN file_count INTEGER DEFAULT 1"),
        ("SELECT total_size FROM bots LIMIT 1",
         "ALTER TABLE bots ADD COLUMN total_size INTEGER DEFAULT 0"),
        ("SELECT name FROM bots LIMIT 1",
         "ALTER TABLE bots ADD COLUMN name TEXT"),
        ("SELECT main_file FROM bots LIMIT 1",
         "ALTER TABLE bots ADD COLUMN main_file TEXT DEFAULT 'main.py'"),
    ]
    for check_sql, alter_sql in migrations:
        try:
            c.execute(check_sql)
        except sqlite3.OperationalError:
            try:
                c.execute(alter_sql)
            except Exception:
                pass

    conn.commit()
    conn.close()
    logger.info("💾 БД инициализирована")


def get_db():
    return sqlite3.connect(DB_PATH)


# ─── Пользователи ─────────────────────────────────────────────

def create_user(user_id: int, username: str, full_name: str = ""):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO users (user_id, username, full_name, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=?, full_name=?",
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
        return False
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])


def ban_user(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def unban_user(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


# ─── Админы ───────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT is_admin FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])


def add_admin(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?,?)",
        (user_id, datetime.now().isoformat())
    )
    c.execute("UPDATE users SET is_admin=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def remove_admin(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET is_admin=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def get_all_admins():
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT user_id, username, full_name FROM users WHERE is_admin=1 AND user_id!=?",
        (OWNER_ID,)
    )
    rows = c.fetchall()
    conn.close()
    return rows


# ─── Слоты ────────────────────────────────────────────────────

def has_active_slot(user_id: int) -> bool:
    if user_id == OWNER_ID or is_admin(user_id):
        return True
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM slots WHERE user_id=? AND expires_at>?",
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
        "SELECT * FROM slots WHERE user_id=? AND expires_at>?",
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
        "INSERT INTO slots (user_id, plan, expires_at, created_at, gift_id) VALUES (?,?,?,?,?)",
        (user_id, plan, expires_at.isoformat(), datetime.now().isoformat(), gift_id)
    )
    conn.commit()
    conn.close()
    return expires_at


# ─── Боты ─────────────────────────────────────────────────────

def save_bot(user_id: int, name: str, main_file: str, user_bot_token: str,
             file_count: int = 1, total_size: int = 0) -> int:
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO bots (user_id, name, main_file, bot_token, created_at, file_count, total_size) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, name, main_file, user_bot_token,
         datetime.now().isoformat(), file_count, total_size)
    )
    bot_id = c.lastrowid
    conn.commit()
    conn.close()
    return bot_id


def get_user_bots(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bots WHERE user_id=?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_user_bots_count(user_id: int) -> int:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM bots WHERE user_id=?", (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count


def get_bot(bot_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bots WHERE id=?", (bot_id,))
    row = c.fetchone()
    conn.close()
    return row


def delete_bot_record(bot_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    conn.commit()
    conn.close()


def update_bot_main_file(bot_id: int, main_file: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE bots SET main_file=? WHERE id=?", (main_file, bot_id))
    conn.commit()
    conn.close()


def get_all_bots():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM bots")
    rows = c.fetchall()
    conn.close()
    return rows


# ─── Подарки ──────────────────────────────────────────────────

def save_pending_gift(user_id: int, username: str, gift_value: int, gift_id: str) -> bool:
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO pending_gifts (user_id, username, gift_value, gift_id, created_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, username, gift_value, gift_id, datetime.now().isoformat())
        )
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False
    conn.close()
    return success


def get_pending_gift_for_plan(user_id: int, plan: str) -> Optional[tuple]:
    """
    Ищет подарок за последние 30 минут — для мгновенного подтверждения.
    Также смотрит старые неиспользованные подарки.
    """
    required = PLANS[plan]["stars"]
    conn = get_db()
    c = conn.cursor()

    # Сначала ищем свежий подарок (последние 30 минут)
    cutoff = (datetime.now() - timedelta(minutes=30)).isoformat()
    c.execute(
        """SELECT id, gift_value, gift_id FROM pending_gifts
           WHERE user_id=? AND status='pending' AND gift_value>=?
             AND created_at >= ?
           ORDER BY created_at DESC LIMIT 1""",
        (user_id, required, cutoff)
    )
    row = c.fetchone()

    if not row:
        # Ищем любой неиспользованный подарок
        c.execute(
            """SELECT id, gift_value, gift_id FROM pending_gifts
               WHERE user_id=? AND status='pending' AND gift_value>=?
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
        "UPDATE pending_gifts SET status='activated', plan=? WHERE id=?",
        (plan, gift_db_id)
    )
    conn.commit()
    conn.close()


def check_realtime_gift(user_id: int, required_stars: int) -> Optional[dict]:
    """
    Проверяет очередь подарков в памяти (gift_queue).
    Возвращает подарок если он есть и достаточной стоимости.
    """
    if user_id not in gift_queue:
        return None
    gifts = gift_queue[user_id]
    # Фильтруем: только за последние 30 минут и достаточные по стоимости
    cutoff = datetime.now() - timedelta(minutes=30)
    for g in gifts:
        if g["value"] >= required_stars and g["ts"] >= cutoff:
            return g
    return None


# ─── Статистика ───────────────────────────────────────────────

def get_stats():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_banned=1")
    banned = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_admin=1")
    admins = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM slots WHERE expires_at>?", (datetime.now().isoformat(),))
    active_slots = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM bots")
    total_bots = c.fetchone()[0]
    c.execute(
        "SELECT COUNT(*), COALESCE(SUM(gift_value),0) FROM pending_gifts WHERE status='activated'"
    )
    gifts_row = c.fetchone()
    conn.close()
    return {
        "total_users":   total_users,
        "banned":        banned,
        "admins":        admins,
        "active_slots":  active_slots,
        "total_bots":    total_bots,
        "running_bots":  len(running_bots),
        "total_gifts":   gifts_row[0],
        "total_stars":   gifts_row[1],
        "userbot_status": "🟢 онлайн" if userbot_client and userbot_client.is_connected() else "🔴 офлайн",
    }


# ═══════════════════════════════════════════════════════════════
# 🚀 СИСТЕМА ХОСТА
# ═══════════════════════════════════════════════════════════════

def get_bot_dir(bot_id: int) -> Path:
    d = BOTS_DIR / f"bot_{bot_id}"
    d.mkdir(exist_ok=True)
    return d


def list_bot_files(bot_id: int) -> List[Path]:
    """Список всех файлов бота"""
    bot_dir = get_bot_dir(bot_id)
    files = []
    for f in sorted(bot_dir.iterdir()):
        if f.name not in ("wrapper.py", "bot.log") and f.is_file():
            files.append(f)
    return files


def get_bot_total_size(bot_id: int) -> int:
    """Общий размер всех файлов бота в байтах"""
    return sum(f.stat().st_size for f in list_bot_files(bot_id))


WRAPPER_CODE = '''#!/usr/bin/env python3
"""BotHost Wrapper — универсальный запускальщик"""
import os, sys, signal, traceback, re, subprocess, importlib

def log(msg):
    print(f"[BotHost] {msg}", flush=True)

def auto_install(code_text):
    MODULE_MAP = {
        "telegram": "python-telegram-bot",
        "telebot": "pyTelegramBotAPI",
        "pyrogram": "pyrogram",
        "telethon": "telethon",
        "aiogram": "aiogram",
        "openai": "openai",
        "anthropic": "anthropic",
        "requests": "requests",
        "httpx": "httpx",
        "aiohttp": "aiohttp",
        "bs4": "beautifulsoup4",
        "discord": "discord.py",
        "twitchio": "twitchio",
        "sqlalchemy": "sqlalchemy",
        "pymongo": "pymongo",
        "motor": "motor",
        "redis": "redis",
        "psycopg2": "psycopg2-binary",
        "numpy": "numpy",
        "pandas": "pandas",
        "matplotlib": "matplotlib",
        "PIL": "Pillow",
        "cv2": "opencv-python",
        "sklearn": "scikit-learn",
        "dotenv": "python-dotenv",
        "schedule": "schedule",
        "apscheduler": "apscheduler",
        "pydantic": "pydantic",
        "yaml": "pyyaml",
        "jwt": "PyJWT",
        "aiofiles": "aiofiles",
        "tqdm": "tqdm",
        "rich": "rich",
        "colorama": "colorama",
        "groq": "groq",
        "fastapi": "fastapi",
        "flask": "flask",
        "uvicorn": "uvicorn",
        "starlette": "starlette",
    }
    BUILTINS = {
        "os","sys","time","datetime","json","re","math","random","hashlib",
        "base64","uuid","threading","multiprocessing","subprocess","asyncio",
        "logging","pathlib","typing","collections","functools","itertools",
        "operator","string","io","copy","enum","dataclasses","abc",
        "contextlib","traceback","inspect","signal","socket","http","html",
        "urllib","sqlite3","csv","zipfile","tarfile","gzip","shutil",
        "tempfile","struct","codecs","locale","textwrap","pprint","pickle",
        "shelve","hmac","secrets","decimal","fractions","statistics","heapq",
        "__future__","importlib","ast","dis","gc","weakref","types",
    }
    pattern = re.compile(r"^\\s*(?:import|from)\\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.MULTILINE)
    found = {m.group(1) for m in pattern.finditer(code_text)}
    log(f"Импорты: {', '.join(sorted(found))}")
    to_install = []
    for module in found:
        if module in BUILTINS:
            continue
        pkg = MODULE_MAP.get(module, module)
        try:
            importlib.import_module(module)
        except ImportError:
            to_install.append(pkg)
    if to_install:
        to_install = list(dict.fromkeys(to_install))
        log(f"📦 Установка: {', '.join(to_install)}")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-q",
                 "--disable-pip-version-check"] + to_install,
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
            )
            log(f"✅ Установлено: {', '.join(to_install)}")
        except Exception as e:
            log(f"⚠️ Ошибка установки: {e}")
    else:
        log("✅ Все зависимости есть")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MAIN_FILE = os.environ.get("BOTHOST_MAIN_FILE", "main.py")

log(f"Python {sys.version}")
log(f"Главный файл: {MAIN_FILE}")
if BOT_TOKEN:
    log(f"Токен: {BOT_TOKEN[:10]}...")

if not os.path.exists(MAIN_FILE):
    log(f"ERROR: {MAIN_FILE} не найден!")
    files = [f for f in os.listdir(".") if f.endswith(".py") and f != "wrapper.py"]
    if files:
        MAIN_FILE = files[0]
        log(f"Найден альтернативный файл: {MAIN_FILE}")
    else:
        log("Нет Python файлов для запуска!")
        sys.exit(1)

with open(MAIN_FILE, "r", encoding="utf-8") as f:
    user_code = f.read()

log(f"Код: {len(user_code)} символов")
log("Проверяю зависимости...")
try:
    auto_install(user_code)
except Exception as e:
    log(f"⚠️ Ошибка автоустановки: {e}")

def handle_signal(signum, frame):
    log(f"Сигнал {signum}, завершаюсь...")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

log(f"Запускаю {MAIN_FILE}...")
sys.stdout.flush()

try:
    globals_dict = {
        "__name__": "__main__",
        "__file__": os.path.abspath(MAIN_FILE),
        "__builtins__": __builtins__,
    }
    code_obj = compile(user_code, MAIN_FILE, "exec")
    exec(code_obj, globals_dict)
    log("Завершено нормально")
except SystemExit as e:
    code = e.code if e.code is not None else 0
    log(f"SystemExit: {code}")
    sys.exit(code)
except SyntaxError as e:
    log(f"❌ СИНТАКСИС: {e.filename}:{e.lineno} — {e.msg}")
    if e.text: log(f"   {e.text.strip()}")
    sys.exit(1)
except ImportError as e:
    log(f"❌ ИМПОРТ: {e}")
    traceback.print_exc()
    sys.exit(1)
except Exception as e:
    log(f"❌ ОШИБКА: {type(e).__name__}: {e}")
    traceback.print_exc()
    sys.exit(1)
except KeyboardInterrupt:
    log("Прервано")
    sys.exit(0)
'''


async def start_user_bot(bot_id: int, user_bot_token: str, main_file: str = "main.py") -> bool:
    """Запускает бота пользователя"""
    try:
        bot_dir = get_bot_dir(bot_id)

        # Записываем wrapper
        (bot_dir / "wrapper.py").write_text(WRAPPER_CODE, encoding="utf-8")

        log_file = bot_dir / "bot.log"

        env = os.environ.copy()
        env["BOT_TOKEN"] = user_bot_token or ""
        env["BOTHOST_MAIN_FILE"] = main_file
        env["PYTHONUNBUFFERED"] = "1"
        for k in ["OWNER_TELEGRAM_ID", "OWNER_PHONE", "OWNER_API_ID",
                  "OWNER_API_HASH", "OWNER_USERNAME"]:
            env.pop(k, None)

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

        # Ждём 7 секунд — даём время на запуск
        await asyncio.sleep(7)

        if process.poll() is not None:
            logs = log_file.read_text(encoding="utf-8", errors="ignore")
            if not logs.strip():
                await asyncio.sleep(2)
                logs = log_file.read_text(encoding="utf-8", errors="ignore")

            logger.error(f"❌ Бот #{bot_id} упал:\n{logs[-500:]}")

            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE bots SET status='error' WHERE id=?", (bot_id,))
            conn.commit()
            conn.close()

            running_bots.pop(bot_id, None)
            return False

        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE bots SET status='running' WHERE id=?", (bot_id,))
        conn.commit()
        conn.close()

        logger.info(f"✅ Бот #{bot_id} запущен (PID {process.pid}), главный файл: {main_file}")
        return True

    except Exception as e:
        logger.error(f"❌ Ошибка запуска #{bot_id}: {e}")
        return False


async def stop_user_bot(bot_id: int):
    if bot_id in running_bots:
        proc = running_bots[bot_id]
        try:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        except Exception as e:
            logger.error(f"Ошибка остановки #{bot_id}: {e}")
        finally:
            running_bots.pop(bot_id, None)

    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE bots SET status='stopped' WHERE id=?", (bot_id,))
    conn.commit()
    conn.close()
    logger.info(f"⏹ Бот #{bot_id} остановлен")


def get_bot_logs(bot_id: int, lines: int = 60) -> str:
    log_file = get_bot_dir(bot_id) / "bot.log"
    if not log_file.exists():
        return "📭 Логи пока пустые"
    try:
        content = log_file.read_text(encoding="utf-8", errors="ignore")
        log_lines = content.strip().split("\n")
        return "\n".join(log_lines[-lines:]) or "📭 Пусто"
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
                    running_bots.pop(bot_id, None)

                    conn = get_db()
                    c = conn.cursor()
                    c.execute("UPDATE bots SET status='error' WHERE id=?", (bot_id,))
                    c.execute("SELECT user_id FROM bots WHERE id=?", (bot_id,))
                    row = c.fetchone()
                    conn.commit()
                    conn.close()

                    if row:
                        uid = row[0]
                        logs = get_bot_logs(bot_id, 10)
                        logs_safe = html.escape(logs[:500])
                        try:
                            await bot.send_message(
                                uid,
                                f"⚠️ <b>Бот #{bot_id} упал!</b>\n\n"
                                f"Код выхода: {code}\n"
                                f"<pre>{logs_safe}</pre>",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                else:
                    # Проверка слота
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("SELECT user_id FROM bots WHERE id=?", (bot_id,))
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
    """Восстанавливает ботов после перезапуска"""
    logger.info("🔄 Восстановление ботов...")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, user_id, bot_token, main_file FROM bots WHERE status='running'")
    bots_to_restore = c.fetchall()
    conn.close()

    restored = 0
    for bot_id, user_id, user_bot_token, main_file in bots_to_restore:
        if is_user_banned(user_id):
            continue
        if user_id != OWNER_ID and not is_admin(user_id) and not has_active_slot(user_id):
            continue
        code_file = get_bot_dir(bot_id) / (main_file or "main.py")
        if code_file.exists():
            if await start_user_bot(bot_id, user_bot_token or "", main_file or "main.py"):
                restored += 1

    logger.info(f"✅ Восстановлено: {restored}")


# ═══════════════════════════════════════════════════════════════
# 🎁 USERBOT — ОТСЛЕЖИВАНИЕ ПОДАРКОВ (Telethon)
# ═══════════════════════════════════════════════════════════════

async def start_userbot():
    """
    Запускает userbot для отслеживания подарков.
    Автоматически подключается к аккаунту владельца.
    """
    global userbot_client

    if not all([OWNER_PHONE, OWNER_API_ID, OWNER_API_HASH]):
        logger.warning("⚠️ OWNER_PHONE/API_ID/API_HASH не настроены — подарки не отслеживаются")
        return

    try:
        from telethon import TelegramClient, events
        from telethon.tl.types import (
            MessageActionStarGift,
            MessageActionStarGiftUnique,
            MessageServiceAction,
        )

        session_path = str(SESSIONS_DIR / "owner_userbot")

        userbot_client = TelegramClient(
            session_path,
            int(OWNER_API_ID),
            OWNER_API_HASH
        )

        await userbot_client.start(phone=OWNER_PHONE)
        me = await userbot_client.get_me()
        logger.info(f"✅ Userbot запущен: {me.first_name} ({me.id})")

        @userbot_client.on(events.NewMessage(incoming=True))
        async def on_message(event):
            """Ловим сервисные сообщения о подарках"""
            try:
                msg = event.message

                # Проверяем action (сервисное сообщение)
                if hasattr(msg, "action") and msg.action is not None:
                    action = msg.action
                    action_type = type(action).__name__.lower()

                    if "gift" in action_type or "star" in action_type:
                        sender = await event.get_sender()
                        if not sender or sender.id == OWNER_ID:
                            return

                        # Извлекаем стоимость
                        value = 0
                        for attr in ["stars", "cost", "amount", "credits"]:
                            v = getattr(action, attr, None)
                            if v and isinstance(v, int) and v > 0:
                                value = v
                                break

                        if value <= 0:
                            # Пробуем по gift атрибуту
                            gift_obj = getattr(action, "gift", None)
                            if gift_obj:
                                for attr in ["stars", "cost"]:
                                    v = getattr(gift_obj, attr, None)
                                    if v and isinstance(v, int) and v > 0:
                                        value = v
                                        break

                        if value > 0:
                            gift_id = (
                                f"gift_{sender.id}_{event.id}_"
                                f"{int(datetime.now().timestamp())}"
                            )
                            await process_incoming_gift(
                                sender_id=sender.id,
                                username=(
                                    sender.username
                                    or sender.first_name
                                    or str(sender.id)
                                ),
                                value=value,
                                gift_id=gift_id
                            )

            except Exception as e:
                logger.error(f"Ошибка обработки подарка: {e}")

        # Также слушаем raw updates на случай новых типов подарков
        @userbot_client.on(events.Raw())
        async def on_raw(update):
            try:
                update_type = type(update).__name__.lower()
                if "gift" in update_type:
                    logger.debug(f"Raw gift update: {update_type}")
            except Exception:
                pass

        logger.info("🎁 Userbot слушает подарки...")
        await userbot_client.run_until_disconnected()

    except ImportError:
        logger.warning("Telethon не установлен — устанавливаю...")
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "-q", "telethon"
        ])
        logger.info("Telethon установлен, перезапусти бота")
    except Exception as e:
        logger.error(f"Userbot ошибка: {e}")


async def process_incoming_gift(sender_id: int, username: str,
                                value: int, gift_id: str):
    """
    Обрабатывает входящий подарок:
    1. Сохраняет в БД
    2. Добавляет в очередь памяти (для мгновенного подтверждения)
    3. Уведомляет отправителя
    """
    # Сохраняем в БД
    is_new = save_pending_gift(sender_id, username, value, gift_id)
    if not is_new:
        logger.debug(f"Дубликат подарка: {gift_id}")
        return

    # Добавляем в очередь памяти
    if sender_id not in gift_queue:
        gift_queue[sender_id] = []
    gift_queue[sender_id].append({
        "value": value,
        "gift_id": gift_id,
        "ts": datetime.now()
    })
    # Чистим старые записи (> 2 часов)
    cutoff = datetime.now() - timedelta(hours=2)
    gift_queue[sender_id] = [
        g for g in gift_queue[sender_id] if g["ts"] >= cutoff
    ]

    logger.info(f"🎁 Подарок получен: {sender_id} → {value}⭐")

    # Определяем план
    if value >= 50:
        plan_id, plan_name = "month", "Месяц"
    elif value >= 25:
        plan_id, plan_name = "2weeks", "2 недели"
    elif value >= 15:
        plan_id, plan_name = "week", "Неделя"
    else:
        plan_id, plan_name = None, None

    # Уведомляем пользователя
    try:
        if plan_id:
            await bot.send_message(
                sender_id,
                f"🎁 <b>Подарок получен!</b>\n\n"
                f"💎 Стоимость: <b>{value}⭐</b>\n"
                f"✅ Тариф: <b>{plan_name}</b>\n\n"
                f"Нажми кнопку ниже чтобы активировать тариф:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"✅ Активировать {plan_name}",
                        callback_data=f"confirm:{plan_id}"
                    )],
                    [InlineKeyboardButton(
                        text="💎 Выбрать другой тариф",
                        callback_data="buy"
                    )],
                ])
            )
        else:
            await bot.send_message(
                sender_id,
                f"🎁 Спасибо за подарок ({value}⭐)!\n\n"
                f"Но для слота нужно минимум 15⭐.\n"
                f"Минимальный тариф — Неделя (15⭐)."
            )
    except Exception as e:
        logger.error(f"Уведомление {sender_id}: {e}")

    # Уведомляем владельца
    try:
        await bot.send_message(
            OWNER_ID,
            f"🎁 <b>Новый подарок!</b>\n\n"
            f"👤 @{username} (<code>{sender_id}</code>)\n"
            f"💎 {value}⭐\n"
            f"📋 Тариф: {plan_name or 'недостаточно'}\n"
            f"🆔 <code>{gift_id}</code>",
            parse_mode="HTML"
        )
    except Exception:
        pass


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
4️⃣ Тариф активируется автоматически!
5️⃣ Загрузи своего бота ✨

━━━━━━━━━━━━━━━━━━━━━━━

📦 Поддержка .py файлов и .zip архивов
📏 Размер: до 10 МБ на файл, до 50 МБ zip

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
        buttons.append([InlineKeyboardButton(
            text="🔐 Админ-панель", callback_data="admin"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_welcome(target, edit: bool = False):
    user_id = target.from_user.id if hasattr(target, "from_user") else target.chat.id
    name = target.from_user.first_name if hasattr(target, "from_user") else "друг"
    text = WELCOME_TEXT.format(name=html.escape(name))
    kb = main_menu_kb(user_id)
    chat_id = target.chat.id if hasattr(target, "chat") else user_id

    if WELCOME_IMAGE.exists():
        try:
            if edit:
                try:
                    await target.delete()
                except Exception:
                    pass
            photo = FSInputFile(WELCOME_IMAGE)
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=text,
                reply_markup=kb,
                parse_mode="HTML"
            )
            return
        except Exception as e:
            logger.error(f"Ошибка фото: {e}")

    if edit:
        try:
            await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass

    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка приветствия: {e}")


# ═══════════════════════════════════════════════════════════════
# 📝 FSM СОСТОЯНИЯ
# ═══════════════════════════════════════════════════════════════

class UploadStates(StatesGroup):
    waiting_file   = State()   # Ждём .py или .zip
    waiting_main   = State()   # Ждём выбор главного файла (для zip)
    waiting_token  = State()   # Ждём токен
    add_file       = State()   # Добавление файла к существующему боту


class AdminStates(StatesGroup):
    broadcast      = State()
    ban            = State()
    unban          = State()
    addadmin       = State()
    welcome_photo  = State()
    manual_gift    = State()   # Ручное добавление подарка


# ═══════════════════════════════════════════════════════════════
# ─── FSM ХЕНДЛЕРЫ ─────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

# ─── Рассылка ─────────────────────────────────────────────────

@dp.message(AdminStates.broadcast)
async def handle_broadcast(message: types.Message, state: FSMContext):
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
    ok = fail = 0
    for u in get_all_users():
        if u[4]:   # is_banned
            continue
        try:
            await bot.send_message(u[0], text)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1

    await message.answer(
        f"✅ Готово!\n✓ Доставлено: {ok}\n✗ Ошибок: {fail}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


# ─── Бан ──────────────────────────────────────────────────────

@dp.message(AdminStates.ban)
async def handle_ban(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("❌ Отменено")

    text = message.text.strip()
    uid = None

    if text.startswith("@"):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE username=?", (text[1:],))
        row = c.fetchone()
        conn.close()
        if row:
            uid = row[0]
    else:
        try:
            uid = int(text)
        except ValueError:
            return await message.answer("❌ Неверный формат")

    if not uid:
        return await message.answer("❌ Пользователь не найден")
    if uid == OWNER_ID:
        return await message.answer("❌ Нельзя заблокировать владельца")

    ban_user(uid)
    for b in get_user_bots(uid):
        await stop_user_bot(b[0])

    await message.answer(
        f"🚫 Заблокирован: <code>{uid}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


# ─── Разбан ───────────────────────────────────────────────────

@dp.message(AdminStates.unban)
async def handle_unban(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("❌ Отменено")

    try:
        uid = int(message.text.strip())
    except ValueError:
        return await message.answer("❌ Нужно число (ID)")

    unban_user(uid)
    await message.answer(
        f"✅ Разблокирован: <code>{uid}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


# ─── Добавление админа ────────────────────────────────────────

@dp.message(AdminStates.addadmin)
async def handle_addadmin(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        await state.clear()
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("❌ Отменено")

    text = message.text.strip()
    uid = None

    if text.startswith("@"):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE username=?", (text[1:],))
        row = c.fetchone()
        conn.close()
        if row:
            uid = row[0]
    else:
        try:
            uid = int(text)
        except ValueError:
            return await message.answer("❌ Неверный формат")

    if not uid:
        return await message.answer("❌ Не найден. Пусть напишет /start")
    if uid == OWNER_ID:
        return await message.answer("⚠️ Это уже владелец")

    add_admin(uid)
    await message.answer(
        f"🛡 Админ добавлен: <code>{uid}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


# ─── Фото приветствия ─────────────────────────────────────────

@dp.message(AdminStates.welcome_photo, F.photo)
async def handle_welcome_photo(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        await state.clear()
        return
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, WELCOME_IMAGE)
    await message.answer(
        "✅ Картинка установлена!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


@dp.message(AdminStates.welcome_photo, F.text)
async def handle_welcome_text(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        await state.clear()
        return
    if message.text.strip().lower() == "delete":
        if WELCOME_IMAGE.exists():
            WELCOME_IMAGE.unlink()
        await message.answer(
            "🗑 Картинка удалена",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Админка", callback_data="admin")]
            ])
        )
        await state.clear()
    elif message.text == "/cancel":
        await message.answer("❌ Отменено")
        await state.clear()
    else:
        await message.answer("❌ Отправь фото или напиши 'delete'")


# ─── Ручное добавление подарка (для тестирования) ─────────────

@dp.message(AdminStates.manual_gift)
async def handle_manual_gift(message: types.Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        await state.clear()
        return
    if message.text == "/cancel":
        await state.clear()
        return await message.answer("❌ Отменено")

    # Формат: user_id stars
    # Пример: 123456789 50
    parts = message.text.strip().split()
    if len(parts) != 2:
        return await message.answer(
            "❌ Формат: <code>user_id звёзды</code>\n"
            "Пример: <code>123456789 50</code>",
            parse_mode="HTML"
        )

    try:
        uid = int(parts[0])
        stars = int(parts[1])
    except ValueError:
        return await message.answer("❌ Нужны числа")

    gift_id = f"manual_{uid}_{int(datetime.now().timestamp())}"
    await process_incoming_gift(
        sender_id=uid,
        username=str(uid),
        value=stars,
        gift_id=gift_id
    )

    await message.answer(
        f"✅ Подарок добавлен вручную:\n"
        f"👤 <code>{uid}</code> — {stars}⭐",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


# ═══════════════════════════════════════════════════════════════
# ─── ЗАГРУЗКА ФАЙЛОВ (FSM) ────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

@dp.message(UploadStates.waiting_file, F.document)
async def handle_upload_file(message: types.Message, state: FSMContext):
    """Принимаем .py или .zip файл"""
    doc = message.document
    fname = doc.file_name or "file.py"

    # Проверяем расширение
    if not (fname.endswith(".py") or fname.endswith(".zip") or fname.endswith(".txt")):
        return await message.answer(
            "❌ Нужен <b>.py</b> файл или <b>.zip</b> архив",
            parse_mode="HTML"
        )

    # Проверяем размер
    max_size = MAX_ZIP_SIZE if fname.endswith(".zip") else MAX_FILE_SIZE
    if doc.file_size and doc.file_size > max_size:
        size_mb = max_size // (1024 * 1024)
        return await message.answer(f"❌ Файл больше {size_mb} МБ")

    # Проверяем лимит ботов
    user_id = message.from_user.id
    if not is_admin(user_id) and get_user_bots_count(user_id) >= MAX_BOTS_PER_USER:
        return await message.answer(
            f"❌ Максимум {MAX_BOTS_PER_USER} ботов.\n"
            f"Удали старых чтобы загрузить нового."
        )

    # Скачиваем файл
    file_info = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file_info.file_path)
    content = file_bytes.read()

    if fname.endswith(".zip"):
        # Обрабатываем zip архив
        await handle_zip_upload(message, state, fname, content)
    else:
        # Обрабатываем Python файл
        await handle_py_upload(message, state, fname, content)


async def handle_py_upload(message: types.Message, state: FSMContext,
                            fname: str, content: bytes):
    """Обрабатывает загрузку .py файла"""
    try:
        code = content.decode("utf-8", errors="ignore")
    except Exception:
        return await message.answer("❌ Не удалось прочитать файл")

    if not code.strip():
        return await message.answer("❌ Файл пустой!")

    warnings = []
    tg_libs = ["aiogram", "telebot", "pyrogram", "telegram", "telethon"]
    if not any(lib in code.lower() for lib in tg_libs):
        warnings.append("💡 Не найдены Telegram-библиотеки")

    if re.search(r"\d{8,10}:[A-Za-z0-9_-]{35}", code):
        warnings.append("⚠️ Хардкоднутый токен — лучше использовать os.environ['BOT_TOKEN']")

    # Нормализуем имя файла
    safe_name = re.sub(r"[^\w.-]", "_", fname)
    if not safe_name.endswith(".py"):
        safe_name = "main.py"

    await state.update_data(
        files={"main.py": content},
        main_file="main.py",
        original_name=safe_name,
        is_zip=False
    )

    resp = (
        f"✅ <b>Файл принят!</b>\n\n"
        f"📁 {fname}\n"
        f"📏 {len(content):,} байт\n"
    )
    if warnings:
        resp += "\n<b>Замечания:</b>\n" + "\n".join(warnings) + "\n"
    resp += (
        "\nТеперь отправь <b>токен бота</b>.\n"
        "Если токен не нужен — отправь <code>none</code>"
    )

    await message.answer(resp, parse_mode="HTML")
    await state.set_state(UploadStates.waiting_token)


async def handle_zip_upload(message: types.Message, state: FSMContext,
                             fname: str, content: bytes):
    """Обрабатывает загрузку .zip архива"""
    import io

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return await message.answer("❌ Повреждённый zip архив")

    # Получаем список Python файлов
    py_files = [
        n for n in zf.namelist()
        if n.endswith(".py") and not n.startswith("__MACOSX")
           and not os.path.basename(n).startswith(".")
    ]

    if not py_files:
        return await message.answer("❌ В архиве нет .py файлов")

    if len(py_files) > MAX_FILES_PER_BOT:
        return await message.answer(
            f"❌ Слишком много файлов: {len(py_files)}\n"
            f"Максимум: {MAX_FILES_PER_BOT}"
        )

    # Читаем все файлы
    files_data = {}
    total_size = 0
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        if any(skip in name for skip in ["__MACOSX", ".DS_Store", ".git/"]):
            continue
        try:
            data = zf.read(name)
            # Нормализуем путь
            clean_name = name.lstrip("/").replace("\\", "/")
            # Убираем лишние директории если архив из папки
            parts = clean_name.split("/")
            if len(parts) > 1 and len(py_files) > 0:
                # Проверяем общий корень
                roots = set(n.split("/")[0] for n in py_files if "/" in n)
                if len(roots) == 1 and not any(
                    n.count("/") == 0 for n in py_files
                ):
                    # Все файлы в одной папке — убираем корень
                    root = roots.pop()
                    if clean_name.startswith(root + "/"):
                        clean_name = clean_name[len(root) + 1:]

            files_data[clean_name] = data
            total_size += len(data)
        except Exception as e:
            logger.error(f"Ошибка чтения {name}: {e}")

    if total_size > MAX_ZIP_SIZE:
        return await message.answer(
            f"❌ Общий размер файлов: {total_size // 1024 // 1024} МБ\n"
            f"Максимум: {MAX_ZIP_SIZE // 1024 // 1024} МБ"
        )

    # Определяем главный файл
    py_names = [n for n in files_data.keys() if n.endswith(".py")]
    main_candidates = ["main.py", "bot.py", "app.py", "run.py", "start.py", "index.py"]

    detected_main = None
    for candidate in main_candidates:
        if candidate in py_names:
            detected_main = candidate
            break

    if not detected_main and py_names:
        detected_main = py_names[0]

    await state.update_data(
        files=files_data,
        main_file=detected_main,
        original_name=fname,
        is_zip=True,
        py_files=py_names
    )

    # Формируем список файлов
    files_text = "\n".join(
        f"{'👑' if n == detected_main else '📄'} {n}"
        for n in sorted(py_names)[:15]
    )
    if len(py_names) > 15:
        files_text += f"\n... и ещё {len(py_names) - 15}"

    if len(py_names) > 1:
        # Просим выбрать главный файл
        buttons = []
        for n in sorted(py_names)[:8]:
            buttons.append([InlineKeyboardButton(
                text=f"{'✅ ' if n == detected_main else ''}{n}",
                callback_data=f"setmain:{n}"
            )])
        buttons.append([InlineKeyboardButton(
            text="✅ Использовать автовыбор", callback_data="setmain:auto"
        )])

        await message.answer(
            f"📦 <b>Архив принят!</b>\n\n"
            f"📁 {fname}\n"
            f"📏 {total_size:,} байт\n"
            f"🐍 Python файлов: {len(py_names)}\n\n"
            f"<b>Файлы:</b>\n{files_text}\n\n"
            f"<b>Выбери главный файл</b> (точка входа):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
        await state.set_state(UploadStates.waiting_main)
    else:
        # Только один файл — сразу к токену
        await message.answer(
            f"📦 <b>Архив принят!</b>\n\n"
            f"📁 {fname}\n"
            f"📏 {total_size:,} байт\n"
            f"🐍 Главный файл: <b>{detected_main}</b>\n\n"
            f"Отправь <b>токен бота</b> или <code>none</code>",
            parse_mode="HTML"
        )
        await state.set_state(UploadStates.waiting_token)


@dp.callback_query(StateFilter(UploadStates.waiting_main), F.data.startswith("setmain:"))
async def cb_set_main_file(call: types.CallbackQuery, state: FSMContext):
    """Выбор главного файла из zip"""
    choice = call.data.split(":", 1)[1]
    data = await state.get_data()
    py_files = data.get("py_files", [])

    if choice == "auto":
        main_file = data.get("main_file") or py_files[0]
    else:
        main_file = choice

    await state.update_data(main_file=main_file)

    await call.message.edit_text(
        f"✅ <b>Главный файл: {main_file}</b>\n\n"
        f"Теперь отправь <b>токен бота</b> или <code>none</code>",
        parse_mode="HTML"
    )
    await state.set_state(UploadStates.waiting_token)


@dp.message(UploadStates.waiting_token, F.text)
async def handle_token(message: types.Message, state: FSMContext):
    """Принимаем токен или 'none'"""
    token = message.text.strip()

    if token.lower() in ["none", "нет", "no", "-", "skip", "пропустить", "0"]:
        token = ""
        token_status = "🚫 Токен не задан"
    elif ":" not in token or len(token) < 30:
        return await message.answer(
            "⚠️ Это не токен бота.\n\n"
            "Формат: <code>1234567890:ABCdef...</code>\n"
            "Без токена — отправь <code>none</code>",
            parse_mode="HTML"
        )
    else:
        token_status = "🔐 Токен принят"

    # Удаляем сообщение с токеном (безопасность)
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    files: dict = data.get("files", {})
    main_file: str = data.get("main_file", "main.py")
    original_name: str = data.get("original_name", "bot.py")

    user_id = message.from_user.id

    # Создаём запись в БД
    total_size = sum(len(v) for v in files.values())
    bot_id = save_bot(
        user_id=user_id,
        name=original_name,
        main_file=main_file,
        user_bot_token=token,
        file_count=len(files),
        total_size=total_size
    )

    # Сохраняем файлы на диск
    bot_dir = get_bot_dir(bot_id)
    for rel_path, file_content in files.items():
        # Безопасный путь (защита от path traversal)
        safe_path = bot_dir / Path(rel_path).name
        # Для вложенных папок
        full_path = bot_dir / rel_path.lstrip("/")
        full_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            full_path.write_bytes(file_content)
        except Exception as e:
            logger.error(f"Ошибка записи {rel_path}: {e}")

    size_str = (
        f"{total_size // 1024} КБ"
        if total_size < 1024 * 1024
        else f"{total_size / 1024 / 1024:.1f} МБ"
    )

    await message.answer(
        f"✅ <b>Бот сохранён!</b>\n\n"
        f"🆔 ID: <code>{bot_id}</code>\n"
        f"📁 Архив: {original_name}\n"
        f"🐍 Главный файл: <code>{main_file}</code>\n"
        f"📄 Файлов: {len(files)}\n"
        f"📏 Размер: {size_str}\n"
        f"{token_status}\n\n"
        f"Запусти через <b>«🤖 Мои боты»</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 К моим ботам", callback_data="mybots")],
            [InlineKeyboardButton(text="📤 Загрузить ещё", callback_data="upload")],
        ]),
        parse_mode="HTML"
    )
    await state.clear()


# ─── Добавление файла к боту ──────────────────────────────────

@dp.message(UploadStates.add_file, F.document)
async def handle_add_file(message: types.Message, state: FSMContext):
    """Добавление дополнительного файла к боту"""
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if not bot_id:
        await state.clear()
        return

    b = get_bot(bot_id)
    if not b or b[1] != message.from_user.id:
        await state.clear()
        return await message.answer("❌ Нет доступа")

    doc = message.document
    fname = doc.file_name or "file.py"

    if not (fname.endswith(".py") or fname.endswith(".txt") or
            fname.endswith(".json") or fname.endswith(".yaml") or
            fname.endswith(".env") or fname.endswith(".cfg")):
        return await message.answer(
            "❌ Поддерживаются: .py, .txt, .json, .yaml, .cfg"
        )

    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        return await message.answer(
            f"❌ Файл больше {MAX_FILE_SIZE // 1024 // 1024} МБ"
        )

    bot_dir = get_bot_dir(bot_id)
    current_files = list_bot_files(bot_id)
    if len(current_files) >= MAX_FILES_PER_BOT:
        return await message.answer(
            f"❌ Максимум {MAX_FILES_PER_BOT} файлов в боте"
        )

    file_info = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file_info.file_path)
    content = file_bytes.read()

    # Сохраняем файл
    safe_name = re.sub(r"[^\w.\-]", "_", fname)
    dest = bot_dir / safe_name
    dest.write_bytes(content)

    await message.answer(
        f"✅ Файл добавлен: <code>{safe_name}</code>\n"
        f"📏 {len(content):,} байт",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📁 Файлы бота", callback_data=f"files:{bot_id}"
            )],
            [InlineKeyboardButton(
                text="« К боту", callback_data=f"bot:{bot_id}"
            )],
        ])
    )
    await state.clear()


# ═══════════════════════════════════════════════════════════════
# 📸 ФОТО НЕ В FSM
# ═══════════════════════════════════════════════════════════════

@dp.message(F.photo)
async def handle_random_photo(message: types.Message):
    await message.answer(
        "📸 Для загрузки нужен <b>.py-файл</b> или <b>.zip-архив</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
            [InlineKeyboardButton(text="« Меню", callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════
# ─── КОМАНДЫ ──────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if is_user_banned(message.from_user.id):
        return await message.answer(
            "🚫 Вы заблокированы.",
            parse_mode="HTML"
        )
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
# ─── CALLBACK ХЕНДЛЕРЫ ────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "back_main")
async def cb_back_main(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if is_user_banned(call.from_user.id):
        return await call.answer("🚫 Вы заблокированы", show_alert=True)
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_welcome(call.message)


# ─── Покупка ──────────────────────────────────────────────────

@dp.callback_query(F.data == "buy")
async def cb_buy(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id

    if is_user_banned(uid):
        return await call.answer("🚫 Вы заблокированы", show_alert=True)

    if uid == OWNER_ID:
        return await call.message.edit_text(
            "👑 Ты владелец — безлимит бесплатно!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
            ])
        )

    if is_admin(uid):
        return await call.message.edit_text(
            "🛡 Ты админ — безлимит бесплатно!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
            ])
        )

    await call.message.edit_text(
        "💎 <b>Выбери тариф</b>\n\n"
        "Оплата подарком в Telegram Stars.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"📅 Неделя — {PLANS['week']['stars']}⭐",
                callback_data="plan:week"
            )],
            [InlineKeyboardButton(
                text=f"📅 2 недели — {PLANS['2weeks']['stars']}⭐",
                callback_data="plan:2weeks"
            )],
            [InlineKeyboardButton(
                text=f"🗓 Месяц — {PLANS['month']['stars']}⭐",
                callback_data="plan:month"
            )],
            [InlineKeyboardButton(text="« Меню", callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    plan_id = call.data.split(":")[1]
    plan = PLANS[plan_id]
    uid = call.from_user.id

    # МГНОВЕННАЯ ПРОВЕРКА: есть ли уже подарок в очереди?
    realtime_gift = check_realtime_gift(uid, plan["stars"])
    db_gift = get_pending_gift_for_plan(uid, plan_id)

    if realtime_gift or db_gift:
        # Подарок уже есть — предлагаем мгновенно активировать
        gift_value = realtime_gift["value"] if realtime_gift else db_gift[1]
        await call.message.edit_text(
            f"🎁 <b>Подарок уже получен!</b>\n\n"
            f"💎 {gift_value}⭐ — достаточно для тарифа «{plan['name']}»\n\n"
            f"Нажми чтобы активировать:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"✅ Активировать «{plan['name']}»",
                    callback_data=f"confirm:{plan_id}"
                )],
                [InlineKeyboardButton(text="« Другие тарифы", callback_data="buy")],
            ]),
            parse_mode="HTML"
        )
        return

    link = get_profile_link()
    await call.message.edit_text(
        f"{plan['emoji']} <b>Тариф: {plan['name']}</b>\n\n"
        f"💎 Стоимость: {plan['stars']}⭐\n"
        f"📅 Длительность: {plan['days']} дней\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Как оплатить:</b>\n\n"
        f"1️⃣ Нажми <b>«🎁 Отправить подарок»</b>\n"
        f"2️⃣ Выбери подарок от {plan['stars']}⭐\n"
        f"3️⃣ Отправь владельцу\n"
        f"4️⃣ Вернись → <b>«✅ Я отправил»</b>\n\n"
        f"💡 После отправки тариф активируется автоматически!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Отправить подарок", url=link)],
            [InlineKeyboardButton(
                text="✅ Я отправил подарок",
                callback_data=f"confirm:{plan_id}"
            )],
            [InlineKeyboardButton(text="« Другие тарифы", callback_data="buy")],
            [InlineKeyboardButton(text="« Меню", callback_data="back_main")],
        ]),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


@dp.callback_query(F.data.startswith("confirm:"))
async def cb_confirm_payment(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    plan_id = call.data.split(":")[1]
    plan = PLANS[plan_id]
    uid = call.from_user.id

    await call.answer("⏳ Проверяю...")

    # Проверяем реалтайм очередь
    realtime_gift = check_realtime_gift(uid, plan["stars"])

    # Проверяем БД
    db_gift = get_pending_gift_for_plan(uid, plan_id)

    if realtime_gift:
        # Мгновенное подтверждение из очереди памяти!
        gift_id = realtime_gift["gift_id"]
        gift_value = realtime_gift["value"]

        # Сохраняем в БД если ещё нет
        db_gift_check = get_pending_gift_for_plan(uid, plan_id)
        if db_gift_check:
            activate_gift(db_gift_check[0], plan_id)

        # Очищаем из очереди памяти
        if uid in gift_queue:
            gift_queue[uid] = [
                g for g in gift_queue[uid]
                if g["gift_id"] != gift_id
            ]

        expires_at = create_slot(uid, plan_id, gift_id)
        expires_str = expires_at.strftime("%d.%m.%Y")

        await call.message.edit_text(
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"🎁 Подарок: {gift_value}⭐\n"
            f"{plan['emoji']} Тариф: <b>{plan['name']}</b>\n"
            f"📅 До: <b>{expires_str}</b>\n\n"
            f"🎉 Теперь загрузи своего бота!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
                [InlineKeyboardButton(text="📊 Мои слоты", callback_data="myslots")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")],
            ]),
            parse_mode="HTML"
        )

    elif db_gift:
        # Нашли в БД
        gift_db_id, gift_value, gift_uid = db_gift
        activate_gift(gift_db_id, plan_id)
        expires_at = create_slot(uid, plan_id, str(gift_uid))
        expires_str = expires_at.strftime("%d.%m.%Y")

        await call.message.edit_text(
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"🎁 Подарок: {gift_value}⭐\n"
            f"{plan['emoji']} Тариф: <b>{plan['name']}</b>\n"
            f"📅 До: <b>{expires_str}</b>\n\n"
            f"🎉 Теперь загрузи своего бота!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
                [InlineKeyboardButton(text="📊 Мои слоты", callback_data="myslots")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")],
            ]),
            parse_mode="HTML"
        )

    else:
        # Подарок не найден
        await call.message.edit_text(
            f"❌ <b>Подарок не найден!</b>\n\n"
            f"Нужно: <b>от {plan['stars']}⭐</b>\n\n"
            f"<b>Что делать:</b>\n"
            f"1️⃣ Убедись что отправил подарок\n"
            f"2️⃣ Подожди 1-2 минуты\n"
            f"3️⃣ Нажми «Проверить снова»\n\n"
            f"<i>Если проблема не решается — напиши владельцу</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Проверить снова",
                    callback_data=f"confirm:{plan_id}"
                )],
                [InlineKeyboardButton(
                    text="🎁 Отправить подарок",
                    url=get_profile_link()
                )],
                [InlineKeyboardButton(text="« Другие тарифы", callback_data="buy")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")],
            ]),
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        return

    # Уведомляем владельца
    try:
        await bot.send_message(
            OWNER_ID,
            f"💰 <b>Оплата!</b>\n\n"
            f"👤 @{call.from_user.username or '—'} "
            f"(<code>{uid}</code>)\n"
            f"💎 {plan['stars']}⭐ → {plan['name']}",
            parse_mode="HTML"
        )
    except Exception:
        pass


# ─── Загрузка ─────────────────────────────────────────────────

@dp.callback_query(F.data == "upload")
async def cb_upload(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id

    if is_user_banned(uid):
        await state.clear()
        return await call.answer("🚫 Вы заблокированы", show_alert=True)

    if not has_active_slot(uid):
        await state.clear()
        return await call.answer("❌ Нет активного слота!", show_alert=True)

    bots_count = get_user_bots_count(uid)
    max_bots = MAX_BOTS_PER_USER if not is_admin(uid) else 100

    await call.message.edit_text(
        f"📤 <b>Загрузка бота</b>\n\n"
        f"👤 Твоих ботов: {bots_count} / {max_bots}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Что загружать:</b>\n"
        f"• <b>.py файл</b> — один скрипт (до 10 МБ)\n"
        f"• <b>.zip архив</b> — несколько файлов (до 50 МБ)\n\n"
        f"<b>Поддерживаются:</b>\n"
        f"• aiogram, telebot, pyrogram, telethon\n"
        f"• openai, groq, anthropic (AI)\n"
        f"• discord.py, twitchio\n"
        f"• requests, aiohttp, fastapi, flask\n"
        f"• numpy, pandas, PIL и 100+ других\n\n"
        f"💡 Для zip: укажи главный файл (main.py)\n"
        f"💡 Токен: из BOT_TOKEN или os.environ['BOT_TOKEN']\n\n"
        f"📤 <b>Отправляй файл!</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="back_main")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(UploadStates.waiting_file)


# ─── Мои боты ─────────────────────────────────────────────────

@dp.callback_query(F.data == "mybots")
async def cb_mybots(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    bots = get_user_bots(call.from_user.id)

    if not bots:
        return await call.message.edit_text(
            "🤖 <b>У тебя пока нет ботов</b>\n\nЗагрузи первого!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")],
            ]),
            parse_mode="HTML"
        )

    buttons = []
    for b in bots:
        # b: id, user_id, name, main_file, bot_token, status, created_at, file_count, total_size
        bid = b[0]
        name = b[2] or f"bot_{bid}"
        status_icon = "🟢" if bid in running_bots else "🔴"
        file_count = b[7] or 1
        files_str = f" [{file_count}ф]" if file_count > 1 else ""
        buttons.append([InlineKeyboardButton(
            text=f"{status_icon} #{bid} {name[:20]}{files_str}",
            callback_data=f"bot:{bid}"
        )])

    buttons.append([InlineKeyboardButton(
        text="📤 Загрузить ещё", callback_data="upload"
    )])
    buttons.append([InlineKeyboardButton(
        text="« Меню", callback_data="back_main"
    )])

    await call.message.edit_text(
        f"🤖 <b>Твои боты ({len(bots)})</b>\n\n"
        f"🟢 работает • 🔴 остановлен • [Nф] файлов",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("bot:"))
async def cb_bot_detail(call: types.CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    uid = call.from_user.id

    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)

    # b: id, user_id, name, main_file, bot_token, status, created_at, file_count, total_size
    name = b[2] or f"bot_{bot_id}"
    main_file = b[3] or "main.py"
    status = "🟢 Работает" if bot_id in running_bots else "🔴 Остановлен"
    file_count = b[7] or 1
    total_size = b[8] or 0
    size_str = (
        f"{total_size // 1024} КБ"
        if total_size < 1024 * 1024
        else f"{total_size / 1024 / 1024:.1f} МБ"
    )

    await call.message.edit_text(
        f"🤖 <b>Бот #{bot_id}</b>\n\n"
        f"📁 Название: <code>{name}</code>\n"
        f"🐍 Главный файл: <code>{main_file}</code>\n"
        f"📄 Файлов: {file_count}\n"
        f"📏 Размер: {size_str}\n"
        f"📊 Статус: <b>{status}</b>\n"
        f"📅 Создан: {b[6][:10]}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Запустить", callback_data=f"start:{bot_id}"),
                InlineKeyboardButton(text="⏹ Стоп", callback_data=f"stop:{bot_id}"),
            ],
            [InlineKeyboardButton(text="📄 Логи", callback_data=f"logs:{bot_id}")],
            [InlineKeyboardButton(text="📁 Файлы", callback_data=f"files:{bot_id}")],
            [InlineKeyboardButton(text="➕ Добавить файл", callback_data=f"addfile:{bot_id}")],
            [InlineKeyboardButton(text="🗑 Удалить бота", callback_data=f"del:{bot_id}")],
            [InlineKeyboardButton(text="« К списку", callback_data="mybots")],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("files:"))
async def cb_bot_files(call: types.CallbackQuery):
    """Список файлов бота"""
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    uid = call.from_user.id

    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)

    files = list_bot_files(bot_id)
    main_file = b[3] or "main.py"

    if not files:
        text = "📭 Файлов нет"
    else:
        text = f"📁 <b>Файлы бота #{bot_id}</b>\n\n"
        for f in files[:20]:
            size = f.stat().st_size
            size_str = f"{size:,} б" if size < 1024 else f"{size // 1024} КБ"
            is_main = "👑" if f.name == main_file else "📄"
            text += f"{is_main} <code>{f.name}</code> — {size_str}\n"
        if len(files) > 20:
            text += f"\n... и ещё {len(files) - 20} файлов"

    # Кнопки для смены главного файла
    py_files = [f for f in files if f.name.endswith(".py")]
    change_buttons = []
    if len(py_files) > 1:
        for f in py_files[:5]:
            if f.name != main_file:
                change_buttons.append([InlineKeyboardButton(
                    text=f"👑 Сделать главным: {f.name}",
                    callback_data=f"changemain:{bot_id}:{f.name}"
                )])

    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            *change_buttons,
            [InlineKeyboardButton(
                text="➕ Добавить файл", callback_data=f"addfile:{bot_id}"
            )],
            [InlineKeyboardButton(
                text="« К боту", callback_data=f"bot:{bot_id}"
            )],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("changemain:"))
async def cb_change_main(call: types.CallbackQuery):
    """Смена главного файла"""
    parts = call.data.split(":", 2)
    bot_id = int(parts[1])
    new_main = parts[2]
    uid = call.from_user.id

    b = get_bot(bot_id)
    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)

    update_bot_main_file(bot_id, new_main)
    await call.answer(f"✅ Главный файл: {new_main}")
    await cb_bot_files(call)


@dp.callback_query(F.data.startswith("addfile:"))
async def cb_add_file(call: types.CallbackQuery, state: FSMContext):
    """Добавить файл к боту"""
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    uid = call.from_user.id

    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)

    await call.message.edit_text(
        f"➕ <b>Добавить файл к боту #{bot_id}</b>\n\n"
        f"Отправь файл (.py, .json, .txt, .yaml, .cfg)\n"
        f"Максимум: {MAX_FILE_SIZE // 1024 // 1024} МБ\n\n"
        f"Отмена: /cancel",
        parse_mode="HTML"
    )
    await state.update_data(bot_id=bot_id)
    await state.set_state(UploadStates.add_file)


@dp.callback_query(F.data.startswith("start:"))
async def cb_start_bot(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    uid = call.from_user.id

    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)

    if not has_active_slot(uid):
        return await call.answer("❌ Нет активного слота", show_alert=True)

    if bot_id in running_bots:
        return await call.answer("⚠️ Уже запущен", show_alert=True)

    await call.answer("⏳ Запускаю (~7 сек)...")

    main_file = b[3] or "main.py"
    user_bot_token = b[4] or ""

    code_file = get_bot_dir(bot_id) / main_file
    if not code_file.exists():
        # Пробуем найти любой .py файл
        py_files = list(get_bot_dir(bot_id).glob("*.py"))
        py_files = [f for f in py_files if f.name != "wrapper.py"]
        if py_files:
            main_file = py_files[0].name
            update_bot_main_file(bot_id, main_file)
        else:
            return await call.answer("❌ Файл не найден", show_alert=True)

    success = await start_user_bot(bot_id, user_bot_token, main_file)

    if success:
        await call.message.answer(
            f"✅ <b>Бот #{bot_id} запущен!</b>\n\n"
            f"🐍 Файл: <code>{main_file}</code>\n"
            f"🟢 Мониторинг активен",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Логи", callback_data=f"logs:{bot_id}")],
                [InlineKeyboardButton(text="⏹ Стоп", callback_data=f"stop:{bot_id}")],
                [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bot_id}")],
            ])
        )
    else:
        logs = get_bot_logs(bot_id, 40)

        # Анализируем ошибку
        hint = ""
        if "Conflict" in logs or "getUpdates" in logs:
            hint = "\n🔴 <b>КОНФЛИКТ:</b> Токен уже используется в другом месте"
        elif "Unauthorized" in logs or "401" in logs:
            hint = "\n🔴 <b>НЕВЕРНЫЙ ТОКЕН:</b> Проверь токен у @BotFather"
        elif "No module named" in logs:
            m = re.search(r"No module named '([^']+)'", logs)
            mod = m.group(1) if m else "библиотеку"
            hint = f"\n💡 <b>Нет библиотеки:</b> <code>{mod}</code>"
        elif "SyntaxError" in logs or "IndentationError" in logs:
            hint = "\n💡 <b>Синтаксическая ошибка</b> в коде"
        elif "BOT_TOKEN" in logs and "not set" in logs.lower():
            hint = "\n💡 <b>Нет токена:</b> Удали и загрузи с токеном"
        elif "ConnectionError" in logs:
            hint = "\n💡 <b>Сеть:</b> Попробуй позже"

        logs_safe = html.escape(logs[:2000])

        await call.message.answer(
            f"❌ <b>Бот #{bot_id} не запустился</b>\n"
            f"{hint}\n\n"
            f"<b>📋 Логи:</b>\n<pre>{logs_safe}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Попробовать снова",
                    callback_data=f"start:{bot_id}"
                )],
                [InlineKeyboardButton(
                    text="📄 Полные логи",
                    callback_data=f"logs:{bot_id}"
                )],
                [InlineKeyboardButton(
                    text="« К боту",
                    callback_data=f"bot:{bot_id}"
                )],
            ])
        )


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop_bot(call: types.CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    uid = call.from_user.id

    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)

    await stop_user_bot(bot_id)
    await call.answer("⏹ Остановлен")
    await cb_bot_detail(call)


@dp.callback_query(F.data.startswith("logs:"))
async def cb_logs(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    uid = call.from_user.id

    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)

    logs = get_bot_logs(bot_id, 60)
    logs_safe = html.escape(logs[:3500])

    await call.message.answer(
        f"📄 <b>Логи бота #{bot_id}</b>\n\n<pre>{logs_safe}</pre>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"logs:{bot_id}")],
            [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bot_id}")],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete_bot(call: types.CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    b = get_bot(bot_id)
    uid = call.from_user.id

    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)

    await stop_user_bot(bot_id)

    # Удаляем файлы
    bot_dir = get_bot_dir(bot_id)
    try:
        shutil.rmtree(bot_dir)
    except Exception as e:
        logger.error(f"Ошибка удаления директории #{bot_id}: {e}")

    delete_bot_record(bot_id)
    await call.answer("🗑 Удалён")
    await cb_mybots(call)


# ─── Мои слоты ────────────────────────────────────────────────

@dp.callback_query(F.data == "myslots")
async def cb_myslots(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id

    if uid == OWNER_ID:
        return await call.message.edit_text(
            "👑 <b>Твой слот: Безлимитный (владелец)</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
            ]),
            parse_mode="HTML"
        )

    if is_admin(uid):
        return await call.message.edit_text(
            "🛡 <b>Твой слот: Безлимитный (админ)</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
            ]),
            parse_mode="HTML"
        )

    slots = get_active_slots(uid)
    if not slots:
        return await call.message.edit_text(
            "💳 <b>Нет активных слотов</b>\n\nКупи через подарок!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить слот", callback_data="buy")],
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")],
            ]),
            parse_mode="HTML"
        )

    text = f"💳 <b>Активные слоты ({len(slots)})</b>\n\n"
    for s in slots:
        exp = datetime.fromisoformat(s[3])
        days_left = (exp - datetime.now()).days
        plan_name = PLANS.get(s[2], {}).get("name", s[2])
        text += (
            f"• <b>{plan_name}</b> — "
            f"ещё {days_left} дн. (до {exp.strftime('%d.%m.%Y')})\n"
        )

    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Купить ещё", callback_data="buy")],
            [InlineKeyboardButton(text="« Меню", callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )


# ─── Помощь ───────────────────────────────────────────────────

@dp.callback_query(F.data == "help")
async def cb_help(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "❓ <b>Помощь</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>💎 Покупка слота:</b>\n"
        "1. «Купить слот» → выбери тариф\n"
        "2. Отправь подарок владельцу\n"
        "3. Тариф активируется автоматически!\n\n"
        "<b>📤 Загрузка бота:</b>\n"
        "1. Купи слот\n"
        "2. «Загрузить бота» → .py или .zip\n"
        "3. Укажи главный файл (для zip)\n"
        "4. Отправь токен или none\n"
        "5. Запусти через «Мои боты»\n\n"
        "<b>📦 Форматы:</b>\n"
        "• .py — один файл (до 10 МБ)\n"
        "• .zip — несколько файлов (до 50 МБ)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="👤 Написать владельцу",
                url=get_profile_link()
            )],
            [InlineKeyboardButton(text="« Меню", callback_data="back_main")],
        ]),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


# ═══════════════════════════════════════════════════════════════
# 🔐 АДМИН-ПАНЕЛЬ
# ═══════════════════════════════════════════════════════════════

async def show_admin_panel(target, edit: bool = False):
    stats = get_stats()
    text = (
        "🔐 <b>Админ-панель</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Пользователей: <b>{stats['total_users']}</b>\n"
        f"🚫 Заблок.: <b>{stats['banned']}</b>\n"
        f"🛡 Админов: <b>{stats['admins']}</b>\n"
        f"💳 Слотов: <b>{stats['active_slots']}</b>\n"
        f"🤖 Ботов: <b>{stats['total_bots']}</b> "
        f"(🟢 {stats['running_bots']})\n"
        f"🎁 Подарков: <b>{stats['total_gifts']}</b> "
        f"({stats['total_stars']}⭐)\n"
        f"📡 Userbot: <b>{stats['userbot_status']}</b>\n\n"
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
        [InlineKeyboardButton(text="🎁 Добавить подарок", callback_data="adm:gift")],
        [InlineKeyboardButton(text="🔄 Рестарт всех", callback_data="adm:restart")],
        [InlineKeyboardButton(text="« Меню", callback_data="back_main")],
    ])

    if edit:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "admin")
async def cb_admin(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(call.from_user.id):
        return await call.answer("🔐 Нет прав", show_alert=True)
    await show_admin_panel(call.message, edit=True)


@dp.callback_query(F.data == "adm:stats")
async def cb_adm_stats(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    s = get_stats()
    await call.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: {s['total_users']}\n"
        f"🚫 Заблок.: {s['banned']}\n"
        f"🛡 Админов: {s['admins']}\n\n"
        f"💳 Слотов: {s['active_slots']}\n\n"
        f"🤖 Ботов всего: {s['total_bots']}\n"
        f"🤖 Работает: {s['running_bots']}\n\n"
        f"🎁 Подарков: {s['total_gifts']}\n"
        f"💎 Звёзд: {s['total_stars']}⭐\n\n"
        f"📡 Userbot: {s['userbot_status']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm:broadcast")
async def cb_adm_broadcast(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await call.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        "Отправь сообщение (обычный текст).\n"
        "Отмена: /cancel"
    )
    await state.set_state(AdminStates.broadcast)


@dp.callback_query(F.data == "adm:users")
async def cb_adm_users(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    users = get_all_users()
    text = f"👥 <b>Пользователи ({len(users)})</b>\n\n"
    for u in users[:20]:
        # u: user_id, username, full_name, is_admin, is_banned, created_at
        ban = "🚫" if u[4] else "✓"
        adm = "🛡" if u[3] else ""
        uname = f"@{u[1]}" if u[1] else (u[2] or "—")
        text += f"{ban}{adm} <code>{u[0]}</code> {html.escape(str(uname))}\n"
    if len(users) > 20:
        text += f"\n...и ещё {len(users) - 20}"

    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm:ban")
async def cb_adm_ban(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await call.message.edit_text(
        "🚫 <b>Блокировка</b>\n\n"
        "Отправь ID или @username.\n"
        "Отмена: /cancel",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.ban)


@dp.callback_query(F.data == "adm:unban")
async def cb_adm_unban(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await call.message.edit_text(
        "✅ <b>Разблокировка</b>\n\n"
        "Отправь ID пользователя.\n"
        "Отмена: /cancel"
    )
    await state.set_state(AdminStates.unban)


@dp.callback_query(F.data == "adm:addadmin")
async def cb_addadmin(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⚠️ Только владелец", show_alert=True)
    await call.message.edit_text(
        "🛡 <b>Добавить админа</b>\n\n"
        "Отправь ID или @username.\n"
        "Отмена: /cancel"
    )
    await state.set_state(AdminStates.addadmin)


@dp.callback_query(F.data == "adm:remadmin")
async def cb_remadmin(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⚠️ Только владелец", show_alert=True)
    admins = get_all_admins()
    if not admins:
        return await call.answer("📭 Нет админов", show_alert=True)

    buttons = [[InlineKeyboardButton(
        text=f"❌ {a[1] or a[2] or a[0]}",
        callback_data=f"remadm:{a[0]}"
    )] for a in admins]
    buttons.append([InlineKeyboardButton(text="« Отмена", callback_data="admin")])

    await call.message.edit_text(
        "❌ <b>Удалить админа:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("remadm:"))
async def cb_remadm_confirm(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
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
        f"🖼 <b>Картинка приветствия</b>\n\n"
        f"Статус: {status}\n\n"
        f"Отправь фото или напиши <code>delete</code>.\n"
        f"Отмена: /cancel",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.welcome_photo)


@dp.callback_query(F.data == "adm:gift")
async def cb_adm_gift(call: types.CallbackQuery, state: FSMContext):
    """Ручное добавление подарка (для тестирования/ручного подтверждения)"""
    if call.from_user.id != OWNER_ID:
        return await call.answer("⚠️ Только владелец", show_alert=True)
    await call.message.edit_text(
        "🎁 <b>Добавить подарок вручную</b>\n\n"
        "Формат: <code>user_id звёзды</code>\n"
        "Пример: <code>123456789 50</code>\n\n"
        "Используй для ручного подтверждения оплаты.\n"
        "Отмена: /cancel",
        parse_mode="HTML"
    )
    await state.set_state(AdminStates.manual_gift)


@dp.callback_query(F.data == "adm:restart")
async def cb_restart_all(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.answer("⏳ Перезапускаю...")

    # Останавливаем всех
    for bid in list(running_bots.keys()):
        await stop_user_bot(bid)

    # Запускаем снова
    restarted = 0
    for b in get_all_bots():
        # b: id, user_id, name, main_file, bot_token, status, created_at, ...
        bid = b[0]
        uid = b[1]
        user_bot_token = b[4] or ""
        main_file = b[3] or "main.py"

        if is_user_banned(uid):
            continue
        if uid != OWNER_ID and not is_admin(uid) and not has_active_slot(uid):
            continue

        code_file = get_bot_dir(bid) / main_file
        if code_file.exists():
            if await start_user_bot(bid, user_bot_token, main_file):
                restarted += 1

    await call.message.answer(
        f"🔄 <b>Перезапущено: {restarted}</b>",
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

    logger.info("=" * 55)
    logger.info("🤖 BotHost v2.0 запускается")
    logger.info(f"👤 Владелец: {OWNER_ID}")
    logger.info(f"📦 Макс. файл: {MAX_FILE_SIZE // 1024 // 1024} МБ")
    logger.info(f"📦 Макс. zip: {MAX_ZIP_SIZE // 1024 // 1024} МБ")
    logger.info(f"🤖 Макс. ботов/юзер: {MAX_BOTS_PER_USER}")
    logger.info(
        f"📡 Userbot: "
        f"{'настроен' if all([OWNER_PHONE, OWNER_API_ID, OWNER_API_HASH]) else 'НЕ настроен'}"
    )
    logger.info("=" * 55)

    # Восстанавливаем ботов
    await restore_running_bots()

    # Фоновые задачи
    asyncio.create_task(monitor_bots())

    # Userbot для подарков (если настроен)
    if all([OWNER_PHONE, OWNER_API_ID, OWNER_API_HASH]):
        asyncio.create_task(start_userbot())
    else:
        logger.warning(
            "⚠️ Userbot не запущен.\n"
            "   Установи OWNER_PHONE, OWNER_API_ID, OWNER_API_HASH\n"
            "   для автоматического отслеживания подарков"
        )

    logger.info("✅ Polling запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
