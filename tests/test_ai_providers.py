# -*- coding: utf-8 -*-
"""Тесты провайдеров LLM (quantum/ai/providers.py) с фейковой aiohttp.

Discord и сеть не нужны: `session.post(...)` подменяется асинхронным
контекстным менеджером, который возвращает заранее заданный ответ,
бросает таймаут или сетевую ошибку. Проверяются разбор ответов Gemini и
OpenAI-совместимых API, коды 401/403/429/5xx, таймаут, сетевой сбой и
выбор модели/URL из окружения.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402
import pytest  # noqa: E402

from quantum.ai.providers import (  # noqa: E402
    CerebrasProvider,
    CloudflareProvider,
    CohereProvider,
    GeminiProvider,
    GitHubModelsProvider,
    GroqProvider,
    HuggingFaceProvider,
    LLM7Provider,
    MistralProvider,
    NvidiaProvider,
    OpenRouterProvider,
    SambaNovaProvider,
    _is_auth_error,
    make_providers,
    provider_catalog,
)

# Переменные, которые влияют на провайдеров: перед каждым тестом чистим,
# чтобы реальное окружение разработчика не меняло результат.
_ENV_KEYS = (
    "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_API_URL",
    "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OPENROUTER_BASE_URL",
    "LLM7_API_KEY", "LLM7_MODEL", "LLM7_BASE_URL",
    "GROQ_API_KEY", "GROQ_MODEL", "GROQ_BASE_URL",
    "CEREBRAS_API_KEY", "CEREBRAS_MODEL",
    "MISTRAL_API_KEY", "MISTRAL_MODEL",
    "GITHUB_MODELS_TOKEN", "GITHUB_MODELS_MODEL",
    "NVIDIA_API_KEY", "NVIDIA_MODEL",
    "SAMBANOVA_API_KEY", "SAMBANOVA_MODEL",
    "COHERE_API_KEY", "COHERE_MODEL",
    "HUGGINGFACE_API_KEY", "HUGGINGFACE_MODEL",
    "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_AI_MODEL",
    "AI_MODEL", "AI_MAX_OUTPUT_TOKENS", "AI_REQUEST_TIMEOUT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ни одного ключа/модели из окружения — каждый тест задаёт своё."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class _FakeResponse:
    """Ответ aiohttp: статус, тело и (опционально) JSON или ошибка разбора."""

    def __init__(self, status=200, body="", payload=None, json_error=None,
                 headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = body
        self._payload = payload
        self._json_error = json_error

    async def text(self):
        return self._body

    async def json(self, content_type=None):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _FakePost:
    """Асинхронный контекстный менеджер: то, что возвращает session.post()."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    """Сессия-заглушка: пишет вызовы и отдаёт заданный ответ/ошибку."""

    def __init__(self, response=None, error=None, get_response=None):
        self.response = response
        self.error = error
        self.get_response = get_response
        self.calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakePost(self.response, self.error)

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        return _FakePost(self.get_response)


def _gemini_payload(*texts):
    """Тело успешного ответа Gemini: по одному part на переданный текст."""
    return {"candidates": [
        {"content": {"parts": [{"text": t} for t in texts]}},
    ]}


def _openai_payload(content):
    """Тело успешного ответа OpenAI-совместимого API."""
    return {"choices": [{"message": {"content": content}}]}


def _run(coro):
    return asyncio.run(coro)


def _complete(provider, messages=None):
    """Вызывает complete() в новом event loop (async-тестов в проекте нет)."""
    return _run(provider.complete(messages or [
        {"role": "system", "content": "инструкция"},
        {"role": "user", "content": "вопрос"},
    ]))


# ============================================================
# Базовая логика AIProvider
# ============================================================
def test_enabled_requires_key(monkeypatch):
    """Без ключа провайдер выключен; ключ с пробелами обрезается."""
    provider = GeminiProvider(_FakeSession())
    assert provider.enabled is False
    assert provider._api_key() == ""

    monkeypatch.setenv("GEMINI_API_KEY", "  secret-key  ")
    assert provider.enabled is True
    assert provider._api_key() == "secret-key"
    print("  ok: enabled зависит от ключа")


def test_record_tracks_status_and_failures():
    """_record: ошибка копит счётчик, успех его не сбрасывает."""
    provider = GeminiProvider(_FakeSession())
    provider._record(500, "boom")
    assert provider.last_status == 500
    assert provider.failures == 1
    assert provider.last_error == "boom"

    provider._record(200, "")
    assert provider.failures == 1
    assert provider.last_error == ""
    assert provider.last_status == 200
    print("  ok: статистика вызовов")


def test_status_line_disabled_and_error(monkeypatch):
    """status_line показывает причину выключения и текст последней ошибки."""
    disabled = OpenRouterProvider(_FakeSession())
    line = disabled.status_line()
    assert "выключен" in line
    assert "OPENROUTER_API_KEY" in line

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "x/y")
    provider = OpenRouterProvider(_FakeSession())
    provider._record(200, "")
    assert "работает" in provider.status_line()

    provider._record(500, "сервер лёг")
    line = provider.status_line()
    assert "ошибка: сервер лёг" in line
    assert "последний HTTP 500" in line
    print("  ok: status_line")


def test_is_auth_error_only_401_and_403():
    assert _is_auth_error(401) is True
    assert _is_auth_error(403) is True
    assert _is_auth_error(400) is False
    assert _is_auth_error(429) is False
    assert _is_auth_error(500) is False


# ============================================================
# Каскад провайдеров
# ============================================================
def test_make_providers_only_with_keys_in_order(monkeypatch):
    """В каскад попадают только включённые, порядок — Gemini→OpenRouter→LLM7."""
    assert make_providers(_FakeSession()) == []

    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    monkeypatch.setenv("LLM7_API_KEY", "l7")
    providers = make_providers(_FakeSession())
    assert [p.name for p in providers] == ["openrouter", "llm7"]

    monkeypatch.setenv("GEMINI_API_KEY", "gm")
    providers = make_providers(_FakeSession())
    assert [p.name for p in providers] == ["gemini", "openrouter", "llm7"]
    print("  ok: каскад и порядок провайдеров")


# ============================================================
# Gemini: выбор модели и URL
# ============================================================
def test_gemini_model_priority(monkeypatch):
    """GEMINI_MODEL → AI_MODEL (только gemini-модель) → значение по умолчанию."""
    provider = GeminiProvider(_FakeSession())
    assert provider._model == "gemini-flash-latest"

    monkeypatch.setenv("AI_MODEL", "default")     # селектор LLM7, не Gemini
    assert provider._model == "gemini-flash-latest"

    monkeypatch.setenv("AI_MODEL", "gemini-2.0-pro")
    assert provider._model == "gemini-2.0-pro"

    monkeypatch.setenv("GEMINI_MODEL", "gemini-3-flash")
    assert provider._model == "gemini-3-flash"
    print("  ok: приоритет модели Gemini")


def test_gemini_max_output_tokens(monkeypatch):
    """AI_MAX_OUTPUT_TOKENS: число, мусор и отсутствие значения."""
    provider = GeminiProvider(_FakeSession())
    assert provider._max_output == 2000

    monkeypatch.setenv("AI_MAX_OUTPUT_TOKENS", "512")
    assert provider._max_output == 512

    monkeypatch.setenv("AI_MAX_OUTPUT_TOKENS", "много")
    assert provider._max_output == 2000
    print("  ok: лимит токенов Gemini")


def test_gemini_build_url(monkeypatch):
    """URL по умолчанию подставляет модель; GEMINI_API_URL переопределяет."""
    provider = GeminiProvider(_FakeSession())
    url = provider._build_url()
    assert url.startswith("https://generativelanguage.googleapis.com/")
    assert "gemini-flash-latest" in url
    assert url.endswith(":generateContent")

    monkeypatch.setenv("GEMINI_API_URL",
                       "https://proxy.test/v1/models/{model}:generateContent")
    assert provider._build_url() == \
        "https://proxy.test/v1/models/gemini-flash-latest:generateContent"
    print("  ok: URL Gemini")


# ============================================================
# Gemini: запрос и разбор ответа
# ============================================================
def test_gemini_request_shape(monkeypatch):
    """System уходит в system_instruction, ключ — только в x-goog-api-key."""
    monkeypatch.setenv("GEMINI_API_KEY", "GEM-KEY")
    session = _FakeSession(_FakeResponse(payload=_gemini_payload("ответ")))
    provider = GeminiProvider(session, timeout=12)

    answer = _complete(provider, [
        {"role": "system", "content": "инструкция"},
        {"role": "system", "content": "ещё инструкция"},
        {"role": "user", "content": "вопрос"},
        {"role": "user", "content": "   "},          # пустое — пропускается
    ])

    assert answer == "ответ"
    assert provider.requests == 1

    call = session.calls[0]
    assert call["headers"]["x-goog-api-key"] == "GEM-KEY"
    assert "Authorization" not in call["headers"]
    assert call["timeout"].total == 12

    body = call["json"]
    assert body["contents"] == [{"parts": [{"text": "вопрос"}]}]
    assert body["system_instruction"] == {
        "parts": [{"text": "инструкция\nещё инструкция"}]}
    assert body["generationConfig"] == {"maxOutputTokens": 2000}
    print("  ok: тело запроса Gemini")


def test_gemini_system_only_gets_empty_part(monkeypatch):
    """Без user-сообщений всё равно отправляется один contents-элемент."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    session = _FakeSession(_FakeResponse(payload=_gemini_payload("ok")))
    provider = GeminiProvider(session)

    _complete(provider, [{"role": "system", "content": "только система"}])
    body = session.calls[0]["json"]
    assert body["contents"] == [{"parts": [{"text": ""}]}]
    assert body["system_instruction"]["parts"][0]["text"] == "только система"
    print("  ok: пустой contents не ломает запрос")


def test_gemini_without_key_makes_no_request(monkeypatch):
    """Без ключа complete() возвращает None и не трогает сеть."""
    session = _FakeSession(_FakeResponse(payload=_gemini_payload("не должно быть")))
    provider = GeminiProvider(session)
    assert _complete(provider) is None
    assert session.calls == []
    assert provider.requests == 0
    print("  ok: без ключа нет запроса")


def test_gemini_extract_joins_parts(monkeypatch):
    """Текст склеивается из всех candidates/parts, мусор пропускается."""
    payload = {"candidates": [
        {"content": {"parts": [{"text": "первый"}, {"text": "второй"}]}},
        "строка вместо кандидата",
        {"content": {"parts": [{"text": "третий"}, {"no_text": 1}, "мусор"]}},
    ]}
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    provider = GeminiProvider(_FakeSession(_FakeResponse(payload=payload)))
    assert _complete(provider) == "первый\nвторой\nтретий"
    print("  ok: разбор candidates Gemini")


def test_gemini_extract_empty_returns_none(monkeypatch):
    """Пустые candidates/parts/текст — None, а не пустая строка."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    for payload in ({}, {"candidates": []},
                    {"candidates": [{"content": {"parts": []}}]},
                    {"candidates": [{"content": {"parts": [{"text": "  "}]}}]}):
        provider = GeminiProvider(_FakeSession(_FakeResponse(payload=payload)))
        assert _complete(provider) is None
    print("  ok: пустой ответ Gemini = None")


@pytest.mark.parametrize("status, expected", [
    (401, "авторизация 401"),
    (403, "авторизация 403"),
    (429, "временный сбой 429"),
    (500, "временный сбой 500"),
    (400, "неверный запрос"),
    (404, "модель не найдена"),
    (418, "HTTP 418"),
])
def test_gemini_http_errors(monkeypatch, status, expected):
    """Любой не-200: None, запись в статистику и понятная причина."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    session = _FakeSession(_FakeResponse(
        status=status, body='{"error": "подробности от Google"}'))
    provider = GeminiProvider(session)

    assert _complete(provider) is None
    assert expected in provider.last_error
    assert provider.last_status == status
    assert provider.failures == 1
    assert provider.requests == 1
    print(f"  ok: Gemini HTTP {status}")


def test_gemini_non_dict_payload(monkeypatch):
    """Ответ не-объект (список/строка) — None, без падения."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    provider = GeminiProvider(_FakeSession(_FakeResponse(payload=["не объект"])))
    assert _complete(provider) is None
    print("  ok: не-объект в ответе Gemini")


def test_gemini_broken_json(monkeypatch):
    """200 с невалидным JSON — None и отдельная причина, без исключения."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    session = _FakeSession(_FakeResponse(
        payload=None, json_error=ValueError("Expecting value")))
    provider = GeminiProvider(session)

    assert _complete(provider) is None
    assert "JSON" in provider.last_error
    assert provider.failures == 1
    print("  ok: битый JSON Gemini")


def test_gemini_timeout(monkeypatch):
    """Таймаут — None, last_status остаётся пустым, ошибка в статистике."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    provider = GeminiProvider(_FakeSession(error=asyncio.TimeoutError()), timeout=5)
    assert _complete(provider) is None
    assert provider.last_status is None
    assert provider.last_error.startswith("таймаут")
    assert provider.failures == 1
    print("  ok: таймаут Gemini")


def test_gemini_network_error(monkeypatch):
    """Сетевая ошибка aiohttp не пробрасывается наружу."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    provider = GeminiProvider(
        _FakeSession(error=aiohttp.ClientError("connection reset")))
    assert _complete(provider) is None
    assert "сетевой сбой" in provider.last_error
    assert "connection reset" in provider.last_error
    print("  ok: сетевой сбой Gemini")


# ============================================================
# OpenAI-совместимые провайдеры
# ============================================================
def test_openai_request_shape(monkeypatch):
    """URL /chat/completions, Bearer-ключ, model и max_tokens в теле."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "OR-KEY")
    session = _FakeSession(_FakeResponse(
        payload=_openai_payload("  ответ модели  ")))
    provider = OpenRouterProvider(session, timeout=9)

    messages = [{"role": "user", "content": "вопрос"}]
    answer = _run(provider.complete(messages))

    assert answer == "ответ модели"          # пробелы обрезаны
    call = session.calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer OR-KEY"
    assert call["timeout"].total == 9
    assert call["json"]["model"] == "gpt-4o-mini"
    assert call["json"]["max_tokens"] == 2000
    assert call["json"]["messages"] == messages
    print("  ok: тело запроса OpenRouter")


def test_openai_base_url_and_model_from_env(monkeypatch):
    """Хвостовой слэш в base URL не даёт двойного слэша; модель из env."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.test/v1/")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude")
    monkeypatch.setenv("AI_MAX_OUTPUT_TOKENS", "64")
    session = _FakeSession(_FakeResponse(payload=_openai_payload("ok")))
    provider = OpenRouterProvider(session)

    _complete(provider)
    call = session.calls[0]
    assert call["url"] == "https://proxy.test/v1/chat/completions"
    assert call["json"]["model"] == "anthropic/claude"
    assert call["json"]["max_tokens"] == 64
    print("  ok: base URL и модель из окружения")


def test_openai_without_key_makes_no_request(monkeypatch):
    """Без ключа сеть не трогается."""
    session = _FakeSession(_FakeResponse(payload=_openai_payload("нет")))
    provider = OpenRouterProvider(session)
    assert _complete(provider) is None
    assert session.calls == []
    print("  ok: без ключа нет запроса (OpenAI-совместимый)")


def test_openai_extract_error_payload(monkeypatch):
    """200 с полем error — это ошибка, а не ответ."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    provider = OpenRouterProvider(_FakeSession(_FakeResponse(
        payload={"error": {"message": "quota exceeded"}})))
    assert _complete(provider) is None
    print("  ok: error в теле ответа")


def test_openai_extract_empty_choices(monkeypatch):
    """Нет choices / пустой content / content=None — None."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    for payload in ({}, {"choices": []}, {"choices": [{"message": {"content": " "}}]},
                    {"choices": [{"message": {}}]},
                    {"choices": [{"message": {"content": None}}]}):
        provider = OpenRouterProvider(_FakeSession(_FakeResponse(payload=payload)))
        assert _complete(provider) is None
    print("  ok: пустой ответ OpenAI-совместимого API = None")


@pytest.mark.parametrize("status, expected", [
    (401, "авторизация 401"),
    (403, "авторизация 403"),
    (429, "временный сбой 429"),
    (500, "временный сбой 500"),
    (503, "временный сбой 503"),
    (400, "неверный запрос (проверь модель)"),
    (418, "HTTP 418"),
])
def test_openai_http_errors(monkeypatch, status, expected):
    """Те же правила failover, что и у Gemini."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    provider = OpenRouterProvider(_FakeSession(_FakeResponse(
        status=status, body="провайдер ответил ошибкой")))
    assert _complete(provider) is None
    assert expected in provider.last_error
    assert provider.last_status == status
    assert provider.failures == 1
    print(f"  ok: OpenRouter HTTP {status}")


def test_openai_non_dict_payload(monkeypatch):
    """Ответ не-объект — None."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    provider = OpenRouterProvider(_FakeSession(_FakeResponse(payload="строка")))
    assert _complete(provider) is None
    print("  ok: не-объект в ответе OpenRouter")


def test_openai_max_output_tokens(monkeypatch):
    """AI_MAX_OUTPUT_TOKENS: число и мусор."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    provider = OpenRouterProvider(_FakeSession())
    assert provider._max_output == 2000

    monkeypatch.setenv("AI_MAX_OUTPUT_TOKENS", "333")
    assert provider._max_output == 333

    monkeypatch.setenv("AI_MAX_OUTPUT_TOKENS", "много")
    assert provider._max_output == 2000
    print("  ok: лимит токенов OpenAI-совместимого API")


def test_openai_broken_json(monkeypatch):
    """Невалидный JSON в ответе — None с отдельной причиной."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    provider = OpenRouterProvider(_FakeSession(_FakeResponse(
        payload=None, json_error=ValueError("bad json"))))
    assert _complete(provider) is None
    assert "JSON" in provider.last_error
    print("  ok: битый JSON OpenRouter")


def test_openai_timeout_and_network_error(monkeypatch):
    """Таймаут и ClientError обрабатываются одинаково для всех провайдеров."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    timeout_provider = OpenRouterProvider(
        _FakeSession(error=asyncio.TimeoutError()), timeout=3)
    assert _complete(timeout_provider) is None
    assert "таймаут" in timeout_provider.last_error
    assert timeout_provider.last_status is None

    net_provider = OpenRouterProvider(
        _FakeSession(error=aiohttp.ClientError("dns fail")))
    assert _complete(net_provider) is None
    assert "сетевой сбой" in net_provider.last_error
    print("  ok: таймаут и сетевой сбой OpenRouter")


# ============================================================
# LLM7: свои селекторы моделей
# ============================================================
def test_llm7_model_selector(monkeypatch):
    """LLM7 по умолчанию `default`; AI_MODEL (модель Gemini) игнорируется."""
    monkeypatch.setenv("LLM7_API_KEY", "l7")
    monkeypatch.setenv("AI_MODEL", "gemini-2.5-flash")
    provider = LLM7Provider(_FakeSession())
    assert provider._model == "default"

    monkeypatch.setenv("LLM7_MODEL", "pro")
    assert provider._model == "pro"

    assert provider._base_url == "https://api.llm7.io/v1"
    print("  ok: селекторы моделей LLM7")


# ============================================================
# Новые провайдеры из списка free-llm-api-resources
# ============================================================
def _url_and_model(provider):
    return provider._base_url, provider._model


def test_new_providers_defaults(monkeypatch):
    """У каждого нового провайдера — свой URL и модель по умолчанию."""
    cases = [
        (GroqProvider, "GROQ_API_KEY",
         "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
        (CerebrasProvider, "CEREBRAS_API_KEY",
         "https://api.cerebras.ai/v1", "gpt-oss-120b"),
        (MistralProvider, "MISTRAL_API_KEY",
         "https://api.mistral.ai/v1", "mistral-small-latest"),
        (GitHubModelsProvider, "GITHUB_MODELS_TOKEN",
         "https://models.github.ai/inference", "openai/gpt-4o-mini"),
        (NvidiaProvider, "NVIDIA_API_KEY",
         "https://integrate.api.nvidia.com/v1",
         "meta/llama-3.3-70b-instruct"),
        (SambaNovaProvider, "SAMBANOVA_API_KEY",
         "https://api.sambanova.ai/v1", "Meta-Llama-3.3-70B-Instruct"),
        (CohereProvider, "COHERE_API_KEY",
         "https://api.cohere.ai/compatibility/v1", "command-a-03-2025"),
        (HuggingFaceProvider, "HUGGINGFACE_API_KEY",
         "https://router.huggingface.co/v1",
         "meta-llama/Llama-3.1-8B-Instruct"),
    ]
    for cls, env_key, base, model in cases:
        provider = cls(_FakeSession())
        assert provider.enabled is False
        monkeypatch.setenv(env_key, "k")
        assert provider.enabled is True
        assert _url_and_model(provider) == (base, model), cls.__name__
        monkeypatch.delenv(env_key)


def test_cloudflare_requires_account_id(monkeypatch):
    """Cloudflare без account id выключен, с ним — собирает URL аккаунта."""
    provider = CloudflareProvider(_FakeSession())
    assert provider.enabled is False

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    assert provider.enabled is False
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")
    assert provider.enabled is True
    assert provider._base_url == \
        "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1"


def test_cascade_order_with_many_keys(monkeypatch):
    """Порядок каскада: быстрые бесплатные тиры идут раньше LLM7."""
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY",
                "MISTRAL_API_KEY", "GITHUB_MODELS_TOKEN", "OPENROUTER_API_KEY",
                "NVIDIA_API_KEY", "SAMBANOVA_API_KEY", "COHERE_API_KEY",
                "HUGGINGFACE_API_KEY", "LLM7_API_KEY"):
        monkeypatch.setenv(key, "k")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc")

    names = [p.name for p in make_providers(_FakeSession())]
    assert names == [
        "gemini", "groq", "cerebras", "mistral", "github", "openrouter",
        "nvidia", "sambanova", "cohere", "huggingface", "cloudflare", "llm7",
    ], names
    print("  ok: полный каскад провайдеров")


# ============================================================
# Пауза после исчерпанного лимита
# ============================================================
def test_429_sets_cooldown_and_availability(monkeypatch):
    """429 уводит провайдера в паузу: его пропускают, пока она идёт."""
    monkeypatch.setenv("GROQ_API_KEY", "k")
    session = _FakeSession(_FakeResponse(429, body="rate limit"))
    provider = GroqProvider(session)

    assert provider.available is True
    assert _complete(provider) is None
    assert provider.available is False
    assert provider.cooldown_left > 0
    assert "пауза после лимита" in provider.status_line()
    print("  ok: 429 ставит провайдера в паузу")


def test_auth_error_sets_long_cooldown(monkeypatch):
    """401/403 — ключ неверный: длинная пауза, а не дёрганье каждый раз."""
    monkeypatch.setenv("CEREBRAS_API_KEY", "bad")
    provider = CerebrasProvider(_FakeSession(_FakeResponse(401, body="bad key")))

    assert _complete(provider) is None
    assert provider.available is False
    assert provider.cooldown_left > 600
    print("  ok: неверный ключ — длинная пауза")


def test_success_clears_cooldown(monkeypatch):
    """Успешный ответ снимает паузу."""
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    provider = MistralProvider(
        _FakeSession(_FakeResponse(200, payload=_openai_payload("привет"))))
    provider._start_cooldown(429)

    assert provider.available is False
    assert _complete(provider) == "привет"
    assert provider.available is True
    assert provider.cooldown_left == 0
    print("  ok: успех снимает паузу")


AI_ENV_KEYS = ("GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY",
               "MISTRAL_API_KEY", "GITHUB_MODELS_TOKEN", "GITHUB_TOKEN",
               "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "SAMBANOVA_API_KEY",
               "COHERE_API_KEY", "HUGGINGFACE_API_KEY",
               "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
               "LLM7_API_KEY")


def test_provider_catalog_lists_every_provider(monkeypatch):
    """/ai_status видит все 12 провайдеров, включая выключенные."""
    for key in AI_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    catalog = provider_catalog()

    assert len(catalog) == 12
    assert all(item["enabled"] is False for item in catalog)
    names = [item["name"] for item in catalog]
    assert names[0] == "gemini" and names[-1] == "llm7"
    # У каждого есть переменная ключа, которую можно показать админу.
    assert all(item["env"] for item in catalog)


def test_provider_catalog_marks_only_configured(monkeypatch):
    """Ключ есть только у Groq — активен только он."""
    for key in AI_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    enabled = [item["name"] for item in provider_catalog()
               if item["enabled"]]

    assert enabled == ["groq"]


def test_provider_catalog_cloudflare_needs_account_id(monkeypatch):
    """Cloudflare без account id — не активен, хотя токен есть."""
    for key in AI_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token")
    catalog = {item["name"]: item for item in provider_catalog()}
    assert catalog["cloudflare"]["enabled"] is False

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    catalog = {item["name"]: item for item in provider_catalog()}
    assert catalog["cloudflare"]["enabled"] is True
    assert "CLOUDFLARE_ACCOUNT_ID" in catalog["cloudflare"]["env"]


def test_gemini_falls_back_to_discovered_model(monkeypatch):
    """404 на настроенной модели: ListModels и повтор с доступной моделью."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-removed-model")
    payload = {"candidates": [{"content": {"parts": [{"text": "ОК"}]}}]}
    models = {"models": [
        {"name": "models/gemini-9-pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-9-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-embedding-1", "supportedGenerationMethods": ["embedContent"]},
    ]}

    class _SeqSession(_FakeSession):
        def __init__(self):
            super().__init__(get_response=_FakeResponse(payload=models))
            self._responses = [
                _FakeResponse(status=404, body='{"error": "not found"}'),
                _FakeResponse(payload=payload),
            ]

        def post(self, url, **kwargs):
            self.calls.append({"url": url, **kwargs})
            return _FakePost(self._responses.pop(0))

    session = _SeqSession()
    provider = GeminiProvider(session)

    assert _complete(provider) == "ОК"
    assert provider._model == "gemini-9-flash"
    assert len(session.get_calls) == 1
    assert "gemini-9-flash" in session.calls[-1]["url"]
    assert provider.last_status == 200
    assert provider.last_error == ""
    print("  ok: Gemini переключился на доступную модель")


def test_gemini_404_without_model_list_keeps_error(monkeypatch):
    """404 и пустой список моделей: понятная ошибка, без падения."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    session = _FakeSession(
        _FakeResponse(status=404, body='{"error": "not found"}'),
        get_response=_FakeResponse(payload={"models": []}))
    provider = GeminiProvider(session)

    assert _complete(provider) is None
    assert "модель не найдена" in provider.last_error
    assert provider.last_status == 404
    print("  ok: 404 без резервной модели — ошибка остаётся")


def test_openai_html_200_is_reported_and_cooled_down(monkeypatch):
    """200 с HTML вместо JSON: понятная причина и пауза, без ложного успеха."""
    monkeypatch.setenv("GITHUB_MODELS_TOKEN", "k")
    session = _FakeSession(_FakeResponse(
        status=200, body="<html><body>Sign in</body></html>",
        json_error=ValueError("Expecting value"),
        headers={"Content-Type": "text/html"}))
    provider = GitHubModelsProvider(session)

    assert _complete(provider) is None
    assert "не JSON" in provider.last_error
    assert "text/html" in provider.last_error
    assert provider.cooldown_left > 0, "без паузы бот будет долбить шлюз"
    print("  ok: HTML вместо JSON — ошибка и пауза")


def test_openai_error_payload_with_200(monkeypatch):
    """200 с объектом error внутри — ошибка API, а не пустой ответ."""
    monkeypatch.setenv("GITHUB_MODELS_TOKEN", "k")
    provider = GitHubModelsProvider(_FakeSession(_FakeResponse(
        payload={"error": {"message": "quota exceeded"}})))

    assert _complete(provider) is None
    assert "quota exceeded" in provider.last_error
    assert provider.last_status == 400
    print("  ok: error внутри 200 распознан")


def test_openai_sse_body_is_parsed(monkeypatch):
    """SSE-ответ (data: {...}) разбирается как обычный JSON."""
    monkeypatch.setenv("GROQ_API_KEY", "k")
    body = ('data: {"choices": [{"message": {"content": "привет"}}]}\n'
            "data: [DONE]\n")
    provider = GroqProvider(_FakeSession(_FakeResponse(
        body=body, json_error=ValueError("Expecting value"))))

    assert _complete(provider) == "привет"
    print("  ok: SSE-ответ провайдера разобран")


def test_status_line_shows_human_cooldown(monkeypatch):
    """Пауза в /health — в часах и минутах, а не в секундах."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    provider = OpenRouterProvider(_FakeSession())
    provider._record(429, "лимит", retry_after=7200)

    line = provider.status_line()

    assert "2 ч" in line
    assert "7200" not in line
