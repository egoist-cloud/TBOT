# -*- coding: utf-8 -*-
"""Тесты ролей клана и отчёта /check_all в лог-канал."""
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
    assert "Блокировок не найдено" in text
    assert "без SteamID: 1" in text


# ---------- автоматическое снятие роли клана ----------

class _FakeRole:
    def __init__(self, role_id, name, position=1):
        self.id = role_id
        self.name = name
        self.position = position
        self.members = []
        self.deleted = False

    async def delete(self, reason=None):
        self.deleted = True

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
        self.bot = False

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


def test_clan_sync_members_includes_all_linked(monkeypatch):
    """В /check_clan попадают все /link, даже без роли TALENT."""
    talent = _FakeRole(botmod.TALENT_ROLE_ID, "TALENT")
    guild = _FakeGuild([talent])
    talent_member = _FakeMember(111, [talent])
    linked_member = _FakeMember(222, [])
    bot_member = _FakeMember(333, [talent])
    bot_member.bot = True
    guild.members = [talent_member, linked_member, bot_member]

    async def fake_links():
        return {"111": STEAM, "222": "76561198000000001"}

    monkeypatch.setattr(botmod.links_repo, "get_all_links", fake_links)

    members = asyncio.run(botmod._clan_sync_members(guild))
    assert {m.id for m in members} == {111, 222}


def test_auto_remove_role_when_left_clan(monkeypatch):
    """Игрок вышел из клана — автоматика снимает роль и чистит clan_members."""
    old_role = _FakeRole(555, "OldClan", position=3)
    guild = _FakeGuild([old_role])
    member = _FakeMember(111222333, [old_role])
    monkeypatch.setattr(botmod, "CLAN_ROLE_REMOVE_CONFIRMATIONS", 1)

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
    monkeypatch.setattr(botmod, "CLAN_ROLE_REMOVE_CONFIRMATIONS", 1)

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
    monkeypatch.setattr(botmod, "CLAN_ROLE_REMOVE_CONFIRMATIONS", 1)

    _fake_api(monkeypatch, [{"id": 3, "name": "HighClan",
                             "members": [{"userId": 99}]}])

    result = asyncio.run(botmod.sync_clan_role_by_id(guild, member, STEAM))
    assert "не полностью" in result
    assert "выше роли бота" in result
    assert high_role not in member.removed
    assert high_role in member.roles


def test_auto_remove_succeeds_for_low_role_even_if_high_stays(monkeypatch):
    """Одна роль выше бота не мешает снять остальные роли клана."""
    high_role = _FakeRole(888, "HighClan", position=12)
    low_role = _FakeRole(889, "LowClan", position=3)
    guild = _FakeGuild([high_role, low_role], me_top=10)
    member = _FakeMember(777888990, [high_role, low_role])
    monkeypatch.setattr(botmod, "CLAN_ROLE_REMOVE_CONFIRMATIONS", 1)

    # Низкая роль известна боту (запомнена при прошлой синхронизации).
    botmod._remember_clan_role_name("LowClan")

    _fake_api(monkeypatch, [{"id": 3, "name": "HighClan",
                             "members": [{"userId": 99}]}])

    result = asyncio.run(botmod.sync_clan_role_by_id(guild, member, STEAM))
    assert "не полностью" in result
    assert low_role in member.removed
    assert low_role not in member.roles
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


def test_all_blocks_falls_back_only_for_missing_array(monkeypatch):
    """Если в админ-профиле нет mutes, публичный fallback нужен только для них."""
    now = int(time.time())
    raw_ban = {
        "id": 1, "adminId": 42, "reason": "test",
        "timestamp": now - 60, "endTimeStamp": now + 3600, "unbanId": None,
        "user": {"id": 42, "nick": "lilcom"},
        "action": {"id": 7, "nick": "Miki", "steamid64": STEAM},
    }

    async def fake_user(_sid):
        return {"id": 7, "nick": "Miki"}

    async def fake_full(_sid, user=None):
        return {"bans": [raw_ban], "user": {"id": 7}}

    calls = []

    async def fake_public(sid, category, uid=0):
        calls.append(category)
        return [], ""

    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)
    monkeypatch.setattr(botmod.api, "cs2red_full_user", fake_full)
    monkeypatch.setattr(botmod.api, "_public_blocks", fake_public)

    bans, mutes, err = asyncio.run(botmod.api.cs2red_all_blocks(STEAM))
    assert err == ""
    assert len(bans) == 1
    assert mutes == []
    assert calls == ["MUTES"]


def test_clan_report_skips_unchanged_roles(monkeypatch):
    """В «Изменения ролей» не должны попадать игроки, у которых всё уже верно."""
    sent = {}

    async def fake_safe_send(_target, *args, **kwargs):
        sent["text"] = args[0]
        sent["title"] = kwargs.get("title")

    monkeypatch.setattr(botmod, "safe_send", fake_safe_send)
    monkeypatch.setattr(botmod.bot, "get_channel", lambda _cid: object())

    same = SimpleNamespace(display_name="Same")
    removed = SimpleNamespace(display_name="Removed")
    results = [(same, "✅ уже в 'X'"), (removed, "🔄 клан снят")]
    asyncio.run(botmod._report_clan_sync(results, 2, 1, "Тест"))

    assert "🔄 клан снят" in sent["text"]
    assert "уже в" not in sent["text"]
    assert "Тест" in sent["title"]


def test_auto_remove_skips_when_user_id_missing(monkeypatch):
    """Некорректный ответ cs2red (без id) не должен снимать роль клана."""
    old_role = _FakeRole(555, "OldClan", position=3)
    guild = _FakeGuild([old_role])
    member = _FakeMember(999888777, [old_role])

    async def fake_user(_sid):
        return {"nick": "Miki"}  # нет ни id, ни roleId

    async def fake_clans():
        return [{"id": 1, "name": "OldClan", "members": []}]

    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)
    monkeypatch.setattr(botmod.api, "cs2red_clans", fake_clans)

    result = asyncio.run(botmod.sync_clan_role_by_id(guild, member, STEAM))
    assert "роли не тронуты" in result, result
    assert old_role in member.roles
    assert member.removed == []


def test_sync_role_skips_when_user_id_missing(monkeypatch):
    """sync_role_by_id не трогает роли, если в ответе cs2red нет id."""
    role = _FakeRole(601, "VIP", position=3)
    guild = _FakeGuild([role])
    member = _FakeMember(555444333, [role])

    async def fake_user(_sid):
        return {"nick": "Miki"}

    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)

    result = asyncio.run(botmod.sync_role_by_id(guild, member, STEAM))
    assert "роль не тронута" in result, result
    assert role in member.roles
    assert member.removed == []


def test_normalize_block_active_none_is_active():
    """active=null в ответе cs2red — блокировка всё ещё активна."""
    now = int(time.time())
    block = botmod.api.normalize_block(
        {"active": None, "timestamp": now - 60, "endTimeStamp": now + 3600,
         "action": {"steamid64": STEAM, "nick": "Miki"}},
        "Бан", from_admin=True)
    assert block["active"] is True


def test_normalize_block_explicit_false_is_inactive():
    now = int(time.time())
    block = botmod.api.normalize_block(
        {"active": False, "timestamp": now - 60, "endTimeStamp": now + 3600,
         "action": {"steamid64": STEAM, "nick": "Miki"}},
        "Бан", from_admin=True)
    assert block["active"] is False


# ============================================================
# Роль не снимается без подтверждения
# ============================================================
def test_clan_role_kept_on_first_miss_and_removed_on_second(monkeypatch):
    """Один неполный ответ /api/clans не снимает роль клана."""
    old_role = _FakeRole(901, "OldClan", position=3)
    guild = _FakeGuild([old_role])
    member = _FakeMember(121212121, [old_role])

    botmod._remember_clan_role_name("OldClan")
    botmod._CLAN_MISS_COUNTS.pop(member.id, None)
    monkeypatch.setattr(botmod, "CLAN_ROLE_REMOVE_CONFIRMATIONS", 2)
    _fake_api(monkeypatch, [{"id": 1, "name": "OldClan",
                             "members": [{"userId": 99}]}])

    first = asyncio.run(botmod.sync_clan_role_by_id(guild, member, STEAM))
    assert old_role in member.roles, first
    assert "не найден" in first, first

    second = asyncio.run(botmod.sync_clan_role_by_id(guild, member, STEAM))
    assert old_role not in member.roles, second
    assert "клан снят" in second, second
    assert botmod._CLAN_MISS_COUNTS.get(member.id) is None


def test_clan_role_miss_counter_resets_when_player_found(monkeypatch):
    """Игрок снова в составе — счётчик промахов обнуляется."""
    role = _FakeRole(902, "OldClan", position=3)
    guild = _FakeGuild([role])
    member = _FakeMember(131313131, [role])

    botmod._remember_clan_role_name("OldClan")
    monkeypatch.setattr(botmod, "CLAN_ROLE_REMOVE_CONFIRMATIONS", 2)
    _fake_api(monkeypatch, [{"id": 1, "name": "OldClan",
                             "members": [{"userId": 99}]}])
    asyncio.run(botmod.sync_clan_role_by_id(guild, member, STEAM))
    assert botmod._CLAN_MISS_COUNTS.get(member.id) == 1

    botmod._CLAN_MISS_COUNTS[member.id] = 1
    _fake_api(monkeypatch, [{"id": 1, "name": "OldClan",
                             "members": [{"userId": 7}]}])
    asyncio.run(botmod.sync_clan_role_by_id(guild, member, STEAM))
    assert botmod._CLAN_MISS_COUNTS.get(member.id) is None
    assert role in member.roles
