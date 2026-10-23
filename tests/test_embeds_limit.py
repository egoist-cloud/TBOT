# -*- coding: utf-8 -*-
"""Тесты разбиения embed'ов под лимит Discord (6000 символов на сообщение)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

import script as botmod  # noqa: E402


def _make(desc_len: int, title: str = "t") -> discord.Embed:
    return discord.Embed(title=title, description="x" * desc_len)


def test_embed_chars_sums_title_description_fields_footer():
    """Размер считается так же, как его считает Discord."""
    e = discord.Embed(title="abc", description="de")
    e.add_field(name="n", value="vv")
    e.set_footer(text="f")
    assert botmod._embed_chars(e) == 3 + 2 + 1 + 2 + 1


def test_two_large_embeds_split_into_messages():
    """2 embed'а по 3200 символов не влезают в 6000 — должны разъехаться."""
    embeds = [_make(3200, "a"), _make(3200, "b")]
    batches = botmod._batch_embeds(embeds)
    assert len(batches) == 2
    for batch in batches:
        assert sum(botmod._embed_chars(e) for e in batch) <= botmod.EMBED_MSG_LIMIT


def test_small_embeds_stay_in_one_batch():
    """Если суммарно влезает — одно сообщение."""
    embeds = [_make(1900, "a"), _make(1900, "b"), _make(1900, "c")]
    batches = botmod._batch_embeds(embeds)
    assert len(batches) == 1
    assert len(batches[0]) == 3


def test_more_than_ten_embeds_split():
    """Больше 10 embed'ов — несколько сообщений (лимит Discord)."""
    embeds = [_make(10, str(i)) for i in range(12)]
    batches = botmod._batch_embeds(embeds)
    assert len(batches) == 2
    assert [len(b) for b in batches] == [10, 2]


def test_every_batch_fits_limits():
    """Инвариант: каждая пачка укладывается в оба лимита."""
    embeds = [_make(1500, f"t{i}") for i in range(9)]
    batches = botmod._batch_embeds(embeds)
    for batch in batches:
        assert len(batch) <= botmod.EMBED_MSG_MAX_COUNT
        assert sum(botmod._embed_chars(e) for e in batch) <= botmod.EMBED_MSG_LIMIT


def test_single_too_large_embed_is_trimmed():
    """Одиночный слишком большой embed обрезается, а не роняет отправку."""
    e = _make(7000)
    botmod._trim_embed(e)
    assert botmod._embed_chars(e) <= botmod.EMBED_MSG_LIMIT
    assert e.description.endswith("…")


def test_links_table_embeds_fit_limits():
    """Таблица логов: пачки embed'ов не превышают лимит Discord."""
    embeds = [_make(3200, f"стр. {i}") for i in range(3)]
    embeds.append(_make(4000, "🚫 Не авторизованы"))
    batches = botmod._batch_embeds(embeds)
    assert batches
    for batch in batches:
        assert sum(botmod._embed_chars(e) for e in batch) <= 6000


def test_limit_constants_are_safe():
    """Запас до жёсткого лимита Discord 6000 и 10 embed'ов на сообщение."""
    assert botmod.EMBED_MSG_LIMIT <= 6000
    assert botmod.EMBED_MSG_MAX_COUNT <= 10
