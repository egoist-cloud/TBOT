# -*- coding: utf-8 -*-
"""Тесты панелей в стиле Components V2 (контейнер, секции, строки кнопок)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

import script as botmod  # noqa: E402


def _embed():
    e = discord.Embed(title="🛠 Панель управления", description="Тестовая панель")
    e.add_field(name="Статус", value="работает", inline=True)
    e.add_field(name="Версия", value="1.0", inline=True)
    e.add_field(name="Раздел", value="действия", inline=False)
    e.set_footer(text="Quantum")
    return e


def _items(layout):
    return list(layout.walk_children())


def _buttons(layout):
    return [i for i in _items(layout) if isinstance(i, discord.ui.Button)]


def test_layout_is_components_v2_container():
    """Панель — один LayoutView с контейнером, текстом и строками."""
    layout = botmod._v2_layout_from_embed(_embed(), botmod.PanelView())

    assert isinstance(layout, discord.ui.LayoutView)
    assert len(layout.children) == 1
    container = layout.children[0]
    assert isinstance(container, discord.ui.Container)
    texts = [i for i in _items(layout) if isinstance(i, discord.ui.TextDisplay)]
    assert any("## 🛠 Панель управления" in t.content for t in texts)
    assert any("**Статус** — работает" in t.content for t in texts)
    assert any("**Раздел**" in t.content for t in texts)
    assert any(isinstance(i, discord.ui.Separator) for i in _items(layout))
    print("  ok: панель собирается как Components V2")


def test_buttons_kept_with_custom_ids():
    """Все кнопки панели остаются в layout со своими custom_id."""
    layout = botmod._v2_layout_from_embed(_embed(), botmod.PanelView())
    ids = {b.custom_id for b in _buttons(layout)}
    assert ids == {"panel_link_all", "panel_sync", "panel_rules", "panel_health"}
    rows = [i for i in _items(layout) if isinstance(i, discord.ui.ActionRow)]
    assert len(rows) >= 2, "кнопки разложены по строкам"
    print("  ok: кнопки и custom_id сохранены")


def test_clan_view_select_inside_container():
    """Клан-панель: селект клана и кнопка-ссылка живут в контейнере."""
    layout = botmod._v2_layout_from_embed(_embed(), botmod.ClanView())
    selects = [i for i in _items(layout) if isinstance(i, discord.ui.Select)]
    assert selects, "селект клана должен остаться"
    assert _buttons(layout), "кнопка-ссылка должна остаться"
    print("  ok: клан-панель с селектом в V2")


def test_safe_send_converts_panel(monkeypatch):
    """safe_send сам отправляет панель контейнером вместо embed+view."""
    async def noop():
        return None

    monkeypatch.setattr(botmod, "_rate_limit_send", noop)
    captured = {}

    class _Target:
        async def send(self, **kwargs):
            captured.update(kwargs)
            return object()

    asyncio.run(botmod.safe_send(_Target(), embed=_embed(),
                                 view=botmod.PanelView()))

    assert isinstance(captured.get("view"), discord.ui.LayoutView)
    assert "embed" not in captured
    print("  ok: safe_send отдаёт V2-контейнер")


def test_safe_send_leaves_card_views_alone(monkeypatch):
    """Карточки заявок остаются embed'ами: голосование читает message.embeds."""
    async def noop():
        return None

    monkeypatch.setattr(botmod, "_rate_limit_send", noop)
    captured = {}

    class _Target:
        async def send(self, **kwargs):
            captured.update(kwargs)
            return object()

    asyncio.run(botmod.safe_send(_Target(), embed=_embed(),
                                 view=botmod.ApplicationManagement("abc")))

    assert isinstance(captured.get("embed"), discord.Embed)
    assert not isinstance(captured.get("view"), discord.ui.LayoutView)
    print("  ok: карточки заявок не ломаются")


class _EditInteraction:
    def __init__(self):
        self.edited = None

    async def edit_original_response(self, **kwargs):
        self.edited = kwargs


def test_give_panel_edits_as_v2(monkeypatch):
    """Перерисовка /give тоже уходит контейнером, без embed."""
    async def noop(*_a, **_kw):
        return []

    monkeypatch.setattr(botmod.moderation_repo, "get_moderator_commands", noop)
    panel = botmod.GivePanelView(9001)
    i = _EditInteraction()

    asyncio.run(panel._edit(i))

    assert isinstance(i.edited.get("view"), discord.ui.LayoutView)
    assert "embed" not in i.edited
    # повторная перерисовка не должна ломать layout
    asyncio.run(panel._edit(i))
    assert isinstance(i.edited.get("view"), discord.ui.LayoutView)
    print("  ok: /give перерисовывается в V2 и переживает повтор")
