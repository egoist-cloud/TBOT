# Quantum (TBOT)

Discord-бот клана: привязка Discord ↔ Steam, заявки с голосованием,
синхронизация ролей, RAG-ответы по правилам клана.

## Возможности

- **Привязка аккаунтов** Discord ↔ Steam через панель `cs2red`, проверка
  ролей клана, автосинхронизация по расписанию.
- **Заявки**: анкеты через модальное окно, публикация в канале заявок и в
  отдельной ветке, голосование («Принять» / «Отклонить»), выдача роли при
  достижении `VOTES_REQUIRED` голосов.
- **Статистика модераторов**: `/status` показывает личные цифры по заявкам
  (принято / отклонено / в работе). Ответ всегда ephemeral — свои цифры
  видит только вызвавший, чужую статистику может посмотреть модератор.
- **ИИ**: ответы по правилам клана (`rules.txt`) с RAG-поиском по BM25,
  кэшем ответов и лимитом запросов на пользователя. Провайдеры:
  Gemini → OpenRouter → LLM7.
- **GitHub-синк**: данные хранятся в отдельном репозитории, поэтому
  привязки и базы не теряются при рестарте контейнера на Render.
- **Health-check** для Render по HTTP (`GET /` и `GET /health`).

## Запуск

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # Windows: copy .env.example .env
# заполни .env
python script.py
```

## Переменные окружения

Полный список — в [`.env.example`](.env.example). Важное:

| Переменная | Назначение |
|---|---|
| `DISCORD_TOKEN` | токен бота |
| `GUILD_ID` | ID сервера (без него команды не синхронизируются по guild) |
| `AUTO_ROLE_ID` | роль, выдаваемая при принятии заявки |
| `ALLOWED_ROLE_ID`, `REVIEWER_ROLE_ID_2`, `EXTRA_REVIEWER_ROLE_1..3` | роли ревьюеров |
| `SPAM_VOTE_ROLE_ID` | роль, которой можно голосовать многократно |
| `VOTES_REQUIRED` | сколько голосов нужно для решения (по умолчанию 2) |
| `CS2RED_COOKIE_STRING` | админские cookie cs2red (или отдельные `CS2RED_*`) |
| `STEAM_API_KEY` | ключ Steam Web API |
| `GEMINI_API_KEY` / `OPENROUTER_API_KEY` / `LLM7_API_KEY` | ключи ИИ |
| `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_BRANCH` | синк данных с GitHub |
| `DATA_DIR` | папка с `quantum.db`, `rules.txt`, JSON-базами |

Секреты (`CS2RED_*`, `STEAM_API_KEY`, `*_API_KEY`, `GITHUB_TOKEN`,
`DISCORD_TOKEN`) маскируются в логах и в `error_log.txt`.

## Безопасность

- HTTP-сессии разделены: админские cookie и заголовки `cs2red` уходят
  **только** на `cs2red.ru`. Steam, GitHub и LLM-провайдеры получают
  публичную сессию без cookie (`API.session` / `API.cs2red`).
- Хост определяется по hostname, а не по подстроке в URL.
- Панель управления (`/panel`) показывается только админу и только
  ephemeral-сообщением; каждая кнопка панели сама проверяет права.
- Голоса по заявке атомарны и привязаны к `app_id` заявки, а не к id
  сообщения: сообщение в канале и в ветке — одна запись голосов.

## Разработка и CI

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
pytest -v
```

CI (`.github/workflows/ci.yml`) запускает `ruff` и `pytest` на Python 3.12
для `main`, `fix/**` и `feat/**`.

## Данные

Основное хранилище — SQLite (`quantum.db`, таблица `votes`, `applications`,
`links`, `players`, `users`, `moderators`, `seen`, `ai_cache`).
При первом запуске JSON-файлы (`links.json`, `players_db.json`,
`users_db.json`, `moderators.json`, `votes.json`, `seen.json`)
переносятся в SQLite и переименовываются в `*.bak`.
