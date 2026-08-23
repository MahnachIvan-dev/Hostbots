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
# 🔧 КОНФИГУРАЦИЯ (все чувствительные данные — из переменных окружения)
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")          # Токен бота (обязательно)
OWNER_ID       = int(os.environ.get("OWNER_ID", "0"))    # ID владельца (обязательно)
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "")
OWNER_PHONE    = os.environ.get("OWNER_PHONE", "")
OWNER_API_ID   = os.environ.get("OWNER_API_ID", "")
OWNER_API_HASH = os.environ.get("OWNER_API_HASH", "")
OWNER_SESSION  = os.environ.get("OWNER_SESSION", "")   # ← StringSession строка (для userbot)

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

if not BOT_TOKEN or OWNER_ID == 0:
    log.error("❌ BOT_TOKEN и OWNER_ID должны быть заданы в переменных окружения!")
    sys.exit(1)

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

            #
