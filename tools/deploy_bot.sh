#!/usr/bin/env bash
# Рестарт бота с «тихой минутой»: ждём, пока никто не заполняет анкету, затем пересобираем.
# Использование (на сервере, из каталога стека):
#   tools/deploy_bot.sh                 # тихая минута (120 с) → docker compose up -d --build bot
#   QUIET=60 MAX_WAIT=600 tools/deploy_bot.sh
#   FORCE=1 tools/deploy_bot.sh         # без ожидания (осознанно)
set -euo pipefail
cd "$(dirname "$0")/.."

QUIET="${QUIET:-120}"
MAX_WAIT="${MAX_WAIT:-900}"
DB="${DB:-data/forum.db}"

if [ "${FORCE:-0}" != "1" ]; then
  if ! python3 tools/quiet_minute.py --db "$DB" --quiet "$QUIET" --max-wait "$MAX_WAIT"; then
    echo "Тишины не дождались. Повтори позже или FORCE=1 (делегатов в анкете сбросит на /start → «Продолжить»)." >&2
    exit 2
  fi
fi

docker compose up -d --build bot
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
