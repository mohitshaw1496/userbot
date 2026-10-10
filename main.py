
import os
import ast
import operator
import asyncio
import sqlite3
import threading
from datetime import datetime

from flask import Flask, jsonify
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from deep_translator import GoogleTranslator


# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing from Render environment variables.")
if not API_ID or not API_HASH:
    raise RuntimeError("TG_API_ID or TG_API_HASH is missing.")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID is missing.")

app = Flask(__name__)

user_client = None
login_waiting = set()

afk_reason = None
autocal = False
reminder_counter = 0
reminder_tasks = {}


# =========================================================
# DATABASE
# =========================================================

DB_PATH = "todos.db"


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                task TEXT NOT NULL,
                done INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)


def add_todo(task):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO todos (owner_id, task, done, created_at) "
            "VALUES (?, ?, 0, ?)",
            (OWNER_ID, task, datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def get_todos(include_done=False):
    with sqlite3.connect(DB_PATH) as conn:
        if include_done:
            rows = conn.execute(
                "SELECT id, task, done FROM todos "
                "WHERE owner_id = ? ORDER BY id",
                (OWNER_ID,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, task, done FROM todos "
                "WHERE owner_id = ? AND done = 0 ORDER BY id",
                (OWNER_ID,),
            ).fetchall()
    return rows


def finish_todo(todo_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE todos SET done = 1 WHERE id = ? AND owner_id = ?",
            (todo_id, OWNER_ID),
        )
        return cur.rowcount > 0


init_db()


# =========================================================
# FLASK / RENDER HEALTH CHECK
# =========================================================

@app.route("/")
def home():
    return "Telegram manager is running."


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "userbot_connected": bool(
            user_client and user_client.is_connected()
        ),
    })


def run_web():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


# =========================================================
# SAFE CALCULATOR
# =========================================================

_ALLOWED_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def calculate(expression):
    tree = ast.parse(expression, mode="eval")

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)

        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value

        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in _ALLOWED_OPERATORS:
                raise ValueError("Operator not allowed.")

            left = evaluate(node.left)
            right = evaluate(node.right)

            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponent is too large.")

            result = _ALLOWED_OPERATORS[op_type](left, right)

            if isinstance(result, (int, float)) and abs(result) > 1e100:
                raise ValueError("Result is too large.")

            return result

        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)
            if op_type not in _ALLOWED_OPERATORS:
                raise ValueError("Operator not allowed.")
            return _ALLOWED_OPERATORS[op_type](evaluate(node.operand))

        raise ValueError("Invalid expression.")

    return evaluate(tree)


def parse_duration(value):
    value = value.strip().lower()

    units = {
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
    }

    number = ""
    unit = ""

    for char in value:
        if char.isdigit() or char == ".":
            if unit:
                raise ValueError("Use a duration like 30m or 2h.")
            number += char
        else:
            unit += char

    if not number or not unit or unit not in units:
        raise ValueError("Use a duration like 30s, 10m, 2h, or 1d.")

    seconds = float(number) * units[unit]

    if seconds <= 0 or seconds > 7 * 86400:
        raise ValueError("Duration must be between 1 second and 7 days.")

    return seconds


# =========================================================
# USERBOT COMMANDS
# Commands are sent from your own Telegram account.
# =========================================================

async def handle_user_commands(client, event):
    global afk_reason, autocal, reminder_counter

    text = (event.raw_text or "").strip()
    if not text:
        return

    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    # Ignore regular messages unless auto-calculator is enabled.
    if not command.startswith("."):
        if autocal:
            try:
                result = calculate(text)
                await event.reply(f"= {result}")
            except Exception:
                pass
        return

    if command == ".help":
        await event.reply(
            "**Userbot commands**\n\n"
            "`.help` — show commands\n"
            "`.ping` — check response\n"
            "`.id` — show chat/user ID\n"
            "`.cal 2+2` — calculate\n"
            "`.autocal on/off` — auto calculator\n"
            "`.todo task` — add a task\n"
            "`.todos` — list unfinished tasks\n"
            "`.done 1` — finish a task\n"
            "`.remind 10m text` — set a reminder\n"
            "`.reminders` — list active reminders\n"
            "`.afk reason` — enable AFK replies\n"
            "`.back` — disable AFK replies\n"
            "`.purge 10` — delete your last N messages\n"
            "`.cleanall` — delete your last 100 messages\n"
            "`.pfp` — send your profile photo\n"
            "Reply to a user with `.block` or `.unblock`\n"
            "Reply to a message with `.copy`\n"
            "`.quote text` — format a quote\n"
            "`.translate es hello` — translate text\n"
            "`.spam 3 text` — send up to 3 messages"
        )
        return

    if command == ".ping":
        await event.reply("Pong!")
        return

    if command == ".id":
        chat = await event.get_chat()
        reply = await event.get_reply_message()
        user_id = reply.sender_id if reply else event.sender_id
        await event.reply(
            f"Chat ID: `{event.chat_id}`\nUser ID: `{user_id}`"
        )
        return

    if command == ".cal":
        if not argument:
            await event.reply("Usage: `.cal 2+2`")
            return
        try:
            await event.reply(f"= {calculate(argument)}")
        except Exception:
            await event.reply("Invalid calculation.")
        return

    if command == ".autocal":
        value = argument.lower()
        if value not in ("on", "off"):
            await event.reply("Usage: `.autocal on` or `.autocal off`")
            return
        autocal = value == "on"
        await event.reply(f"Auto-calculator: {'ON' if autocal else 'OFF'}")
        return

    if command == ".todo":
        if not argument:
            await event.reply("Usage: `.todo Buy groceries`")
            return
        todo_id = add_todo(argument)
        await event.reply(f"Added task #{todo_id}: {argument}")
        return

    if command == ".todos":
        todos = get_todos()
        if not todos:
            await event.reply("You have no unfinished tasks.")
            return
        lines = [f"#{row[0]} — {row[1]}" for row in todos]
        await event.reply("**Your tasks**\n" + "\n".join(lines))
        return

    if command == ".done":
        try:
            todo_id = int(argument)
        except ValueError:
            await event.reply("Usage: `.done 1`")
            return

        if finish_todo(todo_id):
            await event.reply(f"Task #{todo_id} completed.")
        else:
            await event.reply("Task not found or already completed.")
        return

    if command == ".remind":
        reminder_parts = argument.split(maxsplit=1)
        if len(reminder_parts) != 2:
            await event.reply("Usage: `.remind 10m take a break`")
            return

        try:
            seconds = parse_duration(reminder_parts[0])
        except ValueError as exc:
            await event.reply(str(exc))
            return

        reminder_counter += 1
        reminder_id = reminder_counter
        chat_id = event.chat_id
        reminder_text = reminder_parts[1]

        async def send_reminder():
            try:
                await asyncio.sleep(seconds)
                await client.send_message(
                    chat_id,
                    f"⏰ Reminder #{reminder_id}: {reminder_text}",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("Reminder error:", type(exc).__name__)
            finally:
                reminder_tasks.pop(reminder_id, None)

        task = asyncio.create_task(send_reminder())
        reminder_tasks[reminder_id] = {
            "task": task,
            "text": reminder_text,
            "chat_id": chat_id,
        }

        await event.reply(f"Reminder #{reminder_id} set.")
        return

    if command == ".reminders":
        if not reminder_tasks:
            await event.reply("No active reminders.")
            return

        lines = [
            f"#{rid}: {info['text']}"
            for rid, info in reminder_tasks.items()
        ]
        await event.reply("**Active reminders**\n" + "\n".join(lines))
        return

    if command == ".afk":
        afk_reason = argument or "Away"
        await event.reply(f"AFK enabled: {afk_reason}")
        return

    if command == ".back":
        afk_reason = None
        await event.reply("Welcome back! AFK disabled.")
        return

    if command in (".purge", ".cleanall"):
        if command == ".cleanall":
            count = 100
        else:
            try:
                count = int(argument)
            except ValueError:
                await event.reply("Usage: `.purge 10`")
                return

        if count < 1 or count > 100:
            await event.reply("Choose a number between 1 and 100.")
            return

        deleted = 0
        try:
            async for message in client.iter_messages(
                event.chat_id, limit=count + 1
            ):
                if message.out:
                    await message.delete()
                    deleted += 1
        except Exception as exc:
            print("Purge error:", type(exc).__name__)
            return

        # The command may have deleted itself, so don't reply afterward.
        return

    if command == ".pfp":
        try:
            photo = await client.download_profile_photo("me", file=bytes)
            if not photo:
                await event.reply("No profile photo found.")
                return
            await client.send_file(event.chat_id, photo, caption="My profile photo")
        except Exception as exc:
            print("Profile photo error:", type(exc).__name__)
            await event.reply("Could not retrieve the profile photo.")
        return

    if command in (".block", ".unblock"):
        reply = await event.get_reply_message()
        if not reply:
            await event.reply(f"Reply to a user's message with `{command}`.")
            return

        try:
            target = await reply.get_sender()
            if command == ".block":
                await client.block_user(target)
                await event.reply("User blocked.")
            else:
                await client.unblock_user(target)
                await event.reply("User unblocked.")
        except Exception as exc:
            print("Block command error:", type(exc).__name__)
            await event.reply("Could not complete that action.")
        return

    if command == ".copy":
        reply = await event.get_reply_message()
        if not reply:
            await event.reply("Reply to a text message with `.copy`.")
            return
        if reply.raw_text:
            await client.send_message(event.chat_id, reply.raw_text)
        else:
            await event.reply("The replied message has no text to copy.")
        return

    if command == ".quote":
        if not argument:
            await event.reply("Usage: `.quote text`")
            return
        await event.reply(f"❝ {argument} ❞")
        return

    if command == ".translate":
        translation_parts = argument.split(maxsplit=1)
        if len(translation_parts) != 2:
            await event.reply("Usage: `.translate es Hello there`")
            return

        language, source_text = translation_parts
        try:
            translated = await asyncio.to_thread(
                GoogleTranslator(
                    source="auto",
                    target=language
                ).translate,
                source_text,
            )
            await event.reply(translated)
        except Exception as exc:
            print("Translation error:", type(exc).__name__)
            await event.reply(
                "Translation failed. Check the language code, "
                "for example `es`, `fr`, or `hi`."
            )
        return

    if command == ".spam":
        spam_parts = argument.split(maxsplit=1)
        if len(spam_parts) != 2:
            await event.reply("Usage: `.spam 3 Hello`")
            return

        try:
            count = int(spam_parts[0])
        except ValueError:
            await event.reply("Enter a valid count.")
            return

        if count < 1 or count > 5:
            await event.reply("Count must be between 1 and 5.")
            return

        # Use only in chats where repeated messages are welcome.
        for _ in range(count):
            await client.send_message(event.chat_id, spam_parts[1])
            await asyncio.sleep(1)
        return


# =========================================================
# CONNECT / DISCONNECT USER ACCOUNT
# =========================================================

async def connect_user(session):
    global user_client

    candidate = TelegramClient(
        StringSession(session.strip()),
        API_ID,
        API_HASH,
    )

    try:
        await candidate.connect()

        if not await candidate.is_user_authorized():
            await candidate.disconnect()
            return False, "Session is invalid or unauthorized."

        me = await candidate.get_me()
        if not me:
            await candidate.disconnect()
            return False, "Could not identify the account."

        # Register handlers only after successful authorization.
        @candidate.on(events.NewMessage(outgoing=True))
        async def outgoing_commands(event):
            try:
                await handle_user_commands(candidate, event)
            except Exception as exc:
                print("Command handler error:", type(exc).__name__)

        @candidate.on(events.NewMessage(incoming=True))
        async def incoming_afk(event):
            if not afk_reason or event.out or not event.is_private:
                return
            if event.sender_id == OWNER_ID:
                return

            try:
                await event.reply(f"I'm AFK: {afk_reason}")
            except Exception as exc:
                print("AFK handler error:", type(exc).__name__)

        old = user_client
        user_client = candidate

        if old and old.is_connected():
            await old.disconnect()

        return True, f"Connected as {me.first_name}."

    except Exception as exc:
        if candidate.is_connected():
            await candidate.disconnect()
        print("User connection error:", type(exc).__name__)
        return False, "Could not connect. Check Render logs."


# =========================================================
# MANAGEMENT BOT
# =========================================================

def is_owner(update: Update):
    return (
        update.effective_user is not None
        and update.effective_user.id == OWNER_ID
    )


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        return

    await update.effective_message.reply_text(
        "Telegram manager is online.\n\n"
        "Use /login to connect your user account.\n"
        "Use /status to check the connection.\n"
        "Use /help to see management commands."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        return

    await update.effective_message.reply_text(
        "/login — connect with a Telethon StringSession\n"
        "/cancel — cancel login input\n"
        "/status — show connection status\n\n"
        "After login, send userbot commands from your own account, "
        "such as .help, .ping, .cal 2+2, and .todo test."
    )


async def login_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        return

    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "For security, use /login in a private chat."
        )
        return

    login_waiting.add(OWNER_ID)
    await update.effective_message.reply_text(
        "Send your Telethon StringSession in your next message.\n"
        "Keep it private: anyone with this session may access your account.\n"
        "Use /cancel to cancel."
    )


async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        return

    login_waiting.discard(OWNER_ID)
    await update.effective_message.reply_text("Login cancelled.")


async def receive_session(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        return

    if OWNER_ID not in login_waiting:
        return

    if update.effective_chat.type != "private":
        return

    session = (update.effective_message.text or "").strip()
    if not session or session.startswith("/"):
        return

    login_waiting.discard(OWNER_ID)

    # Delete the session message as soon as possible.
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    status_message = await context.bot.send_message(
        chat_id=OWNER_ID,
        text="Connecting your Telegram account...",
    )

    try:
        success, message = await connect_user(session)
        await status_message.edit_text(message)
    except Exception as exc:
        print("Login error:", type(exc).__name__)
        await status_message.edit_text(
            "Login failed. Check the Render logs and try /login again."
        )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        return

    connected = bool(user_client and user_client.is_connected())

    if connected:
        try:
            me = await user_client.get_me()
            label = me.first_name if me else "Unknown"
            await update.effective_message.reply_text(
                f"Userbot: connected as {label}"
            )
        except Exception:
            await update.effective_message.reply_text(
                "Userbot: connected, but account lookup failed."
            )
    else:
        await update.effective_message.reply_text("Userbot: disconnected.")


async def error_handler(update, context):
    error = context.error
    print("Management bot error:", type(error).__name__)


def run_bot():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("status", status_command))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_session,
        )
    )

    application.add_error_handler(error_handler)
    application.run_polling(drop_pending_updates=True)


# =========================================================
# MAIN
# =========================================================

def main():
    threading.Thread(target=run_web, daemon=True).start()
    run_bot()


if __name__ == "__main__":
    main()
