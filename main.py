import ast
import asyncio
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSION_PATH = str(DATA_DIR / "telegram_userbot")
DB_PATH = DATA_DIR / "userbot.sqlite3"
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-userbot")
client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
owner_id = None
afk_reason = None
afk_replied = set()

HELP = """Userbot commands
.help — list commands
.ping — check connection
.id — show your user ID and current chat ID
.cal 2+2 — basic arithmetic
.todo text — add a to-do
.todos — list open to-dos
.done 1 — complete a to-do
.remind 10m text — set a reminder (10s to 30d)
.reminders — list pending reminders
.afk reason — enable private-chat away replies
.back — disable AFK replies
"""


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS todos (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
        db.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, due INTEGER NOT NULL, chat_id INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0)")


def calculate(expr):
    if not expr or len(expr) > 100:
        raise ValueError("Enter a short arithmetic expression, e.g. .cal 2+2")
    tree = ast.parse(expr, mode="eval")
    allowed = (ast.Expression, ast.Constant, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Load)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError("Only basic arithmetic is supported.")
        if isinstance(node, ast.Constant):
            if type(node.value) not in (int, float) or abs(node.value) > 10**12:
                raise ValueError("Only reasonably sized numbers are allowed.")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if not isinstance(node.right, ast.Constant) or type(node.right.value) not in (int, float) or abs(node.right.value) > 8:
                raise ValueError("Exponent must be a numeric value between -8 and 8.")
    result = eval(compile(tree, "<calculator>", "eval"), {"__builtins__": {}}, {})
    if type(result) not in (int, float) or abs(result) > 10**100:
        raise ValueError("Result is outside the supported range.")
    return result


def parse_duration(value):
    m = re.fullmatch(r"(\d{1,5})([smhd])", value.lower())
    if not m:
        raise ValueError("Use a duration such as 30s, 10m, 2h, or 1d.")
    seconds = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if not 10 <= seconds <= 30 * 86400:
        raise ValueError("Reminder duration must be between 10 seconds and 30 days.")
    return seconds


async def authorize_only():
    """Run once from Render Shell to authorize this account on the persistent disk."""
    await client.start()
    me = await client.get_me()
    log.info("Authorized Telegram account ID %s. Session stored on persistent disk.", me.id)
    await client.disconnect()


@client.on(events.NewMessage(outgoing=True))
async def command_handler(event):
    global afk_reason
    text = event.raw_text or ""
    if not text.startswith(".") or event.sender_id != owner_id:
        return
    command, _, arg = text.strip().partition(" ")
    command, arg = command.lower(), arg.strip()
    try:
        if command == ".help":
            await event.reply(HELP)
        elif command == ".ping":
            await event.reply("Pong — connected.")
        elif command == ".id":
            await event.reply(f"Your user ID: `{owner_id}`\nCurrent chat ID: `{event.chat_id}`")
        elif command == ".cal":
            await event.reply(f"Result: `{calculate(arg)}`")
        elif command == ".todo":
            if not arg:
                await event.reply("Usage: .todo buy milk")
                return
            with db_connect() as db:
                cur = db.execute("INSERT INTO todos(text) VALUES (?)", (arg[:500],))
                item_id = cur.lastrowid
            await event.reply(f"Added to-do #{item_id}: {arg[:500]}")
        elif command == ".todos":
            with db_connect() as db:
                rows = db.execute("SELECT id, text FROM todos WHERE done=0 ORDER BY id LIMIT 50").fetchall()
            await event.reply("\n".join(f"{r['id']}. {r['text']}" for r in rows) if rows else "No open to-dos.")
        elif command == ".done":
            if not arg.isdigit():
                await event.reply("Usage: .done 1")
                return
            with db_connect() as db:
                cur = db.execute("UPDATE todos SET done=1 WHERE id=? AND done=0", (int(arg),))
            await event.reply("To-do completed." if cur.rowcount else "Open to-do not found.")
        elif command == ".remind":
            parts = arg.split(maxsplit=1)
            if len(parts) != 2:
                await event.reply("Usage: .remind 10m take a break")
                return
            due = int(time.time()) + parse_duration(parts[0])
            with db_connect() as db:
                cur = db.execute("INSERT INTO reminders(text, due, chat_id) VALUES (?, ?, ?)", (parts[1][:500], due, event.chat_id))
                rid = cur.lastrowid
            await event.reply(f"Reminder #{rid} set.")
        elif command == ".reminders":
            with db_connect() as db:
                rows = db.execute("SELECT id, text, due FROM reminders WHERE sent=0 ORDER BY due LIMIT 30").fetchall()
            now = int(time.time())
            await event.reply("\n".join(f"#{r['id']} {r['text']} (in {max(0, r['due']-now)}s)" for r in rows) if rows else "No pending reminders.")
        elif command == ".afk":
            afk_reason = arg[:300] or "Away"
            afk_replied.clear()
            await event.reply(f"AFK enabled: {afk_reason}")
        elif command == ".back":
            afk_reason = None
            afk_replied.clear()
            await event.reply("AFK disabled.")
    except FloodWaitError as exc:
        log.warning("Telegram requested a flood wait of %s seconds", exc.seconds)
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
        await event.reply(f"Error: {exc}")
    except Exception:
        log.exception("Command failed")
        await event.reply("Command failed; check service logs.")


@client.on(events.NewMessage(incoming=True))
async def afk_handler(event):
    if not afk_reason or not event.is_private or event.sender_id == owner_id or event.sender_id in afk_replied:
        return
    afk_replied.add(event.sender_id)
    try:
        await event.reply(f"I'm away right now: {afk_reason}")
    except FloodWaitError as exc:
        log.warning("AFK response rate limited for %s seconds", exc.seconds)


async def reminder_loop():
    while True:
        now = int(time.time())
        with db_connect() as db:
            rows = db.execute("SELECT id, text, chat_id FROM reminders WHERE sent=0 AND due<=? LIMIT 20", (now,)).fetchall()
            for row in rows:
                db.execute("UPDATE reminders SET sent=1 WHERE id=?", (row["id"],))
        for row in rows:
            try:
                await client.send_message(row["chat_id"], f"⏰ Reminder: {row['text']}")
            except Exception:
                log.exception("Could not send reminder #%s", row["id"])
        await asyncio.sleep(5)


async def run_bot():
    global owner_id
    init_db()
    if not Path(SESSION_PATH + ".session").exists():
        raise RuntimeError("No session found. Open Render Shell and run: python main.py --login")
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Session is not authorized. Open Render Shell and run: python main.py --login")
    me = await client.get_me()
    owner_id = me.id
    log.info("Userbot connected as account ID %s", owner_id)
    asyncio.create_task(reminder_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    if "--login" in sys.argv:
        asyncio.run(authorize_only())
    else:
        try:
            asyncio.run(run_bot())
        except KeyboardInterrupt:
            pass
