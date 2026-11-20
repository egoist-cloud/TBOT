# -*- coding: utf-8 -*-
"""Тесты фоновых задач: авто-привязка и синхронизация ролей по расписанию."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return SimpleNamespace(id=1)


def _member(mid, bot=False):
    return SimpleNamespace(id=mid, bot=bot, display_name=f"user{mid}")


def test_intervals_are_three_hours_and_two_hours():
    """Авто-привязка — раз в 3 часа, sync_all — раз в 2 часа."""
    assert botmod.auto_link_watch.hours == 3
    assert botmod.sync_all_watch.minutes == 120
    print("  ok: расписания 3 ч и 2 ч")


def test_first_run_delays():
    """Синхронизация ролей ждёт полный интервал, авто-привязка — 2–5 мин."""
    assert botmod._sync_all_first_delay() == 120 * 60
    # Хостинг перезапускается чаще интервала, поэтому авто-привязка стартует
    # почти сразу: иначе обход непривязанных не успевает пройти ни разу.
    delay = botmod._auto_link_first_delay()
    assert 2 * 60 <= delay <= 5 * 60, delay
    print("  ok: задержки первого прогона")


def test_unlinked_members_skips_linked_and_bots():
    """Задача берёт только тех, кто ещё не привязан."""
    guild = SimpleNamespace(members=[
        _member(1), _member("2"), _member(3, bot=True),
    ])
    unlinked = botmod._unlinked_members(guild, {"1": "76561198000000001"})
    assert [str(m.id) for m in unlinked] == ["2"], unlinked
    print("  ok: привязанные и боты не попадают в авто-привязку")


def test_unlinked_members_all_linked_means_nothing_to_do():
    """Если привязаны все — список пуст, задача молча выходит."""
    guild = SimpleNamespace(members=[_member(10), _member(11)])
    assert botmod._unlinked_members(guild, {"10": "1", "11": "2"}) == []
    print("  ok: «уже сделано» — задача ничего не запускает")


def test_background_followup_strips_ephemeral():
    """Фоновый запуск пишет в лог-канал обычным сообщением."""
    channel = _FakeChannel()
    followup = botmod._BackgroundFollowup(channel)

    asyncio.run(followup.send("привет", embed=None, ephemeral=True))

    assert len(channel.sent) == 1
    _, kwargs = channel.sent[0]
    assert "ephemeral" not in kwargs, "ephemeral в фоне не используется"
    print("  ok: ephemeral убирается для фоновых сообщений")


def test_background_followup_without_channel():
    """Нет лог-канала — не падаем, прогресс просто некуда писать."""
    msg = asyncio.run(botmod._BackgroundFollowup(None).send("x", ephemeral=True))
    assert asyncio.run(msg.edit(embed=None)) is None
    print("  ok: без лог-канала задача не падает")


def test_background_context_has_required_interface():
    """Контекст даёт guild/client/user/followup/response для run_* функций."""
    guild = SimpleNamespace(members=[])
    ctx = botmod._BackgroundContext(guild, _FakeChannel(), "Синхронизация")

    assert ctx.guild is guild
    assert ctx.client is botmod.bot
    assert ctx.user.display_name == "Синхронизация"
    assert ctx.response.is_done() is True
    print("  ok: фоновый контекст совместим с run_auto_link/run_sync_all")
