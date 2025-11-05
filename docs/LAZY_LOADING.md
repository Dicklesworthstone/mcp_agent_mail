# Lazy Loading MCP Tools - Foundation

## Overview

MCP Agent Mail exposes 27 tools consuming ~25k tokens by default. This categorization provides the foundation for future lazy loading to reduce context usage to ~10k tokens.

## Tool Categories

### Core Tools (8 tools, ~9k tokens)
Essential for basic coordination:
- `health_check` - Server readiness check
- `ensure_project` - Create/verify project  
- `register_agent` - Register agent identity
- `whois` - Get agent profile info
- `send_message` - Send markdown messages
- `reply_message` - Reply to messages
- `fetch_inbox` - Get recent messages
- `mark_message_read` - Mark messages as read

### Extended Tools (19 tools, ~16k tokens)
Advanced features for specialized workflows:

**Messaging**: `acknowledge_message`

**Search**: `search_messages`, `summarize_thread`, `summarize_threads`

**Identity**: `create_agent_identity`

**Contacts**: `request_contact`, `respond_contact`, `list_contacts`, `set_contact_policy`

**File Reservations**: `file_reservation_paths`, `release_file_reservations`, `force_release_file_reservation`, `renew_file_reservations`

**Macros**: `macro_start_session`, `macro_prepare_thread`, `macro_file_reservation_cycle`, `macro_contact_handshake`

**Infrastructure**: `install_precommit_guard`, `uninstall_precommit_guard`

## Current Implementation

### What's Included (v2 - Meta-Tools)

✅ **Tool Categorization**: Constants define core vs extended tools
✅ **Metadata**: Each extended tool has category and description
✅ **Tool Registry**: Extended tools registered for dynamic invocation
✅ **Meta-Tools**: `list_extended_tools` and `call_extended_tool` functional
✅ **Environment Variable**: `MCP_TOOLS_MODE` support (behavior in Phase 3)
✅ **Zero Breaking Changes**: All 27 tools remain functional

### What's Not Yet Implemented

⚠️ **Conditional Registration**: Runtime tool filtering (future)
⚠️ **Context Savings**: Requires conditional registration (Phase 3)

## Context Reduction Potential

| Mode | Tools Exposed | Approx Tokens | Savings |
|------|--------------|---------------|---------|
| Extended (current) | 27 tools | ~25k | - |
| Core (future) | 8 core + 2 meta | ~10k | **60%** |

## Roadmap

### Phase 1: Foundation (Complete ✅)
- ✅ Tool categorization constants
- ✅ Metadata for discovery
- ✅ Registry placeholder

### Phase 2: Meta-Tools (Complete ✅)
- ✅ Implement `list_extended_tools`
- ✅ Implement `call_extended_tool`
- ✅ Add environment variable support
- ✅ Integration tests

### Phase 3: Runtime Filtering (Next)
- [ ] Conditional tool registration
- [ ] FastMCP enhancement or workaround
- [ ] Full context savings validation

## Design Decisions

**Why Constants First?**
- Documents the categorization
- Zero risk to production
- Enables gradual implementation
- Allows discussion before behavior changes

**Why These Categories?**
- Core = minimum viable agent coordination
- Extended = specialized/advanced workflows
- Categorization based on usage patterns from real deployments

**Why Not Filter Now?**
- Requires FastMCP runtime filtering or decorator refactoring
- Meta-tools provide value independently
- Foundation enables experimentation

## Related Work

- GitHub Issue: anthropics/claude-code#7336
- Community POC: github.com/machjesusmoto/claude-lazy-loading
- Discussion: Lazy loading as MCP protocol enhancement

## For Contributors

This foundation enables multiple implementation paths:

1. **Meta-Tool Approach**: Expose extended tools via proxy tools
2. **Decorator Refactoring**: Conditional `@mcp.tool` registration
3. **Post-Registration Filtering**: Remove tools after FastMCP init
4. **FastMCP Enhancement**: Runtime tool exposure control

The constants in `app.py` serve as the source of truth for all approaches.

## Using Meta-Tools

### List Available Extended Tools

```json
{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"list_extended_tools","arguments":{}}}
```

Returns all 19 extended tools with their categories and descriptions.

### Call an Extended Tool Dynamically

```json
{"jsonrpc":"2.0","id":"2","method":"tools/call","params":{"name":"call_extended_tool","arguments":{
  "tool_name": "list_contacts",
  "arguments": {
    "project_key": "/path/to/project",
    "agent_name": "AgentName"
  }
}}}
```

This allows programmatic access to extended tools without exposing them all in the tools list.

---

**Status**: Phase 2 complete (Meta-Tools functional)
**Risk**: Zero (additive only, no behavior changes)
**Impact**: Enables programmatic tool discovery and invocation, foundation for Phase 3
