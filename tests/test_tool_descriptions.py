"""Every MCP tool must reach clients with a description (GH #275).

FastMCP takes a tool's description from ``description=`` or, failing that, the
function docstring. A tool with neither is listed with no description, and an
agent choosing tools has nothing to go on.
"""

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server


@pytest.mark.asyncio
async def test_every_listed_tool_has_a_description(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        tools = await client.list_tools()

    names = {tool.name for tool in tools}
    assert {"install_precommit_guard", "uninstall_precommit_guard"} <= names

    missing = sorted(tool.name for tool in tools if not (tool.description or "").strip())
    assert missing == [], f"tools listed without a description: {missing}"
