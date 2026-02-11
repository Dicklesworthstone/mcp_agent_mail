#!/usr/bin/env bash
set -euo pipefail

# MCP Agent Mail — TL;DR installer
# - Installs uv (if missing)
# - Sets up Python 3.14 venv with uv
# - Syncs dependencies
# - Runs auto-detect integration and starts the HTTP server on port 8765
#
# Usage examples:
#   ./scripts/install.sh --yes
#   ./scripts/install.sh --dir "$HOME/mcp_agent_mail" --yes
#   curl -fsSL https://raw.githubusercontent.com/Dicklesworthstone/mcp_agent_mail/main/scripts/install.sh | bash -s -- --yes

REPO_URL="https://github.com/Dicklesworthstone/mcp_agent_mail"
REPO_NAME="mcp_agent_mail"
BRANCH="main"
DEFAULT_CLONE_DIR="$PWD/${REPO_NAME}"
CLONE_DIR=""
YES=0
NO_START=0
START_ONLY=0
PROJECT_DIR=""
INTEGRATION_TOKEN="${INTEGRATION_BEARER_TOKEN:-}"
HTTP_PORT_OVERRIDE=""
SKIP_BEADS=0
SKIP_BV=0
BEADS_INSTALL_URL="https://raw.githubusercontent.com/Dicklesworthstone/beads_rust/main/install.sh"
BV_INSTALL_URL="https://raw.githubusercontent.com/Dicklesworthstone/beads_viewer/main/install.sh"
SUMMARY_LINES=()
LAST_BR_VERSION=""
LAST_BV_VERSION=""

usage() {
  cat <<EOF
MCP Agent Mail installer

Options:
  --dir DIR              Clone/use repo at DIR (default: ./mcp_agent_mail)
  --branch NAME          Git branch to clone (default: main)
  --port PORT            HTTP server port (default: 8765); sets HTTP_PORT in .env
  --skip-beads           Do not install the Beads Rust (br) CLI automatically
  --skip-bv              Do not install the Beads Viewer (bv) TUI automatically
  -y, --yes              Non-interactive; assume Yes where applicable
  --no-start             Do not run integration/start; just set up venv + deps
  --start-only           Skip clone/setup; run integration/start in current repo
  --project-dir PATH     Pass-through to integration (where to write client configs)
  --token HEX            Use/set INTEGRATION_BEARER_TOKEN for this run
  -h, --help             Show help

Examples:
  ./scripts/install.sh --yes
  ./scripts/install.sh --port 9000 --yes
  ./scripts/install.sh --dir "\$HOME/mcp_agent_mail" --yes
  curl -fsSL https://raw.githubusercontent.com/Dicklesworthstone/mcp_agent_mail/main/scripts/install.sh | bash -s -- --yes
  curl -fsSL https://raw.githubusercontent.com/Dicklesworthstone/mcp_agent_mail/main/scripts/install.sh | bash -s -- --port 9000 --yes
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) shift; CLONE_DIR="${1:-}" ;;
    --dir=*) CLONE_DIR="${1#*=}" ;;
    --branch) shift; BRANCH="${1:-}" ;;
    --branch=*) BRANCH="${1#*=}" ;;
    --port) shift; HTTP_PORT_OVERRIDE="${1:-}" ;;
    --port=*) HTTP_PORT_OVERRIDE="${1#*=}" ;;
    -y|--yes) YES=1 ;;
    --no-start) NO_START=1 ;;
    --start-only) START_ONLY=1 ;;
    --project-dir) shift; PROJECT_DIR="${1:-}" ;;
    --project-dir=*) PROJECT_DIR="${1#*=}" ;;
    --token) shift; INTEGRATION_TOKEN="${1:-}" ;;
    --token=*) INTEGRATION_TOKEN="${1#*=}" ;;
    --skip-beads) SKIP_BEADS=1 ;;
    --skip-bv) SKIP_BV=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift || true
done

# Define logging helpers early so they're available for validation
info() { printf "\033[1;36m[INFO]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[ OK ]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ERR ]\033[0m %s\n" "$*"; }

# Validate port if provided
if [[ -n "${HTTP_PORT_OVERRIDE}" ]]; then
  if ! [[ "${HTTP_PORT_OVERRIDE}" =~ ^[0-9]+$ ]]; then
    err "Port must be a number (got: ${HTTP_PORT_OVERRIDE})"
    exit 1
  fi
  if [[ "${HTTP_PORT_OVERRIDE}" -lt 1 || "${HTTP_PORT_OVERRIDE}" -gt 65535 ]]; then
    err "Port must be between 1 and 65535 (got: ${HTTP_PORT_OVERRIDE})"
    exit 1
  fi
fi

need_cmd() { command -v "$1" >/dev/null 2>&1 || return 1; }

record_summary() {
  SUMMARY_LINES+=("$1")
}

print_summary() {
  if [[ ${#SUMMARY_LINES[@]} -eq 0 ]]; then
    return 0
  fi
  echo
  info "Installation summary"
  local line
  for line in "${SUMMARY_LINES[@]}"; do
    echo "  - ${line}"
  done
}

find_br_binary() {
  if command -v br >/dev/null 2>&1; then
    command -v br
    return 0
  fi

  local candidates=("${HOME}/.local/bin/br" "${HOME}/bin/br" "/usr/local/bin/br")
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  return 1
}

maybe_add_br_path() {
  local binary_path="$1"
  local dir
  dir=$(dirname "${binary_path}")
  if [[ ":${PATH}:" != *":${dir}:"* ]]; then
    export PATH="${dir}:${PATH}"
    ok "Temporarily added ${dir} to PATH so this session can invoke 'br' immediately"
  fi
}

rewrite_path_snippet() {
  local rc_file="$1"
  local marker="$2"
  local end_marker="$3"
  local snippet="$4"

  local tmp
  tmp=$(mktemp "${rc_file}.XXXXXX") || return 1

  local in_block=0
  local replaced=0
  local line

  while IFS='' read -r line || [[ -n "${line}" ]]; do
    if [[ "${in_block}" -eq 0 && "${line}" == "${marker}" ]]; then
      printf '%s' "${snippet}" >> "${tmp}"
      in_block=1
      replaced=1
      continue
    fi

    if [[ "${in_block}" -eq 1 ]]; then
      if [[ "${line}" == "${end_marker}" ]]; then
        in_block=0
      fi
      continue
    fi

    printf '%s\n' "${line}" >> "${tmp}"
  done < "${rc_file}"

  if [[ "${in_block}" -eq 1 ]]; then
    rm -f "${tmp}"
    return 1
  fi

  if [[ "${replaced}" -eq 0 ]]; then
    rm -f "${tmp}"
    return 1
  fi

  if ! mv "${tmp}" "${rc_file}"; then
    rm -f "${tmp}"
    return 1
  fi

  return 0
}

append_path_snippet() {
  local dir="$1"
  local rc_file="$2"
  if [[ -z "${rc_file}" ]]; then
    return 1
  fi

  local marker="# >>> MCP Agent Mail br path ${dir}"
  local end_marker="# <<< MCP Agent Mail br path"
  local snippet=""
  printf -v snippet '%s\nif [[ ":$PATH:" != *":%s:"* ]]; then\n  export PATH="%s:$PATH"\nfi\n%s\n' \
    "${marker}" "${dir}" "${dir}" "${end_marker}"

  if [[ -f "${rc_file}" ]] && grep -Fq "${marker}" "${rc_file}"; then
    if rewrite_path_snippet "${rc_file}" "${marker}" "${end_marker}" "${snippet}"; then
      ok "Updated ${dir} PATH snippet via ${rc_file}"
      return 0
    fi
    warn "Existing PATH snippet in ${rc_file} could not be updated automatically"
    return 1
  fi

  if ! touch "${rc_file}" >/dev/null 2>&1; then
    return 1
  fi

  {
    printf '\n%s' "${snippet}"
  } >> "${rc_file}"

  ok "Added ${dir} to PATH via ${rc_file}"
  return 0
}

persist_br_path() {
  local binary_path="$1"
  local dir
  dir=$(dirname "${binary_path}")

  local shell_name=""
  if [[ -n "${SHELL:-}" ]]; then
    shell_name=$(basename "${SHELL}")
  fi

  local -a rc_candidates=()
  if [[ "${shell_name}" == "zsh" ]]; then
    rc_candidates+=("~/.zshrc")
  elif [[ "${shell_name}" == "bash" ]]; then
    rc_candidates+=("~/.bashrc")
  fi
  rc_candidates+=("~/.bashrc" "~/.zshrc" "~/.profile")

  local appended=0
  local seen_rc=""
  local rc
  for rc in "${rc_candidates[@]}"; do
    [[ -n "${rc}" ]] || continue
    local rc_path
    rc_path="${rc/#~/${HOME}}"
    if [[ -z "${rc_path}" ]]; then
      continue
    fi
    # Check if we've already seen this path (Bash 3.2 compatible string matching)
    if [[ ":${seen_rc}:" == *":${rc_path}:"* ]]; then
      continue
    fi
    seen_rc="${seen_rc}:${rc_path}"
    if append_path_snippet "${dir}" "${rc_path}"; then
      appended=1
      break
    fi
  done

  if [[ "${appended}" -eq 0 ]]; then
    warn "Could not persist PATH update automatically; ensure ${dir} is in your PATH."
  fi
}

ensure_br_path_ready() {
  local binary_path="$1"
  maybe_add_br_path "${binary_path}"
  persist_br_path "${binary_path}"
}

install_bd_alias() {
  # Install 'bd' alias pointing to 'br' for backwards compatibility
  local shell_name=""
  if [[ -n "${SHELL:-}" ]]; then
    shell_name=$(basename "${SHELL}")
  fi

  # Always check for and remove any old bd binary that might shadow the alias
  # This runs every time to catch cases where bd was reinstalled after a previous run
  local old_bd_paths=("${HOME}/.local/bin/bd" "${HOME}/bin/bd" "/usr/local/bin/bd")
  for old_bd_path in "${old_bd_paths[@]}"; do
    if [[ -x "${old_bd_path}" ]] && [[ ! -L "${old_bd_path}" ]]; then
      # It's an executable file (not a symlink), rename it
      info "Found old bd binary at ${old_bd_path}, renaming to bd.old"
      mv "${old_bd_path}" "${old_bd_path}.old" 2>/dev/null || warn "Could not rename old bd binary at ${old_bd_path}"
    fi
  done

  # Determine target RC file based on shell
  local rc_file=""
  if [[ "${shell_name}" == "zsh" ]]; then
    rc_file="${HOME}/.zshrc"
  elif [[ "${shell_name}" == "bash" ]]; then
    rc_file="${HOME}/.bashrc"
  else
    # Fallback: try zshrc first (common on macOS), then bashrc
    if [[ -f "${HOME}/.zshrc" ]]; then
      rc_file="${HOME}/.zshrc"
    elif [[ -f "${HOME}/.bashrc" ]]; then
      rc_file="${HOME}/.bashrc"
    else
      warn "Could not determine shell RC file for 'bd' alias"
      record_summary "Alias 'bd -> br': skipped (no shell RC file found)"
      return 1
    fi
  fi

  local marker="# >>> MCP Agent Mail bd alias"
  local end_marker="# <<< MCP Agent Mail bd alias"
  local alias_cmd="alias bd='br'"
  local snippet=""
  printf -v snippet '%s\n%s\n%s\n' "${marker}" "${alias_cmd}" "${end_marker}"

  # Check if marker already exists
  if [[ -f "${rc_file}" ]] && grep -Fq "${marker}" "${rc_file}"; then
    # Update existing snippet
    if rewrite_path_snippet "${rc_file}" "${marker}" "${end_marker}" "${snippet}"; then
      ok "Updated 'bd' alias in ${rc_file}"
      record_summary "Alias 'bd -> br': updated in ${rc_file}"
      # Also define it for the current session
      alias bd='br' 2>/dev/null || true
      return 0
    fi
    warn "Existing 'bd' alias in ${rc_file} could not be updated automatically"
    record_summary "Alias 'bd -> br': update failed in ${rc_file}"
    return 1
  fi

  # Check if user has a different 'bd' alias already (without our markers)
  if [[ -f "${rc_file}" ]] && grep -q "^alias bd=" "${rc_file}"; then
    warn "An existing 'bd' alias was found in ${rc_file}; skipping to avoid conflict"
    record_summary "Alias 'bd -> br': skipped (existing alias found)"
    return 0
  fi

  # Append new snippet
  if ! touch "${rc_file}" >/dev/null 2>&1; then
    warn "Could not write to ${rc_file}"
    record_summary "Alias 'bd -> br': failed (cannot write to ${rc_file})"
    return 1
  fi

  {
    printf '\n%s' "${snippet}"
  } >> "${rc_file}"

  ok "Added 'bd' alias (bd -> br) to ${rc_file}"
  record_summary "Alias 'bd -> br': added to ${rc_file}"

  # Also define it for the current session
  alias bd='br' 2>/dev/null || true

  return 0
}

install_am_alias() {
  # Install 'am' alias to quickly start the MCP Agent Mail server
  local repo_dir="$1"

  local shell_name=""
  if [[ -n "${SHELL:-}" ]]; then
    shell_name=$(basename "${SHELL}")
  fi

  # Determine target RC file based on shell
  local rc_file=""
  if [[ "${shell_name}" == "zsh" ]]; then
    rc_file="${HOME}/.zshrc"
  elif [[ "${shell_name}" == "bash" ]]; then
    rc_file="${HOME}/.bashrc"
  else
    # Fallback: try zshrc first (common on macOS), then bashrc
    if [[ -f "${HOME}/.zshrc" ]]; then
      rc_file="${HOME}/.zshrc"
    elif [[ -f "${HOME}/.bashrc" ]]; then
      rc_file="${HOME}/.bashrc"
    else
      warn "Could not determine shell RC file for 'am' alias"
      return 1
    fi
  fi

  local marker="# >>> MCP Agent Mail alias"
  local end_marker="# <<< MCP Agent Mail alias"
  local alias_cmd="alias am='cd \"${repo_dir}\" && scripts/run_server_with_token.sh'"
  local snippet=""
  printf -v snippet '%s\n%s\n%s\n' "${marker}" "${alias_cmd}" "${end_marker}"

  # Check if marker already exists
  if [[ -f "${rc_file}" ]] && grep -Fq "${marker}" "${rc_file}"; then
    # Update existing snippet
    if rewrite_path_snippet "${rc_file}" "${marker}" "${end_marker}" "${snippet}"; then
      ok "Updated 'am' alias in ${rc_file}"
      record_summary "Alias 'am': updated in ${rc_file}"
      return 0
    fi
    warn "Existing 'am' alias in ${rc_file} could not be updated automatically"
    return 1
  fi

  # Check if user has a different 'am' alias already
  if [[ -f "${rc_file}" ]] && grep -q "^alias am=" "${rc_file}"; then
    warn "An existing 'am' alias was found in ${rc_file}; skipping to avoid conflict"
    record_summary "Alias 'am': skipped (existing alias found)"
    return 0
  fi

  # Append new snippet
  if ! touch "${rc_file}" >/dev/null 2>&1; then
    warn "Could not write to ${rc_file}"
    return 1
  fi

  {
    printf '\n%s' "${snippet}"
  } >> "${rc_file}"

  ok "Added 'am' alias to ${rc_file} (run 'am' to start the server)"
  record_summary "Alias 'am': added to ${rc_file}"

  # Also define it for the current session
  # shellcheck disable=SC2139
  alias am="cd \"${repo_dir}\" && scripts/run_server_with_token.sh" 2>/dev/null || true

  return 0
}

verify_br_binary() {
  local binary_path="$1"
  if ! "${binary_path}" --version >/dev/null 2>&1; then
    err "Beads Rust CLI at ${binary_path} failed 'br --version'. You can retry or rerun the installer with --skip-beads to handle it yourself."
    return 1
  fi

  local version_line
  version_line=$("${binary_path}" --version 2>/dev/null | head -n 1 || true)
  if [[ -z "${version_line}" ]]; then
    version_line="br --version command succeeded"
  fi
  LAST_BR_VERSION="${version_line}"
  ok "Beads Rust CLI ready (${version_line})"
}

find_bv_binary() {
  if command -v bv >/dev/null 2>&1; then
    command -v bv
    return 0
  fi

  local candidates=("${HOME}/.local/bin/bv" "${HOME}/bin/bv" "/usr/local/bin/bv")
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done

  return 1
}

maybe_add_bv_path() {
  local binary_path="$1"
  local dir
  dir=$(dirname "${binary_path}")
  if [[ ":${PATH}:" != *":${dir}:"* ]]; then
    export PATH="${dir}:${PATH}"
    ok "Temporarily added ${dir} to PATH so this session can invoke 'bv' immediately"
  fi
}

verify_bv_binary() {
  local binary_path="$1"
  if ! "${binary_path}" --version >/dev/null 2>&1; then
    warn "Beads Viewer at ${binary_path} failed 'bv --version'"
    return 1
  fi

  local version_line
  version_line=$("${binary_path}" --version 2>/dev/null | head -n 1 || true)
  if [[ -z "${version_line}" ]]; then
    version_line="bv --version command succeeded"
  fi
  LAST_BV_VERSION="${version_line}"
  ok "Beads Viewer ready (${version_line})"
  return 0
}

ensure_uv() {
  if need_cmd uv; then
    ok "uv is already installed"
    record_summary "uv: already installed"
    return 0
  fi
  info "Installing uv (Astral)"
  if ! need_cmd curl; then err "curl is required to install uv"; exit 1; fi
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
  if need_cmd uv; then ok "uv installed"; record_summary "uv: installed"; else err "uv install failed"; exit 1; fi
}

ensure_jq() {
  # jq is needed for safe JSON merging in integration scripts
  if need_cmd jq; then
    ok "jq is already installed"
    record_summary "jq: already installed"
    return 0
  fi

  info "jq not found - needed for safe config merging"

  # Try to install automatically
  local installed=0

  if [[ "$(uname -s)" == "Darwin" ]]; then
    # macOS - use Homebrew
    if need_cmd brew; then
      info "Installing jq via Homebrew"
      if brew install jq >/dev/null 2>&1; then
        installed=1
      fi
    fi
  elif [[ -f /etc/debian_version ]]; then
    # Debian/Ubuntu
    if need_cmd apt-get; then
      info "Installing jq via apt"
      if sudo apt-get update -qq >/dev/null 2>&1 && sudo apt-get install -y -qq jq >/dev/null 2>&1; then
        installed=1
      fi
    fi
  elif [[ -f /etc/fedora-release ]] || [[ -f /etc/redhat-release ]]; then
    # Fedora/RHEL
    if need_cmd dnf; then
      info "Installing jq via dnf"
      if sudo dnf install -y -q jq >/dev/null 2>&1; then
        installed=1
      fi
    elif need_cmd yum; then
      info "Installing jq via yum"
      if sudo yum install -y -q jq >/dev/null 2>&1; then
        installed=1
      fi
    fi
  elif [[ -f /etc/alpine-release ]]; then
    # Alpine
    if need_cmd apk; then
      info "Installing jq via apk"
      if sudo apk add --quiet jq >/dev/null 2>&1; then
        installed=1
      fi
    fi
  fi

  if [[ ${installed} -eq 1 ]] && need_cmd jq; then
    ok "jq installed"
    record_summary "jq: installed"
    return 0
  fi

  # Couldn't auto-install
  warn "Could not auto-install jq"
  warn "Integration scripts will fall back to safe mode (won't overwrite existing configs)"
  warn "To install manually:"
  warn "  macOS:   brew install jq"
  warn "  Ubuntu:  sudo apt install jq"
  warn "  Fedora:  sudo dnf install jq"
  record_summary "jq: not installed (manual install recommended)"
  return 0  # Don't fail the installer, just warn
}

update_existing_repo() {
  # Pull latest changes from origin for an existing repo
  # This ensures users get bug fixes and updates when re-running the installer
  local repo_path="$1"

  info "Pulling latest changes from origin/${BRANCH}"
  if ! (cd "${repo_path}" && git fetch origin "${BRANCH}" --depth 1 2>/dev/null); then
    warn "Could not fetch from origin; continuing with existing code"
    return 0
  fi

  # Check if there are updates
  local local_sha remote_sha
  local_sha=$(cd "${repo_path}" && git rev-parse HEAD 2>/dev/null || echo "unknown")
  remote_sha=$(cd "${repo_path}" && git rev-parse "origin/${BRANCH}" 2>/dev/null || echo "unknown")

  if [[ "${local_sha}" == "${remote_sha}" ]]; then
    ok "Already up to date (${local_sha:0:8})"
    record_summary "Repo: already up to date"
    return 0
  fi

  # Stash any local changes to avoid conflicts
  local has_changes=0
  if (cd "${repo_path}" && git diff --quiet 2>/dev/null) && (cd "${repo_path}" && git diff --cached --quiet 2>/dev/null); then
    has_changes=0
  else
    has_changes=1
    info "Stashing local changes before update"
    (cd "${repo_path}" && git stash push -m "installer-auto-stash-$(date +%Y%m%d%H%M%S)" 2>/dev/null) || true
  fi

  # Reset to origin/BRANCH to get latest code
  if (cd "${repo_path}" && git reset --hard "origin/${BRANCH}" 2>/dev/null); then
    ok "Updated to latest (${local_sha:0:8} → ${remote_sha:0:8})"
    record_summary "Repo: updated ${local_sha:0:8} → ${remote_sha:0:8}"
  else
    warn "Could not update repo; continuing with existing code"
    record_summary "Repo: update failed, using existing"
  fi

  # Restore stashed changes if any
  if [[ "${has_changes}" -eq 1 ]]; then
    if (cd "${repo_path}" && git stash pop 2>/dev/null); then
      ok "Restored local changes"
    else
      warn "Could not restore stashed changes; they're saved in git stash"
    fi
  fi
}

ensure_repo() {
  # Determine target directory
  if [[ -z "${CLONE_DIR}" ]]; then CLONE_DIR="${DEFAULT_CLONE_DIR}"; fi

  # If we're already in the repo (local run), use it and update
  if [[ -f "pyproject.toml" ]] && grep -q '^name\s*=\s*"mcp-agent-mail"' pyproject.toml 2>/dev/null; then
    REPO_DIR="$PWD"
    ok "Using existing repo at: ${REPO_DIR}"
    update_existing_repo "${REPO_DIR}"
    return 0
  fi

  # If directory exists and looks like the repo, use it and update
  if [[ -d "${CLONE_DIR}" ]] && [[ -f "${CLONE_DIR}/pyproject.toml" ]] && grep -q '^name\s*=\s*"mcp-agent-mail"' "${CLONE_DIR}/pyproject.toml" 2>/dev/null; then
    REPO_DIR="${CLONE_DIR}"
    ok "Using existing repo at: ${REPO_DIR}"
    update_existing_repo "${REPO_DIR}"
    return 0
  fi

  # Otherwise clone
  info "Cloning ${REPO_URL} (branch=${BRANCH}) to ${CLONE_DIR}"
  need_cmd git || { err "git is required to clone"; exit 1; }
  git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${CLONE_DIR}"
  REPO_DIR="${CLONE_DIR}"
  ok "Cloned repo"
  record_summary "Repo: cloned into ${REPO_DIR}"
}

ensure_python_and_venv() {
  info "Ensuring Python 3.14 and project venv (.venv)"
  uv python install 3.14
  if [[ ! -d "${REPO_DIR}/.venv" ]]; then
    (cd "${REPO_DIR}" && uv venv -p 3.14)
    ok "Created venv at ${REPO_DIR}/.venv"
    record_summary "Venv: created at ${REPO_DIR}/.venv"
  else
    ok "Found existing venv at ${REPO_DIR}/.venv"
    record_summary "Venv: existing at ${REPO_DIR}/.venv"
  fi
}

sync_deps() {
  info "Syncing dependencies with uv"
  (
    cd "${REPO_DIR}"
    # shellcheck disable=SC1091
    source .venv/bin/activate
    uv sync
  )
  ok "Dependencies installed"
  record_summary "Dependencies: uv sync complete"
}

configure_port() {
  if [[ -z "${HTTP_PORT_OVERRIDE}" ]]; then
    return 0
  fi

  local env_file="${REPO_DIR}/.env"
  local tmp="${env_file}.tmp.$$"

  info "Configuring HTTP_PORT=${HTTP_PORT_OVERRIDE} in .env"

  # Set trap to cleanup temp file
  trap "rm -f \"${tmp}\" 2>/dev/null" EXIT INT TERM

  # Set secure umask for .env file operations, save original to restore later
  local old_umask
  old_umask=$(umask)
  umask 077

  if [[ -f "${env_file}" ]]; then
    # File exists - update or append
    if grep -q '^HTTP_PORT=' "${env_file}"; then
      # Replace existing value
      if ! sed "s/^HTTP_PORT=.*/HTTP_PORT=${HTTP_PORT_OVERRIDE}/" "${env_file}" > "${tmp}"; then
        err "Failed to update HTTP_PORT in .env"
        rm -f "${tmp}" 2>/dev/null
        trap - EXIT INT TERM
        umask "${old_umask}"
        return 1
      fi
    else
      # Append new value
      if ! { cat "${env_file}"; echo "HTTP_PORT=${HTTP_PORT_OVERRIDE}"; } > "${tmp}"; then
        err "Failed to append HTTP_PORT to .env"
        rm -f "${tmp}" 2>/dev/null
        trap - EXIT INT TERM
        umask "${old_umask}"
        return 1
      fi
    fi

    # Atomic move
    if ! mv "${tmp}" "${env_file}"; then
      err "Failed to write .env file"
      rm -f "${tmp}" 2>/dev/null
      trap - EXIT INT TERM
      umask "${old_umask}"
      return 1
    fi
  else
    # Create new file
    if ! echo "HTTP_PORT=${HTTP_PORT_OVERRIDE}" > "${tmp}"; then
      err "Failed to create .env file"
      rm -f "${tmp}" 2>/dev/null
      trap - EXIT INT TERM
      umask "${old_umask}"
      return 1
    fi

    if ! mv "${tmp}" "${env_file}"; then
      err "Failed to write .env file"
      rm -f "${tmp}" 2>/dev/null
      trap - EXIT INT TERM
      umask "${old_umask}"
      return 1
    fi
  fi

  # Ensure secure permissions (in case file existed with wrong perms)
  chmod 600 "${env_file}" 2>/dev/null || warn "Could not set .env permissions to 600"

  trap - EXIT INT TERM
  umask "${old_umask}"
  ok "HTTP_PORT set to ${HTTP_PORT_OVERRIDE}"
  record_summary "HTTP port: ${HTTP_PORT_OVERRIDE}"
}

run_integration_and_start() {
  if [[ "${NO_START}" -eq 1 ]]; then
    warn "--no-start specified; skipping integration/start"
    return 0
  fi
  info "Running auto-detect integration and starting server"
  (
    cd "${REPO_DIR}"
    # shellcheck disable=SC1091
    source .venv/bin/activate
    export INTEGRATION_BEARER_TOKEN="${INTEGRATION_TOKEN}"
    args=()
    if [[ "${YES}" -eq 1 ]]; then args+=("--yes"); fi
    if [[ -n "${PROJECT_DIR}" ]]; then args+=("--project-dir" "${PROJECT_DIR}"); fi
    bash scripts/automatically_detect_all_installed_coding_agents_and_install_mcp_agent_mail_in_all.sh "${args[@]}"
  )
}

ensure_beads() {
  if [[ "${SKIP_BEADS}" -eq 1 ]]; then
    warn "--skip-beads specified; not installing Beads Rust CLI"
    record_summary "Beads Rust CLI: skipped (--skip-beads)"
    return 0
  fi

  local br_path
  if br_path=$(find_br_binary); then
    verify_br_binary "${br_path}" || exit 1
    ensure_br_path_ready "${br_path}"
    install_bd_alias
    record_summary "Beads Rust CLI: ${LAST_BR_VERSION}"
    return 0
  fi

  info "Installing Beads Rust (br) CLI"
  if ! need_cmd curl; then
    err "curl is required to install Beads Rust automatically"
    exit 1
  fi

  if ! curl -fsSL "${BEADS_INSTALL_URL}?$(date +%s)" | bash; then
    err "Failed to install Beads Rust automatically. You can install manually via: curl -fsSL ${BEADS_INSTALL_URL} | bash"
    exit 1
  fi

  hash -r 2>/dev/null || true

  if br_path=$(find_br_binary); then
    verify_br_binary "${br_path}" || exit 1
    ensure_br_path_ready "${br_path}"
    install_bd_alias
    record_summary "Beads Rust CLI: ${LAST_BR_VERSION}"
    return 0
  fi

  err "Beads Rust installer finished but 'br' was not detected. Ensure its install directory is on PATH or rerun with --skip-beads to handle installation manually."
  exit 1
}

install_cli_stub() {
  # Install a helpful "mcp-agent-mail" command that explains this is NOT a CLI tool
  # This catches agents that mistakenly try to run it as a shell command
  local stub_dir="${HOME}/.local/bin"
  local stub_path="${stub_dir}/mcp-agent-mail"

  mkdir -p "${stub_dir}" 2>/dev/null || true

  cat > "${stub_path}" <<'STUB_EOF'
#!/usr/bin/env bash
# MCP Agent Mail — Helpful Stub for Confused Agents
#
# If you're seeing this, you (or an AI agent) tried to run "mcp-agent-mail"
# as a CLI command. That's a common mistake!

cat <<'MSG'
╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                              ║
║   🚫  MCP Agent Mail is NOT a CLI tool!                                      ║
║                                                                              ║
║   It's an MCP (Model Context Protocol) server that provides tools to your   ║
║   AI coding agent. You should already have access to these tools as part    ║
║   of your available MCP tools.                                              ║
║                                                                              ║
║   ✅ CORRECT USAGE:                                                          ║
║      Use the MCP tools directly, for example:                               ║
║        • mcp__mcp-agent-mail__register_agent                                ║
║        • mcp__mcp-agent-mail__send_message                                  ║
║        • mcp__mcp-agent-mail__fetch_inbox                                   ║
║                                                                              ║
║   ❌ INCORRECT USAGE:                                                        ║
║      Running shell commands like:                                           ║
║        • mcp-agent-mail send --to BlueLake ...                              ║
║        • mcp-agent-mail --help                                              ║
║                                                                              ║
║   📚 For documentation, see:                                                 ║
║      https://github.com/Dicklesworthstone/mcp_agent_mail                    ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
MSG
exit 1
STUB_EOF

  chmod +x "${stub_path}" 2>/dev/null || true

  # Also create common aliases/variants agents might try
  for variant in "mcp_agent_mail" "mcpagentmail" "agentmail" "agent-mail"; do
    local variant_path="${stub_dir}/${variant}"
    if [[ ! -f "${variant_path}" ]]; then
      ln -sf "${stub_path}" "${variant_path}" 2>/dev/null || true
    fi
  done

  ok "Installed helpful CLI stub at ${stub_path}"
  record_summary "CLI stub: installed (catches mistaken CLI usage)"
}

ensure_bv() {
  if [[ "${SKIP_BV}" -eq 1 ]]; then
    warn "--skip-bv specified; not installing Beads Viewer"
    record_summary "Beads Viewer: skipped (--skip-bv)"
    return 0
  fi

  local bv_path
  if bv_path=$(find_bv_binary); then
    if verify_bv_binary "${bv_path}"; then
      maybe_add_bv_path "${bv_path}"
      record_summary "Beads Viewer: ${LAST_BV_VERSION}"
    else
      warn "Beads Viewer found but verification failed; continuing without bv"
      record_summary "Beads Viewer: found but failed verification"
    fi
    return 0
  fi

  info "Installing Beads Viewer (bv) TUI (optional)"
  if ! need_cmd curl; then
    warn "curl not available; skipping Beads Viewer installation"
    record_summary "Beads Viewer: skipped (no curl)"
    return 0
  fi

  if ! curl -fsSL "${BV_INSTALL_URL}" | bash; then
    warn "Beads Viewer installation failed (non-fatal). You can install manually via: curl -fsSL ${BV_INSTALL_URL} | bash"
    record_summary "Beads Viewer: installation failed (optional)"
    return 0
  fi

  hash -r 2>/dev/null || true

  if bv_path=$(find_bv_binary); then
    if verify_bv_binary "${bv_path}"; then
      maybe_add_bv_path "${bv_path}"
      record_summary "Beads Viewer: ${LAST_BV_VERSION}"
    else
      warn "Beads Viewer installed but verification failed"
      record_summary "Beads Viewer: installed but failed verification"
    fi
    return 0
  fi

  warn "Beads Viewer installer finished but 'bv' was not detected. You can install manually via: curl -fsSL ${BV_INSTALL_URL} | bash"
  record_summary "Beads Viewer: not detected after install (optional)"
  return 0
}

offer_doc_blurbs() {
  if [[ "${YES}" -eq 1 ]]; then
    info "Docs helper available anytime via: uv run python -m mcp_agent_mail.cli docs insert-blurbs"
    return 0
  fi

  echo
  echo "Would you like to automatically detect your code projects and insert the relevant blurbs for Agent Mail into your AGENTS.md and CLAUDE.md files?"
  echo "You will be able to confirm for each detecting project if you want to do that. Otherwise, just skip that, but be sure to add the blurbs yourself manually for the system to work properly."
  read -r -p "[y/N] " doc_choice
  doc_choice=$(printf '%s' "${doc_choice}" | tr -d '[:space:]')
  case "${doc_choice}" in
    y|Y)
      (
        cd "${REPO_DIR}" && uv run python -m mcp_agent_mail.cli docs insert-blurbs
      ) || warn "Docs helper encountered an issue. You can rerun it later with: uv run python -m mcp_agent_mail.cli docs insert-blurbs"
      ;;
    *)
      info "Skipping automatic doc updates; remember to add the blurbs manually."
      ;;
  esac
}

main() {
  if [[ "${START_ONLY}" -eq 1 ]]; then
    info "--start-only specified: skipping clone/setup; starting integration"
    REPO_DIR="$PWD"
    record_summary "Repo: existing at ${REPO_DIR} (--start-only)"
    ensure_beads
    ensure_bv
    install_cli_stub
    install_am_alias "${REPO_DIR}"
    configure_port
    if ! run_integration_and_start; then
      err "Integration failed; aborting."
      exit 1
    fi
    record_summary "Integration: auto-detect + server start"
    print_summary
    offer_doc_blurbs
    exit 0
  fi

  ensure_uv
  ensure_jq
  ensure_beads
  ensure_bv
  install_cli_stub
  ensure_repo
  ensure_python_and_venv
  sync_deps
  install_am_alias "${REPO_DIR}"
  configure_port
  if ! run_integration_and_start; then
    err "Integration failed; aborting."
    exit 1
  fi
  record_summary "Integration: auto-detect + server start"
  print_summary
  offer_doc_blurbs

  echo
  ok "All set!"
  echo "Next runs (open a new terminal or run 'source ~/.zshrc' / 'source ~/.bashrc'):"
  echo "  am                                    # quick alias to start the server"
  echo "  # or manually:"
  echo "  cd \"${REPO_DIR}\""
  echo "  source .venv/bin/activate"
  echo "  uv run python -m mcp_agent_mail.cli"
}

# Handle three execution modes:
# 1. Direct: ./install.sh → BASH_SOURCE[0] == $0 → run main
# 2. Sourced: . install.sh → BASH_SOURCE[0] != $0 → skip main
# 3. Piped: curl ... | bash -s → BASH_SOURCE[0] is unset → run main
if [[ -z "${BASH_SOURCE[0]:-}" ]] || [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
