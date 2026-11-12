# -*- coding: utf-8 -*-
"""Безопасное чтение переменных окружения.

На Render переменные часто остаются пустыми (пользователь создал, но не
заполнил). Обычный ``int(os.getenv("X", "0"))`` на таком значении падает
с ``ValueError`` и бот вообще не стартует. Здесь пустое или некорректное
значение не роняет процесс: берётся значение по умолчанию.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("quantum.env")


def env_int(name: str, default: int = 0) -> int:
    """Целое из окружения; пустое/битое значение -> ``default``."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        # Не число — берём дефолт, но говорим об этом в общий лог.
        log.warning("%s=%r — не число, беру %r", name, raw, default)
        return default


def env_float(name: str, default: float = 0.0) -> float:
    """Дробное из окружения; пустое/битое значение -> ``default``."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r — не число, беру %r", name, raw, default)
        return default


def env_str(name: str, default: str = "") -> str:
    """Строка из окружения; пустое значение -> ``default``."""
    raw = (os.getenv(name) or "").strip()
    return raw or default
