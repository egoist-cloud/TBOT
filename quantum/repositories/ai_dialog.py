"""Репозиторий диалогов с ИИ: таблица ai_dialog.

Хранит последние реплики пользователя и ИИ, чтобы /ask понимал
продолжение разговора («а за это что?»). История не бесконечная:
на пользователя держим не больше KEEP_MESSAGES реплик.
"""
from __future__ import annotations

import time

from ..db import get_db

# Сколько последних реплик хранить на пользователя.
KEEP_MESSAGES = 40


async def add_message(user_id, role: str, content: str) -> None:
    """Добавляет реплику и подрезает историю до KEEP_MESSAGES."""
    if role not in ("user", "assistant"):
        return
    content = (content or "").strip()
    if not content:
        return
    db = await get_db()
    await db.execute(
        "INSERT INTO ai_dialog (user_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?)",
        (str(user_id), role, content[:4000], int(time.time())),
    )
    await db.execute(
        "DELETE FROM ai_dialog WHERE user_id = ? AND id NOT IN ("
        "  SELECT id FROM ai_dialog WHERE user_id = ? "
        "  ORDER BY id DESC LIMIT ?)",
        (str(user_id), str(user_id), KEEP_MESSAGES),
    )
    await db.commit()


async def get_history(user_id, limit: int = 8) -> list[dict]:
    """Последние `limit` реплик в хронологическом порядке."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT role, content FROM ("
        "  SELECT id, role, content FROM ai_dialog WHERE user_id = ? "
        "  ORDER BY id DESC LIMIT ?"
        ") ORDER BY id ASC",
        (str(user_id), max(1, int(limit))),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [{"role": row["role"], "content": row["content"]} for row in rows]


async def clear(user_id) -> int:
    """Удаляет диалог пользователя. Возвращает число удалённых реплик."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) AS c FROM ai_dialog WHERE user_id = ?", (str(user_id),))
    row = await cursor.fetchone()
    await cursor.close()
    await db.execute("DELETE FROM ai_dialog WHERE user_id = ?", (str(user_id),))
    await db.commit()
    return row["c"] if row is not None else 0


async def clear_all() -> int:
    """Удаляет все диалоги (админская очистка). Возвращает число реплик."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM ai_dialog")
    row = await cursor.fetchone()
    await cursor.close()
    await db.execute("DELETE FROM ai_dialog")
    await db.commit()
    return row["c"] if row is not None else 0


async def count(user_id=None) -> int:
    """Число реплик: у пользователя или всего."""
    db = await get_db()
    if user_id is None:
        cursor = await db.execute("SELECT COUNT(*) AS c FROM ai_dialog")
    else:
        cursor = await db.execute(
            "SELECT COUNT(*) AS c FROM ai_dialog WHERE user_id = ?",
            (str(user_id),))
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0
