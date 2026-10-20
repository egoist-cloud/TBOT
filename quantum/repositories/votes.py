"""Репозиторий голосов: таблица votes."""
import json

from ..db import get_db


async def get_votes(message_id) -> dict:
    """Возвращает состояние голосования для сообщения.

    Если записи нет — пустой шаблон {"accept":0,"deny":0,"voters":[]},
    чтобы код бота работал без изменений логики.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT accept, deny, voters FROM votes WHERE message_id = ?",
        (str(message_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return {"accept": 0, "deny": 0, "voters": []}
    try:
        voters = json.loads(row["voters"])
    except (TypeError, ValueError):
        voters = []
    return {"accept": row["accept"], "deny": row["deny"], "voters": voters}


async def save_votes(message_id, state: dict) -> None:
    """Сохраняет состояние голосования (upsert)."""
    db = await get_db()
    await db.execute(
        "INSERT INTO votes (message_id, accept, deny, voters) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(message_id) DO UPDATE SET "
        "accept = excluded.accept, deny = excluded.deny, voters = excluded.voters",
        (
            str(message_id),
            int(state.get("accept", 0) or 0),
            int(state.get("deny", 0) or 0),
            json.dumps([str(v) for v in (state.get("voters") or [])], ensure_ascii=False),
        ),
    )
    await db.commit()


async def count_all() -> int:
    """Число строк в таблице votes (для /health)."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM votes")
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0