# -*- coding: utf-8 -*-
"""Тесты карточки заявки, центра управления и отчёта по активности клана."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, init_db  # noqa: E402


class _Member:
    def __init__(self, member_id=1, name="Игрок", mention=None,
                 administrator=False):
        self.id = member_id
        self.display_name = name
        self.mention = mention or f"<@{member_id}>"
        self.roles = []
        self.guild_permissions = SimpleNamespace(administrator=administrator)


def _stats():
    # Реальная форма cs2red: личная статистика лежит во вложенном rank,
    # а верхнеуровневые kills/deaths в составе клана — клановые.
    return {
        "user": {"nick": "lowsky", "kills": 9999, "deaths": 1,
                 "rank": {"kills": 160, "deaths": 100, "assists": 40,
                          "hits": 620, "shots": 1550, "points": 646922,
                          "rankId": 13}},
        "activity": {
            "week": {"total": 12.5},
            "prev": {"total": 10.0},
            "modes": [{"name": "PUBLIC", "week": 8.0},
                      {"name": "AWP", "week": 4.5}],
        },
    }


# --------------------------------------------------------------------------
# Каналы и карточка заявки
# --------------------------------------------------------------------------
def test_channels_are_configured_as_requested():
    assert botmod.APPLICATIONS_CHANNEL_ID == 1418355322900189391
    assert botmod.APPLICATIONS_CENTER_CHANNEL_ID == 1554796338661826660
    assert botmod.WEEKLY_ACTIVITY_CHANNEL_ID == 1554883485834149888


def test_application_card_matches_reference_layout():
    embed = botmod._build_application_embed(
        nickname="lowsky", steam_id="76561198000000001", age="14",
        play_time="12:00", reason="хочу набрать медийность",
        discord_name="catarsis", discord_id=123456789012345678,
        avatar_url="", stats=_stats())

    names = [f.name for f in embed.fields]
    assert names == ["Никнейм", "Профиль cs2red", "Статистика",
                     "Информация", "Discord"], names
    fields = {f.name: f.value for f in embed.fields}
    assert "lowsky" in fields["Никнейм"]
    assert "cs2red.ru/profile/76561198000000001" in fields["Профиль cs2red"]
    stats_text = fields["Статистика"]
    assert "K/D" in stats_text and "1.60" in stats_text
    # HS на cs2red нет — показываем реальную точность hits/shots.
    assert "HS" not in stats_text and "Точность" in stats_text
    assert "40%" in stats_text
    assert "Очки" in stats_text and "646 922" in stats_text
    assert "За неделю" in stats_text and "прошлая" in stats_text
    assert "Уровень" not in stats_text
    assert chr(10) * 2 not in stats_text
    info = fields["Информация"]
    assert "Возраст" in info and "14" in info
    assert "12:00" in info and "медийность" in info
    assert "@catarsis" in fields["Discord"]
    assert "Quantum" not in embed.footer.text
    assert embed.color.value == 0


def test_application_discord_id_is_extracted_from_card():
    embed = botmod._build_application_embed(
        nickname="n", steam_id=None, age="12", play_time="10:00", reason="",
        discord_name="user", discord_id=987654321098765432,
        avatar_url="", stats=None)
    assert botmod.extract_discord_id_from_embed(embed) == 987654321098765432


def test_application_stats_omits_missing_fields():
    stats = {"user": {"rank": {"kills": 10, "deaths": 5}},
             "activity": None}
    text = botmod._application_stats_text(stats)
    assert "K/D" in text and "2.00" in text
    assert "HS" not in text and "Точность" not in text
    assert "Очки" not in text and "Уровень" not in text
    assert "За неделю" not in text
    assert chr(10) * 2 not in text


def test_application_stats_without_cs2red_data_is_graceful():
    text = botmod._application_stats_text(None)
    assert "cs2red" in text


def test_clan_panel_has_no_emoji_and_button_below():
    embed = botmod._build_clan_conditions_embed()
    for field in embed.fields:
        assert "<a:" not in field.name and "<:" not in field.name, field.name
        assert "💬" not in field.name
    assert "Quantum" not in (embed.footer.text or "")
    # Классический embed: компоненты (список и кнопка) идут под панелью.
    assert botmod.ClanView.v2_style is False


# --------------------------------------------------------------------------
# Центр управления заявками
# --------------------------------------------------------------------------
def test_center_embed_and_view():
    embed = botmod._applications_center_embed(3, 10, [111, 222])
    fields = {f.name: f.value for f in embed.fields}
    assert "3" in fields["Очередь"] and "10" in fields["Очередь"]
    assert "2" in fields["Доступ"]
    assert "Доступ закрыт" in fields["Доступ"]
    assert "<@111>" in fields["Кто получил доступ"]

    view = botmod.ApplicationCenterView()
    ids = [c.custom_id for c in view.children if getattr(c, "custom_id", None)]
    assert "applications_center:queue" in ids
    assert "applications_center:stats" in ids
    assert "applications_center:grant" in ids
    assert "applications_center:revoke" in ids
    assert view.is_persistent()


def test_deny_overrides_reviewer_role(monkeypatch):
    monkeypatch.setattr(botmod, "ALLOWED_ROLE_ID", 777)
    member = _Member(888)
    member.roles = [SimpleNamespace(id=777)]
    assert botmod.user_can_review(member) is True
    botmod._REVIEW_DENY_IDS.add(888)
    try:
        assert botmod.user_can_review(member) is False
    finally:
        botmod._REVIEW_DENY_IDS.discard(888)


def test_center_title_matches_panel_spec():
    assert botmod.APPLICATIONS_CENTER_SPEC.title in \
        botmod._applications_center_embed().title


def test_extra_application_access_is_saved_and_checked():
    async def scenario():
        await init_db()
        try:
            ids = await botmod.set_application_reviewer(555, True)
            assert 555 in ids and 555 in botmod._EXTRA_REVIEWER_IDS
            member = _Member(555)
            assert botmod.user_can_review(member) is True
            ids = await botmod.set_application_reviewer(555, False)
            assert 555 not in ids and 555 not in botmod._EXTRA_REVIEWER_IDS
            assert 555 in botmod._REVIEW_DENY_IDS
            assert botmod.user_can_review(member) is False
            # Выдача доступа снимает запрет.
            await botmod.set_application_reviewer(555, True)
            assert 555 not in botmod._REVIEW_DENY_IDS
            assert botmod.user_can_review(member) is True
        finally:
            await close_db()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Отчёт по активности клана
# --------------------------------------------------------------------------
def _activity(modes):
    return {"week": {"total": sum(m["week"] for m in modes)},
            "prev": {"total": 0.0}, "modes": modes}


def test_clan_activity_counts_only_clan_modes():
    data = _activity([
        {"name": "PUBLIC", "week": 5.0},
        {"name": "AWP", "week": 2.0},
        {"name": "JAIL", "week": 1.0},
        {"name": "ARENA", "week": 100.0},
    ])
    hours, breakdown = botmod._clan_activity_hours(data)
    assert hours == 8.0
    assert set(breakdown) == {"PUBLIC", "AWP", "JAIL"}


def test_clan_activity_ignores_other_modes():
    data = _activity([{"name": "ARENA", "week": 20.0}])
    hours, breakdown = botmod._clan_activity_hours(data)
    assert hours == 0.0 and breakdown == {}


def test_clan_activity_falls_back_when_mode_names_missing():
    data = _activity([{"name": "Режим 1", "week": 3.0},
                      {"name": "Режим 2", "week": 2.0}])
    hours, _ = botmod._clan_activity_hours(data)
    assert hours == 5.0


def test_member_nick_variants_include_username_and_display():
    member = SimpleNamespace(id=1, name="lowsky", display_name="Low Sky")
    assert botmod._member_nick_variants(member) == ["lowsky", "low sky"]


def test_roster_match_requires_single_steam_id():
    roster = [
        {"nick": "lowsky", "steam_id": "76561198000000001"},
        {"nick": "Low Sky", "steam_id": "76561198000000001"},
        {"nick": "other", "steam_id": "76561198000000002"},
    ]
    assert botmod._match_roster_steam(
        roster, {"lowsky", "low sky"}) == "76561198000000001"
    ambiguous = roster + [
        {"nick": "lowsky", "steam_id": "76561198000000003"},
    ]
    assert botmod._match_roster_steam(ambiguous, {"lowsky"}) is None


def test_resolve_member_steam_falls_back_to_display_name(monkeypatch):
    member = SimpleNamespace(id=42, name="lowsky", display_name="Low Sky")

    async def search(nick):
        if nick == "lowsky":
            return [{"steamid64": "76561198000000001"},
                    {"steamid64": "76561198000000002"}]
        if nick == "Low Sky":
            return [{"steamid64": "76561198000000003"}]
        return []

    monkeypatch.setattr(botmod.api, "cs2red_search_by_nick", search)

    sid, source = asyncio.run(botmod._resolve_member_steam(member))
    assert (sid, source) == ("76561198000000003", "display")


def test_resolve_member_steam_uses_roster_when_search_ambiguous(monkeypatch):
    member = SimpleNamespace(id=43, name="lowsky", display_name="Low Sky")

    async def search(_nick):
        return [{"steamid64": "76561198000000001"},
                {"steamid64": "76561198000000002"}]

    monkeypatch.setattr(botmod.api, "cs2red_search_by_nick", search)

    roster = [{"nick": "Low Sky", "steam_id": "76561198000000003"}]
    sid, source = asyncio.run(
        botmod._resolve_member_steam(member, roster=roster))
    assert (sid, source) == ("76561198000000003", "roster")


def test_collect_activity_skips_roster_when_all_linked(monkeypatch):
    member = _Member(55, "Игрок")
    calls = {"roster": 0}

    async def get_link(_member_id):
        return "76561198000000009"

    async def activity(_sid):
        return {"modes": [], "week": {"total": 2.0}}, None

    async def roster():
        calls["roster"] += 1
        return {}, None

    monkeypatch.setattr(botmod.links_repo, "get_link", get_link)
    monkeypatch.setattr(botmod.api, "cs2red_activity", activity)
    monkeypatch.setattr(botmod.api, "cs2red_clan_roster", roster)

    entries = asyncio.run(botmod.collect_clan_activity([member]))
    assert entries[0]["source"] == "link"
    assert entries[0]["hours"] == 2.0
    assert calls["roster"] == 0


def test_application_card_escapes_user_text():
    embed = botmod._build_application_embed(
        nickname="**злой** " + "x" * 300, steam_id="", age="17",
        play_time="20:00", reason="[клик](https://evil.example) " + "р" * 400,
        discord_name="user", discord_id=1, avatar_url="", stats=None)
    fields = {f.name: f.value for f in embed.fields}
    assert r"\*\*злой\*\*" in fields["Никнейм"]
    assert len(fields["Никнейм"]) <= 104
    assert r"\[клик]" in fields["Информация"]
    assert len(fields["Информация"]) <= 1024


def test_user_can_review_denies_user_without_roles(monkeypatch):
    monkeypatch.setattr(botmod, "_EXTRA_REVIEWER_IDS", set())
    monkeypatch.setattr(botmod, "_REVIEW_DENY_IDS", set())
    assert botmod.user_can_review(SimpleNamespace(id=999999)) is False


def test_access_read_failure_keeps_memory(monkeypatch):
    async def broken(_flag):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(botmod, "_EXTRA_REVIEWER_IDS", {111})
    monkeypatch.setattr(botmod, "_REVIEW_DENY_IDS", {222})
    monkeypatch.setattr(botmod.seen_repo, "get_flag", broken)

    assert asyncio.run(botmod.load_application_reviewers()) is False
    assert botmod._EXTRA_REVIEWER_IDS == {111}
    assert botmod._REVIEW_DENY_IDS == {222}


def test_activity_report_marks_roster_source():
    member = _Member(4, "Без привязки")
    entries = [{"member": member, "steam_id": "76561198000000003",
                "source": "roster", "hours": 4.0, "modes": {},
                "reason": ""}]
    embed = botmod._build_clan_activity_embed(entries)
    below = {f.name: f.value for f in embed.fields}["Меньше 8 ч. за неделю"]
    assert "по составу клана" in below


def test_clan_activity_report_lists_below_norm_and_unknown():
    low = _Member(1, "Слабый")
    ok = _Member(2, "Активный")
    unknown = _Member(3, "Без привязки")
    entries = [
        {"member": low, "steam_id": "76561198000000001", "source": "link",
         "hours": 4.0, "modes": {"PUBLIC": 4.0}, "reason": ""},
        {"member": ok, "steam_id": "76561198000000002", "source": "link",
         "hours": 12.0, "modes": {"PUBLIC": 12.0}, "reason": ""},
        {"member": unknown, "steam_id": None, "source": "",
         "hours": None, "modes": {}, "reason": "нет профиля на cs2red"},
    ]
    embed = botmod._build_clan_activity_embed(entries)
    fields = {f.name: f.value for f in embed.fields}
    assert "Проверено" in fields and fields["Проверено"] == "3"
    below = fields["Меньше 8 ч. за неделю"]
    assert low.mention in below and ok.mention not in below
    assert "нет профиля" in fields["Не удалось проверить"]
    assert embed.description and "Public" in embed.description
    assert "Quantum" not in embed.footer.text


def test_active_command_and_weekly_task_are_registered():
    cmd = botmod.bot.tree.get_command("active")
    assert cmd is not None
    assert "active" in botmod.MODERATOR_COMMANDS
    assert "active" in botmod.HELP_DESCRIPTIONS
    assert botmod.weekly_activity_report in botmod.BACKGROUND_LOOPS
    assert botmod.WEEKLY_ACTIVITY_MIN_HOURS == 8
    assert botmod.WEEKLY_ACTIVITY_WEEKDAY == 4
