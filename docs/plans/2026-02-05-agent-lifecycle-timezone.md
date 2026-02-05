# Agent Lifecycle + Local Time Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add soft “dead” agent status with soft-block messaging plus optional local-time formatting in agent listings and UI.

**Architecture:** Store lifecycle status on the Agent row; expose via MCP tools/resources and UI. Keep UTC canonical but optionally return formatted local timestamps when a timezone is supplied.

**Tech Stack:** FastAPI, SQLModel/SQLAlchemy, Jinja templates, MCP tools/resources, Typer CLI.

---

### Task 1: Add lifecycle_status column and schema guard

**Files:**
- Modify: `src/mcp_agent_mail/models.py`
- Modify: `src/mcp_agent_mail/db.py`
- Test: `tests/test_server.py`

**Step 1: Write the failing test**

Add a test that registers an agent, then asserts the Agent dict includes `lifecycle_status == "active"` by default.

```python
async def test_agent_lifecycle_status_default(isolated_env):
    client = isolated_env
    project_key = "/test/lifecycle"
    await client.call_tool("ensure_project", {"human_key": project_key})
    agent = await client.call_tool(
        "register_agent",
        {"project_key": project_key, "program": "codex-cli", "model": "gpt-5", "name": "BlueLake"},
    )
    assert agent.data["lifecycle_status"] == "active"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server.py::test_agent_lifecycle_status_default -v`
Expected: FAIL with missing key `lifecycle_status`.

**Step 3: Write minimal implementation**

- Add `lifecycle_status: str = Field(default="active", max_length=16)` to `Agent`.
- Add `_ensure_agent_lifecycle_columns()` in `db.py` and call from `ensure_schema()`; use `ALTER TABLE agents ADD COLUMN lifecycle_status TEXT` if missing.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server.py::test_agent_lifecycle_status_default -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/mcp_agent_mail/models.py src/mcp_agent_mail/db.py tests/test_server.py
git commit -m "feat: add agent lifecycle status"
```

---

### Task 2: Expose lifecycle status and local time in agent resource output

**Files:**
- Modify: `src/mcp_agent_mail/app.py`
- Test: `tests/test_server.py`

**Step 1: Write the failing test**

Add a test that hits `resource://agents/<project>?tz=Asia/Kolkata` and asserts local fields are present and timezone matches.

```python
async def test_agents_resource_local_time(isolated_env):
    client = isolated_env
    project_key = "/test/agents-tz"
    await client.call_tool("ensure_project", {"human_key": project_key})
    await client.call_tool(
        "register_agent",
        {"project_key": project_key, "program": "codex-cli", "model": "gpt-5", "name": "BlueLake"},
    )
    resource = await client.read_resource(f"resource://agents/{project_key}?tz=Asia/Kolkata")
    payload = json.loads(resource[0].text)
    agent = payload["agents"][0]
    assert agent["timezone"] == "Asia/Kolkata"
    assert "last_active_local" in agent
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server.py::test_agents_resource_local_time -v`
Expected: FAIL with missing `timezone`/`last_active_local`.

**Step 3: Write minimal implementation**

- Add `_format_local(dt, tz)` using `zoneinfo.ZoneInfo` and return `None` if no tz.
- In `agents_directory` and `project_detail`, parse `tz` from query params and pass into `_agent_to_dict`.
- Extend `_agent_to_dict` to include `timezone`, `last_active_local`, `last_heartbeat_local` when tz present.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server.py::test_agents_resource_local_time -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/mcp_agent_mail/app.py tests/test_server.py
git commit -m "feat: add local time fields to agent resources"
```

---

### Task 3: Add MCP tools to mark dead / revive

**Files:**
- Modify: `src/mcp_agent_mail/app.py`
- Test: `tests/test_server.py`

**Step 1: Write the failing test**

```python
async def test_mark_agent_dead_and_revive(isolated_env):
    client = isolated_env
    project_key = "/test/agent-dead"
    await client.call_tool("ensure_project", {"human_key": project_key})
    await client.call_tool(
        "register_agent",
        {"project_key": project_key, "program": "codex-cli", "model": "gpt-5", "name": "BlueLake"},
    )
    dead = await client.call_tool("mark_agent_dead", {"project_key": project_key, "agent_name": "BlueLake"})
    assert dead.data["lifecycle_status"] == "dead"
    alive = await client.call_tool("revive_agent", {"project_key": project_key, "agent_name": "BlueLake"})
    assert alive.data["lifecycle_status"] == "active"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server.py::test_mark_agent_dead_and_revive -v`
Expected: FAIL with tool not found.

**Step 3: Write minimal implementation**

- Add two tools in `app.py`: `mark_agent_dead` and `revive_agent`.
- Update agent row with `lifecycle_status` and refresh.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server.py::test_mark_agent_dead_and_revive -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/mcp_agent_mail/app.py tests/test_server.py
git commit -m "feat: add tools to mark agent dead and revive"
```

---

### Task 4: Soft-block dead recipients in send_message

**Files:**
- Modify: `src/mcp_agent_mail/app.py`
- Test: `tests/test_server.py`

**Step 1: Write the failing test**

```python
async def test_send_message_blocks_dead_recipient(isolated_env):
    client = isolated_env
    project_key = "/test/send-dead"
    await client.call_tool("ensure_project", {"human_key": project_key})
    await client.call_tool(
        "register_agent",
        {"project_key": project_key, "program": "codex-cli", "model": "gpt-5", "name": "GreenLake"},
    )
    await client.call_tool(
        "register_agent",
        {"project_key": project_key, "program": "codex-cli", "model": "gpt-5", "name": "BlueLake"},
    )
    await client.call_tool("mark_agent_dead", {"project_key": project_key, "agent_name": "BlueLake"})
    with pytest.raises(Exception):
        await client.call_tool(
            "send_message",
            {"project_key": project_key, "sender_name": "GreenLake", "to": ["BlueLake"], "subject": "Hi", "body_md": "Test"},
        )
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server.py::test_send_message_blocks_dead_recipient -v`
Expected: FAIL (message goes through).

**Step 3: Write minimal implementation**

- Add `allow_dead_recipients: bool = False` to `send_message` tool signature.
- Before contact checks, batch-load recipients and if any are `dead`, return soft-block error unless override flag is true.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server.py::test_send_message_blocks_dead_recipient -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/mcp_agent_mail/app.py tests/test_server.py
git commit -m "feat: soft-block messages to dead agents"
```

---

### Task 5: CLI commands for agent status

**Files:**
- Modify: `src/mcp_agent_mail/cli.py`
- Test: `tests/test_cli_extended.py`

**Step 1: Write the failing test**

Add a CLI test that marks an agent dead and then lists agents showing status.

```python
def test_cli_agents_dead_and_list(tmp_path, isolated_env, runner):
    # Use existing CLI test scaffolding
    res = runner.invoke(app, ["agents", "dead", str(tmp_path), "BlueLake"])
    assert res.exit_code == 0
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_extended.py::test_cli_agents_dead_and_list -v`
Expected: FAIL (command not found).

**Step 3: Write minimal implementation**

- Add `agents_app = typer.Typer(...)` and `app.add_typer(agents_app, name="agents")`.
- Add `list`, `dead`, and `revive` subcommands.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli_extended.py::test_cli_agents_dead_and_list -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/mcp_agent_mail/cli.py tests/test_cli_extended.py
git commit -m "feat: add agent lifecycle CLI commands"
```

---

### Task 6: UI controls + timezone auto-detect

**Files:**
- Modify: `src/mcp_agent_mail/http.py`
- Modify: `src/mcp_agent_mail/templates/mail_project.html`
- Test: `tests/test_mail_viewer_e2e.py`

**Step 1: Write the failing test**

Add an E2E test or a minimal template render test ensuring status badge + action exists.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mail_viewer_e2e.py::test_agents_status_controls -v`
Expected: FAIL (elements missing).

**Step 3: Write minimal implementation**

- In `http.py`, accept `tz` param in `/mail/{project}` and compute local fields for agents.
- Add POST endpoint `/mail/{project}/agents/{agent}/status`.
- In `mail_project.html`, display local timestamps if present and add “Mark dead / Revive” controls that call the endpoint.
- Add JS to detect browser timezone and reload with `?tz=` once if missing.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mail_viewer_e2e.py::test_agents_status_controls -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/mcp_agent_mail/http.py src/mcp_agent_mail/templates/mail_project.html tests/test_mail_viewer_e2e.py
git commit -m "feat: add agent status controls and local time UI"
```

---

### Task 7: Docs update

**Files:**
- Modify: `README.md`

**Step 1: Update docs**

- Document `tz` query param on `resource://agents/{project_key}`.
- Document `mark_agent_dead`, `revive_agent` tools and `allow_dead_recipients` flag.

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document agent lifecycle and timezone fields"
```

---

### Task 8: Lint + type check

**Step 1: Run lint**

Run: `ruff check --fix --unsafe-fixes`

**Step 2: Run type check**

Run: `uvx ty check`

**Step 3: Commit if any changes**

```bash
git add -A
git commit -m "chore: fix lint and type issues"
```

---

### Task 9: Final verification

**Step 1: Run targeted tests**

Run:
```
uv run pytest tests/test_server.py::test_agent_lifecycle_status_default -v
uv run pytest tests/test_server.py::test_agents_resource_local_time -v
uv run pytest tests/test_server.py::test_mark_agent_dead_and_revive -v
uv run pytest tests/test_server.py::test_send_message_blocks_dead_recipient -v
uv run pytest tests/test_cli_extended.py::test_cli_agents_dead_and_list -v
```

**Step 2: Summarize results**

Record failures (if any) and fix.

**Step 3: Final commit (if needed)**

```bash
git add -A
git commit -m "test: verify agent lifecycle + timezone"
```
