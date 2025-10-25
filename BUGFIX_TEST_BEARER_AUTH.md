# Test Fix: Bearer Authentication Test Failure

## Problem

Test `test_http_bearer_and_cors_preflight` was failing with:
- Expected: 401 (Unauthorized) when no bearer token provided
- Actual: 200 (OK) - request was being auto-authenticated

## Root Cause

The server has a **localhost auto-authentication feature** for development convenience:

**Location:** `src/mcp_agent_mail/http.py:90-96`

```python
# Allow localhost without Authorization when enabled
try:
    client_host = request.client.host if request.client else ""
except Exception:
    client_host = ""
if self._allow_localhost and client_host in {"127.0.0.1", "::1", "localhost"}:
    return await call_next(request)
```

**Config:** `src/mcp_agent_mail/config.py:208`

```python
allow_localhost_unauthenticated=_bool(_decouple_config("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", default="true"), default=True)
```

By default, `HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED="true"`, meaning localhost connections bypass bearer authentication entirely.

## Why The Test Failed

1. Test sets up bearer authentication with `HTTP_BEARER_TOKEN="token123"`
2. Test expects request without bearer token → 401 Unauthorized
3. BUT: Test client connects from localhost
4. Middleware sees localhost → auto-authenticates → returns 200 OK
5. Test assertion fails: expected 401, got 200

## The Fix

**Disable localhost auto-authentication in the test** by setting the env var to `"false"`:

```python
@pytest.mark.asyncio
async def test_http_bearer_and_cors_preflight(isolated_env, monkeypatch):
    # Enable Bearer and CORS
    monkeypatch.setenv("HTTP_BEARER_TOKEN", "token123")
    monkeypatch.setenv("HTTP_CORS_ENABLED", "true")
    monkeypatch.setenv("HTTP_CORS_ORIGINS", "http://example.com")
    # Disable localhost auto-authentication to properly test bearer auth
    monkeypatch.setenv("HTTP_ALLOW_LOCALHOST_UNAUTHENTICATED", "false")  # ← ADDED
```

**Result:**
- ✓ Test properly validates bearer authentication behavior
- ✓ 401 when no token provided
- ✓ 200 when correct token provided
- ✓ CORS headers working correctly

## Lesson Learned

**When testing authentication logic, ensure convenience bypasses are disabled.**

Tests should exercise the actual authentication flow, not development shortcuts. The localhost auto-authentication feature is useful for local development, but tests validating security behavior must disable it to ensure proper coverage.
