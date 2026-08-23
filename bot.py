"""
🤖 BotHost v3.2 — StringSession + ручное подтверждение оплаты
✅ Userbot через готовую StringSession (без кодов)
✅ Ручное подтверждение оплаты администратором
✅ Мгновенное автоподтверждение если userbot видит подарок
✅ Большие файлы (10MB/.py, 50MB/.zip)
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
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ═══════════════════════════════════════════════════════════════
# 🔧 КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN      = "8711311188:AAHnhjvLhyYASMxUI-1hLyktHXhSsmYXnww"
OWNER_ID       = int(os.environ["8269807543"])
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "")
OWNER_PHONE    = os.environ.get("OWNER_PHONE", "")
OWNER_API_ID   = os.environ.get("OWNER_API_ID", "")
OWNER_API_HASH = os.environ.get("OWNER_API_HASH", "")
OWNER_SESSION  = os.environ.get("OWNER_SESSION", "")   # ← StringSession строка

DATA_DIR     = Path("./data")
BOTS_DIR     = DATA_DIR / "bots"
DB_PATH      = DATA_DIR / "bot.db"
WELCOME_IMG  = DATA_DIR / "welcome.jpg"

DATA_DIR.mkdir(exist_ok=True)
BOTS_DIR.mkdir(exist_ok=True)

MAX_PY_SIZE       = 10 * 1024 * 1024
MAX_ZIP_SIZE      = 50 * 1024 * 1024
MAX_FILES_PER_BOT = 20
MAX_BOTS_PER_USER = 10

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
log = logging.getLogger("BotHost")

bot          = Bot(token=BOT_TOKEN)
dp           = Dispatcher(storage=MemoryStorage())
running_bots: Dict[int, subprocess.Popen] = {}
gift_queue:   Dict[int, List[dict]]        = {}

userbot_status = {
    "connected": False,
    "phone":     OWNER_PHONE or "не задан",
    "client":    None,
}


# ═══════════════════════════════════════════════════════════════
# 💾 БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            full_name  TEXT,
            is_admin   INTEGER DEFAULT 0,
            is_banned  INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS slots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            plan       TEXT,
            expires_at TEXT,
            created_at TEXT,
            gift_id    TEXT
        );
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
        );
        CREATE TABLE IF NOT EXISTS gifts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            username    TEXT,
            stars       INTEGER,
            gift_id     TEXT UNIQUE,
            status      TEXT DEFAULT 'pending',
            plan        TEXT,
            created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS manual_payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            plan        TEXT,
            stars       INTEGER,
            note        TEXT,
            created_at  TEXT,
            approved_by INTEGER
        );
    """)
    conn.commit()
    conn.close()
    log.info("💾 БД готова")


def db():
    return sqlite3.connect(DB_PATH)


# ─── Users ────────────────────────────────────────────────────

def upsert_user(uid: int, username: str, full_name: str = ""):
    with db() as conn:
        conn.execute(
            "INSERT INTO users(user_id,username,full_name,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,"
            "full_name=excluded.full_name",
            (uid, username, full_name, datetime.now().isoformat())
        )


def is_banned(uid: int) -> bool:
    if uid == OWNER_ID:
        return False
    with db() as conn:
        r = conn.execute(
            "SELECT is_banned FROM users WHERE user_id=?", (uid,)
        ).fetchone()
    return bool(r and r[0])


def is_admin(uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    with db() as conn:
        r = conn.execute(
            "SELECT is_admin FROM users WHERE user_id=?", (uid,)
        ).fetchone()
    return bool(r and r[0])


def set_banned(uid: int, val: int):
    with db() as conn:
        conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (val, uid))


def set_admin(uid: int, val: int):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(user_id,created_at) VALUES(?,?)",
            (uid, datetime.now().isoformat())
        )
        conn.execute("UPDATE users SET is_admin=? WHERE user_id=?", (val, uid))


def all_users():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        ).fetchall()


def all_admins():
    with db() as conn:
        return conn.execute(
            "SELECT user_id,username,full_name FROM users "
            "WHERE is_admin=1 AND user_id!=?", (OWNER_ID,)
        ).fetchall()


# ─── Slots ────────────────────────────────────────────────────

def has_slot(uid: int) -> bool:
    if uid == OWNER_ID or is_admin(uid):
        return True
    with db() as conn:
        r = conn.execute(
            "SELECT COUNT(*) FROM slots WHERE user_id=? AND expires_at>?",
            (uid, datetime.now().isoformat())
        ).fetchone()
    return r[0] > 0


def active_slots(uid: int):
    if uid == OWNER_ID:
        return [(0, uid, "owner", "2099-12-31", "", "")]
    if is_admin(uid):
        return [(0, uid, "admin", "2099-12-31", "", "")]
    with db() as conn:
        return conn.execute(
            "SELECT * FROM slots WHERE user_id=? AND expires_at>?",
            (uid, datetime.now().isoformat())
        ).fetchall()


def add_slot(uid: int, plan: str, gift_id: str = "") -> datetime:
    exp = datetime.now() + timedelta(days=PLANS[plan]["days"])
    with db() as conn:
        conn.execute(
            "INSERT INTO slots(user_id,plan,expires_at,created_at,gift_id) "
            "VALUES(?,?,?,?,?)",
            (uid, plan, exp.isoformat(), datetime.now().isoformat(), gift_id)
        )
    return exp


# ─── Bots ─────────────────────────────────────────────────────

def save_bot(uid: int, name: str, main_file: str, token: str,
             file_count: int = 1, total_size: int = 0) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO bots(user_id,name,main_file,bot_token,"
            "created_at,file_count,total_size) VALUES(?,?,?,?,?,?,?)",
            (uid, name, main_file, token,
             datetime.now().isoformat(), file_count, total_size)
        )
        return cur.lastrowid


def get_bot(bot_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bots WHERE id=?", (bot_id,)
        ).fetchone()


def user_bots(uid: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bots WHERE user_id=?", (uid,)
        ).fetchall()


def all_bots():
    with db() as conn:
        return conn.execute("SELECT * FROM bots").fetchall()


def del_bot(bot_id: int):
    with db() as conn:
        conn.execute("DELETE FROM bots WHERE id=?", (bot_id,))


def update_bot_status(bot_id: int, status: str):
    with db() as conn:
        conn.execute("UPDATE bots SET status=? WHERE id=?", (status, bot_id))


def update_main_file(bot_id: int, main_file: str):
    with db() as conn:
        conn.execute("UPDATE bots SET main_file=? WHERE id=?", (main_file, bot_id))


# ─── Gifts ────────────────────────────────────────────────────

def save_gift(uid: int, username: str, stars: int, gift_id: str) -> bool:
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO gifts(user_id,username,stars,gift_id,created_at) "
                "VALUES(?,?,?,?,?)",
                (uid, username, stars, gift_id, datetime.now().isoformat())
            )
        return True
    except sqlite3.IntegrityError:
        return False


def find_gift(uid: int, min_stars: int) -> Optional[tuple]:
    with db() as conn:
        cutoff = (datetime.now() - timedelta(minutes=60)).isoformat()
        r = conn.execute(
            "SELECT id,stars,gift_id FROM gifts "
            "WHERE user_id=? AND status='pending' AND stars>=? "
            "AND created_at>=? ORDER BY created_at DESC LIMIT 1",
            (uid, min_stars, cutoff)
        ).fetchone()
        if not r:
            r = conn.execute(
                "SELECT id,stars,gift_id FROM gifts "
                "WHERE user_id=? AND status='pending' AND stars>=? "
                "ORDER BY created_at DESC LIMIT 1",
                (uid, min_stars)
            ).fetchone()
    return r


def use_gift(gift_db_id: int, plan: str):
    with db() as conn:
        conn.execute(
            "UPDATE gifts SET status='used', plan=? WHERE id=?",
            (plan, gift_db_id)
        )


def get_stats():
    with db() as conn:
        tu  = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        ban = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
        adm = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]
        sl  = conn.execute(
            "SELECT COUNT(*) FROM slots WHERE expires_at>?",
            (datetime.now().isoformat(),)
        ).fetchone()[0]
        tb  = conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
        gr  = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(stars),0) FROM gifts WHERE status='used'"
        ).fetchone()
        mp  = conn.execute(
            "SELECT COUNT(*) FROM manual_payments"
        ).fetchone()[0]
    return dict(
        total_users=tu, banned=ban, admins=adm,
        active_slots=sl, total_bots=tb,
        running=len(running_bots),
        gifts=gr[0], stars=gr[1],
        manual_payments=mp,
        ub_ok=userbot_status["connected"],
        ub_phone=userbot_status["phone"],
    )


# ═══════════════════════════════════════════════════════════════
# 🎁 USERBOT — TELETHON (StringSession)
# ═══════════════════════════════════════════════════════════════

def _extract_stars(action) -> int:
    for attr in ["stars", "cost", "amount", "credits", "count"]:
        v = getattr(action, attr, None)
        if isinstance(v, int) and v > 0:
            return v
    gift_obj = getattr(action, "gift", None)
    if gift_obj:
        for attr in ["stars", "cost", "amount"]:
            v = getattr(gift_obj, attr, None)
            if isinstance(v, int) and v > 0:
                return v
    data_str = str(action)
    for pattern in [r"stars=(\d+)", r"cost=(\d+)", r"amount=(\d+)"]:
        m = re.search(pattern, data_str)
        if m:
            v = int(m.group(1))
            if v > 0:
                return v
    return 0


async def gift_received(sender_id: int, username: str, stars: int, gift_id: str):
    """Обрабатывает входящий подарок от userbot."""
    if not save_gift(sender_id, username, stars, gift_id):
        return  # дубликат

    # В очередь памяти для мгновенного подтверждения
    if sender_id not in gift_queue:
        gift_queue[sender_id] = []
    gift_queue[sender_id].append({
        "value": stars, "gift_id": gift_id, "ts": datetime.now()
    })
    cutoff = datetime.now() - timedelta(hours=2)
    gift_queue[sender_id] = [
        g for g in gift_queue[sender_id] if g["ts"] >= cutoff
    ]

    log.info(f"🎁 Подарок: {sender_id} (@{username}) → {stars}⭐")

    # Определяем подходящий план
    plan_id = None
    for pid, p in sorted(PLANS.items(), key=lambda x: x[1]["stars"], reverse=True):
        if stars >= p["stars"]:
            plan_id = pid
            break

    # Уведомляем клиента с кнопкой активации
    try:
        if plan_id:
            plan = PLANS[plan_id]
            await bot.send_message(
                sender_id,
                f"🎁 <b>Подарок получен!</b>\n\n"
                f"💎 {stars}⭐ — тариф <b>«{plan['name']}»</b>\n\n"
                f"Нажми чтобы активировать:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"✅ Активировать «{plan['name']}»",
                        callback_data=f"confirm:{plan_id}"
                    )],
                    [InlineKeyboardButton(
                        text="💎 Другой тариф",
                        callback_data="buy"
                    )],
                ])
            )
        else:
            await bot.send_message(
                sender_id,
                f"🎁 Получили {stars}⭐, но минимум для слота — "
                f"{PLANS['week']['stars']}⭐"
            )
    except Exception as e:
        log.error(f"Уведомление {sender_id}: {e}")

    # Уведомляем владельца
    try:
        await bot.send_message(
            OWNER_ID,
            f"🎁 <b>Новый подарок!</b>\n"
            f"👤 @{username} (<code>{sender_id}</code>)\n"
            f"💎 {stars}⭐ → "
            f"{PLANS[plan_id]['name'] if plan_id else 'недостаточно'}\n\n"
            f"<i>Подарок зарегистрирован автоматически</i>",
            parse_mode="HTML"
        )
    except Exception:
        pass


async def run_userbot():
    """Запускает userbot через StringSession — без кодов авторизации."""
    global userbot_status

    if not all([OWNER_API_ID, OWNER_API_HASH, OWNER_SESSION]):
        log.warning(
            "⚠️ Userbot не запущен.\n"
            "   Нужны переменные: OWNER_API_ID, OWNER_API_HASH, OWNER_SESSION\n"
            "   Создай сессию скриптом make_session.py"
        )
        return

    # Устанавливаем telethon если нет
    try:
        import telethon
    except ImportError:
        log.info("📦 Устанавливаю telethon...")
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "-q", "telethon"
        ])

    from telethon import TelegramClient, events
    from telethon.sessions import StringSession

    while True:
        try:
            client = TelegramClient(
                StringSession(OWNER_SESSION),
                int(OWNER_API_ID),
                OWNER_API_HASH,
                device_model="BotHost Server",
                system_version="Linux x64",
                app_version="1.0"
            )
            userbot_status["client"] = client

            await client.connect()

            if not await client.is_user_authorized():
                log.error("❌ OWNER_SESSION недействителен!")
                try:
                    await bot.send_message(
                        OWNER_ID,
                        "❌ <b>OWNER_SESSION недействителен!</b>\n\n"
                        "Создай новую сессию скриптом:\n"
                        "<pre>python make_session.py</pre>\n"
                        "И обнови переменную OWNER_SESSION в Railway.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                return  # Не перезапускаем — сессия невалидна

            me = await client.get_me()
            userbot_status["connected"] = True
            userbot_status["phone"] = f"{me.first_name} ({me.phone or OWNER_PHONE})"

            log.info(f"✅ Userbot: {me.first_name} ({me.id})")

            try:
                await bot.send_message(
                    OWNER_ID,
                    f"✅ <b>Userbot подключён!</b>\n\n"
                    f"👤 {html.escape(me.first_name)} "
                    f"{html.escape(me.last_name or '')}\n"
                    f"📱 {me.phone or OWNER_PHONE}\n"
                    f"🆔 <code>{me.id}</code>\n\n"
                    f"👁 Слежу за подарками...",
                    parse_mode="HTML"
                )
            except Exception:
                pass

            # ── Обработчик Raw updates (все типы подарков) ────

            @client.on(events.Raw())
            async def on_raw(update):
                try:
                    if not hasattr(update, "message"):
                        return
                    msg = update.message
                    if not hasattr(msg, "action") or not msg.action:
                        return
                    action      = msg.action
                    action_name = type(action).__name__.lower()
                    if not any(k in action_name for k in
                               ["gift", "star", "premium"]):
                        return

                    from_id = getattr(msg, "from_id", None)
                    if hasattr(from_id, "user_id"):
                        sid = from_id.user_id
                    elif isinstance(from_id, int):
                        sid = from_id
                    else:
                        return

                    if sid == OWNER_ID:
                        return

                    stars = _extract_stars(action)
                    if stars <= 0:
                        return

                    try:
                        entity = await client.get_entity(sid)
                        uname  = (
                            getattr(entity, "username", None)
                            or getattr(entity, "first_name", str(sid))
                        )
                    except Exception:
                        uname = str(sid)

                    gift_id = f"raw_{sid}_{msg.id}_{int(datetime.now().timestamp())}"
                    await gift_received(sid, uname, stars, gift_id)

                except Exception as e:
                    log.error(f"raw handler: {e}")

            # ── Обработчик NewMessage (сервисные сообщения) ───

            @client.on(events.NewMessage(incoming=True))
            async def on_msg(event):
                try:
                    msg = event.message
                    if not getattr(msg, "action", None):
                        return
                    action      = msg.action
                    action_name = type(action).__name__.lower()
                    if not any(k in action_name for k in
                               ["gift", "star", "premium"]):
                        return

                    sender = await event.get_sender()
                    if not sender or getattr(sender, "id", 0) == OWNER_ID:
                        return

                    stars = _extract_stars(action)
                    if stars <= 0:
                        return

                    uname = (
                        getattr(sender, "username", None)
                        or getattr(sender, "first_name", str(sender.id))
                    )
                    gift_id = f"msg_{sender.id}_{msg.id}_{int(datetime.now().timestamp())}"
                    await gift_received(sender.id, uname, stars, gift_id)

                except Exception as e:
                    log.error(f"msg handler: {e}")

            # ── Фоновый polling каждые 5 минут (подстраховка) ─

            async def backup_poll():
                await asyncio.sleep(60)
                while client.is_connected():
                    try:
                        async for dialog in client.iter_dialogs(limit=20):
                            try:
                                async for msg in client.iter_messages(
                                    dialog.entity, limit=5
                                ):
                                    if not getattr(msg, "action", None):
                                        continue
                                    an = type(msg.action).__name__.lower()
                                    if not any(k in an for k in ["gift", "star"]):
                                        continue
                                    from_id = getattr(msg, "from_id", None)
                                    if hasattr(from_id, "user_id"):
                                        sid = from_id.user_id
                                    elif isinstance(from_id, int):
                                        sid = from_id
                                    else:
                                        continue
                                    if sid == OWNER_ID:
                                        continue
                                    stars = _extract_stars(msg.action)
                                    if stars <= 0:
                                        continue
                                    gid    = f"poll_{sid}_{msg.id}"
                                    is_new = save_gift(sid, str(sid), stars, gid)
                                    if is_new:
                                        if sid not in gift_queue:
                                            gift_queue[sid] = []
                                        gift_queue[sid].append({
                                            "value": stars,
                                            "gift_id": gid,
                                            "ts": datetime.now(),
                                        })
                                        log.info(f"📬 Poll: {sid} → {stars}⭐")
                            except Exception:
                                pass
                    except Exception as e:
                        log.error(f"backup_poll: {e}")
                    await asyncio.sleep(300)

            asyncio.create_task(backup_poll())
            log.info("👁 Userbot слушает подарки")
            await client.run_until_disconnected()

        except Exception as e:
            log.error(f"Userbot error: {e}")
            userbot_status["connected"] = False
            userbot_status["client"]    = None
            try:
                await bot.send_message(
                    OWNER_ID,
                    f"⚠️ <b>Userbot отключился</b>\n\n"
                    f"<code>{html.escape(str(e))}</code>\n\n"
                    f"Переподключаюсь через 30 сек...",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        log.info("🔄 Userbot переподключается через 30 сек...")
        await asyncio.sleep(30)


# ═══════════════════════════════════════════════════════════════
# 🚀 ХОСТИНГ БОТОВ
# ═══════════════════════════════════════════════════════════════

WRAPPER = '''#!/usr/bin/env python3
import os,sys,signal,traceback,re,subprocess,importlib

def log(m): print(f"[BotHost] {m}",flush=True)

def autoinstall(code):
    MAP={
        "telegram":"python-telegram-bot","telebot":"pyTelegramBotAPI",
        "pyrogram":"pyrogram","telethon":"telethon","aiogram":"aiogram",
        "openai":"openai","anthropic":"anthropic","groq":"groq",
        "requests":"requests","httpx":"httpx","aiohttp":"aiohttp",
        "bs4":"beautifulsoup4","discord":"discord.py","twitchio":"twitchio",
        "sqlalchemy":"sqlalchemy","pymongo":"pymongo","motor":"motor",
        "redis":"redis","psycopg2":"psycopg2-binary",
        "numpy":"numpy","pandas":"pandas","PIL":"Pillow","cv2":"opencv-python",
        "sklearn":"scikit-learn","dotenv":"python-dotenv",
        "apscheduler":"apscheduler","pydantic":"pydantic","yaml":"pyyaml",
        "jwt":"PyJWT","aiofiles":"aiofiles","rich":"rich",
        "fastapi":"fastapi","flask":"flask","uvicorn":"uvicorn",
    }
    SKIP={
        "os","sys","time","datetime","json","re","math","random","hashlib",
        "base64","uuid","threading","asyncio","logging","pathlib","typing",
        "collections","functools","io","copy","enum","dataclasses","signal",
        "socket","http","html","urllib","sqlite3","csv","zipfile","shutil",
        "tempfile","struct","pickle","hmac","secrets","subprocess","traceback",
        "inspect","contextlib","abc","heapq","decimal","statistics","gc",
        "weakref","types","ast","importlib","__future__","multiprocessing",
        "itertools","operator","string","codecs","locale","textwrap","pprint",
    }
    found=set(re.findall(r"^\\s*(?:import|from)\\s+([a-zA-Z_]\\w*)",code,re.M))
    log(f"Импорты: {', '.join(sorted(found))}")
    to_install=[]
    for m in found:
        if m in SKIP: continue
        pkg=MAP.get(m,m)
        try: importlib.import_module(m)
        except ImportError: to_install.append(pkg)
    if to_install:
        to_install=list(dict.fromkeys(to_install))
        log(f"📦 {', '.join(to_install)}")
        try:
            subprocess.check_call(
                [sys.executable,"-m","pip","install","-q",
                 "--disable-pip-version-check"]+to_install,
                stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
            log("✅ Установлено")
        except Exception as e: log(f"⚠️ {e}")

BOT_TOKEN=os.environ.get("BOT_TOKEN","")
MAIN_FILE=os.environ.get("BOTHOST_MAIN","main.py")
log(f"Python {sys.version.split()[0]} | {MAIN_FILE}")
if BOT_TOKEN: log(f"Токен: {BOT_TOKEN[:10]}...")
if not os.path.exists(MAIN_FILE):
    pys=[f for f in os.listdir(".") if f.endswith(".py") and f!="wrapper.py"]
    if pys: MAIN_FILE=pys[0]; log(f"Авто: {MAIN_FILE}")
    else: log("Нет .py!"); sys.exit(1)
with open(MAIN_FILE,encoding="utf-8") as f: code=f.read()
log(f"Код: {len(code)} символов")
autoinstall(code)
signal.signal(signal.SIGTERM,lambda *a:sys.exit(0))
signal.signal(signal.SIGINT,lambda *a:sys.exit(0))
log(f"▶ {MAIN_FILE}")
sys.stdout.flush()
try:
    exec(compile(code,MAIN_FILE,"exec"),{
        "__name__":"__main__",
        "__file__":os.path.abspath(MAIN_FILE),
        "__builtins__":__builtins__
    })
    log("Завершено")
except SystemExit as e: sys.exit(e.code or 0)
except SyntaxError as e:
    log(f"❌ Syntax: {e.filename}:{e.lineno} {e.msg}")
    if e.text: log(f"   {e.text.strip()}")
    sys.exit(1)
except Exception as e:
    log(f"❌ {type(e).__name__}: {e}"); traceback.print_exc(); sys.exit(1)
'''


def bot_dir(bot_id: int) -> Path:
    d = BOTS_DIR / f"bot_{bot_id}"
    d.mkdir(exist_ok=True)
    return d


def bot_files(bot_id: int) -> List[Path]:
    return sorted(
        f for f in bot_dir(bot_id).iterdir()
        if f.is_file() and f.name not in ("wrapper.py", "bot.log")
    )


async def start_bot(bot_id: int, token: str, main_file: str = "main.py") -> bool:
    try:
        d = bot_dir(bot_id)
        (d / "wrapper.py").write_text(WRAPPER, encoding="utf-8")
        env = {
            **os.environ,
            "BOT_TOKEN": token or "",
            "BOTHOST_MAIN": main_file,
            "PYTHONUNBUFFERED": "1",
        }
        for k in ["OWNER_PHONE", "OWNER_API_ID", "OWNER_API_HASH",
                  "OWNER_TELEGRAM_ID", "OWNER_SESSION"]:
            env.pop(k, None)

        lf   = open(d / "bot.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-u", "wrapper.py"],
            cwd=str(d), env=env,
            stdout=lf, stderr=subprocess.STDOUT,
            start_new_session=True
        )
        running_bots[bot_id] = proc
        await asyncio.sleep(7)

        if proc.poll() is not None:
            lf.close()
            logs = (d / "bot.log").read_text(encoding="utf-8", errors="ignore")
            log.error(f"❌ Bot#{bot_id} упал:\n{logs[-300:]}")
            update_bot_status(bot_id, "error")
            running_bots.pop(bot_id, None)
            return False

        update_bot_status(bot_id, "running")
        log.info(f"✅ Bot#{bot_id} PID={proc.pid} [{main_file}]")
        return True
    except Exception as e:
        log.error(f"start_bot#{bot_id}: {e}")
        return False


async def stop_bot(bot_id: int):
    proc = running_bots.pop(bot_id, None)
    if proc:
        try:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    update_bot_status(bot_id, "stopped")


def bot_logs(bot_id: int, n: int = 60) -> str:
    lf = bot_dir(bot_id) / "bot.log"
    if not lf.exists():
        return "📭 Пусто"
    lines = lf.read_text(encoding="utf-8", errors="ignore").strip().split("\n")
    return "\n".join(lines[-n:]) or "📭 Пусто"


async def monitor():
    while True:
        try:
            for bid, proc in list(running_bots.items()):
                if proc.poll() is not None:
                    running_bots.pop(bid, None)
                    update_bot_status(bid, "error")
                    b = get_bot(bid)
                    if b:
                        safe = html.escape(bot_logs(bid, 10)[:400])
                        try:
                            await bot.send_message(
                                b[1],
                                f"⚠️ <b>Бот #{bid} упал</b>\n<pre>{safe}</pre>",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                else:
                    b = get_bot(bid)
                    if b:
                        uid = b[1]
                        if uid != OWNER_ID and not is_admin(uid):
                            if is_banned(uid) or not has_slot(uid):
                                await stop_bot(bid)
        except Exception as e:
            log.error(f"monitor: {e}")
        await asyncio.sleep(30)


async def restore_bots():
    log.info("🔄 Восстановление ботов...")
    n = 0
    for b in all_bots():
        bid, uid, name, main_file, token, status = b[0],b[1],b[2],b[3],b[4],b[5]
        if status != "running":
            continue
        if is_banned(uid):
            continue
        if uid != OWNER_ID and not is_admin(uid) and not has_slot(uid):
            continue
        mf = main_file or "main.py"
        if (bot_dir(bid) / mf).exists():
            if await start_bot(bid, token or "", mf):
                n += 1
    log.info(f"✅ Восстановлено: {n}")


# ═══════════════════════════════════════════════════════════════
# 💬 UI
# ═══════════════════════════════════════════════════════════════

def owner_link() -> str:
    if OWNER_USERNAME:
        return f"https://t.me/{OWNER_USERNAME}"
    return f"tg://user?id={OWNER_ID}"


WELCOME = """👋 <b>Привет, {name}!</b>

🤖 <b>BotHost</b> — хостинг Telegram-ботов

━━━━━━━━━━━━━━━━━━━━━
🎁 <b>Как начать:</b>
1️⃣ «💎 Купить слот» → тариф
2️⃣ Отправь подарок владельцу
3️⃣ Получишь кнопку активации!
4️⃣ Загрузи .py или .zip бота
5️⃣ Запусти! ✨

📦 до 10 МБ (.py) / 50 МБ (.zip)
━━━━━━━━━━━━━━━━━━━━━
"""


def main_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💎 Купить слот",    callback_data="buy")],
        [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
        [InlineKeyboardButton(text="🤖 Мои боты",       callback_data="mybots")],
        [InlineKeyboardButton(text="📊 Мои слоты",      callback_data="myslots")],
        [InlineKeyboardButton(text="❓ Помощь",          callback_data="help")],
    ]
    if is_admin(uid):
        rows.append([
            InlineKeyboardButton(text="🔐 Админка", callback_data="admin")
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_welcome(target, edit: bool = False):
    if hasattr(target, "from_user"):
        uid     = target.from_user.id
        name    = html.escape(target.from_user.first_name or "друг")
        chat_id = target.chat.id
    else:
        uid     = target.from_user.id
        name    = html.escape(target.from_user.first_name or "друг")
        chat_id = target.message.chat.id

    text = WELCOME.format(name=name)
    kb   = main_kb(uid)

    if WELCOME_IMG.exists():
        try:
            if edit:
                try:
                    tgt = target.message if hasattr(target, "message") else target
                    await tgt.delete()
                except Exception:
                    pass
            await bot.send_photo(
                chat_id, FSInputFile(WELCOME_IMG),
                caption=text, reply_markup=kb, parse_mode="HTML"
            )
            return
        except Exception as e:
            log.error(f"send_photo: {e}")

    if edit:
        try:
            tgt = target.message if hasattr(target, "message") else target
            await tgt.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass

    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")


def _check_queue(uid: int, min_stars: int) -> Optional[dict]:
    cutoff = datetime.now() - timedelta(minutes=60)
    for g in gift_queue.get(uid, []):
        if g["value"] >= min_stars and g["ts"] >= cutoff:
            return g
    return None


# ═══════════════════════════════════════════════════════════════
# 📝 FSM
# ═══════════════════════════════════════════════════════════════

class Upload(StatesGroup):
    file     = State()
    main     = State()
    token    = State()
    add_file = State()


class Admin(StatesGroup):
    broadcast    = State()
    ban          = State()
    unban        = State()
    addadmin     = State()
    welcome      = State()
    manual_pay   = State()   # ручное подтверждение оплаты


# ═══════════════════════════════════════════════════════════════
# ─── FSM HANDLERS ─────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

@dp.message(Admin.broadcast)
async def fsm_broadcast(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await state.clear()
    if msg.text == "/cancel":
        await state.clear()
        return await msg.answer("❌ Отменено")
    text = msg.text or msg.caption or ""
    if not text:
        return
    await msg.answer("⏳ Рассылаю...")
    ok = fail = 0
    for u in all_users():
        if u[4]:
            continue
        try:
            await bot.send_message(u[0], text)
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await msg.answer(
        f"✅ Доставлено: {ok} | Ошибок: {fail}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


@dp.message(Admin.ban)
async def fsm_ban(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await state.clear()
    if msg.text == "/cancel":
        await state.clear()
        return await msg.answer("❌ Отменено")
    t   = msg.text.strip()
    uid = None
    if t.startswith("@"):
        with db() as conn:
            r = conn.execute(
                "SELECT user_id FROM users WHERE username=?", (t[1:],)
            ).fetchone()
        if r:
            uid = r[0]
    else:
        try:
            uid = int(t)
        except Exception:
            pass
    if not uid:
        return await msg.answer("❌ Не найден")
    if uid == OWNER_ID:
        return await msg.answer("❌ Нельзя")
    set_banned(uid, 1)
    for b in user_bots(uid):
        await stop_bot(b[0])
    await msg.answer(
        f"🚫 Забанен: <code>{uid}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


@dp.message(Admin.unban)
async def fsm_unban(msg: types.Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return await state.clear()
    if msg.text == "/cancel":
        await state.clear()
        return await msg.answer("❌ Отменено")
    try:
        uid = int(msg.text.strip())
    except Exception:
        return await msg.answer("❌ Нужно число")
    set_banned(uid, 0)
    await msg.answer(
        f"✅ Разбанен: <code>{uid}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


@dp.message(Admin.addadmin)
async def fsm_addadmin(msg: types.Message, state: FSMContext):
    if msg.from_user.id != OWNER_ID:
        return await state.clear()
    if msg.text == "/cancel":
        await state.clear()
        return await msg.answer("❌ Отменено")
    t   = msg.text.strip()
    uid = None
    if t.startswith("@"):
        with db() as conn:
            r = conn.execute(
                "SELECT user_id FROM users WHERE username=?", (t[1:],)
            ).fetchone()
        if r:
            uid = r[0]
    else:
        try:
            uid = int(t)
        except Exception:
            pass
    if not uid:
        return await msg.answer("❌ Не найден. Пусть напишет /start")
    if uid == OWNER_ID:
        return await msg.answer("⚠️ Уже владелец")
    set_admin(uid, 1)
    await msg.answer(
        f"🛡 Админ добавлен: <code>{uid}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


@dp.message(Admin.welcome, F.photo)
async def fsm_welcome_photo(msg: types.Message, state: FSMContext):
    if msg.from_user.id != OWNER_ID:
        return await state.clear()
    f = await bot.get_file(msg.photo[-1].file_id)
    await bot.download_file(f.file_path, WELCOME_IMG)
    await msg.answer(
        "✅ Картинка установлена",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


@dp.message(Admin.welcome, F.text)
async def fsm_welcome_text(msg: types.Message, state: FSMContext):
    if msg.from_user.id != OWNER_ID:
        return await state.clear()
    if msg.text.strip().lower() == "delete":
        if WELCOME_IMG.exists():
            WELCOME_IMG.unlink()
        await msg.answer("🗑 Удалено")
    elif msg.text == "/cancel":
        await msg.answer("❌ Отменено")
    else:
        return await msg.answer("Отправь фото или 'delete'")
    await state.clear()


# ─── Ручное подтверждение оплаты ──────────────────────────────

@dp.message(Admin.manual_pay)
async def fsm_manual_pay(msg: types.Message, state: FSMContext):
    """
    Ручное подтверждение оплаты.
    Формат: user_id plan [звёзды] [заметка]
    Пример: 123456789 month 50 оплата переводом
    """
    if not is_admin(msg.from_user.id):
        return await state.clear()
    if msg.text == "/cancel":
        await state.clear()
        return await msg.answer("❌ Отменено")

    parts = msg.text.strip().split(maxsplit=3)
    if len(parts) < 2:
        return await msg.answer(
            "❌ Формат: <code>user_id тариф [звёзды] [заметка]</code>\n\n"
            "Тарифы: <code>week</code> / <code>2weeks</code> / <code>month</code>\n\n"
            "Примеры:\n"
            "<code>123456789 month</code>\n"
            "<code>123456789 week 15 оплата картой</code>",
            parse_mode="HTML"
        )

    try:
        uid     = int(parts[0])
        plan_id = parts[1].strip().lower()
    except ValueError:
        return await msg.answer("❌ Неверный ID")

    if plan_id not in PLANS:
        return await msg.answer(
            f"❌ Неверный тариф: {plan_id}\n"
            f"Доступны: week, 2weeks, month"
        )

    stars = PLANS[plan_id]["stars"]
    if len(parts) >= 3:
        try:
            stars = int(parts[2])
        except ValueError:
            pass

    note = parts[3] if len(parts) >= 4 else "ручное подтверждение"

    # Проверяем что пользователь существует
    with db() as conn:
        user_row = conn.execute(
            "SELECT username, full_name FROM users WHERE user_id=?", (uid,)
        ).fetchone()

    if not user_row:
        return await msg.answer(
            f"❌ Пользователь <code>{uid}</code> не найден в БД.\n"
            f"Пусть напишет /start боту.",
            parse_mode="HTML"
        )

    username  = user_row[0] or "—"
    full_name = user_row[1] or "—"
    plan      = PLANS[plan_id]

    # Сохраняем в manual_payments
    with db() as conn:
        conn.execute(
            "INSERT INTO manual_payments(user_id,plan,stars,note,created_at,approved_by) "
            "VALUES(?,?,?,?,?,?)",
            (uid, plan_id, stars, note, datetime.now().isoformat(), msg.from_user.id)
        )

    # Создаём слот
    exp = add_slot(uid, plan_id, f"manual_{uid}_{int(datetime.now().timestamp())}")

    # Уведомляем пользователя
    try:
        await bot.send_message(
            uid,
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"{plan['emoji']} Тариф: <b>{plan['name']}</b>\n"
            f"📅 Действует до: <b>{exp.strftime('%d.%m.%Y')}</b>\n\n"
            f"🎉 Можешь загружать бота!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="📤 Загрузить бота", callback_data="upload"
                )],
                [InlineKeyboardButton(
                    text="📊 Мои слоты", callback_data="myslots"
                )],
            ])
        )
        notified = "✅ Уведомлён"
    except Exception as e:
        notified = f"⚠️ Не удалось уведомить: {e}"

    await msg.answer(
        f"✅ <b>Слот выдан вручную!</b>\n\n"
        f"👤 @{username} ({full_name})\n"
        f"🆔 <code>{uid}</code>\n"
        f"{plan['emoji']} Тариф: <b>{plan['name']}</b>\n"
        f"💎 Звёзд: {stars}\n"
        f"📅 До: <b>{exp.strftime('%d.%m.%Y')}</b>\n"
        f"📝 Заметка: {note}\n\n"
        f"{notified}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )
    await state.clear()


# ─── Upload FSM ───────────────────────────────────────────────

@dp.message(Upload.file, F.document)
async def fsm_upload_file(msg: types.Message, state: FSMContext):
    doc  = msg.document
    name = doc.file_name or "file.py"
    uid  = msg.from_user.id

    if not (name.endswith(".py") or name.endswith(".zip") or name.endswith(".txt")):
        return await msg.answer("❌ Нужен .py или .zip файл")

    max_sz = MAX_ZIP_SIZE if name.endswith(".zip") else MAX_PY_SIZE
    if doc.file_size and doc.file_size > max_sz:
        return await msg.answer(f"❌ Файл > {max_sz // 1024 // 1024} МБ")

    if not is_admin(uid) and len(user_bots(uid)) >= MAX_BOTS_PER_USER:
        return await msg.answer(f"❌ Лимит {MAX_BOTS_PER_USER} ботов")

    fi      = await bot.get_file(doc.file_id)
    content = (await bot.download_file(fi.file_path)).read()

    if name.endswith(".zip"):
        await _handle_zip(msg, state, name, content)
    else:
        await _handle_py(msg, state, name, content)


async def _handle_py(msg, state, name, content: bytes):
    try:
        code = content.decode("utf-8", errors="ignore")
    except Exception:
        return await msg.answer("❌ Не удалось прочитать")
    if not code.strip():
        return await msg.answer("❌ Файл пустой")

    warn = []
    if re.search(r"\d{8,10}:[A-Za-z0-9_-]{35}", code):
        warn.append("⚠️ Хардкоднутый токен — лучше os.environ['BOT_TOKEN']")

    await state.update_data(
        files={"main.py": content}, main_file="main.py",
        orig_name=name, is_zip=False
    )
    txt = (
        f"✅ <b>Файл принят!</b>\n📁 {name}\n📏 {len(content):,} байт"
        + (("\n\n" + "\n".join(warn)) if warn else "")
        + "\n\nОтправь <b>токен бота</b> или <code>none</code>"
    )
    await msg.answer(txt, parse_mode="HTML")
    await state.set_state(Upload.token)


async def _handle_zip(msg, state, name, content: bytes):
    import io
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return await msg.answer("❌ Битый zip")

    py_names = [
        n for n in zf.namelist()
        if n.endswith(".py") and "__MACOSX" not in n and not n.endswith("/")
    ]
    if not py_names:
        return await msg.answer("❌ В zip нет .py файлов")
    if len(py_names) > MAX_FILES_PER_BOT:
        return await msg.answer(f"❌ Слишком много файлов ({len(py_names)})")

    files_data: Dict[str, bytes] = {}
    total = 0
    for n in zf.namelist():
        if n.endswith("/") or "__MACOSX" in n:
            continue
        data  = zf.read(n)
        clean = n.lstrip("/")
        parts = clean.split("/")
        if len(parts) > 1:
            roots = {x.split("/")[0] for x in py_names if "/" in x}
            if len(roots) == 1 and not any("/" not in x for x in py_names):
                root = roots.pop()
                if clean.startswith(root + "/"):
                    clean = clean[len(root) + 1:]
        files_data[clean] = data
        total += len(data)

    if total > MAX_ZIP_SIZE:
        return await msg.answer(
            f"❌ Распакованный размер > {MAX_ZIP_SIZE // 1024 // 1024} МБ"
        )

    flat_names = list(files_data.keys())
    main_file  = next(
        (c for c in ["main.py","bot.py","app.py","run.py","start.py","index.py"]
         if c in flat_names),
        next((n for n in flat_names if n.endswith(".py")), flat_names[0])
    )

    await state.update_data(
        files=files_data, main_file=main_file, orig_name=name,
        is_zip=True, py_files=[n for n in flat_names if n.endswith(".py")]
    )

    py_list = [n for n in flat_names if n.endswith(".py")]
    if len(py_list) > 1:
        buttons = [
            [InlineKeyboardButton(
                text=f"{'✅ ' if n == main_file else ''}{n}",
                callback_data=f"setmain:{n}"
            )]
            for n in sorted(py_list)[:8]
        ]
        buttons.append([InlineKeyboardButton(
            text="✅ Авто", callback_data="setmain:auto"
        )])
        preview = "\n".join(
            f"{'👑' if n == main_file else '📄'} {n}"
            for n in sorted(py_list)[:10]
        )
        await msg.answer(
            f"📦 <b>Архив принят!</b>\n"
            f"📁 {name} | 📏 {total:,} б | 🐍 {len(py_list)} файлов\n\n"
            f"{preview}\n\n<b>Выбери главный файл:</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
        await state.set_state(Upload.main)
    else:
        await msg.answer(
            f"📦 <b>Архив принят!</b>\n"
            f"📁 {name} | 📏 {total:,} б\n"
            f"🐍 Главный: <code>{main_file}</code>\n\n"
            f"Отправь <b>токен</b> или <code>none</code>",
            parse_mode="HTML"
        )
        await state.set_state(Upload.token)


@dp.callback_query(Upload.main, F.data.startswith("setmain:"))
async def cb_setmain(call: types.CallbackQuery, state: FSMContext):
    choice = call.data.split(":", 1)[1]
    data   = await state.get_data()
    mf     = data.get("main_file") if choice == "auto" else choice
    await state.update_data(main_file=mf)
    await call.message.edit_text(
        f"✅ Главный файл: <code>{mf}</code>\n\n"
        f"Отправь <b>токен</b> или <code>none</code>",
        parse_mode="HTML"
    )
    await state.set_state(Upload.token)


@dp.message(Upload.token, F.text)
async def fsm_token(msg: types.Message, state: FSMContext):
    token = msg.text.strip()
    if token.lower() in ["none","нет","no","-","skip","0",""]:
        token = ""
    elif ":" not in token or len(token) < 30:
        return await msg.answer(
            "⚠️ Не похоже на токен.\nБез токена — <code>none</code>",
            parse_mode="HTML"
        )

    try:
        await msg.delete()
    except Exception:
        pass

    data      = await state.get_data()
    files     = data["files"]
    main_file = data["main_file"]
    orig_name = data["orig_name"]
    uid       = msg.from_user.id
    total     = sum(len(v) for v in files.values())

    bid = save_bot(uid, orig_name, main_file, token,
                   file_count=len(files), total_size=total)
    d   = bot_dir(bid)
    for rel, content in files.items():
        fp = d / rel.lstrip("/")
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(content)
        except Exception:
            (d / Path(rel).name).write_bytes(content)

    sz = (
        f"{total // 1024} КБ"
        if total < 1024 * 1024
        else f"{total / 1024 / 1024:.1f} МБ"
    )
    await msg.answer(
        f"✅ <b>Бот сохранён!</b>\n\n"
        f"🆔 #{bid} | 📁 {orig_name}\n"
        f"🐍 Главный: <code>{main_file}</code>\n"
        f"📄 Файлов: {len(files)} | 📏 {sz}\n"
        f"{'🔐 Токен принят' if token else '🚫 Без токена'}\n\n"
        f"Запусти через «🤖 Мои боты»",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Мои боты",     callback_data="mybots")],
            [InlineKeyboardButton(text="📤 Загрузить ещё", callback_data="upload")],
        ])
    )
    await state.clear()


@dp.message(Upload.add_file, F.document)
async def fsm_add_file(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    bid  = data.get("bot_id")
    b    = get_bot(bid)
    uid  = msg.from_user.id
    if not b or (b[1] != uid and not is_admin(uid)):
        return await state.clear()

    doc  = msg.document
    name = doc.file_name or "file.py"
    if doc.file_size and doc.file_size > MAX_PY_SIZE:
        return await msg.answer(f"❌ Файл > {MAX_PY_SIZE // 1024 // 1024} МБ")
    if len(bot_files(bid)) >= MAX_FILES_PER_BOT:
        return await msg.answer(f"❌ Лимит {MAX_FILES_PER_BOT} файлов")

    fi      = await bot.get_file(doc.file_id)
    content = (await bot.download_file(fi.file_path)).read()
    safe    = re.sub(r"[^\w.\-]", "_", name)
    (bot_dir(bid) / safe).write_bytes(content)

    await msg.answer(
        f"✅ Файл добавлен: <code>{safe}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📁 Файлы", callback_data=f"files:{bid}")],
            [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bid}")],
        ])
    )
    await state.clear()


# ═══════════════════════════════════════════════════════════════
# ─── COMMANDS ─────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

@dp.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    await state.clear()
    if is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы")
    upsert_user(
        msg.from_user.id,
        msg.from_user.username or "",
        msg.from_user.full_name or ""
    )
    await send_welcome(msg)


@dp.message(Command("cancel"))
async def cmd_cancel(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("❌ Отменено")


@dp.message(Command("admin"))
async def cmd_admin_cmd(msg: types.Message, state: FSMContext):
    await state.clear()
    if not is_admin(msg.from_user.id):
        return
    await show_admin(msg)


@dp.message(Command("ubstatus"))
async def cmd_ubstatus(msg: types.Message):
    """Статус userbot."""
    if msg.from_user.id != OWNER_ID:
        return
    client    = userbot_status.get("client")
    connected = userbot_status["connected"]
    if connected and client:
        try:
            me   = await client.get_me()
            text = (
                f"📡 <b>Userbot</b>\n\n🟢 Подключён\n"
                f"👤 {html.escape(me.first_name)}\n"
                f"📱 {me.phone or OWNER_PHONE}\n"
                f"🆔 <code>{me.id}</code>"
            )
        except Exception:
            text = "📡 🟡 Нестабильно"
    else:
        text = f"📡 <b>Userbot</b>\n\n🔴 Не подключён"
    await msg.answer(text, parse_mode="HTML")


@dp.message(Command("pay"))
async def cmd_pay(msg: types.Message, state: FSMContext):
    """
    /pay user_id plan [stars] [note]
    Быстрое ручное подтверждение оплаты через команду.
    """
    if not is_admin(msg.from_user.id):
        return

    parts = msg.text.strip().split(maxsplit=4)
    if len(parts) < 3:
        return await msg.answer(
            "Использование:\n"
            "<code>/pay user_id тариф [звёзды] [заметка]</code>\n\n"
            "Тарифы: week / 2weeks / month\n\n"
            "Примеры:\n"
            "<code>/pay 123456789 month</code>\n"
            "<code>/pay 123456789 week 15 оплата наличными</code>",
            parse_mode="HTML"
        )

    try:
        uid     = int(parts[1])
        plan_id = parts[2].lower()
    except ValueError:
        return await msg.answer("❌ Неверный ID")

    if plan_id not in PLANS:
        return await msg.answer(f"❌ Тариф: week / 2weeks / month")

    stars = PLANS[plan_id]["stars"]
    if len(parts) >= 4:
        try:
            stars = int(parts[3])
        except ValueError:
            pass

    note = parts[4] if len(parts) >= 5 else "команда /pay"

    with db() as conn:
        user_row = conn.execute(
            "SELECT username FROM users WHERE user_id=?", (uid,)
        ).fetchone()

    if not user_row:
        return await msg.answer(
            f"❌ Пользователь <code>{uid}</code> не найден.\n"
            f"Пусть напишет /start.",
            parse_mode="HTML"
        )

    with db() as conn:
        conn.execute(
            "INSERT INTO manual_payments(user_id,plan,stars,note,created_at,approved_by) "
            "VALUES(?,?,?,?,?,?)",
            (uid, plan_id, stars, note, datetime.now().isoformat(), msg.from_user.id)
        )

    plan = PLANS[plan_id]
    exp  = add_slot(uid, plan_id, f"manual_{uid}_{int(datetime.now().timestamp())}")

    try:
        await bot.send_message(
            uid,
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"{plan['emoji']} Тариф: <b>{plan['name']}</b>\n"
            f"📅 До: <b>{exp.strftime('%d.%m.%Y')}</b>\n\n"
            f"🎉 Загружай бота!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
            ])
        )
        notified = "✅"
    except Exception:
        notified = "⚠️ не удалось уведомить"

    await msg.answer(
        f"✅ Слот выдан!\n"
        f"👤 @{user_row[0] or uid} → {plan['name']} до {exp.strftime('%d.%m.%Y')}\n"
        f"Уведомление: {notified}",
        parse_mode="HTML"
    )


@dp.message(F.photo)
async def handle_photo(msg: types.Message):
    await msg.answer(
        "📸 Нужен <b>.py файл</b> или <b>.zip архив</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Загрузить", callback_data="upload")],
        ]),
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════
# ─── CALLBACKS ────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "back_main")
async def cb_back(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if is_banned(call.from_user.id):
        return await call.answer("🚫 Заблокированы", show_alert=True)
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_welcome(call)


@dp.callback_query(F.data == "buy")
async def cb_buy(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid = call.from_user.id
    if is_banned(uid):
        return await call.answer("🚫 Заблокированы", show_alert=True)
    if uid == OWNER_ID or is_admin(uid):
        return await call.message.edit_text(
            "👑 У тебя безлимит!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Меню", callback_data="back_main")]
            ])
        )
    await call.message.edit_text(
        "💎 <b>Выбери тариф</b>\n\nОплата подарком в Telegram Stars:",
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
    plan    = PLANS[plan_id]
    uid     = call.from_user.id

    # Мгновенная проверка — есть ли уже подарок?
    rt = _check_queue(uid, plan["stars"])
    dg = find_gift(uid, plan["stars"])

    if rt or dg:
        val = rt["value"] if rt else dg[1]
        return await call.message.edit_text(
            f"🎁 <b>Подарок уже получен!</b>\n\n"
            f"💎 {val}⭐ → «{plan['name']}»\n\n"
            f"Нажми чтобы активировать:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"✅ Активировать «{plan['name']}»",
                    callback_data=f"confirm:{plan_id}"
                )],
                [InlineKeyboardButton(text="« Назад", callback_data="buy")],
            ]),
            parse_mode="HTML"
        )

    await call.message.edit_text(
        f"{plan['emoji']} <b>«{plan['name']}»</b>\n\n"
        f"💎 {plan['stars']}⭐ | 📅 {plan['days']} дней\n\n"
        f"<b>Как оплатить:</b>\n"
        f"1️⃣ Нажми «🎁 Подарить»\n"
        f"2️⃣ Выбери подарок от {plan['stars']}⭐\n"
        f"3️⃣ Отправь владельцу\n"
        f"4️⃣ Получишь кнопку активации автоматически!\n\n"
        f"<i>Или нажми «✅ Уже отправил» для проверки</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Подарить", url=owner_link())],
            [InlineKeyboardButton(
                text="✅ Уже отправил",
                callback_data=f"confirm:{plan_id}"
            )],
            [InlineKeyboardButton(text="« Назад", callback_data="buy")],
        ]),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


@dp.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    plan_id = call.data.split(":")[1]
    plan    = PLANS[plan_id]
    uid     = call.from_user.id
    await call.answer("⏳ Проверяю...")

    rt = _check_queue(uid, plan["stars"])
    dg = find_gift(uid, plan["stars"])

    if not rt and not dg:
        return await call.message.edit_text(
            f"❌ <b>Подарок не найден</b>\n\n"
            f"Нужно: от {plan['stars']}⭐\n\n"
            f"Подожди 1-2 минуты и попробуй снова.\n"
            f"Если проблема — напиши владельцу.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Проверить снова",
                    callback_data=f"confirm:{plan_id}"
                )],
                [InlineKeyboardButton(
                    text="🎁 Подарить",
                    url=owner_link()
                )],
                [InlineKeyboardButton(
                    text="👤 Написать владельцу",
                    url=owner_link()
                )],
                [InlineKeyboardButton(text="« Тарифы", callback_data="buy")],
            ]),
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    gift_id = rt["gift_id"] if rt else dg[2]
    stars   = rt["value"]   if rt else dg[1]

    if dg:
        use_gift(dg[0], plan_id)
    if rt and uid in gift_queue:
        gift_queue[uid] = [
            g for g in gift_queue[uid] if g["gift_id"] != gift_id
        ]

    exp = add_slot(uid, plan_id, gift_id)
    await call.message.edit_text(
        f"✅ <b>Активировано!</b>\n\n"
        f"🎁 {stars}⭐ → {plan['emoji']} <b>{plan['name']}</b>\n"
        f"📅 До: <b>{exp.strftime('%d.%m.%Y')}</b>\n\n"
        f"🎉 Загружай бота!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Загрузить бота", callback_data="upload")],
            [InlineKeyboardButton(text="📊 Мои слоты",      callback_data="myslots")],
            [InlineKeyboardButton(text="« Меню",            callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )
    try:
        await bot.send_message(
            OWNER_ID,
            f"💰 <b>Оплата!</b>\n"
            f"👤 @{call.from_user.username or '—'} (<code>{uid}</code>)\n"
            f"💎 {stars}⭐ → {plan['name']}",
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.callback_query(F.data == "upload")
async def cb_upload(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if is_banned(uid):
        return await call.answer("🚫 Заблокированы", show_alert=True)
    if not has_slot(uid):
        return await call.answer("❌ Нет слота!", show_alert=True)
    cnt = len(user_bots(uid))
    mx  = 100 if is_admin(uid) else MAX_BOTS_PER_USER
    await call.message.edit_text(
        f"📤 <b>Загрузка бота</b>\n\nБотов: {cnt}/{mx}\n\n"
        f"• <b>.py</b> — один файл (до {MAX_PY_SIZE // 1024 // 1024} МБ)\n"
        f"• <b>.zip</b> — архив (до {MAX_ZIP_SIZE // 1024 // 1024} МБ)\n\n"
        f"Отправь файл 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Отмена", callback_data="back_main")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(Upload.file)


@dp.callback_query(F.data == "mybots")
async def cb_mybots(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    bots = user_bots(call.from_user.id)
    if not bots:
        return await call.message.edit_text(
            "🤖 Ботов нет. Загрузи первого!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить", callback_data="upload")],
                [InlineKeyboardButton(text="« Меню",       callback_data="back_main")],
            ])
        )
    rows = []
    for b in bots:
        icon = "🟢" if b[0] in running_bots else "🔴"
        fc   = f" [{b[7]}ф]" if b[7] and b[7] > 1 else ""
        rows.append([InlineKeyboardButton(
            text=f"{icon} #{b[0]} {(b[2] or 'bot')[:18]}{fc}",
            callback_data=f"bot:{b[0]}"
        )])
    rows.append([InlineKeyboardButton(text="📤 Ещё", callback_data="upload")])
    rows.append([InlineKeyboardButton(text="« Меню", callback_data="back_main")])
    await call.message.edit_text(
        f"🤖 <b>Твои боты ({len(bots)})</b>\n🟢 работает · 🔴 стоп",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("bot:"))
async def cb_bot(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    b   = get_bot(bid)
    uid = call.from_user.id
    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    st  = "🟢 Работает" if bid in running_bots else "🔴 Стоп"
    sz  = b[8] or 0
    szs = f"{sz//1024} КБ" if sz < 1024*1024 else f"{sz/1024/1024:.1f} МБ"
    await call.message.edit_text(
        f"🤖 <b>Бот #{bid}</b>\n\n"
        f"📁 {b[2]}\n🐍 {b[3]}\n"
        f"📄 {b[7] or 1} файлов | 📏 {szs}\n"
        f"📊 {st} | 📅 {(b[6] or '')[:10]}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Старт", callback_data=f"start:{bid}"),
                InlineKeyboardButton(text="⏹ Стоп",  callback_data=f"stop:{bid}"),
            ],
            [InlineKeyboardButton(text="📄 Логи",          callback_data=f"logs:{bid}")],
            [InlineKeyboardButton(text="📁 Файлы",         callback_data=f"files:{bid}")],
            [InlineKeyboardButton(text="➕ Добавить файл", callback_data=f"addfile:{bid}")],
            [InlineKeyboardButton(text="🗑 Удалить",        callback_data=f"del:{bid}")],
            [InlineKeyboardButton(text="« Список",          callback_data="mybots")],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("start:"))
async def cb_start(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    bid = int(call.data.split(":")[1])
    b   = get_bot(bid)
    uid = call.from_user.id
    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    if not has_slot(uid):
        return await call.answer("❌ Нет слота", show_alert=True)
    if bid in running_bots:
        return await call.answer("⚠️ Уже запущен", show_alert=True)
    await call.answer("⏳ Запускаю...")
    mf  = b[3] or "main.py"
    tok = b[4] or ""
    ok  = await start_bot(bid, tok, mf)
    if ok:
        await call.message.answer(
            f"✅ <b>Бот #{bid} запущен!</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Логи", callback_data=f"logs:{bid}")],
                [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bid}")],
            ])
        )
    else:
        logs = html.escape(bot_logs(bid, 30)[:2000])
        hint = ""
        if "Conflict"      in logs: hint = "\n🔴 Токен уже используется!"
        elif "Unauthorized" in logs: hint = "\n🔴 Неверный токен!"
        elif "No module"    in logs:
            m = re.search(r"No module named '([^']+)'", logs)
            hint = f"\n💡 Нет библиотеки: {m.group(1) if m else '?'}"
        elif "SyntaxError"  in logs: hint = "\n💡 Синтаксическая ошибка"
        await call.message.answer(
            f"❌ <b>Бот #{bid} не запустился</b>{hint}\n\n<pre>{logs}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Снова",  callback_data=f"start:{bid}")],
                [InlineKeyboardButton(text="📄 Логи",   callback_data=f"logs:{bid}")],
                [InlineKeyboardButton(text="« К боту", callback_data=f"bot:{bid}")],
            ])
        )


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    b   = get_bot(bid)
    uid = call.from_user.id
    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    await stop_bot(bid)
    await call.answer("⏹ Остановлен")
    await cb_bot(call)


@dp.callback_query(F.data.startswith("logs:"))
async def cb_logs(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    bid = int(call.data.split(":")[1])
    b   = get_bot(bid)
    uid = call.from_user.id
    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    safe = html.escape(bot_logs(bid, 60)[:3500])
    await call.message.answer(
        f"📄 <b>Логи #{bid}</b>\n\n<pre>{safe}</pre>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"logs:{bid}")],
            [InlineKeyboardButton(text="« К боту",   callback_data=f"bot:{bid}")],
        ])
    )


@dp.callback_query(F.data.startswith("files:"))
async def cb_files(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    b   = get_bot(bid)
    uid = call.from_user.id
    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌ Нет доступа", show_alert=True)
    files  = bot_files(bid)
    main_f = b[3] or "main.py"
    txt    = f"📁 <b>Файлы #{bid}</b>\n\n"
    for f in files[:20]:
        sz  = f.stat().st_size
        szs = f"{sz:,} б" if sz < 1024 else f"{sz // 1024} КБ"
        ico = "👑" if f.name == main_f else "📄"
        txt += f"{ico} <code>{f.name}</code> — {szs}\n"
    py_list     = [f for f in files if f.name.endswith(".py")]
    change_rows = []
    if len(py_list) > 1:
        for f in py_list[:4]:
            if f.name != main_f:
                change_rows.append([InlineKeyboardButton(
                    text=f"👑 {f.name}",
                    callback_data=f"changemain:{bid}:{f.name}"
                )])
    await call.message.edit_text(
        txt,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            *change_rows,
            [InlineKeyboardButton(text="➕ Добавить", callback_data=f"addfile:{bid}")],
            [InlineKeyboardButton(text="« К боту",   callback_data=f"bot:{bid}")],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("changemain:"))
async def cb_changemain(call: types.CallbackQuery):
    parts   = call.data.split(":", 2)
    bid, mf = int(parts[1]), parts[2]
    b = get_bot(bid)
    if not b or (b[1] != call.from_user.id and not is_admin(call.from_user.id)):
        return await call.answer("❌", show_alert=True)
    update_main_file(bid, mf)
    await call.answer(f"✅ Главный: {mf}")
    await cb_files(call)


@dp.callback_query(F.data.startswith("addfile:"))
async def cb_addfile(call: types.CallbackQuery, state: FSMContext):
    bid = int(call.data.split(":")[1])
    b   = get_bot(bid)
    uid = call.from_user.id
    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌", show_alert=True)
    await call.message.edit_text(
        f"➕ <b>Добавить файл к #{bid}</b>\n\nОтправь файл:",
        parse_mode="HTML"
    )
    await state.update_data(bot_id=bid)
    await state.set_state(Upload.add_file)


@dp.callback_query(F.data.startswith("del:"))
async def cb_del(call: types.CallbackQuery):
    bid = int(call.data.split(":")[1])
    b   = get_bot(bid)
    uid = call.from_user.id
    if not b or (b[1] != uid and not is_admin(uid)):
        return await call.answer("❌", show_alert=True)
    await stop_bot(bid)
    try:
        shutil.rmtree(bot_dir(bid))
    except Exception:
        pass
    del_bot(bid)
    await call.answer("🗑 Удалён")
    await cb_mybots(call)


@dp.callback_query(F.data == "myslots")
async def cb_myslots(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid  = call.from_user.id
    slts = active_slots(uid)
    if uid == OWNER_ID or is_admin(uid):
        txt = "👑 Безлимит (навсегда)"
    elif not slts:
        txt = "💳 Нет активных слотов"
    else:
        txt = f"💳 <b>Слоты ({len(slts)}):</b>\n\n"
        for s in slts:
            exp  = datetime.fromisoformat(s[3])
            days = (exp - datetime.now()).days
            nm   = PLANS.get(s[2], {}).get("name", s[2])
            txt += f"• <b>{nm}</b> — {days} дн. (до {exp.strftime('%d.%m.%Y')})\n"
    await call.message.edit_text(
        txt,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Купить ещё", callback_data="buy")],
            [InlineKeyboardButton(text="« Меню",        callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "help")
async def cb_help(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "❓ <b>Помощь</b>\n\n"
        "<b>Покупка слота:</b>\n"
        "1. «Купить слот» → выбери тариф\n"
        "2. Отправь подарок владельцу\n"
        "3. Получишь кнопку активации\n\n"
        "<b>Загрузка бота:</b>\n"
        "1. «Загрузить бота» → .py/.zip\n"
        "2. Токен или none\n"
        "3. «Мои боты» → Старт\n\n"
        f"• .py до {MAX_PY_SIZE // 1024 // 1024} МБ\n"
        f"• .zip до {MAX_ZIP_SIZE // 1024 // 1024} МБ\n"
        f"• До {MAX_BOTS_PER_USER} ботов",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Владелец", url=owner_link())],
            [InlineKeyboardButton(text="« Меню",      callback_data="back_main")],
        ]),
        parse_mode="HTML",
        disable_web_page_preview=True
    )


# ─── ADMIN ────────────────────────────────────────────────────

async def show_admin(target, edit: bool = False):
    s  = get_stats()
    ub = f"{'🟢' if s['ub_ok'] else '🔴'} {s['ub_phone']}"
    txt = (
        f"🔐 <b>Админка</b>\n\n"
        f"👥 {s['total_users']} | 🚫 {s['banned']} | 🛡 {s['admins']}\n"
        f"💳 Слотов: {s['active_slots']}\n"
        f"🤖 Ботов: {s['total_bots']} (🟢{s['running']})\n"
        f"🎁 {s['gifts']} авто / {s['manual_payments']} ручных / {s['stars']}⭐\n"
        f"📡 Userbot: {ub}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 Userbot",         callback_data="adm:userbot")],
        [InlineKeyboardButton(text="💳 Выдать слот",     callback_data="adm:manualpay")],
        [InlineKeyboardButton(text="📢 Рассылка",         callback_data="adm:bc")],
        [InlineKeyboardButton(text="👥 Юзеры",            callback_data="adm:users")],
        [
            InlineKeyboardButton(text="🚫 Бан",   callback_data="adm:ban"),
            InlineKeyboardButton(text="✅ Разбан", callback_data="adm:unban"),
        ],
        [
            InlineKeyboardButton(text="🛡 +Админ", callback_data="adm:addadmin"),
            InlineKeyboardButton(text="❌ -Админ", callback_data="adm:remadmin"),
        ],
        [InlineKeyboardButton(text="🖼 Приветствие",    callback_data="adm:welcome")],
        [InlineKeyboardButton(text="🔄 Рестарт ботов",  callback_data="adm:restart")],
        [InlineKeyboardButton(text="« Меню",             callback_data="back_main")],
    ])
    if edit:
        await target.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(txt, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "admin")
async def cb_admin(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(call.from_user.id):
        return await call.answer("🔐", show_alert=True)
    await show_admin(call.message, edit=True)


@dp.callback_query(F.data == "adm:manualpay")
async def cb_adm_manualpay(call: types.CallbackQuery, state: FSMContext):
    """Ручное подтверждение оплаты."""
    if not is_admin(call.from_user.id):
        return await call.answer("🔐", show_alert=True)
    await call.message.edit_text(
        "💳 <b>Выдать слот вручную</b>\n\n"
        "Формат:\n"
        "<code>user_id тариф [звёзды] [заметка]</code>\n\n"
        "Тарифы: <code>week</code> / <code>2weeks</code> / <code>month</code>\n\n"
        "Примеры:\n"
        "<code>123456789 month</code>\n"
        "<code>123456789 week 15 оплата картой</code>\n\n"
        "Или используй команду:\n"
        "<code>/pay 123456789 month</code>\n\n"
        "Отмена: /cancel",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(Admin.manual_pay)


@dp.callback_query(F.data == "adm:userbot")
async def cb_adm_userbot(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⚠️ Только владелец", show_alert=True)

    client    = userbot_status.get("client")
    connected = userbot_status["connected"]

    if connected and client:
        try:
            me = await client.get_me()
            status_text = (
                f"🟢 <b>Подключён</b>\n"
                f"👤 {html.escape(me.first_name)} {html.escape(me.last_name or '')}\n"
                f"📱 {me.phone or OWNER_PHONE}\n"
                f"🆔 <code>{me.id}</code>"
            )
        except Exception:
            status_text = "🟡 Нестабильное соединение"
    else:
        status_text = f"🔴 <b>Не подключён</b>"

    has_session = bool(OWNER_SESSION)

    await call.message.edit_text(
        f"📡 <b>Userbot</b>\n\n"
        f"{status_text}\n\n"
        f"{'✅ OWNER_SESSION задан' if has_session else '❌ OWNER_SESSION не задан'}\n\n"
        f"Для создания сессии запусти локально:\n"
        f"<code>python make_session.py</code>\n"
        f"И добавь в Railway: OWNER_SESSION=...",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Перезапустить",
                callback_data="adm:ub_restart"
            )],
            [InlineKeyboardButton(
                text="🔄 Обновить статус",
                callback_data="adm:userbot"
            )],
            [InlineKeyboardButton(text="« Админка", callback_data="admin")],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm:ub_restart")
async def cb_ub_restart(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⚠️ Только владелец", show_alert=True)
    await call.answer("🔄 Перезапускаю...")
    client = userbot_status.get("client")
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    userbot_status["connected"] = False
    userbot_status["client"]    = None
    asyncio.create_task(run_userbot())
    await call.message.answer(
        "🔄 <b>Userbot перезапускается...</b>",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm:bc")
async def cb_adm_bc(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await call.message.edit_text("📢 Отправь текст рассылки (/cancel — отмена):")
    await state.set_state(Admin.broadcast)


@dp.callback_query(F.data == "adm:users")
async def cb_adm_users(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    users = all_users()
    txt   = f"👥 <b>Пользователи ({len(users)})</b>\n\n"
    for u in users[:20]:
        ban = "🚫" if u[4] else "✓"
        adm = "🛡" if u[3] else ""
        un  = f"@{u[1]}" if u[1] else (u[2] or "—")
        txt += f"{ban}{adm} <code>{u[0]}</code> {html.escape(str(un))}\n"
    if len(users) > 20:
        txt += f"...ещё {len(users) - 20}"
    await call.message.edit_text(
        txt,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm:ban")
async def cb_adm_ban(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await call.message.edit_text("🚫 ID или @username (/cancel — отмена):")
    await state.set_state(Admin.ban)


@dp.callback_query(F.data == "adm:unban")
async def cb_adm_unban(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await call.message.edit_text("✅ ID для разбана (/cancel — отмена):")
    await state.set_state(Admin.unban)


@dp.callback_query(F.data == "adm:addadmin")
async def cb_adm_addadmin(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⚠️ Только владелец", show_alert=True)
    await call.message.edit_text("🛡 ID или @username нового админа (/cancel — отмена):")
    await state.set_state(Admin.addadmin)


@dp.callback_query(F.data == "adm:remadmin")
async def cb_adm_remadmin(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⚠️ Только владелец", show_alert=True)
    admins = all_admins()
    if not admins:
        return await call.answer("Нет админов", show_alert=True)
    rows = [
        [InlineKeyboardButton(
            text=f"❌ {a[1] or a[2] or a[0]}",
            callback_data=f"remadm:{a[0]}"
        )]
        for a in admins
    ]
    rows.append([InlineKeyboardButton(text="« Отмена", callback_data="admin")])
    await call.message.edit_text(
        "❌ Удалить админа:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.callback_query(F.data.startswith("remadm:"))
async def cb_remadm(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return
    set_admin(int(call.data.split(":")[1]), 0)
    await call.answer("✅")
    await show_admin(call.message, edit=True)


@dp.callback_query(F.data == "adm:welcome")
async def cb_adm_welcome(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⚠️ Только владелец", show_alert=True)
    st = "✅" if WELCOME_IMG.exists() else "📭"
    await call.message.edit_text(
        f"🖼 Картинка: {st}\n\nОтправь фото или 'delete' (/cancel — отмена):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="admin")]
        ])
    )
    await state.set_state(Admin.welcome)


@dp.callback_query(F.data == "adm:restart")
async def cb_adm_restart(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.answer("⏳")
    for bid in list(running_bots.keys()):
        await stop_bot(bid)
    n = 0
    for b in all_bots():
        if is_banned(b[1]):
            continue
        if b[1] != OWNER_ID and not is_admin(b[1]) and not has_slot(b[1]):
            continue
        mf = b[3] or "main.py"
        if (bot_dir(b[0]) / mf).exists():
            if await start_bot(b[0], b[4] or "", mf):
                n += 1
    await call.message.answer(
        f"🔄 Перезапущено: {n}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Админка", callback_data="admin")]
        ])
    )


# ═══════════════════════════════════════════════════════════════
# 🎯 MAIN
# ═══════════════════════════════════════════════════════════════

async def main():
    init_db()
    log.info("=" * 55)
    log.info("🤖 BotHost v3.2")
    log.info(f"👤 Владелец: {OWNER_ID}")
    log.info(f"📡 Userbot: {'✅ сессия есть' if OWNER_SESSION else '❌ нет OWNER_SESSION'}")
    log.info("=" * 55)

    await restore_bots()
    asyncio.create_task(monitor())

    if OWNER_SESSION and OWNER_API_ID and OWNER_API_HASH:
        asyncio.create_task(run_userbot())
    else:
        log.warning(
            "Userbot не запущен.\n"
            "Нужны: OWNER_SESSION + OWNER_API_ID + OWNER_API_HASH\n"
            "Создай сессию: python make_session.py"
        )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
