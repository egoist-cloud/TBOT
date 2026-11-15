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

Исчерпанный лимит не должен тормозить каждый запрос: после 429 (и других
постоянных ошибок) провайдер уходит в паузу — cooldown_until. Пока пауза
не истекла, `available` == False и сервис его просто пропускает.

Все ключи берутся из os.environ, не хардкодятся.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import aiohttp

from ..env import env_float, env_int

log = logging.getLogger("quantum.ai")

# Общий таймаут на один запрос к провайдеру (сек).
DEFAULT_TIMEOUT = env_float("AI_REQUEST_TIMEOUT", 30.0)

# Паузы после ошибок (сек): лимит/квота, неверный ключ, битая модель, сбой.
COOLDOWN_LIMIT = env_int("AI_PROVIDER_COOLDOWN_SECONDS", 600)
COOLDOWN_AUTH = env_int("AI_PROVIDER_AUTH_COOLDOWN_SECONDS", 3600)
COOLDOWN_CONFIG = env_int("AI_PROVIDER_CONFIG_COOLDOWN_SECONDS", 1800)
COOLDOWN_ERROR = env_int("AI_PROVIDER_ERROR_COOLDOWN_SECONDS", 120)


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
        # До какого момента провайдер в паузе (monotonic-время).
        self.cooldown_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._api_key())

    @property
    def available(self) -> bool:
        """Включён и не в паузе после исчерпанного лимита/ошибки."""
        return self.enabled and time.time() >= self.cooldown_until

    @property
    def cooldown_left(self) -> int:
        return max(0, int(self.cooldown_until - time.time()))

    def _api_key(self) -> str:
        return (os.getenv(self.env_key) or "").strip()

    def _start_cooldown(self, status: int | None, retry_after=None) -> None:
        """Ставит провайдеру паузу: пока она идёт, его не вызываем."""
        if status == 200:
            self.cooldown_until = 0.0
            return
        secs = COOLDOWN_ERROR
        if status in (401, 403):
            secs = COOLDOWN_AUTH
        elif status == 429:
            secs = COOLDOWN_LIMIT
            try:
                if retry_after is not None:
                    secs = max(secs, int(float(retry_after)))
            except (TypeError, ValueError):
                pass
        elif status in (400, 404):
            secs = COOLDOWN_CONFIG
        self.cooldown_until = time.time() + max(5, secs)

    def _record(self, status: int | None, err: str, retry_after=None) -> None:
        """Обновляет статистику вызова: счётчики, последний статус и ошибку."""
        self.last_status = status
        if err:
            self.failures += 1
            self.last_error = err
            self._start_cooldown(status, retry_after)
        else:
            self.last_error = ""
            self._start_cooldown(status)

    def status_line(self) -> str:
        """Строка для /health: включён/выключен, ошибки, пауза, статусы."""
        if not self.enabled:
            return f"{self.name}: выключен (нет {self.env_key})"
        state = f"ошибка: {self.last_error[:120]}" if self.last_error else "работает"
        if self.cooldown_left:
            state += (f", пауза после лимита/ошибки ещё "
                      f"{_fmt_duration(self.cooldown_left)}")
        return (f"{self.name}: {state}, запросов {self.requests}, "
                f"ошибок {self.failures}, последний HTTP {self.last_status}")

    @staticmethod
    def _retry_after(response) -> str | None:
        """Заголовок Retry-After (429 часто его отдаёт)."""
        headers = getattr(response, "headers", None) or {}
        try:
            return headers.get("Retry-After")
        except AttributeError:
            return None

    async def complete(self, messages: list) -> str | None:  # pragma: no cover - абстракция
        raise NotImplementedError


def _is_auth_error(status: int) -> bool:
    return status in (401, 403)


def _fmt_duration(seconds: int) -> str:
    """Секунды в человекочитаемый вид для /health и /ai_status.

    Округляем вверх до минуты: cooldown_left уже усечён до целых секунд,
    и 7199 с иначе показывались бы как «1 ч 59 мин».
    """
    minutes = max(1, (int(seconds) + 59) // 60)
    if minutes >= 60:
        return f"{minutes // 60} ч {minutes % 60} мин"
    return f"{minutes} мин"


def _parse_json_body(raw: str) -> dict | None:
    """JSON из тела ответа, включая SSE-строки ``data: {...}``.

    Некоторые OpenAI-совместимые шлюзы отвечают 200 с SSE или HTML
    (страница логина при неверном токене), а не объектом JSON.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        return data
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            parsed = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


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
    # Отдельная переменная для модели Gemini: ``AI_MODEL`` общий с LLM7 и
    # ломает его, если задать там имя Gemini-модели. ``AI_MODEL`` остаётся
    # запасным вариантом для обратной совместимости.
    model_env = "GEMINI_MODEL"
    # Алиас Google всегда указывает на актуальную flash-модель: жёсткое имя
    # (gemini-2.5-flash) однажды отключают, и провайдер получает 404.
    default_model = "gemini-flash-latest"
    api_url_env = "GEMINI_API_URL"
    default_api_url = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    models_url_env = "GEMINI_MODELS_URL"
    default_models_url = "https://generativelanguage.googleapis.com/v1beta/models"
    max_output_env = "AI_MAX_OUTPUT_TOKENS"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Модель, найденная через ListModels, если настроенная недоступна.
        self._resolved_model = ""

    @property
    def _model(self) -> str:
        if self._resolved_model:
            return self._resolved_model
        # Приоритет: GEMINI_MODEL → AI_MODEL → модель по умолчанию.
        # ``AI_MODEL`` берём только если в нём явно Gemini-модель: в старых
        # .env переменная была общей с LLM7 и там мог лежать селектор
        # вроде ``default``, из-за которого Gemini получал 404.
        explicit = (os.getenv("GEMINI_MODEL") or "").strip()
        if explicit:
            return explicit
        legacy = (os.getenv("AI_MODEL") or "").strip()
        if legacy and ("gemini" in legacy.lower()):
            return legacy
        return self.default_model

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

    async def _discover_model(self) -> str | None:
        """Выбрать доступную flash-модель через ListModels (один раз на 404)."""
        url = os.getenv(self.models_url_env) or self.default_models_url
        try:
            async with self.session.get(
                url, headers={"x-goog-api-key": self._api_key()},
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as r:
                if r.status != 200:
                    log.warning("gemini: список моделей HTTP %s", r.status)
                    return None
                data = await r.json(content_type=None)
        except Exception as e:  # noqa: BLE001 - без списка работаем как раньше
            log.warning("gemini: список моделей: %s", e)
            return None
        names = []
        for item in (data or {}).get("models", []) or []:
            if not isinstance(item, dict):
                continue
            methods = item.get("supportedGenerationMethods") or []
            name = str(item.get("name") or "").removeprefix("models/")
            if "generateContent" not in methods or not name.startswith("gemini"):
                continue
            if any(bad in name for bad in ("embedding", "image", "tts", "live",
                                           "vision", "aqa")):
                continue
            names.append(name)
        # Стабильные flash-модели важнее preview/experimental и lite.
        names.sort(key=lambda name: ("preview" in name or "exp" in name,
                                     "lite" in name or "flash" not in name,
                                     name))
        return names[0] if names else None

    async def complete(self, messages: list) -> str | None:
        if not self.enabled:
            return None
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
        result = await self._post(self._build_url(), headers, body)
        if result is None and self.last_status == 404:
            # Настроенная модель отключена/переименована — берём доступную.
            fallback = await self._discover_model()
            if fallback and fallback != self._model:
                log.info("gemini: модель %s недоступна, переключаюсь на %s",
                         self._model, fallback)
                self._resolved_model = fallback
                result = await self._post(self._build_url(), headers, body)
        return result

    async def _post(self, url: str, headers: dict, body: dict) -> str | None:
        self.requests += 1
        try:
            async with self.session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as r:
                raw = (await r.text())[:2000]
                if r.status != 200:
                    # Тело ошибки — в лог, а не в stdout: в нём бывают куски
                    # промпта и служебные данные аккаунта Google.
                    log.warning("gemini: HTTP %s: %s", r.status, raw[:200])
                    if _is_auth_error(r.status):
                        msg = (f"авторизация {r.status}: неверный API-ключ "
                               f"({self.env_key}): {raw[:200]}")
                        self._record(r.status, msg)
                    elif r.status in (429,) or r.status >= 500:
                        msg = f"временный сбой {r.status}: {raw[:200]}"
                        self._record(r.status, msg, self._retry_after(r))
                    elif r.status == 400:
                        msg = f"неверный запрос (проверь GEMINI_MODEL): {raw[:200]}"
                        self._record(r.status, msg)
                    elif r.status == 404:
                        msg = (f"модель не найдена (проверь GEMINI_MODEL): "
                               f"{raw[:200]}")
                        self._record(r.status, msg)
                    else:
                        msg = f"HTTP {r.status}: {raw[:200]}"
                        self._record(r.status, msg)
                    return None
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = _parse_json_body(raw)
                if not isinstance(data, dict):
                    ctype = r.headers.get("Content-Type", "?")
                    msg = f"ответ не JSON ({ctype}): {raw[:200]}"
                    self._record(None, msg)
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
    # Доп. заголовки конкретного провайдера (например, Accept у GitHub).
    extra_headers: dict = {}

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
        headers.update(self.extra_headers)
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
                        self._record(r.status, msg, self._retry_after(r))
                    elif r.status == 400:
                        msg = f"неверный запрос (проверь модель): {raw[:200]}"
                        self._record(r.status, msg)
                    else:
                        self._record(r.status, f"HTTP {r.status}: {raw[:200]}")
                    return None
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = _parse_json_body(raw)
                if not isinstance(data, dict):
                    ctype = r.headers.get("Content-Type", "?")
                    msg = f"ответ не JSON ({ctype}): {raw[:200]}"
                    self._record(None, msg)
                    return None
                if isinstance(data.get("error"), (dict, str)):
                    msg = f"ошибка API: {str(data['error'])[:200]}"
                    self._record(400, msg)
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
    # У LLM7 свои селекторы моделей (default/fast/pro). Общий ``AI_MODEL``
    # здесь намеренно НЕ используется: если в старом .env там лежит имя
    # Gemini-модели, запросы к LLM7 падали бы с 400.
    model_env = "LLM7_MODEL"
    default_model = "default"

    @property
    def _model(self) -> str:
        return os.getenv("LLM7_MODEL") or self.default_model


class GroqProvider(_OpenAICompatProvider):
    """Groq — быстрый бесплатный тир (llama/gpt-oss)."""

    name = "groq"
    env_key = "GROQ_API_KEY"
    base_url_env = "GROQ_BASE_URL"
    default_base_url = "https://api.groq.com/openai/v1"
    model_env = "GROQ_MODEL"
    default_model = "llama-3.3-70b-versatile"


class CerebrasProvider(_OpenAICompatProvider):
    """Cerebras Cloud — бесплатный тир gpt-oss/Llama."""

    name = "cerebras"
    env_key = "CEREBRAS_API_KEY"
    base_url_env = "CEREBRAS_BASE_URL"
    default_base_url = "https://api.cerebras.ai/v1"
    model_env = "CEREBRAS_MODEL"
    default_model = "gpt-oss-120b"


class MistralProvider(_OpenAICompatProvider):
    """Mistral La Plateforme (Experiment plan)."""

    name = "mistral"
    env_key = "MISTRAL_API_KEY"
    base_url_env = "MISTRAL_BASE_URL"
    default_base_url = "https://api.mistral.ai/v1"
    model_env = "MISTRAL_MODEL"
    default_model = "mistral-small-latest"


class GitHubModelsProvider(_OpenAICompatProvider):
    """GitHub Models — модели из маркетплейса по токену GitHub."""

    name = "github"
    env_key = "GITHUB_MODELS_TOKEN"
    base_url_env = "GITHUB_MODELS_BASE_URL"
    default_base_url = "https://models.github.ai/inference"
    model_env = "GITHUB_MODELS_MODEL"
    default_model = "openai/gpt-4o-mini"
    extra_headers = {"Accept": "application/json"}

    def _api_key(self) -> str:
        # Отдельный токен, иначе — общий GITHUB_TOKEN (если у него есть
        # доступ к GitHub Models).
        return (os.getenv("GITHUB_MODELS_TOKEN")
                or os.getenv("GITHUB_TOKEN") or "").strip()


class NvidiaProvider(_OpenAICompatProvider):
    """NVIDIA NIM (build.nvidia.com) — 40 запросов/минуту бесплатно."""

    name = "nvidia"
    env_key = "NVIDIA_API_KEY"
    base_url_env = "NVIDIA_BASE_URL"
    default_base_url = "https://integrate.api.nvidia.com/v1"
    model_env = "NVIDIA_MODEL"
    default_model = "meta/llama-3.3-70b-instruct"


class SambaNovaProvider(_OpenAICompatProvider):
    """SambaNova Cloud — стартовые $5 кредитов."""

    name = "sambanova"
    env_key = "SAMBANOVA_API_KEY"
    base_url_env = "SAMBANOVA_BASE_URL"
    default_base_url = "https://api.sambanova.ai/v1"
    model_env = "SAMBANOVA_MODEL"
    default_model = "Meta-Llama-3.3-70B-Instruct"


class CohereProvider(_OpenAICompatProvider):
    """Cohere через OpenAI-совместимый эндпоинт /compatibility/v1."""

    name = "cohere"
    env_key = "COHERE_API_KEY"
    base_url_env = "COHERE_BASE_URL"
    default_base_url = "https://api.cohere.ai/compatibility/v1"
    model_env = "COHERE_MODEL"
    default_model = "command-a-03-2025"


class HuggingFaceProvider(_OpenAICompatProvider):
    """HuggingFace Inference Providers (router, OpenAI-совместимый)."""

    name = "huggingface"
    env_key = "HUGGINGFACE_API_KEY"
    base_url_env = "HUGGINGFACE_BASE_URL"
    default_base_url = "https://router.huggingface.co/v1"
    model_env = "HUGGINGFACE_MODEL"
    default_model = "meta-llama/Llama-3.1-8B-Instruct"


class CloudflareProvider(_OpenAICompatProvider):
    """Cloudflare Workers AI — нужен и токен, и account id."""

    name = "cloudflare"
    env_key = "CLOUDFLARE_API_TOKEN"
    base_url_env = "CLOUDFLARE_AI_BASE_URL"
    default_base_url = ""
    model_env = "CLOUDFLARE_AI_MODEL"
    default_model = "@cf/meta/llama-3.1-8b-instruct"

    @property
    def enabled(self) -> bool:
        return bool(self._api_key() and self._account_id())

    def _account_id(self) -> str:
        return (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()

    @property
    def _base_url(self) -> str:
        explicit = (os.getenv(self.base_url_env) or "").strip()
        if explicit:
            return explicit.rstrip("/")
        return ("https://api.cloudflare.com/client/v4/accounts/"
                f"{self._account_id()}/ai/v1")


# Порядок каскада: сначала самые быстрые/щедрые бесплатные тиры.
_PROVIDER_CLASSES = (
    GeminiProvider,
    GroqProvider,
    CerebrasProvider,
    MistralProvider,
    GitHubModelsProvider,
    OpenRouterProvider,
    NvidiaProvider,
    SambaNovaProvider,
    CohereProvider,
    HuggingFaceProvider,
    CloudflareProvider,
    LLM7Provider,
)


def provider_catalog(session=None) -> list[dict]:
    """Все провайдеры каскада: имя, переменные ключа, модель, наличие ключа.

    В отличие от make_providers() возвращает и выключенные провайдеры —
    чтобы /ai_status мог показать, каких ключей не хватает.
    """
    result: list[dict] = []
    for cls in _PROVIDER_CLASSES:
        inst = cls(session)
        if isinstance(inst, CloudflareProvider):
            env_names = "CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID"
        elif isinstance(inst, GitHubModelsProvider):
            env_names = "GITHUB_MODELS_TOKEN (или GITHUB_TOKEN)"
        else:
            env_names = inst.env_key
        result.append({
            "name": inst.name,
            "env": env_names,
            "model": getattr(inst, "_model", "?"),
            "enabled": inst.enabled,
        })
    return result


def make_providers(
    session: aiohttp.ClientSession,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[AIProvider]:
    """Собирает каскад провайдеров в порядке приоритета.

    В список попадают только те, у кого есть ключ. Порядок задан в
    ``_PROVIDER_CLASSES``: Gemini → Groq → Cerebras → Mistral → GitHub →
    OpenRouter → NVIDIA → SambaNova → Cohere → HuggingFace → Cloudflare →
    LLM7. Если у провайдера кончился лимит, он уходит в паузу, а запрос
    обслуживает следующий.
    """
    result: list[AIProvider] = []
    for cls in _PROVIDER_CLASSES:
        inst = cls(session, timeout=timeout)
        if inst.enabled:
            result.append(inst)
    return result