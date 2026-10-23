# -*- coding: utf-8 -*-
"""Тесты проверок блокировок: /check, /check_mutes, /check_bans.

Сеть не используется: методы API подменяются фейками.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402

STEAM = "76561198000000000"


def _mute_raw(**over):
    now = int(time.time())
    raw = {
        "id": 5,
        "reason": "Мат в чате",
        "timestamp": now - 600,
        "endTimeStamp": now + 3600,
        "adminId": 42,
        "user": {"id": 42, "nick": "lilcom"},
        "action": {"id": 7, "nick": "sllletron", "steamid64": STEAM},
    }
    raw.update(over)
    return raw


def test_parse_steam_id():
    assert botmod._parse_steam_id(STEAM) == STEAM
    assert botmod._parse_steam_id(f"  {STEAM}  ") == STEAM
    assert botmod._parse_steam_id("") is None
    assert botmod._parse_steam_id("12345") is None
    assert botmod._parse_steam_id("765611980000000000") is None  # 18 цифр
    assert botmod._parse_steam_id("aaaaaaaaaaaaaaaaa") is None


def test_fmt_block_ts():
    assert botmod._fmt_block_ts(0) == "—"
    assert botmod._fmt_block_ts(1700000000) == "14.11.2023 22:13"


def test_fmt_block_duration():
    now = int(time.time())
    assert botmod._fmt_block_duration(now, 0) == "навсегда"
    assert botmod._fmt_block_duration(now, now - 5) == "истёк"
    assert botmod._fmt_block_duration(now, now + 90 * 86400 + 2 * 3600) == "90 дн. 2 ч."
    assert botmod._fmt_block_duration(now, now + 3 * 3600) == "3 ч."
    assert botmod._fmt_block_duration(now, now + 120) == "2 мин."


def test_normalize_block_mute():
    block = botmod.API.normalize_block(_mute_raw(), "Мут")
    assert block["kind"] == "Мут"
    assert block["player_nick"] == "sllletron"
    assert block["steam_id"] == STEAM
    assert block["reason"] == "Мат в чате"
    assert block["admin"] == "lilcom"
    assert block["active"] is True
    assert block["start_ts"] > 0
    assert block["end_ts"] > block["start_ts"]


def test_normalize_block_autoban():
    now = int(time.time())
    raw = _mute_raw(
        reason="[RAC v4] Обнаружен чит",
        user=None,
        adminId=0,
        endTimeStamp=0,
        timestamp=now - 100,
    )
    block = botmod.API.normalize_block(raw, "Бан")
    assert block["kind"] == "Бан"
    assert block["admin"] == "Античит"
    assert block["end_ts"] == 0
    assert block["active"] is True


def test_normalize_block_expired():
    now = int(time.time())
    raw = _mute_raw(timestamp=now - 7200, endTimeStamp=now - 60)
    assert botmod.API.normalize_block(raw, "Мут")["active"] is False


def test_build_block_embeds_empty():
    embeds = botmod._build_block_embeds(
        [], "🔇 Муты игрока: test", "✅ У игрока нет активных мутов")
    assert len(embeds) == 1
    assert embeds[0].description == "✅ У игрока нет активных мутов"
    assert embeds[0].color.value == 0x000000
    assert "Quantum" in embeds[0].footer.text


def test_build_block_embeds_fields():
    blocks = [
        botmod.API.normalize_block(_mute_raw(), "Мут"),
        botmod.API.normalize_block(_mute_raw(reason="Реклама"), "Мут"),
    ]
    embeds = botmod._build_block_embeds(
        blocks, "🔇 Муты игрока: sllletron", "—")
    assert len(embeds) == 2
    names = [f.name for f in embeds[0].fields]
    assert names == ["Тип", "Игрок", "Причина", "Срок", "Дата блокировки", "Забанил"]
    assert embeds[0].fields[0].value == "Мут"
    assert "76561198000000000" in embeds[0].fields[1].value
    assert embeds[0].color.value == 0x000000
    assert embeds[1].fields[2].value == "Реклама"


def test_build_block_embeds_with_end_date():
    block = botmod.API.normalize_block(_mute_raw(), "Бан")
    embeds = botmod._build_block_embeds([block], "🚫 Блокировки", "—", show_end=True)
    names = [f.name for f in embeds[0].fields]
    assert "Дата разблокировки" in names
    end_value = embeds[0].fields[names.index("Дата разблокировки")].value
    assert end_value != "навсегда"
    assert "навсегда" not in end_value


def test_build_block_embeds_max_limit():
    blocks = [botmod.API.normalize_block(_mute_raw(), "Мут")] * 20
    embeds = botmod._build_block_embeds(blocks, "Т", "—")
    assert len(embeds) == botmod.BLOCK_MAX_EMBEDS


def test_check_commands_registered():
    names = {c.name for c in botmod.bot.tree.get_commands()}
    assert {"check", "check_mutes", "check_bans",
            "check_all", "check_clan"} <= names


def test_filter_blocks_since_keeps_recent():
    now = int(time.time())
    old = {"start_ts": now - 20 * 86400, "active": False}
    fresh = {"start_ts": now - 86400, "active": True}
    undated_active = {"start_ts": 0, "active": True}
    undated_inactive = {"start_ts": 0, "active": False}
    since = now - 14 * 86400
    kept = botmod._filter_blocks_since(
        [old, fresh, undated_active, undated_inactive], since)
    assert fresh in kept
    assert undated_active in kept   # без даты, но активен — показываем
    assert old not in kept
    assert undated_inactive not in kept


def test_check_all_days_default():
    # Отчёт по мутам должен охватывать 2 недели.
    assert botmod.CHECK_ALL_DAYS == 14
    assert botmod.TALENT_ROLE_ID == 1448368118190837800



def test_cs2red_blocks_from_admin_profile(monkeypatch):
    async def fake_user(_sid):
        return {"id": 7, "nick": "sllletron"}

    async def fake_full(_sid, user=None):
        return {"mutes": [_mute_raw()], "bans": []}

    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)
    monkeypatch.setattr(botmod.api, "cs2red_full_user", fake_full)

    blocks, err = asyncio.run(botmod.api.cs2red_blocks(STEAM, "MUTES"))
    assert err == ""
    assert len(blocks) == 1
    assert blocks[0]["kind"] == "Мут"
    assert blocks[0]["from_admin"] is True


def test_cs2red_blocks_public_filters_foreign(monkeypatch):
    async def fake_user(_sid):
        return {"id": 7}

    async def fake_full(_sid, user=None):
        return {}

    async def fake_json(_url, params=None):
        assert params["search"] == STEAM
        return {"bans": [
            _mute_raw(),
            _mute_raw(action={"id": 99, "nick": "other",
                              "steamid64": "76561198999999999"}),
        ]}

    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)
    monkeypatch.setattr(botmod.api, "cs2red_full_user", fake_full)
    monkeypatch.setattr(botmod.api, "_get_json", fake_json)

    blocks, err = asyncio.run(botmod.api.cs2red_blocks(STEAM, "MUTES"))
    assert err == ""
    assert len(blocks) == 1
    assert blocks[0]["steam_id"] == STEAM


def test_cs2red_blocks_unavailable(monkeypatch):
    async def fake_user(_sid):
        return None

    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)

    blocks, err = asyncio.run(botmod.api.cs2red_blocks(STEAM, "BANS"))
    assert blocks == []
    assert err == "unavailable"


def test_cs2red_blocks_not_found(monkeypatch):
    async def fake_user(_sid):
        return {"_not_found": True}

    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)

    blocks, err = asyncio.run(botmod.api.cs2red_blocks(STEAM, "BANS"))
    assert blocks == []
    assert err == "not_found"


def test_normalize_block_admin_player_hint():
    """В админ-профиле игрок может не дублироваться — берём его из профиля."""
    now = int(time.time())
    raw = {
        "id": 1, "adminId": 42, "reason": "test",
        "timestamp": now - 60, "endTimeStamp": 0,
        "user": {"id": 42, "nick": "lilcom"},
    }
    hint = {"id": 7, "nick": "sllletron", "steamid64": STEAM}
    block = botmod.API.normalize_block(raw, "Бан", from_admin=True,
                                       player=hint)
    assert block["player_nick"] == "sllletron"
    assert block["steam_id"] == STEAM
    assert block["player_id"] == 7
    assert block["admin"] == "lilcom"
    assert block["active"] is True
    assert block["end_ts"] == 0  # навсегда


def test_normalize_block_user_equal_player_is_not_admin():
    """Если API положил самого игрока в user — не выдаём его за админа."""
    raw = {
        "id": 1, "reason": "test", "timestamp": int(time.time()),
        "endTimeStamp": 0,
        "user": {"id": 7, "nick": "sllletron"},
        "action": {"id": 7, "nick": "sllletron", "steamid64": STEAM},
    }
    block = botmod.API.normalize_block(raw, "Мут")
    assert block["player_nick"] == "sllletron"
    assert block["admin"] == "Система"


def test_normalize_block_expired_is_inactive():
    now = int(time.time())
    raw = {"id": 1, "timestamp": now - 1000, "endTimeStamp": now - 10,
           "action": {"id": 7, "nick": "x", "steamid64": STEAM}}
    block = botmod.API.normalize_block(raw, "Бан")
    assert block["active"] is False
