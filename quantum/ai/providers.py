# -*- coding: utf-8 -*-
"""Провайдеры LLM для AI-каскада бота.

Каждый провайдер — тонкая обёртка над одним внешним API. Общий интерфейс:
    enabled      — есть ли ключ в окружении
    name         — человекочитаемое имя для логов
    complete(messages) -> str | None

`messages` — это уже готовый список сообщений вида
    [{"role": "system", "content": ...}, {"role": "user", "content": ...}]

Правила failover:
  * сетевой сбой, таймаут, 429, 5xx  -> None (сработает следующий провайдер)
  * 401/403 (ошибка авторизации)      -> логируем, тоже возвращаем None
  * 400 (неверный запрос/модель)      -> логируем, возвращаем None
Все ключи берутся из os.environ, не хардкодятся.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

from ..env import env_float

log = logging.getLogger("quantum.ai")

# Общий таймаут на один запрос к провайдеру (сек).
DEFAULT_TIMEOUT = env_float("AI_REQUEST_TIMEOUT", 30.0)


class AIProvider:
    """Базовый класс провайдера."""

    name = "abstract"
    env_key = ""

    def __init__(self, session: aiohttp.ClientSession, timeout: float = DEFAULT_TIMEOUT):
        self.session = session
        self.timeout = timeout
        self.requests = 0
        self.failures = 0
        self.last_error = ""
        self.last_status: int | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key())

    def _api_key(self) -> str:
        return (os.getenv(self.env_key) or "").strip()

    def _record(self, status: int | None, err: str) -> None:
        """Обновляет статистику вызова: счётчики, последний статус и ошибку."""
        self.last_status = status
        if err:
            self.failures += 1
            self.last_error = err
        else:
            self.last_error = ""

    def status_line(self) -> str:
        """Строка для /health: включён/выключен, ошибки, статусы."""
        if not self.enabled:
            return f"{self.name}: выключен (нет {self.env_key})"
        state = f"ошибка: {self.last_error[:120]}" if self.last_error else "работает"
        return (f"{self.name}: {state}, запросов {self.requests}, "
                f"ошибок {self.failures}, последний HTTP {self.last_status}")

    async def complete(self, messages: list) -> str | None:  # pragma: no cover - абстракция
        raise NotImplementedError


def _is_auth_error(status: int) -> bool:
    return status in (401, 403)


class GeminiProvider(AIProvider):
    """Gemini generateContent API (generativelanguage.googleapis.com).

    Нативный эндпоинт: POST /v1beta/models/{model}:generateContent
    Ключ передаётся в заголовке x-goog-api-key (НЕ Bearer — новый формат
    ключей AQ.Ab8... уже отклоняется через Bearer).
    Тело — нативный формат Gemini:
        {"contents": [{"parts": [{"text": "..."}]}], ...}
    Текст ответа — candidates[].content.parts[].text
    """

    name = "gemini"
    env_key = "GEMINI_API_KEY"
    model_env = "AI_MODEL"
    default_model = "gemini-1.5-flash"
    api_url_env = "GEMINI_API_URL"
    default_api_url = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    max_output_env = "AI_MAX_OUTPUT_TOKENS"

    @property
    def _model(self) -> str:
        return os.getenv(self.model_env) or self.default_model

    @property
    def _max_output(self) -> int:
        try:
            return int(os.getenv(self.max_output_env, "2000"))
        except (TypeError, ValueError):
            return 2000

    def _build_url(self) -> str:
        """URL эндпоинта; {model} в шаблоне подставляется моделью."""
        template = os.getenv(self.api_url_env) or self.default_api_url
        return template.format(model=self._model)

    async def complete(self, messages: list) -> str | None:
        if not self.enabled:
            return None
        print(f"DEBUG: Gemini key = {self._api_key()[:5]}... (len={len(self._api_key())})")
        # Нативный формат Gemini: contents = список частей диалога.
        # Системную инструкцию выносим в system_instruction, остальное — parts.
        contents: list[dict] = []
        system = ""
        for m in messages:
            role = (m or {}).get("role", "user")
            content = str((m or {}).get("content") or "").strip()
            if not content:
                continue
            if role == "system":
                system = (system + "\n" + content).strip()
            else:
                contents.append({"parts": [{"text": content}]})
        # Если нет user-сообщений (только system) — всё равно нужен хотя бы
        # один contents-элемент, иначе Gemini вернёт 400.
        if not contents:
            contents.append({"parts": [{"text": ""}]})

        body: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self._max_output},
        }
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}

        # Ключ ТОЛЬКО в x-goog-api-key, никакого Authorization: Bearer.
        headers = {
            "x-goog-api-key": self._api_key(),
            "Content-Type": "application/json",
        }
        url = self._build_url()
        return await self._post(url, headers, body)

    async def _post(self, url: str, headers: dict, body: dict) -> str | None:
        self.requests += 1
        try:
            async with self.session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as r:
                raw = (await r.text())[:2000]
                if r.status != 200:
                    # Отладочный вывод реального ответа Google в консоль (Render).
                    print(f"Gemini error: {r.status} - {raw[:2000]}")
                    if _is_auth_error(r.status):
                        msg = (f"авторизация {r.status}: неверный API-ключ "
                               f"({self.env_key}): {raw[:200]}")
                        self._record(r.status, msg)
                    elif r.status in (429,) or r.status >= 500:
                        msg = f"временный сбой {r.status}: {raw[:200]}"
                        self._record(r.status, msg)
                    elif r.status == 400:
                        msg = f"неверный запрос (проверь AI_MODEL): {raw[:200]}"
                        self._record(r.status, msg)
                    elif r.status == 404:
                        msg = f"модель не найдена (проверь AI_MODEL): {raw[:200]}"
                        self._record(r.status, msg)
                    else:
                        msg = f"HTTP {r.status}: {raw[:200]}"
                        self._record(r.status, msg)
                    return None
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    self._record(r.status, "не удалось разобрать JSON в ответе")
                    return None
                self._record(r.status, "")
        except asyncio.TimeoutError:
            self._record(None, f"таймаут {self.timeout:.0f} с")
            return None
        except aiohttp.ClientError as e:
            self._record(None, f"сетевой сбой: {e}")
            return None

        return self._extract(data)

    @staticmethod
    def _extract(data: dict) -> str | None:
        if not isinstance(data, dict):
            return None
        parts: list[str] = []
        for cand in data.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            content = cand.get("content") or {}
            for item in content.get("parts") or []:
                if isinstance(item, dict) and item.get("text"):
                    parts.append(str(item["text"]))
        if not parts:
            return None
        text = "\n".join(parts).strip()
        return text or None


class _OpenAICompatProvider(AIProvider):
    """Базовый OpenAI-совместимый провайдер (/chat/completions)."""

    base_url_env = ""
    default_base_url = ""
    model_env = ""
    default_model = "gpt-4o-mini"
    max_output_env = "AI_MAX_OUTPUT_TOKENS"

    @property
    def _base_url(self) -> str:
        return (os.getenv(self.base_url_env) or self.default_base_url).rstrip("/")

    @property
    def _model(self) -> str:
        return os.getenv(self.model_env) or self.default_model

    @property
    def _max_output(self) -> int:
        try:
            return int(os.getenv(self.max_output_env, "2000"))
        except (TypeError, ValueError):
            return 2000

    async def complete(self, messages: list) -> str | None:
        if not self.enabled:
            return None
        body = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_output,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}/chat/completions"
        self.requests += 1
        try:
            async with self.session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as r:
                raw = (await r.text())[:2000]
                if r.status != 200:
                    if _is_auth_error(r.status):
                        msg = (f"авторизация {r.status}: неверный API-ключ "
                               f"({self.env_key}): {raw[:200]}")
                        self._record(r.status, msg)
                    elif r.status in (429,) or r.status >= 500:
                        msg = f"временный сбой {r.status}: {raw[:200]}"
                        self._record(r.status, msg)
                    elif r.status == 400:
                        msg = f"неверный запрос (проверь модель): {raw[:200]}"
                        self._record(r.status, msg)
                    else:
                        self._record(r.status, f"HTTP {r.status}: {raw[:200]}")
                    return None
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    self._record(r.status, "не удалось разобрать JSON в ответе")
                    return None
                self._record(r.status, "")
        except asyncio.TimeoutError:
            self._record(None, f"таймаут {self.timeout:.0f} с")
            return None
        except aiohttp.ClientError as e:
            self._record(None, f"сетевой сбой: {e}")
            return None

        return self._extract(data)

    @staticmethod
    def _extract(data: dict) -> str | None:
        if not isinstance(data, dict):
            return None
        if isinstance(data.get("error"), (dict, str)):
            return None
        choices = data.get("choices") or []
        if not choices:
            return None
        content = str((((choices[0] or {}).get("message") or {}).get("content") or "")).strip()
        return content or None


class OpenRouterProvider(_OpenAICompatProvider):
    """OpenRouter — OpenAI-совместимый API."""

    name = "openrouter"
    env_key = "OPENROUTER_API_KEY"
    base_url_env = "OPENROUTER_BASE_URL"
    default_base_url = "https://openrouter.ai/api/v1"
    model_env = "OPENROUTER_MODEL"
    default_model = "gpt-4o-mini"


class LLM7Provider(_OpenAICompatProvider):
    """LLM7 — OpenAI-совместимый API с селекторами моделей (default/fast/pro)."""

    name = "llm7"
    env_key = "LLM7_API_KEY"
    base_url_env = "LLM7_BASE_URL"
    default_base_url = "https://api.llm7.io/v1"
    model_env = "AI_MODEL"
    default_model = "default"


def make_providers(
    session: aiohttp.ClientSession,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[AIProvider]:
    """Собирает каскад провайдеров в порядке приоритета: Gemini -> OpenRouter -> LLM7."""
    result: list[AIProvider] = []
    for cls in (GeminiProvider, OpenRouterProvider, LLM7Provider):
        inst = cls(session, timeout=timeout)
        if inst.enabled:
            result.append(inst)
    return result