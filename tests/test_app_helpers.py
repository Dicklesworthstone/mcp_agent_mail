from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest
from fastmcp import Client, Context

from mcp_agent_mail.app import (
    ToolExecutionError,
    _enforce_capabilities,
    _iso,
    _latest_filesystem_activity,
    _latest_git_activity,
    _parse_iso,
    _parse_json_safely,
    _reservation_repo_pathspec,
    build_mcp_server,
)


def test_iso_and_parse_helpers():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert _iso(now).endswith("+00:00")
    assert _iso(now.isoformat()).endswith("+00:00")
    assert _iso("not-iso") == "not-iso"

    parsed = _parse_iso("2025-01-01T00:00:00Z")
    assert parsed is not None and parsed.year == 2025
    assert _parse_iso("bad-value") is None

    raw = '{"a": 1}'
    assert _parse_json_safely(raw) == {"a": 1}
    fenced = """```json\n{\n  \"x\": 2\n}\n```"""
    assert _parse_json_safely(fenced) == {"x": 2}
    noisy = "xxx {\n \"y\": 3\n} yyy"
    assert _parse_json_safely(noisy) == {"y": 3}


def test_enforce_capabilities_denied():
    # Minimal stand-in that matches the Context metadata surface
    class DummyCtx:
        def __init__(self):
            self.metadata = {"allowed_capabilities": ["read", "audit"]}

    # Call through and expect a ToolExecutionError with explanatory message
    with pytest.raises(ToolExecutionError) as exc:
        _enforce_capabilities(cast(Context, DummyCtx()), {"write"}, "send_message")
    assert "requires capabilities" in str(exc.value)


def test_latest_filesystem_activity_returns_max(tmp_path) -> None:
    older = tmp_path / "older.txt"
    newer = tmp_path / "newer.txt"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")

    old_ts = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
    new_ts = datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp()
    os.utime(older, (old_ts, old_ts))
    os.utime(newer, (new_ts, new_ts))

    latest = _latest_filesystem_activity([older, newer])

    assert latest is not None
    assert latest == datetime.fromtimestamp(new_ts, tz=timezone.utc)


def test_latest_filesystem_activity_early_exits_on_recent(tmp_path) -> None:
    # The sweeper only needs to know whether *any* match is recent; once a
    # recent mtime is seen it must stop, not stat the rest of a 56k-file glob
    # expansion on the event loop (#240). Prove the scan stops: the first file
    # is recent (inside the grace window) but the second is *even more* recent.
    # If the scan stopped at the first, the returned max is the first's mtime;
    # if it kept going it would observe the larger second mtime instead.
    now = datetime.now(timezone.utc)
    first_recent = tmp_path / "a_first_recent.txt"
    second_more_recent = tmp_path / "b_more_recent.txt"
    first_recent.write_text("x", encoding="utf-8")
    second_more_recent.write_text("y", encoding="utf-8")

    first_ts = (now - timedelta(seconds=100)).timestamp()
    second_ts = now.timestamp()
    os.utime(first_recent, (first_ts, first_ts))
    os.utime(second_more_recent, (second_ts, second_ts))
    recent_after = now - timedelta(seconds=300)

    latest = _latest_filesystem_activity(
        [first_recent, second_more_recent], recent_after=recent_after
    )

    # Returned the first (recent) mtime, NOT the larger second one -> stopped early.
    assert latest is not None
    assert latest == datetime.fromtimestamp(first_ts, tz=timezone.utc)
    assert latest < datetime.fromtimestamp(second_ts, tz=timezone.utc)

    # And without a recent_after window it must still scan all and return the max.
    assert _latest_filesystem_activity(
        [first_recent, second_more_recent]
    ) == datetime.fromtimestamp(second_ts, tz=timezone.utc)


def test_reservation_repo_pathspec_glob_and_exact(tmp_path) -> None:
    git = pytest.importorskip("git")
    repo = git.Repo.init(tmp_path)
    workspace = Path(tmp_path)

    # Glob pattern -> single `:(glob)` magic pathspec (one rev walk, #240).
    assert (
        _reservation_repo_pathspec(repo, workspace, "frontend/**")
        == ":(glob)frontend/**"
    )
    # Exact path -> plain repo-relative pathspec (no magic needed).
    assert _reservation_repo_pathspec(repo, workspace, "README.md") == "README.md"
    # Virtual namespaces have no git presence.
    assert _reservation_repo_pathspec(repo, workspace, "tool://playwright") is None


def test_latest_git_activity_single_glob_walk(tmp_path) -> None:
    git = pytest.importorskip("git")
    repo = git.Repo.init(tmp_path)

    # Two files under a broad glob (one nested like node_modules), one outside.
    (tmp_path / "frontend" / "deep" / "pkg").mkdir(parents=True)
    (tmp_path / "frontend" / "a.js").write_text("1", encoding="utf-8")
    (tmp_path / "frontend" / "deep" / "pkg" / "b.js").write_text("1", encoding="utf-8")
    (tmp_path / "other.txt").write_text("1", encoding="utf-8")
    repo.index.add(["frontend/a.js", "frontend/deep/pkg/b.js", "other.txt"])
    tree_commit = repo.index.commit("init")

    pathspec = _reservation_repo_pathspec(repo, Path(tmp_path), "frontend/**")
    activity = _latest_git_activity(repo, pathspec)
    assert activity is not None
    assert activity == datetime.fromtimestamp(
        tree_commit.committed_date, tz=timezone.utc
    )

    # A later commit touching ONLY a path outside the glob must not move the
    # reported activity for `frontend/**` (semantic equivalence to per-file max).
    import time

    time.sleep(1.1)
    (tmp_path / "other.txt").write_text("2", encoding="utf-8")
    repo.index.add(["other.txt"])
    repo.index.commit("touch other")
    after = _latest_git_activity(repo, pathspec)
    assert after == datetime.fromtimestamp(tree_commit.committed_date, tz=timezone.utc)


@pytest.mark.asyncio
async def test_tool_metrics_resource_populates_after_calls(isolated_env):
    server = build_mcp_server()
    async with Client(server) as client:
        # call a couple tools to increment metrics
        res = await client.call_tool("health_check", {})
        assert res.data["status"] == "ok"
        await client.call_tool("ensure_project", {"human_key": "/backend"})

        # tooling metrics resource
        metrics_blocks = await client.read_resource("resource://tooling/metrics")
        assert metrics_blocks and metrics_blocks[0].text
        # the text is JSON; ensure tools list contains health_check
        assert "health_check" in metrics_blocks[0].text

