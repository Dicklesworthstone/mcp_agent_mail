# Operations Alignment Checklist

This checklist translates the guidance from `GUIDE_TO_OPTIMAL_MCP_SERVER_DESIGN.md`
into concrete, repeatable actions for the ops and client-integration teams.

## 1. Capability & Macro Adoption Review

1. **Read:** `docs/GUIDE_TO_OPTIMAL_MCP_SERVER_DESIGN.md` (sections 3–7). Highlight
   the clusters/macros relevant to your deployments.
2. **Decide roles:** Use `deploy/capabilities/agent_capabilities.example.yaml`
   as a template to assign capability tags to every automated agent.
3. **Update clients:** Ensure each MCP client sends
   `metadata={"allowed_capabilities": [...tags...]}` when establishing a
   session (see `examples/client_bootstrap.py`). Agents without the correct tags
   will now receive deterministic `CAPABILITY_DENIED` errors instead of failing
   silently.
4. **Macro defaults:** Configure small-model workers to prefer macro tools
   (`macro_start_session`, `macro_prepare_thread`, `macro_file_reservation_cycle`)
   before the atomic verbs. This mirrors the
   “workflow mode” recommendations that boosted success rates in field studies.citeturn0academia12
5. **Security testing:** Add the MSB prompt-attack suite to your CI/CD gate (see
   `docs/GUIDE_TO_OPTIMAL_MCP_SERVER_DESIGN.md`, section 7). Record Net Resilient
   Performance (NRP) deltas on every release.citeturn0academia16

## 2. Capability Tag Rollout

1. **Inventory agents:** Fill out the table in `deploy/capabilities/agent_capabilities.example.yaml`
   with real agent identities and their required tags.
2. **Share with clients:** Distribute the completed YAML (or equivalent config)
   to all orchestrators so they can inject the tags automatically when spawning agents.
3. **Backstop:** Enable `TOOLS_LOG_ENABLED=true` temporarily to confirm that
   capability denials are behaving as expected during rollout.

## 3. Observability Pipeline

1. **Configuration:** Set `TOOL_METRICS_EMIT_ENABLED=true` and
   `TOOL_METRICS_EMIT_INTERVAL_SECONDS=<interval>` in your environment (production
   template defaults to 120 s).
2. **Log shipping:** Follow `docs/observability.md` to push JSON logs into Loki
   (or your chosen sink).
3. **Prometheus alerts:** Import `deploy/observability/prometheus_rules.sample.yml`
   and adjust thresholds as needed; this rule fires when any tool’s error ratio
   exceeds 5 % over five minutes.
4. **Dashboards:** Build panels for `calls`, `errors`, and `capabilities` by
   reusing the metrics labels (`cluster`, `capabilities`). Example Grafana JSON
   snippets are provided in `docs/observability.md`.

Check off every item before shipping the next release.

