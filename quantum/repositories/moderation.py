"""Репозиторий модераторов: таблица moderators."""
from ..db import get_db


async def get_moderator(discord_id) -> str | None:
    """Возвращает discord_id модератора или None."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT discord_id FROM moderators WHERE discord_id = ?",
        (str(discord_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row["discord_id"] if row is not None else None


async def is_moderator(discord_id) -> bool:
    """True, если discord_id есть в таблице модераторов."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT 1 FROM moderators WHERE discord_id = ?",
        (str(discord_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def add_moderator(discord_id) -> bool:
    """Добавляет модератора. Возвращает True, если добавлен (а не существовал)."""
    db = await get_db()
    cursor = await db.execute(
        "INSERT OR IGNORE INTO moderators (discord_id) VALUES (?)",
        (str(discord_id),),
    )
    await db.commit()
    return cursor.rowcount > 0


async def remove_moderator(discord_id) -> bool:
    """Удаляет модератора. Возвращает True, если был удалён."""
    db = await get_db()
    cursor = await db.execute(
        "DELETE FROM moderators WHERE discord_id = ?",
        (str(discord_id),),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_all_moderators() -> list:
    """Возвращает список discord_id всех модераторов."""
    db = await get_db()
    cursor = await db.execute("SELECT discord_id FROM moderators")
    rows = await cursor.fetchall()
    await cursor.close()
    return [row["discord_id"] for row in rows]


async def count_all() -> int:
    """Число модераторов (для /health)."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM moderators")
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0