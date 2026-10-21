# -*- coding: utf-8 -*-
"""Проверка логики голосования: атомарность и взаимоисключение голосов."""
import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.db import close_db, init_db  # noqa: E402
from quantum.repositories import votes as votes_repo  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def test_apply_vote_counts_and_persists():
    """Голос увеличивает счётчик и сохраняется в БД."""
    async def scenario():
        await init_db()
        st, applied = await votes_repo.apply_vote("app-1", "accept", 111)
        assert applied is True
        assert st["accept"] == 1
        assert st["deny"] == 0
        assert "111" in st["accept_voters"]
        # Читаем из БД — состояние должно сохраниться.
        again = await votes_repo.get_votes("app-1")
        assert again["accept"] == 1
        assert again["accept_voters"] == ["111"]
        await close_db()

    _run(scenario())
    print("  ok: голос засчитан и сохранён")


def test_double_vote_rejected():
    """Второй голос того же человека не засчитывается."""
    async def scenario():
        await init_db()
        await votes_repo.apply_vote("app-2", "accept", 222)
        st, applied = await votes_repo.apply_vote("app-2", "accept", 222)
        assert applied is False
        assert st["accept"] == 1
        await close_db()

    _run(scenario())
    print("  ok: повторный голос отклонён")


def test_votes_are_mutually_exclusive():
    """Один человек не может быть одновременно «за» и «против»."""
    async def scenario():
        await init_db()
        await votes_repo.apply_vote("app-3", "accept", 333)
        st, applied = await votes_repo.apply_vote("app-3", "deny", 333)
        assert applied is True
        assert st["accept"] == 0
        assert st["deny"] == 1
        assert "333" not in st["accept_voters"]
        assert "333" in st["deny_voters"]
        await close_db()

    _run(scenario())
    print("  ok: голоса взаимоисключающие")


def test_concurrent_votes_are_not_lost():
    """Параллельные голоса не затирают друг друга.

    Без лока два голоса читали бы одно состояние, и один из них
    потерялся бы (счётчик 1 вместо 2).
    """
    async def scenario():
        await init_db()
        results = await asyncio.gather(*[
            votes_repo.apply_vote("app-4", "accept", uid)
            for uid in range(10)
        ])
        assert all(applied for _, applied in results), "голос был отклонён"
        st = await votes_repo.get_votes("app-4")
        assert st["accept"] == 10, f"потеряны голосы: {st['accept']} != 10"
        assert len(st["accept_voters"]) == 10
        await close_db()

    _run(scenario())
    print("  ok: параллельные голоса не теряются")


def test_allow_repeat_for_spam_vote_role():
    """С ролью SPAM_VOTE_ROLE_ID можно голосовать повторно."""
    async def scenario():
        await init_db()
        await votes_repo.apply_vote("app-5", "accept", 444, allow_repeat=True)
        st, applied = await votes_repo.apply_vote("app-5", "accept", 444,
                                                  allow_repeat=True)
        assert applied is True
        assert st["accept"] == 2
        # В списке избирателей пользователь один раз.
        assert st["accept_voters"] == ["444"]
        await close_db()

    _run(scenario())
    print("  ok: повторный голос разрешён для спам-роли")


def test_votes_of_different_applications_are_separate():
    """Разные заявки не смешивают голоса (это баг двух сообщений)."""
    async def scenario():
        await init_db()
        await votes_repo.apply_vote("app-6", "accept", 555)
        st = await votes_repo.get_votes("app-7")
        assert st["accept"] == 0
        assert st["voters"] == []
        await close_db()

    _run(scenario())
    print("  ok: голоса разных заявок не смешиваются")


def test_migration_adds_new_columns_to_old_db():
    """Существующая БД получает новые колонки (accept_voters и app_id)."""
    import tempfile

    import quantum.db as qdb

    async def scenario():
        await qdb.close_db()
        # Отдельная БД со СТАРОЙ схемой, чтобы не портить рабочие данные.
        tmp_dir = Path(tempfile.mkdtemp(prefix="quantum-migrate-"))
        old_db = tmp_dir / "quantum.db"
        conn = sqlite3.connect(str(old_db))
        conn.executescript(
            """
            CREATE TABLE votes (
                message_id TEXT PRIMARY KEY,
                accept     INTEGER NOT NULL DEFAULT 0,
                deny       INTEGER NOT NULL DEFAULT 0,
                voters     TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE applications (
                user_id    TEXT PRIMARY KEY,
                status     TEXT NOT NULL DEFAULT 'pending',
                clan_name  TEXT,
                message_id TEXT,
                channel_id TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        conn.commit()
        conn.close()

        saved_path = qdb.DB_PATH
        qdb.DB_PATH = old_db
        try:
            await qdb.init_db()
            db = await qdb.get_db()

            cur = await db.execute("PRAGMA table_info(votes)")
            cols = {r["name"] for r in await cur.fetchall()}
            await cur.close()
            assert "accept_voters" in cols, "миграция не добавила accept_voters"
            assert "deny_voters" in cols, "миграция не добавила deny_voters"

            cur = await db.execute("PRAGMA table_info(applications)")
            acols = {r["name"] for r in await cur.fetchall()}
            await cur.close()
            assert "app_id" in acols, "миграция не добавила applications.app_id"

            # Мигрированная БД должна работать: голос пишется и читается.
            await votes_repo.apply_vote("legacy-1", "accept", 666)
            st = await votes_repo.get_votes("legacy-1")
            assert st["accept"] == 1
            assert st["accept_voters"] == ["666"]
        finally:
            await qdb.close_db()
            qdb.DB_PATH = saved_path

    try:
        _run(scenario())
    finally:
        _run(qdb.close_db())
    print("  ok: миграция старой БД проходит, голоса работают")


def test_blank_state_shape():
    """Пустое состояние содержит все ожидаемые ключи."""
    st = votes_repo.blank_state()
    assert st == {"accept": 0, "deny": 0, "voters": [],
                  "accept_voters": [], "deny_voters": []}
    print("  ok: форма пустого состояния")


def test_app_id_helpers():
    """app_id достаётся из custom_id кнопки, fallback — id сообщения."""
    app_id = botmod._new_app_id()
    assert app_id and ":" not in app_id
    assert len(app_id) >= 8

    class _Btn:
        custom_id = f"accept_btn:{app_id}"

    assert botmod._app_id_of(_Btn()) == app_id

    class _Btn2:
        custom_id = "accept_btn"

    assert botmod._app_id_of(_Btn2()) == ""

    class _Msg:
        id = 1234567890123456789

    class _Int:
        message = _Msg()

    assert botmod._app_id_for(_Int(), _Btn2()) == "1234567890123456789"
    assert botmod._app_id_for(_Int(), _Btn()) == app_id
    print("  ok: app_id разбирается корректно")
