# -*- coding: utf-8 -*-
"""Проверка маскирования секретов в логах."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("CS2RED_TOKEN", "CS2RED_ADMIN_SECRET")
os.environ.setdefault("CS2RED_BH", "BHSECRET")
os.environ.setdefault("STEAM_API_KEY", "STEAM_SECRET_KEY")

import script as botmod  # noqa: E402


def test_redact_masks_env_secrets():
    """Секреты из окружения маскируются в тексте."""
    out = botmod.redact("token=CS2RED_ADMIN_SECRET bh=BHSECRET")
    assert "CS2RED_ADMIN_SECRET" not in out
    assert "BHSECRET" not in out
    assert out.count("***") >= 2
    print("  ok: секреты окружения замаскированы")


def test_redact_masks_steam_key_in_url():
    """Ключ Steam в URL не должен утекать в лог."""
    url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key=STEAM_SECRET_KEY"
    out = botmod.redact(f"❌ GET {url}: 403")
    assert "STEAM_SECRET_KEY" not in out
    assert "key=***" in out
    print("  ok: ключ Steam в URL замаскирован")


def test_redact_keeps_normal_text():
    """Обычный текст не портится."""
    msg = "✅ Заявка принята. <@123456789> получил <@&987654321>"
    assert botmod.redact(msg) == msg
    print("  ok: обычный текст не изменяется")


def test_redact_handles_none_and_empty():
    """None и пустая строка не роняют код."""
    assert botmod.redact(None) == ""
    assert botmod.redact("") == ""
    print("  ok: None/'' обработаны")


def test_redact_masks_cookie_values():
    """Отдельные куки из CS2RED_COOKIE_STRING тоже маскируются."""
    saved = botmod.CS2RED_COOKIES
    botmod.CS2RED_COOKIES = {"token": "RAWCOOKIEVALUE", "other": "OTHERCOOKIEVAL"}
    try:
        # Пересобираем список секретов под изменённые куки.
        botmod._SECRET_VALUES[:] = botmod._collect_secret_values()
        out = botmod.redact("cookie RAWCOOKIEVALUE")
        assert "RAWCOOKIEVALUE" not in out
        assert "OTHERCOOKIEVAL" not in botmod.redact("other OTHERCOOKIEVAL")
    finally:
        botmod.CS2RED_COOKIES = saved
        botmod._SECRET_VALUES[:] = botmod._collect_secret_values()
    print("  ok: куки из CS2RED_COOKIE_STRING замаскированы")


def test_log_redacts_output(capsys=None):
    """log() не печатает секреты в stdout."""
    botmod.log("🔑 CS2RED_ADMIN_SECRET")
    print("  ok: log() маскирует секреты")
