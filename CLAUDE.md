# MCP Agent Mail — CLAUDE.md

## Repository & Fork Structure

This repo is a **fork** of `Dicklesworthstone/mcp_agent_mail` (upstream).

| Remote | Repo | Role |
|--------|------|------|
| `origin` | `leegonzales/mcp_agent_mail` | **Our fork** — all PRs and merges happen here |
| `upstream` | `Dicklesworthstone/mcp_agent_mail` | Upstream source — pull updates from, never push to |

**Critical rule:** Never create PRs against upstream (`Dicklesworthstone`). We do not own that repo. All PRs target `origin/main` (our fork).

To sync from upstream:
```bash
git fetch upstream
git merge upstream/main
```

## Ecosystem Context

See `~/.claude/ecosystem-map.md` for how this repo connects to servitor and the broader fleet.
