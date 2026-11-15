"""
Quantum - бот клана (v18: исправлен Cloudflare 429, все safe_send)
"""
import os
import re
import json
import logging
import time
import random
import asyncio
import datetime
import functools
import math
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
from quantum.env import env_int, env_float
from quantum.migrations import import_json_to_sqlite
from quantum.agent import Agent
from quantum.repositories import votes as votes_repo
from quantum.repositories import seen as seen_repo
from quantum.repositories import links as links_repo
from quantum.repositories import players as players_repo
from quantum.repositories import users as users_repo
from quantum.repositories import moderation as moderation_repo
from quantum.repositories import applications as applications_repo
from quantum.repositories import audit as audit_repo
from quantum.ai import AIService
from github_sync import GitHubSync
from quantum.ai.cache import count_cache, clear_cache

# BeautifulSoup нужен для парсинга HTML админки cs2red (вкладка «Активность»).
# Если пакет не установлен — бот не должен падать: команда тогда вернёт
# понятную ошибку, а остальные функции работают как обычно.
try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except Exception:  # pragma: no cover - зависит от окружения
    BeautifulSoup = None
    _HAS_BS4 = False

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


def _write_json_file(path, payload) -> None:
    """Синхронная запись JSON. Вызывать через asyncio.to_thread."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _read_json_file(path):
    """Синхронное чтение JSON. Вызывать через asyncio.to_thread."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


UTC = datetime.UTC


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def log(msg: str):
    print(redact(msg), flush=True)


# ---------- logging -> общий лог -----------------------------------------
# Модули (quantum.env, quantum.db, quantum.ai, ...) пишут через logging.
# Единый обработчик проводит их записи через тот же вывод и маскировку
# секретов, что и log(), а уровень задаётся переменной LOG_LEVEL.
def _log_level(value) -> int:
    """Уровень из строки (DEBUG/INFO/...); неизвестный — INFO."""
    name = (value or "INFO").strip().upper()
    level = getattr(logging, name, None)
    return level if isinstance(level, int) else logging.INFO


class _LogBridge(logging.Handler):
    """Проводит записи logging в общий лог бота (stdout с маскировкой).

    Формат «LEVEL имя: сообщение» показывает источник записи. ``redact``
    ищется лениво: при импорте script.py обработчик может сработать до
    того, как функция объявлена ниже.
    """

    def emit(self, record):
        try:
            text = self.format(record)
            _redact = globals().get("redact")
            print(_redact(text) if _redact else text, flush=True)
        except Exception:
            self.handleError(record)


def setup_logging(level=None) -> None:
    """Единая настройка логирования (вызывается при импорте script)."""
    root = logging.getLogger()
    root.setLevel(level if level is not None else _LOG_LEVEL)
    # Повторный импорт/перезагрузка не должен плодить обработчики.
    for handler in list(root.handlers):
        if isinstance(handler, _LogBridge):
            root.removeHandler(handler)
    bridge = _LogBridge()
    bridge.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(bridge)
    # discord.py и aiohttp на INFO шумят на каждый запрос — только важное.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


_LOG_LEVEL = _log_level(os.getenv("LOG_LEVEL"))
setup_logging()


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
# Канал аудита админских и модераторских действий.
AUDIT_LOG_CHANNEL_ID = env_int("AUDIT_LOG_CHANNEL_ID", 1555204495682568202)
# Куда /check присылает мини-отчёт о найденных мутах/банах.
CHECK_LOG_CHANNEL_ID = env_int("CHECK_LOG_CHANNEL_ID", LOG_CHANNEL_ID)

# Канал ИИ-агента: ошибки команд, отчёты о состоянии, идеи по улучшению.
AGENT_CHANNEL_ID = env_int("AGENT_CHANNEL_ID", 1556427610160889907)

ALLOWED_ROLE_ID = env_int("ALLOWED_ROLE_ID", 0)
REVIEWER_ROLE_ID_2 = env_int("REVIEWER_ROLE_ID_2", 0)
SPAM_VOTE_ROLE_ID = env_int("SPAM_VOTE_ROLE_ID", 0)
EXTRA_REVIEWER_ROLE_1 = env_int("EXTRA_REVIEWER_ROLE_1", 0)
EXTRA_REVIEWER_ROLE_2 = env_int("EXTRA_REVIEWER_ROLE_2", 0)
EXTRA_REVIEWER_ROLE_3 = env_int("EXTRA_REVIEWER_ROLE_3", 0)

AUTO_ROLE_ID = env_int("AUTO_ROLE_ID", 0)
# Роль, которая выдаётся автоматически при принятии заявки в клан.
ACCEPT_ROLE_ID = env_int("ACCEPT_ROLE_ID", 1448368118190837800)
BYPASS_ROLE_ID = env_int("BYPASS_ROLE_ID", 0)
NO_COOLDOWN_ROLE_ID = env_int("NO_COOLDOWN_ROLE_ID", 0)

ACTIVITY_ROLE_ID = env_int("ACTIVITY_ROLE_ID", 1448368118190837800)

CLAN_NAME = (os.getenv("CLAN_NAME") or "TALENT").strip() or "TALENT"
CLAN_URL = os.getenv("CLAN_URL", "https://cs2red.ru/clans/id/47")
VOTES_REQUIRED = env_int("VOTES_REQUIRED", 1)

# Роли, чьих участников бот добавляет в ветку заявки и упоминает в ней.
APPLICATION_PING_ROLE_IDS = [1447953747161387131, 1524071237796626523,
                             1378326630031818802]

# Роль клана TALENT, по которой /check и /check_all собирают игроков.
TALENT_ROLE_ID = env_int("TALENT_ROLE_ID", 1448368118190837800)

# Глубина отчёта /check_all: кого замутили за последние N дней.
CHECK_ALL_DAYS = env_int("CHECK_ALL_DAYS", 14)
# Сколько игроков с мутами показывать в embed'е /check_all.
CHECK_ALL_MAX_MEMBERS = env_int("CHECK_ALL_MAX_MEMBERS", 10)
# Минимальный недельный онлайн (часы) для /check_onlineall: кто меньше —
# попадает в отдельное поле «малоактивные».
ONLINE_MIN_HOURS = env_float("ONLINE_MIN_HOURS", 9.0)

# Фиксированный custom_id выпадающего списка заявок. Нужен, чтобы список
# работал после рестарта бота: со случайным custom_id discord.py теряет
# обработчик, и Discord отвечает «Приложение не ответило вовремя» (10062).
CLAN_SELECT_CUSTOM_ID = "clan_select_quantum"

ACTIVITY_GOAL_HOURS = 8
ACTIVITY_MIN_HOURS = 8
APPLY_EMBED_COLOR = 0x000000  # все embed'ы бота — чёрные

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


# Названия ролей cs2red, если сайт не отдал список (/api/data/main).
# Ключ — roleId с сайта, значение — название Discord-роли, как на сайте.
# Переопределяется через env SITE_ROLE_NAMES, например {"2": "Куратор"}.
SITE_ROLE_NAMES = {
    1: "Мл. Модер", 3: "Модератор", 5: "Админ",
    6: "С. Админ", 7: "Спонсор", 13: "Гл.Модератор",
}
try:
    _site_roles_override = json.loads(os.getenv("SITE_ROLE_NAMES", "") or "{}")
except (ValueError, TypeError):
    _site_roles_override = {}
if isinstance(_site_roles_override, dict):
    for _rid, _rname in _site_roles_override.items():
        if str(_rid).strip().isdigit() and str(_rname).strip():
            SITE_ROLE_NAMES[int(_rid)] = str(_rname).strip()

# Discord-роли, которые бот создал сам под роли с сайта. По этому списку
# роль снимается при смене роли на сайте; чужие роли не трогаются.
SITE_ROLES_FILE = "site_roles.json"


def _role_id_int(role_id):
    try:
        return int(role_id)
    except (TypeError, ValueError):
        return None


def site_role_name(role_id) -> str | None:
    """Название роли на сайте по roleId (без сетевых запросов)."""
    rid = _role_id_int(role_id)
    if rid is None or rid <= 0:
        return None
    return (SITE_ROLE_NAMES.get(rid) or ROLE_DISPLAY_MAP.get(rid)
            or ROLE_MAP.get(rid))


def role_display_name(role_id) -> str:
    return site_role_name(role_id) or "Игрок"


def _load_site_role_names() -> set:
    try:
        p = data_path(SITE_ROLES_FILE)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(x).strip() for x in data if str(x).strip()}
    except Exception as e:
        log(f"⚠️ не прочитал {SITE_ROLES_FILE}: {e}")
    return set()


def _remember_site_role_name(name: str):
    name = (name or "").strip()
    if not name:
        return
    names = _load_site_role_names()
    if name in names:
        return
    names.add(name)
    try:
        data_path(SITE_ROLES_FILE).write_text(
            json.dumps(sorted(names), ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:
        log(f"⚠️ не записал {SITE_ROLES_FILE}: {e}")


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

# --- защита cs2red и скорость -------------------------------------------
# Пауза между запросами к cs2red. Меньше 1.5 сек ставить не стоит: сайт
# отвечает 429 и сам начинает резать запросы.
CS2RED_RATE_LIMIT_SECONDS = env_float("CS2RED_RATE_LIMIT_SECONDS", 2.5)
# «Предохранитель»: после 429/403/5xx бот не долбит сайт, а сразу отвечает
# ошибкой и ждёт. Пауза растёт экспоненциально, но не больше MAX.
CS2RED_BLOCK_BASE_SECONDS = env_int("CS2RED_BLOCK_BASE_SECONDS", 60)
CS2RED_BLOCK_MAX_SECONDS = env_int("CS2RED_BLOCK_MAX_SECONDS", 1800)
# Короткий кэш профиля игрока: повторные команды не дёргают cs2red зря.
CS2RED_USER_CACHE_TTL = env_int("CS2RED_USER_CACHE_TTL", 60)


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

# Фоновые задачи должны жить в ссылке: задача, на которую никто не
# ссылается, может быть собрана GC до завершения.
_background_tasks: set = set()


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


# Discord считает сумму текста ВСЕХ embed'ов в одном сообщении и режет
# сообщение с ошибкой 50035 «Embed size exceeds maximum size of 6000».
# Один embed с описанием на 3200 символов ещё проходит, а два — уже нет,
# поэтому пачку embed'ов всегда разбиваем по фактическому размеру.
EMBED_MSG_LIMIT = 5800       # запас до жёсткого лимита Discord (6000)
EMBED_MSG_MAX_COUNT = 10     # максимум embed'ов в одном сообщении


def _embed_chars(embed) -> int:
    """Длина текста embed'а — именно её Discord суммирует для лимита 6000."""
    if not isinstance(embed, discord.Embed):
        return 0
    total = len(embed.title or "") + len(embed.description or "")
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for f in embed.fields:
        total += len(f.name or "") + len(f.value or "")
    return total


def _trim_embed(embed, limit: int = EMBED_MSG_LIMIT):
    """Обрезает description, чтобы даже одиночный embed влез в лимит."""
    if not isinstance(embed, discord.Embed):
        return embed
    over = _embed_chars(embed) - limit
    if over > 0 and embed.description:
        desc = embed.description
        keep = max(0, len(desc) - over - 1)
        embed.description = desc[:keep] + "…"
    return embed


def _batch_embeds(embeds, limit: int = EMBED_MSG_LIMIT,
                  max_count: int = EMBED_MSG_MAX_COUNT):
    """Группирует embed'ы в пачки по суммарному размеру и количеству.

    Гарантирует, что каждая пачка пройдёт проверку Discord (сумма текста
    всех embed'ов ≤ ``limit``, количество ≤ ``max_count``).
    """
    batches, current, current_len = [], [], 0
    for e in embeds:
        _trim_embed(e)
        size = _embed_chars(e)
        if current and (current_len + size > limit
                        or len(current) >= max_count):
            batches.append(current)
            current, current_len = [], 0
        current.append(e)
        current_len += size
    if current:
        batches.append(current)
    return batches


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
    # Требование проекта: любой Embed, уходящий через safe_send, — чёрный.
    # Это же страхует embed'ы из quantum/agent.py и других модулей.
    emb = kwargs.get("embed")
    if isinstance(emb, discord.Embed):
        emb.color = 0x000000
    embs = kwargs.get("embeds")
    if embs:
        for e in embs:
            if isinstance(e, discord.Embed):
                e.color = 0x000000
        embs = list(embs)
        # Discord лимит: сумма текста всех embed'ов ≤ 6000 на сообщение.
        # Если не влезает — отправляем несколькими сообщениями.
        total = sum(_embed_chars(e) for e in embs)
        if total > EMBED_MSG_LIMIT or len(embs) > EMBED_MSG_MAX_COUNT:
            batches = _batch_embeds(embs)
            for idx, batch in enumerate(batches):
                k = dict(kwargs)
                k["embeds"] = batch
                # Текст и кнопки — только в первом сообщении.
                if idx > 0:
                    k.pop("content", None)
                    k.pop("view", None)
                await _safe_send_inner(target, retries, *args, **k)
            return None
    last_exc = None
    for attempt in range(retries):
        try:
            return await target.send(*args, **kwargs)
        except discord.HTTPException as e:
            last_exc = e
            # 50035 «Embed size exceeds maximum size of 6000»: подстраховка —
            # отправляем embed'ы по одному, чтобы сообщение точно прошло.
            if (e.status == 400 and getattr(e, "code", None) == 50035
                    and kwargs.get("embeds") and len(kwargs["embeds"]) > 1):
                for idx, one in enumerate(list(kwargs["embeds"])):
                    k = dict(kwargs)
                    k["embeds"] = [one]
                    if idx > 0:
                        k.pop("content", None)
                        k.pop("view", None)
                    await _safe_send_inner(target, retries, *args, **k)
                return None
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


# Тяжёлые операции (обход всех игроков через cs2red) не должны выполняться
# параллельно: два модератора одновременно запускали /check_all и удваивали
# нагрузку на сайт. Lock на имя операции решает это без блокировки event loop.
_heavy_locks: dict[str, asyncio.Lock] = {}


def _heavy_lock(name: str) -> asyncio.Lock:
    lock = _heavy_locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _heavy_locks[name] = lock
    return lock


def single_flight(name: str, busy_text: str):
    """Не даёт запустить одну и ту же тяжёлую команду дважды.

    Второй вызов получает ephemeral-сообщение «уже выполняется» и не
    создаёт лишних запросов к cs2red.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(interaction, *args, **kwargs):
            lock = _heavy_lock(name)
            if lock.locked():
                try:
                    if not interaction.response.is_done():
                        await interaction.response.defer(ephemeral=True)
                    await safe_send(interaction.followup, busy_text,
                                    ephemeral=True)
                except Exception:
                    pass
                return None
            async with lock:
                return await func(interaction, *args, **kwargs)
        return wrapper
    return decorator


# ============================================================
# ПАРСИНГ АКТИВНОСТИ cs2red (вкладка «Активность»/AFK)
# ============================================================
# Разметка админки cs2red может меняться, поэтому парсер не привязан к
# CSS-классам: он работает по текстовым меткам («Текущая неделя»,
# «Наблюдатель», «Рассмотрено репортов» и т.п.). Числа ищем рядом с
# метками в пределах небольшого окна текста.

# «26 ч.», «20 м.», «1 ч. 30 м.», «45 мин», «1.5 ч.», «26:30», «—».
_TIME_RE = re.compile(
    r"(?P<h>\d+(?:[.,]\d+)?)\s*(?:ч|час|h|hour)"
    r"(?:\s*(?P<m>\d+)\s*(?:м|мин))?"
    r"|(?P<m2>\d+)\s*(?:м|мин)"
)

_WEEK_LABELS = ("текущая неделя", "эта неделя", "за неделю", "current week")
_PREV_LABELS = ("прошлая неделя", "предыдущая неделя", "пред. неделя",
                "previous week", "last week")
_OBSERVER_LABELS = ("наблюдатель", "observer", "afk")
_TOTAL_LABELS = ("общее время", "всего времени", "время", "total")


def _normalize_ws(text: str) -> str:
    """Схлопывает все пробельные символы в один пробел."""
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_time_to_hours(token: str):
    """Превращает «1 ч. 30 м.», «20 м.», «1.5 ч.» или «26:30» в часы (float)."""
    if not token:
        return None
    token = token.strip()
    # «26:30» = 26 ч. 30 м.
    m = re.fullmatch(r"(\d+)\s*[:.]\s*(\d{1,2})", token)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60.0
    total = 0.0
    found = False
    for mm in _TIME_RE.finditer(token.lower()):
        if mm.group("h") is not None:
            total += float(mm.group("h").replace(",", "."))
            if mm.group("m"):
                total += int(mm.group("m")) / 60.0
            found = True
        elif mm.group("m2") is not None:
            total += int(mm.group("m2")) / 60.0
            found = True
    return total if found else None


def _find_label_block(text: str, labels: tuple, limit: int = 200) -> str:
    """Возвращает фрагмент текста сразу после первого вхождения метки.

    Фрагмент ограничен ``limit`` символами и **обрезается** на следующей
    метке недели («Прошлая неделя» и т.п.): иначе окно захватывает соседнюю
    неделю, и время наблюдателя суммировалось с общим временем следующей.
    """
    low = text.lower()
    best = -1
    for lab in labels:
        idx = low.find(lab)
        if idx != -1 and (best == -1 or idx < best):
            best = idx
    if best == -1:
        return ""
    chunk = text[best:best + limit]
    # Обрезаем на следующей метке недели, если она попала в окно.
    cut = len(chunk)
    for lab in _WEEK_LABELS + _PREV_LABELS:
        pos = chunk.lower().find(lab, 1)
        if pos != -1:
            cut = min(cut, pos)
    return chunk[:cut]


def _extract_number(text: str):
    """Первое целое число в тексте (для репортов)."""
    m = re.search(r"\d+", text or "")
    return int(m.group(0)) if m else None


def _extract_total_hours(block: str):
    """Часы «общего времени» из блока недели.

    Сначала пробуем взять значение сразу после метки «Общее время», затем —
    любое время в блоке, исключая «наблюдателя» (AFK-время идёт отдельно).
    """
    if not block:
        return None
    total_block = _find_label_block(block, _TOTAL_LABELS)
    if total_block:
        # Убираем часть с «наблюдателем», чтобы не поймать её время.
        cut = re.split(r"наблюдател|observer", total_block, flags=re.IGNORECASE)[0]
        val = _parse_time_to_hours(cut)
        if val is not None:
            return val
    cut = re.split(r"наблюдател|observer", block, flags=re.IGNORECASE)[0]
    return _parse_time_to_hours(cut)


def _extract_observer_hours(block: str):
    """Время «наблюдателя» (AFK) из блока недели."""
    if not block:
        return None
    obs = _find_label_block(block, _OBSERVER_LABELS)
    if not obs:
        return None
    return _parse_time_to_hours(obs)


def _row_cells(tr) -> list:
    """Тексты ячеек строки таблицы (th/td)."""
    return [_normalize_ws(td.get_text(" ")) for td in tr.find_all(["th", "td"])]


def _match_week_col(cells: list):
    """Определяет, есть ли в строке заголовки недель.

    Возвращает ``(week_idx, prev_idx, total_idx)`` — индексы колонок
    «текущая неделя», «прошлая неделя» и «всего» (любой может быть None),
    либо ``None``, если недельных заголовков в строке нет.
    """
    week_idx = prev_idx = total_idx = None
    for idx, cell in enumerate(cells):
        low = cell.lower()
        if any(lab in low for lab in _WEEK_LABELS):
            week_idx = idx
        elif any(lab in low for lab in _PREV_LABELS):
            prev_idx = idx
        elif "всего" in low or "total" in low:
            total_idx = idx
    if week_idx is None and prev_idx is None:
        return None
    return week_idx, prev_idx, total_idx


def _parse_activity_table(soup):
    """Парсит активность из HTML-таблиц (основной, надёжный путь).

    Сначала находим строку с заголовками «Текущая неделя»/«Прошлая неделя»
    и запоминаем индексы колонок. Затем в строках с метками («Общее время»,
    «Наблюдатель», «Репортов») берём значения из этих колонок.
    """
    result = {"week": {"total": None, "observer": None},
              "prev": {"total": None, "observer": None},
              "reports": {"week": None, "prev": None, "total": None}}
    found_any = False
    cols = None
    for tr in soup.find_all("tr"):
        cells = _row_cells(tr)
        if not cells:
            continue
        detected = _match_week_col(cells)
        # Заголовочная строка: в первой ячейке нет метрики (время/наблюдатель).
        if detected is not None and not any(
                lab in cells[0].lower()
                for lab in _TOTAL_LABELS + _OBSERVER_LABELS):
            cols = detected
            continue
        if cols is None:
            continue
        week_idx, prev_idx, total_idx = cols
        first = cells[0].lower()

        _cell = lambda idx, _c=cells: (  # noqa: E731
            _c[idx] if idx is not None and 0 <= idx < len(_c) else None)
        if any(lab in first for lab in _TOTAL_LABELS):
            w = _parse_time_to_hours(_cell(week_idx) or "")
            p = _parse_time_to_hours(_cell(prev_idx) or "")
            if w is not None:
                result["week"]["total"] = w
                found_any = True
            if p is not None:
                result["prev"]["total"] = p
                found_any = True
        elif any(lab in first for lab in _OBSERVER_LABELS):
            w = _parse_time_to_hours(_cell(week_idx) or "")
            p = _parse_time_to_hours(_cell(prev_idx) or "")
            if w is not None:
                result["week"]["observer"] = w
                found_any = True
            if p is not None:
                result["prev"]["observer"] = p
                found_any = True
        elif "репорт" in first or "report" in first:
            nw = _extract_number(_cell(week_idx) or "")
            np_ = _extract_number(_cell(prev_idx) or "")
            if nw is not None:
                result["reports"]["week"] = nw
                found_any = True
            if np_ is not None:
                result["reports"]["prev"] = np_
                found_any = True
            tv = _extract_number(_cell(total_idx) or "")
            if tv is not None:
                result["reports"]["total"] = tv
            elif nw is not None and np_ is not None:
                result["reports"]["total"] = nw + np_
            found_any = True
    return result if found_any else None


def parse_activity_html(html: str):
    """Парсит HTML страницы ``/admin/user/<id>`` в словарь активности.

    Сначала пробуем табличную раскладку (заголовки недель + строки метрик),
    затем — плоский текст (метки и значения идут подряд). Возвращает
    ``None``, если не удалось найти ни одного показателя.
    """
    if not html or not _HAS_BS4:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None

    # Убираем скрипты/стили: в них бывают числа, которые собьют парсер.
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    table_data = _parse_activity_table(soup)

    # Плоский текст — запасной путь для вёрстки без таблиц.
    text = _normalize_ws(soup.get_text(" "))
    week_block = _find_label_block(text, _WEEK_LABELS)
    prev_block = _find_label_block(text, _PREV_LABELS)
    flat = {
        "week": {"total": _extract_total_hours(week_block),
                 "observer": _extract_observer_hours(week_block)},
        "prev": {"total": _extract_total_hours(prev_block),
                 "observer": _extract_observer_hours(prev_block)},
        "reports": {"week": None, "prev": None, "total": None},
    }
    rep_idx = text.lower().find("репорт")
    if rep_idx == -1:
        rep_idx = text.lower().find("report")
    if rep_idx != -1:
        nums = [int(x) for x in re.findall(r"\d+", text[rep_idx:rep_idx + 120])]
        if len(nums) >= 3:
            flat["reports"]["week"], flat["reports"]["prev"], flat["reports"]["total"] = nums[:3]
        elif len(nums) == 2:
            flat["reports"]["week"], flat["reports"]["total"] = nums
        elif len(nums) == 1:
            flat["reports"]["total"] = nums[0]

    # Объединяем: табличные значения приоритетнее, плоские добивают пропуски.
    data = table_data or {"week": {"total": None, "observer": None},
                          "prev": {"total": None, "observer": None},
                          "reports": {"week": None, "prev": None, "total": None}}
    for section in ("week", "prev", "reports"):
        for key in data[section]:
            if data[section][key] is None:
                data[section][key] = flat[section][key]

    if all(data[s][k] is None for s in ("week", "prev", "reports")
           for k in data[s]):
        return None
    data["raw"] = text[:1500]
    return data


# ============================================================
# АКТИВНОСТЬ: расчёт из JSON админ-профиля cs2red
# ============================================================
# Раньше активность парсилась из HTML /admin/user/<id>, но это SPA:
# в HTML лежит только пустой каркас, а данные приходят отдельным
# запросом GET /api/admin/user?userId=<id>. Там же, в user.Activity,
# лежит готовая статистика по режимам, а в user.admin_reports — репорты.
ACTIVITY_DAYS = ("monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday", "sunday")
MSK_OFFSET = 3 * 3600          # Москва = UTC+3 (без перехода на летнее время)
DAY_SECONDS = 86400
WEEK_SECONDS = 7 * DAY_SECONDS


def _safe_int(value, default: int = 0) -> int:
    """int() с поддержкой строк/float из JSON (``"123.0"``, ``123.0``)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default


def msk_week_start(ts, weeks_ago: int = 0) -> int:
    """Начало недели по МСК (понедельник 00:00) — как считает cs2red.

    ``weeks_ago=0`` — текущая неделя, ``1`` — прошлая. Формула повторяет
    JS-код сайта: ``(day - (day + 3) % 7) * 86400 - weeks * 604800 - 10800``.
    """
    moment = _safe_int(ts, int(time.time()))
    shifted = moment + MSK_OFFSET
    day = shifted // DAY_SECONDS
    monday = (day - (day + 3) % 7) * DAY_SECONDS
    return int(monday - weeks_ago * WEEK_SECONDS - MSK_OFFSET)


def _seconds(value) -> int:
    """Неотрицательные секунды из поля JSON (битые/None -> 0)."""
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def parse_activity_json(user: dict, now: float | None = None) -> dict | None:
    """Считает недельную активность из JSON ``/api/admin/user``.

    Структура ответа (проверено по JS-бандлу админки):

    * ``user.Activity`` — массив по режимам; в каждом ``gamemodeId``,
      ``monday..sunday`` и ``last_monday..last_sunday`` — секунды;
      ``spectator``/``last_spectator`` — тоже секунды;
    * ``user.admin_reports`` — рассмотренные репорты, время записи —
      ``timestamp_result`` или ``timestamp``.

    Возвращает словарь с часами в ``week``/``prev``, количеством репортов
    и разбивкой по режимам, либо ``None``, если данных активности нет.
    """
    if not isinstance(user, dict):
        return None
    raw_activity = user.get("Activity")
    if raw_activity is None:
        raw_activity = user.get("activity")
    if not isinstance(raw_activity, list):
        return None

    now = time.time() if now is None else float(now)
    week_start = msk_week_start(now, 0)
    prev_start = msk_week_start(now, 1)

    week_seconds = prev_seconds = 0
    observer_seconds = prev_observer_seconds = 0
    modes = []

    for item in raw_activity:
        if not isinstance(item, dict):
            continue
        cur = sum(_seconds(item.get(day)) for day in ACTIVITY_DAYS)
        prev = sum(_seconds(item.get("last_" + day)) for day in ACTIVITY_DAYS)
        cur_obs = _seconds(item.get("spectator"))
        prev_obs = _seconds(item.get("last_spectator"))
        week_seconds += cur
        prev_seconds += prev
        observer_seconds += cur_obs
        prev_observer_seconds += prev_obs

        # Нулевые режимы в разбивку не тащим — иначе поле забьётся пустотой.
        if any((cur, prev, cur_obs, prev_obs)):
            modes.append({
                "gamemode_id": _safe_int(item.get("gamemodeId")),
                "week": cur / 3600,
                "prev": prev / 3600,
                "observer": cur_obs / 3600,
                "prev_observer": prev_obs / 3600,
                "seconds": cur,
            })
    modes.sort(key=lambda m: m["week"], reverse=True)

    raw_reports = user.get("admin_reports")
    if not isinstance(raw_reports, list):
        raw_reports = []
    rep_week = rep_prev = 0
    for rec in raw_reports:
        if not isinstance(rec, dict):
            continue
        ts = _safe_int(rec.get("timestamp_result") or rec.get("timestamp"))
        if not ts:
            continue
        if ts >= week_start:
            rep_week += 1
        elif ts >= prev_start:
            rep_prev += 1

    return {
        "week": {"total": week_seconds / 3600,
                 "observer": observer_seconds / 3600,
                 "seconds": week_seconds},
        "prev": {"total": prev_seconds / 3600,
                 "observer": prev_observer_seconds / 3600,
                 "seconds": prev_seconds},
        "reports": {"week": rep_week, "prev": rep_prev,
                    "total": len(raw_reports)},
        "modes": modes,
        "week_start": week_start,
        "prev_start": prev_start,
    }


# ============================================================
# API: cs2red / Steam
# ============================================================
class API:
    # Интервал между запросами к cs2red (анти-DDoS Cloudflare).
    RATE_LIMIT_SECONDS = CS2RED_RATE_LIMIT_SECONDS
    # При 5xx: максимальное число повторов подряд.
    RETRY_MAX = 3
    # При 5xx ждём между повторами.
    RETRY_5xx_WAIT = 10
    # Сколько максимум держать профили в кэше (защита от роста памяти).
    USER_CACHE_MAX = 512

    def __init__(self):
        # Публичная сессия: Steam / GitHub / LLM — БЕЗ cookies и заголовков cs2red.
        self.session: aiohttp.ClientSession | None = None
        # Приватная сессия: только cs2red.ru (админские cookie/заголовки).
        self.cs2red: aiohttp.ClientSession | None = None
        self.sem = asyncio.Semaphore(4)
        self._clans_cache = {"ts": 0.0, "data": None}
        # Список ролей сайта: roleId -> название (кэш на час).
        self._roles_cache = {"ts": 0.0, "data": {}}
        # Названия режимов (gamemodeId) для разбивки активности.
        self._categories_cache = {"ts": 0.0, "data": None}
        self.last_status: int | None = None
        # Анти-DDoS: последовательный крановой протокол к cs2red.
        self._rate_lock = asyncio.Lock()
        self._last_cs2red_ts = 0.0
        # Причина блокировки, если сработала защита (429/403/5xx исчерпаны).
        self.blocked_reason = ""
        # Предохранитель: до какого времени cs2red не трогаем. Раньше при
        # 429 команда «висела» 5 минут; теперь отвечает сразу, а сайт
        # получает паузу в фоне.
        self._blocked_until = 0.0
        self._block_strikes = 0
        # Кэш профилей: steam_id -> (ts, user).
        self._user_cache: dict[str, tuple[float, dict]] = {}

    # ---------- предохранитель и кэш ----------
    def is_blocked(self) -> bool:
        return time.time() < self._blocked_until

    def block_reason_text(self) -> str:
        """Человекочитаемая причина паузы и сколько ждать."""
        if not self.is_blocked():
            return ""
        left = max(0, int(self._blocked_until - time.time()))
        reason = self.blocked_reason or "временная блокировка"
        return f"{reason} (пауза ещё {left} сек)"

    def _block(self, reason: str) -> None:
        """Ставит cs2red на паузу с экспоненциальным ростом."""
        self._block_strikes = min(self._block_strikes + 1, 8)
        delay = min(CS2RED_BLOCK_BASE_SECONDS * (2 ** (self._block_strikes - 1)),
                    CS2RED_BLOCK_MAX_SECONDS)
        self._blocked_until = time.time() + delay
        self.blocked_reason = reason
        log(f"⛔ cs2red на паузе {delay} сек (причина: {reason}, "
            f"подряд: {self._block_strikes})")

    def reset_block(self) -> None:
        """Снимает паузу после успешного ответа или ручного сброса."""
        self._blocked_until = 0.0
        self._block_strikes = 0
        self.blocked_reason = ""

    def _cache_get(self, steam_id):
        key = str(steam_id)
        item = self._user_cache.get(key)
        if not item:
            return None
        ts, user = item
        if time.time() - ts > CS2RED_USER_CACHE_TTL:
            self._user_cache.pop(key, None)
            return None
        return user

    def _cache_put(self, steam_id, user) -> None:
        if not isinstance(user, dict):
            return
        if len(self._user_cache) >= self.USER_CACHE_MAX:
            # Простое вытеснение: удаляем самый старый по времени записи.
            oldest = min(self._user_cache.items(),
                         key=lambda kv: kv[1][0])[0]
            self._user_cache.pop(oldest, None)
        self._user_cache[str(steam_id)] = (time.time(), user)

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
                # Небольшой джиттер: несколько воркеров не бьют залпом.
                await asyncio.sleep(wait + random.uniform(0, 0.25))
            self._last_cs2red_ts = time.time()
            return

    async def _get_json(self, url, params=None):
        session = self.session_for(url)
        if not session:
            return None
        is_cs2red = _is_cs2red_host(url)
        # Предохранитель: после 429/403 не ждём 5 минут внутри команды,
        # а сразу говорим «сайт на паузе». Это и быстрее для Discord, и
        # безопаснее для cs2red.
        if is_cs2red and self.is_blocked():
            return None

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
                            if is_cs2red:
                                self._block("403")
                            log(f"🔴 403 на {url} — авторизация на cs2red. Проверьте куки.")
                            return None

                        # 429 — Cloudflare/Discord прижал: пауза, без зависания.
                        if r.status == 429:
                            if is_cs2red:
                                self._block("429")
                            log(f"🐢 429 на {url} — cs2red временно блокирует "
                                f"запросы. Пауза без ожидания в команде.")
                            return None

                        # 5xx — временный сбой сервера: ретраи с паузой, до RETRY_MAX.
                        if r.status >= 500:
                            consecutive_5xx += 1
                            log(f"⚠️ {r.status} на {url} ({attempt + 1}-я попытка из "
                                f"{self.RETRY_MAX + 1})")
                            if consecutive_5xx > self.RETRY_MAX:
                                if is_cs2red:
                                    self._block(str(r.status))
                                return None
                            await asyncio.sleep(self.RETRY_5xx_WAIT)
                            continue

                        consecutive_5xx = 0
                        r.raise_for_status()
                        # Успешный ответ cs2red снимает прежнюю блокировку:
                        # иначе после одного 403/429 фоновые задачи навсегда
                        # считали сайт недоступным и прерывались.
                        if is_cs2red:
                            self.reset_block()
                        return await r.json(content_type=None)
            except NET_ERRORS as e:
                log(f"⚠️ GET {url}: сетевой сбой: {e}")
                if attempt < self.RETRY_MAX:
                    await asyncio.sleep(self.RETRY_5xx_WAIT)
                    continue
                if is_cs2red:
                    self._block("network")
                return None
            except Exception as e:
                log(f"⚠️ GET {url}: {e}")
                return None
        return None

    async def _get_html(self, url, params=None):
        """GET HTML-страницы cs2red с теми же защитами, что и ``_get_json``.

        Возвращает ``(html | None, error)``: при успехе ``error == ""``,
        иначе — человекочитаемая причина («403», «429», «сеть», «5xx»).
        Отдельный метод нужен, потому что страницы админки отдают HTML,
        а ``_get_json`` пытается разобрать ответ как JSON.
        """
        session = self.session_for(url)
        if not session:
            return None, "нет сессии"
        is_cs2red = _is_cs2red_host(url)
        if is_cs2red and self.is_blocked():
            return None, self.block_reason_text()
        consecutive_5xx = 0
        for attempt in range(self.RETRY_MAX + 1):
            try:
                if is_cs2red:
                    await self._acquire_rate_slot()
                async with self.sem:
                    async with session.get(url, params=params) as r:
                        self.last_status = r.status
                        if r.status == 403:
                            self._block("403")
                            log(f"🔴 403 на {url} — авторизация на cs2red. "
                                f"Проверьте куки.")
                            return None, "403 (нужны актуальные cookie cs2red)"
                        if r.status == 429:
                            self._block("429")
                            log(f"🐢 429 на {url} — cs2red временно блокирует "
                                f"запросы. Пауза без ожидания в команде.")
                            return None, "429 (cs2red временно блокирует запросы)"
                        if r.status >= 500:
                            consecutive_5xx += 1
                            log(f"⚠️ {r.status} на {url} "
                                f"({attempt + 1}-я попытка из {self.RETRY_MAX + 1})")
                            if consecutive_5xx > self.RETRY_MAX:
                                self._block(str(r.status))
                                return None, f"{r.status} (сервер cs2red не отвечает)"
                            await asyncio.sleep(self.RETRY_5xx_WAIT)
                            continue
                        consecutive_5xx = 0
                        r.raise_for_status()
                        if is_cs2red:
                            self.reset_block()
                        # Кодировку не форсируем: aiohttp берёт её из
                        # Content-Type/мета-тега, а у cs2red это UTF-8.
                        html = await r.text()
                        return html, ""
            except NET_ERRORS as e:
                log(f"⚠️ GET {url}: сетевой сбой: {e}")
                if attempt < self.RETRY_MAX:
                    await asyncio.sleep(self.RETRY_5xx_WAIT)
                    continue
                if is_cs2red:
                    self._block("network")
                return None, "сеть"
            except Exception as e:
                log(f"⚠️ GET {url}: {e}")
                return None, str(e)[:120]
        return None, "не удалось получить страницу"

    async def cs2red_internal_id(self, steam_id: str):
        """Внутренний ID игрока в админке cs2red (например, 8081).

        Шаг 1 — быстрый путь через API: ``/api/admin/users?search=`` отдаёт
        ``id``. Если API не ответил или не нашёл игрока, пробуем HTML-поиск
        ``/admin?search=<steam_id>`` и вытаскиваем ``/admin/user/<id>``.
        Возвращает ``(internal_id | None, user | None, error)``.
        """
        user = await self.cs2red_user(steam_id)
        if user is not None and not user.get("_not_found") and user.get("id"):
            try:
                return int(user["id"]), user, ""
            except (TypeError, ValueError):
                pass
        if user is not None and user.get("_not_found"):
            return None, None, "не найден"
        if user is None:
            # API не ответил — пробуем HTML-поиск.
            html, err = await self._get_html(
                "https://cs2red.ru/admin", params={"search": steam_id})
            if not html:
                return None, None, err or "поиск не удался"
            uid = self._extract_internal_id(html)
            if uid:
                return uid, None, ""
            return None, None, "не найден"
        return None, None, "не найден"

    @staticmethod
    def _extract_internal_id(html: str):
        """Достаёт внутренний ID из HTML админки (ссылка ``/admin/user/<id>``)."""
        if not html:
            return None
        m = re.search(r"/admin/user/(\d+)", html)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
        return None

    async def cs2red_user(self, steam_id: str):
        # Кэш на минуту: /check_all, /check_onlineall и /clan_info часто
        # запрашивают одних и тех же игроков подряд.
        cached = self._cache_get(steam_id)
        if cached is not None:
            return cached
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
        # Если API вернул несколько совпадений, выбираем именно этот SteamID.
        # Когда steamid64 в ответе нет, остаётся прежнее поведение (первый).
        for u in users:
            if self.extract_steam64(u) == str(steam_id):
                self._cache_put(steam_id, u)
                return u
        self._cache_put(steam_id, users[0])
        return users[0]

    async def cs2red_roles(self) -> dict:
        """Названия ролей сайта: roleId → «Куратор», «Модератор», ...

        Сайт берёт этот список из /api/data/main — оттуда же берём и мы,
        чтобы название Discord-роли совпадало с тем, что видит игрок.
        """
        ts = asyncio.get_event_loop().time()
        if self._roles_cache["data"] and ts - self._roles_cache["ts"] < 3600:
            return self._roles_cache["data"]
        data = await self._get_json("https://cs2red.ru/api/data/main")
        roles = None
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict):
                roles = inner.get("roles")
            roles = roles or data.get("roles")
        out: dict = {}
        if isinstance(roles, list):
            for r in roles:
                if not isinstance(r, dict):
                    continue
                name = (r.get("name") or r.get("title")
                        or r.get("label") or r.get("role"))
                if isinstance(name, dict):
                    name = name.get("name") or name.get("title")
                rid = _role_id_int(r.get("id"))
                name = str(name or "").strip()
                if rid is not None and name:
                    out[rid] = name
        if out:
            self._roles_cache = {"ts": ts, "data": out}
        return out or self._roles_cache["data"] or {}

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

    async def cs2red_full_user(self, steam_id: str, user: dict | None = None):
        cache_key = f"full:{steam_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        user = user if user is not None else await self.cs2red_user(steam_id)
        if not user or user.get("_not_found") or not user.get("id"):
            return None
        data = await self._get_json(
            "https://cs2red.ru/api/admin/user",
            params={"userId": user["id"]})
        if data and data.get("success"):
            full = data.get("user")
            self._cache_put(cache_key, full)
            return full
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

    async def cs2red_categories(self) -> dict:
        """Названия режимов ``gamemodeId -> имя`` для разбивки активности.

        Справочник меняется редко, поэтому кэшируем на час. Если запрос
        не удался, возвращаем то, что уже было (или пустой словарь).
        """
        ts = asyncio.get_event_loop().time()
        cached = self._categories_cache
        if (cached["data"] is not None
                and ts - cached["ts"] < 3600):
            return cached["data"]
        data = await self._get_json("https://cs2red.ru/api/data/servers")
        if not isinstance(data, dict):
            return cached["data"] or {}
        names = {}
        for cat in (data.get("categories") or []):
            if not isinstance(cat, dict):
                continue
            cid = self._as_int(cat.get("id"))
            name = (cat.get("categoryName") or cat.get("categoryShort")
                    or cat.get("slug") or "")
            if cid and name:
                names[cid] = str(name).strip()
        self._categories_cache = {"ts": ts, "data": names}
        return names

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

    async def player_clan_checked(self, steam_id: str, user: dict | None = None,
                                  clans: list | None = None) -> tuple:
        """Как player_clan, но отличает «не в клане» от «не удалось узнать».

        Возвращает ``(clan | None, error)``: ``error == ""`` — данные получены
        (clan None значит «игрок не в клане»), иначе текст причины.
        """
        if user is None:
            user = await self.cs2red_user(steam_id)
            if user is None:
                return None, "cs2red не ответил"
        if user.get("_not_found") or not user.get("id"):
            return None, ""
        if clans is None:
            clans = await self.cs2red_clans()
            if clans is None:
                return None, "/api/clans не ответил"
        uid = int(user["id"])
        for clan in clans:
            for m in (clan.get("members") or []):
                if int(m.get("userId", 0) or 0) == uid:
                    return {"clan_id": clan.get("id"), "clan_name": clan.get("name"),
                            "clan_tag": clan.get("tag"), "member_role": m.get("roleId")}, ""
        return None, ""


    # ---------- блокировки (баны/муты) ----------
    @staticmethod
    def _as_int(value, default: int = 0) -> int:
        return _safe_int(value, default)

    @classmethod
    def normalize_block(cls, raw: dict, kind: str = "Бан",
                        from_admin: bool = False,
                        player: dict | None = None) -> dict:
        """Приводит запись бана/мута cs2red к единому виду.

        Публичный API отдаёт заблокированного игрока в ``action``, а того,
        кто выдал блокировку, — в ``user`` (у автоматических банов ``user``
        пустой).

        В админ-профиле (``/api/admin/user``) забаненный игрок может не
        дублироваться в записи — он известен из самого профиля. Такой
        объект передаётся в ``player``. Если же ``action`` есть, он
        приоритетнее.

        ``player`` — подсказка ``{"id", "nick", "steamid64"}`` профиля.
        """
        raw = raw if isinstance(raw, dict) else {}
        raw_player = raw.get("action") if isinstance(raw.get("action"), dict) else {}
        if not raw_player and isinstance(raw.get("player"), dict):
            raw_player = raw["player"]
        known_player = player if isinstance(player, dict) else {}
        player_info = raw_player or known_player

        admin = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        if not admin and isinstance(raw.get("admin"), dict):
            admin = raw["admin"]

        # Защита от перепутанных полей: если «user» — это сам забаненный,
        # значит выдавшего в записи нет и показывать игрока как админа нельзя.
        if (admin and player_info
                and cls._as_int(admin.get("id")) > 0
                and cls._as_int(admin.get("id")) == cls._as_int(player_info.get("id"))):
            admin = {}

        start_ts = cls._as_int(raw.get("timestamp") or raw.get("time") or raw.get("date"))
        end_ts = cls._as_int(raw.get("endTimeStamp") or raw.get("endTime")
                             or raw.get("until") or raw.get("end"))
        if 0 < end_ts < 1_000_000_000:
            end_ts = 0  # мусорное значение — считаем «навсегда»

        # Поле active может отсутствовать (публичный API) или быть null —
        # это не значит «блокировка снята». Снятой считаем только явный false.
        active_raw = raw.get("active")
        active = True if active_raw is None else bool(active_raw)
        if raw.get("unbanId") or raw.get("unmuteId"):
            active = False
        if active and end_ts and end_ts <= time.time():
            active = False

        reason = str(raw.get("reason") or raw.get("comment") or "—").strip() or "—"
        admin_id = cls._as_int(raw.get("adminId") or admin.get("id"))
        admin_nick = str(admin.get("nick") or raw.get("adminNick") or "").strip()
        if not admin_nick:
            if admin_id <= 0:
                admin_nick = "Античит" if "RAC" in reason.upper() else "Система"
            else:
                admin_nick = f"ID {admin_id}"

        steam = str(player_info.get("steamid64") or raw.get("steamid64")
                    or raw.get("steam_id") or known_player.get("steamid64")
                    or "").strip()
        nick = str(player_info.get("nick") or raw.get("nick")
                   or known_player.get("nick") or "—").strip() or "—"
        return {
            "id": raw.get("id") or raw.get("actionId"),
            "kind": "Мут" if kind.upper().startswith("МУТ") else "Бан",
            "active": active,
            "player_nick": nick,
            "steam_id": steam if steam.isdigit() else "",
            "player_id": cls._as_int(player_info.get("id")),
            "reason": reason,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "admin": admin_nick,
            "from_admin": from_admin,
        }

    async def _public_blocks(self, steam_id: str, category: str,
                             uid: int = 0) -> tuple[list, str]:
        """Запасной путь: публичный список cs2red с локальной фильтрацией."""
        is_mute = (category or "").upper().startswith("MUTE")
        kind = "Мут" if is_mute else "Бан"
        key = "mutes" if is_mute else "bans"
        data = await self._get_json(
            f"https://cs2red.ru/api/data/{key}",
            params={"begin": 0, "count": 20, "search": steam_id},
        )
        if data is None:
            return [], "unavailable"
        blocks = []
        for raw in (data.get("bans") or []):
            item = self.normalize_block(raw, kind)
            # Чужого игрока в список не пускаем (поиск может игнорироваться).
            if item["steam_id"] and item["steam_id"] != steam_id:
                continue
            if not item["steam_id"] and (not uid or item["player_id"] != uid):
                continue
            blocks.append(item)
        return blocks, ""

    async def cs2red_all_blocks(self, steam_id: str) -> tuple[list, list, str]:
        """Баны и муты игрока одним заходом.

        Админ-профиль cs2red отдаёт оба массива сразу, поэтому хватает
        двух запросов (поиск пользователя + его профиль).

        Возвращает ``(bans, mutes, error)``; ``error`` — "" или
        "unavailable" / "not_found".
        """
        user = await self.cs2red_user(steam_id)
        if user is None:
            return [], [], "unavailable"
        if user.get("_not_found"):
            return [], [], "not_found"
        uid = self._as_int(user.get("id"))
        player_hint = {"id": uid, "steamid64": steam_id,
                       "nick": user.get("nick")}

        bans: list = []
        mutes: list = []
        got = {"bans": False, "mutes": False}

        full = await self.cs2red_full_user(steam_id, user=user)
        admin_profile = isinstance(full, dict)
        if admin_profile:
            for key, kind, target in (("bans", "Бан", bans), ("mutes", "Мут", mutes)):
                source = full.get(key)
                if isinstance(source, list):
                    target.extend(
                        self.normalize_block(x, kind, from_admin=True,
                                             player=player_hint) for x in source)
                    got[key] = True

        # В админ-профиле может не быть одного из массивов — тогда идём
        # в публичный список. Важно: пустой ответ публичного поиска НЕ
        # доказывает отсутствие блокировок (без сессии фильтр может
        # игнорироваться), поэтому в этом случае честно сообщаем об ошибке.
        if not got["bans"]:
            bans, err1 = await self._public_blocks(steam_id, "BANS", uid)
            if err1:
                return [], [], err1
        if not got["mutes"]:
            mutes, err2 = await self._public_blocks(steam_id, "MUTES", uid)
            if err2:
                return [], [], err2

        if not admin_profile and not bans and not mutes:
            return [], [], "unavailable"

        for blocks in (bans, mutes):
            blocks.sort(key=lambda b: b.get("start_ts") or 0, reverse=True)
        return bans, mutes, ""

    async def cs2red_blocks(self, steam_id: str, category: str) -> tuple[list, str]:
        """Баны или муты игрока.

        ``category`` — "BANS" или "MUTES".
        Возвращает ``(blocks, error)``: ``error == ""`` — всё хорошо,
        ``"unavailable"`` — cs2red недоступен (403/429/сеть), ``"not_found"`` —
        такого игрока нет на cs2red.
        """
        is_mute = (category or "BANS").upper().startswith("MUTE")
        bans, mutes, err = await self.cs2red_all_blocks(steam_id)
        if err:
            return [], err
        return (mutes if is_mute else bans), ""

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

    async def cs2red_activity(self, steam_id: str):
        """Активность игрока из JSON админ-профиля cs2red.

        Страница ``/admin/user/<id>`` — SPA: в HTML данных нет, всё лежит
        в ``GET /api/admin/user?userId=<id>``. Берём оттуда ``user.Activity``
        и ``user.admin_reports`` (см. :func:`parse_activity_json`).

        Возвращает ``(data | None, error)``. ``data`` — словарь вида::

            {
              "nick": "Nick",
              "internal_id": 8081,
              "week": {"total": 26.0, "observer": 0.33},   # часы
              "prev": {"total": 12.0, "observer": 0.0},
              "reports": {"week": 3, "prev": 1, "total": 9},
              "modes": [{"gamemode_id": 1, "name": "PUBLIC", ...}],
            }

        ``error``: ``""`` при успехе, иначе «не найден», «недоступен
        профиль» и т.п.
        """
        user = await self.cs2red_user(steam_id)
        if user is None:
            return None, "cs2red не ответил"
        if user.get("_not_found") or not user.get("id"):
            return None, "не найден"

        full = await self.cs2red_full_user(steam_id, user=user)
        if not isinstance(full, dict):
            return None, "нет доступа к профилю (проверьте cookie cs2red)"

        data = parse_activity_json(full)
        if data is None:
            return None, "нет данных активности"

        data["internal_id"] = self._as_int(user.get("id"))
        data["nick"] = user.get("nick")

        # Названия режимов — необязательное украшение: без них отчёт
        # всё равно корректен, поэтому ошибку справочника игнорируем.
        try:
            names = await self.cs2red_categories()
        except Exception:
            names = {}
        for mode in data.get("modes") or []:
            gid = mode.get("gamemode_id") or 0
            mode["name"] = names.get(gid) or (f"Режим {gid}" if gid else "Режим")
        return data, ""

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
async def is_moderator(user_id: int, guild: discord.Guild | None) -> bool:
    if await moderation_repo.is_moderator(user_id):
        return True
    if guild is None:
        # В личных сообщениях участника сервера нет — прав администратора тоже.
        return False
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
        profile["cs2red_role_name"] = (
            await resolve_site_role_name(user.get("roleId")) or "Игрок")
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
    embed = discord.Embed(title="👤 Профиль игрока", color=0x000000, timestamp=now_utc())

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


def _audit_identity(obj):
    """Возвращает (id, имя) для Discord-объекта, id-строки или None."""
    if obj is None:
        return None, ""
    if isinstance(obj, (int, str)):
        return (str(obj) or None), ""
    uid = getattr(obj, "id", None)
    name = (getattr(obj, "display_name", None)
            or getattr(obj, "name", None)
            or (str(uid) if uid else ""))
    return (str(uid) if uid else None), str(name or "")


def _build_audit_embed(action, actor_id, target_id, target_name, details):
    """Собирает чёрный embed аудита (без отправки)."""
    embed = discord.Embed(title=f"🛡 {action}", color=0x000000,
                          timestamp=now_utc())
    if actor_id:
        embed.add_field(name="Кто", value=f"<@{actor_id}>\n`{actor_id}`",
                        inline=True)
    else:
        embed.add_field(name="Кто", value="система", inline=True)
    if target_id or target_name:
        value = (f"<@{target_id}>\n`{target_id}`" if target_id
                 else (target_name or "—"))
        embed.add_field(name="Объект", value=value[:1024], inline=True)
    if details:
        embed.add_field(name="Детали", value=details[:1024], inline=False)
    embed.set_footer(text=f"Quantum • {utc_stamp()} (UTC)")
    return embed


async def _send_audit_embed(bot, embed) -> None:
    """Отправляет embed аудита. Ограничен по времени, ошибки не летят наружу."""
    try:
        channel = bot.get_channel(AUDIT_LOG_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(AUDIT_LOG_CHANNEL_ID)
        if channel is None:
            return
        await asyncio.wait_for(safe_send(channel, embed=embed), timeout=10)
    except Exception as e:
        log(f"⚠️ audit channel: {type(e).__name__}: {e}")


async def audit_action(bot, action: str, *, actor=None, target=None,
                       details: str = "", notify: bool = True):
    """Пишет действие в ``audit_log`` и дублирует его в канал аудита.

    Возвращает Task отправки embed'а (или None). Запись в БД ждём (это
    быстро и важно), а отправку в Discord делаем в фоне: аудит не должен
    задерживать ответ на команду, даже если канал временно недоступен.
    Никогда не бросает исключение.
    """
    actor_id, actor_name = _audit_identity(actor)
    target_id, target_name = _audit_identity(target)
    try:
        await audit_repo.add_entry(
            actor_id=actor_id, actor_name=actor_name, action=action,
            target_id=target_id, target_name=target_name, details=details,
        )
    except Exception as e:
        log(f"⚠️ audit db: {type(e).__name__}: {e}")

    if not notify or not AUDIT_LOG_CHANNEL_ID:
        return None
    embed = _build_audit_embed(action, actor_id, target_id, target_name,
                               details)
    try:
        return asyncio.create_task(_send_audit_embed(bot, embed))
    except RuntimeError:
        # Нет работающего event loop (например, синхронный вызов) —
        # отправляем сразу, если это вообще возможно.
        await _send_audit_embed(bot, embed)
        return None


def utc_stamp() -> str:
    return now_utc().strftime("%d.%m.%Y %H:%M")


# ============================================================
# СИНХРОНИЗАЦИЯ РОЛЕЙ
# ============================================================
async def resolve_site_role_name(role_id) -> str | None:
    """Название роли с сайта: сначала список cs2red, потом локальная карта."""
    rid = _role_id_int(role_id)
    if rid is None or rid <= 0:
        return None
    names = await api.cs2red_roles()
    if rid in names:
        return names[rid]
    return site_role_name(rid)


_WARNED_SITE_ROLE_IDS: set = set()


async def _ensure_site_role(guild, name: str, reason: str = "роль с сайта cs2red"):
    """Возвращает Discord-роль с названием роли сайта, создавая при необходимости."""
    role = discord.utils.get(guild.roles, name=name)
    if role is not None:
        return role
    me = getattr(guild, "me", None)
    perms = getattr(me, "guild_permissions", None)
    if perms is not None and not getattr(perms, "manage_roles", False):
        return None
    try:
        role = await guild.create_role(
            name=name,
            color=discord.Colour(0x2F3136),
            hoist=False,
            mentionable=False,
            reason=reason,
        )
    except discord.Forbidden:
        log(f"⚠️ не создал роль «{name}»: нет права Manage Roles")
        return None
    except discord.HTTPException as e:
        log(f"⚠️ не создал роль «{name}»: {e}")
        return None
    _remember_site_role_name(name)
    log(f"➕ Создана Discord-роль с сайта: «{name}»")
    return role


async def sync_role_by_id(guild, member, steam_id, user: dict | None = None):
    if user is None:
        user = await api.cs2red_user(steam_id)
    if user is None:
        return f"⚠️ {member.display_name}: cs2red не ответил, роль не тронута"
    if user.get("_not_found"):
        # Пустой ответ поиска может быть сбоем API: не снимаем роли вслепую.
        return f"⚠️ {member.display_name}: не найден на cs2red, роль не тронута"
    if not user.get("id"):
        return f"⚠️ {member.display_name}: некорректный ответ cs2red, роль не тронута"
    role_id = user.get("roleId")
    tn = await resolve_site_role_name(role_id)
    managed = {n for n in ROLE_MAP.values() if n} | _load_site_role_names()
    cur = {r for r in member.roles if r.name in managed}

    if tn is None:
        rid = _role_id_int(role_id)
        if rid and rid not in _WARNED_SITE_ROLE_IDS:
            _WARNED_SITE_ROLE_IDS.add(rid)
            log(f"⚠️ roleId={rid} нет в списке ролей cs2red — добавьте в "
                f'SITE_ROLE_NAMES, например {{"{rid}": "Куратор"}}')
        if cur:
            for r in cur:
                try:
                    await member.remove_roles(r)
                except discord.Forbidden:
                    return f"❌ {member.display_name}: нет прав снять роль"
            return f"🔄 {member.display_name}: сняты"
        return f"ℹ️ {member.display_name}: без изменений"

    target = discord.utils.get(guild.roles, name=tn)
    if target is None:
        # Роли на сервере ещё нет — создаём её сами: имя берём с сайта.
        target = await _ensure_site_role(guild, tn)
    if target is None:
        return f"⚠️ роль '{tn}' не найдена и создать не удалось"

    if cur == {target}:
        return f"✅ {member.display_name}: уже '{tn}'"

    for r in cur:
        if r != target:
            try:
                await member.remove_roles(r)
            except discord.Forbidden:
                return f"❌ {member.display_name}: нет прав снять роль"
    try:
        await member.add_roles(target)
        return f"✅ {member.display_name}: '{tn}'"
    except discord.Forbidden:
        return "❌ нет прав"


CLAN_ROLE_COLOR = 0x8E7CF3  # приятный фиолетовый — Discord не умеет градиенты через бота

# Имена ролей, которые бот когда-либо создавал/выдавал. Нужны, чтобы снять
# роль клана, которого уже нет в /api/clans (клан распался, игрок вышел).
CLAN_ROLES_FILE = "clan_roles.json"
# Автоудаление ролей кланов без участников (1 — включено, 0 — выключено).
CLAN_ROLE_CLEANUP_ENABLED = env_int("CLAN_ROLE_CLEANUP", 1) != 0
# Период автоочистки пустых ролей кланов, часы (10 по умолчанию).
CLAN_ROLE_CLEANUP_HOURS = env_int("CLAN_ROLE_CLEANUP_HOURS", 10)


def _load_clan_role_names() -> set:
    try:
        p = data_path(CLAN_ROLES_FILE)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(x).strip() for x in data if str(x).strip()}
    except Exception as e:
        log(f"⚠️ не прочитал {CLAN_ROLES_FILE}: {e}")
    return set()


def _remember_clan_role_name(name: str):
    name = (name or "").strip()
    if not name:
        return
    names = _load_clan_role_names()
    if name in names:
        return
    names.add(name)
    try:
        data_path(CLAN_ROLES_FILE).write_text(
            json.dumps(sorted(names), ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:
        log(f"⚠️ не записал {CLAN_ROLES_FILE}: {e}")


def _known_clan_role_names(clans: list | None) -> set:
    names = {str(c.get("name")).strip() for c in (clans or []) if c.get("name")}
    names |= _load_clan_role_names()
    return names


# Кто какую роль клана получил: discord_id -> role_id. Нужно, чтобы снять
# роль даже если клан переименовали или он исчез из /api/clans.
CLAN_MEMBERS_FILE = "clan_members.json"


def _load_clan_members() -> dict:
    try:
        p = data_path(CLAN_MEMBERS_FILE)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items() if str(v).isdigit()}
    except Exception as e:
        log(f"⚠️ не прочитал {CLAN_MEMBERS_FILE}: {e}")
    return {}


def _save_clan_members(data: dict) -> None:
    try:
        data_path(CLAN_MEMBERS_FILE).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"⚠️ не записал {CLAN_MEMBERS_FILE}: {e}")


def _remember_member_clan_role(discord_id, role_id) -> None:
    data = _load_clan_members()
    if data.get(str(discord_id)) == int(role_id):
        return
    data[str(discord_id)] = int(role_id)
    _save_clan_members(data)


def _forget_member_clan_role(discord_id) -> None:
    data = _load_clan_members()
    if str(discord_id) not in data:
        return
    data.pop(str(discord_id))
    _save_clan_members(data)


def _clan_roles_above_bot(guild, roles) -> list:
    """Роли, которые бот не сможет снять/выдать: они не ниже его top_role."""
    me = guild.me
    if me is None or me.top_role is None:
        return list(roles)
    return [r for r in roles if r.position >= me.top_role.position]


async def _delete_clan_role_if_empty(guild, role, names=None,
                                     exclude_member_id=None) -> str | None:
    """Удаляет роль клана, если её больше никто не носит.

    Вызывается сразу после того, как игрок вышел из клана или ушёл
    с сервера, чтобы роль не висела до часовой уборки. Возвращает имя
    удалённой роли или None. Трогает только роли кланов (clan_roles.json
    и CLAN_ROLE_MAP), остальные — никогда.
    """
    if role is None or role not in getattr(guild, "roles", []):
        return None
    names = _load_clan_role_names() if names is None else set(names)
    is_clan = role.name in names or role.id in CLAN_ROLE_MAP.values()
    # Ушедший участник может ещё висеть в кэше роли — его не считаем.
    holders = [m for m in (getattr(role, "members", None) or [])
               if exclude_member_id is None or m.id != exclude_member_id]
    if not is_clan or holders:
        return None
    if _clan_roles_above_bot(guild, [role]):
        return None
    try:
        await role.delete(reason="Клан больше не представлен на сервере")
    except discord.Forbidden:
        log(f"⚠️ не удалил пустую роль клана «{role.name}»: нет права Manage Roles")
        return None
    except discord.HTTPException as e:
        log(f"⚠️ не удалил пустую роль клана «{role.name}»: {e}")
        return None
    log(f"🧹 Удалена роль клана без участников: «{role.name}»")
    return role.name


async def _remove_roles_checked(member, roles, reason: str = "Синхронизация клана") -> list:
    """Снимает роли и возвращает список ошибок (пустой — всё снято)."""
    errors = []
    for role in roles:
        try:
            await member.remove_roles(role, reason=reason)
        except discord.Forbidden:
            errors.append(f"нет прав снять «{role.name}»: роль бота должна быть выше неё")
        except discord.HTTPException as e:
            errors.append(f"не удалось снять «{role.name}»: {e}")
        except Exception as e:
            errors.append(f"не удалось снять «{role.name}»: {e}")
    return errors



async def _ensure_role_hoisted(role, reason: str = "роли кланов показываем отдельно") -> None:
    """Включает у роли «показывать участников отдельно» (hoist).

    Ошибки не пробрасываем: роль без hoist полезнее, чем упавшая
    синхронизация. Причина видна в логе.
    """
    if getattr(role, "hoist", False):
        return
    try:
        await role.edit(hoist=True, reason=reason)
    except discord.Forbidden:
        log(f"⚠️ роль «{role.name}»: нет прав включить показ отдельно")
    except discord.HTTPException as e:
        log(f"⚠️ роль «{role.name}»: не включил показ отдельно: {e}")


async def ensure_clan_roles_hoisted(guild, names=None) -> int:
    """Включает «показывать участников отдельно» у всех ролей кланов.

    Работает и на ролях, созданных до появления hoist: при следующей
    синхронизации они переключаются сами. Возвращает число изменённых.
    """
    names = _load_clan_role_names() if names is None else set(names)
    if not names:
        return 0
    me = getattr(guild, "me", None)
    top = (me.top_role.position
           if me is not None and getattr(me, "top_role", None) else -1)
    changed = 0
    for role in list(guild.roles):
        if role.name not in names or getattr(role, "hoist", False):
            continue
        if top >= 0 and role.position >= top:
            continue  # выше роли бота — редактировать нельзя
        await _ensure_role_hoisted(role)
        if getattr(role, "hoist", False):
            changed += 1
    return changed


async def cleanup_empty_clan_roles(guild, names=None) -> tuple[list, list]:
    """Удаляет роли кланов без участников — только те, что ведёт бот.

    Список известных имён хранится в ``clan_roles.json``: туда попадают
    роли, которые бот создал или нашёл по имени клана. Служебные роли
    (BAN, модераторы и любые другие) не удаляются никогда — их нет в списке.

    Возвращает (удалённые имена, ошибки).
    """
    deleted: list = []
    errors: list = []
    if not CLAN_ROLE_CLEANUP_ENABLED:
        return deleted, errors
    names = _load_clan_role_names() if names is None else set(names)
    if not names:
        return deleted, errors
    me = getattr(guild, "me", None)
    top = (me.top_role.position
           if me is not None and getattr(me, "top_role", None) else -1)
    for role in list(guild.roles):
        if role.name not in names:
            continue
        if role.members:
            continue  # у клана есть носители — роль остаётся
        if top >= 0 and role.position >= top:
            errors.append(f"«{role.name}»: роль бота ниже — удалить нельзя")
            continue
        try:
            await role.delete(reason="Клан больше не представлен на сервере")
        except discord.Forbidden:
            errors.append(f"«{role.name}»: нет права Manage Roles")
        except discord.HTTPException as e:
            errors.append(f"«{role.name}»: {e}")
        else:
            deleted.append(role.name)
            log(f"🧹 Удалена роль клана без участников: «{role.name}»")
    return deleted, errors


async def _find_or_create_clan_role(guild, member, clan_name: str, reason: str = "автосинхронизация"):
    """Находит роль по имени клана или создаёт её.

    Возвращает (role, status_text) — status_text непуст, если роль создана.
    """
    # 1) Повторно не создаём: роль по названию клана. Если роль была
    #    создана до включения hoist — включаем показ отдельно.
    target = discord.utils.get(guild.roles, name=clan_name)
    if target is not None:
        _remember_clan_role_name(clan_name)
        await _ensure_role_hoisted(target, reason)
        return target, None

    # 2) Проверяем права бота на управление ролями.
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return None, "❌ нет прав Manage Roles (бот не может создать роль клана)"
    me_top = me.top_role.position if me.top_role else 0
    if me_top < 2:
        return None, ("❌ роль бота слишком низко — поднимите её выше ролей клана, "
                      "иначе бот не сможет управлять ролью")

    # 3) Позиция: выше роли участника, но НИЖЕ роли бота — иначе бот
    #    не сможет потом снять эту роль (Forbidden).
    desired = (member.top_role.position + 1) if member.top_role else 1
    pos = max(1, min(desired, me_top - 1))

    try:
        target = await guild.create_role(
            name=clan_name,
            color=discord.Colour(CLAN_ROLE_COLOR),
            # hoist=True — участники клана видны отдельной группой в списке
            # участников (то самое «показывать отдельно»).
            hoist=True,
            mentionable=False,
            reason=reason,
        )
        # Ставим позицию выше роли участника (клип let Discord сортирует админ-роли).
        try:
            await target.edit(position=pos, reason=reason)
        except discord.Forbidden:
            pass
        _remember_clan_role_name(clan_name)
        return target, f"создана роль клана '{clan_name}'"
    except discord.Forbidden:
        return None, "❌ нет прав Manage Roles (не удалось создать роль клана)"
    except discord.HTTPException as e:
        return None, f"❌ ошибка создания роли: {e}"


async def sync_clan_role_by_id(guild, member, steam_id, user: dict | None = None):
    """Синхронизирует роль клана: выдаёт актуальную и снимает прежние.

    Когда игрок выходит из клана, cs2red перестаёт показывать его в составе —
    роль клана снимается. Ошибки снятия (например, роль бота ниже) больше
    не проглатываются: они попадают в результат.
    """
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return ("⚠️ у бота нет права «Управлять ролями» — роли клана не изменены "
                "(поднимите роль бота выше ролей клана)")

    if user is None:
        user = await api.cs2red_user(steam_id)
    if user is None:
        return f"⚠️ {member.display_name}: cs2red не ответил, роли не тронуты"
    if user.get("_not_found"):
        # Не снимаем роль клана из-за пустого/сбойного ответа поиска.
        return f"⚠️ {member.display_name}: не найден на cs2red, роли не тронуты"
    if not user.get("id"):
        return f"⚠️ {member.display_name}: некорректный ответ cs2red, роли не тронуты"

    clans = await api.cs2red_clans()
    if clans is None:
        return f"⚠️ {member.display_name}: /api/clans не ответил, роли не тронуты"

    if not clans:
        return f"⚠️ {member.display_name}: /api/clans пусто, роли не тронуты"

    total_members = sum(len(c.get("members") or []) for c in clans)
    if total_members == 0:
        return f"⚠️ {member.display_name}: в /api/clans нет members, роли не тронуты"

    clan, clan_err = await api.player_clan_checked(steam_id, user=user, clans=clans)
    if clan_err:
        return f"⚠️ {member.display_name}: {clan_err}, роли не тронуты"

    # Роли кланов, которыми управляем: CLAN_ROLE_MAP + все известные имена
    # (включая роли кланов, которых уже нет в /api/clans).
    managed = set(CLAN_ROLE_MAP.values())
    known_names = _known_clan_role_names(clans)

    if clan is None:
        # Игрок не в клане — снимаем все управляемые роли клана.
        stored = _load_clan_members().get(str(member.id))
        managed_ids = managed | ({stored} if stored else set())
        held = [r for r in member.roles
                if r.id in managed_ids or r.name in known_names]
        if held:
            above = _clan_roles_above_bot(guild, held)
            # Снимаем всё, что ниже роли бота, даже если часть ролей выше:
            # иначе одна «высокая» роль блокировала бы всю очистку.
            to_remove = [r for r in held if r not in above]
            errors = await _remove_roles_checked(member, to_remove) if to_remove else []
            if above:
                errors.append("выше роли бота: "
                              + ", ".join(f"«{r.name}»" for r in above))
            # Роль клана, которую больше никто не носит, удаляем сразу.
            for r in to_remove:
                await _delete_clan_role_if_empty(guild, r)
            if errors:
                return "⚠️ клан снят не полностью: " + "; ".join(errors)
            _forget_member_clan_role(member.id)
            return "🔄 клан снят"
        _forget_member_clan_role(member.id)  # запись без роли — чистим
        return "ℹ️ без клана"

    clan_name = (clan.get("clan_name") or "").strip()
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
                return f"⚠️ {created_status or 'не удалось создать роль клана'}"

    _remember_clan_role_name(clan_name)

    above_target = _clan_roles_above_bot(guild, [target])
    if above_target:
        return (f"⚠️ роль «{target.name}» выше роли бота — "
                "поднимите роль бота выше ролей клана")

    # Снимаем прочие управляемые роли клана (включая запомненную за игроком).
    stored = _load_clan_members().get(str(member.id))
    held = [r for r in member.roles
            if (r.id in managed or r.name in known_names
                or (stored is not None and r.id == stored)) and r != target]
    errors = []
    if held:
        above = _clan_roles_above_bot(guild, held)
        if above:
            errors.append("выше роли бота: " + ", ".join(f"«{r.name}»" for r in above))
            held = [r for r in held if r not in above]
        if held:
            errors.extend(await _remove_roles_checked(member, held))
            # Игрок перешёл в другой клан — старая роль могла опустеть.
            for r in held:
                await _delete_clan_role_if_empty(guild, r)
    suffix = (" (не сняты: " + "; ".join(errors) + ")") if errors else ""

    if target in member.roles:
        _remember_member_clan_role(member.id, target.id)
        return (f"✅ уже в '{clan_name}'"
                + (f" ({created_status})" if created_status else "") + suffix)
    try:
        await member.add_roles(target)
        _remember_member_clan_role(member.id, target.id)
        return (f"✅ выдан клан '{clan_name}'"
                + (f" ({created_status})" if created_status else "") + suffix)
    except discord.Forbidden:
        return "❌ нет прав выдать роль"


# ============================================================
# ВЕТКИ ЗАЯВОК
# ============================================================
async def close_application_thread(thread, closed_by, reason):
    """Закрывает ветку заявки: архив + блокировка (без удаления).

    Ветка остаётся читаемой — участники видят историю заявки, но писать
    в неё уже нельзя. Раньше закрытие выкидывало из ветки всех, кроме
    ревьюеров; теперь состав сохраняется.
    """
    if not isinstance(thread, discord.Thread):
        return
    try:
        try:
            await safe_send(thread, f"🔒 **Ветка закрыта.** {reason}\n**Закрыл:** {closed_by.mention}")
        except Exception:
            pass
        try:
            await thread.edit(archived=True, locked=True, reason=f"Заявка {reason}")
        except Exception as e:
            log(f"⚠️ close_thread edit: {e}")
    except Exception as e:
        log(f"⚠️ close_thread: {e}")


async def _keep_only_applicant_and_taker(interaction, taker):
    """Оставляет в ветке только заявителя и модератора, который её взял.

    Вызывается по кнопке «Взять» в ветке: заявку ведёт один модератор,
    остальных участников бот убирает из ветки.
    """
    thread = getattr(interaction, "channel", None)
    if not isinstance(thread, discord.Thread):
        return
    try:
        applicant_id = extract_discord_id_from_embed(interaction.message.embeds[0])
    except Exception:
        applicant_id = None
    if not applicant_id:
        return
    keep = {int(applicant_id), int(taker.id)}
    try:
        members = await thread.fetch_members()
    except Exception:
        members = []
    for m in members:
        if m.id in keep or getattr(m, "bot", False):
            continue
        try:
            await thread.remove_user(m)
        except Exception:
            pass


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


async def _apply_vote(interaction, kind, app_id, tid, show_take,
                      reason: str = ""):
    """Применяет голос и, если порог достигнут, закрывает заявку.

    Вынесено из ``_vote``, потому что отклонение приходит из модального
    окна: там уже нет кнопки, но app_id и заявитель сохранены в модалке.
    ``reason`` — обязательная причина отказа; попадает в embed, БД и аудит.
    """
    st, applied = await votes_repo.apply_vote(
        app_id, kind, interaction.user.id,
        allow_repeat=user_can_spam_vote(interaction.user),
    )
    if not applied:
        await safe_send(interaction.followup, "Вы уже голосовали.", ephemeral=True)
        return

    # В канале-логе «Взять» быть не должно. После голоса мы перерисовываем
    # исходное сообщение, поэтому сохраняем его контекст: ветка — с кнопкой,
    # канал — без неё.
    view = ApplicationManagement(app_id, show_take=show_take)
    if st[kind] >= VOTES_REQUIRED:
        # Порог достигнут — сначала закрываем заявку в БД (CAS внутри
        # finalize_application). Только один модератор получит True и
        # выполнит побочные эффекты: выдачу роли, закрытие ветки, аудит.
        if tid:
            try:
                closed = await applications_repo.finalize_application(
                    tid, "accepted" if kind == "accept" else "rejected",
                    moderator_id=interaction.user.id, reason=reason)
            except Exception as e:
                await log_error(interaction.client, "_apply_vote.finalize", e)
                closed = True  # сбой БД не должен отменять решение модератора
            if not closed:
                await safe_send(interaction.followup,
                                "ℹ️ Заявка уже закрыта другим модератором.",
                                ephemeral=True)
                return
        status = "Принят" if kind == "accept" else "Отклонен"
        embed = (interaction.message.embeds[0]
                 if interaction.message and interaction.message.embeds else None)
        if embed is not None:
            embed.set_footer(text=f"Quantum • Статус: {status}")
            if kind == "deny" and reason:
                embed.add_field(name="Причина отказа", value=reason[:1024],
                                inline=False)
        for c in view.children:
            c.disabled = True
        try:
            await interaction.message.edit(embed=embed, view=view)
        except Exception:
            pass
        member = interaction.guild.get_member(int(tid)) if tid else None
        if kind == "accept" and member and ACCEPT_ROLE_ID:
            # Приняли заявку — автоматически выдаём роль клана.
            role_to_give = interaction.guild.get_role(ACCEPT_ROLE_ID)
            if role_to_give:
                try:
                    await member.add_roles(role_to_give)
                    await log_to_channel(interaction.client,
                        f"✅ Заявка принята. {member.mention} получил {role_to_give.mention}")
                except discord.Forbidden:
                    await log_to_channel(interaction.client,
                        f"⚠️ Не смог выдать роль {role_to_give.mention} игроку "
                        f"{member.mention} — роль бота ниже роли клана.")
        if member:
            await log_to_channel(interaction.client,
                f"{'✅' if kind == 'accept' else '❌'} {member.mention}")
        # Аудит: решение по заявке с причиной отказа.
        try:
            await audit_action(
                interaction.client,
                "Заявка принята" if kind == "accept" else "Заявка отклонена",
                actor=interaction.user,
                target=member or tid,
                details=(f"Заявка `{app_id or tid or '—'}`\n"
                         + (f"Причина: {reason}" if reason else "Без причины")),
            )
        except Exception as e:
            log(f"⚠️ audit заявки: {e}")
        if isinstance(interaction.channel, discord.Thread):
            await close_application_thread(interaction.channel, interaction.user, status)
    else:
        try:
            await interaction.message.edit(view=view)
        except Exception:
            pass


async def _vote(interaction, kind, button=None):
    await interaction.response.defer()
    if not user_can_review(interaction.user):
        await safe_send(interaction.followup, "❌ Нет прав.", ephemeral=True)
        return
    # Заявитель не должен сам решать свою судьбу — голосовать по своей
    # заявке ему нельзя (иначе «Принять»/«Отклонить» жмётся в один клик).
    embed = (interaction.message.embeds[0]
             if interaction.message and interaction.message.embeds else None)
    tid = extract_discord_id_from_embed(embed) if embed else None
    if tid and str(interaction.user.id) == str(tid):
        await safe_send(interaction.followup,
                        "❌ Вы не можете голосовать по своей заявке.", ephemeral=True)
        return

    app_id = _app_id_for(interaction, button)
    await _apply_vote(interaction, kind, app_id, tid,
                      isinstance(interaction.channel, discord.Thread))


class RejectModal(discord.ui.Modal, title="Отклонить заявку"):
    """Модальное окно отказа: причина обязательна.

    Discord сам не даст отправить пустое поле (``required=True``), но
    проверка на сервере всё равно нужна: значение может прийти из
    старого клиента или состоять из пробелов.
    """

    def __init__(self, app_id: str, tid: str, show_take: bool = True):
        super().__init__()
        self.app_id = str(app_id or "")
        self.tid = str(tid or "")
        self.show_take = bool(show_take)
        self.reason = discord.ui.TextInput(
            label="Причина отказа (обязательно)",
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=3,
            max_length=900,
            placeholder="Коротко объясните игроку причину отказа",
        )
        self.add_item(self.reason)

    async def on_submit(self, i):
        # Повторная проверка прав: модалку открывает кнопка, но проверять
        # на всякий случай нужно и здесь — компонент живёт до отправки.
        if not user_can_review(i.user):
            await i.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        if self.tid and str(i.user.id) == str(self.tid):
            await i.response.send_message(
                "❌ Вы не можете голосовать по своей заявке.", ephemeral=True)
            return
        reason = (self.reason.value or "").strip()
        if len(reason) < 3:
            await i.response.send_message(
                "❌ Причина отказа обязательна (минимум 3 символа).",
                ephemeral=True)
            return
        try:
            await i.response.defer()
        except discord.InteractionResponded:
            pass
        await _apply_vote(i, "deny", self.app_id, self.tid, self.show_take,
                          reason=reason)


# ============================================================
# МЕНЮ ЗАЯВКИ
# ============================================================
class ApplicationManagement(discord.ui.View):
    """Кнопки заявки в канале заявок и в ветке.

    app_id зашит в custom_id кнопок: заявка публикуется в канале, в ветке
    и в лог-канале, и все копии должны делить одни и те же голоса.

    В канале-логе кнопки «Взять» нет: заявку берут только в ветке,
    созданной под конкретную заявку игрока (``show_take=False`` для
    сообщения в канале). Кнопки: «Взять», «Принять» (синяя),
    «Отклонить» (красная). Достаточно одного голоса
    (``VOTES_REQUIRED``). Кнопок запретов здесь нет — они живут в
    отдельном view ``ApplicationBansView``.
    """

    def __init__(self, app_id: str = "", show_take: bool = True):
        super().__init__(timeout=None)
        self.app_id = str(app_id or "")
        if not show_take:
            for child in self.children:
                if (child.custom_id or "").startswith("take_btn"):
                    self.remove_item(child)
        if self.app_id:
            # discord.py копирует кнопки в экземпляр: правка не влияет
            # на сам класс и на другие экземпляры.
            for child in self.children:
                if child.custom_id:
                    child.custom_id = f"{child.custom_id}:{self.app_id}"

    async def update_labels(self, mid=None):
        # Счётчики «0/2» убраны: достаточно одного голоса, показывать
        # «Принять 1/1» смысла нет — кнопка просто срабатывает.
        return

    @discord.ui.button(label="🙋 Взять", style=discord.ButtonStyle.secondary,
                       custom_id="take_btn", row=0)
    async def take(self, i, b):
        await i.response.defer()
        if not user_can_review(i.user):
            await safe_send(i.followup, "❌ Нет прав.", ephemeral=True)
            return
        app_id = _app_id_for(i, b)
        try:
            await applications_repo.mark_application_taken(app_id, i.user.id)
        except Exception as e:
            await log_error(i.client, "take_btn.db", e)
        await safe_send(i.followup, f"🙋 Взял {i.user.mention}.")
        try:
            await audit_action(
                i.client, "Заявка взята", actor=i.user,
                details=f"Заявка `{app_id}`")
        except Exception as e:
            await log_error(i.client, "take_btn.audit", e)
        # В ветке заявку берёт один модератор: оставляем только заявителя
        # и того, кто взял — остальных убираем из ветки.
        try:
            await _keep_only_applicant_and_taker(i, i.user)
        except Exception as e:
            await log_error(i.client, "take_btn", e)

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.primary,
                       custom_id="accept_btn", row=0)
    async def accept(self, i, b):
        await _vote(i, "accept", b)

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger,
                       custom_id="deny_btn", row=0)
    async def deny(self, i, b):
        # Причина отказа обязательна: сначала модальное окно, голос — после.
        if not user_can_review(i.user):
            await i.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        embed = (i.message.embeds[0] if i.message and i.message.embeds else None)
        tid = extract_discord_id_from_embed(embed) if embed else None
        if tid and str(i.user.id) == str(tid):
            await i.response.send_message(
                "❌ Вы не можете голосовать по своей заявке.", ephemeral=True)
            return
        app_id = _app_id_for(i, b)
        await i.response.send_modal(RejectModal(
            app_id, tid or "", isinstance(i.channel, discord.Thread)))


class ApplicationBansView(discord.ui.View):
    """Кнопки запретов — только в лог-канале заявок.

    Заявка в логе нужна, чтобы выдавать блокировки удобно: кнопок
    «Принять»/«Отклонить» здесь нет, они остаются в канале заявок и ветке.
    """

    def __init__(self, app_id: str = ""):
        super().__init__(timeout=None)
        self.app_id = str(app_id or "")
        if self.app_id:
            for child in self.children:
                if child.custom_id:
                    child.custom_id = f"{child.custom_id}:{self.app_id}"

    @discord.ui.button(label="🚫 Выдать запрет", style=discord.ButtonStyle.secondary,
                       custom_id="tempban_btn", row=0)
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
                       custom_id="permban_btn", row=0)
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
        await audit_action(i.client, "Перманентный запрет", actor=i.user,
                           target=tid, details=f"Discord ID: `{tid}`")

    @discord.ui.button(label="✅ Снять запрет", style=discord.ButtonStyle.success,
                       custom_id="unban_btn", row=0)
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
            await audit_action(i.client, "Запрет снят", actor=i.user,
                               target=tid, details=f"Discord ID: `{tid}`")


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
        await audit_action(i.client, "Временный запрет", actor=i.user,
                           target=self.tid,
                           details=(f"Срок: {hours} ч.\n"
                                    f"Причина: {self.reason.value.strip() or '—'}"))

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
            # Показываем дату — иначе игрок не понимает, какую заявку ждёт,
            # а «зависшая» заявка блокирует подачу навсегда.
            created = _parse_iso_utc(existing.get("created_at"))
            since = f" от {created.strftime('%d.%m.%Y')}" if created else ""
            log(f"⏳ заявка {i.user.id}: отклонена подача, активна заявка{since}")
            await safe_send(
                i.followup,
                f"❌ У вас уже есть заявка{since} на рассмотрении. "
                "Дождитесь её завершения.",
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
        # Один app_id на заявку: сообщение в канале, в ветке и в логе
        # должны показывать одни и те же счётчики голосов.
        app_id = _new_app_id()
        msg = None
        if ch:
            try:
                # В канале-логе — «Принять / Отклонить» без «Взять»:
                # заявку берут только в ветке, созданной под игрока.
                view = ApplicationManagement(app_id, show_take=False)
                msg = await safe_send(ch, content=f"🔔 Новая заявка от {i.user.mention}",
                                      embed=e, view=view)
            except Exception as ex:
                log(f"⚠️ Канал заявок: {ex}")

        # Копия заявки в лог-канал: только кнопки запретов, без
        # «Принять»/«Отклонить» — блокировки удобно выдавать из лога.
        log_ch = (i.guild.get_channel(APPLICATION_LOGS_CHANNEL_ID)
                  if APPLICATION_LOGS_CHANNEL_ID else None)
        if log_ch and (ch is None or log_ch.id != getattr(ch, "id", None)):
            try:
                await safe_send(log_ch, content=f"🛡 Копия заявки {i.user.mention}",
                                embed=e, view=ApplicationBansView(app_id))
            except Exception as ex:
                log(f"⚠️ Лог заявок: {ex}")

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
        # Держим ссылку на задачу: без неё GC может убить её до создания
        # ветки, и заявка останется без обсуждения.
        task = asyncio.create_task(_create_application_thread(i, e, app_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


async def _create_application_thread(i, e, app_id: str = ""):
    """Создаёт приватную ветку заявки.

    В ветку добавляются: заявитель, все ревьюеры и участники ролей из
    ``APPLICATION_PING_ROLE_IDS``. Первое сообщение упоминает эти роли,
    чтобы уведомление ушло всем заинтересованным сразу.
    """
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

        # Кого позвать в ветку: ревьюеры + участники ролей пинга.
        added, tasks_ = set(), []
        role_ids = list(get_all_reviewer_role_ids()) + list(APPLICATION_PING_ROLE_IDS)
        for rid in role_ids:
            role = i.guild.get_role(rid) if rid else None
            if role:
                for m in role.members:
                    if m.id not in added and not m.bot:
                        added.add(m.id)
                        tasks_.append(t.add_user(m))
        for x in range(0, len(tasks_), 20):
            await asyncio.gather(*tasks_[x:x + 20], return_exceptions=True)

        # Упоминание ролей идёт выше текста заявки.
        pings = " ".join(f"<@&{rid}>" for rid in APPLICATION_PING_ROLE_IDS
                         if i.guild.get_role(rid))
        header = (f"{i.user.mention}, ваша заявка на рассмотрении."
                  + (f"\n{pings}" if pings else ""))
        # Тот же app_id, что и у сообщения в канале: голоса общие.
        view = ApplicationManagement(app_id)
        await safe_send(t, content=header, embed=e, view=view)
    except Exception as ex:
        log(f"⚠️ Ветка: {ex}")


async def expire_stale_applications(days=None) -> list:
    """Закрывает заявки, по которым нет решения дольше N дней.

    Без этого «pending»-заявка висит вечно и блокирует игроку новую
    подачу: on_submit отвечает «у вас уже есть заявка на рассмотрении».
    Срок совпадает с обещанием в тексте условий (7 дней).

    Возвращает список user_id закрытых заявок.
    """
    days = APPLICATION_TTL_DAYS if days is None else int(days)
    if days <= 0:
        return []
    deadline = now_utc() - datetime.timedelta(days=days)
    try:
        rows = await applications_repo.list_pending_applications()
    except Exception as e:
        log(f"⚠️ автозакрытие заявок: не прочитал список: {e}")
        return []

    closed = []
    for row in rows:
        created = _parse_iso_utc(row.get("created_at"))
        if created is None or created > deadline:
            continue
        user_id = row.get("user_id")
        reason = f"Автоотклонение: нет решения за {days} дн."
        try:
            ok = await applications_repo.finalize_application(
                user_id, "rejected", moderator_id=None, reason=reason)
        except Exception as e:
            log(f"⚠️ автозакрытие заявки {user_id}: {e}")
            continue
        if not ok:
            continue
        closed.append(user_id)
        log(f"⌛ Заявка {user_id} закрыта автоматически (старше {days} дн.)")
        await audit_action(
            bot, "Автоотклонение заявки",
            details=f"Игрок: <@{user_id}>\nПричина: {reason}")
        user = bot.get_user(int(user_id)) if str(user_id).isdigit() else None
        if user is not None:
            try:
                await user.send(
                    f"⌛ Ваша заявка в клан {CLAN_NAME} отклонена автоматически: "
                    f"решение не принято за {days} дн. Можно подать новую.")
            except Exception:
                pass
    return closed


@tasks.loop(hours=1)
async def expire_applications():
    """Раз в час закрывает заявки, превысившие APPLICATION_TTL_DAYS."""
    try:
        closed = await expire_stale_applications()
        if closed:
            ch = bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
            if ch is not None:
                await safe_send(ch, f"⌛ Автоотклонено заявок: {len(closed)}")
    except Exception as e:
        await log_error(bot, "expire_applications", e)


@expire_applications.before_loop
async def before_expire_applications():
    await bot.wait_until_ready()
    await asyncio.sleep(random.randint(60, 180))


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
# Команды, доступные любому участнику сервера без роли модератора.
# /search включён осознанно: внутри он отдаёт чужую историю только
# ревьюеру (см. проверку target.id != i.user.id), а свою — всем.
# /check и /status остаются модераторскими: это статистика по заявкам,
# которые модератор брал/решал, и она выдаётся через /give.
PUBLIC_COMMANDS = {"link", "unlink", "ping", "health", "lookup", "ask",
                   "rules", "search", "help"}

# Команды, которые нельзя делегировать через /give: они управляют доступом
# или меняют данные опаснее, чем обычная модерация. Проверка прав внутри
# самих команд остаётся, но панель их не показывает.
ADMIN_ONLY_COMMANDS = {
    "give", "take", "admlink", "auto_link", "sync_all", "export_members",
    "link_all", "player_list", "player_note", "ai_clear_cache", "panel",
    "activity_set", "activity_clear",
}

# Команды, которые админ может выдать модератору. Порядок = порядок в меню.
MODERATOR_COMMANDS = [
    "check", "check_all", "check_bans", "check_mutes", "check_clan",
    "check_onlineall", "activity", "activity_all", "activity_show",
    "search", "player_info", "clan_info", "status", "admins", "moders",
]


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
        if not await is_moderator(i.user.id, i.guild):
            if not i.response.is_done():
                await i.response.send_message("❌ Нет доступа.", ephemeral=True)
            return False
        # Администраторы сервера не ограничены списком команд модератора.
        if admin_only(i):
            return True
        # Даже при полном доступе ("*") служебные команды остаются админскими.
        if cmd.name in ADMIN_ONLY_COMMANDS:
            if not i.response.is_done():
                await i.response.send_message(
                    "❌ Эта команда только для администраторов.", ephemeral=True)
            return False
        if not await moderation_repo.has_command_access(i.user.id, cmd.name):
            if not i.response.is_done():
                await i.response.send_message(
                    "❌ Нет доступа к этой команде. "
                    "Попросите администратора выдать её в /give.",
                    ephemeral=True)
            return False
        return True

    async def setup_hook(self):
        try:
            log("🔧 setup_hook: начало")
            # discord.py НЕ вызывает Bot.interaction_check: проверку доступа
            # выполняет CommandTree (app_commands/tree.py). Без этой привязки
            # метод ниже — мёртвый код, и все команды доступны любому
            # участнику сервера. Привязываем гейт к дереву команд.
            self.tree.interaction_check = self.interaction_check
            await init_db()
            await import_json_to_sqlite(DATA_DIR)
            log("✅ SQLite готов (init_db + миграция)")
            try:
                removed = await audit_repo.prune(180)
                if removed:
                    log(f"🧹 Аудит: удалено старых записей — {removed}")
            except Exception as e:
                log(f"⚠️ audit prune: {type(e).__name__}: {e}")
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
            # Кнопки запретов в лог-канале — отдельный persistent view.
            self.add_view(ApplicationBansView())
            log("✅ ApplicationBansView view добавлен")
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
            health_watch.start()
            daily_summary.start()
            clan_panel_refresh.start()
            expire_applications.start()
            auto_link_watch.start()
            sync_all_watch.start()
            clan_role_cleanup_watch.start()
            if not GUILD_ID:
                log("⚠️ GUILD_ID = 0 — автосинхронизация ролей клана и /check "
                    "не смогут найти сервер (задайте GUILD_ID)")
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
                self.add_view(ApplicationBansView(app_id))
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
agent = Agent(bot, AGENT_CHANNEL_ID, redact=redact)

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
async def on_command_error(ctx, err):
    """Ошибки префиксных команд (``?ask``, ``?rules`` и т.д.).

    Без этого обработчика discord.py только пишет трейсбек в stderr:
    ошибки префиксных команд не попадали ни в лог-канал, ни в
    error_log.txt, ни в ИИ-агента.
    """
    if isinstance(err, commands.CommandNotFound):
        return
    if isinstance(err, commands.CommandOnCooldown):
        await safe_send(ctx.channel, f"⏳ Подождите {err.retry_after:.0f} сек.")
        return
    if isinstance(err, commands.MissingRequiredArgument):
        await safe_send(ctx.channel, f"❌ Не хватает аргумента: `{err.param.name}`.")
        return
    if isinstance(err, commands.CheckFailure):
        await safe_send(ctx.channel, "❌ Нет доступа.")
        return
    # CommandInvokeError заворачивает исходное исключение — логируем его.
    original = getattr(err, "original", err)
    command = getattr(ctx.command, "name", "?") if ctx.command else "?"
    await log_error(ctx.bot, f"prefix:{command}", original)


@bot.event
async def on_ready():
    log(f"✅ Бот {bot.user} готов!")
    log(f"🆔 GUILD_ID: {GUILD_ID}")
    log(f"🍪 CS2RED cookies: {len(CS2RED_COOKIES)} шт.")
    log(f"📡 Guilds: {[g.name for g in bot.guilds]}")


@bot.event
async def on_member_remove(member):
    """Участник ушёл с сервера: роль клана без носителей удаляем сразу."""
    try:
        _forget_member_clan_role(member.id)
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        names = _load_clan_role_names()
        managed_ids = set(CLAN_ROLE_MAP.values())
        for role in list(getattr(member, "roles", [])):
            if role.name in names or role.id in managed_ids:
                await _delete_clan_role_if_empty(
                    guild, role, names=names, exclude_member_id=member.id)
    except Exception as e:
        log(f"⚠️ on_member_remove: {type(e).__name__}: {e}")


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
                # Не перехватываем SteamID, уже привязанный к другому Discord:
                # иначе смена ника на сервере позволяла бы увести чужую привязку.
                owner = await links_repo.get_owner_of(steam_id)
                if owner and str(owner) != str(member.id):
                    log(f"🐣 on_member_join {member.display_name}: SteamID `{steam_id}` "
                        f"уже привязан к {owner} — автопривязка пропущена")
                    return
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
    # В ЛС у User нет guild_permissions — не роняем команду, просто отказываем.
    perms = getattr(getattr(i, "user", None), "guild_permissions", None)
    return bool(perms and perms.administrator)


def admin_only_ctx(ctx):
    """То же, что admin_only, но для текстовых команд (ctx.author, а не i.user)."""
    perms = getattr(getattr(ctx, "author", None), "guild_permissions", None)
    return bool(perms and perms.administrator)


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
        await audit_action(i.client, "Привязка SteamID", actor=i.user,
                           target=i.user,
                           details=(f"SteamID: `{steam_id}`\n"
                                    f"Ник на cs2red: {u.get('nick') or '—'}"))
        try:
            s1 = await sync_role_by_id(i.guild, i.user, steam_id, user=u)
            s2 = await sync_clan_role_by_id(i.guild, i.user, steam_id, user=u)
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
        if i.guild is None or not isinstance(i.user, discord.Member):
            # В ЛС ролей нет: отвязываем и чистим запись, но не пытаемся
            # обращаться к i.user.roles (у User такого атрибута нет).
            _forget_member_clan_role(i.user.id)
            await safe_send(i.followup,
                            "✅ Отвязано. Роли на сервере не изменены (команда в ЛС).",
                            ephemeral=False)
            await audit_action(i.client, "Отвязка SteamID", actor=i.user,
                               target=i.user,
                               details=f"SteamID: `{rid}` (ЛС)")
            return
        managed_n = {n for n in ROLE_MAP.values() if n} | _load_site_role_names()
        managed_i = set(CLAN_ROLE_MAP.values())
        known_clan_names = _load_clan_role_names()
        stored_role = _load_clan_members().get(str(i.user.id))
        removed = []
        failed = []
        for r in list(i.user.roles):
            is_clan_role = (r.id in managed_i or r.name in known_clan_names
                            or (stored_role is not None and r.id == stored_role))
            if r.name not in managed_n and not is_clan_role:
                continue
            try:
                await i.user.remove_roles(r)
                removed.append(r.name)
            except discord.Forbidden:
                failed.append(r.name)
            except Exception as e:
                failed.append(f"{r.name}: {e}")
        _forget_member_clan_role(i.user.id)
        text = f"✅ Отвязано. Снято: {', '.join(removed) if removed else 'нет'}"
        if failed:
            text += ("\n⚠️ Не удалось снять: " + ", ".join(failed)
                     + " — роль бота должна быть выше этих ролей.")
        await safe_send(i.followup, text, ephemeral=False)
        await audit_action(i.client, "Отвязка SteamID", actor=i.user,
                           target=i.user, details=f"SteamID: `{rid}`")
    except Exception as e:
        await log_error(i.client, "/unlink", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="admlink", description="Привязать SteamID пользователю (админ)")
@app_commands.describe(user="Участник Discord", steam_id="SteamID64 (17 цифр)")
@app_commands.default_permissions(administrator=True)
async def admlink(i, user: discord.Member, steam_id: str):
    """Админская привязка: переносит SteamID у старого владельца и синхронизирует роли.

    Обычный ``add_link`` падал с UNIQUE constraint, если SteamID уже был
    привязан к другому Discord (например, игрок перешёл на новый аккаунт).
    Здесь привязка переносится, а роли обновляются сразу.
    """
    await i.response.defer(ephemeral=True)
    if not await _admin_guard(i):
        return
    steam_id = (steam_id or "").strip()
    if not (steam_id.isdigit() and len(steam_id) == 17 and steam_id.startswith("7656119")):
        await safe_send(i.followup, "❌ Неверный SteamID (ожидается SteamID64 из 17 цифр, "
                                    "начинающийся с 7656119).", ephemeral=True)
        return
    try:
        if user.bot:
            await safe_send(i.followup, "❌ Нельзя привязать бота.", ephemeral=True)
            return

        # Данные cs2red нужны для ника и синхронизации ролей. Если сайт
        # недоступен, привязку всё равно сохраняем — команда админская.
        user_info, user_err = None, ""
        try:
            user_info = await asyncio.wait_for(
                api.cs2red_user(steam_id), timeout=CMD_API_TIMEOUT)
        except asyncio.TimeoutError:
            user_err = "cs2red не ответил вовремя"
        if user_info is None:
            user_err = user_err or "cs2red не ответил"
        elif user_info.get("_not_found"):
            user_err = "игрок не найден на cs2red"
            user_info = None
        elif not user_info.get("id"):
            user_err = "некорректный ответ cs2red"
            user_info = None

        was_linked = await links_repo.get_link(user.id)
        # steam_id уникален: если он за другим Discord, переносим привязку.
        previous = await links_repo.transfer_link(user.id, steam_id)

        sync_lines = []
        if i.guild is None:
            sync_lines.append("⚠️ команда вне сервера: роли не изменены")
        elif user_info is None:
            sync_lines.append(f"⚠️ {user_err}: роли не изменены")
        else:
            try:
                sync_lines.append(await sync_role_by_id(
                    i.guild, user, steam_id, user=user_info))
            except Exception as e:
                sync_lines.append("⚠️ роль по cs2red не синхронизирована")
                await log_error(i.client, "/admlink.role", e)
            try:
                sync_lines.append(await sync_clan_role_by_id(
                    i.guild, user, steam_id, user=user_info))
            except Exception as e:
                sync_lines.append("⚠️ роль клана не синхронизирована")
                await log_error(i.client, "/admlink.clan", e)

        if previous:
            action = f"перенесена от <@{previous}>"
        elif was_linked:
            action = "обновлена"
        else:
            action = "создана"

        embed = discord.Embed(
            title="✅ Админ-привязка",
            color=0x000000,
            timestamp=now_utc(),
        )
        embed.add_field(name="Пользователь", value=user.mention, inline=True)
        embed.add_field(name="Steam ID", value=f"`{steam_id}`", inline=True)
        embed.add_field(name="Ник на cs2red",
                        value=((user_info or {}).get("nick") or "—")[:1024],
                        inline=True)
        embed.add_field(name="Действие", value=action, inline=False)
        embed.add_field(name="Роли", value=("\n".join(sync_lines) or "—")[:1024],
                        inline=False)
        embed.set_footer(text=f"Выполнил: {i.user.display_name}")

        await safe_send(i.followup, embed=embed, ephemeral=True)
        await log_to_channel(i.client,
            f"🔗 /admlink: {user.mention} ({user.id}) → `{steam_id}` "
            f"({action}) от {i.user.mention}\n"
            + "\n".join(sync_lines))
        await audit_action(
            i.client, "Админ-привязка", actor=i.user, target=user,
            details=(f"SteamID: `{steam_id}`\n{action}\n"
                     + "\n".join(sync_lines))[:1024])
    except Exception as e:
        await log_error(i.client, "/admlink", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="clan_roleclear",
                  description="Удалить роли кланов, которые никто не носит (админ)")
@app_commands.default_permissions(administrator=True)
async def clan_roleclear(i):
    await i.response.defer(ephemeral=True)
    if not await _admin_guard(i):
        return
    try:
        guild = i.guild
        if guild is None:
            await safe_send(i.followup, "❌ Команда работает только на сервере.",
                            ephemeral=True)
            return
        known = _load_clan_role_names()
        deleted, errors = await cleanup_empty_clan_roles(guild)
        lines = [f"🧹 Удалено ролей клана: {len(deleted)}"]
        if deleted:
            lines += [f"• «{n}»" for n in deleted[:25]]
            if len(deleted) > 25:
                lines.append(f"…и ещё {len(deleted) - 25}")
        if errors:
            lines.append("⚠️ Не удалось удалить:")
            lines += [f"• {e}" for e in errors[:10]]
        if not deleted and not errors:
            lines.append("Пустых ролей кланов нет — чистить нечего "
                         f"(известных ролей кланов: {len(known)}).")
        await safe_send(i.followup, "\n".join(lines)[:1900], ephemeral=True)
        await log_to_channel(
            i.client,
            f"🧹 /clan_roleclear: удалено {len(deleted)}, ошибок {len(errors)}")
        await audit_action(
            i.client, "Принудительная очистка ролей кланов", actor=i.user,
            details=(f"Удалено: {len(deleted)}\nОшибок: {len(errors)}\n"
                     f"Известных ролей кланов: {len(known)}"))
    except Exception as e:
        await log_error(bot, "/clan_roleclear", e)
        await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)


@bot.tree.command(name="sync_all", description="Синхронизировать всех")
@single_flight("sync_all", "⏳ Синхронизация уже выполняется. Подождите.")
async def sync_all(i):
    await i.response.defer(ephemeral=True)
    if not await _admin_guard(i):
        return
    await run_sync_all(i, ephemeral=True)


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
        api.reset_block()

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
                # Один запрос к cs2red на игрока вместо двух.
                try:
                    user = await api.cs2red_user(sid)
                except Exception as e:
                    await log_error(bot, f"sync_all:fetch:{did}", e)
                    user = None
                if user is None:
                    res.append(f"⚠️ {m.display_name}: cs2red не ответил, роли не тронуты")
                    if api.blocked_reason in ("403", "429"):
                        stopped = True
                else:
                    try:
                        r1 = await sync_role_by_id(i.guild, m, sid, user=user)
                    except Exception as e:
                        await log_error(bot, f"sync_all:{did}", e)
                        r1 = f"⚠️ ошибка роли: {str(e)[:150]}"
                    res.append(r1)
                    try:
                        r2 = await sync_clan_role_by_id(i.guild, m, sid, user=user)
                    except Exception as e:
                        await log_error(bot, f"sync_all:clan:{did}", e)
                        r2 = f"⚠️ ошибка клана: {str(e)[:150]}"
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
            color=0x000000,
            timestamp=now_utc(),
        )
        try:
            await msg.edit(embed=final)
        except Exception:
            await safe_send(i.followup, text[:1900], title="🔗 Синхронизация",
                            ephemeral=ephemeral)
        await log_to_channel(i.client, text)
        await audit_action(
            i.client, "Синхронизация ролей", actor=i.user,
            details=(f"Обработано: {processed} из {total}\n"
                     f"Строк результата: {len(res)}"
                     + (f"\nОстановлено: {api.blocked_reason}"
                        if api.blocked_reason else "")))
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
@single_flight("link_all", "⏳ Массовая привязка уже выполняется. Подождите.")
async def link_all(i):
    await i.response.defer(ephemeral=True)
    if not await _admin_guard(i):
        return

    try:
        # Данные о привязках берем из БД (SQLite), а не из mass_links.json.
        data = await links_repo.get_all_links()
        if not data:
            await safe_send(i.followup,
                            "ℹ️ В базе нет привязок. Добавьте их через /link или /admlink.",
                            ephemeral=True)
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
                        ephemeral=True
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
                        ephemeral=True)
        await log_to_channel(i.client,
            f"📊 /link_all: +{len(added)} / уже {len(already)} / "
            f"не SID {len(skip_not_sid)} / нет на сервере {len(no_member)} / "
            f"нет на cs2red {len(not_on_site)} / прочее {len(other)}")
    except Exception as e:
        await log_error(i.client, "/link_all", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


def _unlinked_members(guild, links) -> list:
    """Участники без привязки Discord↔Steam (ботов не считаем)."""
    linked_ids = {str(k) for k in (links or {})}
    return [m for m in guild.members
            if not m.bot and str(m.id) not in linked_ids]


@bot.tree.command(name="auto_link", description="Авто-привязка по никам через админ-панель cs2red (админ)")
@app_commands.default_permissions(administrator=True)
@single_flight("auto_link", "⏳ Авто-привязка уже выполняется. Подождите.")
async def auto_link(i):
    await i.response.defer(ephemeral=True)
    if not await _admin_guard(i):
        return
    await run_auto_link(i, ephemeral=True)


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
    api.reset_block()
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
            color=0x000000,
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
            await safe_send(i.followup, embed=embed, ephemeral=True)
        await log_to_channel(i.client,
            f"🔗 /auto_link: привязано {len(added)}, неоднозначно "
            f"{len(skipped_ambigu)}, не найдено {len(skipped_none)}, ошибок {len(errors)}")
    except Exception as e:
        await log_error(i.client, "/auto_link", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="export_members", description="Выгрузить привязки в mass_links.json")
async def export_members(i):
    await i.response.defer(ephemeral=True)
    if not await _admin_guard(i):
        return
    try:
        links = await links_repo.get_all_links()
        if not links:
            await safe_send(i.followup, "ℹ️ Нет привязанных.", ephemeral=True)
            return
        valid = {}
        for did, sid in links.items():
            if isinstance(sid, str) and sid.isdigit() and len(sid) == 17 and sid.startswith("7656119"):
                valid[did] = sid
        try:
            await asyncio.to_thread(_write_json_file,
                                    data_path("mass_links.json"), valid)
        except Exception as e:
            await log_error(i.client, "/export_members.write", e)
            await safe_send(i.followup, f"❌ {e}", ephemeral=True)
            return
        await safe_send(i.followup,
            f"✅ Выгружено привязок: **{len(valid)}**",
            ephemeral=True
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
    await i.response.defer(ephemeral=True)

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
            await safe_send(i.followup, "❌ Нет SteamID", ephemeral=True)
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
            ephemeral=True
        )
    except Exception as e:
        await log_error(i.client, "/player_info", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="player_note", description="Заметка к профилю")
@app_commands.describe(member="Участник Discord", note="Заметка")
async def player_note(i, member: discord.Member, note: str):
    await i.response.defer(ephemeral=True)
    if not await _admin_guard(i):
        return
    try:
        sid = await links_repo.get_link(member.id)
        if not sid:
            await safe_send(i.followup, "❌ Нет SteamID", ephemeral=True)
            return

        async def _make():
            return await build_player_profile(i.guild, member.id, sid)
        profile = await players_repo.get_player(sid) or await _make()
        profile["notes"] = note
        profile["updated_at"] = now_utc().isoformat()
        await players_repo.save_player(sid, profile)
        await safe_send(i.followup, f"✅ Заметка для {member.mention}", ephemeral=True)
        await send_player_report(i.client, profile)
    except Exception as e:
        await log_error(i.client, "/player_note", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="player_list", description="Вся база")
async def player_list(i):
    await i.response.defer(ephemeral=True)
    if not await _admin_guard(i):
        return
    try:
        db = await players_repo.get_all_players()
        if not db:
            await safe_send(i.followup, "ℹ️ Пусто", ephemeral=True)
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
        await safe_send(i.followup, text[:1900], ephemeral=True)
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
# БЛОКИРОВКИ: /check_mutes, /check_bans, /check
# ============================================================
BLOCK_CHECK_DELAY = 3.0   # пауза между запросами к cs2red (требование сайта)
BLOCK_MAX_EMBEDS = 10     # Discord: не больше 10 embed'ов в одном сообщении


def _fmt_block_ts(ts: int) -> str:
    """Unix-время -> «ДД.ММ.ГГГГ ЧЧ:ММ» (UTC)."""
    if not ts:
        return "—"
    try:
        return datetime.datetime.fromtimestamp(ts, UTC).strftime("%d.%m.%Y %H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"


def _fmt_block_duration(start_ts: int, end_ts: int) -> str:
    """Длительность блокировки: «90 дн.», «2 ч.», «навсегда» или «истёк»."""
    if not end_ts:
        return "навсегда"
    now = time.time()
    if end_ts <= now:
        return "истёк"
    seconds = int(end_ts - (start_ts or now))
    days, rem = divmod(max(seconds, 0), 86400)
    hours = rem // 3600
    if days:
        return f"{days} дн." + (f" {hours} ч." if hours else "")
    if hours:
        return f"{hours} ч."
    return f"{max(1, rem // 60)} мин."


def _parse_steam_id(value: str) -> str | None:
    value = (value or "").strip()
    if value.isdigit() and len(value) == 17 and value.startswith("7656119"):
        return value
    return None


def _build_block_embeds(blocks: list, title: str, empty_text: str,
                        show_end: bool = False) -> list:
    """Один embed на блокировку (у Discord лимит 25 полей на embed)."""
    if not blocks:
        e = discord.Embed(title=title, description=empty_text,
                          color=0x000000, timestamp=now_utc())
        e.set_footer(text=f"Quantum • {utc_stamp()}")
        return [e]

    embeds = []
    for b in blocks[:BLOCK_MAX_EMBEDS]:
        e = discord.Embed(title=title, color=0x000000, timestamp=now_utc())
        player = (b.get("player_nick") or "—")[:900]
        sid = b.get("steam_id") or ""
        e.add_field(name="Тип", value=(b.get("kind") or "Бан")[:1024], inline=True)
        e.add_field(name="Игрок",
                    value=(f"{player}\n`{sid}`" if sid else player)[:1024],
                    inline=True)
        e.add_field(name="Причина", value=(b.get("reason") or "—")[:1024], inline=False)
        e.add_field(name="Срок",
                    value=_fmt_block_duration(b.get("start_ts") or 0, b.get("end_ts") or 0),
                    inline=True)
        e.add_field(name="Дата блокировки", value=_fmt_block_ts(b.get("start_ts") or 0),
                    inline=True)
        if show_end:
            end_ts = b.get("end_ts") or 0
            e.add_field(name="Дата разблокировки",
                        value=_fmt_block_ts(end_ts) if end_ts else "навсегда",
                        inline=True)
        e.add_field(name="Забанил", value=(b.get("admin") or "—")[:1024], inline=True)
        e.set_footer(text=f"Quantum • {utc_stamp()}")
        embeds.append(e)
    return embeds


async def _resolve_nick(steam_id: str, blocks: list) -> str:
    """Ник игрока: из блокировок, иначе запрос к cs2red."""
    for b in blocks:
        if b.get("player_nick") and b["player_nick"] != "—":
            return b["player_nick"]
    try:
        user = await api.cs2red_user(steam_id)
    except Exception:
        user = None
    if user and not user.get("_not_found"):
        return user.get("nick") or steam_id
    return steam_id


def _build_check_log_text(initiator: str, checked: int, offenders: list,
                          skipped: int, errors: int) -> str:
    """Текст мини-отчёта для лог-канала: кто получал мут/бан и когда."""
    lines = [
        f"Инициатор: {initiator}",
        f"Проверено: {checked} · с блокировками: {len(offenders)} · "
        f"без SteamID: {skipped} · ошибок: {errors}",
    ]
    if offenders:
        lines.append("\nКто получал блокировку и когда:")
        for m, sid, active in offenders:
            for b in active:
                when = _fmt_block_ts(b.get("start_ts") or 0)
                duration = _fmt_block_duration(b.get("start_ts") or 0, b.get("end_ts") or 0)
                nick = b.get("player_nick") or m.display_name
                lines.append(
                    f"• {m.display_name} ({m.id}, {nick}, {sid}) — "
                    f"{b.get('kind') or 'Бан'} · {when} · {duration} · "
                    f"{(b.get('reason') or '—')[:120]} · "
                    f"выдал: {b.get('admin') or '—'}")
    else:
        lines.append("\n✅ Блокировок не найдено.")
    return "\n".join(lines)


@bot.tree.command(name="check_mutes", description="Проверить муты игрока на cs2red")
@app_commands.describe(steam_id="SteamID64 (17 цифр)")
async def check_mutes(i, steam_id: str):
    await i.response.defer(ephemeral=True)
    try:
        sid = _parse_steam_id(steam_id)
        if not sid:
            await safe_send(i.followup, "❌ Неверный SteamID (нужен SteamID64 из 17 цифр).",
                            ephemeral=True)
            return
        _bans, blocks, err = await asyncio.wait_for(api.cs2red_all_blocks(sid),
                                                    timeout=CMD_API_TIMEOUT)
        if err == "unavailable":
            await safe_send(i.followup, "❌ Сайт cs2red временно недоступен", ephemeral=True)
            return
        if err == "not_found":
            await safe_send(i.followup, "❌ Игрок не найден на cs2red.", ephemeral=True)
            return
        active = [b for b in blocks if b.get("active")]
        nick = await _resolve_nick(sid, active or blocks)
        embeds = _build_block_embeds(active, f"🔇 Муты игрока: {nick}",
                                     "✅ У игрока нет активных мутов")
        await safe_send(i.followup, embeds=embeds[:BLOCK_MAX_EMBEDS], ephemeral=True)
    except asyncio.TimeoutError:
        await log_error(i.client, "/check_mutes", "TIMEOUT")
        try:
            await safe_send(i.followup, "❌ cs2red не ответил вовремя.", ephemeral=True)
        except Exception:
            pass
    except Exception as e:
        await log_error(i.client, "/check_mutes", e)
        try:
            await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)
        except Exception:
            pass


@bot.tree.command(name="check_bans", description="Проверить баны и муты игрока на cs2red")
@app_commands.describe(steam_id="SteamID64 (17 цифр)")
async def check_bans(i, steam_id: str):
    await i.response.defer(ephemeral=True)
    try:
        sid = _parse_steam_id(steam_id)
        if not sid:
            await safe_send(i.followup, "❌ Неверный SteamID (нужен SteamID64 из 17 цифр).",
                            ephemeral=True)
            return
        bans, mutes, err = await asyncio.wait_for(api.cs2red_all_blocks(sid),
                                                  timeout=CMD_API_TIMEOUT)
        if err == "unavailable":
            await safe_send(i.followup, "❌ Сайт cs2red временно недоступен", ephemeral=True)
            return
        if err == "not_found":
            await safe_send(i.followup, "❌ Игрок не найден на cs2red.", ephemeral=True)
            return
        active = [b for b in (bans + mutes) if b.get("active")]
        active.sort(key=lambda b: b.get("start_ts") or 0, reverse=True)
        nick = await _resolve_nick(sid, active or (bans + mutes))
        embeds = _build_block_embeds(active, f"🚫 Блокировки игрока: {nick}",
                                     "✅ У игрока нет активных блокировок", show_end=True)
        await safe_send(i.followup, embeds=embeds[:BLOCK_MAX_EMBEDS], ephemeral=True)
    except asyncio.TimeoutError:
        await log_error(i.client, "/check_bans", "TIMEOUT")
        try:
            await safe_send(i.followup, "❌ cs2red не ответил вовремя.", ephemeral=True)
        except Exception:
            pass
    except Exception as e:
        await log_error(i.client, "/check_bans", e)
        try:
            await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)
        except Exception:
            pass


def _taken_application_line(row: dict) -> str:
    """Одна строка списка /check: статус, ticket, заявитель, клан, дата."""
    status = (row.get("status") or "").strip().lower()
    emoji = _STATUS_EMOJI.get(status, "▫️")
    ident = (row.get("message_id") or row.get("app_id")
             or row.get("user_id") or "")
    line = f"{emoji} `ticket-{ident}`" if ident else f"{emoji} —"
    applicant = row.get("user_id") or ""
    if applicant:
        line += f" · <@{applicant}>"
    clan = (row.get("clan_name") or "").strip()
    if clan:
        line += f" · **{clan}**"
    line += f" · {_fmt_dt(row.get('taken_at'))}"
    reason = (row.get("decision_reason") or "").strip()
    if reason and status == "rejected":
        line += f"\n   📝 {reason[:160]}"
    return line


def _build_taken_applications_embeds(target, rows: list) -> list:
    """Embeds со всеми заявками, которые «взял» модератор.

    Список разбивается на части по 20 строк и ~1000 символов, чтобы
    каждая часть влезала в лимит поля Discord. safe_send сам уложит
    embeds в сообщения по лимитам 6000 символов / 10 embeds.
    """
    active_statuses = {"pending", "voting"}
    active = sum(1 for row in rows
                 if (row.get("status") or "").lower() in active_statuses)

    pages: list[list[str]] = []
    current: list[str] = []
    used = 0
    for row in rows:
        line = _taken_application_line(row)
        if current and (len(current) >= 20 or used + len(line) + 1 > 1000):
            pages.append(current)
            current = []
            used = 0
        current.append(line)
        used += len(line) + 1
    if current:
        pages.append(current)
    if not pages:
        pages = [[]]

    embeds = []
    for idx, lines in enumerate(pages):
        if idx == 0:
            title = f"📋 Заявки, которые взял {target.display_name}"
        else:
            title = f"📋 Заявки, которые взял {target.display_name} · стр. {idx + 1}"
        embed = discord.Embed(title=title, color=0x000000,
                              timestamp=now_utc())
        if idx == 0:
            try:
                embed.set_thumbnail(url=target.display_avatar.url)
            except Exception:
                pass
            embed.description = (f"Всего взято: **{len(rows)}** · "
                                 f"активных сейчас: **{active}**")
            embed.add_field(
                name="👤 Модератор",
                value=getattr(target, "mention", f"<@{target.id}>"),
                inline=True,
            )
            embed.add_field(name="📨 Взято заявок", value=str(len(rows)),
                            inline=True)
            embed.add_field(name="⏳ Активных", value=str(active), inline=True)
        else:
            embed.description = (f"Продолжение списка · всего взято: "
                                 f"**{len(rows)}**")
        embed.add_field(
            name="📜 Список",
            value=("\n".join(lines) if lines
                   else "Модератор ещё не брал заявки.")[:1024],
            inline=False,
        )
        embed.set_footer(text=f"Quantum • {utc_stamp()}")
        embeds.append(embed)
    return embeds


def _build_taken_applications_embed(target, rows: list) -> discord.Embed:
    """Первый embed списка /check (совместимость с тестами)."""
    return _build_taken_applications_embeds(target, rows)[0]


@bot.tree.command(name="check",
                  description="Все заявки, которые взял модератор")
@app_commands.describe(user="Чей список показать (по умолчанию — ваш)")
async def check(i, user: discord.Member | None = None):
    """Показывает историю заявок, взятых модератором кнопкой «Взять».

    В отличие от /status (только активные заявки), здесь виден весь
    список: и заявки в работе, и уже принятые/отклонённые.
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

        rows = await applications_repo.get_taken_applications(
            target.id, limit=None)
        embeds = _build_taken_applications_embeds(target, rows)
        await safe_send(i.followup, embeds=embeds, ephemeral=True)
    except Exception as e:
        await log_error(i.client, "/check", e)
        try:
            await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)
        except Exception:
            pass


# ============================================================
# /check_all — баны и муты за 2 недели у всех игроков с ролью TALENT
# ============================================================
def _filter_blocks_since(blocks: list, since_ts: int) -> list:
    """Оставляет блокировки, начавшиеся не раньше since_ts.

    Активные без даты начала тоже показываем: лучше показать лишнее,
    чем пропустить действующий бан/мут.
    """
    result = []
    for b in blocks:
        start = b.get("start_ts") or 0
        if start >= since_ts or (not start and b.get("active")):
            result.append(b)
    return result


def _recent_blocks(bans: list, mutes: list, since_ts: int) -> list:
    """Баны и муты за период, новые сверху.

    Раньше /check_all смотрел только муты и терял баны. Здесь обе
    категории объединяются до фильтрации по времени.
    """
    blocks = (bans or []) + (mutes or [])
    recent = _filter_blocks_since(blocks, since_ts)
    recent.sort(key=lambda b: b.get("start_ts") or 0, reverse=True)
    return recent


def _talent_members(guild) -> list:
    """Участники с ролью TALENT: сначала по id, затем по имени роли."""
    role = guild.get_role(TALENT_ROLE_ID)
    if role is None:
        role = discord.utils.get(guild.roles, name=CLAN_NAME)
    if role is None:
        return []
    return [m for m in guild.members if role in m.roles and not m.bot]


async def _clan_sync_members(guild) -> list:
    """Кого проверять при синхронизации ролей клана.

    Это не только участники с ролью TALENT: если игрок через /link
    ушёл в другой клан, старой роли TALENT у него может уже не быть,
    но новую роль всё равно нужно выдать. Поэтому в список попадают
    все привязанные через /link, все с ролью TALENT и все, у кого
    есть любая известная роль клана.
    """
    talent_ids = {m.id for m in _talent_members(guild)}
    linked = await links_repo.get_all_links()
    linked_ids = {int(k) for k in linked if str(k).isdigit()}
    managed_ids = set(CLAN_ROLE_MAP.values())
    known_names = _load_clan_role_names()
    result = []
    for m in guild.members:
        if m.bot:
            continue
        has_clan_role = any(
            r.id in managed_ids or r.name in known_names for r in m.roles)
        if m.id in talent_ids or m.id in linked_ids or has_clan_role:
            result.append(m)
    return result


@bot.tree.command(name="check_all",
                  description="Баны и муты за 2 недели у всех игроков с ролью TALENT")
@single_flight("check_all", "⏳ /check_all уже выполняется. Подождите.")
async def check_all(i):
    """Проверяет всех игроков с ролью TALENT на баны и муты за 2 недели.

    Показывает, кто получал блокировку, какого типа, когда, на какой
    срок и кто выдал. Ответ ephemeral: отчёт видит только вызвавший.
    """
    await i.response.defer(ephemeral=True)
    try:
        guild = i.guild
        if guild is None:
            await safe_send(i.followup, "❌ Команда работает только на сервере.",
                            ephemeral=True)
            return
        members = _talent_members(guild)
        if not members:
            await safe_send(i.followup,
                            f"ℹ️ Нет участников с ролью «{CLAN_NAME}».", ephemeral=True)
            return

        since = int(time.time()) - CHECK_ALL_DAYS * 86400
        checked = skipped = errors = 0
        found = []
        for idx, m in enumerate(members, 1):
            sid = await links_repo.get_link(m.id)
            if not sid:
                skipped += 1
            else:
                try:
                    bans, mutes, err = await asyncio.wait_for(
                        api.cs2red_all_blocks(sid), timeout=CMD_API_TIMEOUT)
                    if err:
                        errors += 1
                    else:
                        checked += 1
                        recent = _recent_blocks(bans, mutes, since)
                        if recent:
                            found.append((m, sid, recent))
                except asyncio.TimeoutError:
                    errors += 1
                except Exception as e:
                    errors += 1
                    await log_error(i.client, f"/check_all:{m.id}", e)

            if idx % 5 == 0:
                try:
                    await i.edit_original_response(
                        content=f"⏳ Проверено {idx}/{len(members)}…")
                except Exception:
                    pass
            if idx < len(members):
                await asyncio.sleep(BLOCK_CHECK_DELAY)

        embed = discord.Embed(
            title=f"🚫 Баны и муты за {CHECK_ALL_DAYS} дн. — клан «{CLAN_NAME}»",
            color=0x000000, timestamp=now_utc())
        embed.add_field(name="👥 Игроков с ролью", value=str(len(members)), inline=True)
        embed.add_field(name="✅ Проверено", value=str(checked), inline=True)
        embed.add_field(name="🚫 С блокировками", value=str(len(found)), inline=True)
        embed.add_field(name="🔗 Без SteamID", value=str(skipped), inline=True)
        embed.add_field(name="⚠️ Ошибок", value=str(errors), inline=True)
        if found:
            lines = []
            for m, _sid, blocks in found[:CHECK_ALL_MAX_MEMBERS]:
                for b in blocks[:3]:
                    lines.append(
                        f"• {m.mention} — {b.get('kind') or 'Бан'} · "
                        f"{_fmt_block_ts(b.get('start_ts') or 0)} · "
                        f"{_fmt_block_duration(b.get('start_ts') or 0, b.get('end_ts') or 0)} · "
                        f"{(b.get('reason') or '—')[:60]} · "
                        f"выдал: {b.get('admin') or '—'}")
            if len(found) > CHECK_ALL_MAX_MEMBERS:
                lines.append(f"…и ещё {len(found) - CHECK_ALL_MAX_MEMBERS}")
            embed.add_field(name="Блокировки", value="\n".join(lines)[:1024],
                            inline=False)
        else:
            embed.add_field(
                name="Блокировки",
                value=f"✅ За {CHECK_ALL_DAYS} дн. банов и мутов не найдено",
                inline=False)
        embed.set_footer(text=f"Quantum • {utc_stamp()}")
        await safe_send(i.followup, embed=embed, ephemeral=True)

        # Мини-отчёт в лог-канал: кто получал бан/мут и когда.
        try:
            log_ch = bot.get_channel(CHECK_LOG_CHANNEL_ID) if CHECK_LOG_CHANNEL_ID else None
            if log_ch is not None:
                text = _build_check_log_text(i.user.display_name, checked,
                                             found, skipped, errors)
                await safe_send(log_ch, text,
                                title=f"🚫 /check_all — клан «{CLAN_NAME}»")
        except Exception as e:
            await log_error(bot, "/check_all.log", e)
    except Exception as e:
        await log_error(i.client, "/check_all", e)
        try:
            await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)
        except Exception:
            pass


# ============================================================
# /check_onlineall — недельный онлайн всех игроков с ролью TALENT
# ============================================================
def _fmt_hours(value) -> str:
    """Часы в читаемый вид: «26 ч.» или «20 м.» для значений < 1 часа."""
    if value is None:
        return "—"
    if value < 1:
        return f"{int(round(value * 60))} м."
    return f"{value:.1f} ч."


def _build_onlineall_embed(rows: list, low: list, no_link: list,
                           errors: list, checked: int, total: int) -> discord.Embed:
    """Собирает embed отчёта о недельном онлайне участников клана.

    ``rows`` — список кортежей (member, data) для тех, кто в норме;
    ``low``  — то же для тех, у кого онлайн ниже порога.
    """
    embed = discord.Embed(
        title=f"📊 Недельный онлайн — клан «{CLAN_NAME}»",
        color=0x000000, timestamp=now_utc())
    embed.add_field(name="👥 Игроков с ролью", value=str(total), inline=True)
    embed.add_field(name="✅ Проверено", value=str(checked), inline=True)
    embed.add_field(name=f"⚠️ Онлайн < {ONLINE_MIN_HOURS:g} ч.",
                    value=str(len(low)), inline=True)

    if low:
        lines = []
        for m, data in low[:CHECK_ALL_MAX_MEMBERS]:
            w = (data.get("week") or {}).get("total")
            p = (data.get("prev") or {}).get("total")
            o = (data.get("week") or {}).get("observer")
            obs = f", наблюд. {_fmt_hours(o)}" if o else ""
            lines.append(f"• {m.mention} — сейчас {_fmt_hours(w)}, "
                         f"прошлая {_fmt_hours(p)}{obs}")
        if len(low) > CHECK_ALL_MAX_MEMBERS:
            lines.append(f"…и ещё {len(low) - CHECK_ALL_MAX_MEMBERS}")
        embed.add_field(name="⚠️ Малоактивные", value="\n".join(lines)[:1024],
                        inline=False)
    else:
        embed.add_field(name="⚠️ Малоактивные",
                        value=f"✅ У всех онлайн ≥ {ONLINE_MIN_HOURS:g} ч.",
                        inline=False)

    if rows:
        lines = []
        for m, data in rows[:CHECK_ALL_MAX_MEMBERS]:
            w = (data.get("week") or {}).get("total")
            p = (data.get("prev") or {}).get("total")
            o = (data.get("week") or {}).get("observer")
            obs = f", наблюд. {_fmt_hours(o)}" if o else ""
            lines.append(f"• {m.display_name} — {_fmt_hours(w)} "
                         f"(прошлая {_fmt_hours(p)}{obs})")
        if len(rows) > CHECK_ALL_MAX_MEMBERS:
            lines.append(f"…и ещё {len(rows) - CHECK_ALL_MAX_MEMBERS}")
        embed.add_field(name="✅ В норме", value="\n".join(lines)[:1024], inline=False)

    if no_link:
        embed.add_field(name="🔗 Без /link",
                        value=", ".join(m.display_name for m in no_link[:20])[:1024],
                        inline=False)
    if errors:
        lines = [f"• {m.display_name} — {txt[:80]}" for m, txt in errors[:10]]
        embed.add_field(name="⚠️ Не удалось проверить",
                        value="\n".join(lines)[:1024], inline=False)

    embed.set_footer(text=f"Quantum • {utc_stamp()}")
    return embed


async def collect_online_stats(members: list, progress=None):
    """Обходит участников и собирает их недельный онлайн.

    Возвращает ``(rows, low, no_link, errors, checked)``. Пауза
    ``BLOCK_CHECK_DELAY`` между игроками — требование cs2red.
    """
    rows, low, no_link, errors = [], [], [], []
    checked = 0
    for idx, m in enumerate(members, 1):
        sid = await links_repo.get_link(m.id)
        if not sid:
            no_link.append(m)
        else:
            try:
                data, err = await asyncio.wait_for(
                    api.cs2red_activity(sid), timeout=CMD_API_TIMEOUT)
                if err:
                    errors.append((m, err))
                else:
                    checked += 1
                    week = (data.get("week") or {}).get("total")
                    if week is not None and week < ONLINE_MIN_HOURS:
                        low.append((m, data))
                    else:
                        rows.append((m, data))
            except asyncio.TimeoutError:
                errors.append((m, "таймаут"))
            except Exception as e:
                errors.append((m, str(e)[:80]))
                await log_error(bot, f"/check_onlineall:{m.id}", e)

        if progress:
            try:
                await progress(idx, len(members))
            except Exception:
                pass
        if idx < len(members):
            await asyncio.sleep(BLOCK_CHECK_DELAY)

    # Малоактивные — сверху, по возрастанию онлайна.
    low.sort(key=lambda x: (x[1].get("week") or {}).get("total") or 0)
    rows.sort(key=lambda x: (x[1].get("week") or {}).get("total") or 0,
              reverse=True)
    return rows, low, no_link, errors, checked


@bot.tree.command(name="check_onlineall",
                  description="Недельный онлайн игроков клана TALENT (кто < 9 ч.)")
@single_flight("check_onlineall",
               "⏳ /check_onlineall уже выполняется. Подождите.")
async def check_onlineall(i):
    """Проверяет недельный онлайн всех игроков с ролью TALENT.

    Для каждого берётся JSON админ-профиля cs2red (``user.Activity``):
    онлайн за текущую и прошлую неделю и время наблюдателя. Кто наиграл
    меньше ``ONLINE_MIN_HOURS`` (по умолчанию 9 ч.) — выводится отдельным
    полем. Ответ ephemeral.
    """
    await i.response.defer(ephemeral=True)
    try:
        guild = i.guild
        if guild is None:
            await safe_send(i.followup, "❌ Команда работает только на сервере.",
                            ephemeral=True)
            return
        if not CS2RED_COOKIES:
            await safe_send(i.followup,
                            "❌ Нет cookie cs2red (CS2RED_COOKIE_STRING) — "
                            "профиль активности недоступен.", ephemeral=True)
            return
        members = _talent_members(guild)
        if not members:
            await safe_send(i.followup,
                            f"ℹ️ Нет участников с ролью «{CLAN_NAME}».",
                            ephemeral=True)
            return

        async def progress(done, total):
            if done % 5 == 0:
                try:
                    await i.edit_original_response(
                        content=f"⏳ Проверено {done}/{total}…")
                except Exception:
                    pass

        rows, low, no_link, errors, checked = await collect_online_stats(
            members, progress=progress)
        embed = _build_onlineall_embed(rows, low, no_link, errors,
                                       checked, len(members))
        await safe_send(i.followup, embed=embed, ephemeral=True)

        # Итог в лог-канал: сколько малоактивных и кто.
        try:
            log_ch = bot.get_channel(CHECK_LOG_CHANNEL_ID) if CHECK_LOG_CHANNEL_ID else None
            if log_ch is not None:
                who = ", ".join(m.mention for m, _ in low[:20]) or "нет"
                await safe_send(
                    log_ch,
                    f"📊 /check_onlineall от {i.user.display_name}\n"
                    f"Проверено: {checked}/{len(members)}, "
                    f"< {ONLINE_MIN_HOURS:g} ч.: {len(low)}\n{who}",
                    title="📊 /check_onlineall")
        except Exception as e:
            await log_error(bot, "/check_onlineall.log", e)
    except Exception as e:
        await log_error(i.client, "/check_onlineall", e)
        try:
            await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)
        except Exception:
            pass


# ============================================================
# /activity <steam_id> — активность одного игрока (вкладка «Активность»)
# ============================================================
def _build_activity_embed(steam_id: str, data: dict) -> discord.Embed:
    """Embed с активностью одного игрока за текущую/прошлую неделю."""
    nick = data.get("nick") or "—"
    internal_id = data.get("internal_id") or "?"
    embed = discord.Embed(
        title=f"📊 Активность: {nick} (ID: {internal_id})",
        color=0x000000, timestamp=now_utc())
    embed.add_field(name="SteamID", value=f"`{steam_id}`", inline=False)

    week = data.get("week") or {}
    prev = data.get("prev") or {}
    embed.add_field(
        name="Текущая неделя",
        value=f"Время: `{_fmt_hours(week.get('total'))}`\n"
              f"Наблюдатель: `{_fmt_hours(week.get('observer'))}`",
        inline=True)
    embed.add_field(
        name="Прошлая неделя",
        value=f"Время: `{_fmt_hours(prev.get('total'))}`\n"
              f"Наблюдатель: `{_fmt_hours(prev.get('observer'))}`",
        inline=True)
    rep = data.get("reports") or {}
    embed.add_field(
        name="Репорты",
        value=f"Неделя: `{rep.get('week') if rep.get('week') is not None else '—'}`\n"
              f"Прошлая: `{rep.get('prev') if rep.get('prev') is not None else '—'}`\n"
              f"Всего: `{rep.get('total') if rep.get('total') is not None else '—'}`",
        inline=True)

    modes = data.get("modes") or []
    if modes:
        lines = []
        for mode in modes[:8]:
            name = str(mode.get("name") or "Режим")[:40]
            obs = mode.get("observer")
            extra = f", наблюд. {_fmt_hours(obs)}" if obs else ""
            lines.append(f"• {name}: `{_fmt_hours(mode.get('week'))}` "
                         f"(прошлая `{_fmt_hours(mode.get('prev'))}`{extra})")
        if len(modes) > 8:
            lines.append(f"…и ещё {len(modes) - 8}")
        embed.add_field(name="По режимам", value="\n".join(lines)[:1024],
                        inline=False)

    wk = week.get("total")
    if wk is not None:
        note = ("⚠️ Ниже нормы" if wk < ONLINE_MIN_HOURS else "✅ В норме")
        embed.add_field(
            name="Итог",
            value=f"{note} (порог {ONLINE_MIN_HOURS:g} ч.)", inline=False)
    embed.set_footer(text=f"Quantum • {utc_stamp()}")
    return embed


@bot.tree.command(name="activity",
                  description="Активность игрока на cs2red по SteamID")
async def activity(i, steam_id: str):
    """Показывает недельную активность игрока (эта и прошлая неделя).

    ``steam_id`` — SteamID64 (17 цифр). Данные берутся из JSON
    админ-профиля cs2red (``user.Activity``). Ответ ephemeral.
    """
    await i.response.defer(ephemeral=True)
    try:
        steam_id = (steam_id or "").strip()
        if not (steam_id.isdigit() and len(steam_id) == 17):
            await safe_send(i.followup,
                            "❌ Укажите SteamID64 (17 цифр).", ephemeral=True)
            return
        if not CS2RED_COOKIES:
            await safe_send(i.followup,
                            "❌ Нет cookie cs2red (CS2RED_COOKIE_STRING).",
                            ephemeral=True)
            return
        try:
            data, err = await asyncio.wait_for(
                api.cs2red_activity(steam_id), timeout=CMD_API_TIMEOUT)
        except asyncio.TimeoutError:
            data, err = None, "таймаут"
        if err or not data:
            if err == "не найден":
                await safe_send(i.followup, "❌ Игрок не найден.", ephemeral=True)
            else:
                await safe_send(i.followup,
                                f"❌ Не удалось получить активность: {err}",
                                ephemeral=True)
            return
        await safe_send(i.followup, embed=_build_activity_embed(steam_id, data),
                        ephemeral=True)
    except Exception as e:
        await log_error(i.client, "/activity", e)
        try:
            await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)
        except Exception:
            pass


# ============================================================
# /check_clan — проверка ролей клана у участников
# ============================================================
def _member_clan_roles(guild, member, known_names: set) -> list:
    """Роли участника, которые являются ролями клана."""
    managed_ids = set(CLAN_ROLE_MAP.values())
    return [r for r in member.roles
            if r.id in managed_ids or r.name in known_names]


async def _collect_clan_role_issues(guild, members: list) -> dict:
    """Собирает проблемы с ролями клана у участников.

    Возвращает словарь со списками:
      extra     — (member, role, лишние роли) у кого больше одной роли клана;
      unlinked  — участники с ролью клана, но без привязки /link;
      missing   — привязан, в клане, но роли клана нет;
      wrong     — привязан, роль есть, но не та (имя ≠ клана на cs2red);
      errors    — (member, текст) не удалось проверить.
    """
    known_names = _load_clan_role_names()
    issues = {"extra": [], "unlinked": [], "missing": [],
              "wrong": [], "errors": [], "checked": 0}
    for m in members:
        sid = await links_repo.get_link(m.id)
        held = _member_clan_roles(guild, m, known_names)
        if not sid:
            if held:
                issues["unlinked"].append((m, [r.name for r in held]))
            continue
        try:
            user = await api.cs2red_user(sid)
            if user is None:
                issues["errors"].append((m, "cs2red не ответил"))
                continue
            if user.get("_not_found") or not user.get("id"):
                issues["errors"].append((m, "игрок не найден на cs2red"))
                continue
            clan, clan_err = await api.player_clan_checked(sid, user=user)
            if clan_err:
                issues["errors"].append((m, clan_err))
                continue
            _remember_clan_role_name(
                (clan or {}).get("clan_name") or "")
        except Exception as e:
            issues["errors"].append((m, f"ошибка: {str(e)[:80]}"))
            continue

        issues["checked"] += 1
        clan_name = ((clan or {}).get("clan_name") or "").strip()
        if not clan:
            if held:
                issues["wrong"].append((m, [r.name for r in held], None))
        elif len(held) > 1:
            issues["extra"].append((m, [r.name for r in held], clan_name))
        elif not held:
            issues["missing"].append((m, clan_name))
        elif held[0].name != clan_name:
            issues["wrong"].append((m, [r.name for r in held], clan_name))
    return issues


def _build_check_clan_embed(issues: dict, total: int) -> discord.Embed:
    """Embed с результатами /check_clan."""
    extra = issues["extra"]
    unlinked = issues["unlinked"]
    missing = issues["missing"]
    wrong = issues["wrong"]
    errors = issues["errors"]

    embed = discord.Embed(
        title="🏰 Проверка ролей кланов",
        color=0x000000, timestamp=now_utc())
    embed.add_field(name="👥 Участников в проверке", value=str(total), inline=True)
    embed.add_field(name="✅ Проверено", value=str(issues["checked"]), inline=True)
    embed.add_field(name="⚠️ Проблем", value=str(len(extra) + len(missing)
                                                  + len(wrong)), inline=True)

    def fmt(rows, tail="") -> str:
        out = []
        for row in rows[:10]:
            m = row[0]
            if len(row) == 3 and row[2]:
                out.append(f"• {m.mention} — {', '.join(row[1])} → **{row[2]}**")
            elif len(row) == 3:
                out.append(f"• {m.mention} — {', '.join(row[1])} (нет клана)")
            elif len(row) == 2 and isinstance(row[1], list):
                out.append(f"• {m.mention} — {', '.join(row[1])}")
            else:
                out.append(f"• {m.mention} — {row[1]}")
        if len(rows) > 10:
            out.append(f"…и ещё {len(rows) - 10}")
        return ("\n".join(out) if out else "✅ Нет") + tail

    embed.add_field(name=f"🔀 Несколько ролей клана ({len(extra)})",
                    value=fmt(extra)[:1024], inline=False)
    embed.add_field(name=f"🔗 Без /link, но с ролью ({len(unlinked)})",
                    value=fmt(unlinked)[:1024], inline=False)
    embed.add_field(name=f"🚫 Роль есть, но не в клане ({len(wrong)})",
                    value=fmt(wrong)[:1024], inline=False)
    embed.add_field(name=f"➕ В клане без роли ({len(missing)})",
                    value=fmt(missing)[:1024], inline=False)
    if errors:
        embed.add_field(name=f"⚠️ Ошибки проверки ({len(errors)})",
                        value=fmt(errors)[:1024], inline=False)
    embed.set_footer(text=f"Quantum • {utc_stamp()}")
    return embed


async def sync_clan_roles_for_members(guild, members: list) -> list:
    """Прогоняет sync_clan_role_by_id по списку и возвращает результаты."""
    results = []
    for m in members:
        sid = await links_repo.get_link(m.id)
        if not sid:
            continue
        try:
            result = await sync_clan_role_by_id(guild, m, sid)
        except Exception as e:
            result = f"⚠️ ошибка синхронизации: {str(e)[:150]}"
        results.append((m, result))
        await asyncio.sleep(api.RATE_LIMIT_SECONDS)
    return results


class CheckClanView(discord.ui.View):
    """Кнопки под отчётом /check_clan: синхронизация и очистка ролей."""

    def __init__(self):
        super().__init__(timeout=300)

    async def _guard(self, i) -> bool:
        if not i.user.guild_permissions.administrator:
            await safe_send(i.followup, "❌ Нужны права администратора.", ephemeral=True)
            return False
        if i.guild is None:
            await safe_send(i.followup, "❌ Только на сервере.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔄 Синхронизировать роли",
                       style=discord.ButtonStyle.primary)
    async def sync(self, i, _b):
        """Снимает старые роли клана и выдаёт актуальные по cs2red."""
        await i.response.defer(ephemeral=True)
        if not await self._guard(i):
            return
        members = await _clan_sync_members(i.guild)
        results = await sync_clan_roles_for_members(i.guild, members)
        changed = [r for _m, r in results
                   if r.startswith("🔄") or "выдан" in r]
        problems = [(m, r) for m, r in results
                    if r.startswith(("⚠️", "❌"))]
        text = (f"🔄 Синхронизировано участников: **{len(results)}**\n"
                f"Изменений ролей: **{len(changed)}**")
        if problems:
            text += "\n⚠️ " + "\n⚠️ ".join(
                f"{m.display_name}: {r}" for m, r in problems[:10])
        await safe_send(i.followup, text, ephemeral=True)
        await audit_action(
            i.client, "Синхронизация ролей клана", actor=i.user,
            details=(f"Участников: {len(results)}\n"
                     f"Изменений: {len(changed)}\n"
                     f"Проблем: {len(problems)}"))

    @discord.ui.button(label="🧹 Очистить лишние роли",
                       style=discord.ButtonStyle.danger)
    async def clean(self, i, _b):
        await i.response.defer(ephemeral=True)
        if not await self._guard(i):
            return
        guild = i.guild
        members = await _clan_sync_members(guild)
        known_names = _load_clan_role_names()
        removed_total = 0
        problems = []
        for m in members:
            held = _member_clan_roles(guild, m, known_names)
            if len(held) < 2:
                continue
            sid = await links_repo.get_link(m.id)
            keep = None
            if sid:
                try:
                    user = await api.cs2red_user(sid)
                    clan, err = await api.player_clan_checked(sid, user=user) \
                        if user and not user.get("_not_found") else (None, "")
                    if not err and clan:
                        keep = (clan.get("clan_name") or "").strip()
                        _remember_clan_role_name(keep)
                except Exception as e:
                    problems.append(f"{m.display_name}: {str(e)[:60]}")
                    continue
            # Если клан известен — оставляем ровно его роль, иначе снимаем все.
            to_remove = [r for r in held if not keep or r.name != keep]
            if not to_remove:
                continue
            above = _clan_roles_above_bot(guild, to_remove)
            below = [r for r in to_remove if r not in above]
            errs = await _remove_roles_checked(m, below) if below else []
            removed_total += len(below)
            if above:
                errs.append("выше роли бота: " + ", ".join(r.name for r in above))
            if errs:
                problems.append(f"{m.display_name}: " + "; ".join(errs))
            if keep:
                left = [r for r in held if r.name == keep]
                if left:
                    _remember_member_clan_role(m.id, left[0].id)
        text = f"🧹 Снято лишних ролей клана: **{removed_total}**"
        if problems:
            text += "\n⚠️ " + "\n⚠️ ".join(problems[:10])
        await safe_send(i.followup, text, ephemeral=True)
        await audit_action(
            i.client, "Очистка лишних ролей клана", actor=i.user,
            details=(f"Снято ролей: {removed_total}\n"
                     f"Проблем: {len(problems)}"))


@bot.tree.command(name="check_clan",
                  description="Проверить роли клана у всех привязанных через /link")
@single_flight("check_clan", "⏳ /check_clan уже выполняется. Подождите.")
async def check_clan(i):
    """Ищет лишние и устаревшие роли клана, а также отсутствующие.

    Проверяются не только участники с ролью TALENT, но и все, кто
    привязан через /link: игрок мог уйти из TALENT в другой клан, и
    старая роль должна быть снята, а новая — выдана. Отчёт ephemeral +
    кнопки синхронизации и очистки лишних ролей.
    """
    await i.response.defer(ephemeral=True)
    try:
        guild = i.guild
        if guild is None:
            await safe_send(i.followup, "❌ Команда работает только на сервере.",
                            ephemeral=True)
            return
        members = await _clan_sync_members(guild)
        if not members:
            await safe_send(
                i.followup,
                "ℹ️ Нет привязанных участников и участников с ролью клана.",
                ephemeral=True)
            return

        issues = await _collect_clan_role_issues(guild, members)
        embed = _build_check_clan_embed(issues, len(members))
        has_issues = bool(issues["extra"] or issues["missing"]
                          or issues["wrong"] or issues["unlinked"])
        view = CheckClanView() if has_issues else None
        await safe_send(i.followup, embed=embed, view=view, ephemeral=True)
    except Exception as e:
        await log_error(i.client, "/check_clan", e)
        try:
            await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)
        except Exception:
            pass


# ============================================================
# /clan_info — профиль клана одним embed
# ============================================================
async def _build_clan_info_embed(guild) -> discord.Embed:
    """Собирает профиль клана: состав на cs2red, роли в Discord, проблемы."""
    members = _talent_members(guild)
    embed = discord.Embed(
        title=f"🏰 Клан «{CLAN_NAME}»",
        color=0x000000, timestamp=now_utc())

    # Данные cs2red: состав клана и топ по времени на серверах.
    clans = await api.cs2red_clans()
    on_site = None
    if clans:
        for c in clans:
            if (c.get("name") or "").strip().lower() == CLAN_NAME.lower():
                on_site = c
                break
    if on_site is not None:
        roster = on_site.get("members") or []
        embed.add_field(name="👥 Состав на cs2red", value=str(len(roster)), inline=True)
        tags = on_site.get("tag") or "—"
        embed.add_field(name="🏷 Тег", value=tags, inline=True)
    else:
        embed.add_field(name="👥 Состав на cs2red",
                        value="не удалось получить", inline=True)

    # Discord: сколько с ролью клана и сколько без привязки.
    linked = await links_repo.get_all_links()
    linked_ids = {int(k) for k in linked if str(k).isdigit()}
    no_link = [m for m in members if m.id not in linked_ids]
    embed.add_field(name="🎭 С ролью в Discord", value=str(len(members)), inline=True)
    embed.add_field(name="🔗 Без /link", value=str(len(no_link)), inline=True)

    # Разбивка по кланам из cs2red, если данные есть.
    if members:
        checked = 0
        in_clan = 0
        for m in members[:30]:  # ограничиваем: у cs2red есть лимиты запросов
            sid = await links_repo.get_link(m.id)
            if not sid:
                continue
            try:
                user = await api.cs2red_user(sid)
                clan, err = await api.player_clan_checked(sid, user=user)
            except Exception:
                continue
            if err:
                continue
            checked += 1
            if clan:
                in_clan += 1
            await asyncio.sleep(REQUEST_DELAY)
        embed.add_field(name="✅ Подтверждено в клане",
                        value=f"{in_clan} из {checked}", inline=True)

    url = CLAN_URL if CLAN_URL else "—"
    embed.add_field(name="🔗 Ссылка", value=url, inline=False)
    embed.set_footer(text=f"Quantum • {utc_stamp()}")
    return embed


@bot.tree.command(name="clan_info", description="Профиль клана: состав, роли, привязки")
async def clan_info(i):
    await i.response.defer(ephemeral=True)
    try:
        if i.guild is None:
            await safe_send(i.followup, "❌ Команда работает только на сервере.",
                            ephemeral=True)
            return
        embed = await _build_clan_info_embed(i.guild)
        await safe_send(i.followup, embed=embed, ephemeral=True)
    except Exception as e:
        await log_error(i.client, "/clan_info", e)
        try:
            await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)
        except Exception:
            pass


# ============================================================
# /ping
# ============================================================
# --- /help: короткие описания и разделы по реальным правам ---
HELP_DESCRIPTIONS = {
    "link": "привязать свой SteamID",
    "unlink": "отвязать SteamID",
    "lookup": "информация об игроке: SteamID, клан, активность",
    "clan_info": "состав клана, роли и привязки",
    "activity": "активность игрока на cs2red",
    "rules": "правила клана",
    "ask": "вопрос по правилам (ИИ)",
    "ping": "проверить, что бот жив",
    "help": "эта справка",
    "search": "история заявок игрока в клан",
    "check": "заявки, которые взял модератор",
    "status": "статистика модератора по заявкам",
    "check_bans": "баны и муты игрока",
    "check_mutes": "муты игрока",
    "check_all": "баны и муты у всех с ролью TALENT",
    "check_onlineall": "недельный онлайн клана TALENT",
    "check_clan": "роли клана у всех привязанных через /link",
    "moders": "панель модераторов: права и приоритет",
    "admins": "администраторы и модераторы клана",
    "health": "состояние всех систем бота",
    "player_info": "карточка игрока из базы",
    "activity_show": "показать активность игрока",
    "activity_all": "активность по роли",
    "take": "забрать доступ модератора",
    "give": "выдать доступ модератора",
    "panel": "панель управления ботом",
    "sync_all": "синхронизировать роли всех привязанных",
    "auto_link": "авто-привязка по никам",
    "link_all": "массовая привязка из базы",
    "export_members": "выгрузить привязки в mass_links.json",
    "admlink": "привязать SteamID другому игроку",
    "clan_roleclear": "удалить роли кланов без участников",
    "player_list": "вся база игроков",
    "player_note": "заметка к профилю игрока",
    "audit": "журнал админских действий",
    "activity_set": "установить часы активности",
    "activity_clear": "сбросить активность",
    "ai_clear_cache": "очистить кэш ответов ИИ",
}

HELP_SECTION_TITLES = {
    "player": "👤 Игроку",
    "mod": "🛡 Модератору",
    "admin": "⚙️ Админу",
}


def _help_command_section(cmd) -> str:
    """Раздел справки по фактическим правам команды."""
    if cmd.name in PUBLIC_COMMANDS:
        return "player"
    perms = getattr(cmd, "default_permissions", None)
    if cmd.name in ADMIN_ONLY_COMMANDS or getattr(perms, "administrator", False):
        return "admin"
    return "mod"


def _help_lines() -> dict:
    """Строки справки по разделам (все команды дерева + текстовые)."""
    out = {"player": [], "mod": [], "admin": []}
    for cmd in sorted(bot.tree.get_commands(), key=lambda c: c.name):
        desc = HELP_DESCRIPTIONS.get(cmd.name) or (cmd.description or "").strip()
        out[_help_command_section(cmd)].append(f"`/{cmd.name}` — {desc}")
    out["admin"].append("`!setup_clan` — первичная настройка клана (текст)")
    return out


def _split_field(lines: list, limit: int = 1000) -> list:
    """Режет список строк на части под лимит поля Embed (1024)."""
    chunks: list = []
    cur = ""
    for line in lines:
        piece = line if not cur else "\n" + line
        if cur and len(cur) + len(piece) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur += piece
    if cur:
        chunks.append(cur)
    return chunks or ["—"]


def _build_help_embed() -> discord.Embed:
    e = discord.Embed(
        title="📖 Команды TBOT",
        color=0x000000,
        timestamp=now_utc(),
        description=("Справка по разделам: что доступно игроку, модератору и админу.\n"
                     "Модераторам доступны и команды игрока."),
    )
    lines = _help_lines()
    for key in ("player", "mod", "admin"):
        title = HELP_SECTION_TITLES[key]
        for idx, chunk in enumerate(_split_field(lines[key])):
            e.add_field(name=title if idx == 0 else f"{title} ({idx + 1})",
                        value=chunk, inline=False)
    e.add_field(
        name="⌨️ Текстовые команды",
        value=("`!setup_clan` — настройка клана (админ)\n"
               "`!ask`, `!rules`, `!ai_clear_cache` — те же, что слэш-команды"),
        inline=False,
    )
    e.set_footer(text="Quantum • /help")
    return e


@bot.tree.command(name="help", description="Список всех команд бота по разделам")
async def help_command(i):
    await i.response.send_message(embed=_build_help_embed(), ephemeral=True)


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
    e = discord.Embed(title="🏓 Pong!", color=0x000000, timestamp=now_utc())
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

    for name, repo in [
        ("links.json", links_repo),
        ("players_db.json", players_repo),
        ("users_db.json", users_repo),
        ("moderators.json", moderation_repo),
        ("votes.json", votes_repo),
        ("seen.json", seen_repo),
    ]:
        try:
            # count_all() возвращает int — len() к нему неприменим, поэтому
            # раньше в отчёте всегда было «записей: ?».
            size = await repo.count_all()
            add("📁", f"Файл `{name}`", f"записей: `{size}`", True)
        except Exception as e:
            add("📁", f"Файл `{name}`", f"❌ {str(e)[:200]}", False)

    mass_file = data_path("mass_links.json")
    if mass_file.exists():
        try:
            mass = await asyncio.to_thread(_read_json_file, mass_file)
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
        # mass_links.json — это выгрузка /export_members, а не хранилище
        # привязок (они в SQLite). Пока экспорт не делали, файла нет — это
        # штатная ситуация, а не проблема.
        add("📁", "Файл `mass_links.json`",
            "ℹ️ не создан (создаётся командой `/export_members`)", True)

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
        providers = ai_service.providers
        any_ai_ok = False
        for prov in providers:
            ok = prov.last_error == ""
            if ok:
                any_ai_ok = True
            add("🤖", f"AI ({prov.name.title()})", prov.status_line(), ok)
        # Не настроенные провайдеры (без ключа) не попадают в список.
        # Это НЕ проблема, если хотя бы один провайдер настроен и работает:
        # каскад просто использует доступный. Красный флаг ставим только
        # когда ИИ не настроен вообще.
        configured = {p.name for p in providers}
        missing = [n for n in ("gemini", "openrouter", "llm7")
                   if n not in configured]
        if missing:
            names = ", ".join(n.title() for n in missing)
            add("🤖", "AI (резерв)",
                f"ℹ️ не настроены: {names} (нужны ключи в .env)",
                any_ai_ok)
        if not providers:
            add("🤖", "AI-каскад", "❌ ни один ИИ-провайдер не настроен", False)
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
        title = "✅ Все системы работают"
    elif ok_count >= fail_count:
        title = f"⚠️ Работает: {ok_count}/{total_checks}, проблем: {fail_count}"
    else:
        title = f"❌ Проблем много: {fail_count} из {total_checks}"

    embed = discord.Embed(
        title=f"🩺 Проверка систем бота — {title}",
        description=(f"**Итог:** ✅ `{ok_count}` работает / ❌ `{fail_count}` проблем\n"
                     f"**Всего проверок:** `{total_checks}`"),
        color=0x000000, timestamp=now_utc(),
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
def _moderator_permissions_text(commands) -> str:
    """Короткое описание прав модератора для /give и уведомлений."""
    if commands is None:
        return "—"
    if "*" in commands:
        return "все команды"
    if not commands:
        return "нет команд"
    return ", ".join(f"`/{c}`" for c in commands)


def _moderator_priority(commands) -> dict:
    """Права модератора: сколько выдано, уровень и бейдж для /moders."""
    total = len(MODERATOR_COMMANDS)
    if commands is None:
        granted, wildcard = 0, False
    else:
        wildcard = "*" in commands
        granted = (total if wildcard
                   else len(set(commands) & set(MODERATOR_COMMANDS)))
    if wildcard or granted >= total:
        return {"granted": total, "rank": 4, "badge": "👑",
                "label": "Полный доступ"}
    if granted >= max(1, round(total * 2 / 3)):
        return {"granted": granted, "rank": 3, "badge": "🛡",
                "label": "Высокий"}
    if granted >= max(1, round(total / 3)):
        return {"granted": granted, "rank": 2, "badge": "🔹",
                "label": "Средний"}
    if granted > 0:
        return {"granted": granted, "rank": 1, "badge": "🔸",
                "label": "Базовый"}
    return {"granted": 0, "rank": 0, "badge": "⛔", "label": "Без прав"}


# 3 inline-поля на одного модератора: 8 * 3 = 24 (лимит Discord — 25).
MODERATORS_PER_EMBED = 8


async def _collect_moderator_rows(guild) -> list:
    """Собирает данные для панели /moders: права, Discord и cs2red."""
    rows = []
    for discord_id in await moderation_repo.get_all_moderators():
        uid = str(discord_id)
        commands = await moderation_repo.get_moderator_commands(uid)
        priority = _moderator_priority(commands)
        member = (guild.get_member(int(uid))
                  if guild is not None and uid.isdigit() else None)
        user = bot.get_user(int(uid)) if uid.isdigit() else None
        target = member or user
        name = target.display_name if target is not None else uid
        mention = target.mention if target is not None else f"`{uid}`"

        steam_id = await links_repo.get_link(uid)
        cs2red = "❌ не привязан"
        if steam_id:
            url = f"https://cs2red.ru/profile/{steam_id}"
            cached = await players_repo.get_player(steam_id)
            nick = str(cached.get("cs2red_nick")
                       or cached.get("steam_nick") or "").strip()
            if nick:
                safe_nick = " ".join(nick.split())
                safe_nick = (safe_nick.replace("[", "").replace("]", "")
                             .replace("(", "").replace(")", "")[:40])
                cs2red = f"[{safe_nick}]({url})"
            else:
                cs2red = f"[{steam_id}]({url})"

        rows.append({
            "id": uid,
            "name": discord.utils.escape_markdown(str(name))[:200],
            "mention": mention,
            "steam_id": steam_id,
            "cs2red": cs2red,
            "total": len(MODERATOR_COMMANDS),
            **priority,
        })
    rows.sort(key=lambda row: (-row["rank"], row["name"].casefold()))
    return rows


def _build_moderator_embeds(rows: list) -> list:
    """Чёрные embed'ы с таблицей: модератор | права | cs2red."""
    total = len(rows)
    chunks = [rows[i:i + MODERATORS_PER_EMBED]
              for i in range(0, total, MODERATORS_PER_EMBED)]
    counts = {rank: 0 for rank in range(5)}
    for row in rows:
        counts[row["rank"]] += 1
    linked = sum(1 for row in rows if row["steam_id"])

    embeds = []
    for page, chunk in enumerate(chunks, 1):
        title = f"🛠 Модераторы — {total}"
        if len(chunks) > 1:
            title += f" ({page}/{len(chunks)})"
        if page == 1:
            description = (
                f"Всего: **{total}** · 👑 **{counts[4]}** · "
                f"🛡 **{counts[3]}** · 🔹 **{counts[2]}** · "
                f"🔸 **{counts[1]}** · ⛔ **{counts[0]}**\n"
                f"С профилем cs2red: **{linked}** из **{total}**\n"
                "Легенда: 👑 полный · 🛡 высокий · 🔹 средний · "
                "🔸 базовый · ⛔ без прав")
        else:
            description = f"Часть **{page}** из **{len(chunks)}**"

        embed = discord.Embed(title=title, description=description,
                              color=0x000000, timestamp=now_utc())
        for row in chunk:
            embed.add_field(
                name=f"{row['badge']} {row['name']}"[:256],
                value=row["mention"][:1024],
                inline=True)
            embed.add_field(
                name=f"📊 {row['granted']}/{row['total']}"[:256],
                value=(f"**{row['label']}**\n"
                       f"Приоритет: **{row['rank']}/4**")[:1024],
                inline=True)
            embed.add_field(name="🔗 cs2red.ru", value=row["cs2red"][:1024],
                            inline=True)

        if bot.user:
            embed.set_thumbnail(url=bot.user.display_avatar.url)
            embed.set_footer(text=f"Quantum • {utc_stamp()} (UTC)",
                             icon_url=bot.user.display_avatar.url)
        else:
            embed.set_footer(text=f"Quantum • {utc_stamp()} (UTC)")
        embeds.append(embed)
    return embeds


@bot.tree.command(name="moders", description="Панель модераторов: права, приоритет, cs2red")
async def moders(i):
    await i.response.defer(ephemeral=True)
    try:
        rows = await _collect_moderator_rows(i.guild)
        if not rows:
            await safe_send(i.followup, "ℹ️ Пусто", ephemeral=True)
            return
        await safe_send(i.followup, embeds=_build_moderator_embeds(rows),
                        ephemeral=True)
    except Exception as e:
        await log_error(i.client, "/moders", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


class GiveUserSelect(discord.ui.UserSelect):
    """Выбор участника, которому выдаём доступ."""

    def __init__(self, panel):
        super().__init__(
            placeholder="Выберите участника",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.panel = panel

    async def callback(self, i):
        await self.panel.pick_user(i, self.values[0])


class GiveCommandsSelect(discord.ui.Select):
    """Мульти-выбор команд, доступных выбранному модератору."""

    def __init__(self, panel):
        options = []
        for name in MODERATOR_COMMANDS:
            cmd = bot.tree.get_command(name)
            desc = (getattr(cmd, "description", "") or "").strip()
            options.append(discord.SelectOption(
                label=f"/{name}"[:100],
                value=name,
                description=desc[:100] or None,
                default=name in panel.selected,
            ))
        super().__init__(
            placeholder="Команды, которые можно использовать",
            min_values=0,
            max_values=len(options),
            options=options,
            row=1,
        )
        self.panel = panel

    async def callback(self, i):
        await self.panel.pick_commands(i, self.values)


class GivePanelView(discord.ui.View):
    """Панель /give: участник + команды + «все»/«очистить».

    Панель эфемерная и управляется только тем админом, который её открыл.
    Кнопки «Все права» и «Очистить» применяются сразу, ручной выбор команд
    сохраняется кнопкой «Сохранить».
    """

    def __init__(self, owner_id: int, target=None, permissions=None):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.target = target
        self.is_moderator = permissions is not None
        self.selected = self._selection_from(permissions)
        self.user_select = GiveUserSelect(self)
        self.command_select = GiveCommandsSelect(self)
        self.add_item(self.user_select)
        self.add_item(self.command_select)
        self._refresh()

    @staticmethod
    def _selection_from(permissions) -> set:
        if permissions is None or "*" in permissions:
            return set(MODERATOR_COMMANDS)
        return {c for c in permissions if c in MODERATOR_COMMANDS}

    @property
    def all_selected(self) -> bool:
        return self.selected >= set(MODERATOR_COMMANDS)

    async def _guard(self, i) -> bool:
        if i.user.id != self.owner_id:
            await i.response.send_message(
                "❌ Это меню открыл другой администратор.", ephemeral=True)
            return False
        perms = getattr(i.user, "guild_permissions", None)
        if not (perms and perms.administrator):
            await i.response.send_message("❌ Нужны права администратора.",
                                          ephemeral=True)
            return False
        return True

    async def _fail(self, i, exc):
        """Ошибка панели: пишем в лог и отвечаем админу, не роняя interaction."""
        await log_error(i.client, "/give", exc)
        try:
            if i.response.is_done():
                await i.followup.send(f"❌ Ошибка: {str(exc)[:200]}",
                                      ephemeral=True)
            else:
                await i.response.send_message(f"❌ Ошибка: {str(exc)[:200]}",
                                              ephemeral=True)
        except Exception:
            pass

    def _refresh(self):
        self.user_select.placeholder = (
            f"Выбран: {self.target.display_name}" if self.target
            else "Выберите участника")[:150]
        for option in self.command_select.options:
            option.default = option.value in self.selected

    def build_embed(self) -> discord.Embed:
        e = discord.Embed(title="🛠 Доступ модератора", color=0x000000,
                          timestamp=now_utc())
        if self.target:
            e.add_field(name="Пользователь",
                        value=f"{self.target.mention}\n`{self.target.id}`",
                        inline=False)
            status = "✅ есть в /moders" if self.is_moderator else "❌ не модератор"
        else:
            e.add_field(name="Пользователь",
                        value="не выбран — выберите в меню ниже",
                        inline=False)
            status = "—"
        e.add_field(name="Статус", value=status, inline=True)
        if self.all_selected:
            commands = "все команды"
        elif not self.selected:
            commands = "нет команд"
        else:
            commands = "\n".join(
                f"• `/{c}`" for c in MODERATOR_COMMANDS if c in self.selected)
        e.add_field(name="Команды", value=commands[:1024], inline=False)
        e.add_field(
            name="Как пользоваться",
            value=("1. Выберите участника.\n"
                   "2. Отметьте команды в меню.\n"
                   "3. Нажмите «💾 Сохранить».\n"
                   "«➕ Все права» и «🧹 Очистить» применяются сразу."),
            inline=False)
        e.set_footer(text=f"Quantum • {utc_stamp()} (UTC)")
        return e

    async def _persist(self) -> bool:
        if self.target is None:
            return False
        if not self.selected:
            await moderation_repo.remove_moderator(self.target.id)
            self.is_moderator = False
        else:
            payload = ["*"] if self.all_selected else sorted(self.selected)
            await moderation_repo.set_moderator_commands(self.target.id, payload)
            self.is_moderator = True
        return True

    async def _edit(self, i):
        self._refresh()
        await i.edit_original_response(embed=self.build_embed(), view=self)

    async def pick_user(self, i, user):
        try:
            if not await self._guard(i):
                return
            if getattr(user, "bot", False):
                await i.response.send_message("❌ Нельзя выдать доступ боту.",
                                              ephemeral=True)
                return
            await i.response.defer()
            self.target = user
            permissions = await moderation_repo.get_moderator_commands(user.id)
            self.is_moderator = permissions is not None
            self.selected = self._selection_from(permissions)
            await self._edit(i)
        except Exception as e:
            await self._fail(i, e)

    async def pick_commands(self, i, values):
        try:
            if not await self._guard(i):
                return
            await i.response.defer()
            self.selected = {v for v in values if v in MODERATOR_COMMANDS}
            await self._edit(i)
        except Exception as e:
            await self._fail(i, e)

    @discord.ui.button(label="💾 Сохранить", style=discord.ButtonStyle.success,
                       row=2)
    async def save(self, i, _button):
        try:
            if not await self._guard(i):
                return
            if self.target is None:
                await i.response.send_message("❌ Сначала выберите участника.",
                                              ephemeral=True)
                return
            await i.response.defer()
            await self._persist()
            await self._edit(i)
            perms = (["*"] if self.all_selected else sorted(self.selected))
            await log_to_channel(
                i.client,
                f"🛠 /give: {i.user.mention} → {self.target.mention} — "
                f"{_moderator_permissions_text(perms)}")
            await audit_action(
                i.client, "Изменение прав модератора", actor=i.user,
                target=self.target,
                details=_moderator_permissions_text(perms))
        except Exception as e:
            await self._fail(i, e)

    @discord.ui.button(label="➕ Все права", style=discord.ButtonStyle.primary,
                       row=2)
    async def all_rights(self, i, _button):
        try:
            if not await self._guard(i):
                return
            await i.response.defer()
            self.selected = set(MODERATOR_COMMANDS)
            saved = await self._persist()
            await self._edit(i)
            if saved:
                await log_to_channel(
                    i.client,
                    f"🛠 /give: {i.user.mention} выдал {self.target.mention} "
                    f"все права")
                await audit_action(i.client, "Выданы все права", actor=i.user,
                                   target=self.target,
                                   details="Все команды модератора")
        except Exception as e:
            await self._fail(i, e)

    @discord.ui.button(label="🧹 Очистить", style=discord.ButtonStyle.danger,
                       row=2)
    async def clear(self, i, _button):
        try:
            if not await self._guard(i):
                return
            await i.response.defer()
            self.selected = set()
            saved = await self._persist()
            await self._edit(i)
            if saved:
                await log_to_channel(
                    i.client,
                    f"🧹 /give: {i.user.mention} снял все права с "
                    f"{self.target.mention}")
                await audit_action(i.client, "Сняты все права", actor=i.user,
                                   target=self.target,
                                   details="Модератор удалён из /moders")
        except Exception as e:
            await self._fail(i, e)

    @discord.ui.button(label="✖ Закрыть", style=discord.ButtonStyle.secondary,
                       row=2)
    async def close(self, i, _button):
        try:
            if not await self._guard(i):
                return
            await i.response.defer()
            self.stop()
            await i.edit_original_response(content="Панель закрыта.", embed=None,
                                           view=None)
        except Exception as e:
            await self._fail(i, e)


@bot.tree.command(name="give", description="Панель доступа модератора")
@app_commands.describe(user="Участник (необязательно — можно выбрать в панели)")
@app_commands.default_permissions(administrator=True)
async def give(i, user: discord.Member | None = None):
    await i.response.defer(ephemeral=True)
    if not await _admin_guard(i):
        return
    try:
        if user is not None and user.bot:
            await safe_send(i.followup, "❌ Нельзя выдать доступ боту.",
                            ephemeral=True)
            return
        permissions = None
        if user is not None:
            permissions = await moderation_repo.get_moderator_commands(user.id)
        view = GivePanelView(i.user.id, target=user, permissions=permissions)
        await safe_send(i.followup, embed=view.build_embed(), view=view,
                        ephemeral=True)
        await log_to_channel(
            i.client,
            f"🛠 /give: панель открыл {i.user.mention}"
            + (f" для {user.mention}" if user else ""))
    except Exception as e:
        await log_error(i.client, "/give", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="take", description="Забрать доступ")
async def take(i, user: discord.Member):
    if not admin_only(i):
        await i.response.send_message("❌", ephemeral=True)
        return
    await i.response.defer(ephemeral=True)
    try:
        removed = await moderation_repo.remove_moderator(user.id)
        await safe_send(i.followup,
            f"❌ {user.mention} лишён." if removed else "ℹ️ Уже без.",
            ephemeral=True
        )
        if removed:
            await audit_action(i.client, "Снят доступ модератора",
                               actor=i.user, target=user,
                               details="Удалён из /moders")
    except Exception as e:
        await log_error(i.client, "/take", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


# ============================================================
# ЖУРНАЛ АУДИТА (/audit)
# ============================================================
def _audit_line(row: dict) -> str:
    """Одна строка журнала аудита."""
    actor = row.get("actor_id")
    target = row.get("target_id")
    who = f"<@{actor}>" if actor else (row.get("actor_name") or "система")
    line = (f"• `{_fmt_dt(row.get('ts'))}` · **{row.get('action') or '—'}**\n"
            f"  кто: {who}")
    if target:
        line += f" · объект: <@{target}>"
    elif row.get("target_name"):
        line += f" · объект: {row['target_name']}"
    details = (row.get("details") or "").strip()
    if details:
        line += "\n  " + details.replace("\n", " ")[:300]
    return line


@bot.tree.command(name="audit", description="Журнал админских действий")
@app_commands.describe(user="Показать действия участника (кто или над кем)",
                       limit="Сколько записей показать (1–50)")
@app_commands.default_permissions(administrator=True)
async def audit(i, user: discord.Member | None = None, limit: int = 15):
    """Ephemeral-журнал: кто, когда и что сделал.

    Записи пишутся в таблицу ``audit_log`` и дублируются в канал аудита.
    Команда доступна только администраторам: в журнале бывают причины
    отказов и служебные подробности.
    """
    await i.response.defer(ephemeral=True)
    if not await _admin_guard(i):
        return
    try:
        limit = max(1, min(int(limit or 15), 50))
        rows = await audit_repo.list_entries(limit=limit)
        if user is not None:
            by_actor = await audit_repo.list_entries(
                actor_id=user.id, limit=limit)
            by_target = await audit_repo.list_entries(
                target_id=user.id, limit=limit)
            merged = {r["id"]: r for r in (by_actor + by_target)}
            rows = sorted(merged.values(), key=lambda r: r["id"],
                          reverse=True)[:limit]
        if not rows:
            await safe_send(i.followup, "ℹ️ Записей аудита нет.",
                            ephemeral=True)
            return
        title = "🛡 Аудит действий"
        if user is not None:
            title += f" · {user.display_name}"
        text = "\n".join(_audit_line(r) for r in rows)
        embeds = _split_text_embeds(title, text)
        await safe_send(i.followup, embeds=embeds, ephemeral=True)
    except Exception as e:
        await log_error(i.client, "/audit", e)
        try:
            await safe_send(i.followup, f"❌ Ошибка: {str(e)[:200]}",
                            ephemeral=True)
        except Exception:
            pass


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

    # Поле 4: список заявок в работе. Показываем заявителя и отметку
    # «взял», чтобы модератор видел не только номер заявки, но и то,
    # какие активные заявки закреплены лично за ним.
    tickets = []
    for item in in_work[:20]:
        ident = (item.get("message_id") or item.get("user_id")
                 or item.get("app_id") or "")
        if not ident:
            continue
        line = f"`ticket-{ident}`"
        applicant = item.get("user_id") or ""
        if applicant:
            line += f" · <@{applicant}>"
        if item.get("taken_by"):
            line += " · 🙋 взял"
        clan = (item.get("clan_name") or "").strip()
        if clan:
            line += f" · **{clan}**"
        tickets.append(line)
    work_text = "\n".join(tickets) if tickets else "Нет активных заявок"
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
    await i.response.defer(ephemeral=True)
    try:
        guild = i.guild
        if guild is None:
            await safe_send(i.followup, "❌ Команда работает только на сервере.",
                            ephemeral=True)
            return

        admin_members = await _collect_admins(guild)
        mod_lines = await _collect_bot_mods(guild)
        role_lines = await _collect_reviewer_roles(guild)
        embed = _build_admins_embed(guild, admin_members, mod_lines, role_lines)
        await safe_send(i.followup, embed=embed, ephemeral=True)
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
        reason = (item.get("decision_reason") or "").strip()
        if reason and status == "rejected":
            line += f"\n📝 {reason[:120]}"
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
        await i.response.send_message("❌", ephemeral=True)
        return None
    sid = await _get_steam_of(m)
    if not sid:
        await i.response.send_message("❌ Нет SteamID", ephemeral=True)
        return None
    return sid


@bot.tree.command(name="activity_set", description="Установить часы активности")
async def activity_set(i, member: discord.Member, server: str, hours: float):
    sid = await _activity_guard(i, member)
    if not sid:
        return
    if hours < 0:
        await i.response.send_message("❌", ephemeral=True)
        return
    await i.response.defer(ephemeral=True)
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
            f"{build_activity_bar(total, ACTIVITY_GOAL_HOURS)}", ephemeral=True
        )
        await audit_action(i.client, "Изменение активности", actor=i.user,
                           target=member,
                           details=(f"Сервер: {server.upper()}\n"
                                    f"Часы: {hours}\nSteamID: `{sid}`"))
    except Exception as e:
        await log_error(i.client, "/activity_set", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="activity_clear", description="Сбросить активность")
async def activity_clear(i, member: discord.Member):
    sid = await _activity_guard(i, member)
    if not sid:
        return
    await i.response.defer(ephemeral=True)
    try:
        p = await players_repo.get_player(sid)
        p["activity"] = {}
        p["updated_at"] = now_utc().isoformat()
        await players_repo.save_player(sid, p)
        await safe_send(i.followup, "✅ Сброшено", ephemeral=True)
        await audit_action(i.client, "Сброс активности", actor=i.user,
                           target=member, details=f"SteamID: `{sid}`")
    except Exception as e:
        await log_error(i.client, "/activity_clear", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="activity_show", description="Активность игрока")
async def activity_show(i, member: discord.Member):
    sid = await _activity_guard(i, member, need_admin=False)
    if not sid:
        return
    await i.response.defer(ephemeral=True)
    try:
        a = (await players_repo.get_player(sid)).get("activity", {})
        if not a:
            await safe_send(i.followup, "ℹ️ Пусто", ephemeral=True)
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
            + f"\n\n{build_activity_bar(t, ACTIVITY_GOAL_HOURS)}", ephemeral=True
        )
    except Exception as e:
        await log_error(i.client, "/activity_show", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


@bot.tree.command(name="activity_all", description="Активность роли")
async def activity_all(i):
    await i.response.defer(ephemeral=True)
    try:
        role = i.guild.get_role(ACTIVITY_ROLE_ID) if ACTIVITY_ROLE_ID else None
        if role is None:
            await safe_send(i.followup, "❌ Роль не найдена", ephemeral=True)
            return
        ms = [m for m in role.members if not m.bot]
        if not ms:
            await safe_send(i.followup, "ℹ️ Пусто", ephemeral=True)
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
            await safe_send(i.followup, head + body + foot, ephemeral=True)
        else:
            cut = body[:2000 - len(head) - len(foot) - 100]
            cut = cut[:cut.rfind("\n")]
            await safe_send(i.followup, head + cut + "\n_...обрезано_" + foot, ephemeral=True)
    except Exception as e:
        await log_error(i.client, "/activity_all", e)
        await close_interaction(i, f"❌ Ошибка: {str(e)[:200]}")


# ============================================================
# !setup_clan (алиас !setup) - официальные условия вступления
# ============================================================
def _build_clan_conditions_embed() -> discord.Embed:
    """Embed с официальными условиями вступления в клан.

    Используется текстовой командой !setup_clan, чтобы текст условий
    всегда был единым и не расходился с требованиями клана.
    """
    embed = discord.Embed(
        title="Официальные условия вступления в клан.",
        color=0x000000,
    )
    embed.description = (
        "Перед подачей заявки внимательно ознакомьтесь с нашими требованиями.\n"
        "Несоответствие любому из пунктов приведёт к отказу."
    )
    embed.add_field(
        name="📋 Общие требования",
        value=("• Отсутствие VAC-банов в Steam и блокировок на cs2red.ru.\n"
               "• Отсутствие членства в других кланах.\n"
               "• Возраст — от 12 лет."),
        inline=False,
    )
    embed.add_field(
        name=f"🎯 Требования для {CLAN_NAME}",
        value=("• Рейтинг — от 1.000 ммр.\n"
               "• Онлайн — не менее 3 часов в день."),
        inline=False,
    )
    embed.add_field(
        name="📝 Что указать в заявке",
        value="• Steam-профиль (ссылка или SteamID64)",
        inline=False,
    )
    embed.add_field(
        name="⚠️ Важно!",
        value=("Если вам не напишут в течение 7 дней — заявка отклоняется.\n"
               "После принятия есть 2 дня на вступление."),
        inline=False,
    )
    embed.add_field(
        name="💬 По вопросам вступления",
        value="Обратитесь к администрации.",
        inline=False,
    )
    embed.set_footer(text=f"Quantum • {now_utc().strftime('%d.%m.%Y %H:%M')}")
    return embed


# ---------- панель условий: публикация и автообновление ----------
# Discord со временем перестаёт принимать нажатия на компоненты старых
# сообщений, поэтому панель периодически пересоздаётся: старое сообщение
# удаляется, новое публикуется с чистым view.
CLAN_PANEL_REFRESH_HOURS = env_int("CLAN_PANEL_REFRESH_HOURS", 72)
# Заявка без решения дольше этого срока закрывается автоматически — иначе
# она навсегда блокирует игроку новую подачу (анти-дубликат в on_submit).
APPLICATION_TTL_DAYS = env_int("APPLICATION_TTL_DAYS", 7)

_PANEL_CHANNEL_FLAG = "clan_panel_channel"
_PANEL_MESSAGE_FLAG = "clan_panel_message"
_PANEL_AT_FLAG = "clan_panel_at"


def _parse_iso_utc(value):
    """ISO-дата из БД -> aware datetime в UTC (или None)."""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def _save_clan_panel(channel_id, message_id) -> None:
    """Запоминает, где лежит текущая панель заявки."""
    await seen_repo.set_flag(_PANEL_CHANNEL_FLAG, str(channel_id))
    await seen_repo.set_flag(_PANEL_MESSAGE_FLAG, str(message_id))
    await seen_repo.set_flag(_PANEL_AT_FLAG, str(time.time()))


# Предупреждения о панели не чаще раза в 6 часов на каждую причину:
# задача крутится часто, а один и тот же текст в логе — шум.
_PANEL_ALERT_SECONDS = 6 * 3600
_panel_alert_at: dict = {}


def _panel_alert_once(key: str, text: str) -> bool:
    """Пишет предупреждение о панели в общий лог (с троттлингом)."""
    now = time.time()
    if now - _panel_alert_at.get(key, 0.0) < _PANEL_ALERT_SECONDS:
        return False
    _panel_alert_at[key] = now
    log(f"⚠️ Панель заявки: {text}")
    return True


async def _delete_clan_panel_message(channel, message_id) -> tuple[str, str]:
    """Удаляет прежнее сообщение панели.

    Возвращает (статус, подробности):
      * ``deleted``   — сообщение удалено;
      * ``missing``   — его уже нет (удалили вручную) — это норма;
      * ``forbidden`` — нет права Manage Messages;
      * ``error``     — прочий сбой (текст исключения в подробностях).
    """
    try:
        old = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        return "missing", ""
    except discord.Forbidden:
        return "forbidden", "нет права читать историю канала"
    except Exception as e:  # noqa: BLE001 - удаление best-effort
        return "error", f"{type(e).__name__}: {e}"
    try:
        await old.delete()
        return "deleted", ""
    except discord.NotFound:
        return "missing", ""
    except discord.Forbidden:
        return "forbidden", "нет права Manage Messages"
    except Exception as e:  # noqa: BLE001 - удаление best-effort
        return "error", f"{type(e).__name__}: {e}"


async def refresh_clan_panel(bot) -> str:
    """Пересоздаёт панель заявки: удаляет старое сообщение, пишет новое.

    Возвращает человекочитаемый статус — его пишет вызывающая задача.
    Канал ищется по id (переименование не ломает обновление), а вот
    удалённый канал или отсутствие прав не считаются успехом.
    """
    raw_channel = await seen_repo.get_flag(_PANEL_CHANNEL_FLAG)
    if not raw_channel:
        return ("панель не настроена: выполните !setup_clan в канале "
                "с условиями")
    try:
        channel_id = int(raw_channel)
    except (TypeError, ValueError):
        return f"битый id канала панели в meta: {raw_channel!r}"
    channel = bot.get_channel(channel_id)
    if channel is None:
        return (f"канал {channel_id} недоступен: удалён или бот его не видит "
                f"(проверьте права и что канал не удалили)")

    delete_status, delete_detail = "missing", ""
    raw_message = await seen_repo.get_flag(_PANEL_MESSAGE_FLAG)
    if raw_message:
        try:
            delete_status, delete_detail = await _delete_clan_panel_message(
                channel, int(raw_message))
        except Exception as e:  # noqa: BLE001 - удаление не должно мешать публикации
            delete_status, delete_detail = "error", f"{type(e).__name__}: {e}"

    try:
        msg = await safe_send(channel, embed=_build_clan_conditions_embed(),
                              view=ClanView())
    except discord.Forbidden:
        return (f"нет прав на публикацию в канале {channel_id}: нужны "
                f"Send Messages и Embed Links")
    if msg is None:
        return "не удалось отправить новую панель в канал"
    await _save_clan_panel(channel.id, msg.id)

    if delete_status == "forbidden":
        return ("панель опубликована, но старое сообщение не удалено "
                f"({delete_detail}): в канале будет две панели")
    if delete_status == "error":
        return ("панель опубликована, старое сообщение удалить не удалось: "
                f"{delete_detail}")
    return "панель обновлена"


@tasks.loop(hours=6)
async def clan_panel_refresh():
    """Раз в CLAN_PANEL_REFRESH_HOURS перепубликует панель заявки."""
    try:
        raw_channel = await seen_repo.get_flag(_PANEL_CHANNEL_FLAG)
        if not raw_channel:
            # Первый деплой без !setup_clan: раньше здесь был тихий return.
            if _panel_alert_once(
                    "not-configured",
                    "не настроена — выполните !setup_clan в канале с условиями"):
                await _notify_panel_problem(
                    "Панель заявки не настроена: выполните !setup_clan")
            return

        raw_at = await seen_repo.get_flag(_PANEL_AT_FLAG)
        try:
            posted_at = float(raw_at or 0)
        except (TypeError, ValueError):
            posted_at = 0.0
        if posted_at and time.time() - posted_at < \
                CLAN_PANEL_REFRESH_HOURS * 3600:
            return

        status = await refresh_clan_panel(bot)
        if status == "панель обновлена":
            _panel_alert_at.pop("refresh", None)
            log("🔄 Панель заявки обновлена "
                f"(каждые {CLAN_PANEL_REFRESH_HOURS} ч)")
            return
        if _panel_alert_once("refresh", status):
            await _notify_panel_problem(f"не обновлена — {status}")
    except Exception as e:
        await log_error(bot, "clan_panel_refresh", e)


async def _notify_panel_problem(text: str) -> None:
    """Дублирует проблему с панелью в лог-канал (если он доступен)."""
    channel = bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
    if channel is None:
        return
    try:
        await safe_send(channel, f"⚠️ {text}")
    except Exception as e:  # noqa: BLE001 - лог не должен ронять задачу
        log(f"⚠️ панель: не смог написать в лог-канал: {e}")


@clan_panel_refresh.before_loop
async def before_clan_panel_refresh():
    await bot.wait_until_ready()
    await asyncio.sleep(random.randint(30, 120))


@bot.command(name="setup_clan", aliases=["setup"])
@commands.has_permissions(administrator=True)
async def setup_clan(ctx):
    """Публикует в канале официальные условия вступления и список выбора клана.

    Текстовая команда (не слэш): сообщение с условиями остаётся в канале,
    под ним — выпадающий список кланов, который открывает модальное окно
    заявки. Доступна только администраторам сервера.
    """
    try:
        await ctx.message.delete()
    except Exception:
        pass
    if ctx.guild is None:
        await safe_send(ctx.channel, "❌ Команда работает только на сервере.")
        return
    if not admin_only_ctx(ctx):
        await safe_send(ctx.channel, "❌ Нет прав.")
        return
    try:
        # Повторный запуск не должен оставлять в канале две панели.
        old_id = await seen_repo.get_flag(_PANEL_MESSAGE_FLAG)
        if old_id:
            try:
                status, detail = await _delete_clan_panel_message(
                    ctx.channel, int(old_id))
                if status not in ("deleted", "missing"):
                    log(f"⚠️ панель: старое сообщение не удалено ({status}"
                        f"{': ' + detail if detail else ''})")
            except Exception as e:
                # Удаление старой панели — best-effort: она не должна
                # мешать публикации новой.
                log(f"⚠️ панель: не удалил прежнее сообщение: {e}")
        embed = _build_clan_conditions_embed()
        view = ClanView()
        msg = await safe_send(ctx.channel, embed=embed, view=view)
        if msg is not None:
            await _save_clan_panel(ctx.channel.id, msg.id)
    except Exception as e:
        await log_error(ctx.bot, "!setup_clan", e)
        await safe_send(ctx.channel, f"❌ Ошибка: {str(e)[:200]}")


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
                          color=0x000000, timestamp=now_utc())
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


async def _publish_links_table(guild: discord.Guild):
    """Публикует таблицу привязанных + список не привязанных в лог-канал.

    Прежний вид логов: сначала таблица «Discord | SteamID | Клан»,
    затем embed «🚫 Не авторизованы через /link». Discord принимает
    максимум 10 embed'ов и 6000 символов суммарно на сообщение —
    ``safe_send`` сам разбивает пачки по фактическому размеру.
    """
    ch = bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
    if ch is None:
        return
    try:
        embeds = await build_auto_sync_embeds(guild)
        await safe_send(ch, embeds=embeds)
    except Exception as e:
        await log_error(bot, "_publish_links_table", e)


@tasks.loop(minutes=SYNC_INTERVAL_MINUTES)
async def auto_sync():
    try:
        log(f"⏰ auto_sync {now_utc().isoformat()}")
        # Статус блокировки cs2red относится только к текущему запуску.
        api.reset_block()
        links = await links_repo.get_all_links()
        guild = bot.get_guild(GUILD_ID)
        if not links or guild is None:
            return

        clan_results = []
        stopped = ""
        for did, sid in links.items():
            if stopped:
                break
            m = guild.get_member(int(did)) if str(did).isdigit() else None
            if m is None:
                continue
            # Один запрос к cs2red на игрока: передаём его в обе синхронизации,
            # иначе на 100 привязанных игроков уходило вдвое больше запросов
            # и автосинхронизация не успевала завершиться за 15 минут.
            try:
                user = await api.cs2red_user(sid)
            except Exception as e:
                await log_error(bot, f"auto_sync:fetch:{did}", e)
                user = None
            if user is None:
                clan_results.append((m, "⚠️ cs2red не ответил, роли не тронуты"))
                if api.blocked_reason in ("403", "429"):
                    stopped = api.blocked_reason
                    break
                await asyncio.sleep(REQUEST_DELAY)
                continue
            # Раздельные try: ошибка выдачи роли по cs2red-роли не должна
            # отменять синхронизацию роли клана (иначе роли «залипают»).
            try:
                await sync_role_by_id(guild, m, sid, user=user)
            except Exception as e:
                await log_error(bot, f"auto_sync:{did}", e)
            try:
                result = await sync_clan_role_by_id(guild, m, sid, user=user)
                clan_results.append((m, result))
            except Exception as e:
                await log_error(bot, f"auto_sync:clan:{did}", e)
                clan_results.append((m, f"⚠️ ошибка синхронизации: {str(e)[:150]}"))
            await asyncio.sleep(REQUEST_DELAY)
            # Если cs2red заблокировал запросы — не долбим его остальными
            # игроками до следующего запуска.
            if api.blocked_reason in ("403", "429"):
                stopped = api.blocked_reason
                break

        try:
            updated = len([1 for _, r in clan_results
                           if r.startswith("🔄") or "выдан" in r])
            note = (f"⛔ Остановлено: cs2red блокирует ({stopped}), "
                    "остальные игроки не проверены.") if stopped else ""
            await _report_clan_sync(clan_results, len(clan_results), updated,
                                    "Автосинхронизация кланов", note=note,
                                    silent_if_empty=True)
        except Exception as e:
            await log_error(bot, "auto_sync.clan_report", e)

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
async def _report_clan_sync(results, checked: int, updated: int, source: str,
                            note: str = "", silent_if_empty: bool = False):
    """Пишет в лог-канал, что изменилось и где не хватило прав.

    Без этого автоматическое снятие ролей «молчало»: в канал уходила
    только цифра «обновлено N», а причина (роль выше роли бота) терялась.
    """
    ch = bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
    if ch is None:
        return
    problems = [(m, r) for m, r in results if r.startswith(("⚠️", "❌"))]
    # «✅ уже в» — это не изменение, иначе каждый час в отчёт попадали бы
    # все участники клана. Показываем только реальные выдачи и снятия.
    changes = [(m, r) for m, r in results if r.startswith("🔄") or "выдан" in r]
    if silent_if_empty and not problems and not changes and not note:
        return

    lines = [f"Проверено: {checked} · изменений: {updated} · проблем: {len(problems)}"]
    if changes:
        lines.append("\nИзменения ролей:")
        lines += [f"• {m.display_name} — {r}" for m, r in changes[:30]]
    if problems:
        lines.append("\n⚠️ Не удалось изменить роль:")
        lines += [f"• {m.display_name} — {r}" for m, r in problems[:30]]
    if note:
        lines.append("")
        lines.append(note)
    try:
        await safe_send(ch, "\n".join(lines), title=f"⏰ {source}")
    except Exception as e:
        log(f"⚠️ {source} лог: {e}")


@tasks.loop(hours=1)
async def hourly_clan_sync():
    """Автоматически, раз в час, обновляет роли кланов для всех привязанных игроков."""
    try:
        # Сбрасываем статус прошлого запуска, иначе после одного 403/429
        # каждый следующий час прерывался бы на первом игроке.
        api.reset_block()
        links = await links_repo.get_all_links()
        guild = bot.get_guild(GUILD_ID)
        if not links or guild is None:
            return

        checked = 0
        updated = 0
        results = []
        stopped = ""
        # Реально обрабатываем кланы: проверим клан игрока через cs2red и применим роль.
        for did, sid in links.items():
            m = guild.get_member(int(did)) if str(did).isdigit() else None
            if m is None:
                continue

            try:
                result = await sync_clan_role_by_id(guild, m, sid)
            except Exception as e:
                # Один упавший игрок не должен останавливать весь цикл.
                await log_error(bot, f"hourly_clan_sync:{did}", e)
                result = f"⚠️ ошибка синхронизации: {str(e)[:150]}"

            checked += 1
            results.append((m, result))
            if result.startswith("🔄") or "выдан" in result:
                updated += 1

            await asyncio.sleep(api.RATE_LIMIT_SECONDS)

            # Если cs2red начал блокировать (403/429) — прерываем цикл и ждём следующего запуска.
            if api.blocked_reason in ("403", "429"):
                stopped = api.blocked_reason
                log(f"⏰ hourly_clan_sync: cs2red блокирует ({stopped}) — пауза до следующего цикла")
                break

        # Диагностика: роли выше роли бота снять нельзя, а участников без
        # /link автоматика вообще не видит.
        me = guild.me
        managed_ids = set(CLAN_ROLE_MAP.values())
        known_names = _load_clan_role_names()
        above = []
        if me is not None and me.top_role is not None:
            above = [r for r in guild.roles
                     if (r.id in managed_ids or r.name in known_names)
                     and r.position >= me.top_role.position]
        linked_ids = {int(k) for k in links if str(k).isdigit()}
        unlinked = [m for m in guild.members
                    if not m.bot and m.id not in linked_ids
                    and any(r.id in managed_ids or r.name in known_names for r in m.roles)]

        # Роли кланов: включаем «показывать отдельно». Удаление пустых
        # ролей вынесено в отдельную задачу (раз в 10 часов), чтобы роли
        # не исчезали в середине синхронизации.
        hoisted = 0
        try:
            hoisted = await ensure_clan_roles_hoisted(guild)
        except Exception as e:
            log(f"⚠️ hourly_clan_sync: показ отдельно: {e}")

        notes = []
        if hoisted:
            notes.append(f"🔔 Ролей клана с показом отдельно включено: {hoisted}")
        if stopped:
            notes.append(f"⛔ Остановлено: cs2red блокирует ({stopped}), остальные не проверены.")
        if above:
            notes.append("⚠️ Эти роли клана выше роли бота, автоматически снять их нельзя: "
                         + ", ".join(f"«{r.name}»" for r in above[:20])
                         + ". Поднимите роль бота выше них в настройках сервера.")
        if unlinked:
            notes.append(f"🔗 С ролью клана, но без /link: {len(unlinked)} — "
                         "автоматически не проверяются (привяжите SteamID).")

        log(f"⏰ hourly_clan_sync: проверено {checked}, ролей обновлено {updated}, "
            f"проблем {len([1 for _, r in results if r.startswith(('⚠️', '❌'))])}")
        await _report_clan_sync(results, checked, updated,
                                "Часовая синхронизация кланов", note="\n".join(notes))
        # Раз в час возвращаем прежний вид лога: таблица привязанных
        # (Discord | SteamID | Клан), затем список не привязанных через /link.
        await _publish_links_table(guild)
    except Exception as e:
        await log_error(bot, "hourly_clan_sync", e)


@hourly_clan_sync.before_loop
async def before_hourly_clan_sync():
    await bot.wait_until_ready()
    await asyncio.sleep(random.randint(60, 180))


# ============================================================
# ФОНОВЫЕ ЗАДАЧИ: АВТО-ПРИВЯЗКА И СИНХРОНИЗАЦИЯ РОЛЕЙ
# ============================================================
# Запускают те же функции, что /auto_link и /sync_all: логика одна,
# меняется только источник «интеракции» — фоновый контекст.
AUTO_LINK_INTERVAL_HOURS = env_int("AUTO_LINK_INTERVAL_HOURS", 3)
SYNC_ALL_INTERVAL_MINUTES = env_int("SYNC_ALL_INTERVAL_MINUTES", 120)


def _auto_link_first_delay() -> int:
    """Первый автозапуск — через полный интервал после старта бота."""
    return max(60, AUTO_LINK_INTERVAL_HOURS * 3600)


def _sync_all_first_delay() -> int:
    """Первый автозапуск синхронизации — через полный интервал после старта."""
    return max(60, SYNC_ALL_INTERVAL_MINUTES * 60)


def _clan_cleanup_first_delay() -> int:
    """Первый автозапуск очистки ролей кланов — через полный интервал."""
    return max(60, CLAN_ROLE_CLEANUP_HOURS * 3600)


class _BackgroundActor:
    """Заглушка автора действия для аудита и подписи Embed'а."""

    def __init__(self, name: str):
        self.display_name = name
        self.id = 0
        self.mention = name


class _NullMessage:
    """Сообщение-заглушка: прогресс некуда править — молча пропускаем."""

    async def edit(self, **kwargs):
        return None

    async def delete(self):
        return None


class _BackgroundFollowup:
    """followup для фонового запуска: пишет в лог-канал без ephemeral."""

    def __init__(self, channel):
        self._channel = channel

    async def send(self, *args, **kwargs):
        if self._channel is None:
            return _NullMessage()
        kwargs.pop("ephemeral", None)
        msg = await safe_send(self._channel, *args, **kwargs)
        return msg if msg is not None else _NullMessage()


class _BackgroundResponse:
    """response-заглушка: для close_interaction в обработчике ошибок."""

    def is_done(self) -> bool:
        return True

    async def send_message(self, *args, **kwargs):
        return None

    async def defer(self, *args, **kwargs):
        return None


class _BackgroundContext:
    """Минимальный «Interaction» для вызова командной логики по расписанию."""

    def __init__(self, guild, channel, actor_name: str):
        self.guild = guild
        self.client = bot
        self.user = _BackgroundActor(actor_name)
        self.followup = _BackgroundFollowup(channel)
        self.response = _BackgroundResponse()


@tasks.loop(hours=max(1, AUTO_LINK_INTERVAL_HOURS))
async def auto_link_watch():
    """Авто-привязка по никам раз в N часов — только если есть непривязанные."""
    try:
        guild = bot.get_guild(GUILD_ID)
        if guild is None or not CS2RED_COOKIES:
            return
        if api.is_blocked():
            log(f"⏸ Авто-привязка пропущена: cs2red на паузе ({api.blocked_reason})")
            return
        links = await links_repo.get_all_links()
        unlinked = _unlinked_members(guild, links)
        if not unlinked:
            return  # всё уже привязано — повторно не надо
        lock = _heavy_lock("auto_link")
        if lock.locked():
            log("⏸ Авто-привязка пропущена: уже выполняется вручную")
            return
        log(f"🔗 Авто-привязка по расписанию: непривязанных {len(unlinked)}")
        async with lock:
            channel = bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
            ctx = _BackgroundContext(guild, channel, "Авто-привязка")
            await run_auto_link(ctx, ephemeral=False)
    except Exception as e:
        await log_error(bot, "auto_link_watch", e)


@auto_link_watch.before_loop
async def before_auto_link_watch():
    await bot.wait_until_ready()
    # После перезапуска не стартуем сразу: ждём полный интервал.
    # Принудительно можно запустить кнопкой или командой.
    await asyncio.sleep(_auto_link_first_delay())


@tasks.loop(minutes=max(15, SYNC_ALL_INTERVAL_MINUTES))
async def sync_all_watch():
    """Синхронизация ролей всех привязанных раз в N минут."""
    try:
        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            return
        links = await links_repo.get_all_links()
        if not links:
            return
        if api.is_blocked():
            log(f"⏸ Синхронизация ролей пропущена: cs2red на паузе ({api.blocked_reason})")
            return
        lock = _heavy_lock("sync_all")
        if lock.locked():
            log("⏸ Синхронизация ролей пропущена: уже выполняется вручную")
            return
        log(f"🔄 Синхронизация ролей по расписанию: привязок {len(links)}")
        async with lock:
            channel = bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
            ctx = _BackgroundContext(guild, channel, "Синхронизация")
            await run_sync_all(ctx, ephemeral=False)
    except Exception as e:
        await log_error(bot, "sync_all_watch", e)


@sync_all_watch.before_loop
async def before_sync_all_watch():
    await bot.wait_until_ready()
    # Первый прогон — через интервал (2 ч по умолчанию), не при старте.
    await asyncio.sleep(_sync_all_first_delay())


@tasks.loop(hours=max(1, CLAN_ROLE_CLEANUP_HOURS))
async def clan_role_cleanup_watch():
    """Удаляет роли кланов без участников раз в N часов (10 по умолчанию)."""
    try:
        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            return
        deleted, errors = await cleanup_empty_clan_roles(guild)
        if not deleted and not errors:
            return
        log(f"🧹 Автоочистка ролей кланов: удалено {len(deleted)}, "
            f"ошибок {len(errors)}")
        channel = bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
        if channel is None:
            return
        text = f"🧹 Удалены пустые роли кланов: {len(deleted)}"
        if deleted:
            text += "\n" + ", ".join(f"«{n}»" for n in deleted[:30])
        if errors:
            text += "\n⚠️ " + "; ".join(errors[:10])
        await safe_send(channel, text[:1900])
    except Exception as e:
        await log_error(bot, "clan_role_cleanup_watch", e)


@clan_role_cleanup_watch.before_loop
async def before_clan_role_cleanup_watch():
    await bot.wait_until_ready()
    # Как и остальные задачи: первый прогон — через полный интервал.
    await asyncio.sleep(_clan_cleanup_first_delay())


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
# HEALTH-CHECK: автопинг в лог-канал при сбое систем
# ============================================================
HEALTH_CHECK_MINUTES = env_int("HEALTH_CHECK_MINUTES", 15)
# Сколько подряд неудачных проверок терпим перед сообщением в лог-канал.
HEALTH_FAILS_BEFORE_ALERT = env_int("HEALTH_FAILS_BEFORE_ALERT", 2)

# Состояние прошлой проверки: чтобы не спамить в канал одной и той же
# проблемой, пишем только при переходе «было ок → стало плохо».
_health_state = {"cs2red_ok": True, "ai_ok": True, "ws_ok": True,
                 "cs2red_fails": 0, "ai_fails": 0, "ws_fails": 0}


async def _health_probe_cs2red() -> bool:
    """Дешёвая проверка cs2red: один запрос к публичному списку серверов."""
    try:
        data = await api._get_json("https://cs2red.ru/api/data/servers")
        return data is not None
    except Exception:
        return False


def _health_probe_ai() -> bool:
    """ИИ считается живым, если задан хотя бы один провайдер."""
    try:
        return bool(ai_service and ai_service.providers)
    except Exception:
        return False


async def _health_alert(problems: list):
    """Пишет в лог-канал, что система деградировала."""
    if not problems:
        return
    ch = bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
    if ch is None:
        return
    text = "⚠️ **Health-check: проблемы с системами**\n" + "\n".join(
        f"• {p}" for p in problems)
    try:
        await safe_send(ch, text, title="🩺 Health-check")
    except Exception as e:
        log(f"⚠️ health alert: {e}")


@tasks.loop(minutes=HEALTH_CHECK_MINUTES)
async def health_watch():
    """Следит за cs2red, ИИ и WebSocket. При сбое пишет в лог-канал.

    Сообщение отправляется только при переходе «работало → сломалось»,
    чтобы канал не забивался одним и тем же текстом каждые 15 минут.
    """
    try:
        problems = []

        cs2red_ok = await _health_probe_cs2red()
        if cs2red_ok:
            _health_state["cs2red_fails"] = 0
            if not _health_state["cs2red_ok"]:
                problems.append("cs2red снова доступен ✅")
        else:
            _health_state["cs2red_fails"] += 1
            if (_health_state["cs2red_ok"]
                    and _health_state["cs2red_fails"] >= HEALTH_FAILS_BEFORE_ALERT):
                problems.append("cs2red не отвечает на запросы")
        _health_state["cs2red_ok"] = cs2red_ok

        ai_ok = _health_probe_ai()
        if ai_ok:
            _health_state["ai_fails"] = 0
            if not _health_state["ai_ok"]:
                problems.append("ИИ-провайдеры снова доступны ✅")
        else:
            _health_state["ai_fails"] += 1
            if (_health_state["ai_ok"]
                    and _health_state["ai_fails"] >= HEALTH_FAILS_BEFORE_ALERT):
                problems.append("нет доступных ИИ-провайдеров")
        _health_state["ai_ok"] = ai_ok

        ws_ok = bot.latency * 1000 < 1000 and not bot.is_closed()
        if ws_ok:
            _health_state["ws_fails"] = 0
        else:
            _health_state["ws_fails"] += 1
            if (_health_state["ws_ok"]
                    and _health_state["ws_fails"] >= HEALTH_FAILS_BEFORE_ALERT):
                problems.append(f"высокая задержка Discord ({bot.latency * 1000:.0f} мс)")
        _health_state["ws_ok"] = ws_ok

        if problems:
            await _health_alert(problems)
    except Exception as e:
        await log_error(bot, "health_watch", e)


@health_watch.before_loop
async def before_health_watch():
    await bot.wait_until_ready()
    await asyncio.sleep(random.randint(30, 120))


# ============================================================
# ЕЖЕДНЕВНАЯ СВОДКА
# ============================================================
DAILY_SUMMARY_HOUR = env_int("DAILY_SUMMARY_HOUR", 9)  # UTC


async def _build_daily_summary_embed() -> discord.Embed:
    """Один embed за сутки: заявки, роли, активности, ошибки."""
    embed = discord.Embed(
        title="📅 Ежедневная сводка",
        color=0x000000, timestamp=now_utc())

    # Заявки за сутки.
    try:
        apps = await applications_repo.list_pending_applications()
        embed.add_field(name="📨 Заявок в работе", value=str(len(apps)), inline=True)
    except Exception as e:
        log(f"⚠️ daily: заявки: {e}")
        embed.add_field(name="📨 Заявок в работе", value="?", inline=True)

    # Роли клана: сколько участников и сколько без привязки.
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if guild is not None:
        try:
            members = _talent_members(guild)
            linked = await links_repo.get_all_links()
            linked_ids = {int(k) for k in linked if str(k).isdigit()}
            no_link = sum(1 for m in members if m.id not in linked_ids)
            embed.add_field(name=f"🎭 Роль «{CLAN_NAME}»",
                            value=str(len(members)), inline=True)
            embed.add_field(name="🔗 Без /link", value=str(no_link), inline=True)
        except Exception as e:
            log(f"⚠️ daily: роли: {e}")

    # Состояние систем.
    cs2red = getattr(api, "last_status", None) or "нет данных"
    embed.add_field(name="🍪 cs2red", value=str(cs2red), inline=True)
    embed.add_field(name="🤖 ИИ", value=_ai_status()[:200], inline=False)
    embed.add_field(name="⏱ Аптайм", value=_uptime_str(), inline=True)
    embed.set_footer(text=f"Quantum • {utc_stamp()}")
    return embed


@tasks.loop(minutes=30)
async def daily_summary():
    """Раз в сутки (в DAILY_SUMMARY_HOUR UTC) публикует сводку в лог-канал."""
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        if now.hour != DAILY_SUMMARY_HOUR or now.minute >= 30:
            return
        ch = bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
        if ch is None:
            return
        embed = await _build_daily_summary_embed()
        await safe_send(ch, embed=embed)
    except Exception as e:
        await log_error(bot, "daily_summary", e)


@daily_summary.before_loop
async def before_daily_summary():
    await bot.wait_until_ready()
    await asyncio.sleep(random.randint(30, 120))


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
    await i.response.defer(ephemeral=True)
    try:
        removed = await clear_cache()
        await safe_send(i.followup, "✅ Кэш ИИ успешно очищен.", title="🤖 Кэш ИИ",
                        ephemeral=True)
        if removed:
            await log_to_channel(i.client,
                f"🗑 Кэш ИИ очищен командой {i.user.mention} (удалено: {removed})")
            await audit_action(i.client, "Очистка кэша ИИ", actor=i.user,
                               details=f"Удалено записей: {removed}")
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
        # Двойной клик и запуск из расписания не должны стартовать второй прогон.
        lock = _heavy_lock("auto_link")
        if lock.locked():
            await safe_send(interaction.followup,
                            "⏳ Авто-привязка уже выполняется. Подождите.",
                            ephemeral=True)
            return
        async with lock:
            # Полный запуск /auto_link (прогресс-бар, защита от 403/429).
            try:
                await run_auto_link(interaction, ephemeral=True)
            except Exception as e:
                log(f"⚠️ panel_link_all: {e}")
                await safe_send(interaction.followup,
                                f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)

    @discord.ui.button(label="🔗 Синхронизация", style=discord.ButtonStyle.secondary,
                       custom_id="panel_sync", row=0)
    async def panel_sync(self, interaction, button):
        if not await _panel_guard(interaction):
            return
        button.disabled = True
        await interaction.response.defer(ephemeral=True)
        lock = _heavy_lock("sync_all")
        if lock.locked():
            await safe_send(interaction.followup,
                            "⏳ Синхронизация уже выполняется. Подождите.",
                            ephemeral=True)
            return
        async with lock:
            # Полный запуск /sync_all (прогресс-бар, остановка при 403/429).
            try:
                await run_sync_all(interaction, ephemeral=True)
            except Exception as e:
                log(f"⚠️ panel_sync: {e}")
                await safe_send(interaction.followup,
                                f"❌ Ошибка: {str(e)[:200]}", ephemeral=True)

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
    """Честный health-check для Render и Docker HEALTHCHECK.

    Имя НЕ `health`: в модуле уже есть слэш-команда `health`, и
    переопределение одноимённой функции путало бы читателя кода
    (и сломало бы `from script import health`).

    200 — бот залогинился и WebSocket не закрыт; иначе 503. Без этого
    HEALTHCHECK видел только живость Flask-потока: процесс есть, а бот
    уже отвалился от Discord — и оркестратор ничего не замечал.
    """
    ready = bot.is_ready() and not bot.is_closed()
    latency = float(bot.latency)
    payload = {
        "ok": ready,
        "ready": bot.is_ready(),
        "closed": bot.is_closed(),
        # До первого heartbeat discord.py отдаёт NaN — это не 0 мс.
        "latency_ms": None if math.isnan(latency) else round(latency * 1000, 1),
        "uptime_seconds": int(time.time() - BOT_START_TIME),
    }
    return payload, (200 if ready else 503)


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
