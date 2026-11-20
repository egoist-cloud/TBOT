# -*- coding: utf-8 -*-
"""Тесты ролей с сайта cs2red: имя роли берётся с сайта, роль создаётся сама."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402


class _Role:
    def __init__(self, name, rid=1, position=1):
        self.id = rid
        self.name = name
        self.position = position


class _Member:
    def __init__(self, roles=(), name="Игрок"):
        self.id = 1
        self.display_name = name
        self.roles = list(roles)
        self.added = []
        self.removed = []

    async def add_roles(self, role):
        self.roles.append(role)
        self.added.append(role)

    async def remove_roles(self, role):
        if role in self.roles:
            self.roles.remove(role)
        self.removed.append(role)


class _Guild:
    def __init__(self, roles=(), manage_roles=True):
        self.roles = list(roles)
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=manage_roles),
            top_role=_Role("bot", position=50),
        )
        self.created = []

    async def create_role(self, **kwargs):
        role = _Role(kwargs["name"], rid=900 + len(self.created))
        self.created.append(kwargs)
        self.roles.append(role)
        return role


def _patch(monkeypatch, names):
    """Подменяет список ролей сайта и файл site_roles.json."""
    store = set()

    async def fake_roles():
        return names

    monkeypatch.setattr(botmod.api, "cs2red_roles", fake_roles)
    monkeypatch.setattr(botmod, "_load_site_role_names", lambda: set(store))
    monkeypatch.setattr(botmod, "_remember_site_role_name", store.add)
    return store


# ============================================================
# Названия ролей
# ============================================================
def test_site_role_name_comes_from_site_list(monkeypatch):
    """Название берётся из списка сайта: roleId=2 → «Куратор»."""
    _patch(monkeypatch, {2: "Куратор"})
    assert asyncio.run(botmod.resolve_site_role_name(2)) == "Куратор"
    print("  ok: «Куратор» берётся с сайта")


def test_site_role_name_fallback(monkeypatch):
    """Сайт не ответил — работает локальная карта."""
    _patch(monkeypatch, {})
    assert asyncio.run(botmod.resolve_site_role_name(3)) == "Модератор"
    print("  ok: есть локальный запас названий")


# ============================================================
# Синхронизация
# ============================================================
def test_sync_creates_missing_role_and_assigns(monkeypatch):
    """Роли «Куратор» нет на сервере — бот создаёт её и выдаёт."""
    store = _patch(monkeypatch, {2: "Куратор"})
    guild = _Guild([])
    member = _Member()

    res = asyncio.run(botmod.sync_role_by_id(
        guild, member, "76561198000000001", user={"id": 7, "roleId": 2}))

    assert "Куратор" in res, res
    assert guild.created and guild.created[0]["name"] == "Куратор"
    assert member.added and member.added[0].name == "Куратор"
    assert "Куратор" in store, "созданная роль запоминается"
    print("  ok: роль с сайта создаётся и выдаётся")


def test_sync_reuses_existing_role(monkeypatch):
    """Роль с таким названием уже есть — второй раз не создаём."""
    _patch(monkeypatch, {2: "Куратор"})
    existing = _Role("Куратор", rid=77)
    guild = _Guild([existing])
    member = _Member()

    asyncio.run(botmod.sync_role_by_id(
        guild, member, "1", user={"id": 7, "roleId": 2}))

    assert guild.created == [], "дубликат роли не создаётся"
    assert member.added == [existing]
    print("  ok: существующая роль переиспользуется")


def test_sync_switches_role(monkeypatch):
    """Роль на сайте сменилась — старую ботовую снимаем, новую выдаём."""
    store = _patch(monkeypatch, {2: "Куратор"})
    store.add("Модератор")
    old = _Role("Модератор", rid=55)
    guild = _Guild([old])
    member = _Member([old])

    asyncio.run(botmod.sync_role_by_id(
        guild, member, "1", user={"id": 7, "roleId": 2}))

    assert old in member.removed
    assert [r.name for r in member.added] == ["Куратор"]
    print("  ok: при смене роли старая снимается")


def test_sync_without_role_removes_bot_roles(monkeypatch):
    """Игрок без роли на сайте — ботовые роли снимаются."""
    store = _patch(monkeypatch, {})
    store.add("Куратор")
    old = _Role("Куратор", rid=55)
    guild = _Guild([old])
    member = _Member([old])

    res = asyncio.run(botmod.sync_role_by_id(
        guild, member, "1", user={"id": 7, "roleId": 0}))

    assert "сняты" in res
    assert old in member.removed
    assert member.added == []
    print("  ok: без роли на сайте — роль снимается")


def test_sync_no_manage_roles_does_not_create(monkeypatch):
    """Нет права Manage Roles — роли не создаём, синхронизация не падает."""
    _patch(monkeypatch, {2: "Куратор"})
    guild = _Guild([], manage_roles=False)
    member = _Member()

    res = asyncio.run(botmod.sync_role_by_id(
        guild, member, "1", user={"id": 7, "roleId": 2}))

    assert guild.created == []
    assert "не удалось" in res
    print("  ok: без Manage Roles роль не создаётся")


# ============================================================
# Роль с сайта не снимается, когда данных не хватает
# ============================================================
def test_sync_keeps_role_when_roleid_missing(monkeypatch):
    """В ответе cs2red нет roleId — роль не трогаем."""
    _patch(monkeypatch, {})
    held = _Role("Модератор", rid=77)
    guild = _Guild([held])
    member = _Member([held])

    res = asyncio.run(botmod.sync_role_by_id(
        guild, member, "76561198000000001", user={"id": 7}))

    assert "нет roleId" in res, res
    assert member.removed == []
    assert held in member.roles


def test_sync_keeps_role_for_unknown_roleid(monkeypatch):
    """roleId есть, но названия бот не знает — снимать нельзя."""
    _patch(monkeypatch, {})
    held = _Role("Модератор", rid=77)
    guild = _Guild([held])
    member = _Member([held])

    res = asyncio.run(botmod.sync_role_by_id(
        guild, member, "76561198000000001", user={"id": 7, "roleId": 4242}))

    assert "неизвестна" in res, res
    assert member.removed == []
    assert held in member.roles


def test_sync_removes_role_when_site_has_nothing(monkeypatch):
    """roleId=0 — на сайте роли нет, старое поведение сохраняется."""
    _patch(monkeypatch, {})
    held = _Role("VIP", rid=77)
    guild = _Guild([held])
    member = _Member([held])

    res = asyncio.run(botmod.sync_role_by_id(
        guild, member, "76561198000000001", user={"id": 7, "roleId": 0}))

    assert "сняты" in res, res
    assert held not in member.roles
