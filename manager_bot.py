
# manager_bot.py
import os
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Telegram Manager Bot\n\n"
        "/login - Account setup instructions\n"
        "/status - Check configuration\n"
        "/help - Show commands"
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "Management commands:\n"
        "/login - Set up your account\n"
        "/status - Check configuration\n\n"
        "User-client commands (send from your Telegram account):\n"
        ".help\n.id\n.ping\n.cal\n"
        ".todo add Buy milk\n.todo list\n"
        ".todo done 1\n.remind 10 Take a break"
    )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Account setup:\n\n"
        "1. Generate your Telethon StringSession locally.\n"
        "2. Open your Render Web Service settings.\n"
        "3. Add TG_API_ID, TG_API_HASH and TG_STRING_SESSION.\n"
        "4. Redeploy your service.\n\n"
        "Security: never send your StringSession, login code, "
        "or two-step verification password in this chat.\n\n"
        "This command gives setup instructions; it does not "
        "automatically log in an account."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    required = ("TG_API_ID", "TG_API_HASH", "TG_STRING_SESSION")
    missing = [key for key in required if not os.environ.get(key)]

    if missing:
        message = "Missing Render environment variables:\n" + "\n".join(missing)
    else:
        message = (
            "Required environment variables are configured.\n"
            "This does not confirm the session is authorized "
            "or that the user client is running."
        )

    await update.effective_message.reply_text(message)


def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("status", status))
    application.run_polling()


if __name__ == "__main__":
    main()
