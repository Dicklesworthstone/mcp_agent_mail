import asyncio
import json
from typing import Any

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.db import ensure_schema, reset_database_state


async def _call(tool_name: str, args: dict[str, Any]) -> Any:
    # Use FastMCP internal call helper for consistency across versions
    mcp = build_mcp_server()
    _contents, structured = await mcp._mcp_call_tool(tool_name, args)
    return structured


def test_ensure_product_and_link_project(tmp_path, monkeypatch) -> None:
    # Enable gated features for product bus
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    reset_database_state()
    asyncio.run(ensure_schema())
    # Ensure product (unique ids to avoid cross-run collisions)
    unique = "_prod_" + hex(hash(str(tmp_path)) & 0xFFFFF)[2:]
    prod = asyncio.run(_call("ensure_product", {"product_key": f"my-product{unique}", "name": f"My Product{unique}"}))
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
    res_list = asyncio.run(mcp._mcp_read_resource(f"resource://product/{prod['product_uid']}"))
    assert res_list and getattr(res_list[0], "content", None)
    payload = json.loads(res_list[0].content)
    assert any(p["slug"] == slug for p in payload.get("projects", []))

