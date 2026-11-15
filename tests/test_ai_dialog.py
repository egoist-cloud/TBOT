# -*- coding: utf-8 -*-
"""Тесты памяти диалогов с ИИ (quantum/repositories/ai_dialog.py)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quantum.db import close_db, init_db  # noqa: E402
from quantum.repositories import ai_dialog as repo  # noqa: E402


def test_dialog_keeps_order_and_limit():
    """История отдаётся в хронологическом порядке и не длиннее лимита."""
    async def scenario():
        await init_db()
        try:
            for i in range(5):
                await repo.add_message(930001, "user", f"вопрос {i}")
                await repo.add_message(930001, "assistant", f"ответ {i}")

            history = await repo.get_history(930001, limit=4)
            assert [h["role"] for h in history] == \
                ["user", "assistant", "user", "assistant"]
            assert history[-1]["content"] == "ответ 4"
            assert history[0]["content"] == "вопрос 3"
            print("  ok: история в правильном порядке")
        finally:
            await close_db()

    asyncio.run(scenario())


def test_dialog_trim_and_clear():
    """Старые реплики подрезаются, clear удаляет диалог игрока."""
    async def scenario():
        await init_db()
        try:
            for i in range(repo.KEEP_MESSAGES + 10):
                await repo.add_message(930002, "user", f"m{i}")
            total = await repo.count(930002)
            assert total == repo.KEEP_MESSAGES, total

            removed = await repo.clear(930002)
            assert removed == repo.KEEP_MESSAGES
            assert await repo.get_history(930002) == []
            print("  ok: подрезка и очистка диалога")
        finally:
            await close_db()

    asyncio.run(scenario())


def test_dialog_ignores_junk():
    """Пустой текст и чужие роли не сохраняются."""
    async def scenario():
        await init_db()
        try:
            before = await repo.count(930003)
            await repo.add_message(930003, "user", "   ")
            await repo.add_message(930003, "system", "служебное")
            assert await repo.count(930003) == before
            print("  ok: мусор не пишется в диалог")
        finally:
            await close_db()

    asyncio.run(scenario())
