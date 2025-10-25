# CRITICAL BUG: macro_start_session Broken - Tool Invocation Pattern Error

## Symptoms

```
AttributeError: 'NoneType' object has no attribute 'run'
```

When calling `macro_start_session` with `claim_paths` parameter, the server crashes.

## Root Cause

**Location**: `src/mcp_agent_mail/app.py:2683`

**Incorrect Code**:
```python
_claim_tool = cast(FunctionTool, cast(Any, globals().get("claim_paths")))
```

**Problem**:
- `globals().get("claim_paths")` returns `None`
- This is because `claim_paths` is NOT in the global/module namespace
- All tools are defined inside `build_mcp_server()` function as local functions
- `globals()` only sees module-level variables, not function-local variables
- So `globals().get("claim_paths")` → `None`
- Then `_claim_tool.run()` → `AttributeError: 'NoneType' object has no attribute 'run'`

## Why This Happened

The codebase has **two different patterns** for calling other tools from within macro tools:

### Pattern 1: CORRECT (used in macro_claim_cycle)
**Line 2797**:
```python
claims_tool = cast(FunctionTool, cast(Any, claim_paths))  # Direct reference ✅
```

### Pattern 2: BROKEN (used in macro_start_session)
**Line 2683**:
```python
_claim_tool = cast(FunctionTool, cast(Any, globals().get("claim_paths")))  # ❌ BROKEN
```

Someone copy-pasted an old/incorrect pattern instead of using the correct pattern from `macro_claim_cycle`.

## Architecture Context

```python
def build_mcp_server() -> FastMCP:
    mcp = FastMCP(...)

    # All tools are LOCAL to this function
    @mcp.tool()
    async def claim_paths(...):
        ...

    @mcp.tool()
    async def macro_claim_cycle(...):
        # CORRECT: Direct reference
        claims_tool = cast(FunctionTool, cast(Any, claim_paths))
        ...

    @mcp.tool()
    async def macro_start_session(...):
        # BROKEN: Using globals()
        _claim_tool = cast(FunctionTool, cast(Any, globals().get("claim_paths")))  # None!
        ...

    return mcp
```

Since all tools are defined in the same function scope, they can reference each other directly.

## The Fix Applied

**Changed line 2683 from**:
```python
_claim_tool = cast(FunctionTool, cast(Any, globals().get("claim_paths")))  # ❌ BROKEN
```

**To**:
```python
_claim_tool = cast(FunctionTool, await mcp.get_tool("claim_paths"))  # ✅ FIXED
```

**Why this works**:
- `mcp.get_tool("claim_paths")` retrieves the tool from the FastMCP registry by name
- This bypasses the parameter shadowing issue (claim_paths parameter shadows claim_paths function)
- Must use `await` because `get_tool()` is an async method
- Uses the tool registry pattern which is the recommended FastMCP approach

## Prevention Strategy

### 1. Code Review Checklist
- ❌ NEVER use `globals().get("tool_name")` to reference other tools
- ✅ ALWAYS reference tools directly: `cast(FunctionTool, cast(Any, tool_name))`

### 2. Search for Similar Issues
```bash
# Find all uses of globals().get() in app.py
grep -n "globals().get" src/mcp_agent_mail/app.py
```

**Current result**: Only one occurrence (the bug)

### 3. Linting Rule
Add a linting rule or pre-commit hook to detect this pattern:
```yaml
# .pre-commit-config.yaml or similar
- id: no-globals-get-pattern
  name: Prevent globals().get() in app.py
  entry: 'globals\(\)\.get\('
  language: pygrep
  files: src/mcp_agent_mail/app.py
```

### 4. Code Comments
Add a comment near macro tools explaining the correct pattern:
```python
# NOTE: To call another tool from within a macro, use direct reference:
# tool_fn = cast(FunctionTool, cast(Any, other_tool_name))
# result = await tool_fn.run({...})
# DO NOT use globals().get() - tools are local to build_mcp_server()
```

### 5. Test Coverage

The test suite didn't catch this because:
- `macro_start_session` was only tested without the `claim_paths` parameter
- The code path that calls `claim_paths` was never exercised

**Regression Test Added** ✅:
- Created `tests/test_macro_start_session_with_claims.py`
- Two tests:
  1. `test_macro_start_session_with_claim_paths` - Exercises the claim_paths parameter
  2. `test_macro_start_session_without_claims_still_works` - Verifies backward compatibility
- Tests verify that claims are created and returned correctly
- **Result**: Test suite increased from 108 to 110 tests, all passing

## Impact

- **Severity**: CRITICAL
- **Affected**: Any agent trying to use `macro_start_session` with `claim_paths` parameter
- **Workaround**: Don't use `claim_paths` parameter; call `claim_paths` and `register_agent` separately
- **Status**: Not caught by tests (test gap)

## Timeline

This bug was likely introduced when `macro_start_session` was created, possibly by:
1. Copy-pasting from an old codebase with different architecture
2. Not following the established pattern in `macro_claim_cycle`
3. Not testing the `claim_paths` code path

## Related Code

**Other macros that correctly call tools**:
- `macro_claim_cycle` (line 2797) ✅
- `macro_contact_handshake` (line 2875 - doesn't call other tools, uses direct functions) ✅

**No other instances of this bug pattern found.**
