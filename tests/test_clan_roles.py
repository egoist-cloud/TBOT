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
    def __init__(self, name, members=(), position=5, hoist=False, rid=1):
        self.id = rid
        self.name = name
        self.members = list(members)
        self.position = position
        self.hoist = hoist
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
