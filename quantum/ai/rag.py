# -*- coding: utf-8 -*-
"""Локальный поиск по правилам через BM25 (чистый Python, без внешних библиотек).

Что делает:
  * читает rules.txt из DATA_DIR (по умолчанию рядом с ботом);
  * разбивает текст на «документы» — отдельные пункты вида «2.2. ...»;
  * индексирует их токенами и ищет по BM25-скорингу;
  * нормализует текст: нижний регистр, снятие пунктуации, транслит
    кириллицы -> латиницы (чтобы «ультра» совпадало с «ULTRA» и т.п.);
  * если в запросе есть номер пункта (например «2.2») — в первую очередь
    ищет точное совпадение по номеру, а не по словам;
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
# Номер пункта внутри строки/запроса: «2.2», «п. 2.2», «пункт 2.2».
CLAUSE_NUM_RE = re.compile(r"\b(\d{1,2}(?:\.\d{1,2}){1,3})\b")
# Токены для индекса (после транслитерации — только a-z0-9)
TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

_DEFAULT_RULES_FILE = "rules.txt"

_K1 = 1.5
_B = 0.75

# Кириллица -> латиница (транслитерация для поиска по русским словам и их
# латинским аналогам «ультра»->«ultra», «паблик»->«public»).

_TRANS = {
    ord("а"): "a", ord("б"): "b", ord("в"): "v", ord("г"): "g", ord("д"): "d",
    ord("е"): "e", ord("ё"): "e", ord("ж"): "zh", ord("з"): "z", ord("и"): "i",
    ord("й"): "i", ord("к"): "k", ord("л"): "l", ord("м"): "m", ord("н"): "n",
    ord("о"): "o", ord("п"): "p", ord("р"): "r", ord("с"): "s", ord("т"): "t",
    ord("у"): "u", ord("ф"): "f", ord("х"): "h", ord("ц"): "c", ord("ч"): "ch",
    ord("ш"): "sh", ord("щ"): "sch", ord("ъ"): "", ord("ы"): "y", ord("ь"): "",
    ord("э"): "e", ord("ю"): "iu", ord("я"): "ia",
    ord("А"): "a", ord("Б"): "b", ord("В"): "v", ord("Г"): "g", ord("Д"): "d",
    ord("Е"): "e", ord("Ё"): "e", ord("Ж"): "zh", ord("З"): "z", ord("И"): "i",
    ord("Й"): "i", ord("К"): "k", ord("Л"): "l", ord("М"): "m", ord("Н"): "n",
    ord("О"): "o", ord("П"): "p", ord("Р"): "r", ord("С"): "s", ord("Т"): "t",
    ord("У"): "u", ord("Ф"): "f", ord("Х"): "h", ord("Ц"): "c", ord("Ч"): "ch",
    ord("Ш"): "sh", ord("Щ"): "sch", ord("Ъ"): "", ord("Ы"): "y", ord("Ь"): "",
    ord("Э"): "e", ord("Ю"): "iu", ord("Я"): "ia",
}


# Словарь частоупотребимых русских слов -> английских аналогов (для поиска
# «паблик» ~ PUBLIC, «ультра» ~ ULTRA и т.д.). Применяется до транслитерации.

_WORD_MAP = {
    "паблик": "public", "паблике": "public", "паблике": "public",
    "паблик": "public", "паблика": "public", "паблика": "public",
    "ультра": "ultra", "ультры": "ultra", "ультре": "ultra",
    "экстрим": "extreme", "экстрима": "extreme",
    "медиум": "medium", "медиума": "medium",
    "базовый": "basic", "базовая": "basic", "базового": "basic",
    "лайт": "lite", "лайте": "lite",
    "минигейм": "minigame", "минигейма": "minigame",
    "мини-гейм": "minigame",
    "маньяк": "maniac", "маньяка": "maniac",
    "тюрьма": "jail", "тюрьме": "jail", "тюрьмы": "jail",
    "арена": "arena", "арены": "arena", "арене": "arena",
    "ввдм": "wwdm",
    "авп": "awp",
    "сноусуп": "smoke", "дым": "smoke",
    "паутинка": "web", "паутинки": "web", "паутинку": "web",
    "граб": "grab", "граба": "grab",
    "флеш": "flash", "шприц": "syringe",
    "привилегия": "privilege", "привилегии": "privilege",
    "привилегий": "privilege",
    "правило": "rule", "правила": "rule", "правил": "rule", "правилам": "rule",
    "бан": "ban", "баны": "ban", "блокировка": "ban", "блокировку": "ban",
    "мут": "mute", "мута": "mute",
    "чит": "cheat", "читы": "cheat", "читов": "cheat", "софт": "cheat",
    "скрипт": "script", "скрипты": "script",
}


def _apply_word_map(text: str) -> str:
    """Заменяет русские слова их английскими аналогами (до транслитерации).

    Сортировка по убыванию длины ключа: чтобы «паблике» заменялось целиком,
    а не как «паблик» + «е».
    """
    if not text:
        return text
    low = (text or "").lower()
    for ru, en in sorted(_WORD_MAP.items(), key=lambda kv: len(kv[0]), reverse=True):
        low = low.replace(ru, en)
    return low


def normalize_token(text: str) -> str:
    """Нижний регистр + словесные синонимы + транслит кириллицы -> латиницы."""
    if not text:
        return ""
    return _apply_word_map(text).translate(_TRANS)


def _normalize_text(text: str) -> str:
    """Нормализует текст для токенизации: синонимы + транслит."""
    t = _apply_word_map(text)
    return t.translate(_TRANS)


def _tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(_normalize_text(text))


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
        # карта id -> normalized(text) для поиска по номеру
        self._by_num: dict[str, str] = {}
        self._loaded: float = 0.0
        self._lock = threading.Lock()

        # Предвычисленные для BM25 параметры (читаются только при load()).
        self._n = 0                 # число документов
        self._avgdl = 0.0           # средняя длина документа (в токенах)
        self._doc_len = []          # длина каждого документа (в токенах)
        self._idf_cache: dict[str, float] = {}  # слово -> idf (для всех слов)

    def load(self) -> int:
        """Перечитывает rules.txt (идемпотентно). Возвращает число пунктов.

        При загрузке предвычисляет idf для ВСЕХ слов и удерживает их в памяти,
        чтобы поиск (search) не пересчитывал статистику на каждый запрос.
        """
        text = _load_rules_text(self.data_dir)
        docs = _split_rules(text)
        tokens = []
        df: Counter = Counter()
        by_num: dict[str, str] = {}
        for doc in docs:
            by_num[doc["id"]] = doc["text"]
            toks = _tokenize(doc["text"])
            tokens.append(Counter(toks))
            df.update(set(toks))

        n = len(docs)
        doc_len = [sum(c.values()) for c in tokens]
        avgdl = (sum(doc_len) / n) if n else 0.0
        # Предвычисляем idf для всех встречающихся слов.
        idf_cache: dict[str, float] = {}
        for word, df_t in df.items():
            idf_cache[word] = math.log(1 + (n - df_t + 0.5) / (df_t + 0.5))

        with self._lock:
            self._docs = docs
            self._tokens = tokens
            self._df = df
            self._by_num = by_num
            self._n = n
            self._avgdl = avgdl
            self._doc_len = doc_len
            self._idf_cache = idf_cache
        return len(docs)

    @property
    def ready(self) -> bool:
        return bool(self._docs)

    def lookup_number(self, query: str):
        """Если в запросе есть номер пункта (2.2) — возвращает (id, text) или None."""
        m = CLAUSE_NUM_RE.search(query or "")
        if not m:
            return None
        num = m.group(1)
        with self._lock:
            text = self._by_num.get(num)
        if text is not None:
            return (num, text)
        return None

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Возвращает [{id, text, score}] — топ пунктов по BM25.

        Если в запросе есть номер пункта — он возвращается первым
        (точное совпадение), даже если словесного скоринга нет.
        """
        # Приоритет: точный номер.
        num_hit = self.lookup_number(query)
        if num_hit is not None:
            num_id, num_text = num_hit
            # Может быть нужно вернуть только этот пункт? Но чтобы поддержать
            # контекст, вернём его первым, а дальше — словесные совпадения.
            # Если у запроса нет внятных слов кроме номера — возвращаем только его.

        with self._lock:
            docs = list(self._docs)
            tokens = list(self._tokens)
            doc_len = list(self._doc_len)
            n = self._n
            avgdl = self._avgdl
            idf_cache = dict(self._idf_cache)
        q_tokens = _tokenize(query)
        # Если в запросе только номер и ничего более внятного (или номер найден
        # точно) — отдаём этот пункт сразу.
        if num_hit is not None and not q_tokens:
            return [{"id": num_hit[0], "text": num_hit[1], "score": 1e9}]

        base: list[dict] = []
        if num_hit is not None:
            base = [{"id": num_hit[0], "text": num_hit[1], "score": 1e9}]

        if not q_tokens or not docs:
            return base

        # Используем предвычисленные idf и среднюю длину — без пересчёта.
        scored = []
        for idx, doc in enumerate(docs):
            counter = tokens[idx]
            dl = doc_len[idx]
            score = 0.0
            for t in set(q_tokens):
                tf = counter.get(t, 0)
                if tf == 0:
                    continue
                denom = tf + _K1 * (1 - _B + _B * (dl / avgdl if avgdl else 1.0))
                score += idf_cache.get(t, 0.0) * (_K1 + 1) * tf / denom
            if score > 0:
                scored.append({"id": doc["id"], "text": doc["text"], "score": score})

        scored.sort(key=lambda x: x["score"], reverse=True)

        # Не дублируем номер, если он уже есть в base.
        existing_ids = {d["id"] for d in base}
        for item in scored:
            if item["id"] not in existing_ids:
                base.append(item)
            if len(base) >= top_k:
                break
        return base[:top_k]


def search_rules(query: str, top_k: int = 5, index: RulesIndex | None = None) -> list[dict]:
    """Удобная функция: создаёт/использует индекс и ищет по правилам."""
    if index is None:
        index = RulesIndex()
        index.load()
    if not index.ready:
        index.load()
    return index.search(query, top_k=top_k)