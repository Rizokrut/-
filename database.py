from __future__ import annotations

import aiosqlite
import shutil
from pathlib import Path

DB_PATH = "gossip.db"
DB_FILE = Path(DB_PATH)

# дефолты настроек
DEFAULT_COOLDOWN_MIN = 1
DEFAULT_NIGHT_MODE = 1  # 1 = вкл, 0 = выкл


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                nick TEXT,
                nick_changed_at TEXT,
                last_seen_at TEXT
            )
            """
        )
        ucols = {r[1] for r in await (await db.execute("PRAGMA table_info(users)")).fetchall()}
        if "last_seen_at" not in ucols:
            await db.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS rate (
                telegram_id INTEGER PRIMARY KEY,
                last_sent_at REAL,
                streak INTEGER DEFAULT 0,
                muted_until REAL DEFAULT 0
            )
            """
        )
        cols = {r[1] for r in await (await db.execute("PRAGMA table_info(rate)")).fetchall()}
        if "streak" not in cols:
            await db.execute("ALTER TABLE rate ADD COLUMN streak INTEGER DEFAULT 0")
        if "muted_until" not in cols:
            await db.execute("ALTER TABLE rate ADD COLUMN muted_until REAL DEFAULT 0")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                channel_msg_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                telegram_id INTEGER PRIMARY KEY
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        # дефолты, если ещё нет
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('cooldown_min', ?)",
            (str(DEFAULT_COOLDOWN_MIN),),
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('night_mode', ?)",
            (str(DEFAULT_NIGHT_MODE),),
        )
        await db.commit()


async def get_setting(key: str, default: str = "") -> str:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = await cur.fetchone()
            return row[0] if row else default
    except Exception:
        # старая база без таблицы settings
        return default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await db.commit()


async def get_cooldown_min() -> int:
    raw = await get_setting("cooldown_min", str(DEFAULT_COOLDOWN_MIN))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_COOLDOWN_MIN


async def set_cooldown_min(minutes: int) -> None:
    await set_setting("cooldown_min", str(max(0, minutes)))


async def get_night_mode() -> bool:
    raw = await get_setting("night_mode", str(DEFAULT_NIGHT_MODE))
    return raw not in ("0", "false", "False", "")


async def set_night_mode(enabled: bool) -> None:
    await set_setting("night_mode", "1" if enabled else "0")


async def ensure_main_admin(main_admin_id: int) -> None:
    if not main_admin_id:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO admins (telegram_id) VALUES (?)",
            (main_admin_id,),
        )
        await db.commit()


async def is_admin(telegram_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM admins WHERE telegram_id = ?", (telegram_id,)
        )
        return await cur.fetchone() is not None


async def add_admin(telegram_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO admins (telegram_id) VALUES (?)",
            (telegram_id,),
        )
        await db.commit()
        return cur.rowcount > 0


async def remove_admin(telegram_id: int, protect_id: int) -> str:
    if telegram_id == protect_id:
        return "protected"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM admins WHERE telegram_id = ?", (telegram_id,)
        )
        await db.commit()
        if cur.rowcount == 0:
            return "not_found"
        return "ok"


async def list_admins() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT telegram_id FROM admins ORDER BY telegram_id")
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def get_user(telegram_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def touch_user(telegram_id: int) -> None:
    """Зафиксировать, что юзер писал боту (для рассылки). Ник не трогаем."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, nick, nick_changed_at, last_seen_at)
            VALUES (?, NULL, NULL, datetime('now'))
            ON CONFLICT(telegram_id) DO UPDATE SET
                last_seen_at = datetime('now')
            """,
            (telegram_id,),
        )
        await db.commit()


async def get_nick(telegram_id: int) -> str | None:
    user = await get_user(telegram_id)
    if not user:
        return None
    nick = (user.get("nick") or "").strip()
    return nick or None


async def set_nick(telegram_id: int, nick: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, nick, nick_changed_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(telegram_id) DO UPDATE SET
                nick = excluded.nick,
                nick_changed_at = excluded.nick_changed_at
            """,
            (telegram_id, nick),
        )
        await db.commit()


async def clear_nick(telegram_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_id, nick, nick_changed_at)
            VALUES (?, NULL, datetime('now'))
            ON CONFLICT(telegram_id) DO UPDATE SET
                nick = NULL,
                nick_changed_at = excluded.nick_changed_at
            """,
            (telegram_id,),
        )
        await db.commit()


async def get_rate(telegram_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT last_sent_at, streak, muted_until FROM rate WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cur.fetchone()
        if not row:
            return {"last_sent_at": None, "streak": 0, "muted_until": 0}
        return dict(row)


async def set_rate(
    telegram_id: int,
    last_sent_at: float,
    streak: int,
    muted_until: float,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO rate (telegram_id, last_sent_at, streak, muted_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                last_sent_at = excluded.last_sent_at,
                streak = excluded.streak,
                muted_until = excluded.muted_until
            """,
            (telegram_id, last_sent_at, streak, muted_until),
        )
        await db.commit()


async def set_mute(telegram_id: int, muted_until: float) -> None:
    """Выставить только muted_until, last_sent/streak не трогаем."""
    rate = await get_rate(telegram_id)
    await set_rate(
        telegram_id,
        rate.get("last_sent_at") or 0,
        rate.get("streak") or 0,
        muted_until,
    )


async def save_post(channel_msg_id: int, user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO posts (channel_msg_id, user_id, created_at)
            VALUES (?, ?, datetime('now'))
            """,
            (channel_msg_id, user_id),
        )
        await db.commit()


async def get_post_author(channel_msg_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id FROM posts WHERE channel_msg_id = ?",
            (channel_msg_id,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else None


async def list_post_authors(limit: int = 40) -> list[int]:
    """Уникальные авторы постов, свежие сверху."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT user_id FROM posts
            GROUP BY user_id
            ORDER BY MAX(created_at) DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


async def list_all_post_authors() -> list[int]:
    """Все уникальные авторы постов."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT user_id FROM posts
            GROUP BY user_id
            ORDER BY MAX(created_at) DESC
            """
        )
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


async def list_broadcast_recipients() -> list[int]:
    """
    Все, кому бот может попытаться написать:
    — заходили в бота / писали (users)
    — авторы постов
    — есть запись в rate
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT telegram_id AS uid FROM users
            UNION
            SELECT user_id AS uid FROM posts
            UNION
            SELECT telegram_id AS uid FROM rate
            ORDER BY uid
            """
        )
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


async def list_muted(now: float) -> list[tuple[int, float]]:
    """Список (user_id, muted_until) у кого сейчас активный мут."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT telegram_id, muted_until FROM rate WHERE muted_until > ?",
            (now,),
        )
        rows = await cur.fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]


async def export_db_path() -> Path:
    return DB_FILE


async def import_db_from_file(source_path: str | Path) -> None:
    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError("Файл не найден")
    shutil.copy2(source, DB_FILE)
    # после импорта старой базы — досоздать таблицы/дефолты
    await init_db()
