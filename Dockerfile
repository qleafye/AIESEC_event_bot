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

# HEALTHCHECK намеренно отсутствует: PID 1 контейнера — сам бот (long polling), поэтому
# «процесс жив» == «контейнер запущен», а проверять это healthcheck'ом бессмысленно.
# Осмысленная проверка требует heartbeat-файла из цикла поллинга — отдельная задача.

CMD ["python", "main.py"]
