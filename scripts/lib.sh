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
  trap 'last=$BASH_COMMAND; log_err "Error on: ${last}"' ERR
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
  mkdir -p "$dir"
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] write ${target}"
    cat >/dev/null # consume stdin
    return 0
  fi
  local tmp
  tmp="${target}.tmp.$$"
  umask 077
  cat >"$tmp"
  mv "$tmp" "$target"
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

set_secure_file() { [[ "${DRY_RUN}" == "1" ]] && { _print "[dry-run] chmod 600 $1"; return 0; }; chmod 600 "$1" 2>/dev/null || true; }
set_secure_exec() { [[ "${DRY_RUN}" == "1" ]] && { _print "[dry-run] chmod 700 $1"; return 0; }; chmod 700 "$1" 2>/dev/null || true; }
set_secure_dir() { [[ "${DRY_RUN}" == "1" ]] && { _print "[dry-run] chmod 700 $1"; return 0; }; chmod 700 "$1" 2>/dev/null || true; }

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
  if [[ ! -f "$file" ]]; then
    return 0  # Nothing to backup
  fi
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] backup ${file}"
    return 0
  fi

  # Create backup directory at project root
  local backup_dir="backup_config_files"
  mkdir -p "$backup_dir"

  # Create unique backup name that encodes path information
  local backup_name
  if [[ "$file" == "$HOME"* ]]; then
    # HOME directory file - use relative path from HOME
    local rel_path="${file#$HOME/}"
    rel_path="${rel_path//\//_}"  # Replace / with _
    backup_name="home_${rel_path}"
  else
    # Non-HOME path (project-local or absolute)
    local sanitized="${file//\//_}"  # Replace / with _
    # Remove leading dots and underscores
    while [[ "$sanitized" == .* ]] || [[ "$sanitized" == _* ]]; do
      sanitized="${sanitized#.}"
      sanitized="${sanitized#_}"
    done
    backup_name="local_${sanitized}"
  fi

  # Create backup with timestamp BEFORE .bak extension
  local timestamp
  timestamp=$(date +%Y%m%d_%H%M%S)
  local backup_path="${backup_dir}/${backup_name}.${timestamp}.bak"

  # Copy with error handling
  if ! cp "$file" "$backup_path"; then
    echo "ERROR: Failed to backup ${file}" >&2
    return 1
  fi

  _print "Backed up ${file} to ${backup_path}"
}

# Update or append env var in .env atomically (backup first)
update_env_var() {
  local key="$1"; local value="$2"; local env_file=".env"
  if [[ "${DRY_RUN}" == "1" ]]; then _print "[dry-run] set ${key} in .env"; return 0; fi
  if [[ -f "$env_file" ]]; then
    backup_file "$env_file"
    if grep -q "^${key}=" "$env_file"; then
      sed -E -i "s/^${key}=.*/${key}=${value}/" "$env_file"
    else
      echo "${key}=${value}" >> "$env_file"
    fi
  else
    umask 077
    echo "${key}=${value}" > "$env_file"
  fi
  set_secure_file "$env_file"
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
  _print "Server starting (logs: ${log_file})"
}


