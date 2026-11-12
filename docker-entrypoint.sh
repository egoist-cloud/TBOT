#!/bin/sh
# Первый старт контейнера: перенос JSON-баз из /seed в /data.
#
# Каталог /seed монтируется read-only из ./seed (см. docker-compose.yml).
# Если он пуст или не смонтирован — шаг ничего не делает. Ошибка импорта
# останавливает контейнер, чтобы не работать на полупустой базе.
set -eu

if [ -d "${SEED_DIR:-/seed}" ]; then
    python -m quantum.import_seed
fi

# Дальше — обычная команда образа (python script.py).
exec "$@"
