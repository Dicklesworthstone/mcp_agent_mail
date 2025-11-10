import asyncio
from typing import Any

from mcp_agent_mail.app import build_mcp_server  # type: ignore
from mcp_agent_mail.db import ensure_schema, reset_database_state


async def _call(tool_name: str, args: dict[str, Any]) -> Any:
    # Access FastMCP internals in a tolerant way for testing
    from fastmcp.tools.tool import FunctionTool  # type: ignore
    mcp = build_mcp_server()
    tool = next(t for t in getattr(mcp, "_tools", []) if isinstance(t, FunctionTool) and t.name == tool_name)  # type: ignore[attr-defined]
    return await tool.run(args)


def test_ensure_product_and_link_project(tmp_path) -> None:
    reset_database_state()
    asyncio.run(ensure_schema())
    # Ensure product
    prod = asyncio.run(_call("ensure_product", {"product_key": "my-product", "name": "My Product"}))
    assert prod["product_uid"]
    # Ensure project exists for linking via existing helper path: _get_project_by_identifier needs a row
    # Use ensure_project tool to create project
    project_result = asyncio.run(_call("ensure_project", {"human_key": str(tmp_path)}))
    slug = project_result.get("slug") or project_result["project"]["slug"]
    # Link
    link = asyncio.run(_call("products_link", {"product_key": prod["product_uid"], "project_key": slug}))
    assert link["linked"] is True
    # Product resource lists the project
    mcp = build_mcp_server()
    resource = next(r for r in getattr(mcp, "_resources", []) if r.name == "resource://product/{key}")  # type: ignore[attr-defined]
    res = resource.func(prod["product_uid"])  # type: ignore[attr-defined]
    assert any(p["slug"] == slug for p in res["projects"])


