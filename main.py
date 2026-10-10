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
PORT = int(os.getenv("PORT", "10000"))

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
    target = LANGUAGE_ALIASES.get(target.lower().strip(), target.lower().strip())
    if not source_text.strip():
        raise ValueError("No text was provided to translate.")
