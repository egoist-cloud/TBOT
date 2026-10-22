# -*- coding: utf-8 -*-
"""Тесты ролей клана и отчёта /check в лог-канал."""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402

STEAM = "76561198000000000"


def test_clan_members_roundtrip():
    botmod._forget_member_clan_role(111)
    assert botmod._load_clan_members().get("111") is None

    botmod._remember_member_clan_role(111, 555)
    assert botmod._load_clan_members()["111"] == 555

    # Повторная запись того же значения не ломает файл.
    botmod._remember_member_clan_role(111, 555)
    assert botmod._load_clan_members()["111"] == 555

    botmod._remember_member_clan_role(222, 777)
    botmod._forget_member_clan_role(111)
    data = botmod._load_clan_members()
    assert "111" not in data
    assert data["222"] == 777
    botmod._forget_member_clan_role(222)


def test_remember_clan_role_name():
    botmod._remember_clan_role_name("TestClan")
    assert "TestClan" in botmod._load_clan_role_names()
    names = botmod._known_clan_role_names([{"name": "OtherClan"}])
    assert "TestClan" in names
    assert "OtherClan" in names


def test_clan_roles_above_bot():
    role_below = SimpleNamespace(name="Below", position=3)
    role_equal = SimpleNamespace(name="Equal", position=5)
    role_above = SimpleNamespace(name="Above", position=7)
    guild = SimpleNamespace(me=SimpleNamespace(top_role=SimpleNamespace(position=5)))

    blocked = botmod._clan_roles_above_bot(guild, [role_below, role_equal, role_above])
    assert [r.name for r in blocked] == ["Equal", "Above"]


def test_clan_roles_above_bot_without_me():
    roles = [SimpleNamespace(name="A", position=1)]
    guild = SimpleNamespace(me=None)
    assert botmod._clan_roles_above_bot(guild, roles) == roles


def _block():
    now = int(time.time())
    raw = {
        "reason": "Мат в чате",
        "timestamp": now - 600,
        "endTimeStamp": now + 3600,
        "adminId": 42,
        "user": {"id": 42, "nick": "lilcom"},
        "action": {"id": 7, "nick": "sllletron", "steamid64": STEAM},
    }
    return botmod.API.normalize_block(raw, "Мут")


def test_check_log_text_with_blocks():
    member = SimpleNamespace(display_name="Miki", id=123456789)
    text = botmod._build_check_log_text("admin", 12, [(member, STEAM, [_block()])], 2, 1)
    assert "Инициатор: admin" in text
    assert "Проверено: 12" in text
    assert "Miki" in text
    assert "123456789" in text
    assert "Мут" in text
    assert "Мат в чате" in text
    assert "выдал: lilcom" in text


def test_check_log_text_empty():
    text = botmod._build_check_log_text("admin", 5, [], 1, 0)
    assert "Активных блокировок нет" in text
    assert "без SteamID: 1" in text


# ---------- автоматическое снятие роли клана ----------

class _FakeRole:
    def __init__(self, role_id, name, position=1):
        self.id = role_id
        self.name = name
        self.position = position

    def __eq__(self, other):
        return isinstance(other, _FakeRole) and other.id == self.id

    def __hash__(self):
        return hash(self.id)


class _FakeMember:
    def __init__(self, member_id, roles):
        self.id = member_id
        self.display_name = "Miki"
        self.roles = list(roles)
        self.removed = []
        self.added = []

    async def remove_roles(self, *roles, reason=None):
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)
            self.removed.append(role)

    async def add_roles(self, *roles, reason=None):
        for role in roles:
            self.roles.append(role)
            self.added.append(role)


class _FakeGuild:
    def __init__(self, roles, me_top=10):
        self.roles = list(roles)
        self.me = SimpleNamespace(
            top_role=SimpleNamespace(position=me_top),
            guild_permissions=SimpleNamespace(manage_roles=True),
        )

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)


def _fake_api(monkeypatch, clans):
    async def fake_user(_sid):
        return {"id": 7, "nick": "Miki"}

    async def fake_clans():
        return clans

    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)
    monkeypatch.setattr(botmod.api, "cs2red_clans", fake_clans)


def test_auto_remove_role_when_left_clan(monkeypatch):
    """Игрок вышел из клана — автоматика снимает роль и чистит clan_members."""
    old_role = _FakeRole(555, "OldClan", position=3)
    guild = _FakeGuild([old_role])
    member = _FakeMember(111222333, [old_role])

    botmod._remember_member_clan_role(member.id, old_role.id)
    _fake_api(monkeypatch, [{"id": 1, "name": "OldClan",
                             "members": [{"userId": 99}]}])

    result = asyncio.run(botmod.sync_clan_role_by_id(guild, member, STEAM))
    assert "клан снят" in result
    assert old_role in member.removed
    assert old_role not in member.roles
    assert botmod._load_clan_members().get(str(member.id)) is None


def test_auto_remove_role_by_stored_id(monkeypatch):
    """Клан переименовали/удалили — роль находится по clan_members.json."""
    old_role = _FakeRole(777, "RenamedClan", position=3)
    guild = _FakeGuild([old_role])
    member = _FakeMember(444555666, [old_role])

    botmod._remember_member_clan_role(member.id, old_role.id)
    _fake_api(monkeypatch, [{"id": 2, "name": "ДругойКлан",
                             "members": [{"userId": 99}]}])

    result = asyncio.run(botmod.sync_clan_role_by_id(guild, member, STEAM))
    assert "клан снят" in result
    assert old_role in member.removed


def test_auto_remove_reports_hierarchy(monkeypatch):
    """Роль выше роли бота — не пытаемся снять, объясняем причину."""
    high_role = _FakeRole(888, "HighClan", position=12)
    guild = _FakeGuild([high_role], me_top=10)
    member = _FakeMember(777888999, [high_role])

    _fake_api(monkeypatch, [{"id": 3, "name": "HighClan",
                             "members": [{"userId": 99}]}])

    result = asyncio.run(botmod.sync_clan_role_by_id(guild, member, STEAM))
    assert "выше" in result
    assert "поднимите роль бота" in result
    assert high_role not in member.removed
    assert high_role in member.roles


def test_auto_switch_clan_removes_old_role(monkeypatch):
    """Игрок перешёл в другой клан — старая роль снимается, новая выдаётся."""
    old_role = _FakeRole(555, "OldClan", position=3)
    new_role = _FakeRole(556, "NewClan", position=4)
    guild = _FakeGuild([old_role, new_role])
    member = _FakeMember(123123123, [old_role])

    # Так выглядит состояние после прошлой выдачи роли ботом.
    botmod._remember_clan_role_name("OldClan")
    botmod._remember_member_clan_role(member.id, old_role.id)

    _fake_api(monkeypatch, [{"id": 4, "name": "NewClan",
                             "members": [{"userId": 7}]}])

    result = asyncio.run(botmod.sync_clan_role_by_id(guild, member, STEAM))
    assert "выдан клан" in result
    assert old_role in member.removed
    assert new_role in member.roles
    assert botmod._load_clan_members()[str(member.id)] == new_role.id
    botmod._forget_member_clan_role(member.id)
