"""Репозиторий привязок: таблица links (discord_id -> steam_id)."""
from ..db import get_db


async def get_link(discord_id) -> str | None:
    """Возвращает steam_id для discord_id или None."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT steam_id FROM links WHERE discord_id = ?",
        (str(discord_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row["steam_id"] if row is not None else None


async def get_owner_of(steam_id) -> str | None:
    """Возвращает discord_id владельца steam_id или None."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT discord_id FROM links WHERE steam_id = ?",
        (str(steam_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row["discord_id"] if row is not None else None


async def add_link(discord_id, steam_id) -> None:
    """Сохраняет привязку (upsert). steam_id уникален по схеме БД."""
    db = await get_db()
    await db.execute(
        "INSERT INTO links (discord_id, steam_id) VALUES (?, ?) "
        "ON CONFLICT(discord_id) DO UPDATE SET steam_id = excluded.steam_id",
        (str(discord_id), str(steam_id)),
    )
    await db.commit()


async def remove_link(discord_id) -> str | None:
    """Удаляет привязку, возвращает удалённый steam_id или None."""
    db = await get_db()
    existing = await get_link(discord_id)
    if existing is None:
        return None
    await db.execute("DELETE FROM links WHERE discord_id = ?", (str(discord_id),))
    await db.commit()
    return existing


async def get_all_links() -> dict:
    """Возвращает все привязки видом {discord_id: steam_id}."""
    db = await get_db()
    cursor = await db.execute("SELECT discord_id, steam_id FROM links")
    rows = await cursor.fetchall()
    await cursor.close()
    return {row["discord_id"]: row["steam_id"] for row in rows}


async def count_all() -> int:
    """Число привязок (для /health)."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM links")
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0