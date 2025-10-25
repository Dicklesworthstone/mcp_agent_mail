# Project TODO (in progress)

- [x] Persistence archive
  - [x] Define storage root and per-project structure (agents/, messages/, file_reservations/, attachments/)
  - [x] Implement Markdown writing with JSON front matter for canonical message + inbox/outbox copies
  - [x] Persist agent profiles to json under agents/
  - [x] Persist file reservation JSON artifacts with hashed filenames
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
- [x] File Reservations/leases
  - [x] Expand reservation tool to detect glob overlaps
  - [x] Implement release_file_reservations tool returning updated status
  - [x] Build resource for active file reservations per project
  - [x] Prepare pre-commit hook generator installing guard
- [x] Resources
  - [x] resource://message/{id}{?project} returning body + metadata
  - [x] resource://thread/{thread_id}{?project,include_bodies}
  - [x] resource://inbox/{agent}{?project,...}
  - [x] resource://file_reservations/{project}{?active_only}
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
  - [x] Test file reservation conflict detection, release tool, resources
  - [x] Test search and summaries tools
  - [x] Test CLI serve-http with auth defaults and migrations command
  - [x] Add image conversion test (mocking Pillow)

# Deployment Enhancements (Detailed Backlog)

- [x] **Production ASGI entrypoint**  
  Provide a first-class entryway for running the HTTP transport in production environments.  
  - [x] Create `src/mcp_agent_mail/__main__.py` (or `run.py`) exposing a callable that bootstraps settings and starts the FastAPI/uvicorn server so that `python -m mcp_agent_mail.http` “just works”.  
  - [x] Supply a documented `uvicorn` CLI snippet (e.g., `uvicorn mcp_agent_mail.http:build_http_app --factory`) plus example environment variable usage.  
  - [x] Add a lightweight `gunicorn` config demonstrating worker selection, graceful timeout, async worker class, and log redirection for multi-worker deployments.

- [x] **Container image**  (verified)
  Deliver a reproducible container workflow.  
  - [x] Author a multi-stage Dockerfile: stage 1 builds wheels via `uv`, stage 2 installs only runtime deps, stage 3 runs as a non-root user and uses a lean base (e.g., `python:3.14-slim`).  
  - [x] Provide entrypoint/CMD equivalent to `uvicorn mcp_agent_mail.http:build_http_app --host 0.0.0.0 --port 8765` and allow overrides via env vars.  
  - [x] Create a sample `docker-compose.yml` that wires the MCP server with Postgres (async connection) showing env config, volume mounts (for archive), and health checks.  
  - [x] Document the build/push flow and recommended multi-arch strategy.

- [x] **Process supervisor packaging**  (verified)
  Aid on-prem/bare metal operators.  
  - [x] Provide a `systemd` unit template (`mcp-agent-mail.service`) that sources `/etc/mcp-agent-mail.env`, runs uvicorn, automatically restarts on failure, and logs to journal.  
  - [x] Include optional log rotation config (logrotate snippet) for when journald isn’t available.  
  - [x] Document manual deployment steps: copy binaries, set permissions, enable service.

- [x] **Automation scripts**  (verified)
  Simplify bootstrap and recurring ops.  
  - [x] Add `scripts/` directory with `deploy.sh` / `bootstrap.sh` that: runs `uv sync`, copies `.env` from a template if missing, seeds initial DB (calling `cli migrate`), optionally installs pre-commit guard, and prints next steps.  (verified: scripts copy .env, verify decouple, print next steps)
  - [x] Optionally add a Makefile (or uv’s `task`/`run` alias) with targets: `make serve`, `make lint`, `make typecheck`, `make guard-install`, etc.  (verified: `Makefile` present)
  - [x] Verify `python-decouple` can load key variables from `.env` (e.g., `HTTP_HOST`, `HTTP_PORT`, `DATABASE_URL`, `STORAGE_ROOT`) and fail fast if missing.
  - [x] Consider templating environment files (staging/prod) and verifying they load via `python-decouple`.  (templates present under `deploy/env/`; verification included)
  - [x] Print clear “Next steps” at script end (server start command, guard install example).

- [x] **CI/CD integration**  
  Establish automated safeguards.  
  - [x] GitHub Actions workflow for `lint` (Ruff) + `type check` (Ty) triggered on pushes/PRs.  
  - [x] Separate workflow that builds and pushes Docker images to registry on tagged releases (with version tagging strategy).  
  - [x] Optional nightly workflow to run `cli migrate`, `cli list-projects`, etc., and capture artifacts/logs for manual review.

# Spec Alignment Backlog (from project_idea_and_guide.md)

- [x] **Messaging persistence & Git history**  (implemented: commit & diff summaries in resources)  
  Current status: canonical markdown archived and commits enriched with diff summaries.  
  - [x] Expose resource/tool for per-agent inbox/outbox browsing, with context about commit history and diff summaries.  (verified: `resource://inbox/{agent}` and `resource://outbox/{agent}` include per-message `commit` with `diff_summary`)  
  - [x] Store thread-level metadata (e.g., transcripts, digest files) so history of a conversation is easy to review from Git.  
  - [x] Add commit message trailers (e.g., `Agent:`, `Thread:`) to enable log filtering.  
    - [x] Verified: Commit trailers present in storage commits; formatting validated across send/claim flows.

- [x] **Ack management & urgent views**  (verified)  
  - [x] Build resources/tools listing “messages requiring ACK” and “urgent unread”, akin to flagged email views.  
  - [x] CLI/agent tooling to remind agents of outstanding acknowledgements, maybe integrate with claims guard.  
  - [x] Implement ack TTL checks—warnings or auto-claims if deadlines missed.  (background worker warns; optional claim escalation implemented)

- [x] **File Reservations & leases extensions**  (verified)
  - [x] Add CLI command for installing/removing the pre-commit guard (currently only a tool).
  - [x] Add server-side enforcement (e.g., refusal to send message updates if file reservation conflicts).  (send_message blocks on conflicting active exclusive file reservations when enabled)
  - [x] Provide a heartbeat/renewal tool so agents can extend leases without reissuing reservations.

- [x] **Search & summarization improvements**  (verified)  
  - [x] Upgrade summarizer: incorporate heuristics (e.g., parse markdown TODOs or code references) or optional LLM integration for richer briefs.  
  - [x] Provide multi-thread digests, top mentions, action item extraction beyond simple keyword checks.  (implemented via `summarize_threads` aggregate)

- [x] **Attachment handling**  (verified)  
  - [x] Make conversion configurable per agent/project, allow storing original binary if required (alongside WebP).  
  - [x] Add deduplication manifest (tracking global SHA) and metadata (type, dimensions).  
  - [x] Remember agent preference for inline vs file attachments.

- [x] **Agent directory enhancements**  (verified)  
  - [x] Add `whois(agent)` tool returning project assignments, recent activity, last git commit info.  
  - [x] Integrate with Git to show the agent’s most recent archive commit summaries.

- [x] **CLI/guard tooling**  (verified)
  - [x] Add CLI command to list active file reservations with expiry countdowns, and optionally raise warnings for soon-to-expire leases.  (commands `file-reservations active` and `file-reservations soon` implemented)
  - [x] Build guard integration tests (mock git) to ensure the generated hook catches conflicts.
   - [x] Offer CLI command to review ack status (`cli list-acks`).
    - [x] Implemented `list-acks`; includes ack age and thread columns.

- [x] **HTTP transport hardening**  (verified)  
  - [x] Add rate limiting (e.g., `slowapi`) and CORS toggles.  
  - [x] Integrate OpenTelemetry instrumentation for tracing metrics.  
  - [x] Provide sample middleware for request logging.

- [x] **Security & Auth**  
  - [x] Optional JWT-based auth with per-agent tokens and rotation.  
  - [x] Basic RBAC: read-only vs tools; audit logs for tools/calls.  
  - [x] TLS termination guidance and sample reverse-proxy config.

- [x] **Rate limiting (robust)**  
  - [x] Token-bucket with sliding window and per-endpoint limits.  
  - [x] Pluggable store (Redis) for multi-worker enforcement.

- [x] **Logging & Observability**  
  - [x] Replace prints with structured logging (json).  
  - [x] Error reporting with context (project/agent/message).  
  - [x] Metrics: background task status, conversion failures, claims TTL expirations.

- [ ] **Cleanup & Retention**  
  - [ ] Background compaction/dedup/retention for old messages/attachments.  
  - [ ] Quotas for attachment storage and inbox sizes.

- [ ] **Testing Expansion**
  - [x] HTTP JSON-RPC tests for auth/RBAC and rate limiting.
  - [ ] File reservation conflict tests covering edge patterns and TTL transitions.
  - [ ] Attachment policy tests for agent/server overrides.

- [x] **Migrations**  
  - [x] Add Alembic migrations for recent schema changes: `agents.attachments_policy`, `agents.contact_policy`, and the `agent_links` table.  
  - [x] Wire CLI `migrate` to Alembic (`alembic upgrade head`) consistently (currently uses `ensure_schema`).

- [x] **Database improvements**
  - [x] Add indexes on created_ts, thread_id, importance for faster queries.
  - [x] Implement scheduled cleanup for expired file reservations/old messages (maybe via background tasks).
  - [x] Prepare migrations once schema evolves (Alembic integration).

- [x] **Testing gaps**  (verified via scripts)
  - [x] Add manual/automated scripts to verify guard behavior (without invoking pytest).  
  - [x] Scripted integration tests for HTTP endpoints (liveness/readiness, token auth) using curl-like commands.  
  - [x] Document manual testing steps for CLI flows (`serve-http`, `migrate`, etc.).

- [ ] **Documentation**  (partial: README expanded; onboarding doc missing)  
  - [x] Align README configuration matrix with current env names and add reverse-proxy/TLS section.  
  - [ ] Provide onboarding doc for agents: how to register, claim paths, send messages, acknowledge.  
  - [x] Create architecture diagram covering DB, archive, guard, CLI, HTTP.  (present in README)

- [ ] **Advanced roadmap items**  
  - [ ] Integrate optional LLM summarizer for threads, action items, and triage.  
  - [ ] Build watchers/notifications (e.g., send urgent ack reminders).  
  - [x] Provide integration scripts for Codex/Claude agents (watch repo, send/receive messages).  
  - [ ] Track attachments via hashed directories with accountability logs.

- [x] **Recent Internal Note**
  - [x] Pre-commit hook generator updated to use `string.Template` for safer substitutions (no curly-brace conflicts) and clean formatting (`src/mcp_agent_mail/app.py`).
  - [x] **Container image**  
    Deliver a reproducible container workflow.  
    - [x] Author a multi-stage Dockerfile: stage 1 builds wheels via `uv`, stage 2 installs only runtime deps, stage 3 runs as a non-root user and uses a lean base.  
    - [x] Provide entrypoint/CMD equivalent to `uvicorn mcp_agent_mail.http:build_http_app --host 0.0.0.0 --port 8765` and allow overrides via env vars.  
    - [x] Create a sample `docker-compose.yml` with Postgres wiring and volumes.  
    - [x] Document the build/push flow and recommended multi-arch strategy.  (added in README)

  - [x] **Process supervisor packaging**  
    Aid on-prem/bare metal operators.  
    - [x] Provide a `systemd` unit template `deploy/systemd/mcp-agent-mail.service`.  
    - [x] Include optional log rotation config.  (see `deploy/logrotate/mcp-agent-mail`)  
    - [x] Document manual deployment steps.  (see README)

  - [x] **Automation scripts**  
    Simplify bootstrap and recurring ops.  
    - [x] Add `scripts/bootstrap.sh` that installs deps and runs migrations.  
    - [x] Consider Makefile/task runner integration.  
    - [x] Template env files for staging/prod.
