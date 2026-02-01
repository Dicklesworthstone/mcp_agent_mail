#!/usr/bin/env bash
# Shared helpers for setup/integration scripts
# - Colorized logging (best-effort)
# - Flags parsing: --yes, --dry-run, --quiet, --debug, --regenerate-token, --show-token, --project-dir
# - Dependency checks and traps
# - Atomic writes and JSON validation
# - Readiness polling and secure perms

set -euo pipefail

# Initialize colors if not already defined
init_colors() {
  if [[ -n "${NO_COLOR:-}" ]]; then
    _b=""; _dim=""; _red=""; _grn=""; _ylw=""; _blu=""; _mag=""; _cyn=""; _rst=""
    return
  fi
  if command -v tput >/dev/null 2>&1 && [[ -t 1 ]]; then
    _b=${_b:-$(tput bold)}; _dim=${_dim:-$(tput dim)}; _red=${_red:-$(tput setaf 1)}; _grn=${_grn:-$(tput setaf 2)}; _ylw=${_ylw:-$(tput setaf 3)}; _blu=${_blu:-$(tput setaf 4)}; _mag=${_mag:-$(tput setaf 5)}; _cyn=${_cyn:-$(tput setaf 6)}; _rst=${_rst:-$(tput sgr0)}
  else
    _b=""; _dim=""; _red=""; _grn=""; _ylw=""; _blu=""; _mag=""; _cyn=""; _rst=""
  fi
}

# Basic logging helpers (honor QUIET)
_print() { [[ "${QUIET:-0}" == "1" ]] && return 0; printf "%b\n" "$*"; }
log_step() { _print "${_b}${_cyn}==> ${1}${_rst}"; }
log_ok()   { _print "${_grn}${1}${_rst}"; }
log_warn() { _print "${_ylw}${1}${_rst}"; }
log_err()  { _print "${_red}${1}${_rst}"; }

# Parse common flags; sets globals: AUTO_YES, DRY_RUN, QUIET, DEBUG, REGENERATE_TOKEN, SHOW_TOKEN, PROJECT_DIR
parse_common_flags() {
  AUTO_YES=${AUTO_YES:-0}
  DRY_RUN=${DRY_RUN:-0}
  QUIET=${QUIET:-0}
  DEBUG=${DEBUG:-0}
  REGENERATE_TOKEN=${REGENERATE_TOKEN:-0}
  SHOW_TOKEN=${SHOW_TOKEN:-0}
  PROJECT_DIR=${PROJECT_DIR:-}
  local -a args=("$@");
  for ((i=0; i<${#args[@]}; i++)); do
    a="${args[$i]}"
    case "$a" in
      --yes) AUTO_YES=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --quiet) QUIET=1 ;;
      --debug) DEBUG=1 ;;
      --regenerate-token) REGENERATE_TOKEN=1 ;;
      --show-token) SHOW_TOKEN=1 ;;
      --project-dir) i=$((i+1)); PROJECT_DIR="${args[$i]:-}" ;;
      --project-dir=*) PROJECT_DIR="${a#*=}" ;;
    esac
  done
  export AUTO_YES DRY_RUN QUIET DEBUG REGENERATE_TOKEN SHOW_TOKEN PROJECT_DIR
  if [[ "${DEBUG}" == "1" ]]; then set -x; fi
}

# Traps and diagnostics
setup_traps() {
  if [[ "${DEBUG:-0}" == "1" ]]; then
    set -o errtrace
    trap 'last=$BASH_COMMAND; log_err "Error on: ${last}"' ERR
  fi
}

# Dependency checks
require_cmd() {
  local cmd="$1"; shift || true
  command -v "$cmd" >/dev/null 2>&1 || { log_err "Missing dependency: $cmd"; exit 1; }
}

# Atomic write: read content from stdin and atomically move to target
write_atomic() {
  local target="$1"; shift || true
  local dir; dir=$(dirname "$target")

  # Create directory with error checking
  if ! mkdir -p "$dir"; then
    echo "ERROR: Failed to create directory ${dir}" >&2
    return 1
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] write ${target}"
    cat >/dev/null # consume stdin
    return 0
  fi

  local tmp
  tmp="${target}.tmp.$$"

  # Set up cleanup trap for temp file (double quotes = expand now, not at trap execution)
  trap "rm -f \"$tmp\" 2>/dev/null" EXIT INT TERM

  # Create temp file with secure permissions (600)
  umask 077
  if ! cat >"$tmp"; then
    echo "ERROR: Failed to write temp file ${tmp}" >&2
    rm -f "$tmp" 2>/dev/null
    trap - EXIT INT TERM
    return 1
  fi

  # Atomic move
  if ! mv "$tmp" "$target"; then
    echo "ERROR: Failed to move ${tmp} to ${target}" >&2
    rm -f "$tmp" 2>/dev/null
    trap - EXIT INT TERM
    return 1
  fi

  # Clear trap after successful completion
  trap - EXIT INT TERM
}

# JSON validate via jq or Python
json_validate() {
  local file="$1"
  if command -v jq >/dev/null 2>&1; then
    jq empty "$file" >/dev/null 2>&1 || { log_err "Invalid JSON: $file"; return 1; }
  else
    if command -v python >/dev/null 2>&1; then
      python -c 'import json,sys; json.load(open(sys.argv[1],"r",encoding="utf-8"))' "$file" >/dev/null 2>&1 || { log_err "Invalid JSON: $file"; return 1; }
    else
      uv run python -c 'import json,sys; json.load(open(sys.argv[1],"r",encoding="utf-8"))' "$file" >/dev/null 2>&1 || { log_err "Invalid JSON: $file"; return 1; }
    fi
  fi
}

# Escape a string for safe embedding in JSON
# Usage: escaped=$(json_escape_string "$raw_string") || exit 1
# Returns: JSON-escaped string WITH quotes (e.g., "value")
# Exits with error if escaping fails
json_escape_string() {
  local raw="$1"
  local result

  if command -v jq >/dev/null 2>&1; then
    # Use jq for proper JSON escaping
    if ! result=$(jq -n --arg str "$raw" '$str' 2>&1); then
      echo "ERROR: jq failed to escape JSON string" >&2
      return 1
    fi
  elif command -v python >/dev/null 2>&1; then
    if ! result=$(python -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$raw" 2>&1); then
      echo "ERROR: python failed to escape JSON string" >&2
      return 1
    fi
  elif command -v uv >/dev/null 2>&1; then
    if ! result=$(uv run python -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$raw" 2>&1); then
      echo "ERROR: uv python failed to escape JSON string" >&2
      return 1
    fi
  else
    echo "ERROR: No JSON escaping tool available (need jq, python, or uv)" >&2
    return 1
  fi

  # Validate result is non-empty
  if [[ -z "$result" ]]; then
    echo "ERROR: JSON escaping produced empty result" >&2
    return 1
  fi

  echo "$result"
}

# Merge MCP server config into existing settings JSON
# Usage: json_merge_mcp_server <existing_json> <server_name> <server_config_json>
# Returns: merged JSON preserving all existing keys
# Requires: jq (for safe JSON handling without quote injection vulnerabilities)
json_merge_mcp_server() {
  local existing="$1"
  local server_name="$2"
  local server_config="$3"

  if ! command -v jq >/dev/null 2>&1; then
    log_err "jq is required for JSON merge. Install: brew install jq (macOS) or apt install jq (Linux)"
    return 1
  fi

  echo "$existing" | jq --arg name "$server_name" --argjson config "$server_config" \
    '.mcpServers = (.mcpServers // {}) | .mcpServers[$name] = $config'
}

# Append hook to existing hooks array without duplicating
# Usage: json_append_hook <existing_json> <hook_type> <hook_json> <identifier>
# hook_type: SessionStart, PreToolUse, PostToolUse
# identifier: string to check for duplicates (e.g., "mcp-agent-mail")
# Requires: jq (for safe JSON handling without quote injection vulnerabilities)
json_append_hook() {
  local existing="$1"
  local hook_type="$2"
  local hook_json="$3"
  local identifier="$4"

  if ! command -v jq >/dev/null 2>&1; then
    log_err "jq is required for JSON merge. Install: brew install jq (macOS) or apt install jq (Linux)"
    return 1
  fi

  # Check if already exists (use --arg to safely pass identifier)
  if echo "$existing" | jq -e --arg id "$identifier" ".hooks.${hook_type}[]? | .hooks[]? | .command | contains(\$id)" >/dev/null 2>&1; then
    # Already exists, return unchanged
    echo "$existing"
    return
  fi
  # Append new hook
  echo "$existing" | jq --argjson hook "$hook_json" \
    ".hooks = (.hooks // {}) | .hooks.${hook_type} = ((.hooks.${hook_type} // []) + [\$hook])"
}

# Ensure settings.local.json is in .gitignore
# Usage: ensure_gitignore_entry <gitignore_path> <pattern>
ensure_gitignore_entry() {
  local gitignore="$1"
  local pattern="$2"

  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] ensure ${pattern} in ${gitignore}"
    return 0
  fi

  if [[ -f "$gitignore" ]]; then
    if ! grep -qF "$pattern" "$gitignore" 2>/dev/null; then
      echo "" >> "$gitignore"
      echo "# Claude Code local settings (contains secrets)" >> "$gitignore"
      echo "$pattern" >> "$gitignore"
      log_ok "Added ${pattern} to .gitignore"
    fi
  else
    cat > "$gitignore" <<EOF
# Claude Code local settings (contains secrets)
${pattern}
EOF
    log_ok "Created .gitignore with ${pattern}"
  fi
}

# Set file permissions to 600 with error checking
set_secure_file() {
  local file="$1"
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] chmod 600 ${file}"
    return 0
  fi
  if [[ ! -e "$file" ]]; then
    log_warn "Cannot chmod: file does not exist: ${file}"
    return 1
  fi
  if ! chmod 600 "$file" 2>/dev/null; then
    log_warn "Failed to chmod 600 ${file} (permissions/readonly filesystem?)"
    return 1
  fi
}

# Set file permissions to 700 (executable) with error checking
set_secure_exec() {
  local file="$1"
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] chmod 700 ${file}"
    return 0
  fi
  if [[ ! -e "$file" ]]; then
    log_warn "Cannot chmod: file does not exist: ${file}"
    return 1
  fi
  if ! chmod 700 "$file" 2>/dev/null; then
    log_warn "Failed to chmod 700 ${file} (permissions/readonly filesystem?)"
    return 1
  fi
}

# Set directory permissions to 700 with error checking
set_secure_dir() {
  local dir="$1"
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] chmod 700 ${dir}"
    return 0
  fi
  if [[ ! -d "$dir" ]]; then
    log_warn "Cannot chmod: directory does not exist: ${dir}"
    return 1
  fi
  if ! chmod 700 "$dir" 2>/dev/null; then
    log_warn "Failed to chmod 700 ${dir} (permissions/readonly filesystem?)"
    return 1
  fi
}

# Readiness polling: host, port, path, tries, delay_seconds
readiness_poll() {
  local host="$1"; local port="$2"; local path="$3"; local tries="$4"; local delay="$5"
  local url="http://${host}:${port}${path}"
  local n
  for ((n=0; n<tries; n++)); do
    if curl -fsS --connect-timeout 1 --max-time 2 --retry 0 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

# Run command honoring DRY_RUN
run_cmd() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] $*"
    return 0
  fi
  "$@"
}

# Backup a file to backup_config_files/ with timestamp before .bak extension
# Usage: backup_file "/path/to/file"
#
# Creates distinguishable backup names for files from different locations:
#   - HOME files: home_.claude_settings.json.TIMESTAMP.bak
#   - Project files: local_claude_settings.json.TIMESTAMP.bak
backup_file() {
  local file="$1"

  # Validate input
  if [[ -z "$file" ]]; then
    echo "ERROR: backup_file called with empty file path" >&2
    return 1
  fi

  if [[ ! -f "$file" ]]; then
    return 0  # Nothing to backup
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] backup ${file}"
    return 0
  fi

  # Create backup directory at project root
  local backup_dir="backup_config_files"
  if ! mkdir -p "$backup_dir"; then
    echo "ERROR: Failed to create backup directory ${backup_dir}" >&2
    return 1
  fi

  # Create unique backup name that encodes path information
  # Sanitize glob metacharacters to prevent find pattern matching issues
  local backup_name
  # Check if file is under HOME (require trailing slash to avoid false prefix matches)
  # Also validate HOME is non-empty to avoid matching everything
  if [[ -n "$HOME" && "$file" == "$HOME/"* ]]; then
    # HOME directory file - use relative path from HOME
    local rel_path="${file#$HOME/}"
    rel_path="${rel_path//\//_}"  # Replace / with _
    # Sanitize glob metacharacters: * ? [ ] { }
    rel_path="${rel_path//\*/STAR}"
    rel_path="${rel_path//\?/QMARK}"
    rel_path="${rel_path//\[/LBRACK}"
    rel_path="${rel_path//\]/RBRACK}"
    rel_path="${rel_path//\{/LBRACE}"
    rel_path="${rel_path//\}/RBRACE}"
    backup_name="home_${rel_path}"
  else
    # Non-HOME path (project-local or absolute)
    local sanitized="${file//\//_}"  # Replace / with _
    # Sanitize glob metacharacters: * ? [ ] { }
    sanitized="${sanitized//\*/STAR}"
    sanitized="${sanitized//\?/QMARK}"
    sanitized="${sanitized//\[/LBRACK}"
    sanitized="${sanitized//\]/RBRACK}"
    sanitized="${sanitized//\{/LBRACE}"
    sanitized="${sanitized//\}/RBRACE}"
    # Remove leading dots and underscores
    while [[ "$sanitized" == .* ]] || [[ "$sanitized" == _* ]]; do
      sanitized="${sanitized#.}"
      sanitized="${sanitized#_}"
    done
    backup_name="local_${sanitized}"
  fi

  # Create backup with timestamp (nanoseconds for uniqueness) BEFORE .bak extension
  local timestamp
  timestamp=$(date +%Y%m%d_%H%M%S_%N)
  local backup_path="${backup_dir}/${backup_name}.${timestamp}.bak"

  # Copy with error handling
  if ! cp "$file" "$backup_path"; then
    echo "ERROR: Failed to backup ${file}" >&2
    return 1
  fi

  _print "Backed up ${file} to ${backup_path}"

  # Cleanup old backups (keep last 10 for this file pattern)
  cleanup_old_backups "$backup_dir" "$backup_name" 10
}

# Cleanup old backup files, keeping only the most recent N
# Usage: cleanup_old_backups <backup_dir> <backup_pattern> <keep_count>
cleanup_old_backups() {
  local backup_dir="$1"
  local backup_pattern="$2"
  local keep_count="${3:-10}"

  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] cleanup old backups matching ${backup_pattern}"
    return 0
  fi

  # Find all backups matching this pattern, sort by timestamp (newest first), delete old ones
  # Pattern: ${backup_pattern}.TIMESTAMP.bak
  local old_backups
  old_backups=$(find "$backup_dir" -maxdepth 1 -name "${backup_pattern}.*.bak" -type f 2>/dev/null | sort -r | tail -n +$((keep_count + 1)))

  if [[ -n "$old_backups" ]]; then
    while IFS= read -r old_backup; do
      rm -f "$old_backup" 2>/dev/null && _print "Removed old backup: ${old_backup}"
    done <<< "$old_backups"
  fi
}

# Update or append env var in .env atomically (backup first)
update_env_var() {
  local key="$1"; local value="$2"; local env_file=".env"
  if [[ "${DRY_RUN}" == "1" ]]; then _print "[dry-run] set ${key} in .env"; return 0; fi

  local tmp="${env_file}.tmp.$$"
  # Double quotes = expand now, not at trap execution
  trap "rm -f \"$tmp\" 2>/dev/null" EXIT INT TERM

  if [[ -f "$env_file" ]]; then
    backup_file "$env_file"

    # Use atomic write: read old file, modify, write to temp, move
    if grep -q "^${key}=" "$env_file"; then
      # Replace existing key
      umask 077
      if ! sed -E "s/^${key}=.*/${key}=${value}/" "$env_file" > "$tmp"; then
        rm -f "$tmp" 2>/dev/null
        trap - EXIT INT TERM
        echo "ERROR: Failed to update ${key} in ${env_file}" >&2
        return 1
      fi
    else
      # Append new key
      umask 077
      if ! { cat "$env_file"; echo "${key}=${value}"; } > "$tmp"; then
        rm -f "$tmp" 2>/dev/null
        trap - EXIT INT TERM
        echo "ERROR: Failed to append ${key} to ${env_file}" >&2
        return 1
      fi
    fi

    # Atomic move
    if ! mv "$tmp" "$env_file"; then
      rm -f "$tmp" 2>/dev/null
      trap - EXIT INT TERM
      echo "ERROR: Failed to move temp file to ${env_file}" >&2
      return 1
    fi
  else
    # Create new file
    umask 077
    if ! echo "${key}=${value}" > "$tmp"; then
      rm -f "$tmp" 2>/dev/null
      trap - EXIT INT TERM
      echo "ERROR: Failed to create ${env_file}" >&2
      return 1
    fi

    if ! mv "$tmp" "$env_file"; then
      rm -f "$tmp" 2>/dev/null
      trap - EXIT INT TERM
      echo "ERROR: Failed to move temp file to ${env_file}" >&2
      return 1
    fi
  fi

  trap - EXIT INT TERM
  # Bug #5 fix: set_secure_file logs its own warning, no need to duplicate
  set_secure_file "$env_file" || true
}

# Confirmation prompt honoring AUTO_YES and TTY; usage: confirm "Message?" || exit 1
confirm() {
  local msg="$1"
  if [[ "${AUTO_YES}" == "1" ]]; then return 0; fi
  if [[ ! -t 0 ]]; then return 1; fi
  read -r -p "${msg} [y/N] " _ans || return 1
  [[ "${_ans}" == "y" || "${_ans}" == "Y" ]]
}

# Return a space-separated list of PIDs listening on a TCP port (best-effort)
find_listening_pids_for_port() {
  local port="$1"
  local pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids=$(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ')
  elif command -v fuser >/dev/null 2>&1; then
    # fuser prints like: 8765/tcp: 1234 2345
    pids=$(fuser -n tcp "${port}" 2>/dev/null | sed -E 's/.*: *//' | tr ' ' '\n' | tr '\n' ' ')
  elif command -v ss >/dev/null 2>&1; then
    # ss -ltnp output includes users:(("python",pid=1234,fd=...))
    pids=$(ss -ltnp 2>/dev/null | awk -v p=":${port}" '$4 ~ p {print $0}' | sed -nE 's/.*pid=([0-9]+).*/\1/p' | tr '\n' ' ')
  fi
  echo "${pids}" | xargs -n1 echo | awk 'NF' | sort -u | tr '\n' ' '
}

# Gracefully kill a list of PIDs owned by current user; escalate to KILL after timeout
kill_pids_graceful() {
  local timeout_s="${1:-5}"; shift || true
  local pids=("$@")
  [[ ${#pids[@]} -eq 0 ]] && return 0
  local me; me=$(id -un)
  local to_kill=()
  local pid
  for pid in "${pids[@]}"; do
    [[ -z "$pid" ]] && continue
    local owner
    owner=$(ps -o user= -p "$pid" 2>/dev/null | awk '{print $1}')
    if [[ "$owner" == "$me" ]]; then
      to_kill+=("$pid")
    else
      log_warn "Skipping PID $pid owned by $owner"
    fi
  done
  [[ ${#to_kill[@]} -eq 0 ]] && return 0
  if [[ "${DRY_RUN}" == "1" ]]; then _print "[dry-run] kill -TERM ${to_kill[*]}"; return 0; fi
  kill -TERM "${to_kill[@]}" 2>/dev/null || true
  local end=$(( $(date +%s) + timeout_s ))
  while :; do
    local alive=()
    for pid in "${to_kill[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then alive+=("$pid"); fi
    done
    [[ ${#alive[@]} -eq 0 ]] && break
    if (( $(date +%s) >= end )); then
      log_warn "Escalating to SIGKILL for: ${alive[*]}"
      kill -KILL "${alive[@]}" 2>/dev/null || true
      break
    fi
    sleep 0.2
  done
}

# Start server in background using run helper; log to logs directory
start_server_background() {
  local helper="scripts/run_server_with_token.sh"
  local stamp
  stamp=$(date +%Y%m%d_%H%M%S)
  mkdir -p logs
  local log_file="logs/server_${stamp}.log"
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] ${helper} > ${log_file} 2>&1 &"
    return 0
  fi
  if [[ -x "$helper" ]]; then
    nohup "$helper" >"$log_file" 2>&1 &
  else
    nohup uv run python -m mcp_agent_mail.cli serve-http >"$log_file" 2>&1 &
  fi
  # Export PID so caller can kill it later
  export _BACKGROUND_SERVER_PID=$!
  _print "Server starting (PID: ${_BACKGROUND_SERVER_PID}, logs: ${log_file})"
}

# Stop background server started by start_server_background
stop_background_server() {
  if [[ -n "${_BACKGROUND_SERVER_PID:-}" ]]; then
    if kill -0 "${_BACKGROUND_SERVER_PID}" 2>/dev/null; then
      _print "Stopping background server (PID: ${_BACKGROUND_SERVER_PID})"
      kill -TERM "${_BACKGROUND_SERVER_PID}" 2>/dev/null || true
      # Wait briefly for graceful shutdown
      local waited=0
      while kill -0 "${_BACKGROUND_SERVER_PID}" 2>/dev/null && [[ $waited -lt 5 ]]; do
        sleep 0.5
        waited=$((waited + 1))
      done
      # Force kill if still running
      if kill -0 "${_BACKGROUND_SERVER_PID}" 2>/dev/null; then
        kill -9 "${_BACKGROUND_SERVER_PID}" 2>/dev/null || true
      fi
    fi
    unset _BACKGROUND_SERVER_PID
  fi
}

# Cross-platform helper to kill processes on a TCP port (works on Linux and macOS)
# Usage: kill_port_processes <port>
# Only kills processes owned by current user for safety
# shellcheck disable=SC2015  # A && B || C pattern is intentional here
kill_port_processes() {
  local port="$1"
  local current_user
  current_user=$(id -un)
  local killed=0

  # Try lsof first (available on macOS and most Linux)
  if command -v lsof >/dev/null 2>&1; then
    local pids
    pids=$(lsof -t -i :"${port}" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
      for pid in $pids; do
        local owner
        owner=$(ps -o user= -p "$pid" 2>/dev/null | awk '{print $1}') || owner=""
        if [[ "$owner" == "$current_user" ]]; then
          # First try SIGTERM for graceful shutdown
          kill -TERM "$pid" 2>/dev/null && killed=1 || true
        elif [[ -n "$owner" ]]; then
          log_warn "Port ${port} in use by PID $pid owned by $owner; skipping"
        fi
        # If owner is empty, process likely already exited
      done
    fi
  # Fall back to fuser (typically Linux)
  elif command -v fuser >/dev/null 2>&1; then
    local pids
    pids=$(fuser -n tcp "${port}" 2>/dev/null | sed -E 's/.*: *//' | tr ' ' '\n' | awk 'NF') || pids=""
    if [[ -n "$pids" ]]; then
      for pid in $pids; do
        local owner
        owner=$(ps -o user= -p "$pid" 2>/dev/null | awk '{print $1}') || owner=""
        if [[ "$owner" == "$current_user" ]]; then
          kill -TERM "$pid" 2>/dev/null && killed=1 || true
        elif [[ -n "$owner" ]]; then
          log_warn "Port ${port} in use by PID $pid owned by $owner; skipping"
        fi
      done
    fi
  fi

  # Give processes time to exit cleanly
  if [[ $killed -eq 1 ]]; then
    sleep 0.5
    # Check if any are still running and force-kill
    if command -v lsof >/dev/null 2>&1; then
      local remaining
      remaining=$(lsof -t -i :"${port}" 2>/dev/null || true)
      if [[ -n "$remaining" ]]; then
        for pid in $remaining; do
          local owner
          owner=$(ps -o user= -p "$pid" 2>/dev/null | awk '{print $1}') || owner=""
          if [[ "$owner" == "$current_user" ]]; then
            kill -9 "$pid" 2>/dev/null || true
          fi
        done
        sleep 0.3
      fi
    fi
  fi
}


