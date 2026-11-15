# -*- coding: utf-8 -*-
"""Тесты общего реестра панелей: усыновление, дубли, refresh."""
import asyncio
from types import SimpleNamespace

import discord
import quantum.panels as panels
from quantum.panels import PanelRegistry, PanelSpec


class _Message:
    def __init__(self, message_id, spec=None, *, nested=False, bot=True,
                 forbidden=False):
        self.id = message_id
        self.author = SimpleNamespace(bot=bot)
        self.embeds = []
        self.deleted = False
        self.forbidden = forbidden
        if spec is not None:
            component = SimpleNamespace(content=spec.title)
            self.components = [
                SimpleNamespace(children=[component]) if nested else component
            ]
        else:
            self.components = []

    async def delete(self):
        if self.forbidden:
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"),
                "Missing Permissions")
        self.deleted = True


class _History:
    def __init__(self, messages, error=None):
        self.messages = messages
        self.error = error
        self.limits = []

    def __call__(self, limit=None):
        self.limits.append(limit)

        async def gen():
            if self.error is not None:
                raise self.error
            for message in self.messages:
                yield message

        return gen()


class _Channel:
    def __init__(self, channel_id, messages, *, history_error=None):
        self.id = channel_id
        self.history = _History(messages, history_error)
        self.sent = []

    async def fetch_message(self, message_id):
        for message in self.history.messages:
            if int(message.id) == int(message_id):
                return message
        raise discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "unknown message")


async def _send(channel, embed=None, view=None, allowed_mentions=None):
    message = _Message(900 + len(channel.sent))
    channel.sent.append({"embed": embed, "view": view,
                         "allowed_mentions": allowed_mentions,
                         "message": message})
    return message


def _flags(monkeypatch, initial=None):
    stored = dict(initial or {})

    async def get_flag(name):
        return stored.get(name)

    async def set_flag(name, value):
        stored[name] = value

    monkeypatch.setattr(panels.seen_repo, "get_flag", get_flag)
    monkeypatch.setattr(panels.seen_repo, "set_flag", set_flag)
    return stored


def _client(channel):
    return SimpleNamespace(get_channel=lambda cid: channel
                           if channel and cid == channel.id else None)


def _spec():
    return PanelSpec("test_panel", 42, "🎭 Тест", "test_btn")


def _builder():
    return "embed", "view", None


def test_matches_embed_and_components():
    spec = _spec()
    assert PanelRegistry.matches(spec, _Message(1, spec, nested=True))
    old = _Message(2)
    old.embeds = [SimpleNamespace(title="🎭 Тест — старые условия")]
    assert PanelRegistry.matches(spec, old)
    human = _Message(3, spec, bot=False)
    assert not PanelRegistry.matches(spec, human)
    other = _Message(4)
    other.components = [SimpleNamespace(custom_id="other")]
    assert not PanelRegistry.matches(spec, other)


def test_adopts_existing_panel_without_flag(monkeypatch):
    """Флаг потерян: панель находится в истории и усыновляется."""
    spec = _spec()
    first = _Message(11, spec)
    duplicate = _Message(12, spec)
    channel = _Channel(spec.channel_id, [first, duplicate])
    flags = _flags(monkeypatch)

    result = asyncio.run(
        PanelRegistry(_client(channel), _send, spec).ensure(_builder))

    assert result.status == "exists"
    assert result.message_id == 11
    assert result.deleted == 1
    assert duplicate.deleted and not first.deleted
    assert channel.sent == []
    assert flags["test_panel_message"] == "11"
    assert flags["test_panel_channel"] == "42"


def test_refresh_removes_tracked_and_duplicates(monkeypatch):
    """Refresh чистит и сохранённое сообщение вне истории, и все дубли."""
    spec = _spec()
    tracked = _Message(99, spec)
    first = _Message(11, spec)
    duplicate = _Message(12, spec)
    channel = _Channel(spec.channel_id, [first, duplicate])
    channel.history.messages.append(tracked)
    flags = _flags(monkeypatch, {"test_panel_message": "99"})

    async def build():
        return "embed", "view"

    result = asyncio.run(
        PanelRegistry(_client(channel), _send, spec).ensure(build, refresh=True))

    assert result.status == "published"
    assert result.deleted == 3
    assert all(m.deleted for m in (tracked, first, duplicate))
    assert len(channel.sent) == 1
    assert flags["test_panel_message"] == str(channel.sent[0]["message"].id)


def test_unreadable_history_does_not_publish_duplicate(monkeypatch):
    """Историю не читаем и флага нет — вслепую не публикуем."""
    spec = _spec()
    forbidden = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
    channel = _Channel(spec.channel_id, [], history_error=forbidden)
    flags = _flags(monkeypatch)

    result = asyncio.run(
        PanelRegistry(_client(channel), _send, spec).ensure(_builder))

    assert result.status == "no_history"
    assert channel.sent == []
    assert "test_panel_message" not in flags


def test_unreadable_history_keeps_tracked_panel(monkeypatch):
    """История недоступна, но id сохранён: панель считается на месте."""
    spec = _spec()
    tracked = _Message(77, spec)
    forbidden = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
    channel = _Channel(spec.channel_id, [tracked],
                       history_error=forbidden)
    _flags(monkeypatch, {"test_panel_message": "77"})

    result = asyncio.run(
        PanelRegistry(_client(channel), _send, spec).ensure(_builder))

    assert result.status == "exists"
    assert result.message_id == 77
    assert channel.sent == []
    assert not tracked.deleted


def test_refresh_reports_delete_forbidden_but_still_publishes(monkeypatch):
    spec = _spec()
    old = _Message(55, spec, forbidden=True)
    channel = _Channel(spec.channel_id, [old])
    _flags(monkeypatch, {"test_panel_message": "55"})

    result = asyncio.run(
        PanelRegistry(_client(channel), _send, spec).ensure(
            _builder, refresh=True))

    assert result.status == "published"
    assert "Manage Messages" in result.delete_problem
    assert len(channel.sent) == 1
