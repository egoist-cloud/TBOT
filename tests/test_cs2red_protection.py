# -*- coding: utf-8 -*-
"""Тесты защиты cs2red и скорости: предохранитель, кэш, single-flight."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402

STEAM = "76561198000000001"


def test_block_and_reset():
    api = botmod.api
    api.reset_block()
    assert not api.is_blocked()
    assert api.block_reason_text() == ""

    api._block("429")
    assert api.is_blocked()
    assert "429" in api.block_reason_text()

    api.reset_block()
    assert not api.is_blocked()
    assert api.blocked_reason == ""
    print("  ok: предохранитель ставится и снимается")


def test_block_backoff_grows_and_caps():
    api = botmod.api
    old_base = botmod.CS2RED_BLOCK_BASE_SECONDS
    old_max = botmod.CS2RED_BLOCK_MAX_SECONDS
    try:
        botmod.CS2RED_BLOCK_BASE_SECONDS = 1
        botmod.CS2RED_BLOCK_MAX_SECONDS = 4
        api.reset_block()
        api._block("429")
        first = api._blocked_until - time.time()
        api._block("429")
        second = api._blocked_until - time.time()
        api._block("429")
        api._block("429")
        capped = api._blocked_until - time.time()
        assert 0 < first <= 1.2, first
        assert second > first, (first, second)
        assert capped <= 4.2, capped
    finally:
        botmod.CS2RED_BLOCK_BASE_SECONDS = old_base
        botmod.CS2RED_BLOCK_MAX_SECONDS = old_max
        api.reset_block()
    print("  ok: пауза растёт и не превышает максимум")


def test_get_json_short_circuits_when_blocked():
    async def scenario():
        api = botmod.api
        old_session = api.session
        try:
            api.session = object()  # чтобы session_for вернул не None
            api._block("429")
            started = time.time()
            result = await api._get_json(
                "https://cs2red.ru/api/admin/users")
            elapsed = time.time() - started
            assert result is None
            assert elapsed < 0.5, elapsed
        finally:
            api.session = old_session
            api.reset_block()

    asyncio.run(scenario())
    print("  ok: при паузе запрос не выполняется и не висит")


def test_user_cache_avoids_duplicate_requests():
    async def scenario():
        api = botmod.api
        calls = {"n": 0}
        original = api._get_json

        async def fake_get_json(url, params=None):
            calls["n"] += 1
            return {"success": True, "users": [{
                "id": 1, "nick": "test", "steamid64": STEAM}]}

        api._get_json = fake_get_json
        api._user_cache.clear()
        try:
            first = await api.cs2red_user(STEAM)
            second = await api.cs2red_user(STEAM)
            assert first and second and first["id"] == 1
            assert calls["n"] == 1, calls
        finally:
            api._get_json = original
            api._user_cache.clear()

    asyncio.run(scenario())
    print("  ok: повторный профиль берётся из кэша")


def test_cache_ttl_expires():
    async def scenario():
        api = botmod.api
        api._user_cache.clear()
        api._cache_put("ttl-key", {"id": 5})
        assert api._cache_get("ttl-key") == {"id": 5}
        api._user_cache["ttl-key"] = (time.time() - 9999, {"id": 5})
        assert api._cache_get("ttl-key") is None
        api._user_cache.clear()

    asyncio.run(scenario())
    print("  ok: просроченный кэш не отдаётся")


def test_single_flight_blocks_second_run():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        @botmod.single_flight("unit-flight", "⏳ уже выполняется")
        async def heavy(interaction):
            started.set()
            await release.wait()
            return "done"

        first_task = asyncio.create_task(heavy(object()))
        await started.wait()

        class _Resp:
            def is_done(self):
                return False

            async def defer(self, **kwargs):
                self.deferred = True

        sent = []

        class _Followup:
            async def send(self, *args, **kwargs):
                sent.append(kwargs.get("embed"))

        fake = type("I", (), {})()
        fake.response = _Resp()
        fake.followup = _Followup()

        busy = await heavy(fake)
        assert busy is None
        assert sent and "уже выполняется" in (sent[0].description or "")

        release.set()
        assert await first_task == "done"
        # После завершения lock свободен — следующий запуск проходит.
        release.set()
        assert await heavy(object()) == "done"

    asyncio.run(scenario())
    print("  ok: одна тяжёлая команда за раз")
