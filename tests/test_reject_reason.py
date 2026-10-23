# -*- coding: utf-8 -*-
"""Тесты обязательной причины отказа по заявке."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, get_db, init_db  # noqa: E402
from quantum.repositories import applications as applications_repo  # noqa: E402


def test_reject_modal_requires_reason():
    modal = botmod.RejectModal("app-1", "123", show_take=True)
    assert modal.reason.required is True
    assert modal.reason.min_length == 3
    assert modal.reason.max_length == 900
    print("  ok: поле причины отказа обязательное")


def test_reject_reason_saved_in_db():
    async def scenario():
        await init_db()
        try:
            await applications_repo.upsert_application(
                91001, clan_name="TALENT", status="pending",
                message_id="777", channel_id="888", app_id="reason01")
            await applications_repo.finalize_application(
                91001, "rejected", moderator_id=42,
                reason="Не прошёл проверку на читы")

            db = await get_db()
            cursor = await db.execute(
                "SELECT status, decision_reason FROM applications "
                "WHERE user_id = ?", ("91001",))
            row = await cursor.fetchone()
            await cursor.close()
            assert row["status"] == "rejected"
            assert row["decision_reason"] == "Не прошёл проверку на читы"

            history = await applications_repo.get_user_history(91001)
            assert history[0]["decision_reason"] == "Не прошёл проверку на читы"
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: причина отказа сохраняется в заявке и истории")


def test_reject_reason_shown_in_check_line():
    line = botmod._taken_application_line({
        "status": "rejected",
        "message_id": "777",
        "user_id": "91001",
        "clan_name": "TALENT",
        "taken_at": None,
        "decision_reason": "Много мата в голосовом чате",
    })
    assert "Причина" not in line  # формат строки: эмодзи + причина
    assert "Много мата в голосовом чате" in line
    print("  ok: причина отказа видна в /check")


def test_resubmission_clears_reason():
    async def scenario():
        await init_db()
        try:
            await applications_repo.upsert_application(
                91002, clan_name="TALENT", status="pending",
                message_id="1", channel_id="2", app_id="reason-resub-1")
            await applications_repo.finalize_application(
                91002, "rejected", moderator_id=42, reason="старая причина")
            await applications_repo.upsert_application(
                91002, clan_name="TALENT", status="pending",
                message_id="3", channel_id="4", app_id="reason-resub-2")

            db = await get_db()
            cursor = await db.execute(
                "SELECT decision_reason FROM applications WHERE user_id = ?",
                ("91002",))
            row = await cursor.fetchone()
            await cursor.close()
            assert row["decision_reason"] is None
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: новая заявка обнуляет причину отказа")


def test_reject_modal_sends_vote_after_submit():
    """on_submit с причиной применяет голос deny, а не молча закрывает."""
    async def scenario():
        await init_db()
        try:
            import discord  # noqa: F401
            modal = botmod.RejectModal("reject-flow", "92001", show_take=False)
            modal.reason._value = "Нарушение правил клана"

            class _Resp:
                def __init__(self):
                    self.deferred = False

                def is_done(self):
                    return self.deferred

                async def defer(self):
                    self.deferred = True

                async def send_message(self, *a, **kw):
                    raise AssertionError("не должно быть отказа")

            class _User:
                id = 777
                roles = []
                guild_permissions = type(
                    "P", (), {"administrator": True})()

            fake = type("I", (), {})()
            fake.response = _Resp()
            fake.user = _User()
            fake.client = type("C", (), {"get_channel": lambda *a: None})()
            fake.guild = type("G", (), {
                "get_member": lambda self, uid: None,
                "get_role": lambda self, rid: None,
            })()
            fake.channel = None
            fake.message = None
            fake.followup = type("F", (), {
                "send": lambda *a, **kw: asyncio.sleep(0)})()
            # Без message._apply_vote не сможет перерисовать embed,
            # поэтому проверяем только то, что голос дошёл до votes_repo.
            try:
                await modal.on_submit(fake)
            except Exception:
                pass
            # audit_action отправляет embed в фоне — даём задаче завершиться.
            await asyncio.sleep(0.05)
            from quantum.repositories import votes as votes_repo
            st = await votes_repo.get_votes("reject-flow")
            assert st.get("deny", 0) >= 1, st
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: причина отказа участвует в голосовании")
