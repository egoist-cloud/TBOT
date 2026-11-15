# -*- coding: utf-8 -*-
"""Тесты автообновления панели заявки и автоотклонения старых заявок.

Панель с условиями и выбором клана пересоздаётся раз в
CLAN_PANEL_REFRESH_HOURS: старое сообщение удаляется, новое публикуется
с чистым view. Заявки без решения старше APPLICATION_TTL_DAYS закрываются
автоматически — иначе они навсегда блокируют игроку новую подачу.
"""
import asyncio
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

import script as botmod  # noqa: E402
from quantum.db import close_db, get_db, init_db  # noqa: E402
from quantum.repositories import applications as applications_repo  # noqa: E402
from quantum.repositories import seen as seen_repo  # noqa: E402


class _FakeMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _FakeChannel:
    def __init__(self, channel_id, old_message=None):
        self.id = channel_id
        self._old = old_message
        self.sent = []

    async def fetch_message(self, message_id):
        if self._old is None:
            raise botmod.discord.NotFound(
                type("R", (), {"status": 404, "reason": "Not Found"})(),
                "unknown message")
        return self._old

    async def send(self, *args, **kwargs):
        msg = _FakeMessage(900000 + len(self.sent))
        self.sent.append({"message": msg, "args": args, "kwargs": kwargs})
        return msg


class _FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        if self.channel is None:
            return None
        return self.channel if channel_id == self.channel.id else None


def _http_response(status, reason):
    return type("R", (), {"status": status, "reason": reason})()


def _forbidden():
    return botmod.discord.Forbidden(
        _http_response(403, "Forbidden"), "Missing Permissions")


class _NamedChannel(_FakeChannel):
    """Канал с именем: переименование не должно мешать обновлению."""

    def __init__(self, channel_id, old_message, name):
        super().__init__(channel_id, old_message)
        self.name = name


class _ForbiddenDeleteMessage(_FakeMessage):
    """Старое сообщение есть, но удалить его бот не может."""

    async def delete(self):
        raise _forbidden()


class _ForbiddenSendChannel(_FakeChannel):
    """Публиковать в канал бот не может."""

    async def send(self, *args, **kwargs):
        raise _forbidden()


async def _seed(user_id, app_id, status="pending"):
    await applications_repo.upsert_application(
        user_id, clan_name="TALENT", status=status,
        message_id=f"msg-{app_id}", channel_id="chan", app_id=app_id,
    )


async def _reset_panel_flags():
    """Убирает флаги панели: БД в тестах общая на всю сессию."""
    await seen_repo.set_flag("clan_panel_channel", "")
    await seen_repo.set_flag("clan_panel_message", "")
    await seen_repo.set_flag("clan_panel_at", "")


async def _set_created_at(user_id, when):
    db = await get_db()
    await db.execute("UPDATE applications SET created_at = ? WHERE user_id = ?",
                     (when.isoformat(), str(user_id)))
    await db.commit()


# ============================================================
# Панель: публикация и пересоздание
# ============================================================
def test_refresh_panel_without_publication():
    """Если панель ни разу не публиковали — ничего не делаем."""
    async def scenario():
        await init_db()
        try:
            await seen_repo.set_flag("clan_panel_channel", "")
            bot = _FakeBot(_FakeChannel(555))
            status = await botmod.refresh_clan_panel(bot)
            assert "не настроена" in status, status
            assert "!setup_clan" in status, status
            assert bot.channel.sent == []
        finally:
            await _reset_panel_flags()
            await close_db()

    asyncio.run(scenario())
    print("  ok: панель без публикации не трогается")


def test_refresh_panel_replaces_message():
    """Старое сообщение удаляется, новое уходит с embed и view, id сохранён."""
    async def scenario():
        await init_db()
        try:
            old = _FakeMessage(111)
            channel = _FakeChannel(555, old_message=old)
            bot = _FakeBot(channel)

            await seen_repo.set_flag("clan_panel_channel", "555")
            await seen_repo.set_flag("clan_panel_message", "111")
            await seen_repo.set_flag("clan_panel_at", "1")  # очень старая панель

            status = await botmod.refresh_clan_panel(bot)
            assert status == "панель обновлена"
            assert old.deleted is True, "старое сообщение должно быть удалено"
            assert len(channel.sent) == 1

            sent = channel.sent[0]
            kwargs = sent["kwargs"]
            # Панель теперь уходит контейнером Components V2 (без embed).
            assert isinstance(kwargs.get("view"), discord.ui.LayoutView)
            texts = " ".join(
                item.content for item in kwargs["view"].walk_children()
                if isinstance(item, discord.ui.TextDisplay))
            assert "Официальные условия" in texts

            assert await seen_repo.get_flag("clan_panel_message") == \
                str(sent["message"].id)
            assert float(await seen_repo.get_flag("clan_panel_at")) > 1
        finally:
            await _reset_panel_flags()
            await close_db()

    asyncio.run(scenario())
    print("  ok: панель пересоздаётся (delete + publish)")


def test_refresh_panel_survives_missing_message():
    """Удалённое вручную сообщение не мешает опубликовать новое."""
    async def scenario():
        await init_db()
        try:
            channel = _FakeChannel(556, old_message=None)  # fetch -> NotFound
            bot = _FakeBot(channel)
            await seen_repo.set_flag("clan_panel_channel", "556")
            await seen_repo.set_flag("clan_panel_message", "222")

            status = await botmod.refresh_clan_panel(bot)
            assert status == "панель обновлена"
            assert len(channel.sent) == 1
        finally:
            await _reset_panel_flags()
            await close_db()

    asyncio.run(scenario())
    print("  ok: отсутствующее старое сообщение не ломает обновление")


# ============================================================
# Автоотклонение старых заявок
# ============================================================
def test_expire_stale_applications_closes_only_old():
    """Заявка старше срока закрывается с причиной, свежая остаётся pending."""
    async def scenario():
        await init_db()
        try:
            await _seed(93101, "exp-old", "pending")
            await _seed(93102, "exp-fresh", "pending")
            old_date = botmod.now_utc() - datetime.timedelta(days=10)
            await _set_created_at(93101, old_date)

            closed = await botmod.expire_stale_applications(days=7)
            assert "93101" in closed
            assert "93102" not in closed

            db = await get_db()
            cursor = await db.execute(
                "SELECT status, decision_reason FROM applications "
                "WHERE user_id = ?", ("93101",))
            row = await cursor.fetchone()
            await cursor.close()
            assert row["status"] == "rejected"
            assert "Автоотклонение" in row["decision_reason"]

            cursor = await db.execute(
                "SELECT status FROM applications WHERE user_id = ?", ("93102",))
            row = await cursor.fetchone()
            await cursor.close()
            assert row["status"] == "pending"

            history = await applications_repo.get_user_history(93101)
            assert history[0]["status"] == "rejected"
            assert "Автоотклонение" in history[0]["decision_reason"]

            # Второй прогон уже нечего закрывать — CAS вернул False.
            assert "93101" not in await botmod.expire_stale_applications(days=7)
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: просроченная заявка закрывается один раз")


def test_expire_does_not_touch_voting():
    """Правило распространяется на voting: оно тоже «в работе»."""
    async def scenario():
        await init_db()
        try:
            await _seed(93111, "exp-voting", "voting")
            await _set_created_at(
                93111, botmod.now_utc() - datetime.timedelta(days=30))

            closed = await botmod.expire_stale_applications(days=7)
            assert "93111" in closed
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: voting-заявка тоже истекает")


def test_expire_disabled_with_zero_days():
    """APPLICATION_TTL_DAYS=0 отключает автоотклонение."""
    async def scenario():
        await init_db()
        try:
            await _seed(93121, "exp-off", "pending")
            await _set_created_at(
                93121, botmod.now_utc() - datetime.timedelta(days=365))
            assert await botmod.expire_stale_applications(days=0) == []

            db = await get_db()
            cursor = await db.execute(
                "SELECT status FROM applications WHERE user_id = ?", ("93121",))
            row = await cursor.fetchone()
            await cursor.close()
            assert row["status"] == "pending"
        finally:
            await close_db()

    asyncio.run(scenario())
    print("  ok: TTL=0 выключает автозакрытие")


# ============================================================
# Ошибочные ситуации при обновлении панели
# ============================================================
def test_refresh_panel_channel_deleted():
    """Канал удалён: понятный статус, ничего не отправляем."""
    async def scenario():
        await init_db()
        try:
            bot = _FakeBot(None)  # get_channel -> None
            await seen_repo.set_flag("clan_panel_channel", "777")
            await seen_repo.set_flag("clan_panel_message", "111")
            await seen_repo.set_flag("clan_panel_at", "1")

            status = await botmod.refresh_clan_panel(bot)
            assert "недоступен" in status, status
            assert "777" in status
            # Флаги не трогаем: канал могут вернуть правами/восстановить.
            assert await seen_repo.get_flag("clan_panel_message") == "111"
        finally:
            await _reset_panel_flags()
            await close_db()

    asyncio.run(scenario())
    print("  ok: удалённый канал даёт понятный статус")


def test_refresh_panel_channel_renamed():
    """Канал переименовали — поиск по id, обновление проходит."""
    async def scenario():
        await init_db()
        try:
            old = _FakeMessage(111)
            channel = _NamedChannel(555, old, "новое-имя-канала")
            bot = _FakeBot(channel)
            await seen_repo.set_flag("clan_panel_channel", "555")
            await seen_repo.set_flag("clan_panel_message", "111")
            await seen_repo.set_flag("clan_panel_at", "1")

            status = await botmod.refresh_clan_panel(bot)
            assert status == "панель обновлена", status
            assert old.deleted is True
            assert len(channel.sent) == 1
        finally:
            await _reset_panel_flags()
            await close_db()

    asyncio.run(scenario())
    print("  ok: переименование канала не ломает обновление")


def test_refresh_panel_without_manage_messages():
    """Нет Manage Messages: новая панель публикуется, статус предупреждает."""
    async def scenario():
        await init_db()
        try:
            old = _ForbiddenDeleteMessage(111)
            channel = _FakeChannel(555, old_message=old)
            bot = _FakeBot(channel)
            await seen_repo.set_flag("clan_panel_channel", "555")
            await seen_repo.set_flag("clan_panel_message", "111")
            await seen_repo.set_flag("clan_panel_at", "1")

            status = await botmod.refresh_clan_panel(bot)
            assert "Manage Messages" in status, status
            assert "две панели" in status, status
            assert len(channel.sent) == 1, "новая панель должна уйти"
            assert await seen_repo.get_flag("clan_panel_message") == \
                str(channel.sent[0]["message"].id)
        finally:
            await _reset_panel_flags()
            await close_db()

    asyncio.run(scenario())
    print("  ok: без Manage Messages панель дублируется, но живёт")


def test_refresh_panel_without_send_permission():
    """Нет прав на отправку: статус про права, старые флаги целы."""
    async def scenario():
        await init_db()
        try:
            channel = _ForbiddenSendChannel(555, old_message=_FakeMessage(111))
            bot = _FakeBot(channel)
            await seen_repo.set_flag("clan_panel_channel", "555")
            await seen_repo.set_flag("clan_panel_message", "111")
            await seen_repo.set_flag("clan_panel_at", "1")

            status = await botmod.refresh_clan_panel(bot)
            assert "нет прав на публикацию" in status, status
            assert "Send Messages" in status
            assert channel.sent == []
            assert await seen_repo.get_flag("clan_panel_message") == "111"
        finally:
            await _reset_panel_flags()
            await close_db()

    asyncio.run(scenario())
    print("  ok: без прав на отправку — понятный статус и без потери флагов")


def test_panel_task_warns_when_not_configured(monkeypatch):
    """Первый деплой без !setup_clan: предупреждение в лог, не чаще раза в 6 ч."""
    async def scenario():
        await init_db()
        try:
            await _reset_panel_flags()
            botmod._panel_alert_at.clear()
            messages = []
            monkeypatch.setattr(botmod, "log", messages.append)

            await botmod.clan_panel_refresh.coro()
            assert any("!setup_clan" in m for m in messages), messages

            count = len(messages)
            await botmod.clan_panel_refresh.coro()
            assert len(messages) == count, "повтор не должен спамить лог"
        finally:
            botmod._panel_alert_at.clear()
            await _reset_panel_flags()
            await close_db()

    asyncio.run(scenario())
    print("  ok: отсутствие настройки панели пишется в лог")


def test_panel_task_reports_missing_channel(monkeypatch):
    """Канал удалён: задача пишет предупреждение, а не молчит."""
    async def scenario():
        await init_db()
        try:
            await seen_repo.set_flag("clan_panel_channel", "999999")
            await seen_repo.set_flag("clan_panel_message", "111")
            await seen_repo.set_flag("clan_panel_at", "1")
            botmod._panel_alert_at.clear()
            messages = []
            monkeypatch.setattr(botmod, "log", messages.append)

            await botmod.clan_panel_refresh.coro()
            assert any("недоступен" in m for m in messages), messages
        finally:
            botmod._panel_alert_at.clear()
            await _reset_panel_flags()
            await close_db()

    asyncio.run(scenario())
    print("  ok: задача сообщает об удалённом канале")


def test_parse_iso_utc():
    """created_at из БД: aware/naive/invalid."""
    aware = botmod._parse_iso_utc("2026-11-01T10:00:00+00:00")
    assert aware is not None and aware.tzinfo is not None
    naive = botmod._parse_iso_utc("2026-11-01T10:00:00")
    assert naive is not None and naive.tzinfo is not None
    assert botmod._parse_iso_utc("") is None
    assert botmod._parse_iso_utc("вчера") is None
    print("  ok: разбор created_at")
