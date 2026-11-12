# -*- coding: utf-8 -*-
"""Тесты /search: история заявок игрока в клан.

В таблице applications хранится только последняя заявка игрока
(user_id — PRIMARY KEY), поэтому полная история ведётся в отдельной
таблице application_history.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, get_db, init_db  # noqa: E402
from quantum.repositories import applications as applications_repo  # noqa: E402
from quantum.repositories import moderation as moderation_repo  # noqa: E402


class _FakeTarget:
    """Заглушка участника Discord для сборки Embed."""

    display_name = "Игрок"
    display_avatar = SimpleNamespace(url="https://cdn.discordapp.com/embed/avatars/2.png")


class _FakeMember:
    def __init__(self, member_id, name="Игрок"):
        self.id = member_id
        self.display_name = name
        self.mention = f"<@{member_id}>"


class _FakeGuild:
    def __init__(self, members=()):
        self._members = {m.id: m for m in members}

    def get_member(self, member_id):
        return self._members.get(member_id)


def test_search_command_registered():
    """Команда /search зарегистрирована в дереве команд."""
    cmd = botmod.bot.tree.get_command("search")
    assert cmd is not None, "/search не зарегистрирован"
    print("  ok: команда /search зарегистрирована")


def test_history_created_with_application():
    """Новая заявка попадает в историю."""
    async def scenario():
        await init_db()
        try:
            await applications_repo.upsert_application(
                7001, clan_name="TALENT", status="pending",
                message_id="9001", channel_id="9002", app_id="hs01")

            history = await applications_repo.get_user_history(7001)
            assert len(history) == 1, history
            item = history[0]
            assert item["app_id"] == "hs01", item
            assert item["status"] == "pending", item
            assert item["clan_name"] == "TALENT", item
            assert item["message_id"] == "9001", item
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: новая заявка попадает в историю")


def test_history_not_duplicated_for_same_app():
    """Повторная запись той же заявки не дублирует историю."""
    async def scenario():
        await init_db()
        try:
            for _ in range(3):
                await applications_repo.upsert_application(
                    7002, clan_name="TALENT", status="pending",
                    message_id="9011", channel_id="9012", app_id="hs02")

            history = await applications_repo.get_user_history(7002)
            assert len(history) == 1, history
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: история не дублируется")


def test_resubmission_adds_new_history_entry():
    """Повторная подача заявки — новая запись в истории."""
    async def scenario():
        await init_db()
        try:
            await applications_repo.upsert_application(
                7003, clan_name="TALENT", status="pending",
                message_id="9021", channel_id="9022", app_id="hs03")
            await applications_repo.finalize_application(
                7003, "rejected", moderator_id=555)
            await applications_repo.upsert_application(
                7003, clan_name="TALENT", status="pending",
                message_id="9023", channel_id="9022", app_id="hs04")

            history = await applications_repo.get_user_history(7003)
            assert len(history) == 2, history
            # Новые сверху.
            assert history[0]["app_id"] == "hs04", history
            assert history[1]["app_id"] == "hs03", history
            # Решение по первой заявке сохранилось в истории.
            assert history[1]["status"] == "rejected", history
            assert history[1]["moderator_id"] == "555", history
            # Новая заявка — снова в работе.
            assert history[0]["status"] == "pending", history
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: повторная заявка добавляет запись в историю")


def test_history_summary():
    """Сводка по истории: всего/принято/отклонено/в работе и по кланам."""
    async def scenario():
        await init_db()
        try:
            await applications_repo.upsert_application(
                7004, clan_name="TALENT", status="pending",
                message_id="9031", channel_id="9032", app_id="hs05")
            await applications_repo.finalize_application(
                7004, "accepted", moderator_id=777)

            await applications_repo.upsert_application(
                7005, clan_name="OTHER", status="pending",
                message_id="9041", channel_id="9042", app_id="hs06")
            await applications_repo.finalize_application(
                7005, "rejected", moderator_id=777)

            await applications_repo.upsert_application(
                7006, clan_name="TALENT", status="pending",
                message_id="9051", channel_id="9052", app_id="hs07")

            summary = await applications_repo.get_history_summary(7006)
            assert summary["total"] == 1, summary
            assert summary["in_work"] == 1, summary

            summary = await applications_repo.get_history_summary(7004)
            assert summary["total"] == 1, summary
            assert summary["accepted"] == 1, summary
            assert summary["clan_main"] == 1, summary

            summary = await applications_repo.get_history_summary(7005)
            assert summary["rejected"] == 1, summary
            assert summary["clan_other"] == 1, summary
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: сводка по истории считается")


def test_history_migrated_from_applications():
    """Текущие заявки переносятся в историю при старте (миграция)."""
    async def scenario():
        await init_db()
        try:
            # Заявка, созданная до появления app_id (как в старой БД).
            db = await get_db()
            await db.execute(
                "INSERT INTO applications (user_id, status, clan_name, "
                "message_id, channel_id, app_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("7010", "accepted", "TALENT", "9061", "9062", None,
                 "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
            await db.commit()

            # Повторная инициализация = повторная миграция (идемпотентно).
            await init_db()

            history = await applications_repo.get_user_history(7010)
            assert len(history) == 1, history
            assert history[0]["status"] == "accepted", history
            # Без app_id подставляется стабильный идентификатор.
            assert history[0]["app_id"] == "legacy-9061", history

            # Ещё один запуск — дублей не появляется.
            await init_db()
            history = await applications_repo.get_user_history(7010)
            assert len(history) == 1, history
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: старые заявки переносятся в историю без дублей")


def test_search_embed_layout():
    """Embed истории: сводка, кланы, список заявок."""
    summary = {"total": 3, "accepted": 1, "rejected": 1, "in_work": 1,
               "clan_main": 2, "clan_other": 1}
    history = [
        {"status": "pending", "message_id": "111", "app_id": "a1",
         "clan_name": "TALENT", "created_at": "2026-10-21T17:51:00+00:00",
         "moderator_id": None},
        {"status": "accepted", "message_id": "222", "app_id": "a2",
         "clan_name": "TALENT", "created_at": "2026-10-20T10:00:00+00:00",
         "moderator_id": "42"},
    ]
    embed = botmod._build_search_embed(_FakeTarget(), summary, history)

    assert embed.color.value == 0, "embed должен быть чёрным"
    assert embed.title == "🔎 Заявки: Игрок", embed.title
    assert embed.thumbnail.url == _FakeTarget.display_avatar.url

    fields = {f.name: f.value for f in embed.fields}
    stats = fields["📊 Сводка"]
    assert "**Всего заявок:** 3" in stats
    assert "**Принято:** 1" in stats
    assert "**Отклонено:** 1" in stats
    assert "**В работе:** 1" in stats

    clans = fields["📂 По кланам"]
    assert "**В клан TALENT:** 2" in clans
    assert "**В другие кланы:** 1" in clans

    items = fields["📜 История заявок"]
    assert "`ticket-111`" in items
    assert "⏳" in items
    assert "✅" in items
    assert "<@42>" in items
    assert "21.10.2026 17:51" in items
    print("  ok: embed истории собран по формату")


def test_search_embed_empty():
    """Без заявок — заглушка."""
    embed = botmod._build_search_embed(
        _FakeTarget(), {"total": 0, "accepted": 0, "rejected": 0,
                        "in_work": 0, "clan_main": 0, "clan_other": 0}, [])
    fields = {f.name: f.value for f in embed.fields}
    assert fields["📜 История заявок"] == "Заявок не найдено."
    print("  ok: заглушка для игрока без заявок")


def test_collect_bot_mods_is_awaitable():
    """Регресс: /admins падал из-за забытого await (coroutine не iterable)."""
    async def scenario():
        await init_db()
        try:
            member = _FakeMember(4242, "Модератор")
            guild = _FakeGuild([member])
            await moderation_repo.add_moderator(4242)

            rows = await botmod._collect_bot_mods(guild)
            assert isinstance(rows, list), rows
            assert len(rows) == 1, rows
            assert "<@4242>" in rows[0], rows
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: модераторы бота собираются без ошибки")
