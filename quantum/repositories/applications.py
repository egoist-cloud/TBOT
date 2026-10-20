# -*- coding: utf-8 -*-
"""Репозиторий заявок в клан: таблица applications (анти-дубликаты)."""
from __future__ import annotations

import datetime

from ..db import get_db


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


async def get_active_application(user_id) -> dict | None:
    """Возвращает активную заявку пользователя или None.

    Активной считаем заявку со статусом pending (на рассмотрении).
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT user_id, status, clan_name, message_id, channel_id, "
        "created_at, updated_at FROM applications "
        "WHERE user_id = ? AND status = 'pending'",
        (str(user_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return dict(row)


async def upsert_application(user_id, *, clan_name="", status="pending",
                             message_id="", channel_id="") -> None:
    """Сохраняет/обновляет заявку пользователя (по user_id)."""
    db = await get_db()
    now = _now()
    await db.execute(
        "INSERT INTO applications (user_id, status, clan_name, message_id, "
        "channel_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "status = excluded.status, clan_name = excluded.clan_name, "
        "message_id = excluded.message_id, channel_id = excluded.channel_id, "
        "updated_at = excluded.updated_at",
        (str(user_id), status, clan_name, message_id, channel_id, now, now),
    )
    await db.commit()


async def finalize_application(user_id, status: str) -> None:
    """Закрывает активную заявку пользователя (статус принят/отклонён/отозвана)."""
    db = await get_db()
    await db.execute(
        "UPDATE applications SET status = ?, updated_at = ? WHERE user_id = ?",
        (status, _now(), str(user_id)),
    )
    await db.commit()