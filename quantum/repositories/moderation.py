"""Репозиторий модераторов: таблица moderators.

Кроме членства в /moders храним список разрешённых команд:
``["*"]`` — все команды, пустой список — ни одной. Старые записи без
колонки команд получают полный доступ при миграции.
"""
import json

from ..db import get_db

ALL_COMMANDS = ["*"]


def _parse_commands(raw) -> list:
    """Разбирает JSON-список команд. Битые данные = полный доступ."""
    if raw is None:
        return list(ALL_COMMANDS)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return list(ALL_COMMANDS)
    if isinstance(data, str):
        data = [data]
    if not isinstance(data, list):
        return list(ALL_COMMANDS)
    return [str(item) for item in data]


def _dump_commands(commands) -> str:
    if commands is None:
        commands = ALL_COMMANDS
    return json.dumps([str(c) for c in commands], ensure_ascii=False)


async def get_moderator(discord_id) -> str | None:
    """Возвращает discord_id модератора или None."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT discord_id FROM moderators WHERE discord_id = ?",
        (str(discord_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row["discord_id"] if row is not None else None


async def is_moderator(discord_id) -> bool:
    """True, если discord_id есть в таблице модераторов."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT 1 FROM moderators WHERE discord_id = ?",
        (str(discord_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row is not None


async def get_moderator_commands(discord_id) -> list | None:
    """Возвращает список разрешённых команд или None, если не модератор."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT commands FROM moderators WHERE discord_id = ?",
        (str(discord_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return _parse_commands(row["commands"])


async def has_command_access(discord_id, command_name: str) -> bool:
    """True, если модератору разрешена конкретная команда."""
    commands = await get_moderator_commands(discord_id)
    if commands is None:
        return False
    return "*" in commands or str(command_name) in commands


async def set_moderator_commands(discord_id, commands) -> None:
    """Создаёт модератора или заменяет его список команд (upsert)."""
    db = await get_db()
    await db.execute(
        "INSERT INTO moderators (discord_id, commands) VALUES (?, ?) "
        "ON CONFLICT(discord_id) DO UPDATE SET commands = excluded.commands",
        (str(discord_id), _dump_commands(commands)),
    )
    await db.commit()


async def add_moderator(discord_id, commands=None) -> bool:
    """Добавляет модератора. Возвращает True, если добавлен (а не существовал)."""
    db = await get_db()
    cursor = await db.execute(
        "INSERT OR IGNORE INTO moderators (discord_id, commands) VALUES (?, ?)",
        (str(discord_id), _dump_commands(commands)),
    )
    await db.commit()
    return cursor.rowcount > 0


async def remove_moderator(discord_id) -> bool:
    """Удаляет модератора. Возвращает True, если был удалён."""
    db = await get_db()
    cursor = await db.execute(
        "DELETE FROM moderators WHERE discord_id = ?",
        (str(discord_id),),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_all_moderators() -> list:
    """Возвращает список discord_id всех модераторов."""
    db = await get_db()
    cursor = await db.execute("SELECT discord_id FROM moderators")
    rows = await cursor.fetchall()
    await cursor.close()
    return [row["discord_id"] for row in rows]


async def count_all() -> int:
    """Число модераторов (для /health)."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM moderators")
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0
