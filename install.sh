#!/usr/bin/env bash
# One-command, data-preserving bootstrap installer.
# bash <(curl -Ls https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main/install.sh)
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

REPO_URL="${REPO_URL:-https://github.com/arefhere51/telegram_csv_search.git}"
BRANCH="${BRANCH:-main}"
DEFAULT_DIR="/telegram_csv_search"
if [[ ! -d "/" || ! -w "/" || "${EUID:-$(id -u)}" -ne 0 ]]; then
  DEFAULT_DIR="${HOME:-/tmp}/telegram_csv_search"
fi
PROJECT_DIR="${PROJECT_DIR:-$DEFAULT_DIR}"
INSTALL_MODE="auto"
SYSTEMD_STOPPED_FOR_UPDATE=0
for ((i=1; i<=$#; i++)); do
  if [[ "${!i}" == "--mode" && $((i+1)) -le $# ]]; then
    next=$((i+1)); INSTALL_MODE="${!next}"
  fi
done

# Resolve relative paths even if the caller's previous working directory disappeared.
if [[ "$PROJECT_DIR" != /* ]]; then
  invoking_dir="$(pwd -P 2>/dev/null || printf '/')"
  PROJECT_DIR="${invoking_dir%/}/$PROJECT_DIR"
fi
PROJECT_DIR="$(realpath -m -- "$PROJECT_DIR")"
PARENT_DIR="$(dirname -- "$PROJECT_DIR")"
mkdir -p "$PARENT_DIR"
cd /

printf '\nFinderBot — نصب یک‌دستوری\n'
printf 'مخزن: %s\nمسیر نصب: %s\n\n' "$REPO_URL" "$PROJECT_DIR"

install_bootstrap_packages() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || {
    echo "ERROR: git is missing. Install git or run this installer as root." >&2
    exit 1
  }
  command -v apt-get >/dev/null 2>&1 || {
    echo "ERROR: git is missing and apt-get is unavailable. Install git, then retry." >&2
    exit 1
  }
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends git ca-certificates
}
command -v git >/dev/null 2>&1 || install_bootstrap_packages

stop_existing_systemd_bot() {
  [[ "$INSTALL_MODE" != "foreground" ]] || return 0
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || return 0
  command -v systemctl >/dev/null 2>&1 || return 0
  [[ "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" == "systemd" ]] || return 0
  if systemctl is-active --quiet telegram-csv-search.service; then
    echo "Stopping the existing FinderBot service before updating its code..."
    systemctl stop telegram-csv-search.service
    SYSTEMD_STOPPED_FOR_UPDATE=1
  fi
}

if [[ -d "$PROJECT_DIR/.git" ]]; then
  stop_existing_systemd_bot
  echo "Updating the existing Git checkout (ignored data and virtualenv are retained)..."
  ENV_BACKUP=""
  if [[ -f "$PROJECT_DIR/.env" ]]; then
    ENV_BACKUP="$(mktemp "$PARENT_DIR/.finderbot-env.XXXXXX")"
    cp -p -- "$PROJECT_DIR/.env" "$ENV_BACKUP"
  fi
  restore_env() {
    if [[ -n "$ENV_BACKUP" && -f "$ENV_BACKUP" ]]; then
      cp -p -- "$ENV_BACKUP" "$PROJECT_DIR/.env"
      rm -f -- "$ENV_BACKUP"
      ENV_BACKUP=""
    fi
  }
  update_cleanup() {
    local status=$?
    restore_env
    if [[ "$status" -ne 0 && "$SYSTEMD_STOPPED_FOR_UPDATE" -eq 1 ]]; then
      echo "Update failed; attempting to bring the previous FinderBot service back up..." >&2
      systemctl start telegram-csv-search.service || true
    fi
    exit "$status"
  }
  trap update_cleanup EXIT
  git -C "$PROJECT_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$PROJECT_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
  git -C "$PROJECT_DIR" reset --hard "origin/$BRANCH"
  restore_env
  trap - EXIT
  SYSTEMD_STOPPED_FOR_UPDATE=0
else
  STAGE=""
  PRESERVE_DIR=""
  SOURCE_BACKUP=""
  if [[ -e "$PROJECT_DIR" ]]; then
    echo "Existing non-Git installation found; keeping configuration and runtime data in place."
    PRESERVE_DIR="$(mktemp -d "$PARENT_DIR/.finderbot-preserve.XXXXXX")"
    PRESERVE_ITEMS=(.env data database logs run venv .venv credentials .finderbot-permissions-ready)
    add_preserve_item() {
      local candidate="$1" current
      [[ -n "$candidate" && "$candidate" != "." && "$candidate" != ".git" ]] || return 0
      for current in "${PRESERVE_ITEMS[@]}"; do [[ "$current" == "$candidate" ]] && return 0; done
      PRESERVE_ITEMS+=("$candidate")
    }
    if [[ -f "$PROJECT_DIR/.env" ]]; then
      for key in DATA_DIRECTORY DATABASE_PATH LOG_DIRECTORY; do
        value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$PROJECT_DIR/.env")"
        value="${value%$'\r'}"
        value="${value#\"}"; value="${value%\"}"
        value="${value#\'}"; value="${value%\'}"
        [[ -n "$value" ]] || continue
        if [[ "$value" == /* ]]; then resolved="$(realpath -m -- "$value")"; else resolved="$(realpath -m -- "$PROJECT_DIR/$value")"; fi
        if [[ "$resolved" == "$PROJECT_DIR/"* ]]; then
          relative="${resolved#"$PROJECT_DIR"/}"
          add_preserve_item "${relative%%/*}"
        fi
      done
    fi
    for item in "${PRESERVE_ITEMS[@]}"; do
      if [[ -e "$PROJECT_DIR/$item" || -L "$PROJECT_DIR/$item" ]]; then
        mv -- "$PROJECT_DIR/$item" "$PRESERVE_DIR/$item"
      fi
    done
    shopt -s nullglob
    for item in "$PROJECT_DIR"/drive-service-account*.json "$PROJECT_DIR"/google-drive-service-account*.json; do
      mv -- "$item" "$PRESERVE_DIR/"
    done
    shopt -u nullglob
    SOURCE_BACKUP="${PROJECT_DIR}.backup.$(date +%Y%m%d%H%M%S)"
    suffix=0
    while [[ -e "$SOURCE_BACKUP" ]]; do suffix=$((suffix + 1)); SOURCE_BACKUP="${PROJECT_DIR}.backup.$(date +%Y%m%d%H%M%S).$suffix"; done
    mv -- "$PROJECT_DIR" "$SOURCE_BACKUP"
  fi

  STAGE="$(mktemp -d "$PARENT_DIR/.finderbot-clone.XXXXXX")"
  rmdir "$STAGE"
  if ! git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$STAGE"; then
    rm -rf -- "$STAGE"
    if [[ -n "$SOURCE_BACKUP" && -d "$SOURCE_BACKUP" ]]; then mv -- "$SOURCE_BACKUP" "$PROJECT_DIR"; fi
    if [[ -n "$PRESERVE_DIR" && -d "$PRESERVE_DIR" ]]; then
      for item in "${PRESERVE_ITEMS[@]}"; do
        if [[ -e "$PRESERVE_DIR/$item" || -L "$PRESERVE_DIR/$item" ]]; then mv -- "$PRESERVE_DIR/$item" "$PROJECT_DIR/$item"; fi
      done
      for item in "$PRESERVE_DIR"/drive-service-account*.json "$PRESERVE_DIR"/google-drive-service-account*.json; do
        [[ -e "$item" ]] || continue
        mv -- "$item" "$PROJECT_DIR/"
      done
      rmdir "$PRESERVE_DIR" 2>/dev/null || true
    fi
    exit 1
  fi
  mv -- "$STAGE" "$PROJECT_DIR"
  if [[ -n "$PRESERVE_DIR" && -d "$PRESERVE_DIR" ]]; then
    for item in "${PRESERVE_ITEMS[@]}"; do
      if [[ -e "$PRESERVE_DIR/$item" || -L "$PRESERVE_DIR/$item" ]]; then
        rm -rf -- "$PROJECT_DIR/$item"
        mv -- "$PRESERVE_DIR/$item" "$PROJECT_DIR/$item"
      fi
    done
    for item in "$PRESERVE_DIR"/drive-service-account*.json "$PRESERVE_DIR"/google-drive-service-account*.json; do
      [[ -e "$item" ]] || continue
      mv -- "$item" "$PROJECT_DIR/"
    done
    rmdir "$PRESERVE_DIR" 2>/dev/null || true
  fi
  if [[ -n "$SOURCE_BACKUP" ]]; then
    echo "Previous source checkout kept as a rollback copy: $SOURCE_BACKUP"
  fi
fi

cd "$PROJECT_DIR"
chmod +x install_vps.sh railway_start.sh finderbot install_finderbot_command.sh

# Pass through installer options. The installer preserves .env/database/data and
# creates the `finderbot` command as part of this same run.
exec ./install_vps.sh --project-dir "$PROJECT_DIR" "$@"
