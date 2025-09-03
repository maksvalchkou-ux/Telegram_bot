import os
import time
import random
import threading
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional

from flask import Flask
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, User, Poll
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    PollHandler
)

# ========= НАСТРОЙКИ =========
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

NICK_COOLDOWN = timedelta(hours=1)  # лимит генерации на инициатора

# ========= ТЕКСТЫ =========
WELCOME_TEXT = (
    "Привет! Я ваш ламповый бот для чата 🔥\n\n"
    "Жми кнопки ниже, чтобы посмотреть, что я умею, или открыть статистику и ачивки."
)

HELP_TEXT = (
    "🛠 Что умеет этот бот (v1 + ники):\n"
    "• /start — меню с кнопками\n"
    "• /nick — сгенерировать ник себе\n"
    "• /nick @user или ответом — сгенерировать ник другу\n"
    "• Если цель — админ: запускаем голосование (2 минуты)\n"
    "• Лимит для инициатора: 1 раз в час, антиповторы\n\n"
    "Дальше добавим репутацию, 8ball, триггеры, статистику и ачивки."
)

STATS_PLACEHOLDER = (
    "📊 Статистика (заглушка v1):\n"
    "• Топ репутации: скоро\n"
    "• Текущие ники: показывать можем уже сейчас (в следующем шаге)\n"
    "• Сообщения/символы: скоро\n"
    "• Ачивки: скоро\n\n"
    "Прокачаем это на следующих шагах 😉"
)

ACHIEVEMENTS_PLACEHOLDER = (
    "🏅 Список ачивок (заглушка v1):\n"
    "Матёрые достижения уже в разработке 😈\n"
    "Скоро здесь будет полный перечень с условиями получения."
)

# ========= КНОПКИ =========
BTN_HELP = "help_info"
BTN_STATS = "stats_open"
BTN_ACH = "ach_list"

def main_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🧰 Что умеет этот бот", callback_data=BTN_HELP)],
        [
            InlineKeyboardButton("📊 Статистика", callback_data=BTN_STATS),
            InlineKeyboardButton("🏅 Ачивки", callback_data=BTN_ACH),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

# ========= ПАМЯТЬ (на время работы процесса) =========
# ники: chat_id -> { user_id: "ник" }
NICKS: Dict[int, Dict[int, str]] = {}
# занятые ники в чате: chat_id -> set(nick)
TAKEN_NICKS: Dict[int, set] = {}
# время последней генерации никнейма инициатором: initiator_user_id -> datetime
LAST_NICK_ACTION: Dict[int, datetime] = {}
# известные пользователи по username (чтобы можно было /nick @username): username_lower -> user_id
KNOWN_USERS: Dict[str, int] = {}
# активные голосования за ник админа: poll_id -> (chat_id, target_user_id, target_username, pending_nick)
ADMIN_NICK_POLLS: Dict[str, Tuple[int, int, str, str]] = {}

# ========= СЛОВАРИ ДЛЯ НИКОВ =========
ADJ = [
    "шальной","хрустящий","лысый","бурлящий","ламповый","коварный","бархатный","дерзкий","мягкотелый",
    "стальной","сонный","бравый","хитрый","космический","солёный","дымный","пряный","бодрый","тёплый",
    "грозный","подозрительный","барский","весёлый","рандомный","великосветский"
]
NOUN = [
    "ёж","краб","барсук","жираф","карась","барон","пират","самурай","тракторист","клоун","волк","кот",
    "кабан","медведь","сова","дракондон","гусь","козырь","джентльмен","шаман","киборг","арбуз","колобок",
    "профессор","червяк"
]
TAILS = [
    "из подъезда №3","с приветом","на максималках","XL","в тапках","из будущего","при бабочке","deluxe",
    "edition 2.0","без тормозов","официально","с огоньком","в отставке","на бобине","turbo","™️",
    "prime","на районе","с сюрпризом","VIP"
]
EMOJIS = ["🦔","🦀","🦊","🐻","🐺","🐗","🐱","🦉","🐟","🦆","🦄","🐲","🥒","🍉","🧀","🍔","🍺","☕️","🔥","💣","✨","🛠️","👑","🛸"]

# «перчинка», умеренно токсично, подмешиваем с вероятностью ~25%
SPICY = [
    "подозрительный тип","хитрожоп","задорный бузотёр","порочный джентльмен","дворовый князь",
    "барон с понтами","сомнительный эксперт","самурай-недоучка","киборг на минималках",
    "пират без лицензии","клоун-пофигист","барсук-бродяга"
]

def _user_key(u: User) -> str:
    return f"@{u.username}" if u.username else (u.full_name or f"id{u.id}")

async def _is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator","creator","owner")
    except Exception:
        return False

def _cooldown_ok(initiator_id: int) -> Optional[str]:
    """Проверяем лимит 1/час. Возвращаем None, если всё ок, иначе строку с временем ожидания."""
    now = datetime.utcnow()
    last = LAST_NICK_ACTION.get(initiator_id)
    if last and now - last < NICK_COOLDOWN:
        left = (last + NICK_COOLDOWN) - now
        mins = int(left.total_seconds() // 60) + 1
        return f"подожди ещё ~{mins} мин."
    return None

def _mark_cooldown(initiator_id: int):
    LAST_NICK_ACTION[initiator_id] = datetime.utcnow()

def _ensure_chat_maps(chat_id: int):
    if chat_id not in NICKS:
        NICKS[chat_id] = {}
    if chat_id not in TAKEN_NICKS:
        TAKEN_NICKS[chat_id] = set()

def _make_nick(chat_id: int, prev: Optional[str]) -> str:
    """Собираем ник из частей, избегаем прямого повтора prev и стараемся не дублировать в чате."""
    _ensure_chat_maps(chat_id)
    taken = TAKEN_NICKS[chat_id]

    for _ in range(50):  # до 50 попыток найти не занятый и не равный prev
        parts = []
        # ~25% шанс подмешать «перчинку» вместо прилагательного
        if random.random() < 0.25:
            parts.append(random.choice(SPICY))
        else:
            parts.append(random.choice(ADJ))
        parts.append(random.choice(NOUN))
        if random.random() < 0.85:
            parts.append(random.choice(TAILS))
        nick = " ".join(parts)
        if random.random() < 0.7:
            nick += " " + random.choice(EMOJIS)

        if prev and nick == prev:
            continue
        if nick in taken:
            continue
        return nick

    # если не нашли — возвращаем хоть что-то
    return f"{random.choice(ADJ)} {random.choice(NOUN)} {random.choice(TAILS)}"

def _set_nick(chat_id: int, user_id: int, nick: str):
    _ensure_chat_maps(chat_id)
    # освободим прежний ник из занятых
    prev = NICKS[chat_id].get(user_id)
    if prev and prev in TAKEN_NICKS[chat_id]:
        TAKEN_NICKS[chat_id].discard(prev)
    # ставим новый
    NICKS[chat_id][user_id] = nick
    TAKEN_NICKS[chat_id].add(nick)

def _resolve_target_user(update: Update) -> Optional[User]:
    """Цель по reply приоритете — самый надёжный способ."""
    if update.message and update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    return None

def _resolve_target_by_arg(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """Попытка из аргумента @username найти user_id по словарю KNOWN_USERS."""
    if not context.args:
        return None
    arg = context.args[0]
    if arg.startswith("@"):
        uid = KNOWN_USERS.get(arg[1:].lower())
        return uid
    return None

async def _update_known_user(user: User):
    if user and user.username:
        KNOWN_USERS[user.username.lower()] = user.id

# ========= КОМАНДЫ И КНОПКИ =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await _update_known_user(update.effective_user)
        await update.message.reply_text(WELCOME_TEXT, reply_markup=main_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(HELP_TEXT, reply_markup=main_keyboard())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data
    if data == BTN_HELP:
        await q.message.reply_text(HELP_TEXT, reply_markup=main_keyboard())
    elif data == BTN_STATS:
        # позже сюда подставим реальную статистику
        await q.message.reply_text(STATS_PLACEHOLDER, reply_markup=main_keyboard())
    elif data == BTN_ACH:
        await q.message.reply_text(ACHIEVEMENTS_PLACEHOLDER, reply_markup=main_keyboard())
    else:
        await q.message.reply_text("¯\\_(ツ)_/¯ Неизвестная кнопка", reply_markup=main_keyboard())

# ---- /nick ----
async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await _update_known_user(update.effective_user)

    chat_id = update.effective_chat.id
    initiator = update.effective_user

    # лимит для инициатора
    cd = _cooldown_ok(initiator.id)
    if cd:
        await update.message.reply_text(f"Потерпи, {cd}")
        return

    # определяем цель
    target_user: Optional[User] = _resolve_target_user(update)
    target_id: Optional[int] = None
    target_username = None

    if target_user:
        target_id = target_user.id
        target_username = _user_key(target_user)
        await _update_known_user(target_user)
    else:
        # попытка по аргументу @username
        by_arg_id = _resolve_target_by_arg(context)
        if by_arg_id:
            target_id = by_arg_id
            # username отображения
            for uname, uid in KNOWN_USERS.items():
                if uid == by_arg_id:
                    target_username = f"@{uname}"
                    break

    # если цель не определена — считаем, что сам себе
    if target_id is None:
        target_id = initiator.id
        target_username = _user_key(initiator)

    # голосование, если цель — админ и это не сам себе
    is_target_admin = await _is_admin(chat_id, target_id, context)
    if is_target_admin and target_id != initiator.id:
        _ensure_chat_maps(chat_id)
        prev = NICKS[chat_id].get(target_id)
        new_nick = _make_nick(chat_id, prev)

        poll_msg = await update.message.reply_poll(
    question=f"Меняем ник админу {target_username} на «{new_nick}»?",
    options=["Да", "Нет"],
    is_anonymous=False,
)
# Сохраняем контекст
ADMIN_NICK_POLLS[poll_msg.poll.id] = (chat_id, target_id, target_username, new_nick)

# Ставим таймер на 120 секунд: сами закроем опрос и подведём итоги
context.job_queue.run_once(
    close_admin_poll_job,
    when=120,
    data={
        "poll_id": poll_msg.poll.id,
        "chat_id": chat_id,
        "message_id": poll_msg.message_id,
    },
    name=f"closepoll:{poll_msg.poll.id}",
)

_mark_cooldown(initiator.id)
return

    # обычный случай: применяем ник сразу
    _ensure_chat_maps(chat_id)
    prev = NICKS[chat_id].get(target_id)
    new_nick = _make_nick(chat_id, prev)
    _set_nick(chat_id, target_id, new_nick)
    _mark_cooldown(initiator.id)

    if target_id == initiator.id:
        await update.message.reply_text(f"Твой новый ник: «{new_nick}»")
    else:
        await update.message.reply_text(f"{target_username} теперь известен(а) как «{new_nick}»")

# ---- обработка закрытия голосований ----
async def on_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll: Poll = update.poll
    if poll.id not in ADMIN_NICK_POLLS:
        return
    if not poll.is_closed:
        return

    chat_id, target_id, target_username, pending_nick = ADMIN_NICK_POLLS.pop(poll.id)
    # результаты
    yes_votes = 0
    no_votes = 0
    for opt in poll.options:
        if opt.text == "Да":
            yes_votes = opt.voter_count
        elif opt.text == "Нет":
            no_votes = opt.voter_count

    if yes_votes > no_votes:
        _ensure_chat_maps(chat_id)
        prev = NICKS[chat_id].get(target_id)
        if pending_nick != prev:
            _set_nick(chat_id, target_id, pending_nick)
        text = f"🎉 Голосование принято! {target_username} теперь «{pending_nick}»"
    else:
        text = f"❌ Голосование не прошло. Ник {target_username} остаётся без изменений."
    # отправим в чат
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        pass

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
    # веб-сервер для Render
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(API_TOKEN).build()

    # команды
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("nick", cmd_nick))

    # кнопки
    application.add_handler(CallbackQueryHandler(on_button))

    # голосования
    application.add_handler(PollHandler(on_poll))

    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
