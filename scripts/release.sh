#!/bin/sh
# Безопасный релиз Quantum/TBOT на Docker Compose-хосте.
#
# Скрипт не читает .env и не печатает секреты. Он обновляет git-дерево до
# конкретного SHA, собирает новый образ quantum-bot:latest, поднимает
# контейнер и проверяет /health. При ошибке возвращает quantum-bot:previous.
#
# Обязательные переменные:
#   DEPLOY_DIR       каталог с docker-compose.yml и .env
#   TARGET_SHA       коммит, который должен быть задеплоен
#   HEALTH_URL       например http://127.0.0.1:8080/health
# Необязательные:
#   ALLOW_FIRST_DEPLOY=1  разрешить первый релиз без previous-образа
#   HEALTH_RETRIES=30     число попыток /health
#   HEALTH_DELAY=5        пауза между попытками, секунд
#   HEALTH_TIMEOUT=5      таймаут одного curl, секунд

set -eu

: "${DEPLOY_DIR:?DEPLOY_DIR is required}"
: "${TARGET_SHA:?TARGET_SHA is required}"
: "${HEALTH_URL:?HEALTH_URL is required}"

log() { printf '%s\n' "$*"; }

for cmd in git docker curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        log "ERROR: не найдена команда $cmd"
        exit 2
    fi
done
if ! docker compose version >/dev/null 2>&1; then
    log "ERROR: не установлен Docker Compose plugin (docker compose)"
    exit 2
fi

if [ ! -d "$DEPLOY_DIR" ]; then
    log "ERROR: DEPLOY_DIR не существует: $DEPLOY_DIR"
    exit 2
fi
cd "$DEPLOY_DIR"
if [ ! -f docker-compose.yml ] || [ ! -f .env ]; then
    log "ERROR: в DEPLOY_DIR нужны docker-compose.yml и .env"
    exit 2
fi

# Не перезаписываем чужие локальные правки на production-хосте.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    log "ERROR: в $DEPLOY_DIR есть локальные изменения tracked-файлов"
    exit 2
fi

log "==> Получаю коммит $TARGET_SHA"
git fetch --prune origin
if ! git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null; then
    git fetch origin "$TARGET_SHA"
fi
git checkout --detach "$TARGET_SHA"

HAS_PREVIOUS=0
if docker image inspect quantum-bot:latest >/dev/null 2>&1; then
    docker tag quantum-bot:latest quantum-bot:previous
    HAS_PREVIOUS=1
    log "==> Предыдущий образ сохранён как quantum-bot:previous"
elif [ "${ALLOW_FIRST_DEPLOY:-0}" != "1" ]; then
    log "ERROR: нет quantum-bot:latest для отката. "
    log "Для первого релиза запустите с ALLOW_FIRST_DEPLOY=1."
    exit 2
fi

log "==> Собираю образ"
if ! docker compose build bot; then
    log "ERROR: сборка образа не удалась; работающий контейнер не тронут"
    exit 2
fi

check_health() {
    attempt=1
    while [ "$attempt" -le "${HEALTH_RETRIES:-30}" ]; do
        if curl -fsS --max-time "${HEALTH_TIMEOUT:-5}" "$HEALTH_URL" \
                >/dev/null 2>&1; then
            log "==> /health отвечает 200 (попытка $attempt)"
            return 0
        fi
        if [ "$attempt" -lt "${HEALTH_RETRIES:-30}" ]; then
            sleep "${HEALTH_DELAY:-5}"
        fi
        attempt=$((attempt + 1))
    done
    return 1
}

rollback() {
    log "ERROR: новый релиз не прошёл /health"
    if [ "$HAS_PREVIOUS" = "1" ]; then
        log "==> Откат на quantum-bot:previous"
        docker tag quantum-bot:previous quantum-bot:latest
        if docker compose up -d --no-build bot && check_health; then
            log "==> Откат выполнен, предыдущая версия здорова"
        else
            log "ERROR: откат поднялся, но /health не восстановился"
        fi
    else
        log "ERROR: previous-образа нет; останавливаю новый контейнер"
        docker compose stop bot || true
    fi
    exit 1
}

log "==> Запускаю контейнер"
if ! docker compose up -d --no-build bot; then
    rollback
fi

if ! check_health; then
    rollback
fi

log "==> Релиз $TARGET_SHA успешно развёрнут"