from __future__ import annotations

import aiosqlite

DB_PATH = "gossip.db"


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                nick TEXT,
                nick_changed_at TEXT
            )
            """
        )
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
        await db.commit()


async def get_user(telegram_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


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
