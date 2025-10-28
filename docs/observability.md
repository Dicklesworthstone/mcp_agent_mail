# Observability Cookbook

This note collects the minimum wiring required to turn the MCP tool metrics into
actionable signals. It assumes you have already set up `structlog` (the default
logging backend for `mcp-agent-mail`) to emit JSON to stdout.

## 1. Server settings

```
TOOL_METRICS_EMIT_ENABLED=true
TOOL_METRICS_EMIT_INTERVAL_SECONDS=120
```

With these flags enabled the HTTP service spawns a background task that logs a
`tool_metrics_snapshot` event every two minutes. Each entry is the same payload
you would get from `resource://tooling/metrics`:

```json
{
  "event": "tool_metrics_snapshot",
  "tools": [
    {"name": "send_message", "cluster": "messaging", "capabilities": ["messaging", "write"], "calls": 42, "errors": 1},
    {"name": "file_reservation_paths", "cluster": "file_reservations", "capabilities": ["file_reservations", "repository"], "calls": 11, "errors": 0}
  ]
}
```

## 2. Log pipeline recipe (Loki / Prometheus)

1. Ship stdout to Loki (or any structured log store).
2. Extract the `tools[]` array with a pipeline stage (for Loki: `json` stage).
3. Flatten per-tool metrics:

```
{app="mcp-agent-mail"}
| json
| line_format "{{ .tool_name }} {{ .calls }} {{ .errors }}"
```

4. Feed into Prometheus via the Loki recording rule:

```
record: mcp_tool_error_ratio
expr: sum by (tool) (rate(tool_errors[5m])) / sum by (tool) (rate(tool_calls[5m]))
```

5. Alert when `mcp_tool_error_ratio > 0.05` for 5 minutes.

## 3. Dashboards

Suggested panels:

- **Top error sources**: `topk(5, mcp_tool_error_ratio)`
- **Calls by cluster**: sum the `tool_calls` metric using the `cluster` label (provided by the snapshot).
- **Macro adoption**: track `tool_calls{tool=~"macro_.*"}` so you know when to invest in additional macros.

## 4. Bonus: recent tool usage resource

When building interactive UIs, poll `resource://tooling/recent?agent=<name>&project=<slug>` to surface the last few successful invocations in the UI. This makes it easy to link “what just worked” with a macro or capability tip.

