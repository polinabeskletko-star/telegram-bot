import os
import random
import asyncio
from collections import defaultdict
from datetime import datetime, time

import pytz
import httpx
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==== SETTINGS & ENV ====

TOKEN = os.environ.get("BOT_TOKEN")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID")  # e.g. "-1001234567890"
TIMEZONE = os.environ.get("BOT_TZ", "Australia/Brisbane")

# Telegram user IDs
TARGET_USER_ID = int(os.environ.get("TARGET_USER_ID", "0"))   # Максим
SUPPORT_USER_ID = int(os.environ.get("SUPPORT_USER_ID", "0")) # Сергей

# Optional: куда слать служебные сообщения (например, тебе в личку)
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

# OpenAI
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

client: OpenAI | None = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)

# Память сообщений за день для вечернего обзора
DAILY_MESSAGES: defaultdict[str, list[str]] = defaultdict(list)


# ---------- HELPERS ----------

def get_tz() -> pytz.BaseTzInfo:
    return pytz.timezone(TIMEZONE)


def is_night_time(dt: datetime) -> bool:
    """
    Ночь: с 22:00 включительно до 07:00 (07:00 уже не ночь).
    """
    hour = dt.hour
    return hour >= 22 or hour < 7


async def log_to_admin(context: ContextTypes.DEFAULT_TYPE, message: str):
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=message)
        except Exception as e:
            print("Failed to send admin log:", e)


async def call_openai(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 120,
    temperature: float = 0.7,
) -> tuple[str | None, str | None]:
    """
    Обёртка над OpenAI. Возвращает (text, error_message).
    """
    if client is None:
        return None, "OpenAI client is not configured (no API key)."

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = resp.choices[0].message.content.strip()
        return text, None
    except Exception as e:
        err = f"Error calling OpenAI: {e}"
        print(err)
        return None, err


async def fetch_weather_summary() -> str | None:
    """
    Берём текущую погоду по координатам Брисбена через Open-Meteo.
    Без ключа, только httpx.
    """
    # Координаты Брисбена
    latitude = -27.47
    longitude = 153.03

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current_weather": "true",
        "timezone": TIMEZONE,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client_http:
            resp = await client_http.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        cw = data.get("current_weather") or {}
        temp = cw.get("temperature")
        code = cw.get("weathercode")

        if temp is None or code is None:
            return None

        # Очень грубая расшифровка кода
        if code == 0:
            desc = "ясно"
        elif code in (1, 2, 3):
            desc = "облачно"
        elif code in (45, 48):
            desc = "туман"
        elif 51 <= code <= 67:
            desc = "морось или дождь"
        elif 71 <= code <= 77:
            desc = "снег (если вдруг такое случится)"
        elif 80 <= code <= 82:
            desc = "дождевые ливни"
        elif 95 <= code <= 99:
            desc = "гроза, самое время задуматься о смысле жизни"
        else:
            desc = "какая-то странная погода, но жить можно"

        return f"В Брисбене сейчас около {temp}°C, {desc}."
    except Exception as e:
        print("Weather fetch error:", e)
        return None


async def generate_message_for_kind(
    kind: str,
    now: datetime,
    user_text: str | None = None,
    weather_text: str | None = None,
    day_messages: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """
    kind:
      - "sarcastic_reply"   — ответ Максиму
      - "support_for_maxim" — поддержка от имени бота на сообщения Сергея
      - "weekend_hourly"    — часовой вопрос по выходным
      - "weekday_morning"   — утреннее сообщение по будням (с погодой)
      - "daily_summary"     — вечерний саркастический обзор дня
    """
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    weekday_names = ["понедельник", "вторник", "среда", "четверг",
                     "пятница", "суббота", "воскресенье"]
    weekday_name = weekday_names[weekday]
    time_str = now.strftime("%H:%M")
    date_str = now.strftime("%Y-%m-%d")

    if kind == "sarcastic_reply":
        system_prompt = (
            "Ты дружелюбный, но довольно саркастичный бот-друг по имени 'Друг Максима'. "
            "Ты пишешь по-русски, на 'ты', коротко (1–2 предложения). "
            "Мягко подкалывай Максима, но без грубости и откровенной токсичности. "
            "Не используй эмодзи в каждом сообщении, максимум один, и не всегда."
        )
        user_prompt = (
            f"Сегодня {weekday_name}, время {time_str}. "
            f"Максим написал в чат: «{user_text}».\n"
            "Ответь коротко, с лёгкой иронией. Не повторяй дословно текст Максима. "
            "Сообщение должно быть самостоятельным, а не выглядеть как явный ответ."
        )
        return await call_openai(system_prompt, user_prompt, max_tokens=80, temperature=0.9)

    if kind == "support_for_maxim":
        system_prompt = (
            "Ты бот-поддержка Максима. Ты видишь сообщения от другого человека, "
            "который его подбадривает. Твоя задача — добавить ещё одну короткую, "
            "искреннюю, но не приторную поддержку для Максима. Пиши по-русски, на 'ты'. "
            "1 короткое предложение, максимум два. Не будь слишком льстивым, "
            "избегай громких слов типа 'невероятный', 'величайший' и т.п. "
            "Сообщение должно быть самостоятельным высказыванием, не ответом этому человеку. "
            "Обязательно упоминай Максима по имени хотя бы один раз."
        )
        user_prompt = (
            f"Сегодня {weekday_name}, время {time_str}. "
            f"Другой человек написал в чат слова поддержки Максиму: «{user_text}».\n"
            "Сформулируй от себя ещё одну естественную, живую поддержку для Максима."
        )
        return await call_openai(system_prompt, user_prompt, max_tokens=60, temperature=0.7)

    if kind == "weekend_hourly":
        system_prompt = (
            "Ты бот-друг Максима в Telegram-чате. "
            "По выходным ты примерно раз в час задаёшь Максиму вопрос, как у него дела "
            "и чем он занят. Пиши по-русски, на 'ты'. "
            "Коротко: 1–2 предложения. Можно иногда язвительно, но по-доброму. "
            "Не повторяй каждый раз одну и ту же формулировку. "
            "Не злоупотребляй эмодзи — максимум один, и не в каждом сообщении."
        )
        user_prompt = (
            f"Сейчас {weekday_name}, {time_str}. "
            "Придумай очередной вопрос или небольшое обращение к Максиму, "
            "которое поможет ему почувствовать внимание и немного улыбнуться."
        )
        return await call_openai(system_prompt, user_prompt, max_tokens=80, temperature=0.9)

    if kind == "weekday_morning":
        system_prompt = (
            "Ты бот-друг Максима в рабочем чате. "
            "По будням в 7 утра ты желаешь Максиму доброго утра и хорошего рабочего дня. "
            "Пиши по-русски, на 'ты', 1–2 предложения. "
            "Тон лёгкий, доброжелательный, можно с лёгким юмором и лёгким сарказмом. "
            "Упоминай, что впереди рабочий день. Эмодзи можно, но не обязательно."
        )
        weather_part = weather_text or "Про погоду тебе ничего не известно."
        user_prompt = (
            f"Сегодня {weekday_name}, дата {date_str}, время {time_str}. "
            f"Информация о погоде: {weather_part} "
            "Сделай короткое утреннее сообщение для Максима: поздоровайся, "
            "пожелай хорошего рабочего дня и намекни, что ты будешь за ним наблюдать в чате."
        )
        return await call_openai(system_prompt, user_prompt, max_tokens=80, temperature=0.8)

    if kind == "daily_summary":
        system_prompt = (
            "Ты саркастичный, но не злонамеренный бот-друг Максима. "
            "Ты подводишь итоги дня по переписке в чате. "
            "Пиши по-русски, на 'ты'. 3–6 предложений. "
            "Можешь иронизировать, подколоть участников, особенно Максима, "
            "но избегай оскорблений и жесткой токсичности."
        )
        messages_text = "\n".join(day_messages or [])
        # Немного ограничим размер
        if len(messages_text) > 3000:
            messages_text = messages_text[-3000:]

        user_prompt = (
            f"Сегодня {weekday_name}, дата {date_str}. Вот сообщения за день в чате:\n"
            f"{messages_text}\n\n"
            "Сделай краткий, саркастичный обзор дня в чате для Максима. "
            "Подчеркни самые забавные или типичные моменты."
        )
        return await call_openai(system_prompt, user_prompt, max_tokens=200, temperature=0.9)

    return None, "Unknown message kind"


# ---------- COMMAND HANDLERS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    if chat_type == "private":
        await update.message.reply_text(
            "Привет! Я Друг Максима 🤖\n"
            "В группе я буду:\n"
            "• По будням в 7:00 желать Максиму доброго утра и хорошего рабочего дня (с погодой).\n"
            "• По выходным писать ему примерно раз в час в случайное время.\n"
            "• В 20:30 делать саркастический обзор дня.\n"
            "Ночью с 22:00 до 7:00 я молчу 😴"
        )
    else:
        await update.message.reply_text(
            "Я здесь, чтобы поддерживать Максима и слегка его подкалывать:\n"
            "• Будни: сообщение в 7:00 с погодой.\n"
            "• Выходные: раз в час в случайную минуту.\n"
            "• Каждый день в 20:30 — обзор дня.\n"
            "Ночью с 22:00 до 7:00 я не беспокою."
        )


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"Chat ID for this chat: `{cid}`",
        parse_mode="Markdown",
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Your user ID: `{user.id}`\nUsername: @{user.username}",
        parse_mode="Markdown",
    )


async def echo_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Echo только в личке, в группах молчим."""
    if update.effective_chat.type != "private":
        return
    text = update.message.text
    await update.message.reply_text(f"Ты написал: {text}")


# ---------- GROUP MESSAGE HANDLER ----------

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None:
        return

    chat = message.chat
    user = message.from_user
    text = message.text or ""

    chat_id = chat.id
    user_id = user.id

    print(
        f"DEBUG UPDATE: chat_id={chat_id} chat_type={chat.type} "
        f"user_id={user_id} user_name={user.username} text='{text}'"
    )

    # Если это не целевой групповой чат, ничего не делаем
    if GROUP_CHAT_ID and int(GROUP_CHAT_ID) != chat_id:
        return

    # Логируем сообщение для вечернего обзора
    tz = get_tz()
    now = datetime.now(tz)
    date_key = now.strftime("%Y-%m-%d")
    author = user.first_name or user.username or str(user_id)
    DAILY_MESSAGES[date_key].append(f"{author}: {text}")
    # ограничим размер списка, чтобы не раздувался бесконечно
    if len(DAILY_MESSAGES[date_key]) > 200:
        DAILY_MESSAGES[date_key] = DAILY_MESSAGES[date_key][-200:]

    # Сообщения Максима — саркастичный ответ
    if TARGET_USER_ID and user_id == TARGET_USER_ID:
        ai_text, err = await generate_message_for_kind(
            "sarcastic_reply", now=now, user_text=text
        )
        if ai_text is None:
            fallback = "Максим, я даже не знаю, что сказать… Ты сам понял, что написал? 😉"
            print(f"OpenAI error for sarcastic_reply: {err}")
            await message.chat.send_message(fallback)
            return

        await message.chat.send_message(ai_text)
        return

    # Сообщения Сергея — дополнительная поддержка Максима
    if SUPPORT_USER_ID and user_id == SUPPORT_USER_ID:
        ai_text, err = await generate_message_for_kind(
            "support_for_maxim", now=now, user_text=text
        )
        if ai_text is None:
            fallback = "Максим, кажется, вселенная сегодня явно за тебя."
            print(f"OpenAI error for support_for_maxim: {err}")
            await message.chat.send_message(fallback)
            return

        await message.chat.send_message(ai_text)
        return

    # Остальные пользователи — бот молчит (в группе)
    return


# ---------- SCHEDULED JOBS ----------

async def weekend_random_hourly_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Запускается каждую минуту.
    По выходным раз в час выбирает случайную минуту и в неё шлёт сообщение Максиму.
    """
    if not GROUP_CHAT_ID:
        return

    tz = get_tz()
    now = datetime.now(tz)

    weekday = now.weekday()  # 0=Mon ... 6=Sun
    if weekday < 5:
        # Будни — этим джобом не занимаемся
        return

    # Ночной режим
    if is_night_time(now):
        return

    job = context.job
    if job.data is None:
        job.data = {}

    data = job.data
    current_hour = now.hour
    last_hour = data.get("last_hour")
    target_minute = data.get("target_minute")
    sent_this_hour = data.get("sent_this_hour", False)

    # Новый час — планируем новую случайную минуту и сбрасываем флаг
    if last_hour is None or current_hour != last_hour:
        target_minute = random.randint(0, 59)
        sent_this_hour = False
        data["last_hour"] = current_hour
        data["target_minute"] = target_minute
        data["sent_this_hour"] = sent_this_hour
        print(f"[Weekend scheduler] New hour {current_hour}, planned minute {target_minute}")

    # Если ещё не отправляли в этом часе и наступила нужная минута — шлём
    if not sent_this_hour and now.minute == target_minute:
        text, err = await generate_message_for_kind(
            "weekend_hourly", now=now
        )
        if text is None:
            text = "Максим, как у тебя дела? Чем сейчас занимаешься?"
            print(f"OpenAI error for weekend_hourly: {err}")

        try:
            await context.bot.send_message(
                chat_id=int(GROUP_CHAT_ID),
                text=text,
            )
            data["sent_this_hour"] = True
            print(f"[Weekend scheduler] Sent hourly message at {now}")
        except Exception as e:
            print("Error sending weekend hourly message:", e)

    job.data = data


async def weekday_morning_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Запускается в 7:00 по будням.
    """
    if not GROUP_CHAT_ID:
        return

    tz = get_tz()
    now = datetime.now(tz)

    weekday = now.weekday()
    if weekday >= 5:
        # На всякий случай: по выходным это сообщение не нужно
        return

    weather_text = await fetch_weather_summary()

    text, err = await generate_message_for_kind(
        "weekday_morning", now=now, weather_text=weather_text
    )
    if text is None:
        base = "Доброе утро, Максим! Удачи сегодня на работе — я слежу за тобой из чата. 😉"
        if weather_text:
            text = f"{base}\n\nКстати, {weather_text}"
        else:
            text = base
        print(f"OpenAI error for weekday_morning: {err}")

    try:
        await context.bot.send_message(
            chat_id=int(GROUP_CHAT_ID),
            text=text,
        )
        print(f"[Weekday morning] Sent morning message at {now}")
    except Exception as e:
        print("Error sending weekday morning message:", e)


async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Вечерний саркастический обзор в 20:30 каждый день.
    """
    if not GROUP_CHAT_ID:
        return

    tz = get_tz()
    now = datetime.now(tz)
    date_key = now.strftime("%Y-%m-%d")
    messages = DAILY_MESSAGES.get(date_key, [])

    if not messages:
        text = "Сегодня в чате тишина. Видимо, жизнь у всех настолько насыщенная, что даже пожаловаться некогда."
    else:
        text, err = await generate_message_for_kind(
            "daily_summary", now=now, day_messages=messages
        )
        if text is None:
            text = "Итоги дня: что-то вы тут писали, но у меня нет сил всё это анализировать. Считай, что день прошёл… как обычно."
            print(f"OpenAI error for daily_summary: {err}")

    try:
        await context.bot.send_message(
            chat_id=int(GROUP_CHAT_ID),
            text=text,
        )
        print(f"[Daily summary] Sent summary at {now} with {len(messages)} messages.")
    except Exception as e:
        print("Error sending daily summary:", e)

    # Чистим за прошедший день
    if date_key in DAILY_MESSAGES:
        del DAILY_MESSAGES[date_key]


# ---------- MAIN APP ----------

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment variables!")

    print("Starting bot application...")

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CommandHandler("whoami", whoami))

    # Echo only in private chats
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
            echo_private,
        )
    )

    # Group messages in target chat
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
            handle_group_message,
        )
    )

    # JobQueue scheduling
    job_queue = app.job_queue
    tz = get_tz()
    now = datetime.now(tz)

    print(
        f"Local time now: {now} [{TIMEZONE}]. "
        "Scheduling weekday morning, weekend hourly and daily summary jobs."
    )

    # Будние утренние сообщения в 7:00 (пн–пт)
    job_queue.run_daily(
        weekday_morning_job,
        time=time(7, 0, tzinfo=tz),
        days=(0, 1, 2, 3, 4),
        name="weekday_morning_job",
    )

    # Выходные: джоба раз в минуту, внутри — логика случайной минуты
    job_queue.run_repeating(
        weekend_random_hourly_job,
        interval=60,          # каждую минуту
        first=0,              # сразу
        name="weekend_random_hourly_job",
        data={},              # для хранения состояния по часам
    )

    # Вечерний обзор в 20:30 каждый день
    job_queue.run_daily(
        daily_summary_job,
        time=time(20, 30, tzinfo=tz),
        days=(0, 1, 2, 3, 4, 5, 6),
        name="daily_summary_job",
    )

    print("Bot started and jobs scheduled...")
    app.run_polling()


if __name__ == "__main__":
    main()
