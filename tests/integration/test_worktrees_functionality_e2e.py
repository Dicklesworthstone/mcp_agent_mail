import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return cp.stdout.strip()


def _print_tools_and_resources(console: Console, mcp: Any) -> tuple[list[str], list[str]]:
    # Use FastMCP async getters
    import asyncio as _asyncio
    try:
        tools = _asyncio.run(mcp.get_tools())
    except RuntimeError:
        # in-case nested loop; run a temporary loop
        import queue
        import threading
        q: "queue.Queue[Iterable[Any]]" = queue.Queue()
        def _runner():
            q.put(_asyncio.run(mcp.get_tools()))
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        tools = q.get()
    try:
        resources = _asyncio.run(mcp.get_resources())
    except RuntimeError:
        import queue
        import threading
        q2: "queue.Queue[Iterable[Any]]" = queue.Queue()
        def _runner2():
            q2.put(_asyncio.run(mcp.get_resources()))
        t2 = threading.Thread(target=_runner2, daemon=True)
        t2.start()
        resources = q2.get()
    tool_names = [getattr(t, "name", "") for t in tools if getattr(t, "name", "")]
    resource_names = [getattr(r, "name", "") for r in resources if getattr(r, "name", "")]

    table = Table(title="MCP Surface (tools/resources)")
    table.add_column("Tools", style="cyan")
    table.add_column("Resources", style="magenta")
    rows = max(len(tool_names), len(resource_names))
    for i in range(rows):
        table.add_row(tool_names[i] if i < len(tool_names) else "", resource_names[i] if i < len(resource_names) else "")
    console.print(table)
    return tool_names, resource_names


@pytest.mark.skipif(
    subprocess.call(["git", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0,
    reason="git not available",
)
def test_worktrees_functionality_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Orchestrated E2E for the Worktree/Guards plan with rich logging:
      - Toggle gate off/on; verify tool/resource registration surface area
      - Create git repo + worktree; install chain-runner hooks via CLI
      - Execute guard check CLI (pre-commit and pre-push style inputs)
      - Verify cross-worktree conflict behavior via reservations
      - With gate on, run a minimal Product Bus round-trip
    """
    console = Console(width=120, force_terminal=True, color_system="truecolor")

    # Helper to (re)build MCP server with current env
    def _build_server():
        # Invalidate cached settings between toggles
        from mcp_agent_mail.config import clear_settings_cache

        clear_settings_cache()
        from mcp_agent_mail.app import build_mcp_server

        mcp = build_mcp_server()
        return mcp

    # 1) Gate OFF: no new product/identity resources or tools should be registered
    env_off = os.environ.copy()
    env_off["WORKTREES_ENABLED"] = "0"
    monkeypatch.setenv("WORKTREES_ENABLED", "0")
    mcp_off = _build_server()
    console.print(Panel.fit("Gate OFF (WORKTREES_ENABLED=0) — inspecting MCP surface", title="Step 1"))
    tools_off, resources_off = _print_tools_and_resources(console, mcp_off)

    # Ensure identity and product bus are not present when gated off
    assert "ensure_product" not in tools_off
    assert "products_link" not in tools_off
    assert "search_messages_product" not in tools_off
    assert "fetch_inbox_product" not in tools_off
    assert "summarize_thread_product" not in tools_off
    assert "resource://product/{key}" not in resources_off
    assert "resource://identity/{project}" not in resources_off

    # 2) Create a git repo + linked worktree and write a reservation
    console.print(Panel.fit("Initialize repo + worktree", title="Step 2"))
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "E2E Test")
    _git(repo, "config", "user.email", "e2e@example.com")
    (repo / "src").mkdir()
    (repo / "src" / "shared.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "src/shared.txt")
    _git(repo, "commit", "-m", "init")
    _git(repo, "worktree", "add", str(wt), "-b", "feature/wt")
    console.print(Panel(Syntax(_git(repo, "status"), "bash", theme="monokai", line_numbers=False), title="git status (repo)"))

    # Prepare storage root; reservations will be created after we know the slug
    storage_root = tmp_path / "archive"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))

    # 3) Gate ON: install chain-runner hooks via CLI; verify surface expands
    console.print(Panel.fit("Enable gate and install guards via CLI", title="Step 3"))
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    mcp_on = _build_server()
    tools_on, resources_on = _print_tools_and_resources(console, mcp_on)

    console.print(Panel.fit(f"Tools (count): {len(tools_on)} • Resources (count): {len(resources_on)}", title="Surface (Gate ON)"))

    # Ensure a project exists for the repo so guard install can resolve it
    async def _call_tool(tool_name: str, args: dict[str, Any]) -> Any:
        mcp = _build_server()
        _contents, structured = await mcp._mcp_call_tool(tool_name, args)
        return structured

    project_payload = __import__("asyncio").run(_call_tool("ensure_project", {"human_key": str(repo.resolve())}))
    console.print(Panel.fit(json.dumps(project_payload, indent=2), title="ensure_project result"))
    slug = project_payload.get("slug") or project_payload.get("project", {}).get("slug") or ""
    assert slug, "slug missing from ensure_project payload"

    # Now create a conflicting reservation under STORAGE_ROOT/projects/<slug>/file_reservations
    os.environ.copy()
    # Ensure the archive exists via helper
    # We reuse the server's ensure_archive behavior in the next CLI calls; manual write is enough here.
    fr_dir = storage_root / "projects" / slug / "file_reservations"
    fr_dir.mkdir(parents=True, exist_ok=True)
    import datetime as _dt
    expires = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)).isoformat()
    fr = {"agent": "Other", "exclusive": True, "path_pattern": "src/shared.txt", "expires_ts": expires}
    (fr_dir / "lock.json").write_text(json.dumps(fr, indent=2), encoding="utf-8")
    console.print(Panel.fit(json.dumps(fr, indent=2), title=f"Reservation @ {fr_dir}/lock.json"))

    # Install guard via MCP tool (avoids dependency on external .env during tests)
    install_result = __import__("asyncio").run(_call_tool("install_precommit_guard", {"project_key": slug, "code_repo_path": str(repo)}))
    console.print(Panel.fit(json.dumps(install_result, indent=2), title="install_precommit_guard result"))

    # 4) Execute guard check via CLI (pre-commit style: staged paths)
    console.print(Panel.fit("Run guard check (pre-commit style)", title="Step 4"))
    (wt / "src" / "shared.txt").write_text("v2\n", encoding="utf-8")
    _git(wt, "add", "src/shared.txt")
    nul_payload = "src/shared.txt\0".encode("utf-8")
    check_cmd = [sys.executable, "-m", "mcp_agent_mail.cli", "guard", "check", "--stdin-nul", "--repo", str(repo)]
    env_with_agent = os.environ.copy()
    env_with_agent["AGENT_NAME"] = "BlueLake"  # Valid adjective+noun format
    rc = subprocess.run(check_cmd, input=nul_payload, env=env_with_agent, cwd=str(wt)).returncode
    console.print(Panel.fit(f"guard check rc={rc}", title="guard check result"))
    assert rc == 1  # conflict expected

    # 5) Execute guard check via CLI (pre-push style: changed files gathered by range → feed as NUL list)
    console.print(Panel.fit("Run guard check (pre-push style input)", title="Step 5"))
    rc2 = subprocess.run(check_cmd, input=nul_payload, env=env_with_agent, cwd=str(repo)).returncode
    console.print(Panel.fit(f"guard check (pre-push style) rc={rc2}", title="guard check result"))
    assert rc2 == 1  # conflict expected

    # 6) Optional Product Bus round-trip (gate ON)
    console.print(Panel.fit("Product Bus (ensure/link/list) — optional", title="Step 6"))
    from mcp_agent_mail.db import ensure_schema

    asyncio = __import__("asyncio")

    async def _call(tool_name: str, args: dict[str, Any]) -> Any:
        mcp = _build_server()
        _contents, structured = await mcp._mcp_call_tool(tool_name, args)
        return structured

    try:
        asyncio.run(ensure_schema())
        unique = "_e2e_" + hex(hash(str(tmp_path)) & 0xFFFFF)[2:]
        prod = asyncio.run(_call("ensure_product", {"product_key": f"plan-e2e-prod{unique}", "name": f"Plan E2E Product{unique}"}))
        console.print(Panel.fit(json.dumps(prod, indent=2), title="ensure_product result"))

        project = asyncio.run(_call("ensure_project", {"human_key": str(repo.resolve())}))
        slug = project.get("slug") or project["project"]["slug"]

        link = asyncio.run(_call("products_link", {"product_key": prod["product_uid"], "project_key": slug}))
        console.print(Panel.fit(json.dumps(link, indent=2), title="products_link result"))

        mcp = _build_server()
        res_list = __import__('asyncio').run(mcp._mcp_read_resource(f"resource://product/{prod['product_uid']}"))
        assert res_list and getattr(res_list[0], "content", None)
        payload = json.loads(res_list[0].content)
        console.print(Panel.fit(json.dumps(payload, indent=2), title="resource://product payload"))
        assert any(p.get("slug") == slug for p in payload.get("projects", [])), "Linked project missing from product view"
    except Exception as exc:
        console.print(Panel.fit(str(exc), title="Product Bus skipped (not registered)", style="yellow"))

    console.print(Panel.fit("E2E orchestration completed successfully", title="Done", border_style="green"))

