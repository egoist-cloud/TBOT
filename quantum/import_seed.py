# -*- coding: utf-8 -*-
"""Перенос JSON-баз в DATA_DIR (Docker volume) при первом старте контейнера.

До SQLite бот хранил links.json, players_db.json, moderators.json и т.п.
рядом с собой. В контейнере база живёт в /data, поэтому при первом запуске
файлы нужно один раз скопировать в volume — дальше
``import_json_to_sqlite()`` переносит их в quantum.db.

Запускается entrypoint'ом: ``python -m quantum.import_seed``.

  * источник — SEED_DIR (по умолчанию /seed, монтируется read-only);
  * приёмник — DATA_DIR (/data);
  * уже существующие в DATA_DIR файлы не перезаписываются;
  * повторный запуск пропускается по маркеру DATA_DIR/.seed_imported,
    чтобы рестарт не копировал и не импортировал одно и то же заново.

Принудительно повторить: ``FORCE_SEED_IMPORT=1`` (и удалить маркер, если
файлы уже переименованы в .bak внутри volume).
"""
from __future__ import annotations

import asyncio
import datetime
import os
import shutil
import sys
from pathlib import Path

from .db import DATA_DIR, close_db, init_db
from .migrations import import_json_to_sqlite

# Файлы, которые умеет импортировать quantum.migrations.
SEED_FILES = (
    "links.json",
    "players_db.json",
    "moderators.json",
    "users_db.json",
    "votes.json",
    "seen.json",
)

MARKER_NAME = ".seed_imported"


def marker_path() -> Path:
    """Маркер «перенос уже выполнялся» внутри DATA_DIR."""
    return DATA_DIR / MARKER_NAME


def copy_seed(seed_dir: Path) -> list:
    """Копирует недостающие файлы из seed_dir в DATA_DIR.

    Возвращает список скопированных имён. Существующие файлы не трогаем:
    в DATA_DIR может лежать более свежая копия (например, .bak после
    предыдущего импорта или файл, положенный вручную).
    """
    copied = []
    for name in SEED_FILES:
        src = Path(seed_dir) / name
        dst = DATA_DIR / name
        if not src.is_file():
            continue
        if dst.exists():
            print(f"[seed] {name}: уже есть в {DATA_DIR} — пропуск")
            continue
        shutil.copy2(src, dst)
        copied.append(name)
        print(f"[seed] {name}: скопирован в {DATA_DIR}")
    return copied


async def _import_db() -> None:
    """Создаёт таблицы (если нужно) и импортирует JSON в SQLite."""
    await init_db()
    try:
        await import_json_to_sqlite(DATA_DIR)
    finally:
        await close_db()


def main() -> int:
    """Точка входа entrypoint'а. 0 — успех/нечего делать, 1 — ошибка импорта."""
    seed_dir = Path(os.getenv("SEED_DIR", "/seed"))
    marker = marker_path()

    if marker.exists() and not os.getenv("FORCE_SEED_IMPORT"):
        print(f"[seed] {marker} уже есть — перенос выполнялся ранее")
        return 0

    if not seed_dir.is_dir():
        print(f"[seed] каталог {seed_dir} не смонтирован — переносить нечего")
        return 0

    copied = copy_seed(seed_dir)
    if not copied:
        print("[seed] новых файлов нет — импорт не требуется")
        return 0

    try:
        asyncio.run(_import_db())
    except Exception as e:
        # Контейнер не должен стартовать с полуперенесёнными данными:
        # пусть падение будет видно в логах, а не через пустые таблицы.
        print(f"[seed] ОШИБКА импорта: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    stamp = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    marker.write_text(f"{stamp}\n{', '.join(copied)}\n", encoding="utf-8")
    print(f"[seed] перенесено в SQLite: {', '.join(copied)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
