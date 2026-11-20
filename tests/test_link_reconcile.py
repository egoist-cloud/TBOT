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


def test_nick_variants_use_only_discord_username():
    """Поиск идёт по member.name, display_name игнорируется."""
    member = _member(1, display="*MaYoR Kitti-", name="mayor")
    variants = botmod._nick_variants(member)

    assert variants == ["mayor"]


def test_pick_nick_match_requires_single_result():
    """Один результат — берём; ноль или два+ — не привязываем."""
    one = [{"nick": "Игрок Один", "steamid64": "76561198000000011"}]
    two = one + [{"nick": "Игрок Два", "steamid64": "76561198000000022"}]

    assert botmod._pick_nick_match(one, "игрок один") == one[0]
    assert botmod._pick_nick_match(two, "игрок один") is None
    assert botmod._pick_nick_match([], "кто-то") is None


def test_auto_link_member_uses_discord_username(monkeypatch):
    """display_name не важен — привязка только по username."""
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
    print("  ok: привязка по Discord username")


def test_auto_link_skips_multiple_profiles_by_username(monkeypatch):
    """ddkkx -> 2 профиля в админке: не привязываем, даже если ник совпал."""
    member = _member(1000, display="egoist", name="ddkkx")
    seen = []

    async def search(nick):
        seen.append(nick)
        return [{"nick": "ahmeddkkx", "steamid64": "76561198000000001"},
                {"nick": "egoist", "steamid64": "76561198000000002"}]

    monkeypatch.setattr(botmod.api, "cs2red_search_by_nick", search)

    steam, err = asyncio.run(botmod._auto_link_member(member, quiet=True))

    assert seen == ["ddkkx"]
    assert steam is None
    assert "профилей: 2" in err, err


def test_run_auto_link_reports_username_ambiguity(monkeypatch):
    """/auto_link ищет по username и не привязывает 2 профиля."""
    member = _member(1001, display="egoist", name="ddkkx")
    seen = []

    class _Msg:
        def __init__(self):
            self.kwargs = {}

        async def edit(self, **kwargs):
            self.kwargs = kwargs

    class _Followup:
        def __init__(self):
            self.msg = None

        async def send(self, *_args, **_kwargs):
            self.msg = _Msg()
            return self.msg

    followup = _Followup()
    interaction = SimpleNamespace(
        followup=followup,
        guild=SimpleNamespace(members=[member]),
        user=SimpleNamespace(display_name="Admin"),
        client=object(),
    )
    links = {}

    async def search(nick, limit=10):
        seen.append(nick)
        if nick == "__probe__":
            return []
        return [{"nick": "ahmeddkkx", "steamid64": "76561198000000001"},
                {"nick": "egoist", "steamid64": "76561198000000002"}]

    async def get_all_links():
        return dict(links)

    async def get_owner_of(_sid):
        return None

    async def add_link(did, sid):
        links[str(did)] = str(sid)

    def reset_block():
        return None

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(botmod, "CS2RED_COOKIES", ["x"])
    monkeypatch.setattr(botmod, "api", SimpleNamespace(
        reset_block=reset_block,
        cs2red_search_by_nick=search,
        blocked_reason="",
        last_status=200,
        RATE_LIMIT_SECONDS=0,
        extract_steam64=lambda u: u.get("steamid64"),
    ))
    monkeypatch.setattr(botmod, "links_repo", SimpleNamespace(
        get_all_links=get_all_links,
        get_owner_of=get_owner_of,
        add_link=add_link,
    ))
    monkeypatch.setattr(botmod, "_reconcile_links_from_players", noop)
    monkeypatch.setattr(botmod, "log_to_channel", noop)

    asyncio.run(botmod.run_auto_link(interaction, ephemeral=True))

    assert seen == ["__probe__", "ddkkx"]
    assert links == {}
    fields = {f.name: f.value for f in followup.msg.kwargs["embed"].fields}
    assert fields["⚠️ Неоднозначно"] == "1"
    assert fields["⏭ Не найдено"] == "0"
    assert "@ddkkx" in fields["Неоднозначные (пропущены)"]


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


# ============================================================
# Привязка по событию выдачи роли
# ============================================================
class _Role:
    def __init__(self, rid, name="роль"):
        self.id = rid
        self.name = name

    def __eq__(self, other):
        return getattr(other, "id", None) == self.id

    def __hash__(self):
        return hash(self.id)


def _event_member(uid, roles=()):
    return SimpleNamespace(id=uid, display_name="Игрок", global_name=None,
                           name="player", bot=False, mention=f"<@{uid}>",
                           roles=list(roles))


def _patch_role_event(monkeypatch, linked=False):
    """Общие заглушки для обработчика выдачи роли."""
    calls = []

    async def get_link(_uid):
        return "76561198000000099" if linked else None

    async def rate_slot():
        return None

    async def auto_link(member, quiet=False):
        calls.append(("link", member.id, quiet))
        return "76561198000000010", None

    async def log_channel(_client, text):
        calls.append(("log", text))

    monkeypatch.setattr(botmod, "CS2RED_COOKIES", {"cookie": "value"})
    monkeypatch.setattr(botmod.links_repo, "get_link", get_link)
    monkeypatch.setattr(botmod.api, "_acquire_rate_slot", rate_slot)
    monkeypatch.setattr(botmod.api, "is_blocked", lambda: False)
    monkeypatch.setattr(botmod, "_auto_link_member", auto_link)
    monkeypatch.setattr(botmod, "log_to_channel", log_channel)
    botmod._AUTO_LINK_ON_ROLE_LAST.clear()
    return calls


def test_role_grant_triggers_auto_link(monkeypatch):
    """Выдали любую роль — участник без привязки привязывается сразу."""
    calls = _patch_role_event(monkeypatch)

    before = _event_member(901)
    after = _event_member(901, roles=[_Role(5, "девушка")])
    asyncio.run(botmod.on_member_update(before, after))

    assert ("link", 901, True) in calls, calls
    assert any(c[0] == "log" for c in calls), calls
    print("  ok: выдача роли запускает привязку")


def test_role_grant_skips_linked_member(monkeypatch):
    """Уже привязанного не трогаем — лишних запросов к cs2red нет."""
    calls = _patch_role_event(monkeypatch, linked=True)

    before = _event_member(902)
    after = _event_member(902, roles=[_Role(6, "TALENT")])
    asyncio.run(botmod.on_member_update(before, after))

    assert calls == [], calls
    print("  ok: привязанный участник пропущен")


def test_role_grant_ignores_role_removal(monkeypatch):
    """Снятие роли не запускает привязку."""
    calls = _patch_role_event(monkeypatch)

    roles = [_Role(6, "TALENT")]
    asyncio.run(botmod.on_member_update(_event_member(903, roles),
                                        _event_member(903)))

    assert calls == [], calls
    print("  ok: снятие роли не привязывает")


def test_role_grant_cooldown_blocks_repeat(monkeypatch):
    """Повторная выдача роли тому же участнику не выжигает лимит cs2red."""
    calls = _patch_role_event(monkeypatch)

    before = _event_member(904)
    after = _event_member(904, roles=[_Role(7)])
    asyncio.run(botmod.on_member_update(before, after))
    asyncio.run(botmod.on_member_update(before, after))

    assert [c for c in calls if c[0] == "link"] == [("link", 904, True)]
    print("  ok: повторная выдача роли — в пределах кулдауна")


def test_auto_link_first_delay_is_early():
    """Первый обход после старта — через 2–5 минут, а не через 10–20."""
    delay = botmod._auto_link_first_delay()
    assert 120 <= delay <= 300, delay
