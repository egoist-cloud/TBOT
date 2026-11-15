"""Статистика участников: время в голосовых каналах и число сообщений.

Данные копятся в таблице ``user_stats``. Войс считается по сессиям
(вошёл в канал → вышел/переключился), сообщения — простым счётчиком.
Нужно для команды /profile и мини-топа.
"""
from ..db import get_db


async def add_messages(discord_id, count: int = 1) -> None:
    """Прибавляет сообщения участнику (upsert)."""
    db = await get_db()
    await db.execute(
        "INSERT INTO user_stats (discord_id, messages, updated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(discord_id) DO UPDATE SET "
        "messages = messages + excluded.messages, updated_at = datetime('now')",
        (str(discord_id), int(count)),
    )
    await db.commit()


async def add_voice_seconds(discord_id, seconds: int) -> None:
    """Прибавляет секунды в войсе (upsert)."""
    seconds = int(seconds)
    if seconds <= 0:
        return
    db = await get_db()
    await db.execute(
        "INSERT INTO user_stats (discord_id, voice_seconds, updated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(discord_id) DO UPDATE SET "
        "voice_seconds = voice_seconds + excluded.voice_seconds, "
        "updated_at = datetime('now')",
        (str(discord_id), seconds),
    )
    await db.commit()


async def get_stats(discord_id) -> dict:
    """Статистика участника; нули, если он ещё ничего не набрал."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT voice_seconds, messages FROM user_stats WHERE discord_id = ?",
        (str(discord_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return {"voice_seconds": 0, "messages": 0}
    return {"voice_seconds": int(row["voice_seconds"]),
            "messages": int(row["messages"])}


async def get_message_rank(discord_id) -> tuple[int, int]:
    """Место по числу сообщений и всего участников с сообщениями.

    При равном числе сообщений место одинаковое: считаем, сколько людей
    написали строго больше.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) AS n FROM user_stats WHERE messages > "
        "(SELECT COALESCE((SELECT messages FROM user_stats WHERE discord_id = ?), 0))",
        (str(discord_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    ahead = int(row["n"]) if row is not None else 0
    cursor = await db.execute(
        "SELECT COUNT(*) AS n FROM user_stats WHERE messages > 0")
    row = await cursor.fetchone()
    await cursor.close()
    total = int(row["n"]) if row is not None else 0
    return ahead + 1, total


async def top_by_messages(limit: int = 10) -> list:
    """Мини-топ по сообщениям: [(discord_id, messages), ...]."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT discord_id, messages FROM user_stats "
        "WHERE messages > 0 ORDER BY messages DESC, discord_id LIMIT ?",
        (max(1, int(limit)),),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [(str(r["discord_id"]), int(r["messages"])) for r in rows]
