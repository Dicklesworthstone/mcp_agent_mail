from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from PIL import Image
from rich.table import Table
from sqlalchemy import text

from mcp_agent_mail import share
from mcp_agent_mail.app import ToolExecutionError, _compile_pathspec, _patterns_overlap, build_mcp_server
from mcp_agent_mail.cli import _collect_preview_status
from mcp_agent_mail.config import clear_settings_cache, get_settings
from mcp_agent_mail.db import ensure_schema, get_session, reset_database_state
from mcp_agent_mail.http import build_http_app
from mcp_agent_mail.storage import ensure_archive
from tests.e2e.utils import assert_matches_golden, make_console, render_phase, write_log

INLINE_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMA"
    "ASsJTYQAAAAASUVORK5CYII="
)


def _tool_data(result: Any) -> Any:
    data = getattr(result, "data", result)
    structured = getattr(result, "structured_content", None)
    if (
        isinstance(structured, dict)
        and "result" in structured
        and isinstance(data, list)
        and data
        and type(data[0]).__name__ == "Root"
    ):
        return structured["result"]
    return data


def _parse_resource_json(blocks: list[Any]) -> dict[str, Any]:
    assert blocks, "expected resource blocks"
    text_payload = blocks[0].text or ""
    return json.loads(text_payload)


def _parse_search_html(html: str) -> dict[str, Any]:
    subjects = re.findall(r"<h4[^>]*>(.*?)</h4>", html, flags=re.S)
    subjects = [re.sub(r"<[^>]+>", "", s).strip() for s in subjects]
    return {
        "subject_count": len(subjects),
        "subjects_sample": subjects[:3],
        "mark_count": html.count("<mark>"),
        "hits_badge_count": len(re.findall(r">\\s*\\d+\\s+match(?:es)?\\s*<", html)),
        "has_snippet_block": "line-clamp-2" in html,
    }


def _cache_hit_ratio() -> float:
    """Return PathSpec cache hit ratio, or 0.0 when no samples collected."""
    info = _compile_pathspec.cache_info()
    total = info.hits + info.misses
    return (info.hits / total) if total else 0.0


def _render_cache_stats_table(console) -> None:
    """Render cache stats with Rich for log visibility."""
    info = _compile_pathspec.cache_info()
    ratio = _cache_hit_ratio()
    table = Table(title="PathSpec Cache Stats")
    table.add_column("Hits", justify="right")
    table.add_column("Misses", justify="right")
    table.add_column("Maxsize", justify="right")
    table.add_column("Currsize", justify="right")
    table.add_column("Hit Ratio", justify="right")
    table.add_row(
        str(info.hits),
        str(info.misses),
        str(info.maxsize),
        str(info.currsize),
        f"{ratio:.2%}",
    )
    console.print(table)


async def _measure_file_reservation_commit_delta(
    server,
    *,
    project_key: str,
    agent_name: str,
) -> int:
    """Return number of archive commits added after a file_reservation_paths call."""
    settings = get_settings()
    slug = project_key.strip("/").replace("/", "-")
    archive = await ensure_archive(settings, slug)
    before = list(archive.repo.iter_commits())
    async with Client(server) as client:
        await client.call_tool(
            "file_reservation_paths",
            {
                "project_key": project_key,
                "agent_name": agent_name,
                "paths": ["src/a.py", "src/b.py"],
                "ttl_seconds": 3600,
                "exclusive": True,
                "reason": "phase2-commit-batch",
            },
        )
    after = list(archive.repo.iter_commits())
    return len(after) - len(before)


def _iso_at(base: datetime, *, offset_seconds: int) -> str:
    ts = base + timedelta(seconds=offset_seconds)
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _scrub_commit_meta(payload: dict[str, Any]) -> None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        commit = entry.get("commit")
        if not isinstance(commit, dict):
            continue
        if "hexsha" in commit:
            commit["hexsha"] = "<commit_hexsha>"
        if "authored_ts" in commit:
            commit["authored_ts"] = "<commit_authored_ts>"


def _coerce_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if hasattr(payload, "model_dump"):
        try:
            payload = payload.model_dump(mode="json")
        except TypeError:
            payload = payload.model_dump()
    if hasattr(payload, "root"):
        payload = payload.root
    return payload if isinstance(payload, list) else []


async def _stabilize_timestamps(base: datetime) -> None:
    async with get_session() as session:
        rows = await session.execute(text("SELECT id FROM projects ORDER BY id"))
        for row in rows.fetchall():
            ts = base + timedelta(seconds=int(row[0]))
            await session.execute(
                text("UPDATE projects SET created_at = :ts WHERE id = :id"),
                {"ts": ts, "id": row[0]},
            )

        rows = await session.execute(text("SELECT id FROM products ORDER BY id"))
        for row in rows.fetchall():
            ts = base + timedelta(seconds=50 + int(row[0]))
            await session.execute(
                text("UPDATE products SET created_at = :ts WHERE id = :id"),
                {"ts": ts, "id": row[0]},
            )

        rows = await session.execute(text("SELECT id FROM product_project_links ORDER BY id"))
        for row in rows.fetchall():
            ts = base + timedelta(seconds=75 + int(row[0]))
            await session.execute(
                text("UPDATE product_project_links SET created_at = :ts WHERE id = :id"),
                {"ts": ts, "id": row[0]},
            )

        rows = await session.execute(text("SELECT id FROM agents ORDER BY id"))
        for row in rows.fetchall():
            inception = base + timedelta(seconds=100 + int(row[0]))
            last_active = base + timedelta(seconds=120 + int(row[0]))
            await session.execute(
                text(
                    "UPDATE agents SET inception_ts = :inception, last_active_ts = :last_active WHERE id = :id"
                ),
                {"inception": inception, "last_active": last_active, "id": row[0]},
            )

        rows = await session.execute(text("SELECT id FROM messages ORDER BY id"))
        for row in rows.fetchall():
            ts = base + timedelta(seconds=200 + int(row[0]))
            await session.execute(
                text("UPDATE messages SET created_ts = :ts WHERE id = :id"),
                {"ts": ts, "id": row[0]},
            )

        rows = await session.execute(
            text("SELECT message_id, agent_id, read_ts, ack_ts FROM message_recipients")
        )
        for row in rows.fetchall():
            if row[2] is not None:
                read_ts = base + timedelta(seconds=300 + int(row[0]))
                await session.execute(
                    text(
                        "UPDATE message_recipients SET read_ts = :read_ts WHERE message_id = :mid AND agent_id = :aid"
                    ),
                    {"read_ts": read_ts, "mid": row[0], "aid": row[1]},
                )
            if row[3] is not None:
                ack_ts = base + timedelta(seconds=400 + int(row[0]))
                await session.execute(
                    text(
                        "UPDATE message_recipients SET ack_ts = :ack_ts WHERE message_id = :mid AND agent_id = :aid"
                    ),
                    {"ack_ts": ack_ts, "mid": row[0], "aid": row[1]},
                )

        rows = await session.execute(text("SELECT id, released_ts FROM file_reservations ORDER BY id"))
        for row in rows.fetchall():
            created_ts = base + timedelta(seconds=500 + int(row[0]))
            expires_ts = base + timedelta(seconds=600 + int(row[0]))
            await session.execute(
                text(
                    "UPDATE file_reservations SET created_ts = :created_ts, expires_ts = :expires_ts WHERE id = :id"
                ),
                {"created_ts": created_ts, "expires_ts": expires_ts, "id": row[0]},
            )
            if row[1] is not None:
                released_ts = base + timedelta(seconds=700 + int(row[0]))
                await session.execute(
                    text("UPDATE file_reservations SET released_ts = :released_ts WHERE id = :id"),
                    {"released_ts": released_ts, "id": row[0]},
                )

        rows = await session.execute(text("SELECT id, expires_ts FROM agent_links ORDER BY id"))
        for row in rows.fetchall():
            created_ts = base + timedelta(seconds=800 + int(row[0]))
            updated_ts = base + timedelta(seconds=900 + int(row[0]))
            await session.execute(
                text("UPDATE agent_links SET created_ts = :created_ts, updated_ts = :updated_ts WHERE id = :id"),
                {"created_ts": created_ts, "updated_ts": updated_ts, "id": row[0]},
            )
            if row[1] is not None:
                expires_ts = base + timedelta(seconds=950 + int(row[0]))
                await session.execute(
                    text("UPDATE agent_links SET expires_ts = :expires_ts WHERE id = :id"),
                    {"expires_ts": expires_ts, "id": row[0]},
                )
        await session.commit()


def _touch_bundle_files(bundle_root: Path, base: float) -> None:
    files = sorted([p for p in bundle_root.rglob("*") if p.is_file()])
    for idx, path in enumerate(files):
        ts = base + idx
        os.utime(path, (ts, ts))


@pytest.mark.asyncio
async def test_isomorphism_e2e_suite(isolated_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WORKTREES_ENABLED", "1")
    monkeypatch.setenv("INLINE_IMAGE_MAX_BYTES", "1024")
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("MESSAGING_AUTO_HANDSHAKE_ON_BLOCK", "false")
    monkeypatch.setenv("TOOLS_LOG_ENABLED", "false")
    monkeypatch.setenv("LOG_RICH_ENABLED", "false")
    clear_settings_cache()

    console = make_console()
    phases: list[dict[str, Any]] = []

    server = build_mcp_server()
    async with Client(server) as client:
        render_phase(console, "setup", {"project": "alpha/beta", "agents": "register"})
        alpha_project = _tool_data(await client.call_tool("ensure_project", {"human_key": "/alpha"}))
        beta_project = _tool_data(await client.call_tool("ensure_project", {"human_key": "/beta"}))
        alpha_key = alpha_project["slug"]
        beta_key = beta_project["slug"]
        phases.append(
            {
                "phase": "setup",
                "alpha_project": alpha_project,
                "beta_project": beta_project,
            }
        )

        agents_alpha = ["BlueLake", "RedStone", "GreenCastle", "StormyCanyon"]
        agents_beta = ["PurpleBear", "JadePond", "BlueLake"]
        for name in agents_alpha:
            await client.call_tool(
                "register_agent",
                {"project_key": alpha_key, "program": "codex", "model": "gpt-5", "name": name},
            )
        for name in agents_beta:
            await client.call_tool(
                "register_agent",
                {"project_key": beta_key, "program": "codex", "model": "gpt-5", "name": name},
            )
        await client.call_tool(
            "set_contact_policy",
            {"project_key": beta_key, "agent_name": "BlueLake", "policy": "open"},
        )

        render_phase(console, "messaging", {"subject": "Launch Plan", "thread": "THREAD-1"})
        small_path = tmp_path / "small.png"
        large_path = tmp_path / "large.png"
        Image.new("RGB", (4, 4), color=(255, 0, 0)).save(small_path)
        Image.new("RGB", (128, 128), color=(0, 128, 255)).save(large_path)
        body_md = "Launch kickoff\n\n![inline](data:image/png;base64,{})\n".format(INLINE_PNG_BASE64)
        send_result = _tool_data(
            await client.call_tool(
                "send_message",
                {
                    "project_key": alpha_key,
                    "sender_name": "BlueLake",
                    "to": ["RedStone"],
                    "cc": ["GreenCastle"],
                    "bcc": ["StormyCanyon"],
                    "subject": "Launch Plan",
                    "body_md": body_md,
                    "attachment_paths": [str(small_path), str(large_path)],
                    "convert_images": True,
                    "ack_required": True,
                    "thread_id": "THREAD-1",
                },
            )
        )
        message_id = int((send_result.get("deliveries") or [{}])[0].get("payload", {}).get("id"))

        send_followup = _tool_data(
            await client.call_tool(
                "send_message",
                {
                    "project_key": alpha_key,
                    "sender_name": "BlueLake",
                    "to": ["RedStone"],
                    "subject": "Follow Up",
                    "body_md": "Follow up details",
                    "thread_id": "THREAD-1",
                },
            )
        )
        followup_id = int((send_followup.get("deliveries") or [{}])[0].get("payload", {}).get("id"))

        await client.call_tool("mark_message_read", {"project_key": alpha_key, "agent_name": "RedStone", "message_id": message_id})
        await client.call_tool("acknowledge_message", {"project_key": alpha_key, "agent_name": "RedStone", "message_id": message_id})

        beta_message = _tool_data(
            await client.call_tool(
                "send_message",
                {
                    "project_key": beta_key,
                    "sender_name": "PurpleBear",
                    "to": ["BlueLake"],
                    "subject": "Beta Hello",
                    "body_md": "beta side",
                    "thread_id": "THREAD-1",
                },
            )
        )
        beta_message_id = int((beta_message.get("deliveries") or [{}])[0].get("payload", {}).get("id"))
        phases.append(
            {
                "phase": "messaging",
                "message_id": message_id,
                "followup_id": followup_id,
                "beta_message_id": beta_message_id,
            }
        )

        render_phase(console, "file reservations", {"path": "src/core.py"})
        reservation = _tool_data(
            await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": alpha_key,
                    "agent_name": "BlueLake",
                    "paths": ["src/core.py"],
                    "ttl_seconds": 600,
                    "exclusive": True,
                    "reason": "e2e",
                },
            )
        )
        conflict = _tool_data(
            await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": alpha_key,
                    "agent_name": "GreenCastle",
                    "paths": ["src/core.py"],
                    "ttl_seconds": 600,
                    "exclusive": True,
                    "reason": "conflict",
                },
            )
        )
        await client.call_tool(
            "renew_file_reservations",
            {"project_key": alpha_key, "agent_name": "BlueLake", "paths": ["src/core.py"], "extend_seconds": 600},
        )
        await client.call_tool(
            "release_file_reservations",
            {"project_key": alpha_key, "agent_name": "BlueLake", "paths": ["src/core.py"]},
        )
        post_release = _tool_data(
            await client.call_tool(
                "file_reservation_paths",
                {
                    "project_key": alpha_key,
                    "agent_name": "GreenCastle",
                    "paths": ["src/core.py"],
                    "ttl_seconds": 600,
                    "exclusive": True,
                    "reason": "after-release",
                },
            )
        )
        phases.append(
            {
                "phase": "file_reservations",
                "initial": reservation,
                "conflict": conflict,
                "post_release": post_release,
            }
        )

        render_phase(console, "phase2_checks", {"pathspec_cache": "warmup", "commit_batching": "measure"})
        _compile_pathspec.cache_clear()
        for _ in range(20):
            _patterns_overlap("src/**", "src/file.txt")
            _patterns_overlap("docs/**", "docs/readme.md")
            _patterns_overlap("assets/*.png", "assets/logo.png")
        cache_info = _compile_pathspec.cache_info()
        cache_ratio = _cache_hit_ratio()
        _render_cache_stats_table(console)
        assert cache_ratio >= 0.9, f"PathSpec cache hit ratio too low: {cache_ratio:.2%}"

        perf_project = _tool_data(
            await client.call_tool(
                "ensure_project", {"human_key": f"/perf-phase2-{uuid.uuid4().hex[:6]}"}
            )
        )
        perf_key = perf_project["slug"]
        perf_agent = _tool_data(
            await client.call_tool(
                "create_agent_identity",
                {
                    "project_key": perf_key,
                    "program": "codex",
                    "model": "gpt-5",
                    "task_description": "phase2 commit batching check",
                },
            )
        )
        perf_agent_name = perf_agent["name"]
        commit_delta = await _measure_file_reservation_commit_delta(
            server,
            project_key=perf_key,
            agent_name=perf_agent_name,
        )
        assert commit_delta == 1, f"Expected 1 commit, got {commit_delta}"

        phase2_cache_info = cache_info
        phase2_cache_ratio = cache_ratio
        phase2_commit_delta = commit_delta
        phase2_project = perf_key

        render_phase(console, "contacts", {"from": "BlueLake", "to": "PurpleBear"})
        request_contact = _tool_data(
            await client.call_tool(
                "request_contact",
                {
                    "project_key": alpha_key,
                    "from_agent": "BlueLake",
                    "to_agent": "PurpleBear",
                    "to_project": beta_key,
                    "reason": "e2e",
                },
            )
        )
        approve_contact = _tool_data(
            await client.call_tool(
                "respond_contact",
                {
                    "project_key": beta_key,
                    "to_agent": "PurpleBear",
                    "from_agent": "BlueLake",
                    "from_project": alpha_key,
                    "accept": True,
                    "ttl_seconds": 3600,
                },
            )
        )
        deny_request = _tool_data(
            await client.call_tool(
                "request_contact",
                {
                    "project_key": alpha_key,
                    "from_agent": "BlueLake",
                    "to_agent": "JadePond",
                    "to_project": beta_key,
                    "reason": "deny-case",
                },
            )
        )
        deny_contact = _tool_data(
            await client.call_tool(
                "respond_contact",
                {
                    "project_key": beta_key,
                    "to_agent": "JadePond",
                    "from_agent": "BlueLake",
                    "from_project": alpha_key,
                    "accept": False,
                },
            )
        )
        cross_send = _tool_data(
            await client.call_tool(
                "send_message",
                {
                    "project_key": alpha_key,
                    "sender_name": "BlueLake",
                    "to": [f"PurpleBear@{beta_key}"],
                    "subject": "Cross Project",
                    "body_md": "approved path",
                },
            )
        )
        denied_error: dict[str, Any] = {}
        try:
            await client.call_tool(
                "send_message",
                {
                    "project_key": alpha_key,
                    "sender_name": "BlueLake",
                    "to": [f"JadePond@{beta_key}"],
                    "subject": "Should Fail",
                    "body_md": "blocked path",
                },
            )
        except ToolExecutionError as exc:
            denied_error = {"error_type": exc.error_type, "message": str(exc)}
        except Exception as exc:
            denied_error = {"error_type": exc.__class__.__name__, "message": str(exc)}
        phases.append(
            {
                "phase": "contacts",
                "request": request_contact,
                "approve": approve_contact,
                "deny_request": deny_request,
                "deny": deny_contact,
                "cross_send": cross_send,
                "denied_error": denied_error,
            }
        )

        render_phase(console, "product bus", {"product": "Acme Suite"})
        product = _tool_data(await client.call_tool("ensure_product", {"name": "Acme Suite"}))
        product_link_alpha = _tool_data(
            await client.call_tool("products_link", {"product_key": product["product_uid"], "project_key": alpha_key})
        )
        product_link_beta = _tool_data(
            await client.call_tool("products_link", {"product_key": product["product_uid"], "project_key": beta_key})
        )
        phases.append(
            {
                "phase": "product_bus",
                "product": product,
                "link_alpha": product_link_alpha,
                "link_beta": product_link_beta,
            }
        )

        base_time = datetime(2025, 1, 1, 0, 0, 0)
        await _stabilize_timestamps(base_time)

        whois = _tool_data(
            await client.call_tool(
                "whois",
                {"project_key": alpha_key, "agent_name": "BlueLake", "include_recent_commits": False},
            )
        )
        phases.append({"phase": "identity", "whois": whois})

        project_resource = _parse_resource_json(
            await client.read_resource(f"resource://project/{alpha_key}")
        )
        beta_resource = _parse_resource_json(
            await client.read_resource(f"resource://project/{beta_key}")
        )
        message_resource = _parse_resource_json(
            await client.read_resource(f"resource://message/{message_id}?project={alpha_key}")
        )
        followup_resource = _parse_resource_json(
            await client.read_resource(f"resource://message/{followup_id}?project={alpha_key}")
        )
        beta_message_resource = _parse_resource_json(
            await client.read_resource(f"resource://message/{beta_message_id}?project={beta_key}")
        )

        inbox_since_ts = (base_time + timedelta(seconds=200 + followup_id - 1)).isoformat()
        inbox = _tool_data(
            await client.call_tool(
                "fetch_inbox",
                {"project_key": alpha_key, "agent_name": "RedStone", "include_bodies": True, "since_ts": inbox_since_ts, "limit": 10},
            )
        )
        mailbox = _parse_resource_json(
            await client.read_resource(f"resource://mailbox/RedStone?project={alpha_key}&limit=5")
        )
        outbox = _parse_resource_json(
            await client.read_resource(f"resource://outbox/BlueLake?project={alpha_key}&limit=5")
        )
        _scrub_commit_meta(mailbox)
        _scrub_commit_meta(outbox)

        search_tool = _tool_data(
            await client.call_tool("search_messages", {"project_key": alpha_key, "query": "Launch", "limit": 5})
        )

        # HTTP search: FTS then fallback by dropping the FTS table.
        app = build_http_app(get_settings(), server=server)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            fts_resp = await http_client.get(f"/mail/{alpha_key}", params={"q": "Launch"})
            fts_html = fts_resp.text
            fts_summary = _parse_search_html(fts_html)

        async with get_session() as session:
            await session.execute(text("DROP TABLE IF EXISTS fts_messages"))
            await session.commit()

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            like_resp = await http_client.get(f"/mail/{alpha_key}", params={"q": "\"Launch"})
            like_html = like_resp.text
            like_summary = _parse_search_html(like_html)
        phases.append(
            {
                "phase": "search",
                "fts": fts_summary,
                "like_fallback": like_summary,
            }
        )

        if fts_summary["hits_badge_count"] > 0:
            assert fts_summary["mark_count"] > 0
        assert like_summary["mark_count"] == 0
        assert like_summary["hits_badge_count"] == 0

        phase2_metrics = {
            "pathspec_cache": {
                "hits": phase2_cache_info.hits,
                "misses": phase2_cache_info.misses,
                "ratio": phase2_cache_ratio,
            },
            "commit_batching": {
                "project": phase2_project,
                "commit_delta": phase2_commit_delta,
            },
            "snippet_metrics": {
                "fts": fts_summary,
                "like_fallback": like_summary,
            },
        }
        phases.append({"phase": "phase2_verification", "metrics": phase2_metrics})
        write_log("phase2_verification", phase2_metrics)

        # Recreate FTS tables/triggers so later snapshot scrubbing succeeds.
        reset_database_state()
        await ensure_schema()

        reservations_snapshot = _parse_resource_json(
            await client.read_resource(f"resource://file_reservations/{alpha_key}?active_only=false")
        )
        contacts_raw = _coerce_list(
            _tool_data(
                await client.call_tool("list_contacts", {"project_key": alpha_key, "agent_name": "BlueLake"})
            )
        )
        contacts: list[dict[str, Any]] = []
        for entry in contacts_raw:
            if isinstance(entry, dict):
                contacts.append(entry)
                continue
            if hasattr(entry, "model_dump"):
                try:
                    contacts.append(entry.model_dump(mode="json"))
                except TypeError:
                    contacts.append(entry.model_dump())
                continue
            if hasattr(entry, "dict") and callable(getattr(entry, "dict", None)):
                contacts.append(entry.dict())
        reservations_snapshot = [entry for entry in reservations_snapshot if isinstance(entry, dict)]
        reservations_snapshot.sort(key=lambda entry: entry.get("id") or 0)
        for entry in reservations_snapshot:
            res_id = entry.get("id")
            if isinstance(res_id, int):
                entry["created_ts"] = _iso_at(base_time, offset_seconds=500 + res_id)
                entry["expires_ts"] = _iso_at(base_time, offset_seconds=600 + res_id)
                if entry.get("released_ts") is not None:
                    entry["released_ts"] = _iso_at(base_time, offset_seconds=700 + res_id)
        contacts.sort(key=lambda entry: entry.get("to") or "")
        for idx, entry in enumerate(contacts, start=1):
            if "updated_ts" in entry:
                entry["updated_ts"] = _iso_at(base_time, offset_seconds=900 + idx)
            if "expires_ts" in entry:
                entry["expires_ts"] = _iso_at(base_time, offset_seconds=950 + idx)

        product_resource = _parse_resource_json(
            await client.read_resource(f"resource://product/{product['product_uid']}")
        )
        product_inbox = _tool_data(
            await client.call_tool(
                "fetch_inbox_product",
                {"product_key": product["product_uid"], "agent_name": "BlueLake", "limit": 10, "include_bodies": True},
            )
        )
        product_thread = _tool_data(
            await client.call_tool(
                "summarize_thread_product",
                {"product_key": product["product_uid"], "thread_id": "THREAD-1", "include_examples": True, "llm_mode": False},
            )
        )
        if isinstance(product_resource, dict):
            if product_resource.get("product_uid"):
                product["product_uid"] = product_resource["product_uid"]
            if product_resource.get("created_at"):
                product["created_at"] = product_resource["created_at"]
            for link in (product_link_alpha, product_link_beta):
                if isinstance(link, dict) and isinstance(link.get("product"), dict):
                    link["product"]["product_uid"] = product_resource.get("product_uid")

        settings = get_settings()
        database_path = share.resolve_sqlite_database_path(settings.database.url)
        bundle_root = tmp_path / "bundle"
        snapshot_path = bundle_root / "mailbox.sqlite3"
        inline_threshold = share.INLINE_ATTACHMENT_THRESHOLD
        detach_threshold = share.DETACH_ATTACHMENT_THRESHOLD
        chunk_threshold = share.DEFAULT_CHUNK_THRESHOLD
        chunk_size = share.DEFAULT_CHUNK_SIZE
        snapshot_ctx = share.create_snapshot_context(
            source_database=database_path,
            snapshot_path=snapshot_path,
            project_filters=[],
            scrub_preset="standard",
        )
        summary = share.summarize_snapshot(
            snapshot_path,
            storage_root=Path(settings.storage.root),
            inline_threshold=inline_threshold,
            detach_threshold=detach_threshold,
        )
        hosting_hints = share.detect_hosting_hints(bundle_root)

        fixed_time = datetime(2025, 1, 1, 12, 0, 0)
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return fixed_time
                return fixed_time.replace(tzinfo=tz)

        monkeypatch.setattr(share, "datetime", FixedDatetime)

        bundle_artifacts = share.build_bundle_assets(
            snapshot_ctx.snapshot_path,
            bundle_root,
            storage_root=Path(settings.storage.root),
            inline_threshold=inline_threshold,
            detach_threshold=detach_threshold,
            chunk_threshold=chunk_threshold,
            chunk_size=chunk_size,
            scope=snapshot_ctx.scope,
            project_filters=[],
            scrub_summary=snapshot_ctx.scrub_summary,
            hosting_hints=hosting_hints,
            fts_enabled=snapshot_ctx.fts_enabled,
            export_config={
                "inline_threshold": inline_threshold,
                "detach_threshold": detach_threshold,
                "chunk_threshold": chunk_threshold,
                "chunk_size": chunk_size,
                "scrub_preset": "standard",
                "projects": [],
            },
        )

        _touch_bundle_files(bundle_root, base=1_725_000_000.0)
        preview_status = _collect_preview_status(bundle_root)
        verify = share.verify_bundle(bundle_root)
        phases.append(
            {
                "phase": "share_export",
                "summary": summary,
                "verify": verify,
                "preview_status": preview_status,
            }
        )

        alpha_project_stable = _tool_data(await client.call_tool("ensure_project", {"human_key": "/alpha"}))
        beta_project_stable = _tool_data(await client.call_tool("ensure_project", {"human_key": "/beta"}))

    reservation_times: dict[int, dict[str, Any]] = {
        entry["id"]: entry for entry in reservations_snapshot if isinstance(entry, dict) and entry.get("id") is not None
    }
    reservation_lookup = {
        (entry.get("agent"), entry.get("path_pattern")): entry
        for entry in reservations_snapshot
        if isinstance(entry, dict)
    }
    for item in reservation.get("granted", []):
        if item.get("id") in reservation_times:
            item["expires_ts"] = reservation_times[item["id"]].get("expires_ts")
    for holder in conflict.get("conflicts", []):
        for entry in holder.get("holders", []):
            res_id = entry.get("id")
            if res_id in reservation_times:
                entry["expires_ts"] = reservation_times[res_id].get("expires_ts")
            else:
                key = (entry.get("agent"), entry.get("path_pattern"))
                if key in reservation_lookup:
                    entry["expires_ts"] = reservation_lookup[key].get("expires_ts")
    for item in conflict.get("granted", []):
        if item.get("id") in reservation_times:
            item["expires_ts"] = reservation_times[item["id"]].get("expires_ts")
    for item in post_release.get("granted", []):
        if item.get("id") in reservation_times:
            item["expires_ts"] = reservation_times[item["id"]].get("expires_ts")

    {entry.get("to"): entry for entry in contacts if isinstance(entry, dict)}
    contact_index = {entry.get("to"): idx for idx, entry in enumerate(contacts, start=1) if isinstance(entry, dict)}
    if request_contact.get("to") in contact_index:
        request_contact["expires_ts"] = _iso_at(base_time, offset_seconds=950 + contact_index[request_contact["to"]])
    if approve_contact.get("to") in contact_index:
        approve_contact["expires_ts"] = _iso_at(base_time, offset_seconds=950 + contact_index[approve_contact["to"]])
    if deny_request.get("to") in contact_index:
        deny_request["expires_ts"] = _iso_at(base_time, offset_seconds=950 + contact_index[deny_request["to"]])

    cross_deliveries = cross_send.get("deliveries") if isinstance(cross_send, dict) else None
    if isinstance(cross_deliveries, list) and cross_deliveries:
        payload = cross_deliveries[0].get("payload", {}) if isinstance(cross_deliveries[0], dict) else {}
        message_id = payload.get("id")
        if isinstance(message_id, int):
            payload["created_ts"] = _iso_at(base_time, offset_seconds=200 + message_id)

    replacements = [
        (str(bundle_root), "<bundle_root>"),
        (str(tmp_path), "<tmp_path>"),
        (str(Path(settings.storage.root)), "<storage_root>"),
        (str(snapshot_path), "<snapshot_path>"),
        (str(database_path), "<database_path>"),
        (str(product.get("product_uid") or ""), "<product_uid>"),
        (str(phase2_project), "<phase2_project>"),
    ]

    result = {
        "identity": {
            "ensure_project_alpha": alpha_project_stable,
            "ensure_project_beta": beta_project_stable,
            "project_resource_alpha": project_resource,
            "project_resource_beta": beta_resource,
            "whois": whois,
        },
        "messaging": {
            "message": message_resource,
            "followup": followup_resource,
            "beta_message": beta_message_resource,
        },
        "inbox_outbox": {
            "fetch_inbox_since": inbox_since_ts,
            "fetch_inbox": inbox,
            "mailbox": mailbox,
            "outbox": outbox,
        },
        "search": {
            "tool_results": search_tool,
            "http_fts": fts_summary,
            "http_like_fallback": like_summary,
        },
        "file_reservations": {
            "initial_reservation": reservation,
            "conflict_attempt": conflict,
            "post_release": post_release,
            "snapshot": reservations_snapshot,
        },
        "contacts": {
            "request_contact": request_contact,
            "approve_contact": approve_contact,
            "deny_request": deny_request,
            "deny_contact": deny_contact,
            "cross_send": cross_send,
            "denied_error": denied_error,
            "list_contacts": contacts,
        },
        "product_bus": {
            "product": product,
            "link_alpha": product_link_alpha,
            "link_beta": product_link_beta,
            "resource": product_resource,
            "inbox": product_inbox,
            "thread_summary": product_thread,
        },
        "share_export": {
            "summary": summary,
            "scrub_summary": asdict(snapshot_ctx.scrub_summary),
            "bundle_artifacts": {
                "attachments_manifest": bundle_artifacts.attachments_manifest,
                "chunk_manifest": bundle_artifacts.chunk_manifest,
                "viewer_data": bundle_artifacts.viewer_data,
            },
            "verify": verify,
            "preview_status": preview_status,
        },
    }

    update = os.getenv("E2E_UPDATE", "") == "1"
    assert_matches_golden(
        "isomorphism_e2e",
        result,
        console=console,
        replacements=replacements,
        update=update,
    )

    log_payload = {
        "phases": phases,
        "result": result,
    }
    write_log("isomorphism_e2e", log_payload)
