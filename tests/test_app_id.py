# -*- coding: utf-8 -*-
"""Проверка единого app_id заявки: кнопки, реестр persistent-view, голоса."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, init_db  # noqa: E402
from quantum.repositories import votes as votes_repo  # noqa: E402


def _custom_ids(view):
    return {c.custom_id for c in view.children}


def test_app_id_is_embedded_into_button_custom_ids():
    """app_id попадает в custom_id кнопок заявки."""
    app_id = botmod._new_app_id()
    view = botmod.ApplicationManagement(app_id)
    ids = _custom_ids(view)
    assert f"accept_btn:{app_id}" in ids
    assert f"deny_btn:{app_id}" in ids
    assert f"voted_list_btn:{app_id}" in ids
    assert f"take_btn:{app_id}" in ids
    print("  ok: app_id зашит в custom_id кнопок")


def test_legacy_view_keeps_static_custom_ids():
    """Старая заявка (без app_id) работает по id сообщения."""
    view = botmod.ApplicationManagement()
    ids = _custom_ids(view)
    assert "accept_btn" in ids
    assert "deny_btn" in ids
    print("  ok: legacy-view сохраняет статические custom_id")


def test_two_applications_do_not_conflict():
    """Две заявки дают разные custom_id — конфликта в реестре нет."""
    v1 = botmod.ApplicationManagement("aaaaaaaaaa")
    v2 = botmod.ApplicationManagement("bbbbbbbbbb")
    assert _custom_ids(v1) != _custom_ids(v2)
    assert not (_custom_ids(v1) & _custom_ids(v2))
    print("  ok: две заявки не конфликтуют по custom_id")


def test_instance_mutation_does_not_affect_class():
    """Правка custom_id в экземпляре не трогает кнопки класса."""
    botmod.ApplicationManagement("zzzzzzzzzz")
    fresh = botmod.ApplicationManagement()
    assert "accept_btn" in _custom_ids(fresh), "класс был изменён через экземпляр"
    print("  ok: экземпляр не влияет на класс")


def test_channel_and_thread_share_one_vote_record():
    """Сообщение в канале и в ветке делят одни голоса (app_id)."""
    async def scenario():
        await init_db()
        app_id = botmod._new_app_id()

        # Голос «за» в канале.
        await votes_repo.apply_vote(app_id, "accept", 111)
        # Голос «против» в ветке — та же заявка, тот же app_id.
        st, applied = await votes_repo.apply_vote(app_id, "deny", 222)

        assert applied is True
        assert st["accept"] == 1
        assert st["deny"] == 1
        # Два голоса в одной записи: баг «двух сообщений» устранён.
        assert len(st["voters"]) == 2
        await close_db()

    asyncio.run(scenario())
    print("  ok: канал и ветка делят одну запись голосов")


def test_update_labels_uses_app_id():
    """Счётчики на кнопках берутся из записи по app_id."""
    async def scenario():
        await init_db()
        app_id = botmod._new_app_id()
        await votes_repo.apply_vote(app_id, "accept", 333)

        view = botmod.ApplicationManagement(app_id)
        await view.update_labels()

        labels = {c.custom_id.split(":", 1)[0]: c.label for c in view.children}
        assert labels["accept_btn"].startswith("Принять 1/")
        assert labels["deny_btn"].startswith("Отклонить 0/")
        assert labels["voted_list_btn"].startswith("👥 1")
        await close_db()

    asyncio.run(scenario())
    print("  ok: счётчики на кнопках считаются по app_id")
