# -*- coding: utf-8 -*-
"""Единое безопасное форматирование текста.

Сначала вызывается redactor из script.py (он знает все секреты, включая
куки cs2red), и только потом результат обрезается. Обратный порядок
опасен: обрезка может разрезать ключ, и маскирование уже не найдёт его.
"""
from __future__ import annotations

from typing import Callable

def _redact(text: str) -> str:
    """Заглушка до подключения redactor из script.py."""
    return text


def configure_redactor(redactor: Callable[[str], str]) -> None:
    """Подключает общий redactor из script.py."""
    global _redact
    _redact = redactor


def safe_text(text, limit: int | None = None) -> str:
    """Маскирует секреты во всём тексте и только затем обрезает его."""
    value = "" if text is None else str(text)
    value = _redact(value)
    return value if limit is None else value[:limit]
