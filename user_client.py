
import ast
import asyncio
import os
import re
import sqlite3
import time
from pathlib import Path

from telethon import TelegramClient, events

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

DATA = Path(os.environ.get("DATA_DIR", "data"))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "user.sqlite3"

client = TelegramClient(str(DATA / "account"), API_ID, API_HASH)
owner_id = None
afk_reason = None
afk_replied = set()


def init_db():
    with sqlite3.connect(DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                done INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                due INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                sent INTEGER DEFAULT 0
            )
        """)


def calc(expression):
    if len(expression) > 100:
        raise ValueError("Expression too long")

    tree = ast.parse(expression, mode="eval")
    allowed = (
        ast.Expression, ast.Constant, ast.BinOp, ast.UnaryOp,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
        ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Load,
    )

    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError("Basic arithmetic only")
        if isinstance(node, ast.Constant):
            if type(node.value) not in (int, float):
                raise ValueError("Numbers only")
            if abs(node.value) > 10**12:
                raise ValueError("Number too large")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if not isinstance(node.right, ast.Constant):
                raise ValueError("Use a numeric exponent")
            if abs(node.right.value) > 8:
                raise ValueError("Exponent too large")

    result = eval(
        compile(tree, "<calc>", "eval"),
        {"__builtins__": {}},
        {},
    )
    if not isinstance(result, (int, float)) or abs(result) > 10**100:
        raise ValueError("Result out of range")
    return result


def parse_duration(value):
    match = re.fullmatch(r"(\d{1,5})([smhd])", value.lower())
    if not match:
        raise ValueError("Use 30s, 10m, 2h or 1d")
    seconds = int(match.group(1)) * {
        "s": 1, "m": 60, "h": 3600, "d": 86400
    }[match.group(2)]
    if not 10 <= seconds <= 30 * 86400:
        raise ValueError("Duration must be 10 seconds to 30 days")
    return seconds


HELP = """Commands:
.help
.ping
.id
.cal 2+2
.todo text
.todos
.done 1
.remind 10m text
.reminders
.afk reason
.back"""


@client.on(events.NewMessage(outgoing=True))
async def on_command(event):
    global afk_reason

    text = (event.raw_text or "").strip()
    if event.sender_id != owner_id or not text.startswith("."):
        return

    command, _, arg = text.partition(" ")
    command, arg = command.lower(), arg.strip()

    try:
        if command == ".help":
            await event.reply(HELP)

        elif command == ".ping":
            await event.reply("Pong — connected.")

        elif command == ".id":
            await event.reply(
                f"User ID: `{owner_id}`\nChat ID: `{event.chat_id}`"
            )

        elif command == ".cal":
            await event.reply(f"Result: `{calc(arg)}`")

        elif command == ".todo":
            if not arg:
                await event.reply("Usage: .todo buy milk")
                return
            with sqlite3.connect(DB) as conn:
                cur = conn.execute(
                    "INSERT INTO todos(text) VALUES (?)", (arg[:500],)
                )
                todo_id = cur.lastrowid
            await event.reply(f"Added to-do #{todo_id}")

        elif command == ".todos":
            with sqlite3.connect(DB) as conn:
                rows = conn.execute(
                    "SELECT id, text FROM todos WHERE done=0 ORDER BY id"
                ).fetchall()
            await event.reply(
                "\n".join(f"{i}. {t}" for i, t in rows[:50])
                if rows else "No open to-dos."
            )

        elif command == ".done":
            if not arg.isdigit():
                await event.reply("Usage: .done 1")
                return
            with sqlite3.connect(DB) as conn:
                cur = conn.execute(
                    "UPDATE todos SET done=1 WHERE id=? AND done=0",
                    (int(arg),),
                )
            await event.reply(
                "Completed." if cur.rowcount else "To-do not found."
            )

        elif command == ".remind":
            parts = arg.split(maxsplit=1)
            if len(parts) != 2:
                await event.reply("Usage: .remind 10m take a break")
                return
            due = int(time.time()) + parse_duration(parts[0])
            with sqlite3.connect(DB) as conn:
                cur = conn.execute(
                    """INSERT INTO reminders(text, due, chat_id)
                       VALUES (?, ?, ?)""",
                    (parts[1][:500], due, event.chat_id),
                )
                reminder_id = cur.lastrowid
            await event.reply(f"Reminder #{reminder_id} created.")

        elif command == ".reminders":
            with sqlite3.connect(DB) as conn:
                rows = conn.execute(
                    """SELECT id, text, due FROM reminders
                       WHERE sent=0 ORDER BY due LIMIT 30"""
                ).fetchall()
            now = int(time.time())
            await event.reply(
                "\n".join(
                    f"#{i}: {t} (in {max(0, due-now)} sec)"
                    for i, t, due in rows
                ) if rows else "No pending reminders."
            )

        elif command == ".afk":
            afk_reason = arg[:300] or "Away"
            afk_replied.clear()
            await event.reply(f"AFK enabled: {afk_reason}")

        elif command == ".back":
            afk_reason = None
            afk_replied.clear()
            await event.reply("AFK disabled.")

    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
        await event.reply(f"Error: {exc}")


@client.on(events.NewMessage(incoming=True))
async def on_incoming(event):
    if (
        afk_reason
        and event.is_private
        and event.sender_id != owner_id
        and event.sender_id not in afk_replied
    ):
        afk_replied.add(event.sender_id)
        await event.reply(f"I'm away: {afk_reason}")


async def reminder_loop():
    while True:
        now = int(time.time())
        with sqlite3.connect(DB) as conn:
            rows = conn.execute(
                """SELECT id, text, chat_id FROM reminders
                   WHERE sent=0 AND due<=? LIMIT 20""",
                (now,),
            ).fetchall()
            for reminder_id, _, _ in rows:
                conn.execute(
                    "UPDATE reminders SET sent=1 WHERE id=?",
                    (reminder_id,),
                )

        for reminder_id, text, chat_id in rows:
            try:
                await client.send_message(chat_id, f"⏰ Reminder: {text}")
            except Exception as exc:
                print(f"Reminder {reminder_id} failed: {exc}")

        await asyncio.sleep(5)


async def main():
    global owner_id
    init_db()
    await client.start()
    owner_id = (await client.get_me()).id
    print(f"Connected as Telegram user ID {owner_id}")
    asyncio.create_task(reminder_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
