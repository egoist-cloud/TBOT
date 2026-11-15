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
    "Помощь по правилам клуба TALENT на cs2red.ru (CS2). Отвечай по-русски."
    "Только по тексту правил из запроса, кратко (максимум 3-4 предложения)."
    "Без предисловий, без «В представленных правилах нет информации». "
    "Если правильного ответа нет — пиши ОТВЕТ: Нет данных.\n\n"
    "Если пользователь спрашивает про конкретный пункт (например, '2.2') — "
    "используй его текст и в ОСНОВАНИЕ укажи только номер («2.2»), без текста.\n"
    "Если пользователь спрашивает про оскорбления, мат или ругань — "
    "ищи в правилах пункт 2.2 (Правила общения).\n"
    "Если вопрос про VIP (ультра, экстрим, паблик, минигейм, wwdm, тюрьма и т.п.) — "
    "ищи ТОЛЬКО в разделе VIP (пункты 7.1–7.31) и укажи конкретный номер.\n"
    "Если вопрос касается VIP-привилегий (пункты 7.1–7.31), форматируй поле ОТВЕТ "
    "в виде списка с маркерами (используй - или •). Разделяй блоки на логические "
    "части (например «Лимиты обороны:», «VIP-меню:», «Дополнительно:»). "
    "Не пиши всё в одну строку.\n"
    "Если вопрос касается VIP-привилегий, ты ОБЯЗАН вывести ВСЕ возможности "
    "из найденного пункта (лимиты, VIP-меню, регенерацию, прыжки, скидки, доход "
    "и т.д.), а не только первое предложение. Форматируй ответ списком.\n"
    "Для VIP-пунктов (7.x) НАКАЗАНИЕ не предусмотрено — ставь '-' (это описание функций, "
    "а не санкция).\n\n"
    "НАКАЗАНИЕ (обязательно):\n"
    "Если в найденном тексте правила есть строка с наказанием (например, "
    "«Устное предупреждение...», «Блокировка игры на...», «БИП...», «МУТ...»), "
    "ты ОБЯЗАН вывести её в поле НАКАЗАНИЕ. Не пиши «нет данных», если в тексте "
    "правила есть хотя бы одно слово про наказание. Если наказаний несколько — "
    "перечисли их. Только если в правиле реально нет никаких санкций — ставь '-'.\n\n"
    "Формат (строго, по строке на поле):\n"
    "ОТВЕТ: <суть, 1-4 предложения>\n"
    "НАКАЗАНИЕ: <что грозит, иначе '-'>\n"
    "ОСНОВАНИЕ: <только номера пунктов через запятую, например 2.2>\n"
    "Больше ничего не пиши."
)

# Режим обычного вопроса: правила не нашлись, но ИИ знает проект и диалог.
_SYSTEM_CHAT_PROMPT = (
    "Ты — ИИ-помощник Discord-бота игрового клуба TALENT (CS2, cs2red.ru). "
    "Отвечай по-русски, кратко и по делу (обычно 1-5 предложений).\n"
    "Используй историю диалога и информацию о проекте ниже. Не выдумывай "
    "команды, роли и правила, которых там нет; если ответа нет — скажи, что "
    "не знаешь, и предложи спросить у администратора.\n"
    "Не используй поля ОТВЕТ/НАКАЗАНИЕ/ОСНОВАНИЕ — это разговорный ответ."
)

_DATA_DIR_ENV = "DATA_DIR"


def _default_data_dir() -> str:
    return os.getenv(_DATA_DIR_ENV, ".")


def _build_messages(query: str, rules_context: str, examples: str,
                    history=None, project_context: str = "",
                    general: bool = False) -> list:
    """Промпт: системная инструкция + знания проекта + история диалога.

    history — прошлые реплики [{"role": "user"/"assistant", "content": ...}],
    чтобы ИИ понимал продолжение разговора («а за это какое наказание?»).
    project_context — факты о боте/командах/сайте из ai_project.md.
    general — вопрос не про правила: обычный ответ без полей формата.
    """
    if general:
        system = _SYSTEM_CHAT_PROMPT
        if project_context:
            system += ("\n\nИНФОРМАЦИЯ О ПРОЕКТЕ (бот клуба TALENT, команды, "
                       f"роли, сайт, кланы):\n{project_context}")
        user_block = f"ВОПРОС: {query}"
        if examples:
            user_block += f"\n\nПРИМЕРЫ ОТВЕТОВ (шаблон стиля):\n{examples}"
    else:
        system = _SYSTEM_PROMPT
        if project_context:
            system += (
                "\n\nИНФОРМАЦИЯ О ПРОЕКТЕ (бот клуба TALENT, команды, роли, "
                f"сайт, кланы):\n{project_context}")
        user_block = (f"ПРАВИЛА (релевантные пункты):\n{rules_context}\n\n"
                      f"ВОПРОС ИГРОКА: {query}")
        if examples:
            user_block += f"\n\nПРИМЕРЫ ОТВЕТОВ (шаблон стиля):\n{examples}"
    messages: list = [{"role": "system", "content": system}]
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:1500]})
    messages.append({"role": "user", "content": user_block})
    return messages


class AIService:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        data_dir: str | None = None,
        top_k: int = 3,
    ):
        self.session = session
        self.data_dir = data_dir or _default_data_dir()
        self.top_k = top_k
        self.providers = make_providers(session)
        self.limiter = RateLimiter()
        self.rules = RulesIndex(self.data_dir)
        self._cache_warm = False
        self.project_context = self._load_project_context()

    def _load_project_context(self) -> str:
        """Факты о проекте из ai_project.md (если файл есть).

        Ищем в DATA_DIR, а если там нет (на хостинге DATA_DIR может быть
        отдельным томом) — рядом с кодом, в корне проекта.
        """
        name = os.getenv("AI_PROJECT_FILE") or "ai_project.md"
        path = Path(name)
        if path.is_absolute():
            candidates = [path]
        else:
            project_root = Path(__file__).resolve().parents[2]
            candidates = [Path(self.data_dir) / name,
                          project_root / name]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate.read_text(encoding="utf-8").strip()[:4000]
            except Exception as e:
                log.warning("ai_project.md (%s): %s", candidate, e)
        return ""

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
    async def ask(self, user_id, query: str, history=None) -> dict:
        """Главный метод. Возвращает словарь:
        {"ok": bool, "response": str, "rules_used": [ids], "provider": str,
         "cached": bool, "error": str, "limited": bool, "remaining": int}

        history — прошлые реплики диалога (список словарей role/content).
        С историей кэш не используется: ответ зависит от контекста.
        """
        query = (query or "").strip()
        if not query:
            return {"ok": False, "response": "", "rules_used": [],
                    "provider": "", "cached": False,
                    "error": "пустой запрос", "limited": False, "remaining": 0}

        # 1) кэш (только для вопроса без истории диалога)
        if not history:
            cached = await get_cached(query)
            if cached is not None:
                return {"ok": True, "response": cached, "rules_used": [],
                        "provider": "cache", "cached": True, "error": "",
                        "limited": False,
                        "remaining": self.limiter.remaining(user_id)}
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
        messages = _build_messages(query, rules_context, examples, history,
                                   self.project_context,
                                   general=not rules_hits)

        # 5) провайдеры: пропускаем те, что в паузе после лимита/ошибки
        provider = ""
        skipped = 0
        for prov in self.providers:
            if not getattr(prov, "available", True):
                skipped += 1
                log.debug("%s: в паузе после ошибки, пропуск", prov.name)
                continue
            log.debug("%s: запрос", prov.name)
            try:
                result = await prov.complete(messages)
            except Exception as e:
                log.warning("%s: ошибка вызова: %s", prov.name, e)
                continue
            if result:
                provider = prov.name
                log.debug("%s: ответ получен", prov.name)
                break
            log.debug("%s: пустой ответ", prov.name)

        if not provider:
            # Запрос не дошёл до ИИ — не тратим попытку пользователя.
            self.limiter.release_last(user_id)
            if self.providers and skipped == len(self.providers):
                msg = ("Все ИИ-провайдеры временно в паузе после лимита — "
                       "попробуй через несколько минут.")
            else:
                msg = ("Не удалось получить ответ ни от одного ИИ-провайдера. "
                       "Проверь, что задан хотя бы один ключ "
                       "(GEMINI_API_KEY / GROQ_API_KEY / CEREBRAS_API_KEY / "
                       "MISTRAL_API_KEY / OPENROUTER_API_KEY / LLM7_API_KEY).")
            return {"ok": False, "response": "", "rules_used": rules_used,
                    "provider": "", "cached": False, "error": msg,
                    "limited": False, "remaining": self.limiter.remaining(user_id)}

        # 6) кэшируем только ответы без контекста диалога
        if not history:
            await set_cached(query, result)
        return {"ok": True, "response": result, "rules_used": rules_used,
                "provider": provider, "cached": False, "error": "",
                "limited": False, "remaining": self.limiter.remaining(user_id)}