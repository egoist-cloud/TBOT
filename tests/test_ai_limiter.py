# -*- coding: utf-8 -*-
"""Тесты ограничителя частоты (quantum/ai/limiter.py).

Лимитер — чистая in-memory логика: окно в час на пользователя, без БД и
event loop. Поэтому проверяем границы окна и изоляцию пользователей.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantum.ai.limiter import RateLimiter  # noqa: E402


def test_default_limit_comes_from_env(monkeypatch):
    """Лимит по умолчанию читается из AI_USER_REQUESTS_PER_HOUR."""
    monkeypatch.setenv("AI_USER_REQUESTS_PER_HOUR", "3")
    assert RateLimiter().max_per_hour == 3
    print("  ok: лимит взят из окружения")


def test_explicit_limit_wins_over_env(monkeypatch):
    """Явный аргумент важнее переменной окружения."""
    monkeypatch.setenv("AI_USER_REQUESTS_PER_HOUR", "3")
    assert RateLimiter(max_per_hour=7).max_per_hour == 7
    print("  ok: явный лимит важнее окружения")


def test_invalid_env_falls_back_to_20(monkeypatch):
    """Пустое и нечисловое значение не роняют лимитер."""
    for bad in ("", "много", "3.5"):
        monkeypatch.setenv("AI_USER_REQUESTS_PER_HOUR", bad)
        assert RateLimiter().max_per_hour == 20, bad
    print("  ok: кривое окружение -> 20")


def test_allow_until_limit_then_deny():
    """Запросы пускаются ровно до лимита, дальше — отказ."""
    limiter = RateLimiter(max_per_hour=2)
    assert limiter.allow(1) is True
    assert limiter.remaining(1) == 1
    assert limiter.allow(1) is True
    assert limiter.remaining(1) == 0
    assert limiter.allow(1) is False
    # отказ не «съедает» остаток и не увеличивает счётчик
    assert limiter.remaining(1) == 0
    print("  ok: лимит срабатывает ровно на границе")


def test_users_are_isolated_and_ids_are_stringified():
    """int и str одного id — один пользователь, разные id не мешают друг другу."""
    limiter = RateLimiter(max_per_hour=1)
    assert limiter.allow(42) is True
    assert limiter.allow("42") is False
    assert limiter.allow(43) is True
    print("  ok: пользователи изолированы")


def test_old_events_fall_out_of_window():
    """События старше часа не считаются: квота возвращается."""
    limiter = RateLimiter(max_per_hour=2)
    limiter._data["7"] = [time.time() - 3700.0] * 5
    assert limiter.remaining(7) == 2
    assert limiter.allow(7) is True
    assert limiter.remaining(7) == 1
    print("  ok: окно в час очищается")


def test_reset_clears_user():
    """reset() возвращает полную квоту и не падает на неизвестном id."""
    limiter = RateLimiter(max_per_hour=1)
    limiter.allow("x")
    assert limiter.remaining("x") == 0
    limiter.reset("x")
    assert limiter.remaining("x") == 1
    limiter.reset("нет-такого")
    print("  ok: reset очищает счётчик")


def test_unknown_user_has_full_quota():
    """Незнакомый пользователь сразу имеет полную квоту."""
    assert RateLimiter(max_per_hour=5).remaining("unknown") == 5
    print("  ok: у нового пользователя полная квота")
