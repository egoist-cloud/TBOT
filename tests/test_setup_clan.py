# -*- coding: utf-8 -*-
"""Тесты /setup_clan: публикация официальных условий вступления в клан.

Сообщение должно остаться в канале, а под ним — выпадающий список
кланов, который открывает модальное окно заявки.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=1, **kwargs)


class _FakeResponse:
    def __init__(self):
        self.deferred = []
        self.done = False

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)
        self.done = True


class _FakeUser:
    def __init__(self, administrator=True):
        self.id = 123
        self.guild_permissions = SimpleNamespace(administrator=administrator)


class _FakeInteraction:
    def __init__(self, administrator=True, in_guild=True):
        self.user = _FakeUser(administrator)
        self.guild = SimpleNamespace(id=1, name="Клан") if in_guild else None
        self.client = None
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


def test_setup_clan_registered():
    """Слэш-команда /setup_clan зарегистрирована."""
    cmd = botmod.bot.tree.get_command("setup_clan")
    assert cmd is not None, "/setup_clan не зарегистрирован"
    print("  ok: команда /setup_clan зарегистрирована")


def test_setup_clan_is_admin_only():
    """Команда доступна только администраторам."""
    cmd = botmod.bot.tree.get_command("setup_clan")
    perms = getattr(cmd, "default_permissions", None)
    assert perms is not None, "не заданы права по умолчанию"
    assert perms.administrator is True, perms
    print("  ok: команда доступна только администраторам")


def test_conditions_embed_content():
    """Embed содержит все официальные условия вступления."""
    embed = botmod._build_clan_conditions_embed()
    assert embed.color.value == 0, "embed должен быть чёрным"
    assert embed.title == "Официальные условия вступления в клан.", embed.title

    fields = {f.name: f.value for f in embed.fields}
    general = fields["📋 Общие требования"]
    assert "Отсутствие VAC-банов в Steam и блокировок на cs2red.ru." in general
    assert "Отсутствие членства в других кланах." in general
    assert "Возраст — от 12 лет." in general

    clan = fields[f"🎯 Требования для {botmod.CLAN_NAME}"]
    assert "Рейтинг — от 1.000 ммр." in clan
    assert "Онлайн — не менее 3 часов в день." in clan

    form = fields["📝 Что указать в заявке"]
    assert "Steam-профиль (ссылка или SteamID64)" in form

    important = fields["⚠️ Важно!"]
    assert "в течение 7 дней" in important
    assert "2 дня на вступление" in important

    assert fields["💬 По вопросам вступления"] == "Обратитесь к администрации."
    assert "Перед подачей заявки" in embed.description
    assert embed.footer.text.startswith("Quantum •"), embed.footer.text
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


def test_setup_clan_posts_message_with_view():
    """Команда отправляет в канал embed + выпадающий список."""
    async def scenario():
        i = _FakeInteraction()
        await botmod.setup_clan.callback(i)
        assert i.response.deferred, "команда должна сначала defer"
        assert i.followup.sent, "сообщение не отправлено"
        kwargs = i.followup.sent[0]
        assert kwargs.get("embed") is not None, kwargs
        view = kwargs.get("view")
        assert view is not None, "к сообщению не приложен view"
        assert any(isinstance(c, botmod.discord.ui.Select) for c in view.children)
        print("  ok: сообщение с embed'ом и списком отправлено в канал")

    asyncio.run(scenario())


def test_setup_clan_denied_for_non_admin():
    """Без прав администратора условия не публикуются."""
    async def scenario():
        i = _FakeInteraction(administrator=False)
        await botmod.setup_clan.callback(i)
        # Ответ есть (отказ), но без embed'а с условиями и без списка.
        for kwargs in i.followup.sent:
            assert kwargs.get("view") is None, kwargs
            assert kwargs.get("ephemeral") is True, kwargs
        print("  ok: без прав администратора условия не публикуются")

    asyncio.run(scenario())


def test_setup_clan_only_in_guild():
    """В личных сообщениях команда не работает."""
    async def scenario():
        i = _FakeInteraction(in_guild=False)
        await botmod.setup_clan.callback(i)
        for kwargs in i.followup.sent:
            assert kwargs.get("view") is None, kwargs
        print("  ok: вне сервера команда ничего не публикует")

    asyncio.run(scenario())
