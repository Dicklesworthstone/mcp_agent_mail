#!/usr/bin/env bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_LAUNCHER_PATH="$0"

MCP_PRODUCT_NAME="Claude"
MCP_CLI_BIN="claude"

# Support --dry-run as an alias for --test
args=()
for arg in "$@"; do
    if [[ "$arg" == "--dry-run" ]]; then
        args+=("--test")
    else
        args+=("$arg")
    fi
done

# Location-aware sourcing: works from both root and scripts/ directory
if [[ -f "$SCRIPT_DIR/mcp_common.sh" ]]; then
    source "$SCRIPT_DIR/mcp_common.sh" "${args[@]}"
elif [[ -f "$SCRIPT_DIR/scripts/mcp_common.sh" ]]; then
    source "$SCRIPT_DIR/scripts/mcp_common.sh" "${args[@]}"
else
    echo "❌ Error: Cannot find mcp_common.sh" >&2
    exit 1
fi
exit $?
