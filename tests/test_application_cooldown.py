# -*- coding: utf-8 -*-
"""Тесты кулдауна после отказа по заявке."""
import asyncio
import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, get_db, init_db  # noqa: E402
from quantum.repositories import applications as applications_repo  # noqa: E402


def _iso(days_ago: float = 0) -> str:
    return (datetime.datetime.now(datetime.UTC)
            - datetime.timedelta(days=days_ago)).isoformat()


def test_rejection_cooldown_has_reason_and_until():
    async def scenario():
        await init_db()
        try:
            await applications_repo.upsert_application(
                93001, clan_name="TALENT", status="pending",
                message_id="1", channel_id="2", app_id="cool-1")
            await applications_repo.finalize_application(
                93001, "rejected", moderator_id=42,
                reason="Не прошёл проверку на читы")

            cooldown = await applications_repo.get_rejection_cooldown(93001, 3)
            assert cooldown is not None
            assert cooldown["reason"] == "Не прошёл проверку на читы"
            assert cooldown["until"] > cooldown["rejected_at"]
            # 0 дней — кулдаун выключен.
            assert await applications_repo.get_rejection_cooldown(93001, 0) is None
        finally:
            await close_db()

    asyncio.run(scenario())


def test_old_rejection_has_no_cooldown_but_auto_rejection_does():
    async def scenario():
        await init_db()
        try:
            await applications_repo.upsert_application(
                93002, clan_name="TALENT", status="pending",
                message_id="3", channel_id="4", app_id="cool-old")
            await applications_repo.finalize_application(
                93002, "rejected", moderator_id=42, reason="старый отказ")
            db = await get_db()
            await db.execute(
                "UPDATE applications SET updated_at = ? WHERE user_id = ?",
                (_iso(days_ago=10), "93002"))
            await db.commit()
            assert await applications_repo.get_rejection_cooldown(
                93002, 3) is None

            # Автоотклонение по TTL — тоже отказ, кулдаун действует.
            await applications_repo.upsert_application(
                93003, clan_name="TALENT", status="pending",
                message_id="5", channel_id="6", app_id="cool-auto")
            await applications_repo.finalize_application(
                93003, "rejected", moderator_id=None,
                reason="Автоотклонение: нет решения за 7 дн.")
            auto = await applications_repo.get_rejection_cooldown(93003, 3)
            assert auto is not None
            assert "Автоотклонение" in auto["reason"]
        finally:
            await close_db()

    asyncio.run(scenario())


def test_cooldown_message_contains_reason_and_date():
    text = botmod._rejection_cooldown_text({
        "reason": "Не прошёл проверку на читы",
        "until": "2026-11-20T12:00:00+00:00",
    })
    assert "Не прошёл проверку на читы" in text
    assert "Следующая попытка" in text
    assert "20.11.2026" in text


def test_rejection_dm_contains_next_attempt(monkeypatch):
    text = botmod._rejection_dm_text("Не прошёл проверку")
    assert "Не прошёл проверку" in text
    assert "Следующая попытка" in text

    monkeypatch.setattr(botmod, "APPLICATION_REJECT_COOLDOWN_DAYS", 0)
    text = botmod._rejection_dm_text("Причина")
    assert "сразу" in text


def test_application_form_blocks_during_cooldown(monkeypatch):
    sent = []

    async def fake_blocked(_uid):
        return False, ""

    async def fake_active(_uid):
        return None

    async def fake_cooldown(_uid, _days):
        return {"reason": "Не прошёл проверку на читы",
                "until": "2026-11-20T12:00:00+00:00"}

    async def fake_safe_send(target, *args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr(botmod, "is_user_blocked", fake_blocked)
    monkeypatch.setattr(botmod.applications_repo, "get_active_application",
                        fake_active)
    monkeypatch.setattr(botmod.applications_repo, "get_rejection_cooldown",
                        fake_cooldown)
    monkeypatch.setattr(botmod, "safe_send", fake_safe_send)

    async def defer(**kwargs):
        return None

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=93010),
        response=SimpleNamespace(defer=defer),
        followup=object(),
        guild=None,
    )
    modal = botmod.QuestionnaireModal("TALENT")

    asyncio.run(modal.on_submit(interaction))

    assert sent, "игрок должен получить объяснение"
    text = sent[0][0][0]
    assert "Не прошёл проверку на читы" in text
    assert "Следующая попытка" in text
    assert "20.11.2026" in text
