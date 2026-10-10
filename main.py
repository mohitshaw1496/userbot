
import asyncio
import os
import sqlite3
import threading
from datetime import datetime

import ast
import io
import math
import operator
import re
import asyncio

from PIL import Image, ImageDraw, ImageFont
from deep_translator import GoogleTranslator
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest
from telethon.tl.types import DocumentAttributeSticker, InputStickerSetEmpty


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

    
        @candidate.on(events.NewMessage(incoming=True))
        async def afk_replies(event):
            if not afk_reason or event.is_private is False:
                return
            if event.sender_id == OWNER_ID:
                return
            try:
                await event.reply(f"I'm currently AFK: {afk_reason}")
            except Exception:
                pass


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

        
        afk_reason = None
        autocal = False
        reminders = {}
        reminder_counter = 0

        def calculate(expression):
            tree = ast.parse(expression, mode="eval")

            def evaluate(node):
                if isinstance(node, ast.Expression):
                    return evaluate(node.body)
                if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                    return node.value
                if isinstance(node, ast.BinOp):
                    ops = {
                        ast.Add: operator.add,
                        ast.Sub: operator.sub,
                        ast.Mult: operator.mul,
                        ast.Div: operator.truediv,
                        ast.FloorDiv: operator.floordiv,
                        ast.Mod: operator.mod,
                        ast.Pow: operator.pow,
                    }
                    fn = ops.get(type(node.op))
                    if fn is None:
                        raise ValueError("Unsupported operator")
                    left, right = evaluate(node.left), evaluate(node.right)
                    if isinstance(node.op, ast.Pow) and abs(right) > 100:
                        raise ValueError("Exponent too large")
                    result = fn(left, right)
                    if abs(result) > 1e100:
                        raise ValueError("Result too large")
                    return result
                if isinstance(node, ast.UnaryOp):
                    if isinstance(node.op, ast.UAdd):
                        return evaluate(node.operand)
                    if isinstance(node.op, ast.USub):
                        return -evaluate(node.operand)
                raise ValueError("Invalid expression")

            return evaluate(tree)

        async def send_sticker(event, image_bytes, name="sticker.webp"):
            image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
            image.thumbnail((512, 512))
            output = io.BytesIO()
            image.save(output, format="WEBP", quality=90)
            output.seek(0)
            output.name = name
            await candidate.send_file(
                event.chat_id,
                output,
                force_document=True,
                attributes=[
                    DocumentAttributeSticker(
                        alt="🙂",
                        stickerset=InputStickerSetEmpty(),
                    )
                ],
                reply_to=event.id,
            )

        @candidate.on(events.NewMessage(outgoing=True))
        async def commands(event):
            nonlocal afk_reason, autocal, reminder_counter

            text = (event.raw_text or "").strip()
            if not text:
                return

            parts = text.split()
            cmd = parts[0].lower()

            # Auto-calculation of arithmetic typed as a normal message.
            if not text.startswith("."):
                if autocal and re.fullmatch(r"[\d\s.+*/()%\-]+", text):
                    try:
                        result = calculate(text)
                        await event.reply(f"= {result}")
                    except Exception:
                        pass
                return

            # .help
            if cmd == ".help":
                await event.reply(
                    "📋 Commands\n"
                    ".help — this list\n"
                    ".ping — check userbot\n"
                    ".id — chat/user ID\n"
                    ".cal 2+2 — calculator\n"
                    ".autocal on/off — auto-solve sums\n"
                    ".todo text — add task\n"
                    ".todos — list tasks\n"
                    ".done 1 — finish task\n"
                    ".remind 1h text — set reminder\n"
                    ".reminders — list reminders\n"
                    ".afk reason — away reply\n"
                    ".back — turn off AFK\n"
                    ".purge 10 — delete own recent messages\n"
                    ".cleanall — delete own recent messages (up to 1000)\n"
                    ".pfp — download replied user's profile photo\n"
                    ".block / .unblock — reply to a user's message\n"
                    ".sticker — convert replied image to sticker\n"
                    ".copy — copy replied message without forward tag\n"
                    ".spam 5 text — repeat up to 20 times\n"
                    ".quote text — create text sticker\n"
                    ".translate Spanish — translate replied text"
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
                    await event.reply("Invalid or unsupported arithmetic expression.")

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
                await event.reply(f"Added task #{cur.lastrowid}.")

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
                await event.reply(
                    "Task completed." if cur.rowcount else "Task not found."
                )

            elif cmd == ".remind":
                match = re.match(
                    r"^\.remind\s+(\d+)(s|m|h|d)?\s+(.+)$",
                    text,
                    re.IGNORECASE,
                )
                if not match:
                    await event.reply("Usage: .remind 30s/10m/1h/2d message")
                    return

                amount = int(match.group(1))
                unit = (match.group(2) or "m").lower()
                message = match.group(3)
                seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]

                if seconds < 1 or seconds > 7 * 86400:
                    await event.reply("Reminder duration must be within 7 days.")
                    return

                reminder_counter += 1
                rid = reminder_counter
                reminders[rid] = {"text": message, "seconds": seconds}

                async def fire_reminder():
                    await asyncio.sleep(seconds)
                    try:
                        await candidate.send_message(
                            event.chat_id, f"⏰ Reminder: {message}"
                        )
                    finally:
                        reminders.pop(rid, None)

                asyncio.create_task(fire_reminder())
                await event.reply(f"Reminder #{rid} set.")

            elif cmd == ".reminders":
                await event.reply(
                    "\n".join(
                        f"#{rid}: {item['text']} ({item['seconds']} seconds)"
                        for rid, item in reminders.items()
                    ) if reminders else "No active reminders."
                )

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
                limit = min(int(parts[1]), 100)
                deleted = 0
                async for msg in candidate.iter_messages(event.chat_id, limit=500):
                    if msg.id == event.id:
                        continue
                    if msg.out and deleted < limit:
                        await msg.delete()
                        deleted += 1
                    if deleted >= limit:
                        break
                await event.reply(f"Deleted {deleted} of your messages.")

            elif cmd == ".cleanall":
                deleted = 0
                async for msg in candidate.iter_messages(event.chat_id, limit=1000):
                    if msg.out:
                        try:
                            await msg.delete()
                            deleted += 1
                        except Exception:
                            pass
                await event.reply(f"Deleted {deleted} of your messages (up to 1000 scanned).")

            elif cmd == ".pfp":
                reply = await event.get_reply_message()
                if not reply:
                    await event.reply("Reply to a user's message with .pfp")
                    return
                sender = await reply.get_sender()
                photo = await candidate.download_profile_photo(sender, file=bytes)
                if photo:
                    await candidate.send_file(event.chat_id, photo, caption="Profile photo")
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
                    await candidate(BlockRequest(sender.id))
                    await event.reply("User blocked.")
                else:
                    await candidate(UnblockRequest(sender.id))
                    await event.reply("User unblocked.")

            elif cmd == ".sticker":
                reply = await event.get_reply_message()
                if not reply or not reply.media:
                    await event.reply("Reply to an image with .sticker")
                    return
                try:
                    image_bytes = await reply.download_media(file=bytes)
                    if not image_bytes:
                        raise ValueError("No image")
                    await send_sticker(event, image_bytes)
                except Exception:
                    await event.reply("Could not convert that media. Try replying to a regular image.")

            elif cmd == ".copy":
                reply = await event.get_reply_message()
                if not reply:
                    await event.reply("Reply to a message with .copy")
                    return
                if reply.message:
                    await candidate.send_message(
                        event.chat_id, reply.message, reply_to=event.id
                    )
                elif reply.media:
                    media = await reply.download_media(file=bytes)
                    if media:
                        await candidate.send_file(
                            event.chat_id, media, caption=reply.message or "",
                            reply_to=event.id
                        )
                else:
                    await event.reply("That message cannot be copied.")

            elif cmd == ".spam":
                if len(parts) < 3 or not parts[1].isdigit():
                    await event.reply("Usage: .spam 5 text (maximum 20)")
                    return
                count = int(parts[1])
                message = text.split(maxsplit=2)[2]
                if not 1 <= count <= 20:
                    await event.reply("Choose a count from 1 to 20.")
                    return
                for _ in range(count):
                    await candidate.send_message(event.chat_id, message)
                    await asyncio.sleep(1)

            elif cmd == ".quote":
                quote_text = text[len(parts[0]):].strip()
                if not quote_text:
                    reply = await event.get_reply_message()
                    quote_text = reply.message if reply else ""
                if not quote_text:
                    await event.reply("Usage: .quote text, or reply to text with .quote")
                    return
                try:
                    image = Image.new("RGB", (512, 512), "#20242b")
                    draw = ImageDraw.Draw(image)
                    font = ImageFont.load_default(size=24)
                    lines = []
                    words = quote_text.split()
                    line = ""
                    for word in words:
                        if len(line + " " + word) > 28:
                            lines.append(line)
                            line = word
                        else:
                            line = (line + " " + word).strip()
                    if line:
                        lines.append(line)
                    draw.multiline_text((30, 50), "\n".join(lines[:12]), fill="white", font=font, spacing=10)
                    output = io.BytesIO()
                    image.save(output, format="WEBP")
                    await send_sticker(event, output.getvalue(), "quote.webp")
                except Exception:
                    await event.reply("Could not create the quote sticker.")

            elif cmd == ".translate":
                language = parts[1] if len(parts) > 1 else "en"
                reply = await event.get_reply_message()
                source_text = reply.message if reply else text[len(parts[0]):].strip()
                if len(parts) > 1 and not reply:
                    source_text = text.split(maxsplit=1)[1]
                    language = parts[1]
                    source_text = source_text[len(language):].strip()
                if not source_text:
                    await event.reply("Reply to text with .translate es, or use .translate es hello")
                    return
                try:
                    translated = await asyncio.to_thread(
                        lambda: GoogleTranslator(source="auto", target=language).translate(source_text)
                    )
                    await event.reply(translated or "No translation returned.")
                except Exception:
                    await event.reply("Translation failed. Use a supported language code such as en, es, fr, or hi.")

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
