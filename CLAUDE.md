# Claude Code Agent Notes

Claude Code (or Claude Desktop) must assume the MCP Agent Mail server is already running in the background before it connects. Always start/refresh the server with a background `bash -lc` call so you capture the PID and tee logs to a safe location:

```bash
bash -lc "cd /Users/jleechan/mcp_agent_mail && ./scripts/run_server_with_token.sh >/tmp/mcp_agent_mail_server.log 2>&1 & echo \$!"
```

- Keep the printed PID handy; stop the service with `kill <PID>` when you are done.
- Tail `/tmp/mcp_agent_mail_server.log` if Claude reports connection errors.
- Launch Claude Code/Claude Desktop **after** the command above succeeds so it can reuse the existing HTTP MCP endpoint at `http://127.0.0.1:8765/mcp/`.

With the server running, Claude agents can call `ensure_project`, `register_agent`, `fetch_inbox`, and the other MCP tools without additional setup.
