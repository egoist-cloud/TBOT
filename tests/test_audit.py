# -*- coding: utf-8 -*-
"""Тесты аудита админских действий: таблица, репозиторий и канал."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, get_db, init_db  # noqa: E402
from quantum.repositories import audit as audit_repo  # noqa: E402


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(id=1)


class _FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


def test_audit_table_created():
    async def scenario():
        await init_db()
        try:
            db = await get_db()
            cursor = await db.execute("PRAGMA table_info(audit_log)")
            cols = {row["name"] for row in await cursor.fetchall()}
            await cursor.close()
            assert {"ts", "actor_id", "action", "target_id", "details"} <= cols
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: таблица audit_log создана")


def test_audit_add_and_filters():
    async def scenario():
        await init_db()
        try:
            entry_id = await audit_repo.add_entry(
                actor_id=987654321, actor_name="Админ",
                action="Тестовое действие A",
                target_id=987654322, target_name="Игрок",
                details="подробности")
            assert entry_id > 0

            await audit_repo.add_entry(
                actor_id=987654323, actor_name="Другой",
                action="Тестовое действие B")

            all_rows = await audit_repo.list_entries(limit=50)
            actions = {row["action"] for row in all_rows}
            assert {"Тестовое действие A", "Тестовое действие B"} <= actions

            by_actor = await audit_repo.list_entries(actor_id=987654321)
            assert len(by_actor) == 1
            assert by_actor[0]["target_id"] == "987654322"

            by_target = await audit_repo.list_entries(
                target_id=987654322)
            assert len(by_target) == 1

            by_action = await audit_repo.list_entries(
                action="Тестовое действие A")
            assert len(by_action) == 1

            assert await audit_repo.count_entries() >= 2
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: аудит пишется и фильтруется")


def test_audit_action_writes_db_and_channel():
    async def scenario():
        await init_db()
        try:
            channel = _FakeChannel()
            fake_bot = _FakeBot(channel)
            actor = SimpleNamespace(id=987654344, display_name="Модератор")
            target = SimpleNamespace(id=987654345, display_name="Игрок")

            task = await botmod.audit_action(
                fake_bot, "Проверка аудита", actor=actor, target=target,
                details="Причина: тест")
            if task is not None:
                await task

            rows = await audit_repo.list_entries(actor_id=987654344)
            assert rows and rows[0]["action"] == "Проверка аудита"
            assert rows[0]["target_id"] == "987654345"
            assert "тест" in rows[0]["details"]

            assert channel.sent, "аудит не отправлен в канал"
            embed = channel.sent[0]["kwargs"]["embed"]
            assert embed.color.value == 0x000000
            assert "Проверка аудита" in embed.title
            names = [f.name for f in embed.fields]
            assert "Кто" in names and "Объект" in names and "Детали" in names
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: audit_action пишет в БД и в канал")


def test_audit_command_registered():
    assert botmod.bot.tree.get_command("audit") is not None
    print("  ok: /audit зарегистрирован")


def test_audit_channel_id_default():
    assert botmod.AUDIT_LOG_CHANNEL_ID == 1555204495682568202
    print("  ok: канал аудита по умолчанию задан")
