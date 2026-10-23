# -*- coding: utf-8 -*-
"""Аудит админских и модераторских действий.

Каждое важное действие (привязка SteamID, выдача/снятие прав, запреты,
решение по заявке, синхронизация ролей) попадает в таблицу ``audit_log``.
Копия записи публикуется в отдельный канал ``AUDIT_LOG_CHANNEL_ID`` — это
делает уже ``script.audit_action``, чтобы репозиторий не зависел от discord.

Таблица append-only: записи не редактируются. ``prune`` удаляет старые
строки, чтобы база не росла бесконечно.
"""
from __future__ import annotations

import datetime

from ..db import get_db


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


async def add_entry(*, actor_id=None, actor_name="", action: str,
                    target_id=None, target_name="", details="") -> int:
    """Добавляет запись в аудит. Возвращает id записи.

    Ни одно поле не обязательно, кроме ``action``: аудит не должен
    ломать основное действие, если у актора/цели нет Discord-объекта.
    """
    action = (action or "").strip()
    if not action:
        raise ValueError("audit action is empty")
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO audit_log "
        "(ts, actor_id, actor_name, action, target_id, target_name, details) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            _now(),
            str(actor_id) if actor_id else None,
            (actor_name or "")[:200],
            action[:200],
            str(target_id) if target_id else None,
            (target_name or "")[:200],
            (details or "")[:1900],
        ),
    )
    await db.commit()
    return int(cursor.lastrowid or 0)


async def list_entries(*, limit: int = 20, offset: int = 0,
                       actor_id=None, target_id=None,
                       action: str | None = None) -> list:
    """Последние записи аудита (новые сверху) с необязательными фильтрами."""
    limit = max(1, min(int(limit or 20), 100))
    offset = max(0, int(offset or 0))
    where, params = [], []
    if actor_id:
        where.append("actor_id = ?")
        params.append(str(actor_id))
    if target_id:
        where.append("target_id = ?")
        params.append(str(target_id))
    if action:
        where.append("action = ?")
        params.append(str(action))
    sql = ("SELECT id, ts, actor_id, actor_name, action, target_id, "
           "target_name, details FROM audit_log")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    db = await get_db()
    cursor = await db.execute(sql, tuple(params))
    rows = await cursor.fetchall()
    await cursor.close()
    return [dict(r) for r in rows]


async def count_entries() -> int:
    """Общее число записей аудита (для /health и панели)."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM audit_log")
    row = await cursor.fetchone()
    await cursor.close()
    return int(row["c"]) if row is not None else 0


async def prune(keep_days: int = 90) -> int:
    """Удаляет записи старше ``keep_days`` дней. Возвращает число удалённых."""
    keep_days = max(1, int(keep_days or 90))
    cutoff = (datetime.datetime.now(datetime.UTC)
              - datetime.timedelta(days=keep_days)).isoformat()
    db = await get_db()
    cursor = await db.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
    await db.commit()
    return int(cursor.rowcount or 0)
