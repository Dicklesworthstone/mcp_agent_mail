#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${ROOT_DIR}/scripts/lib.sh" ]]; then
  # shellcheck disable=SC1090
  . "${ROOT_DIR}/scripts/lib.sh"
fi

if [[ -z "${HTTP_BEARER_TOKEN:-}" ]] && declare -F resolve_integration_bearer_token >/dev/null 2>&1; then
  HTTP_BEARER_TOKEN="$(resolve_integration_bearer_token "${ROOT_DIR}")"
fi
if [[ -z "${HTTP_BEARER_TOKEN:-}" ]]; then
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    HTTP_BEARER_TOKEN=$(grep -E '^HTTP_BEARER_TOKEN=' "${ROOT_DIR}/.env" 2>/dev/null | tail -n 1 | sed -E 's/^HTTP_BEARER_TOKEN=//') || true
  fi
fi
if [[ -z "${HTTP_BEARER_TOKEN:-}" ]] && declare -F generate_bearer_token >/dev/null 2>&1; then
  HTTP_BEARER_TOKEN="$(generate_bearer_token)"
fi
if [[ -z "${HTTP_BEARER_TOKEN:-}" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    HTTP_BEARER_TOKEN="$(openssl rand -hex 32)"
  elif command -v uv >/dev/null 2>&1; then
    HTTP_BEARER_TOKEN="$(uv run python -c 'import secrets;print(secrets.token_hex(32))')"
  else
    HTTP_BEARER_TOKEN="$(date +%s)_$(hostname 2>/dev/null || echo host)"
  fi
fi
export HTTP_BEARER_TOKEN

uv run python -m mcp_agent_mail.cli serve-http "$@"
