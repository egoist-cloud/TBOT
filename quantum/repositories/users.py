"""Репозиторий пользователей (баны/кулдауны): таблица users."""
import json

from ..db import get_db


def _deserialize(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


async def get_user(discord_id) -> dict:
    """Возвращает данные пользователя словарём (или {} если нет)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT data FROM users WHERE discord_id = ?",
        (str(discord_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return _deserialize(row["data"]) if row is not None else {}


async def save_user(discord_id, data: dict) -> None:
    """Сохраняет данные пользователя (upsert по discord_id)."""
    db = await get_db()
    await db.execute(
        "INSERT INTO users (discord_id, data) VALUES (?, ?) "
        "ON CONFLICT(discord_id) DO UPDATE SET data = excluded.data",
        (str(discord_id), json.dumps(data, ensure_ascii=False, default=str)),
    )
    await db.commit()


async def remove_user(discord_id) -> None:
    """Удаляет данные пользователя."""
    db = await get_db()
    await db.execute("DELETE FROM users WHERE discord_id = ?", (str(discord_id),))
    await db.commit()


async def get_all_users() -> dict:
    """Возвращает всех пользователей видом {discord_id: data_dict}."""
    db = await get_db()
    cursor = await db.execute("SELECT discord_id, data FROM users")
    rows = await cursor.fetchall()
    await cursor.close()
    return {row["discord_id"]: _deserialize(row["data"]) for row in rows}


async def count_all() -> int:
    """Число пользователей (для /health)."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM users")
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0