# mcp-agent-mail

A mail-like coordination layer for coding agents, exposed as an HTTP-only FastMCP server. It gives agents memorable identities, an inbox/outbox, searchable message history, and voluntary file-claim “leases” to avoid stepping on each other.

Think of it as asynchronous email + directory + change-intent signaling for your agents, backed by Git (for human-auditable artifacts) and SQLite (for indexing and queries).

Status: Under active development. The design is captured in detail in `project_idea_and_guide.md` (start with the original prompt at the top of that file).

## Why this exists

Modern projects often run multiple coding agents at once (backend, frontend, scripts, infra). Without a shared coordination fabric, agents:

- Overwrite each other’s edits or panic on unexpected diffs
- Miss critical context from parallel workstreams
- Require humans to “liaison” messages across tools and teams

This project provides a lightweight, interoperable layer so agents can:

- Register a temporary-but-persistent identity (e.g., GreenCastle)
- Send/receive GitHub-Flavored Markdown messages with images
- Search, summarize, and thread conversations
- Declare advisory claims (leases) on files/globs to signal intent
- Inspect a directory of active agents, programs/models, and activity

It’s designed for: FastMCP clients and CLI tools (Claude Code, Codex, Gemini CLI, etc.) coordinating across one or more codebases.

## Core ideas (at a glance)

- HTTP-only FastMCP server (Streamable HTTP). No SSE, no STDIO.
- Dual persistence model:
  - Human-readable markdown in a per-project Git repo for every canonical message and per-recipient inbox/outbox copy
  - SQLite with FTS5 for fast search, directory queries, and claims/leases
- “Directory/LDAP” style queries for agents; memorable adjective+noun names
- Advisory claims for editing surfaces; optional pre-commit guard
- Resource layer for convenient reads (e.g., `resource://inbox/{agent}`)

## Typical use cases

- Multiple agents splitting a large refactor across services while staying in sync
- Frontend and backend teams of agents coordinating thread-by-thread
- Protecting critical migrations with exclusive claims and a pre-commit guard
- Searching and summarizing long technical discussions as threads evolve

## Architecture

```
Coding Agents (various CLIs)
        |  HTTP (Streamable) tools/resources
        v
  mcp-agent-mail (FastMCP server)
        |                                   
        | writes/reads                             indexes/queries
        v                                          v
Per-project Git repo (.mcp-mail/)           SQLite (FTS5)
  ├─ agents/<AgentName>/{inbox,outbox}/     agents/messages/claims
  ├─ agents/<AgentName>/profile.json
  ├─ messages/YYYY/MM/<msg-id>.md (canonical)
  └─ claims/<sha1-of-path>.json
```

### On-disk layout (per project)

```
<store>/projects/<slug>/repo/
  agents/<AgentName>/profile.json
  agents/<AgentName>/inbox/YYYY/MM/<msg-id>.md
  agents/<AgentName>/outbox/YYYY/MM/<msg-id>.md
  messages/YYYY/MM/<msg-id>.md
  claims/<sha1-of-path>.json
  attachments/<xx>/<sha1>.webp
```

### Message file format

Messages are GitHub-Flavored Markdown with JSON frontmatter (fenced by `---json`). Attachments are either WebP files referenced by relative path or inline base64 WebP data URIs.

```markdown
---json
{
  "id": "msg_20251023_7b3dc3a7",
  "thread_id": "TKT-123",
  "project": "/abs/path/backend",
  "from": "GreenCastle",
  "to": ["BlueLake"],
  "cc": [],
  "created": "2025-10-23T15:22:14Z",
  "importance": "high",
  "ack_required": true,
  "attachments": [
    {"type": "file", "media_type": "image/webp", "path": "attachments/2a/2a6f.../diagram.webp"}
  ]
}
---

# Build plan for /api/users routes

... body markdown ...
```

### Data model (SQLite)

- `projects(id, human_key, slug, created_ts, meta)`
- `agents(id, project_id, name, program, model, inception_ts, task, last_active_ts)`
- `messages(id, project_id, thread_id, subject, body_md, from_agent, created_ts, importance, ack_required, attachments_json)`
- `message_recipients(message_id, agent_name, kind, read_ts, ack_ts)`
- `claims(id, project_id, agent_name, path, exclusive, reason, created_ts, expires_ts, released_ts)`
- `fts_messages(subject, body_md)` + triggers for incremental updates

## How it works (key flows)

1) Create an identity

- `create_agent(project_key, program, model, task_description, name_hint?)` → creates a memorable name, stores to DB, writes `agents/<Name>/profile.json` in Git, and commits.

2) Send a message

- `send_message(project_key, from_agent, to[], subject, body_md, cc?, bcc?, importance?, ack_required?, thread_id?, convert_images?)`
- Writes a canonical message under `messages/YYYY/MM/`, an outbox copy for the sender, and inbox copies for each recipient; commits all artifacts.
- Optionally converts images (local paths or data URIs) to WebP and embeds small ones inline.

3) Check inbox

- `check_my_messages(project_key, agent_name, since_ts?, urgent_only?, include_bodies?, limit?)` returns recent messages, preserving thread_id where available.
- `acknowledge_message(project_key, agent_name, message_id)` marks acknowledgements.

4) Avoid conflicts with claims (leases)

- `claim_paths(project_key, agent_name, paths[], ttl_seconds, exclusive, reason)` records an advisory lease in DB and writes JSON claim artifacts in Git; conflicts are reported if overlapping active exclusives exist.
- `release_claims(project_key, agent_name, paths[])` releases active leases. JSON artifacts remain for audit history.
- Optional: install a pre-commit hook in your code repo that blocks commits conflicting with other agents’ active exclusive claims.

5) Search & summarize

- `search_messages(project_key, query, limit?)` uses FTS5 over subject and body.
- `summarize_thread(project_key, thread_id, include_examples?)` extracts key points, actions, and participants from the thread.
- `reply_message(project_key, from_agent, reply_to_message_id, body_md, ...)` creates a subject-prefixed reply, preserving or creating a thread.

## Tools (MCP surface)

| Tool | Purpose |
| :-- | :-- |
| `create_agent(...)` | Register a new agent identity and write `profile.json` in Git |
| `whois(project_key, agent_name)` | Fetch a profile for one agent |
| `list_agents(project_key, active_only=True)` | Directory-style listing of agents and activity |
| `send_message(...)` | Create canonical + inbox/outbox markdown artifacts and commit |
| `reply_message(...)` | Reply to an existing message and continue the thread |
| `check_my_messages(...)` | Pull recent messages for an agent |
| `acknowledge_message(...)` | Mark a message as acknowledged by agent |
| `claim_paths(...)` | Request advisory leases on files/globs |
| `release_claims(...)` | Release existing leases |
| `search_messages(...)` | FTS5 search over subject/body |
| `summarize_thread(...)` | Extract summary/action items across a thread |
| `install_precommit_guard(project_key, code_repo_path)` | Install a Git pre-commit guard in a target repo |
| `uninstall_precommit_guard(code_repo_path)` | Remove the guard |

## Resource layer (read-only URIs)

Expose common reads as resources that clients can fetch:

- `resource://inbox/{agent}{?project,since_ts,urgent_only,include_bodies,limit}`
- `resource://message/{id}{?project}`
- `resource://thread/{thread_id}{?project,include_bodies}`

Example (conceptual) resource read:

```json
{
  "method": "resources/read",
  "params": {"uri": "resource://inbox/BlueLake?project=/abs/path/backend&limit=20"}
}
```

## Claims and the optional pre-commit guard

Exclusive claims are advisory but visible and auditable:

- A claim JSON is written to `claims/<sha1(path)>.json` capturing holder, pattern, exclusivity, created/expires
- The pre-commit guard scans active exclusive claims and blocks commits that touch conflicting paths held by another agent
- Agents should set `AGENT_NAME` (or rely on `GIT_AUTHOR_NAME`) so the guard knows who “owns” the commit

Install the guard into a code repo (conceptual tool call):

```json
{
  "method": "tools/call",
  "params": {
    "name": "install_precommit_guard",
    "arguments": {
      "project_key": "/abs/path/backend",
      "code_repo_path": "/abs/path/backend"
    }
  }
}
```

## Configuration

Configuration is loaded from an existing `.env` via `python-decouple`. Do not use `os.getenv` or auto-dotenv loaders.

```python
from decouple import Config as DecoupleConfig, RepositoryEnv

decouple_config = DecoupleConfig(RepositoryEnv(".env"))

MCP_MAIL_STORE = decouple_config("MCP_MAIL_STORE", default="~/.mcp-agent-mail")
HTTP_HOST = decouple_config("HOST", default="127.0.0.1")
HTTP_PORT = int(decouple_config("PORT", default=8765))
```

Common variables you may set:

- `MCP_MAIL_STORE`: Root for per-project archives and SQLite (default: `~/.mcp-agent-mail`)
- `HOST` / `PORT`: Server bind host/port when running HTTP transport
- Optional: any future limits (attachment max bytes, etc.)

## Development quick start

This repository targets Python 3.14 and uses `uv` with a virtual environment. We manage dependencies via `pyproject.toml` only.

```bash
uv venv --python 3.14
source .venv/bin/activate  # or use direnv
uv sync --dev
```

Run the server (HTTP-only). Depending on your entrypoint, one of the following patterns will apply when the implementation is in place:

```bash
# If the project exposes a CLI entry (example):
uv run mcp-agent-mail/cli.py serve-http

# Or a Python module entry:
uv run python -m mcp_agent_mail serve-http

# Or a direct script entry:
uv run python server.py
```

Connect with your MCP client using the HTTP (Streamable HTTP) transport on the configured host/port.

## Design choices and rationale

- **HTTP-only FastMCP**: Streamable HTTP is the modern remote transport; SSE is legacy; STDIO is not exposed here by design
- **Git + Markdown**: Human-auditable, diffable artifacts that fit developer workflows (inbox/outbox mental model)
- **SQLite + FTS5**: Efficient indexing/search with minimal ops footprint
- **Advisory claims**: Make intent explicit and reviewable; optional guard enforces claims at commit time
- **WebP attachments**: Compact images by default; inline embedding keeps small diagrams in context

## Examples (conceptual tool calls)

Create an agent:

```json
{
  "method": "tools/call",
  "params": {
    "name": "create_agent",
    "arguments": {
      "project_key": "/abs/path/backend",
      "program": "codex-cli",
      "model": "gpt5-codex",
      "task_description": "Auth refactor"
    }
  }
}
```

Send a message (auto-convert images to WebP; inline small ones):

```json
{
  "method": "tools/call",
  "params": {
    "name": "send_message",
    "arguments": {
      "project_key": "/abs/path/backend",
      "from_agent": "GreenCastle",
      "to": ["BlueLake"],
      "subject": "Plan for /api/users",
      "body_md": "Here is the flow...\n\n![diagram](docs/flow.png)",
      "convert_images": true,
      "image_embed_policy": "auto",
      "inline_max_bytes": 32768
    }
  }
}
```

Claim a surface for editing:

```json
{
  "method": "tools/call",
  "params": {
    "name": "claim_paths",
    "arguments": {
      "project_key": "/abs/path/backend",
      "agent_name": "GreenCastle",
      "paths_list": ["app/api/*.py"],
      "ttl_seconds": 7200,
      "exclusive": true,
      "reason": "migrations"
    }
  }
}
```

## Operational notes

- One async session per request/task; don’t share across concurrent coroutines
- Use explicit loads in async code; avoid implicit lazy loads
- Use async-friendly file operations when needed; Git operations are serialized with a file lock
- Clean shutdown should dispose any async engines/resources (if introduced later)

## Roadmap (selected)

See `TODO.md` for the in-progress task list, including:

- Filesystem archive and Git integration hardening (locks, authoring, commits)
- Agent identity workflow polish (uniqueness, activity tracking)
- Messaging enhancements (replies, read/ack semantics, urgent-only)
- Claims/leases (overlap detection, releases, resources)
- Resources for inbox, thread, message, claims
- Search UI and thread summaries
- Config/auth/CLI and health endpoints
- Tests for archive, claims, search, CLI

---

If you’re building with or contributing to this project, please read `project_idea_and_guide.md` for full design context and the motivation behind these decisions. Contributions that preserve the clean, HTTP-only FastMCP approach and the Git+SQLite dual persistence model are welcome.
