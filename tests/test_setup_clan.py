# -*- coding: utf-8 -*-
"""Тесты !setup_clan: публикация официальных условий вступления в клан.

Команда текстовая (не слэш). Сообщение должно остаться в канале, а под
ним — выпадающий список кланов, который открывает модальное окно заявки.
"""
import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, init_db  # noqa: E402


class _FakeMessage:
    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _FakeChannel:
    def __init__(self):
        self.sent = []


class _FakeAuthor:
    def __init__(self, administrator=True):
        self.id = 123
        self.guild_permissions = SimpleNamespace(administrator=administrator)


class _FakeCtx:
    def __init__(self, administrator=True, in_guild=True):
        self.author = _FakeAuthor(administrator)
        self.guild = SimpleNamespace(id=1, name="Клан") if in_guild else None
        self.bot = None
        self.channel = _FakeChannel()
        self.message = _FakeMessage()


def _patch_send(monkeypatch, ctx):
    """Пишем отправленные сообщения в ctx.channel.sent вместо Discord."""
    async def fake_safe_send(target, *args, **kwargs):
        ctx.channel.sent.append(kwargs)

    monkeypatch.setattr(botmod, "safe_send", fake_safe_send)


def test_setup_clan_is_text_command():
    """Команда — текстовая !setup_clan, а не слэш."""
    names = {c.name for c in botmod.bot.commands}
    assert "setup_clan" in names, "!setup_clan не зарегистрирована"
    assert botmod.bot.tree.get_command("setup_clan") is None, \
        "слэш-команда /setup_clan должна быть удалена"
    print("  ok: !setup_clan зарегистрирована как текстовая команда")


def test_setup_clan_alias_setup():
    """Старое !setup остаётся алиасом."""
    cmds = [c for c in botmod.bot.commands if c.name == "setup_clan"]
    assert cmds and "setup" in cmds[0].aliases, cmds[0].aliases if cmds else None
    print("  ok: !setup — алиас !setup_clan")


def test_setup_clan_is_admin_only():
    """Команда доступна только администраторам (проверка на уровне декоратора)."""
    cmds = [c for c in botmod.bot.commands if c.name == "setup_clan"]
    assert cmds, "!setup_clan не зарегистрирована"
    # checks содержит has_permissions(administrator=True)
    assert any("has_permissions" in repr(c) or "administrator" in repr(c)
               for c in cmds[0].checks), cmds[0].checks
    print("  ok: команда доступна только администраторам")


def test_conditions_embed_content(monkeypatch):
    """Embed содержит условия, пинг пользователя и дату без бренда."""
    monkeypatch.setattr(botmod, "CLAN_QUESTIONS_USER_ID", 1113899288192356402)
    monkeypatch.setattr(botmod, "CLAN_QUESTIONS_ROLE_ID", 0)
    embed = botmod._build_clan_conditions_embed()
    assert embed.color.value == 0, "embed должен быть чёрным"
    assert embed.title == "Официальные условия вступления в клан.", embed.title

    fields = {f.name: f.value for f in embed.fields}
    general = fields["Общие требования"]
    assert "VAC-банов" in general
    assert "cs2red.ru" in general
    assert "членства в других кланах" in general
    assert "от **12 лет**" in general

    clan = fields[f"Требования для {botmod.CLAN_NAME}"]
    assert "1000 MMR" in clan
    assert "3 часов" in clan

    form = fields["Что указать в заявке"]
    assert "Игровой никнейм" in form
    assert "SteamID64" in form

    important = fields["Важно"]
    assert f"**{botmod.APPLICATION_TTL_DAYS} дн.**" in important
    assert "**2 дня** на вступление" in important
    if botmod.APPLICATION_REJECT_COOLDOWN_DAYS > 0:
        assert "повторная заявка" in important

    questions = fields["По вопросам вступления"]
    assert f"<@{botmod.CLAN_QUESTIONS_USER_ID}>" in questions, questions
    assert "администрации" not in questions
    assert "Перед подачей заявки" in embed.description
    assert "Выберите клан" in embed.description, embed.description
    assert "Quantum" not in embed.footer.text, embed.footer.text
    assert re.fullmatch(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}",
                        embed.footer.text), embed.footer.text
    print("  ok: embed содержит все условия вступления")


def test_clan_view_has_select():
    """Под embed идёт выпадающий список с названием «Выберите клан»."""
    view = botmod.ClanView()
    selects = [c for c in view.children if isinstance(c, botmod.discord.ui.Select)]
    assert len(selects) == 1, selects
    sel = selects[0]
    assert sel.placeholder == "Выберите клан", sel.placeholder
    assert sel.custom_id == botmod.CLAN_SELECT_CUSTOM_ID, sel.custom_id
    labels = [o.label for o in sel.options]
    assert botmod.CLAN_NAME in labels, labels
    print("  ok: под embed — список выбора клана")


def test_setup_clan_posts_message_with_view(monkeypatch):
    """Команда отправляет в канал embed + выпадающий список."""
    async def scenario():
        await init_db()  # команда сохраняет id панели в meta
        try:
            ctx = _FakeCtx()
            _patch_send(monkeypatch, ctx)
            await botmod.setup_clan.callback(ctx)
        finally:
            await close_db()
        assert ctx.message.deleted, "команда должна удалять своё сообщение"
        assert ctx.channel.sent, "сообщение не отправлено"
        kwargs = ctx.channel.sent[0]
        assert kwargs.get("embed") is not None, kwargs
        view = kwargs.get("view")
        assert view is not None, "к сообщению не приложен view"
        assert any(isinstance(c, botmod.discord.ui.Select) for c in view.children)
        print("  ok: сообщение с embed'ом и списком отправлено в канал")

    asyncio.run(scenario())


def test_setup_clan_denied_for_non_admin(monkeypatch):
    """Без прав администратора условия не публикуются."""
    async def scenario():
        ctx = _FakeCtx(administrator=False)
        _patch_send(monkeypatch, ctx)
        await botmod.setup_clan.callback(ctx)
        for kwargs in ctx.channel.sent:
            assert kwargs.get("view") is None, kwargs
            assert kwargs.get("embed") is None, kwargs
        print("  ok: без прав администратора условия не публикуются")

    asyncio.run(scenario())


def test_setup_clan_only_in_guild(monkeypatch):
    """В личных сообщениях команда не работает."""
    async def scenario():
        ctx = _FakeCtx(in_guild=False)
        _patch_send(monkeypatch, ctx)
        await botmod.setup_clan.callback(ctx)
        for kwargs in ctx.channel.sent:
            assert kwargs.get("view") is None, kwargs
        print("  ok: вне сервера команда ничего не публикует")

    asyncio.run(scenario())


def test_clan_panel_accent_is_black():
    """Полоска у V2-панели чёрная, а не синяя."""
    layout = botmod._v2_layout_from_embed(
        botmod._build_clan_conditions_embed(), botmod.ClanView())
    container = layout.children[0]

    assert int(container.accent_colour) == 0x000000, container.accent_colour



def _questions_text(embed) -> str:
    return {f.name: f.value for f in embed.fields}["По вопросам вступления"]


def test_conditions_embed_mentions_existing_questions_role(monkeypatch):
    """Роль вопросов есть на сервере — в панели её упоминание."""
    monkeypatch.setattr(botmod, "CLAN_QUESTIONS_USER_ID", 0)
    monkeypatch.setattr(botmod, "CLAN_QUESTIONS_ROLE_ID", 1396606001188175922)
    role = SimpleNamespace(id=1396606001188175922, name="Куратор")
    guild = SimpleNamespace(
        get_role=lambda rid: role if rid == role.id else None)
    client = SimpleNamespace(get_guild=lambda _gid: guild)

    embed = botmod._build_clan_conditions_embed(client)

    assert f"<@&{role.id}>" in _questions_text(embed)


def test_conditions_embed_hides_missing_questions_role(monkeypatch):
    """Роли нет на сервере — вместо «Неизвестная роль» обычный текст."""
    monkeypatch.setattr(botmod, "CLAN_QUESTIONS_USER_ID", 0)
    monkeypatch.setattr(botmod, "CLAN_QUESTIONS_ROLE_ID", 1396606001188175922)
    guild = SimpleNamespace(get_role=lambda _rid: None)
    client = SimpleNamespace(get_guild=lambda _gid: guild)

    embed = botmod._build_clan_conditions_embed(client)

    questions = _questions_text(embed)
    assert "<@&" not in questions, questions
    assert "администрации" in questions


def test_clan_panel_refresh_default_is_38_hours():
    """Панель заявки пересоздаётся примерно раз в 38 часов."""
    assert botmod.CLAN_PANEL_REFRESH_HOURS == 38


def test_clan_panel_allowed_mentions_only_user(monkeypatch):
    """Пинг разрешён только пользователю вопросов, не @everyone/@here."""
    monkeypatch.setattr(botmod, "CLAN_QUESTIONS_USER_ID", 1113899288192356402)
    monkeypatch.setattr(botmod, "CLAN_QUESTIONS_ROLE_ID", 0)
    mentions = botmod._clan_panel_mentions()

    assert mentions is not None
    assert [u.id for u in mentions.users] == [botmod.CLAN_QUESTIONS_USER_ID]
    assert mentions.roles is False
    assert mentions.everyone is False


def test_clan_panel_allowed_mentions_role_fallback(monkeypatch):
    """Старая настройка роли продолжает работать, если пользователь не задан."""
    monkeypatch.setattr(botmod, "CLAN_QUESTIONS_USER_ID", 0)
    monkeypatch.setattr(botmod, "CLAN_QUESTIONS_ROLE_ID", 123456789)
    mentions = botmod._clan_panel_mentions()

    assert mentions is not None
    assert [r.id for r in mentions.roles] == [123456789]
    assert mentions.users is False
