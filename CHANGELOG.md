# Changelog

All notable changes to [MCP Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) are documented here.

Format: entries are organized by version in reverse chronological order. Each version header links to the GitHub comparison or release when one exists. **GitHub Release** markers distinguish versions that have published release artifacts from plain git tags. Commit links point to representative commits for each capability.

---

## [Unreleased] (v0.3.0...main)

128 commits since v0.3.0, spanning 2026-01-07 through 2026-03-18. No release tag yet.

### Features

- **Persistent window-based agent identity** -- agents get a stable identity tied to their terminal pane, surviving restarts ([32afeab](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/32afeabaa2350ec7a133235c8e2b99141858bbb0))
- **Canonical per-pane agent identity file contract** -- formalized the on-disk identity file protocol for multi-agent environments ([6715be6](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/6715be67b9982abe2611c3e466f98a2946cd0f86))
- **Broadcast and topic threads** -- all-agent visibility messaging with topic-based threading for coordination ([b4ad9fc](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/b4ad9fc82070efd59f315807a83ca56ea345c616))
- **On-demand project-wide message summarization** -- generate summaries across an entire project's message history ([ee53048](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ee5304819ef6316acaa17a1d721dd026841d1fc5))
- **Virtual namespace support for tool/resource reservations** -- extend file reservation semantics to abstract namespaces ([b1c2051](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/b1c20517e29ecdac7b42f0b08b9f7d89e1d41747))
- **Agent retire and project archive soft-delete** -- lifecycle management for winding down agents and archiving projects (#102, #103) ([8f3d627](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8f3d6275d4ddf7b4ff1b914504459b2305bacb78))
- **Hard delete with "I UNDERSTAND" confirmation** -- permanent deletion requiring explicit confirmation string (#105) ([74caaeb](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/74caaeb3d86c01c2877ece5cbc5ab7c7bad73f7e))
- **Sender identity verification and safe defaults** -- verify message sender identity with secure-by-default configuration ([56dce9e](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/56dce9e9b991581feaa73a905b5245461e608f1a))
- **Periodic FD health monitor** -- background watchdog for file descriptor exhaustion in multi-agent deployments ([81dd55f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/81dd55f48125c8a50453a89d168b3802afce5723))
- **`/mcp` endpoint alias** -- mount `/mcp` alongside `/api` for client compatibility, increase SQLite pool to 50 ([8d6a12d](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8d6a12d9c42150abcba9087202da3a96794bd96b))
- **Contact enforcement optimization** -- batch queries for contact policy checks, SPA support ([8b6e67c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8b6e67c574725e8304b44b11820287b25717547f))
- **TOON output format support** -- optional TOON-encoded output for tools and resources ([49beea8](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/49beea847145b2095eb337d2d7aad413ba9e05d9))
- **Commit queue and archive locking** -- serialized Git commit queue for high-concurrency multi-agent scenarios ([4fb2f38](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/4fb2f3801c46be6f6763dd0bd46927168ddd4642))
- **Git index.lock retry with exponential backoff** -- resilient Git operations when concurrent agents contend on the index ([5969db9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/5969db9ab19363764cf23c200060c48fa3b66187))
- **Installer hardening** -- robust TOML URL upsert, Codex integration exports, improved token handling ([535d8aa](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/535d8aa3a7ac1c6b9fc4fb81f1b7acea8aba7c24))
- **Rewrite `run_server_with_token` to use Rust `am` binary** -- server launcher now delegates to the compiled Rust binary with config.env ([1fca641](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1fca641f3aff47f9c198a81e8f7e86377cf3ea90))

### Bug Fixes

- **AsyncFileLock FD leaks** -- prevent file descriptor exhaustion under sustained load ([51524ae](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/51524ae3ae47c64cbb31be8e1fee33290584bbfd))
- **Lightweight `/api/health` bypass** -- health checks no longer blocked when the MCP layer is saturated ([14889f3](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/14889f30e3fb68b699facdc44e5eed8aaf706c44))
- **LIKE escape character ambiguity** -- switch to `!` as the LIKE ESCAPE char to avoid backslash escaping issues across SQLite/PostgreSQL ([1fddd32](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1fddd3270989d7fa324437403a2a7d6d4a1e967b))
- **Missing schema migrations** -- add migrations for `registration_token` and `topic` columns (#106) ([caf4826](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/caf48261b892b2a2071f9f59a119152b70b91ffb))
- **4 bugs in one pass** -- cross-project `git_paths_removed` leak, falsy-0 id checks, XSS in confirmation dialog, missing auth on archive tools ([84442d3](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/84442d3c23265c116739753669520ef9dda75fba))
- **OAuth metadata paths** -- normalize trailing-slash behavior and return consistent 404s for disabled OAuth ([79b25db](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/79b25db8738dc8526545197e3223633b359b55a0))
- **Installer `exec` issue** -- remove `exec` before `run_server_with_token.sh` so the calling script resumes ([7019585](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/70195854e2b0dd1e948b2d3cc3e62e2bac56b3d3))
- **Identity script hardening** -- parent directory permissions, path validation, TOCTOU race protection ([ddcef4f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ddcef4f4ccc61f98ed94d8f1ef7cb66d201ede30))
- **Storage cleanup** -- replace unreliable refcount eviction with time-based cleanup, close EMFILE-recovery repo handles ([61bb6d1](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/61bb6d1e39f3be316c2a405f0b5a6497e128f46a))
- **Hardcoded bearer tokens** -- replace with placeholders in `.mcp.json` files ([50c34cc](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/50c34cce0f7d8bccff17b0aea94e4ec53d3e54f3))
- **CLI default subcommand** -- default to `serve-http` when no subcommand given ([061435c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/061435cd2b3a52f08b6f5e51b5cfe8ca2f2b5d87))
- **Integration settings overwrite** -- merge settings instead of overwriting (#76) ([9ee83f3](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/9ee83f3e6e7f4e20d0ec37f9ccc6c6c84de7d2f5))

### Security

- **Constant-time bearer token comparison** -- prevent timing side-channels in auth ([7c298d4](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7c298d4e3df065ecf71035654e214c5a6cd48f4b))
- **Localhost bypass prevention** -- block spoofed localhost headers behind reverse proxies ([c911f7c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/c911f7cf70465819773dfa25b3f2b4d0b0c4100f))
- **Gate absolute attachment paths** -- prevent path traversal through attachment references ([fc57155](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/fc571551c9bf24f93a0b394e2a4b4d6b34c5ea50))
- **Remove `ack_required` bypass** in contact policy enforcement ([d947651](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/d9476511f3617fbfbbd2db8e8a3e8e794b4d9b56))

### Other

- **License changed** to MIT with OpenAI/Anthropic Rider ([dd79b83](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/dd79b83b482a3cafdb32bbb5ac9b4a48d9fc08bc))
- Ruff auto-fixes applied (collapsed nested if, unused variables, import sorting)
- Python requirement downgraded from 3.14 to 3.12 for broader compatibility

---

## [v0.3.0] -- 2026-01-07 (GitHub Release)

**Stdio Transport, Tool Filtering, Push Notifications**

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.3.0) --|-- [Compare v0.2.1...v0.3.0](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.2.1...v0.3.0)

Artifacts: `mcp_agent_mail-0.3.0-py3-none-any.whl`, `mcp_agent_mail-0.3.0.tar.gz`, `SHA256SUMS.txt`

### Features

- **Stdio transport** (`am serve-stdio`) -- run the MCP server over stdin/stdout for direct CLI integration with Claude Code and similar tools. All logging redirected to stderr to preserve protocol integrity ([07b685c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/07b685c))
- **Tool filtering profiles** -- reduce token overhead by exposing only needed tools via `TOOLS_FILTER_PROFILE` (full, core, minimal, messaging, custom). The "minimal" profile cuts context by ~70% ([14c4c23](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/14c4c23))
- **Push notification signals** -- file-system signal files that agents can watch with inotify/FSEvents/kqueue for instant notification without polling ([250567d](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/250567d))

### Bug Fixes

- **Stdio protocol corruption** -- rich console output and tool debug panels were writing to stdout, corrupting the stdio protocol ([ad350e9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ad350e9), [e10166c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/e10166c))
- **EMFILE recovery** -- automatic cache cleanup when hitting "too many open files" limit ([50e7e45](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/50e7e45))
- **Python 3.14 cancellation safety** -- async session management compatible with `CancelledError` as `BaseException` ([8758df1](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8758df1))
- **SQLite pool tuning** -- improved concurrent access reliability ([da35417](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/da35417))
- **Viewer SRI** -- excluded HTML from integrity map to fix asset loading ([0f8036b](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0f8036b))

---

## [v0.2.1] -- 2026-01-06 (GitHub Release)

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.2.1) --|-- [Compare v0.2.0...v0.2.1](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.2.0...v0.2.1)

Artifacts: `mcp_agent_mail-0.2.1-py3-none-any.whl`, `mcp_agent_mail-0.2.1.tar.gz`, `SHA256SUMS.txt`

### Features

- **`am doctor` diagnostic commands** -- CLI commands for diagnosing and repairing Agent Mail installations ([3ff1241](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/3ff1241))
- **Thread ID format validation** -- reject malicious thread IDs at the utility layer ([01ac7f0](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/01ac7f0))

### Bug Fixes

- **Python 3.14 cancellation safety** -- `asyncio.shield()` wraps all async cleanup (session close, engine dispose, AsyncFileLock, archive_write_lock) now that `CancelledError` is a `BaseException` ([327e8cb](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/327e8cb), [91ea1a9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/91ea1a9))
- **FD exhaustion prevention** -- SQLite now uses `NullPool` to prevent file descriptor exhaustion on macOS; other backends retain standard pooling ([327e8cb](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/327e8cb))
- **Concurrency robustness** -- `ensure_project()` and `_get_or_create_agent()` handle `IntegrityError` for truly idempotent concurrent creation ([0e15860](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0e15860))
- **Path traversal prevention** -- `_resolve_archive_relative_path()` blocks directory traversal in attachment paths with `Path.resolve().relative_to()` defense ([91ea1a9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/91ea1a9))
- **Git blob resource leak** -- close `data_stream` after reading blob contents ([1e42952](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1e42952))
- **Storage race condition** -- fix `_ensure_repo` cache put race ([61df73b](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/61df73b))
- **Guard chain-runner preservation** -- preserve existing pre-commit chain when other plugins are installed ([9510799](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/9510799))
- **Proactive FD cleanup** -- integrate cleanup into repo creation path ([f814f8d](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/f814f8d))

---

## [v0.2.0] -- 2026-01-06 (GitHub Release)

**Comprehensive Test Coverage Milestone**

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.2.0) --|-- [Compare v0.1.5...v0.2.0](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.5...v0.2.0)

Artifacts: `mcp_agent_mail-0.2.0-py3-none-any.whl`, `mcp_agent_mail-0.2.0.tar.gz`, `checksums-v0.2.0.txt`

### Highlights

Completion of all 30 beads in the testing-tasks-v2 initiative. 424+ tests passing across all priority levels (P0-P4), 26 new test suites, 11 bug fixes, zero breaking changes.

### Test Suites Added

| Priority | Category | Tests | Scope |
|----------|----------|-------|-------|
| P0 | Regression | 3 suites | Datetime handling, session context, agent name validation |
| P1 | Core flows | 5 suites | Contact management, message delivery, file reservation lifecycle, project/agent setup, MCP resources |
| P2 | CLI | 73 tests | Mail commands (29), archive commands (24), guard commands (20) |
| P2 | HTTP | 40 tests | Authentication (15), rate limiting (13), server/transport (12) |
| P2 | Guards | 34 tests | Pre-commit enforcement (14), pre-push enforcement (20) |
| P2 | Error handling | 43 tests | Database failures (10), git archive corruption (21), invalid inputs (12) |
| P3 | Non-functional | -- | Path traversal prevention, input sanitization, multi-agent stress testing, MCP tool latency benchmarks |
| P4 | End-to-end | 2 suites | Multi-agent development workflow, disaster recovery (backup/restore cycle) |

### Bug Fixes

- **Broken double-checked locking** in LLM initialization -- race condition causing duplicate LiteLLM setup ([e3689a0](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/e3689a0))
- **Naive/aware datetime mixing** for SQLite compatibility ([214502f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/214502f))
- **LRU repo cache EMFILE errors** under high concurrency ([18f395f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/18f395f))
- **Project identity and rate limit handling** improvements ([6221843](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/6221843))

---

## [v0.1.5] -- 2026-01-04 (GitHub Release)

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.1.5) --|-- [Compare v0.1.4...v0.1.5](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.4...v0.1.5)

### Bug Fixes

- **Critical datetime comparison fix** -- `_naive_utc()` helper ensures naive UTC datetimes for all SQLite comparisons, eliminating `TypeError: can't compare offset-naive and offset-aware datetimes` in file reservation TTL transitions ([4255d1a](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/4255d1a))
- **Placeholder detection for better DX** -- server, shell hooks, and install scripts now detect common placeholder patterns (`YOUR_*`, `PLACEHOLDER`, `<PROJECT>`, etc.) and return clear `CONFIGURATION_ERROR` messages instead of cryptic `NOT_FOUND` errors ([8de9e9b](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8de9e9b))
- **Hook placeholder standardization** -- all hooks silently exit when unconfigured instead of making failed API calls ([5db6ada](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/5db6ada))

---

## [v0.1.4] -- 2026-01-04 (GitHub Release)

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.1.4) --|-- [Compare v0.1.3...v0.1.4](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.3...v0.1.4)

### Bug Fixes

- **Gemini CLI integration** -- use `httpUrl` instead of `url` for Streamable HTTP transport; remove invalid `type: http` key that caused startup failures; clean up legacy config from previous runs ([259c75f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/259c75f))
- **Codex CLI integration** -- fix TOML structure (top-level keys before section headers), handle notify hook JSON as command-line argument, remove obsolete config ([905b4a1](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/905b4a1))
- **File reservation Git artifacts** -- write JSON artifacts on release/expire operations, not just on create; centralized payload building via `_file_reservation_payload()` and `_write_file_reservation_records()` ([ab2ce8f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ab2ce8f))
- **Lifespan improvements** -- proper engine disposal and repo cache clearing on shutdown

---

## [v0.1.3] -- 2025-12-31 (GitHub Release)

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.1.3) --|-- [Compare v0.1.2...v0.1.3](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.2...v0.1.3)

### Bug Fixes

- **Docker build fix** -- copy `README.md` before `uv sync` because hatchling requires it during dependency resolution (`pyproject.toml` references it via `readme = "README.md"`) ([da048e3](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/da048e3))

---

## [v0.1.2] -- 2025-12-31 (GitHub Release)

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.1.2) --|-- [Compare v0.1.1...v0.1.2](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.1...v0.1.2)

### Bug Fixes

- **CI type checker errors** -- add explicit `cast(Path, ...)` for `rglob()` results in `cli.py` and `storage.py` where the type checker infers `Path | Buffer` but runtime values are always `Path`; remove redundant `cast(str, name)` for `os.walk()` filenames ([a5a5fb4](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/a5a5fb4))

---

## [v0.1.1] -- 2025-12-31 (GitHub Release)

**Agent Name Namespace Expansion**

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.1.1) --|-- [Compare v0.1.0...v0.1.1](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.0...v0.1.1)

### Bug Fixes

- **Agent name namespace exhaustion** -- the original word lists only provided 132 possible name combinations (12 adjectives x 11 nouns). Expanded to 62 adjectives and 69 nouns for 4,278 combinations (33x increase), making exhaustion virtually impossible for real-world usage ([3331a07](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/3331a07))

---

## [v0.1.0] -- 2025-10-23 through 2025-12-31

**Initial release.** No GitHub Release artifact; the v0.1.1 tag is the first published release. This section covers the foundational development from the initial commit through the pre-v0.1.1 state (~393 commits).

### Core Capabilities

- **Agent identity system** -- auto-generated memorable names (AdjectiveNoun format), `register_agent` and `whois` tools, sanitized user-provided names
- **Messaging** -- send, reply, read/acknowledge, search (FTS5-backed), summarize threads, cross-project delivery with explicit addressing
- **File reservations** -- advisory path claim conflict detection, release tool, TTL-based expiry, Git-auditable artifacts
- **MCP resources** -- inbox, outbox, urgent-unread, ack-required, ack-overdue, recent usage, dynamic capabilities
- **Git-backed storage** -- human-auditable message archive, attachment deduplication, commit trailers
- **SQLite indexing** -- FTS5 full-text search, performance indexes, Alembic migrations

### Server and Transport

- **FastAPI HTTP wrapper** -- Bearer token auth, health endpoints, CORS middleware, rate limiting (token-bucket), JWKS/JWT/RBAC
- **CLI** -- Typer-based with subcommands for mail, guard, archive, doctor operations; Rich visual enhancements
- **Pre-commit/pre-push guards** -- conflict detection hooks with bypass support
- **Contact policies** -- auto-allow heuristics, ACK TTL warnings, escalation

### Integrations

- Automated setup scripts for **Claude Code**, **Codex**, **Cursor**, and **Gemini CLI**
- Meta-integration script for zero-friction automation with auto-token generation
- Docker multi-arch builds, systemd deployment, Prometheus alert rules, logrotate

### Infrastructure

- CI/CD workflows (GitHub Actions), nightly background claims cleanup
- Production observability guide, MCP design best practices documentation
- Comprehensive tool docstrings with usage patterns, pitfalls, and examples
- One-line `curl | bash` installer with `am` shell alias

---

[Unreleased]: https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.3.0...main
[v0.3.0]: https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.2.1...v0.3.0
[v0.2.1]: https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.2.0...v0.2.1
[v0.2.0]: https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.5...v0.2.0
[v0.1.5]: https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.4...v0.1.5
[v0.1.4]: https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.3...v0.1.4
[v0.1.3]: https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.2...v0.1.3
[v0.1.2]: https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.1...v0.1.2
[v0.1.1]: https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.0...v0.1.1
