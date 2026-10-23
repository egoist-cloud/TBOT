# -*- coding: utf-8 -*-
"""/give: панель выдачи доступа и права модераторов по командам."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, get_db, init_db  # noqa: E402


async def _reset_moderators() -> None:
    db = await get_db()
    await db.execute("DELETE FROM moderators")
    await db.commit()


class _FakeResponse:
    def __init__(self):
        self.done = False
        self.messages = []

    async def defer(self, **kwargs):
        self.done = True

    def is_done(self):
        return self.done

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class _FakeFollowup:
    async def send(self, *args, **kwargs):
        pass


class _FakeUser:
    def __init__(self, user_id=9001, admin=False, name="Admin"):
        self.id = user_id
        self.display_name = name
        self.mention = f"<@{user_id}>"
        self.bot = False
        self.roles = []
        self.guild_permissions = SimpleNamespace(administrator=admin)


class _FakeInteraction:
    def __init__(self, user_id=9001, admin=True):
        self.user = _FakeUser(user_id, admin)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.guild = object()
        self.client = object()
        self.command = None
        self.edited = None

    async def edit_original_response(self, **kwargs):
        self.edited = kwargs


def _capture(monkeypatch):
    sent, logs = [], []

    async def fake_safe_send(_target, *args, **kwargs):
        sent.append((args, kwargs))

    async def fake_log_to_channel(_bot, text):
        logs.append(text)

    async def fake_log_error(_bot, context, exc):
        raise AssertionError(f"неожиданная ошибка {context}: {exc}")

    async def fake_close(_i, text=None, **_kwargs):
        raise AssertionError(f"неожиданное закрытие: {text}")

    monkeypatch.setattr(botmod, "safe_send", fake_safe_send)
    monkeypatch.setattr(botmod, "log_to_channel", fake_log_to_channel)
    monkeypatch.setattr(botmod, "log_error", fake_log_error)
    monkeypatch.setattr(botmod, "close_interaction", fake_close)
    return sent, logs


def test_moderator_default_has_all_commands():
    """Новый модератор без явного списка получает все команды."""
    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            assert await botmod.moderation_repo.add_moderator(701) is True
            assert await botmod.moderation_repo.get_moderator_commands(701) == ["*"]
            assert await botmod.moderation_repo.has_command_access(701, "check")
            assert await botmod.moderation_repo.has_command_access(701, "anything")
        finally:
            await close_db()

    asyncio.run(scenario())


def test_moderator_command_list_is_enforced():
    """Явный список команд ограничивает доступ, пустой — запрещает всё."""
    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            await botmod.moderation_repo.set_moderator_commands(
                702, ["check", "status"])
            assert await botmod.moderation_repo.has_command_access(702, "check")
            assert not await botmod.moderation_repo.has_command_access(702, "admins")

            await botmod.moderation_repo.set_moderator_commands(702, [])
            assert await botmod.moderation_repo.get_moderator_commands(702) == []
            assert not await botmod.moderation_repo.has_command_access(702, "check")
            assert await botmod.moderation_repo.is_moderator(702)
        finally:
            await close_db()

    asyncio.run(scenario())


def test_non_moderator_has_no_permissions():
    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            assert await botmod.moderation_repo.get_moderator_commands(703) is None
            assert not await botmod.moderation_repo.has_command_access(703, "check")
        finally:
            await close_db()

    asyncio.run(scenario())


def test_old_moderators_table_gets_all_commands():
    """Миграция старой таблицы moderators не отбирает права."""
    async def scenario():
        await init_db()
        try:
            db = await get_db()
            await db.execute("DROP TABLE moderators")
            await db.execute(
                "CREATE TABLE moderators (discord_id TEXT PRIMARY KEY)")
            await db.execute(
                "INSERT INTO moderators (discord_id) VALUES ('704')")
            await db.commit()

            await init_db()
            assert await botmod.moderation_repo.get_moderator_commands(704) == ["*"]
            assert await botmod.moderation_repo.has_command_access(704, "check")
        finally:
            await close_db()

    asyncio.run(scenario())


def test_give_command_opens_panel(monkeypatch):
    """/give без участника показывает embed и панель выбора."""
    sent, logs = _capture(monkeypatch)

    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            i = _FakeInteraction()
            await botmod.give.callback(i, None)
            assert len(sent) == 1
            _args, kwargs = sent[0]
            embed = kwargs["embed"]
            view = kwargs["view"]
            assert isinstance(view, botmod.GivePanelView)
            assert embed.color.value == 0x000000
            fields = {f.name: f.value for f in embed.fields}
            assert "не выбран" in fields["Пользователь"]
            assert "Команды" in fields
            assert logs and "панель открыл" in logs[0]
        finally:
            await close_db()

    asyncio.run(scenario())


def test_give_panel_loads_and_saves_permissions(monkeypatch):
    """Панель подхватывает права участника и сохраняет новый выбор."""
    _capture(monkeypatch)

    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            await botmod.moderation_repo.set_moderator_commands(
                222, ["check", "status"])
            panel = botmod.GivePanelView(9001)
            target = _FakeUser(222, name="Player")
            i = _FakeInteraction()
            await panel.pick_user(i, target)
            assert panel.is_moderator is True
            assert panel.selected == {"check", "status"}
            assert i.edited is not None
            assert "есть в /moders" in {
                f.name: f.value for f in i.edited["embed"].fields}["Статус"]

            panel.selected = {"check", "admins"}
            await panel._persist()
            assert await botmod.moderation_repo.get_moderator_commands(222) == [
                "admins", "check"]

            panel.selected = set()
            await panel._persist()
            assert panel.is_moderator is False
            assert await botmod.moderation_repo.get_moderator_commands(222) is None
        finally:
            await close_db()

    asyncio.run(scenario())


def test_interaction_check_blocks_missing_command(monkeypatch):
    """Модератор с ограниченным списком не может вызвать чужую команду."""
    _capture(monkeypatch)

    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            await botmod.moderation_repo.set_moderator_commands(801, ["status"])
            i = _FakeInteraction(user_id=801, admin=False)
            i.command = SimpleNamespace(name="check")
            allowed = await botmod.bot.interaction_check(i)
            assert allowed is False
            assert i.response.messages
            assert "Нет доступа к этой команде" in i.response.messages[0][0][0]

            i2 = _FakeInteraction(user_id=801, admin=False)
            i2.command = SimpleNamespace(name="status")
            assert await botmod.bot.interaction_check(i2) is True

            # Администратор сервера не ограничен списком.
            i3 = _FakeInteraction(user_id=801, admin=True)
            i3.command = SimpleNamespace(name="check")
            assert await botmod.bot.interaction_check(i3) is True
        finally:
            await close_db()

    asyncio.run(scenario())


def test_public_command_always_allowed():
    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            i = _FakeInteraction(user_id=802, admin=False)
            i.command = SimpleNamespace(name="ping")
            assert await botmod.bot.interaction_check(i) is True
        finally:
            await close_db()

    asyncio.run(scenario())


def test_admin_only_commands_stay_admin_only(monkeypatch):
    """Даже с «всеми правами» модератор не получает /take и /give."""
    _capture(monkeypatch)

    async def scenario():
        await init_db()
        await _reset_moderators()
        try:
            await botmod.moderation_repo.set_moderator_commands(803, ["*"])
            i = _FakeInteraction(user_id=803, admin=False)
            i.command = SimpleNamespace(name="take")
            assert await botmod.bot.interaction_check(i) is False
            assert "только для администраторов" in i.response.messages[0][0][0]

            i2 = _FakeInteraction(user_id=803, admin=True)
            i2.command = SimpleNamespace(name="take")
            assert await botmod.bot.interaction_check(i2) is True
        finally:
            await _reset_moderators()
            await close_db()

    asyncio.run(scenario())
