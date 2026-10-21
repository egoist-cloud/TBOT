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


async def _ensure_column(db, table: str, column: str, ddl: str) -> None:
    """Добавляет колонку в существующую таблицу, если её ещё нет.

    Нужна для миграции БД, созданных до появления колонок
    (accept_voters / deny_voters / applications.app_id).
    """
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    await cursor.close()
    if any(r["name"] == column for r in rows):
        return
    try:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        await db.commit()
        print(f"[db] миграция: {table}.{column} добавлена")
    except Exception as e:
        print(f"[db] миграция {table}.{column}: {e}")


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
            message_id    TEXT PRIMARY KEY,
            accept        INTEGER NOT NULL DEFAULT 0,
            deny          INTEGER NOT NULL DEFAULT 0,
            voters        TEXT NOT NULL DEFAULT '[]',
            accept_voters TEXT NOT NULL DEFAULT '[]',
            deny_voters   TEXT NOT NULL DEFAULT '[]'
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

        CREATE TABLE IF NOT EXISTS applications (
            user_id     TEXT PRIMARY KEY,
            status      TEXT NOT NULL DEFAULT 'pending',
            clan_name   TEXT,
            message_id  TEXT,
            channel_id  TEXT,
            app_id      TEXT,
            moderator_id TEXT,
            created_at  TEXT,
            updated_at  TEXT
        );

        -- История заявок: в applications хранится только последняя заявка
        -- игрока (user_id — PRIMARY KEY), а /search показывает все.
        CREATE TABLE IF NOT EXISTS application_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT NOT NULL,
            clan_name    TEXT,
            status       TEXT NOT NULL DEFAULT 'pending',
            message_id   TEXT,
            channel_id   TEXT,
            app_id       TEXT,
            moderator_id TEXT,
            created_at   TEXT,
            updated_at   TEXT,
            UNIQUE(user_id, app_id)
        );
        """
    )
    # Миграции для БД, созданных до этих колонок. Идут ДО создания
    # индексов: индекс по applications(app_id) на старой БД падал бы
    # с "no such column: app_id" и ломал бы запуск бота.
    await _ensure_column(db, "votes", "accept_voters", "TEXT NOT NULL DEFAULT '[]'")
    await _ensure_column(db, "votes", "deny_voters", "TEXT NOT NULL DEFAULT '[]'")
    await _ensure_column(db, "applications", "app_id", "TEXT")
    await _ensure_column(db, "applications", "moderator_id", "TEXT")
    # Индексы — только после миграций, когда колонки точно есть.
    await db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_cache_expires ON ai_cache (expires_at);
        CREATE INDEX IF NOT EXISTS idx_applications_app ON applications (app_id);
        CREATE INDEX IF NOT EXISTS idx_app_history_user ON application_history (user_id);
        """
    )
    # Переносим текущие заявки в историю (идемпотентно: UNIQUE(user_id, app_id)).
    # У заявок, созданных до появления app_id, подставляем стабильный
    # идентификатор 'legacy-<message_id>' — иначе повторный запуск добавлял бы
    # дубли (NULL в UNIQUE не сравнивается).
    try:
        await db.execute(
            "INSERT OR IGNORE INTO application_history "
            "(user_id, clan_name, status, message_id, channel_id, app_id, "
            "moderator_id, created_at, updated_at) "
            "SELECT user_id, clan_name, status, message_id, channel_id, "
            "COALESCE(NULLIF(app_id, ''), "
            "'legacy-' || COALESCE(NULLIF(message_id, ''), user_id)), "
            "moderator_id, created_at, updated_at FROM applications"
        )
        await db.commit()
    except Exception as e:
        print(f"[db] перенос заявок в историю: {e}")
    await db.commit()