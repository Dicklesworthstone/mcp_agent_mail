#!/usr/bin/env bash
set -euo pipefail

# Source shared helpers
ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
if [[ -f "${ROOT_DIR}/scripts/lib.sh" ]]; then
  # shellcheck disable=SC1090
  . "${ROOT_DIR}/scripts/lib.sh"
else
  echo "FATAL: scripts/lib.sh not found" >&2
  exit 1
fi
init_colors
setup_traps
parse_common_flags "$@"
require_cmd uv
require_cmd curl

log_step "OpenAI Codex CLI Integration (one-stop MCP config)"
echo
echo "This script will:"
echo "  1) Detect your MCP HTTP endpoint from settings."
echo "  2) Auto-generate a bearer token if missing and embed it."
echo "  3) Generate a project-local codex.mcp.json (auto-backup existing)."
echo "  4) Create scripts/run_server_with_token.sh to start the server with the token."
echo "  5) Install a project-local Codex PostToolUse hook (.codex/hooks.json) for inbox reminders."
echo
TARGET_DIR="${PROJECT_DIR:-}"
if [[ -z "${TARGET_DIR}" ]]; then TARGET_DIR="${ROOT_DIR}"; fi
if ! confirm "Proceed?"; then log_warn "Aborted."; exit 1; fi

cd "$ROOT_DIR"

log_step "Resolving HTTP endpoint from settings"
eval "$(uv run python - <<'PY'
import shlex
from mcp_agent_mail.config import get_settings
s = get_settings()
print(f"export _HTTP_HOST={shlex.quote(str(s.http.host))}")
print(f"export _HTTP_PORT={shlex.quote(str(s.http.port))}")
print(f"export _HTTP_PATH={shlex.quote(str(s.http.path))}")
print(f"export _HTTP_BEARER_TOKEN={shlex.quote(str(s.http.bearer_token or ''))}")
PY
)"

# Validate Python eval output (Bug 15)
if [[ -z "${_HTTP_HOST}" || -z "${_HTTP_PORT}" || -z "${_HTTP_PATH}" ]]; then
  log_err "Failed to detect HTTP endpoint from settings (Python eval failed)"
  exit 1
fi

_URL="http://${_HTTP_HOST}:${_HTTP_PORT}${_HTTP_PATH}"
log_ok "Detected MCP HTTP endpoint: ${_URL}"

_TOKEN_GENERATED=0
_TOKEN="$(resolve_integration_bearer_token "${ROOT_DIR}")"
if [[ -z "${_TOKEN}" ]]; then
  _TOKEN="$(generate_bearer_token)"
  _TOKEN_GENERATED=1
  log_ok "Generated bearer token."
fi
if [[ "${_TOKEN_GENERATED}" == "1" ]]; then
  # Keep local integrations consistent by persisting the generated token to .env.
  # This ensures scripts/run_server_with_token.sh and Codex configs use the same token.
  if update_env_var "HTTP_BEARER_TOKEN" "${_TOKEN}"; then
    log_ok "Saved bearer token to .env"
  else
    log_warn "Failed to save bearer token to .env (continuing)"
  fi
fi

_MORPH_ENABLED_TOOLS=$(default_morph_enabled_tools)
_MORPH_API_KEY=$(resolve_morph_api_key)
if [[ -n "${_MORPH_API_KEY}" ]]; then
  log_ok "Morph MCP will be configured in grep-only mode."
else
  log_warn "Morph API key not found; skipping morph-mcp setup."
fi

OUT_JSON="${TARGET_DIR}/codex.mcp.json"
backup_file "$OUT_JSON"
log_step "Writing ${OUT_JSON}"
if [[ -n "${_TOKEN}" ]]; then
  AUTH_HEADER_LINE="        \"Authorization\": \"Bearer ${_TOKEN}\""
else
  AUTH_HEADER_LINE=''
fi
MORPH_MCP_JSON=""
if [[ -n "${_MORPH_API_KEY}" ]]; then
  MORPH_MCP_JSON=$(cat <<JSONFRAG
,
    "morph-mcp": {
      "command": "npx",
      "args": ["-y", "@morphllm/morphmcp"],
      "env": {
        "ENABLED_TOOLS": "${_MORPH_ENABLED_TOOLS}",
        "MORPH_API_KEY": "${_MORPH_API_KEY}"
      }
    }
JSONFRAG
)
fi
write_atomic "$OUT_JSON" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "http",
      "url": "${_URL}",
      "headers": {${AUTH_HEADER_LINE}}
    }${MORPH_MCP_JSON}
  }
}
JSON
json_validate "$OUT_JSON" || true
set_secure_file "$OUT_JSON"

log_step "Creating run helper script (centralized in lib.sh)"
mkdir -p scripts
RUN_HELPER="scripts/run_server_with_token.sh"
write_run_helper_script "$RUN_HELPER"

log_step "Checking server and registering agent"
_AGENT=""
_SERVER_AVAILABLE=0
if readiness_poll "${_HTTP_HOST}" "${_HTTP_PORT}" "/health/readiness" 3 0.5; then
  _SERVER_AVAILABLE=1
  log_ok "Server is reachable."

  _AUTH_ARGS=()
  if [[ -n "${_TOKEN}" ]]; then _AUTH_ARGS+=("-H" "Authorization: Bearer ${_TOKEN}"); fi

  # Escape the project path for JSON
  _HUMAN_KEY_ESCAPED=$(json_escape_string "${TARGET_DIR}") || { log_err "Failed to escape project path"; exit 1; }

  # ensure_project
  if curl -fsS --connect-timeout 2 --max-time 5 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"ensure_project\",\"arguments\":{\"human_key\":${_HUMAN_KEY_ESCAPED}}}}" \
      "${_URL}" >/dev/null 2>&1; then
    log_ok "Ensured project on server"
  else
    log_warn "Failed to ensure project"
  fi

  # register_agent - DON'T pass a name, let server auto-generate adjective+noun name
  # Capture response to extract the generated name
  _REGISTER_RESPONSE=$(curl -sS --connect-timeout 2 --max-time 5 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":${_HUMAN_KEY_ESCAPED},\"program\":\"codex-cli\",\"model\":\"gpt-5-codex\",\"task_description\":\"setup\"}}}" \
      "${_URL}" 2>/dev/null || echo "")

  _REG_TOKEN=""
  if [[ -n "${_REGISTER_RESPONSE}" ]]; then
    # Extract agent name + registration_token from JSON response using jq or Python
    if command -v jq >/dev/null 2>&1; then
      _AGENT=$(echo "${_REGISTER_RESPONSE}" | jq -r '.result.content[0].text // empty' 2>/dev/null | jq -r '.name // empty' 2>/dev/null || echo "")
      _REG_TOKEN=$(echo "${_REGISTER_RESPONSE}" | jq -r '.result.content[0].text // empty' 2>/dev/null | jq -r '.registration_token // empty' 2>/dev/null || echo "")
    else
      _AGENT=$(echo "${_REGISTER_RESPONSE}" | uv run python -c 'import sys,json; r=json.load(sys.stdin); c=r.get("result",{}).get("content",[]); print(json.loads(c[0]["text"])["name"] if c else "")' 2>/dev/null || echo "")
      _REG_TOKEN=$(echo "${_REGISTER_RESPONSE}" | uv run python -c 'import sys,json; r=json.load(sys.stdin); c=r.get("result",{}).get("content",[]); print(json.loads(c[0]["text"]).get("registration_token","") if c else "")' 2>/dev/null || echo "")
    fi
    if [[ -n "${_AGENT}" ]]; then
      log_ok "Registered agent: ${_AGENT}"
    else
      log_warn "Could not parse agent name from response"
    fi
    if [[ -z "${_REG_TOKEN}" ]]; then
      log_warn "Could not parse registration_token from register_agent response."
      log_warn "The inbox-reminder hook will silently no-op until this is set (fetch_inbox auth)."
    fi
  else
    log_warn "Failed to register agent"
  fi
else
  _rc=1; log_warn "Server not reachable. Start with: uv run python -m mcp_agent_mail.cli serve-http"
  log_warn "Hooks will be configured without agent name. Agent will need to call register_agent at session start."
fi

# If we still don't have an agent name, warn the user
_PROJ_DISPLAY=$(basename "$TARGET_DIR")
if [[ -z "${_AGENT}" ]]; then
  _AGENT="YOUR_AGENT_NAME"
  log_warn "No agent name available (server not running). Using placeholder '${_AGENT}'."
  log_warn "Hooks with placeholder values will silently skip execution."
  log_warn "After starting the server, reconfigure integration."
fi

echo
log_step "Installing inbox-reminder hook (Codex PostToolUse)"
# Codex's legacy top-level `notify` program cannot remind the agent: current
# Codex spawns it with stdin/stdout/stderr redirected to /dev/null and never
# feeds its output to the model (GH #273). Codex hooks (.codex/hooks.json)
# use the same envelope as Claude Code: a PostToolUse command hook that prints
# {"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":...}}
# lands in the model's context. check_inbox.sh already emits that envelope
# when AGENT_MAIL_HOOK_FORMAT=json.
#
# The hook is project-local (<project>/.codex/hooks.json), so every project
# gets its own agent identity and rate-limit window instead of one machine-wide
# inbox. Codex loads project-local hooks only once the project's .codex/ layer
# is trusted, and asks the user to review/trust each hook definition by hash.
HOOKS_DIR="${TARGET_DIR}/.codex/hooks"
mkdir -p "${HOOKS_DIR}"
INBOX_HOOK="${HOOKS_DIR}/check_inbox.sh"
if [[ -f "${ROOT_DIR}/scripts/hooks/check_inbox.sh" ]]; then
  cp "${ROOT_DIR}/scripts/hooks/check_inbox.sh" "${INBOX_HOOK}"
  chmod +x "${INBOX_HOOK}"
  log_ok "Installed inbox check hook to ${INBOX_HOOK}"
else
  log_warn "Could not find check_inbox.sh script"
fi

# Secrets (bearer + registration token) live in a mode-0700 wrapper script,
# never in hooks.json, which may be committed alongside other project hooks.
INBOX_WRAPPER="${HOOKS_DIR}/inbox_wrapper.sh"
write_atomic "$INBOX_WRAPPER" <<SH
#!/usr/bin/env bash
# Generated by scripts/integrate_codex_cli.sh — Codex PostToolUse inbox reminder.
export AGENT_MAIL_PROJECT='${TARGET_DIR}'
export AGENT_MAIL_AGENT='${_AGENT}'
export AGENT_MAIL_URL='${_URL}'
export AGENT_MAIL_TOKEN='${_TOKEN}'
export AGENT_MAIL_REGISTRATION_TOKEN='${_REG_TOKEN:-}'
export AGENT_MAIL_HOOK_FORMAT='json'
export AGENT_MAIL_INTERVAL='120'
exec '${INBOX_HOOK}' "\$@"
SH
chmod 700 "$INBOX_WRAPPER"
# The wrapper embeds the bearer and registration tokens: keep it out of git.
ensure_gitignore_entry "${TARGET_DIR}/.gitignore" ".codex/hooks/inbox_wrapper.sh"

HOOKS_JSON="${TARGET_DIR}/.codex/hooks.json"
INBOX_HOOK_ENTRY=$(cat <<HOOKJSON
{ "matcher": "Bash", "hooks": [ { "type": "command", "command": "'${INBOX_WRAPPER}'", "timeout": 10 } ] }
HOOKJSON
)
EXISTING_HOOKS="{}"
if [[ -f "$HOOKS_JSON" ]]; then
  EXISTING_HOOKS=$(cat "$HOOKS_JSON" 2>/dev/null || echo "{}")
  if command -v jq >/dev/null 2>&1 && ! echo "$EXISTING_HOOKS" | jq empty 2>/dev/null; then
    log_warn "Existing ${HOOKS_JSON} has invalid JSON; backing it up and starting fresh"
    backup_file "$HOOKS_JSON"
    EXISTING_HOOKS="{}"
  fi
fi
if command -v jq >/dev/null 2>&1; then
  MERGED_HOOKS=$(json_append_hook "$EXISTING_HOOKS" "PostToolUse" "$INBOX_HOOK_ENTRY" "inbox_wrapper.sh")
  write_atomic "$HOOKS_JSON" <<<"$MERGED_HOOKS"
  json_validate "$HOOKS_JSON" || log_warn "Invalid JSON in ${HOOKS_JSON}"
  log_ok "Merged PostToolUse inbox hook into ${HOOKS_JSON} (existing hooks preserved)"
elif [[ ! -f "$HOOKS_JSON" ]]; then
  write_atomic "$HOOKS_JSON" <<JSON
{
  "hooks": {
    "PostToolUse": [
      ${INBOX_HOOK_ENTRY}
    ]
  }
}
JSON
  json_validate "$HOOKS_JSON" || log_warn "Invalid JSON in ${HOOKS_JSON}"
  log_ok "Wrote ${HOOKS_JSON}"
else
  log_warn "jq not found and ${HOOKS_JSON} already exists; add this PostToolUse entry by hand:"
  log_warn "  ${INBOX_HOOK_ENTRY}"
fi
chmod 644 "$HOOKS_JSON" 2>/dev/null || true
_print "Codex will ask you to review and trust the new hook (hash-pinned) before it runs;"
_print "project-local hooks load only when ${TARGET_DIR}/.codex is trusted."

# A previous version of this installer registered scripts/hooks/codex_notify.sh
# through the top-level `notify` key. That path never reached the agent; the
# entry is harmless but now dead weight.
if grep -q 'notify_wrapper.sh' "${HOME}/.codex/config.toml" 2>/dev/null; then
  log_warn "${HOME}/.codex/config.toml still has a 'notify = [...notify_wrapper.sh]' entry from an older install."
  log_warn "It no longer does anything (Codex discards notify output); you can delete that line."
fi

log_step "Registering MCP server in Codex CLI config"
# Update user-level ~/.codex/config.toml
CODEX_DIR="${HOME}/.codex"
mkdir -p "$CODEX_DIR"
USER_TOML="${CODEX_DIR}/config.toml"
backup_file "$USER_TOML"

# Ensure MCP server section exists and points at the detected endpoint (idempotent).
# Always upsert the MCP URL in-place.
# Rationale: older installs wrote /mcp/ but the server defaults to /api/. Re-running this installer
# should fix stale URLs automatically without requiring users to edit config by hand.
_UPDATED_USER_TOML="$(uv run python - "$USER_TOML" "$_URL" "$_MORPH_API_KEY" "$_MORPH_ENABLED_TOOLS" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
url = sys.argv[2]
morph_key = sys.argv[3]
morph_enabled = sys.argv[4]

try:
    text = path.read_text(encoding="utf-8")
except FileNotFoundError:
    text = ""
except Exception:
    text = path.read_text(encoding="utf-8", errors="replace")

lines = text.splitlines(keepends=True)

agent_header_re = re.compile(
    r'^\s*\[mcp_servers(?:\.mcp_agent_mail|\."mcp_agent_mail"|\.\'mcp_agent_mail\'|\.mcp-agent-mail|\."mcp-agent-mail"|\.\'mcp-agent-mail\')\]\s*(?:#.*)?$'
)
morph_env_header_re = re.compile(
    r'^\s*\[mcp_servers(?:\.morph-mcp|\."morph-mcp"|\.\'morph-mcp\'|\.morph_mcp|\."morph_mcp"|\.\'morph_mcp\')\.env\]\s*(?:#.*)?$'
)
morph_header_re = re.compile(
    r'^\s*\[mcp_servers(?:\.morph-mcp|\."morph-mcp"|\.\'morph-mcp\'|\.morph_mcp|\."morph_mcp"|\.\'morph_mcp\')\]\s*(?:#.*)?$'
)
table_header_re = re.compile(r"^\s*\[.*\]\s*(?:#.*)?$")
url_line_re = re.compile(
    r'^(?P<indent>\s*)url\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s#]+)(?P<comment>\s*#.*)?\s*$'
)
command_line_re = re.compile(
    r'^(?P<indent>\s*)command\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s#]+)(?P<comment>\s*#.*)?\s*$'
)
args_line_re = re.compile(
    r'^(?P<indent>\s*)args\s*=\s*\[[^\]]*\](?P<comment>\s*#.*)?\s*$'
)
enabled_line_re = re.compile(
    r'^(?P<indent>\s*)ENABLED_TOOLS\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s#]+)(?P<comment>\s*#.*)?\s*$'
)
key_line_re = re.compile(
    r'^(?P<indent>\s*)MORPH_API_KEY\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s#]+)(?P<comment>\s*#.*)?\s*$'
)

out: list[str] = []
mode: str | None = None
agent_found = False
morph_found = False
morph_env_found = False
url_written = False
command_written = False
args_written = False
enabled_written = False
key_written = False


def emit_url(indent: str = "", comment: str = "") -> None:
    out.append(f'{indent}url = "{url}"{comment}\n')


def emit_command(indent: str = "", comment: str = "") -> None:
    out.append(f'{indent}command = "npx"{comment}\n')


def emit_args(indent: str = "", comment: str = "") -> None:
    out.append(f'{indent}args = ["-y", "@morphllm/morphmcp"]{comment}\n')


def emit_enabled(indent: str = "", comment: str = "") -> None:
    out.append(f'{indent}ENABLED_TOOLS = "{morph_enabled}"{comment}\n')


def emit_key(indent: str = "", comment: str = "") -> None:
    out.append(f'{indent}MORPH_API_KEY = "{morph_key}"{comment}\n')


def flush_section() -> None:
    global mode

    if mode == "agent" and not url_written:
        emit_url()
    elif mode == "morph" and morph_key:
        if not command_written:
            emit_command()
        if not args_written:
            emit_args()
    elif mode == "morph_env" and morph_key:
        if not enabled_written:
            emit_enabled()
        if not key_written:
            emit_key()

    mode = None


for line in lines:
    if mode and table_header_re.match(line):
        flush_section()

    if morph_env_header_re.match(line):
        mode = "morph_env"
        morph_env_found = True
        enabled_written = False
        key_written = False
        out.append(line if line.endswith("\n") else line + "\n")
        continue

    if morph_header_re.match(line):
        mode = "morph"
        morph_found = True
        command_written = False
        args_written = False
        out.append(line if line.endswith("\n") else line + "\n")
        continue

    if agent_header_re.match(line):
        mode = "agent"
        agent_found = True
        url_written = False
        out.append(line if line.endswith("\n") else line + "\n")
        continue

    if mode == "agent":
        m = url_line_re.match(line.rstrip("\r\n"))
        if m:
            emit_url(indent=m.group("indent") or "", comment=m.group("comment") or "")
            url_written = True
            continue

    if mode == "morph":
        m = command_line_re.match(line.rstrip("\r\n"))
        if m:
            emit_command(indent=m.group("indent") or "", comment=m.group("comment") or "")
            command_written = True
            continue

        m = args_line_re.match(line.rstrip("\r\n"))
        if m:
            emit_args(indent=m.group("indent") or "", comment=m.group("comment") or "")
            args_written = True
            continue

    if mode == "morph_env":
        m = enabled_line_re.match(line.rstrip("\r\n"))
        if m:
            emit_enabled(indent=m.group("indent") or "", comment=m.group("comment") or "")
            enabled_written = True
            continue

        m = key_line_re.match(line.rstrip("\r\n"))
        if m:
            emit_key(indent=m.group("indent") or "", comment=m.group("comment") or "")
            key_written = True
            continue

    out.append(line if line.endswith("\n") else line + "\n")

if mode:
    flush_section()

if not agent_found:
    if out and out[-1].strip():
        out.append("\n")
    out.append("# MCP servers configuration (mcp-agent-mail)\n")
    out.append("[mcp_servers.mcp_agent_mail]\n")
    emit_url()

if morph_key and not morph_found:
    if out and out[-1].strip():
        out.append("\n")
    out.append("# Morph MCP configuration (grep-only to avoid edit-file billing)\n")
    out.append("[mcp_servers.morph-mcp]\n")
    emit_command()
    emit_args()

if morph_key and not morph_env_found:
    if out and out[-1].strip():
        out.append("\n")
    out.append("[mcp_servers.morph-mcp.env]\n")
    emit_enabled()
    emit_key()

sys.stdout.write("".join(out))
PY
)"

# Write atomically so partially-written configs never happen.
write_atomic "$USER_TOML" <<<"$_UPDATED_USER_TOML"

# Also write project-local .codex/config.toml for portability
LOCAL_CODEX_DIR="${TARGET_DIR}/.codex"
mkdir -p "$LOCAL_CODEX_DIR"
LOCAL_TOML="${LOCAL_CODEX_DIR}/config.toml"

# Backup before writing
if [[ -f "$LOCAL_TOML" ]]; then
  backup_file "$LOCAL_TOML"
fi

# Inbox reminders come from .codex/hooks.json (PostToolUse), not from the
# top-level `notify` key: Codex discards notify output (GH #273).
write_atomic "$LOCAL_TOML" <<TOML
# Project-local Codex configuration
# NOTE: Top-level keys must appear BEFORE any [section] headers in TOML

# MCP servers configuration
[mcp_servers.mcp_agent_mail]
url = "${_URL}"
# headers can be added if needed; localhost allowed without Authorization
$(if [[ -n "${_MORPH_API_KEY}" ]]; then cat <<TOMLFRAG

[mcp_servers.morph-mcp]
command = "npx"
args = ["-y", "@morphllm/morphmcp"]

[mcp_servers.morph-mcp.env]
ENABLED_TOOLS = "${_MORPH_ENABLED_TOOLS}"
MORPH_API_KEY = "${_MORPH_API_KEY}"
TOMLFRAG
fi)
TOML
set_secure_file "$LOCAL_TOML" || true

log_ok "==> Done."
if [[ -n "${_AGENT}" ]]; then
  _print "Your agent name is: ${_AGENT}"
fi
_print "Codex CLI should now be configured to use MCP Agent Mail."
if [[ ${_SERVER_AVAILABLE} -eq 0 ]]; then
  _print "Remember to start the server: uv run python -m mcp_agent_mail.cli serve-http"
fi
