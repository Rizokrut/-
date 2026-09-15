import asyncio
import logging
import re
import time
from datetime import datetime, timedelta

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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

RATE_SEC = 15
NICK_DAYS = 30
LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/)",
    re.IGNORECASE,
)
NICK_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё_\-]{1,15}$")

pending_media: dict[str, dict] = {}


class Flow(StatesGroup):
    nick = State()
    gossip = State()
    reply_wait_fwd = State()
    reply_wait_text = State()


def menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Сплетни"), KeyboardButton(text="Правила")],
            [KeyboardButton(text="Ответить на пост"), KeyboardButton(text="Сменить ник")],
        ],
        resize_keyboard=True,
    )


def sub_kb() -> InlineKeyboardMarkup:
    rows = []
    if CHANNEL_URL:
        rows.append([InlineKeyboardButton(text="Подписаться", url=CHANNEL_URL)])
    rows.append([InlineKeyboardButton(text="Проверить подписку", callback_data="chk")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def is_member(bot: Bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        logger.exception("get_chat_member")
        return False


async def gate(message: Message) -> bool:
    if not await is_member(message.bot, message.from_user.id):
        await message.answer(
            "Сначала подпишись на канал со сплетнями.",
            reply_markup=sub_kb(),
        )
        return False
    return True


def has_link(text: str | None) -> bool:
    return bool(text and LINK_RE.search(text))


def format_post(text: str, nick: str) -> str:
    body = (text or "").strip()
    return f"{body}\n\n[{nick}]"


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await gate(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("nick"):
        await state.set_state(Flow.nick)
        await message.answer(
            "Придумай ник до 15 символов.\nБуквы, цифры, _ и -.\nМенять можно раз в 30 дней."
        )
        return
    await message.answer(
        f"Ник: [{user['nick']}]\nЖми «Сплетни» или просто напиши текст.",
        reply_markup=menu_kb(),
    )


@router.callback_query(F.data == "chk")
async def check_sub(callback: CallbackQuery, state: FSMContext) -> None:
    if await is_member(callback.bot, callback.from_user.id):
        await callback.message.answer("Подписка ок. /start")
        await callback.answer()
        return
    await callback.answer("Ещё не подписан", show_alert=True)


@router.message(Flow.nick)
async def save_nick(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    raw = (message.text or "").strip()
    if not NICK_RE.fullmatch(raw):
        await message.answer("Ник 1–15 символов: буквы, цифры, _ -")
        return
    old = await db.get_user(message.from_user.id)
    if old and old.get("nick") and old.get("nick_changed_at"):
        try:
            changed = datetime.fromisoformat(str(old["nick_changed_at"]).replace(" ", "T"))
            if datetime.utcnow() - changed < timedelta(days=NICK_DAYS):
                left = NICK_DAYS - (datetime.utcnow() - changed).days
                await message.answer(f"Ник можно сменить через ~{left} дн.")
                await state.clear()
                return
        except Exception:
            pass
    await db.set_nick(message.from_user.id, raw)
    await state.clear()
    await message.answer(f"Ник сохранён: [{raw}]", reply_markup=menu_kb())


@router.message(F.text == "Правила")
async def rules(message: Message) -> None:
    await message.answer(
        "Правила\n"
        "• подписка на канал обязательна\n"
        "• ник до 15 символов, смена раз в 30 дней\n"
        "• одно сообщение в 15 секунд\n"
        "• стикеры нельзя\n"
        "• ссылки нельзя\n"
        "• фото/видео уходят админу на проверку\n"
        "• ответ на пост: кнопка → перешли пост боту → напиши текст"
    )


@router.message(F.text == "Сменить ник")
async def change_nick(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    await state.set_state(Flow.nick)
    await message.answer("Новый ник:")


@router.message(F.text == "Ответить на пост")
async def reply_start(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("nick"):
        await start(message, state)
        return
    await state.set_state(Flow.reply_wait_fwd)
    await message.answer(
        "Открой канал, перешли нужный пост сюда боту.\n"
        "Потом напишешь текст ответа."
    )


@router.message(Flow.reply_wait_fwd)
async def reply_got_fwd(message: Message, state: FSMContext) -> None:
    src = message.forward_from_chat or (
        message.forward_origin.chat
        if getattr(message, "forward_origin", None) is not None
        and getattr(message.forward_origin, "chat", None)
        else None
    )
    mid = message.forward_from_message_id
    if getattr(message, "forward_origin", None) is not None:
        mid = getattr(message.forward_origin, "message_id", mid)
    chat_ok = False
    if src and str(src.id) == str(CHANNEL_ID):
        chat_ok = True
    if not chat_ok or not mid:
        await message.answer("Нужен пересланный пост именно из канала сплетен.")
        return
    await state.update_data(reply_to=int(mid))
    await state.set_state(Flow.reply_wait_text)
    await message.answer("Пиши текст ответа.")


@router.message(F.sticker)
async def no_stickers(message: Message) -> None:
    await message.answer("Стикеры нельзя.")


@router.message(F.text == "Сплетни")
async def gossip_hint(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("nick"):
        await start(message, state)
        return
    await state.set_state(Flow.gossip)
    await message.answer("Пиши текст. Он уйдёт в канал без твоего телеграм-имени.")


def _too_fast(last: float | None) -> bool:
    if last is None:
        return False
    return time.time() - last < RATE_SEC


async def ensure_ready(message: Message, state: FSMContext) -> dict | None:
    if not await gate(message):
        return None
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("nick"):
        await start(message, state)
        return None
    last = await db.last_sent(message.from_user.id)
    if _too_fast(last):
        wait = RATE_SEC - int(time.time() - (last or 0))
        await message.answer(f"Подожди ещё {max(1, wait)} сек.")
        return None
    return user


@router.message(F.photo | F.video | F.animation | F.document | F.voice | F.video_note)
async def media_msg(message: Message, state: FSMContext) -> None:
    user = await ensure_ready(message, state)
    if not user:
        return
    caption = message.caption or message.text or ""
    if has_link(caption):
        await message.answer("Ссылки нельзя.")
        return
    key = f"{message.from_user.id}:{message.message_id}"
    pending_media[key] = {
        "user_id": message.from_user.id,
        "nick": user["nick"],
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "caption": caption,
        "reply_to": (await state.get_data()).get("reply_to"),
    }
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Ок", callback_data=f"m:ok:{key}"),
                InlineKeyboardButton(text="Нет", callback_data=f"m:no:{key}"),
            ]
        ]
    )
    await message.bot.copy_message(
        ADMIN_ID, message.chat.id, message.message_id
    )
    await message.bot.send_message(
        ADMIN_ID,
        f"Медиа на проверку\nник [{user['nick']}]\nid {message.from_user.id}",
        reply_markup=kb,
    )
    await message.answer("Медиа ушло админу. Если ок — появится в канале.")
    await state.set_state(None)


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
        await callback.message.answer("Отклонено")
        try:
            await callback.bot.send_message(item["user_id"], "Медиа не пропустили.")
        except Exception:
            pass
        await callback.answer()
        return
    text = format_post(item.get("caption") or " ", item["nick"])
    kwargs = {"chat_id": CHANNEL_ID, "from_chat_id": item["chat_id"], "message_id": item["message_id"]}
    if item.get("reply_to"):
        kwargs["reply_to_message_id"] = item["reply_to"]
    try:
        await callback.bot.copy_message(**kwargs)
        # caption overwrite not in copy_message easily; send extra text reply
        await callback.bot.send_message(
            CHANNEL_ID,
            text,
            reply_to_message_id=item.get("reply_to"),
        )
        await db.touch_sent(item["user_id"])
        await callback.bot.send_message(item["user_id"], "Опубликовано.")
    except Exception:
        logger.exception("publish media")
        await callback.message.answer("Не смог запостить. Бот админ канала?")
    await callback.answer()


@router.message(F.text)
async def text_msg(message: Message, state: FSMContext) -> None:
    if message.text in {"Сплетни", "Правила", "Ответить на пост", "Сменить ник"}:
        return
    st = await state.get_state()
    if st == Flow.nick.state:
        return
    user = await ensure_ready(message, state)
    if not user:
        return
    if has_link(message.text):
        await message.answer("Ссылки нельзя.")
        return
    data = await state.get_data()
    reply_to = data.get("reply_to") if st == Flow.reply_wait_text.state else None
    text = format_post(message.text, user["nick"])
    try:
        await message.bot.send_message(
            CHANNEL_ID,
            text,
            reply_to_message_id=reply_to,
        )
    except Exception:
        logger.exception("send channel")
        await message.answer("Не отправилось в канал. Проверь, что бот админ.")
        return
    await db.touch_sent(message.from_user.id)
    await state.clear()
    await message.answer("Ушло в канал.", reply_markup=menu_kb())


async def main() -> None:
    await db.init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
