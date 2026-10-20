"""Репозиторий активности: таблица seen."""
from ..db import get_db


async def get_flag(key: str):
    """Возвращает значение служебного флага из таблицы meta или None."""
    db = await get_db()
    cursor = await db.execute("SELECT value FROM meta WHERE key = ?", (key,))
    row = await cursor.fetchone()
    await cursor.close()
    return row["value"] if row is not None else None


async def set_flag(key: str, value) -> None:
    """Сохраняет служебный флаг в таблице meta (upsert)."""
    db = await get_db()
    await db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    await db.commit()


async def get_activity(nickname):
    """Возвращает сохранённое значение активности никеля или None."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT activity FROM seen WHERE nickname = ?",
        (str(nickname),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row["activity"] if row is not None else None


async def get_all() -> dict:
    """Возвращает всю таблицу видом {nickname: activity}."""
    db = await get_db()
    cursor = await db.execute("SELECT nickname, activity FROM seen")
    rows = await cursor.fetchall()
    await cursor.close()
    return {row["nickname"]: row["activity"] for row in rows}


async def set_activity(nickname, activity: int) -> None:
    """Сохраняет/обновляет активность никеля (upsert)."""
    db = await get_db()
    await db.execute(
        "INSERT INTO seen (nickname, activity) VALUES (?, ?) "
        "ON CONFLICT(nickname) DO UPDATE SET activity = excluded.activity",
        (str(nickname), int(activity)),
    )
    await db.commit()


async def set_many(values: dict) -> None:
    """Массово сохраняет {nickname: activity}. Активность приводится к int."""
    db = await get_db()
    for nickname, activity in values.items():
        try:
            activity = int(activity)
        except (TypeError, ValueError):
            continue
        await db.execute(
            "INSERT INTO seen (nickname, activity) VALUES (?, ?) "
            "ON CONFLICT(nickname) DO UPDATE SET activity = excluded.activity",
            (str(nickname), activity),
        )
    await db.commit()


async def count_all() -> int:
    """Число строк в таблице seen (для /health)."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM seen")
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0