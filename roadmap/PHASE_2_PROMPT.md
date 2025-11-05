# Phase 2: Meta-Tools Implementation - Copy/Paste Prompt

Copy the text below and paste it into a new Claude Code session to continue the lazy loading implementation:

---

## Context

I'm implementing Phase 2 of the MCP Agent Mail lazy loading feature. Phase 1 (tool categorization foundation) is complete and merged. I need to implement meta-tools for dynamic tool discovery and invocation.

## Current State

**Branch**: `feature/lazy-load-mcp-tools` (Phase 1 complete)
**Latest commits**:
- `eda48e5` - Documentation
- `6d0e3c3` - Tool categorization constants

**Files modified**:
- `src/mcp_agent_mail/app.py`: Added CORE_TOOLS, EXTENDED_TOOLS, EXTENDED_TOOL_METADATA constants
- `docs/LAZY_LOADING.md`: Comprehensive documentation
- `roadmap/LAZY_LOADING_ROADMAP.md`: Implementation roadmap

**What exists**:
- ✅ `CORE_TOOLS` constant (8 tools)
- ✅ `EXTENDED_TOOLS` constant (19 tools)  
- ✅ `EXTENDED_TOOL_METADATA` with categories and descriptions
- ✅ `_EXTENDED_TOOL_REGISTRY = {}` (empty placeholder)

**What's needed**:
- ❌ `list_extended_tools` tool implementation
- ❌ `call_extended_tool` tool implementation
- ❌ Registry population logic
- ❌ Integration tests

## Task: Implement Phase 2 Meta-Tools

Please implement Phase 2 of the lazy loading feature according to the roadmap in `roadmap/LAZY_LOADING_ROADMAP.md`.

### Specific Tasks

1. **Create new branch from current feature branch**:
   ```bash
   git checkout feature/lazy-load-mcp-tools
   git pull
   git checkout -b feature/lazy-load-phase2-meta-tools
   ```

2. **Implement `list_extended_tools` tool**:
   - Location: `src/mcp_agent_mail/app.py` after `health_check` tool (~line 2243)
   - Returns metadata for all 19 extended tools
   - Groups by category (messaging, search, contacts, etc.)
   - See roadmap section 2.1 for complete implementation

3. **Populate `_EXTENDED_TOOL_REGISTRY`**:
   - Use Option B (post-registration) from roadmap section 2.2
   - Add registry population after all tool definitions in `build_mcp_server()`
   - Register all 19 extended tools

4. **Implement `call_extended_tool` tool**:
   - Location: After `list_extended_tools`
   - Validates tool_name is in EXTENDED_TOOLS
   - Retrieves function from `_EXTENDED_TOOL_REGISTRY`
   - Invokes with provided arguments
   - See roadmap section 2.3 for complete implementation

5. **Add environment variable support**:
   - File: `src/mcp_agent_mail/config.py`
   - Add `tools_mode: str` to Settings dataclass
   - Add to get_settings(): `tools_mode=_decouple_config("MCP_TOOLS_MODE", default="extended").lower()`
   - See roadmap section 2.4

6. **Create integration tests**:
   - File: `tests/test_lazy_loading.py` (new)
   - Test `list_extended_tools` returns 19 tools
   - Test `call_extended_tool` invokes successfully
   - Test error handling for invalid tool names
   - See roadmap section 2.5 for test templates

7. **Update documentation**:
   - File: `docs/LAZY_LOADING.md`
   - Mark meta-tools as implemented
   - Update implementation status
   - See roadmap section 2.6

8. **Test everything**:
   ```bash
   # Run tests
   pytest tests/test_lazy_loading.py -v
   
   # Restart server
   ./scripts/run_server_with_token.sh
   
   # Verify no errors in logs
   tail -50 /tmp/mcp_agent_mail_server.log
   ```

9. **Create PR**:
   - Commit changes with descriptive message
   - Push to new branch
   - Create PR against `feature/lazy-load-mcp-tools`
   - Link to Phase 1 PR #3

## Important Notes

- **DO NOT** implement conditional registration (that's Phase 3)
- **DO** keep all 27 tools registered (no context savings yet)
- **DO** use the code templates in the roadmap
- **DO** follow existing code patterns in app.py
- **DO** add comprehensive error handling
- **DO NOT** modify the Phase 1 constants (CORE_TOOLS, EXTENDED_TOOLS)

## Success Criteria

- [ ] `list_extended_tools` tool works and returns 19 tools
- [ ] `call_extended_tool` can invoke any extended tool
- [ ] All 19 extended tools in `_EXTENDED_TOOL_REGISTRY`
- [ ] Integration tests pass
- [ ] Server starts without errors
- [ ] No breaking changes to existing functionality
- [ ] Documentation updated

## Reference Files

- Roadmap: `roadmap/LAZY_LOADING_ROADMAP.md`
- Documentation: `docs/LAZY_LOADING.md`
- Main implementation: `src/mcp_agent_mail/app.py`
- Current constants start at line ~1949

## Questions?

If you need clarification:
1. Check the roadmap first
2. Look at existing tool implementations (e.g., `health_check`, `ensure_project`)
3. Review Phase 1 PR #3 for context
4. Ask specific questions about implementation details

## Expected Time

Estimated 4-6 hours for complete implementation and testing.

---

After completing Phase 2, we'll move to Phase 3 (Conditional Registration) which will actually achieve the 60% context reduction.

Let's implement Phase 2!
