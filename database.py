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
                last_sent_at REAL
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


async def last_sent(telegram_id: int) -> float | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT last_sent_at FROM rate WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cur.fetchone()
        return float(row[0]) if row else None


async def touch_sent(telegram_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO rate (telegram_id, last_sent_at)
            VALUES (?, strftime('%s','now'))
            ON CONFLICT(telegram_id) DO UPDATE SET
                last_sent_at = strftime('%s','now')
            """,
            (telegram_id,),
        )
        await db.commit()
