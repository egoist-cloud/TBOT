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
