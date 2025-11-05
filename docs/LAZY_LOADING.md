# Lazy Loading MCP Tools

## Overview

MCP Agent Mail exposes 27 tools consuming ~25k tokens by default. This "lazy loading" feature allows you to reduce context usage to ~9k tokens by only exposing core coordination tools.

## Tool Categories

### Core Tools (8 tools, ~9k tokens)
Always exposed, essential for basic coordination:
- `health_check` - Server readiness check
- `ensure_project` - Create/verify project  
- `register_agent` - Register agent identity
- `whois` - Get agent profile info
- `send_message` - Send markdown messages
- `reply_message` - Reply to messages
- `fetch_inbox` - Get recent messages
- `mark_message_read` - Mark messages as read

### Extended Tools (19 tools, ~16k tokens)
Available via meta-tools when needed:
- **Messaging**: `acknowledge_message`
- **Search**: `search_messages`, `summarize_thread`, `summarize_threads`
- **Identity**: `create_agent_identity`
- **Contacts**: `request_contact`, `respond_contact`, `list_contacts`, `set_contact_policy`
- **File Reservations**: `file_reservation_paths`, `release_file_reservations`, `force_release_file_reservation`, `renew_file_reservations`
- **Macros**: `macro_start_session`, `macro_prepare_thread`, `macro_file_reservation_cycle`, `macro_contact_handshake`
- **Infrastructure**: `install_precommit_guard`, `uninstall_precommit_guard`

## Usage

### Enable Core Mode

Set environment variable before starting the server:

```bash
export MCP_TOOLS_MODE=core
./scripts/run_server_with_token.sh
```

Or in Claude Code settings:

```json
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp/",
      "env": {
        "MCP_TOOLS_MODE": "core"
      }
    }
  }
}
```

### Discover Extended Tools

```python
# List all available extended tools
result = await list_extended_tools()
# Returns: {"total_extended_tools": 19, "categories": {...}, "tools": [...]}
```

### Invoke Extended Tools

```python
# Reserve file paths using extended tool
result = await call_extended_tool(
    tool_name="file_reservation_paths",
    arguments={
        "project_key": "/abs/path/project",
        "agent_name": "BlueLake",
        "paths": ["src/**/*.py"],
        "ttl_seconds": 3600,
        "exclusive": True
    }
)
```

## Context Savings

| Mode | Tools Exposed | Approx Tokens | Savings |
|------|--------------|---------------|---------|
| Extended (default) | 27 tools | ~25k | - |
| Core | 8 core + 2 meta | ~10k | 60% |

## Meta-Tools

Two new tools enable lazy loading:

1. **`list_extended_tools`** - Discover available extended tools
   - Returns metadata for all extended tools
   - Groups by category (messaging, file_reservations, etc.)
   - No arguments required

2. **`call_extended_tool`** - Invoke extended tool dynamically
   - Parameters: `tool_name` (string), `arguments` (dict)
   - Provides same functionality as direct tool call
   - Returns result from invoked tool

## Backward Compatibility

- Default mode is "extended" (all tools exposed)
- No breaking changes to existing clients
- Extended mode identical to previous behavior
- Core mode is opt-in via environment variable

## Implementation Status

**Current Status**: Partial implementation
- ✅ Tool categorization defined
- ✅ Meta-tools implemented
- ✅ Environment variable support
- ⚠️  Conditional registration (requires FastMCP enhancement)

**Limitations**: 
- All tools are currently registered with FastMCP regardless of mode
- Context savings require FastMCP support for conditional tool registration
- Meta-tools work but extended tools still appear in tools/list

**Future Work**:
- Implement runtime tool filtering
- Add FastMCP hook for conditional registration  
- Create integration tests for both modes

## Related Issues

- GitHub anthropics/claude-code#7336 - Lazy Loading Feature Request
- Community proof-of-concept: github.com/machjesusmoto/claude-lazy-loading

