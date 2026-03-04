#!/bin/sh
# identity-resolve.sh — Resolve a tmux pane to its agent name.
#
# Canonical path:
#   ~/.local/state/agent-mail/identity/<project_hash>/<pane_id>
#
# Usage:
#   identity-resolve.sh [project_path] [pane_id]
#   identity-resolve.sh --cleanup [project_path]
#
# Modes:
#   resolve (default)  Print the agent name for the given pane/project.
#                      Exits 0 and prints the name, or exits 1 if not found / stale.
#   --cleanup          Remove all stale identity files (older than STALE_SECONDS)
#                      for the given project (or all projects if project_path omitted).
#                      Prints each removed file path to stdout.
#
# Arguments:
#   project_path  Optional. Absolute path to the project. Defaults to $PWD.
#   pane_id       Optional. Defaults to $TMUX_PANE.
#
# Environment:
#   IDENTITY_STALE_SECONDS  Staleness threshold in seconds. Default: 86400 (24h).

set -eu

# ── helpers ──────────────────────────────────────────────────────────────────

die() { printf 'identity-resolve: error: %s\n' "$1" >&2; exit 1; }

project_hash() {
    _input="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s' "$_input" | sha256sum | cut -c1-12
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s' "$_input" | shasum -a 256 | cut -c1-12
    elif command -v openssl >/dev/null 2>&1; then
        printf '%s' "$_input" | openssl dgst -sha256 -hex 2>/dev/null | sed 's/^.*= //' | cut -c1-12
    else
        die "no sha256 tool found (need sha256sum, shasum, or openssl)"
    fi
}

epoch_now() {
    date +%s
}

STALE_SECONDS="${IDENTITY_STALE_SECONDS:-86400}"
BASE_DIR="${HOME}/.local/state/agent-mail/identity"

# ── mode dispatch ────────────────────────────────────────────────────────────

MODE="resolve"
if [ "${1:-}" = "--cleanup" ]; then
    MODE="cleanup"
    shift
fi

# ── cleanup mode ─────────────────────────────────────────────────────────────

if [ "$MODE" = "cleanup" ]; then
    PROJECT_PATH="${1:-}"
    NOW=$(epoch_now)

    if [ -n "$PROJECT_PATH" ]; then
        HASH=$(project_hash "$PROJECT_PATH")
        SEARCH_DIR="${BASE_DIR}/${HASH}"
    else
        SEARCH_DIR="$BASE_DIR"
    fi

    [ -d "$SEARCH_DIR" ] || exit 0

    # Walk identity files. Skip temp files and directories.
    # Use a find-free approach for maximum POSIX compat on simple trees.
    # The directory structure is only 2 levels deep: <hash>/<pane_id>
    _cleanup_dir() {
        _dir="$1"
        for _file in "$_dir"/*; do
            [ -f "$_file" ] || continue
            # Skip temp files (pattern: *.tmp.*)
            case "$_file" in *.tmp.*) continue ;; esac

            # Read timestamp from line 2.
            _ts=""
            _linenum=0
            while IFS= read -r _line || [ -n "$_line" ]; do
                _linenum=$((_linenum + 1))
                if [ "$_linenum" -eq 2 ]; then
                    _ts="$_line"
                    break
                fi
            done < "$_file"

            # If no timestamp or not a number, treat as stale.
            case "$_ts" in
                ''|*[!0-9]*) _ts=0 ;;
            esac

            _age=$((NOW - _ts))
            if [ "$_age" -gt "$STALE_SECONDS" ]; then
                rm -f "$_file" && printf '%s\n' "$_file"
            fi
        done

        # Remove the directory if now empty.
        rmdir "$_dir" 2>/dev/null || true
    }

    if [ -n "$PROJECT_PATH" ]; then
        # Cleanup a single project hash directory.
        _cleanup_dir "$SEARCH_DIR"
    else
        # Cleanup all project hash directories.
        for _subdir in "$SEARCH_DIR"/*/; do
            [ -d "$_subdir" ] || continue
            _cleanup_dir "$_subdir"
        done
    fi

    exit 0
fi

# ── resolve mode ─────────────────────────────────────────────────────────────

PROJECT_PATH="${1:-${PWD}}"
PANE_ID="${2:-${TMUX_PANE:-}}"

[ -z "$PANE_ID" ] && die "pane_id not provided and TMUX_PANE is not set"

HASH=$(project_hash "$PROJECT_PATH")
IDENTITY_FILE="${BASE_DIR}/${HASH}/${PANE_ID}"

[ -f "$IDENTITY_FILE" ] || exit 1

# Read the file: line 1 = agent name, line 2 = timestamp.
AGENT_NAME=""
FILE_TS=""
_linenum=0
while IFS= read -r _line || [ -n "$_line" ]; do
    _linenum=$((_linenum + 1))
    case "$_linenum" in
        1) AGENT_NAME="$_line" ;;
        2) FILE_TS="$_line" ;;
    esac
    [ "$_linenum" -ge 2 ] && break
done < "$IDENTITY_FILE"

[ -z "$AGENT_NAME" ] && exit 1

# Staleness check.
case "$FILE_TS" in
    ''|*[!0-9]*) FILE_TS=0 ;;
esac

NOW=$(epoch_now)
AGE=$((NOW - FILE_TS))

if [ "$AGE" -gt "$STALE_SECONDS" ]; then
    # Stale identity. Print nothing, exit non-zero.
    exit 1
fi

printf '%s\n' "$AGENT_NAME"
