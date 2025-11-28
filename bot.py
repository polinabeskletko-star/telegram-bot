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

# ================== SETTINGS / ENV VARS ==================

# Telegram bot token
TOKEN = os.environ.get("BOT_TOKEN")

# Group chat ID where hourly question will be sent (e.g. "-1001234567890")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID")

# Timezone (default: Brisbane)
TIMEZONE = os.environ.get("BOT_TZ", "Australia/Brisbane")

# Target user and chat for sarcastic replies (Максим)
TARGET_USER_ID_ENV = os.environ.get("TARGET_USER_ID")   # numeric string
TARGET_CHAT_ID = os.environ.get("TARGET_CHAT_ID")       # string chat id
TARGET_USER_ID = int(TARGET_USER_ID_ENV) if TARGET_USER_ID_ENV else None

# Second user: пишет про Максима, бот усиливает поддержку Максима
SUPPORT_USER_ID_ENV = os.environ.get("SUPPORT_USER_ID")
SUPPORT_USER_ID = (
    int(SUPPORT_USER_ID_ENV)
    if SUPPORT_USER_ID_ENV
    else 502791142  # дефолт
)

# OpenAI
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
client = OpenAI()  # API key берётся из OPENAI_API_KEY


# ================== HELPERS ==================

def get_tz() -> pytz.BaseTzInfo:
    """Return timezone object from TIMEZONE setting."""
    return pytz.timezone(TIMEZONE)


def compute_next_quarter_hour(dt: datetime) -> datetime:
    """
    Return the next time at HH:15 after the given datetime `dt`.
    `dt` must be timezone-aware.
    Example: 09:02 -> 09:15, 09:20 -> 10:15, etc.
    """
    next_run = dt.replace(minute=15, second=0, microsecond=0)
    if dt >= next_run:
        next_run = next_run + timedelta(hours=1)
    return next_run


def is_night_time(dt: datetime) -> bool:
    """
    Night time = 22:00–09:00 (inclusive 22:00, exclusive 09:00).
    During this time the bot will NOT send the hourly question.
    """
    hour = dt.hour
    return hour >= 22 or hour < 9


def describe_part_of_day_ru(dt: datetime) -> str:
    """Return Russian description of time of day."""
    hour = dt.hour
    if 9 <= hour < 12:
        return "утро"
    elif 12 <= hour < 18:
        return "день"
    elif 18 <= hour < 22:
        return "вечер"
    else:
        return "ночь"


def build_hourly_prompt(now: datetime) -> str:
    """Prompt для генерации ежечасного вопроса к Максиму."""
    weekday_names = [
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
    ]
    weekday = weekday_names[now.weekday()]
    part_of_day = describe_part_of_day_ru(now)

    return (
        "Сгенерируй ОДИН короткий вопрос по-русски для телеграм-чата, "
        "обращаясь к Максиму по имени. "
        "Смысл: узнать, как у него дела и чем он сейчас занимается. "
        "Стиль: дружелюбный, чуть-чуть шутливый, но без грубостей. "
        "Не пиши смайлики и не используй хэштеги. "
        "Это должно быть самостоятельное сообщение в чат, а не ответ на чью-то реплику. "
        "Упомяни в формулировке, что сейчас " + part_of_day +
        " и " + weekday + ". "
        "Максимум 20 слов. Только текст вопроса, без пояснений."
    )


def build_sarcastic_prompt(user_text: str) -> str:
    """Prompt для саркастического сообщения про Максима."""
    return (
        "Ты язвительный, но доброжелательный друг в телеграм-чате. "
        "В чате есть Максим, к которому ты обращаешься на 'ты' по имени. "
        "Сделай одно короткое шутливое сообщение по-русски, как будто ты комментируешь поведение или слова Максима. "
        "Стиль: лёгкий сарказм, без оскорблений, без мата, максимум 25 слов. "
        "Это должно быть самостоятельное сообщение в чат, а не ответ на чью-то реплику, не упоминай, что кто-то что-то писал. "
        "Не используй смайлики и хэштеги. "
        "Текст, который писал Максим:\n\n"
        f"{user_text}\n\n"
        "Теперь придумай одно подходящее саркастическое сообщение в чат, обращаясь к Максиму. "
        "Только текст сообщения, без пояснений."
    )


def build_supportive_prompt(user_text: str) -> str:
    """
    Prompt для поддерживающего сообщения для Максима
    на основе реплики второго пользователя.
    Сообщение должно быть коротким и без чрезмерной лести.
    """
    return (
        "Ты спокойный, поддерживающий друг в телеграм-чате. "
        "В чате есть Максим, к которому ты обращаешься на 'ты' по имени. "
        "Ниже дано сообщение другого пользователя, который описывает, что Максим делает, чувствует или как он себя ведёт. "
        "Твоя задача — написать одно короткое, простое сообщение по-русски, которое поддерживает Максима: "
        "покажи, что ты видишь его усилия и веришь, что он справится. "
        "Стиль: естественный разговорный, без пафоса и без чрезмерных комплиментов. "
        "Не используй слова вроде «невероятно», «великолепный», «вдохновляешь всех вокруг» и подобные громкие фразы. "
        "Обращайся к Максиму напрямую. "
        "Это должно быть самостоятельное сообщение в чат, а не прямой ответ на чью-то реплику. "
        "Не упоминай других людей и не говори, что отвечаешь на чьё-то сообщение. "
        "Максимум 15 слов. Не используй смайлики и хэштеги. "
        "Сообщение другого пользователя (про Максима):\n\n"
        f"{user_text}\n\n"
        "Теперь придумай одно короткое поддерживающее сообщение, обращаясь к Максиму. "
        "Только текст сообщения, без пояснений."
    )


def generate_ai_text(prompt: str, fallback: str) -> str:
    """
    Вспомогательная функция: вызвать OpenAI Responses API и вернуть текст.
    В случае ошибки вернёт fallback и напечатает ошибку в логи.
    """
    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
        )
        if resp.output and resp.output[0].content:
            text = resp.output[0].content[0].text.strip()
            if text:
                return text
    except Exception as e:
        print("Error calling OpenAI, using fallback text:", e)

    return fallback


# ================== COMMAND HANDLERS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    chat_type = update.effective_chat.type
    if chat_type == "private":
        await update.message.reply_text(
            "Привет! Я Друг Максима 🤖\n"
            "В группе я каждый час в :15 спрашиваю, как у Максима дела, формулировки зависят от времени суток.\n"
            "Ночью с 22:00 до 9:00 я молчу 😴\n"
            "В чате я шучу над Максимом и поддерживаю его, "
            "когда другой пользователь пишет про него что-то хорошее."
        )
    else:
        await update.message.reply_text(
            "Я отправляю вопрос Максиму каждый час в :15, кроме ночи с 22:00 до 9:00. "
            "Саркастически комментирую сообщения Максима и поддерживаю его, "
            "когда второй выбранный пользователь пишет про него."
        )


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send back the current chat ID (useful to configure GROUP_CHAT_ID / TARGET_CHAT_ID)."""
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"Chat ID for this chat: `{cid}`",
        parse_mode="Markdown"
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Return user id for testing TARGET_USER_ID / SUPPORT_USER_ID."""
    user = update.effective_user
    if not user:
        return
    await update.message.reply_text(f"Your user id: `{user.id}`", parse_mode="Markdown")


async def echo_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Simple echo reply ONLY in private chats.
    In groups the bot stays quiet (except scheduled messages + target jokes/support).
    """
    if update.effective_chat.type != "private":
        return

    text = update.message.text
    await update.message.reply_text(f"Ты написал: {text}")


# ================== GROUP MESSAGE HANDLER (JOKES & SUPPORT) ==================

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатываем сообщения в группах.
    - TARGET_USER_ID (Максим): язвительное, но доброе сообщение про него.
    - SUPPORT_USER_ID: поддерживающее сообщение для Максима на основе текста второго пользователя.
    Все ответы — отдельные сообщения в чат, НЕ reply.
    """
    message = update.message
    if not message:
        return

    chat = update.effective_chat
    user = update.effective_user
    text = message.text or ""

    chat_id_str = str(chat.id)
    user_id = user.id if user else None
    user_name = user.username if user and user.username else (user.full_name if user else "Unknown")

    print(
        f"DEBUG UPDATE: chat_id={chat.id} chat_type={chat.type} "
        f"user_id={user_id} user_name={user_name} text='{text}'"
    )

    # Ограничиваемся целевым чатом (если задан)
    if TARGET_CHAT_ID and chat_id_str != TARGET_CHAT_ID:
        return

    if user_id is None:
        return

    # ----- Ветка 1: сарказм для Максима (TARGET_USER_ID) -----
    if TARGET_USER_ID is not None and user_id == TARGET_USER_ID:
        print(
            f"TARGET (sarcastic) MESSAGE: from user {user_id} in chat {chat.id}: '{text}'"
        )

        prompt = build_sarcastic_prompt(text)
        fallback = "Максим, ты как всегда на высоте… по уровню хаоса в своём расписании."
        reply_text = generate_ai_text(prompt, fallback)

        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=reply_text
            )
            print("Sarcastic standalone message sent.")
        except Exception as e:
            print("Error sending sarcastic message:", e)
        return

    # ----- Ветка 2: поддержка для Максима на основе второго пользователя -----
    if SUPPORT_USER_ID is not None and user_id == SUPPORT_USER_ID:
        print(
            f"SUPPORT (for Maxim) MESSAGE: from user {user_id} in chat {chat.id}: '{text}'"
        )

        prompt = build_supportive_prompt(text)
        fallback = "Максим, видно, что ты стараешься. Всё получится, просто продолжай двигаться вперёд."
        reply_text = generate_ai_text(prompt, fallback)

        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=reply_text
            )
            print("Supportive standalone message sent.")
        except Exception as e:
            print("Error sending supportive message:", e)
        return

    # Остальные пользователи — игнор
    return


# ================== SCHEDULED HOURLY MESSAGE ==================

async def hourly_message(context: ContextTypes.DEFAULT_TYPE):
    """
    Ежечасное сообщение в GROUP_CHAT_ID в HH:15,
    но только если не ночь (22:00–09:00).
    Текст формируется через OpenAI, чтобы фразы отличались и учитывали время суток.
    """
    chat_id = GROUP_CHAT_ID
    if not chat_id:
        print("GROUP_CHAT_ID is not set; skipping hourly message.")
        return

    tz = get_tz()
    now = datetime.now(tz)

    if is_night_time(now):
        print(f"{now} – night time, hourly message not sent.")
        return

    prompt = build_hourly_prompt(now)
    fallback = "Максим, как у тебя дела? Чем занимаешься сейчас?"

    text = generate_ai_text(prompt, fallback)

    try:
        chat_id_int = int(chat_id)
        await context.bot.send_message(
            chat_id=chat_id_int,
            text=text
        )
        print(f"{now} – hourly AI message sent to chat {chat_id_int}: {text}")
    except Exception as e:
        print("Error sending hourly message:", e)


# ================== MAIN APP ==================

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment variables!")

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CommandHandler("whoami", whoami))

    # Private echo
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            echo_private,
        )
    )

    # Group messages (for sarcastic + supportive replies)
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
            handle_group_message,
        )
    )

    # JobQueue scheduling (HH:15 every hour)
    job_queue = app.job_queue
    tz = get_tz()
    now = datetime.now(tz)
    first_run = compute_next_quarter_hour(now)

    print(
        f"Local time now: {now} [{TIMEZONE}]. "
        f"First hourly_message scheduled at: {first_run} "
        f"(HH:15 each hour, skipping 22:00–09:00)."
    )

    job_queue.run_repeating(
        hourly_message,
        interval=3600,   # every hour
        first=first_run,
    )

    print(
        "Bot started and hourly AI job scheduled...\n"
        f"TARGET_USER_ID (sarcasm): {TARGET_USER_ID}, "
        f"SUPPORT_USER_ID (support for Maxim): {SUPPORT_USER_ID}, "
        f"TARGET_CHAT_ID: {TARGET_CHAT_ID}"
    )
    app.run_polling()


if __name__ == "__main__":
    main()