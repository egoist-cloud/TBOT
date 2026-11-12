# -*- coding: utf-8 -*-
"""Безопасное чтение переменных окружения.

На Render переменные часто остаются пустыми (пользователь создал, но не
заполнил). Обычный ``int(os.getenv("X", "0"))`` на таком значении падает
с ``ValueError`` и бот вообще не стартует. Здесь пустое или некорректное
значение не роняет процесс: берётся значение по умолчанию.
"""
from __future__ import annotations

import os


def env_int(name: str, default: int = 0) -> int:
    """Целое из окружения; пустое/битое значение -> ``default``."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[env] {name}={raw!r} — не число, беру {default}", flush=True)
        return default


def env_float(name: str, default: float = 0.0) -> float:
    """Дробное из окружения; пустое/битое значение -> ``default``."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[env] {name}={raw!r} — не число, беру {default}", flush=True)
        return default


def env_str(name: str, default: str = "") -> str:
    """Строка из окружения; пустое значение -> ``default``."""
    raw = (os.getenv(name) or "").strip()
    return raw or default
