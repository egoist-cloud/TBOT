# -*- coding: utf-8 -*-
"""Тесты новых возможностей: /clan_info, health-check, ежедневная сводка,
кнопки заявок и авто-выдача роли при принятии."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import script as botmod  # noqa: E402


def test_ping_roles_are_the_three_requested():
    """Роли для ветки заявки — ровно те три id."""
    assert set(botmod.APPLICATION_PING_ROLE_IDS) == {
        1447953747161387131, 1524071237796626523, 1378326630031818802}


def test_gemini_default_model_is_not_removed():
    """Модель Gemini по умолчанию существует (gemini-1.5-* удалены Google)."""
    from quantum.ai.providers import GeminiProvider
    assert GeminiProvider.default_model != "gemini-1.5-flash"
    assert GeminiProvider.default_model.startswith("gemini-")
    assert "1.5" not in GeminiProvider.default_model


def test_llm7_model_is_separate_from_gemini(monkeypatch):
    """У LLM7 своя переменная модели: AI_MODEL с именем Gemini его не ломает."""
    import aiohttp
    from quantum.ai.providers import GeminiProvider, LLM7Provider

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("LLM7_API_KEY", "k")
    monkeypatch.setenv("LLM7_MODEL", "")
    monkeypatch.setenv("GEMINI_MODEL", "")
    monkeypatch.setenv("AI_MODEL", "gemini-2.5-flash")

    async def scenario():
        async with aiohttp.ClientSession() as s:
            assert GeminiProvider(s)._model == "gemini-2.5-flash"
            # LLM7 игнорирует AI_MODEL и берёт свой дефолт-селектор.
            assert LLM7Provider(s)._model == "default"

    asyncio.run(scenario())


def test_gemini_ignores_non_gemini_ai_model(monkeypatch):
    """Старый общий AI_MODEL=default не должен уходить в Gemini (404)."""
    import aiohttp
    from quantum.ai.providers import GeminiProvider

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "")
    monkeypatch.setenv("AI_MODEL", "default")

    async def scenario():
        async with aiohttp.ClientSession() as s:
            assert GeminiProvider(s)._model == GeminiProvider.default_model

    asyncio.run(scenario())


def test_accept_role_id_default():
    """При принятии заявки выдаётся роль клана 1448368118190837800."""
    assert botmod.ACCEPT_ROLE_ID == 1448368118190837800


def test_new_tasks_exist():
    """health_watch и daily_summary зарегистрированы как фоновые задачи."""
    assert hasattr(botmod, "health_watch")
    assert hasattr(botmod, "daily_summary")
    assert hasattr(botmod, "_keep_only_applicant_and_taker")


def test_clan_info_registered():
    """Слэш-команда /clan_info зарегистрирована, ответ ephemeral."""
    cmd = botmod.bot.tree.get_command("clan_info")
    assert cmd is not None, "/clan_info не зарегистрирован"


def test_bans_view_separate_from_management():
    """Кнопки запретов и кнопки решения — в разных view."""
    mgmt = {c.custom_id for c in botmod.ApplicationManagement().children}
    bans = {c.custom_id for c in botmod.ApplicationBansView().children}
    assert not (mgmt & bans), "наборы кнопок не должны пересекаться"
    assert "accept_btn" in mgmt and "deny_btn" in mgmt and "take_btn" in mgmt
    assert "tempban_btn" in bans and "permban_btn" in bans and "unban_btn" in bans


class _FakeRole:
    def __init__(self, role_id, name, position=1):
        self.id = role_id
        self.name = name
        self.position = position


class _FakeMember:
    def __init__(self, member_id, roles=()):
        self.id = member_id
        self.display_name = f"u{member_id}"
        self.roles = list(roles)
        self.bot = False


class _FakeGuild:
    def __init__(self, roles, members):
        self.roles = list(roles)
        self.members = list(members)

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)


def test_clan_info_embed_builds(monkeypatch):
    """_build_clan_info_embed собирает embed даже без данных cs2red."""
    async def fake_clans():
        return [{"name": botmod.CLAN_NAME, "tag": "T",
                 "members": [{"userId": 1}]}]

    async def fake_links():
        return {}

    async def fake_user(_sid):
        return {"id": 7, "nick": "Miki"}

    async def fake_checked(_sid, user=None, clans=None):
        return {"clan_name": botmod.CLAN_NAME}, ""

    monkeypatch.setattr(botmod.api, "cs2red_clans", fake_clans)
    monkeypatch.setattr(botmod.api, "cs2red_user", fake_user)
    monkeypatch.setattr(botmod.api, "player_clan_checked", fake_checked)
    monkeypatch.setattr(botmod.links_repo, "get_all_links", fake_links)
    monkeypatch.setattr(botmod.links_repo, "get_link",
                        lambda _mid: _async_value("76561198000000000"))

    talent = _FakeRole(botmod.TALENT_ROLE_ID, botmod.CLAN_NAME)
    guild = _FakeGuild([talent], [_FakeMember(1, [talent]), _FakeMember(2, [talent])])
    embed = asyncio.run(botmod._build_clan_info_embed(guild))
    assert embed.color.value == 0x000000
    assert "Quantum" in embed.footer.text
    names = " ".join(f.name for f in embed.fields)
    assert "Состав на cs2red" in names
    assert "С ролью в Discord" in names


async def _async_value(v):
    return v


def test_daily_summary_embed_builds(monkeypatch):
    """_build_daily_summary_embed собирает embed без падений."""
    async def fake_pending():
        return []

    async def fake_links():
        return {}

    monkeypatch.setattr(botmod.applications_repo, "list_pending_applications",
                        fake_pending)
    monkeypatch.setattr(botmod.links_repo, "get_all_links", fake_links)
    monkeypatch.setattr(botmod, "GUILD_ID", 0)

    embed = asyncio.run(botmod._build_daily_summary_embed())
    assert embed.color.value == 0x000000
    names = " ".join(f.name for f in embed.fields)
    assert "Заявок в работе" in names
