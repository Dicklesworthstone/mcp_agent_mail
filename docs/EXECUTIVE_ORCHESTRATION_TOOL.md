# Executive Orchestration Tool

The Executive Orchestration Tool coordinates fully autonomous agent teams by generating twice-daily (or cron-driven) operating briefs, publishing them through MCP Agent Mail, and wiring those conversations back to the existing beads task ledger. It currently powers the 04:00 executive cadence but is designed to expand into a full business control loop.

## Capabilities at a Glance

- **Autonomous run orchestration**: wraps `autopilot.daily_sync.run_daily_sync()` to greet every executive agent with an actionable summary.
- **Ledger-aware briefings**: reads internal SQLModel tables first, then the shared `.beads/beads.db` queue, mapping priority → bead value and stitching together description, acceptance criteria, and notes.
- **Agent bootstrapping**: ensures an `Autopilot` agent identity exists for any project before dispatching messages.
- **Structured messaging**: relies on updated prompts (see `generate_response.py`) so executives respond with `## Commitments`, `## Bead Adjustments`, and `## Risks & Requests` sections that downstream automation can parse.
- **Logging**: writes a JSON artifact per run under `logs/autopilot/`, making it easy to replay results or feed observability pipelines.

## Code Layout

```
src/mcp_agent_mail/autopilot/
├── __init__.py            # exports run_daily_sync & CLI entry
├── daily_sync.py          # main orchestration loop (tasks → briefs → MCP send)
└── ledger.py              # shared task/bead utilities and SQLModel fallbacks
src/mcp_agent_mail/models.py  # Task, TaskAssignment, BeadTransaction tables
generate_response.py          # prompt updates that align executive replies with the tool
```

## Prerequisites

- **Database access**: `DATABASE_URL` should point to the same store the MCP server is using. The runner auto-upgrades SQLite URLs to `sqlite+aiosqlite` and Postgres URLs to `postgresql+asyncpg` when needed. If unset, it defaults to `sqlite+aiosqlite:///messages.db`.
- **Beads workspace**: default path `.beads/beads.db`; override with `BEADS_DB_PATH` for bespoke environments.
- **MCP endpoint**: by default `http://127.0.0.1:8765/mcp/`. Provide `--endpoint` to target a remote server.
- **Authorization**: supply `--bearer-token` or populate `HTTP_BEARER_TOKEN`/`MCP_BEARER_TOKEN` when sending live messages. Dry runs do not require auth.
- **Agents**: ensure the intended executive roster (e.g., `GreenPresident`, `WhiteFox`, `BlackDog`) is registered in the database; missing names will surface as runtime errors with the project slug/human key for easier debugging.

## Quick Start

### Dry Run

Run a non-sending pass that extracts tasks and renders briefs without contacting MCP:

```bash
cd mcp_agent_mail
DATABASE_URL=sqlite+aiosqlite:///messages.db \
  .venv/bin/python -m mcp_agent_mail.autopilot.daily_sync \
  --dry-run \
  --task-limit 10
```

Output includes the run ID, per-agent bead balances, backlog excerpts, and the generated thread identifiers. Logs are written under `logs/autopilot/daily_sync_<run_id>.json`.

### Live Dispatch

1. Ensure the MCP HTTP server is running (`scripts/run_server_with_token.sh`).
2. Export a bearer token or pass it explicitly:
   ```bash
   HTTP_BEARER_TOKEN=$(cat .bearer-token)
   .venv/bin/python -m mcp_agent_mail.autopilot.daily_sync \
     --bearer-token "$HTTP_BEARER_TOKEN" \
     --task-limit 25
   ```
3. Inspect inboxes at `http://127.0.0.1:8765/mail` or pull the resource endpoints from agents to confirm delivery.

### Scheduling Guidance

- **Cron example (local)**
  ```cron
  0 4 * * * cd /path/to/mcp_agent_mail && \
    HTTP_BEARER_TOKEN=$(cat .bearer-token) \
    .venv/bin/python -m mcp_agent_mail.autopilot.daily_sync --task-limit 25 >> logs/autopilot/cron.log 2>&1
  ```
- **Workflow runners**: when deploying to an orchestrator (Airflow, Temporal, GitHub Actions), call the module with the appropriate environment variables and capture the emitted JSON for auditing.

## Beads Integration Notes

- Priorities `3`, `2`, `1` map directly to bead values, allowing execs to adjust incentives via `BEAD_TXN` markers in their replies.
- Tasks with `assignee` in Beads appear under “Assigned Tasks”; unassigned issues fall into “Backlog Opportunities.”
- To test against production beads without mutating data, run in `--dry-run` mode—no write operations occur.

## Troubleshooting

| Symptom | Likely Cause | Resolution |
| --- | --- | --- |
| `Agents not registered for project …` | Executive name missing in `agents` table | Register the agent via MCP (`register_agent`) or adjust the `--agents` list |
| `The asyncio extension requires an async driver` | `DATABASE_URL` uses sync driver (`psycopg2`, plain `sqlite`) | The tool now auto-normalizes, but double-check environment overrides or set an explicit async URL |
| `nodename nor servname provided` | Dispatching to a remote Postgres host without DNS access | Verify network reachability or fall back to SQLite during local testing |
| No tasks appear | Neither SQLModel tables nor Beads database contain open items | Populate tasks in Beads (`bd ready`) or insert into `tasks` table |

## Roadmap Candidates

1. **Scheduler shim**: wrap the CLI in a reusable `scripts/autopilot_exec_sync.sh` with health checks and exponential backoff.
2. **Bidirectional bead ledger**: parse `BEAD_TXN` sections from responses and persist them via `ledger.record_bead_transaction()` automatically.
3. **Dynamic agent rosters**: allow a configuration file (`autopilot.toml`) to define per-run agent lists, priorities, and templated messaging.
4. **Inbox follow-up collector**: add a companion command that harvests thread responses after a delay and produces a consolidated status report.

Contributions and enhancement ideas should align with this document; update it whenever behavior or configuration expectations change.
