# Testing Infrastructure and Coverage Tasks

## Testing Infrastructure Foundation
priority: 1
labels: testing, infrastructure, foundation

Set up comprehensive testing infrastructure with detailed logging, fixtures, and helpers for real component testing (no mocks). This is the foundation for all other test tasks.

### Deliverables
- Enhanced conftest.py with rich logging fixtures
- Test helper utilities for common operations
- Standardized assertion helpers with detailed output
- Test database seeding utilities
- Git archive test fixtures

---

## Unit Tests: models.py
priority: 2
labels: testing, unit, models
deps: Testing Infrastructure Foundation

Complete unit test coverage for models.py (currently 99% - maintain and extend).

### Test Areas
- All SQLModel field validations
- Default factory functions (_utcnow_naive)
- Unique constraints behavior
- Foreign key relationships
- JSON field serialization (Message.attachments)

### Notes
- Use real SQLite database, no mocks
- Test edge cases for all field types

---

## Unit Tests: config.py
priority: 2
labels: testing, unit, config
deps: Testing Infrastructure Foundation

Complete unit test coverage for config.py (currently 77%).

### Test Areas
- Settings class initialization
- Environment variable loading via python-decouple
- Default value fallbacks
- Settings caching and clear_settings_cache()
- All configuration options validation
- Edge cases: missing env vars, invalid values

### Missing Coverage (lines 20-22, 179-285, 340-342)
- JWT/JWKS configuration
- Redis configuration
- HTTP configuration options

---

## Unit Tests: db.py - Session Management
priority: 2
labels: testing, unit, database
deps: Testing Infrastructure Foundation

Unit tests for database session management (currently 26%).

### Test Areas
- get_session() context manager behavior
- ensure_schema() idempotency
- reset_database_state() cleanup
- Async session lifecycle
- Connection pooling behavior
- Transaction commit/rollback scenarios

### Missing Coverage (lines 44-290)
- Engine creation with various DATABASE_URL formats
- Session scoping for concurrent access
- Migration helpers

---

## Unit Tests: db.py - Migrations
priority: 3
labels: testing, unit, database, migrations
deps: Unit Tests: db.py - Session Management

Unit tests for database migration functionality.

### Test Areas
- Schema creation from scratch
- Schema updates (adding columns)
- Index creation
- Constraint validation
- Backward compatibility checks

### Notes
- Test with real SQLite database
- Verify all tables created correctly

---

## Unit Tests: app.py - Core Helpers
priority: 2
labels: testing, unit, app, helpers
deps: Testing Infrastructure Foundation

Unit tests for app.py core helper functions (currently 4% overall).

### Test Areas
- _naive_utc() datetime conversion
- _ensure_utc() timezone handling
- _iso() ISO format conversion
- _max_datetime() comparisons
- _safe_component() path sanitization
- _canonical_project_pair() ordering
- validate_agent_name_format() validation

### Notes
- Test edge cases: None values, timezone-aware vs naive
- Test invalid inputs and error handling

---

## Unit Tests: app.py - Project Operations
priority: 2
labels: testing, unit, app, projects
deps: Unit Tests: app.py - Core Helpers, Unit Tests: db.py - Session Management

Unit tests for project-related operations in app.py.

### Test Areas
- _get_project_by_identifier() lookup
- _ensure_project() creation and idempotency
- Project slug generation
- Human key normalization
- Project sibling suggestions
- _evaluate_project_siblings()
- update_project_sibling_status()

### Notes
- Test with real database
- Verify Git archive creation

---

## Unit Tests: app.py - Agent Operations
priority: 2
labels: testing, unit, app, agents
deps: Unit Tests: app.py - Project Operations

Unit tests for agent-related operations in app.py.

### Test Areas
- _get_or_create_agent() creation and update
- _get_agent() lookup with suggestions
- _find_similar_agents() fuzzy matching
- _detect_agent_name_mistake() validation
- Agent name validation (adjective+noun format)
- last_active_ts updates
- attachments_policy handling
- contact_policy handling

### Notes
- Test case-insensitive matching
- Test placeholder detection

---

## Unit Tests: app.py - Messaging
priority: 2
labels: testing, unit, app, messaging
deps: Unit Tests: app.py - Agent Operations

Unit tests for messaging operations in app.py.

### Test Areas
- _create_message() with all parameters
- _deliver_message() routing logic
- MessageRecipient creation (to/cc/bcc)
- Thread ID handling
- Importance levels
- ack_required flag
- Attachment handling
- _update_recipient_timestamp() for read/ack

### Notes
- Test message delivery to multiple recipients
- Test self-messages
- Verify inbox/outbox artifacts created

---

## Unit Tests: app.py - File Reservations
priority: 2
labels: testing, unit, app, file-reservations
deps: Unit Tests: app.py - Agent Operations

Unit tests for file reservation operations in app.py.

### Test Areas
- _create_file_reservation() with TTL
- _expire_stale_file_reservations() cleanup
- _file_reservations_conflict() detection
- _collect_file_reservation_statuses() activity heuristics
- Glob pattern matching
- Exclusive vs shared reservations
- Stale detection (agent activity, mail, filesystem, git)

### Notes
- Test pattern overlap detection
- Test TTL expiration
- Test force release

---

## Unit Tests: app.py - Contact Management
priority: 2
labels: testing, unit, app, contacts
deps: Unit Tests: app.py - Agent Operations

Unit tests for contact/AgentLink operations in app.py.

### Test Areas
- request_contact() link creation
- respond_contact() approval/denial
- list_contacts() retrieval
- set_contact_policy() updates
- Contact policy enforcement (open/auto/contacts_only/block_all)
- Cross-project contacts
- TTL expiration

### Notes
- Test bidirectional links
- Test auto-approval scenarios

---

## Unit Tests: app.py - Macros
priority: 2
labels: testing, unit, app, macros
deps: Unit Tests: app.py - Messaging, Unit Tests: app.py - File Reservations, Unit Tests: app.py - Contact Management

Unit tests for macro operations in app.py.

### Test Areas
- macro_start_session() full flow
- macro_prepare_thread() context gathering
- macro_file_reservation_cycle() lease management
- macro_contact_handshake() approval flow

### Notes
- Macros combine multiple operations
- Test error handling when sub-operations fail
- Test with real database and Git

---

## Unit Tests: app.py - MCP Resources
priority: 2
labels: testing, unit, app, resources
deps: Unit Tests: app.py - Messaging

Unit tests for MCP resource handlers in app.py.

### Test Areas
- resource://project/{slug}
- resource://agents/{project}
- resource://inbox/{agent}
- resource://outbox/{agent}
- resource://thread/{id}
- resource://identity/{path} (worktree mode)
- resource://product/{key}

### Notes
- Test query parameters (limit, include_bodies)
- Test error responses for missing resources

---

## Unit Tests: app.py - MCP Tools
priority: 1
labels: testing, unit, app, tools
deps: Unit Tests: app.py - Macros, Unit Tests: app.py - MCP Resources

Unit tests for all MCP tool handlers in app.py.

### Test Areas
- health_check
- ensure_project
- register_agent / create_agent_identity
- whois
- send_message / reply_message
- fetch_inbox
- mark_message_read / acknowledge_message
- search_messages
- summarize_thread
- file_reservation_paths / release / renew / force_release
- request_contact / respond_contact / list_contacts / set_contact_policy
- All macro tools
- Build slot tools (when enabled)
- Product bus tools

### Notes
- Test via FastMCP Client for realistic MCP protocol
- Test error responses (ToolError)
- Test input validation

---

## Unit Tests: storage.py - Archive Operations
priority: 2
labels: testing, unit, storage, git
deps: Testing Infrastructure Foundation

Unit tests for Git archive operations in storage.py (currently 9%).

### Test Areas
- ensure_archive() initialization
- ProjectArchive class operations
- write_agent_profile() persistence
- write_message_artifacts() inbox/outbox
- write_file_reservation_records()
- Git commit operations
- _archive_write_lock() concurrency

### Notes
- Test with real Git repos
- Test concurrent access patterns
- Verify file structure matches spec

---

## Unit Tests: storage.py - Inbox/Outbox
priority: 2
labels: testing, unit, storage, mailbox
deps: Unit Tests: storage.py - Archive Operations

Unit tests for mailbox operations in storage.py.

### Test Areas
- Inbox file structure (agents/{name}/inbox/YYYY/MM/*.md)
- Outbox file structure (agents/{name}/outbox/YYYY/MM/*.md)
- Message file naming conventions
- Markdown content generation
- Attachment embedding
- Thread ID tracking

### Notes
- Verify Git commits have correct author/message
- Test file path sanitization

---

## Unit Tests: cli.py - Core Commands
priority: 2
labels: testing, unit, cli
deps: Testing Infrastructure Foundation

Unit tests for CLI core commands (currently 8%).

### Test Areas
- mail status command
- mail inbox command
- mail send command
- mail ack command
- mail search command

### Notes
- Use typer.testing.CliRunner
- Test JSON output format
- Test rich console output

---

## Unit Tests: cli.py - Guard Commands
priority: 2
labels: testing, unit, cli, guards
deps: Unit Tests: cli.py - Core Commands

Unit tests for CLI guard commands.

### Test Areas
- guard status command
- guard install command
- guard uninstall command
- Pre-commit hook behavior
- Pre-push hook behavior

### Notes
- Test hook chain composition
- Test bypass modes

---

## Unit Tests: cli.py - Archive Commands
priority: 2
labels: testing, unit, cli, archive
deps: Unit Tests: cli.py - Core Commands

Unit tests for CLI archive commands.

### Test Areas
- archive save command
- archive list command
- archive restore command
- archive export command

### Notes
- Test ZIP file creation/extraction
- Test database backup/restore

---

## Unit Tests: http.py - Server Setup
priority: 2
labels: testing, unit, http
deps: Testing Infrastructure Foundation

Unit tests for HTTP server setup in http.py (currently 4%).

### Test Areas
- create_http_app() initialization
- Uvicorn worker configuration
- CORS setup
- SSE transport setup
- Health endpoint

### Notes
- Test with real HTTP client (httpx)
- No mocked responses

---

## Unit Tests: http.py - Authentication
priority: 2
labels: testing, unit, http, auth
deps: Unit Tests: http.py - Server Setup

Unit tests for HTTP authentication in http.py.

### Test Areas
- Static bearer token auth
- JWT validation
- JWKS key fetching
- Token expiration
- Invalid token handling

### Notes
- Test both auth modes
- Test error responses

---

## Unit Tests: http.py - Rate Limiting
priority: 3
labels: testing, unit, http, rate-limit
deps: Unit Tests: http.py - Server Setup

Unit tests for HTTP rate limiting in http.py.

### Test Areas
- In-memory rate limiter
- Redis-backed rate limiter
- Rate limit headers
- 429 responses
- Per-client tracking

### Notes
- Test burst handling
- Test rate limit recovery

---

## Unit Tests: guard.py - Pre-commit
priority: 2
labels: testing, unit, guards
deps: Testing Infrastructure Foundation

Unit tests for pre-commit guard logic in guard.py (currently 8%).

### Test Areas
- Staged file detection
- File reservation conflict checking
- Agent identification
- Bypass mode (AGENT_MAIL_BYPASS)
- Warning mode (AGENT_MAIL_GUARD_MODE=warn)

### Notes
- Test with real Git repos
- Test rename detection (-M flag)

---

## Unit Tests: guard.py - Pre-push
priority: 2
labels: testing, unit, guards
deps: Unit Tests: guard.py - Pre-commit

Unit tests for pre-push guard logic in guard.py.

### Test Areas
- Commit enumeration (git rev-list)
- Tree diff detection (git diff-tree)
- Remote branch tracking
- Conflict resolution

### Notes
- Test with multiple commits
- Test force push scenarios

---

## Unit Tests: share.py - Archive Save
priority: 2
labels: testing, unit, share
deps: Testing Infrastructure Foundation

Unit tests for archive save functionality in share.py (currently 12%).

### Test Areas
- ZIP archive creation
- Database backup inclusion
- Storage root backup
- Label/metadata handling
- Incremental vs full backup

### Notes
- Test large archive handling
- Verify ZIP structure

---

## Unit Tests: share.py - Archive Restore
priority: 2
labels: testing, unit, share
deps: Unit Tests: share.py - Archive Save

Unit tests for archive restore functionality in share.py.

### Test Areas
- ZIP extraction
- Database restoration
- Storage root restoration
- Conflict handling (--force)
- Integrity validation

### Notes
- Test corrupted archive handling
- Test partial restore scenarios

---

## Unit Tests: llm.py - Provider Integration
priority: 3
labels: testing, unit, llm
deps: Testing Infrastructure Foundation

Unit tests for LLM provider integration in llm.py (currently 17%).

### Test Areas
- LiteLLM initialization
- Model selection
- Token counting (tiktoken)
- Response parsing
- Error handling (rate limits, timeouts)

### Notes
- May need real API calls or recorded responses
- Test fallback behavior

---

## Integration Tests: Full Messaging Flow
priority: 1
labels: testing, integration, e2e, messaging
deps: Unit Tests: app.py - MCP Tools

End-to-end integration test for complete messaging workflow.

### Test Scenario
1. Create project
2. Register two agents
3. Agent A sends message to Agent B
4. Agent B fetches inbox, sees message
5. Agent B marks as read
6. Agent B acknowledges
7. Verify all state in database and Git archive

### Logging
- Rich console output at each step
- Timing information
- Database state dumps

---

## Integration Tests: File Reservation Conflicts
priority: 1
labels: testing, integration, e2e, file-reservations
deps: Unit Tests: app.py - MCP Tools

End-to-end integration test for file reservation conflict handling.

### Test Scenario
1. Agent A reserves src/**/*.py exclusively
2. Agent B attempts to reserve src/main.py
3. Verify conflict returned
4. Agent A releases reservation
5. Agent B successfully reserves
6. Test stale detection and force release

### Logging
- Detailed conflict information
- Timing of TTL expiration

---

## Integration Tests: Contact Management Flow
priority: 2
labels: testing, integration, e2e, contacts
deps: Unit Tests: app.py - MCP Tools

End-to-end integration test for contact request/approval flow.

### Test Scenario
1. Agent A requests contact with Agent B
2. Agent B fetches inbox, sees contact request
3. Agent B approves contact
4. Agent A can now message Agent B
5. Test cross-project contacts
6. Test contact policy enforcement

### Logging
- Contact link state transitions
- Policy evaluation details

---

## Integration Tests: Thread Conversations
priority: 2
labels: testing, integration, e2e, threads
deps: Integration Tests: Full Messaging Flow

End-to-end integration test for threaded conversations.

### Test Scenario
1. Agent A starts thread with subject [bd-123]
2. Agent B replies in thread
3. Agent A replies back
4. Fetch thread, verify all messages
5. Summarize thread
6. Search within thread

### Logging
- Thread ID tracking
- Message ordering

---

## Integration Tests: Guard Pre-commit
priority: 2
labels: testing, integration, e2e, guards
deps: Unit Tests: guard.py - Pre-push

End-to-end integration test for pre-commit guard.

### Test Scenario
1. Set up Git repo with guard installed
2. Agent A reserves file.py
3. Agent B (different identity) modifies file.py
4. Agent B attempts commit
5. Verify guard blocks commit
6. Test warning mode
7. Test bypass mode

### Logging
- Guard execution trace
- Conflict details

---

## Integration Tests: Archive Save/Restore
priority: 2
labels: testing, integration, e2e, archive
deps: Unit Tests: cli.py - Archive Commands

End-to-end integration test for disaster recovery.

### Test Scenario
1. Create project with messages, agents, reservations
2. Save archive with label
3. Delete database and storage
4. Restore from archive
5. Verify all data recovered
6. Verify message timestamps preserved

### Logging
- Archive contents listing
- Restoration progress

---

## Integration Tests: HTTP Transport
priority: 2
labels: testing, integration, e2e, http
deps: Unit Tests: http.py - Rate Limiting

End-to-end integration test for HTTP/SSE transport.

### Test Scenario
1. Start HTTP server
2. Connect via SSE
3. Call tools through HTTP
4. Verify responses
5. Test authentication
6. Test rate limiting

### Logging
- Request/response traces
- SSE event stream

---

## Integration Tests: Concurrent Access
priority: 2
labels: testing, integration, e2e, concurrency
deps: Integration Tests: Full Messaging Flow, Integration Tests: File Reservation Conflicts

End-to-end integration test for concurrent operations.

### Test Scenario
1. Multiple agents operating simultaneously
2. Concurrent message sends
3. Concurrent file reservations
4. Concurrent inbox fetches
5. Verify no data corruption
6. Verify proper locking

### Logging
- Timing of concurrent operations
- Lock acquisition/release

---

## E2E Test Script: Multi-Agent Workflow
priority: 1
labels: testing, e2e, script, multi-agent
deps: Integration Tests: Full Messaging Flow, Integration Tests: File Reservation Conflicts, Integration Tests: Thread Conversations

Comprehensive E2E test script simulating realistic multi-agent workflow.

### Scenario
Simulate a development team with 3 agents working on a codebase:
1. BlueLake: Backend developer
2. GreenMountain: Frontend developer
3. RedStone: Code reviewer

### Workflow
1. BlueLake announces work on API endpoint
2. BlueLake reserves backend/**
3. GreenMountain announces work on UI
4. GreenMountain reserves frontend/**
5. BlueLake sends PR notification
6. RedStone reviews and comments
7. Back-and-forth discussion in thread
8. BlueLake releases reservation
9. Full audit trail verification

### Logging Requirements
- Rich console panels for each step
- Timing breakdown
- State snapshots
- Git archive diff at each stage

---

## E2E Test Script: Disaster Recovery
priority: 2
labels: testing, e2e, script, disaster-recovery
deps: Integration Tests: Archive Save/Restore

Comprehensive E2E test script for disaster recovery scenarios.

### Scenario
Test full backup/restore cycle with data verification:
1. Create complex state (multiple projects, agents, messages, threads)
2. Save labeled archive
3. Corrupt/delete state
4. Restore from archive
5. Verify byte-perfect restoration
6. Continue operations post-restore

### Logging Requirements
- Pre/post state comparison
- Archive manifest
- Restoration progress bar
- Data integrity checksums

---

## E2E Test Script: Performance Under Load
priority: 3
labels: testing, e2e, script, performance
deps: Integration Tests: Concurrent Access

Comprehensive E2E test script for performance benchmarking.

### Scenario
Test system behavior under load:
1. Create 100 agents
2. Send 1000 messages
3. Create 500 file reservations
4. Concurrent operations (10 parallel)
5. Measure latencies
6. Verify no degradation

### Logging Requirements
- Latency histograms
- Throughput metrics
- Memory usage
- Database query timing

---

## Test Coverage: Achieve 50% Overall
priority: 1
labels: testing, milestone, coverage
deps: Unit Tests: app.py - MCP Tools, Unit Tests: cli.py - Core Commands, Unit Tests: http.py - Server Setup, Unit Tests: guard.py - Pre-commit, Unit Tests: share.py - Archive Save

Milestone: Achieve 50% overall test coverage.

### Current State
- Overall: 9%
- app.py: 4%
- cli.py: 8%
- http.py: 4%

### Target
- Overall: 50%
- All modules: minimum 30%

### Verification
Run: pytest --cov=src/mcp_agent_mail --cov-fail-under=50

---

## Test Coverage: Achieve 80% Overall
priority: 2
labels: testing, milestone, coverage
deps: Test Coverage: Achieve 50% Overall

Milestone: Achieve 80% overall test coverage.

### Target
- Overall: 80%
- All modules: minimum 70%
- Critical paths: 95%+

### Verification
Run: pytest --cov=src/mcp_agent_mail --cov-fail-under=80

---

## Test Coverage: Achieve 95% Overall
priority: 3
labels: testing, milestone, coverage
deps: Test Coverage: Achieve 80% Overall

Milestone: Achieve 95% overall test coverage.

### Target
- Overall: 95%
- All modules: minimum 90%
- No untested critical paths

### Verification
Run: pytest --cov=src/mcp_agent_mail --cov-fail-under=95
