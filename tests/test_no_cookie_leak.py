# -*- coding: utf-8 -*-
"""
Проверка, что админские cookie cs2red не уходят на сторонние хосты.

Поднимаем локальный HTTP-сервер и делаем запросы к нему через
api.session (публичная сессия) и api.cs2red (приватная). На чужой
хост cookie приходить не должны.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Фейковые куки cs2red, чтобы тест не зависел от окружения.
os.environ.setdefault("CS2RED_TOKEN", "CS2RED_ADMIN_SECRET")
os.environ.setdefault("CS2RED_BH", "BHSECRET")
os.environ.setdefault("CS2RED_I", "ISECRET")

from aiohttp import web  # noqa: E402

import ai_rules  # noqa: E402
import script as botmod  # noqa: E402


RECEIVED: list = []


async def handler(request: web.Request) -> web.Response:
    RECEIVED.append({
        "path": request.path,
        "cookie": request.headers.get("Cookie", ""),
        "referer": request.headers.get("Referer", ""),
        "xrw": request.headers.get("X-Requested-With", ""),
    })
    return web.json_response({"ok": True})


async def main() -> int:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    # Порт 0 -> случайный свободный порт (не конфликтует в CI).
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    api = botmod.API()
    await api.start()

    base = f"http://127.0.0.1:{port}"

    # 1) Публичная сессия (Steam / GitHub / LLM) -> чужой хост.
    async with api.session.get(f"{base}/steam") as r:
        await r.text()
    # 2) Приватная сессия -> имитация cs2red.
    async with api.cs2red.get(f"{base}/cs2red") as r:
        await r.text()
    # 3) _get_json сам выбирает сессию по хосту.
    await api._get_json(f"{base}/auto")
    # 4) ai_rules._fetch получает api и сам выбирает сессию.
    await ai_rules._fetch(api, f"{base}/rules-fetch", {"token": "X"},
                          {"Referer": "https://cs2red.ru/admin"})

    await api.close()
    await runner.cleanup()

    failures = []
    for rec in RECEIVED:
        cookie = rec["cookie"]
        referer = rec["referer"]
        if rec["path"] in ("/cs2red", "/rules-fetch"):
            # Эти запросы намеренно идут с куками (эмуляция cs2red).
            continue
        # Публичные хосты не должны получать cs2red-куки.
        if "CS2RED_ADMIN_SECRET" in cookie or "BHSECRET" in cookie or "ISECRET" in cookie:
            failures.append(f"{rec['path']}: cookie утек -> {cookie}")
        if "cs2red.ru" in referer:
            failures.append(f"{rec['path']}: Referer утек -> {referer}")
        if rec["xrw"]:
            failures.append(f"{rec['path']}: X-Requested-With утек -> {rec['xrw']}")

    print("Запросы:")
    for rec in RECEIVED:
        print(f"  {rec['path']:14} cookie={rec['cookie']!r} referer={rec['referer']!r}")

    if failures:
        print("\n❌ УТЕЧКА:")
        for f in failures:
            print("  -", f)
        return 1
    print("\n✅ Утечки нет: публичные хосты не получили cookie/заголовки cs2red")
    return 0


def test_no_cookie_leak():
    """Cookie и заголовки cs2red не должны уходить на сторонние хосты."""
    rc = asyncio.run(main())
    assert rc == 0, "cookie/заголовки cs2red утекли на сторонний хост"


def test_is_cs2red_host_checks_hostname():
    """Хост определяется по hostname, а не по подстроке в URL."""
    assert ai_rules.is_cs2red_host("https://cs2red.ru/rules") is True
    assert ai_rules.is_cs2red_host("https://www.cs2red.ru/x") is True
    assert ai_rules.is_cs2red_host("https://api.steampowered.com/x") is False
    assert ai_rules.is_cs2red_host("https://evil.example/cs2red.ru") is False
    assert ai_rules.is_cs2red_host("") is False
    print("  ok: хост cs2red определяется по hostname")


def test_session_for_routes_by_host():
    """session_for отдаёт приватную сессию только для cs2red.ru."""
    api = botmod.API()
    api.session = object()
    api.cs2red = object()
    try:
        assert api.session_for("https://cs2red.ru/api") is api.cs2red
        assert api.session_for("https://api.steampowered.com/x") is api.session
        assert api.session_for("https://api.github.com/x") is api.session
        assert api.session_for("https://evil.example/cs2red.ru") is api.session
        assert api.session_for("") is api.session
    finally:
        api.session = None
        api.cs2red = None
    print("  ok: сессия выбирается по хосту")


def test_ai_rules_fetch_strips_cookies_for_foreign_hosts():
    """ai_rules._fetch не отправляет куки cs2red на чужой хост."""
    captured = {}

    class _Resp:
        status = 200
        charset = "utf-8"

        class _Content:
            @staticmethod
            async def read(limit):
                return b"ok"

        content = _Content()

    class _Ctx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Sess:
        def get(self, url, cookies=None, headers=None):
            captured["url"] = url
            captured["cookies"] = cookies
            captured["headers"] = headers
            return _Ctx()

    async def _run():
        await ai_rules._fetch(
            _Sess(), "https://api.steampowered.com/x",
            {"token": "SECRET"}, {"Referer": "https://cs2red.ru/admin"})

    asyncio.run(_run())
    assert captured["cookies"] is None, "куки отправлены на чужой хост"
    assert captured["headers"] is None, "заголовки cs2red отправлены на чужой хост"
    print("  ok: _fetch чистит куки и заголовки для чужих хостов")
