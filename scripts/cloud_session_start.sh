#!/usr/bin/env bash
# Хук SessionStart (.claude/settings.json): готовит облачную сессию Claude Code (claude.ai/code).
# Локально ничего не делает — там CLAUDE_CODE_REMOTE не задан.
set -euo pipefail
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}"

# Локальный commit-msg вырезает строку Claude-Session (репозиторий публичный).
git config core.hooksPath .githooks

# Зависимости — в .venv проекта: системный Python Ubuntu 24.04 не даёт pip ставить пакеты глобально.
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt -r requirements-dev.txt

# PATH с .venv — для всех команд сессии.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$PWD/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
