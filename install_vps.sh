#!/usr/bin/env bash
# Idempotent installer for Ubuntu/Debian VPS and foreground/container deployments.
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SERVICE_USER="csvsearch"
SERVICE_NAME="telegram-csv-search"
MODE="auto"
SKIP_IMPORT=0
FORCE_IMPORT=0
NO_START=0
NON_INTERACTIVE=0
SYSTEMD_WAS_ACTIVE=0

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log() { printf '\n%b[%s]%b %s\n' "$BLUE" "$(date '+%F %T')" "$NC" "$*"; }
warn() { printf '%b%s%b\n' "$YELLOW" "$*" "$NC"; }
die() { printf '\n%bERROR:%b %s\n' "$RED" "$NC" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Telegram CSV Search installer

Usage: ./install_vps.sh [options]

Options:
  --project-dir PATH    Project directory (default: this script's directory)
  --service-user NAME   Linux service user for systemd (default: csvsearch)
  --mode MODE           auto, systemd, or foreground (default: auto)
  --skip-import         Do not import source CSV files during a first install
  --import-existing     Explicitly import/recheck CSV files on an existing install
  --no-start            Configure the service/command but leave the bot stopped
  --non-interactive     Use TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS from environment
  -h, --help            Show this help

Existing .env, CSV data and SQLite databases are preserved on upgrades. The
finderbot command is installed automatically. In auto mode, systemd is used
only when PID 1 is systemd; containers should run ./railway_start.sh.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir) [[ $# -ge 2 ]] || die "--project-dir needs a value"; PROJECT_DIR="$(realpath -m "$2")"; shift 2 ;;
    --service-user) [[ $# -ge 2 ]] || die "--service-user needs a value"; SERVICE_USER="$2"; shift 2 ;;
    --mode) [[ $# -ge 2 ]] || die "--mode needs a value"; MODE="$2"; shift 2 ;;
    --skip-import) SKIP_IMPORT=1; shift ;;
    --import-existing) FORCE_IMPORT=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done
case "$MODE" in auto|systemd|foreground) ;; *) die "--mode must be auto, systemd, or foreground." ;; esac

[[ -f "$PROJECT_DIR/requirements.txt" ]] || die "requirements.txt was not found in $PROJECT_DIR."
[[ -f "$PROJECT_DIR/bot.py" ]] || die "bot.py was not found in $PROJECT_DIR."
[[ -f "$PROJECT_DIR/import_csv.py" ]] || die "import_csv.py was not found in $PROJECT_DIR."

SYSTEMD_AVAILABLE=0
if command -v systemctl >/dev/null 2>&1 && [[ "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" == "systemd" ]]; then SYSTEMD_AVAILABLE=1; fi
if [[ "$MODE" == "auto" ]]; then
  if [[ "$SYSTEMD_AVAILABLE" -eq 1 ]]; then MODE="systemd"; else MODE="foreground"; fi
fi
if [[ "$MODE" == "systemd" && "$SYSTEMD_AVAILABLE" -ne 1 ]]; then
  die "systemd mode was requested, but systemd is not PID 1. Use --mode foreground on Railway/Docker."
fi
if [[ "$MODE" == "systemd" && "${EUID:-$(id -u)}" -ne 0 ]]; then die "Systemd mode must be run with sudo or as root."; fi

restore_service_on_failure() {
  local status=$?
  trap - EXIT
  if [[ "$status" -ne 0 && "$SYSTEMD_WAS_ACTIVE" -eq 1 ]]; then
    echo "Installer failed; attempting to restart the previously active FinderBot service..." >&2
    systemctl start "$SERVICE_NAME" || true
  fi
  exit "$status"
}
trap restore_service_on_failure EXIT

if [[ "$SYSTEMD_AVAILABLE" -eq 1 ]] && systemctl is-active --quiet "$SERVICE_NAME"; then
  [[ "$MODE" == "systemd" ]] || die "The systemd bot service is active. Use --mode systemd for upgrades to avoid starting a duplicate foreground bot."
  log "Stopping the active FinderBot service before updating files and dependencies"
  systemctl stop "$SERVICE_NAME"
  SYSTEMD_WAS_ACTIVE=1
fi

install_apt_packages() {
  local packages=(python3 python3-venv python3-pip ca-certificates)
  [[ "$MODE" != "systemd" ]] || packages+=(git util-linux)
  command -v apt-get >/dev/null 2>&1 || die "Required Python/venv packages are missing and apt-get is unavailable."
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Install the required OS packages as root, then rerun the installer."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends "${packages[@]}"
}

if [[ "$MODE" == "systemd" ]]; then
  log "Installing required operating-system packages"
  install_apt_packages
  if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
else
  if command -v python3 >/dev/null 2>&1; then
    :
  elif command -v apt-get >/dev/null 2>&1 && [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    install_apt_packages
  else
    die "python3 is required in foreground/container mode."
  fi
fi

EXISTING_CONFIG=0
if [[ -f "$PROJECT_DIR/.env" ]]; then
  EXISTING_CONFIG=1
  log "Preserving existing .env configuration; the setup wizard will not overwrite it"
else
  TOKEN="${TELEGRAM_BOT_TOKEN:-}"
  USER_IDS="${ALLOWED_USER_IDS:-}"
  CSV_HEADER="${CSV_HAS_HEADER:-true}"
  CSV_DELIMITER="${CSV_DELIMITER:-,}"
  CSV_ENCODING="${CSV_ENCODING:-utf-8-sig}"
  UPLOAD_ENABLED="${UPLOAD_ENABLED:-true}"
  LOCAL_API_ENABLED="false"
  TELEGRAM_BASE_URL="${TELEGRAM_BASE_URL:-https://api.telegram.org/bot}"
  TELEGRAM_FILE_BASE_URL="${TELEGRAM_FILE_BASE_URL:-https://api.telegram.org/file/bot}"

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
      case "${value,,}" in y|yes|بله|true) printf 'true'; return ;; n|no|خیر|false) printf 'false'; return ;; *) warn "Please answer yes or no." ;; esac
    done
  }
  valid_token() { [[ "$1" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; }
  valid_ids() { [[ "$1" =~ ^[0-9]+([[:space:]]*,[[:space:]]*[0-9]+)*$ ]]; }

  if [[ "$NON_INTERACTIVE" -eq 0 ]]; then
    [[ -t 0 ]] || die "No .env exists and this is not an interactive terminal. Set TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS and pass --non-interactive."
    cat <<EOF

Telegram CSV Search - first-time setup
--------------------------------------
Detected mode: $MODE
The bot token is read silently and is never printed.
EOF
    while [[ -z "$TOKEN" ]] || ! valid_token "$TOKEN"; do
      TOKEN="$(ask_secret "Paste your BotFather token")"
      valid_token "$TOKEN" || warn "Invalid token format."
    done
    while [[ -z "$USER_IDS" ]] || ! valid_ids "$USER_IDS"; do
      USER_IDS="$(ask_value "Enter numeric Telegram user ID(s), comma-separated")"
      valid_ids "$USER_IDS" || warn "Use numeric IDs only, e.g. 123456789"
    done
    CSV_HEADER="$(ask_yes_no "Do your CSV files normally contain a header row?" "${CSV_HEADER:-true}")"
    CSV_DELIMITER="$(ask_value "CSV delimiter (one character)" "$CSV_DELIMITER")"
    [[ ${#CSV_DELIMITER} -eq 1 ]] || die "CSV delimiter must be exactly one character."
    CSV_ENCODING="$(ask_value "CSV encoding" "$CSV_ENCODING")"
    UPLOAD_ENABLED="$(ask_yes_no "Allow authorized private-chat CSV uploads?" "$UPLOAD_ENABLED")"
    LOCAL_API_ENABLED="$(ask_yes_no "Connect to an already running Local Bot API Server for large files?" "n")"
    if [[ "$LOCAL_API_ENABLED" == "true" ]]; then
      TELEGRAM_BASE_URL="$(ask_value "Local Bot API base URL" "http://127.0.0.1:8081/bot")"
      TELEGRAM_FILE_BASE_URL="$(ask_value "Local Bot API file URL" "http://127.0.0.1:8081/file/bot")"
      warn "This configures URLs only; it does not install or start Local Bot API Server."
    fi
  fi

  valid_token "${TOKEN:-}" || die "Set a valid TELEGRAM_BOT_TOKEN before a non-interactive first install."
  valid_ids "${USER_IDS:-}" || die "Set numeric ALLOWED_USER_IDS before a non-interactive first install."
  [[ ${#CSV_DELIMITER} -eq 1 ]] || die "CSV_DELIMITER must be exactly one character."
  FIRST_ADMIN="${USER_IDS%%,*}"
  FIRST_ADMIN="$(printf '%s' "$FIRST_ADMIN" | tr -d '[:space:]')"
  mkdir -p "$PROJECT_DIR"
  log "Creating new private .env configuration"
  cat > "$PROJECT_DIR/.env" <<EOF
TELEGRAM_BOT_TOKEN=$TOKEN
ALLOWED_USER_IDS=$USER_IDS
ADMIN_USER_IDS=$FIRST_ADMIN
PRIMARY_ADMIN_USER_IDS=$FIRST_ADMIN
ACCESS_MODE=private
DATA_DIRECTORY=./data
DATABASE_PATH=./database/search.db
LOG_DIRECTORY=./logs
CSV_DELIMITER=$CSV_DELIMITER
CSV_ENCODING=$CSV_ENCODING
CSV_HAS_HEADER=$CSV_HEADER
CSV_COLUMN_NAMES=column_1,column_2,name,birth_date,phone
DUPLICATE_MODE=exact
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
UPLOAD_MAX_BYTES=10737418240
UPLOAD_PROGRESS_INTERVAL_SECONDS=3
DELETE_CSV_AFTER_IMPORT=false
TELEGRAM_BASE_URL=$TELEGRAM_BASE_URL
TELEGRAM_FILE_BASE_URL=$TELEGRAM_FILE_BASE_URL
EOF
  chmod 600 "$PROJECT_DIR/.env"
  unset TOKEN USER_IDS
fi

log "Preparing Python environment"
if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
  if ! python3 -m venv "$PROJECT_DIR/venv" 2>/dev/null; then
    if command -v apt-get >/dev/null 2>&1 && [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
      install_apt_packages
      python3 -m venv "$PROJECT_DIR/venv" || die "Could not create the Python virtual environment after installing python3-venv."
    else
      die "Could not create venv: install python3-venv on Debian/Ubuntu, then rerun."
    fi
  fi
fi
PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install --requirement "$PROJECT_DIR/requirements.txt"
chmod +x "$PROJECT_DIR/finderbot" "$PROJECT_DIR/railway_start.sh" "$PROJECT_DIR/install_finderbot_command.sh"

log "Validating configuration and preparing persistent directories"
mapfile -t BOT_PATHS < <("$PYTHON_BIN" - "$PROJECT_DIR" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from config import load_settings
settings = load_settings()
settings.validate_for_bot()
for path in (settings.data_directory, settings.database_path.parent, settings.log_directory):
    path.mkdir(parents=True, exist_ok=True)
    print(path)
PY
)
[[ ${#BOT_PATHS[@]} -eq 3 ]] || die "Configuration validation failed; review .env without sharing the bot token."
DATA_DIR="${BOT_PATHS[0]}"
DATABASE_DIR="${BOT_PATHS[1]}"
LOG_DIR="${BOT_PATHS[2]}"

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
ExecStart=$PYTHON_BIN $PROJECT_DIR/bot.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only
ProtectSystem=full
ReadWritePaths=$DATABASE_DIR $LOG_DIR $DATA_DIR
[Install]
WantedBy=multi-user.target
EOF
  PERMISSIONS_MARKER="$PROJECT_DIR/.finderbot-permissions-ready"
  if [[ ! -f "$PERMISSIONS_MARKER" ]]; then
    chown -R "$SERVICE_USER:$SERVICE_USER" "$PROJECT_DIR" "$DATA_DIR" "$DATABASE_DIR" "$LOG_DIR"
    touch "$PERMISSIONS_MARKER"
    chown "$SERVICE_USER:$SERVICE_USER" "$PERMISSIONS_MARKER"
  else
    # New code must be readable by the service account after each update, but large
    # CSV/database directories were already assigned once; don't traverse multi-GB
    # user data again on every installation.
    PERSISTENT_TOPS=()
    for persistent_path in "$DATA_DIR" "$DATABASE_DIR" "$LOG_DIR"; do
      if [[ "$persistent_path" == "$PROJECT_DIR/"* ]]; then
        relative_path="${persistent_path#"$PROJECT_DIR"/}"
        top_level="${relative_path%%/*}"
        case " ${PERSISTENT_TOPS[*]} " in *" $top_level "*) ;; *) PERSISTENT_TOPS+=("$top_level") ;; esac
      fi
    done
    FIND_EXCLUDES=()
    for top_level in "${PERSISTENT_TOPS[@]}"; do FIND_EXCLUDES+=( ! -path "$PROJECT_DIR/$top_level" ); done
    find "$PROJECT_DIR" -mindepth 1 -maxdepth 1 "${FIND_EXCLUDES[@]}" \
      -exec chown -R "$SERVICE_USER:$SERVICE_USER" {} +
    chown "$SERVICE_USER:$SERVICE_USER" "$PROJECT_DIR" "$DATA_DIR" "$DATABASE_DIR" "$LOG_DIR"
  fi
  chmod 750 "$PROJECT_DIR"
  chmod 640 "$PROJECT_DIR/.env"
  for dir in "$DATA_DIR" "$DATABASE_DIR" "$LOG_DIR"; do chmod u+rwx,g+rwx "$dir"; done
  if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]] && id -u "$SUDO_USER" >/dev/null 2>&1; then
    usermod -aG "$SERVICE_USER" "$SUDO_USER" || warn "Could not add $SUDO_USER to the $SERVICE_USER group. Use sudo finderbot for service control."
  fi
fi

log "Installing the finderbot command"
"$PROJECT_DIR/install_finderbot_command.sh"

if [[ "$MODE" == "systemd" ]]; then
  runuser -u "$SERVICE_USER" -- "$PYTHON_BIN" - "$PROJECT_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from config import load_settings
from database import connect, initialize_access_control, initialize_database
settings = load_settings()
connection = connect(settings.database_path)
try:
    initialize_database(connection)
    initialize_access_control(connection, settings)
finally:
    connection.close()
PY
else
  "$PYTHON_BIN" - "$PROJECT_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from config import load_settings
from database import connect, initialize_access_control, initialize_database
settings = load_settings()
connection = connect(settings.database_path)
try:
    initialize_database(connection)
    initialize_access_control(connection, settings)
finally:
    connection.close()
PY
fi

if [[ "$SKIP_IMPORT" -eq 0 ]]; then
  if [[ "$EXISTING_CONFIG" -eq 1 && "$FORCE_IMPORT" -eq 0 ]]; then
    warn "Existing installation detected: CSV import was skipped to avoid changing existing data. Use --import-existing to explicitly recheck/import source files."
  else
    log "Importing CSV files from the configured DATA_DIRECTORY"
    if [[ "$MODE" == "systemd" ]]; then
      runuser -u "$SERVICE_USER" -- "$PYTHON_BIN" "$PROJECT_DIR/import_csv.py"
    else
      "$PYTHON_BIN" "$PROJECT_DIR/import_csv.py"
    fi
  fi
fi

if [[ "$MODE" == "systemd" ]]; then
  log "Configuring persistent systemd service"
  install -m 644 "$PROJECT_DIR/telegram-csv-search.service" "/etc/systemd/system/$SERVICE_NAME.service"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  if [[ "$NO_START" -eq 0 ]]; then
    systemctl restart "$SERVICE_NAME"
    trap - EXIT
  else
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    trap - EXIT
    warn "Configured and enabled but left stopped (--no-start). Run: finderbot"
  fi
  cat <<EOF

Installation/update complete in systemd mode.
FinderBot command: finderbot
Service:           $SERVICE_NAME
Check:             systemctl status $SERVICE_NAME
Logs:              journalctl -u $SERVICE_NAME -f
EOF
else
  cat <<EOF

Configuration complete in foreground/container mode.
FinderBot command: finderbot
Platform Start Command:
  ./railway_start.sh

For Railway/Docker, use the platform Start Command and persistent volume.
EOF
  if [[ "$NO_START" -eq 0 ]]; then
    trap - EXIT
    log "Starting bot in foreground"
    exec "$PROJECT_DIR/railway_start.sh"
  fi
  trap - EXIT
fi
