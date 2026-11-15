# -*- coding: utf-8 -*-
"""Тесты кнопки «Получить роль»: авто-привязка по нику + синхронизация ролей."""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402

SID = "76561198000000001"


def _member(uid=700001, nick="egoist"):
    return SimpleNamespace(id=uid, display_name=nick, mention=f"<@{uid}>",
                           bot=False)


class _Response:
    async def defer(self, ephemeral=False):
        return None


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def text(self) -> str:
        args, kwargs = self.sent[-1]
        embed = kwargs.get("embed")
        parts = [str(a) for a in args]
        if embed is not None:
            data = embed.to_dict()
            parts += [str(data.get("title", "")), str(data.get("description", ""))]
        return " ".join(parts)


class _Interaction:
    def __init__(self, member=None):
        self.response = _Response()
        self.followup = _Followup()
        self.guild = SimpleNamespace(
            get_member=lambda _uid: member or _member())
        self.user = SimpleNamespace(id=(member or _member()).id)
        self.client = object()


def _reset_cooldown():
    botmod._ROLE_CLAIM_LAST.clear()


def _claim_item():
    view = botmod.RoleClaimView()
    return next(i for i in view.children if i.custom_id == "role_claim_btn")


# ============================================================
# Авто-привязка
# ============================================================
def test_auto_link_single_match(monkeypatch):
    linked = []

    async def search(_nick, limit=10):
        return [{"steamid64": SID, "nick": "egoist"}]

    async def owner(_sid):
        return None

    async def add(uid, sid):
        linked.append((uid, sid))

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(botmod.api, "cs2red_search_by_nick", search)
    monkeypatch.setattr(botmod.links_repo, "get_owner_of", owner)
    monkeypatch.setattr(botmod.links_repo, "add_link", add)
    monkeypatch.setattr(botmod, "log_to_channel", noop)

    steam, err = asyncio.run(botmod._auto_link_member(_member()))

    assert steam == SID and err is None
    assert linked == [(700001, SID)]
    print("  ok: один профиль — привязываем")


def test_auto_link_ambiguous(monkeypatch):
    async def search(_nick, limit=10):
        return [{"steamid64": SID}, {"steamid64": "76561198000000002"}]

    monkeypatch.setattr(botmod.api, "cs2red_search_by_nick", search)

    steam, err = asyncio.run(botmod._auto_link_member(_member()))
    assert steam is None and "профилей: 2" in err
    print("  ok: несколько профилей — не угадываем")


def test_auto_link_respects_owner(monkeypatch):
    async def search(_nick, limit=10):
        return [{"steamid64": SID}]

    async def owner(_sid):
        return "999999"

    monkeypatch.setattr(botmod.api, "cs2red_search_by_nick", search)
    monkeypatch.setattr(botmod.links_repo, "get_owner_of", owner)

    steam, err = asyncio.run(botmod._auto_link_member(_member()))
    assert steam is None and "другому Discord" in err
    print("  ok: чужой SteamID не перехватываем")


# ============================================================
# Кнопка
# ============================================================
def _patch_claim(monkeypatch, linked=True):
    calls = []

    async def get_link(_uid):
        return SID if linked else None

    async def auto(_member):
        return SID, None

    async def get_user(_sid):
        return {"id": 1, "roleId": 3}

    async def sync_role(guild, member, sid, user=None):
        calls.append("site")
        return "✅ роль сайта"

    async def sync_clan(guild, member, sid, user=None):
        calls.append("clan")
        return "✅ роль клана"

    async def noop(*_a, **_kw):
        return None

    monkeypatch.setattr(botmod.links_repo, "get_link", get_link)
    monkeypatch.setattr(botmod, "_auto_link_member", auto)
    monkeypatch.setattr(botmod.api, "cs2red_user", get_user)
    monkeypatch.setattr(botmod, "sync_role_by_id", sync_role)
    monkeypatch.setattr(botmod, "sync_clan_role_by_id", sync_clan)
    monkeypatch.setattr(botmod, "audit_action", noop)
    return calls


def test_claim_button_syncs_roles(monkeypatch):
    _reset_cooldown()
    calls = _patch_claim(monkeypatch, linked=True)
    i = _Interaction()

    asyncio.run(_claim_item().callback(i))

    assert calls == ["site", "clan"], calls
    assert "роль сайта" in i.followup.text()
    print("  ok: кнопка выдаёт роль сайта и клана")


def test_claim_button_links_then_syncs(monkeypatch):
    """Непривязанному сначала находится SteamID, потом выдаются роли."""
    _reset_cooldown()
    calls = _patch_claim(monkeypatch, linked=False)
    i = _Interaction()

    asyncio.run(_claim_item().callback(i))

    assert calls == ["site", "clan"]
    assert "Привязан SteamID" in i.followup.text()
    print("  ok: сначала авто-привязка, потом роли")


def test_claim_button_cooldown(monkeypatch):
    _reset_cooldown()
    _patch_claim(monkeypatch, linked=True)
    i = _Interaction()
    botmod._ROLE_CLAIM_LAST[i.user.id] = time.time()

    asyncio.run(_claim_item().callback(i))

    assert "Подожди минуту" in i.followup.text()
    print("  ok: защита от спама кнопкой")


# ============================================================
# Публикация панели
# ============================================================
class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=555, edit=None)

    async def fetch_message(self, _mid):
        return SimpleNamespace(id=_mid)


def test_ensure_role_panel_publishes_once(monkeypatch):
    channel = _Channel()
    flags = {}
    client = SimpleNamespace(get_channel=lambda _cid: channel)

    async def get_flag(_name):
        return flags.get(_name)

    async def set_flag(name, value):
        flags[name] = value

    monkeypatch.setattr(botmod.seen_repo, "get_flag", get_flag)
    monkeypatch.setattr(botmod.seen_repo, "set_flag", set_flag)

    status = asyncio.run(botmod.ensure_role_panel(client))
    assert status == "панель опубликована"
    assert len(channel.sent) == 1
    assert channel.sent[0]["embed"] is not None
    assert flags[botmod.ROLE_PANEL_FLAG] == "555"

    # повторный запуск: сообщение найдено — второй панели нет
    status = asyncio.run(botmod.ensure_role_panel(client))
    assert status == "панель уже на месте"
    assert len(channel.sent) == 1
    print("  ok: панель публикуется один раз")
