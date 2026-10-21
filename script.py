"""
Quantum - бот клана (v18: исправлен Cloudflare 429, все safe_send)
"""
import os
import re
import json
import time
import random
import asyncio
import datetime
import platform
import sys
import threading
import traceback
import uuid
from pathlib import Path
from collections import deque
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from flask import Flask

from quantum.db import init_db, close_db
from quantum.env import env_int
from quantum.migrations import import_json_to_sqlite
from quantum.agent import Agent
from quantum.repositories import votes as votes_repo
from quantum.repositories import seen as seen_repo
from quantum.repositories import links as links_repo
from quantum.repositories import players as players_repo
from quantum.repositories import users as users_repo
from quantum.repositories import moderation as moderation_repo
from quantum.repositories import applications as applications_repo
from quantum.ai import AIService
from github_sync import GitHubSync
from quantum.ai.cache import count_cache, clear_cache

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("PYTHONUNBUFFERED", "1")

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def data_path(name: str) -> Path:
    return DATA_DIR / name


UTC = datetime.UTC


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def log(msg: str):
    print(redact(msg), flush=True)


# ============================================================
# НАСТРОЙКИ
# ============================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = env_int("GUILD_ID", 0)

LOG_CHANNEL_ID = 1511463623703265361
APPLICATION_LOGS_CHANNEL_ID = 1511463623703265361
APPLICATIONS_CHANNEL_ID = 1511463623703265361
THREADS_CHANNEL_ID = 1236678127778795660
DB_CHANNEL_ID = 1554883485834149888
ERROR_LOG_CHANNEL_ID = 1511463623703265361

# Канал ИИ-агента: ошибки команд, отчёты о состоянии, идеи по улучшению.
AGENT_CHANNEL_ID = env_int("AGENT_CHANNEL_ID", 1556427610160889907)

ALLOWED_ROLE_ID = env_int("ALLOWED_ROLE_ID", 0)
REVIEWER_ROLE_ID_2 = env_int("REVIEWER_ROLE_ID_2", 0)
SPAM_VOTE_ROLE_ID = env_int("SPAM_VOTE_ROLE_ID", 0)
EXTRA_REVIEWER_ROLE_1 = env_int("EXTRA_REVIEWER_ROLE_1", 0)
EXTRA_REVIEWER_ROLE_2 = env_int("EXTRA_REVIEWER_ROLE_2", 0)
EXTRA_REVIEWER_ROLE_3 = env_int("EXTRA_REVIEWER_ROLE_3", 0)

AUTO_ROLE_ID = env_int("AUTO_ROLE_ID", 0)
BYPASS_ROLE_ID = env_int("BYPASS_ROLE_ID", 0)
NO_COOLDOWN_ROLE_ID = env_int("NO_COOLDOWN_ROLE_ID", 0)

ACTIVITY_ROLE_ID = env_int("ACTIVITY_ROLE_ID", 1448368118190837800)

CLAN_NAME = (os.getenv("CLAN_NAME") or "TALENT").strip() or "TALENT"
CLAN_URL = os.getenv("CLAN_URL", "https://cs2red.ru/clans/id/47")
VOTES_REQUIRED = env_int("VOTES_REQUIRED", 2)

# Фиксированный custom_id выпадающего списка заявок. Нужен, чтобы список
# работал после рестарта бота: со случайным custom_id discord.py теряет
# обработчик, и Discord отвечает «Приложение не ответило вовремя» (10062).
CLAN_SELECT_CUSTOM_ID = "clan_select_quantum"

ACTIVITY_GOAL_HOURS = 8
ACTIVITY_MIN_HOURS = 8
APPLY_EMBED_COLOR = 0x2B2D31

BOT_START_TIME = time.time()

CMD_API_TIMEOUT = 30.0

def _parse_cookie_string(raw: str) -> dict:
    """Парсит raw cookie-строку ('name=value; name2=value2') в словарь."""
    result = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                result[k.strip()] = v.strip()
    return result


# Куки для cs2red.admin. Приоритет: CS2RED_COOKIE_STRING (raw-строка),
# иначе набор отдельных переменных CS2RED_*.
_cookie_raw = os.getenv("CS2RED_COOKIE_STRING", "").strip()
if _cookie_raw:
    CS2RED_COOKIES = _parse_cookie_string(_cookie_raw)
else:
    CS2RED_COOKIES = {k: v for k, v in {
        "token": os.getenv("CS2RED_TOKEN", ""),
        "bh": os.getenv("CS2RED_BH", ""),
        "_yasc": os.getenv("CS2RED_YASC", ""),
        "browser_id": os.getenv("CS2RED_BROWSER_ID", ""),
        "yandexuid": os.getenv("CS2RED_YANDEXUID", ""),
        "yuidss": os.getenv("CS2RED_YUIDSS", ""),
        "i": os.getenv("CS2RED_I", ""),
        "__ddg1_": os.getenv("CS2RED_DDG1", ""),
        "__ddg8_": os.getenv("CS2RED_DDG8", ""),
        "__ddg9_": os.getenv("CS2RED_DDG9", ""),
        "__ddg10_": os.getenv("CS2RED_DDG10", ""),
        "yabs-sid": os.getenv("CS2RED_YABS_SID", ""),
        "_ym_uid": os.getenv("CS2RED_YM_UID", ""),
        "_ym_d": os.getenv("CS2RED_YM_D", ""),
        "_ym_isad": os.getenv("CS2RED_YM_ISAD", ""),
        "ymex": os.getenv("CS2RED_YMEX", ""),
    }.items() if v}


# ============================================================
# МАСКИРОВАНИЕ СЕКРЕТОВ В ЛОГАХ
# ============================================================
# Секреты попадали в логи и в Discord-канал ошибок: например, текст
# HTTP-ошибки Steam содержал полный URL с ?key=<STEAM_API_KEY>, а
# traceback aiohttp мог содержать заголовки с cookie cs2red.
_SECRET_ENV_NAMES = (
    "CS2RED_COOKIE_STRING",
    "CS2RED_TOKEN", "CS2RED_BH", "CS2RED_I", "CS2RED_YASC",
    "CS2RED_BROWSER_ID", "CS2RED_YANDEXUID", "CS2RED_YUIDSS",
    "CS2RED_DDG1", "CS2RED_DDG8", "CS2RED_DDG9", "CS2RED_DDG10",
    "CS2RED_YABS_SID", "CS2RED_YM_UID", "CS2RED_YM_D",
    "CS2RED_YM_ISAD", "CS2RED_YMEX",
    "STEAM_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "AI_API_KEY", "LLM7_API_KEY", "OPENROUTER_API_KEY",
    "GITHUB_TOKEN", "DISCORD_TOKEN",
)

# Секреты короче 6 символов не маскируем: такое значение встречается
# в тексте случайно и только испортит читаемость логов.
_MIN_SECRET_LEN = 6


def _collect_secret_values() -> list:
    """Собирает значения всех секретов из окружения (для маскирования)."""
    values = []
    for name in _SECRET_ENV_NAMES:
        val = (os.getenv(name) or "").strip()
        if len(val) >= _MIN_SECRET_LEN:
            values.append(val)
    # Отдельные куки из CS2RED_COOKIE_STRING — тоже секреты.
    for val in (CS2RED_COOKIES or {}).values():
        val = (val or "").strip()
        if len(val) >= _MIN_SECRET_LEN:
            values.append(val)
    # Сначала самые длинные: иначе короткое значение может разрезать
    # длинное и оставить его хвост в открытом виде.
    return sorted(set(values), key=len, reverse=True)


_SECRET_VALUES = _collect_secret_values()


def redact(text) -> str:
    """Маскирует секреты в произвольной строке."""
    if text is None:
        return ""
    s = str(text)
    if not s:
        return s
    for val in _SECRET_VALUES:
        if val in s:
            s = s.replace(val, "***")
    return s


def _is_cs2red_host(url: str) -> bool:
    """Проверка по ХОСТУ, а не по подстроке.

    Проверка подстроки пропустила бы URL вида
    https://evil.example/cs2red.ru — и такой запрос получил бы
    приватную сессию с админскими cookie.
    """
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False
    return host == "cs2red.ru" or host.endswith(".cs2red.ru")


# GitHub-синк данных бота (опционально: требует GITHUB_TOKEN + GITHUB_REPO).
github_sync = GitHubSync(
    token=os.getenv("GITHUB_TOKEN", ""),
    repo=os.getenv("GITHUB_REPO", ""),
    branch=os.getenv("GITHUB_BRANCH", "main") or "main",
    base_path=os.getenv("GITHUB_BASE_PATH", ""),
    commit_prefix="bot data",
    skip_render=True,
)

CS2RED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.6",
    "Referer": "https://cs2red.ru/admin",
    "Origin": "https://cs2red.ru",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-GPC": "1",
}

ROLE_MAP = {1: None, 2: "VIP", 3: "Premium", 4: "Moderator",
            5: "Администратор", 6: "Амбассадор", 9: "Plus"}
ROLE_DISPLAY_MAP = {1: "Мл. Модер", 3: "Модератор", 5: "Админ",
                    6: "С. Админ", 7: "Спонсор", 13: "Гл.Модератор"}


def role_display_name(role_id) -> str:
    return ROLE_DISPLAY_MAP.get(role_id) or ROLE_MAP.get(role_id) or "Игрок"


CLAN_ROLE_MAP = {
    47: 1448368118190837800, 108: 1555181110248476843, 95: 1454987841019838547,
    180: 1546232690288631909, 148: 1555181315874103406, 114: 1548957197659283456,
    155: 1555181474292965436, 272: 1524071629880168458, 61: 1555181722524717056,
    74: 1544421362431557732, 33: 1555181917756854355, 11: 1555182111349145600,
    8: 1555182240290443385, 367: 1555182697230504036, 160: 1555182823168544769,
    140: 1555182933264830524, 213: 1555183033131204719, 208: 1555183533180461126,
    12: 1555183608900358165, 38: 1555183705247588432, 196: 1555183791113510962,
    133: 1555183874605056080,
}

SYNC_INTERVAL_MINUTES = env_int("SYNC_INTERVAL_MINUTES", 15)
REQUEST_DELAY = 0.3

ACTIVITY_TRACK_SECONDS = 300
ACTIVITY_TRACK_DELTA = 300
CLANS_CACHE_TTL = 300


# ============================================================
# ХРАНИЛИЩЕ БД (SQLite через quantum.repositories)
# ============================================================


def _write_error_to_file(context: str, exc):
    try:
        ts = now_utc().strftime("%Y-%m-%d %H:%M:%S")
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) \
            if isinstance(exc, BaseException) else str(exc)
        with open(data_path("error_log.txt"), "a", encoding="utf-8") as f:
            f.write(redact(f"[{ts}] {context}\n{tb}\n{'=' * 60}\n"))
    except Exception as e:
        log(f"⚠️ error_log: {e}")


NET_ERRORS = (
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    aiohttp.ServerTimeoutError,
    asyncio.TimeoutError,
)


async def log_error(bot: commands.Bot, context: str, exc):
    _write_error_to_file(context, exc)
    log(f"❌ [{context}] {exc}")
    # ИИ-агент: считаем ошибку и дублируем её в канал агента.
    try:
        agent.record_error(context)
    except Exception:
        pass
    if isinstance(exc, NET_ERRORS):
        return
    channel = bot.get_channel(ERROR_LOG_CHANNEL_ID) if ERROR_LOG_CHANNEL_ID else None
    if channel is None:
        return
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) \
        if isinstance(exc, BaseException) else str(exc)
    try:
        await safe_send(channel, redact(f"❌ **Ошибка**\n**Контекст:** `{context}`\n```\n{text[:1500]}\n```"))
    except Exception as e:
        log(f"⚠️ не смог отправить лог: {e}")
    # Стилизованный Embed об ошибке — в канал ИИ-агента.
    try:
        await agent.report_error(context, exc)
    except Exception as e:
        log(f"⚠️ agent.report_error: {e}")


# ============================================================
# ЗАЩИТА ОТ ФЛУДА (Cloudflare + Discord 429)
# ============================================================
_send_semaphore = asyncio.Semaphore(2)
_send_timestamps = deque()


async def _rate_limit_send():
    """Глобальный rate-limit: не более 3 сообщений в секунду."""
    async with _send_semaphore:
        now = time.time()
        while _send_timestamps and now - _send_timestamps[0] > 1.0:
            _send_timestamps.popleft()
        if len(_send_timestamps) >= 3:
            wait = 1.0 - (now - _send_timestamps[0])
            if wait > 0:
                await asyncio.sleep(wait)
        _send_timestamps.append(time.time())


def _embed_black(title: str = "", description: str = "") -> discord.Embed:
    """Универсальный чёрный Embed для ответов бота."""
    embed = discord.Embed(color=0x000000)
    if title:
        embed.title = title
    if description:
        embed.description = description
    embed.set_footer(text="Quantum")
    return embed


def _split_text_embeds(title: str, text: str, limit: int = 4000) -> list:
    """Разбивает длинный текст на несколько чёрных Embeds по ~4000 символов."""
    text = text or ""
    if not text:
        return [_embed_black(title, "")]
    chunks = []
    for i in range(0, len(text), limit):
        chunk = text[i:i + limit]
        if i > 0:
            chunks.append(_embed_black(f"{title} (часть {i // limit + 1})", chunk))
        else:
            chunks.append(_embed_black(title, chunk))
    return chunks or [_embed_black(title, "")]


async def safe_send(target, *args, title: str = "", retries: int = 5, **kwargs):
    """
    Отправка с retry. Уважает:
    - Discord 429 (rate-limit) — ждёт retry_after
    - Cloudflare 429 (HTML-заглушка) — ждёт 90 секунд

    Любой переданный обычный текст (без embed=/content=/view=)
    автоматически оборачивается в чёрный Embed (color=0x000000).
    Заголовок Embed можно задать через title=.
    Длинный текст разбивается на несколько Embeds.
    """
    # Обёртка текста в чёрный Embed, если не задан embed/content/view/file.
    has_embed = "embed" in kwargs or "embeds" in kwargs
    has_content = "content" in kwargs
    has_rich = has_embed or has_content or "view" in kwargs or "files" in kwargs
    if not has_rich and args and isinstance(args[0], str):
        text = args[0]
        rest = args[1:]
        kwargs_backup = dict(kwargs)
        # Длинный текст разбиваем на несколько embeds.
        if len(text) > 4000:
            embeds = _split_text_embeds(title or "📊 Результат", text)
            for e in embeds:
                e.color = 0x000000
            return await _send_multi(target, retries, embeds, **kwargs_backup)
        embed = _embed_black(title, text)
        kwargs["embed"] = embed
        args = tuple(rest)

    return await _safe_send_inner(target, retries, *args, **kwargs)


async def _send_multi(target, retries, embeds, **kwargs):
    """Отправляет список Embeds по одному (для длинных ответов)."""
    for emb in embeds:
        k = dict(kwargs)
        k["embed"] = emb
        await _safe_send_inner(target, retries, **k)


async def _safe_send_inner(target, retries, *args, **kwargs):
    await _rate_limit_send()
    last_exc = None
    for attempt in range(retries):
        try:
            return await target.send(*args, **kwargs)
        except discord.HTTPException as e:
            last_exc = e
            if e.status == 429:
                is_cloudflare = False
                try:
                    body = await e.response.text()
                    if "cloudflare" in body.lower() or "access denied" in body.lower():
                        is_cloudflare = True
                except Exception:
                    pass

                if is_cloudflare:
                    wait = 90.0
                    log(f"🛡 Cloudflare 429 — жду {wait:.0f} сек (попытка {attempt + 1}/{retries})")
                else:
                    wait = getattr(e, "retry_after", None) or 5.0
                    log(f"⏱ Discord 429 — жду {wait:.1f} сек (попытка {attempt + 1}/{retries})")

                await asyncio.sleep(wait)
                continue

            if e.status >= 500 and attempt < retries - 1:
                await asyncio.sleep(3)
                continue
            raise
    log(f"❌ safe_send: не отправил после {retries} попыток: {last_exc}")
    return None


# ============================================================
# ГАРАНТИРОВАННОЕ ЗАКРЫТИЕ INTERACTION
# ============================================================
async def close_interaction(interaction: discord.Interaction, text: str = None,
                            ephemeral: bool = False):
    try:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral, thinking=False)
        if text:
            await safe_send(interaction.followup, text, ephemeral=ephemeral)
    except discord.NotFound:
        pass
    except discord.HTTPException as e:
        log(f"⚠️ close_interaction: {e}")


async def run_with_timeout(coro, timeout: float, fallback_msg: str,
                           interaction: discord.Interaction,
                           context: str = ""):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        await log_error(bot, context or "run_with_timeout", "TIMEOUT")
        await close_interaction(interaction, fallback_msg, ephemeral=False)
        return None
    except Exception as e:
        await log_error(bot, context or "run_with_timeout", e)
        await close_interaction(interaction, f"❌ Ошибка: {str(e)[:200]}", ephemeral=False)
        return None


# ============================================================
# API: cs2red / Steam
# ============================================================
class API:
    # Интервал между запросами к cs2red (анти-DDoS Cloudflare).
    RATE_LIMIT_SECONDS = 2.5
    # При 5xx: максимальное число повторов подряд.
    RETRY_MAX = 3
    # При 429 ждём столько же.
    RATE_LIMIT_429_WAIT = 300
    # При 5xx ждём между повторами.
    RETRY_5xx_WAIT = 10

    def __init__(self):
        # Публичная сессия: Steam / GitHub / LLM — БЕЗ cookies и заголовков cs2red.
        self.session: aiohttp.ClientSession | None = None
        # Приватная сессия: только cs2red.ru (админские cookie/заголовки).
        self.cs2red: aiohttp.ClientSession | None = None
        self.sem = asyncio.Semaphore(4)
        self._clans_cache = {"ts": 0.0, "data": None}
        self.last_status: int | None = None
        # Анти-DDoS: последовательный крановой протокол к cs2red.
        self._rate_lock = asyncio.Lock()
        self._last_cs2red_ts = 0.0
        # Причина блокировки, если сработала защита (429/403/5xx исчерпаны).
        self.blocked_reason = ""

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=20, connect=10)

        def _connector():
            # У каждой сессии свой коннектор: общий коннектор закрылся бы
            # вместе с первой закрытой сессией.
            return aiohttp.TCPConnector(
                family=2, ttl_dns_cache=300, limit=16,
                force_close=False, enable_cleanup_closed=True,
            )

        # Публичная сессия — без секретов cs2red. Иначе cookie и заголовки
        # Referer/Origin уходят на api.steampowered.com, api.github.com и
        # LLM-провайдера: aiohttp не фильтрует cookie по домену.
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=_connector(),
        )
        # Приватная — только для самого cs2red.ru.
        self.cs2red = aiohttp.ClientSession(
            headers=CS2RED_HEADERS,
            cookies=CS2RED_COOKIES or None,
            timeout=timeout,
            connector=_connector(),
        )
        log(f"🌐 API sessions ready. Cookies: {len(CS2RED_COOKIES)}, "
            f"Headers: {len(CS2RED_HEADERS)} (только для cs2red.ru)")

    async def close(self):
        for sess in (self.cs2red, self.session):
            if sess and not sess.closed:
                await sess.close()

    def session_for(self, url: str):
        """Выбирает сессию по ХОСТУ.

        cs2red.ru -> приватная (с админскими cookie), все остальные хосты
        (Steam, GitHub, LLM) -> публичная, без cookie.
        """
        if _is_cs2red_host(url):
            return self.cs2red or self.session
        return self.session

    async def _acquire_rate_slot(self):
        """Обеспечивает не более 1 запроса к cs2red в RATE_LIMIT_SECONDS."""
        async with self._rate_lock:
            now = time.time()
            wait = self.RATE_LIMIT_SECONDS - (now - self._last_cs2red_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_cs2red_ts = time.time()
            return

    async def _get_json(self, url, params=None):
        session = self.session_for(url)
        if not session:
            return None
        is_cs2red = _is_cs2red_host(url)

        # 5xx-ретраи: если подряд падаем, останавливаем cs2red-поток.
        consecutive_5xx = 0
        for attempt in range(self.RETRY_MAX + 1):
            try:
                # Rate-limit только для cs2red (Steam API не трогаем).
                if is_cs2red:
                    await self._acquire_rate_slot()

                async with self.sem:
                    async with session.get(url, params=params) as r:
                        self.last_status = r.status

                        # 403 — авторизация сломана: останавливаемся окончательно.
                        if r.status == 403:
                            self.blocked_reason = "403"
                            log(f"🔴 403 на {url} — авторизация на cs2red. Проверьте куки.")
                            return None

                        # 429 — Cloudflare/Discord прижал: пауза 5 минут, потом стоп.
                        if r.status == 429:
                            self.blocked_reason = "429"
                            log(f"🐢 429 на {url} — cs2red временно блокирует запросы "
                                f"(429). Пауза {self.RATE_LIMIT_429_WAIT} сек.")
                            await asyncio.sleep(self.RATE_LIMIT_429_WAIT)
                            # После паузы 429 всё равно стоп (не рискуем уронить сайт).
                            return None

                        # 5xx — временный сбой сервера: ретраи с паузой, до RETRY_MAX.
                        if r.status >= 500:
                            consecutive_5xx += 1
                            log(f"⚠️ {r.status} на {url} ({attempt + 1}-я попытка из "
                                f"{self.RETRY_MAX + 1})")
                            if consecutive_5xx > self.RETRY_MAX:
                                self.blocked_reason = f"{r.status}"
                                return None
                            await asyncio.sleep(self.RETRY_5xx_WAIT)
                            continue

                        consecutive_5xx = 0
                        r.raise_for_status()
                        return await r.json(content_type=None)
            except NET_ERRORS as e:
                log(f"⚠️ GET {url}: сетевой сбой: {e}")
                if attempt < self.RETRY_MAX:
                    await asyncio.sleep(self.RETRY_5xx_WAIT)
                    continue
                self.blocked_reason = "network"
                return None
            except Exception as e:
                log(f"⚠️ GET {url}: {e}")
                return None
        return None

    async def cs2red_user(self, steam_id: str):
        data = await self._get_json(
            "https://cs2red.ru/api/admin/users",
            params={"begin": 0, "count": 1, "search": steam_id})
        if data is None:
            return None
        if not data.get("success"):
            return None
        users = data.get("users") or []
        if not users:
            return {"_not_found": True, "nick": None, "roleId": None}
        return users[0]

    async def cs2red_search_by_nick(self, nickname: str, limit: int = 10):
        """Ищет игроков в админ-панели cs2red по нику.

        Возвращает список объектов игроков (у каждого есть поле steamid64),
        или None при ошибке. 403 трактуется как ошибка авторизации.
        """
        data = await self._get_json(
            "https://cs2red.ru/api/admin/users",
            params={"begin": 0, "count": limit, "search": nickname})
        if data is None:
            return None
        if not data.get("success"):
            return None
        return data.get("users") or []

    @staticmethod
    def extract_steam64(user: dict) -> str | None:
        """Достаёт SteamID64 из объекта пользователя cs2red."""
        if not isinstance(user, dict):
            return None
        for key in ("steamid64", "steamId64", "steam_id", "steamid", "steamId", "steamID"):
            val = user.get(key)
            if val:
                sid = str(val).strip()
                if sid.isdigit() and len(sid) == 17 and sid.startswith("7656119"):
                    return sid
        return None

    async def cs2red_full_user(self, steam_id: str):
        user = await self.cs2red_user(steam_id)
        if not user or user.get("_not_found") or not user.get("id"):
            return None
        data = await self._get_json(
            "https://cs2red.ru/api/admin/user",
            params={"userId": user["id"]})
        if data and data.get("success"):
            return data.get("user")
        return None

    async def cs2red_clans(self):
        ts = asyncio.get_event_loop().time()
        if self._clans_cache["data"] is not None and ts - self._clans_cache["ts"] < CLANS_CACHE_TTL:
            return self._clans_cache["data"]
        data = await self._get_json("https://cs2red.ru/api/clans")
        if data is None:
            return None
        clans = data.get("clans") or []
        self._clans_cache = {"ts": ts, "data": clans}
        return clans

    async def player_clan(self, steam_id: str):
        user = await self.cs2red_user(steam_id)
        if not user or user.get("_not_found") or not user.get("id"):
            return None
        uid = int(user["id"])
        clans = await self.cs2red_clans()
        if clans is None:
            return None
        for clan in clans:
            for m in (clan.get("members") or []):
                if int(m.get("userId", 0) or 0) == uid:
                    return {"clan_id": clan.get("id"), "clan_name": clan.get("name"),
                            "clan_tag": clan.get("tag"), "member_role": m.get("roleId")}
        return None

    async def server_times(self) -> dict:
        data = await self._get_json("https://cs2red.ru/api/data/servers")
        result = {}
        for s in ((data or {}).get("servers") or []):
            raw = s.get("data")
            if not raw:
                continue
            try:
                d = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            for p in d.get("players", []):
                nick, t = p.get("nick"), p.get("time", 0)
                if nick:
                    result[nick] = max(result.get(nick, 0), t)
        return result

    async def _steam_get(self, iface: str, params: dict):
        key = os.getenv("STEAM_API_KEY")
        if not key:
            return None
        params["key"] = key
        params["steamids"] = params.pop("steam_id")
        return await self._get_json(f"https://api.steampowered.com/{iface}", params)

    async def steam_summary(self, steam_id: str):
        data = await self._steam_get("ISteamUser/GetPlayerSummaries/v2/", {"steam_id": steam_id})
        players = ((data or {}).get("response") or {}).get("players") or []
        return players[0] if players else None

    async def steam_bans(self, steam_id: str) -> dict:
        data = await self._steam_get("ISteamUser/GetPlayerBans/v1/", {"steam_id": steam_id})
        players = (data or {}).get("players") or []
        if not players:
            return {}
        p = players[0]
        return {"vac_banned": p.get("VACBanned", False),
                "number_of_vac_bans": p.get("NumberOfVACBans", 0),
                "days_since_last_ban": p.get("DaysSinceLastBan", 0),
                "community_banned": p.get("CommunityBanned", False),
                "number_of_game_bans": p.get("NumberOfGameBans", 0),
                "economy_ban": p.get("EconomyBan", "none")}


api = API()


# ============================================================
# ДОСТУП / ХЕЛПЕРЫ
# ============================================================
async def is_moderator(user_id: int, guild: discord.Guild) -> bool:
    if await moderation_repo.is_moderator(user_id):
        return True
    member = guild.get_member(user_id)
    return bool(member and member.guild_permissions.administrator)


def get_all_reviewer_role_ids() -> list:
    return [ALLOWED_ROLE_ID, REVIEWER_ROLE_ID_2, SPAM_VOTE_ROLE_ID,
            EXTRA_REVIEWER_ROLE_1, EXTRA_REVIEWER_ROLE_2, EXTRA_REVIEWER_ROLE_3]


def _has_any_role(member, role_ids) -> bool:
    return any(r.id in role_ids for r in member.roles)


def user_can_review(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or _has_any_role(
        member, set(get_all_reviewer_role_ids()))


def user_can_spam_vote(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or _has_any_role(
        member, {SPAM_VOTE_ROLE_ID})


def collect_reviewer_ids(guild: discord.Guild) -> set:
    ids = set()
    for rid in get_all_reviewer_role_ids():
        role = guild.get_role(rid) if rid else None
        if role:
            ids.update(m.id for m in role.members)
    ids.update(m.id for m in guild.members if m.guild_permissions.administrator)
    if guild.me:
        ids.add(guild.me.id)
    return ids


async def is_user_blocked(discord_id: int):
    entry = await users_repo.get_user(discord_id)
    if isinstance(entry, str) or not isinstance(entry, dict):
        return False, ""
    typ = entry.get("type", "temp")
    reason = entry.get("reason") or ""
    suffix = f" Причина: {reason}" if reason and reason != "-" else ""
    if typ == "perm":
        return True, "⛔ Вам **перманентно запрещено** подавать заявки." + suffix
    if typ == "temp":
        try:
            until = datetime.datetime.fromisoformat(entry.get("until"))
        except Exception:
            return False, ""
        now = now_utc()
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        if now < until:
            rem = until - now
            h, m = int(rem.seconds // 3600), int((rem.seconds % 3600) // 60)
            return True, f"🚫 Вам **запрещено** подавать заявки. Через **{h} ч. {m} мин.**" + suffix
        await users_repo.remove_user(discord_id)
    return False, ""


def _fmt_timestamp(ts):
    if not ts:
        return "-"
    if ts == 0:
        return "перманентно"
    try:
        return datetime.datetime.fromtimestamp(ts, UTC).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(ts)


def build_activity_bar(hours: float, goal: float = 8.0, length: int = 10) -> str:
    ratio = max(0.0, min(1.0, hours / goal)) if goal > 0 else 0.0
    filled = int(round(ratio * length))
    if hours <= 0:
        e, s = "⚫", "Не играет"
    elif hours < goal * 0.25:
        e, s = "🔴", "Очень мало"
    elif hours < goal * 0.5:
        e, s = "🟠", "Мало"
    elif hours < goal * 0.75:
        e, s = "🟡", "Средне"
    elif hours < goal:
        e, s = "🟢", "Хорошо"
    elif hours == goal:
        e, s = "🟢", "Выполнено"
    else:
        e, s = "🟢", "Отлично"
    extra = f" (+{hours - goal:.1f} ч.)" if hours > goal else ""
    return f"`{'█' * filled}{'░' * (length - filled)}` {hours:.1f} / {goal:.0f} ч. - {e} {s}{extra}"


async def get_player_activity(steam_id: str) -> float:
    entry = await players_repo.get_player(steam_id)
    a = entry.get("activity", {})
    if not isinstance(a, dict):
        return 0.0
    total = 0.0
    for h in a.values():
        try:
            total += float(h)
        except (ValueError, TypeError):
            continue
    return total


def extract_discord_id_from_embed(embed: discord.Embed) -> int:
    for f in embed.fields:
        if f.name == "Discord (упоминание)":
            m = re.search(r"\d{17,20}", f.value)
            if m:
                return int(m.group(0))
    return 0


# ============================================================
# ПРОФИЛЬ
# ============================================================
async def build_player_profile(guild, discord_id, steam_id=None):
    profile = {"discord_id": str(discord_id), "updated_at": now_utc().isoformat()}
    member = guild.get_member(discord_id)
    if member:
        profile["discord_name"] = member.display_name
        profile["discord_joined"] = member.joined_at.isoformat() if member.joined_at else None
    if not steam_id:
        steam_id = await links_repo.get_link(discord_id)
    if not steam_id:
        return profile
    profile["steamid64"] = steam_id
    user = await api.cs2red_user(steam_id)
    if user and not user.get("_not_found"):
        profile["cs2red_nick"] = user.get("nick", "-")
        profile["cs2red_role_id"] = user.get("roleId")
        profile["cs2red_role_name"] = role_display_name(user.get("roleId"))
    full = await api.cs2red_full_user(steam_id)
    if full:
        profile["cs2red_admins"] = full.get("admins", [])
        profile["cs2red_vips"] = full.get("vips", [])
        inner = full.get("user") or {}
        if inner:
            profile["cs2red_nick"] = inner.get("nick", profile.get("cs2red_nick"))
            profile["cs2red_role_id"] = inner.get("roleId", profile.get("cs2red_role_id"))
    profile["activity_hours"] = await get_player_activity(steam_id)
    summ = await api.steam_summary(steam_id)
    if summ:
        profile["steam_avatar"] = summ.get("avatarfull")
        profile["steam_status"] = {0: "offline", 1: "online", 2: "busy", 3: "away"}.get(
            summ.get("personastate"), "unknown")
        profile["steam_nick"] = summ.get("personaname")
    sb = await api.steam_bans(steam_id)
    if sb:
        profile["steam_bans"] = sb
    return profile


async def send_player_report(bot, profile):
    channel = bot.get_channel(DB_CHANNEL_ID) if DB_CHANNEL_ID else None
    if not channel:
        log(f"⚠️ Канал {DB_CHANNEL_ID} не найден.")
        return
    embed = discord.Embed(title="👤 Профиль игрока", color=0x2B2D31, timestamp=now_utc())

    def field(name, value, inline=False):
        if value:
            embed.add_field(name=name, value=str(value)[:1024], inline=inline)

    if profile.get("discord_name"):
        field("Discord", f"{profile['discord_name']} (`{profile['discord_id']}`)")
    field("SteamID64", f"`{profile.get('steamid64')}`" if profile.get("steamid64") else None)
    field("Ник на cs2red", profile.get("cs2red_nick"))
    if profile.get("cs2red_role_name"):
        field("Роль на cs2red",
              f"{profile['cs2red_role_name']} (roleId={profile.get('cs2red_role_id')})")
    field("Ник в Steam", profile.get("steam_nick"))
    field("Статус Steam", profile.get("steam_status"))
    field("📝 Заметки", profile.get("notes"))
    field("📊 Активность за неделю",
          build_activity_bar(profile.get("activity_hours", 0.0), ACTIVITY_GOAL_HOURS))
    sb = profile.get("steam_bans", {})
    if sb:
        vl = ["❌ **VAC-бан**" if sb.get("vac_banned") else "✅ VAC-банов нет"]
        if sb.get("community_banned"):
            vl.append("❌ **Бан сообщества**")
        if sb.get("number_of_game_bans", 0) > 0:
            vl.append(f"❌ **Игровых банов**: {sb['number_of_game_bans']}")
        if sb.get("economy_ban") and sb["economy_ban"] != "none":
            vl.append(f"❌ **Economy**: {sb['economy_ban']}")
        field("🛡️ Steam-баны", "\n".join(vl))
    history = []
    if sb.get("vac_banned"):
        history.append("❌ VAC-бан")
    h = profile.get("activity_hours", 0.0)
    if h < ACTIVITY_MIN_HOURS:
        history.append(f"⚠️ Малоактивный ({h:.1f} ч. < {ACTIVITY_MIN_HOURS})")
    field("Итог", "\n".join(history) or "✅ Чист")
    admins = profile.get("cs2red_admins") or []
    if admins:
        lines = [f"👑 roleId={a.get('roleId', '-')} до "
                 f"{'перманентно' if a.get('expires', 0) == 0 else _fmt_timestamp(a.get('expires'))}"
                 for a in admins[:10]]
        if len(admins) > 10:
            lines.append(f"...и ещё {len(admins) - 10}")
        field(f"👑 Админ-права ({len(admins)})", "\n".join(lines))
    vips = [v for v in (profile.get("cs2red_vips") or [])
            if v.get("expires", 0) == 0 or v.get("expires", 0) >= int(now_utc().timestamp())]
    if vips:
        field(f"⭐ VIP ({len(vips)})",
              "\n".join(f"⭐ {v.get('group', '-')}" for v in vips[:10]))
    if profile.get("steam_avatar"):
        embed.set_thumbnail(url=profile["steam_avatar"])
    embed.set_footer(text=f"Обновлено: {profile.get('updated_at', '-')}")
    await safe_send(channel, embed=embed)


# ============================================================
# ЛОГИ
# ============================================================
async def _send(bot, channel_id, text):
    if not channel_id:
        return
    ch = bot.get_channel(channel_id)
    if ch:
        await safe_send(ch, text[:1900] + ("\n...(обрезано)" if len(text) > 1900 else ""))


async def log_to_channel(bot, text):
    if LOG_CHANNEL_ID:
        await _send(bot, LOG_CHANNEL_ID, text)


async def log_to_applications(bot, text):
    if APPLICATION_LOGS_CHANNEL_ID:
        await _send(bot, APPLICATION_LOGS_CHANNEL_ID, text)


def utc_stamp() -> str:
    return now_utc().strftime("%d.%m.%Y %H:%M")


# ============================================================
# СИНХРОНИЗАЦИЯ РОЛЕЙ
# ============================================================
async def sync_role_by_id(guild, member, steam_id):
    user = await api.cs2red_user(steam_id)
    if user is None:
        return f"⚠️ {member.display_name}: cs2red не ответил, роль не тронута"
    if user.get("_not_found"):
        tn = None
    else:
        tn = ROLE_MAP.get(user.get("roleId"))
    managed = {n for n in ROLE_MAP.values() if n}
    cur = {r for r in member.roles if r.name in managed}

    if tn is None:
        if cur:
            for r in cur:
                await member.remove_roles(r)
            return f"🔄 {member.display_name}: сняты"
        return f"ℹ️ {member.display_name}: без изменений"

    target = discord.utils.get(guild.roles, name=tn)
    if target is None:
        return f"⚠️ роль '{tn}' не найдена"

    if cur == {target}:
        return f"✅ {member.display_name}: уже '{tn}'"

    for r in cur:
        if r != target:
            await member.remove_roles(r)
    try:
        await member.add_roles(target)
        return f"✅ {member.display_name}: '{tn}'"
    except discord.Forbidden:
        return "❌ нет прав"


CLAN_ROLE_COLOR = 0x8E7CF3  # приятный фиолетовый — Discord не умеет градиенты через бота


async def _find_or_create_clan_role(guild, member, clan_name: str, reason: str = "автосинхронизация"):
    """Находит роль по имени клана или создаёт её.

    Возвращает (role, status_text) — status_text непуст, если роль создана.
    """
    # 1) Повторно не создаём: роль по названию клана.
    target = discord.utils.get(guild.roles, name=clan_name)
    if target is not None:
        return target, None

    # 2) Проверяем права бота на управление ролями.
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return None, "❌ нет прав Manage Roles (бот не может создать роль клана)"

    # 3) Позиция: на 1 выше top_role участника.
    pos = (member.top_role.position + 1) if member.top_role else 1

    try:
        target = await guild.create_role(
            name=clan_name,
            color=discord.Colour(CLAN_ROLE_COLOR),
            hoist=False,
            mentionable=False,
            reason=reason,
        )
        # Ставим позицию выше роли участника (клип let Discord сортирует админ-роли).
        try:
            await target.edit(position=pos, reason=reason)
        except discord.Forbidden:
            pass
        return target, f"создана роль клана '{clan_name}'"
    except discord.Forbidden:
        return None, "❌ нет прав Manage Roles (не удалось создать роль клана)"
    except discord.HTTPException as e:
        return None, f"❌ ошибка создания роли: {e}"


async def sync_clan_role_by_id(guild, member, steam_id):
    # Проверяем права бота заранее (Manage Roles нужен для создания роли).
    me = guild.me
    if me is not None and not me.guild_permissions.manage_roles:
        log("⚠️ sync_clan: бот не имеет права Manage Roles — роль создать не удастся")

    user = await api.cs2red_user(steam_id)
    if user is None:
        return f"⚠️ {member.display_name}: cs2red не ответил, роли не тронуты"

    clans = await api.cs2red_clans()
    if clans is None:
        return f"⚠️ {member.display_name}: /api/clans не ответил, роли не тронуты"

    if not clans:
        return f"⚠️ {member.display_name}: /api/clans пусто, роли не тронуты"

    total_members = sum(len(c.get("members") or []) for c in clans)
    if total_members == 0:
        return f"⚠️ {member.display_name}: в /api/clans нет members, роли не тронуты"

    clan = await api.player_clan(steam_id)
    # Роли кланов, которыми управляем: известные по CLAN_ROLE_MAP + создаваемые по имени.
    managed = set(CLAN_ROLE_MAP.values())

    if clan is None:
        # Игрок не в клане — снимаем все управляемые роли клана.
        held = [r for r in member.roles if r.id in managed or r.name in {
            c.get("name") for c in clans}]
        if held:
            for r in held:
                try:
                    await member.remove_roles(r)
                except discord.Forbidden:
                    pass
            return "🔄 клан снят"
        return "ℹ️ без клана"

    clan_name = clan.get("clan_name") or ""
    if not clan_name:
        return "ℹ️ клан без имени"

    # Ищем роль по имени (или по зарезервированному id в CLAN_ROLE_MAP).
    target = discord.utils.get(guild.roles, name=clan_name)
    created_status = None
    if target is None:
        tid = CLAN_ROLE_MAP.get(clan.get("clan_id"))
        if tid is not None:
            target = guild.get_role(tid)
        if target is None:
            # Роли нет — создаём.
            target, created_status = await _find_or_create_clan_role(guild, member, clan_name)
            if target is None:
                return f"⚠️ {created_status}" or "⚠️ не удалось создать роль клана"

    # Снимаем прочие управляемые роли клана.
    held = [r for r in member.roles if (r.id in managed or r.name in {
        c.get("name") for c in clans}) and r != target]
    for r in held:
        try:
            await member.remove_roles(r)
        except discord.Forbidden:
            pass

    if target in member.roles:
        return f"✅ уже в '{clan_name}'" + (f" ({created_status})" if created_status else "")
    try:
        await member.add_roles(target)
        return f"✅ выдан клан '{clan_name}'" + (f" ({created_status})" if created_status else "")
    except discord.Forbidden:
        return "❌ нет прав выдать роль"


# ============================================================
# ВЕТКИ ЗАЯВОК
# ============================================================
async def close_application_thread(thread, closed_by, reason):
    if not isinstance(thread, discord.Thread):
        return
    try:
        try:
            await safe_send(thread, f"🔒 **Ветка закрыта.** {reason}\n**Закрыл:** {closed_by.mention}")
        except Exception:
            pass
        await asyncio.sleep(5)
        try:
            await thread.edit(archived=True, locked=True, reason=f"Заявка {reason}")
        except Exception:
            pass
        keep = collect_reviewer_ids(thread.guild)
        try:
            members = await thread.fetch_members()
        except Exception:
            members = []
        for m in members:
            if m.id not in keep:
                try:
                    await thread.remove_user(m)
                except Exception:
                    pass
    except Exception as e:
        log(f"⚠️ close_thread: {e}")


# ============================================================
# ГОЛОСА
# ============================================================
def _new_app_id() -> str:
    """Идентификатор заявки. Один на заявку, а не на сообщение.

    Заявка публикуется сразу в двух местах: в канале заявок и в
    отдельной ветке. Раньше голоса считались по id сообщения, поэтому
    у одной заявки было две независимые записи голосов и можно было
    проголосовать «за» в канале, а «против» — в ветке.
    """
    return uuid.uuid4().hex[:10]


def _app_id_of(button) -> str:
    """Достаёт app_id из custom_id кнопки вида 'accept_btn:<app_id>'."""
    cid = getattr(button, "custom_id", "") or ""
    return cid.split(":", 1)[1] if ":" in cid else ""


def _app_id_for(interaction, button=None) -> str:
    """app_id заявки из кнопки; fallback — id сообщения (старые сообщения)."""
    app_id = _app_id_of(button) if button is not None else ""
    if app_id:
        return app_id
    message = getattr(interaction, "message", None)
    return str(message.id) if message is not None else ""


async def _get_votes(app_id):
    return await votes_repo.get_votes(app_id)


async def _save_votes(app_id, st):
    await votes_repo.save_votes(app_id, st)


async def _vote(interaction, kind, button=None):
    await interaction.response.defer()
    if not user_can_review(interaction.user):
        await safe_send(interaction.followup, "❌ Нет прав.", ephemeral=True)
        return
    app_id = _app_id_for(interaction, button)
    st, applied = await votes_repo.apply_vote(
        app_id, kind, interaction.user.id,
        allow_repeat=user_can_spam_vote(interaction.user),
    )
    if not applied:
        await safe_send(interaction.followup, "Вы уже голосовали.", ephemeral=True)
        return

    view = ApplicationManagement(app_id)
    await view.update_labels()
    if st[kind] >= VOTES_REQUIRED:
        embed = interaction.message.embeds[0]
        status = "Принят" if kind == "accept" else "Отклонен"
        embed.set_footer(text=f"Quantum • Статус: {status}")
        for c in view.children:
            c.disabled = True
        try:
            await interaction.message.edit(embed=embed, view=view)
        except Exception:
            pass
        tid = extract_discord_id_from_embed(embed)
        member = interaction.guild.get_member(tid) if tid else None
        if kind == "accept" and member and AUTO_ROLE_ID:
            role_to_give = interaction.guild.get_role(AUTO_ROLE_ID)
            if role_to_give:
                try:
                    await member.add_roles(role_to_give)
                    await log_to_channel(interaction.client,
                        f"✅ Заявка принята. {member.mention} получил {role_to_give.mention}")
                except discord.Forbidden:
                    pass
        if member:
            await log_to_channel(interaction.client,
                f"{'✅' if kind == 'accept' else '❌'} {member.mention}")
        # Закрываем активную заявку в БД (анти-дубликат освобождается).
        if tid:
            try:
                await applications_repo.finalize_application(
                    tid, "accepted" if kind == "accept" else "rejected",
                    moderator_id=interaction.user.id)
            except Exception as e:
                log(f"⚠️ Не финализировал заявку: {e}")
        if isinstance(interaction.channel, discord.Thread):
            await close_application_thread(interaction.channel, interaction.user, status)
    else:
        try:
            await interaction.message.edit(view=view)
        except Exception:
            pass


# ============================================================
# МЕНЮ ЗАЯВКИ
# ============================================================
class ApplicationManagement(discord.ui.View):
    """Кнопки заявки.

    app_id зашит в custom_id кнопок: заявка публикуется в канале и в
    ветке, и обе копии должны делить одни и те же голоса. Без app_id
    в custom_id две заявки конфликтовали бы в реестре persistent-view.
    """

    def __init__(self, app_id: str = ""):
        super().__init__(timeout=None)
        self.app_id = str(app_id or "")
        if self.app_id:
            # discord.py копирует кнопки в экземпляр: правка не влияет
            # на сам класс и на другие экземпляры.
            for child in self.children:
                if child.custom_id:
                    child.custom_id = f"{child.custom_id}:{self.app_id}"

    async def update_labels(self, mid=None):
        st = await _get_votes(self.app_id or mid)
        for c in self.children:
            base = (c.custom_id or "").split(":", 1)[0]
            if base == "accept_btn":
                c.label = f"Принять {st['accept']}/{VOTES_REQUIRED}"
            elif base == "deny_btn":
                c.label = f"Отклонить {st['deny']}/{VOTES_REQUIRED}"
            elif base == "voted_list_btn":
                c.label = f"👥 {len(st['voters'])}"

    @discord.ui.button(label="🙋 Взять", style=discord.ButtonStyle.primary,
                       custom_id="take_btn", row=0)
    async def take(self, i, b):
        await i.response.defer()
        if not user_can_review(i.user):
            await safe_send(i.followup, "❌", ephemeral=True)
            return
        await safe_send(i.followup, f"🙋 Взял {i.user.mention}.")
        b.disabled = True
        try:
            await i.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Принять 0/2", style=discord.ButtonStyle.success,
                       custom_id="accept_btn", row=0)
    async def accept(self, i, b):
        await _vote(i, "accept", b)

    @discord.ui.button(label="Отклонить 0/2", style=discord.ButtonStyle.danger,
                       custom_id="deny_btn", row=0)
    async def deny(self, i, b):
        await _vote(i, "deny", b)

    @discord.ui.button(label="🚫 Выдать запрет", style=discord.ButtonStyle.secondary,
                       custom_id="tempban_btn", row=1)
    async def temp_ban(self, i, b):
        if not user_can_review(i.user):
            await i.response.send_message("❌", ephemeral=True)
            return
        tid = extract_discord_id_from_embed(i.message.embeds[0])
        if not tid:
            await i.response.send_message("❌ ID не найден", ephemeral=True)
            return
        await i.response.send_modal(TempBanModal(tid))

    @discord.ui.button(label="⛔ Перм. запрет", style=discord.ButtonStyle.danger,
                       custom_id="permban_btn", row=1)
    async def perm_ban(self, i, b):
        await i.response.defer()
        if not user_can_review(i.user):
            await safe_send(i.followup, "❌", ephemeral=True)
            return
        tid = extract_discord_id_from_embed(i.message.embeds[0])
        if not tid:
            await safe_send(i.followup, "❌", ephemeral=True)
            return
        await users_repo.save_user(tid, {
            "type": "perm", "until": None, "reason": f"Выдал {i.user}"})
        await safe_send(i.followup, f"⛔ <@{tid}> перм. запрет")
        await log_to_channel(i.client, f"⛔ perm <@{tid}> от {i.user.mention}")

    @discord.ui.button(label="✅ Снять запрет", style=discord.ButtonStyle.success,
                       custom_id="unban_btn", row=1)
    async def unban(self, i, b):
        await i.response.defer()
        if not user_can_review(i.user):
            await safe_send(i.followup, "❌", ephemeral=True)
            return
        tid = extract_discord_id_from_embed(i.message.embeds[0])
        if not tid:
            await safe_send(i.followup, "❌", ephemeral=True)
            return
        had = await users_repo.get_user(tid)
        await users_repo.remove_user(tid)
        await safe_send(i.followup, f"✅ снят с <@{tid}>" if had else "ℹ️ нет")
        if had:
            await log_to_channel(i.client, f"✅ unban <@{tid}> от {i.user.mention}")

    @discord.ui.button(label="👥 0", style=discord.ButtonStyle.secondary,
                       custom_id="voted_list_btn", row=1)
    async def voted_list(self, i, b):
        st = await _get_votes(_app_id_for(i, b))
        if not st["voters"]:
            await i.response.send_message("Пусто", ephemeral=True)
            return
        await i.response.send_message("Голосовали: " + ", ".join(f"<@{u}>" for u in st["voters"]),
                                      ephemeral=True)


class TempBanModal(discord.ui.Modal, title="Запрет"):
    def __init__(self, tid):
        super().__init__()
        self.tid = tid
        self.hours = discord.ui.TextInput(label="Часов", default="12", max_length=6)
        self.reason = discord.ui.TextInput(label="Причина", required=False, max_length=200)
        self.add_item(self.hours)
        self.add_item(self.reason)

    async def on_submit(self, i):
        try:
            hours = int(self.hours.value.strip())
            if hours <= 0:
                raise ValueError
        except ValueError:
            await i.response.send_message("❌ число", ephemeral=True)
            return
        until = (now_utc() + datetime.timedelta(hours=hours)).isoformat()
        await users_repo.save_user(self.tid, {
            "type": "temp", "until": until, "reason": self.reason.value.strip() or "-"})
        await i.response.send_message(f"🚫 <@{self.tid}> запрет на {hours} ч.")
        await log_to_channel(i.client, f"🚫 temp <@{self.tid}> {hours}ч от {i.user.mention}")

# ============================================================
# МОДАЛЬНОЕ ОКНО ЗАЯВКИ
# ============================================================
class QuestionnaireModal(discord.ui.Modal, title="Заявка в клан"):
    def __init__(self, clan_name):
        super().__init__()
        self.clan_name = clan_name
        self.nickname = discord.ui.TextInput(label="Ваш игровой никнейм")
        self.steam_input = discord.ui.TextInput(label="Steam-профиль (ссылка или SteamID64)")
        self.age = discord.ui.TextInput(label="Ваш возраст")
        self.time = discord.ui.TextInput(label="Время (по МСК)")
        self.reason = discord.ui.TextInput(label="Комментарий",
                                            style=discord.TextStyle.paragraph, required=False)
        for it in (self.nickname, self.steam_input, self.age, self.time, self.reason):
            self.add_item(it)

    async def on_submit(self, i):
        # Отвечаем сразу (defer) — Discord ждёт 3 секунды. Дальше идут
        # проверки БД (бан, дубликат заявки): они уже не тормозят ответ.
        try:
            await i.response.defer(ephemeral=False)
        except discord.InteractionResponded:
            pass
        except discord.HTTPException as e:
            log(f"⚠️ defer формы заявки: {e}")
        blocked, rt = await is_user_blocked(i.user.id)
        if blocked:
            await safe_send(i.followup, rt, ephemeral=True)
            return

        # Анти-дубликат: если у пользователя уже есть активная заявка — не создаём новую.
        existing = await applications_repo.get_active_application(i.user.id)
        if existing:
            await safe_send(
                i.followup,
                "❌ У вас уже есть заявка на рассмотрении. Дождитесь её завершения.",
                ephemeral=True,
            )
            return

        m = re.search(r"\d{17}", self.steam_input.value.strip())
        sid = m.group(0) if m else None
        if sid:
            steam_url = f"https://steamcommunity.com/profiles/{sid}"
            cs2red_url = f"https://cs2red.ru/profile/{sid}"
            csstats_url = f"https://csstats.gg/player/{sid}"
            sid_d = f"`{sid}`"
        else:
            steam_url = cs2red_url = csstats_url = "https://cs2red.ru"
            sid_d = "`- не распознан -`"
        e = discord.Embed(title="ЗАЯВКА В КЛАН", color=APPLY_EMBED_COLOR)
        e.add_field(name="Игрок", value=self.nickname.value, inline=False)
        e.add_field(name="Steam", value=f"[перейти]({steam_url})", inline=False)
        e.add_field(name="Сайт", value=f"[перейти]({cs2red_url})", inline=False)
        e.add_field(name="csstats", value=f"[перейти]({csstats_url})", inline=False)
        e.add_field(name="Возраст", value=f"```\n{self.age.value}\n```", inline=False)
        e.add_field(name="Discord", value=f"```\n{i.user.name}\n```", inline=False)
        e.add_field(name="МСК", value=f"```\n{self.time.value}\n```", inline=False)
        if self.reason.value and self.reason.value.strip():
            e.add_field(name="Комментарий", value=f"```\n{self.reason.value}\n```", inline=False)
        e.add_field(name="SteamID", value=sid_d, inline=False)
        e.add_field(name="Discord (упоминание)", value=i.user.mention, inline=False)
        e.set_thumbnail(url=i.user.display_avatar.url)
        e.set_footer(text=f"{CLAN_NAME} • {utc_stamp()}")

        ch = i.guild.get_channel(APPLICATIONS_CHANNEL_ID) if APPLICATIONS_CHANNEL_ID else None
        # Один app_id на заявку: сообщение в канале и сообщение в ветке
        # должны показывать одни и те же счётчики голосов.
        app_id = _new_app_id()
        msg = None
        if ch:
            try:
                view = ApplicationManagement(app_id)
                msg = await safe_send(ch, content=f"🔔 Новая заявка от {i.user.mention}",
                                      embed=e, view=view)
                if msg:
                    await view.update_labels()
                    await msg.edit(view=view)
            except Exception as ex:
                log(f"⚠️ Канал заявок: {ex}")

        # Фиксируем заявку в БД (анти-дубликат + ссылка на сообщение заявки).
        try:
            await applications_repo.upsert_application(
                i.user.id,
                clan_name=self.clan_name,
                status="pending",
                message_id=str(msg.id) if msg else "",
                channel_id=str(ch.id) if ch else "",
                app_id=app_id,
            )
        except Exception as ex:
            log(f"⚠️ Не сохранил заявку в БД: {ex}")

        await safe_send(i.followup, "✅ Ваша заявка отправлена на рассмотрение!", ephemeral=True)
        asyncio.create_task(_create_application_thread(i, e, app_id))


async def _create_application_thread(i, e, app_id: str = ""):
    try:
        if not THREADS_CHANNEL_ID:
            return
        ch = i.guild.get_channel(THREADS_CHANNEL_ID)
        if ch is None:
            return
        t = await ch.create_thread(name=f"заявка-{i.user.name.lower()}"[:100],
                                   type=discord.ChannelType.private_thread,
                                   invitable=False, auto_archive_duration=1440)
        try:
            await t.add_user(i.user)
        except Exception:
            pass
        added, tasks_ = set(), []
        for rid in get_all_reviewer_role_ids():
            role = i.guild.get_role(rid) if rid else None
            if role:
                for m in role.members:
                    if m.id not in added and not m.bot:
                        added.add(m.id)
                        tasks_.append(t.add_user(m))
        for x in range(0, len(tasks_), 20):
            await asyncio.gather(*tasks_[x:x + 20], return_exceptions=True)
        # Тот же app_id, что и у сообщения в канале: голоса общие.
        view = ApplicationManagement(app_id)
        msg = await safe_send(t, content=f"{i.user.mention}, ваша заявка на рассмотрении.",
                              embed=e, view=view)
        if msg:
            await view.update_labels()
            await msg.edit(view=view)
    except Exception as ex:
        log(f"⚠️ Ветка: {ex}")


async def open_application_modal(i, clan_name: str) -> None:
    """Открывает модальное окно заявки. Первое действие — сам ответ.

    Discord даёт 3 секунды на ответ, иначе взаимодействие протухает
    (10062 «Приложение не ответило вовремя»). Поэтому здесь нет ни
    запросов к БД, ни обращений к API: проверки выполняются уже внутри
    модального окна (QuestionnaireModal.on_submit).
    """
    try:
        await i.response.send_modal(QuestionnaireModal(clan_name))
        return
    except discord.InteractionResponded:
        pass  # уже отвечено — отправим через followup
    except discord.HTTPException as e:
        log(f"⚠️ send_modal: {e}")
    try:
        await i.followup.send_modal(QuestionnaireModal(clan_name))
    except discord.HTTPException as e:
        log(f"⚠️ followup.send_modal: {e}")
        await safe_send(i.followup,
                        "❌ Не удалось открыть форму заявки. Попробуйте ещё раз.",
                        ephemeral=True)


class ClanSelect(discord.ui.Select):
    """Выбор клана для подачи заявки.

    Фиксированный custom_id обязателен: без него discord.py не находит
    обработчик после рестарта бота (Render redeploy) и Discord отвечает
    «Приложение не ответило вовремя» (10062).
    """

    def __init__(self):
        super().__init__(
            custom_id=CLAN_SELECT_CUSTOM_ID,
            placeholder="Выберите клан",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=CLAN_NAME,
                                          description=f"Клан {CLAN_NAME}")],
        )

    async def callback(self, i):
        # Первое и единственное действие — ответ модальным окном.
        # Никаких запросов к БД/API до ответа: Discord ждёт 3 секунды,
        # иначе взаимодействие протухает (10062).
        # Проверки (дубликат заявки, бан) делаются внутри on_submit.
        await open_application_modal(i, self.values[0])


class LegacyClanSelect(discord.ui.DynamicItem, template=r"^[0-9a-f]{32}$"):
    """Обработчик старых выпадающих списков.

    До фикса custom_id генерировался случайно, поэтому после рестарта
    бота Discord не находил обработчик и возвращал 10062. Этот item
    ловит такие (уже опубликованные) списки по их случайному custom_id.
    """

    def __init__(self, item):
        super().__init__(item)
        self.clan_name = CLAN_NAME

    async def callback(self, i):
        # Проверку делаем здесь, а не в interaction_check: если проверка
        # отклонит взаимодействие, ответ не уйдёт и снова будет 10062.
        msg = getattr(i, "message", None)
        user = getattr(getattr(i, "client", None), "user", None)
        author_id = getattr(getattr(msg, "author", None), "id", None)
        if msg is None or user is None or author_id != user.id:
            try:
                await i.response.send_message(
                    "❌ Этот список устарел. Напишите в чат клана — пришлём "
                    "новую форму заявки.", ephemeral=True)
            except discord.HTTPException as e:
                log(f"⚠️ ответ на устаревший список: {e}")
            return
        await open_application_modal(i, self.clan_name)


class ClanView(discord.ui.View):
    """Персистентный view: работает и после рестарта бота.

    timeout=None + явный custom_id у select'а позволяют зарегистрировать
    view через bot.add_view() — тогда старое сообщение в канале продолжает
    работать, даже если бот перезапустили.
    """

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ClanSelect())
        self.add_item(discord.ui.Button(label=CLAN_NAME, url=CLAN_URL, row=1))


# ============================================================
# БОТ
# ============================================================
PUBLIC_COMMANDS = {"link", "unlink", "ping", "health", "lookup", "ask", "rules"}


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def interaction_check(self, i):
        cmd = getattr(i, "command", None)
        if cmd is None or cmd.name in PUBLIC_COMMANDS:
            return True
        if not await is_moderator(i.user.id, i.guild) and not i.response.is_done():
            await i.response.send_message("❌ Нет доступа.", ephemeral=True)
            return False
        return True

    async def setup_hook(self):
        try:
            log("🔧 setup_hook: начало")
            await init_db()
            await import_json_to_sqlite(DATA_DIR)
            log("✅ SQLite готов (init_db + миграция)")
            await api.start()
            log("✅ API session ready")

            # GitHub-синк данных (если настроен): подтягиваем отсутствующие файлы.
            if github_sync.enabled and api.session:
                try:
                    _saved = await github_sync.pull(
                        api.session,
                        ["links.json", "players_db.json", "users_db.json",
                         "moderators.json", "votes.json", "seen.json"],
                        DATA_DIR,
                    )
                    log(f"🐙 GitHub-синк: получено {len(_saved)} файлов")
                except Exception as e:
                    log(f"⚠️ GitHub pull: {type(e).__name__}: {e}")

            global ai_service
            ai_service = AIService(api.session, data_dir=str(DATA_DIR))
            n_providers = len(ai_service.providers)
            if n_providers == 0:
                # Ключей нет — бот работает, но ИИ-команды сообщат о недоступности.
                log("⚠️ AI провайдеры не настроены: задайте GEMINI_API_KEY, "
                    "OPENROUTER_API_KEY или LLM7_API_KEY.")
            else:
                n_rules = ai_service.ensure_rules()
                log(f"✅ AI-сервис готов (провайдеров: {n_providers}, "
                    f"правил: {n_rules})")

            # Persistent-view и фоновые задачи нужны ВСЕГДА, даже если
            # GUILD_ID не задан: иначе бот поднимается «живым», но кнопки
            # заявок не работают и фоновые задачи не крутятся.
            self.add_view(ApplicationManagement())
            log("✅ ApplicationManagement view добавлен")
            # Персистентный view выбора клана: опубликованное ранее сообщение
            # со списком продолжает работать после рестарта бота.
            self.add_view(ClanView())
            log("✅ ClanView (выбор клана) добавлен")
            # Ловим старые списки со случайным custom_id (иначе 10062).
            try:
                self.add_dynamic_items(LegacyClanSelect)
                log("✅ LegacyClanSelect добавлен")
            except Exception as e:
                log(f"⚠️ LegacyClanSelect: {e}")
            restored = await self._restore_application_views()
            log(f"✅ Кнопки открытых заявок восстановлены: {restored}")
            self.add_view(PanelView())
            log("✅ PanelView добавлен")

            if GUILD_ID:
                g = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=g)
                log(f"🔄 Синхронизация команд для guild {GUILD_ID}...")
                synced = await self.tree.sync(guild=g)
                log(f"✅ Команд синхронизировано: {len(synced)}")
            else:
                log("⚠️ GUILD_ID = 0 — команды синхронизируются только глобально")

            auto_sync.start()
            activity_tracker.start()
            hourly_clan_sync.start()
            agent_watch.start()
            log("✅ Фоновые задачи запущены")
        except Exception as e:
            log(f"❌ ОШИБКА в setup_hook: {type(e).__name__}: {e}")
            traceback.print_exc()
            raise

    async def _restore_application_views(self) -> int:
        """Поднимает кнопки заявок, которые остались на рассмотрении.

        Без этого после рестарта бота кнопки на сообщениях с открытыми
        заявками перестают работать.
        """
        try:
            pending = await applications_repo.list_pending_applications()
        except Exception as e:
            log(f"⚠️ Не удалось прочитать заявки: {e}")
            return 0
        count = 0
        for rec in pending:
            app_id = (rec.get("app_id") or "").strip()
            if not app_id:
                # Заявка создана до появления app_id: её кнопки работают
                # по id сообщения, отдельный view не нужен.
                continue
            try:
                self.add_view(ApplicationManagement(app_id))
                count += 1
            except Exception as e:
                log(f"⚠️ Не поднял view заявки {app_id}: {e}")
        return count

    async def close(self):
        await api.close()
        await close_db()
        await super().close()


bot = MyBot()

# ИИ-агент: наблюдает за системой, пишет об ошибках и предлагает идеи.
agent = Agent(bot, AGENT_CHANNEL_ID)

# ИИ-сервис (каскад провайдеров + кэш + RAG). Инициализируется в setup_hook.
ai_service: AIService | None = None


@bot.tree.error
async def on_app_command_error(i, err):
    if isinstance(err, discord.app_commands.CheckFailure):
        msg = "❌ Нет доступа."
        try:
            if not i.response.is_done():
                await i.response.send_message(msg, ephemeral=True)
            else:
                await safe_send(i.followup, msg, ephemeral=True)
        except Exception:
            pass
        return
    await log_error(i.client, f"cmd:{i.command}", err)


@bot.event
async def on_ready():
    log(f"✅ Бот {bot.user} готов!")
    log(f"🆔 GUILD_ID: {GUILD_ID}")
    log(f"🍪 CS2RED cookies: {len(CS2RED_COOKIES)} шт.")
    log(f"📡 Guilds: {[g.name for g in bot.guilds]}")


@bot.event
async def on_member_join(member):
    """Авто-привязка нового участника к Steam ID по его нику при входе на сервер."""
    try:
        # Пропускаем ботов.
        if member.bot:
            return

        # Уже привязан в БД?
        ex = await links_repo.get_link(member.id)
        if ex:
            return

        nick = (member.display_name or "").strip()
        if not nick:
            return

        await api._acquire_rate_slot()
        users = await api.cs2red_search_by_nick(nick)
        if users is None:
            # Тихо логируем ошибку cs2red (403/429/сбой) — не падаем.
            reason = api.blocked_reason or getattr(api, "last_status", None)
            log(f"🐣 on_member_join {member}: cs2red не ответил ({reason})")
            return

        if len(users) == 1:
            steam_id = api.extract_steam64(users[0])
            if steam_id:
                await links_repo.add_link(member.id, steam_id)
                c_nick = users[0].get("nick") or nick
                log(f"✅ Автопривязка при входе: {member.display_name} → `{steam_id}` "
                    f"(ник на cs2red: {c_nick})")
                # Лог в канал.
                if LOG_CHANNEL_ID:
                    ch = bot.get_channel(LOG_CHANNEL_ID)
                    if ch is not None:
                        try:
                            await safe_send(
                                ch,
                                f"✅ Автопривязка при входе: {member.mention} → `{steam_id}` "
                                f"(ник на cs2red: {c_nick})")
                        except Exception as e:
                            log(f"⚠️ on_member_join лог: {e}")
                return

        # Неоднозначный или нулевой результат — пропуск.
        log(f"🐣 on_member_join {member.display_name} ({nick}): "
            f"найдено {len(users)} игроков — пропуск автопривязки")
    except Exception as e:
        # Никогда не роняем обработчик из-за единичной ошибки.
        log(f"⚠️ on_member_join: {type(e).__name__}: {e}")


def admin_only(i):
    return i.user.guild_permissions.administrator


async def _admin_guard(i):
    """Проверка прав администратора. Ответ об отказе идёт через followup,
    поэтому вызывать `_admin_guard` можно только ПОСЛЕ `await i.response.defer()`."""
    if not admin_only(i):
        try:
            await i.followup.send("❌ Нужны права админа.", ephemeral=True)
        except Exception:
            try:
                await i.response.send_message("❌ Нужны права админа.", ephemeral=True)
            except Exception:
                pass
        return False
    return True


# ============================================================
# КОМАНДЫ ПРИВЯЗКИ
# ============================================================
@bot.tree.command(name="link", description="Привязать SteamID")
@app_commands.describe(steam_id="SteamID64 (17 цифр)")
@app_commands.checks.cooldown(1, 30)
async def link(i, steam_id: str):
    if not (steam_id.isdigit() and len(steam_id) == 17 and steam_id.startswith("7656119")):
        await i.response.send_message("❌ Неверный SteamID.", ephemeral=True)
        return
    await i.response.defer(ephemeral=False)
    try:
        owner = await links_repo.get_owner_of(steam_id)
        if owner and int(owner) != i.user.id:
            await safe_send(i.followup, "❌ SteamID занят.", ephemeral=False)
            return

        u = await run_with_timeout(
            api.cs2red_user(steam_id),
            timeout=CMD_API_TIMEOUT,
            fallback_msg="⚠️ cs2red не ответил за 30 сек. Попробуй позже или проверь `/health`.",
            interaction=i,
            context="/link",
        )
        if u is None:
            return
        if u.get("_not_found"):
            await safe_send(i.followup, f"❌ Игрок `{steam_id}` не найден на cs2red.", ephemeral=False)
            return

        await links_repo.add_link(i.user.id, steam_id)
        await safe_send(i.followup,
            f"✅ Привязано: `{steam_id}` (ник **{u.get('nick', '-')}**)",
            ephemeral=False
        )
        try:
            s1 = await sync_role_by_id(i.guild, i.user, steam_id)
            s2 = await sync_clan_role_by_id(i.guild, i.user, steam_id)
            await log_to_channel(i.client, f"🔗 /link - {s1}\n{s2}")
        except Exception as e:
            await log_error(i.client, "/link.sync", e)
    except Exception as e:
        await log_error(i.client, "/link", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="unlink", description="Отвязать SteamID")
async def unlink(i):
    await i.response.defer(ephemeral=False)
    try:
        rid = await links_repo.remove_link(i.user.id)
        if rid is None:
            await safe_send(i.followup, "ℹ️ Не привязан.", ephemeral=False)
            return
        managed_n = {n for n in ROLE_MAP.values() if n}
        managed_i = set(CLAN_ROLE_MAP.values())
        removed = []
        for r in i.user.roles:
            if r.name in managed_n or r.id in managed_i:
                try:
                    await i.user.remove_roles(r)
                    removed.append(r.name)
                except discord.Forbidden:
                    pass
        await safe_send(i.followup,
            f"✅ Отвязано. Снято: {', '.join(removed) if removed else 'нет'}",
            ephemeral=False
        )
    except Exception as e:
        await log_error(i.client, "/unlink", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="admlink", description="Привязать SteamID пользователю (админ)")
@app_commands.describe(user="Участник Discord", steam_id="SteamID64 (17 цифр)")
@app_commands.default_permissions(administrator=True)
async def admlink(i, user: discord.Member, steam_id: str):
    await i.response.defer(ephemeral=False)
    if not await _admin_guard(i):
        return
    steam_id = steam_id.strip()
    if not (steam_id.isdigit() and len(steam_id) == 17 and steam_id.startswith("7656119")):
        await safe_send(i.followup, "❌ Неверный SteamID (ожидается SteamID64 из 17 цифр, "
                                    "начинающийся с 7656119).", ephemeral=False)
        return
    try:
        if user.bot:
            await safe_send(i.followup, "❌ Нельзя привязать бота.", ephemeral=False)
            return
        # Проверяем, есть ли уже привязка (для информативного лога).
        was_linked = await links_repo.get_link(user.id)
        await links_repo.add_link(user.id, steam_id)

        embed = discord.Embed(
            title="✅ Админ-привязка",
            color=0x57F287,
            timestamp=now_utc(),
        )
        embed.add_field(name="Пользователь", value=user.mention, inline=True)
        embed.add_field(name="Steam ID", value=f"`{steam_id}`", inline=True)
        embed.add_field(name="Действие",
                        value="Привязка обновлена" if was_linked else "Привязка создана",
                        inline=False)
        embed.set_footer(text=f"Выполнил: {i.user.display_name}")

        await safe_send(i.followup,
            f"✅ Пользователь {user.mention} успешно привязан к Steam ID: `{steam_id}`.",
            ephemeral=False
        )
        await safe_send(i.followup, embed=embed, ephemeral=False)
        await log_to_channel(i.client,
            f"🔗 /admlink: {user.mention} ({user.id}) "
            f"{'обновлён' if was_linked else 'привязан'} → `{steam_id}` "
            f"от {i.user.mention}")
    except Exception as e:
        await log_error(i.client, "/admlink", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="sync_all", description="Синхронизировать всех")
async def sync_all(i):
    await i.response.defer(ephemeral=False)
    if not await _admin_guard(i):
        return
    await run_sync_all(i)


async def run_sync_all(i, ephemeral: bool = False):
    """Полная логика синхронизации ролей (используется командой /sync_all
    и кнопкой «Синхронизация» в панели). Ожидает, что defer уже вызван."""
    try:
        links = await links_repo.get_all_links()
        if not links:
            await safe_send(i.followup, "ℹ️ Пусто.", ephemeral=ephemeral)
            return

        total = len(links)
        processed = 0
        res = []
        stopped = False
        api.blocked_reason = ""

        def _progress(processed_: int, total_: int) -> discord.Embed:
            pct = int((processed_ / total_) * 100) if total_ else 0
            e = discord.Embed(
                title="🔗 Синхронизация ролей",
                color=0x000000,
                timestamp=now_utc(),
            )
            e.add_field(name="Обработано", value=f"{processed_} из {total_}", inline=True)
            e.add_field(name="Прогресс", value=f"{pct}%", inline=True)
            return e

        # Промежуточное сообщение — пользователь видит, что процесс идёт.
        msg = await i.followup.send(embed=_progress(0, total), ephemeral=ephemeral)
        last_update = 0.0

        for did, sid in links.items():
            if stopped:
                break
            processed += 1
            m = i.guild.get_member(int(did)) if str(did).isdigit() else None
            if m is None:
                res.append(f"⚠️ {did}: нет")
            else:
                r1 = await sync_role_by_id(i.guild, m, sid)
                if api.blocked_reason in ("403", "429"):
                    res.append(f"⛔ остановлено: {api.blocked_reason} ({r1})")
                    stopped = True
                else:
                    res.append(r1)
                    r2 = await sync_clan_role_by_id(i.guild, m, sid)
                    if api.blocked_reason in ("403", "429"):
                        res.append(f"⛔ остановлено: {api.blocked_reason} ({r2})")
                        stopped = True
                    else:
                        res.append(r2)

            await asyncio.sleep(REQUEST_DELAY)

            # Прогресс не чаще раза в 10 секунд.
            now = time.time()
            if now - last_update >= 10.0:
                last_update = now
                try:
                    await msg.edit(embed=_progress(processed, total))
                except Exception:
                    pass

        # Если остановились из-за 403/429 — выводим понятное сообщение.
        if api.blocked_reason in ("403", "429"):
            reason = api.blocked_reason
            await safe_send(
                i.followup,
                ("❌ Ошибка авторизации (403). Проверьте куки на cs2red."
                 if reason == "403"
                 else "❌ Сайт cs2red временно заблокировал запросы (429). "
                      "Подождите 5 минут."),
                ephemeral=ephemeral,
            )

        text = f"📊 Синхронизация ({len(res)}):\n" + "\n".join(res)
        # Обновляем итоговый Embed в том же сообщении.
        final = discord.Embed(
            title="🔗 Синхронизация ролей",
            description=text[:4000] or "—",
            color=0x57F287 if not stopped else 0xFEE75C,
            timestamp=now_utc(),
        )
        try:
            await msg.edit(embed=final)
        except Exception:
            await safe_send(i.followup, text[:1900], title="🔗 Синхронизация",
                            ephemeral=ephemeral)
        await log_to_channel(i.client, text)
    except Exception as e:
        await log_error(i.client, "/sync_all", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


async def linked_member_autocomplete(i, current):
    links = await links_repo.get_all_links()
    if not links:
        return []
    ch = []
    for did, sid in links.items():
        m = i.guild.get_member(int(did))
        if m is None:
            continue
        s = current.lower()
        if s and s not in m.display_name.lower() and s not in sid:
            continue
        ch.append(app_commands.Choice(name=f"{m.display_name} | {sid}", value=str(m.id)))
        if len(ch) >= 25:
            break
    return ch


# ============================================================
# /link_all
# ============================================================
@bot.tree.command(name="link_all", description="Массовая привязка/синхронизация из базы SQLite")
async def link_all(i):
    await i.response.defer(ephemeral=False)
    if not await _admin_guard(i):
        return

    try:
        # Данные о привязках берем из БД (SQLite), а не из mass_links.json.
        data = await links_repo.get_all_links()
        if not data:
            await safe_send(i.followup,
                            "ℹ️ В базе нет привязок. Добавьте их через /link или /admlink.",
                            ephemeral=False)
            return

        total = len(data)
        log(f"📥 /link_all: {len(data)} записей из БД")

        added = []
        skip_not_sid = []
        no_member = []
        not_on_site = []
        already = []
        other = []

        processed = 0
        last_progress = 0

        for did, sid in data.items():
            sid = str(sid).strip()

            if not (did.isdigit() and 17 <= len(did) <= 20):
                other.append(f"`{did}`: не Discord ID")
                processed += 1
                continue

            if not (sid.isdigit() and len(sid) == 17 and sid.startswith("7656119")):
                skip_not_sid.append(f"`{did}`: не SteamID (`{sid}`)")
                processed += 1
                continue

            current_link = await links_repo.get_link(did)
            if current_link == sid:
                already.append(f"`{did}` → `{sid}`")
                processed += 1
                continue

            m = i.guild.get_member(int(did))
            if m is None:
                no_member.append(f"`{did}` ({sid})")
                processed += 1
                continue

            owner = await links_repo.get_owner_of(sid)
            if owner and owner != str(did):
                other.append(f"{m.display_name}: SteamID занят <@{owner}>")
                processed += 1
                continue

            user = await api.cs2red_user(sid)
            if user is None or user.get("_not_found"):
                not_on_site.append(f"{m.display_name}: `{sid}`")
                processed += 1
                continue

            await links_repo.add_link(did, sid)
            added.append(f"✅ {m.display_name} → `{sid}` (cs2red: {user.get('nick', '-')})")
            processed += 1

            if processed - last_progress >= 20:
                last_progress = processed
                try:
                    await safe_send(i.followup,
                        f"⏳ Прогресс: {processed}/{total}…",
                        ephemeral=False
                    )
                except Exception:
                    pass

            await asyncio.sleep(REQUEST_DELAY)

        text = (
            f"📊 **Массовая привязка**\n"
            f"✅ Добавлено: **{len(added)}**\n"
            f"ℹ️ Уже привязано: **{len(already)}**\n"
            f"⏭ Не SteamID (ники): **{len(skip_not_sid)}**\n"
            f"⚠️ Нет на сервере: **{len(no_member)}**\n"
            f"⚠️ Нет на cs2red: **{len(not_on_site)}**\n"
            f"⚠️ Прочее: **{len(other)}**\n\n"
        )
        if added:
            text += "**Последние добавленные:**\n" + "\n".join(added[-10:]) + "\n\n"
        if not_on_site:
            text += "**Нет на cs2red:**\n" + "\n".join(not_on_site[:10]) + "\n\n"
        if no_member:
            text += "**Нет на сервере:**\n" + "\n".join(no_member[:10]) + "\n\n"
        if other:
            text += "**Прочее:**\n" + "\n".join(other[:10])
        await safe_send(i.followup, text[:1900], title="🔗 Массовая привязка",
                        ephemeral=False)
        await log_to_channel(i.client,
            f"📊 /link_all: +{len(added)} / уже {len(already)} / "
            f"не SID {len(skip_not_sid)} / нет на сервере {len(no_member)} / "
            f"нет на cs2red {len(not_on_site)} / прочее {len(other)}")
    except Exception as e:
        await log_error(i.client, "/link_all", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="auto_link", description="Авто-привязка по никам через админ-панель cs2red (админ)")
@app_commands.default_permissions(administrator=True)
async def auto_link(i):
    await i.response.defer(ephemeral=False)
    if not await _admin_guard(i):
        return
    await run_auto_link(i)


async def run_auto_link(i, ephemeral: bool = False):
    """Полная логика авто-привязки по никам (используется командой /auto_link
    и кнопкой «Привязать всех» в панели). Ожидает, что defer уже вызван."""
    # Проверяем, что куки вообще заданы.
    if not CS2RED_COOKIES:
        await safe_send(i.followup,
                        "❌ Ошибка авторизации на cs2red. Проверьте куки "
                        "(CS2RED_COOKIE_STRING или CS2RED_*).", ephemeral=ephemeral)
        return

    # Проверочный запрос: убеждаемся, что авторизация работает.
    api.blocked_reason = ""
    probe = await api.cs2red_search_by_nick("__probe__", limit=1)
    if probe is None:
        if api.blocked_reason == "403" or getattr(api, "last_status", None) == 403:
            await safe_send(i.followup,
                            "❌ Ошибка авторизации (403). Проверьте куки на cs2red.",
                            ephemeral=ephemeral)
            return
        if api.blocked_reason == "429":
            await safe_send(i.followup,
                            "❌ Сайт cs2red временно заблокировал запросы (429). "
                            "Подождите 5 минут.",
                            ephemeral=ephemeral)
            return
        await safe_send(i.followup,
                        "❌ cs2red не ответил на проверочный запрос. Попробуйте позже.",
                        ephemeral=ephemeral)
        return

    try:
        # Все участники сервера (без ботов).
        members = [m for m in i.guild.members if not m.bot]
        # Уже привязанные ID из БД.
        linked = await links_repo.get_all_links()

        linked_ids = set()
        for did, _sid in linked.items():
            if str(did).isdigit():
                linked_ids.add(str(did))

        unlinked = [m for m in members if str(m.id) not in linked_ids]

        total = len(unlinked)
        processed = 0
        added = []
        skipped_ambigu = []
        skipped_none = []
        errors = []

        # --- Стартовый Embed с прогрессом ---
        def _progress_embed(processed_: int, total_: int, added_: int) -> discord.Embed:
            pct = int((processed_ / total_) * 100) if total_ else 0
            e = discord.Embed(
                title="🔗 Авто-привязка по никам",
                color=0x000000,
                timestamp=now_utc(),
            )
            e.add_field(name="Обработано", value=f"{processed_} из {total_}", inline=True)
            e.add_field(name="Прогресс", value=f"{pct}%", inline=True)
            e.add_field(name="✅ Привязано", value=str(added_), inline=True)
            return e

        msg = await i.followup.send(
            embed=_progress_embed(0, total, 0), ephemeral=ephemeral)

        # Обновляем прогресс не чаще, чем раз в 10 секунд.
        last_update = 0.0

        for m in unlinked:
            processed += 1
            nick = (m.display_name or "").strip()
            if not nick:
                skipped_none.append(f"{m.display_name}: пустой ник")
                continue

            try:
                users = await api.cs2red_search_by_nick(nick)
                if users is None:
                    reason = api.blocked_reason
                    if reason == "403" or getattr(api, "last_status", None) == 403:
                        errors.append("403: авторизация на cs2red (куки) — команда прервана")
                        await msg.edit(embed=_progress_embed(processed, total, len(added)))
                        await safe_send(i.followup,
                                        "❌ Ошибка авторизации (403). Проверьте куки.",
                                        ephemeral=ephemeral)
                        break
                    if reason == "429":
                        errors.append("429: сайт cs2red временно заблокировал запросы")
                        await msg.edit(embed=_progress_embed(processed, total, len(added)))
                        await safe_send(
                            i.followup,
                            "❌ Сайт cs2red временно заблокировал запросы (429). "
                            "Подождите 5 минут.",
                            ephemeral=ephemeral)
                        break
                    if reason:
                        errors.append(f"{reason}: ошибка сервера cs2red — команда прервана")
                        await msg.edit(embed=_progress_embed(processed, total, len(added)))
                        await safe_send(i.followup,
                                        f"❌ Ошибка сервера cs2red ({reason}). "
                                        "Повторите позже.",
                                        ephemeral=ephemeral)
                        break
                    errors.append(f"{m.display_name}: cs2red не ответил")
                    continue

                steam_id = None
                if len(users) == 1:
                    steam_id = api.extract_steam64(users[0])

                if steam_id is None:
                    skipped_ambigu.append(f"{m.display_name} ({nick})")
                else:
                    await links_repo.add_link(m.id, steam_id)
                    added.append(f"{m.display_name} → `{steam_id}`")
            except Exception as e:
                errors.append(f"{m.display_name}: {e}")

            # задержка, чтобы не забанили за флуд запросами
            await asyncio.sleep(api.RATE_LIMIT_SECONDS)

            # Обновляем Embed раз в 10 секунд (или каждые ~5 человек — при
            # задержке 2.5с это совпадает по времени). Не обновляем на каждом шаге.
            now = time.time()
            if now - last_update >= 10.0:
                last_update = now
                try:
                    await msg.edit(embed=_progress_embed(processed, total, len(added)))
                except Exception:
                    pass

        # --- Финальный отчёт ---
        embed = discord.Embed(
            title="🔗 Авто-привязка по никам",
            color=0x57F287 if not errors else 0xFEE75C,
            timestamp=now_utc(),
        )
        embed.add_field(name="Всего участников", value=str(len(members)), inline=True)
        embed.add_field(name="Уже привязано", value=str(len(linked)), inline=True)
        embed.add_field(name="Проверено", value=str(processed), inline=True)
        embed.add_field(name="✅ Привязано", value=str(len(added)), inline=True)
        embed.add_field(name="⚠️ Неоднозначно", value=str(len(skipped_ambigu)), inline=True)
        embed.add_field(name="⏭ Не найдено", value=str(len(skipped_none)), inline=True)
        embed.add_field(name="❌ Ошибок", value=str(len(errors)), inline=True)
        if added:
            embed.add_field(name="Последние привязки",
                            value="\n".join(added[-10:]), inline=False)
        if skipped_ambigu:
            embed.add_field(
                name="Неоднозначные (пропущены)",
                value="\n".join(skipped_ambigu[:10]), inline=False)
        if errors:
            embed.add_field(name="Ошибки", value="\n".join(errors[:10]), inline=False)
        embed.set_footer(text=f"Выполнил: {i.user.display_name}")

        # Обновим итоговый Embed в том же сообщении.
        try:
            await msg.edit(embed=embed)
        except Exception:
            await safe_send(i.followup, embed=embed, ephemeral=False)
        await log_to_channel(i.client,
            f"🔗 /auto_link: привязано {len(added)}, неоднозначно "
            f"{len(skipped_ambigu)}, не найдено {len(skipped_none)}, ошибок {len(errors)}")
    except Exception as e:
        await log_error(i.client, "/auto_link", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="export_members", description="Выгрузить привязки в mass_links.json")
async def export_members(i):
    await i.response.defer(ephemeral=False)
    if not await _admin_guard(i):
        return
    try:
        links = await links_repo.get_all_links()
        if not links:
            await safe_send(i.followup, "ℹ️ Нет привязанных.", ephemeral=False)
            return
        valid = {}
        for did, sid in links.items():
            if isinstance(sid, str) and sid.isdigit() and len(sid) == 17 and sid.startswith("7656119"):
                valid[did] = sid
        try:
            with open(data_path("mass_links.json"), "w", encoding="utf-8") as f:
                json.dump(valid, f, ensure_ascii=False, indent=2)
        except Exception as e:
            await log_error(i.client, "/export_members.write", e)
            await safe_send(i.followup, f"❌ {e}", ephemeral=False)
            return
        await safe_send(i.followup,
            f"✅ Выгружено привязок: **{len(valid)}**",
            ephemeral=False
        )
        await log_to_channel(i.client, f"📤 /export_members — {len(valid)}")
    except Exception as e:
        await log_error(i.client, "/export_members", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


# ============================================================
# БАЗА ИГРОКОВ
# ============================================================
@bot.tree.command(name="player_info", description="База игрока")
@app_commands.describe(member="Участник Discord")
async def player_info(i, member: discord.Member):
    await i.response.defer(ephemeral=False)

    try:
        profile = await run_with_timeout(
            build_player_profile(i.guild, member.id),
            timeout=CMD_API_TIMEOUT,
            fallback_msg="⏱ Сбор профиля занял больше 30 сек. "
                         "Скорее всего cs2red или Steam не отвечают. "
                         "Попробуй `/health` — проверить состояние API.",
            interaction=i,
            context="/player_info",
        )
        if profile is None:
            return

        if not profile.get("steamid64"):
            await safe_send(i.followup, "❌ Нет SteamID", ephemeral=False)
            return

        old = await players_repo.get_player(profile["steamid64"])
        for k in ("activity", "notes"):
            if k in old:
                profile[k] = old[k]
        profile["activity_hours"] = await get_player_activity(profile["steamid64"])
        await players_repo.save_player(profile["steamid64"], profile)

        await send_player_report(i.client, profile)

        await safe_send(i.followup,
            f"✅ Профиль **{member.display_name}** отправлен в <#{DB_CHANNEL_ID}>.",
            ephemeral=False
        )
    except Exception as e:
        await log_error(i.client, "/player_info", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="player_note", description="Заметка к профилю")
@app_commands.describe(member="Участник Discord", note="Заметка")
async def player_note(i, member: discord.Member, note: str):
    await i.response.defer(ephemeral=False)
    if not await _admin_guard(i):
        return
    try:
        sid = await links_repo.get_link(member.id)
        if not sid:
            await safe_send(i.followup, "❌ Нет SteamID", ephemeral=False)
            return

        async def _make():
            return await build_player_profile(i.guild, member.id, sid)
        profile = await players_repo.get_player(sid) or await _make()
        profile["notes"] = note
        profile["updated_at"] = now_utc().isoformat()
        await players_repo.save_player(sid, profile)
        await safe_send(i.followup, f"✅ Заметка для {member.mention}", ephemeral=False)
        await send_player_report(i.client, profile)
    except Exception as e:
        await log_error(i.client, "/player_note", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="player_list", description="Вся база")
async def player_list(i):
    await i.response.defer(ephemeral=False)
    if not await _admin_guard(i):
        return
    try:
        db = await players_repo.get_all_players()
        if not db:
            await safe_send(i.followup, "ℹ️ Пусто", ephemeral=False)
            return
        links = await links_repo.get_all_links()
        rev = {s: d for d, s in links.items()}
        lines = []
        for sid, p in db.items():
            nick = p.get("cs2red_nick") or p.get("discord_name") or sid
            fl = "❌VAC " if p.get("steam_bans", {}).get("vac_banned") else ""
            did = p.get("discord_id") or rev.get(sid)
            m = i.guild.get_member(int(did)) if did else None
            part = f" - {m.mention}" if m else (f" - `{did}`" if did else "")
            lines.append(f"{fl}`{sid}` - {nick}{part}")
        text = "📊 **База:**\n" + "\n".join(lines[:50])
        if len(lines) > 50:
            text += f"\n...и ещё {len(lines) - 50}"
        await safe_send(i.followup, text[:1900], ephemeral=False)
    except Exception as e:
        await log_error(i.client, "/player_list", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="lookup", description="Инфо об игроке")
async def lookup(i, member: discord.Member):
    await i.response.defer(ephemeral=False)
    try:
        sid = await links_repo.get_link(member.id)
        if not sid:
            await safe_send(i.followup, "❌ Не привязан", ephemeral=False)
            return
        u = await run_with_timeout(
            api.cs2red_user(sid),
            timeout=CMD_API_TIMEOUT,
            fallback_msg="⚠️ cs2red не ответил за 30 сек.",
            interaction=i,
            context="/lookup",
        )
        if u is None:
            return
        nick = u.get("nick", "-") if u and not u.get("_not_found") else "-"
        rid = u.get("roleId") if u and not u.get("_not_found") else None
        clan = await api.player_clan(sid)
        ct = f" (клан: {clan['clan_name']})" if clan else " (без клана)"
        await safe_send(i.followup,
            f"🎮 **{nick}**{ct}\n👤 {member.mention}\n`{sid}`\n"
            f"Роль: **{role_display_name(rid) if rid else '-'}**",
            ephemeral=False
        )
    except Exception as e:
        await log_error(i.client, "/lookup", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


# ============================================================
# /ping
# ============================================================
@bot.tree.command(name="ping", description="Проверить, жив ли бот")
async def ping(i):
    await i.response.defer(ephemeral=False)
    ws = bot.latency * 1000
    up = int(time.time() - BOT_START_TIME)
    d, r = divmod(up, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    us = f"{up} сек." if up < 60 else (f"{m} мин. {s} сек." if up < 3600
                                        else (f"{h} ч. {m} мин." if up < 86400
                                              else f"{d} д. {h} ч."))
    status = "🟢 Отлично" if ws < 150 else ("🟡 Норма" if ws < 400 else "🟠 Высокая")
    e = discord.Embed(title="🏓 Pong!", color=0x2B2D31, timestamp=now_utc())
    e.add_field(name="Статус", value="✅ Бот жив", inline=False)
    e.add_field(name="⏱ Аптайм", value=f"`{us}`", inline=True)
    e.add_field(name="📡 WS", value=f"`{ws:.0f} мс`", inline=True)
    e.add_field(name="Связь", value=status, inline=False)
    e.add_field(name="🍪 Cookies", value=f"`{len(CS2RED_COOKIES)}`", inline=True)
    e.add_field(name="🆔 Guild", value=f"`{GUILD_ID}`", inline=True)
    e.add_field(name="Python",
                value=f"{platform.python_version()} • discord.py {discord.__version__}",
                inline=False)
    e.set_footer(text=f"Quantum • {utc_stamp()}")
    if bot.user:
        e.set_thumbnail(url=bot.user.display_avatar.url)
    await safe_send(i.followup, embed=e, ephemeral=False)


# ============================================================
# /health
# ============================================================
@bot.tree.command(name="health", description="Проверить работу всех систем бота")
async def health(i: discord.Interaction):
    await i.response.defer(ephemeral=False)

    results = []
    ok_count = 0
    fail_count = 0

    def add(emoji, name, status, ok):
        nonlocal ok_count, fail_count
        results.append((emoji, name, status, ok))
        if ok:
            ok_count += 1
        else:
            fail_count += 1

    ws_ms = bot.latency * 1000
    add("🌐", "Discord WebSocket", f"`{ws_ms:.0f} мс`", ws_ms < 500)

    guild = i.guild
    if guild:
        add("🏠", "Guild",
            f"`{guild.name}` (`{guild.id}`), участников: `{guild.member_count}`", True)
    else:
        add("🏠", "Guild", "не найден", False)

    me = guild.me if guild else None
    if me and guild:
        needed = {
            "Manage Roles": me.guild_permissions.manage_roles,
            "Send Messages": me.guild_permissions.send_messages,
            "Embed Links": me.guild_permissions.embed_links,
            "Manage Threads": me.guild_permissions.manage_threads,
            "Read Message History": me.guild_permissions.read_message_history,
        }
        missing = [n for n, ok in needed.items() if not ok]
        if not missing:
            add("🔐", "Права бота", "все необходимые выданы ✅", True)
        else:
            add("🔐", "Права бота", f"❌ нет: {', '.join(missing)}", False)
    else:
        add("🔐", "Права бота", "не удалось получить", False)

    try:
        t0 = time.perf_counter()
        test = await api._get_json("https://cs2red.ru/api/admin/users",
                                   params={"begin": 0, "count": 1})
        dt = (time.perf_counter() - t0) * 1000
        if test is not None and test.get("success") is not None:
            add("🍪", "cs2red /admin/users",
                f"`{dt:.0f} мс`, cookies `{len(CS2RED_COOKIES)}` шт.", True)
        else:
            status = getattr(api, "last_status", "?")
            add("🍪", "cs2red /admin/users",
                f"❌ ответ `{status}`, cookies `{len(CS2RED_COOKIES)}` шт.", False)
    except Exception as e:
        add("🍪", "cs2red /admin/users", f"❌ {e}", False)

    try:
        t0 = time.perf_counter()
        clans_raw = await api._get_json("https://cs2red.ru/api/clans")
        dt = (time.perf_counter() - t0) * 1000
        if clans_raw and clans_raw.get("success"):
            clans = clans_raw.get("clans") or []
            total_members = sum(len(c.get("members") or []) for c in clans)
            if clans and total_members > 0:
                add("🏰", "cs2red /api/clans",
                    f"`{dt:.0f} мс`, кланов `{len(clans)}`, участников `{total_members}`",
                    True)
            else:
                add("🏰", "cs2red /api/clans",
                    f"⚠️ пусто: кланов `{len(clans)}`, members `{total_members}`", False)
        else:
            status = getattr(api, "last_status", "?")
            add("🏰", "cs2red /api/clans", f"❌ ответ `{status}`", False)
    except Exception as e:
        add("🏰", "cs2red /api/clans", f"❌ {e}", False)

    steam_key = os.getenv("STEAM_API_KEY")
    if not steam_key:
        add("🎮", "Steam API", "❌ ключ не задан", False)
    else:
        try:
            t0 = time.perf_counter()
            test = await api._steam_get("ISteamUser/GetPlayerBans/v1/",
                                        {"steam_id": "76561198000000000"})
            dt = (time.perf_counter() - t0) * 1000
            if test is not None:
                add("🎮", "Steam API", f"`{dt:.0f} мс`, ключ работает", True)
            else:
                add("🎮", "Steam API", "❌ не отвечает", False)
        except Exception as e:
            add("🎮", "Steam API", f"❌ {e}", False)

    for name, data in [
        ("links.json", await links_repo.count_all()),
        ("players_db.json", await players_repo.count_all()),
        ("users_db.json", await users_repo.count_all()),
        ("moderators.json", await moderation_repo.count_all()),
        ("votes.json", await votes_repo.count_all()),
        ("seen.json", await seen_repo.count_all()),
    ]:
        try:
            size = len(data) if hasattr(data, "__len__") else "?"
            add("📁", f"Файл `{name}`", f"записей: `{size}`", True)
        except Exception as e:
            add("📁", f"Файл `{name}`", f"❌ {e}", False)

    mass_file = data_path("mass_links.json")
    if mass_file.exists():
        try:
            with open(mass_file, "r", encoding="utf-8") as f:
                mass = json.load(f)
            total = len(mass)
            valid = sum(1 for k, v in mass.items()
                        if isinstance(v, str) and v.isdigit()
                        and len(v) == 17 and v.startswith("7656119"))
            add("📁", "Файл `mass_links.json`",
                f"всего `{total}`, валидных SteamID `{valid}`", True)
        except json.JSONDecodeError:
            add("📁", "Файл `mass_links.json`",
                "❌ JSON повреждён (проверь на jsonlint.com)", False)
        except Exception as e:
            add("📁", "Файл `mass_links.json`", f"❌ {e}", False)
    else:
        add("📁", "Файл `mass_links.json`", "⚠️ не найден", False)

    for name, task_obj, interval in [
        ("auto_sync", auto_sync, f"{SYNC_INTERVAL_MINUTES} мин"),
        ("activity_tracker", activity_tracker, f"{ACTIVITY_TRACK_SECONDS // 60} мин"),
    ]:
        if task_obj.is_running():
            nxt = task_obj.next_iteration
            if nxt:
                sec = max(0, int((nxt - now_utc()).total_seconds()))
                m_, s_ = divmod(sec, 60)
                next_str = f"{m_} мин. {s_} сек." if m_ else f"{s_} сек."
                add("⚙️", f"Задача `{name}`",
                    f"✅ работает, интервал {interval}, след. через `{next_str}`", True)
            else:
                add("⚙️", f"Задача `{name}`", f"✅ работает, интервал {interval}", True)
        else:
            add("⚙️", f"Задача `{name}`", "❌ не запущена", False)

    for name, cid in [
        ("LOG_CHANNEL_ID", LOG_CHANNEL_ID),
        ("DB_CHANNEL_ID", DB_CHANNEL_ID),
        ("APPLICATIONS_CHANNEL_ID", APPLICATIONS_CHANNEL_ID),
        ("THREADS_CHANNEL_ID", THREADS_CHANNEL_ID),
    ]:
        if not cid:
            add("📢", f"Канал `{name}`", "⚠️ не задан", False)
            continue
        ch = bot.get_channel(cid)
        if ch is None:
            add("📢", f"Канал `{name}`", f"❌ не найден (`{cid}`)", False)
        else:
            perm_ok = True
            if me:
                perm_ok = ch.permissions_for(me).send_messages
            add("📢", f"Канал `{name}`",
                f"`#{ch.name}` ({cid}), send: {'✅' if perm_ok else '❌'}", perm_ok)

    # ---------- ИИ-каскад ----------
    if ai_service is None:
        add("🤖", "AI-сервис", "не инициализирован", False)
    else:
        for prov in ai_service.providers:
            ok = prov.last_error == ""
            add("🤖", f"AI ({prov.name.title()})", prov.status_line(), ok)
        # Не настроенные провайдеры (без ключа) не попадают в список,
        # поэтому явно покажем, что их нет.
        configured = {p.name for p in ai_service.providers}
        for name in ("gemini", "openrouter", "llm7"):
            if name not in configured:
                add("🤖", f"AI ({name.title()})", "Не настроен (нет ключа)", False)
        try:
            cache_count = await count_cache()
            add("🗃️", "AI Cache", f"записей в БД — {cache_count}", True)
        except Exception as e:
            add("🗃️", "AI Cache", f"❌ {e}", False)

    last_status = getattr(api, "last_status", None)
    add("📡", "Последний HTTP-статус cs2red",
        f"`{last_status}`" if last_status else "нет данных", last_status == 200)

    total_checks = ok_count + fail_count
    if fail_count == 0:
        color = 0x57F287
        title = "✅ Все системы работают"
    elif ok_count >= fail_count:
        color = 0xFEE75C
        title = f"⚠️ Работает: {ok_count}/{total_checks}, проблем: {fail_count}"
    else:
        color = 0xED4245
        title = f"❌ Проблем много: {fail_count} из {total_checks}"

    embed = discord.Embed(
        title=f"🩺 Проверка систем бота — {title}",
        description=(f"**Итог:** ✅ `{ok_count}` работает / ❌ `{fail_count}` проблем\n"
                     f"**Всего проверок:** `{total_checks}`"),
        color=color, timestamp=now_utc(),
    )
    for emoji, name, status, ok in results:
        arrow = "✅" if ok else "❌"
        embed.add_field(name=f"{emoji} {arrow} {name}", value=status[:1024], inline=False)

    if bot.user:
        embed.set_footer(text=f"Quantum • {utc_stamp()} (UTC)",
                         icon_url=bot.user.display_avatar.url)

    await safe_send(i.followup, embed=embed, ephemeral=False)
    await log_to_channel(i.client, f"🩺 /health: ✅ {ok_count} / ❌ {fail_count}")


# ============================================================
# МОДЕРАТОРЫ
# ============================================================
@bot.tree.command(name="moders", description="Список модераторов бота")
async def moders(i):
    await i.response.defer(ephemeral=False)
    try:
        mods = await moderation_repo.get_all_moderators()
        if not mods:
            await safe_send(i.followup, "ℹ️ Пусто", ephemeral=False)
            return
        lines = []
        for u in mods:
            member = i.guild.get_member(int(u)) if str(u).isdigit() else None
            target = member or bot.get_user(int(u)) if str(u).isdigit() else None
            if member:
                lines.append(f"• {member.mention}")
            elif target:
                lines.append(f"• @{target.display_name} (ник хоста: {target.display_name})")
            else:
                lines.append(f"• `{u}`")
        await safe_send(i.followup, f"🛠 ({len(mods)}):\n" + "\n".join(lines),
                        title="🛠 Список модераторов", ephemeral=False)
    except Exception as e:
        await log_error(i.client, "/moders", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="give", description="Выдать доступ")
async def give(i, user: discord.Member):
    if not admin_only(i):
        await i.response.send_message("❌", ephemeral=False)
        return
    await i.response.defer(ephemeral=False)
    try:
        added = await moderation_repo.add_moderator(user.id)
        await safe_send(i.followup,
            f"✅ {user.mention} получил доступ." if added else "ℹ️ Уже есть.",
            ephemeral=False
        )
    except Exception as e:
        await log_error(i.client, "/give", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="take", description="Забрать доступ")
async def take(i, user: discord.Member):
    if not admin_only(i):
        await i.response.send_message("❌", ephemeral=False)
        return
    await i.response.defer(ephemeral=False)
    try:
        removed = await moderation_repo.remove_moderator(user.id)
        await safe_send(i.followup,
            f"❌ {user.mention} лишён." if removed else "ℹ️ Уже без.",
            ephemeral=False
        )
    except Exception as e:
        await log_error(i.client, "/take", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


# ============================================================
# СТАТИСТИКА МОДЕРАТОРА (/status)
# ============================================================
def _build_status_embed(target, stats: dict) -> discord.Embed:
    """Чёрный Embed профиля модератора.

    Поля: эффективность (личные цифры), разбивка по кланам, статус
    заявок и список заявок в работе.
    """
    accepted = int(stats.get("accepted", 0) or 0)
    rejected = int(stats.get("rejected", 0) or 0)
    total = int(stats.get("total", 0) or 0) or (accepted + rejected)
    clan_main = int(stats.get("clan_main", 0) or 0)
    clan_other = int(stats.get("clan_other", 0) or 0)
    pending = int(stats.get("pending", 0) or 0)
    pending_total = int(stats.get("pending_total", 0) or 0)
    in_work = stats.get("in_work") or []

    embed = discord.Embed(
        title=f"👤 Профиль: {target.display_name}",
        color=0x000000,
    )
    try:
        embed.set_thumbnail(url=target.display_avatar.url)
    except Exception:
        pass

    # Поле 1: эффективность.
    embed.add_field(
        name="📊 Эффективность",
        value=(
            f"**Принято заявок:** {accepted}\n"
            f"**Отклонено заявок:** {rejected}\n"
            f"**Всего обработано:** {total}"
        ),
        inline=True,
    )

    # Поле 2: по категориям (кланы).
    embed.add_field(
        name="📂 По категориям",
        value=(
            f"**Заявок в клан {CLAN_NAME}:** {clan_main}\n"
            f"**Заявок в другие кланы:** {clan_other}"
        ),
        inline=True,
    )

    # Поле 3: статус заявок.
    embed.add_field(
        name="📊 Статус заявок",
        value=(
            f"**В работе:** {pending}\n"
            f"**В работе в клане:** {pending_total}"
        ),
        inline=True,
    )

    # Поле 4: список заявок в работе.
    tickets = []
    for item in in_work[:20]:
        ident = (item.get("message_id") or item.get("user_id")
                 or item.get("app_id") or "")
        if ident:
            tickets.append(f"`ticket-{ident}`")
    work_text = ", ".join(tickets) if tickets else "Нет активных заявок"
    embed.add_field(name="🛠 Заявки в работе", value=work_text[:1024], inline=False)

    embed.set_footer(text=f"Quantum • {now_utc().strftime('%d.%m.%Y %H:%M')}")
    return embed


@bot.tree.command(name="status", description="Профиль модератора: статистика по заявкам")
@app_commands.describe(user="Чей профиль посмотреть (по умолчанию — ваш)")
async def status(i, user: discord.Member = None):
    """Личная статистика модератора по таблице applications.

    Ответ всегда ephemeral: свои цифры видит только вызвавший команду.
    Чужую статистику может запросить только модератор или админ.
    """
    await i.response.defer(ephemeral=True)
    try:
        member = i.user if isinstance(i.user, discord.Member) else None
        is_reviewer = bool(member and user_can_review(member))
        if not is_reviewer and i.guild is not None:
            is_reviewer = await is_moderator(i.user.id, i.guild)

        target = user or i.user
        if target.id != i.user.id and not is_reviewer:
            await safe_send(i.followup, "❌ У вас нет прав.", ephemeral=True)
            return

        stats = await applications_repo.get_user_stats(target.id)
        embed = _build_status_embed(target, stats)
        await safe_send(i.followup, embed=embed, ephemeral=True)
    except Exception as e:
        await log_error(i.client, "/status", e)
        try:
            await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)
        except Exception:
            pass


# ============================================================
# /admins — кто администрирует клан
# ============================================================
_admin_cache: dict[int, tuple[float, list]] = {}
_ADMIN_CACHE_TTL = 60.0


async def _collect_admins(guild) -> list:
    """Администраторы сервера по правам. Результат кэшируется на минуту.

    Список участников discord.py кэширует сам, но на больших серверах он
    может быть неполным — тогда один раз догружаем его через
    chunk_members (Discord жёстко лимитирует такие запросы).
    """
    now = time.time()
    cached = _admin_cache.get(guild.id)
    if cached and now - cached[0] < _ADMIN_CACHE_TTL:
        return cached[1]

    members = list(guild.members)
    if not members:
        try:
            await guild.chunk_members()
            members = list(guild.members)
        except Exception as e:
            log(f"⚠️ chunk_members: {e}")
    admins = [m for m in members if m.guild_permissions.administrator]
    # Люди — первыми, боты — в конце.
    admins.sort(key=lambda m: (m.bot, (m.display_name or "").lower()))
    _admin_cache[guild.id] = (time.time(), admins)
    return admins


async def _collect_bot_mods(guild) -> list:
    """Модераторы бота (таблица moderators) в виде строк для Embed."""
    rows = []
    moderators = await moderation_repo.get_all_moderators()
    for uid in moderators or []:
        member = guild.get_member(int(uid)) if str(uid).isdigit() else None
        if member:
            rows.append(f"**{member.display_name}** — {member.mention}")
        else:
            rows.append(f"• `{uid}` — не в сервере")
    return rows


async def _collect_reviewer_roles(guild) -> list:
    """Ревьюерские роли (из переменных окружения) в виде строк."""
    rows = []
    for rid in get_all_reviewer_role_ids():
        if not rid:
            continue
        role = guild.get_role(rid)
        if role is None:
            continue
        rows.append(f"{role.mention} — **{role.name}**")
    return rows


def _build_admins_embed(guild, admin_members: list, mod_lines: list,
                        role_lines: list) -> discord.Embed:
    """Embed со списком администраторов, модераторов и ревьюеров."""
    if admin_members:
        admin_rows = []
        for m in admin_members:
            icon = "🤖 " if m.bot else ""
            admin_rows.append(f"{icon}**{m.display_name}** — {m.mention}")
    else:
        admin_rows = ["—"]

    embed = discord.Embed(
        title="🛡 Администрация клана",
        color=0x000000,
        timestamp=now_utc(),
    )
    embed.description = (
        f"Клан **{CLAN_NAME}** · сервер **{guild.name}**\n"
        f"👥 Участников: **{guild.member_count}**")
    embed.add_field(name="👑 Администраторы Discord",
                    value="\n".join(admin_rows)[:1024] or "—", inline=False)
    embed.add_field(name="⚙️ Модераторы бота",
                    value="\n".join(mod_lines)[:1024] or "—", inline=False)
    embed.add_field(name="🎯 Ревьюеры заявок",
                    value="\n".join(role_lines)[:1024] or "—", inline=False)
    embed.add_field(
        name="📊 Сводка",
        value=(f"Администраторов: **{len(admin_members)}**\n"
               f"Модераторов: **{len(mod_lines)}**\n"
               f"Ревьюерских ролей: **{len(role_lines)}**"),
        inline=True)
    embed.set_footer(text="Quantum • Администрация")
    return embed


@bot.tree.command(name="admins", description="Администраторы и модераторы клана")
async def admins(i):
    """Кто администрирует клан: права Discord, модераторы бота, ревьюеры."""
    await i.response.defer(ephemeral=False)
    try:
        guild = i.guild
        if guild is None:
            await safe_send(i.followup, "❌ Команда работает только на сервере.")
            return

        admin_members = await _collect_admins(guild)
        mod_lines = await _collect_bot_mods(guild)
        role_lines = await _collect_reviewer_roles(guild)
        embed = _build_admins_embed(guild, admin_members, mod_lines, role_lines)
        await safe_send(i.followup, embed=embed)
    except Exception as e:
        await log_error(i.client, "/admins", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


# ============================================================
# /search — история заявок игрока в клан
# ============================================================
_STATUS_EMOJI = {
    "accepted": "✅",
    "rejected": "❌",
    "pending": "⏳",
    "voting": "🗳️",
    "withdrawn": "↩️",
}


def _fmt_dt(value) -> str:
    """ISO-строка времени → 'дд.мм.гггг чч:мм'."""
    if not value:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(str(value))
    except Exception:
        return str(value)[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%d.%m.%Y %H:%M")


def _build_search_embed(target, summary: dict, history: list) -> discord.Embed:
    """Embed с историей заявок игрока в клан."""
    total = int(summary.get("total", 0) or 0)
    accepted = int(summary.get("accepted", 0) or 0)
    rejected = int(summary.get("rejected", 0) or 0)
    in_work = int(summary.get("in_work", 0) or 0)
    clan_main = int(summary.get("clan_main", 0) or 0)
    clan_other = int(summary.get("clan_other", 0) or 0)

    embed = discord.Embed(
        title=f"🔎 Заявки: {target.display_name}",
        color=0x000000,
    )
    try:
        embed.set_thumbnail(url=target.display_avatar.url)
    except Exception:
        pass
    embed.description = f"Клан **{CLAN_NAME}** · всего заявок: **{total}**"

    embed.add_field(
        name="📊 Сводка",
        value=(f"**Всего заявок:** {total}\n"
               f"**Принято:** {accepted}\n"
               f"**Отклонено:** {rejected}\n"
               f"**В работе:** {in_work}"),
        inline=True,
    )
    embed.add_field(
        name="📂 По кланам",
        value=(f"**В клан {CLAN_NAME}:** {clan_main}\n"
               f"**В другие кланы:** {clan_other}"),
        inline=True,
    )

    lines: list[str] = []
    used = 0
    for item in history[:15]:
        status = (item.get("status") or "").strip().lower()
        emoji = _STATUS_EMOJI.get(status, "▫️")
        ident = (item.get("message_id") or item.get("app_id")
                 or item.get("user_id") or "")
        ticket = f"`ticket-{ident}`" if ident else "—"
        clan = item.get("clan_name") or "—"
        line = (f"{emoji} {ticket} · **{clan}** · "
                f"{_fmt_dt(item.get('created_at'))}")
        mod_id = item.get("moderator_id")
        if mod_id:
            line += f" · решил <@{mod_id}>"
        if used + len(line) + 1 > 1000:
            break
        lines.append(line)
        used += len(line) + 1
    rest = max(0, len(history) - 15)
    if rest:
        lines.append(f"_…и ещё {rest} заявок_")
    text = "\n".join(lines) if lines else "Заявок не найдено."
    embed.add_field(name="📜 История заявок", value=text[:1024], inline=False)

    embed.set_footer(text=f"Quantum • {now_utc().strftime('%d.%m.%Y %H:%M')}")
    return embed


@bot.tree.command(name="search", description="История заявок игрока в клан")
@app_commands.describe(user="Чьи заявки показать (по умолчанию — ваши)")
async def search(i, user: discord.Member = None):
    """История заявок игрока в клан: какие заявки подавал и чем они закончились.

    Ответ всегда ephemeral: свою историю видит только вызвавший команду.
    Чужую историю может запросить только модератор или админ.
    """
    await i.response.defer(ephemeral=True)
    try:
        member = i.user if isinstance(i.user, discord.Member) else None
        is_reviewer = bool(member and user_can_review(member))
        if not is_reviewer and i.guild is not None:
            is_reviewer = await is_moderator(i.user.id, i.guild)

        target = user or i.user
        if target.id != i.user.id and not is_reviewer:
            await safe_send(i.followup, "❌ У вас нет прав.", ephemeral=True)
            return

        summary = await applications_repo.get_history_summary(target.id)
        history = await applications_repo.get_user_history(target.id, limit=25)
        embed = _build_search_embed(target, summary, history)
        await safe_send(i.followup, embed=embed, ephemeral=True)
    except Exception as e:
        await log_error(i.client, "/search", e)
        try:
            await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)
        except Exception:
            pass


# ============================================================
# АКТИВНОСТЬ
# ============================================================
async def _get_steam_of(m):
    return await links_repo.get_link(m.id)


async def _activity_guard(i, m, need_admin=True):
    if need_admin and not admin_only(i):
        await i.response.send_message("❌", ephemeral=False)
        return None
    sid = await _get_steam_of(m)
    if not sid:
        await i.response.send_message("❌ Нет SteamID", ephemeral=False)
        return None
    return sid


@bot.tree.command(name="activity_set", description="Установить часы активности")
async def activity_set(i, member: discord.Member, server: str, hours: float):
    sid = await _activity_guard(i, member)
    if not sid:
        return
    if hours < 0:
        await i.response.send_message("❌", ephemeral=False)
        return
    await i.response.defer(ephemeral=False)
    try:
        p = await players_repo.get_player(sid)
        a = p.get("activity", {})
        if not isinstance(a, dict):
            a = {}
        a[server.upper()] = float(hours)
        p.update(activity=a, discord_id=str(member.id),
                 discord_name=member.display_name, updated_at=now_utc().isoformat())
        await players_repo.save_player(sid, p)
        total = sum(float(v) for v in a.values() if isinstance(v, (int, float)))
        await safe_send(i.followup,
            f"✅ {member.display_name} `{server.upper()}`: {hours} ч.\n"
            f"{build_activity_bar(total, ACTIVITY_GOAL_HOURS)}", ephemeral=False
        )
    except Exception as e:
        await log_error(i.client, "/activity_set", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="activity_clear", description="Сбросить активность")
async def activity_clear(i, member: discord.Member):
    sid = await _activity_guard(i, member)
    if not sid:
        return
    await i.response.defer(ephemeral=False)
    try:
        p = await players_repo.get_player(sid)
        p["activity"] = {}
        p["updated_at"] = now_utc().isoformat()
        await players_repo.save_player(sid, p)
        await safe_send(i.followup, "✅ Сброшено", ephemeral=False)
    except Exception as e:
        await log_error(i.client, "/activity_clear", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="activity_show", description="Активность игрока")
async def activity_show(i, member: discord.Member):
    sid = await _activity_guard(i, member, need_admin=False)
    if not sid:
        return
    await i.response.defer(ephemeral=False)
    try:
        a = (await players_repo.get_player(sid)).get("activity", {})
        if not a:
            await safe_send(i.followup, "ℹ️ Пусто", ephemeral=False)
            return
        lines, t = [], 0.0
        for s, h in sorted(a.items()):
            try:
                hh = float(h)
            except (ValueError, TypeError):
                continue
            t += hh
            lines.append(f"• `{s}`: {hh} ч.")
        await safe_send(i.followup,
            f"📊 **{member.display_name}**\n" + "\n".join(lines)
            + f"\n\n{build_activity_bar(t, ACTIVITY_GOAL_HOURS)}", ephemeral=False
        )
    except Exception as e:
        await log_error(i.client, "/activity_show", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="activity_all", description="Активность роли")
async def activity_all(i):
    await i.response.defer(ephemeral=False)
    try:
        role = i.guild.get_role(ACTIVITY_ROLE_ID) if ACTIVITY_ROLE_ID else None
        if role is None:
            await safe_send(i.followup, "❌ Роль не найдена", ephemeral=False)
            return
        ms = [m for m in role.members if not m.bot]
        if not ms:
            await safe_send(i.followup, "ℹ️ Пусто", ephemeral=False)
            return
        db, links = await players_repo.get_all_players(), await links_repo.get_all_links()
        data = []
        for m in ms:
            sid = links.get(str(m.id))
            h = 0.0
            if sid:
                a = db.get(sid, {}).get("activity", {})
                if isinstance(a, dict):
                    for v in a.values():
                        try:
                            h += float(v)
                        except (ValueError, TypeError):
                            pass
            data.append((m, h))
        data.sort(key=lambda x: x[1], reverse=True)
        body = "\n".join(f"{build_activity_bar(h, ACTIVITY_GOAL_HOURS)} - {m.mention}"
                         for m, h in data)
        done = sum(1 for _, h in data if h >= ACTIVITY_GOAL_HOURS)
        head = f"📊 **Роль {role.mention}**\n\n"
        foot = f"\n\n📈 Всего: {len(data)}\n✅ Выполнили: {done}\n⚠️ Малоактивных: {len(data) - done}"
        if len(head + body + foot) <= 2000:
            await safe_send(i.followup, head + body + foot, ephemeral=False)
        else:
            cut = body[:2000 - len(head) - len(foot) - 100]
            cut = cut[:cut.rfind("\n")]
            await safe_send(i.followup, head + cut + "\n_...обрезано_" + foot, ephemeral=False)
    except Exception as e:
        await log_error(i.client, "/activity_all", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


# ============================================================
# !setup — обновлённый текст
# ============================================================
@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    e = discord.Embed(title="Официальные условия вступления в клан.",
                      color=0x36393F)
    e.description = (
        "Перед подачей заявки внимательно ознакомьтесь с нашими требованиями.\n"
        "**Несоответствие любому из пунктов приведёт к отказу.**\n\n"
        "**Общие требования:**\n"
        "• Отсутствие VAC-банов в Steam и блокировок на cs2red.ru.\n"
        "• Отсутствие членства в других кланах.\n"
        "• Возраст — **от 12 лет**.\n\n"
        f"**Требования для {CLAN_NAME}:**\n"
        "• Рейтинг — **от 1.000 ммр**.\n"
        "• Онлайн — **не менее 3 часов в день**.\n\n"
        "**Что указать в заявке:**\n"
        "• Steam-профиль (ссылку или SteamID64)\n\n"
        "**Важно!**\n"
        "Если вам не напишут в течение 7 дней — заявка отклоняется.\n"
        "После принятия есть **2 дня** на вступление.\n\n"
        "По вопросам вступления: обратитесь к администрации."
    )
    v = ClanView()
    await safe_send(ctx.channel, embed=e, view=v)


# ============================================================
# ФОНОВАЯ ЗАДАЧА: ТРЕКЕР АКТИВНОСТИ
# ============================================================
@tasks.loop(seconds=ACTIVITY_TRACK_SECONDS)
async def activity_tracker():
    try:
        links = await links_repo.get_all_links()
        if not links:
            return
        online = await api.server_times()
        if not online:
            return
        db = await players_repo.get_all_players()
        n2s = {p.get("cs2red_nick"): s for s, p in db.items() if p.get("cs2red_nick")}
        seeded = await seen_repo.get_flag("_seeded")
        if not seeded:
            for sid in links.values():
                if sid in n2s:
                    continue
                u = await api.cs2red_user(sid)
                if u and u.get("nick"):
                    n2s[u["nick"]] = sid
                    p = db.setdefault(sid, {})
                    p["cs2red_nick"] = u["nick"]
                    await players_repo.save_player(sid, p)
        now = now_utc().isoformat()
        matched = 0
        for nick, secs in online.items():
            sid = n2s.get(nick)
            if not sid:
                continue
            prev = await seen_repo.get_activity(nick)
            delta = (secs - prev) if prev is not None and secs >= prev else \
                (min(secs, ACTIVITY_TRACK_DELTA * 1.5) if prev is not None else 0.0)
            await seen_repo.set_activity(nick, secs)
            if delta <= 0:
                continue
            p = db.get(sid, {})
            a = p.get("activity", {})
            if not isinstance(a, dict):
                a = {}
            a["AUTO"] = float(a.get("AUTO", 0)) + delta / 3600.0
            p.update(activity=a, discord_name=p.get("discord_name") or nick, updated_at=now)
            db[sid] = p
            await players_repo.save_player(sid, p)
            matched += 1
        await seen_repo.set_flag("_seeded", True)
        if matched:
            log(f"🕐 Трекер: +{matched}")
    except Exception as e:
        await log_error(bot, "activity_tracker", e)


@activity_tracker.before_loop
async def before_activity_tracker():
    await bot.wait_until_ready()


# ============================================================
# АВТОСИНХРОНИЗАЦИЯ + 2 ЭМБЕДА
# ============================================================
def _short(text: str, length: int) -> str:
    if len(text) <= length:
        return text
    return text[:length - 1] + "…"


def _pad(text: str, length: int) -> str:
    text = _short(text, length)
    return text + " " * (length - len(text))


async def build_auto_sync_embeds(guild: discord.Guild) -> list:
    links = await links_repo.get_all_links()

    rows = []
    for did_str, sid in links.items():
        try:
            did = int(did_str)
        except ValueError:
            continue
        member = guild.get_member(did)
        if member is None:
            continue

        nick_cs2 = "-"
        clan_name = "—"
        try:
            user = await api.cs2red_user(sid)
            if user and not user.get("_not_found"):
                nick_cs2 = user.get("nick", "-")
            clan = await api.player_clan(sid)
            if clan:
                clan_name = clan.get("clan_name", "—")
        except Exception:
            pass

        rows.append((member.display_name, sid, nick_cs2, clan_name))

    rows.sort(key=lambda x: (x[3], x[0].lower()))

    W_DISC = 20
    W_STEAM = 17
    W_CLAN = 20

    header = (f"{_pad('Discord', W_DISC)} | "
              f"{_pad('SteamID', W_STEAM)} | "
              f"{_pad('Клан', W_CLAN)}")
    divider = "-" * W_DISC + "-+-" + "-" * W_STEAM + "-+-" + "-" * W_CLAN
    header_block = header + "\n" + divider

    MAX_PAGE_CHARS = 3200
    pages = []
    current_lines = []
    current_len = len(header_block)

    for disc, sid, _nick_cs2, clan_name in rows:
        line = f"{_pad(disc, W_DISC)} | {_pad(sid, W_STEAM)} | {_pad(clan_name, W_CLAN)}"
        line_len = len(line) + 1
        if current_len + line_len > MAX_PAGE_CHARS and current_lines:
            pages.append(current_lines)
            current_lines = []
            current_len = len(header_block)
        current_lines.append(line)
        current_len += line_len

    if current_lines:
        pages.append(current_lines)

    if not pages:
        pages = [[]]

    embeds = []
    total_rows = len(rows)
    for idx, page in enumerate(pages):
        body = header_block
        if page:
            body += "\n" + "\n".join(page)
        else:
            body += "\n(нет привязанных игроков)"

        title = f"⏰ Автосинхронизация — {utc_stamp()} UTC"
        if len(pages) > 1:
            title += f" (стр. {idx + 1}/{len(pages)})"

        e = discord.Embed(title=title, description=f"```\n{body}\n```",
                          color=0x57F287, timestamp=now_utc())
        e.set_footer(text=f"Всего привязано: {total_rows}")
        embeds.append(e)

    not_linked = [m for m in guild.members
                  if not m.bot and str(m.id) not in links]
    not_linked.sort(key=lambda m: m.display_name.lower())

    if not_linked:
        nl_lines = [f"• {_short(m.display_name, 30)} (`{m.id}`)"
                    for m in not_linked[:100]]
        nl_text = "\n".join(nl_lines)
        if len(not_linked) > 100:
            nl_text += f"\n..._и ещё {len(not_linked) - 100}_"
        e2 = discord.Embed(title="🚫 Не авторизованы через /link",
                           description=nl_text[:4000], color=0x000000,
                           timestamp=now_utc())
        e2.set_footer(text=f"Всего без /link: {len(not_linked)}")
    else:
        e2 = discord.Embed(title="🚫 Не авторизованы через /link",
                           description="Все участники авторизованы ✅",
                           color=0x000000, timestamp=now_utc())

    embeds.append(e2)
    return embeds


@tasks.loop(minutes=SYNC_INTERVAL_MINUTES)
async def auto_sync():
    try:
        log(f"⏰ auto_sync {now_utc().isoformat()}")
        links = await links_repo.get_all_links()
        guild = bot.get_guild(GUILD_ID)
        if not links or guild is None:
            return

        for did, sid in links.items():
            m = guild.get_member(int(did))
            if m is None:
                continue
            try:
                await sync_role_by_id(guild, m, sid)
                await sync_clan_role_by_id(guild, m, sid)
            except Exception as e:
                await log_error(bot, f"auto_sync:{did}", e)
            await asyncio.sleep(REQUEST_DELAY)

        try:
            channel = bot.get_channel(LOG_CHANNEL_ID)
            if channel is not None:
                embeds = await build_auto_sync_embeds(guild)
                for emb in embeds:
                    try:
                        await safe_send(channel, embed=emb)
                    except Exception as e:
                        await log_error(bot, "auto_sync.report.embed", e)
                    await asyncio.sleep(2.0)
        except Exception as e:
            await log_error(bot, "auto_sync.report", e)

    except Exception as e:
        await log_error(bot, "auto_sync", e)


@auto_sync.error
async def auto_sync_error(err):
    await log_error(bot, "auto_sync.error", err)


@auto_sync.before_loop
async def before_auto_sync():
    await bot.wait_until_ready()
    await asyncio.sleep(random.randint(0, 120))


# ============================================================
# ЧАСОВАЯ СИНХРОНИЗАЦИЯ КЛАНОВ
# ============================================================
@tasks.loop(hours=1)
async def hourly_clan_sync():
    """Автоматически, раз в час, обновляет роли кланов для всех привязанных игроков."""
    try:
        links = await links_repo.get_all_links()
        guild = bot.get_guild(GUILD_ID)
        if not links or guild is None:
            return

        checked = 0
        updated = 0
        # Реально обрабатываем кланы: проверим клан игрока через cs2red и применим роль.
        for did, sid in links.items():
            m = guild.get_member(int(did)) if str(did).isdigit() else None
            if m is None:
                continue

            result = await sync_clan_role_by_id(guild, m, sid)
            # Статус: "создана роль"/"выдан"/"уже в"/"клан снят"/"без клана" = изменений; 
            # при отсутствии изменений тоже считаем проверенным.
            checked += 1
            if any(k in result for k in ("выдан", "создана", "клан снят", "уже в")):
                updated += 1

            await asyncio.sleep(api.RATE_LIMIT_SECONDS)

            # Если cs2red начал блокировать (403/429) — прерываем цикл и ждём следующего запуска.
            if api.blocked_reason in ("403", "429"):
                reason = api.blocked_reason
                log(f"⏰ hourly_clan_sync: cs2red блокирует ({reason}) — пауза до следующего цикла")
                break

        log(f"⏰ hourly_clan_sync: проверено {checked}, ролей обновлено {updated}")
        # Логируем в канал, если он доступен.
        if LOG_CHANNEL_ID:
            ch = bot.get_channel(LOG_CHANNEL_ID)
            if ch is not None:
                try:
                    await safe_send(ch, f"⏰ Кланы: проверено {checked}, обновлено {updated}.",
                                    title="⏰ Часовая синхронизация кланов")
                except Exception as e:
                    log(f"⚠️ hourly_clan_sync лог: {e}")
    except Exception as e:
        await log_error(bot, "hourly_clan_sync", e)


@hourly_clan_sync.before_loop
async def before_hourly_clan_sync():
    await bot.wait_until_ready()
    await asyncio.sleep(random.randint(60, 180))


# ============================================================
# ИИ-АГЕНТ: НАБЛЮДЕНИЕ ЗА СИСТЕМОЙ, ОТЧЁТЫ, ИДЕИ
# ============================================================
AGENT_CHECK_MINUTES = env_int("AGENT_CHECK_MINUTES", 30)


@tasks.loop(minutes=AGENT_CHECK_MINUTES)
async def agent_watch():
    """Цикл ИИ-агента: следит за системой, шлёт отчёты и идеи.

    Отчёт и идея публикуются по своему расписанию (agent.py), а не каждый
    цикл: иначе канал агента забивается сообщениями.
    """
    try:
        providers = ai_service.providers if ai_service else []
        extra = {
            "uptime": _uptime_str(),
            "ping": f"{bot.latency * 1000:.0f} мс",
            "ai": _ai_status(),
            "rules": _rules_status(),
            "github": _github_status(),
            "cs2red": getattr(api, "last_status", None) or "нет данных",
        }
        await agent.run_cycle(providers=providers, extra=extra)
    except Exception as e:
        await log_error(bot, "agent_watch", e)


@agent_watch.before_loop
async def before_agent_watch():
    await bot.wait_until_ready()
    await asyncio.sleep(random.randint(60, 180))


# ============================================================
# ИИ: /ask, !ask, /rules, !rules
# ============================================================
def _parse_ai_response(response: str) -> dict:
    """Разбирает ответ ИИ формата ОТВЕТ/НАКАЗАНИЕ/ОСНОВАНИЕ."""
    result = {"answer": "", "punishment": "", "basis": ""}
    if not response:
        return result
    for line in response.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("ответ:"):
            result["answer"] = line.split(":", 1)[1].strip()
        elif low.startswith("наказание:"):
            result["punishment"] = line.split(":", 1)[1].strip()
        elif low.startswith("основание:"):
            result["basis"] = line.split(":", 1)[1].strip()
    if not result["answer"]:
        # ИИ не следовал шаблону — кладём весь ответ как есть в поле Ответ.
        result["answer"] = response
    return result


def _build_ai_embed(res: dict, query: str) -> discord.Embed:
    """Собирает красивый Embed с ответом ИИ."""
    response = res.get("response") or ""
    rules_used = res.get("rules_used") or []
    cached = res.get("cached")
    provider = res.get("provider") or ""

    # Заголовок зависит от того, отвечали ли по конкретным правилам.
    if rules_used:
        title = "📖 Разбор правил"
    else:
        title = "💡 Ответ на вопрос"

    embed = discord.Embed(
        title=title,
        color=0x000000,
        timestamp=now_utc(),
    )
    embed.add_field(name="Вопрос", value=query[:1024], inline=False)

    parsed = _parse_ai_response(response)

    # VIP-пункты: номера начинаются с 7.
    is_vip = any(str(rid).startswith("7.") for rid in rules_used)

    # Значения полей с ограничением по длине (1024 на поле).
    def _field(value: str) -> str:
        if not value or value == "-":
            return "_— нет данных_"
        if len(value) > 4090:
            return value[:4087] + "..."
        return value

    answer = parsed.get("answer") or response or "_пустой ответ_"
    punishment = parsed.get("punishment")
    basis = parsed.get("basis") or ("Правила: " + ", ".join(rules_used) if rules_used else "")

    # Поле «Ответ» — может быть длинным, режем на части по 1024.
    answer_text = _field(answer)
    if len(answer_text) <= 1024:
        embed.add_field(name="Ответ", value=answer_text, inline=False)
    else:
        chunks = [answer_text[i:i + 1024] for i in range(0, len(answer_text), 1024)]
        embed.add_field(name=f"Ответ (часть 1/{len(chunks)})",
                        value=chunks[0], inline=False)
        for idx, chunk in enumerate(chunks[1:], start=2):
            embed.add_field(name=f"Ответ (часть {idx}/{len(chunks)})",
                            value=chunk, inline=False)

    # Для VIP-пунктов поле «Наказание» скрываем: это описание функций, а не санкция.
    if not is_vip:
        embed.add_field(name="Наказание", value=_field(punishment), inline=False)

    # Для VIP-пунктов поле «Основание» не выводим (это описание функций).
    if basis and not is_vip:
        basis_text = _field(basis)
        if not basis_text.startswith("Правила") and not basis_text.startswith("П.П"):
            basis_text = "П.П. " + basis_text
        embed.add_field(name="Основание", value=basis_text, inline=False)

    footer = []
    if provider:
        footer.append(f"Провайдер: {provider}")
    footer.append("Из кэша: Да" if cached else "Из кэша: Нет")
    embed.set_footer(text=" | ".join(footer))
    return embed


def _format_ai_error(res: dict) -> str:
    if res.get("limited"):
        remaining = res.get("remaining", 0)
        return (f"⏳ Превышен лимит запросов к ИИ. "
                f"Осталось попыток в часе: **{remaining}**.")
    return f"❌ {res.get('error') or 'Не удалось получить ответ.'}"


@bot.tree.command(name="ask", description="Задать вопрос по правилам")
@app_commands.describe(question="Ваш вопрос")
async def ask(i, question: str):
    global ai_service
    await i.response.defer(ephemeral=False)
    if ai_service is None:
        await safe_send(i.followup, "❌ ИИ-сервис ещё не готов.", ephemeral=False)
        return
    res = await ai_service.ask(i.user.id, question)
    if not res.get("ok"):
        await safe_send(i.followup, _format_ai_error(res), ephemeral=False)
        return
    embed = _build_ai_embed(res, question)
    await safe_send(i.followup, embed=embed, ephemeral=False)


@bot.tree.command(name="rules", description="Показать правила клана")
async def rules(i):
    await i.response.defer(ephemeral=False)
    if ai_service is None or not getattr(ai_service, "rules", None):
        await safe_send(i.followup, "❌ Правила не загружены.", ephemeral=False)
        return
    idx = ai_service.rules
    if not idx.ready:
        idx.load()
    lines = []
    seen = set()
    for doc in idx._docs:
        if doc["id"] not in seen:
            seen.add(doc["id"])
            lines.append(f"**{doc['id']}** — {doc['text'][:80]}")
        if len(lines) >= 50:
            break
    if not lines:
        await safe_send(i.followup, "ℹ️ Правила не найдены. Положи rules.txt в DATA_DIR.",
                        ephemeral=False)
        return
    await safe_send(i.followup, "📜 **Правила клана:**\n" + "\n".join(lines)[:1900],
                        title="📖 Правила", ephemeral=False)


@bot.command(name="ask")
async def ask_legacy(ctx, *, question: str):
    global ai_service
    if ai_service is None:
        await safe_send(ctx.channel, "❌ ИИ-сервис ещё не готов.")
        return
    res = await ai_service.ask(ctx.author.id, question)
    if not res.get("ok"):
        await safe_send(ctx.channel, _format_ai_error(res))
        return
    embed = _build_ai_embed(res, question)
    await safe_send(ctx.channel, embed=embed)


@bot.command(name="rules")
async def rules_legacy(ctx):
    if ai_service is None or not getattr(ai_service, "rules", None):
        await safe_send(ctx.channel, "❌ Правила не загружены.")
        return
    idx = ai_service.rules
    if not idx.ready:
        idx.load()
    lines = []
    seen = set()
    for doc in idx._docs:
        if doc["id"] not in seen:
            seen.add(doc["id"])
            lines.append(f"**{doc['id']}** — {doc['text'][:80]}")
        if len(lines) >= 50:
            break
    if not lines:
        await safe_send(ctx.channel, "ℹ️ Правила не найдены. Положи rules.txt в DATA_DIR.")
        return
    await safe_send(ctx.channel, "📜 **Правила клана:**\n" + "\n".join(lines)[:1900],
                        title="📖 Правила")


# ============================================================
# ОЧИСТКА КЭША ИИ (только администраторы)
# ============================================================
@bot.tree.command(name="ai_clear_cache", description="Очистить кэш ответов ИИ (админ)")
@app_commands.default_permissions(administrator=True)
async def ai_clear_cache(i):
    if not i.user.guild_permissions.administrator:
        await i.response.send_message("❌ Нужны права администратора.", ephemeral=True)
        return
    await i.response.defer(ephemeral=False)
    try:
        removed = await clear_cache()
        await safe_send(i.followup, "✅ Кэш ИИ успешно очищен.", title="🤖 Кэш ИИ",
                        ephemeral=False)
        if removed:
            await log_to_channel(i.client,
                f"🗑 Кэш ИИ очищен командой {i.user.mention} (удалено: {removed})")
    except Exception as e:
        await log_error(i.client, "/ai_clear_cache", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.command(name="ai_clear_cache")
@commands.has_permissions(administrator=True)
async def ai_clear_cache_legacy(ctx):
    try:
        removed = await clear_cache()
        await safe_send(ctx.channel, "✅ Кэш ИИ успешно очищен.", title="🤖 Кэш ИИ")
        if removed:
            await log_to_channel(ctx.bot,
                f"🗑 Кэш ИИ очищен командой {ctx.author.mention} (удалено: {removed})")
    except Exception as e:
        await log_error(ctx.bot, "!ai_clear_cache", e)
        await safe_send(ctx.channel, f"❌ Ошибка: {str(e)[:200]}")


# ============================================================
# ПАНЕЛЬ УПРАВЛЕНИЯ (/panel)
# ============================================================
async def _panel_guard(interaction) -> bool:
    """Проверка прав для кнопок панели.

    Кнопки панели видны всем в канале, поэтому каждая кнопка обязана
    сама проверять права: иначе любой участник сервера мог запустить
    привязку всех и синхронизацию ролей.
    """
    if interaction.user.guild_permissions.administrator:
        return True
    try:
        guild = interaction.guild
        if guild is not None and await is_moderator(interaction.user.id, guild):
            return True
    except Exception:
        pass
    try:
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
    except Exception:
        pass
    return False


class PanelView(discord.ui.View):
    """Панель управления ботом: кнопки запускают служебные действия."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔄 Привязать всех", style=discord.ButtonStyle.primary,
                       custom_id="panel_link_all", row=0)
    async def panel_link_all(self, interaction, button):
        if not await _panel_guard(interaction):
            return
        button.disabled = True
        await interaction.response.defer(ephemeral=True)
        # Полный запуск /auto_link (прогресс-бар, защита от 403/429).
        try:
            await run_auto_link(interaction, ephemeral=True)
        except Exception as e:
            log(f"⚠️ panel_link_all: {e}")
            await safe_send(interaction.followup, f"❌ Ошибка: {str(e)[:200]}",
                            ephemeral=True)

    @discord.ui.button(label="🔗 Синхронизация", style=discord.ButtonStyle.secondary,
                       custom_id="panel_sync", row=0)
    async def panel_sync(self, interaction, button):
        if not await _panel_guard(interaction):
            return
        button.disabled = True
        await interaction.response.defer(ephemeral=True)
        # Полный запуск /sync_all (прогресс-бар, остановка при 403/429).
        try:
            await run_sync_all(interaction, ephemeral=True)
        except Exception as e:
            log(f"⚠️ panel_sync: {e}")
            await safe_send(interaction.followup, f"❌ Ошибка: {str(e)[:200]}",
                            ephemeral=True)

    @discord.ui.button(label="📖 Обновить правила", style=discord.ButtonStyle.secondary,
                       custom_id="panel_rules", row=1)
    async def panel_rules(self, interaction, button):
        if not await _panel_guard(interaction):
            return
        button.disabled = True
        await interaction.response.defer(ephemeral=True)
        if ai_service is None:
            await interaction.followup.send("❌ ИИ-сервис не инициализирован.",
                                            ephemeral=True)
            return
        n = ai_service.rules.load()
        await interaction.followup.send(f"✅ Правила обновлены: {n} пунктов.",
                                        ephemeral=True)

    @discord.ui.button(label="💚 Health", style=discord.ButtonStyle.success,
                       custom_id="panel_health", row=1)
    async def panel_health(self, interaction, button):
        if not await _panel_guard(interaction):
            return
        button.disabled = True
        await interaction.response.defer(ephemeral=True)
        try:
            embed = await build_panel_health_embed()
            await safe_send(interaction.followup, embed=embed, ephemeral=True)
        except Exception as e:
            log(f"⚠️ panel_health: {e}")
            try:
                await safe_send(interaction.followup,
                                f"❌ Ошибка при проверке состояния: {str(e)[:200]}",
                                ephemeral=True)
            except Exception:
                pass


@bot.tree.command(name="panel", description="Панель управления ботом (админ)")
@app_commands.default_permissions(administrator=True)
async def panel(i):
    await i.response.defer(ephemeral=True)
    if not await _admin_guard(i):
        return
    embed = await build_panel_embed()
    view = PanelView()
    # Панель — только для админов, поэтому показываем её ephemeral:
    # в канале её не видно, кнопки не достаются посторонним.
    await safe_send(i.followup, embed=embed, view=view, ephemeral=True)


def _uptime_str() -> str:
    up = int(time.time() - BOT_START_TIME)
    d, r = divmod(up, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин {s} сек"
    return f"{s} сек"


def _ai_status() -> str:
    """Статус ИИ-провайдеров (для панели и отчётов агента)."""
    if ai_service is None:
        return "Не инициализирован"
    prov = ai_service.providers
    if not prov:
        return "Нет настроенных провайдеров"
    p = prov[0]
    req = sum(getattr(x, "requests", 0) for x in prov)
    errs = sum(getattr(x, "failures", 0) for x in prov)
    model = getattr(p, "_model", "?")
    return (f"{p.name} ({model})\nпровайдеров: {len(prov)}\n"
            f"запросов: {req}, ошибок: {errs}")


def _rules_status() -> str:
    """Статус правил rules.txt (для панели и отчётов агента)."""
    if ai_service is None or ai_service.rules is None:
        return "Не загружены"
    rules = ai_service.rules
    n_docs = len(getattr(rules, "_docs", []) or [])
    syms = sum(len(d.get("text", "")) for d in getattr(rules, "_docs", []) or [])
    return f"Пунктов: {n_docs}\nСимволов: {syms}\nИсточник: rules.txt"


async def build_panel_health_embed() -> discord.Embed:
    """Собирает Embed со сводным состоянием систем (для кнопки Health)."""
    e = discord.Embed(
        title="🩺 Состояние системы",
        color=0x000000,
        timestamp=now_utc(),
    )
    # Базовые метрики
    e.add_field(name="Аптайм", value=_uptime_str(), inline=True)
    e.add_field(name="WS Пинг", value=f"{bot.latency * 1000:.0f} мс", inline=True)
    e.add_field(name="Cookies", value=str(len(CS2RED_COOKIES)), inline=True)
    linked = await links_repo.count_all()
    e.add_field(name="Привязок", value=str(linked), inline=True)
    try:
        apps_total = await applications_repo.count_applications()
        apps_pending = await applications_repo.count_pending_applications()
    except Exception:
        apps_total = apps_pending = "?"
    e.add_field(name="Заявок", value=f"{apps_total} (в работе: {apps_pending})", inline=True)

    # Блок ИИ
    e.add_field(name="🤖 ИИ", value=_ai_status(), inline=False)

    # Блок Правила
    e.add_field(name="📖 Правила", value=_rules_status(), inline=False)

    # cs2red и ошибки: используем api.last_status, ошибок в лог-файле — считаем.
    last_status = getattr(api, "last_status", None)
    e.add_field(name="📡 cs2red", value=f"HTTP: {last_status}" if last_status else "нет данных",
                inline=True)

    # GitHub-синк
    e.add_field(name="🐙 GitHub", value=_github_status(), inline=True)
    e.set_footer(text="Quantum • Панель администратора")
    return e


def _github_status() -> str:
    """Формирует строку статуса GitHub-синка для панели."""
    if not github_sync.enabled:
        missing = []
        if not getattr(github_sync, "token", ""):
            missing.append("GITHUB_TOKEN")
        if not getattr(github_sync, "repo", ""):
            missing.append("GITHUB_REPO")
        return f"Синк не настроен (нет {', '.join(missing)})"
    state = ("ошибка: " + (github_sync.last_error or "")[:80]
             if github_sync.last_error else "работает")
    return (f"статус: {state}\n"
            f"репозиторий: {getattr(github_sync, 'repo', '?')}\n"
            f"ветка: {getattr(github_sync, 'branch', '?')}\n"
            f"отправлено: {github_sync.pushed_total}, получено: {github_sync.pulled_total}")


async def build_panel_embed() -> discord.Embed:
    """Собирает расширенную Embed-панель управления."""
    e = discord.Embed(
        title="🛠 Панель управления",
        description="Админ-панель бота. Выберите действие кнопками ниже.",
        color=0x000000,
        timestamp=now_utc(),
    )
    e.add_field(name="Аптайм", value=_uptime_str(), inline=True)
    e.add_field(name="WS Пинг", value=f"{bot.latency * 1000:.0f} мс", inline=True)
    e.add_field(name="Cookies", value=str(len(CS2RED_COOKIES)), inline=True)
    try:
        linked = await links_repo.count_all()
    except Exception:
        linked = "?"
    e.add_field(name="Привязок", value=str(linked), inline=True)
    try:
        apps_total = await applications_repo.count_applications()
    except Exception:
        apps_total = "?"
    e.add_field(name="Заявок", value=str(apps_total), inline=True)

    # ИИ
    if ai_service is not None:
        prov = ai_service.providers
        if prov:
            p = prov[0]
            model = getattr(p, "_model", "?")
            req = sum(getattr(x, "requests", 0) for x in prov)
            errs = sum(getattr(x, "failures", 0) for x in prov)
            ai_txt = (f"статус: работает · провайдер: {p.name}\n"
                      f"модель: {model}\nзапросов: {req}, ошибок: {errs}")
        else:
            ai_txt = "Нет настроенных провайдеров"
    else:
        ai_txt = "Не инициализирован"
    e.add_field(name="🤖 ИИ", value=ai_txt, inline=False)

    # Правила
    rules_parts = []
    if ai_service is not None and ai_service.rules is not None:
        docs = getattr(ai_service.rules, "_docs", []) or []
        rules_parts.append(f"пунктов: {len(docs)}")
        rules_parts.append(f"символов: {sum(len(d.get('text', '')) for d in docs)}")
        rules_parts.append("источник: rules.txt")
        rules_parts.append("обновлено: последний запуск/обновление")
    else:
        rules_parts.append("не загружены")
    e.add_field(name="📖 Правила", value="\n".join(rules_parts), inline=False)

    # GitHub
    e.add_field(name="🐙 GitHub", value=_github_status(), inline=True)

    # Что делают кнопки
    btns = (
        "🔄 **Привязать всех** — автопривязка по никам всех непривязанных\n"
        "🔗 **Синхронизация** — синхронизация ролей привязанных\n"
        "📖 **Обновить правила** — перечитать rules.txt (база знаний)\n"
        "💚 **Health** — развернуть состояние систем"
    )
    e.add_field(name="⚙️ Что делают кнопки", value=btns, inline=False)
    e.set_footer(text="Quantum • Панель администратора")
    return e


# ============================================================
# HTTP-СЕРВЕР ДЛЯ RENDER
# ============================================================
app = Flask(__name__)


@app.route("/")
@app.route("/health")
def flask_health():
    """Health-check для Render.

    Имя НЕ `health`: в модуле уже есть слэш-команда `health`, и
    переопределение одноимённой функции путало бы читателя кода
    (и сломало бы `from script import health`).
    """
    return "Bot is running"


def run_health_server():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


# ============================================================
# ЗАПУСК
# ============================================================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log("❌ DISCORD_TOKEN не задан!")
    elif GUILD_ID == 0:
        log("❌ GUILD_ID не задан!")
    else:
        threading.Thread(target=run_health_server, daemon=True).start()
        bot.run(DISCORD_TOKEN)
