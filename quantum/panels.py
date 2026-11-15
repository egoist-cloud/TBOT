# -*- coding: utf-8 -*-
"""Единый реестр панелей: поиск, усыновление, удаление дублей, публикация.

Раньше каждая панель (заявка, роли, админ-панель) хранила id сообщения в
meta и по-своему переживала его потерю. Из-за этого дубли появлялись
снова и снова: после сброса БД или нового DATA_DIR панель публиковалась
заново, хотя старая всё ещё лежала в канале.

PanelSpec описывает панель: ключ флагов, канал по умолчанию, заголовок и
custom_id кнопки. PanelRegistry делает всё остальное:

  * ищет панели в истории канала по заголовку Embed или тексту/кнопке
    Components V2 (включая старые версии без сохранённого id);
  * «усыновляет» найденную панель, если id потерялся, и пишет его в meta;
  * удаляет дубли, оставляя ровно одну панель;
  * публикует новую, когда панели нет или запрошен refresh;
  * если историю прочитать нельзя — не публикует вслепую, чтобы не
    создать ещё один дубль.

Новой панели достаточно завести PanelSpec и builder — логика дублей уже
общая и не требует копирования.
"""
from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import discord

from .repositories import seen as seen_repo

log = logging.getLogger("quantum.panels")


@dataclass(frozen=True)
class PanelSpec:
    """Описание панели. Ключ задаёт имена флагов в таблице meta."""

    key: str
    channel_id: int
    title: str
    custom_id: str = ""
    history_limit: int = 100

    def flag(self, suffix: str) -> str:
        """Имя флага: ``clan_panel_channel``, ``role_panel_message`` и т.п."""
        return f"{self.key}_{suffix}"


@dataclass
class PanelResult:
    """Результат ensure(): статус, id сообщения и что было удалено."""

    status: str
    message_id: int | None = None
    channel_id: int | None = None
    deleted: int = 0
    error: str = ""
    # Старое сообщение не удалилось, но новая панель уже опубликована.
    delete_problem: str = ""


class PanelRegistry:
    """Одна логика панелей для всех каналов."""

    def __init__(self, client, send: Callable[..., Any],
                 spec: PanelSpec):
        self.client = client
        self._send = send
        self.spec = spec

    # ------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------
    @staticmethod
    def matches(spec: PanelSpec, message) -> bool:
        """Наша панель? Embed по заголовку или Components V2 по тексту/кнопке."""
        author = getattr(message, "author", None)
        if getattr(author, "bot", False) is not True:
            return False
        for embed in getattr(message, "embeds", None) or []:
            title = str(getattr(embed, "title", "") or "")
            if spec.title and spec.title in title:
                return True
        stack = list(getattr(message, "components", None) or [])
        while stack:
            component = stack.pop()
            custom_id = getattr(component, "custom_id", None)
            if spec.custom_id and custom_id == spec.custom_id:
                return True
            content = getattr(component, "content", None)
            if isinstance(content, str) and spec.title and spec.title in content:
                return True
            stack.extend(getattr(component, "children", None) or [])
        return False

    async def resolve_channel(self):
        """Канал панели: сохранённый в meta, затем канал по умолчанию."""
        candidates: list[int] = []
        raw = await seen_repo.get_flag(self.spec.flag("channel"))
        if raw:
            try:
                candidates.append(int(raw))
            except (TypeError, ValueError):
                log.warning("панель %s: битый id канала в meta: %r",
                            self.spec.key, raw)
        if self.spec.channel_id and self.spec.channel_id not in candidates:
            candidates.append(self.spec.channel_id)
        if not candidates:
            return None, f"канал панели не задан ({self.spec.key})"
        for channel_id in candidates:
            channel = self.client.get_channel(channel_id)
            if channel is not None:
                return channel, ""
        return None, (f"канал {candidates[0]} недоступен: удалён или бот его "
                      f"не видит (проверьте права и id канала)")

    async def _scan(self, channel):
        """Все панели в истории. None — историю прочитать не удалось."""
        history = getattr(channel, "history", None)
        if history is None:  # тестовые фейки и каналы без истории
            return []
        panels = []
        try:
            async for message in history(limit=self.spec.history_limit):
                if self.matches(self.spec, message):
                    panels.append(message)
        except discord.Forbidden:
            log.warning("панель %s: нет доступа к истории канала", self.spec.key)
            return None
        except Exception as e:  # noqa: BLE001 - очистка best-effort
            log.warning("панель %s: не прочитал историю: %s: %s",
                        self.spec.key, type(e).__name__, e)
            return None
        return panels

    async def _tracked(self, channel):
        """Сохранённое сообщение панели: (message, статус ошибки)."""
        raw = await seen_repo.get_flag(self.spec.flag("message"))
        if not raw:
            return None, ""
        try:
            return await channel.fetch_message(int(raw)), ""
        except discord.NotFound:
            return None, ""  # сообщение удалили — панель вернём
        except discord.Forbidden:
            return None, "forbidden"
        except Exception as e:  # noqa: BLE001 - не блокируем refresh
            log.warning("панель %s: не проверил сообщение: %s", self.spec.key, e)
            return None, "error"

    async def tracked_exists(self, channel) -> bool:
        """Лежит ли сохранённое сообщение панели в канале.

        Если проверить нельзя (нет истории/прав), считаем что лежит: лучше
        пропустить лишнее обновление, чем наплодить дублей.
        """
        tracked, err = await self._tracked(channel)
        return tracked is not None or bool(err)

    # ------------------------------------------------------------
    # Удаление
    # ------------------------------------------------------------
    async def _delete_message(self, message) -> tuple[int, str]:
        """Удаляет сообщение: (сколько удалено, проблема для отчёта)."""
        try:
            await message.delete()
            return 1, ""
        except discord.NotFound:
            return 0, ""
        except discord.Forbidden:
            return 0, "нет права Manage Messages"
        except Exception as e:  # noqa: BLE001 - удаление best-effort
            return 0, f"{type(e).__name__}: {e}"

    async def _delete(self, messages, keep_id=None) -> tuple[int, str]:
        """Удаляет панели, кроме keep_id. Возвращает (удалено, проблема)."""
        deleted, problem = 0, ""
        for message in messages or []:
            if keep_id is not None and str(message.id) == str(keep_id):
                continue
            count, err = await self._delete_message(message)
            deleted += count
            problem = problem or err
        return deleted, problem

    async def _save(self, channel_id, message_id, *, touch: bool) -> None:
        await seen_repo.set_flag(self.spec.flag("channel"), str(channel_id))
        await seen_repo.set_flag(self.spec.flag("message"), str(message_id))
        if touch:
            await seen_repo.set_flag(self.spec.flag("at"), str(time.time()))

    # ------------------------------------------------------------
    # Публикация
    # ------------------------------------------------------------
    async def ensure(self, build, *, refresh: bool = False,
                     channel=None) -> PanelResult:
        """Одна панель в канале.

        build() -> (embed, view) или (embed, view, allowed_mentions);
        может быть async. refresh=True пересоздаёт панель даже если она
        на месте (так обновляется панель заявки). channel позволяет
        опубликовать в конкретный канал (!setup_clan).
        """
        if channel is None:
            channel, channel_err = await self.resolve_channel()
            if channel is None:
                return PanelResult("no_channel", error=channel_err)

        channel_id = getattr(channel, "id", None) or self.spec.channel_id
        scanned = await self._scan(channel)
        tracked, tracked_err = await self._tracked(channel)
        problem = ""
        if tracked_err == "forbidden":
            if not refresh:
                return PanelResult("forbidden", channel_id=channel_id,
                                   error="нет доступа к истории канала")
            problem = "нет права читать историю канала"
        elif tracked_err:
            if not refresh:
                return PanelResult("verify_error", channel_id=channel_id,
                                   error="не удалось проверить панель")
        if scanned is None and tracked is None and not refresh:
            return PanelResult("no_history", channel_id=channel_id,
                               error="нет доступа к истории канала")

        if not refresh:
            keep = tracked or (scanned[0] if scanned else None)
            if keep is not None:
                deleted, delete_problem = await self._delete(scanned, keep.id)
                await self._save(channel_id, keep.id, touch=False)
                return PanelResult("exists", keep.id, channel_id, deleted,
                                   delete_problem=delete_problem)

        deleted, delete_problem = await self._delete(
            scanned, tracked.id if tracked is not None else None)
        if tracked is not None:
            count, err = await self._delete_message(tracked)
            deleted += count
            problem = problem or err
        problem = problem or delete_problem

        built = build()
        if inspect.isawaitable(built):
            built = await built
        embed, view, *rest = built
        allowed_mentions = rest[0] if rest else None
        try:
            message = await self._send(channel, embed=embed, view=view,
                                       allowed_mentions=allowed_mentions)
        except discord.Forbidden:
            return PanelResult("send_forbidden", channel_id=channel_id,
                               deleted=deleted, error=problem)
        if message is None:
            return PanelResult("send_failed", channel_id=channel_id,
                               deleted=deleted, error=problem)
        await self._save(channel_id, message.id, touch=True)
        return PanelResult("published", message.id, channel_id, deleted,
                           delete_problem=problem)
