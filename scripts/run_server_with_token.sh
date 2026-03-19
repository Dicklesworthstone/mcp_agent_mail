#!/usr/bin/env bash
set -euo pipefail

AM_RUST_BIN="/Users/jemanuel/.local/bin/am"
AM_RUST_ENV_FILE="${HOME}/.config/mcp-agent-mail/config.env"

trim_ascii_whitespace() {
  local value="${1:-}"
  value="${value#\"${value%%[![:space:]]*}\"}"
  value="${value%\"${value##*[![:space:]]}\"}"
  printf '%s\n' "$value"
}

load_env_key() {
  local key="$1"
  [ -f "$AM_RUST_ENV_FILE" ] || return 0

  local raw
  raw=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$AM_RUST_ENV_FILE" 2>/dev/null | tail -1 | sed -E "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*//" || true)
  [ -n "$raw" ] || return 0

  raw=$(trim_ascii_whitespace "$raw")
  local parsed="" quote="" prev="" char=""
  local raw_len=${#raw}
  local i=0
  while [ "$i" -lt "$raw_len" ]; do
    char="${raw:$i:1}"
    if [ -n "$quote" ]; then
      parsed="${parsed}${char}"
      if [ "$char" = "$quote" ]; then
        quote=""
      fi
    else
      if [ "$char" = '"' ] || [ "$char" = "'" ]; then
        quote="$char"
        parsed="${parsed}${char}"
      elif [ "$char" = "#" ]; then
        if [ -z "$prev" ] || [[ "$prev" =~ [[:space:]] ]]; then
          break
        fi
        parsed="${parsed}${char}"
      else
        parsed="${parsed}${char}"
      fi
    fi
    prev="$char"
    i=$((i + 1))
  done

  raw=$(trim_ascii_whitespace "$parsed")
  raw="${raw%\"}"
  raw="${raw#\"}"
  raw="${raw%\'}"
  raw="${raw#\'}"
  export "${key}=${raw}"
}

for key in DATABASE_URL STORAGE_ROOT HTTP_HOST HTTP_PORT HTTP_PATH HTTP_BEARER_TOKEN TUI_ENABLED LLM_ENABLED LLM_DEFAULT_MODEL WORKTREES_ENABLED; do
  load_env_key "$key"
done

if [ ! -x "$AM_RUST_BIN" ]; then
  echo "mcp-agent-mail Rust CLI not found at $AM_RUST_BIN" >&2
  exit 1
fi

exec "$AM_RUST_BIN" "$@"
