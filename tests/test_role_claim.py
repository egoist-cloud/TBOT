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


# ============================================================
# Ровно одна панель в канале
# ============================================================
import discord  # noqa: E402


class _PanelMessage:
    def __init__(self, mid, title=botmod.ROLE_PANEL_TITLE):
        self.id = mid
        self.author = SimpleNamespace(bot=True)
        self.embeds = [discord.Embed(title=title)]
        self.components = []
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _PanelChannel(_Channel):
    def __init__(self, messages=()):
        super().__init__()
        self.messages = list(messages)

    def history(self, limit=100):
        async def gen():
            for message in self.messages[:limit]:
                yield message

        return gen()

    async def fetch_message(self, mid):
        for message in self.messages:
            if str(message.id) == str(mid):
                return message
        raise RuntimeError(f"unknown message {mid}")


def _patch_flags(monkeypatch, flags):
    async def get_flag(name):
        return flags.get(name)

    async def set_flag(name, value):
        flags[name] = value

    monkeypatch.setattr(botmod.seen_repo, "get_flag", get_flag)
    monkeypatch.setattr(botmod.seen_repo, "set_flag", set_flag)


def test_ensure_role_panel_removes_duplicates(monkeypatch):
    """Три панели в канале — остаётся одна, лишние удаляются."""
    panels = [_PanelMessage(mid) for mid in (101, 102, 103)]
    channel = _PanelChannel(panels)
    flags = {botmod.ROLE_PANEL_FLAG: "101"}
    _patch_flags(monkeypatch, flags)
    client = SimpleNamespace(get_channel=lambda _cid: channel)

    status = asyncio.run(botmod.ensure_role_panel(client))

    assert status == "панель уже на месте, удалено дублей: 2", status
    assert panels[0].deleted is False
    assert panels[1].deleted is True and panels[2].deleted is True
    assert channel.sent == [], "новую панель не публикуем"
    print("  ok: дубли панели ролей удаляются")


def test_ensure_role_panel_adopts_existing_without_flag(monkeypatch):
    """Флаг потерян, но панель в канале есть — усыновляем, не дублируем."""
    panels = [_PanelMessage(mid) for mid in (201, 202)]
    channel = _PanelChannel(panels)
    flags = {}
    _patch_flags(monkeypatch, flags)
    client = SimpleNamespace(get_channel=lambda _cid: channel)

    status = asyncio.run(botmod.ensure_role_panel(client))

    assert status == "панель уже на месте, удалено дублей: 1", status
    assert flags[botmod.ROLE_PANEL_FLAG] == "201"
    assert channel.sent == []
    assert panels[0].deleted is False and panels[1].deleted is True
    print("  ok: потерянный флаг не создаёт вторую панель")


def test_ensure_role_panel_skips_when_history_unreadable(monkeypatch):
    """История недоступна и флага нет — лучше не публиковать, чем дублировать."""
    class _NoHistory(_Channel):
        def history(self, limit=100):
            async def gen():
                raise RuntimeError("missing access")
                yield  # pragma: no cover

            return gen()

    channel = _NoHistory()
    flags = {}
    _patch_flags(monkeypatch, flags)
    client = SimpleNamespace(get_channel=lambda _cid: channel)

    status = asyncio.run(botmod.ensure_role_panel(client))

    assert "нет доступа к истории" in status, status
    assert channel.sent == []
    print("  ok: без истории канала панель не дублируется")
