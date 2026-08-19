FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Процесс бота не должен быть root внутри контейнера. UID/GID зафиксированы = 1000, чтобы
# совпадать с обычным первым пользователем хоста: ./data и ./logs монтируются volume'ами,
# и файлы там должны быть доступны на запись этому UID (на сервере: chown -R 1000:1000 data logs;
# google_credentials.json должен читаться UID 1000). Подробности — README, раздел «Docker».
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser . .
RUN mkdir -p /app/data /app/logs && chown -R appuser:appuser /app/data /app/logs

USER appuser

# HEALTHCHECK по heartbeat-файлу (services/heartbeat.py): PID 1 — сам бот, так что «процесс
# жив» Docker видит и без проверки; проверяем живость long polling'а. Бот пишет файл в /tmp
# раз в 30 с, пока getUpdates отвечает; --check даёт exit 1, если файлу > 120 с или его нет.
# Docker unhealthy-контейнер сам не перезапускает (restart: always — про выход процесса),
# статус — диагностика: docker inspect --format '{{.State.Health.Status}}' <container>.
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 CMD python -m services.heartbeat --check

CMD ["python", "main.py"]
