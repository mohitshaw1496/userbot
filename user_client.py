
# user_client.py
import os
import re
import asyncio
import sqlite3
import calendar
from datetime import datetime

from telethon import TelegramClient, events
from telethon.sessions import StringSession

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_STRING = os.environ["TG_STRING_SESSION"]

DB_PATH = os.environ.get("TODO_DB", "todos.db")

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
)

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    task TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
)
""")
db.commit()


def add_todo(user_id, task):
    cursor = db.execute(
        "INSERT INTO todos (user_id, task) VALUES (?, ?)",
        (user_id, task),
    )
    db.commit()
    return cursor.lastrowid


def get_todos(user_id):
    return db.execute(
        "SELECT id, task, done FROM todos WHERE user_id=? ORDER BY id",
        (user_id,),
    ).fetchall()


def finish_todo(user_id, todo_id):
    cursor = db.execute(
        "UPDATE todos SET done=1 WHERE id=? AND user_id=?",
        (todo_id, user_id),
    )
    db.commit()
    return cursor.rowcount > 0


@client.on(events.NewMessage(outgoing=True, pattern=r"^\.help$"))
async def help_handler(event):
    await event.reply(
        "Commands:\n"
        ".help - Show commands\n"
        ".ping - Check response\n"
        ".id - Show chat and account IDs\n"
        ".cal - Show this month's calendar\n"
        ".todo add <task> - Add a task\n"
        ".todo list - List tasks\n"
        ".todo done <id> - Complete a task\n"
        ".remind <minutes> <message> - Set a reminder"
    )


@client.on(events.NewMessage(outgoing=True, pattern=r"^\.ping$"))
async def ping_handler(event):
    await event.reply("Pong!")


@client.on(events.NewMessage(outgoing=True, pattern=r"^\.id$"))
async def id_handler(event):
    me = await client.get_me()
    chat = await event.get_chat()
    await event.reply(
        f"Your user ID: {me.id}\n"
        f"Chat ID: {event.chat_id}\n"
        f"Chat: {getattr(chat, 'title', None) or getattr(chat, 'username', None) or 'Private chat'}"
    )


@client.on(events.NewMessage(outgoing=True, pattern=r"^\.cal$"))
async def calendar_handler(event):
    now = datetime.now()
    result = calendar.month(now.year, now.month)
    await event.reply(f"```text\n{result}\n```", parse_mode="md")


@client.on(events.NewMessage(outgoing=True, pattern=r"^\.todo(?:\s|$)"))
async def todo_handler(event):
    me = await client.get_me()
    parts = event.raw_text.split(maxsplit=2)

    if len(parts) < 2:
        await event.reply("Usage: .todo add <task> | .todo list | .todo done <id>")
        return

    action = parts[1].lower()

    if action == "add":
        if len(parts) < 3 or not parts[2].strip():
            await event.reply("Usage: .todo add Buy milk")
            return
        todo_id = add_todo(me.id, parts[2].strip())
        await event.reply(f"Added task #{todo_id}.")

    elif action == "list":
        rows = get_todos(me.id)
        if not rows:
            await event.reply("Your todo list is empty.")
            return

        lines = [
            f"{'✅' if done else '⬜'} {todo_id}. {task}"
            for todo_id, task, done in rows
        ]
        await event.reply("\n".join(lines))

    elif action == "done":
        if len(parts) < 3 or not parts[2].isdigit():
            await event.reply("Usage: .todo done 1")
            return
        if finish_todo(me.id, int(parts[2])):
            await event.reply("Task marked complete.")
        else:
            await event.reply("Task not found.")

    else:
        await event.reply("Use .todo add, .todo list, or .todo done.")


@client.on(events.NewMessage(outgoing=True, pattern=r"^\.remind\s+"))
async def remind_handler(event):
    match = re.match(
        r"^\.remind\s+(\d+)\s+(.+)$",
        event.raw_text,
        re.DOTALL,
    )
    if not match:
        await event.reply("Usage: .remind 10 Take a break")
        return

    minutes = int(match.group(1))
    message = match.group(2).strip()

    if minutes < 1 or minutes > 10080:
        await event.reply("Choose between 1 minute and 7 days.")
        return

    await event.reply(f"Reminder set for {minutes} minute(s).")

    async def send_reminder():
        await asyncio.sleep(minutes * 60)
        try:
            await client.send_message(
                event.chat_id,
                f"⏰ Reminder: {message}",
            )
        except Exception as exc:
            print(f"Could not send reminder: {type(exc).__name__}")

    asyncio.create_task(send_reminder())


async def main():
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telegram session is not authorized. "
            "Generate a valid session locally."
        )

    me = await client.get_me()
    print(f"Connected as Telegram user ID {me.id}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
