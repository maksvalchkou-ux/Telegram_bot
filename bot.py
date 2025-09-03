import os
import re
import random
import threading
from flask import Flask

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# Токен берём из переменных окружения Render (Environment → BOT_TOKEN)
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# ---------- ДАННЫЕ ----------
answers = [
    "Да ✅", "Нет ❌", "Возможно 🤔", "Лучше не надо 😅",
    "Сто процентов 💯", "Спроси позже ⏳", "Я бы не рисковал 🚫"
]

nicknames = [
    "Сырный Барон 🧀", "Колбасный Лорд 🌭", "Котяра 3000 🐱",
    "Маг Подтяжек 🧙", "Эльф Ларька 🧝", "Орёл-обосрал 🦅"
]

reputation = {}  # простая память в RAM; для 3 друзей ок

reactions = {
    "пиво": "А где моё?! 🍺",
    "работа": "Фу, не матерись! 🤢",
    "утро": "Утро бывает только после обеда. 🌞"
}

# ---------- ХЭНДЛЕРЫ ----------
async def cmd_8ball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(random.choice(answers))

async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой новый ник: {random.choice(nicknames)}")

_plus_minus_re = re.compile(r"(?P<sign>\+1|-1)\s+(?P<user>@?\S+)")

async def plus_minus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    m = _plus_minus_re.search(text)
    if not m:
        return
    user = m.group("user")
    delta = 1 if m.group("sign") == "+1" else -1
    reputation[user] = reputation.get(user, 0) + delta
    await update.message.reply_text(f"{user} теперь имеет {reputation[user]} очков репутации!")

async def word_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower()
    for word, reply in reactions.items():
        if word in text:
            await update.message.reply_text(reply)
            break

# ---------- FLASK для Render (healthcheck) ----------
app = Flask(__name__)

@app.get("/")
def health():
    return "Bot is running!"

def run_flask():
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# ---------- MAIN ----------
def main():
    # Flask — в отдельном потоке (Render ждёт, что сервис слушает PORT)
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(API_TOKEN).build()

    application.add_handler(CommandHandler("8ball", cmd_8ball))
    application.add_handler(CommandHandler("nick", cmd_nick))
    # +1 / -1
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(_plus_minus_re), plus_minus))
    # реакции на слова
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, word_reactions))

    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
