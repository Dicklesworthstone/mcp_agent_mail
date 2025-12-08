#!/usr/bin/env python3
"""
Disable non-essential MCP tools in Agent Mail.

Reduces tool count from 35 to 12 essential tools by commenting out:
- Contact management (5 tools)
- Macros/convenience (4 tools)
- Viewer features (3 tools)
- Setup/guards (2 tools)
- Build slots (3 tools)
- Product bus (5 tools)
- Advanced/rare (1 tool)

Usage:
    python scripts/disable_nonessential_tools.py          # Preview changes
    python scripts/disable_nonessential_tools.py --apply  # Apply changes
"""

import re
from pathlib import Path

# Tools to disable (line numbers where @mcp.tool appears)
TOOLS_TO_DISABLE = {
    2763: "create_agent_identity",
    3927: "request_contact",
    4059: "respond_contact",
    4119: "list_contacts",
    4148: "set_contact_policy",
    4237: "mark_message_read",
    4386: "macro_start_session",
    4448: "macro_prepare_thread",
    4510: "macro_file_reservation_cycle",
    4562: "macro_contact_handshake",
    4815: "summarize_thread",
    4865: "summarize_threads",
    5002: "install_precommit_guard",
    5028: "uninstall_precommit_guard",
    5313: "force_release_file_reservation",
    5636: "acquire_build_slot",
    5684: "renew_build_slot",
    5716: "release_build_slot",
    5797: "ensure_product",
    5838: "products_link",
    5932: "search_messages_product",
    5994: "fetch_inbox_product",
    6042: "summarize_thread_product",
}

def find_tool_function_range(lines: list[str], start_line: int) -> tuple[int, int]:
    """Find the start and end of a tool function definition."""
    # Find where the function definition starts (line with @mcp.tool)
    decorator_line = start_line - 1  # Convert to 0-indexed

    # Find the next @mcp.tool or end of file
    function_start = decorator_line
    function_end = len(lines)

    # Look for the start of the next function (next @mcp.tool or final return mcp)
    for i in range(decorator_line + 1, len(lines)):
        line = lines[i].strip()
        
        # Stop if we hit the next tool
        if line.startswith("@mcp.tool("):
            function_end = i
            break
        
        # CRITICAL: Stop before the final 'return mcp' statement (end of build_mcp_server)
        if line == "return mcp":
            function_end = i
            break

    return function_start, function_end

def disable_tools(app_py_path: Path, apply: bool = False) -> None:
    """Disable non-essential tools by commenting them out."""

    print(f"Reading {app_py_path}...")
    lines = app_py_path.read_text().splitlines(keepends=True)

    if not apply:
        print("\n🔍 PREVIEW MODE - No changes will be made")
        print("   Run with --apply to actually modify the file\n")

    modified_lines = lines.copy()
    total_lines_commented = 0

    for line_num, tool_name in sorted(TOOLS_TO_DISABLE.items()):
        start, end = find_tool_function_range(lines, line_num)

        # Check if already disabled
        if lines[start].strip().startswith("# DISABLED:"):
            print(f"⏭️  Line {line_num:4d}: {tool_name:30s} (already disabled)")
            continue

        # Comment out the entire function
        lines_to_comment = end - start
        for i in range(start, end):
            if not modified_lines[i].startswith("# DISABLED:"):
                modified_lines[i] = f"# DISABLED: {modified_lines[i]}"
                total_lines_commented += 1

        print(f"{'✅' if apply else '🔄'} Line {line_num:4d}: {tool_name:30s} ({lines_to_comment} lines)")

    print(f"\n📊 Summary:")
    print(f"   Tools disabled: {len(TOOLS_TO_DISABLE)}")
    print(f"   Lines commented: {total_lines_commented}")
    print(f"   Reduction: {len(TOOLS_TO_DISABLE)} / 35 tools = {len(TOOLS_TO_DISABLE)/35*100:.0f}% fewer tools")

    if apply:
        # Create backup
        backup_path = app_py_path.with_suffix(".py.backup")
        print(f"\n💾 Creating backup: {backup_path}")
        backup_path.write_text(app_py_path.read_text())

        # Write modified file
        print(f"✍️  Writing changes to {app_py_path}")
        app_py_path.write_text("".join(modified_lines))
        print("✅ Done!")
    else:
        print("\n💡 To apply these changes, run:")
        print(f"   python {Path(__file__).name} --apply")

if __name__ == "__main__":
    import sys

    app_py = Path(__file__).parent.parent / "src" / "mcp_agent_mail" / "app.py"

    if not app_py.exists():
        print(f"❌ Error: Could not find {app_py}")
        sys.exit(1)

    apply = "--apply" in sys.argv

    disable_tools(app_py, apply=apply)
