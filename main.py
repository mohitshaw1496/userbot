
import os
import ast
import operator
import asyncio
import sqlite3
import threading
import io
import re
import textwrap
from datetime import datetime

from flask import Flask, jsonify
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeSticker,
    DocumentAttributeImageSize,
    DocumentAttributeFilename,
    InputStickerSetEmpty,
)
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from deep_translator import GoogleTranslator
from PIL import Image, ImageDraw, ImageFont, ImageOps


# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")
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

DB_PATH = "todos.db"


# =========================================================
# WEB SERVER
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
# TODO DATABASE
# =========================================================

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
            """INSERT INTO todos
               (owner_id, task, done, created_at)
               VALUES (?, ?, 0, ?)""",
            (
                OWNER_ID,
                task,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return cur.lastrowid


def get_todos():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            """SELECT id, task FROM todos
               WHERE owner_id = ? AND done = 0 ORDER BY id""",
            (OWNER_ID,),
        ).fetchall()


def finish_todo(todo_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """UPDATE todos SET done = 1
               WHERE id = ? AND owner_id = ? AND done = 0""",
            (todo_id, OWNER_ID),
        )
        return cur.rowcount > 0


init_db()


# =========================================================
# CALCULATOR AND DURATION PARSER
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

        if isinstance(node, ast.Constant):
            if type(node.value) in (int, float):
                return node.value
            raise ValueError("Invalid number.")

        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in _ALLOWED_OPERATORS:
                raise ValueError("Unsupported operator.")

            left = evaluate(node.left)
            right = evaluate(node.right)

            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponent too large.")

            result = _ALLOWED_OPERATORS[op_type](left, right)

            if isinstance(result, (int, float)) and abs(result) > 1e100:
                raise ValueError("Result too large.")

            return result

        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)
            if op_type not in _ALLOWED_OPERATORS:
                raise ValueError("Unsupported operator.")
            return _ALLOWED_OPERATORS[op_type](evaluate(node.operand))

        raise ValueError("Invalid expression.")

    return evaluate(tree)


def parse_duration(value):
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*"
        r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
        r"h|hr|hrs|hour|hours|d|day|days)\s*",
        value.lower(),
    )

    if not match:
        raise ValueError("Use a duration like 30s, 10m, 2h, or 1d.")

    number = float(match.group(1))
    unit = match.group(2)

    if unit in ("s", "sec", "secs", "second", "seconds"):
        multiplier = 1
    elif unit in ("m", "min", "mins", "minute", "minutes"):
        multiplier = 60
    elif unit in ("h", "hr", "hrs", "hour", "hours"):
        multiplier = 3600
    else:
        multiplier = 86400

    seconds = number * multiplier

    if seconds < 1 or seconds > 7 * 86400:
        raise ValueError("Duration must be between 1 second and 7 days.")

    return seconds


# =========================================================
# IMAGE AND STICKER HELPERS
# =========================================================

def load_font(size):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue

    return ImageFont.load_default()


def make_sticker_image(image_bytes):
    """Convert a supported image into a 512x512 WebP sticker image."""
    with Image.open(io.BytesIO(image_bytes)) as original:
        image = ImageOps.exif_transpose(original).convert("RGBA")

    image.thumbnail((512, 512), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    x = (512 - image.width) // 2
    y = (512 - image.height) // 2
    canvas.alpha_composite(image, (x, y))

    output = io.BytesIO()
    canvas.save(output, format="WEBP", quality=90, method=6)
    output.seek(0)
    return output


def make_quote_sticker(name, quote_text, photo_bytes=None):
    """Create a 512x512 quote sticker with a sender avatar and name."""
    canvas = Image.new("RGBA", (512, 512), (27, 30, 43, 255))
    draw = ImageDraw.Draw(canvas)

    white = (255, 255, 255, 255)
    accent = (110, 190, 255, 255)
    muted = (190, 200, 220, 255)

    # Rounded card background.
    draw.rounded_rectangle(
        (12, 12, 500, 500),
        radius=30,
        fill=(37, 42, 59, 255),
        outline=(90, 110, 145, 255),
        width=3,
    )

    # Sender avatar.
    avatar = None

    if photo_bytes:
        try:
            with Image.open(io.BytesIO(photo_bytes)) as source:
                avatar = ImageOps.fit(
                    source.convert("RGBA"),
                    (100, 100),
                    method=Image.Resampling.LANCZOS,
                )
        except Exception:
            avatar = None

    if avatar is None:
        avatar = Image.new("RGBA", (100, 100), (80, 105, 155, 255))
        avatar_draw = ImageDraw.Draw(avatar)
        initial = (name[:1] or "?").upper()
        avatar_draw.text(
            (50, 50),
            initial,
            font=load_font(44),
            fill=white,
            anchor="mm",
        )

    mask = Image.new("L", (100, 100), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 99, 99), fill=255)
    canvas.paste(avatar, (35, 35), mask)

    name_font = load_font(27)
    quote_font = load_font(25)

    display_name = name or "Unknown user"
    while draw.textbbox((0, 0), display_name, font=name_font)[2] > 335:
        if len(display_name) <= 4:
            break
        display_name = display_name[:-2] + "…"

    draw.text((155, 48), display_name, font=name_font, fill=accent)

    # Wrap text based on actual rendered width.
    words = quote_text.split()
    lines = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=quote_font)[2] <= 420:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    if not lines:
        lines = [" "]

    # Limit sticker text to fit; preserve a readable result.
    max_lines = 10
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:35] + "…"

    line_height = 34
    available_height = 355
    if len(lines) * line_height > available_height:
        quote_font = load_font(21)
        line_height = 29

    y = 165
    draw.text((38, 135), "❝", font=load_font(38), fill=accent)

    for line in lines:
        if y > 460:
            break
        draw.text((45, y), line, font=quote_font, fill=white)
        y += line_height

    draw.text(
        (38, 462),
        "TELEGRAM QUOTE",
        font=load_font(14),
        fill=muted,
    )

    output = io.BytesIO()
    canvas.save(output, format="WEBP", quality=90, method=6)
    output.seek(0)
    return output


async def send_sticker(client, chat_id, sticker_data):
    sticker_data.seek(0)
    sticker_data.name = "sticker.webp"

    attributes = [
        DocumentAttributeSticker(
            alt="🙂",
            stickerset=InputStickerSetEmpty(),
        ),
        DocumentAttributeImageSize(w=512, h=512),
        DocumentAttributeFilename("sticker.webp"),
    ]

    await client.send_file(
        chat_id,
        sticker_data,
        force_document=True,
        mime_type="image/webp",
        attributes=attributes,
    )


async def get_profile_photo_bytes(client, entity):
    try:
        result = await client.download_profile_photo(entity, file=bytes)
        return result if isinstance(result, bytes) else None
    except Exception:
        return None


# =========================================================
# TRANSLATION
# =========================================================

LANGUAGE_ALIASES = {
    "english": "en",
    "hindi": "hi",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "portuguese": "pt",
    "russian": "ru",
    "japanese": "ja",
    "korean": "ko",
    "chinese": "zh-CN",
    "arabic": "ar",
    "bengali": "bn",
    "urdu": "ur",
    "punjabi": "pa",
    "tamil": "ta",
    "telugu": "te",
    "marathi": "mr",
    "gujarati": "gu",
    "dutch": "nl",
    "turkish": "tr",
    "indonesian": "id",
    "thai": "th",
    "vietnamese": "vi",
    "ukrainian": "uk",
    "persian": "fa",
    "hebrew": "iw",
}


async def translate_text(target, source_text):
    target = LANGUAGE_ALIASES.get(target.lower(), target)

    def do_translate():
        translator = GoogleTranslator(source="auto", target=target)
        return translator.translate(source_text)

    return await asyncio.to_thread(do_translate)


# =========================================================
# USERBOT COMMAND HANDLER
# =========================================================

async def handle_user_commands(client, event):
    global afk_reason, autocal, reminder_counter

    text = (event.raw_text or "").strip()
    if not text:
        return

    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if not command.startswith("."):
        if autocal:
            try:
                await event.reply(f"= {calculate(text)}")
            except Exception:
                pass
        return

    # ---------------- HELP ----------------
    if command == ".help":
        await event.reply(
            "**Userbot Commands**\n\n"
            "`.help` — command list\n"
            "`.ping` — connection test\n"
            "`.id` — chat and sender IDs\n"
            "`.info` — user details; reply to a user's message\n"
            "`.cal 2+2` — calculator\n"
            "`.autocal on/off` — automatic calculation\n"
            "`.todo task` — add a task\n"
            "`.todos` — list unfinished tasks\n"
            "`.done 1` — complete a task\n"
            "`.remind 10m take a break` — reminder\n"
            "`.reminders` — list active reminders\n"
            "`.afk reason` / `.back` — AFK mode\n"
            "`.purge 10` — delete up to 10 of your messages\n"
            "`.cleanall` — delete up to 100 of your messages\n"
            "`.pfp` — send this chat's profile photo\n"
            "Reply to an image with `.sticker` — image sticker\n"
            "Reply to text with `.quote` — quote sticker\n"
            "Reply to a user with `.block` / `.unblock`\n"
            "`.copy` — copy replied text\n"
            "`.translate es Hello` — translate text\n"
            "`.spam 3 Hello` — send up to 5 messages"
        )
        return

    if command == ".ping":
        await event.reply("Pong!")
        return

    if command == ".id":
        reply = await event.get_reply_message()
        await event.reply(
            f"Chat ID: `{event.chat_id}`\n"
            f"Sender ID: `{reply.sender_id if reply else event.sender_id}`"
        )
        return

    # ---------------- USER INFO ----------------
    if command == ".info":
        reply = await event.get_reply_message()

        try:
            if reply:
                entity = await reply.get_sender()
            elif event.is_private:
                entity = await event.get_chat()
            else:
                await event.reply(
                    "Reply to the user's message with `.info`."
                )
                return

            if not entity or not getattr(entity, "id", None):
                await event.reply("Couldn't identify that user.")
                return

            user_id = entity.id
            first = getattr(entity, "first_name", None) or ""
            last = getattr(entity, "last_name", None) or ""
            name = f"{first} {last}".strip() or getattr(
                entity, "title", "Unknown"
            )
            username = getattr(entity, "username", None)
            username_text = f"@{username}" if username else "None"
            permanent_link = f"tg://user?id={user_id}"

            await event.reply(
                "**User Information**\n"
                f"Name: {name}\n"
                f"User ID: `{user_id}`\n"
                f"Username: {username_text}\n"
                f"Permanent link: `{permanent_link}`"
            )
        except Exception as exc:
            print("Info command error:", type(exc).__name__)
            await event.reply("Couldn't retrieve user information.")
        return

    # ---------------- CALCULATOR ----------------
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

    # ---------------- TODO ----------------
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
            await event.reply("No unfinished tasks.")
            return
        await event.reply(
            "**Your tasks**\n" +
            "\n".join(f"#{row[0]} — {row[1]}" for row in todos)
        )
        return

    if command == ".done":
        try:
            todo_id = int(argument)
        except ValueError:
            await event.reply("Usage: `.done 1`")
            return
        await event.reply(
            f"Task #{todo_id} completed."
            if finish_todo(todo_id)
            else "Task not found or already completed."
        )
        return

    # ---------------- REMINDERS ----------------
    if command == ".remind":
        args = argument.split(maxsplit=1)
        if len(args) != 2:
            await event.reply("Usage: `.remind 10m take a break`")
            return

        try:
            seconds = parse_duration(args[0])
        except ValueError as exc:
            await event.reply(str(exc))
            return

        reminder_counter += 1
        reminder_id = reminder_counter
        chat_id = event.chat_id
        reminder_text = args[1]

        async def fire_reminder():
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

        task = asyncio.create_task(fire_reminder())
        reminder_tasks[reminder_id] = {
            "task": task,
            "text": reminder_text,
        }
        await event.reply(f"Reminder #{reminder_id} set.")
        return

    if command == ".reminders":
        if not reminder_tasks:
            await event.reply("No active reminders.")
            return
        await event.reply(
            "**Active reminders**\n" +
            "\n".join(
                f"#{rid}: {info['text']}"
                for rid, info in reminder_tasks.items()
            )
        )
        return

    # ---------------- AFK ----------------
    if command == ".afk":
        afk_reason = argument or "Away"
        await event.reply(f"AFK enabled: {afk_reason}")
        return

    if command == ".back":
        afk_reason = None
        await event.reply("Welcome back! AFK disabled.")
        return

    # ---------------- PROFILE PHOTO ----------------
    if command == ".pfp":
        try:
            chat = await event.get_chat()
            photo = await get_profile_photo_bytes(client, chat)

            if not photo:
                await event.reply("This chat has no accessible profile photo.")
                return

            await client.send_file(
                event.chat_id,
                photo,
                caption="Profile picture",
            )
        except Exception as exc:
            print("PFP command error:", type(exc).__name__)
            await event.reply("Could not download the chat profile picture.")
        return

    # ---------------- IMAGE TO STICKER ----------------
    if command == ".sticker":
        reply = await event.get_reply_message()

        if not reply or not reply.media:
            await event.reply(
                "Reply to an image or photo with `.sticker`."
            )
            return

        try:
            raw_image = await client.download_media(reply, file=bytes)

            if not isinstance(raw_image, bytes):
                await event.reply("Couldn't download the image.")
                return

            sticker = await asyncio.to_thread(
                make_sticker_image, raw_image
            )
            await send_sticker(client, event.chat_id, sticker)

        except Exception as exc:
            print("Sticker command error:", type(exc).__name__)
            await event.reply(
                "Couldn't create the sticker. Try replying to a normal "
                "photo or image file."
            )
        return

    # ---------------- QUOTE STICKER ----------------
    if command == ".quote":
        reply = await event.get_reply_message()

        if not reply or not (reply.raw_text or "").strip():
            await event.reply(
                "Reply to a text message with `.quote` to make a quote sticker."
            )
            return

        try:
            sender = await reply.get_sender()
            if not sender:
                await event.reply("Couldn't identify the message sender.")
                return

            first = getattr(sender, "first_name", None) or ""
            last = getattr(sender, "last_name", None) or ""
            name = f"{first} {last}".strip() or getattr(
                sender, "title", "Unknown user"
            )

            photo = await get_profile_photo_bytes(client, sender)
            quote_sticker = await asyncio.to_thread(
                make_quote_sticker,
                name,
                reply.raw_text.strip(),
                photo,
            )
            await send_sticker(client, event.chat_id, quote_sticker)

        except Exception as exc:
            print("Quote command error:", type(exc).__name__)
            await event.reply("Couldn't create the quote sticker.")
        return

    # ---------------- BLOCK / UNBLOCK ----------------
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
            await event.reply("Couldn't complete that action.")
        return

    # ---------------- COPY ----------------
    if command == ".copy":
        reply = await event.get_reply_message()
        if not reply or not reply.raw_text:
            await event.reply("Reply to a text message with `.copy`.")
            return
        await client.send_message(event.chat_id, reply.raw_text)
        return

    # ---------------- TRANSLATE ----------------
    if command == ".translate":
        args = argument.split(maxsplit=1)

        if len(args) != 2:
            await event.reply(
                "Usage: `.translate es Hello there`\n"
                "You can also use `.translate hindi Hello there`."
            )
            return

        language, source_text = args

        try:
            result = await translate_text(language, source_text)
            if not result:
                await event.reply("No translation was returned.")
            else:
                await event.reply(result)
        except Exception as exc:
            # Don't print private message content.
            print("Translation error:", type(exc).__name__)
            await event.reply(
                "Translation failed. Check the language name/code and "
                "try again. Example: `.translate hi Hello`."
            )
        return

    # ---------------- PURGE ----------------
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

        try:
            async for message in client.iter_messages(
                event.chat_id, limit=count + 1
            ):
                if message.out:
                    await message.delete()
        except Exception as exc:
            print("Purge error:", type(exc).__name__)
        return

    # ---------------- LIMITED SPAM ----------------
    if command == ".spam":
        args = argument.split(maxsplit=1)

        if len(args) != 2:
            await event.reply("Usage: `.spam 3 Hello`")
            return

        try:
            count = int(args[0])
        except ValueError:
            await event.reply("Enter a valid count.")
            return

        if not 1 <= count <= 5:
            await event.reply("Count must be between 1 and 5.")
            return

        for _ in range(count):
            await client.send_message(event.chat_id, args[1])
            await asyncio.sleep(1)
        return


# =========================================================
# CONNECT USER ACCOUNT
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
        return False, "Could not connect. Check the Render logs."


# =========================================================
# MANAGEMENT BOT COMMANDS
# =========================================================

def is_owner(update):
    return (
        update.effective_user is not None
        and update.effective_user.id == OWNER_ID
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    await update.effective_message.reply_text(
        "Telegram manager is online.\n\n"
        "/login — connect your Telegram account\n"
        "/status — connection status\n"
        "/help — command list"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    await update.effective_message.reply_text(
        "/login — connect with a Telethon StringSession\n"
        "/cancel — cancel login\n"
        "/status — check connection\n\n"
        "After login, send `.help` from your own Telegram account "
        "to see userbot commands."
    )


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Use /login in a private chat."
        )
        return

    login_waiting.add(OWNER_ID)
    await update.effective_message.reply_text(
        "Send your Telethon StringSession in your next message.\n"
        "Keep it secret: it can grant access to your account.\n"
        "Use /cancel to cancel."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    try:
        await update.effective_message.delete()
    except Exception:
        pass

    status = await context.bot.send_message(
        chat_id=OWNER_ID,
        text="Connecting your Telegram account...",
    )

    success, message = await connect_user(session)
    await status.edit_text(message)


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_owner(update):
        return

    if user_client and user_client.is_connected():
        try:
            me = await user_client.get_me()
            await update.effective_message.reply_text(
                f"Userbot connected as {me.first_name}."
            )
        except Exception:
            await update.effective_message.reply_text(
                "Connected, but account details could not be retrieved."
            )
    else:
        await update.effective_message.reply_text("Userbot disconnected.")


async def error_handler(update, context):
    print("Management bot error:", type(context.error).__name__)


def run_bot():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("status", status_command))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_session)
    )

    application.add_error_handler(error_handler)
    application.run_polling(drop_pending_updates=True)


# =========================================================
# START
# =========================================================

def main():
    threading.Thread(target=run_web, daemon=True).start()
    run_bot()


if __name__ == "__main__":
    main()
