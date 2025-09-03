import os
import threading
from flask import Flask

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ========= НАСТРОЙКИ =========
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# ========= ТЕКСТЫ =========
WELCOME_TEXT = (
    "Привет! Я ваш ламповый бот для чата 🔥\n\n"
    "Жми кнопки ниже, чтобы посмотреть, что я умею, или открыть статистику и ачивки."
)

HELP_TEXT = (
    "🛠 Что умеет этот бот (v1 — каркас):\n"
    "• /start — показать меню с кнопками\n"
    "• Кнопка «Что умеет этот бот» — эта памятка\n"
    "• Кнопка «Статистика» — сводка по участникам (пока заглушка)\n"
    "• Кнопка «Ачивки» — список достижений (пока заглушка)\n\n"
    "⚙️ В следующих версиях появятся:\n"
    "• Никнеймы (/nick и /nick @user) с антиспамом и голосованием по админам\n"
    "• Репутация (+1/-1) с античитом\n"
    "• Магический шар (/8ball) с большим пулом ответов\n"
    "• Реакции на слова (триггеры)\n"
    "• Полная статистика и ачивки\n"
)

STATS_PLACEHOLDER = (
    "📊 Статистика (заглушка v1):\n"
    "• Топ репутации: скоро\n"
    "• Текущие ники: скоро\n"
    "• Сообщения/символы: скоро\n"
    "• Ачивки: скоро\n\n"
    "Прокачаем это на следующих шагах 😉"
)

ACHIEVEMENTS_PLACEHOLDER = (
    "🏅 Список ачивок (заглушка v1):\n"
    "Мы готовим вкусные достижения с перчинкой 😈\n"
    "Скоро здесь будет полный перечень с условиями получения."
)

# ========= КНОПКИ =========
BTN_HELP = "help_info"
BTN_STATS = "stats_open"
BTN_ACH = "ach_list"


def main_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🧰 Что умеет этот бот", callback_data=BTN_HELP),
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data=BTN_STATS),
            InlineKeyboardButton("🏅 Ачивки", callback_data=BTN_ACH),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ========= ХЭНДЛЕРЫ =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(WELCOME_TEXT, reply_markup=main_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(HELP_TEXT, reply_markup=main_keyboard())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data if q else ""
    if not q:
        return
    await q.answer()

    if data == BTN_HELP:
        await q.message.reply_text(HELP_TEXT, reply_markup=main_keyboard())
    elif data == BTN_STATS:
        await q.message.reply_text(STATS_PLACEHOLDER, reply_markup=main_keyboard())
    elif data == BTN_ACH:
        await q.message.reply_text(ACHIEVEMENTS_PLACEHOLDER, reply_markup=main_keyboard())
    else:
        await q.message.reply_text("¯\\_(ツ)_/¯ Неизвестная кнопка", reply_markup=main_keyboard())


# ========= FLASK для Render (healthcheck) =========
app = Flask(__name__)

@app.get("/")
def health():
    return "Bot is running!"

def run_flask():
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


# ========= ENTRY =========
def main():
    threading.Thread(target=run_flask, daemon=True).start()
    application = Application.builder().token(API_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CallbackQueryHandler(on_button))
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
