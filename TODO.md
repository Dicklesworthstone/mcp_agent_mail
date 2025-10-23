# Project TODO (in progress)

- [x] Persistence archive
  - [x] Define storage root and per-project structure (agents/, messages/, claims/, attachments/)
  - [x] Implement Markdown writing with JSON front matter for canonical message + inbox/outbox copies
  - [x] Persist agent profiles to json under agents/
  - [x] Persist claim JSON artifacts with hashed filenames
  - [x] Ensure all file operations async-friendly (use asyncio.to_thread as needed)
  - [x] Integrate GitPython: repo init per project, add commit helper with lock handling
  - [x] Add advisory file lock to serialize archive writes
- [x] Agent identity workflow
  - [x] Update name generator to check DB + filesystem for uniqueness
  - [x] Expose create identity tool returning full profile (program/model/task)
  - [x] Track last_active and ensure updates on interactions
- [x] Messaging enhancements
  - [x] Support message replies (thread_id, subject prefix)
  - [x] Include read/ack tools updating timestamps
  - [x] Implement urgent-only filter and ack-required flag handling
  - [x] Inline/attachment WebP conversion with Pillow; store under attachments/
  - [x] Provide acknowledgements tool
- [x] Claims/leases
  - [x] Expand claim tool to detect glob overlaps
  - [x] Implement release_claims tool returning updated status
  - [x] Build resource for active claims per project
  - [x] Prepare pre-commit hook generator installing guard
- [x] Resources
  - [x] resource://message/{id}{?project} returning body + metadata
  - [x] resource://thread/{thread_id}{?project,include_bodies}
  - [x] resource://inbox/{agent}{?project,...}
  - [x] resource://claims/{project}{?active_only}
- [x] Search & summaries
  - [x] Configure SQLite FTS tables/triggers for messages
  - [x] search_messages tool w/ query param
  - [x] summarize_thread tool returning keypoints/actions
- [x] Config/auth/CLI
  - [x] Extend settings for storage root, git author, attachment limits
  - [x] Provide CLI command to run migrations and list projects/agents
  - [x] Add optional bearer auth scaffold for HTTP transport
  - [x] Implement health/readiness endpoints on HTTP app via FastAPI wrapper
  - [x] Enrich CLI output with Rich panels/logging
- [x] Testing
  - [x] Expand tests to cover filesystem archive & git commits
  - [x] Test claims conflict detection, release tool, resources
  - [x] Test search and summaries tools
  - [x] Test CLI serve-http with auth defaults and migrations command
  - [x] Add image conversion test (mocking Pillow)

# Deployment Enhancements (Detailed Backlog)

- [x] **Production ASGI entrypoint**  
  Provide a first-class entryway for running the HTTP transport in production environments.  
  - [x] Create `src/mcp_agent_mail/__main__.py` (or `run.py`) exposing a callable that bootstraps settings and starts the FastAPI/uvicorn server so that `python -m mcp_agent_mail.http` “just works”.  
  - [x] Supply a documented `uvicorn` CLI snippet (e.g., `uvicorn mcp_agent_mail.http:build_http_app --factory`) plus example environment variable usage.  
  - [x] Add a lightweight `gunicorn` config demonstrating worker selection, graceful timeout, async worker class, and log redirection for multi-worker deployments.

- [ ] **Container image**  
  Deliver a reproducible container workflow.  
  - [x] Author a multi-stage Dockerfile: stage 1 builds wheels via `uv`, stage 2 installs only runtime deps, stage 3 runs as a non-root user and uses a lean base (e.g., `python:3.14-slim`).  
  - [x] Provide entrypoint/CMD equivalent to `uvicorn mcp_agent_mail.http:build_http_app --host 0.0.0.0 --port 8765` and allow overrides via env vars.  
  - [x] Create a sample `docker-compose.yml` that wires the MCP server with Postgres (async connection) showing env config, volume mounts (for archive), and health checks.  
  - [x] Document the build/push flow and recommended multi-arch strategy.

- [ ] **Process supervisor packaging**  
  Aid on-prem/bare metal operators.  
  - [x] Provide a `systemd` unit template (`mcp-agent-mail.service`) that sources `/etc/mcp-agent-mail.env`, runs uvicorn, automatically restarts on failure, and logs to journal.  
  - [x] Include optional log rotation config (logrotate snippet) for when journald isn’t available.  
  - [x] Document manual deployment steps: copy binaries, set permissions, enable service.

- [ ] **Automation scripts**  
  Simplify bootstrap and recurring ops.  
  - [ ] Add `scripts/` directory with `deploy.sh` / `bootstrap.sh` that: runs `uv sync`, copies `.env.example`, seeds initial DB (calling `cli migrate`), installs pre-commit guard, and prints next steps.  
  - [ ] Optionally add a Makefile (or uv’s `task`/`run` alias) with targets: `make serve`, `make lint`, `make typecheck`, `make guard-install`, etc.  
  - [ ] Consider templating environment files (staging/prod) and verifying they load via `python-decouple`.

- [x] **CI/CD integration**  
  Establish automated safeguards.  
  - [x] GitHub Actions workflow for `lint` (Ruff) + `type check` (Ty) triggered on pushes/PRs.  
  - [x] Separate workflow that builds and pushes Docker images to registry on tagged releases (with version tagging strategy).  
  - [ ] Optional nightly workflow to run `cli migrate`, `cli list-projects`, etc., and capture artifacts/logs for manual review.

# Spec Alignment Backlog (from project_idea_and_guide.md)

- [ ] **Messaging persistence & Git history**  
  Current status: we archive markdown and commit, but missing richer human review surfaces.  
  - [x] Expose resource/tool for per-agent inbox/outbox browsing, with context about commit history and diff summaries.  
  - [x] Store thread-level metadata (e.g., transcripts, digest files) so history of a conversation is easy to review from Git.  
  - [x] Add commit message trailers (e.g., `Agent:`, `Thread:`) to enable log filtering.  
    - [ ] In progress: Implemented trailers in storage commits; validating formatting across flows.

- [ ] **Ack management & urgent views**  
  - [x] Build resources/tools listing “messages requiring ACK” and “urgent unread”, akin to flagged email views.  
  - [x] CLI/agent tooling to remind agents of outstanding acknowledgements, maybe integrate with claims guard.  
  - [ ] Implement ack TTL checks—warnings or auto-claims if deadlines missed.

- [ ] **Claims & leases extensions**  
  - [x] Add CLI command for installing/removing the pre-commit guard (currently only a tool).  
  - [ ] Add server-side enforcement (e.g., refusal to send message updates if claims conflict).  
  - [x] Provide a heartbeat/renewal tool so agents can extend leases without reissuing claims.

- [ ] **Search & summarization improvements**  
  - [ ] Upgrade summarizer: incorporate heuristics (e.g., parse markdown TODOs or code references) or optional LLM integration for richer briefs.  
  - [ ] Provide multi-thread digests, top mentions, action item extraction beyond simple keyword checks.

- [ ] **Attachment handling**  
  - [ ] Make conversion configurable per agent/project, allow storing original binary if required (alongside WebP).  
  - [ ] Add deduplication manifest (tracking global SHA) and metadata (type, dimensions).  
  - [ ] Remember agent preference for inline vs file attachments.

- [ ] **Agent directory enhancements**  
  - [x] Add `whois(agent)` tool returning project assignments, recent activity, last git commit info.  
  - [x] Integrate with Git to show the agent’s most recent archive commit summaries.

- [ ] **CLI/guard tooling**  
  - [ ] Add CLI command to list active claims with expiry countdowns, and optionally raise warnings for soon-to-expire leases.  
  - [ ] Build guard integration tests (mock git) to ensure the generated hook catches conflicts.  
   - [x] Offer CLI command to review ack status (`cli list-acks`).  
    - [x] Implemented `list-acks`; includes ack age and thread columns.

- [ ] **HTTP transport hardening**  
  - [ ] Add rate limiting (e.g., `slowapi`) and CORS toggles.  
  - [ ] Integrate OpenTelemetry instrumentation for tracing metrics.  
  - [ ] Provide sample middleware for request logging.

- [ ] **Database improvements**  
  - [x] Add indexes on created_ts, thread_id, importance for faster queries.  
  - [ ] Implement scheduled cleanup for expired claims/old messages (maybe via background tasks).  
  - [ ] Prepare migrations once schema evolves (Alembic integration).

- [ ] **Testing gaps**  
  - [ ] Add manual/automated scripts to verify guard behavior (without invoking pytest).  
  - [ ] Scripted integration tests for HTTP endpoints (liveness/readiness, token auth) using curl-like commands.  
  - [ ] Document manual testing steps for CLI flows (`serve-http`, `migrate`, etc.).

- [ ] **Documentation**  
  - [ ] Expand README with quickstart, configuration matrix, CLI usage, guard setup, and message flow explanation.  
  - [ ] Provide onboarding doc for agents: how to register, claim paths, send messages, acknowledge.  
  - [ ] Create architecture diagram covering DB, archive, guard, CLI, HTTP.

- [ ] **Advanced roadmap items**  
  - [ ] Integrate optional LLM summarizer for threads, action items, and triage.  
  - [ ] Build watchers/notifications (e.g., send urgent ack reminders).  
  - [ ] Provide integration scripts for Codex/Claude agents (watch repo, send/receive messages).  
  - [ ] Track attachments via hashed directories with accountability logs.

# Recent Internal Note
- [x] Pre-commit hook generator updated to use `string.Template` for safer substitutions (no curly-brace conflicts) and clean formatting (`src/mcp_agent_mail/app.py`).
- [x] **Container image**  
  Deliver a reproducible container workflow.  
  - [x] Author a multi-stage Dockerfile: stage 1 builds wheels via `uv`, stage 2 installs only runtime deps, stage 3 runs as a non-root user and uses a lean base.  
  - [x] Provide entrypoint/CMD equivalent to `uvicorn mcp_agent_mail.http:build_http_app --host 0.0.0.0 --port 8765` and allow overrides via env vars.  
  - [x] Create a sample `docker-compose.yml` with Postgres wiring and volumes.  
  - [ ] Document the build/push flow and recommended multi-arch strategy.

- [x] **Process supervisor packaging**  
  Aid on-prem/bare metal operators.  
  - [x] Provide a `systemd` unit template `deploy/systemd/mcp-agent-mail.service`.  
  - [ ] Include optional log rotation config.  
  - [ ] Document manual deployment steps.

- [x] **Automation scripts**  
  Simplify bootstrap and recurring ops.  
  - [x] Add `scripts/bootstrap.sh` that installs deps and runs migrations.  
  - [x] Consider Makefile/task runner integration.  
  - [x] Template env files for staging/prod.
