# MCP Agent Mail Test Suite - Comprehensive Plan

## P0 - Critical Regression Tests

These tests prevent recurrence of known bugs and must pass before any release.

---

## Regression: Datetime Naive/Aware Handling
priority: 0
labels: regression, critical, datetime

**Background:** We fixed a bug where SQLite (which stores naive datetimes) was compared against timezone-aware Python datetimes, causing `TypeError: can't compare offset-naive and offset-aware datetimes`.

### Test Cases
1. `_naive_utc()` returns naive datetime when given None
2. `_naive_utc()` strips timezone from aware datetime
3. `_utcnow_naive()` model factory returns naive datetime
4. All model default_factory fields produce naive datetimes
5. File reservation expiration comparison works (the original failure point)
6. AgentLink timestamp comparisons work
7. MessageRecipient timestamp updates work
8. ProjectSiblingSuggestion timestamp comparisons work

### Files to Test
- `src/mcp_agent_mail/models.py`: `_utcnow_naive()`
- `src/mcp_agent_mail/app.py`: `_naive_utc()`, all datetime assignments

### Success Criteria
- All datetime comparisons in WHERE clauses succeed
- All model field updates with timestamps succeed
- No `TypeError` for offset-naive/aware comparisons

---

## Regression: Session Context Management
priority: 0
labels: regression, critical, database

**Background:** We fixed a bug where `await session.commit()` was outside the `async with get_session()` block in `force_release_file_reservation`, causing commits to silently fail.

### Test Cases
1. `force_release_file_reservation` actually persists the release
2. All database writes in file reservation functions persist correctly
3. All database writes in contact functions persist correctly
4. Transaction rollback on error works correctly

### Files to Test
- `src/mcp_agent_mail/app.py`: All `async with get_session()` blocks

### Success Criteria
- Database state changes are actually persisted
- Verify via direct SQL query after each operation

---

## Regression: Agent Name Validation
priority: 0
labels: regression, critical, validation

**Background:** Agent names must follow adjective+noun format. Invalid names should be rejected.

### Test Cases
1. Valid names accepted: "BlueLake", "GreenMountain", "RedStone"
2. Invalid names rejected: "BackendHarmonizer", "DatabaseMigrator", "agent1"
3. Placeholder detection works: "YourAgentName", "AgentName", etc.
4. Case-insensitive uniqueness enforced

### Success Criteria
- Clear error messages for invalid names
- Suggestions for valid alternatives

---

## P1 - Core Functionality Tests

These test the primary user-facing functionality.

---

## Core: Message Delivery Flow
priority: 1
labels: core, messaging

Complete test of message delivery from send to acknowledgment.

### Test Cases
1. Send message to single recipient (to)
2. Send message with cc recipients
3. Send message with bcc recipients (not visible to others)
4. Send message to self
5. Send message with thread_id
6. Reply to message (preserves thread)
7. Fetch inbox shows unread messages
8. Mark message as read
9. Acknowledge message (ack_required=true)
10. Search messages by subject/body
11. Summarize thread extracts key points

### Verification
- Database records created correctly
- Git archive artifacts created (inbox/outbox .md files)
- Timestamps are naive UTC

---

## Core: File Reservation Lifecycle
priority: 1
labels: core, file-reservations

Complete test of file reservation from claim to release.

### Test Cases
1. Create exclusive reservation
2. Create shared reservation
3. Conflict detection: exclusive vs exclusive
4. Conflict detection: exclusive vs shared
5. No conflict: shared vs shared
6. Pattern overlap detection (src/** vs src/main.py)
7. TTL expiration releases reservation
8. Manual release before TTL
9. Stale detection (agent inactive)
10. Force release with notification
11. Renew reservation extends TTL

### Verification
- Git archive artifacts created (file_reservations/*.json)
- Conflicts returned with holder information
- Released reservations have released_ts set

---

## Core: Contact Management Flow
priority: 1
labels: core, contacts

Complete test of contact request/approval workflow.

### Test Cases
1. Request contact from Agent A to Agent B
2. Agent B receives contact request in inbox
3. Agent B approves contact
4. Agent A can now message Agent B
5. Agent B denies contact
6. Denied agent cannot message
7. Contact policy: open (anyone can message)
8. Contact policy: contacts_only (approved only)
9. Contact policy: block_all (nobody)
10. Contact expiration after TTL
11. Cross-project contacts

### Verification
- AgentLink records created with correct status
- Policy enforcement blocks/allows messages

---

## Core: Project and Agent Setup
priority: 1
labels: core, setup

Test project and agent registration flows.

### Test Cases
1. ensure_project creates new project
2. ensure_project is idempotent
3. Project slug generated from human_key
4. register_agent creates new agent
5. register_agent updates existing agent
6. create_agent_identity always creates new
7. Agent profile written to Git archive
8. last_active_ts updated on activity
9. whois returns agent details

### Verification
- Database records correct
- Git archive has agents/{name}/profile.json

---

## P1 - MCP Protocol Tests

Test all MCP tools and resources work correctly.

---

## MCP Tools: Happy Path Coverage
priority: 1
labels: mcp, tools

Test each MCP tool with valid inputs via FastMCP Client.

### Tools to Test
1. health_check - returns status ok
2. ensure_project - creates/returns project
3. register_agent - creates/updates agent
4. create_agent_identity - creates new agent
5. whois - returns agent profile
6. send_message - delivers message
7. reply_message - replies in thread
8. fetch_inbox - returns messages
9. mark_message_read - sets read_ts
10. acknowledge_message - sets ack_ts
11. search_messages - FTS query works
12. summarize_thread - extracts summary
13. file_reservation_paths - creates reservations
14. release_file_reservations - releases
15. renew_file_reservations - extends TTL
16. force_release_file_reservation - force release
17. request_contact - creates link
18. respond_contact - approves/denies
19. list_contacts - returns links
20. set_contact_policy - updates policy
21. All macro_* tools

### Verification
- Each tool returns expected response shape
- Side effects occur (database, Git archive)

---

## MCP Resources: Read Access
priority: 1
labels: mcp, resources

Test all MCP resources return correct data.

### Resources to Test
1. resource://project/{slug} - project details
2. resource://agents/{project} - agent list
3. resource://inbox/{agent}?project= - inbox messages
4. resource://outbox/{agent}?project= - outbox messages
5. resource://thread/{id}?project= - thread messages
6. resource://file-reservations/{project} - active reservations

### Verification
- Correct JSON structure
- Query parameters work (limit, include_bodies)
- Missing resources return appropriate error

---

## P2 - Error Handling Tests

Test that errors are handled gracefully with clear messages.

---

## Errors: Invalid Inputs
priority: 2
labels: errors, validation

Test error handling for invalid inputs.

### Test Cases
1. Invalid project_key format
2. Non-existent project
3. Invalid agent name format
4. Non-existent agent
5. Agent name placeholder detection
6. Empty message body
7. Invalid importance level
8. Invalid contact policy
9. Invalid file reservation pattern
10. TTL below minimum (< 60s)
11. Missing required parameters

### Verification
- Clear error messages returned
- Suggestions provided where applicable
- No stack traces exposed to user

---

## Errors: Database Failures
priority: 2
labels: errors, database

Test graceful handling of database issues.

### Test Cases
1. Database file missing (should auto-create)
2. Schema migration on startup
3. Concurrent write handling
4. Transaction rollback on error
5. Session cleanup on exception

### Verification
- Errors logged appropriately
- No data corruption
- Recovery is automatic where possible

---

## Errors: Git Archive Failures
priority: 2
labels: errors, git

Test graceful handling of Git archive issues.

### Test Cases
1. Archive directory missing (should auto-create)
2. Git repo not initialized (should auto-init)
3. Concurrent archive writes (locking)
4. Invalid file paths sanitized
5. Large attachment handling

### Verification
- Errors logged appropriately
- No partial writes
- Lock released on error

---

## P2 - CLI Integration Tests

Test CLI commands work correctly.

---

## CLI: Mail Commands
priority: 2
labels: cli, integration

Test mail-related CLI commands.

### Commands to Test
1. `mcp-agent-mail mail status <path>` - shows project status
2. `mcp-agent-mail mail inbox <project> <agent>` - shows inbox
3. `mcp-agent-mail mail send` - sends message
4. `mcp-agent-mail mail ack <project> <agent> <msg_id>` - acknowledges
5. `mcp-agent-mail mail search <project> <query>` - searches

### Verification
- Exit codes correct (0 success, non-zero failure)
- JSON output format (--json flag)
- Rich console output (default)

---

## CLI: Guard Commands
priority: 2
labels: cli, guards

Test guard-related CLI commands.

### Commands to Test
1. `mcp-agent-mail guard status <path>` - shows guard status
2. `mcp-agent-mail guard install <project> <path>` - installs hooks
3. `mcp-agent-mail guard uninstall <path>` - removes hooks

### Verification
- Hook files created in .git/hooks/
- Chain runner preserves existing hooks
- Uninstall cleans up properly

---

## CLI: Archive Commands
priority: 2
labels: cli, archive

Test archive backup/restore commands.

### Commands to Test
1. `mcp-agent-mail archive save --label <name>` - creates backup
2. `mcp-agent-mail archive list` - lists backups
3. `mcp-agent-mail archive restore <path>` - restores backup

### Verification
- ZIP file created with correct structure
- Database and storage both backed up
- Restore recovers all data

---

## P2 - HTTP Transport Tests

Test HTTP/SSE transport layer.

---

## HTTP: Server and Transport
priority: 2
labels: http, transport

Test HTTP server functionality.

### Test Cases
1. Server starts on configured port
2. Health endpoint returns 200
3. SSE connection established
4. Tool calls work over HTTP
5. Resource reads work over HTTP
6. CORS headers present

### Verification
- Use httpx client for requests
- Verify SSE event stream format

---

## HTTP: Authentication
priority: 2
labels: http, auth

Test HTTP authentication modes.

### Test Cases
1. Static bearer token accepted
2. Invalid static token rejected (401)
3. JWT token accepted (when configured)
4. Expired JWT rejected
5. Invalid JWT signature rejected
6. Missing auth rejected (when required)

### Verification
- Correct HTTP status codes
- Clear error messages in response

---

## HTTP: Rate Limiting
priority: 2
labels: http, rate-limit

Test rate limiting functionality.

### Test Cases
1. Requests within limit succeed
2. Requests over limit return 429
3. Rate limit headers present
4. Per-client tracking works
5. Redis-backed limiter (when configured)

### Verification
- X-RateLimit-* headers correct
- Retry-After header on 429

---

## P2 - Guard Hook Tests

Test pre-commit and pre-push guards.

---

## Guards: Pre-commit Enforcement
priority: 2
labels: guards, git

Test pre-commit guard blocks conflicting commits.

### Test Cases
1. No reservations - commit succeeds
2. Own reservation - commit succeeds
3. Other agent's exclusive reservation - commit blocked
4. Shared reservation - commit succeeds
5. Warning mode (AGENT_MAIL_GUARD_MODE=warn) - warns but allows
6. Bypass mode (AGENT_MAIL_BYPASS=1) - allows all

### Verification
- Exit code 1 when blocked
- Clear conflict message with holder info
- Suggested resolution steps

---

## Guards: Pre-push Enforcement
priority: 2
labels: guards, git

Test pre-push guard blocks conflicting pushes.

### Test Cases
1. No conflicts in commits - push succeeds
2. Conflict in any commit - push blocked
3. Multiple commits checked
4. Remote branch comparison

### Verification
- All commits enumerated correctly
- Conflicts identified across commit range

---

## P3 - Security Tests

Test security-sensitive areas.

---

## Security: Input Sanitization
priority: 3
labels: security, xss

Test XSS and injection prevention.

### Test Cases
1. HTML in message body sanitized
2. Script tags removed
3. Event handlers removed
4. Markdown rendered safely
5. Attachment metadata sanitized

### Verification
- No executable content in output
- Markdown rendering is safe

---

## Security: Path Traversal
priority: 3
labels: security, paths

Test path traversal prevention.

### Test Cases
1. File reservation pattern: "../../../etc/passwd" rejected
2. Attachment path traversal blocked
3. Archive extraction path validation
4. Agent name with path separators rejected

### Verification
- Paths normalized and validated
- No access outside project scope

---

## P3 - Concurrent Access Tests

Test behavior under concurrent load.

---

## Concurrency: Multiple Agents
priority: 3
labels: concurrency

Test multiple agents operating simultaneously.

### Test Cases
1. 10 agents sending messages concurrently
2. Multiple agents claiming same file (conflict handling)
3. Concurrent inbox fetches
4. Concurrent archive writes (locking)
5. No data corruption under load

### Verification
- All operations complete successfully
- No deadlocks
- Data integrity maintained

---

## P3 - Performance Tests

Test performance characteristics.

---

## Performance: Baseline Benchmarks
priority: 3
labels: performance

Establish performance baselines.

### Benchmarks
1. Message send latency (p50, p95, p99)
2. Inbox fetch latency with 100 messages
3. Search latency with 1000 messages
4. File reservation conflict check with 100 reservations
5. Archive write latency

### Targets
- Message send: < 100ms p95
- Inbox fetch: < 200ms p95
- Search: < 500ms p95

---

## P4 - E2E Scenario Tests

Complete end-to-end scenarios.

---

## E2E: Multi-Agent Development Workflow
priority: 4
labels: e2e, scenario

Simulate realistic multi-agent development.

### Scenario
Three agents collaborate on a feature:
1. BlueLake reserves backend/**
2. GreenMountain reserves frontend/**
3. BlueLake sends "Starting API work" [bd-100]
4. GreenMountain replies "UI ready when you are"
5. BlueLake completes, releases reservation
6. RedStone reviews, sends feedback in thread
7. All acknowledge completion

### Verification
- All messages in correct thread
- Reservations properly released
- Audit trail complete in Git

---

## E2E: Disaster Recovery
priority: 4
labels: e2e, recovery

Test full backup/restore cycle.

### Scenario
1. Create project with messages, agents, reservations
2. Simulate some time passing (messages sent)
3. Save archive with label "pre-disaster"
4. Delete database and storage
5. Restore from archive
6. Verify all data recovered
7. Continue operations normally

### Verification
- Message timestamps preserved
- Thread IDs maintained
- File reservations restored
- Git archive intact

---

## Milestones

---

## Milestone: Critical Path Coverage
priority: 1
labels: milestone
deps: Regression: Datetime Naive/Aware Handling, Regression: Session Context Management, Core: Message Delivery Flow, Core: File Reservation Lifecycle

All P0 and P1 tests passing. Critical functionality verified.

### Criteria
- All regression tests pass
- Core messaging flow tested
- Core file reservation flow tested
- No known data loss scenarios

---

## Milestone: Full Integration Coverage
priority: 2
labels: milestone
deps: Milestone: Critical Path Coverage, CLI: Mail Commands, CLI: Guard Commands, HTTP: Server and Transport

All integration points tested.

### Criteria
- CLI commands tested
- HTTP transport tested
- Guard hooks tested
- Error handling verified

---

## Milestone: Production Ready
priority: 3
labels: milestone
deps: Milestone: Full Integration Coverage, Security: Input Sanitization, Concurrency: Multiple Agents

Ready for production deployment.

### Criteria
- Security tests pass
- Concurrent access safe
- Performance acceptable
- E2E scenarios pass
