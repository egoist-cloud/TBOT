# -*- coding: utf-8 -*-
"""Матрица доступа: каждая команда MODERATOR_COMMANDS.

Проверяется ровно тот гейт, который discord.py вызывает перед выполнением
slash-команды: ``MyBot.interaction_check``, привязанный в ``setup_hook``
к ``bot.tree.interaction_check`` (у ``commands.Bot`` своего метода нет,
поэтому без этой привязки проверка не выполняется вообще).

Ожидаемое поведение:

  * обычный участник (нет записи в moderators, нет administrator)
    не получает ни одной команды из MODERATOR_COMMANDS, кроме публичных;
  * модератор с ``["*"]`` получает все команды;
  * модератор с явным списком получает только свои команды;
  * администратор сервера не ограничен списком (но не получает
    ADMIN_ONLY_COMMANDS, если он не администратор);
  * ``/search`` остаётся публичным — игрок видит свою историю заявок.
"""
import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, get_db, init_db  # noqa: E402

MODERATOR_COMMANDS = list(botmod.MODERATOR_COMMANDS)

# Команды, которые гейт обязан закрыть: всё из MODERATOR_COMMANDS,
# что не объявлено публичным.
GATED_COMMANDS = [c for c in MODERATOR_COMMANDS
                  if c not in botmod.PUBLIC_COMMANDS]

ORDINARY_ID = 910_001
MODERATOR_ID = 910_002
ADMIN_ID = 910_003


# --------------------------------------------------------------------------
# Фейки Discord (без сети: только то, что читает interaction_check)
# --------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self):
        self.done = False
        self.messages = []

    def is_done(self):
        return self.done

    async def defer(self, **_kwargs):
        self.done = True

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        self.done = True


class _FakeFollowup:
    async def send(self, *args, **kwargs):
        pass


class _FakeUser:
    def __init__(self, user_id, admin=False):
        self.id = user_id
        self.display_name = f"user-{user_id}"
        self.mention = f"<@{user_id}>"
        self.bot = False
        self.roles = []
        self.guild_permissions = SimpleNamespace(administrator=admin)


class _FakeMember:
    """Участник, которого guild.get_member() отдаёт для админа."""

    def __init__(self, user_id, admin=False):
        self.id = user_id
        self.guild_permissions = SimpleNamespace(administrator=admin)


class _FakeGuild:
    def __init__(self, member=None):
        self._member = member

    def get_member(self, user_id):
        if self._member is not None and self._member.id == user_id:
            return self._member
        return None


class _FakeInteraction:
    def __init__(self, user_id, command, *, admin=False, member=None):
        self.user = _FakeUser(user_id, admin=admin)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.guild = _FakeGuild(member)
        self.client = object()
        self.command = SimpleNamespace(name=command)


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------
async def _reset_moderators() -> None:
    db = await get_db()
    await db.execute("DELETE FROM moderators")
    await db.commit()


def _make(command, *, user_id=ORDINARY_ID, admin=False, member=True):
    """Interaction для команды ``command``.

    ``member=True`` — сервер «знает» участника (для админа это нужно,
    чтобы guild.get_member() вернул его с правами administrator).
    """
    guild_member = _FakeMember(user_id, admin=True) if (admin and member) else None
    return _FakeInteraction(user_id, command, admin=admin, member=guild_member)


def _deny_text(interaction) -> str:
    assert interaction.response.messages, "гейт не ответил отказом"
    return str(interaction.response.messages[0][0][0])


# --------------------------------------------------------------------------
# Инварианты наборов команд
# --------------------------------------------------------------------------
def test_moderator_commands_are_not_admin_only():
    """ADMIN_ONLY_COMMANDS нельзя делегировать через /give."""
    assert MODERATOR_COMMANDS, "MODERATOR_COMMANDS пуст — проверять нечего"
    overlap = set(MODERATOR_COMMANDS) & set(botmod.ADMIN_ONLY_COMMANDS)
    assert not overlap, f"админские команды попали в MODERATOR_COMMANDS: {overlap}"


def test_only_expected_commands_are_public():
    """Список публичных команд меняется только осознанно.

    Если тест упал — новая команда стала доступна всем участникам
    сервера. Убедитесь, что она не ходит в cs2red и не меняет данные,
    и только потом добавьте её в ожидаемый набор.
    """
    expected = {"link", "unlink", "ping", "health", "lookup", "ask",
                "rules", "search", "help"}
    assert set(botmod.PUBLIC_COMMANDS) == expected


def test_default_command_tree_would_allow_everyone():
    """Без привязки CommandTree пропускает любого — это и был баг.

    Поведенческая проверка механизма: свежее дерево команд отвечает True
    даже обычному участнику, а после привязки нашего гейта — False.
    """

    async def scenario():
        await init_db()
        await _reset_moderators()
        # В тестах setup_hook не запускается, поэтому дерево бота ещё
        # с дефолтным методом CommandTree — как сразу после старта.
        tree = botmod.bot.tree
        original = tree.interaction_check
        try:
            i = _make("check_all")
            assert await tree.interaction_check(i) is True

            tree.interaction_check = botmod.bot.interaction_check
            assert await tree.interaction_check(i) is False
            assert "Нет доступа" in _deny_text(i)
        finally:
            tree.interaction_check = original
            await close_db()

    asyncio.run(scenario())


def test_gate_is_wired_into_command_tree():
    """Гейт должен быть привязан именно к CommandTree.

    У ``commands.Bot`` нет своего interaction_check: discord.py вызывает
    ``self.tree.interaction_check`` (app_commands/tree.py). Без строки в
    setup_hook метод MyBot.interaction_check не вызывается никогда —
    это и был баг, из-за которого команды были открыты всем.
    """
    source = inspect.getsource(botmod.MyBot.setup_hook)
    assert "self.tree.interaction_check = self.interaction_check" in source

    tree = botmod.bot.tree
    original = tree.interaction_check
    try:
        tree.interaction_check = botmod.bot.interaction_check
        # Атрибут экземпляра перекрывает метод класса CommandTree —
        # именно на это рассчитывает setup_hook.
        assert tree.interaction_check.__self__ is botmod.bot
        assert tree.interaction_check.__func__ is botmod.MyBot.interaction_check
    finally:
        tree.interaction_check = original


# --------------------------------------------------------------------------
# Обычный участник: ни одна модераторская команда недоступна
# --------------------------------------------------------------------------
@pytest.mark.parametrize("command", GATED_COMMANDS)
def test_ordinary_member_is_denied(command):
    """Участник без роли и без записи в moderators получает отказ."""

    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            i = _make(command)
            assert await botmod.bot.interaction_check(i) is False
            assert "Нет доступа" in _deny_text(i)
        finally:
            await close_db()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Модератор: доступ есть
# --------------------------------------------------------------------------
@pytest.mark.parametrize("command", GATED_COMMANDS)
def test_moderator_with_all_rights_is_allowed(command):
    """Модератор с ["*"] может вызвать любую команду из списка."""

    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            await botmod.moderation_repo.set_moderator_commands(
                MODERATOR_ID, ["*"])
            i = _make(command, user_id=MODERATOR_ID)
            assert await botmod.bot.interaction_check(i) is True
        finally:
            await close_db()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", GATED_COMMANDS)
def test_moderator_with_exact_permission_is_allowed(command):
    """Право выдано ровно на эту команду — доступ есть."""

    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            await botmod.moderation_repo.set_moderator_commands(
                MODERATOR_ID, [command])
            i = _make(command, user_id=MODERATOR_ID)
            assert await botmod.bot.interaction_check(i) is True
        finally:
            await close_db()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", GATED_COMMANDS)
def test_moderator_without_exact_permission_is_denied(command):
    """Право на другую команду не открывает эту."""
    other = next(c for c in GATED_COMMANDS if c != command)

    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            await botmod.moderation_repo.set_moderator_commands(
                MODERATOR_ID, [other])
            i = _make(command, user_id=MODERATOR_ID)
            assert await botmod.bot.interaction_check(i) is False
            assert "Нет доступа к этой команде" in _deny_text(i)
        finally:
            await close_db()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Администратор сервера
# --------------------------------------------------------------------------
@pytest.mark.parametrize("command", GATED_COMMANDS)
def test_guild_administrator_is_allowed(command):
    """Администратор не состоит в moderators, но получает доступ."""

    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            i = _make(command, user_id=ADMIN_ID, admin=True)
            assert await botmod.bot.interaction_check(i) is True
        finally:
            await close_db()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", sorted(botmod.ADMIN_ONLY_COMMANDS))
def test_admin_only_commands_are_closed_to_moderators(command):
    """Даже ["*"] не даёт модератору админскую команду, а админ — даёт."""

    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            await botmod.moderation_repo.set_moderator_commands(
                MODERATOR_ID, ["*"])
            denied = _make(command, user_id=MODERATOR_ID)
            assert await botmod.bot.interaction_check(denied) is False
            assert "только для администраторов" in _deny_text(denied)

            allowed = _make(command, user_id=ADMIN_ID, admin=True)
            assert await botmod.bot.interaction_check(allowed) is True
        finally:
            await close_db()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Публичные команды и известное исключение
# --------------------------------------------------------------------------
def test_search_is_public_for_ordinary_member():
    """/search намеренно публичен: игрок открывает свою историю заявок.

    Чужую историю он всё равно не получит: внутри команды есть проверка
    ``target.id != i.user.id and not is_reviewer``.
    """

    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            i = _make("search")
            assert await botmod.bot.interaction_check(i) is True
        finally:
            await close_db()

    asyncio.run(scenario())


@pytest.mark.parametrize("command", ["ping", "health", "link", "unlink",
                                     "lookup", "ask", "rules"])
def test_public_commands_are_open_to_everyone(command):
    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            i = _make(command)
            assert await botmod.bot.interaction_check(i) is True
        finally:
            await close_db()

    asyncio.run(scenario())
