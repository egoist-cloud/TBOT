# -*- coding: utf-8 -*-
"""Тесты репозитория заявок (quantum/repositories/applications.py).

Закрепляют изменения коммита про статусы и CAS:
  * «активная» заявка — это pending И voting (list/count);
  * finalize_application закрывает заявку ровно один раз (CAS по статусу);
  * финальный статус, модератор и причина попадают в последнюю запись
    истории, а прошлые записи не трогаются.

Discord не нужен: работаем с SQLite напрямую через репозиторий.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantum.db import close_db, get_db, init_db  # noqa: E402
from quantum.repositories import applications as repo  # noqa: E402


async def _seed(user_id, app_id, status="pending", clan_name="TALENT"):
    """Создаёт заявку через upsert (он же пишет первую запись истории)."""
    await repo.upsert_application(
        user_id, clan_name=clan_name, status=status,
        message_id=f"msg-{app_id}", channel_id="chan-1", app_id=app_id,
    )


async def _row(user_id):
    """Строка текущей заявки игрока (или None)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT status, moderator_id, decision_reason, app_id "
        "FROM applications WHERE user_id = ?", (str(user_id),))
    row = await cursor.fetchone()
    await cursor.close()
    return dict(row) if row is not None else None


async def _history(user_id):
    return await repo.get_user_history(user_id, limit=50)


async def _taken(user_id):
    """Кто и когда «взял» заявку (applications + последняя запись истории)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT taken_by, taken_at FROM applications WHERE user_id = ?",
        (str(user_id),))
    row = await cursor.fetchone()
    await cursor.close()
    return dict(row) if row is not None else None


# ============================================================
# Активные заявки: pending + voting
# ============================================================
def test_count_pending_includes_voting():
    """Счётчик «в работе» учитывает voting и не считает закрытые заявки."""
    async def scenario():
        await init_db()
        try:
            before = await repo.count_pending_applications()

            await _seed(92001, "cnt-pending", "pending")
            await _seed(92002, "cnt-voting", "voting")
            await _seed(92003, "cnt-accepted", "accepted")
            await _seed(92004, "cnt-rejected", "rejected")

            after = await repo.count_pending_applications()
            assert after - before == 2, (before, after)
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: count_pending_applications считает voting")


def test_list_pending_includes_voting_and_app_id():
    """list_pending_applications отдаёт voting (с app_id) и не отдаёт закрытые."""
    async def scenario():
        await init_db()
        try:
            await _seed(92011, "list-pending", "pending")
            await _seed(92012, "list-voting", "voting")
            await _seed(92013, "list-accepted", "accepted")

            rows = await repo.list_pending_applications()
            by_user = {row["user_id"]: row for row in rows}

            assert "92011" in by_user, by_user.keys()
            assert "92012" in by_user, "voting-заявка должна поднимать кнопки"
            assert "92013" not in by_user

            assert by_user["92012"]["status"] == "voting"
            assert by_user["92012"]["app_id"] == "list-voting"
            assert by_user["92012"]["message_id"] == "msg-list-voting"
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: list_pending_applications включает voting")


def test_get_active_application_statuses():
    """Активная заявка — pending и voting; закрытая и несуществующая — None."""
    async def scenario():
        await init_db()
        try:
            await _seed(92101, "active-pending", "pending")
            await _seed(92102, "active-voting", "voting")
            await _seed(92103, "active-accepted", "accepted")

            pending = await repo.get_active_application(92101)
            assert pending is not None
            assert pending["app_id"] == "active-pending"
            assert pending["status"] == "pending"

            voting = await repo.get_active_application(92102)
            assert voting is not None
            assert voting["app_id"] == "active-voting"

            assert await repo.get_active_application(92103) is None
            assert await repo.get_active_application(92998) is None
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: get_active_application и статусы")


def test_mark_taken_by_app_id_and_message_id():
    """«Взять» работает по app_id, по message_id (fallback) и требует ключ."""
    async def scenario():
        await init_db()
        try:
            await _seed(92111, "take-by-app", "pending")
            await _seed(92112, "take-by-msg", "pending")

            assert await repo.mark_application_taken("", 777) is False
            assert await repo.mark_application_taken(None, 777) is False

            assert await repo.mark_application_taken("take-by-app", 777) is True
            taken = await _taken(92111)
            assert taken["taken_by"] == "777"
            assert taken["taken_at"]

            # Старые заявки без известного app_id ищутся по id сообщения.
            assert await repo.mark_application_taken(
                "msg-take-by-msg", 778) is True
            taken = await _taken(92112)
            assert taken["taken_by"] == "778"

            assert await repo.mark_application_taken(
                "нет-такой-заявки", 779) is False
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: mark_application_taken (app_id / message_id / пустой ключ)")


def test_finalize_closes_voting_application():
    """Заявку в статусе voting тоже можно закрыть (CAS видит её как активную)."""
    async def scenario():
        await init_db()
        try:
            await _seed(92021, "fin-voting", "voting")
            closed = await repo.finalize_application(
                92021, "accepted", moderator_id=501)
            assert closed is True
            row = await _row(92021)
            assert row["status"] == "accepted"
            assert row["moderator_id"] == "501"
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: voting закрывается через finalize")


# ============================================================
# CAS в finalize_application
# ============================================================
def test_finalize_is_compare_and_swap():
    """Второй вызов возвращает False и не перезаписывает решение первого."""
    async def scenario():
        await init_db()
        try:
            await _seed(92031, "cas-basic", "pending")

            first = await repo.finalize_application(
                92031, "accepted", moderator_id=601, reason="")
            assert first is True

            second = await repo.finalize_application(
                92031, "rejected", moderator_id=602, reason="передумали")
            assert second is False, "CAS должен отклонить повторное закрытие"

            row = await _row(92031)
            assert row["status"] == "accepted"
            assert row["moderator_id"] == "601"
            assert row["decision_reason"] is None

            history = await _history(92031)
            assert history[0]["status"] == "accepted"
            assert history[0]["moderator_id"] == "601"
            assert history[0]["decision_reason"] is None
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: CAS не даёт закрыть заявку дважды")


def test_finalize_unknown_user_returns_false():
    """Нет активной заявки — False, история не создаётся."""
    async def scenario():
        await init_db()
        try:
            assert await repo.finalize_application(
                92999, "accepted", moderator_id=1) is False
            assert await _row(92999) is None
            assert await _history(92999) == []
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: finalize без заявки = False")


def test_finalize_after_close_returns_false():
    """Повторный вызов после rejected тоже False, причина не меняется."""
    async def scenario():
        await init_db()
        try:
            await _seed(92041, "cas-reject", "pending")
            assert await repo.finalize_application(
                92041, "rejected", moderator_id=701,
                reason="не прошёл проверку") is True
            assert await repo.finalize_application(
                92041, "accepted", moderator_id=702) is False

            row = await _row(92041)
            assert row["status"] == "rejected"
            assert row["decision_reason"] == "не прошёл проверку"
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: отклонённую заявку нельзя переиграть")


# ============================================================
# История заявок
# ============================================================
def test_finalize_updates_only_last_history_row():
    """Финальный статус уходит в последнюю запись истории, старая не меняется."""
    async def scenario():
        await init_db()
        try:
            await _seed(92051, "hist-first", "pending")
            await _seed(92051, "hist-second", "pending")

            history = await _history(92051)
            assert len(history) == 2, history
            assert history[0]["app_id"] == "hist-second"
            assert history[1]["app_id"] == "hist-first"

            assert await repo.finalize_application(
                92051, "accepted", moderator_id=801) is True

            history = await _history(92051)
            assert history[0]["app_id"] == "hist-second"
            assert history[0]["status"] == "accepted"
            assert history[0]["moderator_id"] == "801"
            # Прошлая заявка остаётся в истории как есть.
            assert history[1]["app_id"] == "hist-first"
            assert history[1]["status"] == "pending"
            assert history[1]["moderator_id"] is None
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: обновляется только последняя запись истории")


def test_finalize_stores_reason_trimmed_and_limited():
    """Причина обрезается по краям и ограничена 900 символами."""
    async def scenario():
        await init_db()
        try:
            await _seed(92061, "hist-reason", "pending")
            long_reason = "  " + "я" * 1000 + "  "
            assert await repo.finalize_application(
                92061, "rejected", moderator_id=901,
                reason=long_reason) is True

            row = await _row(92061)
            assert row["decision_reason"] == "я" * 900
            assert row["decision_reason"].startswith("я")
            assert not row["decision_reason"].endswith(" ")

            history = await _history(92061)
            assert history[0]["decision_reason"] == "я" * 900
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: причина нормализуется (strip + 900 символов)")


def test_finalize_accepted_has_no_reason():
    """Для принятой заявки пустая причина хранится как NULL."""
    async def scenario():
        await init_db()
        try:
            await _seed(92071, "hist-accept", "pending")
            assert await repo.finalize_application(
                92071, "accepted", moderator_id=902, reason="   ") is True

            row = await _row(92071)
            assert row["decision_reason"] is None
            history = await _history(92071)
            assert history[0]["decision_reason"] is None
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: пустая причина = NULL")


def test_upsert_same_app_id_does_not_duplicate_history():
    """Повторный upsert той же заявки не плодит записи в истории."""
    async def scenario():
        await init_db()
        try:
            await _seed(92081, "dup-app", "pending")
            await _seed(92081, "dup-app", "pending")   # то же app_id

            history = await _history(92081)
            assert len(history) == 1, history
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: без дублей истории при том же app_id")
