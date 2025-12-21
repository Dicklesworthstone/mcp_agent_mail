# Claude Code Integration Guide

This guide describes how to integrate `mcp-agent-mail` with Claude Code in a robust, high-performance, and multi-agent friendly way.

## Why this approach?

The default integration scripts may cause:
1.  **Slow Startup**: Repeated Python startup and framework loading can delay every hook by seconds.
2.  **Concurrency Locks**: Conflicts between the MCP server and hooks over the SQLite database.
3.  **Identity Confusion**: Hardcoded agent names prevent running multiple simulated agents on the same machine.

This "advanced" integration solves these issues by:
- Using `stdio` transport for the MCP server (managed by Claude Code).
- Using optimized, dependency-free scripts for hooks (<20ms execution).
- Isolating the database per project.
- Supporting dynamic identity via environment variables.

## 1. Installation

Install the package as a development dependency in your project:

```bash
uv add --dev mcp-agent-mail
```
*(Or via git URL if not yet on PyPI)*

## 2. Scripts Setup

Copy the scripts from `scripts/integration/` in this repository to your project's `scripts/` folder:
- `mcp_server_stdio.py`: Adapter to launch the server in stdio mode.
- `mcp_session_start.py`: Optimized session start hook (auto-register, UI launch).
- `fast_check.py`: High-performance hook for file locking and notifications.

## 3. Configuration

Create `.mcp.json` in your project root:
```json
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--dev",
        "python",
        "scripts/mcp_server_stdio.py"
      ],
      "env": {
        "DATABASE_URL": "sqlite+aiosqlite:///./.claude/mcp_mail.db"
      }
    }
  }
}
```

## 4. Hooks Configuration

Configure your hooks in `.claude/settings.local.json` to use the optimized scripts:

```json
{
  "env": {
    "AGENT_NAME": "YourAgentName"
  },
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "DATABASE_URL='sqlite+aiosqlite:///./.claude/mcp_mail.db' uv run --dev python scripts/mcp_session_start.py $(pwd)" }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Edit",
        "hooks": [
          { "type": "command", "command": ".venv/bin/python scripts/fast_check.py .claude/mcp_mail.db $(pwd) $AGENT_NAME" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": ".venv/bin/python scripts/fast_check.py .claude/mcp_mail.db $(pwd) $AGENT_NAME --notify-only" }
        ]
      }
    ]
  }
}
```

## 5. Web UI

The `SessionStart` hook will automatically launch the Web UI if port 8765 is free. Access it at:
`http://127.0.0.1:8765/mail/`
