import os
from datetime import datetime, timedelta

import pytz
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==== SETTINGS ====

TOKEN = os.environ.get("BOT_TOKEN")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID")
TARGET_USER_ID = os.environ.get("TARGET_USER_ID")
TIMEZONE = os.environ.get("BOT_TZ", "Australia/Brisbane")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

client = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)


# ---------- HELPERS ----------

def get_tz():
    return pytz.timezone(TIMEZONE)


def compute_next_quarter(dt):
    next_run = dt.replace(minute=15, second=0, microsecond=0)
    if dt >= next_run:
        next_run += timedelta(hours=1)
    return next_run


def is_night_time(dt):
    return dt.hour >= 22 or dt.hour < 9


async def generate_ai_joke(user_text: str) -> str:
    if client is None:
        return "Сегодня я без доступа к шуткам, нужно добавить OPENAI_API_KEY 🤖"

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            instructions=(
                "Ты весёлый друг Максима. Отвечай коротко и смешно, "
                "на русском, слегка подшучивая, но без грубости."
            ),
            input=f"Сообщение: {user_text}\nПридумай смешной ответ."
        )
        return response.output_text.strip()
    except Exception as e:
        print("OpenAI error:", e)
        return "У меня сейчас юмор завис, как ноутбук Максима 😅"


# ---------- COMMANDS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я Друг Максима 🤖\n"
        "• В группе каждый час в :15 я пишу Максиму вопрос.\n"
        "• Ночью с 22:00 до 09:00 молчу.\n"
        "• В группе шучу только с одним человеком.\n"
        "Команда /whoami покажет твой user ID."
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"Your user ID: `{uid}`", parse_mode="Markdown")


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(f"Chat ID: `{cid}`", parse_mode="Markdown")


async def echo_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(f"Ты написал: {update.message.text}")


# ---------- GROUP MESSAGE PROCESSING ----------

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    # Only the configured group
    if GROUP_CHAT_ID and str(update.effective_chat.id) != str(GROUP_CHAT_ID):
        return

    # Only messages from the configured user
    if TARGET_USER_ID and str(update.effective_user.id) != str(TARGET_USER_ID):
        return

    joke = await generate_ai_joke(msg.text)
    await msg.reply_text(joke)


# ---------- SCHEDULED MESSAGE ----------

async def hourly_message(context: ContextTypes.DEFAULT_TYPE):
    if not GROUP_CHAT_ID:
        return

    tz = get_tz()
    now = datetime.now(tz)

    if is_night_time(now):
        print(f"{now} — night, skip")
        return

    try:
        await context.bot.send_message(
            chat_id=int(GROUP_CHAT_ID),
            text="Максим, как у тебя дела? Чем занимаешься?"
        )
        print(f"{now} — sent")
    except Exception as e:
        print("Send error:", e)


# ---------- MAIN ----------

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing!")

    app = Application.builder().token(TOKEN).build()

    # Commands (WORK IN PRIVATE AND GROUP)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("chatid", chat_id))

    # Private echo
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, echo_private))

    # Group AI handler
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, handle_group_message))

    # Scheduler
    tz = get_tz()
    now = datetime.now(tz)
    first_run = compute_next_quarter(now)

    app.job_queue.run_repeating(hourly_message, interval=3600, first=first_run)

    print("Bot is running with polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
