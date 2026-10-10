import os
import re
import io
import ast
import math
import json
import time
import asyncio
import sqlite3
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from flask import Flask
from PIL import Image, ImageDraw, ImageFont, ImageOps
from deep_translator import GoogleTranslator
from telethon import TelegramClient, events, functions
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeSticker,
    DocumentAttributeImageSize,
    InputStickerSetEmpty,
)
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("userbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TG_API_ID = os.getenv("TG_API_ID", "").strip()
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
OWNER_ID = os.getenv("OWNER_ID", "").strip()
try:
    PORT = int(os.getenv("PORT", "10000"))
except (TypeError, ValueError):
    PORT = 10000
    log.warning("Invalid PORT value; falling back to 10000.")

if not BOT_TOKEN or not TG_API_ID or not TG_API_HASH or not OWNER_ID:
    log.warning("Set BOT_TOKEN, TG_API_ID, TG_API_HASH and OWNER_ID in Render environment variables.")

try:
    API_ID = int(TG_API_ID)
    OWNER_ID_INT = int(OWNER_ID)
except ValueError:
    API_ID = 0
    OWNER_ID_INT = 0

app = Flask(__name__)

@app.get("/")
def home():
    return "Userbot is running", 200

@app.get("/health")
def health():
    return {"status": "ok", "user_connected": bool(user_client and user_client.is_connected())}, 200

user_client = None
login_sessions = {}
afk_state = {}

DB_PATH = os.getenv("DB_PATH", "userbot.db")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS todos (id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, task TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, text TEXT NOT NULL, due REAL NOT NULL, sent INTEGER NOT NULL DEFAULT 0)")
init_db()

LANGUAGE_ALIASES = {
    "en": "english", "hi": "hindi", "bn": "bengali", "ta": "tamil", "te": "telugu",
    "mr": "marathi", "gu": "gujarati", "pa": "punjabi", "ur": "urdu", "es": "spanish",
    "fr": "french", "de": "german", "it": "italian", "pt": "portuguese", "ru": "russian",
    "ja": "japanese", "ko": "korean", "zh": "chinese", "ar": "arabic", "id": "indonesian",
    "tr": "turkish", "vi": "vietnamese", "th": "thai", "ne": "nepali", "ml": "malayalam",
    "kn": "kannada", "or": "odia", "as": "assamese"
}

# MyMemory expects ISO language codes, while GoogleTranslator accepts language names.
LANGUAGE_CODES = {
    "english": "en", "hindi": "hi", "bengali": "bn", "tamil": "ta", "telugu": "te",
    "marathi": "mr", "gujarati": "gu", "punjabi": "pa", "urdu": "ur", "spanish": "es",
    "french": "fr", "german": "de", "italian": "it", "portuguese": "pt", "russian": "ru",
    "japanese": "ja", "korean": "ko", "chinese": "zh", "arabic": "ar", "indonesian": "id",
    "turkish": "tr", "vietnamese": "vi", "thai": "th", "nepali": "ne", "malayalam": "ml",
    "kannada": "kn", "odia": "or", "assamese": "as"
}

async def translate_text(target, source_text):
    """Translate text with multiple fallbacks and useful provider diagnostics."""
    requested = (target or "").lower().strip()
    target = LANGUAGE_ALIASES.get(requested, requested)
    target_code = LANGUAGE_CODES.get(target, requested)
    source_text = (source_text or "").strip()
    if not source_text:
        raise ValueError("No text was provided to translate.")

    errors = []

    # Provider 1: deep-translator's Google backend, trying name and ISO code.
    for google_target in dict.fromkeys((target, target_code)):
        try:
            result = await asyncio.to_thread(
                lambda t=google_target: GoogleTranslator(source="auto", target=t).translate(source_text)
            )
            if result and result.strip():
                return result.strip()
            errors.append("GoogleTranslator returned an empty result")
        except Exception as exc:
            errors.append(f"GoogleTranslator/{google_target}: {type(exc).__name__}")

    # Provider 2: Google's lightweight endpoint with automatic source detection.
    try:
        params = urllib.parse.urlencode({
            "client": "gtx", "sl": "auto", "tl": target_code, "dt": "t", "q": source_text
        })
        request = urllib.request.Request(
            "https://translate.googleapis.com/translate_a/single?" + params,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(request, timeout=18) as response:
            payload = json.loads(response.read().decode("utf-8"))
        translated = "".join(part[0] for part in (payload[0] or []) if part and part[0])
        if translated.strip():
            return translated.strip()
        errors.append("Google endpoint returned an empty result")
    except Exception as exc:
        errors.append(f"Google endpoint: {type(exc).__name__}")

    # Provider 3: MyMemory fallback. The service may reject autodetect, so try it
    # first and then English as a last resort for English source text.
    for source_code in ("autodetect", "en"):
        try:
            params = urllib.parse.urlencode({
                "q": source_text, "langpair": f"{source_code}|{target_code}"
            })
            request = urllib.request.Request(
                "https://api.mymemory.translated.net/get?" + params,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
            result = data.get("responseData", {}).get("translatedText")
            status = data.get("responseStatus")
            if result and str(status or 200) == "200" and result.strip():
                return result.strip()
            errors.append(f"MyMemory ({source_code}): API status {status}; no usable translation")
        except Exception as exc:
            errors.append(f"MyMemory/{source_code}: {type(exc).__name__}")

    log.error("All translation providers failed: %s", " | ".join(errors))
    raise RuntimeError("All translation providers failed; check Render logs for provider details.")

# Calculator accepts only arithmetic syntax; no names, calls, or attributes.
def safe_calculate(expression):
    tree = ast.parse(expression, mode="eval")
    allowed = (ast.Expression, ast.Constant, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult,
               ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Load, ast.LShift, ast.RShift)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError("Only basic arithmetic is allowed")
        if isinstance(node, ast.Constant) and (not isinstance(node.value, (int, float)) or isinstance(node.value, bool)):
            raise ValueError("Invalid number")
        if isinstance(node, ast.Pow) and isinstance(getattr(node, "parent", None), ast.AST):
            pass
    value = eval(compile(tree, "<calculator>", "eval"), {"__builtins__": {}}, {})
    if isinstance(value, complex) or not math.isfinite(float(value)):
        raise ValueError("Invalid result")
    return value

def parse_duration(text):
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", text.lower())
    if not match:
        raise ValueError("Use a duration such as 30s, 10m, 2h or 1d")
    n = int(match.group(1)); unit = match.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]

def load_font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def save_sticker_webp(image):
    """Encode a static Telegram sticker as WebP, keeping it under Telegram's 512 KB limit."""
    for quality in (85, 75, 65, 55, 45):
        out = io.BytesIO()
        image.save(out, "WEBP", lossless=False, quality=quality, method=6)
        if out.tell() <= 512 * 1024:
            out.seek(0)
            return out
    # Final fallback: reduce dimensions slightly while preserving the sticker's square canvas.
    smaller = image.resize((384, 384), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    smaller.save(out, "WEBP", lossless=False, quality=40, method=6)
    out.seek(0)
    return out

def make_sticker_image(image_bytes):
    im = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    im.thumbnail((512, 512), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    canvas.alpha_composite(im, ((512 - im.width) // 2, (512 - im.height) // 2))
    return save_sticker_webp(canvas)

def make_quote_sticker(text, name, avatar_bytes=None):
    size = (512, 512)
    canvas = Image.new("RGBA", size, (25, 28, 38, 255))
    draw = ImageDraw.Draw(canvas)
    # subtle rounded card
    draw.rounded_rectangle((18, 18, 494, 494), radius=32, fill=(36, 41, 55, 255), outline=(90, 102, 130, 255), width=3)
    if avatar_bytes:
        try:
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
            avatar = ImageOps.fit(avatar, (76, 76), method=Image.Resampling.LANCZOS)
            mask = Image.new("L", (76, 76), 0); ImageDraw.Draw(mask).ellipse((0, 0, 75, 75), fill=255)
            canvas.paste(avatar, (42, 42), mask)
        except Exception:
            pass
    draw.text((134, 58), name[:28], font=load_font(24), fill=(255, 255, 255, 255))
    draw.line((42, 140, 470, 140), fill=(100, 113, 143, 255), width=2)
    font = load_font(27)
    words = text.split()
    lines, line = [], ""
    for word in words:
        # Break unusually long tokens (such as URLs) to prevent overflow.
        chunks, chunk = [], ""
        for char in word:
            if draw.textbbox((0, 0), chunk + char, font=font)[2] > 410 and chunk:
                chunks.append(chunk)
                chunk = char
            else:
                chunk += char
        if chunk:
            chunks.append(chunk)
        for piece in chunks:
            test = f"{line} {piece}".strip()
            if draw.textbbox((0, 0), test, font=font)[2] <= 420:
                line = test
            else:
                if line:
                    lines.append(line)
                line = piece
    if line:
        lines.append(line)
    if len(lines) > 8:
        lines = lines[:8]
        lines[-1] = lines[-1][:35] + "…"
    y = 166
    for line in lines:
        draw.text((46, y), line, font=font, fill=(240, 242, 248, 255)); y += 38
    return save_sticker_webp(canvas)

async def send_sticker(client, chat_id, webp_buffer, alt="🙂", reply_to=None):
    # Telegram stickers are documents with sticker attributes; these attributes make Telegram render it as a sticker.
    webp_buffer.seek(0)
    await client.send_file(
        chat_id,
        webp_buffer,
        file_name="sticker.webp",
        mime_type="image/webp",
        force_document=True,
        attributes=[
            DocumentAttributeImageSize(w=512, h=512),
            DocumentAttributeSticker(alt=alt[:64], stickerset=InputStickerSetEmpty()),
        ],
        reply_to=reply_to,
    )

async def get_reply_image_bytes(event):
    if not event.is_reply:
        return None
    msg = await event.get_reply_message()
    if not msg or not msg.media:
        return None
    try:
        return await event.client.download_media(msg, file=bytes)
    except Exception:
        return None

async def connect_user(session_string):
    global user_client
    if not session_string or not session_string.strip():
        raise ValueError("Empty session string")
    client = TelegramClient(StringSession(session_string.strip()), API_ID, TG_API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ValueError("Session is not authorized. Generate a valid Telethon StringSession.")

    @client.on(events.NewMessage(outgoing=True))
    async def command_handler(event):
        raw = (event.raw_text or "").strip()
        if not raw.startswith("."):
            return
        parts = raw[1:].split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        try:
            if command == "help":
                help_text = (
                    "✨ **USERBOT COMMANDS**\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "📌 **General**\n"
                    "`.ping` · `.id` · `.info` · `.help`\n"
                    "🧮 **Tools**\n"
                    "`.cal 2+2` · `.autocal`\n"
                    "`.translate hindi` — reply to a text message\n"
                    "📋 **Tasks & reminders**\n"
                    "`.todo task` · `.todos` · `.done ID`\n"
                    "`.remind 10m text` · `.reminders`\n"
                    "👤 **Profile & status**\n"
                    "`.afk reason` · `.back` · `.pfp`\n"
                    "🖼️ **Media**\n"
                    "`.sticker` — reply to an image\n"
                    "`.quote` — reply to a text message\n"
                    "`.copy` — reply to a message\n"
                    "🛡️ **Moderation**\n"
                    "`.block` · `.unblock @user` · `.purge 10`\n"
                    "`.cleanall 30` · `.spam 3 hello`\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "Commands work from your connected Telegram account."
                )
                await event.reply(help_text)
            elif command == "ping":
                start = time.monotonic(); msg = await event.reply("Pong!")
                await msg.edit(f"Pong! `{(time.monotonic()-start)*1000:.0f} ms`")
            elif command == "id":
                chat = await event.get_chat(); reply = await event.get_reply_message()
                target = await reply.get_sender() if reply else chat
                await event.reply(f"Chat ID: `{event.chat_id}`\nUser ID: `{getattr(target, 'id', 'unknown')}`")
            elif command == "info":
                reply = await event.get_reply_message()
                entity = await reply.get_sender() if reply else await event.get_chat()
                first = getattr(entity, "first_name", "") or ""
                last = getattr(entity, "last_name", "") or ""
                username = getattr(entity, "username", None)
                await event.reply(f"**Name:** {first} {last}\n**ID:** `{entity.id}`\n**Username:** @{username if username else 'none'}\n**Profile:** tg://user?id={entity.id}")
            elif command == "cal":
                if not arg: await event.reply("Usage: `.cal 12*(3+4)`"); return
                await event.reply(f"`{arg} = {safe_calculate(arg)}`")
            elif command == "autocal":
                reply = await event.get_reply_message()
                expression = arg or (reply.raw_text if reply else "")
                if not expression: await event.reply("Usage: `.autocal 2+2` or reply to a calculation"); return
                try: result = safe_calculate(expression)
                except Exception: return
                await event.reply(f"`{expression} = {result}`")
            elif command == "todo":
                if not arg: await event.reply("Usage: `.todo buy milk`"); return
                with db() as conn:
                    cur = conn.execute("INSERT INTO todos(owner,task,created) VALUES(?,?,?)", (OWNER_ID_INT, arg, time.time()))
                    item_id = cur.lastrowid
                await event.reply(f"Added todo #{item_id}: {arg}")
            elif command == "todos":
                with db() as conn: rows = conn.execute("SELECT id,task,done FROM todos WHERE owner=? ORDER BY id DESC LIMIT 30", (OWNER_ID_INT,)).fetchall()
                await event.reply("**Todos**\n" + ("\n".join(f"{'✅' if r['done'] else '⬜'} #{r['id']} — {r['task']}" for r in rows) if rows else "No todos yet."))
            elif command == "done":
                try: item_id = int(arg.strip().lstrip("#"))
                except ValueError: await event.reply("Usage: `.done 3`"); return
                with db() as conn: cur = conn.execute("UPDATE todos SET done=1 WHERE id=? AND owner=?", (item_id, OWNER_ID_INT))
                await event.reply("Marked done." if cur.rowcount else "Todo not found.")
            elif command == "remind":
                match = re.match(r"^(\S+)\s+(.+)$", arg)
                if not match: await event.reply("Usage: `.remind 10m take a break`"); return
                seconds = parse_duration(match.group(1)); text = match.group(2)
                with db() as conn: cur = conn.execute("INSERT INTO reminders(owner,text,due) VALUES(?,?,?)", (OWNER_ID_INT, text, time.time()+seconds))
                await event.reply(f"Reminder #{cur.lastrowid} set for {match.group(1)} from now.")
            elif command == "reminders":
                with db() as conn: rows = conn.execute("SELECT id,text,due FROM reminders WHERE owner=? AND sent=0 ORDER BY due LIMIT 30", (OWNER_ID_INT,)).fetchall()
                await event.reply("**Reminders**\n" + ("\n".join(f"#{r['id']} — {r['text']} ({datetime.fromtimestamp(r['due']).strftime('%d %b %H:%M')})" for r in rows) if rows else "No pending reminders."))
            elif command == "afk":
                afk_state[OWNER_ID_INT] = arg or "AFK"
                await event.reply("AFK enabled. Use `.back` to disable.")
            elif command == "back":
                afk_state.pop(OWNER_ID_INT, None); await event.reply("Welcome back! AFK disabled.")
            elif command == "pfp":
                reply = await event.get_reply_message()
                entity = await reply.get_sender() if reply else await event.get_chat()
                photo = await event.client.download_profile_photo(entity, file=bytes)
                if not photo: await event.reply("No profile photo found."); return
                # Normalize to JPEG bytes and send as a Telegram photo, not a document.
                with Image.open(io.BytesIO(photo)) as source:
                    jpeg_image = source.convert("RGB")
                    jpeg_buffer = io.BytesIO()
                    jpeg_image.save(jpeg_buffer, format="JPEG", quality=92, optimize=True)
                    jpeg_buffer.seek(0)
                await event.client.send_file(
                    event.chat_id, jpeg_buffer, file_name="profile.jpg",
                    mime_type="image/jpeg", force_document=False, reply_to=event.id
                )
            elif command == "sticker":
                image_bytes = await get_reply_image_bytes(event)
                if not image_bytes: await event.reply("Reply to a photo/image with `.sticker`."); return
                sticker = make_sticker_image(image_bytes)
                await send_sticker(event.client, event.chat_id, sticker, alt="🙂", reply_to=event.id)
            elif command == "quote":
                reply = await event.get_reply_message()
                if not reply or not (reply.raw_text or "").strip(): await event.reply("Reply to a text message with `.quote`."); return
                sender = await reply.get_sender()
                name = " ".join(filter(None, [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")])) or "Telegram user"
                try:
                    avatar = await event.client.download_profile_photo(sender, file=bytes)
                except Exception:
                    log.exception("Could not download avatar for .quote; generating quote without avatar")
                    avatar = None
                sticker = make_quote_sticker(reply.raw_text.strip(), name, avatar)
                # Reply to the original message being quoted, not to the .quote command itself.
                await send_sticker(event.client, event.chat_id, sticker, alt="💬", reply_to=reply.id)
            elif command == "block":
                reply = await event.get_reply_message()
                target = arg or (str((await reply.get_sender()).id) if reply else "")
                if not target: await event.reply("Reply to a user or provide their ID/username."); return
                entity = await event.client.get_entity(target)
                await event.client(functions.contacts.BlockRequest(entity)); await event.reply("User blocked.")
            elif command == "unblock":
                if not arg: await event.reply("Usage: `.unblock @username` or user ID"); return
                entity = await event.client.get_entity(arg)
                await event.client(functions.contacts.UnblockRequest(entity)); await event.reply("User unblocked.")
            elif command == "copy":
                reply = await event.get_reply_message()
                if not reply: await event.reply("Reply to a message with `.copy`."); return
                if reply.raw_text: await event.reply(reply.raw_text)
                elif reply.media:
                    media = await event.client.download_media(reply, file=bytes)
                    if media: await event.client.send_file(event.chat_id, media, caption=reply.message or "", reply_to=event.id)
                    else: await event.reply("Could not copy that media.")
                else: await event.reply("That message has no copyable text or media.")
            elif command == "translate":
                # Usage: reply to a text message and send `.translate hindi`.
                reply = await event.get_reply_message()
                if not reply or not (reply.raw_text or "").strip():
                    await event.reply("Reply to a text message with `.translate hindi` (or another language name/code).")
                    return
                if not arg.strip():
                    await event.reply("Specify a target language, e.g. `.translate hindi`.")
                    return
                language = arg.strip().lower()
                try:
                    result = await translate_text(language, reply.raw_text.strip())
                    await event.reply(f"**Translation ({language})**\n{result}", reply_to=reply.id)
                except Exception as exc:
                    log.exception("Translation command failed")
                    await event.reply(f"Translation failed ({type(exc).__name__}). Try a language name/code like `hindi`, `hi`, `english`, or `fr`, or check Render logs.")
            elif command == "purge":
                try: count = max(1, min(100, int(arg or "10")))
                except ValueError: count = 10
                messages = await event.client.get_messages(event.chat_id, limit=count+1, min_id=event.id)
                ids = [m.id for m in messages if m.id != event.id]
                if ids: await event.client.delete_messages(event.chat_id, ids)
                await event.delete()
            elif command == "cleanall":
                # Delete recent outgoing messages only; this is intentionally bounded.
                try: count = max(1, min(100, int(arg or "30")))
                except ValueError: count = 30
                messages = await event.client.get_messages(event.chat_id, limit=count)
                ids = [m.id for m in messages if m.out]
                if ids: await event.client.delete_messages(event.chat_id, ids)
            elif command == "spam":
                match = re.match(r"^(\d+)\s+(.+)$", arg, flags=re.S)
                if not match: await event.reply("Usage: `.spam 3 hello` (max 10)"); return
                count = max(1, min(10, int(match.group(1))))
                text = match.group(2)
                await event.delete()
                for _ in range(count): await event.client.send_message(event.chat_id, text)
        except Exception as exc:
            log.exception("Command .%s failed", command)
            try: await event.reply(f"Command failed: {type(exc).__name__}")
            except Exception: pass

    @client.on(events.NewMessage(incoming=True))
    async def afk_handler(event):
        if not afk_state or not event.is_private and not event.mentioned:
            return
        sender = await event.get_sender()
        if getattr(sender, "bot", False) or getattr(sender, "id", None) == OWNER_ID_INT:
            return
        reason = afk_state.get(OWNER_ID_INT)
        if reason:
            try: await event.reply(f"I'm currently AFK: {reason}")
            except Exception: pass

    if user_client and user_client.is_connected():
        await user_client.disconnect()
    user_client = client
    return client


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != OWNER_ID_INT:
        await update.message.reply_text("This management bot is private.")
        return
    await update.message.reply_text(
        "Userbot manager ready.\n\n"
        "/login - connect your Telethon StringSession\n"
        "/status - show connection status\n"
        "/cancel - cancel login\n"
        "/help - show commands"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != OWNER_ID_INT:
        return
    await update.message.reply_text(
        "Management commands:\n/login, /status, /cancel, /help\n\n"
        "Send your Telethon StringSession after starting /login. Never share it with anyone."
    )

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID_INT:
        await update.message.reply_text("Not authorized.")
        return
    login_sessions[update.effective_user.id] = True
    context.user_data["awaiting_session"] = True
    await update.message.reply_text(
        "Send your Telethon StringSession in your next message.\n"
        "Treat it like a password: anyone with it can access your Telegram account.\n"
        "Use /cancel to stop."
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        login_sessions.pop(update.effective_user.id, None)
    context.user_data.pop("awaiting_session", None)
    await update.message.reply_text("Cancelled.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID_INT:
        await update.message.reply_text("Not authorized.")
        return
    connected = bool(user_client and user_client.is_connected() and await user_client.is_user_authorized())
    await update.message.reply_text("User account: connected ✅" if connected else "User account: disconnected ❌")

async def session_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID_INT:
        return
    if not context.user_data.get("awaiting_session"):
        return
    session_string = (update.message.text or "").strip()
    if not session_string:
        await update.message.reply_text("Please send a valid StringSession, or /cancel.")
        return
    # Remove the message containing the session from the management chat when possible.
    try:
        await update.message.delete()
    except Exception:
        pass
    context.user_data.pop("awaiting_session", None)
    login_sessions.pop(update.effective_user.id, None)
    status_msg = await update.message.reply_text("Connecting user account...")
    try:
        await connect_user(session_string)
        # Do not store the session in logs or send it back to chat.
        await status_msg.edit_text("Connected successfully. Your userbot is ready.")
    except Exception as exc:
        log.exception("User session connection failed")
        await status_msg.edit_text(f"Login failed: {type(exc).__name__}. Check API ID/hash and StringSession, then try /login again.")

async def reminder_worker(application: Application):
    while True:
        try:
            now = time.time()
            with db() as conn:
                rows = conn.execute("SELECT id,owner,text FROM reminders WHERE sent=0 AND due<=?", (now,)).fetchall()
                if rows:
                    conn.executemany("UPDATE reminders SET sent=1 WHERE id=?", [(r["id"],) for r in rows])
            for row in rows:
                try:
                    await application.bot.send_message(chat_id=row["owner"], text=f"⏰ Reminder: {row['text']}")
                except Exception:
                    log.exception("Could not send reminder %s", row["id"])
        except Exception:
            log.exception("Reminder worker error")
        await asyncio.sleep(5)

async def post_init(application: Application):
    application.create_task(reminder_worker(application))

async def run_bot_once():
    """Start the Telegram management bot; raise on startup/runtime failure."""
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, session_message))

    initialized = False
    polling_started = False
    application_started = False
    try:
        await application.initialize()
        initialized = True
        # Custom lifecycle (rather than run_polling) requires calling post_init explicitly.
        await post_init(application)
        if application.updater is None:
            raise RuntimeError("Telegram bot updater is unavailable.")
        await application.updater.start_polling(drop_pending_updates=True)
        polling_started = True
        await application.start()
        application_started = True
        log.info("Telegram management bot started successfully.")
        # Keep this attempt alive; cancellation or an exception is handled by the retry loop.
        await asyncio.Event().wait()
    finally:
        if polling_started and application.updater is not None:
            try:
                await application.updater.stop()
            except Exception:
                log.exception("Error stopping Telegram polling")
        if application_started:
            try:
                await application.stop()
            except Exception:
                log.exception("Error stopping Telegram application")
        if initialized:
            try:
                await application.shutdown()
            except Exception:
                log.exception("Error shutting down Telegram application")


async def run_bot():
    """Keep the web service alive and retry Telegram startup if it fails."""
    if not BOT_TOKEN or not TG_API_ID or not TG_API_HASH or not OWNER_ID:
        log.error(
            "Missing required Render environment variables. Set BOT_TOKEN, TG_API_ID, "
            "TG_API_HASH, and OWNER_ID. The health web server will remain available."
        )
    if not API_ID or not OWNER_ID_INT:
        log.error("TG_API_ID and OWNER_ID must be valid numeric values.")

    while True:
        if not BOT_TOKEN or not API_ID or not TG_API_HASH or not OWNER_ID_INT:
            await asyncio.sleep(60)
            continue
        try:
            await run_bot_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(
                "Telegram management bot stopped (%s). Retrying in 30 seconds.",
                type(exc).__name__,
            )
            await asyncio.sleep(30)


def run_flask():
    # Render requires a process listening on 0.0.0.0 and its assigned PORT.
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    import threading

    flask_thread = threading.Thread(target=run_flask, name="flask-health-server", daemon=True)
    flask_thread.start()
    log.info("Flask health server starting on 0.0.0.0:%s", PORT)
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("Shutting down")
