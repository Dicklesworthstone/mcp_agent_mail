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
    python -c 'import json,sys; json.load(open(sys.argv[1],"r",encoding="utf-8"))' "$file" >/dev/null 2>&1 || { log_err "Invalid JSON: $file"; return 1; }
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

# Update or append env var in .env atomically (backup first)
update_env_var() {
  local key="$1"; local value="$2"; local env_file=".env"
  if [[ "${DRY_RUN}" == "1" ]]; then _print "[dry-run] set ${key} in .env"; return 0; fi
  if [[ -f "$env_file" ]]; then
    cp "$env_file" "${env_file}.bak.$(date +%s)"
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


