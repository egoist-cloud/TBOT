# -*- coding: utf-8 -*-
"""Асинхронный кэш ответов ИИ в SQLite (таблица ai_cache)."""
from __future__ import annotations

import hashlib
import logging
import os
import time

from ..db import get_db

log = logging.getLogger("quantum.ai")


def _ttl_seconds() -> int:
    try:
        return max(1, int(os.getenv("AI_CACHE_TTL_SECONDS", "86400")))
    except (TypeError, ValueError):
        return 86400


async def setup_cache_cleanup() -> None:
    """Удаляет просроченные записи (ленивая очистка, безопасно при старте)."""
    try:
        db = await get_db()
        await db.execute("DELETE FROM ai_cache WHERE expires_at <= ?",
                         (int(time.time()),))
        await db.commit()
    except Exception as e:
        log.warning("cache cleanup: %s", e)


def _hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()


async def get_cached(query: str) -> str | None:
    """Возвращает закэшированный ответ, если он не истёк."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT response FROM ai_cache WHERE query_hash = ? AND expires_at > ?",
        (_hash(query), int(time.time())),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row["response"] if row is not None else None


async def set_cached(query: str, response: str) -> None:
    """Сохраняет ответ с TTL из AI_CACHE_TTL_SECONDS."""
    db = await get_db()
    await db.execute(
        "INSERT INTO ai_cache (query_hash, response, expires_at) VALUES (?, ?, ?) "
        "ON CONFLICT(query_hash) DO UPDATE SET "
        "response = excluded.response, expires_at = excluded.expires_at",
        (_hash(query), response, int(time.time()) + _ttl_seconds()),
    )
    await db.commit()


async def clear_cache() -> int:
    """Очищает весь кэш. Возвращает число удалённых строк."""
    db = await get_db()
    cursor = await db.execute("DELETE FROM ai_cache")
    await db.commit()
    return cursor.rowcount


async def count_cache() -> int:
    """Возвращает число записей в кэше (для /health)."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM ai_cache")
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0