#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
COMMAND="$PROJECT_DIR/finderbot"
TARGET="/usr/local/bin/finderbot"

if [[ ! -x "$COMMAND" ]]; then
  printf 'FinderBot command is missing or not executable: %s\n' "$COMMAND" >&2
  exit 1
fi

if [[ -L "$TARGET" ]]; then
  CURRENT_TARGET="$(readlink -f -- "$TARGET" || true)"
  if [[ "$CURRENT_TARGET" == "$COMMAND" ]]; then
    printf 'The finderbot command is already installed: %s\n' "$TARGET"
    exit 0
  fi
  printf 'Refusing to replace the existing symlink: %s -> %s\n' "$TARGET" "$CURRENT_TARGET" >&2
  exit 1
elif [[ -e "$TARGET" ]]; then
  printf 'Refusing to overwrite the existing file: %s\n' "$TARGET" >&2
  exit 1
fi

if [[ -w /usr/local/bin ]]; then
  ln -s -- "$COMMAND" "$TARGET"
else
  if ! command -v sudo >/dev/null 2>&1; then
    printf 'Permission denied. Run this script as root or create the symlink manually:\n' >&2
    printf '  ln -s %q %q\n' "$COMMAND" "$TARGET" >&2
    exit 1
  fi
  sudo ln -s -- "$COMMAND" "$TARGET"
fi
printf 'Installed: %s -> %s\n' "$TARGET" "$COMMAND"
printf 'You can now type: finderbot\n'
