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
from telegram.request import HTTPXRequest  # «тихий» polling с таймаутами

# ========= НАСТРОЙКИ =========
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Кулдаун на генерацию никнейма инициатором (для теста 10 сек; потом можно на час)
NICK_COOLDOWN = timedelta(seconds=10)
# Антиспам для авто-триггеров (на чат)
TRIGGER_COOLDOWN = timedelta(seconds=20)

# ========= ТЕКСТЫ =========
WELCOME_TEXT = (
    "Привет! Я ваш ламповый бот для чата 🔥\n\n"
    "Жми кнопки ниже, чтобы посмотреть, что я умею, или открыть статистику и ачивки."
)
HELP_TEXT = (
    "🛠 Что умеет этот бот (v4 — ачивки):\n"
    "• /start — меню с кнопками\n"
    "• /nick — ник себе; /nick @user или ответом — ник другу (включая админа)\n"
    "• /8ball вопрос — магический шар отвечает\n"
    "• Репутация: +1/-1 по реплаю или +1 @user / -1 @user\n"
    "  (самому себе +1 нельзя — ачивка «Читер ёбаный»)\n"
    "• Автоответы: пиво, работа, сон, зал, деньги, привет/пока, любовь\n"
    "• «📊 Статистика» — топ репы, текущие ники и активность\n"
    "• «🏅 Ачивки» — список и твои достижения\n"
)

STATS_TITLE = "📊 Статистика"

# ========= КНОПКИ =========
BTN_HELP  = "help_info"
BTN_STATS = "stats_open"
BTN_ACH   = "ach_list"

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧰 Что умеет этот бот", callback_data=BTN_HELP)],
        [
            InlineKeyboardButton("📊 Статистика", callback_data=BTN_STATS),
            InlineKeyboardButton("🏅 Ачивки",    callback_data=BTN_ACH),
        ],
    ])

# ========= ПАМЯТЬ (in-memory) =========
# — ники
NICKS: Dict[int, Dict[int, str]] = {}     # chat_id -> { user_id: nick }
TAKEN: Dict[int, Set[str]] = {}           # chat_id -> set(nick)
LAST_NICK: Dict[int, datetime] = {}       # initiator_id -> last nick time

# — известные @username и имена
KNOWN: Dict[str, int] = {}                # username_lower -> user_id
NAMES: Dict[int, str] = {}                # user_id -> last display name (@username > full_name)

# — триггеры
LAST_TRIGGER_TIME: Dict[int, datetime] = {}  # chat_id -> last trigger time

# — репутация
REP_GIVEN: Dict[int, int] = {}            # user_id -> суммарно выдал (+/-)
REP_RECEIVED: Dict[int, int] = {}         # user_id -> суммарно получил
REP_POS_GIVEN: Dict[int, int] = {}        # user_id -> выдано +1
REP_NEG_GIVEN: Dict[int, int] = {}        # user_id -> выдано -1

# — счётчики
MSG_COUNT: Dict[int, int] = {}            # user_id -> сообщений
CHAR_COUNT: Dict[int, int] = {}           # user_id -> символов
NICK_CHANGE_COUNT: Dict[int, int] = {}    # user_id -> сколько раз меняли ник
EIGHTBALL_COUNT: Dict[int, int] = {}      # user_id -> вызовов 8ball
TRIGGER_HITS: Dict[int, int] = {}         # user_id -> сколько раз триггерил бот
BEER_HITS: Dict[int, int] = {}            # user_id -> сколько раз словил «пиво»-триггер

# — ачивки
ACHIEVEMENTS: Dict[int, Set[str]] = {}    # user_id -> set(title)

def _achieve(user_id: int, title: str) -> bool:
    got = ACHIEVEMENTS.setdefault(user_id, set())
    if title in got:
        return False
    got.add(title)
    return True

# ========= АЧИВКИ (название -> (описание, условие-объяснение)) =========
ACH_LIST: Dict[str, Tuple[str, str]] = {
    "Читер ёбаный": ("попытка поставить +1 самому себе", "Сам себе +1 — нельзя."),
    "Никофил ебучий": ("5 смен никнейма", "Сменил ник ≥ 5 раз."),
    "Щедрый засранец": ("раздал +10 репутаций", "Выдано +1 ≥ 10 раз."),
    "Любимчик, сука": ("получил +20 репутации", "Получено репы ≥ 20."),
    "Пивной сомелье-алкаш": ("5 раз триггерил «пиво»", "Слово «пиво/пивко…» ≥ 5."),
    "Шароман долбанный": ("10 обращений к /8ball", "Вызовов /8ball ≥ 10."),
}

# Пороги
TH_NICKOFIL = 5
TH_GIVER    = 10
TH_LOVED    = 20
TH_BEER     = 5
TH_BALL     = 10

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
# индекс 1 — «пиво», чтобы отдельно считать BEER_HITS
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
def _display_name(u: User) -> str:
    if u.username:
        return f"@{u.username}"
    return u.full_name or f"id{u.id}"

async def _remember_user(u: Optional[User]):
    if not u:
        return
    if u.username:
        KNOWN[u.username.lower()] = u.id
        NAMES[u.id] = f"@{u.username}"
    else:
        NAMES[u.id] = u.full_name or f"id{u.id}"

def _ensure_chat(chat_id: int):
    if chat_id not in NICKS:
        NICKS[chat_id] = {}
    if chat_id not in TAKEN:
        TAKEN[chat_id] = set()

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
    for _ in range(80):
        parts = []
        parts.append(random.choice(SPICY) if random.random() < 0.25 else random.choice(ADJ))
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

async def _announce_achievement(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, title: str):
    name = NAMES.get(user_id, f"id{user_id}")
    desc = ACH_LIST.get(title, ("", ""))[0]
    try:
        await context.bot.send_message(chat_id, f"🏅 {name} получает ачивку: **{title}** — {desc}", parse_mode="Markdown")
    except Exception:
        pass

def _name_or_id(uid: int) -> str:
    return NAMES.get(uid, f"id{uid}")

# ========= КОМАНДЫ =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await _remember_user(update.effective_user)
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
        await q.message.reply_text(build_stats_text(update.effective_chat.id), reply_markup=main_keyboard())
    elif data == BTN_ACH:
        uid = q.from_user.id if q.from_user else None
        await q.message.reply_text(build_achievements_text(uid), reply_markup=main_keyboard(), parse_mode="Markdown")
    else:
        await q.message.reply_text("¯\\_(ツ)_/¯ Неизвестная кнопка", reply_markup=main_keyboard())

# ---- /nick ----
async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await _remember_user(update.effective_user)

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
        target_name = _display_name(target_user)
        await _remember_user(target_user)
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
        target_name = _display_name(initiator)

    _ensure_chat(chat_id)
    prev = NICKS[chat_id].get(target_id)
    new_nick = _make_nick(chat_id, prev)
    _apply_nick(chat_id, target_id, new_nick)
    _mark_nick(initiator.id)

    # ачивка «Никофил ебучий»
    if NICK_CHANGE_COUNT.get(target_id, 0) >= TH_NICKOFIL:
        if _achieve(target_id, "Никофил ебучий"):
            await _announce_achievement(context, chat_id, target_id, "Никофил ебучий")

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
    await _remember_user(update.effective_user)
    uid = update.effective_user.id
    _inc(EIGHTBALL_COUNT, uid)
    # ачивка «Шароман долбанный»
    if EIGHTBALL_COUNT.get(uid, 0) >= TH_BALL:
        if _achieve(uid, "Шароман долбанный"):
            await _announce_achievement(context, update.effective_chat.id, uid, "Шароман долбанный")

    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Задай вопрос: `/8ball стоит ли идти за пивом?`", parse_mode="Markdown")
        return
    await update.message.reply_text(random.choice(EIGHT_BALL))

# ---- ЕДИНЫЙ ОБРАБОТЧИК ТЕКСТА: счётчики → репутация → триггеры ----
REP_CMD = re.compile(r"^[\+\-]1(\b|$)", re.IGNORECASE)

def _trigger_allowed(chat_id: int) -> bool:
    now = datetime.utcnow()
    last = LAST_TRIGGER_TIME.get(chat_id)
    if last and now - last < TRIGGER_COOLDOWN:
        return False
    LAST_TRIGGER_TIME[chat_id] = now
    return True

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.from_user is None:
        return
    if msg.from_user.is_bot:
        return

    # запомним имя/@username
    await _remember_user(msg.from_user)

    # === 1) Счётчики активности ===
    uid = msg.from_user.id
    _inc(MSG_COUNT, uid)
    _inc(CHAR_COUNT, uid, by=len(msg.text or ""))

    text = (msg.text or "").strip()
    if not text:
        return

    # === 2) Репутация (+1/-1) ===
    if REP_CMD.match(text):
        is_plus = text.startswith("+")
        giver = msg.from_user
        target_user: Optional[User] = None
        target_id: Optional[int] = None
        target_name: Optional[str] = None

        if msg.reply_to_message and msg.reply_to_message.from_user:
            target_user = msg.reply_to_message.from_user
            target_id = target_user.id
            target_name = _display_name(target_user)
            await _remember_user(target_user)
        else:
            parts = text.split()
            if len(parts) >= 2 and parts[1].startswith("@"):
                uid2 = KNOWN.get(parts[1][1:].lower())
                if uid2:
                    target_id = uid2
                    target_name = parts[1]

        if target_id is None:
            await msg.reply_text("Кому ставим репу? Ответь на сообщение или укажи @username.")
            return

        if is_plus and target_id == giver.id:
            # ачивка за попытку +1 себе
            if _achieve(giver.id, "Читер ёбаный"):
                await msg.reply_text("«Читер ёбаный» 🏅 — за попытку +1 себе. Нельзя!")
            else:
                await msg.reply_text("Нельзя +1 себе, хорош мухлевать 🐍")
            return

        delta = 1 if is_plus else -1
        _inc(REP_RECEIVED, target_id, by=delta)
        _inc(REP_GIVEN, giver.id, by=delta)
        if delta > 0:
            _inc(REP_POS_GIVEN, giver.id)
            # ачивка «Щедрый засранец»
            if REP_POS_GIVEN.get(giver.id, 0) >= TH_GIVER:
                if _achieve(giver.id, "Щедрый засранец"):
                    await _announce_achievement(context, update.effective_chat.id, giver.id, "Щедрый засранец")
        else:
            _inc(REP_NEG_GIVEN, giver.id)

        # ачивка «Любимчик, сука»
        if REP_RECEIVED.get(target_id, 0) >= TH_LOVED:
            if _achieve(target_id, "Любимчик, сука"):
                await _announce_achievement(context, update.effective_chat.id, target_id, "Любимчик, сука")

        total = REP_RECEIVED.get(target_id, 0)
        sign = "+" if delta > 0 else "-"
        await msg.reply_text(f"{target_name or 'Пользователь'} получает {sign}1. Текущая репа: {total}")
        return  # после репы больше ничего не делаем

    # === 3) Триггеры ===
    for idx, (pattern, answers) in enumerate(TRIGGERS):
        if pattern.search(text):
            if _trigger_allowed(update.effective_chat.id):
                await msg.reply_text(random.choice(answers))
                _inc(TRIGGER_HITS, uid)
                if idx == 1:  # «пиво»-триггер
                    _inc(BEER_HITS, uid)
                    if BEER_HITS.get(uid, 0) >= TH_BEER:
                        if _achieve(uid, "Пивной сомелье-алкаш"):
                            await _announce_achievement(context, update.effective_chat.id, uid, "Пивной сомелье-алкаш")
            break

# ========= СТАТИСТИКА =========
def build_stats_text(chat_id: int) -> str:
    # Топ по полученной репутации (3 места)
    top = sorted(REP_RECEIVED.items(), key=lambda x: x[1], reverse=True)[:3]
    top_lines = [f"• {_name_or_id(uid)}: {score}" for uid, score in top] or ["• пока пусто"]

    # Текущие ники для чата
    nick_items = NICKS.get(chat_id, {})
    nick_lines = [f"• {_name_or_id(uid)}: {nick}" for uid, nick in nick_items.items()] or ["• пока никому не присвоено"]

    # Активность (топ-3 по сообщениям)
    top_msg = sorted(MSG_COUNT.items(), key=lambda x: x[1], reverse=True)[:3]
    msg_lines = [f"• {_name_or_id(uid)}: {cnt} смс / {CHAR_COUNT.get(uid,0)} симв."
                 for uid, cnt in top_msg] or ["• пока пусто"]

    return (
        f"{STATS_TITLE}\n\n"
        "🏆 Топ по репутации:\n" + "\n".join(top_lines) + "\n\n"
        "📝 Текущие ники:\n" + "\n".join(nick_lines) + "\n\n"
        "⌨️ Активность:\n" + "\n".join(msg_lines)
    )

def build_achievements_text(user_id: Optional[int]) -> str:
    lines = ["🏅 Ачивки — список и твои галочки:\n"]
    got = ACHIEVEMENTS.get(user_id or -1, set())
    for title, (desc, cond) in ACH_LIST.items():
        mark = "✅" if title in got else "▫️"
        lines.append(f"{mark} *{title}* — {desc} _(условие: {cond})_")
    return "\n".join(lines)

# ========= HEALTH для Render =========
app = Flask(__name__)
@app.get("/")
def health():
    return "Bot is running!"

def run_flask():
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# ========= ENTRY =========
async def _pre_init(app: Application):
    # на всякий: удаляем возможный webhook
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

def main():
    # маленький веб-сервер: Render любит, когда кто-то слушает порт
    threading.Thread(target=run_flask, daemon=True).start()

    # «тихие» таймауты для polling (меньше конфликтов в логах на free)
    req = HTTPXRequest(connect_timeout=10.0, read_timeout=25.0, pool_timeout=5.0)

    application: Application = (
        ApplicationBuilder()
        .token(API_TOKEN)
        .get_updates_request(req)
        .post_init(_pre_init)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help",  cmd_help))
    application.add_handler(CommandHandler("nick",  cmd_nick))
    application.add_handler(CommandHandler("8ball", cmd_8ball))

    # Кнопки
    application.add_handler(CallbackQueryHandler(on_button))

    # ЕДИНЫЙ обработчик текстов (не команд)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

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
