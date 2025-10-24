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

```mermaid
flowchart LR
  A[Agents (CLIs: Claude Code, Codex, Gemini CLI, ...)]
  S[mcp-agent-mail (FastMCP, HTTP-only)]
  G[(Per-project Git repo: .mcp-mail/)]
  Q[(SQLite + FTS5)]

  A -- tools/resources (HTTP) --> S
  S -- writes/reads --> G
  S -- indexes/queries --> Q

  subgraph GitTree[Git tree]
    GI1[agents/<Agent>/profile.json]
    GI2[agents/<Agent>/{inbox,outbox}/...]
    GI3[messages/YYYY/MM/<msg-id>.md]
    GI4[claims/<sha1(path)>.json]
    GA[attachments/<xx>/<sha1>.webp]
  end

  G --- GI1
  G --- GI2
  G --- GI3
  G --- GI4
  G --- GA
```

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

### Concurrency and lifecycle

- One request/task = one isolated operation; Git writes are serialized by a lock file in the project repo root
- DB operations are short-lived and scoped to each tool call; FTS triggers keep the search index current
- Artifacts are written first, then committed as a cohesive unit with a descriptive message
- Attachments are content-addressed (sha1) to avoid duplication

## How it works (key flows)

1) Create an identity

- `register_agent(project_key, program, model, name?, task_description?)` → creates/updates a named identity, persists profile to Git, and commits.

2) Send a message

- `send_message(project_key, from_agent, to[], subject, body_md, cc?, bcc?, importance?, ack_required?, thread_id?, convert_images?)`
- Writes a canonical message under `messages/YYYY/MM/`, an outbox copy for the sender, and inbox copies for each recipient; commits all artifacts.
- Optionally converts images (local paths or data URIs) to WebP and embeds small ones inline.

```mermaid
sequenceDiagram
  participant Agent as Agent (e.g., GreenCastle)
  participant Server as FastMCP Server
  participant DB as SQLite (messages, recipients, FTS)
  participant Git as Git Repo (.mcp-mail/)

  Agent->>Server: tools/call send_message(project_key, from_agent, to[], subject, body_md, ...)
  Server->>DB: validate sender, insert into messages, recipients
  DB-->>Server: OK (message id, timestamps)
  Server->>Git: write canonical markdown under messages/YYYY/MM/<id>.md
  Server->>Git: write outbox copy under agents/<from>/outbox/
  Server->>Git: write inbox copies under agents/<to>/inbox/
  Server->>Git: commit all paths with message summary
  Server-->>Agent: { id, created, subject, recipients, attachments }
```

3) Check inbox

- `check_my_messages(project_key, agent_name, since_ts?, urgent_only?, include_bodies?, limit?)` returns recent messages, preserving thread_id where available.
- `acknowledge_message(project_key, agent_name, message_id)` marks acknowledgements.

4) Avoid conflicts with claims (leases)

- `claim_paths(project_key, agent_name, paths[], ttl_seconds, exclusive, reason)` records an advisory lease in DB and writes JSON claim artifacts in Git; conflicts are reported if overlapping active exclusives exist.
- `release_claims(project_key, agent_name, paths[])` releases active leases. JSON artifacts remain for audit history.
- Optional: install a pre-commit hook in your code repo that blocks commits conflicting with other agents’ active exclusive claims.

```mermaid
sequenceDiagram
  participant Agent as Agent
  participant Server as FastMCP Server
  participant DB as SQLite (claims)
  participant Git as Git Repo (.mcp-mail/claims)

  Agent->>Server: tools/call claim_paths(project_key, agent_name, paths[], ttl, exclusive, reason)
  Server->>DB: expire old leases; check overlaps for each path
  DB-->>Server: conflicts/grants
  alt conflicts exist
    Server-->>Agent: { conflicts: [...], granted: [], expires_ts }
  else no conflicts
    Server->>DB: insert claim rows (one per path)
    Server->>Git: write claims/<sha1(path)>.json for each granted path
    Server->>Git: commit "claim: <agent> exclusive/shared <n> path(s)"
    Server-->>Agent: { granted: [..], conflicts: [], expires_ts }
  end
```

5) Search & summarize

- `search_messages(project_key, query, limit?)` uses FTS5 over subject and body.
- `summarize_thread(project_key, thread_id, include_examples?)` extracts key points, actions, and participants from the thread.
- `reply_message(project_key, from_agent, reply_to_message_id, body_md, ...)` creates a subject-prefixed reply, preserving or creating a thread.

### Semantics & invariants

- Identity
  - Names are memorable adjective+noun and unique per project; `name_hint` is sanitized (alnum) and used if available
  - `whois` returns the stored profile; `list_agents` can filter by recent activity
  - `last_active_ts` is bumped on relevant interactions (messages, inbox checks, etc.)
- Threads
  - Replies inherit `thread_id` from the original; if missing, the reply sets `thread_id` to the original message id
  - Subject lines are prefixed (e.g., `Re:`) for readability in mailboxes
- Attachments
  - Image references (file path or data URI) are converted to WebP; small images embed inline when policy allows
  - Non-absolute paths resolve relative to the project repo root
  - Stored under `attachments/<xx>/<sha1>.webp` and referenced by relative path in frontmatter
- Claims
  - TTL-based; exclusive means “please don’t modify overlapping surfaces” for others until expiry or release
  - Conflict detection is per exact path pattern; shared claims can coexist, exclusive conflicts are surfaced
  - JSON artifacts remain in Git for audit even after release (DB tracks release_ts)
- Search
  - External-content FTS virtual table and triggers index subject/body on insert/update/delete
  - Queries are constrained to the project id and ordered by `created_ts DESC`

## Tools (MCP surface)

| Tool | Purpose |
| :-- | :-- |
| `register_agent(...)` | Register a new agent identity and write `profile.json` in Git |
| `whois(project_key, agent_name)` | Fetch a profile for one agent |
| `list_agents(project_key, active_only=True)` | Directory-style listing of agents and activity |
| `send_message(...)` | Create canonical + inbox/outbox markdown artifacts and commit |
| `reply_message(...)` | Reply to an existing message and continue the thread |
| `request_contact(project_key, from_agent, to_agent, reason?, ttl_seconds?)` | Request permission to message another agent |
| `respond_contact(project_key, to_agent, from_agent, accept, ttl_seconds?)` | Approve or deny a contact request |
| `list_contacts(project_key, agent_name)` | List contact links for an agent |
| `set_contact_policy(project_key, agent_name, policy)` | Set policy: `open`, `auto` (default), `contacts_only`, `block_all` |
## Contact model and “consent-lite” messaging

Goal: make coordination “just work” without spam across unrelated agents. The server enforces per-project isolation by default and adds an optional consent layer within a project so agents only contact relevant peers.

### Isolation by project

- All tools require a `project_key`. Agents only see messages addressed to them within that project.
- An agent working in Project A is invisible to agents in Project B unless explicit cross-project contact is established (see below). This avoids distraction between unrelated repositories.

### Policies (per agent)

- `open`: accept any targeted messages in the project.
- `auto` (default): allow messages when there is obvious shared context (e.g., same thread participants; recent overlapping active claims; recent prior direct contact within a TTL); otherwise requires a contact request.
- `contacts_only`: require an approved contact link first.
- `block_all`: reject all new contacts (errors with CONTACT_BLOCKED).

Use `set_contact_policy(project_key, agent_name, policy)` to update.

### Request/approve contact

- `request_contact(project_key, from_agent, to_agent, reason?, ttl_seconds?)` creates or refreshes a pending link and sends a small ack_required “intro” message to the recipient.
- `respond_contact(project_key, to_agent, from_agent, accept, ttl_seconds?)` approves or denies; approval grants messaging until expiry.
- `list_contacts(project_key, agent_name)` surfaces current links.

### Auto-allow heuristics (no explicit request required)

- Same thread: replies or messages to thread participants are allowed.
- Recent overlapping claims: if sender and recipient hold active claims in the project, messaging is allowed.
- Recent prior contact: a sliding TTL allows follow-ups between the same pair.

These heuristics minimize friction while preventing cold spam.

### Cross-project coordination (frontend vs backend repos)

When two repos represent the same underlying project (e.g., `frontend` and `backend`), you have two options:

1) Use the same `project_key` across both workspaces. Agents in both repos operate under one project namespace and benefit from full inbox/outbox coordination automatically.

2) Keep separate `project_key`s and establish explicit contact:
   - In `backend`, agent `GreenCastle` calls:
     - `request_contact(project_key="/abs/path/backend", from_agent="GreenCastle", to_agent="BlueLake", reason="API contract changes")`
   - In `frontend`, `BlueLake` calls:
     - `respond_contact(project_key="/abs/path/backend", to_agent="BlueLake", from_agent="GreenCastle", accept=true)`
   - After approval, messages can be exchanged; in default `auto` policy the server allows follow-up threads/claims-based coordination without re-requesting.

Important: You can also create reciprocal links or set `open` policy for trusted pairs. The consent layer is on by default (CONTACT_ENFORCEMENT_ENABLED=true) but is designed to be non-blocking in obvious collaboration contexts.

| `check_my_messages(...)` | Pull recent messages for an agent |
| `acknowledge_message(...)` | Mark a message as acknowledged by agent |
| `claim_paths(...)` | Request advisory leases on files/globs |
| `release_claims(...)` | Release existing leases |
| `search_messages(...)` | FTS5 search over subject/body |
| `summarize_thread(...)` | Extract summary/action items across a thread |
| `summarize_threads(...)` | Digest across multiple threads (optional LLM refinement) |
| `install_precommit_guard(project_key, code_repo_path)` | Install a Git pre-commit guard in a target repo |
| `uninstall_precommit_guard(code_repo_path)` | Remove the guard |

## Resource layer (read-only URIs)

Expose common reads as resources that clients can fetch:

- `resource://inbox/{agent}{?project,since_ts,urgent_only,include_bodies,limit}`
- `resource://message/{id}{?project}`
- `resource://thread/{thread_id}{?project,include_bodies}`
- `resource://views/urgent-unread/{agent}{?project,limit}`
- `resource://views/ack-required/{agent}{?project,limit}`
- `resource://views/ack-overdue/{agent}{?project,ttl_minutes,limit}`: ack-required messages older than TTL without acknowledgements
- `resource://mailbox-with-commits/{agent}{?project,limit}`: inbox items enriched with per-message commit metadata and diff summaries (recommended)
- `resource://mailbox/{agent}{?project,limit}`: recent inbox items with a basic/heuristic commit reference (legacy/simple)

Example (conceptual) resource read:

```json
{
  "method": "resources/read",
  "params": {"uri": "resource://inbox/BlueLake?project=/abs/path/backend&limit=20"}
}
```

### Resource parameters

- `resource://inbox/{agent}{?project,since_ts,urgent_only,include_bodies,limit}`
  - `project`: disambiguate if the same agent name exists in multiple projects; if omitted, the most recent agent activity determines the project
  - `since_ts`: epoch seconds filter (defaults to 0)
  - `urgent_only`: when true, only `importance in ('high','urgent')`
  - `include_bodies`: include markdown bodies in results
  - `limit`: max results (default 20)
- `resource://message/{id}{?project}`: fetch one message; `project` optional if id is globally unique
- `resource://thread/{thread_id}{?project,include_bodies}`: list a thread’s messages; if `project` omitted, resolves to the most recent matching project

```mermaid
sequenceDiagram
  participant Client as MCP Client
  participant Server as FastMCP Server
  participant DB as SQLite

  Client->>Server: resources/read resource://inbox/BlueLake?project=/abs/backend&limit=20
  Server->>DB: select messages joined with recipients for agent=BlueLake
  DB-->>Server: rows
  Server-->>Client: { project, agent, messages: [...] }
```

- `resource://views/urgent-unread/{agent}{?project,limit}`: high/urgent importance messages where `read_ts` is null
- `resource://views/ack-required/{agent}{?project,limit}`: messages with `ack_required=true` where `ack_ts` is null

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

### Configuration reference

| Name | Default | Description |
| :-- | :-- | :-- |
| `MCP_MAIL_STORE` | `~/.mcp-agent-mail` | Root for per-project repos and SQLite DB |
| `HTTP_HOST` | `127.0.0.1` | Bind host for HTTP transport |
| `HTTP_PORT` | `8765` | Bind port for HTTP transport |
| `HTTP_PATH` | `/mcp/` | HTTP path where MCP endpoint is mounted |
| `HTTP_JWT_ENABLED` | `false` | Enable JWT validation middleware |
| `HTTP_JWT_SECRET` |  | HMAC secret for HS* algorithms (dev) |
| `HTTP_JWT_JWKS_URL` |  | JWKS URL for public key verification |
| `HTTP_JWT_ALGORITHMS` | `HS256` | CSV of allowed algs |
| `HTTP_JWT_AUDIENCE` |  | Expected `aud` (optional) |
| `HTTP_JWT_ISSUER` |  | Expected `iss` (optional) |
| `HTTP_JWT_ROLE_CLAIM` | `role` | Claim name containing role(s) |
| `HTTP_RBAC_ENABLED` | `true` | Enforce read-only vs tools RBAC |
| `HTTP_RBAC_READER_ROLES` | `reader,read,ro` | CSV of reader roles |
| `HTTP_RBAC_WRITER_ROLES` | `writer,write,tools,rw` | CSV of writer roles |
| `HTTP_RBAC_DEFAULT_ROLE` | `reader` | Role used when none present |
| `HTTP_RBAC_READONLY_TOOLS` | see code | CSV of read-only tool names |
| `HTTP_RATE_LIMIT_ENABLED` | `false` | Enable token-bucket limiter |
| `HTTP_RATE_LIMIT_BACKEND` | `memory` | `memory` or `redis` |
| `HTTP_RATE_LIMIT_PER_MINUTE` | `60` | Legacy per-IP limit (fallback) |
| `HTTP_RATE_LIMIT_TOOLS_PER_MINUTE` | `60` | Per-minute for tools/call |
| `HTTP_RATE_LIMIT_TOOLS_BURST` | `0` | Optional burst for tools (0=auto=rpm) |
| `HTTP_RATE_LIMIT_RESOURCES_PER_MINUTE` | `120` | Per-minute for resources/read |
| `HTTP_RATE_LIMIT_RESOURCES_BURST` | `0` | Optional burst for resources (0=auto=rpm) |
| `HTTP_RATE_LIMIT_REDIS_URL` |  | Redis URL for multi-worker limits |
| `HTTP_REQUEST_LOG_ENABLED` | `false` | Print request logs (Rich + JSON) |
| `LOG_JSON_ENABLED` | `false` | Output structlog JSON logs |
| `IMAGE_INLINE_MAX_BYTES` | `65536` | Threshold for inlining WebP images during send_message (if enabled) |
| `KEEP_ORIGINAL_IMAGES` | `false` | Also store original image bytes alongside WebP (attachments/originals/) |
| `LOG_LEVEL` | `info` | Future: server log level |
| `ATTACHMENT_POLICY` | `auto` | Future: `auto`, `file`, or `inline` default for image conversion |
| `HTTP_CORS_ENABLED` | `false` | Enable CORS middleware when true |
| `HTTP_CORS_ORIGINS` |  | CSV of allowed origins (e.g., `https://app.example.com,https://ops.example.com`) |
| `HTTP_CORS_ALLOW_CREDENTIALS` | `false` | Allow credentials on CORS |
| `HTTP_CORS_ALLOW_METHODS` | `*` | CSV of allowed methods or `*` |
| `HTTP_CORS_ALLOW_HEADERS` | `*` | CSV of allowed headers or `*` |
| `KEEP_ORIGINAL_IMAGES` | `false` | Store original attachment bytes alongside WebP |
| `CLAIMS_ENFORCEMENT_ENABLED` | `true` | Block message writes on conflicting claims |
| `ACK_TTL_ENABLED` | `false` | Enable overdue ACK scanning |
| `ACK_TTL_SECONDS` | `1800` | Age threshold (seconds) for overdue ACKs |
| `ACK_TTL_SCAN_INTERVAL_SECONDS` | `60` | Scan interval for overdue ACKs |
| `ACK_ESCALATION_ENABLED` | `false` | Enable escalation for overdue ACKs |
| `ACK_ESCALATION_MODE` | `log` | `log` or `claim` escalation mode |
| `ACK_ESCALATION_CLAIM_TTL_SECONDS` | `3600` | TTL for escalation claims |
| `ACK_ESCALATION_CLAIM_EXCLUSIVE` | `false` | Make escalation claim exclusive |
| `ACK_ESCALATION_CLAIM_HOLDER_NAME` |  | Ops agent name to own escalation claims |

## Development quick start

This repository targets Python 3.14 and uses `uv` with a virtual environment. We manage dependencies via `pyproject.toml` only.

```bash
uv venv --python 3.14
source .venv/bin/activate  # or use direnv
uv sync --dev

# Quick endpoint smoke test (server must be running locally)
bash scripts/test_endpoints.sh

# Pre-commit guard smoke test (no pytest)
bash scripts/test_guard.sh

# Alembic quick start (optional)
# Initialize once per repo, then create/apply migrations as schema evolves.
uv run alembic init alembic
uv run alembic revision -m "init schema"
uv run alembic upgrade head
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

### Quick onboarding for agents

1) Register an identity

```json
{"method":"tools/call","params":{"name":"register_agent","arguments":{"project_key":"/abs/path/backend","program":"codex-cli","model":"gpt5-codex","name":"BlueLake"}}}
```

2) Claim edit surface (optional)

```json
{"method":"tools/call","params":{"name":"claim_paths","arguments":{"project_key":"/abs/path/backend","agent_name":"BlueLake","paths":["app/api/*.py"],"ttl_seconds":3600,"exclusive":true}}}
```

3) Send and acknowledge messages

```json
{"method":"tools/call","params":{"name":"send_message","arguments":{"project_key":"/abs/path/backend","sender_name":"BlueLake","to":["BlueLake"],"subject":"Plan","body_md":"hello"}}}
{"method":"tools/call","params":{"name":"acknowledge_message","arguments":{"project_key":"/abs/path/backend","agent_name":"BlueLake","message_id":"<id>"}}}
```

## End-to-end walkthrough

1. Create two agent identities (backend and frontend projects):

```json
{"method":"tools/call","params":{"name":"register_agent","arguments":{"project_key":"/abs/path/backend","program":"codex-cli","model":"gpt5-codex","name":"GreenCastle","task_description":"Auth refactor"}}}
{"method":"tools/call","params":{"name":"register_agent","arguments":{"project_key":"/abs/path/frontend","program":"claude-code","model":"opus-4.1","name":"BlueLake","task_description":"Navbar redesign"}}}
```

2. Backend agent claims `app/api/*.py` exclusively for 2 hours while preparing DB migrations:

```json
{"method":"tools/call","params":{"name":"claim_paths","arguments":{"project_key":"/abs/path/backend","agent_name":"GreenCastle","paths_list":["app/api/*.py"],"ttl_seconds":7200,"exclusive":true,"reason":"migrations"}}}
```

3. Backend agent sends a design doc with an embedded diagram image:

```json
{"method":"tools/call","params":{"name":"send_message","arguments":{"project_key":"/abs/path/backend","from_agent":"GreenCastle","to":["BlueLake"],"subject":"Plan for /api/users","body_md":"Here is the flow...\n\n![diagram](docs/flow.png)","convert_images":true,"image_embed_policy":"auto","inline_max_bytes":32768}}}
```

4. Frontend agent checks inbox and replies in-thread with questions; reply inherits/sets `thread_id`:

```json
{"method":"tools/call","params":{"name":"check_my_messages","arguments":{"project_key":"/abs/path/backend","agent_name":"BlueLake","include_bodies":true}}}
{"method":"tools/call","params":{"name":"reply_message","arguments":{"project_key":"/abs/path/backend","from_agent":"BlueLake","reply_to_message_id":"msg_20251023_7b3d...","body_md":"Questions: ..."}}}
```

5. Summarize the thread for quick context:

```json
{"method":"tools/call","params":{"name":"summarize_thread","arguments":{"project_key":"/abs/path/backend","thread_id":"TKT-123","include_examples":true}}}
```

6. Pre-commit guard is installed on the backend repo to protect exclusive claims:

```json
{"method":"tools/call","params":{"name":"install_precommit_guard","arguments":{"project_key":"/abs/path/backend","code_repo_path":"/abs/path/backend"}}}
```

## HTTP usage examples (JSON-RPC over Streamable HTTP)

Assuming the server is running at `http://127.0.0.1:8765/mcp/`.

Call a tool:

```bash
curl -sS -X POST http://127.0.0.1:8765/mcp/ \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
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
  }'
```

Read a resource:

```bash
curl -sS -X POST http://127.0.0.1:8765/mcp/ \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": "2",
    "method": "resources/read",
    "params": {
      "uri": "resource://inbox/BlueLake?project=/abs/path/backend&limit=10"
    }
  }'
```

## Search syntax tips (SQLite FTS5)

- Basic terms: `plan users`
- Phrase search: `"build plan"`
- Prefix search: `mig*`
- Boolean operators: `plan AND users NOT legacy`
- Field boosting is not enabled by default; subject and body are indexed. Keep queries concise.

## Design choices and rationale

- **HTTP-only FastMCP**: Streamable HTTP is the modern remote transport; SSE is legacy; STDIO is not exposed here by design
- **Git + Markdown**: Human-auditable, diffable artifacts that fit developer workflows (inbox/outbox mental model)
- **SQLite + FTS5**: Efficient indexing/search with minimal ops footprint
- **Advisory claims**: Make intent explicit and reviewable; optional guard enforces claims at commit time
- **WebP attachments**: Compact images by default; inline embedding keeps small diagrams in context
  - Optional: keep original binaries and dedup manifest under `attachments/` for audit and reuse

## Examples (conceptual tool calls)

Create an agent:

```json
{
  "method": "tools/call",
  "params": {
    "name": "register_agent",
    "arguments": {
      "project_key": "/abs/path/backend",
      "program": "codex-cli",
      "model": "gpt5-codex",
      "name": "GreenCastle",
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

## Security and ops

- Transport
  - HTTP-only (Streamable HTTP). Place behind a reverse proxy (e.g., NGINX) with TLS termination for production
- Auth
  - Optional JWT (HS*/JWKS) via HTTP middleware; enable with `HTTP_JWT_ENABLED=true`
  - Static Bearer token is supported only when JWT is disabled
  - When JWKS is configured (`HTTP_JWT_JWKS_URL`), incoming JWTs must include a matching `kid` header; tokens without `kid` or with unknown `kid` are rejected
  - Starter RBAC (reader vs writer) using role claim; see `HTTP_RBAC_*` settings
- Reverse proxy + TLS (minimal example)
  - NGINX location block:
    ```nginx
    upstream mcp_mail { server 127.0.0.1:8765; }
    server {
      listen 443 ssl;
      server_name mcp.example.com;
      ssl_certificate /etc/letsencrypt/live/mcp.example.com/fullchain.pem;
      ssl_certificate_key /etc/letsencrypt/live/mcp.example.com/privkey.pem;
      location /mcp/ { proxy_pass http://mcp_mail; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto https; }
    }
    ```
- Backups and retention
  - The Git repos and SQLite DB live under `MCP_MAIL_STORE`; back them up together for consistency
- Observability
  - Add logging and metrics at the ASGI layer returned by `mcp.http_app()` (Prometheus, OpenTelemetry)
- Concurrency
  - Git operations are serialized by a file lock per project to avoid index contention

## Python client example (HTTP JSON-RPC)

```python
import httpx, json

URL = "http://127.0.0.1:8765/mcp/"

def call_tool(name: str, arguments: dict) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    r = httpx.post(URL, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])  # surface MCP error
    return data.get("result")

def read_resource(uri: str) -> dict:
    payload = {"jsonrpc":"2.0","id":"2","method":"resources/read","params":{"uri": uri}}
    r = httpx.post(URL, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])  # surface MCP error
    return data.get("result")

if __name__ == "__main__":
    profile = call_tool("register_agent", {
        "project_key": "/abs/path/backend",
        "program": "codex-cli",
        "model": "gpt5-codex",
        "task_description": "Auth refactor",
    })
    inbox = read_resource("resource://inbox/{}?project=/abs/path/backend&limit=5".format(profile["name"]))
    print(json.dumps(inbox, indent=2))
```

## Troubleshooting

- "from_agent not registered"
  - Create the agent first with `create_agent`, or check the `project_key` you’re using matches the sender’s project
- Pre-commit hook blocks commits
  - Set `AGENT_NAME` to your agent identity; release or wait for conflicting exclusive claims; inspect `.git/hooks/pre-commit`
- Inline images didn’t embed
  - Ensure `convert_images=true`, `image_embed_policy="auto"` or `inline`, and the resulting WebP size is below `inline_max_bytes`
- Message not found
  - Confirm the `project` disambiguation when using `resource://message/{id}`; ids are unique per project
- Inbox empty but messages exist
  - Check `since_ts`, `urgent_only`, and `limit`; verify recipient names match exactly (case-sensitive)

## FAQ

- Why Git and SQLite together?
  - Git provides human-auditable artifacts and history; SQLite provides fast queries and FTS search. Each is great at what the other isn’t.
- Are claims enforced?
  - Claims are advisory at the server layer; the optional pre-commit hook adds local enforcement at commit time.
- Why HTTP-only?
  - Streamable HTTP is the modern remote transport for MCP; avoiding extra transports reduces complexity and encourages a uniform integration path.

## API Quick Reference

### Tools

> Tip: to see tools grouped by workflow with recommended playbooks, fetch `resource://tooling/directory`.

| Name | Signature | Returns | Notes |
| :-- | :-- | :-- | :-- |
| `health_check` | `health_check()` | `{status, environment, http_host, http_port, database_url}` | Lightweight readiness probe |
| `ensure_project` | `ensure_project(human_key: str)` | `{id, slug, human_key, created_at}` | Idempotently creates/ensures project |
| `register_agent` | `register_agent(project_key: str, program: str, model: str, name?: str, task_description?: str)` | Agent profile dict | Creates/updates agent; writes profile to Git |
| `send_message` | `send_message(project_key: str, sender_name: str, to: list[str], subject: str, body_md: str, cc?: list[str], bcc?: list[str], attachment_paths?: list[str], convert_images?: bool, importance?: str, ack_required?: bool, thread_id?: str)` | Message dict | Writes canonical + inbox/outbox, converts images |
| `reply_message` | `reply_message(project_key: str, message_id: int, sender_name: str, body_md: str, to?: list[str], cc?: list[str], bcc?: list[str], subject_prefix?: str)` | Message dict | Preserves/creates thread, inherits flags |
| `fetch_inbox` | `fetch_inbox(project_key: str, agent_name: str, limit?: int, urgent_only?: bool, include_bodies?: bool, since_ts?: str)` | `list[dict]` | Non-mutating inbox read |
| `mark_message_read` | `mark_message_read(project_key: str, agent_name: str, message_id: int)` | `{message_id, read, read_at}` | Per-recipient read receipt |
| `acknowledge_message` | `acknowledge_message(project_key: str, agent_name: str, message_id: int)` | `{message_id, acknowledged, acknowledged_at, read_at}` | Sets ack and read |
| `search_messages` | `search_messages(project_key: str, query: str, limit?: int)` | `list[dict]` | FTS5 search (bm25) |
| `summarize_thread` | `summarize_thread(project_key: str, thread_id: str, include_examples?: bool)` | `{thread_id, summary, examples}` | Extracts participants, key points, actions |
| `claim_paths` | `claim_paths(project_key: str, agent_name: str, paths: list[str], ttl_seconds?: int, exclusive?: bool, reason?: str)` | `{granted: list, conflicts: list}` | Advisory leases; Git artifact per path |
| `release_claims` | `release_claims(project_key: str, agent_name: str, paths?: list[str], claim_ids?: list[int])` | `{released, released_at}` | Releases agent’s active claims |
| `renew_claims` | `renew_claims(project_key: str, agent_name: str, extend_seconds?: int, paths?: list[str], claim_ids?: list[int])` | `{renewed, claims[]}` | Extend TTL of existing claims |

### Resources

| URI | Params | Returns | Notes |
| :-- | :-- | :-- | :-- |
| `resource://config/environment` | — | `{environment, database_url, http}` | Inspect server settings |
| `resource://tooling/directory` | — | `{generated_at, clusters[], playbooks[]}` | Grouped tool directory + workflow playbooks |
| `resource://projects` | — | `list[project]` | All projects |
| `resource://project/{slug}` | `slug` | `{project..., agents[]}` | Project detail + agents |
| `resource://claims/{slug}{?active_only}` | `slug`, `active_only?` | `list[claim]` | Claims for a project |
| `resource://message/{id}{?project}` | `id`, `project` | `message` | Single message with body |
| `resource://thread/{thread_id}{?project,include_bodies}` | `thread_id`, `project`, `include_bodies?` | `{project, thread_id, messages[]}` | Thread listing |
| `resource://inbox/{agent}{?project,since_ts,urgent_only,include_bodies,limit}` | listed | `{project, agent, count, messages[]}` | Inbox listing |
| `resource://mailbox/{agent}{?project,limit}` | `project`, `limit` | `{project, agent, count, messages[]}` | Mailbox listing (all messages for an agent) |

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

## Deployment quick notes

- **Direct uvicorn**: `uvicorn mcp_agent_mail.http:build_http_app --factory --host 0.0.0.0 --port 8765`
- **Python module**: `python -m mcp_agent_mail.http --host 0.0.0.0 --port 8765`
- **Gunicorn**: `gunicorn -c deploy/gunicorn.conf.py mcp_agent_mail.http:build_http_app --factory`
- **Docker**: `docker compose up --build`

### CI/CD

- Lint and Typecheck CI: GitHub Actions workflow runs Ruff and Ty on pushes/PRs to main/develop.
- Release: Pushing a tag like `v0.1.0` builds and pushes a multi-arch Docker image to GHCR under `ghcr.io/<owner>/<repo>` with `latest` and version tags.
- Nightly: A scheduled workflow runs migrations and lists projects daily for lightweight maintenance visibility.

### Log rotation (optional)

If not using journald, a sample logrotate config is provided at `deploy/logrotate/mcp-agent-mail` to rotate `/var/log/mcp-agent-mail/*.log` weekly, keeping 7 rotations.

### Logging (journald vs file)

- Default systemd unit (`deploy/systemd/mcp-agent-mail.service`) is configured to send logs to journald (StandardOutput/StandardError=journal).
- For file logging, configure your process manager to write to files under `/var/log/mcp-agent-mail/*.log` and install the provided logrotate config.
- Environment file path for systemd is `/etc/mcp-agent-mail.env` (see `deploy/systemd/mcp-agent-mail.service`).

### Container build and multi-arch push

Use Docker Buildx for multi-arch images. Example flow:

```bash
# Create and select a builder (once)
docker buildx create --use --name mcp-builder || docker buildx use mcp-builder

# Build and test locally (linux/amd64)
docker buildx build --load -t your-registry/mcp-agent-mail:dev .

# Multi-arch build and push (amd64, arm64)
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t your-registry/mcp-agent-mail:latest \
  -t your-registry/mcp-agent-mail:v0.1.0 \
  --push .
```

Recommended tags: a moving `latest` and immutable version tags per release. Ensure your registry login is configured (`docker login`).

### Systemd manual deployment steps

1. Copy project files to `/opt/mcp-agent-mail` and ensure permissions (owner `appuser`).
2. Place environment file at `/etc/mcp-agent-mail.env` based on `deploy/env/production.env`.
3. Install service file `deploy/systemd/mcp-agent-mail.service` to `/etc/systemd/system/`.
4. Reload systemd and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable mcp-agent-mail
sudo systemctl start mcp-agent-mail
sudo systemctl status mcp-agent-mail
```

Optional (non-journald log rotation): install `deploy/logrotate/mcp-agent-mail` into `/etc/logrotate.d/` and write logs to `/var/log/mcp-agent-mail/*.log` via your process manager or app config.

See `deploy/gunicorn.conf.py` for a starter configuration and `TODO.md` for the broader deployment roadmap (Docker, systemd, automation scripts, CI/CD).

## CLI Commands

The project exposes a developer CLI for common operations:

- `serve-http`: run the HTTP transport (Streamable HTTP only)
- `migrate`: ensure schema and FTS structures exist
- `lint` / `typecheck`: developer helpers
- `list-projects [--include-agents]`: enumerate projects
- `guard-install <project_key> <code_repo_path>`: install the pre-commit guard into a repo
- `guard-uninstall <code_repo_path>`: remove the guard from a repo
- `list-acks --project <key> --agent <name> [--limit N]`: show pending acknowledgements for an agent

Examples:

```bash
# Install guard into a repo
uv run python -m mcp_agent_mail.cli guard-install /abs/path/backend /abs/path/backend

# List pending acknowledgements for an agent
uv run python -m mcp_agent_mail.cli list-acks --project /abs/path/backend --agent BlueLake --limit 10
```

## Continuous Integration

This repo includes a GitHub Actions workflow that runs on pushes and PRs:

- Ruff lint: `ruff check` (GitHub format)
- Type check: `uvx ty check`

See `.github/workflows/ci.yml`.
