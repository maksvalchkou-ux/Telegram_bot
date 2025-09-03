import os
import random
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, User, Poll
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes, PollHandler
)

# ========= НАСТРОЙКИ =========
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Render даёт публичный URL в переменной окружения
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL")
if not PUBLIC_URL:
    # Можно также прописать PUBLIC_URL вручную в Environment, если что
    raise RuntimeError("RENDER_EXTERNAL_URL (или PUBLIC_URL) is not set")

PORT = int(os.getenv("PORT", 5000))

# >>> ДЛЯ ТЕСТА: 10 секунд. Потом верните на hours=1 <<<
NICK_COOLDOWN = timedelta(seconds=10)

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
    "• Лимит для инициатора: сейчас 10 сек (для теста), антиповторы\n\n"
    "Дальше добавим репутацию, 8ball, триггеры, статистику и ачивки."
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

# ========= ПАМЯТЬ =========
NICKS: Dict[int, Dict[int, str]] = {}
TAKEN_NICKS: Dict[int, set] = {}
LAST_NICK_ACTION: Dict[int, datetime] = {}
KNOWN_USERS: Dict[str, int] = {}
ADMIN_NICK_POLLS: Dict[str, Tuple[int, int, str, str]] = {}
POLL_MSG_ID: Dict[str, int] = {}

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
SPICY = [
    "подозрительный тип","хитрожоп","задорный бузотёр","порочный джентльмен","дворовый князь",
    "барон с понтами","сомнительный эксперт","самурай-недоучка","киборг на минималках",
    "пират без лицензии","клоун-пофигист","барсук-бродяга"
]

# ========= УТИЛИТЫ =========
def _user_key(u: User) -> str:
    return f"@{u.username}" if u.username else (u.full_name or f"id{u.id}")

async def _is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator", "owner")
    except Exception:
        return False

def _cooldown_ok(initiator_id: int) -> Optional[str]:
    now = datetime.utcnow()
    last = LAST_NICK_ACTION.get(initiator_id)
    if last and now - last < NICK_COOLDOWN:
        left = (last + NICK_COOLDOWN) - now
        secs = int(left.total_seconds())
        if NICK_COOLDOWN < timedelta(minutes=1):
            return f"подожди ещё ~{secs} сек."
        mins = max(1, secs // 60)
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
    _ensure_chat_maps(chat_id)
    taken = TAKEN_NICKS[chat_id]
    for _ in range(50):
        parts = []
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
    return f"{random.choice(ADJ)} {random.choice(NOUN)} {random.choice(TAILS)}"

def _set_nick(chat_id: int, user_id: int, nick: str):
    _ensure_chat_maps(chat_id)
    prev = NICKS[chat_id].get(user_id)
    if prev and prev in TAKEN_NICKS[chat_id]:
        TAKEN_NICKS[chat_id].discard(prev)
    NICKS[chat_id][user_id] = nick
    TAKEN_NICKS[chat_id].add(nick)

def _resolve_target_user(update: Update) -> Optional[User]:
    if update.message and update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    return None

def _resolve_target_by_arg(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    if not context.args:
        return None
    arg = context.args[0]
    if arg.startswith("@"):
        return KNOWN_USERS.get(arg[1:].lower())
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

    cd = _cooldown_ok(initiator.id)
    if cd:
        await update.message.reply_text(f"Потерпи, {cd}")
        return

    target_user = _resolve_target_user(update)
    target_id: Optional[int] = None
    target_username: Optional[str] = None

    if target_user:
        target_id = target_user.id
        target_username = _user_key(target_user)
        await _update_known_user(target_user)
    else:
        by_arg_id = _resolve_target_by_arg(context)
        if by_arg_id:
            target_id = by_arg_id
            for uname, uid in KNOWN_USERS.items():
                if uid == by_arg_id:
                    target_username = f"@{uname}"
                    break

    if target_id is None:
        target_id = initiator.id
        target_username = _user_key(initiator)

    is_target_admin = await _is_admin(chat_id, target_id, context)
    if is_target_admin and target_id != initiator.id:
        _ensure_chat_maps(chat_id)
        prev = NICKS[chat_id].get(target_id)
        new_nick = _make_nick(chat_id, prev)

        poll_msg = await update.message.reply_poll(
            question=f"Меняем ник админу {target_username} на «{new_nick}»?",
            options=["Да", "Нет"],
            is_anonymous=False,
            open_period=120,  # Телеграм сам закроет через 2 минуты
        )

        ADMIN_NICK_POLLS[poll_msg.poll.id] = (chat_id, target_id, target_username, new_nick)
        POLL_MSG_ID[poll_msg.poll.id] = poll_msg.message_id

        # Резерв: если апдейт не придёт — сами закроем и подведём итог
        context.job_queue.run_once(
            close_admin_poll_job,
            when=125,
            data={"poll_id": poll_msg.poll.id, "chat_id": chat_id, "message_id": poll_msg.message_id},
            name=f"closepoll:{poll_msg.poll.id}",
        )

        _mark_cooldown(initiator.id)
        return

    _ensure_chat_maps(chat_id)
    prev = NICKS[chat_id].get(target_id)
    new_nick = _make_nick(chat_id, prev)
    _set_nick(chat_id, target_id, new_nick)
    _mark_cooldown(initiator.id)

    if target_id == initiator.id:
        await update.message.reply_text(f"Твой новый ник: «{new_nick}»")
    else:
        await update.message.reply_text(f"{target_username} теперь известен(а) как «{new_nick}»")

# ---- автособытие закрытого опроса (резерв) ----
async def on_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll: Poll = update.poll
    if poll.id not in ADMIN_NICK_POLLS:
        return
    if not poll.is_closed:
        return

    chat_id, target_id, target_username, pending_nick = ADMIN_NICK_POLLS.pop(poll.id)
    POLL_MSG_ID.pop(poll.id, None)

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

    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        pass

# ---- джоб: закрыть опрос руками и объявить результат ----
async def close_admin_poll_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    poll_id = data.get("poll_id")
    chat_id = data.get("chat_id")
    message_id = data.get("message_id") or POLL_MSG_ID.get(poll_id)

    closed_poll = None
    if message_id:
        try:
            closed_poll = await context.bot.stop_poll(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    info = ADMIN_NICK_POLLS.pop(poll_id, None)
    POLL_MSG_ID.pop(poll_id, None)
    if not info:
        return

    target_chat_id, target_id, target_username, pending_nick = info

    yes_votes = 0
    no_votes = 0
    if closed_poll:
        for opt in closed_poll.options:
            if opt.text == "Да":
                yes_votes = opt.voter_count
            elif opt.text == "Нет":
                no_votes = opt.voter_count

    passed = yes_votes > no_votes

    if passed:
        _ensure_chat_maps(target_chat_id)
        prev = NICKS[target_chat_id].get(target_id)
        if pending_nick != prev:
            _set_nick(target_chat_id, target_id, pending_nick)
        result = f"🎉 Голосование принято! {target_username} теперь «{pending_nick}»"
    else:
        result = f"❌ Голосование не прошло. Ник {target_username} остаётся без изменений."

    try:
        await context.bot.send_message(chat_id=target_chat_id, text=result)
    except Exception:
        pass

# ---- команда для админа: форс-закрыть активный опрос ----
async def cmd_pollclose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    me = update.effective_user

    if not await _is_admin(chat_id, me.id, context):
        await update.message.reply_text("Только для админов.")
        return

    poll_id = None
    info = None
    for pid, data in ADMIN_NICK_POLLS.items():
        if data[0] == chat_id:
            poll_id = pid
            info = data
            break

    if not poll_id or not info:
        await update.message.reply_text("Активных опросов не найдено.")
        return

    message_id = POLL_MSG_ID.get(poll_id)
    if not message_id:
        await update.message.reply_text("Не нашёл message_id опроса. Запусти новый.")
        return

    try:
        closed_poll = await context.bot.stop_poll(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        await update.message.reply_text(f"Не удалось закрыть опрос: {e}")
        return

    ADMIN_NICK_POLLS.pop(poll_id, None)
    POLL_MSG_ID.pop(poll_id, None)

    target_chat_id, target_id, target_username, pending_nick = info

    yes_votes = 0
    no_votes = 0
    if closed_poll:
        for opt in closed_poll.options:
            if opt.text == "Да":
                yes_votes = opt.voter_count
            elif opt.text == "Нет":
                no_votes = opt.voter_count

    if yes_votes > no_votes:
        _ensure_chat_maps(target_chat_id)
        prev = NICKS[target_chat_id].get(target_id)
        if pending_nick != prev:
            _set_nick(chat_id=target_chat_id, user_id=target_id, nick=pending_nick)
        result = f"🎉 Голосование принято! {target_username} теперь «{pending_nick}»"
    else:
        result = f"❌ Голосование не прошло. Ник {target_username} остаётся без изменений."

    await context.bot.send_message(chat_id=target_chat_id, text=result)

# ========= ENTRY =========
def main():
    app = Application.builder().token(API_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("nick", cmd_nick))
    app.add_handler(CommandHandler("pollclose", cmd_pollclose))

    # кнопки
    app.add_handler(CallbackQueryHandler(on_button))

    # события опросов
    app.add_handler(PollHandler(on_poll))

    # Запуск ВЕБХУКАМИ — ни одного getUpdates не будет
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=API_TOKEN,  # секретный путь
        webhook_url=f"{PUBLIC_URL}/{API_TOKEN}",
        allowed_updates=["message", "callback_query", "poll", "poll_answer"],
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
