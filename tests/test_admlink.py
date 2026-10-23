# -*- coding: utf-8 -*-
"""/admlink: перенос привязки и синхронизация ролей.

Раньше команда делала обычный add_link, а steam_id в таблице links
уникален: если игрок перешёл на новый Discord, привязка падала с
UNIQUE constraint. Здесь проверяем, что админская команда переносит
SteamID у старого владельца и сразу обновляет роли.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, get_db, init_db  # noqa: E402


async def _reset_links() -> None:
    """Тесты используют общую временную БД — чистим привязки перед сценарием."""
    db = await get_db()
    await db.execute("DELETE FROM links")
    await db.commit()


STEAM_OLD = "76561198000000101"
STEAM_SAME = "76561198000000102"
STEAM_NOT_FOUND = "76561198000000103"


class _FakeResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True

    def is_done(self):
        return self.deferred

    async def send_message(self, *args, **kwargs):
        pass


class _FakeFollowup:
    async def send(self, *args, **kwargs):
        pass


class _FakeInteraction:
    def __init__(self):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.guild = object()
        self.client = object()
        self.user = SimpleNamespace(
            id=9001,
            display_name="Admin",
            mention="<@9001>",
            guild_permissions=SimpleNamespace(administrator=True),
        )


class _FakeMember:
    def __init__(self, member_id, name="Player"):
        self.id = member_id
        self.bot = False
        self.display_name = name
        self.mention = f"<@{member_id}>"
        self.roles = []


def _capture(monkeypatch):
    sent, logs, errors, closed = [], [], [], []

    async def fake_safe_send(target, *args, **kwargs):
        sent.append((args, kwargs))

    async def fake_log_to_channel(_bot, text):
        logs.append(text)

    async def fake_log_error(_bot, context, exc):
        errors.append((context, exc))

    async def fake_close(_i, text=None, ephemeral=False):
        closed.append(text)

    monkeypatch.setattr(botmod, "safe_send", fake_safe_send)
    monkeypatch.setattr(botmod, "log_to_channel", fake_log_to_channel)
    monkeypatch.setattr(botmod, "log_error", fake_log_error)
    monkeypatch.setattr(botmod, "close_interaction", fake_close)
    return sent, logs, errors, closed


def _sent_embed(sent):
    for _args, kwargs in sent:
        if "embed" in kwargs:
            return kwargs["embed"]
    return None


def _sent_text(sent):
    return "\n".join(str(args[0]) for args, _kwargs in sent if args)


def test_transfer_link_moves_ownership():
    """transfer_link снимает SteamID у прежнего владельца."""
    async def scenario():
        await init_db()
        await _reset_links()
        try:
            await botmod.links_repo.add_link(111, STEAM_OLD)
            previous = await botmod.links_repo.transfer_link(222, STEAM_OLD)
            assert previous == "111"
            assert await botmod.links_repo.get_link(222) == STEAM_OLD
            assert await botmod.links_repo.get_link(111) is None
            assert await botmod.links_repo.get_owner_of(STEAM_OLD) == "222"
        finally:
            await close_db()

    asyncio.run(scenario())


def test_transfer_link_same_owner_is_noop():
    """Повторная привязка тому же Discord не считается переносом."""
    async def scenario():
        await init_db()
        await _reset_links()
        try:
            await botmod.links_repo.add_link(333, STEAM_SAME)
            previous = await botmod.links_repo.transfer_link(333, STEAM_SAME)
            assert previous is None
            assert await botmod.links_repo.get_link(333) == STEAM_SAME
        finally:
            await close_db()

    asyncio.run(scenario())


def test_admlink_transfers_and_syncs_roles(monkeypatch):
    """Команда переносит SteamID и вызывает обе синхронизации ролей."""
    sent, logs, errors, closed = _capture(monkeypatch)
    sync_calls = []

    async def fake_user(_sid):
        return {"id": 7, "nick": "Miki"}

    async def fake_sync(_guild, member, steam_id, user=None):
        sync_calls.append(("role", member.id, steam_id, user))
        return f"🔄 {member.display_name}: роль обновлена"

    async def fake_clan(_guild, member, steam_id, user=None):
        sync_calls.append(("clan", member.id, steam_id, user))
        return f"🔄 {member.display_name}: роль клана обновлена"

    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)
    monkeypatch.setattr(botmod, "sync_role_by_id", fake_sync)
    monkeypatch.setattr(botmod, "sync_clan_role_by_id", fake_clan)

    async def scenario():
        await init_db()
        await _reset_links()
        try:
            await botmod.links_repo.add_link(111, STEAM_OLD)
            i = _FakeInteraction()
            member = _FakeMember(222)
            await botmod.admlink.callback(i, member, f" {STEAM_OLD} ")

            assert await botmod.links_repo.get_link(222) == STEAM_OLD
            assert await botmod.links_repo.get_link(111) is None
            assert [c[0] for c in sync_calls] == ["role", "clan"]
            assert all(c[3].get("nick") == "Miki" for c in sync_calls)

            embed = _sent_embed(sent)
            assert embed is not None
            assert embed.color.value == 0x000000
            fields = {f.name: f.value for f in embed.fields}
            assert "<@111>" in fields["Действие"]
            assert "перенесена" in fields["Действие"]
            assert "Miki" == fields["Ник на cs2red"]
            assert "роль обновлена" in fields["Роли"]
            assert "роль клана обновлена" in fields["Роли"]
            assert not errors and not closed
            assert logs and "перенесена" in logs[0]
        finally:
            await close_db()

    asyncio.run(scenario())


def test_admlink_same_owner_updates(monkeypatch):
    """Если SteamID уже у этого же Discord, действие — «обновлена»."""
    sent, _logs, errors, closed = _capture(monkeypatch)

    async def fake_user(_sid):
        return {"id": 8, "nick": "Same"}

    async def fake_sync(*_a, **_k):
        return "ℹ️ без изменений"

    async def fake_clan(*_a, **_k):
        return "ℹ️ без изменений"

    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)
    monkeypatch.setattr(botmod, "sync_role_by_id", fake_sync)
    monkeypatch.setattr(botmod, "sync_clan_role_by_id", fake_clan)

    async def scenario():
        await init_db()
        await _reset_links()
        try:
            await botmod.links_repo.add_link(333, STEAM_SAME)
            i = _FakeInteraction()
            await botmod.admlink.callback(i, _FakeMember(333), STEAM_SAME)
            embed = _sent_embed(sent)
            fields = {f.name: f.value for f in embed.fields}
            assert fields["Действие"] == "обновлена"
            assert not errors and not closed
        finally:
            await close_db()

    asyncio.run(scenario())


def test_admlink_saves_link_when_cs2red_not_found(monkeypatch):
    """Привязка сохраняется, даже если cs2red игрока не нашёл."""
    sent, _logs, errors, closed = _capture(monkeypatch)
    sync_calls = []

    async def fake_user(_sid):
        return {"_not_found": True}

    async def fake_sync(*_a, **_k):
        sync_calls.append("role")

    async def fake_clan(*_a, **_k):
        sync_calls.append("clan")

    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)
    monkeypatch.setattr(botmod, "sync_role_by_id", fake_sync)
    monkeypatch.setattr(botmod, "sync_clan_role_by_id", fake_clan)

    async def scenario():
        await init_db()
        await _reset_links()
        try:
            i = _FakeInteraction()
            await botmod.admlink.callback(i, _FakeMember(444), STEAM_NOT_FOUND)
            assert await botmod.links_repo.get_link(444) == STEAM_NOT_FOUND
            assert sync_calls == [], "роли не должны меняться без данных cs2red"
            fields = {f.name: f.value for f in _sent_embed(sent).fields}
            assert "не найден" in fields["Роли"]
            assert not errors and not closed
        finally:
            await close_db()

    asyncio.run(scenario())


def test_admlink_rejects_bad_steam_id(monkeypatch):
    """Неверный SteamID отклоняется до записи в БД."""
    sent, _logs, errors, closed = _capture(monkeypatch)

    async def scenario():
        await init_db()
        await _reset_links()
        try:
            i = _FakeInteraction()
            await botmod.admlink.callback(i, _FakeMember(555), "123")
            assert await botmod.links_repo.get_link(555) is None
            assert "Неверный SteamID" in _sent_text(sent)
            assert not errors and not closed
        finally:
            await close_db()

    asyncio.run(scenario())
