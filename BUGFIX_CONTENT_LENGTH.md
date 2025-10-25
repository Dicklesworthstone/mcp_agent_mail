# Bug Fix: Content-Length HTTP Protocol Violation

## Problem

Server was crashing with:
```
RuntimeError: Response content shorter than Content-Length
```

## Root Cause

The HTTP handler was **modifying response bodies** after Content-Length was set:

1. MCP transport serializes response → sets `Content-Length: 1500`
2. ASGI send_wrapper intercepts response
3. Unwraps JSON-RPC → creates smaller body (800 bytes)
4. Sends modified 800-byte body with `Content-Length: 1500` header
5. HTTP protocol violation → Uvicorn crashes

**Location:** `src/mcp_agent_mail/http.py:796-832` (before fix)

## Why The Unwrapping Existed

Commit `7521a04` added "MCP response unwrapping" to make stateless HTTP responses "simpler" for clients by removing the JSON-RPC envelope.

**Before (with unwrapping):**
```json
{"result": {"deliveries": [...], "count": 1}}
```

**After (raw JSON-RPC):**
```json
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{...}"}]}}
```

## Why Unwrapping Was Wrong

1. **Breaks HTTP protocol**: Modifying body invalidates Content-Length
2. **Violates MCP standard**: All MCP clients expect JSON-RPC 2.0 format
3. **Brittle**: Intercepting at ASGI level and modifying bytes
4. **Unnecessary**: MCP SDKs handle JSON-RPC automatically

## The Fix

**Removed all response unwrapping code.** Just pass through standard JSON-RPC responses.

**Changes:**
- Deleted lines 796-832 in http.py (send_wrapper with unwrapping logic)
- Changed to simple pass-through: `await http_transport.handle_request(new_scope, receive, send)`

**Result:**
- ✓ No Content-Length violations
- ✓ Standard JSON-RPC format (what MCP clients expect)
- ✓ No fragile byte manipulation
- ✓ Simple, correct, maintainable

## Lesson Learned

**Don't fight against HTTP fundamentals.**

When you set `Content-Length`, you MUST send exactly that many bytes. If you want to modify responses:
1. Use chunked transfer encoding (remove Content-Length)
2. Do modifications before serialization (not at ASGI level)
3. **Or better: don't modify at all - use standard formats!**

In this case, option 3 was correct. MCP is JSON-RPC 2.0 by design. Clients handle it properly.
