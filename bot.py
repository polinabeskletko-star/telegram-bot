import os
from datetime import datetime, timedelta

import pytz
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==== SETTINGS ====

# Bot token from environment
TOKEN = os.environ.get("BOT_TOKEN")

# Group chat ID where hourly question will be sent (e.g. "-1001234567890")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID")

# Timezone for scheduling. By default: Brisbane. You can override with BOT_TZ env var.
TIMEZONE = os.environ.get("BOT_TZ", "Australia/Brisbane")


# ---------- HELPERS ----------

def get_tz() -> pytz.BaseTzInfo:
    """Return timezone object from TIMEZONE setting."""
    return pytz.timezone(TIMEZONE)


def compute_next_half_hour(dt: datetime) -> datetime:
    """
    Return the next time at HH:30 after the given datetime `dt`.
    `dt` must be timezone-aware.
    Example: 09:21 -> 09:30, 09:35 -> 10:30, etc.
    """
    next_run = dt.replace(minute=30, second=0, microsecond=0)
    if dt >= next_run:
        next_run = next_run + timedelta(hours=1)
    return next_run


def is_night_time(dt: datetime) -> bool:
    """
    Define night time as 22:00–09:00 (inclusive of 22:00, exclusive of 09:00).
    During this time the bot will NOT send the question.
    """
    hour = dt.hour
    # Night if time is 22:00–23:59 or 00:00–08:59
    return hour >= 22 or hour < 9


# ---------- COMMAND HANDLERS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    chat_type = update.effective_chat.type
    if chat_type == "private":
        await update.message.reply_text(
            "Привет! Я Друг Максима 🤖\n"
            "В группе я каждый час в 30 минут буду спрашивать:\n"
            "«Максим, как у тебя дела? Чем занимаешься?»\n"
            "Ночью с 22:00 до 9:00 я молчу 😴"
        )
    else:
        await update.message.reply_text(
            "Я отправляю вопрос Максиму каждый час в 30 минут, "
            "кроме ночи с 22:00 до 9:00."
        )


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send back the current chat ID (useful to configure GROUP_CHAT_ID)."""
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"Chat ID for this chat: `{cid}`",
        parse_mode="Markdown"
    )


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Simple echo reply ONLY in private chats.
    In groups the bot stays quiet (except scheduled messages).
    """
    if update.effective_chat.type != "private":
        return

    text = update.message.text
    await update.message.reply_text(f"Ты написал: {text}")


# ---------- SCHEDULED HOURLY MESSAGE ----------

async def hourly_message(context: ContextTypes.DEFAULT_TYPE):
    """
    Send the hourly message to GROUP_CHAT_ID at HH:30,
    but only if it's not night time (22:00–09:00).
    """
    chat_id = GROUP_CHAT_ID
    if not chat_id:
        print("GROUP_CHAT_ID is not set; skipping hourly message.")
        return

    tz = get_tz()
    now = datetime.now(tz)

    if is_night_time(now):
        print(f"{now} – night time, message not sent.")
        return

    try:
        chat_id_int = int(chat_id)
        await context.bot.send_message(
            chat_id=chat_id_int,
            text="Максим, как у тебя дела? Чем занимаешься?"
        )
        print(f"{now} – message sent to chat {chat_id_int}")
    except Exception as e:
        print("Error sending hourly message:", e)


# ---------- MAIN APP ----------

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment variables!")

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chat_id))

    # Echo ONLY in private chats (no duplication in group)
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            echo,
        )
    )

    # JobQueue scheduling
    job_queue = app.job_queue
    tz = get_tz()
    now = datetime.now(tz)
    first_run = compute_next_half_hour(now)

    print(
        f"Local time now: {now} [{TIMEZONE}]. "
        f"First hourly_message scheduled at: {first_run} "
        f"(HH:40 each hour, skipping 22:00–09:00)."
    )

    # First run at next HH:30, then every 3600 seconds (1 hour)
    job_queue.run_repeating(
        hourly_message,
        interval=3600,
        first=first_run,
    )

    print("Bot started and hourly job scheduled...")
    app.run_polling()


if __name__ == "__main__":
    main()
