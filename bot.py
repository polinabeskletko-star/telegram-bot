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
from openai import OpenAI

# ==== SETTINGS ====

# Bot token from environment
TOKEN = os.environ.get("BOT_TOKEN")

# Group chat ID where hourly question will be sent (e.g. "-1001234567890")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID")

# Timezone for scheduling. By default: Brisbane. You can override with BOT_TZ env var.
TIMEZONE = os.environ.get("BOT_TZ", "Australia/Brisbane")

# OpenAI
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Максим (саркастичные ответы)
TARGET_USER_ID = int(os.environ.get("TARGET_USER_ID", "0"))

# Второй пользователь (поддержка Максима)
SUPPORT_USER_ID = int(os.environ.get("SUPPORT_USER_ID", "0"))

# Твой личный чат для уведомлений о запуске
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")


# ---------- HELPERS ----------

def get_tz() -> pytz.BaseTzInfo:
    """Return timezone object from TIMEZONE setting."""
    return pytz.timezone(TIMEZONE)


def compute_next_quarter(dt: datetime) -> datetime:
    """
    Return the next time at HH:15 after the given datetime `dt`.
    Example: 09:02 -> 09:15, 09:20 -> 10:15, etc.
    `dt` must be timezone-aware.
    """
    next_run = dt.replace(minute=15, second=0, microsecond=0)
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


async def generate_sarcastic_reply(user_text: str) -> str:
    """Саркастичный ответ для Максима через OpenAI, с fallback, если API не сработал."""
    prompt = (
        "Ты дружелюбный, но слегка саркастичный друг по имени Друг Максима. "
        "Ты отвечаешь по-русски. Тон добрый, без оскорблений, но с лёгкой иронией. "
        "Отвечай коротко (1–2 предложения). "
        f"Сообщение Максима: «{user_text}»"
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": "Ты дружелюбный и немного саркастичный друг Максима."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=80,
            temperature=0.8,
        )
        text = response.choices[0].message.content.strip()
        return text
    except Exception as e:
        print("Error calling OpenAI, using fallback joke:", e)
        return "Максим, я даже не знаю, что сказать… Ты сам понял, что написал? 😏"


async def generate_support_reply_for_maxim(original_text: str) -> str:
    """
    Короткая, тёплая поддержка Максима, основанная на сообщении другого человека.
    Ответ должен выглядеть как самостоятельное утверждение, а не прямой ответ.
    """
    prompt = (
        "Ты чат-бот 'Друг Максима'. Ты видишь сообщение от друга Максима, "
        "который пытается его поддержать. На основе этого сообщения придумай "
        "очень короткую (1–2 предложения) поддержку именно для Максима. "
        "Не обращайся к автору сообщения, обращайся только к Максиму. "
        "Не будь чрезмерно пафосным и приторным, просто добрые, спокойные слова. "
        "Пиши по-русски.\n\n"
        f"Сообщение друга: «{original_text}»"
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": "Ты добрый друг Максима и поддерживаешь его."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=80,
            temperature=0.7,
        )
        text = response.choices[0].message.content.strip()
        return text
    except Exception as e:
        print("Error calling OpenAI for support reply, using fallback:", e)
        return "Максим, рядом есть люди, которые в тебя верят. И я в том числе."


# ---------- COMMAND HANDLERS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    chat_type = update.effective_chat.type
    if chat_type == "private":
        await update.message.reply_text(
            "Привет! Я Друг Максима 🤖\n"
            "В группе я каждый час в 15 минут буду спрашивать:\n"
            "«Максим, как у тебя дела? Чем занимаешься?»\n"
            "Ночью с 22:00 до 9:00 я молчу 😴"
        )
    else:
        await update.message.reply_text(
            "Я отправляю вопрос Максиму каждый час в 15 минут, "
            "кроме ночи с 22:00 до 9:00."
        )


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send back the current chat ID (useful to configure GROUP_CHAT_ID)."""
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"Chat ID for this chat: `{cid}`",
        parse_mode="Markdown"
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return user id for debugging / env configuration."""
    user = update.effective_user
    await update.message.reply_text(
        f"Ваш user_id: `{user.id}`",
        parse_mode="Markdown",
    )


async def echo_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Simple echo reply ONLY in private chats.
    In groups the bot stays quiet (except scheduled messages and special replies).
    """
    if update.effective_chat.type != "private":
        return

    text = update.message.text
    await update.message.reply_text(f"Ты написал: {text}")


# ---------- MESSAGE HANDLER FOR GROUP ----------

async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатываем сообщения в группе:
    - если пишет Максим (TARGET_USER_ID) -> саркастичный ответ;
    - если пишет SUPPORT_USER_ID -> короткая поддержка Максима.
    """
    message = update.message
    if not message:
        return

    chat = update.effective_chat
    user = update.effective_user

    chat_id = chat.id
    user_id = user.id
    user_name = user.username or user.full_name
    text = message.text or ""

    print(
        f"DEBUG UPDATE: chat_id={chat_id} chat_type={chat.type} "
        f"user_id={user_id} user_name={user_name} text={text!r}"
    )

    # Только в группе, не в личке
    if chat.type not in ("group", "supergroup"):
        return

    # Максим — сарказм
    if TARGET_USER_ID and user_id == TARGET_USER_ID:
        print(f"TARGET MESSAGE (Maxim): from user {user_id} in chat {chat_id}: {text!r}")
        reply_text = await generate_sarcastic_reply(text)
        await message.reply_text(reply_text)
        print("Sarcastic reply sent.")
        return

    # Друг, поддерживающий Максима
    if SUPPORT_USER_ID and user_id == SUPPORT_USER_ID:
        print(f"SUPPORT MESSAGE: from user {user_id} in chat {chat_id}: {text!r}")
        reply_text = await generate_support_reply_for_maxim(text)
        # ВАЖНО: ответ не как reply, чтобы выглядел самостоятельным
        await context.bot.send_message(chat_id=chat_id, text=reply_text)
        print("Support reply for Maxim sent.")
        return

    # Остальных игнорируем (бот молчит)
    return


# ---------- SCHEDULED HOURLY MESSAGE ----------

async def hourly_message(context: ContextTypes.DEFAULT_TYPE):
    """
    Send the hourly message to GROUP_CHAT_ID at HH:15,
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


# ---------- STARTUP NOTIFICATION ----------

async def on_startup(app: Application):
    """
    Отправляет тебе в личный Telegram сообщение, что бот запустился.
    Вызывается один раз после инициализации приложения.
    """
    if not OWNER_CHAT_ID:
        print("OWNER_CHAT_ID is not set; startup notification skipped.")
        return

    try:
        owner_id = int(OWNER_CHAT_ID)
        tz = get_tz()
        now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
        text = f"🤖 Бот «Друг Максима» перезапущен и работает (время сервера: {now} {TIMEZONE})."
        await app.bot.send_message(chat_id=owner_id, text=text)
        print(f"Startup notification sent to OWNER_CHAT_ID={owner_id}")
    except Exception as e:
        print("Failed to send startup notification:", e)


# ---------- MAIN APP ----------

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment variables!")

    app = Application.builder().token(TOKEN).post_init(on_startup).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CommandHandler("whoami", whoami))

    # Echo ONLY in private chats (no duplication in group)
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            echo_private,
        )
    )

    # Group handler (Maxim + support user)
    app.add_handler(
        MessageHandler(
            filters.TEXT & (filters.ChatType.GROUPS),
            group_message_handler,
        )
    )

    # JobQueue scheduling
    job_queue = app.job_queue
    tz = get_tz()
    now = datetime.now(tz)
    first_run = compute_next_quarter(now)

    print(
        f"Local time now: {now} [{TIMEZONE}]. "
        f"First hourly_message scheduled at: {first_run} "
        f"(HH:15 each hour, skipping 22:00–09:00)."
    )

    # First run at next HH:15, then every 3600 seconds (1 hour)
    job_queue.run_repeating(
        hourly_message,
        interval=3600,
        first=first_run,
    )

    print("Bot started and hourly job scheduled...")
    app.run_polling()


if __name__ == "__main__":
    main()