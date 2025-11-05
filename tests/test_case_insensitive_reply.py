"""Test case-insensitive agent name lookup in reply_message."""

import pytest
from mcp_agent_mail.app import mcp


@pytest.mark.asyncio
async def test_reply_with_lowercase_recipient_name():
    """Test that reply_message handles case-insensitive recipient names like send_message does."""
    # Setup: Create a project and agents
    project = await mcp.call_tool("ensure_project", {
        "project_key": "/test/case_insensitive",
        "slug": "test-case-insensitive"
    })

    # Register agents: GreenSnow and BrownCreek (with capital letters)
    await mcp.call_tool("register_agent", {
        "project_key": "/test/case_insensitive",
        "agent_name": "GreenSnow"
    })

    await mcp.call_tool("register_agent", {
        "project_key": "/test/case_insensitive",
        "agent_name": "BrownCreek"
    })

    # Step 1: GreenSnow sends a message to BrownCreek
    result = await mcp.call_tool("send_message", {
        "project_key": "/test/case_insensitive",
        "sender_name": "GreenSnow",
        "to": ["BrownCreek"],
        "subject": "Test Message",
        "body_md": "Hello BrownCreek!",
        "importance": "normal"
    })

    original_msg_id = result["message_id"]
    thread_id = result["thread_id"]

    # Step 2: BrownCreek replies, but uses lowercase names in the to/cc fields
    # This should work just like send_message (case-insensitive)
    reply_result = await mcp.call_tool("reply_message", {
        "project_key": "/test/case_insensitive",
        "sender_name": "BrownCreek",
        "message_id": original_msg_id,
        "body_md": "Thanks for the message!",
        "to": ["greensnow"],  # lowercase - should resolve to "GreenSnow"
        "cc": ["browncreek"]  # lowercase self-reference - should resolve to "BrownCreek"
    })

    reply_msg_id = reply_result["message_id"]
    assert reply_result["thread_id"] == thread_id

    # Step 3: Verify GreenSnow sees the reply
    greensnow_inbox = await mcp.call_tool("fetch_inbox", {
        "project_key": "/test/case_insensitive",
        "agent_name": "GreenSnow"
    })

    # Find the reply message
    reply_in_inbox = None
    for msg in greensnow_inbox["messages"]:
        if msg["id"] == reply_msg_id:
            reply_in_inbox = msg
            break

    assert reply_in_inbox is not None, "GreenSnow should see the reply message (case-insensitive lookup should work)"
    assert reply_in_inbox["thread_id"] == thread_id
    assert "greensnow" in [r.lower() for r in reply_in_inbox.get("to", [])]

    # Step 4: Verify BrownCreek sees themselves in CC
    browncreek_inbox = await mcp.call_tool("fetch_inbox", {
        "project_key": "/test/case_insensitive",
        "agent_name": "BrownCreek"
    })

    reply_in_browncreek = None
    for msg in browncreek_inbox["messages"]:
        if msg["id"] == reply_msg_id:
            reply_in_browncreek = msg
            break

    assert reply_in_browncreek is not None
    assert "browncreek" in [r.lower() for r in reply_in_browncreek.get("cc", [])]
