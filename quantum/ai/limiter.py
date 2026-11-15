# -*- coding: utf-8 -*-
"""Ограничитель частоты запросов к ИИ (in-memory, per-user).

Пустой словарь на время жизни процесса: счётчики не переживают рестарт,
что для лимита «X запросов в час» допустимо — после рестарта отсчёт
начинается заново.

Потокобезопасность не требуется: все обращения приходят из одного
event loop, поэтому просто ведём список timestamps на пользователя.
"""
from __future__ import annotations

import logging
import os
import time
import threading

log = logging.getLogger("quantum.ai")


class RateLimiter:
    def __init__(self, max_per_hour: int | None = None):
        try:
            default = int(os.getenv("AI_USER_REQUESTS_PER_HOUR", "20"))
        except (TypeError, ValueError):
            default = 20
        self.max_per_hour = default if max_per_hour is None else max_per_hour
        self._data: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, user_id) -> bool:
        """Пропускает ли запрос пользователя? Учитывает текущий, иначе True."""
        now = time.time()
        key = str(user_id)
        with self._lock:
            stamps = self._data.get(key, [])
            # оставляем только события в пределах часа
            deadline = now - 3600.0
            stamps = [t for t in stamps if t > deadline]
            if len(stamps) >= self.max_per_hour:
                self._data[key] = stamps
                return False
            stamps.append(now)
            self._data[key] = stamps
            return True

    def remaining(self, user_id) -> int:
        """Сколько запросов осталось пользователю в текущем часе."""
        now = time.time()
        key = str(user_id)
        deadline = now - 3600.0
        with self._lock:
            stamps = [t for t in self._data.get(key, []) if t > deadline]
        return max(0, self.max_per_hour - len(stamps))

    def reset(self, user_id) -> None:
        key = str(user_id)
        with self._lock:
            self._data.pop(key, None)

    def release_last(self, user_id) -> None:
        """Возвращает один запрос: он не дошёл до ИИ (все провайдеры упали)."""
        key = str(user_id)
        with self._lock:
            stamps = self._data.get(key) or []
            if stamps:
                stamps.pop()
            self._data[key] = stamps