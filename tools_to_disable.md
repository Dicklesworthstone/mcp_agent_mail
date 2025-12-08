# Agent Mail - Tools to Disable for Minimal Setup

## Instructions
For each line number below, add `# DISABLED: ` before the `@mcp.tool` decorator and the function definition.

## Contact Management (5 tools) - Lines 2763-4148
```python
# Line 2763: create_agent_identity
# DISABLED: @mcp.tool(name="create_agent_identity")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def create_agent_identity(...):

# Line 3927: request_contact
# DISABLED: @mcp.tool(name="request_contact")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def request_contact(...):

# Line 4059: respond_contact
# DISABLED: @mcp.tool(name="respond_contact")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def respond_contact(...):

# Line 4119: list_contacts
# DISABLED: @mcp.tool(name="list_contacts")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def list_contacts(...):

# Line 4148: set_contact_policy
# DISABLED: @mcp.tool(name="set_contact_policy")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def set_contact_policy(...):
```

## Convenience/Macros (4 tools) - Lines 4386-4562
```python
# Line 4386: macro_start_session
# DISABLED: @mcp.tool(name="macro_start_session")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def macro_start_session(...):

# Line 4448: macro_prepare_thread
# DISABLED: @mcp.tool(name="macro_prepare_thread")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def macro_prepare_thread(...):

# Line 4510: macro_file_reservation_cycle
# DISABLED: @mcp.tool(name="macro_file_reservation_cycle")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def macro_file_reservation_cycle(...):

# Line 4562: macro_contact_handshake
# DISABLED: @mcp.tool(name="macro_contact_handshake")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def macro_contact_handshake(...):
```

## Viewer Features (3 tools) - Lines 4815-4865
```python
# Line 4815: summarize_thread
# DISABLED: @mcp.tool(name="summarize_thread")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def summarize_thread(...):

# Line 4865: summarize_threads
# DISABLED: @mcp.tool(name="summarize_threads")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def summarize_threads(...):

# Line 4237: mark_message_read (use acknowledge_message instead)
# DISABLED: @mcp.tool(name="mark_message_read")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def mark_message_read(...):
```

## Setup/Guards (2 tools) - Lines 5002-5028
```python
# Line 5002: install_precommit_guard
# DISABLED: @mcp.tool(name="install_precommit_guard")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def install_precommit_guard(...):

# Line 5028: uninstall_precommit_guard
# DISABLED: @mcp.tool(name="uninstall_precommit_guard")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def uninstall_precommit_guard(...):
```

## Advanced/Rare (1 tool) - Line 5313
```python
# Line 5313: force_release_file_reservation
# DISABLED: @mcp.tool(name="force_release_file_reservation")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def force_release_file_reservation(...):
```

## Build Slots (3 tools) - Lines 5636-5716
```python
# Line 5636: acquire_build_slot
# DISABLED: @mcp.tool(name="acquire_build_slot")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def acquire_build_slot(...):

# Line 5684: renew_build_slot
# DISABLED: @mcp.tool(name="renew_build_slot")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def renew_build_slot(...):

# Line 5716: release_build_slot
# DISABLED: @mcp.tool(name="release_build_slot")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def release_build_slot(...):
```

## Product Bus (5 tools) - Lines 5797-6042
```python
# Line 5797: ensure_product
# DISABLED: @mcp.tool(name="ensure_product")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def ensure_product(...):

# Line 5838: products_link
# DISABLED: @mcp.tool(name="products_link")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def products_link(...):

# Line 5932: search_messages_product
# DISABLED: @mcp.tool(name="search_messages_product")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def search_messages_product(...):

# Line 5994: fetch_inbox_product
# DISABLED: @mcp.tool(name="fetch_inbox_product")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def fetch_inbox_product(...):

# Line 6042: summarize_thread_product
# DISABLED: @mcp.tool(name="summarize_thread_product")
# DISABLED: @_instrument_tool(...)
# DISABLED: async def summarize_thread_product(...):
```

## Summary
- **Total tools:** 35
- **Keep active:** 12 (34%)
- **Disable:** 23 (66%)
- **Reduction:** 65% fewer tools exposed

## Quick Disable Script
```bash
# From project root:
cd src/mcp_agent_mail

# Create backup
cp app.py app.py.backup

# Use sed to comment out (macOS/BSD sed):
sed -i '' '2763,2800s/^/# DISABLED: /' app.py
# ... repeat for each range listed above
```
