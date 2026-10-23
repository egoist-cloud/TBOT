# -*- coding: utf-8 -*-
"""/moders: панель с правами, приоритетом и профилями cs2red."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, get_db, init_db  # noqa: E402


STEAM_FULL = "76561198000000201"


class _FakeResponse:
    def __init__(self):
        self.done = False

    async def defer(self, **kwargs):
        self.done = True

    def is_done(self):
        return self.done


class _FakeFollowup:
    pass


class _FakeMember:
    def __init__(self, member_id, name):
        self.id = member_id
        self.display_name = name
        self.mention = f"<@{member_id}>"


class _FakeGuild:
    def __init__(self, members=()):
        self._members = {m.id: m for m in members}

    def get_member(self, member_id):
        return self._members.get(member_id)


class _FakeInteraction:
    def __init__(self, guild=None):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.guild = guild or _FakeGuild()
        self.client = object()
        self.user = SimpleNamespace(
            id=9001,
            mention="<@9001>",
            guild_permissions=SimpleNamespace(administrator=True),
        )


async def _reset() -> None:
    db = await get_db()
    await db.execute("DELETE FROM moderators")
    await db.execute("DELETE FROM links")
    await db.execute("DELETE FROM players")
    await db.commit()


def _capture(monkeypatch):
    sent = []

    async def fake_safe_send(_target, *args, **kwargs):
        sent.append((args, kwargs))

    async def fake_log_error(_bot, context, exc):
        raise AssertionError(f"неожиданная ошибка {context}: {exc}")

    async def fake_close(_i, text=None, **_kwargs):
        raise AssertionError(f"неожиданное закрытие: {text}")

    monkeypatch.setattr(botmod, "safe_send", fake_safe_send)
    monkeypatch.setattr(botmod, "log_error", fake_log_error)
    monkeypatch.setattr(botmod, "close_interaction", fake_close)
    return sent


def test_moders_panel_has_priority_and_cs2red_links(monkeypatch):
    sent = _capture(monkeypatch)

    async def scenario():
        await init_db()
        await _reset()
        try:
            await botmod.moderation_repo.set_moderator_commands(2001, ["*"])
            await botmod.links_repo.add_link(2001, STEAM_FULL)
            await botmod.players_repo.save_player(
                STEAM_FULL, {"cs2red_nick": "Miki"})
            await botmod.moderation_repo.set_moderator_commands(
                2002, ["check", "status"])
            await botmod.moderation_repo.set_moderator_commands(2003, [])

            guild = _FakeGuild([
                _FakeMember(2001, "Full"),
                _FakeMember(2002, "Partial"),
                _FakeMember(2003, "NoRights"),
            ])
            i = _FakeInteraction(guild)
            await botmod.moders.callback(i)

            assert len(sent) == 1
            _args, kwargs = sent[0]
            embeds = kwargs["embeds"]
            assert len(embeds) == 1
            embed = embeds[0]
            assert embed.color.value == 0x000000
            assert embed.title == "🛠 Модераторы — 3"
            assert embed.footer.text.startswith("Quantum • ")
            assert "Всего: **3**" in embed.description
            assert "С профилем cs2red: **1** из **3**" in embed.description

            names = [field.name for field in embed.fields]
            values = [field.value for field in embed.fields]
            assert any(name.startswith("👑 Full") for name in names)
            assert "📊 15/15" in names
            assert "📊 2/15" in names
            assert "📊 0/15" in names
            assert any("Приоритет: **4/4**" in value for value in values)
            assert any("Приоритет: **1/4**" in value for value in values)
            assert any("Приоритет: **0/4**" in value for value in values)
            assert any(STEAM_FULL in value for value in values)
            assert any("[Miki](" in value for value in values)
            assert any("не привязан" in value for value in values)
        finally:
            await _reset()
            await close_db()

    asyncio.run(scenario())


def test_moders_panel_splits_many_people(monkeypatch):
    sent = _capture(monkeypatch)

    async def scenario():
        await init_db()
        await _reset()
        try:
            for uid in range(2100, 2109):
                await botmod.moderation_repo.set_moderator_commands(uid, ["*"])
            i = _FakeInteraction()
            await botmod.moders.callback(i)

            embeds = sent[0][1]["embeds"]
            assert len(embeds) == 2
            assert embeds[0].title.endswith("(1/2)")
            assert embeds[1].title.endswith("(2/2)")
            assert len(embeds[0].fields) == 24
            assert len(embeds[1].fields) == 3
            assert "Часть **2** из **2**" in embeds[1].description
        finally:
            await _reset()
            await close_db()

    asyncio.run(scenario())
