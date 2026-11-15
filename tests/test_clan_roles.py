# -*- coding: utf-8 -*-
"""Тесты ролей кланов: показ отдельно (hoist) и удаление пустых ролей.

Правила:
  * роли кланов создаются с hoist=True и существующие переключаются сами;
  * роль клана без носителей удаляется;
  * трогаются только роли из clan_roles.json — «BAN» и прочие служебные
    роли не удаляются даже пустыми;
  * роли выше роли бота не удаляются и не редактируются (нет прав).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402


class _FakeRole:
    def __init__(self, name, members=(), position=5, hoist=False, rid=1,
                 colour=0, admin=False, managed=False):
        self.id = rid
        self.name = name
        self.members = list(members)
        self.position = position
        self.hoist = hoist
        self.colour = type("C", (), {"value": colour})()
        self.permissions = type("P", (), {"administrator": admin})()
        self.managed = managed
        self.deleted = False
        self.delete_reason = ""
        self.edits = []

    async def delete(self, reason=None):
        self.deleted = True
        self.delete_reason = reason or ""

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "hoist" in kwargs:
            self.hoist = bool(kwargs["hoist"])


class _FakeMe:
    def __init__(self, top_position=10):
        self.top_role = _FakeRole("bot", position=top_position)
        self.guild_permissions = type("P", (), {"manage_roles": True})()


class _FakeGuild:
    def __init__(self, roles, top_position=10):
        self.roles = list(roles)
        self.me = _FakeMe(top_position)
        self.created = []

    async def create_role(self, **kwargs):
        role = _FakeRole(kwargs["name"], position=1)
        role.created_kwargs = kwargs
        self.roles.append(role)
        self.created.append(kwargs)
        return role


class _FakeMember:
    def __init__(self, top_position=3, roles=()):
        self.id = 42
        self.display_name = "Игрок"
        self.top_role = _FakeRole("member", position=top_position)
        self.roles = list(roles)
        self.guild = None

    async def add_roles(self, role, reason=None):
        if role not in self.roles:
            self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        if role in self.roles:
            self.roles.remove(role)


# ============================================================
# Удаление пустых ролей кланов
# ============================================================
def test_cleanup_removes_only_empty_clan_roles():
    """Пустая роль клана удаляется, занятая и служебная — нет."""
    empty = _FakeRole("i hate lj", position=5)
    used = _FakeRole("Undertakers", members=[_FakeMember()], position=5)
    ban = _FakeRole("BAN", position=5)          # пустая, но не роль клана
    guild = _FakeGuild([empty, used, ban])

    deleted, errors = asyncio.run(botmod.cleanup_empty_clan_roles(
        guild, names={"i hate lj", "Undertakers"}))

    assert deleted == ["i hate lj"], deleted
    assert errors == []
    assert empty.deleted is True
    assert used.deleted is False, "у клана есть носители — роль остаётся"
    assert ban.deleted is False, "BAN не в списке ролей кланов"
    print("  ok: удаляются только пустые роли кланов")


def test_cleanup_skips_role_above_bot():
    """Роль выше роли бота удалить нельзя — попадает в ошибки."""
    high = _FakeRole("i hate lj", position=20)
    low = _FakeRole("Псиxушка", position=5)
    guild = _FakeGuild([high, low], top_position=10)

    deleted, errors = asyncio.run(botmod.cleanup_empty_clan_roles(
        guild, names={"i hate lj", "Псиxушка"}))

    assert deleted == ["Псиxушка"]
    assert high.deleted is False
    assert any("i hate lj" in e and "ниже" in e for e in errors), errors
    print("  ok: роль выше бота не удаляется, причина в ошибках")


def test_cleanup_can_be_disabled(monkeypatch):
    """CLAN_ROLE_CLEANUP=0 полностью выключает удаление."""
    role = _FakeRole("i hate lj")
    guild = _FakeGuild([role])
    monkeypatch.setattr(botmod, "CLAN_ROLE_CLEANUP_ENABLED", False)

    deleted, errors = asyncio.run(botmod.cleanup_empty_clan_roles(
        guild, names={"i hate lj"}))

    assert deleted == [] and errors == []
    assert role.deleted is False
    print("  ok: автоудаление можно выключить")


def test_cleanup_without_known_names():
    """Пока бот не создал ни одной роли клана, удалять нечего."""
    role = _FakeRole("BAN")
    guild = _FakeGuild([role])
    deleted, errors = asyncio.run(botmod.cleanup_empty_clan_roles(
        guild, names=set()))
    assert deleted == [] and errors == []
    assert role.deleted is False
    print("  ok: пустой список ролей кланов — не трогаем ничего")


# ============================================================
# Показ участников отдельно (hoist)
# ============================================================
def test_ensure_hoist_enables_only_clan_roles():
    """Старые роли кланов переключаются на «показывать отдельно»."""
    clan_a = _FakeRole("i hate lj", hoist=False)
    clan_b = _FakeRole("Undertakers", hoist=True)     # уже включено
    ban = _FakeRole("BAN", hoist=False)
    guild = _FakeGuild([clan_a, clan_b, ban])

    changed = asyncio.run(botmod.ensure_clan_roles_hoisted(
        guild, names={"i hate lj", "Undertakers"}))

    assert changed == 1
    assert clan_a.hoist is True
    assert clan_b.edits == [], "уже включённую роль не редактируем"
    assert ban.hoist is False, "BAN не трогаем"
    print("  ok: hoist включается только у ролей кланов")


def test_ensure_hoist_skips_role_above_bot():
    """Роль выше бота не редактируется (Forbidden), но остальные — да."""
    high = _FakeRole("i hate lj", position=20, hoist=False)
    low = _FakeRole("OneWay", position=5, hoist=False)
    guild = _FakeGuild([high, low], top_position=10)

    changed = asyncio.run(botmod.ensure_clan_roles_hoisted(
        guild, names={"i hate lj", "OneWay"}))

    assert changed == 1
    assert high.hoist is False and high.edits == []
    assert low.hoist is True
    print("  ok: hoist не применяется выше роли бота")


# ============================================================
# Создание и «усыновление» роли клана
# ============================================================
def test_new_clan_role_is_created_hoisted():
    """Новая роль клана создаётся с hoist=True."""
    guild = _FakeGuild([])
    member = _FakeMember()

    role, status = asyncio.run(botmod._find_or_create_clan_role(
        guild, member, "i hate lj"))

    assert role is not None
    assert "создана" in status
    assert guild.created[0]["hoist"] is True
    assert guild.created[0]["name"] == "i hate lj"
    # Позиция выставляется отдельно, ниже роли бота.
    assert role.edits and role.edits[0]["position"] == 4
    print("  ok: новая роль клана создаётся с hoist")


def test_existing_clan_role_gets_hoisted():
    """Существующая роль клана (созданная раньше) включается автоматически."""
    existing = _FakeRole("Undertakers", hoist=False, position=5)
    guild = _FakeGuild([existing])

    role, status = asyncio.run(botmod._find_or_create_clan_role(
        guild, _FakeMember(), "Undertakers"))

    assert role is existing
    assert status is None
    assert guild.created == [], "роль не пересоздаётся"
    assert existing.hoist is True
    print("  ok: старая роль клана получает hoist сама")


# ============================================================
# Удаление роли клана сразу, когда её никто не носит
# ============================================================
def test_role_deleted_immediately_when_empty():
    """Игрок вышел из клана — роль 123 удаляется, не висит до уборки."""
    role = _FakeRole("123", members=[], position=5)
    guild = _FakeGuild([role])

    deleted = asyncio.run(botmod._delete_clan_role_if_empty(
        guild, role, names={"123"}))

    assert deleted == "123"
    assert role.deleted is True
    print("  ok: пустая роль клана удаляется сразу")


def test_role_kept_when_someone_else_holds_it():
    """В клане остался другой игрок — роль не трогаем."""
    role = _FakeRole("123", members=[_FakeMember()], position=5)
    guild = _FakeGuild([role])

    assert asyncio.run(botmod._delete_clan_role_if_empty(
        guild, role, names={"123"})) is None
    assert role.deleted is False
    print("  ok: роль живёт, пока её кто-то носит")


def test_role_not_deleted_if_not_clan_role():
    """Пустая роль BAN удаляется только вручную, не ботом."""
    role = _FakeRole("BAN", members=[], position=5)
    guild = _FakeGuild([role])

    assert asyncio.run(botmod._delete_clan_role_if_empty(
        guild, role, names=set())) is None
    assert role.deleted is False
    print("  ok: чужие роли не удаляются")


def test_role_above_bot_not_deleted():
    """Роль выше роли бота удалить нельзя — просто пропускаем."""
    role = _FakeRole("123", members=[], position=20)
    guild = _FakeGuild([role], top_position=10)

    assert asyncio.run(botmod._delete_clan_role_if_empty(
        guild, role, names={"123"})) is None
    assert role.deleted is False
    print("  ok: роль выше бота не удаляется")


def test_member_leave_deletes_clan_role(monkeypatch):
    """Игрок вышел с Discord-сервера — роль клана удаляется."""
    role = _FakeRole("123", members=[], position=5)
    guild = _FakeGuild([role])
    member = _FakeMember(roles=[role])
    member.guild = guild
    # Discord может ещё держать ушедшего в кэше роли — он не считается.
    role.members = [member]
    forgotten = []
    monkeypatch.setattr(botmod, "_forget_member_clan_role",
                        lambda mid: forgotten.append(mid))
    monkeypatch.setattr(botmod, "_load_clan_role_names", lambda: {"123"})

    asyncio.run(botmod.on_member_remove(member))

    assert role.deleted is True
    assert forgotten == [member.id]
    print("  ok: уход с сервера удаляет осиротевшую роль клана")


def test_member_leave_keeps_other_roles(monkeypatch):
    """Служебные роли (не клана) при уходе участника не удаляются."""
    role = _FakeRole("BAN", members=[], position=5)
    guild = _FakeGuild([role])
    member = _FakeMember(roles=[role])
    member.guild = guild
    monkeypatch.setattr(botmod, "_forget_member_clan_role", lambda mid: None)
    monkeypatch.setattr(botmod, "_load_clan_role_names", lambda: {"123"})

    asyncio.run(botmod.on_member_remove(member))

    assert role.deleted is False
    print("  ok: не-клановые роли не трогаются")


def test_leaving_clan_via_sync_deletes_role(monkeypatch):
    """Синхронизация увидела выход из клана — роль снята и удалена."""
    role = _FakeRole("123", members=[], position=5)
    guild = _FakeGuild([role])
    member = _FakeMember(roles=[role])

    async def fake_clans():
        return [{"clan_name": "123", "clan_id": 1,
                 "members": [{"nick": "other"}]}]

    async def fake_player_clan(steam_id, user=None, clans=None):
        return None, None

    monkeypatch.setattr(botmod.api, "cs2red_clans", fake_clans)
    monkeypatch.setattr(botmod.api, "player_clan_checked", fake_player_clan)
    monkeypatch.setattr(botmod, "_load_clan_role_names", lambda: {"123"})
    monkeypatch.setattr(botmod, "_load_clan_members", lambda: {})
    monkeypatch.setattr(botmod, "_forget_member_clan_role", lambda mid: None)

    res = asyncio.run(botmod.sync_clan_role_by_id(
        guild, member, "76561198000000001", user={"id": 7}))

    assert "клан снят" in res, res
    assert role not in member.roles
    assert role.deleted is True
    print("  ok: выход из клана удаляет пустую роль сразу")


# ============================================================
# Автоочистка раз в 10 часов и принудительная /clan_roleclear
# ============================================================
def test_auto_cleanup_interval_is_ten_hours():
    """Автоочистка ролей кланов — раз в 10 часов, первый прогон через интервал."""
    assert botmod.clan_role_cleanup_watch.hours == 10
    assert botmod._clan_cleanup_first_delay() == 10 * 3600
    print("  ok: автоочистка раз в 10 часов")


class _CmdResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self, ephemeral=False):
        self.deferred = True


class _CmdFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _CmdInteraction:
    def __init__(self):
        self.response = _CmdResponse()
        self.followup = _CmdFollowup()
        self.guild = _FakeGuild([])
        self.user = object()
        self.client = object()


def _sent_text(i) -> str:
    """Текст последнего ответа: safe_send заворачивает его в Embed."""
    args, kwargs = i.followup.sent[-1]
    parts = [str(a) for a in args]
    embed = kwargs.get("embed")
    if embed is not None:
        data = embed.to_dict()
        parts.append(str(data.get("title", "")))
        parts.append(str(data.get("description", "")))
        for field in data.get("fields", []):
            parts.append(str(field.get("name", "")))
            parts.append(str(field.get("value", "")))
    return " ".join(parts)


def _prepare_command(monkeypatch, deleted, errors=(), allowed=True):
    async def guard(_i):
        return allowed

    async def fake_cleanup(_guild):
        return list(deleted), list(errors)

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(botmod, "_admin_guard", guard)
    monkeypatch.setattr(botmod, "cleanup_empty_clan_roles", fake_cleanup)
    monkeypatch.setattr(botmod, "_load_clan_role_names", lambda: {"123"})
    monkeypatch.setattr(botmod, "log_to_channel", noop)
    monkeypatch.setattr(botmod, "audit_action", noop)


def test_clan_roleclear_reports_deleted(monkeypatch):
    """/clan_roleclear удаляет пустые роли кланов и отчитывается списком."""
    _prepare_command(monkeypatch, ["123", "плагбой лох"])
    i = _CmdInteraction()

    asyncio.run(botmod.clan_roleclear.callback(i))

    text = _sent_text(i)
    assert i.response.deferred is True
    assert "Удалено ролей клана: 2" in text
    assert "123" in text and "плагбой лох" in text, text
    print("  ok: /clan_roleclear показывает удалённые роли")


def test_clan_roleclear_nothing_to_clean(monkeypatch):
    """Пустых ролей нет — понятный ответ, без ошибки."""
    _prepare_command(monkeypatch, [])
    i = _CmdInteraction()

    asyncio.run(botmod.clan_roleclear.callback(i))

    assert "чистить нечего" in _sent_text(i)
    print("  ok: /clan_roleclear без пустых ролей отвечает понятно")


def test_clan_roleclear_shows_errors(monkeypatch):
    """Роль выше бота — команда показывает причину, а не молчит."""
    _prepare_command(monkeypatch, [], errors=["«123»: роль бота ниже"])
    i = _CmdInteraction()

    asyncio.run(botmod.clan_roleclear.callback(i))

    text = _sent_text(i)
    assert "Не удалось удалить" in text and "роль бота ниже" in text
    print("  ok: /clan_roleclear показывает ошибки")


def test_clan_roleclear_requires_admin(monkeypatch):
    """Без прав админа команда ничего не удаляет."""
    _prepare_command(monkeypatch, ["123"], allowed=False)
    called = []

    async def fake_cleanup(_guild):
        called.append(1)
        return [], []

    monkeypatch.setattr(botmod, "cleanup_empty_clan_roles", fake_cleanup)
    i = _CmdInteraction()

    asyncio.run(botmod.clan_roleclear.callback(i))

    assert called == []
    print("  ok: /clan_roleclear защищена правами админа")


# ============================================================
# Роли бота, потерянные из реестров (главная причина «зависших» ролей)
# ============================================================
def test_cleanup_deletes_orphan_bot_role_by_colour():
    """Пустая фиолетовая роль бота удаляется даже без clan_roles.json."""
    orphan = _FakeRole("ㅁBrawlStars", position=5, rid=7001,
                       colour=botmod.CLAN_ROLE_COLOR)
    guild = _FakeGuild([orphan])

    deleted, errors = asyncio.run(botmod.cleanup_empty_clan_roles(
        guild, names=set()))

    assert deleted == ["ㅁBrawlStars"], deleted
    assert errors == []
    assert orphan.deleted is True
    print("  ok: осиротевшая роль бота удаляется по цвету")


def test_cleanup_keeps_coloured_role_with_members():
    """Фиолетовая роль с участниками не удаляется."""
    used = _FakeRole("Undertakers", members=[_FakeMember()], rid=7002,
                     colour=botmod.CLAN_ROLE_COLOR)
    guild = _FakeGuild([used])

    deleted, _ = asyncio.run(botmod.cleanup_empty_clan_roles(
        guild, names=set()))

    assert deleted == []
    assert used.deleted is False
    print("  ok: занятая роль остаётся")


def test_cleanup_keeps_administrator_and_service_roles():
    """Роль с админ-правами или служебным словом не удаляем даже пустой."""
    admin = _FakeRole("Custom", rid=7003, colour=botmod.CLAN_ROLE_COLOR,
                      admin=True)
    staff = _FakeRole("Модератор", rid=7004, colour=botmod.CLAN_ROLE_COLOR)
    guild = _FakeGuild([admin, staff])

    deleted, _ = asyncio.run(botmod.cleanup_empty_clan_roles(
        guild, names=set()))

    assert deleted == []
    assert admin.deleted is False and staff.deleted is False
    print("  ok: админ- и служебные роли защищены")


def test_cleanup_deletes_role_tracked_by_id_after_rename():
    """Роль переименовали на сервере — удаляем по id из bot_roles.json."""
    role = _FakeRole("новое имя", rid=7005, colour=0x000000)
    botmod._remember_bot_role(role, "clan")
    guild = _FakeGuild([role])

    deleted, _ = asyncio.run(botmod.cleanup_empty_clan_roles(
        guild, names=set()))

    assert deleted == ["новое имя"], deleted
    assert role.deleted is True
    assert "7005" not in botmod._load_bot_roles()
    print("  ok: роль узнаётся по id, даже после переименования")


def test_cleanup_ignores_site_role_colour():
    """Роли с сайта создаются другим цветом и уборкой кланов не трогаются."""
    site = _FakeRole("Спонсор", rid=7006, colour=0x2F3136)
    guild = _FakeGuild([site])

    deleted, _ = asyncio.run(botmod.cleanup_empty_clan_roles(
        guild, names=set()))

    assert deleted == [] and site.deleted is False
    print("  ok: роли с сайта не путаются с ролями кланов")


def test_empty_orphan_deleted_immediately_on_leave():
    """Вышел последний носитель — фиолетовая роль уходит сразу."""
    role = _FakeRole("плабой ебырь", rid=7007,
                     colour=botmod.CLAN_ROLE_COLOR)
    guild = _FakeGuild([role])

    deleted = asyncio.run(botmod._delete_clan_role_if_empty(
        guild, role, names=set()))

    assert deleted == "плабой ебырь"
    assert role.deleted is True
    print("  ok: осиротевшая роль удаляется сразу при выходе")


def test_cleanup_waits_for_full_member_cache():
    """Пока участники не догружены, пустые роли не удаляем."""
    role = _FakeRole("i hate lj", colour=botmod.CLAN_ROLE_COLOR, rid=7010)
    guild = _FakeGuild([role])
    guild.chunked = False

    deleted, errors = asyncio.run(botmod.cleanup_empty_clan_roles(
        guild, names={"i hate lj"}))

    assert deleted == []
    assert role.deleted is False
    assert errors and "не загружены" in errors[0]
    print("  ok: уборка ждёт полный кэш участников")
