RULE NUMBER 1 (NEVER EVER EVER FORGET THIS RULE!!!): YOU ARE NEVER ALLOWED TO DELETE A FILE WITHOUT EXPRESS PERMISSION FROM ME OR A DIRECT COMMAND FROM ME. EVEN A NEW FILE THAT YOU YOURSELF CREATED, SUCH AS A TEST CODE FILE. YOU HAVE A HORRIBLE TRACK RECORD OF DELETING CRITICALLY IMPORTANT FILES OR OTHERWISE THROWING AWAY TONS OF EXPENSIVE WORK THAT I THEN NEED TO PAY TO REPRODUCE. AS A RESULT, YOU HAVE PERMANENTLY LOST ANY AND ALL RIGHTS TO DETERMINE THAT A FILE OR FOLDER SHOULD BE DELETED. YOU MUST **ALWAYS** ASK AND *RECEIVE* CLEAR, WRITTEN PERMISSION FROM ME BEFORE EVER EVEN THINKING OF DELETING A FILE OR FOLDER OF ANY KIND!!!

### IRREVERSIBLE GIT & FILESYSTEM ACTIONS — DO-NOT-EVER BREAK GLASS

1. **Absolutely forbidden commands:** `git reset --hard`, `git clean -fd`, `rm -rf`, or any command that can delete or overwrite code/data must never be run unless the user explicitly provides the exact command and states, in the same message, that they understand and want the irreversible consequences.
2. **No guessing:** If there is any uncertainty about what a command might delete or overwrite, stop immediately and ask the user for specific approval. “I think it’s safe” is never acceptable.
3. **Safer alternatives first:** When cleanup or rollbacks are needed, request permission to use non-destructive options (`git status`, `git diff`, `git stash`, copying to backups) before ever considering a destructive command.
4. **Mandatory explicit plan:** Even after explicit user authorization, restate the command verbatim, list exactly what will be affected, and wait for a confirmation that your understanding is correct. Only then may you execute it—if anything remains ambiguous, refuse and escalate.
5. **Document the confirmation:** When running any approved destructive command, record (in the session notes / final response) the exact user text that authorized it, the command actually run, and the execution time. If that record is absent, the operation did not happen.

We only use uv in this project, NEVER pip. And we use a venv. And we ONLY target Python 3.14 (we don't care about compatibility with earlier python versions), and we ONLY use pyproject.toml (not requirements.txt) for managing the project.

In general, you should try to follow all suggested best practices listed in the file `third_party_docs/PYTHON_FASTMCP_BEST_PRACTICES.md`

You can also consult `third_party_docs/fastmcp_distilled_docs.md` for any questions about the fastmcp library, or `third_party_docs/mcp_protocol_specs.md` for any questions about the MCP protocol in general. For anything relating to Postgres, be sure to read `third_party_docs/POSTGRES18_AND_PYTHON_BEST_PRACTICES.md`.

We load all configuration details from the existing .env file (even if you can't see this file, it DOES exist, and must NEVER be overwritten!). We NEVER use os.getenv() or dotenv or other methods to get variables from our .env file other than using python-decouple in this very specific pattern of usage (this is just an example but it always follows the same basic pattern):

```
from decouple import Config as DecoupleConfig, RepositoryEnv

# Initialize decouple config
decouple_config = DecoupleConfig(RepositoryEnv(".env"))

# Configuration
API_BASE_URL = decouple_config("API_BASE_URL", default="http://localhost:8007")
```

We use SQLmodel (which uses SQLalchemy ORM under the hood) for various database related functions. Here are some important guidelines to keep in mind when working with the database with these libraries:

Do:

- Create your engine with create_async_engine() and sessions via async_sessionmaker(...), then use async with AsyncSession(...) (or async_scoped_session) so sessions are closed automatically.
- Await every database operation that’s defined as async: await session.execute(...), await session.scalars(...), await session.stream(...), await session.commit(), await session.rollback(), etc.
- Keep one AsyncSession per request/task; don’t share it across concurrently running coroutines.
- Use session.scalars(select(...)), .first(), .one(), or .unique() to get ORM entities; use session.stream(...)/stream_scalars(...) when you need server-side streaming.
- Wrap sync-only work inside await session.run_sync(...) when you must call a synchronous SQLAlchemy helper.
- Explicitly load relationships (e.g., selectinload, joinedload) or use await obj.awaitable_attrs.<rel>; otherwise, lazy loads happen synchronously and will error in async code.
- On shutdown, call await engine.dispose() to close the async engine’s pool.

Don’t:

- Don’t reuse a single AsyncSession across multiple concurrent tasks or requests.
- Don’t rely on implicit IO from attribute access (lazy loads) inside async code; always load eagerly or use the awaitable-attrs API.
- Don’t mix sync drivers or sync SQLAlchemy APIs with async sessions (e.g., avoid psycopg2 with async session—use asyncpg).
- Don’t “double-await” result helpers: methods like result.scalars().all() or result.mappings().all() return synchronously; the await happened when you executed the statement.
- Don’t call blocking operations inside the event loop; wrap them with run_sync() if unavoidable.
- Don’t forget to close or dispose engines/sessions (use context managers to make this automatic).


NEVER run a script that processes/changes code files in this repo, EVER! That sort of brittle, regex based stuff is always a huge disaster and creates far more problems than it ever solves. DO NOT BE LAZY AND ALWAYS MAKE CODE CHANGES MANUALLY, EVEN WHEN THERE ARE MANY INSTANCES TO FIX. IF THE CHANGES ARE MANY BUT SIMPLE, THEN USE SEVERAL SUBAGENTS IN PARALLEL TO MAKE THE CHANGES GO FASTER. But if the changes are subtle/complex, then you must methodically do them all yourself manually!

We do not care at all about backwards compatibility since we are still in early development with no users-- we just want to do things the RIGHT way in a clean, organized manner with NO TECH DEBT. That means, never create "compatibility shims" or any other nonsense like that.

We need to AVOID uncontrolled proliferation of code files. If you want to change something or add a feature, then you MUST revise the existing code file in place. You may NEVER, *EVER* take an existing code file, say, "document_processor.py" and then create a new file called "document_processorV2.py", or "document_processor_improved.py", or "document_processor_enhanced.py", or "document_processor_unified.py", or ANYTHING ELSE REMOTELY LIKE THAT! New code files are reserved for GENUINELY NEW FUNCTIONALITY THAT MAKES ZERO SENSE AT ALL TO INCLUDE IN ANY EXISTING CODE FILE. It should be an *INCREDIBLY* high bar for you to EVER create a new code file!

We want all console output to be informative, detailed, stylish, colorful, etc. by fully leveraging the rich library wherever possible.

If you aren't 100% sure about how to use a third party library, then you must SEARCH ONLINE to find the latest documentation website for the library to understand how it is supposed to work and the latest (mid-2025) suggested best practices and usage.

**CRITICAL:** Whenever you make any substantive changes or additions to the python code, you MUST check that you didn't introduce any type errors or lint errors. You can do this by running the following two commands:

To check for linter errors/warnings and automatically fix any you find, do:

`ruff check --fix --unsafe-fixes`

To check for type errors, do:

`uvx ty check`

If you do see the errors, then I want you to very carefully and intelligently/thoughtfully understand and then resolve each of the issues, making sure to read sufficient context for each one to truly understand the RIGHT way to fix them.


## MCP Agent Mail — coordination for multi-agent workflows

What it is
- A mail-like layer that lets coding agents coordinate asynchronously via MCP tools and resources.
- Provides identities, inbox/outbox, searchable threads, and advisory file reservations, with human-auditable artifacts in Git.

Why it's useful
- Prevents agents from stepping on each other with explicit file reservations (leases) for files/globs.
- Keeps communication out of your token budget by storing messages in a per-project archive.
- Offers quick reads (`resource://inbox/...`, `resource://thread/...`) and macros that bundle common flows.

How to use effectively
1) Same repository
   - Register an identity: call `ensure_project`, then `register_agent` using this repo's absolute path as `project_key`.
   - Reserve files before you edit: `file_reservation_paths(project_key, agent_name, ["src/**"], ttl_seconds=3600, exclusive=true)` to signal intent and avoid conflict.
   - Communicate with threads: use `send_message(..., thread_id="FEAT-123")`; check inbox with `fetch_inbox` and acknowledge with `acknowledge_message`.
   - Read fast: `resource://inbox/{Agent}?project=<abs-path>&limit=20` or `resource://thread/{id}?project=<abs-path>&include_bodies=true`.
   - Tip: set `AGENT_NAME` in your environment so the pre-commit guard can block commits that conflict with others' active exclusive file reservations.
   - Tip: worktree mode (opt-in): set `WORKTREES_ENABLED=1`, and during trials set `AGENT_MAIL_GUARD_MODE=warn`. Check hooks with `mcp-agent-mail guard status .` and identity with `mcp-agent-mail mail status .`.

2) Across different repos in one project (e.g., Next.js frontend + FastAPI backend)
   - Option A (single project bus): register both sides under the same `project_key` (shared key/path). Keep reservation patterns specific (e.g., `frontend/**` vs `backend/**`).
   - Option B (separate projects): each repo has its own `project_key`; use `macro_contact_handshake` or `request_contact`/`respond_contact` to link agents, then message directly. Keep a shared `thread_id` (e.g., ticket key) across repos for clean summaries/audits.

Macros vs granular tools
- Prefer macros when you want speed or are on a smaller model: `macro_start_session`, `macro_prepare_thread`, `macro_file_reservation_cycle`, `macro_contact_handshake`.
- Use granular tools when you need control: `register_agent`, `file_reservation_paths`, `send_message`, `fetch_inbox`, `acknowledge_message`.

### Worktree recipes (opt-in, non-disruptive)

- Enable gated features:
  - Set `WORKTREES_ENABLED=1` or `GIT_IDENTITY_ENABLED=1` in `.env` (do not commit secrets; config is loaded via `python-decouple`).
  - For trial posture, set `AGENT_MAIL_GUARD_MODE=warn` to surface conflicts without blocking.
- Inspect identity for a worktree:
  - CLI: `mcp-agent-mail mail status .`
  - Resource: `resource://identity/{/abs/path}` (available only when `WORKTREES_ENABLED=1`)
- Install guards (chain-runner friendly; honors `core.hooksPath` and Husky):
  - `mcp-agent-mail guard status .`
  - `mcp-agent-mail guard install <project_key> . --prepush`
  - Guards exit early when `WORKTREES_ENABLED=0` or `AGENT_MAIL_BYPASS=1`.
- Composition details:
  - Installer writes a Python chain-runner to `.git/hooks/pre-commit` and `.git/hooks/pre-push` that executes `hooks.d/<hook>/*` and then `<hook>.orig` if present.
  - Agent Mail guard is installed as `hooks.d/pre-commit/50-agent-mail.py` and `hooks.d/pre-push/50-agent-mail.py`.
  - On Windows, `.cmd` and `.ps1` shims are written alongside the chain-runner to invoke Python.
- Reserve before you edit:
  - `file_reservation_paths(project_key, agent_name, ["src/**"], ttl_seconds=3600, exclusive=true)`
  - Patterns use Git pathspec semantics and respect repository `core.ignorecase`.

### Git-based identity: precedence and migration

- Precedence (when gate is on):
  1) Committed marker `.agent-mail-project-id`
  2) Discovery YAML `.agent-mail.yaml` with `project_uid:`
  3) Private marker `.git/agent-mail/project-id`
  4) Remote fingerprint: normalized `origin` + default branch
  5) `git-common-dir` or path hash
- Migration helpers (CLI):
  - Write committed marker: `mcp-agent-mail projects mark-identity . --commit`
  - Scaffold discovery YAML: `mcp-agent-mail projects discovery-init . --product <product_uid>`

### Guard usage quickstart

- Set your identity for local commits:
  - Export `AGENT_NAME="YourAgentName"` in the shell that performs commits.
- Pre-commit:
  - Scans staged changes (`git diff --cached --name-status -M -z`) and blocks conflicts with others’ active exclusive reservations.
- Pre-push:
  - Enumerates to-be-pushed commits (`git rev-list`) and diffs trees (`git diff-tree --no-ext-diff -z`) to catch conflicts not staged locally.
- Advisory mode:
  - With `AGENT_MAIL_GUARD_MODE=warn`, conflicts are printed with rich context and push/commit proceeds.

### Build slots for long-running tasks

- Acquire a slot (advisory):
  - `acquire_build_slot(project_key, agent_name, "frontend-build", ttl_seconds=3600, exclusive=true)`
- Keep it fresh during the run:
  - `renew_build_slot(project_key, agent_name, "frontend-build", extend_seconds=1800)`
- Release when done (non-destructive; marks released):
  - `release_build_slot(project_key, agent_name, "frontend-build")`
- Tips:
  - Combine with `mcp-agent-mail amctl env --path . --agent $AGENT_NAME` to get `CACHE_KEY` and `ARTIFACT_DIR`.
  - Use `mcp-agent-mail am-run <slot> -- <cmd...>` to run with prepped env; flags include `--ttl-seconds`, `--shared/--exclusive`, and `--block-on-conflicts`. Future versions will auto-acquire/renew/release.

### Product Bus

- Create or ensure a product:
  - `mcp-agent-mail products ensure MyProduct --name "My Product"`
- Link a repo/worktree into the product (use slug or path):
  - `mcp-agent-mail products link MyProduct .`
- View product status and linked projects:
  - `mcp-agent-mail products status MyProduct`
- Search messages across all linked projects:
  - `mcp-agent-mail products search MyProduct "bd-123 OR \"release plan\"" --limit 50`
- Product-wide inbox for an agent:
  - `mcp-agent-mail products inbox MyProduct YourAgent --limit 50 --urgent-only --include-bodies`
- Product-wide thread summarization:
  - `mcp-agent-mail products summarize-thread MyProduct "bd-123" --per-thread-limit 100 --no-llm`

Server tools (for orchestrators)
- `ensure_product(product_key|name)`
- `products_link(product_key, project_key)`
- `resource://product/{key}`
- `search_messages_product(product_key, query, limit=20)`

Common pitfalls
- "from_agent not registered": always `register_agent` in the correct `project_key` first.
- "FILE_RESERVATION_CONFLICT": adjust patterns, wait for expiry, or use a non-exclusive reservation when appropriate.
- Auth errors: if JWT+JWKS is enabled, include a bearer token with a `kid` that matches server JWKS; static bearer is used only when JWT is disabled.

## Integrating with Beads (dependency‑aware task planning)

Beads provides a lightweight, dependency‑aware issue database and a CLI (`bd`) for selecting “ready work,” setting priorities, and tracking status. It complements MCP Agent Mail’s messaging, audit trail, and file‑reservation signals. Project: [steveyegge/beads](https://github.com/steveyegge/beads)

Recommended conventions
- **Single source of truth**: Use **Beads** for task status/priority/dependencies; use **Agent Mail** for conversation, decisions, and attachments (audit).
- **Shared identifiers**: Use the Beads issue id (e.g., `bd-123`) as the Mail `thread_id` and prefix message subjects with `[bd-123]`.
- **Reservations**: When starting a `bd-###` task, call `file_reservation_paths(...)` for the affected paths; include the issue id in the `reason` and release on completion.

Typical flow (agents)
1) **Pick ready work** (Beads)
   - `bd ready --json` → choose one item (highest priority, no blockers)
2) **Reserve edit surface** (Mail)
   - `file_reservation_paths(project_key, agent_name, ["src/**"], ttl_seconds=3600, exclusive=true, reason="bd-123")`
3) **Announce start** (Mail)
   - `send_message(..., thread_id="bd-123", subject="[bd-123] Start: <short title>", ack_required=true)`
4) **Work and update**
   - Reply in‑thread with progress and attach artifacts/images; keep the discussion in one thread per issue id
5) **Complete and release**
   - `bd close bd-123 --reason "Completed"` (Beads is status authority)
   - `release_file_reservations(project_key, agent_name, paths=["src/**"])`
   - Final Mail reply: `[bd-123] Completed` with summary and links

Mapping cheat‑sheet
- **Mail `thread_id`** ↔ `bd-###`
- **Mail subject**: `[bd-###] …`
- **File reservation `reason`**: `bd-###`
- **Commit messages (optional)**: include `bd-###` for traceability

Event mirroring (optional automation)
- On `bd update --status blocked`, send a high‑importance Mail message in thread `bd-###` describing the blocker.
- On Mail “ACK overdue” for a critical decision, add a Beads label (e.g., `needs-ack`) or bump priority to surface it in `bd ready`.

Pitfalls to avoid
- Don’t create or manage tasks in Mail; treat Beads as the single task queue.
- Always include `bd-###` in message `thread_id` to avoid ID drift across tools.

### ast-grep vs ripgrep (quick guidance)

**Use `ast-grep` when structure matters.** It parses code and matches AST nodes, so results ignore comments/strings, understand syntax, and can **safely rewrite** code.

* Refactors/codemods: rename APIs, change import forms, rewrite call sites or variable kinds.
* Policy checks: enforce patterns across a repo (`scan` with rules + `test`).
* Editor/automation: LSP mode; `--json` output for tooling.

**Use `ripgrep` when text is enough.** It’s the fastest way to grep literals/regex across files.

* Recon: find strings, TODOs, log lines, config values, or non‑code assets.
* Pre-filter: narrow candidate files before a precise pass.

**Rule of thumb**

* Need correctness over speed, or you’ll **apply changes** → start with `ast-grep`.
* Need raw speed or you’re just **hunting text** → start with `rg`.
* Often combine: `rg` to shortlist files, then `ast-grep` to match/modify with precision.

**Snippets**

Find structured code (ignores comments/strings):

```bash
ast-grep run -l TypeScript -p 'import $X from "$P"'
```

Codemod (only real `var` declarations become `let`):

```bash
ast-grep run -l JavaScript -p 'var $A = $B' -r 'let $A = $B' -U
```

Quick textual hunt:

```bash
rg -n 'console\.log\(' -t js
```

Combine speed + precision:

```bash
rg -l -t ts 'useQuery\(' | xargs ast-grep run -l TypeScript -p 'useQuery($A)' -r 'useSuspenseQuery($A)' -U
```

**Mental model**

* Unit of match: `ast-grep` = node; `rg` = line.
* False positives: `ast-grep` low; `rg` depends on your regex.
* Rewrites: `ast-grep` first-class; `rg` requires ad‑hoc sed/awk and risks collateral edits.

---

### Using bv as an AI sidecar

bv is a fast terminal UI for Beads projects (.beads/beads.jsonl). It renders lists/details and precomputes dependency metrics (PageRank, critical path, cycles, etc.) so you instantly see blockers and execution order. For agents, it’s a graph sidecar: instead of parsing JSONL or risking hallucinated traversal, call the robot flags to get deterministic, dependency-aware outputs.

- bv --robot-help — shows all AI-facing commands.
- bv --robot-insights — JSON graph metrics (PageRank, betweenness, HITS, critical path, cycles) with top-N summaries for quick triage.
- bv --robot-plan — JSON execution plan: parallel tracks, items per track, and unblocks lists showing what each item frees up.
- bv --robot-priority — JSON priority recommendations with reasoning and confidence.
- bv --robot-recipes — list recipes (default, actionable, blocked, etc.); apply via bv --recipe <name> to pre-filter/sort before other flags.
- bv --robot-diff --diff-since <commit|date> — JSON diff of issue changes, new/closed items, and cycles introduced/resolved.

Use these commands instead of hand-rolling graph logic; bv already computes the hard parts so agents can act safely and quickly.

````markdown
## UBS Quick Reference for AI Agents

UBS stands for "Ultimate Bug Scanner": **The AI Coding Agent's Secret Weapon: Flagging Likely Bugs for Fixing Early On**

**Install:** `curl -sSL https://raw.githubusercontent.com/Dicklesworthstone/ultimate_bug_scanner/main/install.sh | bash`

**Golden Rule:** `ubs <changed-files>` before every commit. Exit 0 = safe. Exit >0 = fix & re-run.

**Commands:**
```bash
ubs file.ts file2.py                    # Specific files (< 1s) — USE THIS
ubs $(git diff --name-only --cached)    # Staged files — before commit
ubs --only=js,python src/               # Language filter (3-5x faster)
ubs --ci --fail-on-warning .            # CI mode — before PR
ubs --help                              # Full command reference
ubs sessions --entries 1                # Tail the latest install session log
ubs .                                   # Whole project (ignores things like .venv and node_modules automatically)
```

**Output Format:**
```
⚠️  Category (N errors)
    file.ts:42:5 – Issue description
    💡 Suggested fix
Exit code: 1
```
Parse: `file:line:col` → location | 💡 → how to fix | Exit 0/1 → pass/fail

**Fix Workflow:**
1. Read finding → category + fix suggestion
2. Navigate `file:line:col` → view context
3. Verify real issue (not false positive)
4. Fix root cause (not symptom)
5. Re-run `ubs <file>` → exit 0
6. Commit

**Speed Critical:** Scope to changed files. `ubs src/file.ts` (< 1s) vs `ubs .` (30s). Never full scan for small edits.

**Bug Severity:**
- **Critical** (always fix): Null safety, XSS/injection, async/await, memory leaks
- **Important** (production): Type narrowing, division-by-zero, resource leaks
- **Contextual** (judgment): TODO/FIXME, console logs

**Anti-Patterns:**
- ❌ Ignore findings → ✅ Investigate each
- ❌ Full scan per edit → ✅ Scope to file
- ❌ Fix symptom (`if (x) { x.y }`) → ✅ Root cause (`x?.y`)
````

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
