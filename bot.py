import os
import random
import asyncio
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

# Optional: личка владельца для служебных сообщений
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")
ADMIN_CHAT_ID = OWNER_CHAT_ID or os.environ.get("ADMIN_CHAT_ID")

# OpenAI
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

client: OpenAI | None = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)

# Погода (Open-Meteo, без ключа)
BRISBANE = {"name": "Брисбене", "lat": -27.47, "lon": 153.03}
KALUGA = {"name": "Калуге", "lat": 54.51, "lon": 36.27}


# ---------- HELPERS ----------

def get_tz() -> pytz.BaseTzInfo:
    return pytz.timezone(TIMEZONE)


def is_night_time(dt: datetime) -> bool:
    """Ночь: с 22:00 включительно до 07:00 не пишем вообще."""
    hour = dt.hour
    return hour >= 22 or hour < 7


async def log_to_admin(context: ContextTypes.DEFAULT_TYPE, message: str):
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=message)
        except Exception as e:
            print("Failed to send admin log:", e)


async def call_openai(system_prompt: str, user_prompt: str,
                      max_tokens: int = 120, temperature: float = 0.7) -> tuple[str | None, str | None]:
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


async def fetch_weather_summary(city: dict) -> str | None:
    """
    Получаем краткую сводку погоды через Open-Meteo.
    city = {"name": "Брисбене", "lat": -27.47, "lon": 153.03}
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "current_weather": "true",
        "daily": "temperature_2m_max,temperature_2m_min",
        "forecast_days": 1,
        "timezone": "auto",
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client_http:
            r = await client_http.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        print(f"Weather API error for {city['name']}: {e}")
        return None

    current = data.get("current_weather", {}) or {}
    daily = data.get("daily", {}) or {}

    temp_now = current.get("temperature")
    max_list = daily.get("temperature_2m_max") or []
    min_list = daily.get("temperature_2m_min") or []
    t_max = max_list[0] if max_list else None
    t_min = min_list[0] if min_list else None

    parts: list[str] = []
    if temp_now is not None:
        parts.append(f"сейчас около {round(temp_now)}°C")
    if t_min is not None and t_max is not None:
        parts.append(f"в течение дня от {round(t_min)}°C до {round(t_max)}°C")

    if not parts:
        return None

    return f"Погода в {city['name']}: " + ", ".join(parts)


async def generate_message_for_kind(
    kind: str,
    now: datetime,
    user_text: str | None = None,
    weather_summary: str | None = None,
    day_messages: list[dict] | None = None,
    comparison_text: str | None = None,
) -> tuple[str | None, str | None]:
    """
    kind:
      - "sarcastic_reply"     — ответ Максиму
      - "support_for_maxim"   — поддержка от имени бота на сообщения Сергея
      - "weekend_regular"     — выходные, раз в 3 часа, с погодой
      - "weekday_morning"     — будни, 7:00, с погодой
      - "evening_summary"     — обзор дня в 20:30
      - "weather_comparison"  — сравнение погоды Брисбен vs Калуга
      - "good_night"          — спокойной ночи в 21:00
    """
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    weekday_names = [
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье",
    ]
    weekday_name = weekday_names[weekday]
    time_str = now.strftime("%H:%M")

    # ---- Ответ Максиму, сарказм ----
    if kind == "sarcastic_reply":
        system_prompt = (
            "Ты дружелюбный, но максимально саркастичный бот-друг по имени 'Друг Максима'. "
            "Пишешь по-русски, на 'ты', 1–2 предложения. "
            "Подкалываешь Максима жёстко, но без токсичности и оскорблений. "
            "Иногда слегка абсурдный юмор, можно использовать метафоры. "
            "Не повторяй текст Максима, не отвечай прямо на его реплику — "
            "сообщение должно выглядеть как самостоятельное наблюдение. "
            "Эмодзи можно, но не во всех сообщениях и не больше одного-двух."
        )
        user_prompt = (
            f"Сегодня {weekday_name}, время {time_str}. "
            f"Максим написал в чат: «{user_text}».\n"
            "Сделай короткий саркастичный комментарий в его адрес, как будто ты давно его знаешь "
            "и уже ничему не удивляешься."
        )
        return await call_openai(system_prompt, user_prompt, max_tokens=80, temperature=0.9)

    # ---- Поддержка Максима (сообщения Сергея) ----
    if kind == "support_for_maxim":
        system_prompt = (
            "Ты бот-поддержка Максима. Ты видишь сообщения другого человека, "
            "который его подбадривает. Твоя задача — добавить ещё одно короткое, "
            "искреннее, но не приторное сообщение поддержки. "
            "Пиши по-русски, на 'ты', 1 короткое предложение, максимум два. "
            "Избегай пафосных слов типа 'гениальный', 'величайший', 'невероятный'. "
            "Сообщение самостоятельное, НЕ ответ этому человеку. "
            "Обязательно упоминай Максима по имени хотя бы один раз."
        )
        user_prompt = (
            f"Сегодня {weekday_name}, время {time_str}. "
            f"Другой человек написал в чат слова поддержки Максиму: «{user_text}».\n"
            "Сформулируй от себя ещё одну естественную, живую, но короткую поддержку для Максима."
        )
        return await call_openai(system_prompt, user_prompt, max_tokens=60, temperature=0.7)

    # ---- Выходные, регулярные сообщения с погодой ----
    if kind == "weekend_regular":
        system_prompt = (
            "Ты бот-друг Максима в Telegram-чате. "
            "По выходным ты примерно раз в три часа пишешь Максиму что-то смешное и задаёшь вопрос, "
            "как у него дела или чем он занят. "
            "Пиши по-русски, на 'ты', 1–2 предложения, можно с юмором и лёгкой иронией. "
            "Обязательно упоминай погоду, но не сухо как синоптик, а в забавном или бытовом контексте. "
            "Не повторяй одну и ту же формулировку, не используй шаблонные фразы из мотивационных книг."
        )
        weather_part = weather_summary or "данных о погоде нет, но представь, что она эпичная."
        user_prompt = (
            f"Сейчас {weekday_name}, {time_str}. {weather_part}\n"
            "Придумай короткое смешное обращение к Максиму с вопросом о том, чем он занимается, "
            "с отсылкой к погоде."
        )
        return await call_openai(system_prompt, user_prompt, max_tokens=90, temperature=0.9)

    # ---- Будни, утро 7:00 с погодой ----
    if kind == "weekday_morning":
        system_prompt = (
            "Ты бот-друг Максима в рабочем чате. "
            "По будням в 7 утра ты желаешь ему доброго утра и хорошего рабочего дня. "
            "Пишешь по-русски, на 'ты', 1–2 предложения. "
            "Тон доброжелательный, с лёгким юмором. "
            "Обязательно упоминай погоду и как она сочетается с рабочим днём Максима. "
            "Можно слегка подшутить над тем, что ему опять надо вставать и работать."
        )
        weather_part = weather_summary or "про погоду сведений нет, но можем сделать вид, что всё идеально."
        user_prompt = (
            f"Сегодня {weekday_name}, время {time_str}. {weather_part}\n"
            "Сделай короткое утреннее сообщение для Максима: поприветствуй, "
            "пожелай хорошего рабочего дня и свяжи это с погодой."
        )
        return await call_openai(system_prompt, user_prompt, max_tokens=90, temperature=0.8)

    # ---- Вечерний обзор дня ----
    if kind == "evening_summary":
        system_prompt = (
            "Ты — язвительный, но добрый бот-наблюдатель за чатом Максима. "
            "Твоя задача — сделать короткий саркастический обзор активности за день. "
            "Пиши по-русски, на 'вы', но Максиму можно на 'ты'. "
            "Используй иронию, подмечай типичные темы и странности переписки, "
            "но не переходи на оскорбления и не раскрывай ничего личного. "
            "Ответ 2–4 предложения."
        )
        msgs = day_messages or []
        if not msgs:
            user_prompt = (
                f"Сегодня {weekday_name}, время {time_str}. "
                "В чате практически никто ничего не писал. "
                "Сделай короткий саркастический комментарий про 'мёртвый чат' и молчание Максима."
            )
        else:
            # соберём краткий лог
            snippets: list[str] = []
            for m in msgs[-40:]:  # ограничим объём
                uname = m.get("user_name") or f"id{m.get('user_id')}"
                txt = m.get("text", "")
                txt = txt.replace("\n", " ")
                if len(txt) > 80:
                    txt = txt[:77] + "..."
                snippets.append(f"{uname}: {txt}")
            joined = "\n".join(snippets)
            user_prompt = (
                f"Сегодня {weekday_name}, время {time_str}. Вот краткий лог сообщений за день:\n"
                f"{joined}\n\n"
                "Сделай общую саркастическую выжимку: чем занимался чат, как вёл себя Максим, "
                "на что это всё похоже. Не цитируй сообщения дословно, говори обобщённо."
            )
        return await call_openai(system_prompt, user_prompt, max_tokens=200, temperature=0.9)

    # ---- Сравнение погоды Брисбен vs Калуга ----
    if kind == "weather_comparison":
        system_prompt = (
            "Ты бот-друг Максима. "
            "Твоя задача — сравнить погоду в двух городах с лёгким юмором. "
            "Пиши по-русски, 1–3 предложения. "
            "Можно слегка подшутить над тем, где лучше жить, но без политических тем и грубостей."
        )
        user_prompt = (
            f"Сейчас {weekday_name}, время {time_str}. Вот краткое описание погоды:\n"
            f"{comparison_text}\n\n"
            "Сделай короткий забавный комментарий, сравнивая два города по погоде."
        )
        return await call_openai(system_prompt, user_prompt, max_tokens=120, temperature=0.8)

    # ---- Спокойной ночи в 21:00 ----
    if kind == "good_night":
        system_prompt = (
            "Ты бот-друг Максима. "
            "В 9 вечера ты желаешь ему спокойной ночи и приятных снов. "
            "Пишешь по-русски, на 'ты', 1–2 предложения. "
            "Тон тёплый, с лёгким юмором или мягким сарказмом, но без жёстких подколов. "
            "Можно намекнуть, что завтра снова вставать и страдать, но спать всё равно надо."
        )
        user_prompt = (
            f"Сегодня {weekday_name}, время {time_str}. "
            "Сделай короткое пожелание спокойной ночи и приятных снов Максиму."
        )
        return await call_openai(system_prompt, user_prompt, max_tokens=80, temperature=0.8)

    return None, "Unknown message kind"


# ---------- COMMAND HANDLERS ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    if chat_type == "private":
        await update.message.reply_text(
            "Привет! Я Друг Максима 🤖\n"
            "В группе я буду:\n"
            "• По будням в 7:00 желать Максиму доброго утра и хорошего рабочего дня (с погодой).\n"
            "• По выходным писать ему примерно раз в 3 часа в случайное время (тоже с погодой).\n"
            "• В 20:30 делать саркастический обзор дня.\n"
            "• В 21:00 желать спокойной ночи.\n"
            "Ночью с 22:00 до 7:00 я молчу 😴"
        )
    else:
        await update.message.reply_text(
            "Я здесь, чтобы поддерживать и слегка подшучивать над Максимом:\n"
            "• Будни: утреннее сообщение в 7:00 с погодой.\n"
            "• Выходные: раз в 3 часа, в случайную минуту, тоже с погодой.\n"
            "• Каждый день в 20:30 — обзор переписки.\n"
            "• Каждый день в 21:00 — пожелание спокойной ночи.\n"
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

    # Только целевой чат
    if GROUP_CHAT_ID and int(GROUP_CHAT_ID) != chat_id:
        return

    tz = get_tz()
    now = datetime.now(tz)

    # Логируем сообщение для вечернего обзора
    bot_data = context.application.bot_data
    msgs = bot_data.setdefault("daily_messages", [])
    msgs.append(
        {
            "date": now.date().isoformat(),
            "timestamp": now.isoformat(),
            "user_id": user_id,
            "user_name": user.username or user.full_name,
            "text": text,
        }
    )
    # ограничим размер
    if len(msgs) > 500:
        del msgs[0:len(msgs) - 500]

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

    # Сообщения Сергея — только если упомянут Максим
    if SUPPORT_USER_ID and user_id == SUPPORT_USER_ID:
        lower = text.lower()
        if "максим" in lower or "максим " in lower:
            ai_text, err = await generate_message_for_kind(
                "support_for_maxim", now=now, user_text=text
            )
            if ai_text is None:
                fallback = "Максим, видишь — тебя поддерживают не просто так."
                print(f"OpenAI error for support_for_maxim: {err}")
                await message.chat.send_message(fallback)
                return

            await message.chat.send_message(ai_text)
        return

    # Остальные пользователи — бот молчит
    return


# ---------- SCHEDULED JOBS ----------

async def weekend_three_hour_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Запускается каждую минуту.
    По выходным раз в 3 часа выбирает случайную минуту и в неё шлёт сообщение Максиму.
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
    current_block = now.hour // 3  # 0..7, каждый блок = 3 часа
    last_block = data.get("last_block")
    target_minute = data.get("target_minute")
    sent_this_block = data.get("sent_this_block", False)

    # Новый 3-часовой блок — планируем новую случайную минуту
    if last_block is None or current_block != last_block:
        target_minute = random.randint(0, 59)
        sent_this_block = False
        data["last_block"] = current_block
        data["target_minute"] = target_minute
        data["sent_this_block"] = sent_this_block
        print(f"[Weekend scheduler] New block {current_block}, planned minute {target_minute}")

    # Если ещё не отправляли в этом блоке и наступила нужная минута — шлём
    if not sent_this_block and now.minute == target_minute:
        weather = await fetch_weather_summary(BRISBANE)
        text, err = await generate_message_for_kind(
            "weekend_regular", now=now, weather_summary=weather
        )
        if text is None:
            text = "Максим, как у тебя дела? Погоду я не знаю, но подозреваю, что она махнула рукой и пошла пить кофе."
            print(f"OpenAI error for weekend_regular: {err}")

        try:
            await context.bot.send_message(
                chat_id=int(GROUP_CHAT_ID),
                text=text,
            )
            data["sent_this_block"] = True
            print(f"[Weekend scheduler] Sent 3-hour message at {now}")
        except Exception as e:
            print("Error sending weekend regular message:", e)

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
        # На всякий случай: по выходным не нужно
        return

    weather = await fetch_weather_summary(BRISBANE)
    text, err = await generate_message_for_kind(
        "weekday_morning", now=now, weather_summary=weather
    )
    if text is None:
        text = "Доброе утро, Максим! Про погоду я не в курсе, но работать всё равно придётся. 😉"
        print(f"OpenAI error for weekday_morning: {err}")

    try:
        await context.bot.send_message(
            chat_id=int(GROUP_CHAT_ID),
            text=text,
        )
        print(f"[Weekday morning] Sent morning message at {now}")
    except Exception as e:
        print("Error sending weekday morning message:", e)


async def evening_summary_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Саркастический обзор дня в 20:30.
    """
    if not GROUP_CHAT_ID:
        return

    tz = get_tz()
    now = datetime.now(tz)

    bot_data = context.application.bot_data
    msgs = bot_data.get("daily_messages", [])
    today = now.date().isoformat()
    todays_msgs = [m for m in msgs if m.get("date") == today]

    text, err = await generate_message_for_kind(
        "evening_summary", now=now, day_messages=todays_msgs
    )
    if text is None:
        text = "Итоги дня: все что-то писали, но в историю это точно не войдёт."
        print(f"OpenAI error for evening_summary: {err}")

    try:
        await context.bot.send_message(
            chat_id=int(GROUP_CHAT_ID),
            text=text,
        )
        print(f"[Evening summary] Sent summary at {now}")
    except Exception as e:
        print("Error sending evening summary message:", e)

    # очищаем сообщения за сегодняшний день
    bot_data["daily_messages"] = [m for m in msgs if m.get("date") != today]


async def daily_weather_comparison_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Раз в день сравниваем погоду в Брисбене и Калуге.
    """
    if not GROUP_CHAT_ID:
        return

    tz = get_tz()
    now = datetime.now(tz)

    # Ночью не шутим про погоду
    if is_night_time(now):
        return

    w_bne = await fetch_weather_summary(BRISBANE)
    w_kal = await fetch_weather_summary(KALUGA)

    if not w_bne and not w_kal:
        print("Weather comparison skipped: no data for both cities")
        return

    comparison_lines = []
    if w_bne:
        comparison_lines.append(w_bne)
    if w_kal:
        comparison_lines.append(w_kal)

    comp_text = "\n".join(comparison_lines)

    text, err = await generate_message_for_kind(
        "weather_comparison", now=now, comparison_text=comp_text
    )
    if text is None:
        text = "Сравнил погоду в Брисбене и Калуге и решил, что Максиму лучше не знать подробностей."
        print(f"OpenAI error for weather_comparison: {err}")

    try:
        await context.bot.send_message(
            chat_id=int(GROUP_CHAT_ID),
            text=text,
        )
        print(f"[Weather comparison] Sent comparison at {now}")
    except Exception as e:
        print("Error sending weather comparison message:", e)


async def good_night_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Пожелание спокойной ночи в 21:00 каждый день.
    """
    if not GROUP_CHAT_ID:
        return

    tz = get_tz()
    now = datetime.now(tz)

    # На всякий случай: если вдруг время съехало в ночь — не шлём
    if is_night_time(now):
        return

    text, err = await generate_message_for_kind(
        "good_night", now=now
    )
    if text is None:
        text = "Спокойной ночи, Максим. Завтра снова рабочий день, так что давай хотя бы притворимся, что ты выспишься. 😴"
        print(f"OpenAI error for good_night: {err}")

    try:
        await context.bot.send_message(
            chat_id=int(GROUP_CHAT_ID),
            text=text,
        )
        print(f"[Good night] Sent good night message at {now}")
    except Exception as e:
        print("Error sending good night message:", e)


# ---------- MAIN APP ----------

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment variables!")

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
        "Scheduling weekday morning, weekend 3-hour messages, evening summary, good night and weather comparison."
    )

    # 1) Будние утренние сообщения в 7:00 (пн–пт)
    job_queue.run_daily(
        weekday_morning_job,
        time=time(7, 0, tzinfo=tz),
        days=(0, 1, 2, 3, 4),
        name="weekday_morning_job",
    )

    # 2) Выходные: джоба раз в минуту, логика 3 часов внутри
    job_queue.run_repeating(
        weekend_three_hour_job,
        interval=60,          # каждую минуту
        first=0,              # сразу
        name="weekend_three_hour_job",
        data={},
    )

    # 3) Вечерний обзор в 20:30 каждый день
    job_queue.run_daily(
        evening_summary_job,
        time=time(20, 30, tzinfo=tz),
        days=(0, 1, 2, 3, 4, 5, 6),
        name="evening_summary_job",
    )

    # 4) Сравнение погоды в 12:00 каждый день
    job_queue.run_daily(
        daily_weather_comparison_job,
        time=time(12, 0, tzinfo=tz),
        days=(0, 1, 2, 3, 4, 5, 6),
        name="daily_weather_comparison_job",
    )

    # 5) Спокойной ночи в 21:00 каждый день
    job_queue.run_daily(
        good_night_job,
        time=time(21, 0, tzinfo=tz),
        days=(0, 1, 2, 3, 4, 5, 6),
        name="good_night_job",
    )

    print("Bot started and jobs scheduled...")
    app.run_polling()


if __name__ == "__main__":
    main()