import os
import re
import random
import threading
import html
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set, Tuple, List

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.request import HTTPXRequest

# ========= НАСТРОЙКИ =========
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Кулдаун на генерацию никнейма инициатором (для теста 10 сек; потом можно на час)
NICK_COOLDOWN = timedelta(seconds=10)
# Антиспам для авто-триггеров (на чат)
TRIGGER_COOLDOWN = timedelta(seconds=20)
# Лимит репутации: сколько выдач (+/-) один пользователь может сделать за 24 часа
REP_DAILY_LIMIT = 10
REP_WINDOW = timedelta(hours=24)
UTC = timezone.utc

# ========= ТЕКСТЫ =========
WELCOME_TEXT = (
    "Привет! Я ваш ламповый бот для чата 🔥\n\n"
    "Жми кнопки ниже — там памятка, статистика и ачивки."
)
HELP_TEXT = (
    "🛠 Что умеет этот бот:\n"
    "• /start — открыть меню\n"
    "• /nick — ник себе; /nick @user или ответом — ник другу\n"
    "• /8ball вопрос — магический шар отвечает\n"
    "• +1 / -1 — репутация по реплаю или с @username\n"
    "• «📊 Статистика» — топ репы, текущие ники и активность\n"
    "• «🏅 Ачивки» — список возможных достижений\n"
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
# история выдач (+/-) для дневного лимита: user_id -> [utc datetimes]
REP_GIVE_TIMES: Dict[int, List[datetime]] = {}

# — счётчики
MSG_COUNT: Dict[int, int] = {}            # user_id -> сообщений
CHAR_COUNT: Dict[int, int] = {}           # user_id -> символов
NICK_CHANGE_COUNT: Dict[int, int] = {}    # user_id -> сколько раз меняли ник
EIGHTBALL_COUNT: Dict[int, int] = {}      # user_id -> вызовов 8ball
TRIGGER_HITS: Dict[int, int] = {}         # user_id -> сколько раз триггерил бот
BEER_HITS: Dict[int, int] = {}            # user_id -> сколько раз словил «пиво»-триггер
LAST_MSG_AT: Dict[int, datetime] = {}     # user_id -> последний момент, когда писал

# — админ-взаимодействия
ADMIN_PLUS_GIVEN: Dict[int, int] = {}     # user_id -> сколько раз поставил +1 админам
ADMIN_MINUS_GIVEN: Dict[int, int] = {}    # user_id -> сколько раз поставил -1 админам

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
    # базовые
    "Читер ёбаный":         ("попытка поставить +1 самому себе",         "Сам себе +1 — нельзя."),
    "Никофил ебучий":       ("5 смен никнейма",                           "Сменил ник ≥ 5 раз."),
    "Ник-коллекционер":     ("10 смен никнейма",                          "Сменил ник ≥ 10 раз."),
    "Щедрый засранец":      ("раздал +10 репутаций",                      "Выдано +1 ≥ 10 раз."),
    "Заводила-плюсовик":    ("раздал любую репу 20 раз",                  "Любых выдач (±1) ≥ 20."),
    "Любимчик, сука":       ("получил +20 репутации",                     "Получено репы ≥ 20."),
    "Токсик-магнит":        ("ушёл в минус по репе",                      "Полученная репа ≤ -10."),
    "Пивной сомелье-алкаш": ("5 раз триггерил «пиво»",                    "«Пиво»-триггеров ≥ 5."),
    "Пивозавр":             ("20 «пивных» триггеров",                     "«Пиво»-триггеров ≥ 20."),
    "Шароман долбанный":    ("/8ball вызван 10 раз",                      "Вызовов /8ball ≥ 10."),
    "Клаводробилка":        ("настрочил 5000 символов",                   "Символов ≥ 5000."),
    "Писарь-маховик":       ("накидал 100 сообщений",                     "Сообщений ≥ 100."),
    "Триггер-мейкер":       ("15 раз триггерил бота",                     "Любых триггеров ≥ 15."),
    # взаимодействия с админом и «соц» очивки
    "Тронолом":             ("менял ник админа",                          "Сменил ник пользователю-админу."),
    "Подхалим генеральский":("поставил +1 админу 5 раз",                  "Выдал +1 админам ≥ 5."),
    "Ужалил короля":        ("влепил -1 админам 3 раза",                  "Выдал -1 админам ≥ 3."),
    "Крутой чел":           ("накопил солидную репу",                     "Полученная репутация ≥ 50."),
    "Опущенный":            ("опустился по репутации ниже плинтуса",      "Полученная репутация ≤ -20."),
    "Пошёл смотреть коров": ("пропадал 5 дней",                           "Перерыв ≥ 5 дней."),
    "Споткнулся о ***":     ("пропадал 3 дня",                            "Перерыв ≥ 3 дня."),
    "Сортирный поэт":       ("шутит ниже пояса",                          "NSFW-слова в сообщениях."),
    # большие цифры
    "Король репы":          ("репутация как у бога",                      "Полученная репутация ≥ 100."),
    "Минусатор-маньяк":     ("раздал -1 десять раз",                      "Выдано -1 ≥ 10."),
    "Флудераст":            ("спамил как шаман",                          "Сообщений ≥ 300."),
    "Словесный понос":      ("разлил океан текста",                       "Символов ≥ 20000."),
    "Секретный дрочер шара":("подсел на 8ball",                           "Вызовов /8ball ≥ 30."),
}

# Пороги
TH_NICKOFIL = 5
TH_NICKCOLL = 10
TH_GIVER_POS = 10
TH_GIVER_ANY = 20
TH_LOVED    = 20
TH_TOXIC    = -10
TH_BEER_1   = 5
TH_BEER_2   = 20
TH_BALL_1   = 10
TH_BALL_2   = 30
TH_CHARS_1  = 5000
TH_CHARS_2  = 20000
TH_MSGS_1   = 100
TH_MSGS_2   = 300
TH_COOL     = 50
TH_DOWN     = -20
TH_ADMIN_PLUS = 5
TH_ADMIN_MINUS = 3

# NSFW-нейтральные слова (без наездов на группы)
NSFW_WORDS = re.compile(r"\b(секс|69|кутёж|жоп|перд|фалл|эрот|порн|xxx|🍑|🍆)\b", re.IGNORECASE)

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
    return f"@{u.username}" if u.username else (u.full_name or f"id{u.id}")

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
    now = datetime.now(UTC)
    last = LAST_NICK.get(uid)
    if last and now - last < NICK_COOLDOWN:
        left = int((last + NICK_COOLDOWN - now).total_seconds())
        return f"подожди ещё ~{left} сек."
    return None

def _mark_nick(uid: int):
    LAST_NICK[uid] = datetime.now(UTC)

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

async def _is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

def _within_limit_and_mark(giver_id: int) -> Tuple[bool, Optional[int]]:
    """Проверка дневного лимита выдачи репутации (±1) за 24 часа.
       Возвращает (ok, secs_left_until_free_slot)."""
    now = datetime.now(UTC)
    arr = REP_GIVE_TIMES.get(giver_id, [])
    # чистим старые
    arr = [t for t in arr if now - t < REP_WINDOW]
    REP_GIVE_TIMES[giver_id] = arr
    if len(arr) >= REP_DAILY_LIMIT:
        oldest = min(arr)
        secs = int((oldest + REP_WINDOW - now).total_seconds())
        return False, max(1, secs)
    # ок, добавим этот вызов
    arr.append(now)
    REP_GIVE_TIMES[giver_id] = arr
    return True, None

def _name_or_id(uid: int) -> str:
    return NAMES.get(uid, f"id{uid}")

# ========= КОМАНДЫ/КНОПКИ =========
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
        await q.message.reply_text(build_achievements_catalog(), reply_markup=main_keyboard(), parse_mode="HTML")
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
    target_user: Optional[User] = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user = update.message.reply_to_message.from_user

    target_id: Optional[int] = None
    target_name: Optional[str] = None
    if target_user:
        target_id = target_user.id
        target_name = _display_name(target_user)
        await _remember_user(target_user)
    else:
        if update.message.text:
            parts = update.message.text.split()
            if len(parts) >= 2 and parts[1].startswith("@"):
                uid = KNOWN.get(parts[1][1:].lower())
                if uid:
                    target_id = uid
                    target_name = parts[1]

    if target_id is None:
        target_id = initiator.id
        target_name = _display_name(initiator)

    _ensure_chat(chat_id)
    prev = NICKS[chat_id].get(target_id)
    new_nick = _make_nick(chat_id, prev)
    _apply_nick(chat_id, target_id, new_nick)
    _mark_nick(initiator.id)

    # ачивки за ники
    cnt = NICK_CHANGE_COUNT.get(target_id, 0)
    if cnt >= TH_NICKOFIL and _achieve(target_id, "Никофил ебучий"):
        await _announce_achievement(context, chat_id, target_id, "Никофил ебучий")
    if cnt >= TH_NICKCOLL and _achieve(target_id, "Ник-коллекционер"):
        await _announce_achievement(context, chat_id, target_id, "Ник-коллекционер")

    # ачивка за взаимодействие с админом (если менял ник не себе)
    if target_id != initiator.id:
        if await _is_admin(chat_id, target_id, context):
            if _achieve(initiator.id, "Тронолом"):
                await _announce_achievement(context, chat_id, initiator.id, "Тронолом")

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
    if EIGHTBALL_COUNT.get(uid, 0) >= TH_BALL_1 and _achieve(uid, "Шароман долбанный"):
        await _announce_achievement(context, update.effective_chat.id, uid, "Шароман долбанный")
    if EIGHTBALL_COUNT.get(uid, 0) >= TH_BALL_2 and _achieve(uid, "Секретный дрочер шара"):
        await _announce_achievement(context, update.effective_chat.id, uid, "Секретный дрочер шара")

    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Задай вопрос: `/8ball стоит ли идти за пивом?`", parse_mode="Markdown")
        return
    await update.message.reply_text(random.choice(EIGHT_BALL))

# ---- ЕДИНЫЙ ОБРАБОТЧИК ТЕКСТА: счётчики → репутация → триггеры/NSFW/AFK ----
REP_CMD = re.compile(r"^[\+\-]1(\b|$)", re.IGNORECASE)

def _trigger_allowed(chat_id: int) -> bool:
    now = datetime.now(UTC)
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

    # === 0) AFK-ачивки: проверяем разрыв с прошлого сообщения ===
    now = datetime.now(UTC)
    uid = msg.from_user.id
    if uid in LAST_MSG_AT:
        gap = now - LAST_MSG_AT[uid]
        if gap >= timedelta(days=5) and _achieve(uid, "Пошёл смотреть коров"):
            await _announce_achievement(context, update.effective_chat.id, uid, "Пошёл смотреть коров")
        elif gap >= timedelta(days=3) and _achieve(uid, "Споткнулся о ***"):
            await _announce_achievement(context, update.effective_chat.id, uid, "Споткнулся о ***")
    LAST_MSG_AT[uid] = now

    # === 1) Счётчики активности ===
    text = (msg.text or "")
    _inc(MSG_COUNT, uid)
    _inc(CHAR_COUNT, uid, by=len(text))
    # объёмные ачивки
    if CHAR_COUNT.get(uid, 0) >= TH_CHARS_1 and _achieve(uid, "Клаводробилка"):
        await _announce_achievement(context, update.effective_chat.id, uid, "Клаводробилка")
    if CHAR_COUNT.get(uid, 0) >= TH_CHARS_2 and _achieve(uid, "Словесный понос"):
        await _announce_achievement(context, update.effective_chat.id, uid, "Словесный понос")
    if MSG_COUNT.get(uid, 0) >= TH_MSGS_1 and _achieve(uid, "Писарь-маховик"):
        await _announce_achievement(context, update.effective_chat.id, uid, "Писарь-маховик")
    if MSG_COUNT.get(uid, 0) >= TH_MSGS_2 and _achieve(uid, "Флудераст"):
        await _announce_achievement(context, update.effective_chat.id, uid, "Флудераст")

    t = text.strip()
    if not t:
        return

    # === 2) Репутация (+1/-1) ===
    if REP_CMD.match(t):
        is_plus = t.startswith("+")
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
            parts = t.split()
            if len(parts) >= 2 and parts[1].startswith("@"):
                uid2 = KNOWN.get(parts[1][1:].lower())
                if uid2:
                    target_id = uid2
                    target_name = parts[1]

        if target_id is None:
            await msg.reply_text("Кому ставим репу? Ответь на сообщение или укажи @username.")
            return

        # лимит выдач за 24 часа
        ok, secs_left = _within_limit_and_mark(giver.id)
        if not ok:
            mins = (secs_left or 60) // 60
            await msg.reply_text(f"Лимит репутации на 24 часа исчерпан (10/10). Попробуй через ~{mins} мин.")
            return

        # запрет «+1 себе»
        if is_plus and target_id == giver.id:
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
        else:
            _inc(REP_NEG_GIVEN, giver.id)

        # админ-взаимодействия
        try:
            if await _is_admin(update.effective_chat.id, target_id, context):
                if delta > 0:
                    _inc(ADMIN_PLUS_GIVEN, giver.id)
                    if ADMIN_PLUS_GIVEN.get(giver.id, 0) >= TH_ADMIN_PLUS and _achieve(giver.id, "Подхалим генеральский"):
                        await _announce_achievement(context, update.effective_chat.id, giver.id, "Подхалим генеральский")
                else:
                    _inc(ADMIN_MINUS_GIVEN, giver.id)
                    if ADMIN_MINUS_GIVEN.get(giver.id, 0) >= TH_ADMIN_MINUS and _achieve(giver.id, "Ужалил короля"):
                        await _announce_achievement(context, update.effective_chat.id, giver.id, "Ужалил короля")
        except Exception:
            pass

        # большие реп-ачивки для цели
        if REP_RECEIVED.get(target_id, 0) >= TH_LOVED and _achieve(target_id, "Любимчик, сука"):
            await _announce_achievement(context, update.effective_chat.id, target_id, "Любимчик, сука")
        if REP_RECEIVED.get(target_id, 0) <= TH_TOXIC and _achieve(target_id, "Токсик-магнит"):
            await _announce_achievement(context, update.effective_chat.id, target_id, "Токсик-магнит")
        if REP_RECEIVED.get(target_id, 0) >= TH_COOL and _achieve(target_id, "Крутой чел"):
            await _announce_achievement(context, update.effective_chat.id, target_id, "Крутой чел")
        if REP_RECEIVED.get(target_id, 0) <= TH_DOWN and _achieve(target_id, "Опущенный"):
            await _announce_achievement(context, update.effective_chat.id, target_id, "Опущенный")

        # «Заводила-плюсовик» и «Щедрый засранец», «Минусатор-маньяк»
        total_gives = REP_POS_GIVEN.get(giver.id, 0) + REP_NEG_GIVEN.get(giver.id, 0)
        if total_gives >= TH_GIVER_ANY and _achieve(giver.id, "Заводила-плюсовик"):
            await _announce_achievement(context, update.effective_chat.id, giver.id, "Заводила-плюсовик")
        if REP_POS_GIVEN.get(giver.id, 0) >= TH_GIVER_POS and _achieve(giver.id, "Щедрый засранец"):
            await _announce_achievement(context, update.effective_chat.id, giver.id, "Щедрый засранец")
        if REP_NEG_GIVEN.get(giver.id, 0) >= 10 and _achieve(giver.id, "Минусатор-маньяк"):
            await _announce_achievement(context, update.effective_chat.id, giver.id, "Минусатор-маньяк")

        total = REP_RECEIVED.get(target_id, 0)
        sign = "+" if delta > 0 else "-"
        await msg.reply_text(f"{_name_or_id(target_id)} получает {sign}1. Текущая репа: {total}")
        return  # после репы триггеры не обрабатываем

    # === 3) Триггеры ===
    for idx, (pattern, answers) in enumerate(TRIGGERS):
        if pattern.search(t):
            if _trigger_allowed(update.effective_chat.id):
                await msg.reply_text(random.choice(answers))
                _inc(TRIGGER_HITS, uid)
                # общий триггер-ачив
                if TRIGGER_HITS.get(uid, 0) >= 15 and _achieve(uid, "Триггер-мейкер"):
                    await _announce_achievement(context, update.effective_chat.id, uid, "Триггер-мейкер")
                # пивной
                if idx == 1:
                    _inc(BEER_HITS, uid)
                    if BEER_HITS.get(uid, 0) >= TH_BEER_1 and _achieve(uid, "Пивной сомелье-алкаш"):
                        await _announce_achievement(context, update.effective_chat.id, uid, "Пивной сомелье-алкаш")
                    if BEER_HITS.get(uid, 0) >= TH_BEER_2 and _achieve(uid, "Пивозавр"):
                        await _announce_achievement(context, update.effective_chat.id, uid, "Пивозавр")
            break

    # === 4) NSFW-детект для «Сортирный поэт» ===
    if NSFW_WORDS.search(t):
        # дадим ачивку при первом попадании
        if _achieve(uid, "Сортирный поэт"):
            await _announce_achievement(context, update.effective_chat.id, uid, "Сортирный поэт")

# ========= СТАТИСТИКА =========
def build_stats_text(chat_id: int) -> str:
    # Топ по полученной репутации (3 места)
    top = sorted(REP_RECEIVED.items(), key=lambda x: x[1], reverse=True)[:3]
    top_lines = [f"• {_name_or_id(uid)}: {score}" for uid, score in top] or ["• пока пусто"]

    # Текущие ники (по чату)
    nick_items = NICKS.get(chat_id, {})
    nick_lines = [f"• {_name_or_id(uid)}: {nick}" for uid, nick in nick_items.items()] or ["• пока никому не присвоено"]

    # Активность (топ-3 по сообщениям)
    top_msg = sorted(MSG_COUNT.items(), key=lambda x: x[1], reverse=True)[:3]
    msg_lines = [f"• {_name_or_id(uid)}: {cnt} смс / {CHAR_COUNT.get(uid,0)} симв."
                 for uid, cnt in top_msg] or ["• пока пусто"]

    # Ачивки по людям (все, у кого они есть)
    ach_lines = []
    for uid, titles in ACHIEVEMENTS.items():
        if not titles:
            continue
        title_list = ", ".join(sorted(titles))
        ach_lines.append(f"• {_name_or_id(uid)}: {title_list}")
    if not ach_lines:
        ach_lines = ["• пока ни у кого нет"]

    return (
        f"{STATS_TITLE}\n\n"
        "🏆 Топ по репутации:\n" + "\n".join(top_lines) + "\n\n"
        "📝 Текущие ники:\n" + "\n".join(nick_lines) + "\n\n"
        "⌨️ Активность:\n" + "\n".join(msg_lines) + "\n\n"
        "🏅 Ачивки участников:\n" + "\n".join(ach_lines)
    )

def build_achievements_catalog() -> str:
    lines = ["🏅 Список возможных ачивок:<br/>"]
    for title, (desc, cond) in ACH_LIST.items():
        lines.append(
            f"• <b>{html.escape(title)}</b> — {html.escape(desc)} "
            f"<i>(условие: {html.escape(cond)})</i>"
        )
    return "<br/>".join(lines)

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

    # «тихие» таймауты для polling (меньше конфликтов на free)
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
