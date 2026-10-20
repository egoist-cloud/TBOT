"""Асинхронный доступ к SQLite через aiosqlite. WAL включён."""
import os
from pathlib import Path

import aiosqlite

# База кладётся туда же, куда бот складывает JSON (DATA_DIR, по умолчанию ".").
DATA_DIR = Path(os.getenv("DATA_DIR", ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "quantum.db"

_db = None


async def get_db() -> aiosqlite.Connection:
    """Возвращает единственное (синглтон) соединение с БД.

    Создаёт его лениво при первом обращении и включает WAL.
    """
    global _db
    if _db is None:
        _db = await aiosqlite.connect(str(DB_PATH))
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL;")
        await _db.execute("PRAGMA foreign_keys=ON;")
        await _db.execute("PRAGMA busy_timeout=5000;")
        await _db.commit()
    return _db


async def close_db() -> None:
    """Закрывает соединение (для корректного выхода из бота)."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    db = await get_db()
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS links (
            discord_id TEXT PRIMARY KEY,
            steam_id   TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS players (
            steamid64 TEXT PRIMARY KEY,
            data      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            discord_id TEXT PRIMARY KEY,
            data      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS moderators (
            discord_id TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS votes (
            message_id TEXT PRIMARY KEY,
            accept     INTEGER NOT NULL DEFAULT 0,
            deny       INTEGER NOT NULL DEFAULT 0,
            voters     TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS seen (
            nickname TEXT PRIMARY KEY,
            activity INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS ai_cache (
            query_hash TEXT PRIMARY KEY,
            response   TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ai_cache_expires ON ai_cache (expires_at);
        """
    )
    await db.commit()