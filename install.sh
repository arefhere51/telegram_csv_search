#!/usr/bin/env bash
# One-command bootstrap installer.
# Usage: bash <(curl -Ls https://raw.githubusercontent.com/arefhere51/telegram_csv_search/main/install.sh)
set -Eeuo pipefail
IFS=$'\n\t'

REPO_URL="${REPO_URL:-https://github.com/arefhere51/telegram_csv_search.git}"
BRANCH="${BRANCH:-main}"
DEFAULT_DIR="/telegram_csv_search"
if [[ ! -d "/" || ! -w "/" || "${EUID:-$(id -u)}" -ne 0 ]]; then
  DEFAULT_DIR="${HOME:-/tmp}/telegram_csv_search"
fi
PROJECT_DIR="${PROJECT_DIR:-$DEFAULT_DIR}"

# Resolve a relative target before changing directory. If the invoking shell is still
# inside a directory removed by a previous install attempt, pwd fails; use / as the
# relative-path base in that recovery case. Git must run from a valid working directory.
if [[ "$PROJECT_DIR" != /* ]]; then
  invoking_dir="$(pwd -P 2>/dev/null || printf '/')"
  PROJECT_DIR="${invoking_dir%/}/$PROJECT_DIR"
fi
cd /

printf '\nTelegram CSV Search - one-command installer\n'
printf 'Repository: %s\nTarget:     %s\n\n' "$REPO_URL" "$PROJECT_DIR"

command -v git >/dev/null 2>&1 || {
  echo "ERROR: git is required. Install git and run this command again." >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  echo "ERROR: python3 is required." >&2
  exit 1
}

if [[ -d "$PROJECT_DIR/.git" ]]; then
  echo "Updating existing checkout..."
  git -C "$PROJECT_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$PROJECT_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
  git -C "$PROJECT_DIR" reset --hard "origin/$BRANCH"
else
  if [[ -e "$PROJECT_DIR" ]]; then
    backup="${PROJECT_DIR}.backup.$(date +%Y%m%d%H%M%S)"
    echo "Existing non-Git directory found; moving it to $backup"
    mv "$PROJECT_DIR" "$backup"
  fi
  echo "Cloning repository..."
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$PROJECT_DIR"
fi

cd "$PROJECT_DIR"
chmod +x install_vps.sh railway_start.sh

# Pass through optional installer arguments, e.g. --skip-import or --no-start.
exec ./install_vps.sh --project-dir "$PROJECT_DIR" "$@"
