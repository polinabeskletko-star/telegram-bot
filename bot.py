from telegram.ext import Application, CommandHandler, MessageHandler, filters

import os

TOKEN = os.environ.get("BOT_TOKEN")

async def start(update, context):
    await update.message.reply_text("Hello! Your bot is now running ??")

async def echo(update, context):
    user_text = update.message.text
    await update.message.reply_text(f"You said: {user_text}")

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    print("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
