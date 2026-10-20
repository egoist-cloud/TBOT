# -*- coding: utf-8 -*-
"""Главный сервис ИИ-каскада.

Порядок работы AIService.ask():
  1. нормализуем запрос и ищем в кэше (SQLite);
  2. проверяем лимитер пользователя (in-memory);
  3. RAG-поиск по rules.txt -> топ-5 пунктов;
  4. формируем промпт: системная инструкция + найденные правила +
     примеры из ai_training.json (файл опционален);
  5. перебираем провайдеров по очереди (Gemini -> OpenRouter -> LLM7),
     пока кто-то не вернёт непустой ответ;
  6. кладём ответ в кэш и возвращаем вместе с использованными пунктами.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp

from .cache import get_cached, set_cached
from .limiter import RateLimiter
from .providers import make_providers
from .rag import RulesIndex

log = logging.getLogger("quantum.ai")

_SYSTEM_PROMPT = (
    "Ты помощник Discord-бота клана TALENT на игровом проекте cs2red.ru (CS2). "
    "Отвечай по-русски, кратко и по делу (до 900 символов). "
    "Опирайся ТОЛЬКО на текст правил из запроса. Обязательно указывай номера пунктов. "
    "Если ответа в правилах нет — честно скажи, что этого пункта в правилах нет, "
    "и не выдумывай. Не давай юридических советов."
)

_DATA_DIR_ENV = "DATA_DIR"


def _default_data_dir() -> str:
    return os.getenv(_DATA_DIR_ENV, ".")


def _build_messages(query: str, rules_context: str, examples: str) -> list:
    system = _SYSTEM_PROMPT
    user_block = f"ПРАВИЛА (релевантные пункты):\n{rules_context}\n\nВОПРОС ИГРОКА: {query}"
    if examples:
        user_block += f"\n\nПРИМЕРЫ ОТВЕТОВ (шаблон стиля):\n{examples}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_block},
    ]


class AIService:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        data_dir: str | None = None,
        top_k: int = 5,
    ):
        self.session = session
        self.data_dir = data_dir or _default_data_dir()
        self.top_k = top_k
        self.providers = make_providers(session)
        self.limiter = RateLimiter()
        self.rules = RulesIndex(self.data_dir)
        self._cache_warm = False

    # ---------- подготовка ----------
    def ensure_rules(self) -> int:
        """Гарантирует, что правила загружены. Возвращает число пунктов."""
        if not self.rules.ready:
            return self.rules.load()
        return len(self.rules._docs)

    def _load_examples(self) -> str:
        """Читает примеры из ai_training.json'а, если файл есть (опционально)."""
        path = Path(self.data_dir) / "ai_training.json"
        if not path.is_file():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        if not isinstance(data, list):
            return ""
        lines = []
        for item in data[:6]:
            if not isinstance(item, dict):
                continue
            question = item.get("question") or ""
            answer = item.get("answer") or ""
            if question and answer:
                lines.append(f"Вопрос: {question}\nОтвет: {answer}")
        return "\n\n".join(lines)

    # ---------- основной вход ----------
    async def ask(self, user_id, query: str) -> dict:
        """Главный метод. Возвращает словарь:
        {"ok": bool, "response": str, "rules_used": [ids], "provider": str,
         "cached": bool, "error": str, "limited": bool, "remaining": int}
        """
        query = (query or "").strip()
        if not query:
            return {"ok": False, "response": "", "rules_used": [],
                    "provider": "", "cached": False,
                    "error": "пустой запрос", "limited": False, "remaining": 0}

        # 1) кэш
        cached = await get_cached(query)
        if cached is not None:
            return {"ok": True, "response": cached, "rules_used": [],
                    "provider": "cache", "cached": True, "error": "",
                    "limited": False, "remaining": self.limiter.remaining(user_id)}
        if not self._cache_warm:
            from .cache import setup_cache_cleanup
            await setup_cache_cleanup()
            self._cache_warm = True

        # 2) лимитер
        if not self.limiter.allow(user_id):
            return {"ok": False, "response": "", "rules_used": [],
                    "provider": "", "cached": False,
                    "error": "лимит запросов исчерпан",
                    "limited": True,
                    "remaining": self.limiter.remaining(user_id)}

        # 3) RAG
        self.ensure_rules()
        rules_hits = self.rules.search(query, top_k=self.top_k)
        rules_used = [h["id"] for h in rules_hits]
        if rules_hits:
            rules_context = "\n\n".join(
                f"ПУНКТ {h['id']}: {h['text']}" for h in rules_hits)
        else:
            rules_context = "Релевантные пункты не найдены."

        # 4) промпт
        examples = await asyncio.to_thread(self._load_examples)
        messages = _build_messages(query, rules_context, examples)

        # 5) провайдеры
        provider = ""
        for prov in self.providers:
            print(f"DEBUG: Пробую провайдера {prov.name}")
            try:
                result = await prov.complete(messages)
            except Exception as e:
                print(f"DEBUG: Провайдер {prov.name} бросил исключение: {e}")
                log.warning("%s: ошибка вызова: %s", prov.name, e)
                continue
            if result:
                provider = prov.name
                print(f"DEBUG: Провайдер {prov.name} ответил успешно")
                break
            else:
                print(f"DEBUG: Провайдер {prov.name} вернул None")

        if not provider:
            msg = ("Не удалось получить ответ ни от одного ИИ-провайдера. "
                   "Проверь, что задан хотя бы один ключ "
                   "(GEMINI_API_KEY / OPENROUTER_API_KEY / LLM7_API_KEY).")
            return {"ok": False, "response": "", "rules_used": rules_used,
                    "provider": "", "cached": False, "error": msg,
                    "limited": False, "remaining": self.limiter.remaining(user_id)}

        # 6) кэшируем
        await set_cached(query, result)
        return {"ok": True, "response": result, "rules_used": rules_used,
                "provider": provider, "cached": False, "error": "",
                "limited": False, "remaining": self.limiter.remaining(user_id)}