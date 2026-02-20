---
name: agent-mail
description: "MCP Agent Mail - Mail-like coordination layer for multi-agent workflows. Identities, inbox/outbox, file reservations, contact policies, threaded messaging, pre-commit guard, Human Overseer, static exports, disaster recovery. Git+SQLite backed. Python/FastMCP."
---

# MCP Agent Mail

A mail-like coordination layer for coding agents exposed as an HTTP-only FastMCP server. Provides memorable identities, inbox/outbox, file reservation leases, contact policies, searchable message history, and Human Overseer messaging. Backed by Git (human-auditable artifacts) and SQLite (fast queries with FTS5).

## Why This Exists

Without coordination, multiple agents:
- Overwrite each other's edits or panic on unexpected diffs
- Miss critical context from parallel workstreams
- Require humans to relay messages between tools

Agent Mail solves this with:
- Memorable identities (adjective+noun names like "GreenCastle")
- Advisory file reservations to signal editing intent
- Threaded messaging with importance levels and acknowledgments
- Pre-commit guard to enforce reservations at commit time
- Human Overseer for direct human-to-agent communication

## Starting the Server

```bash
# Quickest way (alias added during install)
am

# Or manually
cd ~/projects/mcp_agent_mail
./scripts/run_server_with_token.sh
```

Default: `http://127.0.0.1:8765`
Web UI for humans: `http://127.0.0.1:8765/mail`

## Core Concepts

### Projects
Each working directory (absolute path) is a project. Agents in the same directory share a project namespace. Use the same `project_key` for agents that need to coordinate.

### Agent Identity
Agents register with adjective+noun names (GreenCastle, BlueLake). Names are unique per project, memorable, and appear in inboxes, commit logs, and the web UI.

### File Reservations (Leases)
Advisory locks on file paths or globs. Before editing files, reserve them to signal intent. Other agents see the reservation and can choose different work. The optional pre-commit guard blocks commits that conflict with others' exclusive reservations.

### Contact Policies
Per-agent policies control who can message whom:

| Policy | Behavior |
|--------|----------|
| `open` | Accept any message in the project |
| `auto` (default) | Allow if shared context exists (same thread, overlapping reservations, recent contact) |
| `contacts_only` | Require explicit contact approval first |
| `block_all` | Reject all new contacts |

### Messages
GitHub-Flavored Markdown with threading, importance levels (`low`, `normal`, `high`, `urgent`), and optional acknowledgment requirements. Images are auto-converted to WebP.

## Essential Workflow

### 1. Start Session (One-Call Bootstrap)

```
macro_start_session(
  human_key="/abs/path/to/project",
  program="claude-code",
  model="opus-4.5",
  task_description="Implementing auth module"
)
```

Returns: `{project, agent, file_reservations, inbox}`

This single call: ensures project exists, registers your identity, optionally reserves files, fetches your inbox.

### 2. Reserve Files Before Editing

```
file_reservation_paths(
  project_key="/abs/path/to/project",
  agent_name="GreenCastle",
  paths=["src/auth/**/*.ts", "src/middleware/auth.ts"],
  ttl_seconds=3600,
  exclusive=true,
  reason="bd-123"
)
```

Returns: `{granted: [...], conflicts: [...]}`

Conflicts are reported but reservations are still granted. Check conflicts and coordinate if needed.

### 3. Announce Your Work

```
send_message(
  project_key="/abs/path/to/project",
  sender_name="GreenCastle",
  to=["BlueLake"],
  subject="[bd-123] Starting auth refactor",
  body_md="Reserving src/auth/**. Will update session handling.",
  thread_id="bd-123",
  importance="normal",
  ack_required=true
)
```

### 4. Check Inbox Periodically

```
fetch_inbox(
  project_key="/abs/path/to/project",
  agent_name="GreenCastle",
  limit=20,
  urgent_only=false,
  include_bodies=true
)
```

Or use resources for fast reads:
```
resource://inbox/GreenCastle?project=/abs/path&limit=20&include_bodies=true
```

### 5. Release Reservations When Done

```
release_file_reservations(
  project_key="/abs/path/to/project",
  agent_name="GreenCastle"
)
```

## The Four Macros

Prefer macros for speed and smaller models. Use granular tools when you need fine control.

| Macro | Purpose |
|-------|---------|
| `macro_start_session` | Bootstrap: ensure project → register agent → optional file reservations → fetch inbox |
| `macro_prepare_thread` | Join existing conversation: register → summarize thread → fetch inbox context |
| `macro_file_reservation_cycle` | Reserve files, do work, optionally auto-release when done |
| `macro_contact_handshake` | Request contact permission, optionally auto-accept, send welcome message |

## Beads Integration (bd-### Workflow)

When using Beads for task management, keep identifiers aligned:

```
1. Pick ready work:     bd ready --json → choose bd-123
2. Reserve files:       file_reservation_paths(..., reason="bd-123")
3. Announce start:      send_message(..., thread_id="bd-123", subject="[bd-123] Starting...")
4. Work and update:     Reply in thread with progress
5. Complete:            bd close bd-123
                        release_file_reservations(...)
                        send_message(..., subject="[bd-123] Completed")
```

Use `bd-###` as:
- Mail `thread_id`
- Message subject prefix `[bd-###]`
- File reservation `reason`
- Commit message reference

## Beads Viewer (bv) Integration

Use bv's robot flags for intelligent task selection:

| Flag | Output | Use Case |
|------|--------|----------|
| `bv --robot-insights` | PageRank, critical path, cycles | "What's most impactful?" |
| `bv --robot-plan` | Parallel tracks, unblocks | "What can run in parallel?" |
| `bv --robot-priority` | Recommendations with confidence | "What should I work on next?" |
| `bv --robot-diff --diff-since <ref>` | Changes since commit/date | "What changed?" |

**Rule of thumb:** Use `bd` for task operations, use `bv` for task intelligence.

## Cross-Project Coordination

For frontend/backend or multi-repo projects:

**Option A: Shared project_key**
Both repos use the same `project_key`. Agents coordinate automatically.

**Option B: Separate projects with contact links**
```
# Backend agent requests contact with frontend agent
request_contact(
  project_key="/abs/path/backend",
  from_agent="GreenCastle",
  to_agent="BlueLake",
  to_project="/abs/path/frontend",
  reason="API contract coordination"
)

# Frontend agent accepts
respond_contact(
  project_key="/abs/path/frontend",
  to_agent="BlueLake",
  from_agent="GreenCastle",
  accept=true
)
```

## Pre-Commit Guard

Install the guard to block commits that conflict with others' exclusive reservations:

```
install_precommit_guard(
  project_key="/abs/path/to/project",
  code_repo_path="/abs/path/to/project"
)
```

### Guard Features
- **Composition-safe**: Chain-runner preserves existing hooks in `hooks.d/`
- **Rename-aware**: Checks both old and new paths for renames/moves
- **NUL-safe**: Handles paths with special characters
- **Git-native matching**: Uses Git wildmatch pathspec semantics

Set `AGENT_NAME` environment variable so the guard knows who you are.

Bypass in emergencies: `AGENT_MAIL_BYPASS=1 git commit ...`

## Tools Reference

### Project & Identity

| Tool | Purpose |
|------|---------|
| `ensure_project(human_key)` | Create/ensure project exists |
| `register_agent(project_key, program, model, name?, task_description?)` | Register identity |
| `whois(project_key, agent_name)` | Get agent profile with recent commits |
| `create_agent_identity(project_key, program, model)` | Always create new unique agent |

### Messaging

| Tool | Purpose |
|------|---------|
| `send_message(project_key, sender, to, subject, body_md, ...)` | Send message |
| `reply_message(project_key, message_id, sender, body_md)` | Reply (preserves thread) |
| `fetch_inbox(project_key, agent, limit?, since_ts?, urgent_only?)` | Get messages |
| `mark_message_read(project_key, agent, message_id)` | Mark as read |
| `acknowledge_message(project_key, agent, message_id)` | Acknowledge receipt |
| `search_messages(project_key, query)` | FTS5 search |
| `summarize_thread(project_key, thread_id)` | Extract key points and actions |

### File Reservations

| Tool | Purpose |
|------|---------|
| `file_reservation_paths(project_key, agent, paths, ttl?, exclusive?)` | Reserve files |
| `release_file_reservations(project_key, agent, paths?)` | Release reservations |
| `renew_file_reservations(project_key, agent, extend_seconds?)` | Extend TTL |
| `force_release_file_reservation(project_key, agent, reservation_id)` | Clear stale reservation |

### Contact Management

| Tool | Purpose |
|------|---------|
| `request_contact(project_key, from_agent, to_agent, reason?)` | Request permission to message |
| `respond_contact(project_key, to_agent, from_agent, accept)` | Accept/deny contact request |
| `list_contacts(project_key, agent_name)` | List contact links |
| `set_contact_policy(project_key, agent_name, policy)` | Set open/auto/contacts_only/block_all |

## Resources (Fast Reads)

Use resources for quick, non-mutating reads:

```
resource://inbox/{agent}?project=<path>&limit=20&include_bodies=true
resource://thread/{thread_id}?project=<path>&include_bodies=true
resource://message/{id}?project=<path>
resource://file_reservations/{slug}?active_only=true
resource://project/{slug}
resource://projects
resource://agents/{project_key}
```

## Search Syntax (FTS5)

```
"exact phrase"
prefix*
term1 AND term2
term1 OR term2
subject:login
body:"api key"
(auth OR login) AND NOT admin
```

Example: `search_messages(project_key, '"auth module" AND error NOT legacy')`

## Web UI Features

Browse at `http://127.0.0.1:8765/mail`:

- **Unified inbox** across all projects
- **Per-project search** with FTS5
- **Thread viewer** with markdown rendering
- **File reservations** browser
- **Human Overseer**: Send high-priority messages to agents from the web UI
- **Related Projects Discovery**: AI-powered suggestions for linking repos

### Human Overseer

Send direct messages to agents with automatic preamble:
- Messages marked as `high` importance
- Bypasses contact policies
- Agents are instructed to pause current work, complete request, then resume

## Static Mailbox Export

Export projects to portable, read-only bundles for auditors, stakeholders, or archives:

```bash
# Interactive wizard (recommended)
uv run python -m mcp_agent_mail.cli share wizard

# Manual export
uv run python -m mcp_agent_mail.cli share export --output ./bundle

# With signing
uv run python -m mcp_agent_mail.cli share export \
  --output ./bundle \
  --signing-key ./keys/signing.key

# Preview locally
uv run python -m mcp_agent_mail.cli share preview ./bundle
```

### Export Features
- Ed25519 cryptographic signing
- Age encryption for confidential distribution
- Scrub presets: `standard` (removes secrets) or `strict` (redacts bodies)
- Deploy to GitHub Pages or Cloudflare Pages via wizard

## Disaster Recovery

```bash
# Save current state
uv run python -m mcp_agent_mail.cli archive save --label nightly

# List restore points
uv run python -m mcp_agent_mail.cli archive list --json

# Restore after disaster
uv run python -m mcp_agent_mail.cli archive restore <file>.zip --force
```

## Mailbox Health (Doctor)

```bash
# Run diagnostics
uv run python -m mcp_agent_mail.cli doctor check

# Preview repairs
uv run python -m mcp_agent_mail.cli doctor repair --dry-run

# Apply repairs (creates backup first)
uv run python -m mcp_agent_mail.cli doctor repair
```

Checks: stale locks, database integrity, orphaned records, FTS sync, expired reservations.

## Common Pitfalls

| Error | Fix |
|-------|-----|
| "sender_name not registered" | Call `register_agent` or `macro_start_session` first |
| "FILE_RESERVATION_CONFLICT" | Wait for expiry, coordinate, or use non-exclusive |
| "CONTACT_BLOCKED" | Use `request_contact` and wait for approval |
| Empty inbox | Check `since_ts`, `urgent_only`, verify agent name matches exactly |

## Installation

```bash
# One-liner (recommended)
curl -fsSL "https://raw.githubusercontent.com/Dicklesworthstone/mcp_agent_mail/main/scripts/install.sh?$(date +%s)" | bash -s -- --yes

# Custom port
curl -fsSL ... | bash -s -- --port 9000 --yes

# Change port after installation
uv run python -m mcp_agent_mail.cli config set-port 9000
```

## Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `STORAGE_ROOT` | `~/.mcp_agent_mail_git_mailbox_repo` | Root for repos and SQLite DB |
| `HTTP_PORT` | `8765` | Server port |
| `HTTP_BEARER_TOKEN` | — | Static bearer token for auth |
| `LLM_ENABLED` | `true` | Enable LLM for summaries/discovery |
| `CONTACT_ENFORCEMENT_ENABLED` | `true` | Enforce contact policy |

## Docker

```bash
docker build -t mcp-agent-mail .
docker run --rm -p 8765:8765 \
  -e HTTP_HOST=0.0.0.0 \
  -v agent_mail_data:/data \
  mcp-agent-mail
```

## Integration with Flywheel

| Tool | Integration |
|------|-------------|
| **NTM** | Agent panes coordinate via mail, dashboard shows inbox |
| **BV** | Task IDs become thread IDs, robot flags inform task selection |
| **CASS** | Search mail threads across sessions |
| **CM** | Extract procedural memory from mail archives |
| **DCG** | Mail notifies agents of blocked commands |
| **RU** | Coordinate multi-repo updates via cross-project mail |
