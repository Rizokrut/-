import asyncio
import html
import logging
import os
import re
import time
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import database as db
from config import ADMIN_ID, BOT_TOKEN, CHANNEL_ID, CHANNEL_URL

TZ = ZoneInfo("Asia/Tashkent")
NIGHT_START = 2   # 02:00
NIGHT_END = 7     # 07:00
NIGHT_COOLDOWN_SEC = 30 * 60  # 30 минут в ночном режиме
NICK_MIN = 2
NICK_MAX = 20
NICK_CHANGE_COOLDOWN_SEC = 24 * 3600  # ник раз в сутки

# варианты кулдауна (минуты)
COOLDOWN_OPTIONS = (1, 5, 10, 15, 20)
# варианты ручного мута (минуты); 0 = навсегда (до снятия)
MUTE_OPTIONS = (5, 15, 30, 60, 180, 0)


def channel_chat_id():
    raw = str(CHANNEL_ID or "").strip()
    if raw.startswith("-") and raw[1:].isdigit():
        return int(raw)
    if raw.isdigit():
        return int(raw)
    return raw


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

MENU = {"💬 Сплетни", "📜 Правила", "↩️ Ответить", "🎭 Ник", "👤 Админ"}
ADMIN_MENU = {
    "📊 Статистика",
    "⏱ Кулдаун",
    "🌙 Ночной режим",
    "🔇 Снять мут",
    "🚫 Дать мут",
    "📢 Рассылка",
    "👥 Список админов",
    "➕ Добавить админа",
    "➖ Удалить админа",
    "💾 Экспорт БД",
    "📥 Импорт БД",
    "↩️ Назад",
}
ALL_BUTTONS = MENU | ADMIN_MENU
ADMIN_TG = "https://t.me/gubkinhelp"

# ссылки / кликабельное
LINK_RE = re.compile(
    r"("
    r"https?://|"
    r"www\.|"
    r"t\.me/|"
    r"telegram\.me/|"
    r"tg://|"
    r"@[\w\d_]{4,}|"
    r"(?:[\w-]+\.)+(?:com|ru|net|org|io|me|xyz|info|pro|tv|cc|app|dev|site|online|link|click|top|live|shop|store|blog|page|tech|cloud|ai|uz|kz|by|ua|su)"
    r")",
    re.IGNORECASE,
)
TG_POST_RE = re.compile(
    r"(https?://)?(t\.me|telegram\.me)/(c/\d+/|(?P<user>[A-Za-z0-9_]+)/)(?P<mid>\d+)",
    re.IGNORECASE,
)
# ник: буквы (лат/кир), цифры, _ - пробел; без ссылок
# только буквы и цифры (лат/кир), без пробелов, _ и -
NICK_RE = re.compile(r"^[^\W_]{2,20}$", re.UNICODE)

pending_media: dict[str, dict] = {}
_counter = {"n": 0, "msg_id": None}


class Flow(StatesGroup):
    reply_wait_fwd = State()
    reply_wait_text = State()
    admin_unmute = State()
    admin_mute_pick = State()
    admin_mute_duration = State()
    admin_add = State()
    admin_remove = State()
    admin_import_db = State()
    admin_broadcast = State()
    set_nick = State()


# ─── клавиатуры ───────────────────────────────────────────────────────────────

def menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💬 Сплетни"), KeyboardButton(text="📜 Правила")],
            [KeyboardButton(text="↩️ Ответить"), KeyboardButton(text="🎭 Ник")],
            [KeyboardButton(text="👤 Админ")],
        ],
        resize_keyboard=True,
    )


def admin_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="⏱ Кулдаун")],
            [KeyboardButton(text="🌙 Ночной режим"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="🚫 Дать мут"), KeyboardButton(text="🔇 Снять мут")],
            [KeyboardButton(text="👥 Список админов")],
            [KeyboardButton(text="➕ Добавить админа"), KeyboardButton(text="➖ Удалить админа")],
            [KeyboardButton(text="💾 Экспорт БД"), KeyboardButton(text="📥 Импорт БД")],
            [KeyboardButton(text="↩️ Назад")],
        ],
        resize_keyboard=True,
    )


def sub_kb() -> InlineKeyboardMarkup:
    rows = []
    if CHANNEL_URL:
        rows.append([InlineKeyboardButton(text="Подписаться", url=CHANNEL_URL)])
    rows.append(
        [InlineKeyboardButton(text="Проверить подписку", callback_data="chk")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _dispatch_menu(message: Message, state: FSMContext) -> None:
    """Повторно обработать кнопку меню после выхода из FSM-состояния."""
    text = message.text or ""
    mapping = {
        "💬 Сплетни": gossip_hint,
        "📜 Правила": rules,
        "↩️ Ответить": reply_start,
        "🎭 Ник": nick_start,
        "👤 Админ": admin_link,
        "📊 Статистика": admin_stats,
        "⏱ Кулдаун": admin_cooldown,
        "🌙 Ночной режим": admin_night,
        "🔇 Снять мут": admin_unmute_start,
        "🚫 Дать мут": admin_mute_start,
        "📢 Рассылка": admin_broadcast_start,
        "👥 Список админов": admin_list,
        "➕ Добавить админа": admin_add_start,
        "➖ Удалить админа": admin_remove_start,
        "💾 Экспорт БД": export_db,
        "📥 Импорт БД": import_db_start,
        "↩️ Назад": admin_back,
    }
    handler = mapping.get(text)
    if handler:
        await handler(message, state)


def cooldown_kb(current: int) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for m in COOLDOWN_OPTIONS:
        mark = " ✅" if m == current else ""
        row.append(
            InlineKeyboardButton(
                text=f"{m} мин{mark}",
                callback_data=f"cd:{m}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def night_kb(enabled: bool) -> InlineKeyboardMarkup:
    label = "Выключить 🌙" if enabled else "Включить 🌙"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data="night:toggle")]
        ]
    )


def mute_duration_kb(uid: int) -> InlineKeyboardMarkup:
    rows = []
    row = []
    labels = {
        5: "5 мин",
        15: "15 мин",
        30: "30 мин",
        60: "1 ч",
        180: "3 ч",
        0: "∞ навсегда",
    }
    for m in MUTE_OPTIONS:
        row.append(
            InlineKeyboardButton(
                text=labels.get(m, f"{m} мин"),
                callback_data=f"mute:{uid}:{m}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(text="Отмена", callback_data="mute:cancel")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─── форматирование / фильтры ─────────────────────────────────────────────────

def format_post(text: str, number: int | str, nick: str | None = None) -> str:
    body = html.escape((text or "").strip())
    if nick:
        sign = f"<b>(c) {html.escape(nick)}</b>"
    else:
        sign = f"<b>№{number}</b>"
    if body:
        return f"{body}\n{sign}"
    return sign


def split_post_link(text: str) -> tuple[int | None, str]:
    raw = (text or "").strip()
    if not raw:
        return None, ""
    match = TG_POST_RE.search(raw)
    if not match:
        return None, raw
    mid = int(match.group("mid"))
    rest = (raw[: match.start()] + raw[match.end() :]).strip()
    return mid, rest


def has_forbidden_links(text: str, allow_post_link: bool = False) -> bool:
    """Проверяет кликабельное/ссылки. allow_post_link — одна ссылка на пост канала ок."""
    if not text:
        return False
    cleaned = text
    if allow_post_link:
        cleaned = TG_POST_RE.sub("", cleaned)
    return bool(LINK_RE.search(cleaned))


# entity-типы, которые делают текст кликабельным
_FORBIDDEN_ENTITIES = {
    "url",
    "text_link",
    "mention",
    "text_mention",
    "email",
    "phone_number",
}


def message_has_forbidden_entities(message: Message, allow_post_link: bool = False) -> bool:
    """Блокирует кликабельные entities Telegram (даже без сырого URL в тексте)."""
    entities = list(message.entities or []) + list(message.caption_entities or [])
    if not entities:
        return False
    text = message.text or message.caption or ""
    for ent in entities:
        et = getattr(ent, "type", None)
        et_s = et.value if hasattr(et, "value") else str(et)
        if et_s not in _FORBIDDEN_ENTITIES:
            continue
        if allow_post_link and et_s in ("url", "text_link"):
            try:
                chunk = text[ent.offset : ent.offset + ent.length]
            except Exception:
                chunk = ""
            url = getattr(ent, "url", None) or chunk
            if TG_POST_RE.search(str(url or "")):
                continue  # разрешённая ссылка на пост канала
        return True
    return False


def validate_nick(raw: str) -> str | None:
    """Вернёт очищенный ник или None если невалидный."""
    nick = (raw or "").strip()
    if len(nick) < NICK_MIN or len(nick) > NICK_MAX:
        return None
    if not NICK_RE.match(nick):
        return None
    if has_forbidden_links(nick):
        return None
    return nick


def nick_delete_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить ник", callback_data="nick:del")]
        ]
    )


def _nick_age_sec(user: dict | None) -> float | None:
    """Секунд с последней смены ника (на другой), или None."""
    if not user or not user.get("nick_changed_at"):
        return None
    try:
        changed = datetime.fromisoformat(
            str(user["nick_changed_at"]).replace(" ", "T")
        )
        return time.time() - changed.replace(tzinfo=None).timestamp()
    except Exception:
        logger.exception("nick age parse")
        return None


def is_night_hours() -> bool:
    now = datetime.now(TZ)
    return NIGHT_START <= now.hour < NIGHT_END


def fmt_left(seconds: float) -> str:
    sec = max(0, int(seconds))
    mins, s = divmod(sec, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours} ч {mins} мин"
    if mins:
        return f"{mins} мин {s} сек"
    return f"{s} сек"


# ─── счётчик постов ───────────────────────────────────────────────────────────

MARKER = re.compile(r"·n:(\d+)·")


def _desc_with_n(desc: str | None, n: int) -> str:
    base = MARKER.sub("", desc or "").strip()
    tag = f"·n:{n}·"
    if not base:
        return tag
    return f"{base}\n{tag}"


async def load_counter(bot: Bot) -> None:
    try:
        chat = await bot.get_chat(channel_chat_id())
        desc = getattr(chat, "description", None) or ""
        match = MARKER.search(desc)
        if match:
            _counter["n"] = int(match.group(1))
            return
    except Exception:
        logger.exception("load_counter")
    _counter["n"] = 0


async def next_number(bot: Bot) -> int:
    _counter["n"] = int(_counter["n"] or 0) + 1
    n = _counter["n"]
    try:
        chat = await bot.get_chat(channel_chat_id())
        desc = getattr(chat, "description", None) or ""
        await bot.set_chat_description(channel_chat_id(), _desc_with_n(desc, n))
    except Exception:
        logger.exception("save_counter")
    return n


# ─── доступ / антифлуд ────────────────────────────────────────────────────────

async def is_member(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(channel_chat_id(), user_id)
        return member.status in (
            "member",
            "administrator",
            "creator",
            "restricted",
        )
    except Exception:
        logger.exception("get_chat_member")
        return False


async def flood_ok(message: Message) -> bool:
    """Кулдаун + ночной режим + ручной мут. Админы без ограничений."""
    uid = message.from_user.id
    try:
        await db.touch_user(uid)
    except Exception:
        logger.exception("touch_user")
    if await db.is_admin(uid):
        return True

    now = time.time()
    rate = await db.get_rate(uid)

    muted = float(rate.get("muted_until") or 0)
    if muted > now:
        left = muted - now
        await message.answer(
            f"🔇 Мут ещё {fmt_left(left)}."
        )
        return False

    last = rate.get("last_sent_at")
    last_f = float(last) if last else 0.0

    # ночной режим (Ташкент 02:00–07:00)
    night_on = await db.get_night_mode()
    if night_on and is_night_hours():
        need = NIGHT_COOLDOWN_SEC
        if last_f and now - last_f < need:
            left = need - (now - last_f)
            await message.answer(
                "🌙 Ночной режим (02:00–07:00, Ташкент).\n"
                f"Куда так быстро, ковбой? Сможешь отправить ещё через: {fmt_left(left)}"
            )
            return False
        await db.set_rate(uid, now, 0, 0)
        return True

    # обычный кулдаун
    cd_min = await db.get_cooldown_min()
    need = max(0, cd_min) * 60
    if need > 0 and last_f and now - last_f < need:
        left = need - (now - last_f)
        await message.answer(
            f"Куда так быстро, ковбой? Сможешь отправить ещё через: {fmt_left(left)}"
        )
        return False

    await db.set_rate(uid, now, 0, 0)
    return True


async def gate(message: Message) -> bool:
    if await db.is_admin(message.from_user.id):
        return True
    if not await is_member(message.bot, message.from_user.id):
        await message.answer(
            "📢 Сначала подпишись на канал со сплетнями.",
            reply_markup=sub_kb(),
        )
        return False
    return True


def _from_our_channel(message: Message) -> bool:
    cid, mid = _forward_channel_id(message)
    return bool(cid and str(cid) == str(channel_chat_id()) and mid)


def _forward_channel_id(message: Message) -> tuple[str | None, int | None]:
    src = message.forward_from_chat
    mid = message.forward_from_message_id
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None)
        if chat is not None:
            src = chat
        mid = getattr(origin, "message_id", mid)
    cid = str(src.id) if src else None
    return cid, int(mid) if mid else None


# ─── хендлеры: старт / подписка / меню ────────────────────────────────────────

@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    try:
        await db.touch_user(message.from_user.id)
    except Exception:
        logger.exception("touch_user start")
    if not await gate(message):
        return
    await message.answer(
        "<b>🤫 Отправляй 100% анонимные сообщения.</b>\n\n"
        "Бот автоматически закинет в канал.\n"
        "Ответ на пост: кинь ссылку на сообщение и текст в одном сообщении.\n"
        "Ник: кнопка «🎭 Ник» — вместо номера будет (c) твой ник.",
        reply_markup=menu_kb(),
    )


@router.callback_query(F.data == "chk")
async def check_sub(callback: CallbackQuery) -> None:
    if await is_member(callback.bot, callback.from_user.id):
        await callback.message.answer("Подписан, свой. /start", reply_markup=menu_kb())
        await callback.answer()
        return
    await callback.answer("Ещё не подписан", show_alert=True)


@router.message(F.text == "📜 Правила")
async def rules(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "📜 <b>Правила</b>\n"
        "1. Запрещено оскорблять студентов, администрацию и преподавательский состав филиала (по возможности)\n"
        "2. Нельзя флудить/спамить\n"
        "3. Запрещена реклама и любые ссылки / упоминания (@)\n"
        "4. Запрещается обращаться к администрации канала, чтобы узнать автора сообщения\n\n"
        "Нарушение этих правил (в особенности 1 и 2) может привести к пожизненному бану."
    )


@router.message(F.text == "↩️ Ответить")
async def reply_start(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    await state.set_state(Flow.reply_wait_fwd)
    await message.answer(
        "↩️ Перешли пост из канала или пришли ссылку на него.\n"
        "Можно сразу: ссылка + текст ответа."
    )


@router.message(Flow.reply_wait_fwd)
async def reply_got_fwd(message: Message, state: FSMContext) -> None:
    # кнопки меню обрабатывают свои хендлеры — только сбрасываем state
    if message.text and message.text in ALL_BUTTONS:
        await state.clear()
        await _dispatch_menu(message, state)
        return
    cid, mid = _forward_channel_id(message)
    if cid and str(cid) == str(channel_chat_id()) and mid:
        await state.update_data(reply_to=mid)
        await state.set_state(Flow.reply_wait_text)
        await message.answer("✍️ Пиши текст ответа.")
        return
    await state.clear()
    await publish_text(message)


@router.message(F.text == "👤 Админ")
async def admin_link(message: Message, state: FSMContext) -> None:
    await state.clear()
    try:
        is_adm = await db.is_admin(message.from_user.id)
    except Exception:
        logger.exception("is_admin")
        is_adm = message.from_user.id == ADMIN_ID

    if is_adm:
        try:
            cd = await db.get_cooldown_min()
            night = await db.get_night_mode()
        except Exception:
            logger.exception("admin settings")
            cd, night = 1, True
        night_s = "вкл" if night else "выкл"
        await message.answer(
            "🛠 <b>Админ-меню</b>\n"
            f"• ⏱ Кулдаун: <b>{cd} мин</b>\n"
            f"• 🌙 Ночной режим: <b>{night_s}</b> (02:00–07:00 Ташкент, раз в 30 мин)\n"
            "• 📢 Рассылка всем, кто писал в канал\n"
            "• 🚫 / 🔇 Дать / снять мут\n"
            "• 👥 / ➕ / ➖ Админы\n"
            "• 💾 / 📥 Экспорт и импорт базы\n"
            "• ↩️ Назад",
            reply_markup=admin_kb(),
        )
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Написать @gubkinhelp", url=ADMIN_TG)]
        ]
    )
    await message.answer("Связь с админом:", reply_markup=kb)


# ─── ник ───────────────────────────────────────────────────────────────────────

@router.message(F.text == "🎭 Ник")
async def nick_start(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    await state.clear()
    current = await db.get_nick(message.from_user.id)
    if current:
        text = (
            f"🎭 Твой ник сейчас: <b>(c) {html.escape(current)}</b>\n\n"
            f"Пришли новый ник ({NICK_MIN}–{NICK_MAX} символов, буквы/цифры).\n"
            "Сменить на другой — раз в сутки."
        )
        await state.set_state(Flow.set_nick)
        await message.answer(text, reply_markup=nick_delete_kb())
        return

    text = (
        "🎭 Ник не задан — в канале показывается номер.\n\n"
        f"Пришли ник ({NICK_MIN}–{NICK_MAX} символов, буквы/цифры).\n"
        "Вместо № будет <b>(c) твой ник</b>."
    )
    await state.set_state(Flow.set_nick)
    await message.answer(text, reply_markup=menu_kb())


@router.callback_query(F.data == "nick:del")
async def nick_delete_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await db.clear_nick(callback.from_user.id)
    await state.clear()
    try:
        await callback.message.edit_text(
            "✅ Ник удалён. Снова будет нумерация.\n"
            "Вернуть можно только тот же ник; другой — через сутки."
        )
    except Exception:
        await callback.message.answer(
            "✅ Ник удалён. Снова будет нумерация.\n"
            "Вернуть можно только тот же ник; другой — через сутки.",
            reply_markup=menu_kb(),
        )
    await callback.answer("Ник удалён")


@router.message(Flow.set_nick, F.text)
async def nick_set(message: Message, state: FSMContext) -> None:
    if message.text and message.text in ALL_BUTTONS:
        await state.clear()
        await _dispatch_menu(message, state)
        return

    raw = (message.text or "").strip()
    nick = validate_nick(raw)
    if not nick:
        await message.answer(
            f"🚫 Ник не подходит.\n"
            f"• {NICK_MIN}–{NICK_MAX} символов\n"
            "• только буквы и цифры"
        )
        return

    user = await db.get_user(message.from_user.id)
    current = ((user or {}).get("nick") or "").strip()
    last = ((user or {}).get("last_nick") or "").strip()
    age = _nick_age_sec(user)

    # уже стоит этот же — ок
    if current and nick == current:
        await state.clear()
        await message.answer(
            f"ℹ️ Ник уже <b>(c) {html.escape(nick)}</b>",
            reply_markup=menu_kb(),
        )
        return

    # восстановление того же ника после удаления — без сдвига таймера
    if not current and last and nick == last:
        await db.set_nick(message.from_user.id, nick, bump_changed=False)
        await state.clear()
        await message.answer(
            f"✅ Ник снова: <b>(c) {html.escape(nick)}</b>",
            reply_markup=menu_kb(),
        )
        return

    # смена на другой (или первый раз после другого) — кулдаун
    if last and nick != last and age is not None and age < NICK_CHANGE_COOLDOWN_SEC:
        left = NICK_CHANGE_COOLDOWN_SEC - age
        await message.answer(
            f"⏳ Другой ник можно поставить через {fmt_left(left)}.\n"
            + (f"Сейчас можно только: <b>{html.escape(last)}</b>" if last else "")
        )
        return

    await db.set_nick(message.from_user.id, nick, bump_changed=True)
    await state.clear()
    await message.answer(
        f"✅ Ник установлен: <b>(c) {html.escape(nick)}</b>",
        reply_markup=menu_kb(),
    )


# ─── админ: статистика / кулдаун / ночь ───────────────────────────────────────

@router.message(F.text == "📊 Статистика")
async def admin_stats(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    await state.clear()
    now = time.time()
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM posts")
        posts_count = (await cur.fetchone())[0]
        cur = await conn.execute("SELECT COUNT(DISTINCT user_id) FROM posts")
        authors_count = (await cur.fetchone())[0]
        cur = await conn.execute(
            "SELECT COUNT(*) FROM rate WHERE muted_until > ?", (now,)
        )
        muted_count = (await cur.fetchone())[0]
        cur = await conn.execute(
            "SELECT COUNT(*) FROM users WHERE nick IS NOT NULL AND nick != ''"
        )
        nick_count = (await cur.fetchone())[0]
    cd = await db.get_cooldown_min()
    night = await db.get_night_mode()
    night_s = "вкл" if night else "выкл"
    night_now = "да 🌙" if (night and is_night_hours()) else "нет"
    await message.answer(
        f"📊 <b>Статистика</b>\n"
        f"Постов в базе: <b>{posts_count}</b>\n"
        f"Уникальных авторов: <b>{authors_count}</b>\n"
        f"С никами: <b>{nick_count}</b>\n"
        f"Сейчас в муте: <b>{muted_count}</b>\n"
        f"Текущий номер: <b>№{_counter['n']}</b>\n"
        f"Кулдаун: <b>{cd} мин</b>\n"
        f"Ночной режим: <b>{night_s}</b> (сейчас: {night_now})",
        reply_markup=admin_kb(),
    )


@router.message(F.text == "⏱ Кулдаун")
async def admin_cooldown(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    await state.clear()
    current = await db.get_cooldown_min()
    await message.answer(
        f"⏱ <b>Кулдаун между сообщениями</b>\n"
        f"Сейчас: <b>{current} мин</b>\n\n"
        "Выбери новый интервал. При слишком частой отправке юзер увидит:\n"
        "«Куда так быстро, ковбой? Сможешь отправить ещё через: …»",
        reply_markup=cooldown_kb(current),
    )


@router.callback_query(F.data.startswith("cd:"))
async def admin_cooldown_set(callback: CallbackQuery) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        minutes = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка")
        return
    if minutes not in COOLDOWN_OPTIONS:
        await callback.answer("Недоступно")
        return
    await db.set_cooldown_min(minutes)
    await callback.message.edit_text(
        f"✅ Кулдаун установлен: <b>{minutes} мин</b>",
        reply_markup=cooldown_kb(minutes),
    )
    await callback.answer(f"{minutes} мин")


@router.message(F.text == "🌙 Ночной режим")
async def admin_night(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    await state.clear()
    enabled = await db.get_night_mode()
    status = "включён ✅" if enabled else "выключен ❌"
    now_t = datetime.now(TZ).strftime("%H:%M")
    await message.answer(
        f"🌙 <b>Ночной режим</b>\n"
        f"Статус: <b>{status}</b>\n"
        f"Окно: 02:00–07:00 (Ташкент)\n"
        f"Интервал ночью: раз в 30 минут\n"
        f"Сейчас в Ташкенте: <b>{now_t}</b>\n"
        f"Сейчас ночь: <b>{'да' if is_night_hours() else 'нет'}</b>",
        reply_markup=night_kb(enabled),
    )


@router.callback_query(F.data == "night:toggle")
async def admin_night_toggle(callback: CallbackQuery) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer()
        return
    enabled = await db.get_night_mode()
    new = not enabled
    await db.set_night_mode(new)
    status = "включён ✅" if new else "выключен ❌"
    now_t = datetime.now(TZ).strftime("%H:%M")
    await callback.message.edit_text(
        f"🌙 <b>Ночной режим</b>\n"
        f"Статус: <b>{status}</b>\n"
        f"Окно: 02:00–07:00 (Ташкент)\n"
        f"Интервал ночью: раз в 30 минут\n"
        f"Сейчас в Ташкенте: <b>{now_t}</b>\n"
        f"Сейчас ночь: <b>{'да' if is_night_hours() else 'нет'}</b>",
        reply_markup=night_kb(new),
    )
    await callback.answer("Вкл" if new else "Выкл")


# ─── админ: рассылка ──────────────────────────────────────────────────────────

@router.message(F.text == "📢 Рассылка")
async def admin_broadcast_start(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    recipients = await db.list_broadcast_recipients()
    await state.set_state(Flow.admin_broadcast)
    await message.answer(
        f"📢 <b>Рассылка</b>\n"
        f"Получателей в базе бота: <b>{len(recipients)}</b>\n\n"
        "⚠️ Telegram <b>не даёт</b> боту список подписчиков канала.\n"
        "Пишем тем, кто хотя бы раз открыл бота /start или отправил сплетню.\n\n"
        "Пришли текст или медиа — уйдёт всем из базы.\n"
        "Отмена — любая кнопка меню.",
        reply_markup=admin_kb(),
    )


@router.message(Flow.admin_broadcast)
async def admin_broadcast_do(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text and message.text in ALL_BUTTONS:
        await state.clear()
        await _dispatch_menu(message, state)
        return

    recipients = await db.list_broadcast_recipients()
    if not recipients:
        await state.clear()
        await message.answer(
            "База пустая: никто ещё не писал боту.\n"
            "Список подписчиков канала через Bot API получить нельзя.\n"
            "После /start у юзеров они появятся в рассылке.\n"
            "Сохраняй БД через «💾 Экспорт БД» перед деплоем.",
            reply_markup=admin_kb(),
        )
        return

    await state.clear()
    status_msg = await message.answer(
        f"⏳ Рассылка на {len(recipients)} чел.…"
    )

    ok = 0
    fail = 0
    for i, uid in enumerate(recipients):
        try:
            await message.bot.copy_message(
                chat_id=uid,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            ok += 1
        except Exception:
            fail += 1
            logger.exception("broadcast to %s", uid)
        if (i + 1) % 20 == 0:
            await asyncio.sleep(1.0)
        else:
            await asyncio.sleep(0.05)

    try:
        await status_msg.edit_text(
            f"✅ Рассылка завершена\n"
            f"Успешно: <b>{ok}</b>\n"
            f"Не доставлено: <b>{fail}</b>\n"
            f"(блок бота / нет диалога / удалили чат)"
        )
    except Exception:
        await message.answer(
            f"✅ Рассылка: ок {ok}, ошибок {fail}",
            reply_markup=admin_kb(),
        )


# ─── админ: мут / размут ──────────────────────────────────────────────────────

@router.message(F.text == "🔇 Снять мут")
async def admin_unmute_start(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    await state.set_state(Flow.admin_unmute)
    muted = await db.list_muted(time.time())
    lines = []
    for uid, until in muted[:20]:
        left = until - time.time()
        lines.append(f"• <code>{uid}</code> — ещё {fmt_left(left)}")
    extra = ""
    if lines:
        extra = "\n\nСейчас в муте:\n" + "\n".join(lines)
    await message.answer(
        "Введи <code>telegram_id</code> пользователя, которому снять мут:"
        + extra,
        reply_markup=admin_kb(),
    )


@router.message(Flow.admin_unmute, F.text)
async def admin_unmute_do(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text and message.text in ALL_BUTTONS:
        await state.clear()
        await _dispatch_menu(message, state)
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("Нужен числовой telegram_id.")
        return
    rate = await db.get_rate(uid)
    await db.set_rate(uid, rate.get("last_sent_at") or 0, rate.get("streak") or 0, 0)
    await state.clear()
    await message.answer(f"✅ Мут снят у <code>{uid}</code>", reply_markup=admin_kb())


@router.message(F.text == "🚫 Дать мут")
async def admin_mute_start(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    await state.set_state(Flow.admin_mute_pick)
    authors = await db.list_post_authors(30)
    if not authors:
        await message.answer(
            "В базе пока нет авторов постов.\n"
            "Можно ввести telegram_id вручную.",
            reply_markup=admin_kb(),
        )
        return
    # inline кнопки по авторам
    rows = []
    for uid in authors[:24]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=str(uid),
                    callback_data=f"mutepick:{uid}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="Ввести id вручную", callback_data="mutepick:manual")]
    )
    await message.answer(
        "🚫 <b>Дать мут</b>\n"
        "Выбери автора из последних постов канала или введи id:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("mutepick:"))
async def admin_mute_pick(callback: CallbackQuery, state: FSMContext) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer()
        return
    part = callback.data.split(":", 1)[1]
    if part == "manual":
        await state.set_state(Flow.admin_mute_pick)
        await callback.message.answer(
            "Введи <code>telegram_id</code> для мута:",
            reply_markup=admin_kb(),
        )
        await callback.answer()
        return
    try:
        uid = int(part)
    except ValueError:
        await callback.answer("Ошибка")
        return
    await state.update_data(mute_uid=uid)
    await state.set_state(Flow.admin_mute_duration)
    await callback.message.answer(
        f"Срок мута для <code>{uid}</code>:",
        reply_markup=mute_duration_kb(uid),
    )
    await callback.answer()


@router.message(Flow.admin_mute_pick, F.text)
async def admin_mute_pick_text(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text and message.text in ALL_BUTTONS:
        await state.clear()
        await _dispatch_menu(message, state)
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("Нужен числовой telegram_id.")
        return
    await state.update_data(mute_uid=uid)
    await state.set_state(Flow.admin_mute_duration)
    await message.answer(
        f"Срок мута для <code>{uid}</code>:",
        reply_markup=mute_duration_kb(uid),
    )


@router.callback_query(F.data.startswith("mute:"))
async def admin_mute_do(callback: CallbackQuery, state: FSMContext) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer()
        return
    if parts[1] == "cancel":
        await state.clear()
        await callback.message.edit_text("Отменено.")
        await callback.answer()
        return
    if len(parts) != 3:
        await callback.answer("Ошибка")
        return
    try:
        uid = int(parts[1])
        minutes = int(parts[2])
    except ValueError:
        await callback.answer("Ошибка")
        return

    if minutes == 0:
        # «навсегда» — далеко в будущем (~10 лет)
        until = time.time() + 10 * 365 * 24 * 3600
        label = "навсегда (до снятия)"
    else:
        until = time.time() + minutes * 60
        label = fmt_left(minutes * 60)

    await db.set_mute(uid, until)
    await state.clear()
    await callback.message.edit_text(
        f"✅ Мут выдан <code>{uid}</code> на {label}"
    )
    await callback.answer("Готово")


# ─── админ: список / добавить / удалить ───────────────────────────────────────

@router.message(F.text == "👥 Список админов")
async def admin_list(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    await state.clear()
    admins = await db.list_admins()
    lines = []
    for aid in admins:
        mark = " (главный)" if aid == ADMIN_ID else ""
        lines.append(f"• <code>{aid}</code>{mark}")
    text = "👥 <b>Админы:</b>\n" + ("\n".join(lines) if lines else "пусто")
    await message.answer(text, reply_markup=admin_kb())


@router.message(F.text == "➕ Добавить админа")
async def admin_add_start(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    await state.set_state(Flow.admin_add)
    await message.answer(
        "Введи <code>telegram_id</code> нового админа:",
        reply_markup=admin_kb(),
    )


@router.message(Flow.admin_add, F.text)
async def admin_add_do(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text and message.text in ALL_BUTTONS:
        await state.clear()
        await _dispatch_menu(message, state)
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("Нужен числовой telegram_id.")
        return
    added = await db.add_admin(uid)
    await state.clear()
    if added:
        await message.answer(f"✅ Админ <code>{uid}</code> добавлен", reply_markup=admin_kb())
    else:
        await message.answer(f"ℹ️ <code>{uid}</code> уже админ", reply_markup=admin_kb())


@router.message(F.text == "➖ Удалить админа")
async def admin_remove_start(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    await state.set_state(Flow.admin_remove)
    await message.answer(
        "Введи <code>telegram_id</code> админа, которого удалить\n"
        f"(главного <code>{ADMIN_ID}</code> удалить нельзя):",
        reply_markup=admin_kb(),
    )


@router.message(Flow.admin_remove, F.text)
async def admin_remove_do(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text and message.text in ALL_BUTTONS:
        await state.clear()
        await _dispatch_menu(message, state)
        return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("Нужен числовой telegram_id.")
        return
    result = await db.remove_admin(uid, protect_id=ADMIN_ID)
    await state.clear()
    if result == "ok":
        await message.answer(f"✅ Админ <code>{uid}</code> удалён", reply_markup=admin_kb())
    elif result == "protected":
        await message.answer("🚫 Главного админа удалить нельзя", reply_markup=admin_kb())
    else:
        await message.answer(
            f"ℹ️ <code>{uid}</code> не найден в списке админов",
            reply_markup=admin_kb(),
        )


@router.message(F.text == "💾 Экспорт БД")
@router.message(Command("export_db"))
async def export_db(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    await state.clear()

    db_path = await db.export_db_path()
    if not db_path.exists():
        await message.answer("База ещё не создана")
        return

    try:
        file = FSInputFile(db_path, filename="gossip.db")
        await message.answer_document(
            file,
            caption=(
                "💾 Актуальная база данных\n\n"
                "Сохрани этот файл.\n"
                "После деплоя отправь его боту через кнопку «📥 Импорт БД»"
            ),
        )
    except Exception:
        logger.exception("export_db")
        await message.answer("Не удалось отправить файл")


@router.message(F.text == "📥 Импорт БД")
@router.message(Command("import_db"))
async def import_db_start(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    await state.set_state(Flow.admin_import_db)
    await message.answer(
        "📥 Пришли файл <code>gossip.db</code> как документ.\n"
        "Текущая база будет полностью заменена.",
        reply_markup=admin_kb(),
    )


@router.message(Flow.admin_import_db, F.document)
async def import_db_do(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        await state.clear()
        return

    doc = message.document
    if not (doc.file_name and doc.file_name.lower().endswith(".db")):
        await message.answer("Нужен файл с расширением .db")
        return

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            await message.bot.download(doc, destination=tmp.name)
            tmp_path = tmp.name

        await db.import_db_from_file(tmp_path)
        os.unlink(tmp_path)

        await state.clear()
        await message.answer("✅ База успешно импортирована!", reply_markup=admin_kb())
    except Exception:
        logger.exception("import_db")
        await message.answer("❌ Ошибка при импорте базы")
        await state.clear()


@router.message(F.text == "↩️ Назад")
async def admin_back(message: Message, state: FSMContext) -> None:
    if not await db.is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Обычное меню:", reply_markup=menu_kb())


@router.message(F.text == "💬 Сплетни")
async def gossip_hint(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    await state.clear()
    await message.answer("✍️ Пиши текст — уйдёт в канал анонимно.")


@router.message(F.sticker)
async def no_stickers(message: Message) -> None:
    await message.answer("🚫 Стикеры нельзя.")


# ─── публикация ───────────────────────────────────────────────────────────────

async def publish_text(message: Message, reply_to: int | None = None) -> None:
    if not await gate(message):
        return
    if not await flood_ok(message):
        return
    raw = message.text or message.caption or ""
    link_mid, body = split_post_link(raw)
    if reply_to is None:
        reply_to = link_mid

    # запрет ссылок / @ / доменов / entities (кроме одной ссылки на пост канала)
    allow = bool(link_mid or reply_to)
    if (
        has_forbidden_links(body, allow_post_link=False)
        or (has_forbidden_links(raw, allow_post_link=True) and not link_mid)
        or message_has_forbidden_entities(message, allow_post_link=allow)
    ):
        await message.answer("Эй ковбой, полегче — ссылки и упоминания запрещены.")
        return

    if not body.strip():
        if link_mid:
            await message.answer("Напиши текст ответа вместе со ссылкой.")
        return
    try:
        n = await next_number(message.bot)
        nick = await db.get_nick(message.from_user.id)
        sent = await message.bot.send_message(
            channel_chat_id(),
            format_post(body, n, nick=nick),
            reply_to_message_id=reply_to,
            disable_web_page_preview=True,
        )
        await db.save_post(sent.message_id, message.from_user.id)
    except Exception:
        logger.exception("send channel")
        await message.answer("⚠️ Не отправилось. Проверь, что бот админ канала.")


@router.message(Flow.reply_wait_text, F.text)
async def reply_text(message: Message, state: FSMContext) -> None:
    if message.text and message.text in ALL_BUTTONS:
        await state.clear()
        await _dispatch_menu(message, state)
        return
    data = await state.get_data()
    await state.clear()
    await publish_text(message, reply_to=data.get("reply_to"))


@router.message(_from_our_channel)
async def channel_forward_to_reply(message: Message, state: FSMContext) -> None:
    if message.text and message.text in ALL_BUTTONS:
        await state.clear()
        await _dispatch_menu(message, state)
        return
    if not await gate(message):
        return
    cid, mid = _forward_channel_id(message)
    if await db.is_admin(message.from_user.id) and mid:
        uid = await db.get_post_author(mid)
        if uid:
            try:
                chat = await message.bot.get_chat(uid)
                name = html.escape(chat.full_name or chat.first_name or "—")
                uname = f" @{chat.username}" if chat.username else ""
                who = f"{name}{uname}\n<code>{uid}</code>"
            except Exception:
                who = f"<code>{uid}</code>"
            await message.answer(
                f"🤫 Автор:\n{who}\n\nВыдать мут:",
                reply_markup=mute_duration_kb(uid),
            )
        else:
            await message.answer("🤫 Автора нет в базе (пост был до записи).")
        return
    await state.update_data(reply_to=mid)
    await state.set_state(Flow.reply_wait_text)
    await message.answer("✍️ Пиши текст ответа.")


@router.message(F.photo | F.video | F.animation | F.document | F.voice | F.video_note)
async def media_msg(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    if not await flood_ok(message):
        return
    caption = message.caption or ""
    link_mid, body = split_post_link(caption)
    if (
        has_forbidden_links(body, allow_post_link=False)
        or has_forbidden_links(caption, allow_post_link=True)
        or message_has_forbidden_entities(message, allow_post_link=bool(link_mid))
    ):
        await message.answer("Эй ковбой, полегче — ссылки и упоминания запрещены.")
        return
    data = await state.get_data()
    reply_to = data.get("reply_to") or link_mid
    await state.clear()
    key = f"{message.from_user.id}:{message.message_id}"
    pending_media[key] = {
        "user_id": message.from_user.id,
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "caption": body,
        "reply_to": reply_to,
    }
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Ок", callback_data=f"m:ok:{key}"),
                InlineKeyboardButton(text="Нет", callback_data=f"m:no:{key}"),
            ]
        ]
    )
    for admin_id in await db.list_admins():
        try:
            await message.bot.copy_message(admin_id, message.chat.id, message.message_id)
            await message.bot.send_message(
                admin_id,
                f"Медиа на проверку\nid {message.from_user.id}",
                reply_markup=kb,
            )
        except Exception:
            logger.exception("send media to admin %s", admin_id)
    await message.answer("🛡 Медиафайл ушёл на ручную модерацию.")


@router.callback_query(F.data.startswith("m:"))
async def media_mod(callback: CallbackQuery) -> None:
    if not await db.is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, decision, key = callback.data.split(":", 2)
    item = pending_media.pop(key, None)
    if not item:
        await callback.answer("Уже разобрано")
        return
    if decision == "no":
        await callback.answer("Нет")
        return
    try:
        n = await next_number(callback.bot)
        nick = await db.get_nick(item["user_id"])
        sent = await callback.bot.copy_message(
            chat_id=channel_chat_id(),
            from_chat_id=item["chat_id"],
            message_id=item["message_id"],
            caption=format_post(item.get("caption") or "", n, nick=nick),
            reply_to_message_id=item.get("reply_to"),
        )
        await db.save_post(sent.message_id, item["user_id"])
    except Exception:
        logger.exception("publish media")
        await callback.message.answer("Не смог запостить в канал")
    await callback.answer()


@router.message(F.text)
async def text_msg(message: Message, state: FSMContext) -> None:
    if message.text and message.text in ALL_BUTTONS:
        return
    await state.clear()
    await publish_text(message)


async def handle_health(request):
    return web.Response(text="ok")


async def run_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main() -> None:
    await db.init_db()
    await db.ensure_main_admin(ADMIN_ID)
    await run_health_server()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    await load_counter(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
