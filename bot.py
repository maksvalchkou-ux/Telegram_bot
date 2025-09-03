import os
import random
import asyncio
import threading
from aiogram import Bot, Dispatcher, types
from flask import Flask

# Токен из переменных окружения (Render → Environment → BOT_TOKEN)
API_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# --- 8ball ответы ---
answers = [
    "Да ✅", "Нет ❌", "Возможно 🤔", "Лучше не надо 😅",
    "Сто процентов 💯", "Спроси позже ⏳", "Я бы не рисковал 🚫"
]

# --- Никнеймы ---
nicknames = [
    "Сырный Барон 🧀", "Колбасный Лорд 🌭", "Котяра 3000 🐱",
    "Маг Подтяжек 🧙", "Эльф Ларька 🧝", "Орёл-обосрал 🦅"
]

# --- Репутация ---
reputation = {}

# --- Реакции на слова ---
reactions = {
    "пиво": "А где моё?! 🍺",
    "работа": "Фу, не матерись! 🤢",
    "утро": "Утро бывает только после обеда. 🌞"
}

# 🎱 Magic 8ball
@dp.message_handler(commands=["8ball"])
async def magic8ball(message: types.Message):
    await message.reply(random.choice(answers))

# 🌀 Генератор никнеймов
@dp.message_handler(commands=["nick"])
async def nickname(message: types.Message):
    await message.reply(f"Твой новый ник: {random.choice(nicknames)}")

# ⭐ Очки репутации
@dp.message_handler(lambda m: "+1" in m.text or "-1" in m.text)
async def reputation_handler(message: types.Message):
    words = message.text.split()
    if len(words) >= 2:
        user = words[1]
        change = 1 if "+1" in message.text else -1
        reputation[user] = reputation.get(user, 0) + change
        await message.reply(f"{user} теперь имеет {reputation[user]} очков репутации!")

# 🗣️ Реакции на слова
@dp.message_handler()
async def reactions_handler(message: types.Message):
    for word, reply in reactions.items():
        if word in message.text.lower():
            await message.reply(reply)
            break

# ---- Мини веб-сервер (Flask) для Render ----
app = Flask(__name__)

@app.get("/")
def healthcheck():
    return "Bot is running!"

def run_bot():
    # Отдельный event loop в отдельном потоке
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(dp.start_polling())

def main():
    # Запускаем бота в фоновом потоке
    threading.Thread(target=run_bot, daemon=True).start()
    # Поднимаем HTTP-сервер (Render требует слушать PORT)
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
