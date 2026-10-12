#!/usr/bin/env bash
# Cross-platform interactive installer for Ubuntu VPS, Railway, Docker and similar hosts.
set -Eeuo pipefail
IFS=$'\n\t'

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="csvsearch"
SERVICE_NAME="telegram-csv-search"
MODE="auto"
SKIP_IMPORT=0
NO_START=0

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log() { printf '\n%b[%s]%b %s\n' "$BLUE" "$(date '+%F %T')" "$NC" "$*"; }
warn() { printf '%b%s%b\n' "$YELLOW" "$*" "$NC"; }
die() { printf '\n%bERROR:%b %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Cross-platform Telegram CSV Search installer

Usage: sudo ./install_vps.sh [options]

Options:
  --project-dir PATH    Project directory (default: script directory)
  --service-user NAME   Linux service user for systemd (default: csvsearch)
  --mode MODE           auto, systemd, or foreground (default: auto)
  --skip-import         Do not run the first CSV import
  --no-start            Configure but do not start the bot
  -h, --help            Show this help

In auto mode, systemd is used only when PID 1 is systemd. Railway/Docker-like
hosts use foreground mode and receive ./railway_start.sh as their Start Command.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir) [[ $# -ge 2 ]] || die "--project-dir needs a value"; PROJECT_DIR="$(realpath -m "$2")"; shift 2 ;;
    --service-user) [[ $# -ge 2 ]] || die "--service-user needs a value"; SERVICE_USER="$2"; shift 2 ;;
    --mode) [[ $# -ge 2 ]] || die "--mode needs a value"; MODE="$2"; shift 2 ;;
    --skip-import) SKIP_IMPORT=1; shift ;;
    --no-start) NO_START=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done
case "$MODE" in auto|systemd|foreground) ;; *) die "--mode must be auto, systemd, or foreground." ;; esac

[[ -f "$PROJECT_DIR/requirements.txt" ]] || die "requirements.txt was not found in $PROJECT_DIR."
[[ -f "$PROJECT_DIR/bot.py" ]] || die "bot.py was not found in $PROJECT_DIR."
[[ -f "$PROJECT_DIR/import_csv.py" ]] || die "import_csv.py was not found in $PROJECT_DIR."
[[ -t 0 ]] || die "This wizard needs an interactive terminal. Configure variables and use the platform Start Command instead."

SYSTEMD_AVAILABLE=0
if command -v systemctl >/dev/null 2>&1 && [[ "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" == "systemd" ]]; then SYSTEMD_AVAILABLE=1; fi
if [[ "$MODE" == "auto" ]]; then
  if [[ "$SYSTEMD_AVAILABLE" -eq 1 ]]; then MODE="systemd"; else MODE="foreground"; fi
fi
if [[ "$MODE" == "systemd" && "$SYSTEMD_AVAILABLE" -ne 1 ]]; then
  die "systemd mode was requested, but systemd is not PID 1. Use --mode foreground on Railway/Docker."
fi

ask_value() {
  local prompt="$1" default="${2-}" value
  if [[ -n "$default" ]]; then read -r -p "$prompt [$default]: " value; value="${value:-$default}"; else read -r -p "$prompt: " value; fi
  printf '%s' "$value"
}
ask_secret() { local value; read -r -s -p "$1: " value; printf '\n' >&2; printf '%s' "$value"; }
ask_yes_no() {
  local prompt="$1" default="$2" value
  while true; do
    read -r -p "$prompt [$default]: " value; value="${value:-$default}"
    case "${value,,}" in y|yes|بله) printf 'true'; return ;; n|no|خیر) printf 'false'; return ;; *) warn "Please answer yes or no." ;; esac
  done
}
valid_token() { [[ "$1" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; }
valid_ids() { [[ "$1" =~ ^[0-9]+([[:space:]]*,[[:space:]]*[0-9]+)*$ ]]; }

cat <<EOF

Telegram CSV Search - cross-platform guided installer
------------------------------------------------------
Detected mode: $MODE
Your token is read silently and is never printed after entry.
EOF
if [[ "$MODE" == "foreground" ]]; then
  cat <<'EOF'
This looks like Railway/Docker/container mode. No systemctl or systemd will be used.
The bot will run in the foreground, which is the correct behavior for these hosts.
EOF
fi

if [[ "$MODE" == "systemd" && "$EUID" -ne 0 ]]; then die "Systemd mode must be run with sudo or as root."; fi

TOKEN=""
while [[ -z "$TOKEN" ]] || ! valid_token "$TOKEN"; do TOKEN="$(ask_secret "Paste your BotFather token")"; valid_token "$TOKEN" || warn "Invalid token format. Example: 123456789:AA..."; done
USER_IDS=""
while [[ -z "$USER_IDS" ]] || ! valid_ids "$USER_IDS"; do USER_IDS="$(ask_value "Enter numeric Telegram user ID(s), comma-separated")"; valid_ids "$USER_IDS" || warn "Use numeric IDs only, e.g. 123456789"; done
CSV_HEADER="$(ask_yes_no "Do your CSV files normally contain a header row?" "y")"
CSV_DELIMITER="$(ask_value "CSV delimiter (one character)" ",")"
[[ ${#CSV_DELIMITER} -eq 1 ]] || die "CSV delimiter must be exactly one character."
CSV_ENCODING="$(ask_value "CSV encoding" "utf-8-sig")"
UPLOAD_ENABLED="$(ask_yes_no "Allow authorized private-chat CSV uploads?" "y")"
LOCAL_API_ENABLED="$(ask_yes_no "Use a Local Bot API Server for files larger than 20 MB?" "n")"
TELEGRAM_BASE_URL="https://api.telegram.org/bot"
TELEGRAM_FILE_BASE_URL="https://api.telegram.org/file/bot"
if [[ "$LOCAL_API_ENABLED" == "true" ]]; then
  TELEGRAM_BASE_URL="$(ask_value "Local Bot API base URL" "http://127.0.0.1:8081/bot")"
  TELEGRAM_FILE_BASE_URL="$(ask_value "Local Bot API file URL" "http://127.0.0.1:8081/file/bot")"
fi

mkdir -p "$PROJECT_DIR"/{data,database,logs}
if [[ "$MODE" == "systemd" ]]; then
  log "Installing operating-system packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates
  if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"; fi
else
  log "Using host-provided Python runtime"
fi

log "Preparing Python environment"
if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then python3 -m venv "$PROJECT_DIR/venv"; fi
"$PROJECT_DIR/venv/bin/python" -m pip install --upgrade pip
"$PROJECT_DIR/venv/bin/pip" install --requirement "$PROJECT_DIR/requirements.txt"

log "Writing secure configuration"
cat > "$PROJECT_DIR/.env" <<EOF
TELEGRAM_BOT_TOKEN=$TOKEN
ALLOWED_USER_IDS=$USER_IDS
DATA_DIRECTORY=./data
DATABASE_PATH=./database/search.db
LOG_DIRECTORY=./logs
CSV_DELIMITER=$CSV_DELIMITER
CSV_ENCODING=$CSV_ENCODING
CSV_HAS_HEADER=$CSV_HEADER
CSV_COLUMN_NAMES=column_1,column_2,name,birth_date,phone
DUPLICATE_MODE=keep
IMPORT_BATCH_SIZE=5000
IMPORT_PROGRESS_EVERY_ROWS=100000
MAX_RESULTS=100
RESULTS_PER_PAGE=10
MAX_QUERY_LENGTH=160
MAX_FIELD_DISPLAY_CHARS=280
PREFIX_SEARCH_MIN_LENGTH=3
PHONE_NORMALIZATION=true
LOG_LEVEL=INFO
UPLOAD_ENABLED=$UPLOAD_ENABLED
UPLOAD_MAX_BYTES=2000000000
UPLOAD_PROGRESS_INTERVAL_SECONDS=3
# CSV is the authoritative data source and must remain available for search hydration.
DELETE_CSV_AFTER_IMPORT=false
TELEGRAM_BASE_URL=$TELEGRAM_BASE_URL
TELEGRAM_FILE_BASE_URL=$TELEGRAM_FILE_BASE_URL
EOF
chmod 600 "$PROJECT_DIR/.env"

if [[ "$MODE" == "systemd" ]]; then
  cat > "$PROJECT_DIR/telegram-csv-search.service" <<EOF
[Unit]
Description=Authorized Telegram CSV Search Bot
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PROJECT_DIR/venv/bin/python $PROJECT_DIR/bot.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=$PROJECT_DIR/database $PROJECT_DIR/logs $PROJECT_DIR/data
[Install]
WantedBy=multi-user.target
EOF
  chown -R "$SERVICE_USER:$SERVICE_USER" "$PROJECT_DIR"
  chmod 700 "$PROJECT_DIR"; chmod 600 "$PROJECT_DIR/.env"
else
  chmod +x "$PROJECT_DIR/railway_start.sh"
fi

if [[ "$SKIP_IMPORT" -eq 0 ]]; then
  shopt -s nullglob; csv_files=("$PROJECT_DIR/data"/*.csv "$PROJECT_DIR/data"/*.txt); shopt -u nullglob
  if [[ ${#csv_files[@]} -eq 0 ]]; then warn "No CSV/TXT files found in $PROJECT_DIR/data; import skipped."; else
    log "Importing CSV files"
    if [[ "$MODE" == "systemd" ]]; then sudo -u "$SERVICE_USER" -- "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/import_csv.py"; else "$PROJECT_DIR/venv/bin/python" "$PROJECT_DIR/import_csv.py"; fi
  fi
fi

# Always create the schema, even on a brand-new deployment with no CSV yet.
# This lets the bot start and receive the first file through Telegram upload.
if [[ "$MODE" == "systemd" ]]; then
  sudo -u "$SERVICE_USER" -- "$PROJECT_DIR/venv/bin/python" -c \
    "from config import load_settings; from database import connect, initialize_database; s=load_settings(); c=connect(s.database_path); initialize_database(c); c.close()"
else
  "$PROJECT_DIR/venv/bin/python" -c \
    "from config import load_settings; from database import connect, initialize_database; s=load_settings(); c=connect(s.database_path); initialize_database(c); c.close()"
fi

if [[ "$MODE" == "systemd" ]]; then
  log "Configuring systemd"
  install -m 644 "$PROJECT_DIR/telegram-csv-search.service" "/etc/systemd/system/$SERVICE_NAME.service"
  systemctl daemon-reload; systemctl enable "$SERVICE_NAME"
  if [[ "$NO_START" -eq 0 ]]; then systemctl restart "$SERVICE_NAME"; else warn "Installed but not started (--no-start)."; fi
  cat <<EOF

Installation complete in systemd mode.
Check: systemctl status $SERVICE_NAME
Logs:  journalctl -u $SERVICE_NAME -f
EOF
else
  cat <<EOF

Configuration complete in foreground/container mode.
Platform Start Command:
  ./railway_start.sh

For Railway/Docker, set your variables in the platform dashboard and use the
Start Command above. The script imports changed CSVs, then execs the bot.
EOF
  if [[ "$NO_START" -eq 0 ]]; then log "Starting bot in foreground"; exec "$PROJECT_DIR/railway_start.sh"; fi
fi
