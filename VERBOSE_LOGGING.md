# Comprehensive Rich Logging for MCP Agent Mail

This document describes the comprehensive, beautiful console logging system for MCP Agent Mail that provides full visibility into all agent tool calls and system operations.

## Overview

The Rich logging system provides:

- 🎨 **Beautiful formatting** using Rich library panels, tables, and syntax highlighting
- 📊 **Detailed tool call tracking** showing inputs, outputs, timing, and context
- 🔍 **Full visibility** into every MCP tool invocation
- ⚡ **Performance metrics** with precise duration measurements
- 🎯 **Context awareness** displaying agent and project information
- 🚨 **Error details** with comprehensive error panels and stack traces

## Quick Start

### Running with Verbose Logging

The easiest way to run the server with verbose logging enabled is to use the provided script:

```bash
./scripts/run_server_with_token.sh
```

This script automatically enables:
- ✅ Comprehensive tool call logging
- ✅ Rich formatting with colors and panels
- ✅ DEBUG log level for maximum detail
- ✅ HTTP request logging
- ✅ Beautiful startup banner with configuration

## Manual Configuration

You can enable verbose logging manually by setting these environment variables:

```bash
# Enable comprehensive Rich-based logging
export TOOLS_LOG_ENABLED=true
export LOG_RICH_ENABLED=true
export LOG_LEVEL=DEBUG
export LOG_JSON_ENABLED=false
export HTTP_REQUEST_LOG_ENABLED=true

# Then run the server
uv run python -m mcp_agent_mail.cli serve-http
```

Or add them to your `.env` file:

```ini
TOOLS_LOG_ENABLED=true
LOG_RICH_ENABLED=true
LOG_LEVEL=DEBUG
LOG_JSON_ENABLED=false
HTTP_REQUEST_LOG_ENABLED=true
```

## What You'll See

### 1. Startup Banner

When the server starts with verbose logging enabled, you'll see a comprehensive startup panel showing:

```
╭───────────────────────── Server Configuration ─────────────────────────╮
│ 🚀 MCP Agent Mail Server                                               │
│ ├── Server                                                             │
│ │   ├── Environment: development                                       │
│ │   ├── Endpoint: http://127.0.0.1:8765/mcp/                          │
│ │   ├── Database: sqlite+aiosqlite:///./storage.sqlite3               │
│ │   └── Storage: ./storage                                            │
│ ├── Logging                                                            │
│ │   ├── Tools Log: ENABLED                                            │
│ │   ├── Log Level: DEBUG                                              │
│ │   ├── Rich Enabled: yes                                             │
│ │   ├── JSON Format: no                                               │
│ │   └── Request Log: yes                                              │
│ ├── Security                                                           │
│ │   ├── Bearer Auth: ENABLED                                          │
│ │   ├── JWT Auth: disabled                                            │
│ │   ├── RBAC: ENABLED                                                 │
│ │   └── Localhost Bypass: yes                                         │
│ └── Features                                                           │
│     ├── Rate Limiting: disabled                                       │
│     ├── CORS: disabled                                                │
│     ├── OTEL: disabled                                                │
│     ├── LLM: ENABLED                                                  │
│     └── Claims Cleanup: disabled                                      │
╰────────────────────────────────────────────────────────────────────────╯
```

### 2. Tool Call Start

When an agent calls an MCP tool, you'll see a detailed panel showing:

```
╔══════════════════════════════════════════════════════════════════════╗
║                  🚀 MCP TOOL CALL STARTED                           ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  Tool Name        send_message                                       ║
║  Timestamp        2025-10-25 14:23:45.123                           ║
║  Project          /workspace/my-project                             ║
║  Agent            BlueLake                                          ║
║                                                                      ║
║  ╭─────────────────── Input Parameters ──────────────────────╮     ║
║  │ {                                                          │     ║
║  │   "project_key": "/workspace/my-project",                 │     ║
║  │   "sender_name": "BlueLake",                              │     ║
║  │   "to": ["GreenCastle"],                                  │     ║
║  │   "subject": "Plan for /api/users",                       │     ║
║  │   "body_md": "Let's refactor the user API...",            │     ║
║  │   "ack_required": false,                                  │     ║
║  │   "importance": "normal"                                  │     ║
║  │ }                                                          │     ║
║  ╰────────────────────────────────────────────────────────────╯     ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
```

### 3. Tool Call Completion

When the tool call completes, you'll see:

```
╔══════════════════════════════════════════════════════════════════════╗
║                  ✓ MCP TOOL CALL COMPLETED                          ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  ┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓   ║
║  ┃ Field          ┃ Value                                      ┃   ║
║  ┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩   ║
║  │ Tool           │ send_message                               │   ║
║  │ Agent          │ BlueLake                                   │   ║
║  │ Project        │ /workspace/my-project                      │   ║
║  │ Started        │ 2025-10-25 14:23:45.123                   │   ║
║  │ Duration       │ 142.35ms                                   │   ║
║  │ Status         │ ✓ SUCCESS                                  │   ║
║  └────────────────┴────────────────────────────────────────────┘   ║
║                                                                      ║
║  ╭────────────────────── Result ─────────────────────────╮         ║
║  │ {                                                      │         ║
║  │   "deliveries": [                                      │         ║
║  │     {                                                  │         ║
║  │       "project": "/workspace/my-project",              │         ║
║  │       "payload": {                                     │         ║
║  │         "id": 1234,                                    │         ║
║  │         "subject": "Plan for /api/users",              │         ║
║  │         "from": "BlueLake",                            │         ║
║  │         "to": ["GreenCastle"],                         │         ║
║  │         "created_ts": "2025-10-25T14:23:45.123Z"       │         ║
║  │       }                                                │         ║
║  │     }                                                  │         ║
║  │   ],                                                   │         ║
║  │   "count": 1                                           │         ║
║  │ }                                                      │         ║
║  ╰────────────────────────────────────────────────────────╯         ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
```

### 4. Error Display

If a tool call fails, you'll see detailed error information:

```
╔══════════════════════════════════════════════════════════════════════╗
║                  ✗ MCP TOOL CALL FAILED                             ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║  ┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓   ║
║  ┃ Field          ┃ Value                                      ┃   ║
║  ┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩   ║
║  │ Tool           │ send_message                               │   ║
║  │ Agent          │ BlueLake                                   │   ║
║  │ Project        │ /workspace/my-project                      │   ║
║  │ Started        │ 2025-10-25 14:23:45.123                   │   ║
║  │ Duration       │ 45.67ms                                    │   ║
║  │ Status         │ ✗ FAILED                                   │   ║
║  │ Error          │ Agent 'BlueLake' not registered            │   ║
║  └────────────────┴────────────────────────────────────────────┘   ║
║                                                                      ║
║  ╭────────────────── Error Details ──────────────────────╮         ║
║  │ {                                                      │         ║
║  │   "error_type": "ToolExecutionError",                  │         ║
║  │   "error_message": "Agent 'BlueLake' not registered    │         ║
║  │                    for project",                       │         ║
║  │   "error_code": "NOT_FOUND",                           │         ║
║  │   "error_data": {                                      │         ║
║  │     "tool": "send_message"                             │         ║
║  │   }                                                    │         ║
║  │ }                                                      │         ║
║  ╰────────────────────────────────────────────────────────╯         ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
```

## Features

### Syntax Highlighting

All JSON parameters and results are syntax-highlighted using the Monokai theme for easy reading:
- 🔵 Keys are highlighted
- 🟢 Strings are green
- 🟡 Numbers are yellow
- 🟣 Booleans and null are purple

### Duration Color Coding

Tool call durations are color-coded for quick performance assessment:
- 🟢 **Green**: < 100ms (fast)
- 🟡 **Yellow**: 100ms - 1000ms (moderate)
- 🔴 **Red**: > 1000ms (slow)

### Context Awareness

Every tool call shows:
- **Tool name**: The MCP tool being invoked
- **Agent**: Which agent is calling the tool
- **Project**: Which project context
- **Timestamp**: Exact time of invocation
- **Duration**: How long the tool took to execute
- **Status**: Success or failure

### Automatic Truncation

Large outputs are automatically truncated to 2000 characters to prevent overwhelming the console, with a clear "(truncated)" indicator.

### Secure Parameter Display

Sensitive parameters (containing "token", "secret", "password") are automatically masked in startup configuration displays.

## Configuration Options

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TOOLS_LOG_ENABLED` | `false` | Enable comprehensive tool call logging |
| `LOG_RICH_ENABLED` | `true` | Enable Rich library formatting |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `LOG_JSON_ENABLED` | `false` | Output logs in JSON format (disables Rich) |
| `HTTP_REQUEST_LOG_ENABLED` | `false` | Enable HTTP request/response logging |

### Disabling Verbose Logging

To run the server without verbose logging:

```bash
export TOOLS_LOG_ENABLED=false
uv run python -m mcp_agent_mail.cli serve-http
```

Or simply use a different startup script without the logging environment variables.

## Performance Impact

The Rich logging system is designed to have minimal performance impact:

- Logging is only enabled when `TOOLS_LOG_ENABLED=true`
- All logging happens asynchronously to stderr
- Large outputs are automatically truncated
- No logging occurs in production unless explicitly enabled

## Troubleshooting

### Rich library not installed

If you see errors about the Rich library not being available, install it:

```bash
uv pip install rich
```

### Logging not appearing

Check that:
1. `TOOLS_LOG_ENABLED=true` is set
2. You're running via the correct script: `./scripts/run_server_with_token.sh`
3. Your terminal supports ANSI color codes
4. Output is not being redirected (logging goes to stderr)

### Console width issues

If panels are too wide or narrow, you can set the width in the code or use:

```bash
export COLUMNS=120  # Set terminal width
```

## Integration with Other Tools

### Viewing Logs

Since logs go to stderr, you can redirect them:

```bash
# Save logs to file
./scripts/run_server_with_token.sh 2> logs.txt

# View logs with less
./scripts/run_server_with_token.sh 2>&1 | less -R
```

### CI/CD Environments

In CI/CD, you may want to disable Rich formatting:

```bash
export LOG_RICH_ENABLED=false
export LOG_JSON_ENABLED=true  # Structured logs for parsing
```

## Examples

### Typical Tool Call Flow

1. Agent makes a call to `send_message`
2. Start panel appears with all input parameters
3. Tool executes
4. Completion panel shows result and timing
5. Next tool call begins...

### Monitoring Performance

Watch the duration field to identify slow operations:
- Most tools should complete in < 100ms
- Database-heavy operations may take 100-500ms
- Operations involving Git commits may take 500ms-2s
- LLM operations may take 2-10s

### Debugging Errors

When a tool fails:
1. Check the error type (e.g., `NOT_FOUND`, `VALIDATION_ERROR`)
2. Read the error message for details
3. Examine the input parameters to identify issues
4. Check the error_data field for additional context

## Advanced Usage

### Programmatic Logging

You can use the rich_logger module in your own code:

```python
from mcp_agent_mail import rich_logger

# Log an info message
rich_logger.log_info("Agent registered successfully", agent="BlueLake")

# Log an error
rich_logger.log_error("Failed to send message", error=exception, details={"count": 5})

# Log a success
rich_logger.log_success("Message delivered", message_id=1234)

# Use context manager for tool calls
with rich_logger.tool_call_logger(
    tool_name="custom_tool",
    kwargs={"param1": "value1"},
    agent="BlueLake",
    project="/workspace/proj"
):
    # Your tool logic here
    result = do_something()
```

## Future Enhancements

Potential future additions:
- 📈 Real-time performance graphs
- 📊 Aggregated statistics panel
- 🔔 Alert highlighting for slow operations
- 🎯 Filtering by agent/project/tool
- 💾 Log replay capabilities
- 🔍 Search through logged tool calls

## Support

For issues or questions about verbose logging:
- Check this documentation
- Review the source in `src/mcp_agent_mail/rich_logger.py`
- File an issue in the project repository
