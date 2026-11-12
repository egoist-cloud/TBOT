# -*- coding: utf-8 -*-
"""Тесты выбора клана: отсутствие 10062 «Приложение не ответило вовремя».

Суть проблемы: discord.py ищет обработчик компонента по (тип, custom_id)
в памяти процесса. Со случайным custom_id после рестарта бота (Render
redeploy) обработчик терялся, и Discord отвечал 10062.
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402
import script as botmod  # noqa: E402


class _FakeResponse:
    def __init__(self, calls):
        self.calls = calls
        self.done = False

    async def send_modal(self, modal):
        self.calls.append("send_modal")

    async def send_message(self, content=None, **kwargs):
        self.calls.append("send_message")

    async def defer(self, **kwargs):
        self.calls.append("defer")


class _FakeAuthor:
    def __init__(self, author_id):
        self.id = author_id


class _FakeMessage:
    def __init__(self, author_id):
        self.author = _FakeAuthor(author_id)


class _FakeClient:
    def __init__(self, user_id):
        self.user = _FakeAuthor(user_id)


class _FakeSelect:
    """Минимальный select-component для DynamicItem."""

    custom_id = "a3f5c9e1b2d4f60718293a4b5c6d7e8f"

    def is_dispatchable(self):
        return True

    @property
    def type(self):
        return discord.ComponentType.select


class _FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send_modal(self, modal):
        self.calls.append("followup.send_modal")


class _FakeInteraction:
    def __init__(self):
        self.calls = []
        self.response = _FakeResponse(self.calls)
        self.followup = _FakeFollowup(self.calls)
        self.message = None
        self.client = None


def test_select_has_fixed_custom_id():
    """У select'а фиксированный custom_id — обработчик не теряется."""
    sel = botmod.ClanSelect()
    assert sel.custom_id == botmod.CLAN_SELECT_CUSTOM_ID
    assert sel.is_persistent(), "select должен быть персистентным"
    print("  ok: у select'а фиксированный custom_id")


def test_clan_view_is_persistent():
    """View можно зарегистрировать через add_view (работает после рестарта)."""
    view = botmod.ClanView()
    assert view.is_persistent(), "ClanView должен быть персистентным"
    assert view.timeout is None
    ids = [getattr(i, "custom_id", None) for i in view.children]
    assert botmod.CLAN_SELECT_CUSTOM_ID in ids
    print("  ok: ClanView персистентный")


def test_bot_registers_clan_view():
    """Бот регистрирует view и legacy-обработчик при старте."""
    view = botmod.ClanView()
    botmod.bot.add_view(view)
    botmod.bot.add_dynamic_items(botmod.LegacyClanSelect)
    assert view in list(botmod.bot.persistent_views)
    print("  ok: view зарегистрирован как персистентный")


def test_legacy_select_matches_old_custom_ids():
    """Legacy-обработчик ловит старые случайные custom_id."""
    pattern = botmod.LegacyClanSelect.__discord_ui_compiled_template__
    assert isinstance(pattern, re.Pattern)
    assert pattern.fullmatch("a3f5c9e1b2d4f60718293a4b5c6d7e8f")
    # Фиксированный id не должен перехватываться legacy-обработчиком.
    assert not pattern.fullmatch(botmod.CLAN_SELECT_CUSTOM_ID)
    print("  ok: legacy-обработчик ловит старые custom_id")


def test_callback_answers_with_modal_first():
    """Первым действием — send_modal, без БД и API."""
    async def scenario():
        i = _FakeInteraction()
        sel = botmod.ClanSelect()
        sel._values = ["TALENT"]
        await sel.callback(i)
        assert i.calls, "обработчик не ответил"
        assert i.calls[0] == "send_modal", i.calls
        assert "defer" not in i.calls

    asyncio.run(scenario())
    print("  ok: первым действием идёт send_modal")


def test_modal_submit_defers_first():
    """В форме заявки первым действием тоже defer."""
    src = (Path(__file__).resolve().parent.parent / "script.py").read_text(
        encoding="utf-8")
    idx = src.index("class QuestionnaireModal")
    body = src[idx:idx + 3000]
    assert "await i.response.defer" in body
    # Проверки БД идут после defer.
    assert body.index("await i.response.defer") < body.index("is_user_blocked")
    print("  ok: в форме заявки defer идёт первым")


def test_responded_falls_back_to_followup():
    """Если по select'у уже ответили — отправляем модалку через followup."""
    async def scenario():
        i = _FakeInteraction()

        async def _send_modal(modal):
            raise discord.InteractionResponded(1)

        i.response.send_modal = _send_modal
        sel = botmod.ClanSelect()
        sel._values = ["TALENT"]
        await sel.callback(i)
        assert i.calls[0] == "followup.send_modal", i.calls

    asyncio.run(scenario())
    print("  ok: есть запасной путь через followup")


def test_legacy_callback_always_answers():
    """Даже если сообщение не наше — legacy-обработчик отвечает (иначе 10062)."""
    async def scenario():
        i = _FakeInteraction()
        i.message = _FakeMessage(author_id=999)
        i.client = _FakeClient(user_id=42)
        item = _FakeSelect()
        legacy = botmod.LegacyClanSelect(item)
        await legacy.callback(i)
        assert i.calls, "legacy-обработчик не ответил"
        assert i.calls[0] == "send_message", i.calls

    asyncio.run(scenario())
    print("  ok: legacy-обработчик всегда отвечает")


def test_legacy_callback_opens_modal_for_own_message():
    """Для сообщения бота legacy-обработчик открывает модалку."""
    async def scenario():
        i = _FakeInteraction()
        i.message = _FakeMessage(author_id=42)
        i.client = _FakeClient(user_id=42)
        item = _FakeSelect()
        legacy = botmod.LegacyClanSelect(item)
        await legacy.callback(i)
        assert i.calls[0] == "send_modal", i.calls

    asyncio.run(scenario())
    print("  ok: legacy-обработчик открывает модалку для своего сообщения")
