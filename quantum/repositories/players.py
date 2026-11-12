"""Репозиторий игроков: таблица players (steamid64 -> данные)."""
import json

from ..db import get_db


def _deserialize(raw: str) -> dict:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


async def get_player(steam_id) -> dict:
    """Возвращает профиль игрока словарём (или {} если нет)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT data FROM players WHERE steamid64 = ?",
        (str(steam_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return _deserialize(row["data"]) if row is not None else {}


async def save_player(steam_id, data: dict) -> None:
    """Сохраняет профиль игрока (upsert по steamid64)."""
    db = await get_db()
    await db.execute(
        "INSERT INTO players (steamid64, data) VALUES (?, ?) "
        "ON CONFLICT(steamid64) DO UPDATE SET data = excluded.data",
        (str(steam_id), json.dumps(data, ensure_ascii=False, default=str)),
    )
    await db.commit()


async def remove_player(steam_id) -> None:
    """Удаляет профиль игрока."""
    db = await get_db()
    await db.execute("DELETE FROM players WHERE steamid64 = ?", (str(steam_id),))
    await db.commit()


async def get_all_players() -> dict:
    """Возвращает все профили видом {steamid64: profile_dict}."""
    db = await get_db()
    cursor = await db.execute("SELECT steamid64, data FROM players")
    rows = await cursor.fetchall()
    await cursor.close()
    return {row["steamid64"]: _deserialize(row["data"]) for row in rows}


async def count_all() -> int:
    """Число профилей (для /health)."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM players")
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0