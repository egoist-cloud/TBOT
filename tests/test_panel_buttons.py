# -*- coding: utf-8 -*-
"""Тесты кнопок админ-панели: каждая кнопка делает то, что заявлено."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402


class _Response:
    def __init__(self):
        self.deferred = False
        self.denied = False

    async def defer(self, ephemeral=False):
        self.deferred = True

    async def send_message(self, *args, **kwargs):
        self.denied = True


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return SimpleNamespace(edit=None)


class _Interaction:
    def __init__(self):
        self.response = _Response()
        self.followup = _Followup()
        self.user = SimpleNamespace(
            id=1, guild_permissions=SimpleNamespace(administrator=True))
        self.guild = None


def _item(custom_id):
    view = botmod.PanelView()
    return next(i for i in view.children if i.custom_id == custom_id)


def _allow(monkeypatch):
    async def allow(_interaction):
        return True

    monkeypatch.setattr(botmod, "_panel_guard", allow)
    # Локи создаются в своём event loop — для каждого теста чистые.
    monkeypatch.setattr(botmod, "_heavy_locks", {})


def test_all_four_buttons_present():
    """В панели ровно те кнопки, что описаны в подсказке."""
    ids = {i.custom_id for i in botmod.PanelView().children}
    assert ids == {"panel_link_all", "panel_sync",
                   "panel_rules", "panel_health"}
    print("  ok: 4 кнопки на месте")


def test_button_without_rights_does_nothing(monkeypatch):
    """Без прав кнопка не запускает тяжёлые действия."""
    async def deny(_interaction):
        return False

    monkeypatch.setattr(botmod, "_panel_guard", deny)
    called = []

    async def record(*a, **kw):
        called.append(1)

    monkeypatch.setattr(botmod, "run_auto_link", record)
    monkeypatch.setattr(botmod, "run_sync_all", record)
    i = _Interaction()

    asyncio.run(_item("panel_link_all").callback(i))
    asyncio.run(_item("panel_sync").callback(i))

    assert called == [], "без прав ничего не запускается"
    assert i.response.deferred is False
    print("  ok: без прав кнопки не работают")


def test_link_all_button_runs_auto_link(monkeypatch):
    """«Привязать всех» запускает полную авто-привязку."""
    _allow(monkeypatch)
    used = []

    async def fake_run(interaction, ephemeral=False):
        used.append((interaction, ephemeral))

    monkeypatch.setattr(botmod, "run_auto_link", fake_run)
    i = _Interaction()

    asyncio.run(_item("panel_link_all").callback(i))

    assert i.response.deferred is True
    assert used == [(i, True)], used
    print("  ok: «Привязать всех» → run_auto_link")


def test_sync_button_runs_sync_all(monkeypatch):
    """«Синхронизация» запускает полную синхронизацию ролей."""
    _allow(monkeypatch)
    used = []

    async def fake_run(interaction, ephemeral=False):
        used.append((interaction, ephemeral))

    monkeypatch.setattr(botmod, "run_sync_all", fake_run)
    i = _Interaction()

    asyncio.run(_item("panel_sync").callback(i))

    assert i.response.deferred is True
    assert used == [(i, True)], used
    print("  ok: «Синхронизация» → run_sync_all")


def test_rules_button_reloads_rules(monkeypatch):
    """«Обновить правила» перечитывает rules.txt и сообщает число пунктов."""
    _allow(monkeypatch)
    monkeypatch.setattr(
        botmod, "ai_service",
        SimpleNamespace(rules=SimpleNamespace(load=lambda: 7)))
    i = _Interaction()

    asyncio.run(_item("panel_rules").callback(i))

    assert i.response.deferred is True
    text = str(i.followup.sent[-1][0])
    assert "7" in text and "Правила" in text, text
    print("  ok: «Обновить правила» перечитывает базу знаний")


def test_rules_button_without_ai(monkeypatch):
    """ИИ не инициализирован — понятное сообщение, без исключения."""
    _allow(monkeypatch)
    monkeypatch.setattr(botmod, "ai_service", None)
    i = _Interaction()

    asyncio.run(_item("panel_rules").callback(i))

    assert "не инициализирован" in str(i.followup.sent[-1][0])
    print("  ok: без ИИ кнопка правил отвечает понятно")


def test_health_button_sends_embed(monkeypatch):
    """«Health» собирает состояние систем и присылает Embed."""
    _allow(monkeypatch)

    async def fake_embed():
        return SimpleNamespace(title="Состояние")

    monkeypatch.setattr(botmod, "build_panel_health_embed", fake_embed)
    i = _Interaction()

    asyncio.run(_item("panel_health").callback(i))

    assert i.response.deferred is True
    assert i.followup.sent and i.followup.sent[-1][1].get("embed") is not None
    print("  ok: «Health» присылает Embed")


def test_health_button_reports_error(monkeypatch):
    """Health упал — пользователь видит ошибку, а не тишину."""
    _allow(monkeypatch)

    async def broken():
        raise RuntimeError("нет связи")

    monkeypatch.setattr(botmod, "build_panel_health_embed", broken)
    i = _Interaction()

    asyncio.run(_item("panel_health").callback(i))

    args, kwargs = i.followup.sent[-1]
    embed = kwargs.get("embed")
    blob = (str(args) + str(kwargs)
            + (str(embed.to_dict()) if embed is not None else ""))
    assert "Ошибка" in blob
    print("  ok: ошибка Health показывается пользователю")
