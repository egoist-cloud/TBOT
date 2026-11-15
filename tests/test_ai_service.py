# -*- coding: utf-8 -*-
"""Тесты AIService (quantum/ai/service.py).

Сначала чистые функции (сборка промпта, примеры из ai_training.json), затем
ask() с фейковыми провайдерами: кэш, лимит, каскад и фолбэк при ошибке.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from quantum.ai import cache as ai_cache  # noqa: E402
from quantum.ai.service import (  # noqa: E402
    AIService,
    _build_messages,
    _default_data_dir,
)
from quantum.ai.limiter import RateLimiter  # noqa: E402
from quantum.db import close_db, init_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class _FakeProvider:
    """Провайдер-заглушка: либо ответ, либо исключение."""

    def __init__(self, name, answer=None, error=None, available=True):
        self.name = name
        self._answer = answer
        self._error = error
        self.available = available
        self.calls = 0
        self.messages: list = []

    async def complete(self, messages):
        self.calls += 1
        self.messages.append(messages)
        if self._error is not None:
            raise self._error
        return self._answer


def test_build_messages_without_examples():
    """Без примеров промпт — ровно два сообщения: system + user."""
    messages = _build_messages("что за читы?", "ПУНКТ 2.2: про читы", "")
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "TALENT" in messages[0]["content"]
    assert "ПУНКТ 2.2" in messages[1]["content"]
    assert "что за читы?" in messages[1]["content"]
    assert "ПРИМЕРЫ ОТВЕТОВ" not in messages[1]["content"]
    print("  ok: промпт без примеров")


def test_build_messages_with_examples():
    """Примеры ответов добавляются отдельным блоком."""
    messages = _build_messages("вопрос", "правила", "Вопрос: a\nОтвет: b")
    user = messages[1]["content"]
    assert "ПРИМЕРЫ ОТВЕТОВ" in user
    assert "Вопрос: a" in user
    print("  ok: промпт с примерами")


def test_default_data_dir_uses_env(monkeypatch):
    """Каталог данных берётся из DATA_DIR, иначе — текущий."""
    monkeypatch.setenv("DATA_DIR", "/tmp/quantum-data")
    assert _default_data_dir() == "/tmp/quantum-data"
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert _default_data_dir() == "."
    print("  ok: DATA_DIR по умолчанию")


def test_load_examples_missing_and_broken(tmp_path):
    """Нет файла / битый JSON / не список — пустая строка, без исключений."""
    async def scenario():
        async with aiohttp.ClientSession() as session:
            service = AIService(session, data_dir=str(tmp_path))
            assert service._load_examples() == ""          # файла нет

            (tmp_path / "ai_training.json").write_text("{не json", encoding="utf-8")
            assert service._load_examples() == ""          # битый JSON

            (tmp_path / "ai_training.json").write_text('{"question": "a"}', encoding="utf-8")
            assert service._load_examples() == ""          # не список

    asyncio.run(scenario())
    print("  ok: примеры не ломают сервис")


def test_load_examples_skips_junk_items(tmp_path):
    """Строки вместо объектов и пары без ответа пропускаются."""
    items = [
        "строка вместо объекта",
        {"question": "без ответа"},
        {"answer": "без вопроса"},
        {"question": "q0", "answer": "a0"},
        {"question": "q1", "answer": "a1"},
    ]
    (tmp_path / "ai_training.json").write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8")

    async def scenario():
        async with aiohttp.ClientSession() as session:
            service = AIService(session, data_dir=str(tmp_path))
            examples = service._load_examples()
            assert examples.count("Вопрос:") == 2
            assert "Вопрос: q0" in examples and "Ответ: a1" in examples
            assert "без ответа" not in examples

    asyncio.run(scenario())
    print("  ok: мусорные примеры пропущены")


def test_load_examples_takes_first_six(tmp_path):
    """Обрезка идёт по первым 6 элементам файла (data[:6])."""
    items = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(9)]
    (tmp_path / "ai_training.json").write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8")

    async def scenario():
        async with aiohttp.ClientSession() as session:
            service = AIService(session, data_dir=str(tmp_path))
            examples = service._load_examples()
            assert examples.count("Вопрос:") == 6
            assert "Вопрос: q5" in examples
            assert "Вопрос: q6" not in examples

    asyncio.run(scenario())
    print("  ok: примеры обрезаны до 6")


def test_ensure_rules_loads_once():
    """ensure_rules грузит правила один раз, дальше отдаёт число пунктов."""
    async def scenario():
        async with aiohttp.ClientSession() as session:
            service = AIService(session, data_dir=str(ROOT))
            first = service.ensure_rules()
            assert first > 0
            assert service.ensure_rules() == first

    asyncio.run(scenario())
    print("  ok: правила загружаются один раз")


def test_ask_rejects_empty_query():
    """Пустой запрос отсекается до обращения к БД и провайдерам."""
    async def scenario():
        async with aiohttp.ClientSession() as session:
            service = AIService(session, data_dir=str(ROOT))
            result = await service.ask(1, "   ")
            assert result["ok"] is False
            assert result["error"] == "пустой запрос"
            assert result["provider"] == ""

    asyncio.run(scenario())
    print("  ok: пустой запрос отклонён")


def test_ask_returns_cached_answer():
    """Ответ из кэша отдаётся без вызова провайдеров."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                service.providers = []
                await ai_cache.set_cached("вопрос для кэша", "готовый ответ")

                result = await service.ask(2, "вопрос для кэша")
                assert result["ok"] is True
                assert result["cached"] is True
                assert result["provider"] == "cache"
                assert result["response"] == "готовый ответ"
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: кэш отвечает без провайдеров")


def test_ask_respects_limit():
    """Исчерпанный лимит даёт limited=True и понятную ошибку."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                service.limiter = RateLimiter(max_per_hour=0)
                result = await service.ask(3, "вопрос сверх лимита")
                assert result["ok"] is False
                assert result["limited"] is True
                assert result["error"] == "лимит запросов исчерпан"
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: лимит уважается")


def test_ask_without_providers_reports_error():
    """Если ни один провайдер не настроен — ошибка с подсказкой про ключи."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                service.providers = []
                result = await service.ask(4, "вопрос без провайдеров")
                assert result["ok"] is False
                assert "Не удалось получить ответ" in result["error"]
                assert isinstance(result["rules_used"], list)
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: без провайдеров — ошибка, а не падение")


def test_ask_caches_successful_answer():
    """Успешный ответ провайдера возвращается и кладётся в кэш."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                provider = _FakeProvider("Fake", answer="ответ провайдера")
                service.providers = [provider]

                result = await service.ask(5, "уникальный вопрос про VIP 7.1")
                assert result["ok"] is True
                assert result["provider"] == "Fake"
                assert result["response"] == "ответ провайдера"
                assert result["cached"] is False
                assert provider.calls == 1
                assert await ai_cache.get_cached("уникальный вопрос про VIP 7.1") == \
                    "ответ провайдера"
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: успешный ответ закэширован")


def test_ask_falls_back_to_next_provider():
    """Упавший провайдер пропускается, отвечает следующий по каскаду."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                broken = _FakeProvider("Broken", error=RuntimeError("429"))
                empty = _FakeProvider("Empty", answer=None)
                working = _FakeProvider("Working", answer="ответ третьего")
                service.providers = [broken, empty, working]

                result = await service.ask(6, "вопрос для каскада")
                assert result["ok"] is True
                assert result["provider"] == "Working"
                assert broken.calls == 1 and empty.calls == 1 and working.calls == 1
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: каскад переживает ошибку провайдера")


def test_ask_without_rule_hits_uses_general_mode():
    """Если RAG ничего не нашёл, вопрос идёт в разговорном режиме."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                service.rules.search = lambda query, top_k=3: []
                provider = _FakeProvider("Fake", answer="ответ без правил")
                service.providers = [provider]

                result = await service.ask(7, "вопрос без совпадений в правилах")
                assert result["ok"] is True
                assert result["rules_used"] == []
                system = provider.messages[0][0]["content"]
                user_prompt = provider.messages[0][1]["content"]
                assert "ИИ-помощник Discord-бота" in system
                assert user_prompt.startswith("ВОПРОС:")
                assert "ПРАВИЛА" not in user_prompt
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: пустой RAG даёт заглушку в промпте")


# ============================================================
# История диалога
# ============================================================
def test_ask_passes_history_to_provider():
    """История реплик идёт в промпт между system и новым вопросом."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                provider = _FakeProvider("Fake", answer="ответ с контекстом")
                service.providers = [provider]
                history = [
                    {"role": "user", "content": "первый вопрос"},
                    {"role": "assistant", "content": "первый ответ"},
                ]

                result = await service.ask(21, "а за это что?", history=history)
                assert result["ok"] is True
                messages = provider.messages[0]
                assert [m["role"] for m in messages] == \
                    ["system", "user", "assistant", "user"]
                assert messages[1]["content"] == "первый вопрос"
                assert messages[2]["content"] == "первый ответ"
                assert "а за это что?" in messages[3]["content"]
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: история диалога попадает в промпт")


def test_ask_with_history_skips_cache():
    """Ответы с контекстом диалога не кэшируются (они персональные)."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                provider = _FakeProvider("Fake", answer="уникальный ответ 42")
                service.providers = [provider]
                question = "уникальный вопрос про диалог 42"

                result = await service.ask(
                    22, question,
                    history=[{"role": "user", "content": "до этого"}])
                assert result["ok"] is True
                assert await ai_cache.get_cached(question) is None
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: с историей кэш не используется")


# ============================================================
# Пауза провайдеров
# ============================================================
def test_ask_skips_provider_in_cooldown():
    """Провайдер в паузе пропускается, отвечает следующий."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                limited = _FakeProvider("Limited", answer="не должен", available=False)
                working = _FakeProvider("Working", answer="ответ второго")
                service.providers = [limited, working]

                result = await service.ask(23, "вопрос после лимита 7.2")
                assert result["ok"] is True
                assert result["provider"] == "Working"
                assert limited.calls == 0
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: провайдер в паузе пропущен")


def test_ask_all_providers_in_cooldown_message():
    """Если все в паузе — понятное сообщение, а не общая ошибка ключей."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                service.providers = [
                    _FakeProvider("A", available=False),
                    _FakeProvider("B", available=False),
                ]
                result = await service.ask(24, "все лимиты кончились")
                assert result["ok"] is False
                assert "паузе" in result["error"]
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: все в паузе — понятная ошибка")


# ============================================================
# База знаний проекта
# ============================================================
def test_project_context_loaded_and_used(tmp_path):
    """ai_project.md попадает в system-промпт."""
    (tmp_path / "ai_project.md").write_text(
        "Команда /profile показывает войс и сообщения.", encoding="utf-8")

    async def scenario():
        async with aiohttp.ClientSession() as session:
            service = AIService(session, data_dir=str(tmp_path))
            assert "войс" in service.project_context
            provider = _FakeProvider("Fake", answer="ok")
            service.providers = [provider]
            await service.ask(25, "что показывает /profile?")
            system = provider.messages[0][0]["content"]
            assert "ИНФОРМАЦИЯ О ПРОЕКТЕ" in system
            assert "/profile" in system

    asyncio.run(scenario())
    print("  ok: знания проекта в промпте")


def test_project_context_falls_back_to_project_root(tmp_path):
    """Если ai_project.md нет в DATA_DIR, берём файл из корня проекта."""
    async def scenario():
        async with aiohttp.ClientSession() as session:
            service = AIService(session, data_dir=str(tmp_path))
            assert "TALENT" in service.project_context
            print("  ok: знания проекта найдены в корне репозитория")

    asyncio.run(scenario())


def test_build_messages_general_mode():
    """Без найденных правил — разговорный промпт без формата полей."""
    messages = _build_messages(
        "что умеет /profile?", "Релевантные пункты не найдены.", "",
        project_context="Команда /profile показывает войс.", general=True)
    system = messages[0]["content"]
    assert "ОТВЕТ/НАКАЗАНИЕ/ОСНОВАНИЕ" in system
    assert "ИНФОРМАЦИЯ О ПРОЕКТЕ" in system
    assert "ВОПРОС: что умеет /profile?" in messages[-1]["content"]
    assert "ПРАВИЛА" not in messages[-1]["content"]
    print("  ok: разговорный режим промпта")


def test_ask_without_rules_uses_general_prompt():
    """Вопрос не по правилам уходит в разговорный режим."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                service.rules.search = lambda query, top_k=3: []
                provider = _FakeProvider("Fake", answer="просто ответ")
                service.providers = [provider]
                result = await service.ask(26, "привет, как дела у бота?")
                assert result["ok"] is True
                system = provider.messages[0][0]["content"]
                assert "ИИ-помощник Discord-бота" in system
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: вопрос не по правилам — разговорный режим")


def test_failed_request_returns_quota():
    """Если ни один провайдер не ответил, попытка пользователя возвращается."""
    async def scenario():
        await init_db()
        try:
            async with aiohttp.ClientSession() as session:
                service = AIService(session, data_dir=str(ROOT))
                service.limiter = RateLimiter(max_per_hour=1)
                service.providers = [_FakeProvider("A", available=False)]
                first = await service.ask(27, "запрос без ответа")
                assert first["ok"] is False
                assert service.limiter.remaining(27) == 1
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: неудачный запрос не съедает лимит")
