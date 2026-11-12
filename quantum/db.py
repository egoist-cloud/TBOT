"""Асинхронный доступ к SQLite через aiosqlite. WAL включён."""
import asyncio
import logging
import os
from pathlib import Path

import aiosqlite

log = logging.getLogger("quantum.db")

# База кладётся туда же, куда бот складывает JSON (DATA_DIR, по умолчанию ".").
DATA_DIR = Path(os.getenv("DATA_DIR", ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "quantum.db"

_db = None

# Lock создаётся под текущий event loop. Тесты выполняют сценарии через
# asyncio.run (новый loop на каждый тест), а asyncio.Lock привязывается
# к первому loop и на втором падает «bound to a different event loop».
_db_lock = None
_db_lock_loop = None


def _lock_for_current_loop() -> asyncio.Lock:
    """Lock инициализации соединения, привязанный к работающему loop."""
    global _db_lock, _db_lock_loop
    loop = asyncio.get_running_loop()
    if _db_lock is None or _db_lock_loop is not loop:
        _db_lock = asyncio.Lock()
        _db_lock_loop = loop
    return _db_lock


async def get_db() -> aiosqlite.Connection:
    """Возвращает единственное (синглтон) соединение с БД.

    Создаёт его лениво при первом обращении и включает WAL. Двойная
    проверка под локом: без неё два одновременных первых вызова создают
    два соединения, и одно теряется (утечка + второй писатель WAL).
    """
    global _db
    if _db is None:
        async with _lock_for_current_loop():
            if _db is None:
                conn = await aiosqlite.connect(str(DB_PATH))
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA journal_mode=WAL;")
                await conn.execute("PRAGMA foreign_keys=ON;")
                await conn.execute("PRAGMA busy_timeout=5000;")
                await conn.commit()
                _db = conn
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
        log.info("миграция: %s.%s добавлена", table, column)
    except Exception as e:
        log.warning("миграция %s.%s: %s", table, column, e)


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
            discord_id TEXT PRIMARY KEY,
            commands   TEXT NOT NULL DEFAULT '["*"]'
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
            taken_by    TEXT,
            taken_at    TEXT,
            decision_reason TEXT,
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
            taken_by     TEXT,
            taken_at     TEXT,
            decision_reason TEXT,
            created_at   TEXT,
            updated_at   TEXT,
            UNIQUE(user_id, app_id)
        );

        -- Аудит админских и модераторских действий. Пишется всегда, а
        -- копия уходит в отдельный канал (AUDIT_LOG_CHANNEL_ID).
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            actor_id    TEXT,
            actor_name  TEXT,
            action      TEXT NOT NULL,
            target_id   TEXT,
            target_name TEXT,
            details     TEXT
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
    await _ensure_column(db, "applications", "taken_by", "TEXT")
    await _ensure_column(db, "applications", "taken_at", "TEXT")
    await _ensure_column(db, "application_history", "taken_by", "TEXT")
    await _ensure_column(db, "application_history", "taken_at", "TEXT")
    # Причина отказа обязательна начиная с этой версии. Колонка нужна и
    # в текущей заявке (её показывает /check), и в истории (/search).
    await _ensure_column(db, "applications", "decision_reason", "TEXT")
    await _ensure_column(db, "application_history", "decision_reason", "TEXT")
    # Права модераторов по командам. Старые записи получают полный доступ,
    # чтобы обновление бота не отобрало права у уже назначенных людей.
    await _ensure_column(db, "moderators", "commands",
                         """TEXT NOT NULL DEFAULT '["*"]'""")
    # Индексы — только после миграций, когда колонки точно есть.
    await db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_cache_expires ON ai_cache (expires_at);
        CREATE INDEX IF NOT EXISTS idx_applications_app ON applications (app_id);
        CREATE INDEX IF NOT EXISTS idx_app_history_user ON application_history (user_id);
        CREATE INDEX IF NOT EXISTS idx_app_history_taken ON application_history (taken_by);
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts);
        CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log (actor_id);
        CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log (target_id);
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
            "moderator_id, taken_by, taken_at, created_at, updated_at) "
            "SELECT user_id, clan_name, status, message_id, channel_id, "
            "COALESCE(NULLIF(app_id, ''), "
            "'legacy-' || COALESCE(NULLIF(message_id, ''), user_id)), "
            "moderator_id, taken_by, taken_at, created_at, updated_at "
            "FROM applications"
        )
        await db.commit()
    except Exception as e:
        log.warning("перенос заявок в историю: %s", e)
    await db.commit()
