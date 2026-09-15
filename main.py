import html
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

MUTE_SEC = 300
STREAK_NEED = 6
GAP_RESET = 5
NICK_DAYS = 30
MENU = {"Сплетни", "Правила", "Ответить на пост", "Сменить ник"}
LINK_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.IGNORECASE)
NICK_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё_\-]{1,15}$")

pending_media: dict[str, dict] = {}


class Flow(StatesGroup):
    nick = State()
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


def format_post(text: str, nick: str) -> str:
    body = html.escape((text or "").strip())
    name = html.escape(nick)
    sign = f"(с) <b>{name}</b>"
    if body:
        return f"{body}\n{sign}"
    return sign


def has_link(text: str | None) -> bool:
    return bool(text and LINK_RE.search(text))


def parse_changed(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = str(raw).replace(" ", "T", 1)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(raw[:26], fmt)
        except ValueError:
            continue
    return None


def nick_wait_days(user: dict | None) -> int | None:
    if not user or not user.get("nick"):
        return None
    changed = parse_changed(user.get("nick_changed_at"))
    if not changed:
        return None
    passed = datetime.utcnow() - changed
    if passed < timedelta(days=NICK_DAYS):
        return max(1, NICK_DAYS - passed.days)
    return None


async def is_member(bot: Bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_ID, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        logger.exception("get_chat_member")
        return False



async def flood_ok(message: Message) -> bool:
    now = time.time()
    rate = await db.get_rate(message.from_user.id)
    muted = float(rate.get("muted_until") or 0)
    if muted > now:
        left = int(muted - now)
        mins = max(1, left // 60)
        sec = left % 60
        await message.answer(f"🔇 Мут ещё {mins} мин {sec} сек.")
        return False
    last = rate.get("last_sent_at")
    streak = int(rate.get("streak") or 0)
    if last and now - float(last) >= GAP_RESET:
        streak = 0
    streak += 1
    muted_until = 0.0
    if streak >= STREAK_NEED:
        muted_until = now + MUTE_SEC
        streak = 0
        await db.set_rate(message.from_user.id, now, streak, muted_until)
        await message.answer("🔇 Слишком часто. Мут на 5 минут.")
        return False
    await db.set_rate(message.from_user.id, now, streak, 0)
    return True


async def gate(message: Message) -> bool:
    if not await is_member(message.bot, message.from_user.id):
        await message.answer(
            "📢 Сначала подпишись на канал со сплетнями.",
            reply_markup=sub_kb(),
        )
        return False
    return True


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await gate(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("nick"):
        await state.set_state(Flow.nick)
        await message.answer(
            "👋 Придумай ник — до 15 символов.\nБуквы, цифры, _ и -.\nМенять можно раз в 30 дней."
        )
        return
    await message.answer("Готово. Можешь писать 👇", reply_markup=menu_kb())


@router.callback_query(F.data == "chk")
async def check_sub(callback: CallbackQuery) -> None:
    if await is_member(callback.bot, callback.from_user.id):
        await callback.message.answer("Подписка ок. /start")
        await callback.answer()
        return
    await callback.answer("Ещё не подписан", show_alert=True)


@router.message(F.text == "Правила")
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


@router.message(F.text == "Сменить ник")
async def change_nick(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    user = await db.get_user(message.from_user.id)
    left = nick_wait_days(user)
    if left:
        await state.clear()
        await message.answer(f"⏳ Ник можно сменить через {left} дн.")
        return
    await state.set_state(Flow.nick)
    await message.answer("✏️ Новый ник, до 15 символов:")


@router.message(Flow.nick)
async def save_nick(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    if message.text in MENU:
        await state.clear()
        await message.answer("Отмена", reply_markup=menu_kb())
        return
    user = await db.get_user(message.from_user.id)
    left = nick_wait_days(user)
    if left:
        await state.clear()
        await message.answer(f"⏳ Ник можно сменить через {left} дн.", reply_markup=menu_kb())
        return
    raw = (message.text or "").strip()
    if not NICK_RE.fullmatch(raw):
        await message.answer("⚠️ Ник 1–15 символов: буквы, цифры, _ -")
        return
    await db.set_nick(message.from_user.id, raw)
    await state.clear()
    await message.answer(f"✅ Ник сохранён: <b>{html.escape(raw)}</b>", reply_markup=menu_kb())


@router.message(F.text == "Ответить на пост")
async def reply_start(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("nick"):
        await start(message, state)
        return
    await state.set_state(Flow.reply_wait_fwd)
    await message.answer("↩️ Перешли боту пост из канала.\nИли напиши текст — будет новый пост.")


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


@router.message(Flow.reply_wait_fwd)
async def reply_got_fwd(message: Message, state: FSMContext) -> None:
    if message.text in MENU:
        await state.clear()
        return
    cid, mid = _forward_channel_id(message)
    if cid == str(CHANNEL_ID) and mid:
        await state.update_data(reply_to=mid)
        await state.set_state(Flow.reply_wait_text)
        await message.answer("✍️ Пиши текст ответа.")
        return
    await state.clear()
    await publish_text(message, reply_to=None)


@router.message(F.text == "Сплетни")
async def gossip_hint(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("nick"):
        await start(message, state)
        return
    await state.clear()
    await message.answer("✍️ Пиши текст — уйдёт в канал анонимно.")


@router.message(F.sticker)
async def no_stickers(message: Message) -> None:
    await message.answer("🚫 Стикеры нельзя.")


async def publish_text(message: Message, reply_to: int | None) -> None:
    if not await gate(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("nick"):
        return
    if not await flood_ok(message):
        return
    text = message.text or message.caption or ""
    if has_link(text):
        await message.answer("🚫 Ссылки нельзя.")
        return
    try:
        await message.bot.send_message(
            CHANNEL_ID,
            format_post(text, user["nick"]),
            reply_to_message_id=reply_to,
        )
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


@router.message(F.photo | F.video | F.animation | F.document | F.voice | F.video_note)
async def media_msg(message: Message, state: FSMContext) -> None:
    if not await gate(message):
        return
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("nick"):
        await start(message, state)
        return
    if not await flood_ok(message):
        return
    caption = message.caption or ""
    if has_link(caption):
        await message.answer("🚫 Ссылки нельзя.")
        return
    data = await state.get_data()
    reply_to = data.get("reply_to")
    await state.clear()
    key = f"{message.from_user.id}:{message.message_id}"
    pending_media[key] = {
        "user_id": message.from_user.id,
        "nick": user["nick"],
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "caption": caption,
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
        f"Медиа на проверку\nник {html.escape(user['nick'])}\nid {message.from_user.id}",
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
    caption = format_post(item.get("caption") or "", item["nick"])
    try:
        await callback.bot.copy_message(
            chat_id=CHANNEL_ID,
            from_chat_id=item["chat_id"],
            message_id=item["message_id"],
            caption=caption,
            reply_to_message_id=item.get("reply_to"),
        )
        await db.set_rate(item["user_id"], time.time(), 0, 0)
    except Exception:
        logger.exception("publish media")
        await callback.message.answer("Не смог запостить в канал")
    await callback.answer()


@router.message(F.text)
async def text_msg(message: Message, state: FSMContext) -> None:
    if message.text in MENU:
        return
    st = await state.get_state()
    if st == Flow.nick.state:
        return
    await state.clear()
    await publish_text(message, reply_to=None)


async def main() -> None:
    await db.init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
