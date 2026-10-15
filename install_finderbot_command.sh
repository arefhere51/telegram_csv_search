#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
COMMAND="$PROJECT_DIR/finderbot"

if [[ ! -x "$COMMAND" ]]; then
  printf 'FinderBot command is missing or not executable: %s\n' "$COMMAND" >&2
  exit 1
fi

if [[ -n "${FINDERBOT_BIN_DIR:-}" ]]; then
  TARGET_DIR="$(realpath -m -- "$FINDERBOT_BIN_DIR")"
  mkdir -p "$TARGET_DIR"
elif [[ -w /usr/local/bin ]]; then
  TARGET_DIR="/usr/local/bin"
elif command -v sudo >/dev/null 2>&1 && sudo -n test -w /usr/local/bin 2>/dev/null; then
  TARGET_DIR="/usr/local/bin"
else
  TARGET_DIR="${HOME:?HOME must be set}/.local/bin"
  mkdir -p "$TARGET_DIR"
fi
TARGET="$TARGET_DIR/finderbot"

if [[ -L "$TARGET" ]]; then
  LINK_TARGET="$(readlink -- "$TARGET")"
  if [[ "$LINK_TARGET" == "$COMMAND" || "$(readlink -f -- "$TARGET" 2>/dev/null || true)" == "$COMMAND" ]]; then
    printf 'The finderbot command is already installed: %s\n' "$TARGET"
  else
    printf 'Refusing to replace the existing symlink: %s -> %s\n' "$TARGET" "$LINK_TARGET" >&2
    exit 1
  fi
elif [[ -e "$TARGET" ]]; then
  printf 'Refusing to overwrite the existing file: %s\n' "$TARGET" >&2
  exit 1
else
  if [[ -w "$TARGET_DIR" ]]; then
    ln -s -- "$COMMAND" "$TARGET"
  else
    sudo ln -s -- "$COMMAND" "$TARGET"
  fi
  printf 'Installed: %s -> %s\n' "$TARGET" "$COMMAND"
fi

if [[ "$TARGET_DIR" == "${HOME:-}/.local/bin" ]]; then
  PROFILE="${HOME}/.profile"
  touch "$PROFILE"
  if ! grep -Fq '# FinderBot command path' "$PROFILE"; then
    cat >> "$PROFILE" <<'EOF'

# FinderBot command path
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
EOF
  fi
  printf 'The ~/.local/bin path is saved in ~/.profile; reconnect or run: source ~/.profile\n'
fi
printf 'You can now type: finderbot\n'
