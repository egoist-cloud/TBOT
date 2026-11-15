# -*- coding: utf-8 -*-
"""Тесты недельного отчёта: топ, отметка двух недель и ручные кнопки."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

import script as botmod  # noqa: E402


class _Member:
    def __init__(self, member_id, name, role=None):
        self.id = member_id
        self.display_name = name
        self.mention = f"<@{member_id}>"
        self.roles = [role] if role is not None else []
        self.dm = []
        self.removed = []

    async def send(self, text):
        self.dm.append(text)

    async def remove_roles(self, role, reason=None):
        self.removed.append((role, reason))
        self.roles.remove(role)


class _Role:
    def __init__(self, role_id, name):
        self.id = role_id
        self.name = name


class _Guild:
    def __init__(self, members=(), role=None):
        self.members = list(members)
        self.role = role
        self.roles = [role] if role is not None else []

    def get_member(self, member_id):
        return next((m for m in self.members if m.id == member_id), None)

    def get_role(self, role_id):
        return self.role if self.role and self.role.id == role_id else None


def _data(week, prev, observer=0.0):
    return {"week": {"total": week, "observer": observer},
            "prev": {"total": prev, "observer": 0.0}}


def test_command_registered_and_moderator():
    cmd = botmod.bot.tree.get_command("activity_week")
    assert cmd is not None
    assert "недельн" in cmd.description.lower()
    assert "activity_week" in botmod.MODERATOR_COMMANDS


def test_two_weeks_below_threshold():
    assert botmod._two_weeks_below(_data(3.0, 4.0))
    assert not botmod._two_weeks_below(_data(3.0, 12.0))
    assert not botmod._two_weeks_below(_data(12.0, 3.0))
    # Нет данных за прошлую неделю — это не «две недели подряд».
    assert not botmod._two_weeks_below(
        {"week": {"total": 3.0}, "prev": {"total": None}})


def test_weekly_embed_has_top_and_two_week_mark():
    top_m = _Member(2, "Активный")
    low_m = _Member(1, "Лентяй")
    low_two = _Member(3, "Забытый")
    rows = [(top_m, _data(30.0, 25.0)), (low_m, _data(12.0, 10.0))]
    low = [(low_m, _data(4.0, 12.0)), (low_two, _data(2.0, 3.0))]

    embed = botmod._build_weekly_report_embed(
        rows, low, [], [], checked=4, total=4)

    assert embed.color.value == 0x000000
    assert "Quantum" in embed.footer.text
    fields = {f.name: f.value for f in embed.fields}
    assert "🏆 Топ активности" in fields
    assert "Активный" in fields["🏆 Топ активности"]
    low_text = fields["⚠️ Малоактивные"]
    assert low_m.mention in low_text and low_two.mention in low_text
    # Только реально две недели подряд получает красную отметку.
    line_two = next(x for x in low_text.splitlines() if low_two.mention in x)
    line_one = next(x for x in low_text.splitlines() if low_m.mention in x)
    assert "2 недели подряд" in line_two
    assert "2 недели подряд" not in line_one
    assert fields["🔴 Две недели подряд"] == "1"


def test_weekly_embed_without_low_members():
    ok = _Member(2, "Активный")
    embed = botmod._build_weekly_report_embed(
        [(ok, _data(30.0, 25.0))], [], [], [], checked=1, total=1)
    low_field = next(f for f in embed.fields if f.name == "⚠️ Малоактивные")
    assert "≥" in low_field.value


def test_view_has_manual_buttons_for_low_members():
    members = [_Member(i, f"Игрок{i}") for i in range(1, 8)]
    view = botmod.WeeklyActivityView(members)
    # Не больше 5 строк и по две кнопки на игрока.
    assert len(view.children) == 10
    ids = [c.custom_id for c in view.children]
    assert ids[0] == "week_warn:1"
    assert ids[1] == "week_remove:1"
    assert len(ids) == len(set(ids))
    assert all(c.row < 5 for c in view.children)
    empty = botmod.WeeklyActivityView([])
    assert empty.children == []


def test_warning_sends_dm_without_removing_role():
    role = _Role(botmod.TALENT_ROLE_ID, "TALENT")
    member = _Member(10, "Лентяй", role)

    text = asyncio.run(botmod._send_activity_warning(member))

    assert "Предупреждение" in text
    assert len(member.dm) == 1
    assert "Роль пока не снята" in member.dm[0]
    assert member.roles == [role]  # автоматического снятия нет
    assert member.removed == []


def test_warning_reports_closed_dm():
    class _ClosedDm(_Member):
        async def send(self, text):
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"),
                "Cannot send messages to this user")

    member = _ClosedDm(11, "Закрытый")
    text = asyncio.run(botmod._send_activity_warning(member))
    assert "закрыты" in text
    assert member.roles == []


def test_remove_role_is_explicit_and_audited(monkeypatch):
    role = _Role(botmod.TALENT_ROLE_ID, "TALENT")
    member = _Member(12, "Лентяй", role)
    guild = _Guild([member], role)
    sent, audited = [], []

    async def fake_safe_send(target, *args, **kwargs):
        sent.append((target, args, kwargs))

    async def fake_audit(*args, **kwargs):
        audited.append((args, kwargs))

    monkeypatch.setattr(botmod, "safe_send", fake_safe_send)
    monkeypatch.setattr(botmod, "audit_action", fake_audit)

    interaction = SimpleNamespace(
        user=SimpleNamespace(
            id=1, guild_permissions=SimpleNamespace(administrator=True)),
        guild=guild,
        client=object(),
        response=SimpleNamespace(
            defer=lambda **kwargs: asyncio.sleep(0)),
        followup=object(),
    )

    asyncio.run(botmod._weekly_action(interaction, "remove", member.id))

    assert member.roles == []
    assert member.removed and member.removed[0][0] is role
    assert "снята" in sent[0][1][0]
    assert audited and audited[0][1]["target"] is member
