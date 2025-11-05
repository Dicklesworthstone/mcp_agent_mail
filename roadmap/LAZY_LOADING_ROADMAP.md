# Lazy Loading Implementation Roadmap

## Status: Phase 1 Complete ✅

**Current State**: Tool categorization constants defined, documentation complete
**Next Phase**: Implement meta-tools for dynamic tool discovery and invocation

---

## Phase 1: Foundation (COMPLETE) ✅

**Goal**: Document tool categorization and establish constants
**Status**: Complete (PR #3)

### Completed Items
- ✅ Define `CORE_TOOLS` constant (8 tools)
- ✅ Define `EXTENDED_TOOLS` constant (19 tools)
- ✅ Create `EXTENDED_TOOL_METADATA` with categories and descriptions
- ✅ Add `_EXTENDED_TOOL_REGISTRY` placeholder
- ✅ Write comprehensive documentation (`docs/LAZY_LOADING.md`)
- ✅ Validate zero breaking changes
- ✅ Test server startup with new constants

### Deliverables
- `src/mcp_agent_mail/app.py`: +63 lines (constants only)
- `docs/LAZY_LOADING.md`: Complete guide
- Server validated and running

**Context Reduction**: 0% (foundation only, no behavior changes)

---

## Phase 2: Meta-Tools (NEXT) 🎯

**Goal**: Enable dynamic discovery and invocation of extended tools
**Estimated Effort**: 4-6 hours
**Context Reduction**: 0% (tools still exposed, but programmatic access enabled)

### Tasks

#### 2.1: Implement `list_extended_tools` Tool
**File**: `src/mcp_agent_mail/app.py`
**Location**: After `health_check` tool definition (~line 2243)

```python
@mcp.tool(name="list_extended_tools")
@_instrument_tool("list_extended_tools", cluster=CLUSTER_SETUP, capabilities={"discovery"}, complexity="low")
async def list_extended_tools(ctx: Context) -> dict[str, Any]:
    """
    List all extended tools with metadata.
    
    Returns
    -------
    dict
        {
          "total": int,
          "by_category": dict[str, list[str]],
          "tools": list[dict] with name, category, description
        }
    """
    await ctx.info("Listing extended tools")
    
    by_category: dict[str, list[str]] = {}
    tools_list = []
    
    for tool_name in sorted(EXTENDED_TOOLS):
        metadata = EXTENDED_TOOL_METADATA.get(tool_name, {})
        category = metadata.get("category", "uncategorized")
        description = metadata.get("description", "")
        
        by_category.setdefault(category, []).append(tool_name)
        tools_list.append({
            "name": tool_name,
            "category": category,
            "description": description
        })
    
    return {
        "total": len(EXTENDED_TOOLS),
        "by_category": by_category,
        "tools": tools_list
    }
```

**Tests Required**:
- Returns correct count (19 tools)
- Categories match EXTENDED_TOOL_METADATA
- All tools have valid metadata

#### 2.2: Implement Tool Registry Population
**File**: `src/mcp_agent_mail/app.py`
**Strategy**: Add registration logic for extended tools

**Option A**: Decorator wrapper (cleaner, requires refactoring)
```python
def _maybe_register_extended(func, tool_name: str):
    """Register extended tools in the registry for dynamic invocation."""
    if tool_name in EXTENDED_TOOLS:
        _EXTENDED_TOOL_REGISTRY[tool_name] = func
    return func
```

Apply to each extended tool:
```python
@mcp.tool(name="acknowledge_message")
@_instrument_tool("acknowledge_message", ...)
async def acknowledge_message(...):
    ...

# After definition, register it
_EXTENDED_TOOL_REGISTRY["acknowledge_message"] = acknowledge_message
```

**Option B**: Post-registration (simpler, works immediately)

After all tool definitions in `build_mcp_server()`:
```python
# Populate extended tool registry after all tools are defined
_EXTENDED_TOOL_REGISTRY.update({
    "acknowledge_message": acknowledge_message,
    "create_agent_identity": create_agent_identity,
    "search_messages": search_messages,
    # ... all extended tools
})
```

**Recommended**: Option B for Phase 2, refactor to Option A in Phase 3

#### 2.3: Implement `call_extended_tool` Tool
**File**: `src/mcp_agent_mail/app.py`
**Location**: After `list_extended_tools`

```python
@mcp.tool(name="call_extended_tool")
@_instrument_tool("call_extended_tool", cluster=CLUSTER_SETUP, capabilities={"proxy"}, complexity="medium")
async def call_extended_tool(ctx: Context, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Dynamically invoke an extended tool by name.
    
    Parameters
    ----------
    tool_name : str
        Name of extended tool (e.g., "file_reservation_paths")
    arguments : dict
        Tool-specific arguments
        
    Returns
    -------
    dict
        Result from invoked tool
        
    Raises
    ------
    ValueError
        If tool_name not in EXTENDED_TOOLS
    RuntimeError
        If tool not registered (internal error)
    """
    if tool_name not in EXTENDED_TOOLS:
        raise ValueError(
            f"Unknown extended tool: {tool_name}. "
            f"Use list_extended_tools to see available options."
        )
    
    tool_func = _EXTENDED_TOOL_REGISTRY.get(tool_name)
    if not tool_func:
        raise RuntimeError(
            f"Extended tool {tool_name} is not registered. "
            f"This is an internal server error."
        )
    
    await ctx.info(f"Invoking extended tool: {tool_name}")
    
    try:
        result = await tool_func(ctx, **arguments)
        return result
    except TypeError as e:
        # Invalid arguments
        raise ValueError(
            f"Invalid arguments for {tool_name}: {str(e)}"
        ) from e
```

**Tests Required**:
- Validates tool_name in EXTENDED_TOOLS
- Successfully invokes registered tools
- Passes arguments correctly
- Error handling for invalid arguments
- Returns results from invoked tool

#### 2.4: Add Environment Variable Support
**File**: `src/mcp_agent_mail/config.py`

Add to `Settings` dataclass:
```python
# Tool exposure mode: "extended" (all tools, ~25k tokens) | "core" (minimal tools, ~10k tokens)
tools_mode: str
```

Add to `get_settings()`:
```python
tools_mode=_decouple_config("MCP_TOOLS_MODE", default="extended").lower(),
```

**File**: `src/mcp_agent_mail/app.py`

Use in `build_mcp_server()`:
```python
def build_mcp_server() -> FastMCP:
    settings: Settings = get_settings()
    tools_mode = settings.tools_mode  # "extended" or "core"
    
    # Will be used in Phase 3 for conditional registration
```

#### 2.5: Integration Tests
**File**: `tests/test_lazy_loading.py` (new file)

```python
"""Tests for lazy loading meta-tools."""

import pytest
from mcp_agent_mail.app import build_mcp_server, CORE_TOOLS, EXTENDED_TOOLS

@pytest.mark.asyncio
async def test_list_extended_tools():
    """Test list_extended_tools returns correct metadata."""
    mcp = build_mcp_server()
    # Call list_extended_tools
    # Assert returns 19 tools
    # Assert all have valid categories

@pytest.mark.asyncio
async def test_call_extended_tool_valid():
    """Test calling a valid extended tool."""
    mcp = build_mcp_server()
    # Call call_extended_tool with acknowledge_message
    # Assert succeeds

@pytest.mark.asyncio
async def test_call_extended_tool_invalid():
    """Test calling invalid tool raises ValueError."""
    mcp = build_mcp_server()
    # Call call_extended_tool with "fake_tool"
    # Assert raises ValueError

@pytest.mark.asyncio
async def test_extended_tool_registry_populated():
    """Test all extended tools are in registry."""
    from mcp_agent_mail.app import _EXTENDED_TOOL_REGISTRY
    assert len(_EXTENDED_TOOL_REGISTRY) == len(EXTENDED_TOOLS)
    for tool_name in EXTENDED_TOOLS:
        assert tool_name in _EXTENDED_TOOL_REGISTRY
```

#### 2.6: Update Documentation
**File**: `docs/LAZY_LOADING.md`

Update implementation status:
```markdown
## Current Implementation

### What's Included (v2 - Meta-Tools)

✅ **Tool Categorization**: Constants define core vs extended tools
✅ **Metadata**: Each extended tool has category and description
✅ **Tool Registry**: Extended tools registered for dynamic invocation
✅ **Meta-Tools**: `list_extended_tools` and `call_extended_tool` functional
✅ **Environment Variable**: `MCP_TOOLS_MODE` support (behavior in Phase 3)
```

### Deliverables (Phase 2)
- `list_extended_tools` tool implemented
- `call_extended_tool` tool implemented
- `_EXTENDED_TOOL_REGISTRY` populated
- Environment variable support added
- Integration tests passing
- Documentation updated

**Context Reduction**: Still 0% (all tools exposed, but meta-tools provide programmatic access)

---

## Phase 3: Conditional Registration (FUTURE) 🔮

**Goal**: Actually filter tools based on `MCP_TOOLS_MODE` to achieve context savings
**Estimated Effort**: 8-12 hours (requires FastMCP investigation)
**Context Reduction**: 60% when `MCP_TOOLS_MODE=core`

### Research Phase (2-3 hours)

#### 3.1: Investigate FastMCP Tool Registration
**Questions**:
- Can tools be registered conditionally?
- Can tools be unregistered after FastMCP initialization?
- Does FastMCP support runtime tool filtering?
- What's the FastMCP tools/list implementation?

**Files to examine**:
- FastMCP source code (external dependency)
- MCP protocol specification
- FastMCP documentation

**Approaches to evaluate**:

**Option A**: Conditional decorator (cleanest, may not work)
```python
def build_mcp_server() -> FastMCP:
    settings = get_settings()
    mcp = FastMCP(...)
    
    # Only register core tools in core mode
    if settings.tools_mode == "core":
        # Register only CORE_TOOLS + meta-tools
        pass
    else:
        # Register all tools (current behavior)
        pass
```

**Option B**: Post-registration filtering (hack, may work)
```python
def build_mcp_server() -> FastMCP:
    mcp = FastMCP(...)
    
    # Register all tools normally
    ... all @mcp.tool decorators ...
    
    # If core mode, remove extended tools from MCP's registry
    if settings.tools_mode == "core":
        for tool_name in EXTENDED_TOOLS:
            # Remove from mcp._tools or similar
            pass
```

**Option C**: FastMCP enhancement (best, requires external work)
```python
# Contribute to FastMCP:
# Add `mcp.conditional_tool(condition=lambda: settings.tools_mode == "extended")`
```

### Implementation Phase (4-6 hours)

#### 3.2: Implement Chosen Approach
Based on research findings, implement conditional registration

#### 3.3: Validate Context Savings
**Test**:
```bash
# Start in core mode
export MCP_TOOLS_MODE=core
./scripts/run_server_with_token.sh

# Verify only 10 tools exposed via tools/list
# Measure actual token count
```

**Expected**:
- Extended mode: 27 tools, ~25k tokens
- Core mode: 8 core + 2 meta = 10 tools, ~10k tokens

#### 3.4: Update Tests
```python
@pytest.mark.parametrize("tools_mode", ["extended", "core"])
async def test_tools_mode_registration(tools_mode, monkeypatch):
    """Test correct tools registered based on mode."""
    monkeypatch.setenv("MCP_TOOLS_MODE", tools_mode)
    clear_settings_cache()
    
    mcp = build_mcp_server()
    tools_list = get_tools_list(mcp)  # Helper to call tools/list
    
    if tools_mode == "core":
        assert len(tools_list) == 10
        for tool in tools_list:
            assert tool["name"] in CORE_TOOLS or tool["name"] in ["list_extended_tools", "call_extended_tool"]
    else:
        assert len(tools_list) == 27
```

### Deliverables (Phase 3)
- Conditional registration working
- Context savings validated (60% reduction)
- Tests for both modes
- Performance benchmarks
- Migration guide for users

---

## Phase 4: Polish & Optimization (OPTIONAL) ✨

### 4.1: Enhanced Discovery
- Add tool search/filtering in `list_extended_tools`
- Tool usage examples in metadata
- Dependency tracking (which extended tools depend on others)

### 4.2: Caching & Performance
- Cache extended tool metadata
- Optimize registry lookups
- Lazy-load tool documentation

### 4.3: Monitoring
- Track meta-tool usage metrics
- Log which extended tools are actually used
- Suggest tool mode based on usage patterns

### 4.4: Documentation
- Video walkthrough
- Migration examples
- FAQ for common issues

---

## Success Metrics

### Phase 2 Success Criteria
- [ ] `list_extended_tools` returns all 19 extended tools
- [ ] `call_extended_tool` can invoke any extended tool
- [ ] All extended tools in `_EXTENDED_TOOL_REGISTRY`
- [ ] Zero errors when calling meta-tools
- [ ] Integration tests pass
- [ ] Documentation updated

### Phase 3 Success Criteria
- [ ] `MCP_TOOLS_MODE=core` exposes only 10 tools
- [ ] Context usage drops from ~25k to ~10k tokens
- [ ] All core tools functional in core mode
- [ ] Extended tools accessible via `call_extended_tool`
- [ ] No breaking changes for existing users
- [ ] Performance benchmarks validate savings

### Phase 4 Success Criteria
- [ ] User adoption of core mode > 25%
- [ ] Average context savings > 50% for core mode users
- [ ] Zero reported bugs related to lazy loading
- [ ] Community contribution to FastMCP (if needed)

---

## Risk Mitigation

### Technical Risks
- **FastMCP doesn't support conditional registration**
  - Mitigation: Implement post-registration filtering or fork FastMCP
  
- **Meta-tools add complexity**
  - Mitigation: Comprehensive tests and error handling
  
- **Token savings less than expected**
  - Mitigation: Measure actual token counts and adjust categorization

### User Impact Risks
- **Breaking changes**
  - Mitigation: Default to extended mode, make core mode opt-in
  
- **Confusion about tool modes**
  - Mitigation: Clear documentation and warning messages
  
- **Performance degradation**
  - Mitigation: Benchmark both modes before release

---

## Dependencies

- FastMCP library (external)
- MCP protocol specification
- Claude Code settings format
- pytest for testing

## Related Issues

- anthropics/claude-code#7336 - Lazy Loading Feature Request
- github.com/machjesusmoto/claude-lazy-loading - Community POC

## Maintainer Notes

**Current maintainer**: jleechan2015
**Started**: 2025-01-XX
**Phase 1 completed**: 2025-01-XX
**Target Phase 2 completion**: 2025-02-XX
**Target Phase 3 completion**: 2025-03-XX

---

## Quick Start for Contributors

### To Continue Phase 2:

1. Check out the branch:
```bash
git checkout feature/lazy-load-mcp-tools
```

2. Implement `list_extended_tools` (see section 2.1)

3. Populate registry (see section 2.2, Option B recommended)

4. Implement `call_extended_tool` (see section 2.3)

5. Run tests:
```bash
pytest tests/test_lazy_loading.py -v
```

6. Update documentation and create PR

### Questions?
See `docs/LAZY_LOADING.md` or create an issue.
