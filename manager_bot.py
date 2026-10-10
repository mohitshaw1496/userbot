
import os
import sqlite3
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "manager.sqlite3"


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                telegram_user_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'not_linked',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def record_user(user_id):
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            INSERT INTO accounts (telegram_user_id, status)
            VALUES (?, 'not_linked')
            ON CONFLICT(telegram_user_id) DO NOTHING
        """, (user_id,))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    record_user(user.id)
    await update.effective_message.reply_text(
        "Welcome to the account manager.\n\n"
        "Commands:\n"
        "/help — show help\n"
        "/login — account-linking instructions\n"
        "/status — check link status\n\n"
        f"Your Telegram user ID: {user.id}"
    )


async def help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    await update.effective_message.reply_text(
        "/start — register with the manager\n"
        "/login — start secure account linking\n"
        "/status — check account-link status\n\n"
        "This bot never asks you to send an OTP, "
        "2FA password, or session file in chat."
    )


async def login(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    record_user(user.id)
    await update.effective_message.reply_text(
        "Secure account linking:\n\n"
        "1. Run your own isolated Telethon client instance.\n"
        "2. Complete Telegram authorization in its private "
        "terminal or supported secure login interface.\n"
        "3. Keep its session file private.\n\n"
        "This manager records your Telegram user ID, but "
        "does not collect login codes or session credentials. "
        "Contact your service administrator to finish linking."
    )


async def status(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    record_user(user.id)
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT status FROM accounts WHERE telegram_user_id=?",
            (user.id,),
        ).fetchone()

    await update.effective_message.reply_text(
        f"Account-link status: {row[0] if row else 'not_linked'}"
    )


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("login", login))
    app.add_handler(CommandHandler("status", status))
    app.run_polling()


if __name__ == "__main__":
    main()
