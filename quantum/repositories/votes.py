"""Репозиторий голосов: таблица votes.

Ключ строки — app_id заявки (а не id сообщения). Заявка публикуется
сразу в двух местах: в канале заявок и в отдельной ветке. Раньше
голоса считались по id сообщения, поэтому у одной заявки было ДВЕ
независимые записи: можно было проголосовать «за» в канале и
«против» в ветке. Теперь обе копии заявки делят один app_id.
"""
import asyncio
import json

from ..db import get_db

# Голоса пишутся в одну таблицу, поэтому нужен лок: без него два
# одновременных голоса читают одно и то же состояние и затирают
# друг друга — один из голосов терялся.
_vote_lock = asyncio.Lock()


def blank_state() -> dict:
    """Пустое состояние голосования."""
    return {"accept": 0, "deny": 0, "voters": [],
            "accept_voters": [], "deny_voters": []}


def _loads_list(raw) -> list:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


async def get_votes(app_id) -> dict:
    """Возвращает состояние голосования для заявки.

    Если записи нет — пустой шаблон, чтобы код бота работал без
    изменений логики.
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT accept, deny, voters, accept_voters, deny_voters "
        "FROM votes WHERE message_id = ?",
        (str(app_id),),
    )
    row = await cursor.fetchone()
    await cursor.close()
    st = blank_state()
    if row is None:
        return st
    st["accept"] = row["accept"]
    st["deny"] = row["deny"]
    st["voters"] = _loads_list(row["voters"])
    st["accept_voters"] = _loads_list(row["accept_voters"])
    st["deny_voters"] = _loads_list(row["deny_voters"])
    return st


async def save_votes(app_id, state: dict) -> None:
    """Сохраняет состояние голосования (upsert)."""
    db = await get_db()
    accept_voters = [str(v) for v in (state.get("accept_voters") or [])]
    deny_voters = [str(v) for v in (state.get("deny_voters") or [])]
    voters = [str(v) for v in (state.get("voters") or [])]
    if not voters:
        voters = accept_voters + deny_voters
    await db.execute(
        "INSERT INTO votes (message_id, accept, deny, voters, accept_voters, deny_voters) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(message_id) DO UPDATE SET "
        "accept = excluded.accept, deny = excluded.deny, voters = excluded.voters, "
        "accept_voters = excluded.accept_voters, deny_voters = excluded.deny_voters",
        (
            str(app_id),
            int(state.get("accept", 0) or 0),
            int(state.get("deny", 0) or 0),
            json.dumps(voters, ensure_ascii=False),
            json.dumps(accept_voters, ensure_ascii=False),
            json.dumps(deny_voters, ensure_ascii=False),
        ),
    )
    await db.commit()


async def apply_vote(app_id, kind: str, user_id, *,
                     allow_repeat: bool = False) -> tuple[dict, bool]:
    """Атомарно применяет голос. Возвращает (состояние, применён ли голос).

    kind — "accept" или "deny". Один пользователь не может оказаться
    одновременно «за» и «против»: если он уже голосовал с другой
    стороны, его старый голос снимается, а новый засчитывается.
    allow_repeat — для роли SPAM_VOTE_ROLE_ID: можно голосовать
    несколько раз, счётчик увеличится, но в списке избирателей
    пользователь останется один раз.
    """
    key = str(app_id)
    uid = str(user_id)
    kind = "accept" if kind == "accept" else "deny"
    other = "deny" if kind == "accept" else "accept"

    async with _vote_lock:
        st = await get_votes(key)

        # Убираем возможный прошлый голос с противоположной стороны.
        if uid in st[f"{other}_voters"]:
            st[f"{other}_voters"].remove(uid)
            st[other] = max(0, st[other] - 1)

        if uid in st[f"{kind}_voters"] and not allow_repeat:
            return st, False

        if uid not in st[f"{kind}_voters"]:
            st[f"{kind}_voters"].append(uid)
        st[kind] += 1
        st["voters"] = list(dict.fromkeys(st["accept_voters"] + st["deny_voters"]))

        await save_votes(key, st)
        return st, True


async def count_all() -> int:
    """Число строк в таблице votes (для /health)."""
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) AS c FROM votes")
    row = await cursor.fetchone()
    await cursor.close()
    return row["c"] if row is not None else 0
