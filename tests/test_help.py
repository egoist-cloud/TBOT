# -*- coding: utf-8 -*-
"""Тесты /help: разделы по правам, все команды на месте, лимиты Embed."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402


def _embed_text(embed) -> str:
    data = embed.to_dict()
    parts = [str(data.get("title", "")), str(data.get("description", ""))]
    for field in data.get("fields", []):
        parts.append(str(field.get("name", "")))
        parts.append(str(field.get("value", "")))
    return "\n".join(parts)


class _Response:
    def __init__(self):
        self.kwargs = None

    async def send_message(self, *args, **kwargs):
        self.kwargs = kwargs


class _Interaction:
    def __init__(self):
        self.response = _Response()


def test_help_lists_every_command_once():
    """/help не теряет и не дублирует ни одной слэш-команды."""
    text = _embed_text(botmod._build_help_embed())
    for cmd in botmod.bot.tree.get_commands():
        token = f"`/{cmd.name}`"
        assert token in text, f"нет команды {cmd.name}"
        assert text.count(token) == 1, f"дубликат {cmd.name}"
    assert "`!setup_clan`" in text
    print("  ok: все команды перечислены ровно один раз")


def test_help_sections_match_access():
    """Разделы соответствуют реальным правам: игрок / модератор / админ."""
    lines = botmod._help_lines()

    def has(section, name):
        return any(line.startswith(f"`/{name}`") for line in lines[section])

    # Игроку: публичные команды
    for name in ("link", "unlink", "rules", "ask", "ping",
                 "health", "help", "search"):
        assert has("player", name), f"{name} должна быть у игрока"
    # PII-команды доступны не игроку: чужие SteamID и роли на сайте.
    for name in ("lookup", "profile"):
        assert not has("player", name), f"{name} не должна быть у игрока"
    # Модератору: проверки и заявки
    for name in ("check", "status", "check_bans", "check_clan",
                 "lookup", "profile"):
        assert has("mod", name), f"{name} должна быть у модератора"
    # Админу: служебные
    for name in ("panel", "sync_all", "auto_link", "clan_roleclear", "give", "audit"):
        assert has("admin", name), f"{name} должна быть у админа"
    print("  ok: разделы совпадают с правами команд")


def test_help_embed_within_limits():
    """Поля Embed не превышают лимиты Discord (1024 символа, 25 полей)."""
    embed = botmod._build_help_embed()
    assert len(embed.fields) <= 25, len(embed.fields)
    for field in embed.fields:
        assert len(field.value) <= 1024, (field.name, len(field.value))
        assert field.value.strip()
    print("  ok: Embed укладывается в лимиты Discord")


def test_help_command_answers_ephemerally():
    """/help отвечает эфемерно и содержит Embed."""
    i = _Interaction()
    asyncio.run(botmod.help_command.callback(i))

    assert i.response.kwargs is not None
    assert i.response.kwargs.get("ephemeral") is True
    assert i.response.kwargs.get("embed") is not None
    print("  ok: /help отвечает эфемерно")
