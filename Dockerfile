# syntax=docker/dockerfile:1
# Quantum / TBOT — образ Discord-бота.
#
# Все рабочие данные (quantum.db, quantum.db-wal, rules.txt,
# ai_training.json, error_log.txt) бот хранит в DATA_DIR. Здесь это
# /data, а в docker-compose.yml на него смонтирован именованный volume,
# поэтому база переживает пересоздание контейнера и деплой.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8080

# Боту не нужен root. Пользователь создаётся заранее, чтобы /data
# принадлежал ему: иначе SQLite не сможет создать quantum.db и -wal.
RUN groupadd --system --gid 1001 bot \
 && useradd --system --uid 1001 --gid bot --create-home bot

WORKDIR /app

# Зависимости отдельным слоем: правка кода не переустанавливает pip-пакеты.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Entrypoint переносит JSON-базы из /seed в /data на первом старте.
# chmod обязателен: Git на Windows не сохраняет бит исполнения.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# /data создаётся в образе вместе со статическими файлами ИИ. Когда пустой
# именованный volume подключается впервые, Docker копирует туда содержимое
# каталога вместе с владельцем — бот сразу получает права на запись и
# готовые правила, без ручного chown на хосте.
RUN mkdir -p /data \
 && cp -f rules.txt ai_training.json /data/ \
 && chmod +x /usr/local/bin/docker-entrypoint.sh \
 && chown -R bot:bot /app /data

USER bot

# HTTP-health (Flask запускается в script.py на PORT).
EXPOSE 8080

# Том объявлен, чтобы `docker run -v` и compose не теряли данные.
VOLUME ["/data"]

# /health отдаёт 503, пока бот не залогинился (is_ready() == False)
# или уже закрывается (is_closed() == True). Поэтому healthcheck ловит
# и случай «Flask жив, а Discord отвалился». start-period даёт время
# на логин и синхронизацию команд.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8080') + '/health', timeout=3)"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "script.py"]
