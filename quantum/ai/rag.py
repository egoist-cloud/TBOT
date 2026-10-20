# -*- coding: utf-8 -*-
"""Локальный поиск по правилам через BM25 (чистый Python, без внешних библиотек).

Что делает:
  * читает rules.txt из DATA_DIR (по умолчанию рядом с ботом);
  * разбивает текст на «документы» — отдельные пункты вида «2.2. ...»;
  * индексирует их токенами и ищет по BM25-скорингу;
  * отдаёт топ-k найденных пунктов с их id и текстом.

Без внешних сервисов и зависимостей.
"""
from __future__ import annotations

import math
import os
import re
import threading
from collections import Counter
from pathlib import Path

# Пункт в начале строки: «2.2», «2.2.», «2.2)», «п. 2.2»
CLAUSE_RE = re.compile(
    r"^\s*(?:п\.?\s*|пункт\s*)?(\d{1,2}(?:\.\d{1,2}){1,3})\s*[.)\-:]?\s*(.*)$",
    re.IGNORECASE,
)
# Внутри строки: токены для индекса
TOKEN_RE = re.compile(r"[а-яёa-z0-9]{3,}", re.IGNORECASE)

_DEFAULT_RULES_FILE = "rules.txt"

_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text or "")]


def _load_rules_text(data_dir) -> str:
    path = Path(data_dir) / _DEFAULT_RULES_FILE
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _split_rules(text: str) -> list[dict]:
    """Разбирает текст правил на пункты {id, text}."""
    found: list[dict] = []
    current_id: str | None = None
    current_chunks: list[str] = []

    def flush():
        nonlocal current_id, current_chunks
        if current_id:
            body = " ".join(c.strip() for c in current_chunks if c and c.strip()).strip()
            if body:
                found.append({"id": current_id, "text": body})
        current_chunks = []

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            if current_id:
                current_chunks.append("")
            continue
        m = CLAUSE_RE.match(line)
        if m:
            flush()
            current_id = m.group(1)
            rest = m.group(2).strip()
            current_chunks = [rest] if rest else []
            continue
        if current_id is not None:
            current_chunks.append(line)

    flush()
    return found


class RulesIndex:
    def __init__(self, data_dir=None):
        self.data_dir = os.getenv("DATA_DIR", ".") if data_dir is None else data_dir
        self._docs: list[dict] = []
        self._tokens: list[Counter] = []
        self._df: Counter = Counter()
        self._loaded: float = 0.0
        self._lock = threading.Lock()

    def load(self) -> int:
        """Перечитывает rules.txt (идемпотентно). Возвращает число пунктов."""
        text = _load_rules_text(self.data_dir)
        docs = _split_rules(text)
        tokens = []
        df: Counter = Counter()
        for doc in docs:
            toks = _tokenize(doc["text"])
            tokens.append(Counter(toks))
            df.update(set(toks))
        with self._lock:
            self._docs = docs
            self._tokens = tokens
            self._df = df
        return len(docs)

    @property
    def ready(self) -> bool:
        return bool(self._docs)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Возвращает [{id, text, score}] — топ пунктов по BM25."""
        with self._lock:
            docs = list(self._docs)
            tokens = list(self._tokens)
            df = dict(self._df)
        q_tokens = _tokenize(query)
        if not q_tokens or not docs:
            return []

        n = len(docs)
        avgdl = sum(sum(c.values()) for c in tokens) / n if n else 0.0
        idf_cache: dict[str, float] = {}
        for t in set(q_tokens):
            df_t = df.get(t, 0)
            idf_cache[t] = math.log(1 + (n - df_t + 0.5) / (df_t + 0.5))

        scored = []
        for idx, doc in enumerate(docs):
            counter = tokens[idx]
            dl = sum(counter.values())
            score = 0.0
            for t in set(q_tokens):
                tf = counter.get(t, 0)
                if tf == 0:
                    continue
                denom = tf + _K1 * (1 - _B + _B * (dl / avgdl if avgdl else 1.0))
                score += idf_cache[t] * (_K1 + 1) * tf / denom
            if score > 0:
                scored.append({"id": doc["id"], "text": doc["text"], "score": score})

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]


def search_rules(query: str, top_k: int = 5, index: RulesIndex | None = None) -> list[dict]:
    """Удобная функция: создаёт/использует индекс и ищет по правилам."""
    if index is None:
        index = RulesIndex()
        index.load()
    if not index.ready:
        index.load()
    return index.search(query, top_k=top_k)