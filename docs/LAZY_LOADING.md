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

**File Reservations**: `file_reservation_paths`, `release_file_reservations`, `force_release_file_reservation`, `renew_file_reservations`

**Macros**: `macro_start_session`, `macro_prepare_thread`, `macro_file_reservation_cycle`

**Infrastructure**: `install_precommit_guard`, `uninstall_precommit_guard`

## Current Implementation

### What's Included (v1 - Foundation)

✅ **Tool Categorization**: Constants define core vs extended tools
✅ **Metadata**: Each extended tool has category and description
✅ **Registry Placeholder**: `_EXTENDED_TOOL_REGISTRY` prepared for future use
✅ **Zero Breaking Changes**: All 27 tools remain functional

### What's Not Yet Implemented

⚠️ **Meta-Tools**: `list_extended_tools` and `call_extended_tool` (future)
⚠️ **Environment Variable**: `MCP_TOOLS_MODE=core` support (future)
⚠️ **Conditional Registration**: Runtime tool filtering (future)
⚠️ **Context Savings**: Requires meta-tools + conditional registration

## Context Reduction Potential

| Mode | Tools Exposed | Approx Tokens | Savings |
|------|--------------|---------------|---------|
| Extended (current) | 27 tools | ~25k | - |
| Core (future) | 8 core + 2 meta | ~10k | **60%** |

## Roadmap

### Phase 1: Foundation (Current)
- ✅ Tool categorization constants
- ✅ Metadata for discovery
- ✅ Registry placeholder

### Phase 2: Meta-Tools (Next)
- [ ] Implement `list_extended_tools`
- [ ] Implement `call_extended_tool`
- [ ] Add environment variable support
- [ ] Integration tests

### Phase 3: Runtime Filtering (Future)
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

---

**Status**: Foundation complete, meta-tools pending
**Risk**: Zero (additive only, no behavior changes)
**Impact**: Documents categorization, enables future work
