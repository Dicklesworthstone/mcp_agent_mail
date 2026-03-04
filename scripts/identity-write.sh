#!/bin/sh
# identity-write.sh — Write a per-pane agent identity file.
#
# Canonical path:
#   ~/.local/state/agent-mail/identity/<project_hash>/<pane_id>
#
# Usage:
#   identity-write.sh <agent_name> [project_path] [pane_id]
#
# Arguments:
#   agent_name    Required. The agent name string (e.g., "cc-0", "cod-1").
#   project_path  Optional. Absolute path to the project.
#                 Defaults to $PWD.
#   pane_id       Optional. The tmux pane identifier (e.g., "%0").
#                 Defaults to $TMUX_PANE. If neither is set, exits with error.
#
# The file contains two lines:
#   Line 1: agent name
#   Line 2: Unix epoch timestamp (seconds)
#
# Writes are atomic: content goes to a temp file, then mv to final path.
# Exit codes:
#   0 = success
#   1 = missing arguments or write failure

set -eu

# ── helpers ──────────────────────────────────────────────────────────────────

die() { printf 'identity-write: error: %s\n' "$1" >&2; exit 1; }

# Compute SHA-256 of a string, return first 12 hex chars.
# Tries sha256sum (GNU/Linux), then shasum (macOS), then openssl.
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

# Unix epoch in seconds (POSIX-portable).
epoch_now() {
    date +%s
}

# ── argument parsing ─────────────────────────────────────────────────────────

AGENT_NAME="${1:-}"
PROJECT_PATH="${2:-${PWD}}"
PANE_ID="${3:-${TMUX_PANE:-}}"

[ -z "$AGENT_NAME" ] && die "usage: identity-write.sh <agent_name> [project_path] [pane_id]"
[ -z "$PANE_ID" ]    && die "pane_id not provided and TMUX_PANE is not set"

# ── compute paths ────────────────────────────────────────────────────────────

HASH=$(project_hash "$PROJECT_PATH")
IDENTITY_DIR="${HOME}/.local/state/agent-mail/identity/${HASH}"
IDENTITY_FILE="${IDENTITY_DIR}/${PANE_ID}"
TIMESTAMP=$(epoch_now)

# ── atomic write ─────────────────────────────────────────────────────────────

mkdir -p "$IDENTITY_DIR" || die "failed to create directory: ${IDENTITY_DIR}"
chmod 700 "$IDENTITY_DIR" 2>/dev/null || true

TMPFILE="${IDENTITY_FILE}.tmp.$$"

# Ensure cleanup on any exit path.
cleanup() { rm -f "$TMPFILE" 2>/dev/null; }
trap cleanup EXIT INT TERM

printf '%s\n%s\n' "$AGENT_NAME" "$TIMESTAMP" > "$TMPFILE" \
    || die "failed to write temp file: ${TMPFILE}"

chmod 600 "$TMPFILE" 2>/dev/null || true

mv "$TMPFILE" "$IDENTITY_FILE" \
    || die "failed to rename temp file to: ${IDENTITY_FILE}"

# Clear the trap (file already moved).
trap - EXIT INT TERM

printf '%s\n' "$IDENTITY_FILE"
