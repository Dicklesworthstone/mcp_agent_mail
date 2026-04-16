# Changelog

All notable changes to [MCP Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) are documented here.

Format: entries are organized by version in reverse chronological order. Each version header links to the GitHub comparison or release when one exists. **GitHub Release** markers distinguish versions that have published release artifacts from plain git tags. Commit links use full 40-character hashes and point to the canonical GitHub commit URL.

---

## [v0.3.1] - 2026-04-16

### Bug Fixes

- **PostgreSQL fail-fast** -- reject non-SQLite `DATABASE_URL` values at startup with a clear, actionable error instead of crashing on `CREATE VIRTUAL TABLE` deep inside schema init. Gates FTS5 DDL behind a dialect check so SQLite-only migrations stay SQLite-only (#142).
- **Docker uid mismatches** -- mark `/data/mailbox` (and the catch-all `*` inside the container) as a git `safe.directory` after the `USER appuser` switch so bind-mounted host volumes owned by a different uid no longer trigger the non-obvious `Unknown parameter: --cached` failure. Document the gotcha in the README Docker notes (#143).

## [Unreleased] (v0.3.1...main)

131 commits since v0.3.0, spanning 2026-01-07 through 2026-03-21. No release tag yet.

### Agent Identity and Lifecycle

- **Persistent window-based agent identity** -- agents get a stable identity tied to their terminal pane, surviving restarts ([32afeab](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/32afeabaa2350ec7a133235c8e2b99141858bbb0))
- **Canonical per-pane agent identity file contract** -- formalized the on-disk identity file protocol for multi-agent environments ([6715be6](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/6715be67b9982abe2611c3e466f98a2946cd0f86))
- **Sender identity verification and safe defaults** -- verify message sender identity with secure-by-default configuration ([56dce9e](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/56dce9e9b991581feaa73a905b5245461e608f1a))
- **Agent retire and project archive soft-delete** -- lifecycle management for winding down agents and archiving projects (#102, #103) ([8f3d627](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8f3d6275d4ddf7b4ff1b914504459b2305bacb78))
- **Hard delete with "I UNDERSTAND" confirmation** -- permanent deletion requiring explicit confirmation string (#105) ([74caaeb](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/74caaeb3d86c01c2877ece5cbc5ab7c7bad73f7e))

### Messaging and Coordination

- **Broadcast and topic threads** -- all-agent visibility messaging with topic-based threading for coordination ([b4ad9fc](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/b4ad9fc82070efd59f315807a83ca56ea345c616))
- **On-demand project-wide message summarization** -- generate summaries across an entire project's message history ([ee53048](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ee5304819ef6316acaa17a1d721dd026841d1fc5))
- **Contact enforcement optimization** -- batch queries for contact policy checks, SPA support ([8b6e67c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8b6e67c574725e8304b44b11820287b25717547f))
- **TOON output format support** -- optional TOON-encoded output for tools and resources using the `tru` encoder ([49beea8](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/49beea847145b2095eb337d2d7aad413ba9e05d9))

### Server and Transport

- **`/mcp` endpoint alias** -- mount `/mcp` alongside `/api` for client compatibility, increase SQLite pool to 50 ([8d6a12d](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8d6a12d9c42150abcba9087202da3a96794bd96b))
- **Periodic FD health monitor** -- background watchdog for file descriptor exhaustion in multi-agent deployments ([81dd55f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/81dd55f48125c8a50453a89d168b3802afce5723))
- **Rewrite `run_server_with_token` to use Rust `am` binary** -- server launcher now delegates to the compiled Rust binary with config.env ([1fca641](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1fca641f3aff47f9c198a81e8f7e86377cf3ea90))
- **ExpectedErrorFilter** -- suppress verbose tracebacks for expected operational errors ([d2a3f4f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/d2a3f4f02b45222800b31f86da12f97a4035dee5))

### Reservations and Storage

- **Virtual namespace support for tool/resource reservations** -- extend file reservation semantics to abstract namespaces ([b1c2051](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/b1c20517e29ecdac7b42f0b08b9f7d89e1d41747))
- **Commit queue and archive locking** -- serialized Git commit queue for high-concurrency multi-agent scenarios ([4fb2f38](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/4fb2f3801c46be6f6763dd0bd46927168ddd4642))
- **Git index.lock retry with exponential backoff** -- resilient Git operations when concurrent agents contend on the index ([5969db9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/5969db9ab19363764cf23c200060c48fa3b66187))

### Installer and Integration

- **Installer hardening** -- robust TOML URL upsert, Codex integration exports, improved token handling ([535d8aa](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/535d8aaa3ecd4d6e60b711e545ad5de945148fec), [f5bcd0a](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/f5bcd0a4925b3c1b0711f8c75dec441fc8bf0fd5), [496ac59](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/496ac592d7696767f15d15d2ad22a93a652a0803))
- **Avoid nested curl|bash** for br/bv installation ([b2fd4b8](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/b2fd4b89543c013e96ae2151e13a60a595090495))
- **Integration settings merge** -- merge settings instead of overwriting (#76) ([9ee83f3](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/9ee83f33a8bcd05a161ec9d57c3d396474a186b4))

### Bug Fixes

- **AsyncFileLock FD leaks** -- prevent file descriptor exhaustion under sustained load ([51524ae](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/51524ae3ae47c64cbb31be8e1fee33290584bbfd))
- **Lightweight `/api/health` bypass** -- health checks no longer blocked when the MCP layer is saturated ([14889f3](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/14889f30e3fb68b699facdc44e5eed8aaf706c44))
- **LIKE escape character ambiguity** -- switch to `!` as the LIKE ESCAPE char to avoid backslash escaping issues across SQLite/PostgreSQL ([1fddd32](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1fddd3270989d7fa324437403a2a7d6d4a1e967b), [9679dff](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/9679dff1d8f8655354af232910280b37d074fe8a), [5151869](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/5151869d2a20b2b187c248ffdff656dbb5bf6f59))
- **Missing schema migrations** -- add migrations for `registration_token` and `topic` columns (#106) ([caf4826](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/caf48261b892b2a2071f9f59a119152b70b91ffb))
- **Cross-project git_paths_removed leak, falsy-0 id checks, XSS in confirmation dialog, missing auth on archive tools** -- four bugs fixed in one pass ([84442d3](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/84442d3c23265c116739753669520ef9dda75fba))
- **Unbound variable in Gemini CLI integration** + permanent message deletion (#101, #104) ([ba25242](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ba25242d3efd87bd01bf24a4bcc84dddc2b2c9d2))
- **OAuth metadata paths** -- normalize trailing-slash behavior and return consistent 404s for disabled OAuth ([79b25db](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/79b25db8738dc8526545197e3223633b359b55a0), [35529e7](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/35529e717d574f429c1b3ccb9fc06e1fe220fba0))
- **Installer `exec` issue** -- remove `exec` before `run_server_with_token.sh` so the calling script resumes ([7019585](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7019585dd5a55af57060180d80c27c68cff2897d))
- **Identity script hardening** -- parent directory permissions, path validation, TOCTOU race protection ([ddcef4f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ddcef4ff886548a94c19c14b083c4ec53a4955ad))
- **Storage cleanup** -- replace unreliable refcount eviction with time-based cleanup, close EMFILE-recovery repo handles ([61bb6d1](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/61bb6d1e210b0e83e9b1adcd16fa6703c91a672c), [f2b03b9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/f2b03b9aba3b1c4a779dd9473ed839169fb66d4f))
- **Hardcoded bearer tokens** -- replace with placeholders in `.mcp.json` files ([50c34cc](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/50c34cc62b2e46470b639aa24ef422212219e9b1))
- **CLI default subcommand** -- default to `serve-http` when no subcommand given ([061435c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/061435c07455aa3fb3ac83367344ac749feedfdc))
- **send_message silent exceptions** -- replace silent exception handlers with logging ([ed078e1](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ed078e1dd30d38fe113f1b8f1910ca0702b9976d))
- **Identity race condition** -- fix race condition and redundant DB lookups in window identity ([e9424be](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/e9424be3a743a92d2927afa128798562c9a93e12))
- **CLI exit hang** -- prevent CLI commands from hanging on exit (#68) ([7ed9708](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7ed97081383d35de7fac05c6fc2382a40b0b2c67))
- **CLI connection leak** -- reset database state after startup banner ([d32caea](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/d32caeadd0e12cbf00885659b8b624725a345e8c))
- **SQLAlchemy GC warnings** -- configure `pool_reset_on_return` ([cfc22be](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/cfc22be9f097ab074ee13ff70bc5e0de9464a5da))
- **Python requirement** -- downgrade from 3.14 to 3.12 for broader compatibility ([6f61bc0](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/6f61bc0e6873fc38dfc269846cfa514bfa448031))
- **asyncio deprecation** -- use `get_running_loop` instead of `get_event_loop` ([0f45b17](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0f45b17b25da552f24d27a3102d987598a34d117))
- **pathspec deprecation** -- switch to gitignore pattern type ([4ccdfdc](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/4ccdfdcd8d8c749d2cd9b838de33f62aa82e006a))
- **Thread digest appends** -- lock thread digest appends to prevent corruption ([2e4b810](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/2e4b81087120d2efc57bc3ab4920a3b88759c45a))
- **Search session leak** -- use fresh session for LIKE fallback in `search_messages` ([1dcbe84](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1dcbe841e1ca187839b25065bae3a0a1943409fa))
- **Backup path resolution** -- add `expanduser().resolve()` to all backup-related paths ([f2ed9ae](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/f2ed9ae87e4781558a6b2c53d09c41ffeededb48))
- **.mcp.json endpoint URL** -- correct endpoint URL and restore `am` alias ([59301bc](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/59301bc1ad2b8a4e7a72675f59f6cdf0880968ce))
- **Hook dedup collision** -- use unique hook identifiers to prevent deduplication collision ([ac8d968](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ac8d9681e345cd67fed8eb9d4e16f31c4416c922))
- **4 additional bugs from code review** ([12b9d87](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/12b9d878bc2d0959e5ec55cf9451fb7e88bfa45f))

### Security

- **Constant-time bearer token comparison** -- prevent timing side-channels in auth ([7c298d4](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7c298d4e3df065ecf71035654e214c5a6cd48f4b))
- **Localhost bypass prevention** -- block spoofed localhost headers behind reverse proxies ([c911f7c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/c911f7cf70465819773dfa25b3f2b4d0b0c4100f))
- **Gate absolute attachment paths** -- prevent path traversal through attachment references ([fc57155](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/fc57155e9e3210e702bfed54cd016d81dd42b626))
- **Remove `ack_required` bypass** in contact policy enforcement ([d947651](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/d9476518a0d27ee6811d39224df2213b49e1e496))

### Other

- **License updated** to MIT with OpenAI/Anthropic Rider ([dd79b83](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/dd79b83b482a3cafdb32bbb5ac9b4a48d9fc08bc))
- Ruff auto-fixes applied (collapsed nested if, unused variables, import sorting) ([130e3cd](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/130e3cdfdd51ff6c4681de3cf60e6b99f55bea63))
- GitHub social preview image added ([558c0b6](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/558c0b67ddfe2c540574c69e94e457929e62411c))
- ACFS notification workflow for installer changes ([950fa31](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/950fa3175a98edb80271f1224e914333790530b2))

---

## [v0.3.0] -- 2026-01-07 (GitHub Release)

**Stdio Transport, Tool Filtering, Push Notifications**

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.3.0) | [Compare v0.2.1...v0.3.0](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.2.1...v0.3.0)

Tagged at [19c18f4](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/19c18f42750fcadef95a558fc2841fc76f93c253). 18 commits since v0.2.1.

### Transport and Protocol

- **Stdio transport** (`am serve-stdio`) -- run the MCP server over stdin/stdout for direct CLI integration with Claude Code and similar tools. All logging redirected to stderr to preserve protocol integrity ([07b685c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/07b685c4c8cff7c9f3a4cb24aa31e7437d51bffb))
- **Stdio protocol corruption fixes** -- rich console output and tool debug panels were writing to stdout, corrupting the stdio protocol ([ad350e9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ad350e99e69d1a1c9e0ef07e6989e27dd0ebddf9), [e10166c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/e10166c28f14ab2d59a5f6d2c94b29d0b6a6da20))

### Tool Management

- **Tool filtering profiles** -- reduce token overhead by exposing only needed tools via `TOOLS_FILTER_PROFILE` (full, core, minimal, messaging, custom). The "minimal" profile cuts context by ~70% ([a952e21](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/a952e21a27dab7b44f94c65cc6a4dc1fdd06bd5a), [14c4c23](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/14c4c2325f128140f0bfbb9e29a9c1c8b27cde6f))

### Notification and Coordination

- **Push notification signals** -- file-system signal files that agents can watch with inotify/FSEvents/kqueue for instant notification without polling ([250567d](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/250567dd3f4bfb55e9e0e23ce1b0af6a16b5f1c1))

### Stability and Resilience

- **EMFILE recovery** -- automatic cache cleanup when hitting "too many open files" limit; prevent Repo handle leak in commit path ([50e7e45](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/50e7e454ad3e2cdb5a61e7df9f9b6a6fecf2a5b5), [da35417](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/da354176d65e7e2a0d0e2b57cfac8b120ae86ba2))
- **Python 3.14 cancellation safety** -- async session management compatible with `CancelledError` as `BaseException` ([8758df1](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8758df15f78a4d84f9fcc7e7a8b26e16e4f2a4c3))
- **SQLite pool tuning** -- improved concurrent access reliability ([da35417](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/da354176d65e7e2a0d0e2b57cfac8b120ae86ba2))
- **Viewer SRI fix** -- excluded HTML from integrity map to fix asset loading ([0f8036b](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0f8036b6b4dda78e54f43bce9f7f9c27c8f0db10))
- **SQLAlchemy where clause casting** -- use `cast(Any, ...)` for type-safe WHERE clauses ([e480650](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/e4806505bb4a84dcb1bb6d62c1a2af78a4e2e4bc))

### Testing

- Comprehensive tests for tool filtering and notifications ([6104fbd](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/6104fbd34eb67c59c9af7f5cae2bf9fc6f921a7e))
- E2E test updates for advisory file reservation model ([c461fa1](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/c461fa1f0df4c5a6a0b6be11d2e43dc1ec18a3ac))
- Global resource cleanup fixture for FD leak prevention ([4c9f5b2](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/4c9f5b2de65bbac39e7c21f0e87e2c9a9988f62b))
- Fix flaky tests for CI reliability ([c715048](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/c715048e2c53be98c0b5b7f69f38b40ce3d4bfab))

---

## [v0.2.1] -- 2026-01-06 (GitHub Release)

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.2.1) | [Compare v0.2.0...v0.2.1](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.2.0...v0.2.1)

Tagged at [6e0e3a8](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/6e0e3a874ac94897f8a62e2479be23135a74ee83). 20 commits since v0.2.0.

### Diagnostics

- **`am doctor` diagnostic and repair CLI commands** -- new CLI commands for diagnosing and repairing Agent Mail installations ([3ff1241](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/3ff1241dc3d3c09e5c4f1a39c40fa19e5f93b1e5))

### Validation and Security

- **Thread ID format validation** -- new `validate_thread_id_format()` utility prevents malicious thread IDs ([01ac7f0](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/01ac7f05b3f98d3db29bb4ab3daac5f1f69e4d94))
- **Path traversal prevention** -- `_resolve_archive_relative_path()` blocks directory traversal in attachment paths with `Path.resolve().relative_to()` defense-in-depth against symlink escapes ([91ea1a9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/91ea1a9a21db1a5b0dc23b3d38aa4c8c20d26c7a))

### Python 3.14 Compatibility

- **Cancellation safety** -- `asyncio.shield()` wraps all async cleanup (session close, engine dispose, AsyncFileLock, archive_write_lock) now that `CancelledError` is a `BaseException` in Python 3.14 ([327e8cb](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/327e8cb91f9ba1ad64a6b9f94baaf9e4edfab89e), [91ea1a9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/91ea1a9a21db1a5b0dc23b3d38aa4c8c20d26c7a))

### Resource Management

- **FD exhaustion prevention** -- SQLite now uses `NullPool` to prevent file descriptor exhaustion on macOS; PostgreSQL and other backends retain standard pooling ([327e8cb](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/327e8cb91f9ba1ad64a6b9f94baaf9e4edfab89e))
- **Proactive FD cleanup** -- integrate cleanup into repo creation path ([f814f8d](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/f814f8d86e9bcbc19d27a5f2f2b1b39df56ac3bc))
- **Git blob resource leak** -- close `data_stream` after reading blob contents ([1e42952](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1e42952b3d3f3ed24e3eb0c3e7dcdae3d44d3bcf))
- **Storage race condition** -- fix `_ensure_repo` cache put race ([61df73b](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/61df73bf8b5f2f4d09ed6e3f3af1de4220e31f13))

### Concurrency

- **Idempotent concurrent creation** -- `ensure_project()` and `_get_or_create_agent()` handle `IntegrityError` for truly idempotent concurrent creation ([0e15860](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0e1586017a0f5e2a12d4975a1a5579b04f26a2a7))
- **Guard chain-runner preservation** -- preserve existing pre-commit chain when other plugins are installed ([9510799](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/95107998b2ff9a2c2bd2e5c0e38c14ded22e20ed))
- **SRI computation** -- include HTML files in integrity computation ([4bbce02](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/4bbce0242929e6de1a99e28c25b2b6a5ded4b3a8))

---

## [v0.2.0] -- 2026-01-06 (GitHub Release)

**Comprehensive Test Coverage Milestone**

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.2.0) | [Compare v0.1.5...v0.2.0](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.5...v0.2.0)

Tagged at [580e684](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/580e684a49ad8337fecf9fad6c6fd63deb1926ab). 65 commits since v0.1.5. Completion of all 30 beads in the testing-tasks-v2 initiative. 424+ tests passing across all priority levels (P0-P4), 26 new test suites, 11 bug fixes, zero breaking changes.

### Regression Tests (P0)

- **Datetime handling** -- naive/aware datetime regression tests for SQLite compatibility ([1c34c5d](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1c34c5d))
- **Session context persistence** -- verify session context survives across operations ([0750db0](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0750db0))
- **Agent name validation** -- ensure agent name format constraints are enforced ([2aefc55](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/2aefc55))

### Core Flow Tests (P1)

- **Contact management workflow** -- contact request/approval lifecycle ([0a7e78a](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0a7e78a))
- **Message delivery** -- end-to-end message delivery verification ([2aefc55](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/2aefc55))
- **File reservation lifecycle** -- file reservation CRUD operations ([91409eb](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/91409eb))
- **Project and agent setup** -- initialization and registration flows ([324428c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/324428c))
- **MCP resources read access** -- resource endpoint validation ([7c61715](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7c61715))

### Integration Tests (P2)

| Category | Tests | Commits |
|----------|-------|---------|
| CLI: Mail commands | 29 | [aeb3dd9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/aeb3dd9) |
| CLI: Archive commands | 24 | [74cc9de](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/74cc9de) |
| CLI: Guard commands | 20 | [89cc754](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/89cc754) |
| HTTP: Authentication | 15 | [f755d3a](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/f755d3a) |
| HTTP: Rate limiting | 13 | [07f2742](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/07f2742), [9b91d09](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/9b91d09) |
| HTTP: Server/transport | 12 | [fb30031](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/fb30031) |
| Guards: Pre-commit | 14 | [2df583d](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/2df583d) |
| Guards: Pre-push | 20 | [4a264e4](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/4a264e4) |
| Error: Database | 10 | [9f5a410](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/9f5a410) |
| Error: Git archive | 21 | [1440559](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1440559) |
| Error: Invalid inputs | 12 | [b19a19c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/b19a19c) |

### Non-Functional Tests (P3)

- **Security** -- path traversal prevention, input sanitization ([5d9ccec](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/5d9ccec), [a4c4daa](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/a4c4daa))
- **Concurrency** -- multi-agent stress testing with 70% success thresholds ([0bf3d2e](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0bf3d2e))
- **Performance** -- MCP tool latency benchmarks ([e940502](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/e940502))

### End-to-End Scenarios (P4)

- **Multi-agent development workflow** -- full multi-agent development simulation ([0af744f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0af744f))
- **Disaster recovery** -- backup/restore cycle verification ([a9e9e0d](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/a9e9e0d))

### Bug Fixes

- **Broken double-checked locking** in LLM initialization -- race condition causing duplicate LiteLLM setup ([e3689a0](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/e3689a0))
- **Naive/aware datetime mixing** for SQLite compatibility ([53f5062](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/53f5062))
- **LRU repo cache EMFILE errors** under high concurrency ([18f395f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/18f395f))
- **Project identity and rate limit handling** improvements ([6221843](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/6221843))

---

## [v0.1.5] -- 2026-01-04 (GitHub Release)

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.1.5) | [Compare v0.1.4...v0.1.5](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.4...v0.1.5)

Tagged at [f941ace](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/f941ace41e4a69ef0b99627a3022fa3befd8d0b8). 6 commits since v0.1.4.

### Datetime Safety

- **Critical datetime comparison fix** -- `_naive_utc()` helper ensures naive UTC datetimes for all SQLite comparisons, eliminating `TypeError: can't compare offset-naive and offset-aware datetimes` in file reservation TTL transitions. `_ensure_utc_dt()` added for display/filtering operations in the CLI ([4255d1a](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/4255d1a))

### Developer Experience

- **Placeholder detection for better DX** -- server, shell hooks, and install scripts now detect common placeholder patterns (`YOUR_*`, `PLACEHOLDER`, `<PROJECT>`, `{PROJECT}`, `$PROJECT`) and return clear `CONFIGURATION_ERROR` messages instead of cryptic `NOT_FOUND` errors ([8de9e9b](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8de9e9b))
- **Hook placeholder standardization** -- all hooks silently exit when unconfigured instead of making failed API calls; consistent patterns across server and shell hooks ([5db6ada](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/5db6ada), [27d9254](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/27d9254))

---

## [v0.1.4] -- 2026-01-04 (GitHub Release)

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.1.4) | [Compare v0.1.3...v0.1.4](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.3...v0.1.4)

Tagged at [c3595c2](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/c3595c2980da35ba8b972b9507a5db74de9d09be). 4 commits since v0.1.3.

### IDE Integration Fixes

- **Gemini CLI integration** -- use `httpUrl` instead of `url` for Streamable HTTP transport; remove invalid `type: http` key that caused Gemini CLI startup failures; clean up legacy config from previous runs ([259c75f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/259c75f))
- **Codex CLI integration** -- fix TOML structure (top-level keys must appear before section headers); handle notify hook JSON as command-line argument (how Codex actually passes it); remove obsolete `transport = "http"` and `[hooks]` section ([905b4a1](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/905b4a1))

### Storage Consistency

- **File reservation Git artifacts** -- write JSON artifacts on release/expire operations, not just on create; centralized payload building via `_file_reservation_payload()` and `_write_file_reservation_records()` helpers ([ab2ce8f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ab2ce8f))
- **Lifespan improvements** -- proper engine disposal and repo cache clearing on shutdown

---

## [v0.1.3] -- 2025-12-31 (GitHub Release)

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.1.3) | [Compare v0.1.2...v0.1.3](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.2...v0.1.3)

Tagged at [da3d0f2](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/da3d0f289284840673f4a4e8cf81e163f3869a32). 2 commits since v0.1.2.

### Build Fix

- **Docker build fix** -- copy `README.md` before `uv sync` because hatchling requires it during dependency resolution (`pyproject.toml` references it via `readme = "README.md"`) ([da048e3](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/da048e3))

---

## [v0.1.2] -- 2025-12-31 (GitHub Release)

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.1.2) | [Compare v0.1.1...v0.1.2](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.1...v0.1.2)

Tagged at [e2fa471](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/e2fa4718aa2c1e6ebb78955fce13b5be13a0bf75). 2 commits since v0.1.1.

### Type Safety

- **CI type checker errors** -- add explicit `cast(Path, ...)` for `rglob()` results in `cli.py` and `storage.py` where the type checker infers `Path | Buffer` but runtime values are always `Path`; remove redundant `cast(str, name)` for `os.walk()` filenames. Unblocked Docker image builds ([a5a5fb4](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/a5a5fb4))

---

## [v0.1.1] -- 2025-12-31 (GitHub Release)

**Agent Name Namespace Expansion**

[Release page](https://github.com/Dicklesworthstone/mcp_agent_mail/releases/tag/v0.1.1) | [Compare v0.1.0...v0.1.1](https://github.com/Dicklesworthstone/mcp_agent_mail/compare/v0.1.0...v0.1.1)

Tagged at [3331a07](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/3331a07e06fd2dfeae645204a8b7e2cd577107d8). First published GitHub Release.

### Identity

- **Agent name namespace exhaustion fix** -- the original word lists only provided 132 possible name combinations (12 adjectives x 11 nouns). Expanded to 62 adjectives and 69 nouns for 4,278 combinations (33x increase), making exhaustion virtually impossible for real-world usage. Fully backwards compatible -- all original 132 agent names remain valid ([3331a07](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/3331a07e06fd2dfeae645204a8b7e2cd577107d8))

---

## [v0.1.0] -- 2025-10-23 through 2025-12-31

**Initial release.** No GitHub Release artifact; v0.1.1 is the first published release. This section covers the foundational development from the initial commit through the pre-v0.1.1 state (~393 commits).

### Agent Identity and Naming

- **Agent identity system** -- auto-generated memorable names in AdjectiveNoun format (e.g., GreenCastle), `register_agent` and `whois` tools, sanitized user-provided names ([fceea84](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/fceea84f56c6b483c27378576dbaf9372d044464), [441d949](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/441d9498d1c1679dc895f9b4adb75dbcb4632783))
- **Contact enforcement** -- auto-allow heuristics, ACK TTL warnings, ACK escalation, cross-project agent links ([f9f9b3a](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/f9f9b3a5eaf4023de39e3f87ff3dbeb47d6c880d), [617ede0](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/617ede03dea08b42ae1445e9eb643b754e3b1b25))
- **Agent name format constraints** -- unified inbox enforces naming conventions ([48274d6](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/48274d6096929d28fdff6b81e79bdf93e619b9b8))

### Messaging

- **Core messaging** -- send, reply, read/acknowledge tools with comprehensive documentation ([e1a634c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/e1a634c19207ad117fd3d00f49d239e1e367b7df))
- **Search and summarization** -- FTS5-backed `search_messages` and `summarize_thread` tools ([ad04836](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ad04836c6c68149b73d160383f9eebad62ba2304), [7c90d5e](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7c90d5e5988c22bfab87faf31728a5dc81c58718))
- **Cross-project delivery** -- explicit addressing, overlap detection, reply routing ([d009f72](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/d009f72ba821f688c5c0a5c610d8260346c23ea2), [8b0b812](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8b0b8124edd0f56806e5a599ba14fb663b89b1b4), [c887b38](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/c887b38f9bb33c74f5737b5baf04fd6815639299))
- **LLM integration** -- intelligent model selection with provider auto-detection, enhanced summarization, attachment deduplication ([8418694](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/84186943c6ffd0eb2698dc0bd8347010640e3b0b), [b524b02](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/b524b029f6f2d72d3e1816c0f1a87b57a843c3bc))
- **Self-send bypass** for contact policies and idempotent recipient timestamp updates ([5d767dc](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/5d767dc7744e6f1155f1c818f630654a01350e26), [920247c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/920247c3f6ecc339fe4bb04b2865e340d3af5523))
- **Workflow macros** -- `macro_start_session`, multi-thread digest, capability annotations ([1590f8f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1590f8f04b9e22ad7d9e2bed4aabfc7814aaac3d), [582db86](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/582db8685e60573be0d69e611527a97d89da3d00))

### File Reservations

- **Advisory file reservation system** -- path claim conflict detection, release tool, TTL-based expiry, Git-auditable JSON artifacts ([3e4fb6f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/3e4fb6fbf3517dd1a87b1951e1133c603be0923d))
- **Claims-to-file-reservations terminology migration** -- complete rename across docs, tests, core, and UI for clarity ([2f28d9d](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/2f28d9d), [4046f83](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/4046f83))
- **Pattern matching improvements** -- switch to advisory model with comprehensive test suite ([c88f1ff](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/c88f1ff1aafa5ea034362e9a9e54d35a9e3515d3))

### MCP Resources

- **Inbox, outbox, urgent-unread, ack-required, ack-overdue** resource views ([3fd3345](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/3fd3345790f4912d7d605a9928d3f234da5c380a), [cbdad14](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/cbdad1416ed46ab1ae0e89aff13a904565599210))
- **Recent usage resource** and dynamic capability loading ([1590f8f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1590f8f04b9e22ad7d9e2bed4aabfc7814aaac3d), [7746e09](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7746e09c21316c5f8bad1b961a1ae49dcdec58f7))
- **Query string parsing** for view resources with project auto-detection ([6b68bea](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/6b68beab32ef6745c68d43fb54c779eb2fbfd77c), [dfe36a0](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/dfe36a06a8df514e2a7ce6f44a8ad21c89c2f1ae))
- **Git commit tracking** in inbox resource ([7a34d1b](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7a34d1ba3429924a4cd4b0f56471f17f21e47018))

### Web UI

- **Web-based mail interface** -- FTS search, comprehensive agent onboarding docs ([61f3924](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/61f392450ea4839233d5ab934ffc11d2cbed1797))
- **Gmail-style unified inbox** -- split view, advanced filtering, mark-read functionality ([7e4b601](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7e4b601ef42c51d6128e01c074ec0d9080597275), [48274d6](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/48274d6096929d28fdff6b81e79bdf93e619b9b8))
- **Human Overseer messaging UI** -- broadcast messaging for human oversight with auto-start server capability ([ec0d778](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ec0d77882a76cbb6af32d654b15b342feb559873))
- **Time-travel inbox snapshots** -- archive visualization with commit provenance tracking ([5c216fe](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/5c216fec9baba1b7a0cfd28ad35756cfa7fb0a08), [7b308ff](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7b308ff))
- **Command palette and keyboard shortcuts** -- power-user navigation features ([58dd16b](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/58dd16b9d0ef07603b8a6f2e348a78a08b77237c))
- **AI-powered project sibling discovery** -- relationship management between related projects ([7ae1e1f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7ae1e1f))
- **Alpine.js-powered UI** -- HTML sanitization (DOMPurify), dark mode, responsive design, toast notifications, stagger animations ([1ca0cab](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1ca0cabf1ec4a4252634a42e03e5be343e4a19d2), [b90980a](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/b90980a9cfdb678ade6160fc2b2c7fe84887522c))

### Server and Transport

- **FastAPI HTTP wrapper** -- Bearer token auth, health endpoints, rate limiting (token-bucket), MCP response unwrapping ([1bd8d24](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1bd8d248843c7dd9e33c61b4c9f02683c3023b0a), [7521a04](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7521a046044d2a18e90abf1557d036db005fb742))
- **JWT/RBAC auth** -- token-bucket rate limiting, cross-project messaging, enhanced deployment ([d009f72](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/d009f72ba821f688c5c0a5c610d8260346c23ea2))
- **CORS middleware** -- configurable cross-origin resource sharing ([64c71c9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/64c71c9978c35ce2cbcb295d2d5bb3d39c5b17cc), [21a2ae1](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/21a2ae1d452187f6803978a12e9a0456dc231bfe))
- **Alembic migrations** -- ACK escalation, multi-thread digest, claim enforcement ([1fa92a6](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/1fa92a698425bc900d310354a7bc389dbf9b3e47))
- **Pure SQLModel approach** -- removed Alembic migrations in favor of pure SQLModel with SQLite concurrency enhancements ([b97c437](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/b97c4379764174b84b675752898617a99995a7a5))
- **Local auth bypass** -- localhost connections can skip auth ([dbea8fa](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/dbea8fa211d926ddcc691a774dd894955fbdbe04))

### CLI

- **Typer-based CLI** -- subcommands for mail, guard, archive, doctor operations ([9fcd3ae](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/9fcd3ae01550973325537b4bdf9a6a7837e57cc6), [2211854](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/2211854aa73a0703e0c4a503a663b71cc718079d))
- **Rich visual enhancements** -- comprehensive Rich logging, unified console UI ([e6cf595](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/e6cf595ee3ad85bd62a1a9c2e679020891ec3c95), [0c77b78](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0c77b788c94aad918894b1c4d7a3bb82be4b1996))
- **Pre-commit/pre-push guards** -- conflict detection hooks with bypass support, extracted into dedicated module ([3e4fb6f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/3e4fb6fbf3517dd1a87b1951e1133c603be0923d), [405c676](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/405c67601d1857a030c1351b870a8050995687ef))
- **ACK reminder CLI** with Makefile and logrotate config ([27bed02](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/27bed024c403a42c3995f51cfa2889b7dd911793))
- **Reset command** -- clear and reset everything ([7a0874c](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7a0874cd0e9d4baeac579619339890120023c7f4))

### Storage

- **Git-backed storage** -- human-auditable message archive, attachment deduplication, commit trailers, single-repo unification ([8da73eb](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/8da73eb), [418f712](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/418f712c44bc0b50d568978cab48020f206bfec3))
- **SQLite indexing** -- FTS5 full-text search, performance indexes ([ad04836](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/ad04836c6c68149b73d160383f9eebad62ba2304))
- **SoftFileLock** for Windows compatibility ([57c827e](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/57c827ee79ccd3075237f1d166867d4518122400))
- **Archive visualization** -- Git archive exploration UI with commit provenance ([7b308ff](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/7b308ff), [b7932c1](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/b7932c1))

### Integrations

- **Automated setup scripts** for Claude Code, Codex, Cursor, Gemini CLI, Cline, Windsurf, and OpenCode ([195034d](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/195034dc73cca9462af145431b69189fb429a4b5), [73b9622](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/73b9622), [4456569](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/4456569))
- **Meta-integration script** -- zero-friction automation with auto-token generation and unified server launcher ([f6f09eb](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/f6f09eb7982a2a4b9987508172b5bee60768ee94), [cd50347](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/cd503478c989eaacdffdbb629dae3982e1294440))
- **Shared bash library** (`lib.sh`) -- DRY integration scripts with Gemini CLI registration ([0df1d01](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0df1d01))

### Observability and Security

- **Rich logging** -- comprehensive structured logging with `macro_start_session` bug fix ([0c77b78](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0c77b788c94aad918894b1c4d7a3bb82be4b1996))
- **Tool instrumentation** -- capability annotations, complexity metrics, Prometheus alert rules ([582db86](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/582db8685e60573be0d69e611527a97d89da3d00), [f74a12f](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/f74a12f780dd02ece5a32f9fe4080782646c35b8))
- **Path traversal prevention** -- DoS mitigations, XSS sanitization, markup injection protection ([9b0e9ad](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/9b0e9ad), [62c66e9](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/62c66e9))
- **Python 3.14 compatibility** -- patch for `asyncio.iscoroutinefunction` deprecation ([f2a2795](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/f2a27956204f213a18422be2c14209bfdde70272))

### Infrastructure

- **CI/CD workflows** (GitHub Actions), nightly background claims cleanup ([60b6b6e](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/60b6b6ef215cb146aeead6c8a3685f1233536cfe), [10e7919](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/10e7919b830efdc1b272d33d63260bb4c682e6f0))
- **Docker multi-arch builds** -- systemd deployment, logrotate, deployment scripts ([0cd68a5](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/0cd68a51eb00ca1a179b0abc8e202911c0f889f4), [5fa0884](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/5fa08841bb5783e805d166bec4754a72b6dc1ac8))
- **One-line `curl | bash` installer** with `am` shell alias ([be695ac](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/be695ac4f51ae133efcaf47698e4dce60e81ff45))
- **Production observability guide** -- MCP design best practices, client bootstrap example ([48da7b8](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/48da7b8e114b3c73375c9f486140cdbd1013b0a9))
- **Comprehensive test suites** -- integration tests, attachment conversion, CLI command coverage, JWT validation ([104eb79](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/104eb790f780f0f2ec17a4c617b2d611ab5fed51), [2564981](https://github.com/Dicklesworthstone/mcp_agent_mail/commit/2564981bf6ec0736d3984d1cca80d0b4867e69f1))

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
