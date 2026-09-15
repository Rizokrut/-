import html
import logging
import os
import re
import time

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import database as db
from config import ADMIN_ID, BOT_TOKEN, CHANNEL_ID, CHANNEL_URL


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

MUTE_SEC = 300
STREAK_NEED = 6
GAP_RESET = 5
MENU = {"💬 Сплетни", "📜 Правила", "↩️ Ответить", "👤 Админ"}
ADMIN_TG = "https://t.me/gubkinhelp"

# Любые ссылки, кроме одной ссылки на пост канала — для реплая.
LINK_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.IGNORECASE)
TG_POST_RE = re.compile(
    r"(https?://)?(t\.me|telegram\.me)/(c/\d+/|(?P<user>[A-Za-z0-9_]+)/)(?P<mid>\d+)",
    re.IGNORECASE,
)

pending_media: dict[str, dict] = {}
_counter = {"n": 0, "msg_id": None}


class Flow(StatesGroup):
    reply_wait_fwd = State()
    reply_wait_text = State()


def menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💬 Сплетни"), KeyboardButton(text="📜 Правила")],
            [KeyboardButton(text="↩️ Ответить"), KeyboardButton(text="👤 Админ")],
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


def format_post(text: str, number: int | str) -> str:
    body = html.escape((text or "").strip())
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


def extra_links(text: str) -> bool:
    if not text:
        return False
    cleaned = TG_POST_RE.sub("", text)
    return bool(LINK_RE.search(cleaned))


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
    now = time.time()
    rate = await db.get_rate(message.from_user.id)
    muted = float(rate.get("muted_until") or 0)
    if muted > now:
        left = int(muted - now)
        mins, sec = divmod(max(0, left), 60)
        await message.answer(f"🔇 Мут ещё {mins} мин {sec} сек.")
        return False
    last = rate.get("last_sent_at")
    streak = int(rate.get("streak") or 0)
    if last and now - float(last) >= GAP_RESET:
        streak = 0
    streak += 1
    if streak >= STREAK_NEED:
        await db.set_rate(message.from_user.id, now, 0, now + MUTE_SEC)
        await message.answer("🔇 Слишком часто. Мут на 5 минут.")
        return False
    await db.set_rate(message.from_user.id, now, streak, 0)
    return True


async def gate(message: Message) -> bool:
    if message.from_user.id == ADMIN_ID:
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


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await gate(message):
        return
    await message.answer(
        "<b>🤫 Отправляй 100% анонимные сообщения.</b>\n\n"
        "Бот автоматически закинет в канал.\n"
        "Ответ на пост: кинь ссылку на сообщение и текст в одном сообщении.",
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
        "3. Запрещена реклама\n"
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
    if message.text in MENU:
        await state.clear()
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
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Написать @gubkinhelp", url=ADMIN_TG)]
        ]
    )
    await message.answer("Связь с админом:", reply_markup=kb)


@router.message(F.text == "💬 Сплетни")
async def gossip_hint(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    await state.clear()
    await message.answer("✍️ Пиши текст — уйдёт в канал анонимно.")


@router.message(F.sticker)
async def no_stickers(message: Message) -> None:
    await message.answer("🚫 Стикеры нельзя.")


async def publish_text(message: Message, reply_to: int | None = None) -> None:
    if not await gate(message):
        return
    if not await flood_ok(message):
        return
    raw = message.text or message.caption or ""
    link_mid, body = split_post_link(raw)
    if reply_to is None:
        reply_to = link_mid
    if extra_links(body) or (extra_links(raw) and not link_mid):
        await message.answer("🚫 Ссылки нельзя. Можно только ссылку на пост канала.")
        return
    if not body.strip():
        if link_mid:
            await message.answer("Напиши текст ответа вместе со ссылкой.")
        return
    try:
        n = await next_number(message.bot)
        sent = await message.bot.send_message(
            channel_chat_id(),
            format_post(body, n),
            reply_to_message_id=reply_to,
        )
        await db.save_post(sent.message_id, message.from_user.id)
    except Exception:
        logger.exception("send channel")
        await message.answer("⚠️ Не отправилось. Проверь, что бот админ канала.")


@router.message(Flow.reply_wait_text, F.text)
async def reply_text(message: Message, state: FSMContext) -> None:
    if message.text in MENU:
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    await publish_text(message, reply_to=data.get("reply_to"))


@router.message(_from_our_channel)
async def channel_forward_to_reply(message: Message, state: FSMContext) -> None:
    if message.text in MENU:
        await state.clear()
        return
    if not await gate(message):
        return
    cid, mid = _forward_channel_id(message)
    if message.from_user.id == ADMIN_ID and mid:
        uid = await db.get_post_author(mid)
        if uid:
            try:
                chat = await message.bot.get_chat(uid)
                name = html.escape(chat.full_name or chat.first_name or "—")
                nick = f" @{chat.username}" if chat.username else ""
                who = f"{name}{nick}\n<code>{uid}</code>"
            except Exception:
                who = f"<code>{uid}</code>"
            await message.answer(f"🤫 Автор:\n{who}")
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
    if extra_links(body):
        await message.answer("🚫 Ссылки нельзя.")
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
    await message.bot.copy_message(ADMIN_ID, message.chat.id, message.message_id)
    await message.bot.send_message(
        ADMIN_ID,
        f"Медиа на проверку\nid {message.from_user.id}",
        reply_markup=kb,
    )
    await message.answer("🛡 Медиафайл ушёл на ручную модерацию.")


@router.callback_query(F.data.startswith("m:"))
async def media_mod(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
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
        sent = await callback.bot.copy_message(
            chat_id=channel_chat_id(),
            from_chat_id=item["chat_id"],
            message_id=item["message_id"],
            caption=format_post(item.get("caption") or "", n),
            reply_to_message_id=item.get("reply_to"),
        )
        await db.save_post(sent.message_id, item["user_id"])
    except Exception:
        logger.exception("publish media")
        await callback.message.answer("Не смог запостить в канал")
    await callback.answer()


@router.message(F.text)
async def text_msg(message: Message, state: FSMContext) -> None:
    if message.text in MENU:
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