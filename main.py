
import asyncio
import os
import sqlite3
import threading
from datetime import datetime

from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
OWNER_ID = int(os.environ["OWNER_ID"])
PORT = int(os.environ.get("PORT", "10000"))

user_client = None
login_waiting = set()

db = sqlite3.connect("todos.db", check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    done INTEGER DEFAULT 0
)
""")
db.commit()

web = Flask(__name__)


@web.get("/")
@web.get("/health")
def health():
    return {"ok": True}, 200


def run_web():
    web.run(host="0.0.0.0", port=PORT, use_reloader=False)


async def connect_user(session):
    global user_client

    candidate = None

    try:
        candidate = TelegramClient(
            StringSession(session.strip()),
            API_ID,
            API_HASH,
        )

        await candidate.connect()

        if not await candidate.is_user_authorized():
            await candidate.disconnect()
            return False, "Session is not authorized."

        me = await candidate.get_me()

        if me is None:
            await candidate.disconnect()
            return False, "Unable to identify the account."

        @candidate.on(events.NewMessage(outgoing=True))
        async def commands(event):
            text = (event.raw_text or "").strip()

            if not text.startswith("."):
                return

            parts = text.split()
            cmd = parts[0].lower()

            if cmd == ".help":
                await event.reply(
                    ".help — show commands\n"
                    ".ping — test response\n"
                    ".id — show IDs\n"
                    ".cal — date and time\n"
                    ".todo add <task>\n"
                    ".todo list\n"
                    ".todo done <id>\n"
                    ".remind <minutes> <message>"
                )

            elif cmd == ".ping":
                await event.reply("Pong!")

            elif cmd == ".id":
                sender = await event.get_sender()
                await event.reply(
                    f"Chat ID: {event.chat_id}\n"
                    f"Your ID: {getattr(sender, 'id', 'unknown')}"
                )

            elif cmd == ".cal":
                now = datetime.now().astimezone()
                await event.reply(now.strftime("%A, %d %B %Y, %I:%M:%S %p %Z"))

            elif cmd == ".todo":
                if len(parts) >= 3 and parts[1].lower() == "add":
                    task = text.split(maxsplit=2)[2]
                    cur = db.execute(
                        "INSERT INTO todos(task) VALUES (?)", (task,)
                    )
                    db.commit()
                    await event.reply(f"Task #{cur.lastrowid} added.")

                elif len(parts) == 2 and parts[1].lower() == "list":
                    rows = db.execute(
                        "SELECT id, task, done FROM todos ORDER BY id DESC"
                    ).fetchall()

                    if not rows:
                        await event.reply("Your list is empty.")
                    else:
                        await event.reply("\n".join(
                            f"{'✓' if done else '○'} #{tid}: {task}"
                            for tid, task, done in rows[:30]
                        ))

                elif (
                    len(parts) == 3
                    and parts[1].lower() == "done"
                    and parts[2].isdigit()
                ):
                    cur = db.execute(
                        "UPDATE todos SET done=1 WHERE id=?",
                        (int(parts[2]),),
                    )
                    db.commit()
                    await event.reply(
                        "Task completed." if cur.rowcount else "Task not found."
                    )
                else:
                    await event.reply(
                        "Usage: .todo add <task> | .todo list | .todo done <id>"
                    )

            elif cmd == ".remind":
                if len(parts) < 3 or not parts[1].isdigit():
                    await event.reply(".remind <minutes> <message>")
                    return

                minutes = int(parts[1])
                reminder_text = text.split(maxsplit=2)[2]

                if not 1 <= minutes <= 10080:
                    await event.reply("Use a delay from 1 to 10080 minutes.")
                    return

                await event.reply(f"Reminder set for {minutes} minute(s).")

                async def send_reminder():
                    await asyncio.sleep(minutes * 60)
                    try:
                        await candidate.send_message(
                            event.chat_id,
                            f"⏰ Reminder: {reminder_text}",
                        )
                    except Exception:
                        pass

                asyncio.create_task(send_reminder())

        old = user_client
        user_client = candidate

        if old and old.is_connected():
            await old.disconnect()

        return True, f"Connected as {me.first_name}."

    except Exception as exc:
        if candidate:
            try:
                await candidate.disconnect()
            except Exception:
                pass

        # Do not log the session or exception contents.
        return False, f"Login failed ({type(exc).__name__}). Check your session format."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Userbot manager is online.\n"
        "/login — connect your account\n"
        "/status — connection status\n"
        "/help — commands"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Management commands:\n"
        "/login\n/status\n/cancel\n\n"
        "After login, send these commands from your user account:\n"
        ".help, .ping, .id, .cal\n"
        ".todo add <task>\n.todo list\n.todo done <id>\n"
        ".remind <minutes> <message>"
    )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat

    if not user or user.id != OWNER_ID:
        await message.reply_text("Not authorized.")
        return

    if not chat or chat.type != "private":
        await message.reply_text("Use /login in a private chat.")
        return

    login_waiting.add(user.id)
    await message.reply_text(
        "Send your Telethon StringSession in your next message.\n"
        "Use /cancel to cancel. Never send the session in a group."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        login_waiting.discard(update.effective_user.id)

    await update.effective_message.reply_text("Login cancelled.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global user_client

    if not update.effective_user or update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text("Not authorized.")
        return

    connected = bool(
        user_client
        and user_client.is_connected()
        and await user_client.is_user_authorized()
    )

    await update.effective_message.reply_text(
        "Userbot connected." if connected else "Userbot disconnected."
    )


async def receive_session(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat

    if not user or user.id not in login_waiting:
        return

    if user.id != OWNER_ID or not chat or chat.type != "private":
        login_waiting.discard(user.id)
        return

    session = (message.text or "").strip()

    if not session or session.startswith("/"):
        await message.reply_text("Send the session as plain text, or /cancel.")
        return

    login_waiting.discard(user.id)

    # Best effort only; deletion may fail.
    try:
        await message.delete()
    except Exception:
        pass

    progress = await context.bot.send_message(
        chat_id=chat.id,
        text="Validating session…",
    )

    try:
        success, result = await connect_user(session)
    finally:
        session = ""

    await progress.edit_text(result)


async def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("login", login))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("status", status))

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            receive_session,
        )
    )

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(run_bot())
