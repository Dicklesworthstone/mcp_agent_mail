#!/usr/bin/env bash
set -euo pipefail

# Color styles (best-effort)
if command -v tput >/dev/null 2>&1 && [[ -t 1 ]]; then
  _b=$(tput bold); _dim=$(tput dim); _red=$(tput setaf 1); _grn=$(tput setaf 2); _ylw=$(tput setaf 3); _blu=$(tput setaf 4); _mag=$(tput setaf 5); _cyn=$(tput setaf 6); _rst=$(tput sgr0)
else
  _b=""; _dim=""; _red=""; _grn=""; _ylw=""; _blu=""; _mag=""; _cyn=""; _rst=""
fi

printf "%b\n" "${_b}${_cyn}==> OpenAI Codex CLI Integration (one-stop MCP config)${_rst}"
echo
echo "This script will:"
echo "  1) Detect your MCP HTTP endpoint from settings."
echo "  2) Auto-generate a bearer token if missing and embed it."
echo "  3) Generate a project-local codex.mcp.json (auto-backup existing)."
echo "  4) Create scripts/run_server_with_token.sh to start the server with the token."
echo
# Parse args: --yes and --project-dir
_auto_yes=0
TARGET_DIR=""
_args=("$@")
for ((i=0; i<${#_args[@]}; i++)); do
  a="${_args[$i]}"
  case "$a" in
    --yes) _auto_yes=1 ;;
    --project-dir) i=$((i+1)); TARGET_DIR="${_args[$i]:-}" ;;
    --project-dir=*) TARGET_DIR="${a#*=}" ;;
  esac
done
if [[ "${_auto_yes}" == "1" || "${AUTO_YES:-}" == "1" ]]; then
  _ans="y"
else
  read -r -p "Proceed? [y/N] " _ans
fi
if [[ "${_ans:-}" != "y" && "${_ans:-}" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"
if [[ -z "${TARGET_DIR}" ]]; then
  TARGET_DIR="$ROOT_DIR"
fi

printf "%b\n" "${_b}${_cyn}==> Resolving HTTP endpoint from settings${_rst}"
eval "$(uv run python - <<'PY'
from mcp_agent_mail.config import get_settings
s = get_settings()
print(f"export _HTTP_HOST='{s.http.host}'")
print(f"export _HTTP_PORT='{s.http.port}'")
print(f"export _HTTP_PATH='{s.http.path}'")
PY
)"

_URL="http://${_HTTP_HOST}:${_HTTP_PORT}${_HTTP_PATH}"
printf "%b\n" "${_grn}Detected MCP HTTP endpoint:${_rst} ${_URL}"

_TOKEN=""
if [[ -n "${INTEGRATION_BEARER_TOKEN:-}" ]]; then
  _TOKEN="${INTEGRATION_BEARER_TOKEN}"
fi
if [[ -z "${_TOKEN}" && -f .env ]]; then
  _TOKEN=$(grep -E '^HTTP_BEARER_TOKEN=' .env | sed -E 's/^HTTP_BEARER_TOKEN=//') || true
fi
if [[ -z "${_TOKEN}" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    _TOKEN=$(openssl rand -hex 32)
  else
    _TOKEN=$(uv run python - <<'PY'
import secrets; print(secrets.token_hex(32))
PY
)
  fi
  echo "Generated bearer token."
fi

OUT_JSON="${TARGET_DIR}/codex.mcp.json"
if [[ -f "$OUT_JSON" ]]; then cp "$OUT_JSON" "${OUT_JSON}.bak.$(date +%s)"; fi
echo "==> Writing ${OUT_JSON}"
if [[ -n "${_TOKEN}" ]]; then
  AUTH_HEADER_LINE='        "Authorization": "Bearer ${_TOKEN}"
'
else
  AUTH_HEADER_LINE=''
fi
cat > "$OUT_JSON" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "http",
      "url": "${_URL}",
      "headers": {${AUTH_HEADER_LINE}}
    }
  }
}
JSON

printf "%b\n" "${_b}${_cyn}==> Creating run helper script with token${_rst}"
mkdir -p scripts
RUN_HELPER="scripts/run_server_with_token.sh"
cat > "$RUN_HELPER" <<SH
#!/usr/bin/env bash
set -euo pipefail
export HTTP_BEARER_TOKEN="${_TOKEN}"
uv run python -m mcp_agent_mail.cli serve-http "\$@"
SH
chmod +x "$RUN_HELPER"

printf "%b\n" "${_b}${_cyn}==> Attempt readiness check (non-blocking)${_rst}"
set +e
curl -fsS --connect-timeout 1 --max-time 2 --retry 0 "http://${_HTTP_HOST}:${_HTTP_PORT}/health/readiness" >/dev/null 2>&1
_rc=$?
set -e
if [[ $_rc -eq 0 ]]; then
  printf "%b\n" "${_grn}Server readiness OK.${_rst}"
else
  printf "%b\n" "${_ylw}Note:${_rst} server not reachable. Start with: ${_b}uv run python -m mcp_agent_mail.cli serve-http${_rst}"
fi

echo
printf "%b\n" "${_b}${_cyn}==> Registering MCP server in Codex CLI config${_rst}"
# Update user-level ~/.codex/config.toml
CODEX_DIR="${HOME}/.codex"
mkdir -p "$CODEX_DIR"
USER_TOML="${CODEX_DIR}/config.toml"
if [[ -f "$USER_TOML" ]]; then cp "$USER_TOML" "${USER_TOML}.bak.$(date +%s)"; fi
if ! grep -q "^\[mcp_servers.mcp_agent_mail\]" "$USER_TOML" 2>/dev/null; then
  {
    echo ""
    echo "# MCP servers configuration (mcp-agent-mail)"
    echo "[mcp_servers.mcp_agent_mail]"
    echo "transport = \"http\""
    echo "url = \"${_URL}\""
    # Headers omitted for local dev (server allows localhost without Authorization)
  } >> "$USER_TOML"
fi

# Also write project-local .codex/config.toml for portability
LOCAL_CODEX_DIR="${TARGET_DIR}/.codex"
mkdir -p "$LOCAL_CODEX_DIR"
LOCAL_TOML="${LOCAL_CODEX_DIR}/config.toml"
if [[ -f "$LOCAL_TOML" ]]; then cp "$LOCAL_TOML" "${LOCAL_TOML}.bak.$(date +%s)"; fi
cat > "$LOCAL_TOML" <<TOML
# Project-local Codex MCP configuration
[mcp_servers.mcp_agent_mail]
transport = "http"
url = "${_URL}"
# headers can be added if needed; localhost allowed without Authorization
TOML

echo "Done."

printf "%b\n" "${_b}${_cyn}==> Bootstrapping project and agent on server${_rst}"
if [[ $_rc -ne 0 ]]; then
  printf "%b\n" "${_ylw}Skipping bootstrap:${_rst} server not reachable (ensure_project/register_agent)."
else
  _AUTH_ARGS=()
  if [[ -n "${_TOKEN}" ]]; then _AUTH_ARGS+=("-H" "Authorization: Bearer ${_TOKEN}"); fi
  _HUMAN_KEY="${TARGET_DIR}"
  curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"ensure_project\",\"arguments\":{\"human_key\":\"${_HUMAN_KEY}\"}}}" \
    "${_URL}" >/dev/null 2>&1 || true
  curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":\"${_HUMAN_KEY}\",\"program\":\"codex-cli\",\"model\":\"gpt-5-codex\",\"name\":\"${USER:-codex}\",\"task_description\":\"setup\"}}}" \
    "${_URL}" >/dev/null 2>&1 || true
fi

