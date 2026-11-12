# -*- coding: utf-8 -*-
"""Тесты кэша ответов ИИ (quantum/ai/cache.py).

Кэш лежит в SQLite (таблица ai_cache), поэтому каждый сценарий поднимает
временную БД из conftest и закрывает её в finally.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantum.ai import cache as ai_cache  # noqa: E402
from quantum.db import close_db, get_db, init_db  # noqa: E402


def test_ttl_default_env_and_clamp(monkeypatch):
    """TTL: 86400 по умолчанию, из окружения, не меньше секунды, мусор -> дефолт."""
    monkeypatch.delenv("AI_CACHE_TTL_SECONDS", raising=False)
    assert ai_cache._ttl_seconds() == 86400

    monkeypatch.setenv("AI_CACHE_TTL_SECONDS", "60")
    assert ai_cache._ttl_seconds() == 60

    monkeypatch.setenv("AI_CACHE_TTL_SECONDS", "0")
    assert ai_cache._ttl_seconds() == 1

    monkeypatch.setenv("AI_CACHE_TTL_SECONDS", "-5")
    assert ai_cache._ttl_seconds() == 1

    monkeypatch.setenv("AI_CACHE_TTL_SECONDS", "abc")
    assert ai_cache._ttl_seconds() == 86400
    print("  ok: TTL разобран корректно")


def test_hash_is_stable_and_distinct():
    """Одинаковый запрос — одинаковый ключ, разный — разный."""
    first = ai_cache._hash("что за читы?")
    assert first == ai_cache._hash("что за читы?")
    assert first != ai_cache._hash("что за муты?")
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)
    print("  ok: хеш запроса стабилен")


def test_set_get_roundtrip_and_overwrite():
    """Сохранённый ответ читается, повторная запись перезаписывает его."""
    async def scenario():
        await init_db()
        try:
            assert await ai_cache.get_cached("вопрос про кэш") is None
            await ai_cache.set_cached("вопрос про кэш", "первый ответ")
            assert await ai_cache.get_cached("вопрос про кэш") == "первый ответ"

            await ai_cache.set_cached("вопрос про кэш", "второй ответ")
            assert await ai_cache.get_cached("вопрос про кэш") == "второй ответ"
            assert await ai_cache.count_cache() >= 1
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: кэш читается и перезаписывается")


def test_expired_entry_is_not_returned():
    """Просроченная запись не отдаётся, даже если физически лежит в таблице."""
    async def scenario():
        await init_db()
        try:
            await ai_cache.set_cached("протухший вопрос", "ответ")
            db = await get_db()
            await db.execute("UPDATE ai_cache SET expires_at = 1 WHERE query_hash = ?",
                             (ai_cache._hash("протухший вопрос"),))
            await db.commit()
            assert await ai_cache.get_cached("протухший вопрос") is None
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: просроченный ответ не возвращается")


def test_cleanup_removes_only_expired():
    """setup_cache_cleanup удаляет просроченные записи и не трогает свежие."""
    async def scenario():
        await init_db()
        try:
            await ai_cache.clear_cache()
            db = await get_db()
            await db.execute(
                "INSERT INTO ai_cache (query_hash, response, expires_at) VALUES (?, ?, ?)",
                ("old-hash", "старый", 1))
            await db.execute(
                "INSERT INTO ai_cache (query_hash, response, expires_at) VALUES (?, ?, ?)",
                ("fresh-hash", "свежий", int(time.time()) + 600))
            await db.commit()

            await ai_cache.setup_cache_cleanup()
            assert await ai_cache.count_cache() == 1
            assert await ai_cache.get_cached("нет такого") is None
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: очистка удаляет только просроченное")


def test_clear_cache_returns_rowcount():
    """clear_cache возвращает число удалённых строк."""
    async def scenario():
        await init_db()
        try:
            await ai_cache.clear_cache()
            await ai_cache.set_cached("а", "1")
            await ai_cache.set_cached("б", "2")
            assert await ai_cache.clear_cache() == 2
            assert await ai_cache.count_cache() == 0
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: clear_cache считает удалённые строки")


def test_cleanup_survives_db_error(monkeypatch):
    """Ошибка БД при очистке не роняет старт бота — только warning в лог."""
    class _Boom:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("db down")

    async def fake_get_db():
        return _Boom()

    async def scenario():
        monkeypatch.setattr(ai_cache, "get_db", fake_get_db)
        await ai_cache.setup_cache_cleanup()

    asyncio.run(scenario())
    print("  ok: ошибка очистки проглочена")
