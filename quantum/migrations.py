"""Перенос данных из JSON-файлов в SQLite."""
import json
import os
from pathlib import Path

from .db import get_db

# Ключ в seen.json — служебный, в БД не переносится.
SEEN_IGNORE_KEYS = {"_seeded"}


def _read_json(path: Path):
    if not path.is_file():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _rename_to_bak(data_dir, filename: str) -> bool:
    """Переименовывает JSON в .bak. Возвращает True, если файл был переименован."""
    src = Path(data_dir) / filename
    if not src.is_file():
        return False
    dst = src.with_name(filename + ".bak")
    try:
        os.replace(str(src), str(dst))
    except OSError as e:
        print(f"[migrations] {filename}: не удалось переименовать в .bak: {e}")
        return False
    print(f"[migrations] {filename} -> {filename}.bak")
    return True


async def import_votes_json(data_dir) -> int:
    """Переносит votes.json -> таблица votes. Возвращает число строк."""
    data = _read_json(Path(data_dir) / "votes.json")
    if data is None:
        print("[migrations] votes.json: файл не найден, пропуск")
        return 0
    if not isinstance(data, dict):
        print("[migrations] votes.json: неожиданный формат, пропуск")
        return 0

    db = await get_db()
    count = 0
    for message_id, st in data.items():
        if not isinstance(st, dict):
            continue
        voters = st.get("voters") or []
        await db.execute(
            "INSERT OR IGNORE INTO votes (message_id, accept, deny, voters) "
            "VALUES (?, ?, ?, ?)",
            (
                str(message_id),
                int(st.get("accept", 0) or 0),
                int(st.get("deny", 0) or 0),
                json.dumps([str(v) for v in voters], ensure_ascii=False),
            ),
        )
        count += 1
    await db.commit()
    print(f"[migrations] votes.json: перенесено {count} записей")
    if count:
        _rename_to_bak(data_dir, "votes.json")
    return count


async def import_seen_json(data_dir) -> int:
    """Переносит seen.json -> таблица seen, игнорируя _seeded. Возвращает число строк."""
    data = _read_json(Path(data_dir) / "seen.json")
    if data is None:
        print("[migrations] seen.json: файл не найден, пропуск")
        return 0
    if not isinstance(data, dict):
        print("[migrations] seen.json: неожиданный формат, пропуск")
        return 0

    db = await get_db()
    count = 0
    for nickname, activity in data.items():
        if nickname in SEEN_IGNORE_KEYS:
            continue
        try:
            activity = int(activity)
        except (TypeError, ValueError):
            continue
        await db.execute(
            "INSERT OR IGNORE INTO seen (nickname, activity) VALUES (?, ?)",
            (str(nickname), activity),
        )
        count += 1
    await db.commit()
    print(f"[migrations] seen.json: перенесено {count} записей")
    if count:
        _rename_to_bak(data_dir, "seen.json")
    return count


async def import_links_json(data_dir) -> int:
    """Переносит links.json -> таблица links. Возвращает число строк."""
    data = _read_json(Path(data_dir) / "links.json")
    if data is None:
        print("[migrations] links.json: файл не найден, пропуск")
        return 0
    if not isinstance(data, dict):
        print("[migrations] links.json: неожиданный формат, пропуск")
        return 0

    db = await get_db()
    count = 0
    for discord_id, steam_id in data.items():
        await db.execute(
            "INSERT OR IGNORE INTO links (discord_id, steam_id) VALUES (?, ?)",
            (str(discord_id), str(steam_id)),
        )
        count += 1
    await db.commit()
    print(f"[migrations] links.json: перенесено {count} записей")
    if count:
        _rename_to_bak(data_dir, "links.json")
    return count


async def import_players_json(data_dir) -> int:
    """Переносит players_db.json -> таблица players (данные как JSON-строка)."""
    data = _read_json(Path(data_dir) / "players_db.json")
    if data is None:
        print("[migrations] players_db.json: файл не найден, пропуск")
        return 0
    if not isinstance(data, dict):
        print("[migrations] players_db.json: неожиданный формат, пропуск")
        return 0

    db = await get_db()
    count = 0
    for steam_id, obj in data.items():
        await db.execute(
            "INSERT OR IGNORE INTO players (steamid64, data) VALUES (?, ?)",
            (str(steam_id), json.dumps(obj, ensure_ascii=False, default=str)),
        )
        count += 1
    await db.commit()
    print(f"[migrations] players_db.json: перенесено {count} записей")
    if count:
        _rename_to_bak(data_dir, "players_db.json")
    return count


async def import_users_json(data_dir) -> int:
    """Переносит users_db.json -> таблица users (данные как JSON-строка)."""
    data = _read_json(Path(data_dir) / "users_db.json")
    if data is None:
        print("[migrations] users_db.json: файл не найден, пропуск")
        return 0
    if not isinstance(data, dict):
        print("[migrations] users_db.json: неожиданный формат, пропуск")
        return 0

    db = await get_db()
    count = 0
    for discord_id, obj in data.items():
        await db.execute(
            "INSERT OR IGNORE INTO users (discord_id, data) VALUES (?, ?)",
            (str(discord_id), json.dumps(obj, ensure_ascii=False, default=str)),
        )
        count += 1
    await db.commit()
    print(f"[migrations] users_db.json: перенесено {count} записей")
    if count:
        _rename_to_bak(data_dir, "users_db.json")
    return count


async def import_moderators_json(data_dir) -> int:
    """Переносит moderators.json -> таблица moderators. Возвращает число строк."""
    data = _read_json(Path(data_dir) / "moderators.json")
    if data is None:
        print("[migrations] moderators.json: файл не найден, пропуск")
        return 0
    # moderators.json — список discord_id
    if isinstance(data, dict):
        data = list(data.keys())
    elif not isinstance(data, list):
        print("[migrations] moderators.json: неожиданный формат, пропуск")
        return 0

    db = await get_db()
    count = 0
    for discord_id in data:
        await db.execute(
            "INSERT OR IGNORE INTO moderators (discord_id) VALUES (?)",
            (str(discord_id),),
        )
        count += 1
    await db.commit()
    print(f"[migrations] moderators.json: перенесено {count} записей")
    if count:
        _rename_to_bak(data_dir, "moderators.json")
    return count


async def import_json_to_sqlite(data_dir=".") -> None:
    """Запускает перенос всех поддерживаемых JSON в базу."""
    await import_votes_json(data_dir)
    await import_seen_json(data_dir)
    await import_links_json(data_dir)
    await import_players_json(data_dir)
    await import_users_json(data_dir)
    await import_moderators_json(data_dir)