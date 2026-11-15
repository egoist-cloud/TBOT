# -*- coding: utf-8 -*-
"""ИИ-агент (guardian): наблюдает за ботом, ловит ошибки и предлагает идеи.

Агент не вмешивается в работу бота — он только наблюдает:

  * ошибки команд уходят в служебный канал сразу, стилизованным Embed;
  * утренний дайджест — один раз в день после AGENT_DIGEST_HOUR (UTC):
    состояние систем и события за последние сутки;
  * идея по улучшению бота — раз в AGENT_IDEA_INTERVAL_MINUTES (ИИ
    спрашивается напрямую, минуя RAG по правилам клана и лимитер).

Периодических отчётов о состоянии больше нет: канал не забивается
одинаковыми сообщениями, а важные события (ошибки, деградация health)
по-прежнему приходят сразу. Расписание хранится в таблице meta, поэтому
после перезапуска агент не публикует дайджест/идеи «пачкой».
"""
from __future__ import annotations

import datetime
import json
import logging
import time

import discord

from .env import env_int
from .repositories import applications as applications_repo
from .repositories import audit as audit_repo
from .repositories import links as links_repo
from .repositories import moderation as moderation_repo
from .repositories import players as players_repo
from .repositories import seen as seen_repo
from .repositories import users as users_repo
from .repositories import votes as votes_repo

log = logging.getLogger("quantum.agent")

# Час утреннего дайджеста (UTC). -1 отключает дайджест.
AGENT_DIGEST_HOUR = env_int("AGENT_DIGEST_HOUR", 9)
# Периодические идеи по улучшению кода (минуты; 0 — выключить).
AGENT_IDEA_INTERVAL_MINUTES = env_int("AGENT_IDEA_INTERVAL_MINUTES", 360)

# Сколько последних идей помним, чтобы не повторяться.
MAX_IDEAS = 12

# Флаги расписания в таблице meta.
_FLAG_DIGEST = "agent_last_digest"
_FLAG_IDEA = "agent_last_idea"
_FLAG_IDEAS = "agent_ideas"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _parse_idea(text: str) -> dict:
    """Разбирает ответ ИИ вида «ИДЕЯ: … / ПОЛЬЗА: … / РЕАЛИЗАЦИЯ: …»."""
    out = {"idea": "", "benefit": "", "impl": ""}
    keys = {"идея": "idea", "польза": "benefit", "реализация": "impl"}
    for line in (text or "").splitlines():
        stripped = line.strip()
        low = stripped.lower()
        for prefix, field in keys.items():
            if low.startswith(prefix + ":"):
                out[field] = stripped.split(":", 1)[1].strip()
                break
    if not out["idea"]:
        out["idea"] = (text or "").strip()[:500]
    return out


class Agent:
    """Наблюдатель за ботом: ошибки, отчёты, идеи."""

    def __init__(self, bot, channel_id: int = 0, redact=None,
                 idea_channel_id: int | None = None):
        self.bot = bot
        self.channel_id = int(channel_id or 0)
        # Идеи по коду можно вынести в отдельный канал, чтобы не мешались
        # с отчётами о состоянии; по умолчанию — тот же канал агента.
        self.idea_channel_id = int(idea_channel_id or self.channel_id)
        # Маскирование секретов: в канал агента не должны попадать токены
        # и cookie из текста исключений (URL Steam с ?key=, заголовки).
        self._redact = redact or (lambda text: str(text))
        # Счётчик ошибок по командам: контекст -> количество.
        self.error_stats: dict[str, int] = {}
        self.started_at = time.time()
        # Факты о проекте: идеи должны быть про этот бот, а не абстрактные.
        # Заполняется в setup_hook из AIService (единый загрузчик файла).
        self.project_context = ""

    # ------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------
    def channel(self, channel_id: int | None = None):
        """Канал агента/идей (None, если канала нет или он недоступен)."""
        cid = self.channel_id if channel_id is None else int(channel_id)
        if not cid:
            return None
        try:
            return self.bot.get_channel(cid)
        except Exception:
            return None

    async def post(self, embed: discord.Embed,
                   channel_id: int | None = None) -> bool:
        """Отправляет Embed в канал агента (или указанный). False — нет канала."""
        ch = self.channel(channel_id)
        if ch is None:
            return False
        try:
            await ch.send(embed=embed)
            return True
        except Exception as e:
            log.warning("не смог отправить сообщение: %s", e)
            return False

    # ------------------------------------------------------------
    # Ошибки команд
    # ------------------------------------------------------------
    def record_error(self, context: str) -> None:
        """Увеличивает счётчик ошибок для команды."""
        key = (context or "unknown").split(":")[0].strip() or "unknown"
        self.error_stats[key] = self.error_stats.get(key, 0) + 1

    def top_errors(self, limit: int = 5) -> str:
        """Самые проблемные команды (для Embed)."""
        if not self.error_stats:
            return "—"
        items = sorted(self.error_stats.items(), key=lambda kv: -kv[1])[:limit]
        return "\n".join(f"`{name}` — {count}" for name, count in items)

    async def report_error(self, context: str, exc: BaseException) -> bool:
        """Стилизованный Embed с ошибкой — в канал агента."""
        self.record_error(context)
        embed = discord.Embed(
            title="🚨 Ошибка в команде",
            color=0x000000,
            timestamp=_now(),
        )
        embed.add_field(name="Контекст", value=f"`{context}`", inline=True)
        embed.add_field(name="Тип", value=f"`{type(exc).__name__}`", inline=True)
        message = self._redact(str(exc))[:800] or "—"
        embed.add_field(name="Сообщение", value=f"```\n{message}\n```", inline=False)
        embed.add_field(name="🔥 Топ ошибок", value=self.top_errors(), inline=False)
        embed.set_footer(text="Quantum • ИИ-агент")
        return await self.post(embed)

    # ------------------------------------------------------------
    # Метрики
    # ------------------------------------------------------------
    async def snapshot(self, extra: dict | None = None) -> dict:
        """Сводка по системам: БД, заявки, модераторы, ошибки."""
        data = dict(extra or {})

        async def _count(fn):
            try:
                return await fn()
            except Exception:
                return "?"

        data["links"] = await _count(links_repo.count_all)
        data["players"] = await _count(players_repo.count_all)
        data["users"] = await _count(users_repo.count_all)
        data["seen"] = await _count(seen_repo.count_all)
        data["votes"] = await _count(votes_repo.count_all)
        data["mods"] = await _count(moderation_repo.count_all)
        data["apps"] = await _count(applications_repo.count_applications)
        data["apps_pending"] = await _count(
            applications_repo.count_pending_applications)
        data["errors"] = sum(self.error_stats.values())
        return data

    async def _recent_events(self, limit: int = 6) -> tuple[int, str]:
        """События аудита за последние 24 часа: (сколько, строки для embed)."""
        cutoff = _now() - datetime.timedelta(hours=24)
        try:
            rows = await audit_repo.list_entries(limit=100)
        except Exception as e:
            log.warning("agent: не прочитал аудит: %s", e)
            return 0, "—"
        count, lines = 0, []
        for row in rows:
            try:
                ts = datetime.datetime.fromisoformat(str(row.get("ts") or ""))
            except (TypeError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.UTC)
            if ts < cutoff:
                continue
            count += 1
            if len(lines) < limit:
                action = str(row.get("action") or "—")
                target = str(row.get("target_name") or "").strip()
                if not target and row.get("target_id"):
                    target = f"<@{row['target_id']}>"
                lines.append(f"• {action}" + (f" — {target}" if target else ""))
        return count, "\n".join(lines) or "—"

    async def build_digest_embed(self, extra: dict | None = None) -> discord.Embed:
        """Утренний дайджест: состояние систем и события за сутки."""
        s = await self.snapshot(extra)
        events_count, events_text = await self._recent_events()
        embed = discord.Embed(
            title="☀️ Утренний дайджест",
            color=0x000000,
            timestamp=_now(),
        )
        embed.description = (f"Сводка за сутки · {_now().strftime('%d.%m.%Y')}\n"
                             f"Событий в аудите: **{events_count}**")
        embed.add_field(name="⏱️ Аптайм", value=str(s.get("uptime", "—")), inline=True)
        embed.add_field(name="📡 Пинг", value=str(s.get("ping", "—")), inline=True)
        embed.add_field(name="🔥 Ошибок за сессию", value=str(s.get("errors", 0)),
                        inline=True)
        embed.add_field(name="📨 Заявок",
                        value=f"{s.get('apps', '?')} (в работе: {s.get('apps_pending', '?')})",
                        inline=True)
        embed.add_field(name="🔗 Привязок", value=str(s.get("links", "?")), inline=True)
        embed.add_field(name="🛡 Модераторов", value=str(s.get("mods", "?")), inline=True)
        embed.add_field(name="🗳 Голосов", value=str(s.get("votes", "?")), inline=True)
        embed.add_field(name="🤖 ИИ", value=str(s.get("ai", "—")), inline=False)
        embed.add_field(name="📖 Правила", value=str(s.get("rules", "—")), inline=False)
        embed.add_field(name="🐙 GitHub", value=str(s.get("github", "—")), inline=True)
        embed.add_field(name="🌐 cs2red", value=str(s.get("cs2red", "—")), inline=True)
        if self.error_stats:
            embed.add_field(name="🐞 Топ ошибок", value=self.top_errors(),
                            inline=False)
        embed.add_field(name="📋 События за сутки", value=events_text[:1024],
                        inline=False)
        embed.set_footer(text="Quantum • ИИ-агент")
        return embed

    # ------------------------------------------------------------
    # Идеи
    # ------------------------------------------------------------
    async def known_ideas(self) -> list:
        """Последние предложенные идеи (чтобы не повторяться)."""
        raw = await seen_repo.get_flag(_FLAG_IDEAS)
        try:
            data = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [str(x) for x in data]

    async def remember_idea(self, title: str) -> None:
        ideas = await self.known_ideas()
        ideas.append((title or "")[:200])
        await seen_repo.set_flag(
            _FLAG_IDEAS, json.dumps(ideas[-MAX_IDEAS:], ensure_ascii=False))

    async def ask_provider(self, providers, prompt: str) -> tuple[str, str]:
        """Прямой вызов ИИ-провайдера (без RAG и лимитера пользователей).

        Провайдеры в паузе после лимита/ошибки пропускаются — их не дёргаем
        зря, идею сгенерирует следующий доступный.
        """
        for prov in providers or []:
            if not getattr(prov, "available", True):
                continue
            try:
                result = await prov.complete([{"role": "user", "content": prompt}])
            except Exception as e:
                log.warning("agent: %s: %s", getattr(prov, "name", "?"), e)
                continue
            if result:
                return str(result), str(getattr(prov, "name", "?"))
        return "", ""

    def build_idea_prompt(self, stats: dict, known: list | None = None) -> str:
        """Промпт для идеи по улучшению этого кода.

        project_context и stats["code_overview"] дают ИИ конкретику (команды,
        модули, правила проекта), иначе ответы получаются общими.
        """
        known = ", ".join(known or []) or "—"
        code_overview = str(stats.get("code_overview") or "")
        parts = [
            "Ты — ИИ-агент, который наблюдает за Discord-ботом игрового клуба "
            "TALENT (Python, discord.py) и предлагает, что улучшить в его коде.",
            "Возможности бота: привязка Discord↔Steam, заявки в клан с "
            "голосованием, роли клана и сайта, RAG-ответы по правилам, "
            "ИИ-каскад с фолбэком провайдеров, /profile со статистикой, "
            "панели Components V2, синк данных с GitHub.",
            f"Текущее состояние: привязок {stats.get('links')}, "
            f"заявок {stats.get('apps')} (в работе {stats.get('apps_pending')}), "
            f"голосов {stats.get('votes')}, модераторов {stats.get('mods')}, "
            f"ошибок за сессию {stats.get('errors')}.",
            f"Уже предлагалось (не повторяй): {known}",
        ]
        if self.project_context:
            parts.append("Знания о проекте:\n" + self.project_context)
        if code_overview:
            parts.append("Структура кода:\n" + code_overview)
        parts.append(
            "Предложи ОДНУ конкретную идею по улучшению кода, которую реально "
            "реализовать за несколько часов: назови файл/функцию, если это "
            "уместно. Не предлагай новые платные сервисы. Формат ответа строго "
            "такой:\n"
            "ИДЕЯ: <до 120 символов>\n"
            "ПОЛЬЗА: <зачем это клану, до 200 символов>\n"
            "РЕАЛИЗАЦИЯ: <как сделать, до 300 символов>"
        )
        return "\n\n".join(parts)

    async def build_idea_embed(self, providers, stats: dict):
        """Embed с идеей агента (None — если ИИ недоступен)."""
        known = await self.known_ideas()
        prompt = self.build_idea_prompt(stats, known)
        text, provider = await self.ask_provider(providers, prompt)
        if not text:
            return None
        idea = _parse_idea(text)
        embed = discord.Embed(
            title="💡 Идея от ИИ-агента",
            color=0x000000,
            timestamp=_now(),
        )
        embed.description = idea["idea"] or "—"
        if idea["benefit"]:
            embed.add_field(name="🎯 Польза", value=idea["benefit"][:1000],
                            inline=False)
        if idea["impl"]:
            embed.add_field(name="🛠 Как сделать", value=idea["impl"][:1000],
                            inline=False)
        embed.set_footer(text=f"Quantum • ИИ-агент • {provider}")
        return embed

    # ------------------------------------------------------------
    # Цикл наблюдения
    # ------------------------------------------------------------
    async def _flag_time(self, key: str) -> float:
        raw = await seen_repo.get_flag(key)
        try:
            return float(raw or 0)
        except (TypeError, ValueError):
            return 0.0

    async def run_cycle(self, providers=None, extra: dict | None = None) -> None:
        """Один цикл: утренний дайджест (раз в день), затем идея по расписанию."""
        now = time.time()
        today = _now()
        if (0 <= AGENT_DIGEST_HOUR <= 23
                and today.hour >= AGENT_DIGEST_HOUR
                and await seen_repo.get_flag(_FLAG_DIGEST) != today.date().isoformat()):
            embed = await self.build_digest_embed(extra)
            if await self.post(embed):
                # Флаг ставим только после успешной отправки: если канал
                # недоступен, следующая попытка будет через цикл, а не завтра.
                await seen_repo.set_flag(_FLAG_DIGEST, today.date().isoformat())

        if (AGENT_IDEA_INTERVAL_MINUTES > 0
                and now - await self._flag_time(_FLAG_IDEA) >=
                AGENT_IDEA_INTERVAL_MINUTES * 60):
            await self.force_idea(providers, extra)

    async def force_idea(self, providers, extra: dict | None = None):
        """Сгенерировать и опубликовать идею по запросу (без расписания)."""
        stats = await self.snapshot(extra)
        embed = await self.build_idea_embed(providers, stats)
        if embed is None:
            return None
        if not await self.post(embed, channel_id=self.idea_channel_id):
            return None
        await self.remember_idea(embed.description or "")
        await seen_repo.set_flag(_FLAG_IDEA, str(time.time()))
        return embed
