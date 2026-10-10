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

async def run_bot():
    if not BOT_TOKEN or not API_ID or not TG_API_HASH or not OWNER_ID_INT:
        raise RuntimeError("Missing required environment variables: BOT_TOKEN, TG_API_ID, TG_API_HASH, OWNER_ID")
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, session_message))
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    # Keep the bot process alive. Render's health endpoint is served separately in a thread.
    while True:
        await asyncio.sleep(3600)

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    import threading
    threading.Thread(target=run_flask, daemon=True).start()
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("Shutting down")
