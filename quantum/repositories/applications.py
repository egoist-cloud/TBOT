# -*- coding: utf-8 -*-
"""Репозиторий заявок в клан: таблица applications (анти-дубликаты)."""
from __future__ import annotations

import datetime
import logging
import os

from ..db import get_db
from .votes import parse_voters

log = logging.getLogger("quantum.applications")


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


async def get_active_application(user_id) -> dict | None:
    """Возвращает активную заявку пользователя или None.

    Активной считаем заявку со статусом pending (на рассмотрении).
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT user_id, status, clan_name, message_id, channel_id, app_id, "
        "taken_by, taken_at, created_at, updated_at FROM applications "
        "WHERE user_id = ? AND status IN ('pending', 'voting')",
        (str(user_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return dict(row)


async def get_rejection_cooldown(user_id, days: int) -> dict | None:
    """Кулдаун после отказа по заявке.

    Возвращает ``{"reason", "rejected_at", "until"}``, пока повторная
    заявка запрещена, иначе None. Правило одно для любого отказа, включая
    автоотклонение по TTL: причина и дата следующей попытки сохраняются.
    """
    days = int(days or 0)
    if days <= 0:
        return None
    db = await get_db()
    cursor = await db.execute(
        "SELECT status, decision_reason, updated_at "
        "FROM applications WHERE user_id = ?", (str(user_id),))
    row = await cursor.fetchone()
    await cursor.close()
    if row is None or row["status"] != "rejected":
        return None
    try:
        rejected_at = datetime.datetime.fromisoformat(str(row["updated_at"]))
    except (TypeError, ValueError):
        return None
    if rejected_at.tzinfo is None:
        rejected_at = rejected_at.replace(tzinfo=datetime.UTC)
    until = rejected_at + datetime.timedelta(days=days)
    if until <= datetime.datetime.now(datetime.UTC):
        return None
    return {
        "reason": (row["decision_reason"] or "").strip(),
        "rejected_at": rejected_at.isoformat(),
        "until": until.isoformat(),
    }


async def upsert_application(user_id, *, clan_name="", status="pending",
                             message_id="", channel_id="", app_id="") -> None:
    """Сохраняет/обновляет заявку пользователя (по user_id).

    moderator_id при новой заявке сбрасывается: пользователь мог подать
    заявку повторно, и старый решитель не должен засчитываться в его
    статистику за новую заявку. Вместе с ним сбрасывается ``taken_by``:
    «Взять» относится только к конкретной заявке.

    Каждая новая заявка (app_id отличается от предыдущей) попадает в
    application_history — в applications хранится только последняя.
    """
    db = await get_db()
    now = _now()
    uid = str(user_id)
    cursor = await db.execute(
        "SELECT app_id FROM applications WHERE user_id = ?", (uid,))
    prev = await cursor.fetchone()
    await cursor.close()
    prev_app_id = prev["app_id"] if prev is not None else None

    await db.execute(
        "INSERT INTO applications (user_id, status, clan_name, message_id, "
        "channel_id, app_id, moderator_id, taken_by, taken_at, "
        "decision_reason, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "status = excluded.status, clan_name = excluded.clan_name, "
        "message_id = excluded.message_id, channel_id = excluded.channel_id, "
        "app_id = excluded.app_id, moderator_id = NULL, "
        "taken_by = NULL, taken_at = NULL, decision_reason = NULL, "
        "updated_at = excluded.updated_at",
        (uid, status, clan_name, message_id, channel_id, app_id, now, now),
    )
    await db.commit()

    if app_id and app_id != prev_app_id:
        await add_history(user_id, clan_name=clan_name, status=status,
                          message_id=message_id, channel_id=channel_id,
                          app_id=app_id)


async def add_history(user_id, *, clan_name="", status="pending", message_id="",
                      channel_id="", app_id="", moderator_id=None,
                      taken_by=None, taken_at=None) -> None:
    """Добавляет запись в историю заявок (append-only)."""
    db = await get_db()
    now = _now()
    await db.execute(
        "INSERT OR IGNORE INTO application_history "
        "(user_id, clan_name, status, message_id, channel_id, app_id, "
        "moderator_id, taken_by, taken_at, decision_reason, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(user_id), clan_name, status, message_id, channel_id, app_id,
         str(moderator_id) if moderator_id else None,
         str(taken_by) if taken_by else None,
         taken_at or (now if taken_by else None),
         None, now, now),
    )
    await db.commit()


async def get_user_history(user_id, limit: int = 25) -> list:
    """История заявок игрока: новые сверху."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT user_id, clan_name, status, message_id, app_id, moderator_id, "
        "taken_by, taken_at, decision_reason, created_at, updated_at "
        "FROM application_history "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (str(user_id), int(limit)),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [dict(r) for r in rows]


async def get_history_summary(user_id) -> dict:
    """Сводка по истории заявок игрока.

    Возвращает: total, accepted, rejected, in_work, clan_main, clan_other.
    """
    db = await get_db()
    uid = str(user_id)
    main_clan = (DEFAULT_CLAN_NAME or "").strip().lower()
    summary = {"total": 0, "accepted": 0, "rejected": 0, "in_work": 0,
               "clan_main": 0, "clan_other": 0}

    cursor = await db.execute(
        "SELECT status, COUNT(*) AS c FROM application_history "
        "WHERE user_id = ? GROUP BY status", (uid,))
    rows = await cursor.fetchall()
    await cursor.close()
    for row in rows:
        status = row["status"]
        count = row["c"]
        summary["total"] += count
        if status == "accepted":
            summary["accepted"] = count
        elif status == "rejected":
            summary["rejected"] = count
        elif status in ACTIVE_STATUSES:
            summary["in_work"] += count

    cursor = await db.execute(
        "SELECT clan_name, COUNT(*) AS c FROM application_history "
        "WHERE user_id = ? GROUP BY clan_name", (uid,))
    rows = await cursor.fetchall()
    await cursor.close()
    for row in rows:
        name = (row["clan_name"] or "").strip().lower()
        if name and name == main_clan:
            summary["clan_main"] += row["c"]
        else:
            summary["clan_other"] += row["c"]
    return summary


async def list_pending_applications() -> list:
    """Все активные заявки (нужно, чтобы поднять кнопки после рестарта)."""
    db = await get_db()
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    cursor = await db.execute(
        "SELECT user_id, status, clan_name, message_id, channel_id, app_id, "  # nosec B608  # в запрос идут только "?"
        "taken_by, taken_at, created_at, updated_at FROM applications "
        f"WHERE status IN ({placeholders})", ACTIVE_STATUSES
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [dict(r) for r in rows]


async def finalize_application(user_id, status: str, moderator_id=None,
                               reason: str = "") -> bool:
    """Закрывает активную заявку пользователя (статус принят/отклонён/отозвана).

    moderator_id — кто вынес решение (его голос достиг порога). Нужен
    для личной статистики модератора в /status.

    reason — обязательная причина отказа. Для принятых заявок остаётся
    пустой; повторная подача заявки обнуляет её.

    Возвращает True, если заявку закрыл именно этот вызов. UPDATE идёт
    с условием по статусу (CAS): при одновременном голосовании двух
    модераторов закрытие получит только первый, а второй увидит False
    и не станет повторно выдавать роль/закрывать ветку/писать в аудит.
    """
    reason = (reason or "").strip()[:900]
    db = await get_db()
    cursor = await db.execute(
        "UPDATE applications SET status = ?, moderator_id = ?, "
        "decision_reason = ?, updated_at = ? "
        "WHERE user_id = ? AND status IN ('pending', 'voting')",
        (status, str(moderator_id) if moderator_id else None,
         reason or None, _now(), str(user_id)),
    )
    await db.commit()
    if cursor.rowcount == 0:
        # Заявка уже закрыта (двойной клик двух модераторов): историю
        # и побочные эффекты не трогаем.
        return False
    # Дублируем финальный статус в историю заявок (последняя запись игрока).
    try:
        await db.execute(
            "UPDATE application_history SET status = ?, moderator_id = ?, "
            "decision_reason = ?, updated_at = ? "
            "WHERE id = (SELECT id FROM application_history "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 1)",
            (status, str(moderator_id) if moderator_id else None,
             reason or None, _now(), str(user_id)),
        )
        await db.commit()
    except Exception as e:
        log.warning("не удалось обновить историю заявки: %s", e)
    return True


async def mark_application_taken(app_id, moderator_id) -> bool:
    """Запоминает, что модератор «взял» заявку.

    app_id — идентификатор из кнопки; для старых заявок без app_id
    допускается id сообщения. Возвращает True, если удалось обновить
    хотя бы одну запись.
    """
    app_key = str(app_id or "").strip()
    if not app_key:
        return False
    db = await get_db()
    now = _now()
    uid = str(moderator_id)
    updated = False

    # Новые заявки ищем по app_id; старые — по id сообщения. Условие
    # по message_id нужно только как fallback, чтобы не задеть чужие
    # заявки с пустым app_id.
    for table in ("applications", "application_history"):
        sql = (
            f"UPDATE {table} SET taken_by = ?, taken_at = ?, updated_at = ? "  # nosec B608  # table из фиксированного кортежа
            f"WHERE app_id = ?"
        )
        params = (uid, now, now, app_key)
        cursor = await db.execute(sql, params)
        if cursor.rowcount:
            updated = True
        else:
            cursor = await db.execute(
                f"UPDATE {table} SET taken_by = ?, taken_at = ?, updated_at = ? "  # nosec B608  # table из фиксированного кортежа
                f"WHERE message_id = ?",
                (uid, now, now, app_key),
            )
            if cursor.rowcount:
                updated = True
    await db.commit()
    return updated


async def get_taken_applications(moderator_id, limit: int | None = 50) -> list:
    """Все заявки, которые модератор когда-либо «взял».

    История хранит и активные, и завершённые заявки, поэтому /check
    показывает полный список, а не только текущие. При ``limit=None``
    возвращаются все записи.
    """
    db = await get_db()
    sql = (
        "SELECT user_id, clan_name, status, message_id, channel_id, app_id, "
        "moderator_id, taken_by, taken_at, decision_reason, "
        "created_at, updated_at "
        "FROM application_history WHERE taken_by = ? "
        "ORDER BY COALESCE(taken_at, updated_at) DESC, id DESC"
    )
    params: tuple = (str(moderator_id),)
    if limit is not None:
        sql += " LIMIT ?"
        params += (int(limit),)
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    await cursor.close()
    return [dict(r) for r in rows]


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
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    cursor = await db.execute(
        f"SELECT COUNT(*) AS c FROM applications "  # nosec B608  # в запрос идут только "?"
        f"WHERE status IN ({placeholders})", ACTIVE_STATUSES)
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0


# Статусы, при которых заявка ещё «в работе».
ACTIVE_STATUSES = ("pending", "voting")

# Основной клан — по нему считаем «заявки в клан» и «заявки в другие кланы».
DEFAULT_CLAN_NAME = (os.getenv("CLAN_NAME") or "TALENT").strip() or "TALENT"


async def get_user_stats(user_id) -> dict:
    """Личная статистика по заявкам для одного пользователя.

    Возвращает:
      accepted      — заявки, финальное решение по которым принял модератор;
      rejected      — заявки, которые он отклонил;
      total         — принято + отклонено (всего обработано);
      clan_main     — из решённых: заявки в основной клан (CLAN_NAME);
      clan_other    — из решённых: заявки в другие кланы;
      pending       — личные заявки «в работе»: модератор уже голосовал,
                      взял заявку кнопкой «Взять» либо она закреплена за ним;
      pending_total — все заявки «в работе» в клане;
      in_work       — список заявок «в работе» (личные): app_id, user_id,
                      message_id, clan_name.
    """
    db = await get_db()
    uid = str(user_id)
    stats = {
        "accepted": 0, "rejected": 0, "total": 0,
        "clan_main": 0, "clan_other": 0,
        "pending": 0, "pending_total": 0, "in_work": [],
    }
    main_clan = (DEFAULT_CLAN_NAME or "").strip().lower()

    # 1. Решения, вынесенные этим модератором.
    cursor = await db.execute(
        "SELECT status, COUNT(*) AS c FROM applications "
        "WHERE moderator_id = ? AND status IN ('accepted', 'rejected') "
        "GROUP BY status", (uid,))
    rows = await cursor.fetchall()
    await cursor.close()
    for row in rows:
        if row["status"] == "accepted":
            stats["accepted"] = row["c"]
        elif row["status"] == "rejected":
            stats["rejected"] = row["c"]
    stats["total"] = stats["accepted"] + stats["rejected"]

    # 2. Разбивка решённых заявок по кланам.
    cursor = await db.execute(
        "SELECT clan_name, COUNT(*) AS c FROM applications "
        "WHERE moderator_id = ? AND status IN ('accepted', 'rejected') "
        "GROUP BY clan_name", (uid,))
    rows = await cursor.fetchall()
    await cursor.close()
    for row in rows:
        name = (row["clan_name"] or "").strip().lower()
        if name and name == main_clan:
            stats["clan_main"] += row["c"]
        else:
            stats["clan_other"] += row["c"]

    # 3. Все заявки «в работе».
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    cursor = await db.execute(
        f"SELECT user_id, message_id, app_id, clan_name, moderator_id, "  # nosec B608  # в запрос идут только "?"
        f"taken_by, taken_at FROM applications "
        f"WHERE status IN ({placeholders})", ACTIVE_STATUSES)
    active = await cursor.fetchall()
    await cursor.close()
    stats["pending_total"] = len(active)
    active_ids = {r["app_id"] for r in active if r["app_id"]}

    # 4. Личные заявки в работе: модератор уже голосовал, «взял» заявку
    # или она закреплена за ним как за решителем (легаси).
    voted: set = set()
    if active_ids:
        cursor = await db.execute(
            "SELECT message_id, accept_voters, deny_voters FROM votes")
        vote_rows = await cursor.fetchall()
        await cursor.close()
        for row in vote_rows:
            if row["message_id"] not in active_ids:
                continue
            if uid in parse_voters(row["accept_voters"], row["deny_voters"]):
                voted.add(row["message_id"])
    for row in active:
        taken_by = row["taken_by"]
        if (row["app_id"] not in voted and row["moderator_id"] != uid
                and taken_by != uid):
            continue
        stats["in_work"].append({
            "app_id": row["app_id"] or "",
            "user_id": row["user_id"] or "",
            "message_id": row["message_id"] or "",
            "clan_name": row["clan_name"] or "",
            "taken_by": taken_by or "",
            "taken_at": row["taken_at"] or "",
        })
    stats["pending"] = len(stats["in_work"])
    return stats