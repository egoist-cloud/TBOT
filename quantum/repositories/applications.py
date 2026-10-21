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
        "SELECT user_id, status, clan_name, message_id, channel_id, app_id, "
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
                             message_id="", channel_id="", app_id="") -> None:
    """Сохраняет/обновляет заявку пользователя (по user_id)."""
    db = await get_db()
    now = _now()
    await db.execute(
        "INSERT INTO applications (user_id, status, clan_name, message_id, "
        "channel_id, app_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "status = excluded.status, clan_name = excluded.clan_name, "
        "message_id = excluded.message_id, channel_id = excluded.channel_id, "
        "app_id = excluded.app_id, updated_at = excluded.updated_at",
        (str(user_id), status, clan_name, message_id, channel_id, app_id, now, now),
    )
    await db.commit()


async def list_pending_applications() -> list:
    """Все заявки на рассмотрении (нужно, чтобы поднять кнопки после рестарта)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT user_id, status, clan_name, message_id, channel_id, app_id, "
        "created_at, updated_at FROM applications WHERE status = 'pending'"
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [dict(r) for r in rows]


async def finalize_application(user_id, status: str) -> None:
    """Закрывает активную заявку пользователя (статус принят/отклонён/отозвана)."""
    db = await get_db()
    await db.execute(
        "UPDATE applications SET status = ?, updated_at = ? WHERE user_id = ?",
        (status, _now(), str(user_id)),
    )
    await db.commit()


async def count_applications() -> int:
    """Общее число заявок в таблице (для панели)."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM applications")
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0


async def count_pending_applications() -> int:
    """Число активных (на рассмотрении) заявок."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) AS c FROM applications WHERE status = 'pending'")
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0