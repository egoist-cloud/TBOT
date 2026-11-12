# -*- coding: utf-8 -*-
"""Первый старт в Docker: перенос JSON-баз из SEED_DIR в DATA_DIR.

Проверяется ровно то, что делает entrypoint контейнера:
`python -m quantum.import_seed` копирует links.json/players_db.json/
moderators.json в /data и импортирует их в SQLite.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quantum.import_seed as seedmod  # noqa: E402
from quantum.db import DATA_DIR, close_db, get_db, init_db  # noqa: E402


def _clean(*ids) -> None:
    """Убирает маркер, файлы и строки, чтобы тесты не влияли друг на друга."""
    seedmod.marker_path().unlink(missing_ok=True)
    for name in ("links.json", "links.json.bak",
                 "moderators.json", "moderators.json.bak",
                 "players_db.json", "players_db.json.bak"):
        (DATA_DIR / name).unlink(missing_ok=True)

    async def wipe():
        await init_db()
        db = await get_db()
        for uid in ids:
            await db.execute("DELETE FROM links WHERE discord_id = ?", (str(uid),))
            await db.execute("DELETE FROM moderators WHERE discord_id = ?", (str(uid),))
        await db.commit()
        await close_db()

    asyncio.run(wipe())


async def _fetch(discord_id, moderator_id=None):
    db = await get_db()
    cur = await db.execute(
        "SELECT steam_id FROM links WHERE discord_id = ?", (str(discord_id),))
    link = await cur.fetchone()
    await cur.close()
    mod_id = discord_id if moderator_id is None else moderator_id
    cur = await db.execute(
        "SELECT 1 AS found FROM moderators WHERE discord_id = ?", (str(mod_id),))
    mod = await cur.fetchone()
    await cur.close()
    await close_db()
    return link, mod


def test_files_are_copied_and_imported(tmp_path, monkeypatch):
    """Файлы из SEED_DIR попадают в SQLite, а в /data остаётся .bak."""
    _clean(7101, 7102)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "links.json").write_text(
        json.dumps({"7101": "76561198000000001"}), encoding="utf-8")
    (seed / "moderators.json").write_text(json.dumps([7102]), encoding="utf-8")
    monkeypatch.setenv("SEED_DIR", str(seed))

    assert seedmod.main() == 0

    assert seedmod.marker_path().is_file(), "маркер переноса не создан"
    assert not (DATA_DIR / "links.json").exists(), "исходник не переименован"
    assert (DATA_DIR / "links.json.bak").is_file()

    link, mod = asyncio.run(_fetch(7101, 7102))
    assert link is not None and link["steam_id"] == "76561198000000001"
    assert mod is not None, "модератор из moderators.json не импортирован"


def test_existing_file_is_not_overwritten(tmp_path, monkeypatch):
    """Файл, уже лежащий в DATA_DIR, не затирается копией из seed."""
    _clean(7103)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "links.json").write_text(
        json.dumps({"7103": "76561198000000003"}), encoding="utf-8")
    (DATA_DIR / "links.json").write_text(
        json.dumps({"7103": "76561198000000099"}), encoding="utf-8")
    monkeypatch.setenv("SEED_DIR", str(seed))

    assert seedmod.main() == 0

    # Скопировать нечего — импорт не запускался, маркер не ставился.
    assert not seedmod.marker_path().exists()
    assert json.loads((DATA_DIR / "links.json").read_text(encoding="utf-8")) == {
        "7103": "76561198000000099"}


def test_marker_prevents_second_import(tmp_path, monkeypatch):
    """Повторный старт контейнера не импортирует те же файлы заново."""
    _clean(7104)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "links.json").write_text(
        json.dumps({"7104": "76561198000000004"}), encoding="utf-8")
    monkeypatch.setenv("SEED_DIR", str(seed))

    assert seedmod.main() == 0

    # Имитируем ручное удаление привязки: повторный импорт не должен её вернуть.
    async def drop():
        db = await get_db()
        await db.execute("DELETE FROM links WHERE discord_id = ?", ("7104",))
        await db.commit()
        await close_db()

    asyncio.run(drop())

    assert seedmod.main() == 0
    link, _mod = asyncio.run(_fetch(7104))
    assert link is None, "маркер не сработал: запись импортирована повторно"


def test_missing_seed_dir_is_noop(tmp_path, monkeypatch):
    """Если /seed не смонтирован, контейнер всё равно стартует."""
    _clean()
    monkeypatch.setenv("SEED_DIR", str(tmp_path / "absent"))

    assert seedmod.main() == 0
    assert not seedmod.marker_path().exists()
