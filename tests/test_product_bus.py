import asyncio
import json
from typing import Any

from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache
from mcp_agent_mail.db import ensure_schema, reset_database_state
from mcp_agent_mail.utils import slugify


async def _call(tool_name: str, args: dict[str, Any]) -> Any:
    async with Client(build_mcp_server()) as client:
        result = await client.call_tool(tool_name, args)
    return result.data


async def _read_json_resource(uri: str) -> dict[str, Any]:
    async with Client(build_mcp_server()) as client:
        res_list = await client.read_resource(uri)
    assert res_list and res_list[0].text
    return json.loads(res_list[0].text)


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
    payload = asyncio.run(_read_json_resource(f"resource://product/{prod['product_uid']}"))
    assert any(p["slug"] == slug for p in payload.get("projects", []))


def test_products_link_resolves_relative_project_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    reset_database_state()
    asyncio.run(ensure_schema())

    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    unique = "_prod_" + hex(hash(str(tmp_path)) & 0xFFFFF)[2:]
    prod = asyncio.run(_call("ensure_product", {"product_key": f"my-product{unique}", "name": f"My Product{unique}"}))
    project_result = asyncio.run(_call("ensure_project", {"human_key": str(project_dir.resolve())}))
    slug = project_result.get("slug") or project_result["project"]["slug"]

    link = asyncio.run(_call("products_link", {"product_key": prod["product_uid"], "project_key": "."}))

    assert link["linked"] is True
    assert link["project"]["slug"] == slug


def test_products_link_resolves_relative_symlink_project_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    clear_settings_cache()
    reset_database_state()
    asyncio.run(ensure_schema())

    real_project_dir = tmp_path / "real-repo"
    real_project_dir.mkdir()
    symlink_project_dir = tmp_path / "repo-link"
    symlink_project_dir.symlink_to(real_project_dir, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    unique = "_prod_" + hex(hash(str(tmp_path)) & 0xFFFFF)[2:]
    prod = asyncio.run(_call("ensure_product", {"product_key": f"my-product{unique}", "name": f"My Product{unique}"}))
    project_result = asyncio.run(_call("ensure_project", {"human_key": str(symlink_project_dir)}))
    slug = project_result.get("slug") or project_result["project"]["slug"]
    assert slug == slugify(str(symlink_project_dir))

    link = asyncio.run(_call("products_link", {"product_key": prod["product_uid"], "project_key": "./repo-link"}))

    assert link["linked"] is True
    assert link["project"]["slug"] == slug
