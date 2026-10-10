
import ast
import asyncio
import io
import operator
import os
import re
import sqlite3
import threading
from datetime import datetime

from flask import Flask
from PIL import Image, ImageDraw, ImageFont
from deep_translator import GoogleTranslator
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
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
OWNER_ID = int(os.environ["OWNER_ID"])
PORT = int(os.environ.get("PORT", "10000"))

user_client = None
login_waiting = set()
afk_reason = None
autocal = False
reminders = {}
reminder_counter = 0
reminder_tasks = {}

db = sqlite3.connect("todos.db", check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
)
""")
db.commit()

web = Flask(__name__)


@web.get("/")
@web.get("/health")
def health():
    connected = bool(user_client and user_client.is_connected())
    return {"ok": True, "userbot_connected": connected}, 200


def run_web():
    web.run(host="0.0.0.0", port=PORT, use_reloader=False)


def calculate(expression):
    """Safely evaluate basic arithmetic without executing Python code."""
    tree = ast.parse(expression, mode="eval")

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)

        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value

        if isinstance(node, ast.UnaryOp):
            value = evaluate(node.operand)
            if isinstance(node.op, ast.UAdd):
                return value
            if isinstance(node.op, ast.USub):
                return -value

        if isinstance(node, ast.BinOp):
            operations = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: operator.truediv,
                ast.FloorDiv: operator.floordiv,
                ast.Mod: operator.mod,
                ast.Pow: operator.pow,
            }
            fn = operations.get(type(node.op))
            if fn is None:
                raise ValueError("Unsupported operator")

            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("Exponent too large")

            result = fn(left, right)
            if not isinstance(result, (int, float)) or abs(result) > 1e100:
                raise ValueError("Result out of range")
            return result

        raise ValueError("Invalid expression")

    return evaluate(tree)


def parse_duration(value):
    match = re.fullmatch(r"(\d+)(s|m|h|d)?", value.lower())
    if not match:
        raise ValueError("Invalid duration")
    amount = int(match.group(1))
    unit = match.group(2) or "m"
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if not 1 <= seconds <= 7 * 86400:
        raise ValueError("Duration must be between 1 second and 7 days")
    return seconds



async def connect_user(session):
    global user_client

    candidate = TelegramClient(
        StringSession(session.strip()),
        API_ID,
        API_HASH
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

        # Register outgoing user commands.
        @candidate.on(events.NewMessage(outgoing=True))
        async def outgoing_commands(event):
            try:
                await handle_user_commands(candidate, event)
            except Exception as exc:
                print("Command handler error:", type(exc).__name__)

        # Reply to private messages while AFK.
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
        return False, "Could not connect. Check the Render logs."

async def outgoing_commands(event):
    try:
        await handle_user_commands(candidate, event)
    except Exception:
        # Avoid logging private messages or session credentials.
        pass

# Auto-reply to private messages while AFK.
@candidate.on(events.NewMessage(incoming=True))
async def incoming_afk(event):
    if not afk_reason or event.out or not event.is_private:
        return

    if event.sender_id == OWNER_ID:
        return

    try:
        await event.reply(f"I'm AFK: {afk_reason}")
    except Exception:
        pass

old = user_client
user_client = candidate

if old and old.is_connected():
    await old.disconnect()

return True, f"Connected as {me.first_name}."


        if old and old.is_connected():
            await old.disconnect()

        return True, f"Connected as {me.first_name}."

    except Exception as exc:
        if candidate:
            try:
                await candidate.disconnect()
            except Exception:
                pass
        # Do not include exception text, credentials, or session data in replies.
        return False, f"Login failed ({type(exc).__name__}). Check the session and API credentials."


async def handle_user_commands(client, event):
    global afk_reason, autocal, reminder_counter

    text = (event.raw_text or "").strip()
    if not text:
        return

    # AFK replies are handled by the incoming-message handler below.
    if not text.startswith("."):
        if autocal and re.fullmatch(r"[\d\s.+*/()%\-]+", text):
            try:
                result = calculate(text)
                await event.reply(f"= {result}")
            except Exception:
                pass
        return

    parts = text.split()
    cmd = parts[0].lower()

    if cmd == ".help":
        await event.reply(
            "📋 Commands\n"
            ".help — command list\n"
            ".ping — test userbot\n"
            ".id — chat/user ID\n"
            ".cal 2+2 — calculator\n"
            ".autocal on/off — auto-solve sums\n"
            ".todo text — add task\n"
            ".todos — list tasks\n"
            ".done 1 — finish task\n"
            ".remind 1h text — set reminder\n"
            ".reminders — list reminders\n"
            ".afk reason — enable AFK\n"
            ".back — disable AFK\n"
            ".purge 10 — delete recent own messages\n"
            ".cleanall — delete own messages in scan limit\n"
            ".pfp — download replied user's photo\n"
            ".block / .unblock — reply to a user\n"
            ".sticker — convert replied image\n"
            ".copy — copy replied message\n"
            ".spam 5 text — repeat (max 20)\n"
            ".quote text — create quote image\n"
            ".translate es — translate replied text"
        )

    elif cmd == ".ping":
        await event.reply("Pong! 🏓")

    elif cmd == ".id":
        reply = await event.get_reply_message()
        sender = await reply.get_sender() if reply else await event.get_sender()
        await event.reply(
            f"Chat ID: {event.chat_id}\n"
            f"User ID: {getattr(sender, 'id', 'N/A')}"
        )

    elif cmd == ".cal":
        expression = text[len(parts[0]):].strip()
        if not expression:
            await event.reply("Usage: .cal 2+2")
            return
        try:
            await event.reply(f"= {calculate(expression)}")
        except Exception:
            await event.reply("Invalid arithmetic expression.")

    elif cmd == ".autocal":
        if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
            await event.reply("Usage: .autocal on/off")
            return
        autocal = parts[1].lower() == "on"
        await event.reply(f"Auto-calculation {'enabled' if autocal else 'disabled'}.")

    elif cmd == ".todo":
        task = text[len(parts[0]):].strip()
        if not task:
            await event.reply("Usage: .todo text")
            return
        cur = db.execute("INSERT INTO todos(task) VALUES (?)", (task,))
        db.commit()
        await event.reply(f"Task #{cur.lastrowid} added.")

    elif cmd == ".todos":
        rows = db.execute(
            "SELECT id, task, done FROM todos ORDER BY id DESC LIMIT 50"
        ).fetchall()
        await event.reply(
            "\n".join(
                f"{'✓' if done else '○'} #{tid}: {task}"
                for tid, task, done in rows
            ) if rows else "No to-dos yet."
        )

    elif cmd == ".done":
        if len(parts) != 2 or not parts[1].isdigit():
            await event.reply("Usage: .done 1")
            return
        cur = db.execute(
            "UPDATE todos SET done=1 WHERE id=?", (int(parts[1]),)
        )
        db.commit()
        await event.reply("Task completed." if cur.rowcount else "Task not found.")

    elif cmd == ".remind":
        match = re.match(r"^\.remind\s+(\d+[smhd]?)\s+(.+)$", text, re.I)
        if not match:
            await event.reply("Usage: .remind 30s/10m/1h/2d message")
            return

        try:
            seconds = parse_duration(match.group(1))
        except ValueError:
            await event.reply("Duration must be from 1 second to 7 days.")
            return

        reminder_counter += 1
        rid = reminder_counter
        message = match.group(2)
        chat_id = event.chat_id
        reminders[rid] = {"text": message, "seconds": seconds}

        async def fire_reminder():
            try:
                await asyncio.sleep(seconds)
                await client.send_message(chat_id, f"⏰ Reminder: {message}")
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                reminders.pop(rid, None)
                reminder_tasks.pop(rid, None)

        reminder_tasks[rid] = asyncio.create_task(fire_reminder())
        await event.reply(f"Reminder #{rid} set.")

    elif cmd == ".reminders":
        if not reminders:
            await event.reply("No active reminders.")
        else:
            await event.reply("\n".join(
                f"#{rid}: {item['text']} ({item['seconds']} seconds)"
                for rid, item in reminders.items()
            ))

    elif cmd == ".afk":
        afk_reason = text[len(parts[0]):].strip() or "Away"
        await event.reply(f"AFK enabled: {afk_reason}")

    elif cmd == ".back":
        afk_reason = None
        await event.reply("Welcome back! AFK disabled.")

    elif cmd == ".purge":
        if len(parts) != 2 or not parts[1].isdigit():
            await event.reply("Usage: .purge 10")
            return
        count = min(int(parts[1]), 100)
        deleted = 0
        async for msg in client.iter_messages(event.chat_id, limit=500):
            if msg.id == event.id:
                continue
            if msg.out and deleted < count:
                try:
                    await msg.delete()
                    deleted += 1
                except Exception:
                    pass
            if deleted >= count:
                break
        try:
            await event.delete()
        except Exception:
            pass
        # The command message is deleted too; no confirmation is sent.

    elif cmd == ".cleanall":
        deleted = 0
        async for msg in client.iter_messages(event.chat_id, limit=1000):
            if msg.out:
                try:
                    await msg.delete()
                    deleted += 1
                except Exception:
                    pass
        # Deletes up to 1000 outgoing messages found in the scan.

    elif cmd == ".pfp":
        reply = await event.get_reply_message()
        if not reply:
            await event.reply("Reply to a user's message with .pfp")
            return
        sender = await reply.get_sender()
        photo = await client.download_profile_photo(sender, file=bytes)
        if photo:
            await client.send_file(event.chat_id, photo, caption="Profile photo")
        else:
            await event.reply("No profile photo found.")

    elif cmd in (".block", ".unblock"):
        reply = await event.get_reply_message()
        if not reply:
            await event.reply(f"Reply to a user's message with {cmd}")
            return
        sender = await reply.get_sender()
        if not sender or getattr(sender, "bot", False):
            await event.reply("Could not identify a suitable user.")
            return
        if cmd == ".block":
            await client(BlockRequest(sender.id))
            await event.reply("User blocked.")
        else:
            await client(UnblockRequest(sender.id))
            await event.reply("User unblocked.")

    elif cmd in (".sticker", ".quote"):
        reply = await event.get_reply_message()
        if cmd == ".sticker":
            if not reply or not reply.photo:
                await event.reply("Reply to a photo with .sticker")
                return
            raw = await reply.download_media(file=bytes)
        else:
            quote = text[len(parts[0]):].strip()
            if not quote and reply:
                quote = reply.message or ""
            if not quote:
                await event.reply("Usage: .quote text, or reply to text.")
                return
            image = Image.new("RGB", (512, 512), "#20242b")
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default(size=24)
            words = quote.split()
            lines, line = [], ""
            for word in words:
                if len(line + " " + word) > 28:
                    lines.append(line)
                    line = word
                else:
                    line = (line + " " + word).strip()
            if line:
                lines.append(line)
            draw.multiline_text((25, 40), "\n".join(lines[:12]), fill="white", font=font, spacing=10)
            buf = io.BytesIO()
            image.save(buf, format="WEBP")
            raw = buf.getvalue()

        try:
            image = Image.open(io.BytesIO(raw)).convert("RGBA")
            image.thumbnail((512, 512))
            output = io.BytesIO()
            image.save(output, format="WEBP")
            output.seek(0)
            output.name = "sticker.webp"
            await client.send_file(event.chat_id, output, reply_to=event.id)
        except Exception:
            await event.reply("Could not create the sticker.")

    elif cmd == ".copy":
        reply = await event.get_reply_message()
        if not reply:
            await event.reply("Reply to a message with .copy")
            return
        if reply.message:
            await client.send_message(event.chat_id, reply.message, reply_to=event.id)
        elif reply.media:
            media = await reply.download_media(file=bytes)
            if media:
                await client.send_file(
                    event.chat_id, media,
                    caption=reply.message or "",
                    reply_to=event.id,
                )

    elif cmd == ".spam":
        if len(parts) < 3 or not parts[1].isdigit():
            await event.reply("Usage: .spam 5 text (maximum 20)")
            return
        count = int(parts[1])
        if not 1 <= count <= 20:
            await event.reply("Choose a count from 1 to 20.")
            return
        message = text.split(maxsplit=2)[2]
        for _ in range(count):
            await client.send_message(event.chat_id, message)
            await asyncio.sleep(1)

    elif cmd == ".translate":
        reply = await event.get_reply_message()
        language = parts[1] if len(parts) > 1 else "en"
        if reply and reply.message:
            source_text = reply.message
        else:
            match = text.split(maxsplit=2)
            source_text = match[2] if len(match) == 3 else ""
        if not source_text:
            await event.reply("Reply to text with .translate es, or use .translate es hello")
            return
        try:
            translated = await asyncio.to_thread(
                lambda: GoogleTranslator(
                    source="auto", target=language
                ).translate(source_text)
            )
            await event.reply(translated or "No translation returned.")
        except Exception:
            await event.reply("Translation failed. Try a supported language code, e.g. en, hi, es.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Userbot manager online.\n/login — connect account\n/status — connection status\n/help — commands"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Management commands:\n/login\n/status\n/cancel\n\n"
        "Use /login in a private chat to connect your Telethon StringSession."
    )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if not user or user.id != OWNER_ID:
        await update.effective_message.reply_text("Not authorized.")
        return
    if not chat or chat.type != "private":
        await update.effective_message.reply_text("Use /login in a private chat.")
        return

    login_waiting.add(user.id)
    await update.effective_message.reply_text(
        "Send your Telethon StringSession in your next message. "
        "Use /cancel to stop. Never send it in a group."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        login_waiting.discard(update.effective_user.id)
    await update.effective_message.reply_text("Login cancelled.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def receive_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await message.reply_text("Send the session as plain text or use /cancel.")
        return

    login_waiting.discard(user.id)

    try:
        await message.delete()
    except Exception:
        pass

    progress = await context.bot.send_message(chat.id, "Validating session…")
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
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        receive_session,
    ))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main():
    threading.Thread(target=run_web, daemon=True).start()

    async def start_clients():
        # Keep Telegram clients and their event handlers on the same event loop.
        await run_bot()

    asyncio.run(start_clients())


if __name__ == "__main__":
    main()
