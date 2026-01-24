from __future__ import annotations

import subprocess

import pytest
from fastmcp import Client

from mcp_agent_mail import app as app_module
from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.config import clear_settings_cache
from tests.e2e.utils import make_console, render_phase, write_log


def _fake_completed(stdout: str, stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["tru", "--encode"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.asyncio
async def test_toon_format_e2e_flow(isolated_env, monkeypatch):
    monkeypatch.setattr(app_module, "_looks_like_toon_rust_encoder", lambda _exe: True)
    clear_settings_cache()
    console = make_console()

    def _fake_run(payload: str, settings):
        _ = payload, settings
        return _fake_completed(stdout="ok: true\n")

    monkeypatch.setattr(app_module, "_run_toon_encode", _fake_run)
    server = build_mcp_server()

    steps: list[dict[str, object]] = []
    log_payload = {"steps": steps}
    async with Client(server) as client:
        health = await client.call_tool("health_check", {"format": "toon"})
        step = {"tool": "health_check", "format": "toon", "response": health.data}
        steps.append(step)
        render_phase(console, "health_check", {"response_format": health.data.get("format")})

        project = await client.call_tool("ensure_project", {"human_key": "/backend", "format": "toon"})
        step = {"tool": "ensure_project", "format": "toon", "response": project.data}
        steps.append(step)
        render_phase(console, "ensure_project", {"response_format": project.data.get("format")})

        agent = await client.call_tool(
            "register_agent",
            {
                "project_key": "Backend",
                "program": "codex",
                "model": "gpt-5",
                "name": "BlueLake",
                "format": "toon",
            },
        )
        step = {"tool": "register_agent", "format": "toon", "response": agent.data}
        steps.append(step)
        render_phase(console, "register_agent", {"response_format": agent.data.get("format")})

        inbox = await client.read_resource("resource://inbox/BlueLake?project=/backend&format=toon")
        step = {"resource": "resource://inbox/{agent}", "format": "toon", "response": inbox[0].text}
        steps.append(step)
        render_phase(console, "inbox_resource", {"response_bytes": len(inbox[0].text or "")})

    log_path = write_log("toon_format_e2e", log_payload)
    console.print(f"[toon] wrote e2e log: {log_path}")
