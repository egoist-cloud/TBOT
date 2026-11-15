# -*- coding: utf-8 -*-
"""Восстановление потерянных привязок и сопоставление ников.

Проверяем два случая, из-за которых /check_clan писал «не авторизован» у
привязанных игроков: запись links потерялась, а профиль players остался;
и ник в Discord отличается от ника на cs2red.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402


class _Guild:
    def __init__(self, members=()):
        self._members = {m.id: m for m in members}

    def get_member(self, uid):
        return self._members.get(int(uid))


def _member(uid, display="Игрок", name="player"):
    return SimpleNamespace(id=uid, display_name=display, global_name=None,
                           name=name, bot=False, mention=f"<@{uid}>")


def test_reconcile_links_from_players(monkeypatch):
    """Профиль players с discord_id возвращает потерянную привязку."""
    member = _member(555)
    restored = {}

    async def get_all_players():
        return {"76561198000000001": {"discord_id": "555"}}

    async def get_all_links():
        return {}

    async def add_link(did, sid):
        restored[str(did)] = str(sid)

    monkeypatch.setattr(botmod.players_repo, "get_all_players", get_all_players)
    monkeypatch.setattr(botmod.links_repo, "get_all_links", get_all_links)
    monkeypatch.setattr(botmod.links_repo, "add_link", add_link)

    count = asyncio.run(botmod._reconcile_links_from_players(_Guild([member])))

    assert count == 1
    assert restored == {"555": "76561198000000001"}
    print("  ok: потерянная привязка восстановлена из профиля")


def test_reconcile_skips_foreign_and_unknown(monkeypatch):
    """Чужой Steam и участник вне сервера не перезаписываются."""
    restored = []

    async def get_all_players():
        return {"76561198000000002": {"discord_id": "777"},
                "76561198000000003": {"discord_id": "888"}}

    async def get_all_links():
        return {"777": "76561198000000099"}  # у 777 уже есть привязка

    async def add_link(did, sid):
        restored.append((str(did), str(sid)))

    monkeypatch.setattr(botmod.players_repo, "get_all_players", get_all_players)
    monkeypatch.setattr(botmod.links_repo, "get_all_links", get_all_links)
    monkeypatch.setattr(botmod.links_repo, "add_link", add_link)

    count = asyncio.run(botmod._reconcile_links_from_players(_Guild([])))

    assert count == 0
    assert restored == []
    print("  ok: reconcile не трогает чужие привязки")


def test_nick_variants_use_username_as_fallback():
    """Если display_name не нашёлся, пробуем username и чистим украшения."""
    member = _member(1, display="*MaYoR Kitti-", name="mayor")
    variants = botmod._nick_variants(member)

    assert variants[0] == "*MaYoR Kitti-"
    assert "MaYoR Kitti" in variants
    assert "mayor" in variants


def test_pick_nick_match_prefers_exact():
    """Из нескольких результатов берём точное совпадение ника."""
    users = [{"nick": "Игрок Два", "steamid64": "76561198000000022"},
             {"nick": "Игрок Один", "steamid64": "76561198000000011"}]

    match = botmod._pick_nick_match(users, "игрок один")

    assert match["steamid64"] == "76561198000000011"
    assert botmod._pick_nick_match(users, "кто-то") is None


def test_auto_link_member_falls_back_to_username(monkeypatch):
    """display_name не найден — привязка проходит по username."""
    member = _member(999, display="Ник С Эмодзи 🎮", name="egoist")

    async def search(nick):
        if nick == "egoist":
            return [{"nick": "egoist", "steamid64": "76561198000000077"}]
        return []

    async def owner_of(_sid):
        return None

    added = {}

    async def add_link(did, sid):
        added[str(did)] = str(sid)

    monkeypatch.setattr(botmod.api, "cs2red_search_by_nick", search)
    monkeypatch.setattr(botmod.links_repo, "get_owner_of", owner_of)
    monkeypatch.setattr(botmod.links_repo, "add_link", add_link)

    steam, err = asyncio.run(botmod._auto_link_member(member, quiet=True))

    assert err is None
    assert steam == "76561198000000077"
    assert added == {"999": "76561198000000077"}
    print("  ok: привязка по username, если display_name не найден")


def test_relink_tries_all_unlinked_from_members(monkeypatch):
    """/check_clan пробует привязать всех непривязанных из своего списка."""
    with_role = _member(30)
    without_role = _member(31)

    async def get_all_links():
        return {}

    async def link(member, quiet=False):
        return ("76561198000000030", None) if member.id == 30 else (None, "нет")

    monkeypatch.setattr(botmod.links_repo, "get_all_links", get_all_links)
    monkeypatch.setattr(botmod, "_auto_link_member", link)
    monkeypatch.setattr(botmod.api, "is_blocked", lambda: False)
    monkeypatch.setattr(botmod.api, "RATE_LIMIT_SECONDS", 0)
    monkeypatch.setattr(botmod, "CHECK_CLAN_AUTO_LINK", 1)

    added, failed = asyncio.run(
        botmod._relink_role_members([with_role, without_role]))

    assert added == 1
    assert len(failed) == 1, failed
    print("  ok: /check_clan пробует привязать всех без /link")
