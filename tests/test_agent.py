# -*- coding: utf-8 -*-
"""Тесты ИИ-агента: ошибки, отчёты, идеи."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402
from quantum.agent import Agent, _parse_idea  # noqa: E402
from quantum.db import close_db, init_db  # noqa: E402


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, embed=None, **kwargs):
        self.sent.append(embed)
        return None


class _FakeBot:
    def __init__(self, channel=None):
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel


class _FakeProvider:
    name = "Fake"

    async def complete(self, messages):
        return ("ИДЕЯ: Добавить команду /admins\n"
                "ПОЛЬЗА: Видно, кто администрирует клан\n"
                "РЕАЛИЗАЦИЯ: Один embed со списком ролей")


def _agent(channel=None):
    return Agent(_FakeBot(channel or _FakeChannel()), 1556427610160889907)


def test_parse_idea():
    """Ответ ИИ разбирается на идею/пользу/реализацию."""
    parsed = _parse_idea(
        "ИДЕЯ: Сделать отчёт по заявкам\n"
        "ПОЛЬЗА: Админ видит картину\n"
        "РЕАЛИЗАЦИЯ: Считать в БД")
    assert parsed["idea"] == "Сделать отчёт по заявкам"
    assert parsed["benefit"] == "Админ видит картину"
    assert parsed["impl"] == "Считать в БД"
    print("  ok: ответ ИИ разобран")


def test_error_report_goes_to_agent_channel():
    """Ошибка команды уходит в канал агента стилизованным Embed."""
    async def scenario():
        ch = _FakeChannel()
        agent = _agent(ch)
        await agent.report_error("/moders", ValueError("boom"))

        assert len(ch.sent) == 1
        embed = ch.sent[0]
        assert embed.title == "🚨 Ошибка в команде"
        fields = {f.name: f.value for f in embed.fields}
        assert fields["Контекст"] == "`/moders`"
        assert "ValueError" in fields["Тип"]
        assert "boom" in fields["Сообщение"]
        # Ошибка посчитана в статистике.
        assert agent.error_stats["/moders"] == 1

    asyncio.run(scenario())
    print("  ok: ошибка команды отправлена в канал агента")


def test_error_counter_groups_by_command():
    """Счётчик ошибок группируется по команде."""
    agent = _agent()
    agent.record_error("/admins")
    agent.record_error("/admins")
    agent.record_error("/status")
    assert agent.error_stats["/admins"] == 2
    assert agent.error_stats["/status"] == 1
    assert "/admins" in agent.top_errors()
    print("  ok: счётчик ошибок работает")


def test_health_embed_contains_metrics():
    """Отчёт о состоянии содержит основные метрики."""
    async def scenario():
        agent = _agent()
        embed = await agent.build_health_embed(
            {"uptime": "1 ч 2 мин", "ping": "42 мс", "ai": "Gemini",
             "rules": "Пунктов: 10", "github": "ок", "cs2red": "200"})
        fields = {f.name: f.value for f in embed.fields}
        assert embed.title == "🩺 Наблюдение за системой"
        assert fields["⏱️ Аптайм"] == "1 ч 2 мин"
        assert fields["📡 Пинг"] == "42 мс"
        assert fields["🤖 ИИ"] == "Gemini"
        assert fields["🌐 cs2red"] == "200"

    asyncio.run(scenario())
    print("  ok: embed отчёта собран")


def test_idea_embed_from_provider():
    """Идея от ИИ превращается в Embed."""
    async def scenario():
        await init_db()
        try:
            agent = _agent()
            embed = await agent.build_idea_embed([_FakeProvider()], {"links": 10})
            assert embed is not None
            assert embed.title == "💡 Идея от ИИ-агента"
            assert embed.description == "Добавить команду /admins"
            fields = {f.name: f.value for f in embed.fields}
            assert fields["🎯 Польза"] == "Видно, кто администрирует клан"
            assert fields["🛠 Как сделать"] == "Один embed со списком ролей"
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: идея оформлена embed'ом")


def test_idea_history_is_kept():
    """Последние идеи запоминаются, чтобы не повторяться."""
    async def scenario():
        await init_db()
        try:
            agent = _agent()
            await agent.remember_idea("Первая идея")
            await agent.remember_idea("Вторая идея")
            ideas = await agent.known_ideas()
            assert "Первая идея" in ideas
            assert "Вторая идея" in ideas
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: история идей сохраняется")


def test_agents_channel_missing_is_safe():
    """Без канала агент не падает — просто молчит."""
    async def scenario():
        agent = Agent(_FakeBot(None), 1556427610160889907)
        ok = await agent.report_error("/admins", RuntimeError("x"))
        assert ok is False

    asyncio.run(scenario())
    print("  ok: отсутствие канала не ломает агента")


def test_admins_and_status_commands_registered():
    """Команды /admins и /status зарегистрированы."""
    assert botmod.bot.tree.get_command("admins") is not None
    assert botmod.bot.tree.get_command("status") is not None
    print("  ok: команды /admins и /status зарегистрированы")


def test_admins_embed_layout():
    """Embed /admins: три секции + сводка."""
    class _Member:
        def __init__(self, name, mention, bot=False):
            self.display_name = name
            self.mention = mention
            self.bot = bot

    class _Guild:
        name = "TALENT"
        member_count = 120

    guild = _Guild()
    admins_list = [_Member("Админ", "<@1>"), _Member("Бот", "<@2>", bot=True)]
    embed = botmod._build_admins_embed(
        guild, admins_list,
        ["**Мод** — <@3>"], ["<@&4> — **Ревьюер**"])

    assert embed.title == "🛡 Администрация клана"
    assert embed.color.value == 0
    fields = {f.name: f.value for f in embed.fields}
    assert "<@1>" in fields["👑 Администраторы Discord"]
    assert "🤖" in fields["👑 Администраторы Discord"]
    assert "<@3>" in fields["⚙️ Модераторы бота"]
    assert "<@&4>" in fields["🎯 Ревьюеры заявок"]
    assert "Администраторов: **2**" in fields["📊 Сводка"]
    assert "TALENT" in embed.description
    print("  ok: embed /admins собран по формату")


def test_ask_provider_skips_paused_provider():
    """Провайдер в паузе после лимита не вызывается — берём следующий."""
    class _Paused(_FakeProvider):
        name = "Paused"
        available = False

        async def complete(self, messages):  # pragma: no cover - не должен
            raise AssertionError("провайдер в паузе не должен вызываться")

    class _Working(_FakeProvider):
        name = "Working"

    async def scenario():
        text, provider = await _agent().ask_provider(
            [_Paused(), _Working()], "придумай идею")
        assert text and provider == "Working"

    asyncio.run(scenario())
    print("  ok: агент пропускает провайдера в паузе")


def test_idea_prompt_mentions_code_and_project():
    """Промпт идеи содержит структуру кода и знания о проекте."""
    agent = _agent()
    agent.project_context = "Команда /profile показывает войс и сообщения."
    prompt = agent.build_idea_prompt(
        {"links": 10}, [], "script.py (~9500), quantum/ai/service.py (~300)")

    assert "улучшению кода" in prompt
    assert "Структура кода" in prompt
    assert "script.py" in prompt
    assert "/profile" in prompt
    print("  ok: идея генерируется с контекстом кода")


def test_force_idea_posts_and_sets_schedule():
    """Ручная идея публикуется в канал агента и сдвигает расписание."""
    async def scenario():
        await init_db()
        try:
            ch = _FakeChannel()
            agent = _agent(ch)
            embed = await agent.force_idea(
                [_FakeProvider()], {"code_overview": "script.py (~9500)"})

            assert embed is not None
            assert len(ch.sent) == 1
            assert ch.sent[0].title == "💡 Идея от ИИ-агента"
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: ручная идея публикуется")


def test_idea_prompt_rejects_repeat_ideas():
    """Уже предложенные идеи передаются в промпт как запрет на повтор."""
    prompt = _agent().build_idea_prompt(
        {}, ["Добавить /admins", "Сделать отчёт"], "")
    assert "Добавить /admins" in prompt
    assert "не повторяй" in prompt
