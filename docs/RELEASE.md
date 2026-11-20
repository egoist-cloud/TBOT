# Release-процесс

Релиз запускается только после успешного CI. Автоматический сценарий:

1. `CI` на `main` проходит `ruff`, `pytest` и Docker-smoke.
2. GitHub Actions запускает workflow `Release` (`workflow_run`).
3. Job `verify` ещё раз проверяет release-кандидат.
4. Job `Release → Deploy production` ждёт approval environment `production`,
   если в настройках репозитория включены required reviewers.
5. На хосте `scripts/release.sh` сохраняет текущий образ как
   `quantum-bot:previous`, собирает новый, поднимает compose-сервис и
   опрашивает `/health`.
6. Если `/health` не отвечает 200, скрипт возвращает
   `quantum-bot:previous` и поднимает предыдущую версию.

## 1. Настроить GitHub Secrets

В `Settings → Secrets and variables → Actions` добавить:

| Секрет | Что это |
|---|---|
| `DEPLOY_HOST` | хост production-сервера |
| `DEPLOY_USER` | SSH-пользователь с доступом к Docker (не root) |
| `DEPLOY_PATH` | каталог клона проекта с `docker-compose.yml` и `.env` |
| `DEPLOY_SSH_KEY` | приватный SSH-ключ для деплоя |
| `DEPLOY_KNOWN_HOSTS` | known_hosts production-хоста, лучше проверенный по fingerprint |
| `HEALTH_URL` | полный URL `/health`, например `https://bot.example.com/health` |

Значения секретов не коммитятся и не выводятся workflow'ом. `.env`
production-хоста уже должен существовать и содержать рабочие токены;
release-скрипт его не читает и не перезаписывает.

`DEPLOY_KNOWN_HOSTS` можно получить на доверенной машине:

```bash
ssh-keyscan -H bot.example.com
```

Сверьте fingerprint с сервером перед сохранением: workflow использует
`StrictHostKeyChecking=yes`.

## 2. Настроить environment protection

В `Settings → Environments` создайте `production` и добавьте required
reviewers. Тогда даже автоматический запуск после CI не развернётся до
подтверждения человека. Для production также полезно ограничить
deployment branches веткой `main`.

## 3. Требования к production-хосту

- установлены `git`, Docker Engine, Docker Compose plugin и `curl`;
- пользователь из `DEPLOY_USER` может запускать `docker compose` без sudo;
- клон в `DEPLOY_PATH` имеет remote `origin` и доступ на fetch;
- `DATA_DIR`/volume `quantum-data` не удаляются релизом;
- `.env` лежит в `DEPLOY_PATH` и недоступен другим пользователям.

## 4. Первый релиз

Для первого релиза previous-образа ещё нет. Запустите `Release` вручную
(`Actions → Release → Run workflow`), введите `confirm=DEPLOY` и включите
`allow_first_deploy`. Без этого флага скрипт откажется деплоить, чтобы не
оставить production без возможности отката.

## 5. Ручной релиз

Ручной запуск нужен для срочного исправления или первого деплоя:

1. Выберите `Release` → `Run workflow`.
2. Введите `confirm=DEPLOY`.
3. При необходимости включите `allow_first_deploy` только для первого релиза.
4. Подтвердите deployment в environment `production`, если approval включён.

## 6. Откат

Откат автоматический: при ошибке `/health` скрипт возвращает тег
`quantum-bot:previous` на `quantum-bot:latest`, снова поднимает compose и
проверяет здоровье. Workflow завершается с ошибкой, поэтому проблема видна
в Actions.

Ручной откат на хосте:

```bash
cd "$DEPLOY_PATH"
docker tag quantum-bot:previous quantum-bot:latest
docker compose up -d --no-build bot
curl -fsS "$HEALTH_URL"
```

Не используйте `docker compose down -v`: это удалит volume с базой.

## 7. Что проверить после релиза

- `/health` отвечает `200` и `ok: true`;
- в логах нет `Startup preflight` с ошибками;
- бот снова видит guild и роли;
- фоновые задачи запущены (лидер lease);
- при проблеме смотрите `docker compose logs --tail=200 bot`, не выводя `.env`.