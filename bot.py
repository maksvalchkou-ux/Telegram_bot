import os
import re
import random
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, Set, Tuple

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.request import HTTPXRequest  # таймауты для polling

# ========= НАСТРОЙКИ =========
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Кулдаун на генерацию ника инициатором (для теста 10 сек; потом можно на час)
NICK_COOLDOWN = timedelta(seconds=10)

# Антиспам для авто-триггеров (на чат)
TRIGGER_COOLDOWN = timedelta(seconds=20)

# ========= ТЕКСТЫ =========
WELCOME_TEXT = (
    "Привет! Я ваш ламповый бот для чата 🔥\n\n"
    "Жми кнопки ниже, чтобы посмотреть, что я умею, или открыть статистику и ачивки."
)

HELP_TEXT = (
    "🛠 Что умеет этот бот (v3: ники + триггеры + 8ball + репутация):\n"
    "• /start — меню с кнопками\n"
    "• /nick — сгенерировать ник себе\n"
    "• /nick @user или ответом — ник другу (включая админа)\n"
    "• /8ball вопрос — магический шар отвечает\n"
    "• Репутация: +1/-1 по реплаю или +1 @user / -1 @user\n"
    "  (самому себе +1 нельзя — получишь ачивку «Читер ёбаный»)\n"
    "• Автоответы на ключевые слова (работа, пиво, сон, зал, деньги, привет/пока, любовь)\n"
    "• Антиповторы и кулдауны\n\n"
    "Сoon: расширенная статистика и ачивки."
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

# ========= ПАМЯТЬ (in-memory) =========
# ники
NICKS: Dict[int, Dict[int, str]] = {}           # chat_id -> { user_id: nick }
TAKEN: Dict[int, Set[str]] = {}                 # chat_id -> set(nick)
LAST_NICK: Dict[int, datetime] = {}             # initiator_id -> last nick time
KNOWN: Dict[str, int] = {}                      # username_lower -> user_id

# триггеры
LAST_TRIGGER_TIME: Dict[int, datetime] = {}     # chat_id -> last trigger response time

# репутация
REP_GIVEN: Dict[int, int] = {}                  # user_id -> сколько выдал (+1/-1 суммарно)
REP_RECEIVED: Dict[int, int] = {}               # user_id -> сколько получил
REP_POS_GIVEN: Dict[int, int] = {}              # user_id -> выдано +1
REP_NEG_GIVEN: Dict[int, int] = {}              # user_id -> выдано -1

# счётчики
MSG_COUNT: Dict[int, int] = {}                  # user_id -> сообщений
CHAR_COUNT: Dict[int, int] = {}                 # user_id -> символов
NICK_CHANGE_COUNT: Dict[int, int] = {}          # user_id -> сколько раз меняли ник
EIGHTBALL_COUNT: Dict[int, int] = {}            # user_id -> вызовов 8ball
TRIGGER_HITS: Dict[int, int] = {}               # user_id -> сколько раз триггерил бот

# ачивки
ACHIEVEMENTS: Dict[int, Set[str]] = {}          # user_id -> set(title)

def _achieve(user_id: int, title: str) -> bool:
    s = ACHIEVEMENTS.setdefault(user_id, set())
    if title in s:
        return False
    s.add(title)
    return True

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

# ========= ТРИГГЕРЫ =========
TRIGGERS = [
    (re.compile(r"\bработ(а|ать|аю|аем|ает|ал|али|ать|у|ы|е|ой)\b", re.IGNORECASE),
     ["Работка подъехала? Держись, чемпион 🛠️",
      "Опять работать? Забирай +100 к терпению 💪",
      "Трудяга на связи. После — пивко заслужено 🍺"]),
    (re.compile(r"\bпив(о|ос|ко|анд|андос)\b", re.IGNORECASE),
     ["За холодненькое! 🍻","Пенная пауза — святое дело 🍺","Пивко — и всё наладится 😌"]),
    (re.compile(r"\bспат|сон|засып|дрыхн|высп(а|ы)", re.IGNORECASE),
     ["Не забудь поставить будильник ⏰","Сладких снов, котики 😴","Дрыхнуть — это тоже хобби."]),
    (re.compile(r"\bзал\b|\bкач|трен(ир|ер|ирую|ировка)", re.IGNORECASE),
     ["Железо не ждёт! 🏋️","Бицепс сам себя не накачает 💪","После зала — протеин и мемасы."]),
    (re.compile(r"\bденьг|бабк|зарплат|зп\b|\bкэш\b", re.IGNORECASE),
     ["Деньги — пыль. Но приятно, когда их много 💸","На шаурму хватит — жизнь удалась 😎","Финансовый поток уже в пути 🪄"]),
    (re.compile(r"\bприв(ет|ед)|здоро|здравст", re.IGNORECASE),
     ["Приветули 👋","Здарова, легенды!","Йо-йо, чат!"]),
    (re.compile(r"\bпок(а|ед)|до встреч|бб\b", re.IGNORECASE),
     ["Не пропадай 👋","До связи!","Ушёл красиво 🚪"]),
    (re.compile(r"\bлюбл(ю|юлю)|краш|сердечк|романтик", re.IGNORECASE),
     ["Любовь спасёт мир ❤️","Сердечки полетели 💘","Осторожно, милота выше нормы 🫶"]),
]

# ========= УТИЛИТЫ =========
def _user_key(u: User) -> str:
    return f"@{u.username}" if u.username else (u.full_name or f"id{u.id}")

def _ensure_chat(chat_id: int):
    if chat_id not in NICKS:
        NICKS[chat_id] = {}
    if chat_id not in TAKEN:
        TAKEN[chat_id] = set()

async def _save_known(u: User):
    if u and u.username:
        KNOWN[u.username.lower()] = u.id

def _cooldown_text(uid: int) -> Optional[str]:
    now = datetime.utcnow()
    last = LAST_NICK.get(uid)
    if last and now - last < NICK_COOLDOWN:
        left = int((last + NICK_COOLDOWN - now).total_seconds())
        return f"подожди ещё ~{left} сек."
    return None

def _mark_nick(uid: int):
    LAST_NICK[uid] = datetime.utcnow()

def _inc(d: Dict[int, int], key: int, by: int = 1):
    d[key] = d.get(key, 0) + by

def _make_nick(chat_id: int, prev: Optional[str]) -> str:
    _ensure_chat(chat_id)
    taken = TAKEN[chat_id]
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

def _apply_nick(chat_id: int, user_id: int, new_nick: str):
    _ensure_chat(chat_id)
    prev = NICKS[chat_id].get(user_id)
    if prev:
        TAKEN[chat_id].discard(prev)
    NICKS[chat_id][user_id] = new_nick
    TAKEN[chat_id].add(new_nick)
    _inc(NICK_CHANGE_COUNT, user_id)

def _resolve_reply_target(update: Update) -> Optional[User]:
    if update.message and update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    return None

def _resolve_arg_target(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    if not context.args:
        return None
    arg = context.args[0]
    if arg.startswith("@"):
        return KNOWN.get(arg[1:].lower())
    return None

# ========= КОМАНДЫ =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await _save_known(update.effective_user)
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
        text = build_stats_text(update.effective_chat.id)
        await q.message.reply_text(text, reply_markup=main_keyboard())
    elif data == BTN_ACH:
        await q.message.reply_text("🏅 Ачивки скоро подключим. Пока ловите «Читер ёбаный» за +1 себе 😈",
                                   reply_markup=main_keyboard())
    else:
        await q.message.reply_text("¯\\_(ツ)_/¯ Неизвестная кнопка", reply_markup=main_keyboard())

# ---- /nick ----
async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await _save_known(update.effective_user)

    chat_id = update.effective_chat.id
    initiator = update.effective_user

    cd = _cooldown_text(initiator.id)
    if cd:
        await update.message.reply_text(f"Потерпи, {cd}")
        return

    # цель: reply > @user > сам себе
    target_user = _resolve_reply_target(update)
    target_id: Optional[int] = None
    target_name: Optional[str] = None

    if target_user:
        target_id = target_user.id
        target_name = _user_key(target_user)
        await _save_known(target_user)
    else:
        by_arg = _resolve_arg_target(context)
        if by_arg:
            target_id = by_arg
            for uname, uid in KNOWN.items():
                if uid == by_arg:
                    target_name = f"@{uname}"
                    break

    if target_id is None:
        target_id = initiator.id
        target_name = _user_key(initiator)

    _ensure_chat(chat_id)
    prev = NICKS[chat_id].get(target_id)
    new_nick = _make_nick(chat_id, prev)
    _apply_nick(chat_id, target_id, new_nick)
    _mark_nick(initiator.id)

    if target_id == initiator.id:
        await update.message.reply_text(f"Твой новый ник: «{new_nick}»")
    else:
        await update.message.reply_text(f"{target_name} теперь известен(а) как «{new_nick}»")

# ---- /8ball ----
EIGHT_BALL = [
    "Да ✅","Нет ❌","Возможно 🤔","Скорее да, чем нет","Спроси позже 🕐","Сто процентов 💯",
    "Есть шанс, но не сегодня","Не рискуй","Лучше промолчу","Звёзды говорят «да» ✨",
    "Это судьба","Шансы малы","Точно нет","Легенды шепчут «да»","Не сейчас",
    "Абсолютно","М-да… такое себе","Ответ внутри тебя","Ха-ха, конечно!","Даже не думай"
]

async def cmd_8ball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    _inc(EIGHTBALL_COUNT, update.effective_user.id)
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Задай вопрос: `/8ball стоит ли идти за пивом?`", parse_mode="Markdown")
        return
    await update.message.reply_text(random.choice(EIGHT_BALL))

# ---- авто-триггеры + счётчики сообщений ----
def _trigger_allowed(chat_id: int) -> bool:
    now = datetime.utcnow()
    last = LAST_TRIGGER_TIME.get(chat_id)
    if last and now - last < TRIGGER_COOLDOWN:
        return False
    LAST_TRIGGER_TIME[chat_id] = now
    return True

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.from_user is None:
        return
    if msg.from_user.is_bot:
        return

    # счётчики
    uid = msg.from_user.id
    _inc(MSG_COUNT, uid)
    _inc(CHAR_COUNT, uid, by=len(msg.text or ""))

    # триггеры
    text = (msg.text or "").strip()
    if not text:
        return
    for pattern, answers in TRIGGERS:
        if pattern.search(text):
            if _trigger_allowed(update.effective_chat.id):
                await msg.reply_text(random.choice(answers))
                _inc(TRIGGER_HITS, uid)
            break

# ---- РЕПУТАЦИЯ (+1/-1) ----
REP_CMD = re.compile(r"^[\+\-]1(\b|$)", re.IGNORECASE)

def _resolve_target_from_text(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    # ожидаем +1 @user / -1 @user
    if not context.args:
        return None
    arg = context.args[0]
    if arg.startswith("@"):
        return KNOWN.get(arg[1:].lower())
    return None

async def rep_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Срабатывает на сообщения вида:
      - реплаем на сообщение пользователя и пишем: +1  или  -1
      - просто пишем: +1 @username  или  -1 @username
    """
    msg = update.message
    if not msg or msg.from_user is None:
        return
    text = (msg.text or "").strip()
    if not REP_CMD.match(text):
        return

    giver = msg.from_user
    is_plus = text.startswith("+")
    target_user: Optional[User] = None
    target_id: Optional[int] = None
    target_name: Optional[str] = None

    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_user = msg.reply_to_message.from_user
        target_id = target_user.id
        target_name = _user_key(target_user)
        await _save_known(target_user)
    else:
        # попробуем вытащить @username
        parts = text.split()
        if len(parts) >= 2 and parts[1].startswith("@"):
            uid = KNOWN.get(parts[1][1:].lower())
            if uid:
                target_id = uid
                target_name = parts[1]

    # если не нашли цель — пробуем через парсер аргументов (+1 @user)
    if target_id is None:
        uid = _resolve_target_from_text(context)
        if uid:
            target_id = uid
            for uname, u in KNOWN.items():
                if u == uid:
                    target_name = f"@{uname}"
                    break

    # если всё ещё нет цели — выходим
    if target_id is None:
        await msg.reply_text("Кому ставим репу? Ответь на сообщение или укажи @username.")
        return

    # запрет на +1 себе
    if is_plus and target_id == giver.id:
        if _achieve(giver.id, "Читер ёбаный"):
            await msg.reply_text("«Читер ёбаный» 🏅 — за попытку +1 себе. Нельзя!")
        else:
            await msg.reply_text("Нельзя +1 себе, хорош мухлевать 🐍")
        return

    # применяем репутацию
    delta = 1 if is_plus else -1
    _inc(REP_RECEIVED, target_id, by=delta)
    _inc(REP_GIVEN, giver.id, by=delta)
    if delta > 0:
        _inc(REP_POS_GIVEN, giver.id)
    else:
        _inc(REP_NEG_GIVEN, giver.id)

    # ответ
    sign = "+" if delta > 0 else "-"
    total = REP_RECEIVED.get(target_id, 0)
    await msg.reply_text(f"{target_name or 'Пользователь'} получает {sign}1. Текущая репа: {total}")

# ========= ERROR HANDLER =========
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    try:
        print("ERROR:", context.error)
    except Exception:
        pass

# ========= СТАТИСТИКА (текст) =========
def build_stats_text(chat_id: int) -> str:
    # топ по полученной репутации (3 места)
    top = sorted(REP_RECEIVED.items(), key=lambda x: x[1], reverse=True)[:3]
    top_lines = []
    for uid, score in top:
        name = f"id{uid}"
        # попробуем найти ник или @username
        nick = None
        if chat_id in NICKS and uid in NICKS[chat_id]:
            nick = NICKS[chat_id][uid]
        if nick:
            name = f"{name} ({nick})"
        top_lines.append(f"• {name}: {score}")

    if not top_lines:
        top_lines = ["• пока пусто"]

    # текущие ники
    nick_lines = []
    for uid, nick in NICKS.get(chat_id, {}).items():
        nick_lines.append(f"• id{uid}: {nick}")
    if not nick_lines:
        nick_lines = ["• пока никому не присвоено"]

    # активность
    # возьмём топ по сообщениям (3 места)
    top_msg = sorted(MSG_COUNT.items(), key=lambda x: x[1], reverse=True)[:3]
    msg_lines = [f"• id{uid}: {cnt} смс / {CHAR_COUNT.get(uid,0)} симв." for uid, cnt in top_msg] or ["• пока пусто"]

    return (
        "📊 Статистика (черновик)\n\n"
        "🏆 Топ по репутации (полученной):\n" + "\n".join(top_lines) + "\n\n"
        "📝 Текущие ники:\n" + "\n".join(nick_lines) + "\n\n"
        "⌨️ Активность:\n" + "\n".join(msg_lines)
    )

# ========= HEALTH-СЕРВЕР ДЛЯ RENDER =========
app = Flask(__name__)
@app.get("/")
def health():
    return "Bot is running!"

def run_flask():
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# ========= ENTRY =========
async def _pre_init(app: Application):
    # На всякий: снести webhook перед polling, чтобы не было конфликтов
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

def main():
    # Render любит, когда порт слушается — поднимем health-сервер
    threading.Thread(target=run_flask, daemon=True).start()

    # Укороченные таймауты для getUpdates (меньше конфликтов на free-инстансе)
    req = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=25.0,
        pool_timeout=5.0,
    )

    application: Application = (
        ApplicationBuilder()
        .token(API_TOKEN)
        .get_updates_request(req)
        .post_init(_pre_init)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("nick", cmd_nick))
    application.add_handler(CommandHandler("8ball", cmd_8ball))

    # Репутация (+1/-1) — через общий MessageHandler, чтоб работало и по реплаю, и с @username
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, rep_handler))

    # Кнопки
    application.add_handler(CallbackQueryHandler(on_button))

    # Сообщения (триггеры и счётчики) — после репы, чтобы не перехватывать команды
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # Ошибки
    application.add_error_handler(on_error)

    # Запуск polling
    from telegram import Update as TgUpdate
    application.run_polling(
        allowed_updates=TgUpdate.ALL_TYPES,
        timeout=25,
        poll_interval=1.0,
        drop_pending_updates=True,
        close_loop=False,
    )

if __name__ == "__main__":
    main()
