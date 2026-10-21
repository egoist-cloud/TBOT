# -*- coding: utf-8 -*-
"""Тесты /status: личная статистика модератора по заявкам."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, init_db  # noqa: E402
from quantum.repositories import applications as applications_repo  # noqa: E402
from quantum.repositories import votes as votes_repo  # noqa: E402


class _FakeTarget:
    """Заглушка участника Discord для сборки Embed."""

    display_name = "Модератор"
    display_avatar = SimpleNamespace(url="https://cdn.discordapp.com/embed/avatars/1.png")


async def _seed(app_id, applicant, status="pending", moderator=None):
    """Создаёт заявку и (если нужно) закрывает её решением модератора."""
    await applications_repo.upsert_application(
        applicant, clan_name="TALENT", status="pending",
        message_id="111", channel_id="222", app_id=app_id,
    )
    if status != "pending":
        await applications_repo.finalize_application(
            applicant, status, moderator_id=moderator)


def test_status_command_registered():
    """Команда /status зарегистрирована в дереве команд."""
    cmd = botmod.bot.tree.get_command("status")
    assert cmd is not None, "/status не зарегистрирован"
    print("  ok: команда /status зарегистрирована")


def test_stats_split_accepted_and_rejected():
    """Принятые и отклонённые заявки считаются по moderator_id."""
    async def scenario():
        await init_db()
        try:
            await _seed("st001", 9001, "accepted", 101)
            await _seed("st002", 9002, "accepted", 101)
            await _seed("st003", 9003, "rejected", 101)
            await _seed("st004", 9004, "accepted", 202)

            stats = await applications_repo.get_user_stats(101)
            assert stats["accepted"] == 2, stats
            assert stats["rejected"] == 1, stats
            assert stats["total"] == 3, stats
            assert stats["pending"] == 0, stats

            other = await applications_repo.get_user_stats(202)
            assert other["accepted"] == 1, other
            assert other["total"] == 1, other
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: принято/отклонено считаются по решению модератора")


def test_stats_counts_applications_in_work():
    """«В работе» — заявки, по которым модератор уже голосовал."""
    async def scenario():
        await init_db()
        try:
            app_id = "inwork01"
            await _seed(app_id, 9101, "pending", None)
            await votes_repo.apply_vote(app_id, "accept", 303)

            stats = await applications_repo.get_user_stats(303)
            assert stats["pending"] == 1, stats
            assert stats["accepted"] == 0, stats

            # Кто не голосовал — у того заявок в работе нет.
            other = await applications_repo.get_user_stats(404)
            assert other["pending"] == 0, other
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: заявки в работе считаются по факту голоса")


def test_resubmission_clears_previous_moderator():
    """Повторная заявка обнуляет прежнего решителя."""
    async def scenario():
        await init_db()
        try:
            await _seed("resub01", 9201, "accepted", 505)
            stats = await applications_repo.get_user_stats(505)
            assert stats["accepted"] == 1, stats

            await applications_repo.upsert_application(
                9201, clan_name="TALENT", status="pending",
                message_id="333", channel_id="444", app_id="resub02",
            )
            stats = await applications_repo.get_user_stats(505)
            assert stats["accepted"] == 0, stats
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: повторная заявка не засчитывается старому модератору")


def test_status_embed_layout():
    """Embed профиля: эффективность, категории, статус, список заявок."""
    stats = {
        "accepted": 7, "rejected": 2, "total": 9,
        "clan_main": 5, "clan_other": 2,
        "pending": 1, "pending_total": 3,
        "in_work": [{"app_id": "abc", "user_id": "42", "message_id": "111"}],
    }
    embed = botmod._build_status_embed(_FakeTarget(), stats)

    assert embed.color.value == 0, "embed должен быть чёрным"
    assert embed.title == "👤 Профиль: Модератор", embed.title
    assert embed.thumbnail.url == _FakeTarget.display_avatar.url

    fields = {f.name: f.value for f in embed.fields}
    efficiency = fields["📊 Эффективность"]
    assert "**Принято заявок:** 7" in efficiency
    assert "**Отклонено заявок:** 2" in efficiency
    assert "**Всего обработано:** 9" in efficiency

    categories = fields["📂 По категориям"]
    assert "**Заявок в клан TALENT:** 5" in categories
    assert "**Заявок в другие кланы:** 2" in categories

    status = fields["📊 Статус заявок"]
    assert "**В работе:** 1" in status
    assert "**В работе в клане:** 3" in status

    assert fields["🛠 Заявки в работе"] == "`ticket-111`"
    print("  ok: embed профиля собран по формату")


def test_status_embed_empty_work_placeholder():
    """Без заявок в работе — заглушка в списке."""
    stats = {"accepted": 0, "rejected": 0, "total": 0,
             "clan_main": 0, "clan_other": 0,
             "pending": 0, "pending_total": 0, "in_work": []}
    embed = botmod._build_status_embed(_FakeTarget(), stats)
    fields = {f.name: f.value for f in embed.fields}
    assert fields["🛠 Заявки в работе"] == "Нет активных заявок"
    print("  ok: заглушка для пустого списка заявок")


def test_stats_split_by_clan():
    """Решённые заявки делятся на основной клан и другие."""
    async def scenario():
        await init_db()
        try:
            await applications_repo.upsert_application(
                9301, clan_name="TALENT", status="pending",
                message_id="1", channel_id="2", app_id="cl01")
            await applications_repo.finalize_application(
                9301, "accepted", moderator_id=606)

            await applications_repo.upsert_application(
                9302, clan_name="TALENT", status="pending",
                message_id="3", channel_id="4", app_id="cl02")
            await applications_repo.finalize_application(
                9302, "rejected", moderator_id=606)

            await applications_repo.upsert_application(
                9303, clan_name="OTHER", status="pending",
                message_id="5", channel_id="6", app_id="cl03")
            await applications_repo.finalize_application(
                9303, "accepted", moderator_id=606)

            stats = await applications_repo.get_user_stats(606)
            assert stats["accepted"] == 2, stats
            assert stats["rejected"] == 1, stats
            assert stats["total"] == 3, stats
            assert stats["clan_main"] == 2, stats
            assert stats["clan_other"] == 1, stats
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: заявки разбиты по кланам")


def test_stats_in_work_list_contains_ids():
    """Список заявок в работе содержит идентификаторы заявок."""
    async def scenario():
        await init_db()
        try:
            app_id = "work001"
            await applications_repo.upsert_application(
                9401, clan_name="TALENT", status="pending",
                message_id="555", channel_id="666", app_id=app_id)
            await votes_repo.apply_vote(app_id, "accept", 707)

            stats = await applications_repo.get_user_stats(707)
            assert stats["pending"] == 1, stats
            assert stats["pending_total"] >= 1, stats
            assert len(stats["in_work"]) == 1, stats
            item = stats["in_work"][0]
            assert item["message_id"] == "555", item
            assert item["app_id"] == app_id, item
            assert item["user_id"] == "9401", item
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: список заявок в работе содержит ID")
