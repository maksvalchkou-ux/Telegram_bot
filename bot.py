import os
import re
import random
import threading
import html
import json
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set, Tuple, List

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, InputFile
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.request import HTTPXRequest
import httpx

# ========= НАСТРОЙКИ / ENV =========
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

GIST_TOKEN = os.getenv("GIST_TOKEN")
GIST_ID = os.getenv("GIST_ID")
GIST_FILENAME = os.getenv("GIST_FILENAME", "chat_state.json")

SELF_URL = os.getenv("SELF_URL")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # Telegram user id владельца (для allowlist)

# Кулдауны/лимиты
NICK_COOLDOWN = timedelta(hours=1)
TRIGGER_COOLDOWN = timedelta(seconds=20)
REP_DAILY_LIMIT = 10
REP_WINDOW = timedelta(hours=24)
UTC = timezone.utc

# Прочее
ADMINS_TTL_SEC = 600
LOCAL_BACKUP = "state_backup.json"

# ========= ТЕКСТЫ =========
WELCOME_TEXT = (
    "Привет! Я ламповый бот для чата 🔥\n\n"
    "Жми кнопки ниже — памятка и статистика."
)
HELP_TEXT = (
    "🛠 Команды (коротко):\n"
    "• /start — открыть меню\n"
    "• /nick — ник себе; /nick @user или ответом — ник другу\n"
    "• /8ball вопрос — магический шар отвечает\n"
    "• +1 / -1 — репутация по реплаю или с @username\n"
    "• «📊 Статистика» — топ-10 репы, ники, активность и ачивки\n\n"
    "Для админов — набери /admin (в группе) или в ЛС: будет меню."
)
ADMIN_MENU_TEXT = (
    "🔧 Меню админа (работает в ЛС):\n"
    "• /triggers — список/управление триггерами чата\n"
    "• /triggers_add — добавить триггер (мастер)\n"
    "• /triggers_toggle <№> — вкл/выкл по номеру из списка\n"
    "• /triggers_del <№> — удалить\n"
    "• /resettriggers — вернуть дефолтные триггеры\n"
    "• /reset — сбросить ВСЮ историю чата (подтверждение)\n"
    "• /resetuser @user (или по реплаю в группе)\n"
    "• /export, /export_here, /import — бэкапы\n"
    "• /diag — диагностика\n\n"
    "Для владельца:\n"
    "• /allowchat — разрешить текущий чат\n"
    "• /denychat — запретить и стереть данные чата\n"
    "• /listchats — список разрешённых чатов"
)
STATS_TITLE = "📊 Статистика"

# ========= КНОПКИ =========
BTN_HELP  = "help_info"
BTN_STATS = "stats_open"

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧰 Что умеет этот бот", callback_data=BTN_HELP)],
        [InlineKeyboardButton("📊 Статистика", callback_data=BTN_STATS)],
    ])

# ========= ПАМЯТЬ (in-memory) =========
# Allowlist чатов: только здесь бот полноценно работает
ALLOW_CHATS: Set[int] = set()
CHAT_TITLES: Dict[int, str] = {}  # красиво показывать в списках

# — ники
NICKS: Dict[int, Dict[int, str]] = {}
TAKEN: Dict[int, Set[str]] = {}
LAST_NICK: Dict[int, datetime] = {}

# — известные @username и имена
KNOWN: Dict[str, int] = {}  # username_lower -> user_id
NAMES: Dict[int, str] = {}  # user_id -> display (@username > full_name)

# — триггеры: конфигируемые per-chat
class TriggerCfg(dict): ...
# TRIGGERS_CFG[chat_id] = List[ {id,name,pattern,is_regex,ignore_case,word_boundaries,answers:[...],enabled:bool} ]
TRIGGERS_CFG: Dict[int, List[TriggerCfg]] = {}

# — служебное: компилированные regex для быстрого матчинга
TRIGGERS_COMPILED: Dict[int, List[Tuple[re.Pattern, List[str], str, bool]]] = {}  # chat_id -> [(pattern, answers, name, enabled)]

# — спам/тайминги
LAST_TRIGGER_TIME: Dict[int, datetime] = {}

# — репутация/счётчики — ПО ЧАТАМ
REP_GIVEN: Dict[int, Dict[int, int]] = {}
REP_RECEIVED: Dict[int, Dict[int, int]] = {}
REP_POS_GIVEN: Dict[int, Dict[int, int]] = {}
REP_NEG_GIVEN: Dict[int, Dict[int, int]] = {}
REP_GIVE_TIMES: Dict[int, Dict[int, List[datetime]]] = {}

MSG_COUNT: Dict[int, Dict[int, int]] = {}
CHAR_COUNT: Dict[int, Dict[int, int]] = {}
NICK_CHANGE_COUNT: Dict[int, Dict[int, int]] = {}
EIGHTBALL_COUNT: Dict[int, Dict[int, int]] = {}
TRIGGER_HITS: Dict[int, Dict[int, int]] = {}
BEER_HITS: Dict[int, Dict[int, int]] = {}
LAST_MSG_AT: Dict[int, Dict[int, datetime]] = {}

ADMIN_PLUS_GIVEN: Dict[int, Dict[int, int]] = {}
ADMIN_MINUS_GIVEN: Dict[int, Dict[int, int]] = {}

ACHIEVEMENTS: Dict[int, Dict[int, Set[str]]] = {}

# — кеш админов: chat_id -> (set(user_id), expires_ts)
ADMINS_CACHE: Dict[int, Tuple[Set[int], float]] = {}

# — сессии админов для простых мастеров в ЛС (добавление триггера и т.п.)
ADMIN_SESS: Dict[int, Dict[str, any]] = {}  # key: admin user_id

# — локи
STATE_LOCK = asyncio.Lock()

# ========= ДЕФОЛТНЫЕ ТРИГГЕРЫ (МИГРИРУЮТСЯ В CFG ПРИ НУЖДЕ) =========
DEFAULT_TRIGGERS: List[TriggerCfg] = [
    TriggerCfg({
        "id": "work",
        "name": "Работа",
        "pattern": r"\bработ(а|ать|аю|аем|ает|ал|али|ать|у|ы|е|ой)\b",
        "is_regex": True,
        "ignore_case": True,
        "word_boundaries": False,
        "answers": ["Работка подъехала? Держись, чемпион 🛠️",
                    "Опять работать? Забирай +100 к терпению 💪",
                    "Трудяга на связи. После — пивко заслужено 🍺"],
        "enabled": True
    }),
    TriggerCfg({
        "id": "beer",
        "name": "Пиво",
        "pattern": r"\bпив(о|ос|ко|анд|андос)\b",
        "is_regex": True,
        "ignore_case": True,
        "word_boundaries": False,
        "answers": ["За холодненькое! 🍻","Пенная пауза — святое дело 🍺","Пивко — и всё наладится 😌"],
        "enabled": True
    }),
    TriggerCfg({
        "id": "sleep",
        "name": "Сон",
        "pattern": r"\bспат|сон|засып|дрыхн|высп(а|ы)",
        "is_regex": True,
        "ignore_case": True,
        "word_boundaries": False,
        "answers": ["Не забудь поставить будильник ⏰","Сладких снов, котики 😴","Дрыхнуть — это тоже хобби."],
        "enabled": True
    }),
    TriggerCfg({
        "id": "gym",
        "name": "Зал",
        "pattern": r"\bзал\b|\bкач|трен(ир|ер|ирую|ировка)",
        "is_regex": True,
        "ignore_case": True,
        "word_boundaries": False,
        "answers": ["Железо не ждёт! 🏋️","Бицепс сам себя не накачает 💪","После зала — протеин и мемасы."],
        "enabled": True
    }),
    TriggerCfg({
        "id": "money",
        "name": "Деньги",
        "pattern": r"\bденьг|бабк|зарплат|зп\b|\bкэш\b",
        "is_regex": True,
        "ignore_case": True,
        "word_boundaries": False,
        "answers": ["Деньги — пыль. Но приятно, когда их много 💸","На шаурму хватит — жизнь удалась 😎","Финансовый поток уже в пути 🪄"],
        "enabled": True
    }),
    TriggerCfg({
        "id": "hello",
        "name": "Привет",
        "pattern": r"\bприв(ет|ед)|здоро|здравст",
        "is_regex": True,
        "ignore_case": True,
        "word_boundaries": False,
        "answers": ["Приветули 👋","Здарова, легенды!","Йо-йо, чат!"],
        "enabled": True
    }),
    TriggerCfg({
        "id": "bye",
        "name": "Пока",
        "pattern": r"\bпок(а|ед)|до встреч|бб\b",
        "is_regex": True,
        "ignore_case": True,
        "word_boundaries": False,
        "answers": ["Не пропадай 👋","До связи!","Ушёл красиво 🚪"],
        "enabled": True
    }),
    TriggerCfg({
        "id": "love",
        "name": "Любовь",
        "pattern": r"\bлюбл(ю|юлю)|краш|сердечк|романтик",
        "is_regex": True,
        "ignore_case": True,
        "word_boundaries": False,
        "answers": ["Любовь спасёт мир ❤️","Сердечки полетели 💘","Осторожно, милота выше нормы 🫶"],
        "enabled": True
    }),
]

# ========= АЧИВКИ =========
ACH_LIST: Dict[str, Tuple[str, str]] = {
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
    "Тронолом":             ("менял ник админа",                          "Сменил ник пользователю-админу."),
    "Подхалим генеральский":("поставил +1 админу 5 раз",                  "Выдал +1 админам ≥ 5."),
    "Ужалил короля":        ("влепил -1 админам 3 раза",                  "Выдал -1 админам ≥ 3."),
    "Крутой чел":           ("накопил солидную репу",                     "Полученная репа ≥ 50."),
    "Опущенный":            ("опустился по репутации ниже плинтуса",      "Полученная репа ≤ -20."),
    "Пошёл смотреть коров": ("пропадал 5 дней",                           "Перерыв ≥ 5 дней."),
    "Споткнулся о ***":     ("пропадал 3 дня",                            "Перерыв ≥ 3 дня."),
    "Сортирный поэт":       ("шутит ниже пояса",                          "NSFW-слова в сообщениях."),
    "Король репы":          ("репутация как у бога",                      "Полученная репутация ≥ 100."),
    "Минусатор-маньяк":     ("раздал -1 десять раз",                      "Выдано -1 ≥ 10."),
    "Флудераст":            ("спамил как шаман",                          "Сообщений ≥ 300."),
    "Словесный понос":      ("разлил океан текста",                       "Символов ≥ 20000."),
    "Секретный дрочер шара":("подсел на 8ball",                           "Вызовов /8ball ≥ 30."),
}

# ========= МАЛЫЕ УТИЛИТЫ =========
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
    NICKS.setdefault(chat_id, {})
    TAKEN.setdefault(chat_id, set())
    REP_GIVEN.setdefault(chat_id, {})
    REP_RECEIVED.setdefault(chat_id, {})
    REP_POS_GIVEN.setdefault(chat_id, {})
    REP_NEG_GIVEN.setdefault(chat_id, {})
    REP_GIVE_TIMES.setdefault(chat_id, {})
    MSG_COUNT.setdefault(chat_id, {})
    CHAR_COUNT.setdefault(chat_id, {})
    NICK_CHANGE_COUNT.setdefault(chat_id, {})
    EIGHTBALL_COUNT.setdefault(chat_id, {})
    TRIGGER_HITS.setdefault(chat_id, {})
    BEER_HITS.setdefault(chat_id, {})
    LAST_MSG_AT.setdefault(chat_id, {})
    ADMIN_PLUS_GIVEN.setdefault(chat_id, {})
    ADMIN_MINUS_GIVEN.setdefault(chat_id, {})
    ACHIEVEMENTS.setdefault(chat_id, {})
    TRIGGERS_CFG.setdefault(chat_id, None)  # миграция потом

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

def _name_or_id(uid: int) -> str:
    return NAMES.get(uid, f"id{uid}")

def _trigger_allowed(chat_id: int) -> bool:
    now = datetime.now(UTC)
    last = LAST_TRIGGER_TIME.get(chat_id)
    if last and now - last < TRIGGER_COOLDOWN:
        return False
    LAST_TRIGGER_TIME[chat_id] = now
    return True

def _achieve(chat_id: int, user_id: int, title: str) -> bool:
    got = ACHIEVEMENTS.setdefault(chat_id, {}).setdefault(user_id, set())
    if title in got:
        return False
    got.add(title)
    return True

# ========= АДМИНЫ / ALLOWLIST =========
async def _fetch_admins(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Set[int]:
    now = datetime.now().timestamp()
    cached = ADMINS_CACHE.get(chat_id)
    if cached and cached[1] > now:
        return cached[0]
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        ids = {a.user.id for a in admins}
        ADMINS_CACHE[chat_id] = (ids, now + ADMINS_TTL_SEC)
        return ids
    except Exception:
        ADMINS_CACHE[chat_id] = (set(), now + 60)
        return set()

async def _is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if user_id == OWNER_ID:
        return True
    ids = await _fetch_admins(context, chat_id)
    return user_id in ids

def _is_private(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == "private"

def _allowed_chat(chat_id: int) -> bool:
    return chat_id in ALLOW_CHATS

# ========= СЕРИАЛИЗАЦИЯ/БЭКАП =========
def _serialize_state() -> dict:
    return {
        "ALLOW_CHATS": list(ALLOW_CHATS),
        "CHAT_TITLES": {str(k): v for k, v in CHAT_TITLES.items()},
        "NICKS": {str(cid): {str(uid): nick for uid, nick in per.items()} for cid, per in NICKS.items()},
        "TAKEN": {str(cid): list(vals) for cid, vals in TAKEN.items()},
        "LAST_NICK": {str(k): v.isoformat() for k, v in LAST_NICK.items()},
        "KNOWN": KNOWN,
        "NAMES": {str(k): v for k, v in NAMES.items()},
        # репа и счётчики
        "REP_GIVEN": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in REP_GIVEN.items()},
        "REP_RECEIVED": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in REP_RECEIVED.items()},
        "REP_POS_GIVEN": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in REP_POS_GIVEN.items()},
        "REP_NEG_GIVEN": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in REP_NEG_GIVEN.items()},
        "REP_GIVE_TIMES": {
            str(cid): {str(uid): [t.isoformat() for t in arr] for uid, arr in per.items()}
            for cid, per in REP_GIVE_TIMES.items()
        },
        "MSG_COUNT": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in MSG_COUNT.items()},
        "CHAR_COUNT": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in CHAR_COUNT.items()},
        "NICK_CHANGE_COUNT": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in NICK_CHANGE_COUNT.items()},
        "EIGHTBALL_COUNT": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in EIGHTBALL_COUNT.items()},
        "TRIGGER_HITS": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in TRIGGER_HITS.items()},
        "BEER_HITS": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in BEER_HITS.items()},
        "LAST_MSG_AT": {
            str(cid): {str(uid): dt.isoformat() for uid, dt in per.items()}
            for cid, per in LAST_MSG_AT.items()
        },
        "ADMIN_PLUS_GIVEN": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in ADMIN_PLUS_GIVEN.items()},
        "ADMIN_MINUS_GIVEN": {str(cid): {str(uid): int(v) for uid, v in per.items()} for cid, per in ADMIN_MINUS_GIVEN.items()},
        "ACHIEVEMENTS": {
            str(cid): {str(uid): list(titles) for uid, titles in per.items()}
            for cid, per in ACHIEVEMENTS.items()
        },
        # триггеры
        "TRIGGERS_CFG": {
            str(cid): per for cid, per in TRIGGERS_CFG.items()
        },
    }

def _apply_state(data: dict, target_chat_id: Optional[int] = None, only_this_chat: bool = False):
    def parse_dt(s): return datetime.fromisoformat(s)

    # allowlist и заголовки
    ALLOW_CHATS.clear(); ALLOW_CHATS.update(set(data.get("ALLOW_CHATS", [])))
    CHAT_TITLES.clear(); CHAT_TITLES.update({int(k): v for k, v in data.get("CHAT_TITLES", {}).items()})

    if only_this_chat and target_chat_id is not None:
        cid = str(target_chat_id)

        def get_map(key): return {int(uid): v for uid, v in data.get(key, {}).get(cid, {}).items()}
        def get_map_i(key): return {int(uid): int(v) for uid, v in data.get(key, {}).get(cid, {}).items()}

        NICKS[target_chat_id] = get_map("NICKS")
        TAKEN[target_chat_id] = set(data.get("TAKEN", {}).get(cid, []))
        REP_GIVEN[target_chat_id] = get_map_i("REP_GIVEN")
        REP_RECEIVED[target_chat_id] = get_map_i("REP_RECEIVED")
        REP_POS_GIVEN[target_chat_id] = get_map_i("REP_POS_GIVEN")
        REP_NEG_GIVEN[target_chat_id] = get_map_i("REP_NEG_GIVEN")
        MSG_COUNT[target_chat_id] = get_map_i("MSG_COUNT")
        CHAR_COUNT[target_chat_id] = get_map_i("CHAR_COUNT")
        NICK_CHANGE_COUNT[target_chat_id] = get_map_i("NICK_CHANGE_COUNT")
        EIGHTBALL_COUNT[target_chat_id] = get_map_i("EIGHTBALL_COUNT")
        TRIGGER_HITS[target_chat_id] = get_map_i("TRIGGER_HITS")
        BEER_HITS[target_chat_id] = get_map_i("BEER_HITS")
        ADMIN_PLUS_GIVEN[target_chat_id] = get_map_i("ADMIN_PLUS_GIVEN")
        ADMIN_MINUS_GIVEN[target_chat_id] = get_map_i("ADMIN_MINUS_GIVEN")
        LAST_MSG_AT[target_chat_id] = {int(uid): parse_dt(v) for uid, v in data.get("LAST_MSG_AT", {}).get(cid, {}).items()}
        REP_GIVE_TIMES[target_chat_id] = {int(uid): [parse_dt(t) for t in arr]
                                          for uid, arr in data.get("REP_GIVE_TIMES", {}).get(cid, {}).items()}
        ACHIEVEMENTS[target_chat_id] = {int(uid): set(titles) for uid, titles in data.get("ACHIEVEMENTS", {}).get(cid, {}).items()}

        # триггеры чата
        TRIGGERS_CFG[target_chat_id] = data.get("TRIGGERS_CFG", {}).get(cid, None)
        return

    # Полный импорт
    def nested_int_map(obj): return {int(cid): {int(uid): v for uid, v in per.items()} for cid, per in obj.items()}
    def nested_int_map_i(obj): return {int(cid): {int(uid): int(v) for uid, v in per.items()} for cid, per in obj.items()}

    NICKS.clear(); NICKS.update(nested_int_map(data.get("NICKS", {})))
    TAKEN.clear(); TAKEN.update({int(cid): set(vals) for cid, vals in data.get("TAKEN", {}).items()})
    LAST_NICK.clear(); LAST_NICK.update({int(k): parse_dt(v) for k, v in data.get("LAST_NICK", {}).items()})
    KNOWN.clear(); KNOWN.update({k: int(v) for k, v in data.get("KNOWN", {}).items()})
    NAMES.clear(); NAMES.update({int(k): v for k, v in data.get("NAMES", {}).items()})

    REP_GIVEN.clear(); REP_GIVEN.update(nested_int_map_i(data.get("REP_GIVEN", {})))
    REP_RECEIVED.clear(); REP_RECEIVED.update(nested_int_map_i(data.get("REP_RECEIVED", {})))
    REP_POS_GIVEN.clear(); REP_POS_GIVEN.update(nested_int_map_i(data.get("REP_POS_GIVEN", {})))
    REP_NEG_GIVEN.clear(); REP_NEG_GIVEN.update(nested_int_map_i(data.get("REP_NEG_GIVEN", {})))
    REP_GIVE_TIMES.clear()
    for cid, per in data.get("REP_GIVE_TIMES", {}).items():
        REP_GIVE_TIMES[int(cid)] = {int(uid): [parse_dt(t) for t in arr] for uid, arr in per.items()}
    MSG_COUNT.clear(); MSG_COUNT.update(nested_int_map_i(data.get("MSG_COUNT", {})))
    CHAR_COUNT.clear(); CHAR_COUNT.update(nested_int_map_i(data.get("CHAR_COUNT", {})))
    NICK_CHANGE_COUNT.clear(); NICK_CHANGE_COUNT.update(nested_int_map_i(data.get("NICK_CHANGE_COUNT", {})))
    EIGHTBALL_COUNT.clear(); EIGHTBALL_COUNT.update(nested_int_map_i(data.get("EIGHTBALL_COUNT", {})))
    TRIGGER_HITS.clear(); TRIGGER_HITS.update(nested_int_map_i(data.get("TRIGGER_HITS", {})))
    BEER_HITS.clear(); BEER_HITS.update(nested_int_map_i(data.get("BEER_HITS", {})))
    LAST_MSG_AT.clear()
    for cid, per in data.get("LAST_MSG_AT", {}).items():
        LAST_MSG_AT[int(cid)] = {int(uid): parse_dt(v) for uid, v in per.items()}
    ADMIN_PLUS_GIVEN.clear(); ADMIN_PLUS_GIVEN.update(nested_int_map_i(data.get("ADMIN_PLUS_GIVEN", {})))
    ADMIN_MINUS_GIVEN.clear(); ADMIN_MINUS_GIVEN.update(nested_int_map_i(data.get("ADMIN_MINUS_GIVEN", {})))
    ACHIEVEMENTS.clear()
    for cid, per in data.get("ACHIEVEMENTS", {}).items():
        ACHIEVEMENTS[int(cid)] = {int(uid): set(titles) for uid, titles in per.items()}

    # триггеры
    TRIGGERS_CFG.clear()
    for cid, arr in data.get("TRIGGERS_CFG", {}).items():
        TRIGGERS_CFG[int(cid)] = arr

def _build_compiled_triggers_for_chat(chat_id: int):
    """Перекомпилировать триггеры чата после изменений/миграции."""
    lst = TRIGGERS_CFG.get(chat_id) or []
    compiled = []
    for t in lst:
        try:
            if not t.get("enabled", True):
                compiled.append((None, t.get("answers", []), t.get("name",""), False))
                continue
            if t.get("is_regex", True):
                flags = re.IGNORECASE if t.get("ignore_case", True) else 0
                pat = re.compile(t.get("pattern",""), flags)
                compiled.append((pat, t.get("answers", []), t.get("name",""), True))
            else:
                # простая фраза: соберём regex c учётом границ/регистра
                esc = re.escape(t.get("pattern",""))
                if t.get("word_boundaries", False):
                    esc = r"\b" + esc + r"\b"
                flags = re.IGNORECASE if t.get("ignore_case", True) else 0
                pat = re.compile(esc, flags)
                compiled.append((pat, t.get("answers", []), t.get("name",""), True))
        except Exception:
            # невалидный триггер — пропускаем
            continue
    TRIGGERS_COMPILED[chat_id] = compiled

async def cloud_save():
    payload = _serialize_state()
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    # локальный бэкап
    try:
        with open(LOCAL_BACKUP, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass

    if not (GIST_TOKEN and GIST_ID):
        return

    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {"Authorization": f"Bearer {GIST_TOKEN}", "Accept": "application/vnd.github+json"}
    async with httpx.AsyncClient(timeout=12.0) as client:
        try:
            await client.patch(url, json={"files": {GIST_FILENAME: {"content": text}}}, headers=headers)
        except Exception:
            pass

async def cloud_load_if_any():
    # 1) Gist
    if GIST_TOKEN and GIST_ID:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"Bearer {GIST_TOKEN}", "Accept": "application/vnd.github+json"}
        async with httpx.AsyncClient(timeout=12.0) as client:
            try:
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    j = r.json()
                    files = j.get("files", {})
                    if GIST_FILENAME in files and files[GIST_FILENAME].get("content"):
                        data = json.loads(files[GIST_FILENAME]["content"])
                        _apply_state(data, only_this_chat=False)
                        # перекомпилируем триггеры для всех чатов
                        for cid in list(TRIGGERS_CFG.keys()):
                            _build_compiled_triggers_for_chat(cid)
                        return
            except Exception:
                pass
    # 2) локальный
    try:
        if os.path.exists(LOCAL_BACKUP):
            with open(LOCAL_BACKUP, "r", encoding="utf-8") as f:
                data = json.load(f)
            _apply_state(data, only_this_chat=False)
            for cid in list(TRIGGERS_CFG.keys()):
                _build_compiled_triggers_for_chat(cid)
    except Exception:
        pass

# ========= HEALTH / Flask =========
app = Flask(__name__)

@app.get("/")
def root():
    return "Bot is running!"

@app.get("/health")
def health():
    return "ok"

@app.get("/healthz")
def healthz():
    return "ok"

def run_flask():
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

# ========= ПРИВАТНЫЙ / ГРУППОВОЙ ФИЛЬТР =========
async def _guard_private_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Возвращает True, если можно продолжать обработку сообщения в ЛС."""
    if not _is_private(update):
        return True
    uid = update.effective_user.id if update.effective_user else 0
    if uid == OWNER_ID:
        return True
    # простым юзерам в ЛС — ничего
    await context.bot.send_message(uid, "🤖 Этот бот работает только в разрешённых группах. Используйте команды в чате.")
    return False

# ========= СТАРТОВЫЕ КОМАНДЫ / МЕНЮ =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_private(update):
        # ЛС: фильтруем доступ
        if not await _guard_private_access(update, context):
            return
    if update.message:
        await _remember_user(update.effective_user)
        if update.effective_chat.type != "private":
            # сохраним название чата
            CHAT_TITLES[update.effective_chat.id] = update.effective_chat.title or str(update.effective_chat.id)
        await update.message.reply_text(WELCOME_TEXT, reply_markup=main_keyboard())

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_private(update):
        if not await _guard_private_access(update, context):
            return
    if update.message:
        await update.message.reply_text(HELP_TEXT, reply_markup=main_keyboard())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer("Секунду…", show_alert=False)
    data = q.data
    if data == BTN_HELP:
        await q.message.reply_text(HELP_TEXT, reply_markup=main_keyboard())
    elif data == BTN_STATS:
        await q.message.reply_text(build_stats_text(update.effective_chat.id), reply_markup=main_keyboard())
    else:
        await q.message.reply_text("¯\\_(ツ)_/¯ Неизвестная кнопка", reply_markup=main_keyboard())

# ========= ВСПОМОГАТЕЛЬНОЕ: ОЧИСТКИ ПО ЧАТУ/ПОЛЬЗОВАТЕЛЮ =========
def _clear_user_in_chat(chat_id: int, uid: int):
    if uid in NICKS.get(chat_id, {}):
        old = NICKS[chat_id].pop(uid, None)
        if old:
            TAKEN.get(chat_id, set()).discard(old)
    for store in (REP_GIVEN, REP_RECEIVED, REP_POS_GIVEN, REP_NEG_GIVEN,
                  MSG_COUNT, CHAR_COUNT, NICK_CHANGE_COUNT, EIGHTBALL_COUNT,
                  TRIGGER_HITS, BEER_HITS, ADMIN_PLUS_GIVEN, ADMIN_MINUS_GIVEN):
        if uid in store.get(chat_id, {}):
            store[chat_id].pop(uid, None)
    if uid in LAST_MSG_AT.get(chat_id, {}):
        LAST_MSG_AT[chat_id].pop(uid, None)
    if uid in REP_GIVE_TIMES.get(chat_id, {}):
        REP_GIVE_TIMES[chat_id].pop(uid, None)
    if uid in ACHIEVEMENTS.get(chat_id, {}):
        ACHIEVEMENTS[chat_id].pop(uid, None)

def _clear_chat(chat_id: int):
    NICKS[chat_id] = {}
    TAKEN[chat_id] = set()
    REP_GIVEN[chat_id] = {}
    REP_RECEIVED[chat_id] = {}
    REP_POS_GIVEN[chat_id] = {}
    REP_NEG_GIVEN[chat_id] = {}
    REP_GIVE_TIMES[chat_id] = {}
    MSG_COUNT[chat_id] = {}
    CHAR_COUNT[chat_id] = {}
    NICK_CHANGE_COUNT[chat_id] = {}
    EIGHTBALL_COUNT[chat_id] = {}
    TRIGGER_HITS[chat_id] = {}
    BEER_HITS[chat_id] = {}
    LAST_MSG_AT[chat_id] = {}
    ADMIN_PLUS_GIVEN[chat_id] = {}
    ADMIN_MINUS_GIVEN[chat_id] = {}
    ACHIEVEMENTS[chat_id] = {}

# ========= СТАТИСТИКА =========
def build_stats_text(chat_id: int) -> str:
    def top10(d: Dict[int, int]) -> List[Tuple[int, int]]:
        return sorted(d.items(), key=lambda x: x[1], reverse=True)[:10]

    top = top10(REP_RECEIVED.get(chat_id, {}))
    top_lines = [f"• {_name_or_id(uid)}: {score}" for uid, score in top] or ["• пока пусто"]

    nick_items = NICKS.get(chat_id, {})
    nick_lines = [f"• {_name_or_id(uid)}: {nick}" for uid, nick in nick_items.items()] or ["• пока никому не присвоено"]

    top_msg = top10(MSG_COUNT.get(chat_id, {}))
    msg_lines = [f"• {_name_or_id(uid)}: {cnt} смс / {CHAR_COUNT.get(chat_id, {}).get(uid,0)} симв."
                 for uid, cnt in top_msg] or ["• пока пусто"]

    ach_lines = []
    for uid, titles in ACHIEVEMENTS.get(chat_id, {}).items():
        if not titles:
            continue
        ach_lines.append(f"• {_name_or_id(uid)}: {', '.join(sorted(titles))}")
    if not ach_lines:
        ach_lines = ["• пока ни у кого нет"]

    title = CHAT_TITLES.get(chat_id, str(chat_id))
    return (
        f"{STATS_TITLE} — {title}\n\n"
        "🏆 Топ-10 по репутации:\n" + "\n".join(top_lines) + "\n\n"
        "📝 Текущие ники:\n" + "\n".join(nick_lines) + "\n\n"
        "⌨️ Топ-10 по активности:\n" + "\n".join(msg_lines) + "\n\n"
        "🏅 Ачивки участников:\n" + "\n".join(ach_lines)
    )

# ========= НИКИ =========
ADJ = ["шальной","хрустящий","лысый","бурлящий","ламповый","коварный","бархатный","дерзкий","мягкотелый",
       "стальной","сонный","бравый","хитрый","космический","солёный","дымный","пряный","бодрый","тёплый",
       "грозный","подозрительный","барский","весёлый","рандомный","великосветский"]
NOUN = ["ёж","краб","барсук","жираф","карась","барон","пират","самурай","тракторист","клоун","волк","кот",
        "кабан","медведь","сова","дракондон","гусь","козырь","джентльмен","шаман","киборг","арбуз","колобок",
        "профессор","червяк"]
TAILS = ["из подъезда №3","с приветом","на максималках","XL","в тапках","из будущего","при бабочке","deluxe",
         "edition 2.0","без тормозов","официально","с огоньком","в отставке","на бобине","turbo","™️",
         "prime","на районе","с сюрпризом","VIP"]
EMOJIS = ["🦔","🦀","🦊","🐻","🐺","🐗","🐱","🦉","🐟","🦆","🦄","🐲","🥒","🍉","🧀","🍔","🍺","☕️","🔥","💣","✨","🛠️","👑","🛸"]
SPICY = ["подозрительный тип","хитрожоп","задорный бузотёр","порочный джентльмен","дворовый князь",
         "барон с понтами","сомнительный эксперт","самурай-недоучка","киборг на минималках",
         "пират без лицензии","клоун-пофигист","барсук-бродяга"]

def _make_nick(chat_id: int, prev: Optional[str]) -> str:
    taken = TAKEN.setdefault(chat_id, set())
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
    prev = NICKS[chat_id].get(user_id)
    if prev:
        TAKEN[chat_id].discard(prev)
    NICKS[chat_id][user_id] = new_nick
    TAKEN[chat_id].add(new_nick)
    _inc(NICK_CHANGE_COUNT[chat_id], user_id)

async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_private(update):
        if not await _guard_private_access(update, context):
            return
    if not update.message:
        return
    await _remember_user(update.effective_user)

    chat_id = update.effective_chat.id
    _ensure_chat(chat_id)

    initiator = update.effective_user
    cd = _cooldown_text(initiator.id)
    if cd:
        await update.message.reply_text(f"Потерпи, {cd}")
        return

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

    prev = NICKS[chat_id].get(target_id)
    new_nick = _make_nick(chat_id, prev)
    async with STATE_LOCK:
        _apply_nick(chat_id, target_id, new_nick)
        _mark_nick(initiator.id)
        cnt = NICK_CHANGE_COUNT[chat_id].get(target_id, 0)
        nick5 = (cnt >= 5 and _achieve(chat_id, target_id, "Никофил ебучий"))
        nick10 = (cnt >= 10 and _achieve(chat_id, target_id, "Ник-коллекционер"))
    if nick5:
        await _announce_achievement(context, chat_id, target_id, "Никофил ебучий")
    if nick10:
        await _announce_achievement(context, chat_id, target_id, "Ник-коллекционер")
    if target_id != initiator.id and await _is_admin(chat_id, target_id, context):
        if _achieve(chat_id, initiator.id, "Тронолом"):
            await _announce_achievement(context, chat_id, initiator.id, "Тронолом")
    if target_id == initiator.id:
        await update.message.reply_text(f"Твой новый ник: «{new_nick}»")
    else:
        await update.message.reply_text(f"{target_name} теперь известен(а) как «{new_nick}»")

# ========= /8BALL =========
EIGHT_BALL = [
    "Да ✅","Нет ❌","Возможно 🤔","Скорее да, чем нет","Спроси позже 🕐","Сто процентов 💯",
    "Есть шанс, но не сегодня","Не рискуй","Лучше промолчу","Звёзды говорят «да» ✨",
    "Это судьба","Шансы малы","Точно нет","Легенды шепчут «да»","Не сейчас",
    "Абсолютно","М-да… такое себе","Ответ внутри тебя","Ха-ха, конечно!","Даже не думай"
]
async def cmd_8ball(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_private(update):
        if not await _guard_private_access(update, context):
            return
    if not update.message:
        return
    await _remember_user(update.effective_user)
    chat_id = update.effective_chat.id
    _ensure_chat(chat_id)
    uid = update.effective_user.id
    async with STATE_LOCK:
        _inc(EIGHTBALL_COUNT[chat_id], uid)
        c10 = (EIGHTBALL_COUNT[chat_id].get(uid, 0) >= 10 and _achieve(chat_id, uid, "Шароман долбанный"))
        c30 = (EIGHTBALL_COUNT[chat_id].get(uid, 0) >= 30 and _achieve(chat_id, uid, "Секретный дрочер шара"))
    if c10:
        await _announce_achievement(context, chat_id, uid, "Шароман долбанный")
    if c30:
        await _announce_achievement(context, chat_id, uid, "Секретный дрочер шара")
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Задай вопрос: `/8ball стоит ли идти за пивом?`", parse_mode="Markdown")
        return
    await update.message.reply_text(random.choice(EIGHTBALL))

# ========= РЕПУТАЦИЯ/ТРИГГЕРЫ/NSFW =========
REP_CMD = re.compile(r"^[\+\-]1(\b|$)", re.IGNORECASE)
NSFW_RX = re.compile(r"\b(секс|69|кутёж|жоп|перд|фалл|эрот|порн|xxx|🍑|🍆)\b", re.IGNORECASE)

async def _announce_achievement(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, title: str):
    name = NAMES.get(user_id, f"id{user_id}")
    desc = ACH_LIST.get(title, ("", ""))[0]
    try:
        await context.bot.send_message(chat_id, f"🏅 {html.escape(name)} получает ачивку: <b>{html.escape(title)}</b> — {html.escape(desc)}", parse_mode="HTML")
    except Exception:
        pass

def _within_limit_and_mark(chat_id: int, giver_id: int) -> Tuple[bool, Optional[int]]:
    now = datetime.now(UTC)
    per_chat = REP_GIVE_TIMES.setdefault(chat_id, {})
    arr = per_chat.get(giver_id, [])
    arr = [t for t in arr if now - t < REP_WINDOW]
    per_chat[giver_id] = arr
    if len(arr) >= REP_DAILY_LIMIT:
        oldest = min(arr)
        secs = int((oldest + REP_WINDOW - now).total_seconds())
        return False, max(1, secs)
    arr.append(now)
    per_chat[giver_id] = arr
    return True, None

def _ensure_triggers_migrated(chat_id: int):
    if TRIGGERS_CFG.get(chat_id) is None:
        # создать из дефолта (копия)
        TRIGGERS_CFG[chat_id] = [TriggerCfg(dict(t)) for t in DEFAULT_TRIGGERS]
        _build_compiled_triggers_for_chat(chat_id)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.from_user is None:
        return
    if msg.from_user.is_bot:
        return

    # ГРУППЫ: проверяем allowlist
    if update.effective_chat.type in ("group", "supergroup"):
        chat_id = update.effective_chat.id
        if not _allowed_chat(chat_id):
            # Если владелец — подсказываем команду, иначе уходим
            if update.effective_user and update.effective_user.id == OWNER_ID and msg.text and msg.text.startswith("/allowchat"):
                pass  # владелец сейчас разрешит
            else:
                try:
                    await msg.reply_text("🚫 Этот чат не авторизован для использования бота.")
                except Exception:
                    pass
                try:
                    await context.bot.leave_chat(chat_id)
                except Exception:
                    pass
                return

    # ЛС: фильтр
    if _is_private(update):
        if not await _guard_private_access(update, context):
            return

    # Запомним имя/@username
    await _remember_user(msg.from_user)

    chat_id = update.effective_chat.id
    _ensure_chat(chat_id)
    CHAT_TITLES[chat_id] = update.effective_chat.title or str(chat_id)

    # === 0) AFK-ачивки ===
    now = datetime.now(UTC)
    uid = msg.from_user.id
    prev = LAST_MSG_AT[chat_id].get(uid)
    if prev:
        gap = now - prev
        if gap >= timedelta(days=5) and _achieve(chat_id, uid, "Пошёл смотреть коров"):
            await _announce_achievement(context, chat_id, uid, "Пошёл смотреть коров")
        elif gap >= timedelta(days=3) and _achieve(chat_id, uid, "Споткнулся о ***"):
            await _announce_achievement(context, chat_id, uid, "Споткнулся о ***")
    LAST_MSG_AT[chat_id][uid] = now

    # === 1) Счётчики ===
    text = (msg.text or "")
    async with STATE_LOCK:
        _inc(MSG_COUNT[chat_id], uid)
        _inc(CHAR_COUNT[chat_id], uid, by=len(text))
        if CHAR_COUNT[chat_id].get(uid, 0) >= 5000 and _achieve(chat_id, uid, "Клаводробилка"):
            await _announce_achievement(context, chat_id, uid, "Клаводробилка")
        if CHAR_COUNT[chat_id].get(uid, 0) >= 20000 and _achieve(chat_id, uid, "Словесный понос"):
            await _announce_achievement(context, chat_id, uid, "Словесный понос")
        if MSG_COUNT[chat_id].get(uid, 0) >= 100 and _achieve(chat_id, uid, "Писарь-маховик"):
            await _announce_achievement(context, chat_id, uid, "Писарь-маховик")
        if MSG_COUNT[chat_id].get(uid, 0) >= 300 and _achieve(chat_id, uid, "Флудераст"):
            await _announce_achievement(context, chat_id, uid, "Флудераст")

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

        ok, secs_left = _within_limit_and_mark(chat_id, giver.id)
        if not ok:
            mins = (secs_left or 60) // 60
            await msg.reply_text(f"Лимит репутации исчерпан на 24ч (10/10). Попробуй через ~{mins} мин.")
            return

        if is_plus and target_id == giver.id:
            if _achieve(chat_id, giver.id, "Читер ёбаный"):
                await msg.reply_text("«Читер ёбаный» 🏅 — за попытку +1 себе. Нельзя!")
            else:
                await msg.reply_text("Нельзя +1 себе, хорош мухлевать 🐍")
            return

        delta = 1 if is_plus else -1
        async with STATE_LOCK:
            _inc(REP_RECEIVED[chat_id], target_id, by=delta)
            _inc(REP_GIVEN[chat_id], giver.id, by=delta)
            if delta > 0:
                _inc(REP_POS_GIVEN[chat_id], giver.id)
            else:
                _inc(REP_NEG_GIVEN[chat_id], giver.id)

        try:
            if await _is_admin(chat_id, target_id, context):
                if delta > 0:
                    _inc(ADMIN_PLUS_GIVEN[chat_id], giver.id)
                    if ADMIN_PLUS_GIVEN[chat_id].get(giver.id, 0) >= 5 and _achieve(chat_id, giver.id, "Подхалим генеральский"):
                        await _announce_achievement(context, chat_id, giver.id, "Подхалим генеральский")
                else:
                    _inc(ADMIN_MINUS_GIVEN[chat_id], giver.id)
                    if ADMIN_MINUS_GIVEN[chat_id].get(giver.id, 0) >= 3 and _achieve(chat_id, giver.id, "Ужалил короля"):
                        await _announce_achievement(context, chat_id, giver.id, "Ужалил короля")
        except Exception:
            pass

        total = REP_RECEIVED[chat_id].get(target_id, 0)
        if total >= 20 and _achieve(chat_id, target_id, "Любимчик, сука"):
            await _announce_achievement(context, chat_id, target_id, "Любимчик, сука")
        if total <= -10 and _achieve(chat_id, target_id, "Токсик-магнит"):
            await _announce_achievement(context, chat_id, target_id, "Токсик-магнит")
        if total >= 50 and _achieve(chat_id, target_id, "Крутой чел"):
            await _announce_achievement(context, chat_id, target_id, "Крутой чел")
        if total <= -20 and _achieve(chat_id, target_id, "Опущенный"):
            await _announce_achievement(context, chat_id, target_id, "Опущенный")

        total_gives = REP_POS_GIVEN[chat_id].get(giver.id, 0) + REP_NEG_GIVEN[chat_id].get(giver.id, 0)
        if total_gives >= 20 and _achieve(chat_id, giver.id, "Заводила-плюсовик"):
            await _announce_achievement(context, chat_id, giver.id, "Заводила-плюсовик")
        if REP_POS_GIVEN[chat_id].get(giver.id, 0) >= 10 and _achieve(chat_id, giver.id, "Щедрый засранец"):
            await _announce_achievement(context, chat_id, giver.id, "Щедрый засранец")
        if REP_NEG_GIVEN[chat_id].get(giver.id, 0) >= 10 and _achieve(chat_id, giver.id, "Минусатор-маньяк"):
            await _announce_achievement(context, chat_id, giver.id, "Минусатор-маньяк")

        sign = "+" if delta > 0 else "-"
        await msg.reply_text(f"{_name_or_id(target_id)} получает {sign}1. Текущая репа: {total}")
        return

    # === 3) Триггеры ===
    _ensure_triggers_migrated(chat_id)
    compiled = TRIGGERS_COMPILED.get(chat_id, [])
    for idx, (pat, answers, name, enabled) in enumerate(compiled):
        if not enabled or not pat:
            continue
        if pat.search(t) and _trigger_allowed(chat_id):
            await msg.reply_text(random.choice(answers))
            _inc(TRIGGER_HITS[chat_id], uid)
            # спец-учёт "пива" по id триггера
            try:
                trig_id = (TRIGGERS_CFG.get(chat_id) or [])[idx].get("id","")
                if trig_id == "beer":
                    _inc(BEER_HITS[chat_id], uid)
                    if BEER_HITS[chat_id].get(uid, 0) >= 5 and _achieve(chat_id, uid, "Пивной сомелье-алкаш"):
                        await _announce_achievement(context, chat_id, uid, "Пивной сомелье-алкаш")
                    if BEER_HITS[chat_id].get(uid, 0) >= 20 and _achieve(chat_id, uid, "Пивозавр"):
                        await _announce_achievement(context, chat_id, uid, "Пивозавр")
            except Exception:
                pass
            if TRIGGER_HITS[chat_id].get(uid, 0) >= 15 and _achieve(chat_id, uid, "Триггер-мейкер"):
                await _announce_achievement(context, chat_id, uid, "Триггер-мейкер")
            break

    # === 4) NSFW ===
    if NSFW_RX.search(t):
        if _achieve(chat_id, uid, "Сортирный поэт"):
            await _announce_achievement(context, chat_id, uid, "Сортирный поэт")

# ========= ЭКСПОРТ / ИМПОРТ =========
async def _ensure_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        return True
    member = await context.bot.get_chat_member(chat_id, user_id)
    return member.status in ("administrator", "creator")

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_private(update):
        if not await _guard_private_access(update, context):
            return
    if not await _ensure_admin(update, context):
        await update.message.reply_text("Только админ может делать экспорт 🚫")
        return
    data = _serialize_state()
    fname = f"export_all.json"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        await update.message.reply_document(InputFile(fname))
    finally:
        try: os.remove(fname)
        except Exception: pass

async def cmd_export_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_private(update):
        if not await _guard_private_access(update, context):
            return
    if not await _ensure_admin(update, context):
        await update.message.reply_text("Только админ может делать экспорт 🚫")
        return
    chat_id = update.effective_chat.id
    snapshot = _serialize_state()

    def only_chat(section):
        return {str(chat_id): section.get(str(chat_id), {})}

    slim = {
        "ALLOW_CHATS": list(ALLOW_CHATS),
        "CHAT_TITLES": {str(chat_id): CHAT_TITLES.get(chat_id, str(chat_id))},
        "NICKS": {str(chat_id): snapshot["NICKS"].get(str(chat_id), {})},
        "TAKEN": {str(chat_id): snapshot["TAKEN"].get(str(chat_id), [])},
        "LAST_NICK": snapshot["LAST_NICK"],
        "KNOWN": snapshot["KNOWN"],
        "NAMES": snapshot["NAMES"],
        "REP_GIVEN": only_chat(snapshot["REP_GIVEN"]),
        "REP_RECEIVED": only_chat(snapshot["REP_RECEIVED"]),
        "REP_POS_GIVEN": only_chat(snapshot["REP_POS_GIVEN"]),
        "REP_NEG_GIVEN": only_chat(snapshot["REP_NEG_GIVEN"]),
        "REP_GIVE_TIMES": only_chat(snapshot["REP_GIVE_TIMES"]),
        "MSG_COUNT": only_chat(snapshot["MSG_COUNT"]),
        "CHAR_COUNT": only_chat(snapshot["CHAR_COUNT"]),
        "NICK_CHANGE_COUNT": only_chat(snapshot["NICK_CHANGE_COUNT"]),
        "EIGHTBALL_COUNT": only_chat(snapshot["EIGHTBALL_COUNT"]),
        "TRIGGER_HITS": only_chat(snapshot["TRIGGER_HITS"]),
        "BEER_HITS": only_chat(snapshot["BEER_HITS"]),
        "LAST_MSG_AT": only_chat(snapshot["LAST_MSG_AT"]),
        "ADMIN_PLUS_GIVEN": only_chat(snapshot["ADMIN_PLUS_GIVEN"]),
        "ADMIN_MINUS_GIVEN": only_chat(snapshot["ADMIN_MINUS_GIVEN"]),
        "ACHIEVEMENTS": only_chat(snapshot["ACHIEVEMENTS"]),
        "TRIGGERS_CFG": {str(chat_id): TRIGGERS_CFG.get(chat_id, [])},
    }

    fname = f"export_chat_{chat_id}.json"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(slim, f, ensure_ascii=False, indent=2)
        await update.message.reply_document(InputFile(fname))
    finally:
        try: os.remove(fname)
        except Exception: pass

async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_private(update):
        if not await _guard_private_access(update, context):
            return
    if not update.message or not update.message.document:
        await update.message.reply_text("Прикрепи JSON-файл с экспортом.")
        return
    if not await _ensure_admin(update, context):
        await update.message.reply_text("Только админ может делать импорт 🚫")
        return
    chat_id = update.effective_chat.id
    try:
        file = await context.bot.get_file(update.message.document)
        path = f"import_{chat_id}.json"
        await file.download_to_drive(path)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        async with STATE_LOCK:
            _apply_state(data, target_chat_id=chat_id, only_this_chat=True)
            # перекомпилируем триггеры для этого чата
            _ensure_triggers_migrated(chat_id)
            _build_compiled_triggers_for_chat(chat_id)
            await cloud_save()
        await update.message.reply_text("Импорт завершён ✅ (только текущий чат)")
    except json.JSONDecodeError:
        await update.message.reply_text("Файл не похож на валидный JSON ❌")
    except Exception as e:
        await update.message.reply_text(f"Не удалось импортировать: {type(e).__name__}")
    finally:
        try: os.remove(path)
        except Exception: pass

# ========= RESET (АДМИН) =========
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_private(update):
        if not await _guard_private_access(update, context):
            return
    if not await _ensure_admin(update, context):
        await update.message.reply_text("Только админ может сбрасывать историю 🚫")
        return
    chat_id = update.effective_chat.id
    _ensure_chat(chat_id)
    async with STATE_LOCK:
        _clear_chat(chat_id)
        await cloud_save()
    await update.message.reply_text("🔄 История этого чата сброшена админом. Всё начинается заново!")

async def cmd_resetuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_private(update):
        if not await _guard_private_access(update, context):
            return
    if not await _ensure_admin(update, context):
        await update.message.reply_text("Только админ может сбрасывать пользователей 🚫")
        return
    if not update.message:
        return
    chat_id = update.effective_chat.id
    _ensure_chat(chat_id)

    target_id: Optional[int] = None
    target_name: Optional[str] = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        u = update.message.reply_to_message.from_user
        target_id = u.id
        target_name = _display_name(u)
        await _remember_user(u)
    else:
        parts = (update.message.text or "").split()
        if len(parts) >= 2 and parts[1].startswith("@"):
            uid = KNOWN.get(parts[1][1:].lower())
            if uid:
                target_id = uid
                target_name = parts[1]

    if target_id is None:
        await update.message.reply_text("Укажи кого чистим: ответь на его сообщение или `/resetuser @username`", parse_mode="Markdown")
        return

    async with STATE_LOCK:
        _clear_user_in_chat(chat_id, target_id)
        await cloud_save()
    await update.message.reply_text(f"🔄 Данные пользователя {target_name or _name_or_id(target_id)} очищены админом.")

# ========= ALLOWLIST (ТОЛЬКО OWNER) =========
async def cmd_allowchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    chat_id = update.effective_chat.id
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Эту команду нужно вызвать в группе, которую хочешь разрешить.")
        return
    _ensure_chat(chat_id)
    ALLOW_CHATS.add(chat_id)
    CHAT_TITLES[chat_id] = update.effective_chat.title or str(chat_id)
    async with STATE_LOCK:
        await cloud_save()
    await update.message.reply_text("✅ Чат добавлен в список разрешённых. Бот активирован.")

async def cmd_denychat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    chat_id = update.effective_chat.id
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Эту команду нужно вызвать в группе, которую хочешь запретить.")
        return
    if chat_id in ALLOW_CHATS:
        ALLOW_CHATS.discard(chat_id)
    async with STATE_LOCK:
        _clear_chat(chat_id)
        TRIGGERS_CFG.pop(chat_id, None)
        TRIGGERS_COMPILED.pop(chat_id, None)
        CHAT_TITLES.pop(chat_id, None)
        await cloud_save()
    await update.message.reply_text("❌ Чат удалён из разрешённых и полностью очищен. Выходим…")
    try:
        await context.bot.leave_chat(chat_id)
    except Exception:
        pass

async def cmd_listchats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_private(update) and update.effective_user.id != OWNER_ID:
        return
    if update.effective_user.id != OWNER_ID:
        return
    if not ALLOW_CHATS:
        await update.message.reply_text("Список разрешённых чатов пуст.")
        return
    lines = []
    for cid in sorted(ALLOW_CHATS):
        title = CHAT_TITLES.get(cid, str(cid))
        lines.append(f"• {title} — `{cid}`")
    await update.message.reply_text("Разрешённые чаты:\n" + "\n".join(lines), parse_mode="Markdown")

# ========= АДМИН-МЕНЮ / ДМ =========
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа: в группе — сообщает и открывает ЛС; в ЛС — показывает список команд."""
    if update.effective_chat.type in ("group","supergroup"):
        if not await _ensure_admin(update, context):
            await update.message.reply_text("Только админ может открыть админ-меню 🚫")
            return
        # Запомним текущий "целевой" чат для админа (сессия по uid)
        uid = update.effective_user.id
        ADMIN_SESS[uid] = {"chat_id": update.effective_chat.id, "step": None, "new_trigger": None}
        try:
            await context.bot.send_message(uid, f"🔧 Админ-меню для чата: {CHAT_TITLES.get(update.effective_chat.id, update.effective_chat.id)}\n\n{ADMIN_MENU_TEXT}")
        except Exception:
            await update.message.reply_text("Напиши мне в ЛС сначала: открой диалог с ботом и отправь /start.")
            return
        await update.message.reply_text("Открыл админ-меню в ЛС. Если не пришло — напиши боту в личку /start.")
    else:
        # ЛС
        uid = update.effective_user.id
        sess = ADMIN_SESS.get(uid)
        if not sess or "chat_id" not in sess:
            await update.message.reply_text("Сначала вызови /admin в нужном групповом чате, чтобы я знал, чем управлять.")
            return
        await update.message.reply_text(ADMIN_MENU_TEXT)

def _get_admin_target_chat(user_id: int) -> Optional[int]:
    sess = ADMIN_SESS.get(user_id)
    return sess.get("chat_id") if sess else None

# ========= ТРИГГЕРЫ: СПИСОК/ТОГГЛ/УДАЛЕНИЕ =========
def _format_triggers_list(chat_id: int) -> str:
    _ensure_triggers_migrated(chat_id)
    items = TRIGGERS_CFG.get(chat_id) or []
    if not items:
        return "Список триггеров пуст."
    lines = []
    for i, t in enumerate(items, start=1):
        status = "✅" if t.get("enabled", True) else "🚫"
        lines.append(f"{i}. {status} {t.get('name','(без имени)')} — ответов: {len(t.get('answers',[]))}")
    return "Триггеры чата:\n" + "\n".join(lines)

async def cmd_triggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private(update):
        await update.message.reply_text("Эта команда работает в ЛС. Сначала вызови /admin в нужном чате.")
        return
    uid = update.effective_user.id
    chat_id = _get_admin_target_chat(uid)
    if not chat_id:
        await update.message.reply_text("Не знаю, для какого чата управлять. Вызови /admin в группе.")
        return
    if not await _is_admin(chat_id, uid, context):
        await update.message.reply_text("Только админ этого чата может управлять триггерами.")
        return
    await update.message.reply_text(_format_triggers_list(chat_id))

async def cmd_triggers_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private(update):
        await update.message.reply_text("Эта команда работает в ЛС. Сначала вызови /admin в чате.")
        return
    uid = update.effective_user.id
    chat_id = _get_admin_target_chat(uid)
    if not chat_id:
        await update.message.reply_text("Сначала /admin в группе.")
        return
    if not await _is_admin(chat_id, uid, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    try:
        idx = int(context.args[0]) - 1
    except Exception:
        await update.message.reply_text("Укажи номер: /triggers_toggle 2")
        return
    items = TRIGGERS_CFG.get(chat_id) or []
    if not (0 <= idx < len(items)):
        await update.message.reply_text("Неверный номер.")
        return
    items[idx]["enabled"] = not items[idx].get("enabled", True)
    _build_compiled_triggers_for_chat(chat_id)
    async with STATE_LOCK:
        await cloud_save()
    st = "включён" if items[idx]["enabled"] else "выключен"
    await update.message.reply_text(f"Триггер «{items[idx].get('name','')}» {st}.")

async def cmd_triggers_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private(update):
        await update.message.reply_text("Эта команда работает в ЛС. Сначала вызови /admin в чате.")
        return
    uid = update.effective_user.id
    chat_id = _get_admin_target_chat(uid)
    if not chat_id:
        await update.message.reply_text("Сначала /admin в группе.")
        return
    if not await _is_admin(chat_id, uid, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    try:
        idx = int(context.args[0]) - 1
    except Exception:
        await update.message.reply_text("Укажи номер: /triggers_del 3")
        return
    items = TRIGGERS_CFG.get(chat_id) or []
    if not (0 <= idx < len(items)):
        await update.message.reply_text("Неверный номер.")
        return
    name = items[idx].get("name","")
    del items[idx]
    _build_compiled_triggers_for_chat(chat_id)
    async with STATE_LOCK:
        await cloud_save()
    await update.message.reply_text(f"Триггер «{name}» удалён.")

# ========= ДОБАВЛЕНИЕ ТРИГГЕРА — МАСТЕР В ЛС =========
def _reset_admin_wizard(uid: int):
    ADMIN_SESS[uid] = {"chat_id": ADMIN_SESS.get(uid, {}).get("chat_id"), "step": None, "new_trigger": None}

async def cmd_triggers_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private(update):
        await update.message.reply_text("Эта команда работает в ЛС. Сначала вызови /admin в нужном чате.")
        return
    uid = update.effective_user.id
    chat_id = _get_admin_target_chat(uid)
    if not chat_id:
        await update.message.reply_text("Не знаю, для какого чата управлять. Вызови /admin в группе.")
        return
    if not await _is_admin(chat_id, uid, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    # старт мастера
    ADMIN_SESS[uid] = {
        "chat_id": chat_id,
        "step": "type",
        "new_trigger": {"id": f"custom_{int(datetime.now().timestamp())}", "name": "Новый триггер",
                        "pattern": "", "is_regex": False, "ignore_case": True,
                        "word_boundaries": False, "answers": [], "enabled": True}
    }
    await update.message.reply_text(
        "Добавление триггера:\n"
        "Выбери тип:\n"
        "1 — Простая фраза\n"
        "2 — Регэксп\n"
        "(ответь цифрой 1 или 2)"
    )

async def _admin_dm_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает шаги мастера добавления/редактирования триггера в ЛС."""
    if not _is_private(update):
        return
    uid = update.effective_user.id
    sess = ADMIN_SESS.get(uid)
    if not sess or not sess.get("step"):
        return
    txt = (update.message.text or "").strip()
    chat_id = sess.get("chat_id")
    if not chat_id:
        _reset_admin_wizard(uid)
        await update.message.reply_text("Не знаю, для какого чата управлять. Вызови /admin в группе.")
        return

    step = sess["step"]
    new_tr = sess["new_trigger"]

    # Шаг 1: тип
    if step == "type":
        if txt not in ("1","2"):
            await update.message.reply_text("Ответь цифрой 1 (фраза) или 2 (регэксп).")
            return
        new_tr["is_regex"] = (txt == "2")
        sess["step"] = "name"
        await update.message.reply_text("Введи человекочитаемое имя триггера (например, «Музыка»):")
        return

    # Шаг 2: имя
    if step == "name":
        if len(txt) < 2 or len(txt) > 50:
            await update.message.reply_text("Имя слишком короткое/длинное. Введи от 2 до 50 символов.")
            return
        new_tr["name"] = txt
        sess["step"] = "pattern"
        prompt = "Введи REGEXP-паттерн:" if new_tr["is_regex"] else "Введи фразу для поиска:"
        await update.message.reply_text(prompt)
        return

    # Шаг 3: паттерн/фраза
    if step == "pattern":
        if len(txt) < 1 or len(txt) > 100:
            await update.message.reply_text("Слишком коротко/длинно. Введи до 100 символов.")
            return
        if new_tr["is_regex"]:
            try:
                re.compile(txt, re.IGNORECASE if new_tr["ignore_case"] else 0)
            except Exception as e:
                await update.message.reply_text(f"Невалидный регэксп: {e.__class__.__name__}")
                return
            new_tr["pattern"] = txt
        else:
            new_tr["pattern"] = txt
        sess["step"] = "options"
        await update.message.reply_text(
            "Опции (введи цифры подряд, либо 0 чтобы пропустить):\n"
            "1 — Игнорировать регистр (по умолчанию ВКЛ)\n"
            "2 — Только целое слово (границы слова) — работает для простой фразы\n"
            "Пример: 12 — включит обе опции; 0 — оставить по умолчанию"
        )
        return

    # Шаг 4: опции
    if step == "options":
        flags = set(txt.strip())
        if flags and flags != {"0"}:
            new_tr["ignore_case"] = ("1" in flags)
            if not new_tr["is_regex"]:
                new_tr["word_boundaries"] = ("2" in flags)
        sess["step"] = "answers"
        new_tr["answers"] = []
        await update.message.reply_text(
            "Теперь пришли 1–3 ответа (каждый отдельным сообщением). Когда закончишь — напиши «Готово»."
        )
        return

    # Шаг 5: ответы
    if step == "answers":
        if txt.lower() in ("готово","готово.","готово!","done","finish"):
            if not new_tr["answers"]:
                await update.message.reply_text("Нужен хотя бы один ответ. Пришли текст ответа.")
                return
            # сохранить
            TRIGGERS_CFG.setdefault(chat_id, [])
            TRIGGERS_CFG[chat_id].append(TriggerCfg(dict(new_tr)))
            _build_compiled_triggers_for_chat(chat_id)
            async with STATE_LOCK:
                await cloud_save()
            await update.message.reply_text(f"✅ Триггер «{new_tr['name']}» добавлен.\n\n" + _format_triggers_list(chat_id))
            _reset_admin_wizard(uid)
            return
        # добавляем ответ
        if len(txt) > 140:
            await update.message.reply_text("Ответ слишком длинный (макс 140 символов). Пришли короче.")
            return
        new_tr["answers"].append(txt)
        if len(new_tr["answers"]) >= 3:
            TRIGGERS_CFG.setdefault(chat_id, [])
            TRIGGERS_CFG[chat_id].append(TriggerCfg(dict(new_tr)))
            _build_compiled_triggers_for_chat(chat_id)
            async with STATE_LOCK:
                await cloud_save()
            await update.message.reply_text(f"✅ Триггер «{new_tr['name']}» добавлен (3 ответа).\n\n" + _format_triggers_list(chat_id))
            _reset_admin_wizard(uid)
            return
        await update.message.reply_text(f"Ответ добавлен ({len(new_tr['answers'])}). Пришли ещё или напиши «Готово».")
        return

# ========= СБРОС ТРИГГЕРОВ К ДЕФОЛТУ =========
async def cmd_resettriggers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # только админ, только в ЛС через выбранный чат
    if not _is_private(update):
        await update.message.reply_text("Эта команда работает в ЛС. Сначала вызови /admin в чате.")
        return
    uid = update.effective_user.id
    chat_id = _get_admin_target_chat(uid)
    if not chat_id:
        await update.message.reply_text("Сначала /admin в группе — выбрать чат.")
        return
    if not await _is_admin(chat_id, uid, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    TRIGGERS_CFG[chat_id] = [TriggerCfg(dict(t)) for t in DEFAULT_TRIGGERS]
    _build_compiled_triggers_for_chat(chat_id)
    async with STATE_LOCK:
        await cloud_save()
    await update.message.reply_text("🔄 Триггеры сброшены к стандартным.\n\n" + _format_triggers_list(chat_id))
    try:
        await context.bot.send_message(chat_id, "ℹ️ Админ сбросил триггеры чата к стандартным.")
    except Exception:
        pass

# ========= TESTTRIGGER / DIAG (ЛС админа) =========
async def cmd_testtrigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private(update):
        await update.message.reply_text("Команда работает в ЛС.")
        return
    uid = update.effective_user.id
    chat_id = _get_admin_target_chat(uid)
    if not chat_id:
        await update.message.reply_text("Сначала /admin в группе — выбрать чат.")
        return
    if not await _is_admin(chat_id, uid, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    sample = " ".join(context.args).strip()
    if not sample:
        await update.message.reply_text("Пример: /testtrigger текст для проверки")
        return
    _ensure_triggers_migrated(chat_id)
    compiled = TRIGGERS_COMPILED.get(chat_id, [])
    hits = []
    for idx, (pat, answers, name, enabled) in enumerate(compiled):
        if enabled and pat and pat.search(sample):
            # какой ответ бы выдал
            ans = answers[0] if answers else "(нет ответов)"
            hits.append(f"• {name} → «{ans}»")
    if not hits:
        await update.message.reply_text("Ни один триггер не сработал.")
    else:
        await update.message.reply_text("Сработали бы:\n" + "\n".join(hits))

_start_time = datetime.now(UTC)
_last_keepalive_ok = None
_last_save_time = None

async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private(update):
        await update.message.reply_text("Команда работает в ЛС.")
        return
    uid = update.effective_user.id
    chat_id = _get_admin_target_chat(uid)
    if not chat_id:
        await update.message.reply_text("Сначала /admin в группе — выбрать чат.")
        return
    if not await _is_admin(chat_id, uid, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    # оценим размер состояния
    try:
        st_text = json.dumps(_serialize_state(), ensure_ascii=False)
        kb = len(st_text.encode("utf-8")) // 1024
    except Exception:
        kb = -1
    uptime = datetime.now(UTC) - _start_time
    lines = [
        f"Чат: {CHAT_TITLES.get(chat_id, chat_id)}",
        f"Активных триггеров: {sum(1 for t in (TRIGGERS_CFG.get(chat_id) or []) if t.get('enabled', True))} / {len(TRIGGERS_CFG.get(chat_id) or [])}",
        f"Последний автосейв: {_last_save_time.isoformat() if _last_save_time else '—'}",
        f"Размер состояния: ~{kb} KB",
        f"Uptime процесса: {uptime}",
        f"Последний keepalive: {_last_keepalive_ok if _last_keepalive_ok is not None else '—'}",
    ]
    await update.message.reply_text("🛠 Диагностика:\n" + "\n".join(lines))

# ========= HEALTH JOBS =========
async def periodic_save_job(context: ContextTypes.DEFAULT_TYPE):
    global _last_save_time
    async with STATE_LOCK:
        await cloud_save()
        _last_save_time = datetime.now(UTC)

async def keepalive_job(context: ContextTypes.DEFAULT_TYPE):
    global _last_keepalive_ok
    if not SELF_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(SELF_URL)
            _last_keepalive_ok = (r.status_code, r.text[:50])
    except Exception as e:
        _last_keepalive_ok = f"error: {e.__class__.__name__}"

# ========= ПОДПИСКИ НА СОБЫТИЯ ЧЛЕНСТВА (авто-выход) =========
from telegram.ext import ChatMemberHandler

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Если бота добавили в неразрешённый чат — мягко выходим."""
    mc = update.my_chat_member
    if not mc or not mc.chat:
        return
    chat = mc.chat
    # интересует статус "member"/"administrator" из "kicked"/"left"
    new_status = mc.new_chat_member.status if mc.new_chat_member else None
    if chat.type in ("group","supergroup") and new_status in ("member","administrator"):
        if not _allowed_chat(chat.id):
            try:
                await context.bot.send_message(chat.id, "🚫 Этот чат не авторизован для использования бота.")
            except Exception:
                pass
            try:
                await context.bot.leave_chat(chat.id)
            except Exception:
                pass

# ========= ENTRY =========
async def _pre_init(app: Application):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    await cloud_load_if_any()

def main():
    # Flask
    threading.Thread(target=run_flask, daemon=True).start()

    req = HTTPXRequest(connect_timeout=8.0, read_timeout=20.0, pool_timeout=5.0)
    application: Application = (
        ApplicationBuilder()
        .token(API_TOKEN)
        .get_updates_request(req)
        .post_init(_pre_init)
        .build()
    )

    # Команды для всех
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help",  cmd_help))
    application.add_handler(CommandHandler("nick",  cmd_nick))
    application.add_handler(CommandHandler("8ball", cmd_8ball))

    # Статистика
    application.add_handler(CallbackQueryHandler(on_button))

    # Экспорт/импорт
    application.add_handler(CommandHandler("export", cmd_export))
    application.add_handler(CommandHandler("export_here", cmd_export_here))
    application.add_handler(CommandHandler("import", cmd_import))

    # Сбросы
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(CommandHandler("resetuser", cmd_resetuser))

    # Allowlist (только OWNER)
    application.add_handler(CommandHandler("allowchat", cmd_allowchat))
    application.add_handler(CommandHandler("denychat", cmd_denychat))
    application.add_handler(CommandHandler("listchats", cmd_listchats))

    # Админ-меню и триггеры (в ЛС)
    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("triggers", cmd_triggers))
    application.add_handler(CommandHandler("triggers_toggle", cmd_triggers_toggle))
    application.add_handler(CommandHandler("triggers_del", cmd_triggers_del))
    application.add_handler(CommandHandler("triggers_add", cmd_triggers_add))
    application.add_handler(CommandHandler("resettriggers", cmd_resettriggers))
    application.add_handler(CommandHandler("testtrigger", cmd_testtrigger))
    application.add_handler(CommandHandler("diag", cmd_diag))

    # DM-мастер (после /triggers_add)
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, _admin_dm_message_router))

    # Единый обработчик текстов (не команд) — группы
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Авто-выход из неразрешённых чатов по событиям
    application.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # JobQueue: автосейв и keep-alive
    jq = application.job_queue
    if jq is not None:
        jq.run_repeating(periodic_save_job, interval=300, first=120)   # каждые 5 мин
        jq.run_repeating(keepalive_job,     interval=240, first=60)    # каждые 4 мин

    from telegram import Update as TgUpdate
    application.run_polling(
        allowed_updates=TgUpdate.ALL_TYPES,
        timeout=20,
        poll_interval=1.0,
        drop_pending_updates=True,
        close_loop=False,
    )

if __name__ == "__main__":
    main()
