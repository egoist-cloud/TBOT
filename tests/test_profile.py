# -*- coding: utf-8 -*-
"""Тесты /profile: войс, сообщения, роли с сайта, без репутации и койнов."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

import script as botmod  # noqa: E402
from quantum.db import close_db, get_db, init_db  # noqa: E402
from quantum.repositories import stats as stats_repo  # noqa: E402


def _text_of(layout) -> str:
    return "\n".join(
        item.content for item in layout.walk_children()
        if isinstance(item, discord.ui.TextDisplay))


# ============================================================
# Репозиторий статистики
# ============================================================
async def _clear_stats():
    """Таблица общая на сессию тестов — начинаем с чистого листа."""
    db = await get_db()
    await db.execute("DELETE FROM user_stats")
    await db.commit()


def test_stats_repo_counts():
    async def scenario():
        await init_db()
        try:
            await _clear_stats()
            await stats_repo.add_messages(900001, 3)
            await stats_repo.add_messages(900001, 2)
            await stats_repo.add_voice_seconds(900001, 90)
            await stats_repo.add_voice_seconds(900001, 30)
            data = await stats_repo.get_stats(900001)
            assert data == {"messages": 5, "voice_seconds": 120}, data
            # пустой участник
            assert await stats_repo.get_stats(900099) == {
                "messages": 0, "voice_seconds": 0}
            print("  ok: статистика складывается")
        finally:
            await close_db()

    asyncio.run(scenario())


def test_stats_repo_rank():
    async def scenario():
        await init_db()
        try:
            await _clear_stats()
            await stats_repo.add_messages(910001, 10)
            await stats_repo.add_messages(910002, 5)
            await stats_repo.add_messages(910003, 1)
            rank, total = await stats_repo.get_message_rank(910002)
            assert (rank, total) == (2, 3), (rank, total)
            rank1, _ = await stats_repo.get_message_rank(910001)
            assert rank1 == 1
            print("  ok: место в топе считается")
        finally:
            await close_db()

    asyncio.run(scenario())


# ============================================================
# /profile
# ============================================================
class _Avatar:
    url = "https://cdn.discordapp.com/avatars/1/a.png"


class _Member:
    id = 920001
    display_name = "egoist"
    name = "ddkkx"
    mention = "<@920001>"
    display_avatar = _Avatar()


class _Response:
    async def defer(self, ephemeral=False):
        return None


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return None


class _Interaction:
    def __init__(self):
        self.response = _Response()
        self.followup = _Followup()
        self.guild = SimpleNamespace(get_member=lambda _uid: _Member())
        self.user = SimpleNamespace(id=920001)


def _patch_profile(monkeypatch, stats, rank):
    async def fake_stats(_uid):
        return stats

    async def fake_rank(_uid):
        return rank

    async def fake_link(_uid):
        return "76561198000000001"

    async def fake_user(_sid):
        return {"id": 1, "roleId": 7}

    async def fake_full(_sid):
        return {"admins": [{"roleId": 5}], "vips": [{"group": "Спонсор"}]}

    async def fake_name(rid):
        return {7: "Спонсор", 5: "Админ"}.get(int(rid or 0))

    async def fake_top(_limit=5):
        return [("920001", 1012), ("920002", 500)]

    monkeypatch.setattr(botmod.stats_repo, "get_stats", fake_stats)
    monkeypatch.setattr(botmod.stats_repo, "get_message_rank", fake_rank)
    monkeypatch.setattr(botmod.links_repo, "get_link", fake_link)
    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)
    monkeypatch.setattr(botmod.api, "cs2red_full_user", fake_full)
    monkeypatch.setattr(botmod, "resolve_site_role_name", fake_name)
    monkeypatch.setattr(botmod.stats_repo, "top_by_messages", fake_top)


def test_profile_shows_voice_messages_roles(monkeypatch):
    """/profile показывает войс, сообщения, топ и роли с сайта."""
    _patch_profile(monkeypatch,
                   {"voice_seconds": 418 * 3600 + 48 * 60, "messages": 1012},
                   (5, 120))
    i = _Interaction()

    asyncio.run(botmod.profile.callback(i, None))

    assert i.followup.sent, "профиль не отправлен"
    layout = i.followup.sent[-1][1]["view"]
    text = _text_of(layout)
    assert "egoist" in text and "@ddkkx" in text
    assert "418 ч 48 м" in text, text
    assert "1 012" in text, text
    assert "#5 из 120" in text, text
    assert "Спонсор" in text and "Админ" in text, text
    assert "Мини-топ по сообщениям" in text, text
    assert "**#1**" in text, text
    print("  ok: профиль показывает войс, сообщения, топ и роли")


def test_profile_has_no_reputation_or_coins(monkeypatch):
    """Репутацию и койны в профиль не выводим."""
    _patch_profile(monkeypatch, {"voice_seconds": 0, "messages": 0}, (1, 1))
    i = _Interaction()

    asyncio.run(botmod.profile.callback(i, None))

    text = _text_of(i.followup.sent[-1][1]["view"]).lower()
    assert "репутац" not in text
    assert "койн" not in text
    print("  ok: без репутации и койнов")


def test_profile_without_link_has_no_site_roles(monkeypatch):
    """Без привязки SteamID роли сайта не выдумываются."""
    _patch_profile(monkeypatch, {"voice_seconds": 60, "messages": 2}, (1, 1))

    async def no_link(_uid):
        return None

    monkeypatch.setattr(botmod.links_repo, "get_link", no_link)
    i = _Interaction()

    asyncio.run(botmod.profile.callback(i, None))

    text = _text_of(i.followup.sent[-1][1]["view"])
    assert "Роли на сайте" in text
    assert "Спонсор" not in text
    print("  ok: без привязки роли сайта пустые")


# ============================================================
# Сбор статистики событиями
# ============================================================
def test_message_event_counts_and_ignores_bots(monkeypatch):
    counted = []

    async def fake_add(uid, count=1):
        counted.append(uid)

    async def fake_process(_message):
        return None

    monkeypatch.setattr(botmod.stats_repo, "add_messages", fake_add)
    monkeypatch.setattr(botmod.bot, "process_commands", fake_process)

    human = SimpleNamespace(author=SimpleNamespace(id=1, bot=False),
                            guild=object(), content="привет")
    asyncio.run(botmod.on_message(human))
    assert counted == [1]

    asyncio.run(botmod.on_message(SimpleNamespace(
        author=SimpleNamespace(id=2, bot=True), guild=object(), content="x")))
    asyncio.run(botmod.on_message(SimpleNamespace(
        author=SimpleNamespace(id=3, bot=False), guild=None, content="x")))
    asyncio.run(botmod.on_message(SimpleNamespace(
        author=SimpleNamespace(id=4, bot=False), guild=object(), content="!link 1")))
    assert counted == [1], "боты, ЛС и команды не считаются"
    print("  ok: сообщения считаются, команды и боты — нет")


def test_voice_event_accumulates(monkeypatch):
    added = []

    async def fake_add(uid, seconds):
        added.append((uid, seconds))

    monkeypatch.setattr(botmod.stats_repo, "add_voice_seconds", fake_add)
    botmod._VOICE_SESSIONS.clear()

    member = SimpleNamespace(id=77, bot=False)
    empty = SimpleNamespace(channel=None)
    voice = SimpleNamespace(channel=object())

    asyncio.run(botmod.on_voice_state_update(member, empty, voice))
    assert 77 in botmod._VOICE_SESSIONS

    botmod._VOICE_SESSIONS[77] -= 125  # будто просидел 125 секунд
    asyncio.run(botmod.on_voice_state_update(member, voice, empty))

    assert 77 not in botmod._VOICE_SESSIONS
    assert added and added[0][0] == 77 and added[0][1] >= 125
    print("  ok: войс копится и сохраняется при выходе")


def test_profile_zero_messages_has_no_rank(monkeypatch):
    """Без сообщений место в топе не показываем — прочерк, а не «#1 из 1»."""
    _patch_profile(monkeypatch, {"voice_seconds": 0, "messages": 0}, (1, 1))
    i = _Interaction()

    asyncio.run(botmod.profile.callback(i, None))

    text = _text_of(i.followup.sent[-1][1]["view"])
    assert "**Топ по сообщениям** — —" in text, text
    assert "#1 из" not in text
    print("  ok: без сообщений места в топе нет")


def test_profile_handles_plain_admin_and_vip_values(monkeypatch):
    """API может вернуть admins/vips как id или строки — не падаем."""
    _patch_profile(monkeypatch,
                   {"voice_seconds": 60, "messages": 3}, (1, 2))

    async def full(_sid):
        return {"admins": [5], "vips": ["Спонсор"]}

    monkeypatch.setattr(botmod.api, "cs2red_full_user", full)
    i = _Interaction()

    asyncio.run(botmod.profile.callback(i, None))

    text = _text_of(i.followup.sent[-1][1]["view"])
    assert "Админ" in text and "Спонсор" in text, text
    print("  ok: простые admins/vips не ломают профиль")
