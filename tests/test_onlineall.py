# -*- coding: utf-8 -*-
"""Тесты парсера активности cs2red и команды /check_onlineall."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402


def test_command_registered():
    """/check_onlineall зарегистрирована и её описание на русском."""
    cmd = botmod.bot.tree.get_command("check_onlineall")
    assert cmd is not None, "/check_onlineall не зарегистрирован"
    assert "онлайн" in cmd.description.lower()


def test_activity_command_registered():
    """/activity <steam_id> зарегистрирована (одиночная проверка)."""
    cmd = botmod.bot.tree.get_command("activity")
    assert cmd is not None, "/activity не зарегистрирован"
    params = [p.name for p in cmd.parameters]
    assert "steam_id" in params


def test_build_activity_embed():
    """Embed одного игрока: SteamID, недели, репорты."""
    data = {"nick": "Miki", "internal_id": 8081,
            "week": {"total": 26.0, "observer": 20 / 60},
            "prev": {"total": 12.0, "observer": 1.0},
            "reports": {"week": 3, "prev": 1, "total": 9}}
    embed = botmod._build_activity_embed("76561198000000000", data)
    assert embed.color.value == 0x000000
    assert "Miki" in embed.title and "8081" in embed.title
    names = " ".join(f.name for f in embed.fields)
    assert "Текущая неделя" in names
    assert "Прошлая неделя" in names
    assert "Репорты" in names


def test_online_min_hours_default():
    """Порог малоактивности — 9 часов в неделю."""
    assert botmod.ONLINE_MIN_HOURS == 9.0


def test_parse_time_formats():
    """Разные форматы времени приводятся к часам."""
    p = botmod._parse_time_to_hours
    assert p("26 ч.") == 26.0
    assert p("1 ч. 30 м.") == 1.5
    assert abs(p("20 м.") - 20 / 60) < 1e-6
    assert p("1.5 ч.") == 1.5
    assert p("26:30") == 26.5
    assert p("0 ч.") == 0.0
    assert p("") is None
    assert p("нет данных") is None


def test_parse_flat_layout():
    """Метки и значения идут подряд (без таблиц)."""
    html = ("<div>Текущая неделя</div>"
            "<div>Общее время: 26 ч. Наблюдатель: 20 м.</div>"
            "<div>Прошлая неделя</div>"
            "<div>Общее время: 12 ч. Наблюдатель: 1 ч.</div>"
            "<div>Рассмотрено репортов</div><div>3 1 9</div>")
    data = botmod.parse_activity_html(html)
    assert data is not None
    assert data["week"]["total"] == 26.0
    assert abs(data["week"]["observer"] - 20 / 60) < 1e-6
    assert data["prev"]["total"] == 12.0
    assert data["prev"]["observer"] == 1.0
    assert data["reports"] == {"week": 3, "prev": 1, "total": 9}


def test_parse_table_layout_with_total_column():
    """Табличная вёрстка с колонкой «Всего»."""
    html = (
        "<table>"
        "<tr><th>Показатель</th><th>Текущая неделя</th>"
        "<th>Прошлая неделя</th><th>Всего</th></tr>"
        "<tr><td>Общее время</td><td>26 ч.</td><td>12 ч.</td><td>200 ч.</td></tr>"
        "<tr><td>Наблюдатель</td><td>20 м.</td><td>0 м.</td><td>5 ч.</td></tr>"
        "<tr><td>Рассмотрено репортов</td><td>3</td><td>1</td><td>9</td></tr>"
        "</table>")
    data = botmod.parse_activity_html(html)
    assert data is not None
    assert data["week"]["total"] == 26.0
    assert abs(data["week"]["observer"] - 20 / 60) < 1e-6
    assert data["reports"] == {"week": 3, "prev": 1, "total": 9}


def test_parse_observer_not_mixed_with_next_week():
    """Наблюдатель текущей недели не суммируется с временем прошлой."""
    html = ("<div>Текущая неделя Общее время 26 ч. Наблюдатель 20 м."
            " Прошлая неделя Общее время 12 ч. Наблюдатель 1 ч.</div>")
    data = botmod.parse_activity_html(html)
    assert data is not None
    assert abs(data["week"]["observer"] - 20 / 60) < 1e-6  # не 12ч+20м
    assert data["prev"]["observer"] == 1.0


def test_parse_without_activity_returns_none():
    """Нет меток активности — None, а не мусорные данные."""
    assert botmod.parse_activity_html("<html><body>ничего</body></html>") is None
    assert botmod.parse_activity_html("") is None


def test_fmt_hours():
    """Читаемый формат часов."""
    assert botmod._fmt_hours(None) == "—"
    assert botmod._fmt_hours(26.0) == "26.0 ч."
    assert botmod._fmt_hours(0.33) == "20 м."


class _FakeMember:
    def __init__(self, member_id, name):
        self.id = member_id
        self.display_name = name
        self.mention = f"<@{member_id}>"


def test_build_onlineall_embed_splits_low():
    """Малоактивные (< порога) попадают в отдельное поле."""
    low_m = _FakeMember(1, "Лентяй")
    ok_m = _FakeMember(2, "Активный")
    low = [(low_m, {"week": {"total": 3.0, "observer": 0.0},
                    "prev": {"total": 10.0, "observer": 0.0}})]
    rows = [(ok_m, {"week": {"total": 30.0, "observer": 1.0},
                    "prev": {"total": 25.0, "observer": 0.0}})]
    embed = botmod._build_onlineall_embed(rows, low, [], [], checked=2, total=2)
    assert embed.color.value == 0x000000
    assert "Quantum" in embed.footer.text
    names = [f.name for f in embed.fields]
    assert any("Малоактивные" in n for n in names)
    assert any("В норме" in n for n in names)
    low_field = next(f for f in embed.fields if "Малоактивные" in f.name)
    assert low_m.mention in low_field.value


def test_embed_reports_ok_when_nobody_low():
    """Если все в норме — об этом прямо сказано."""
    ok = [( _FakeMember(2, "Активный"),
            {"week": {"total": 30.0, "observer": 0.0},
             "prev": {"total": 25.0, "observer": 0.0}})]
    embed = botmod._build_onlineall_embed(ok, [], [], [], checked=1, total=1)
    low_field = next(f for f in embed.fields if "Малоактивные" in f.name)
    assert "≥" in low_field.value or "нет" in low_field.value.lower()


def test_msk_week_start():
    """Неделя начинается в понедельник 00:00 МСК (UTC+3)."""
    friday = 1792756800           # 2026-10-23 12:00 UTC, пятница
    monday = 1792357200           # 2026-10-19 00:00 МСК
    prev_monday = 1791752400      # 2026-10-12 00:00 МСК
    assert botmod.msk_week_start(friday, 0) == monday
    assert botmod.msk_week_start(friday, 1) == prev_monday
    # Воскресенье 23:59 МСК ещё относится к прошлой неделе.
    assert botmod.msk_week_start(monday - 60, 0) == prev_monday
    # Ровно понедельник 00:00 МСК — уже новая неделя.
    assert botmod.msk_week_start(monday, 0) == monday


def _activity_user(now):
    return {
        "Activity": [
            {"gamemodeId": 1,
             "monday": 3600, "tuesday": 3600, "wednesday": 0, "thursday": 0,
             "friday": 0, "saturday": 0, "sunday": 0, "spectator": 600,
             "last_monday": 7200, "last_tuesday": 0, "last_wednesday": 0,
             "last_thursday": 0, "last_friday": 0, "last_saturday": 0,
             "last_sunday": 0, "last_spectator": 120},
            {"gamemodeId": 2,
             "monday": 1800, "tuesday": 0, "wednesday": 0, "thursday": 0,
             "friday": 0, "saturday": 0, "sunday": 0, "spectator": 0,
             "last_monday": 0, "last_tuesday": 0, "last_wednesday": 0,
             "last_thursday": 0, "last_friday": 0, "last_saturday": 0,
             "last_sunday": 0, "last_spectator": 0},
        ],
        "admin_reports": [
            {"timestamp_result": now - 100},
            {"timestamp": now - 8 * 86400},
            {"timestamp": 0},
        ],
    }


def test_parse_activity_json_weeks_and_modes():
    """Активность из JSON: секунды -> часы, наблюдатель, репорты, режимы."""
    now = 1792756800
    data = botmod.parse_activity_json(_activity_user(now), now=now)
    assert data is not None
    # 3600+3600+1800 = 9000 с = 2.5 ч
    assert data["week"]["total"] == 2.5
    assert abs(data["week"]["observer"] - 600 / 3600) < 1e-9
    # 7200 с = 2 ч
    assert data["prev"]["total"] == 2.0
    assert abs(data["prev"]["observer"] - 120 / 3600) < 1e-9
    assert data["reports"] == {"week": 1, "prev": 1, "total": 3}
    assert [m["gamemode_id"] for m in data["modes"]] == [1, 2]
    # Режимы отсортированы по текущей неделе (2 ч > 0.5 ч).
    assert data["modes"][0]["week"] == 2.0
    assert data["modes"][1]["week"] == 0.5


def test_parse_activity_json_empty_is_zero():
    """Пустая активность — это нули, а не ошибка."""
    data = botmod.parse_activity_json(
        {"Activity": [], "admin_reports": []}, now=1792756800)
    assert data is not None
    assert data["week"]["total"] == 0
    assert data["prev"]["total"] == 0
    assert data["reports"] == {"week": 0, "prev": 0, "total": 0}
    assert data["modes"] == []


def test_parse_activity_json_missing_returns_none():
    """Нет ключа Activity — значит формат не тот, честно возвращаем None."""
    assert botmod.parse_activity_json({}) is None
    assert botmod.parse_activity_json(None) is None


def test_build_activity_embed_with_modes():
    data = {"nick": "Miki", "internal_id": 8081,
            "week": {"total": 2.5, "observer": 10 / 60},
            "prev": {"total": 2.0, "observer": 0.0},
            "reports": {"week": 1, "prev": 1, "total": 3},
            "modes": [{"gamemode_id": 1, "name": "PUBLIC",
                       "week": 2.0, "prev": 2.0, "observer": 0.0},
                      {"gamemode_id": 2, "name": "AWP",
                       "week": 0.5, "prev": 0.0, "observer": 0.0}]}
    embed = botmod._build_activity_embed("76561198000000000", data)
    modes_field = next((f for f in embed.fields if f.name == "По режимам"), None)
    assert modes_field is not None
    assert "PUBLIC" in modes_field.value and "AWP" in modes_field.value


def test_safe_int_variants():
    assert botmod._safe_int("12.9") == 12
    assert botmod._safe_int(12.9) == 12
    assert botmod._safe_int(None, -1) == -1
    assert botmod._safe_int("abc", 5) == 5


class _ActivityMember:
    def __init__(self, member_id, name):
        self.id = member_id
        self.display_name = name
        self.mention = f"<@{member_id}>"
        self.bot = False


class _ActivityRole:
    def __init__(self, members):
        self.members = members
        self.mention = "@TALENT"


class _ActivityGuild:
    def __init__(self, role):
        self.role = role

    def get_role(self, _role_id):
        return self.role


class _ActivityResponse:
    def __init__(self):
        self.ephemeral = None

    async def defer(self, ephemeral=False):
        self.ephemeral = ephemeral

    def is_done(self):
        return self.ephemeral is not None


class _ActivityFollowup:
    async def send(self, *_args, **_kwargs):
        return None


class _ActivityInteraction:
    def __init__(self, role):
        self.response = _ActivityResponse()
        self.followup = _ActivityFollowup()
        self.guild = _ActivityGuild(role)
        self.client = object()
        self.edits = []

    async def edit_original_response(self, content=None, **_kwargs):
        self.edits.append(content)


def test_collect_online_stats_marks_missing_week_as_unknown(monkeypatch):
    """Пустой week.total — «нет данных», а не ложный ноль активности."""
    import asyncio

    member = _ActivityMember(1, "Игрок")

    async def fake_link(_member_id):
        return "76561198000000001"

    async def fake_activity(_steam_id):
        return {"week": {}}, ""

    monkeypatch.setattr(botmod.links_repo, "get_link", fake_link)
    monkeypatch.setattr(botmod.api, "cs2red_activity", fake_activity)
    monkeypatch.setattr(botmod, "BLOCK_CHECK_DELAY", 0)

    rows, low, no_link, errors, checked = asyncio.run(
        botmod.collect_online_stats([member]))

    assert rows == [] and low == [] and no_link == []
    assert checked == 0
    assert errors == [(member, "нет данных активности")]


# ============================================================
# /online — кто сейчас на серверах, со ссылками на профили
# ============================================================
def test_online_command_registered():
    """/online зарегистрирована, описание на русском."""
    cmd = botmod.bot.tree.get_command("online")
    assert cmd is not None, "/online не зарегистрирован"
    assert "онлайн" in cmd.description.lower()
    assert "online" in botmod.MODERATOR_COMMANDS


def test_build_online_embed_links_profiles():
    """В отчёте «сейчас онлайн» ник ведёт на профиль cs2red."""
    from types import SimpleNamespace

    member = SimpleNamespace(id=1, display_name="Игрок", mention="<@1>")
    embed = botmod._build_online_embed(
        [(member, "76561198000000001", "Miki", 1800.0)], 5, [])

    text = " ".join(f.value for f in embed.fields)
    assert "https://cs2red.ru/profile/76561198000000001" in text
    assert "Miki" in text
    assert "30 м." in text
    assert "Никого" not in text


def test_build_online_embed_empty_and_errors():
    """Никого нет — понятный текст; ошибки не ломают embed."""
    embed = botmod._build_online_embed([], 3, ["Игрок: не узнал ник"])
    fields = {f.name: f.value for f in embed.fields}
    assert "Никого" in fields["Сейчас играют"]
    assert "не узнал ник" in fields["⚠️ Не удалось проверить"]


def test_onlineall_embed_shows_profile_links():
    """Недельный отчёт тоже показывает профили обеих групп."""
    from types import SimpleNamespace

    member = SimpleNamespace(id=2, display_name="Игрок", mention="<@2>")
    good = {"nick": "Miki", "steam_id": "76561198000000002",
            "week": {"total": 12.0}, "prev": {"total": 3.0}}
    low = {"nick": "Low", "steam_id": "76561198000000003",
           "week": {"total": 2.0}, "prev": {"total": 1.0}}
    embed = botmod._build_onlineall_embed(
        [(member, good)], [(member, low)], [], [], 2, 2)

    fields = {f.name: f.value for f in embed.fields}
    assert "https://cs2red.ru/profile/76561198000000002" in fields["✅ В норме"]
    assert "https://cs2red.ru/profile/76561198000000003" in \
        fields["⚠️ Малоактивные"]
