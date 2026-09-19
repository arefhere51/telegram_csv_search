#!/usr/bin/env bash
# Foreground entrypoint for Railway, Docker, Render, Fly.io, and similar platforms.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ -x "./venv/bin/python" ]]; then
  PYTHON_BIN="./venv/bin/python"
else
  PYTHON_BIN="python3"
fi

mkdir -p "${DATA_DIRECTORY:-./data}" "$(dirname "${DATABASE_PATH:-./database/search.db}")" "${LOG_DIRECTORY:-./logs}"

# An empty database is created by bot.py, so the first CSV may arrive from Telegram.
# Import only when source files exist; an empty data directory is a valid first-run state.
if find "${DATA_DIRECTORY:-./data}" -maxdepth 1 -type f \( -name '*.csv' -o -name '*.txt' \) -print -quit | grep -q .; then
  "$PYTHON_BIN" import_csv.py
else
  echo "No CSV/TXT source files found; starting with an empty database."
fi

exec "$PYTHON_BIN" bot.py
