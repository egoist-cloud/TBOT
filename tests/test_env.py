# -*- coding: utf-8 -*-
"""Тесты запуска при пустых переменных окружения (как на Render).

На Render переменные часто созданы, но пустые. Бот не должен падать на
импорте из-за `int("")`.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Числовые переменные, которые бот читает при старте.
NUMERIC_VARS = [
    "GUILD_ID", "AGENT_CHANNEL_ID", "ALLOWED_ROLE_ID", "REVIEWER_ROLE_ID_2",
    "SPAM_VOTE_ROLE_ID", "EXTRA_REVIEWER_ROLE_1", "EXTRA_REVIEWER_ROLE_2",
    "EXTRA_REVIEWER_ROLE_3", "AUTO_ROLE_ID", "BYPASS_ROLE_ID",
    "NO_COOLDOWN_ROLE_ID", "ACTIVITY_ROLE_ID", "VOTES_REQUIRED",
    "SYNC_INTERVAL_MINUTES", "AGENT_CHECK_MINUTES",
    "AI_CACHE_TTL_SECONDS", "AI_USER_REQUESTS_PER_HOUR", "AI_MAX_OUTPUT_TOKENS",
    "CHECK_LOG_CHANNEL_ID",
]


def test_import_with_empty_env_vars():
    """Импорт script.py с пустыми числовыми переменными не падает."""
    env = dict(os.environ)
    for name in NUMERIC_VARS:
        env[name] = ""            # переменная есть, но пустая
    env["CLAN_NAME"] = ""        # строка тоже может быть пустой
    env["AI_REQUEST_TIMEOUT"] = ""

    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import script as s; "
        "print('GUILD_ID=', s.GUILD_ID); "
        "print('CLAN_NAME=', s.CLAN_NAME); "
        "print('VOTES_REQUIRED=', s.VOTES_REQUIRED); "
        "print('AGENT_CHANNEL_ID=', s.AGENT_CHANNEL_ID); "
        "print('COMMANDS=', len(list(s.bot.tree.get_commands())))" % str(ROOT)
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, timeout=120, cwd=str(ROOT))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"бот не стартует с пустыми переменными:\n{out}"
    assert "GUILD_ID= 0" in out, out
    assert "CLAN_NAME= TALENT" in out, out
    assert "VOTES_REQUIRED= 2" in out, out
    commands_line = [ln for ln in out.splitlines() if ln.startswith("COMMANDS=")]
    assert commands_line, out
    # Точное число меняется с каждой новой командой — важен сам факт,
    # что дерево команд собралось (и не уменьшилось).
    assert int(commands_line[0].split("=", 1)[1]) >= 28, out
    print("  ok: бот стартует при пустых переменных окружения")


def test_env_helpers():
    """env_int/env_float не бросают исключений на мусорных значениях."""
    from quantum.env import env_float, env_int, env_str

    os.environ["TBOT_TEST_INT"] = ""
    assert env_int("TBOT_TEST_INT", 5) == 5
    os.environ["TBOT_TEST_INT"] = "abc"
    assert env_int("TBOT_TEST_INT", 5) == 5
    os.environ["TBOT_TEST_INT"] = "42"
    assert env_int("TBOT_TEST_INT", 5) == 42

    os.environ["TBOT_TEST_FLOAT"] = ""
    assert env_float("TBOT_TEST_FLOAT", 1.5) == 1.5
    os.environ["TBOT_TEST_FLOAT"] = "3.5"
    assert env_float("TBOT_TEST_FLOAT", 1.5) == 3.5

    os.environ["TBOT_TEST_STR"] = "  "
    assert env_str("TBOT_TEST_STR", "fallback") == "fallback"
    os.environ["TBOT_TEST_STR"] = " value "
    assert env_str("TBOT_TEST_STR", "fallback") == "value"
    print("  ok: безопасные хелперы окружения")
