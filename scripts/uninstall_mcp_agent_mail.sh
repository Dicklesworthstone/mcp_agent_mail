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

# ============================================================================
# CRITICAL SAFETY CHECKS - Validate all path variables before ANY file ops
# ============================================================================
# These checks prevent catastrophic rm operations if variables are empty/unset

# Validate ROOT_DIR
if [[ -z "${ROOT_DIR:-}" ]]; then
  echo "FATAL: ROOT_DIR is empty - refusing to proceed" >&2
  exit 1
fi
if [[ ! -d "${ROOT_DIR}" ]]; then
  echo "FATAL: ROOT_DIR '${ROOT_DIR}' is not a directory - refusing to proceed" >&2
  exit 1
fi
# Ensure ROOT_DIR is an absolute path (starts with /)
if [[ "${ROOT_DIR}" != /* ]]; then
  echo "FATAL: ROOT_DIR '${ROOT_DIR}' is not an absolute path - refusing to proceed" >&2
  exit 1
fi

# Validate HOME
if [[ -z "${HOME:-}" ]]; then
  echo "FATAL: HOME is empty - refusing to proceed" >&2
  exit 1
fi
if [[ ! -d "${HOME}" ]]; then
  echo "FATAL: HOME '${HOME}' is not a directory - refusing to proceed" >&2
  exit 1
fi
if [[ "${HOME}" != /* ]]; then
  echo "FATAL: HOME '${HOME}' is not an absolute path - refusing to proceed" >&2
  exit 1
fi
# Extra safety: HOME should not be root
if [[ "${HOME}" == "/" ]]; then
  echo "FATAL: HOME is '/' - refusing to proceed" >&2
  exit 1
fi

log_step "MCP Agent Mail: Uninstall Configurations"
echo
echo "This will remove MCP Agent Mail configurations installed by:"
echo "  scripts/automatically_detect_all_installed_coding_agents_and_install_mcp_agent_mail_in_all.sh"
echo
echo "What will be removed:"
echo "  - Project-local MCP config files (*.mcp.json, .claude/, .codex/, .vscode/mcp.json)"
echo "  - User-level MCP entries (mcp-agent-mail) from ~/.claude, ~/.codex, ~/.cursor, ~/.gemini"
echo "  - CLI registrations (claude mcp, gemini mcp)"
echo "  - HTTP_BEARER_TOKEN from .env"
echo "  - scripts/run_server_with_token.sh"
echo
echo "What will NOT be removed:"
echo "  - Backup files in backup_config_files/"
echo "  - Server data/storage (use separate cleanup if needed)"
echo "  - The mcp_agent_mail package itself"
echo

TARGET_DIR="${PROJECT_DIR:-}"
if [[ -z "${TARGET_DIR}" ]]; then TARGET_DIR="${ROOT_DIR}"; fi

# Validate TARGET_DIR (critical - used in all rm operations)
if [[ -z "${TARGET_DIR:-}" ]]; then
  echo "FATAL: TARGET_DIR is empty - refusing to proceed" >&2
  exit 1
fi
if [[ ! -d "${TARGET_DIR}" ]]; then
  echo "FATAL: TARGET_DIR '${TARGET_DIR}' is not a directory - refusing to proceed" >&2
  exit 1
fi
if [[ "${TARGET_DIR}" != /* ]]; then
  echo "FATAL: TARGET_DIR '${TARGET_DIR}' is not an absolute path - refusing to proceed" >&2
  exit 1
fi
# Extra safety: TARGET_DIR should not be root or common system directories
if [[ "${TARGET_DIR}" == "/" ]] || [[ "${TARGET_DIR}" == "/usr" ]] || [[ "${TARGET_DIR}" == "/etc" ]] || [[ "${TARGET_DIR}" == "/var" ]] || [[ "${TARGET_DIR}" == "/bin" ]] || [[ "${TARGET_DIR}" == "/sbin" ]]; then
  echo "FATAL: TARGET_DIR '${TARGET_DIR}' is a system directory - refusing to proceed" >&2
  exit 1
fi

if ! confirm "Proceed with uninstall?"; then log_warn "Aborted."; exit 1; fi

# Track what was removed for summary
REMOVED_FILES=()
REMOVED_ENTRIES=()
SKIPPED=()

# Helper: safely remove a file with extensive safety checks
safe_rm() {
  local file="$1"

  # SAFETY: Validate file path is non-empty
  if [[ -z "${file:-}" ]]; then
    log_err "safe_rm called with empty path - SKIPPING"
    return 1
  fi

  # SAFETY: Must be an absolute path
  if [[ "${file}" != /* ]]; then
    log_err "safe_rm: '${file}' is not an absolute path - SKIPPING"
    return 1
  fi

  # SAFETY: Path must have at least 3 components (e.g., /home/user/file, not /file)
  # Count slashes - a safe path like /home/user/.file has at least 3
  local slash_count
  slash_count=$(echo "$file" | tr -cd '/' | wc -c)
  if [[ $slash_count -lt 3 ]]; then
    log_err "safe_rm: '${file}' path too shallow (only ${slash_count} levels) - SKIPPING"
    return 1
  fi

  # SAFETY: Reject paths that look like system directories
  case "$file" in
    /bin/*|/sbin/*|/usr/*|/etc/*|/var/*|/lib/*|/lib64/*|/boot/*|/dev/*|/proc/*|/sys/*)
      log_err "safe_rm: '${file}' appears to be in a system directory - SKIPPING"
      return 1
      ;;
  esac

  # SAFETY: Must NOT be a directory
  if [[ -d "$file" ]]; then
    log_err "safe_rm: '${file}' is a directory, not a file - SKIPPING"
    return 1
  fi

  if [[ -f "$file" ]]; then
    if [[ "${DRY_RUN}" == "1" ]]; then
      _print "[dry-run] rm ${file}"
    else
      rm -f "$file" && log_ok "Removed: ${file}"
    fi
    REMOVED_FILES+=("$file")
  fi
}

# Helper: safely remove a directory if empty
safe_rmdir() {
  local dir="$1"

  # SAFETY: Validate path is non-empty
  if [[ -z "${dir:-}" ]]; then
    log_err "safe_rmdir called with empty path - SKIPPING"
    return 1
  fi

  # SAFETY: Must be an absolute path
  if [[ "${dir}" != /* ]]; then
    log_err "safe_rmdir: '${dir}' is not an absolute path - SKIPPING"
    return 1
  fi

  # SAFETY: Path must have at least 3 components
  local slash_count
  slash_count=$(echo "$dir" | tr -cd '/' | wc -c)
  if [[ $slash_count -lt 3 ]]; then
    log_err "safe_rmdir: '${dir}' path too shallow (only ${slash_count} levels) - SKIPPING"
    return 1
  fi

  # SAFETY: Reject system directories
  case "$dir" in
    /bin/*|/sbin/*|/usr/*|/etc/*|/var/*|/lib/*|/lib64/*|/boot/*|/dev/*|/proc/*|/sys/*)
      log_err "safe_rmdir: '${dir}' appears to be in a system directory - SKIPPING"
      return 1
      ;;
  esac

  if [[ -d "$dir" ]] && [[ -z "$(ls -A "$dir" 2>/dev/null)" ]]; then
    if [[ "${DRY_RUN}" == "1" ]]; then
      _print "[dry-run] rmdir ${dir}"
    else
      rmdir "$dir" 2>/dev/null && log_ok "Removed empty directory: ${dir}"
    fi
  fi
}

# Helper: remove JSON key from file using jq (preserves rest of file)
remove_json_key() {
  local file="$1"
  local key_path="$2"  # e.g., '.mcpServers["mcp-agent-mail"]'
  local description="$3"

  # SAFETY: Validate file path
  if [[ -z "${file:-}" ]]; then
    log_err "remove_json_key called with empty path - SKIPPING"
    return 1
  fi
  if [[ "${file}" != /* ]]; then
    log_err "remove_json_key: '${file}' is not an absolute path - SKIPPING"
    return 1
  fi

  if [[ ! -f "$file" ]]; then
    return 0
  fi

  if ! command -v jq >/dev/null 2>&1; then
    log_warn "jq not found; skipping ${file}"
    SKIPPED+=("$file (no jq)")
    return 1
  fi

  # Check if key exists
  if ! jq -e "${key_path}" "$file" >/dev/null 2>&1; then
    return 0  # Key doesn't exist
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] remove ${description} from ${file}"
    REMOVED_ENTRIES+=("${description} from ${file}")
    return 0
  fi

  local tmp="${file}.tmp.$$"
  trap 'rm -f "$tmp" 2>/dev/null' EXIT INT TERM

  umask 077
  if jq "del(${key_path})" "$file" > "$tmp" 2>/dev/null; then
    if mv "$tmp" "$file" 2>/dev/null; then
      log_ok "Removed ${description} from ${file}"
      REMOVED_ENTRIES+=("${description} from ${file}")
    else
      rm -f "$tmp" 2>/dev/null
      log_warn "Failed to update ${file}"
      SKIPPED+=("$file (mv failed)")
    fi
  else
    rm -f "$tmp" 2>/dev/null
    log_warn "Failed to parse/update ${file}"
    SKIPPED+=("$file (jq failed)")
  fi
  trap - EXIT INT TERM
}

# Helper: remove TOML section from file
remove_toml_section() {
  local file="$1"
  local section="$2"  # e.g., "[mcp_servers.mcp_agent_mail]"

  # SAFETY: Validate file path
  if [[ -z "${file:-}" ]]; then
    log_err "remove_toml_section called with empty path - SKIPPING"
    return 1
  fi
  if [[ "${file}" != /* ]]; then
    log_err "remove_toml_section: '${file}' is not an absolute path - SKIPPING"
    return 1
  fi

  if [[ ! -f "$file" ]]; then
    return 0
  fi

  if ! grep -q "^\[mcp_servers\.mcp_agent_mail\]" "$file" 2>/dev/null; then
    return 0  # Section doesn't exist
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] remove ${section} section from ${file}"
    REMOVED_ENTRIES+=("${section} from ${file}")
    return 0
  fi

  local tmp="${file}.tmp.$$"
  trap 'rm -f "$tmp" 2>/dev/null' EXIT INT TERM

  umask 077
  # Remove the section and its contents until next section or EOF
  # This awk script removes [mcp_servers.mcp_agent_mail] and all lines until next [section]
  if awk '
    BEGIN { skip=0 }
    /^\[mcp_servers\.mcp_agent_mail\]/ { skip=1; next }
    /^\[/ { skip=0 }
    skip==0 { print }
  ' "$file" > "$tmp" 2>/dev/null; then
    # Also remove the comment line if present
    sed -i.bak '/^# MCP servers configuration (mcp-agent-mail)$/d' "$tmp" 2>/dev/null || true
    rm -f "${tmp}.bak" 2>/dev/null || true

    if mv "$tmp" "$file" 2>/dev/null; then
      log_ok "Removed ${section} section from ${file}"
      REMOVED_ENTRIES+=("${section} from ${file}")
    else
      rm -f "$tmp" 2>/dev/null
      log_warn "Failed to update ${file}"
    fi
  else
    rm -f "$tmp" 2>/dev/null
    log_warn "Failed to parse ${file}"
  fi
  trap - EXIT INT TERM
}

# Helper: remove env var from .env file
remove_env_var() {
  local file="$1"
  local key="$2"

  # SAFETY: Validate file path
  if [[ -z "${file:-}" ]]; then
    log_err "remove_env_var called with empty path - SKIPPING"
    return 1
  fi
  if [[ "${file}" != /* ]]; then
    log_err "remove_env_var: '${file}' is not an absolute path - SKIPPING"
    return 1
  fi

  if [[ ! -f "$file" ]]; then
    return 0
  fi

  if ! grep -q "^${key}=" "$file" 2>/dev/null; then
    return 0
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] remove ${key} from ${file}"
    REMOVED_ENTRIES+=("${key} from ${file}")
    return 0
  fi

  local tmp="${file}.tmp.$$"
  trap 'rm -f "$tmp" 2>/dev/null' EXIT INT TERM

  umask 077
  if grep -v "^${key}=" "$file" > "$tmp" 2>/dev/null; then
    if mv "$tmp" "$file" 2>/dev/null; then
      log_ok "Removed ${key} from ${file}"
      REMOVED_ENTRIES+=("${key} from ${file}")
    else
      rm -f "$tmp" 2>/dev/null
    fi
  else
    rm -f "$tmp" 2>/dev/null
  fi
  trap - EXIT INT TERM
}

# ============================================================================
# Stop running server (optional)
# ============================================================================
log_step "Checking for running MCP Agent Mail server"
_pids=$(find_listening_pids_for_port 8765)
if [[ -n "${_pids}" ]]; then
  _print "Found server process(es) on port 8765: ${_pids}"
  if confirm "Stop the running server?"; then
    # shellcheck disable=SC2086
    kill_pids_graceful 5 ${_pids}
    log_ok "Server stopped"
  else
    log_warn "Server left running"
  fi
else
  log_ok "No server running on port 8765"
fi

# ============================================================================
# Remove project-local files
# ============================================================================
log_step "Removing project-local MCP configurations"

# Claude Code project settings
safe_rm "${TARGET_DIR}/.claude/settings.json"
safe_rm "${TARGET_DIR}/.claude/settings.local.json"
# Remove .claude directory if empty
safe_rmdir "${TARGET_DIR}/.claude"

# Codex CLI project config
safe_rm "${TARGET_DIR}/.codex/config.toml"
safe_rmdir "${TARGET_DIR}/.codex"

# MCP JSON config files
safe_rm "${TARGET_DIR}/codex.mcp.json"
safe_rm "${TARGET_DIR}/cursor.mcp.json"
safe_rm "${TARGET_DIR}/gemini.mcp.json"
safe_rm "${TARGET_DIR}/cline.mcp.json"
safe_rm "${TARGET_DIR}/windsurf.mcp.json"

# OpenCode config - remove only mcp-agent-mail entry, not entire file
if [[ -f "${TARGET_DIR}/opencode.json" ]]; then
  remove_json_key "${TARGET_DIR}/opencode.json" '.mcp["mcp-agent-mail"]' "mcp-agent-mail"
  # Check if mcp object is now empty and remove it
  if command -v jq >/dev/null 2>&1; then
    if jq -e '.mcp == {}' "${TARGET_DIR}/opencode.json" >/dev/null 2>&1; then
      remove_json_key "${TARGET_DIR}/opencode.json" '.mcp' "empty mcp object"
    fi
    # If file is now basically empty (only schema), remove it
    if jq -e 'keys | length <= 1' "${TARGET_DIR}/opencode.json" >/dev/null 2>&1; then
      safe_rm "${TARGET_DIR}/opencode.json"
    fi
  fi
fi

# VS Code/GitHub Copilot MCP config - remove only mcp-agent-mail entry
if [[ -f "${TARGET_DIR}/.vscode/mcp.json" ]]; then
  remove_json_key "${TARGET_DIR}/.vscode/mcp.json" '.servers["mcp-agent-mail"]' "mcp-agent-mail"
  # Check if servers object is now empty
  if command -v jq >/dev/null 2>&1; then
    if jq -e '.servers == {}' "${TARGET_DIR}/.vscode/mcp.json" >/dev/null 2>&1; then
      safe_rm "${TARGET_DIR}/.vscode/mcp.json"
    fi
  fi
fi

# Run helper script
safe_rm "${TARGET_DIR}/scripts/run_server_with_token.sh"

# Remove HTTP_BEARER_TOKEN from .env (but keep other entries)
remove_env_var "${TARGET_DIR}/.env" "HTTP_BEARER_TOKEN"
# If .env is now empty, remove it
if [[ -f "${TARGET_DIR}/.env" ]] && [[ ! -s "${TARGET_DIR}/.env" ]]; then
  safe_rm "${TARGET_DIR}/.env"
fi

# ============================================================================
# Remove user-level configurations
# ============================================================================
log_step "Removing user-level MCP configurations"

# Claude user settings - remove mcp-agent-mail entry
HOME_CLAUDE_SETTINGS="${HOME}/.claude/settings.json"
if [[ -f "$HOME_CLAUDE_SETTINGS" ]]; then
  remove_json_key "$HOME_CLAUDE_SETTINGS" '.mcpServers["mcp-agent-mail"]' "mcp-agent-mail"
fi

# Codex user config - remove mcp_servers.mcp_agent_mail section
HOME_CODEX_CONFIG="${HOME}/.codex/config.toml"
if [[ -f "$HOME_CODEX_CONFIG" ]]; then
  remove_toml_section "$HOME_CODEX_CONFIG" "[mcp_servers.mcp_agent_mail]"
fi

# Cursor user MCP config - remove mcp-agent-mail entry
HOME_CURSOR_MCP="${HOME}/.cursor/mcp.json"
if [[ -f "$HOME_CURSOR_MCP" ]]; then
  remove_json_key "$HOME_CURSOR_MCP" '.mcpServers["mcp-agent-mail"]' "mcp-agent-mail"
  # If mcpServers is now empty, remove the file
  if command -v jq >/dev/null 2>&1; then
    if jq -e '.mcpServers == {}' "$HOME_CURSOR_MCP" >/dev/null 2>&1; then
      safe_rm "$HOME_CURSOR_MCP"
    fi
  fi
fi

# Gemini user MCP config - remove mcp-agent-mail entry
HOME_GEMINI_MCP="${HOME}/.gemini/mcp.json"
if [[ -f "$HOME_GEMINI_MCP" ]]; then
  remove_json_key "$HOME_GEMINI_MCP" '.mcpServers["mcp-agent-mail"]' "mcp-agent-mail"
  # If mcpServers is now empty, remove the file
  if command -v jq >/dev/null 2>&1; then
    if jq -e '.mcpServers == {}' "$HOME_GEMINI_MCP" >/dev/null 2>&1; then
      safe_rm "$HOME_GEMINI_MCP"
    fi
  fi
fi

# VS Code user settings - remove chat.mcp.discovery.enabled if set
if [[ "$OSTYPE" == "darwin"* ]]; then
  VSCODE_USER_SETTINGS="${HOME}/Library/Application Support/Code/User/settings.json"
else
  VSCODE_USER_SETTINGS="${HOME}/.config/Code/User/settings.json"
fi
if [[ -f "$VSCODE_USER_SETTINGS" ]]; then
  # Only remove if we added it (check if it's true)
  if command -v jq >/dev/null 2>&1; then
    if jq -e '."chat.mcp.discovery.enabled" == true' "$VSCODE_USER_SETTINGS" >/dev/null 2>&1; then
      remove_json_key "$VSCODE_USER_SETTINGS" '."chat.mcp.discovery.enabled"' "chat.mcp.discovery.enabled"
    fi
  fi
fi

# ============================================================================
# Remove CLI registrations
# ============================================================================
log_step "Removing CLI MCP registrations"

# Claude CLI
if command -v claude >/dev/null 2>&1; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] claude mcp remove --scope user mcp-agent-mail"
    _print "[dry-run] claude mcp remove --scope project mcp-agent-mail"
  else
    claude mcp remove --scope user mcp-agent-mail 2>/dev/null && log_ok "Removed Claude CLI user MCP registration" || true
    (cd "${TARGET_DIR}" && claude mcp remove --scope project mcp-agent-mail 2>/dev/null) && log_ok "Removed Claude CLI project MCP registration" || true
  fi
  REMOVED_ENTRIES+=("Claude CLI MCP registration")
else
  _print "Claude CLI not found; skipping"
fi

# Gemini CLI
if command -v gemini >/dev/null 2>&1; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    _print "[dry-run] gemini mcp remove -s user mcp-agent-mail"
  else
    gemini mcp remove -s user mcp-agent-mail 2>/dev/null && log_ok "Removed Gemini CLI MCP registration" || true
  fi
  REMOVED_ENTRIES+=("Gemini CLI MCP registration")
else
  _print "Gemini CLI not found; skipping"
fi

# ============================================================================
# Summary
# ============================================================================
echo
log_step "Uninstall Summary"

if [[ ${#REMOVED_FILES[@]} -gt 0 ]]; then
  echo "Removed files:"
  for f in "${REMOVED_FILES[@]}"; do
    echo "  - ${f}"
  done
fi

if [[ ${#REMOVED_ENTRIES[@]} -gt 0 ]]; then
  echo "Removed config entries:"
  for e in "${REMOVED_ENTRIES[@]}"; do
    echo "  - ${e}"
  done
fi

if [[ ${#SKIPPED[@]} -gt 0 ]]; then
  echo "Skipped (manual cleanup may be needed):"
  for s in "${SKIPPED[@]}"; do
    echo "  - ${s}"
  done
fi

if [[ ${#REMOVED_FILES[@]} -eq 0 ]] && [[ ${#REMOVED_ENTRIES[@]} -eq 0 ]]; then
  log_ok "No MCP Agent Mail configurations found to remove."
else
  log_ok "Uninstall complete."
fi

echo
_print "Note: Backup files are preserved in backup_config_files/"
_print "To reinstall, run: scripts/automatically_detect_all_installed_coding_agents_and_install_mcp_agent_mail_in_all.sh"
