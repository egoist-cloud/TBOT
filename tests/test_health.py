# -*- coding: utf-8 -*-
"""/health: эндпоинт должен отражать состояние бота, а не только Flask-потока.

Docker HEALTHCHECK и Render используют его как признак живости, поэтому
200 допустим только когда бот залогинился и WebSocket не закрыт.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402


def _patch_bot(monkeypatch, *, ready, closed):
    """Подменяет состояние клиента, не поднимая соединение с Discord."""
    monkeypatch.setattr(botmod.bot, "is_ready", lambda: ready)
    monkeypatch.setattr(botmod.bot, "is_closed", lambda: closed)


def _get(path="/health"):
    return botmod.app.test_client().get(path)


def test_health_503_before_login(monkeypatch):
    """Бот ещё не залогинился — 503, чтобы оркестратор не считал его живым."""
    _patch_bot(monkeypatch, ready=False, closed=False)

    r = _get()

    assert r.status_code == 503
    data = r.get_json()
    assert data["ok"] is False
    assert data["ready"] is False
    assert data["closed"] is False


def test_health_200_when_ready(monkeypatch):
    """Бот готов — 200 и телеметрия в теле."""
    _patch_bot(monkeypatch, ready=True, closed=False)

    r = _get()

    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["ready"] is True
    assert data["closed"] is False
    # До heartbeat latency неизвестна: None, а не 0 мс.
    assert "latency_ms" in data
    assert isinstance(data["uptime_seconds"], int)


def test_health_503_when_closed(monkeypatch):
    """WebSocket закрыт — даже если is_ready() ещё True, отдаём 503."""
    _patch_bot(monkeypatch, ready=True, closed=True)

    r = _get()

    assert r.status_code == 503
    assert r.get_json()["ok"] is False


def test_root_uses_same_check(monkeypatch):
    """`/` отвечает так же, как `/health` (Render может опрашивать корень)."""
    _patch_bot(monkeypatch, ready=False, closed=False)
    assert _get("/").status_code == 503

    _patch_bot(monkeypatch, ready=True, closed=False)
    assert _get("/").status_code == 200


def _fake_ai(*providers):
    from types import SimpleNamespace
    return SimpleNamespace(providers=list(providers))


def test_ai_probe_ignores_providers_in_cooldown(monkeypatch):
    """Провайдер с исчерпанным лимитом не считается живым ИИ."""
    from types import SimpleNamespace
    monkeypatch.setattr(botmod, "ai_service", _fake_ai(
        SimpleNamespace(available=False, cooldown_left=300)))

    assert botmod._health_probe_ai() is False
    assert "через ~5 мин" in botmod._ai_pause_note()


def test_ai_probe_ok_with_available_provider(monkeypatch):
    """Хотя бы один провайдер вне паузы — ИИ работает."""
    from types import SimpleNamespace
    monkeypatch.setattr(botmod, "ai_service", _fake_ai(
        SimpleNamespace(available=False, cooldown_left=300),
        SimpleNamespace(available=True, cooldown_left=0)))

    assert botmod._health_probe_ai() is True


def test_ai_probe_without_keys(monkeypatch):
    """Нет ни одного ключа — понятная причина, а не «пауза»."""
    monkeypatch.setattr(botmod, "ai_service", _fake_ai())

    assert botmod._health_probe_ai() is False
    assert "не настроен" in botmod._ai_pause_note()
