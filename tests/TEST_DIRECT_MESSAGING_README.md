# Direct Messaging Test Suite

This directory contains comprehensive tests for the direct messaging functionality after removing the contact request requirement.

## Test Files

### test_direct_messaging.py
Tests for basic direct messaging functionality within a single project:
- **test_direct_message_without_contact_approval**: Verifies messages can be sent without any prior approval
- **test_bidirectional_messaging_without_approval**: Tests that agents can message each other in both directions
- **test_multiple_recipients_without_approval**: Verifies broadcast messaging to multiple recipients
- **test_first_contact_no_approval_needed**: Confirms the first contact between agents requires no setup
- **test_message_with_cc_and_bcc_no_approval**: Tests CC/BCC functionality without approval
- **test_rapid_succession_messages_no_throttling**: Verifies no throttling for rapid messages
- **test_message_to_nonexistent_agent_fails_gracefully**: Documents behavior with non-existent agents
- **test_self_messaging_works**: Confirms agents can send messages to themselves

### test_cross_project_direct_messaging.py
Tests for cross-project messaging without contact approval:
- **test_cross_project_direct_message**: Verifies agents in different projects can message directly
- **test_cross_project_bidirectional_messaging**: Tests bidirectional cross-project messaging
- **test_cross_project_multiple_recipients**: Verifies messaging to recipients in different projects
- **test_cross_project_first_contact_no_setup**: Confirms first cross-project contact needs no setup
- **test_cross_project_thread_continuation**: Tests thread continuation across projects
- **test_cross_project_rapid_succession**: Verifies rapid cross-project messaging

### test_contact_deprecation.py
Tests verifying the contact requirement has been properly deprecated:
- **test_contact_tools_are_deprecated**: Confirms deprecated tools are not available
- **test_messaging_works_regardless_of_contact_policy_setting**: Verifies messages work even if policy fields exist
- **test_no_contact_required_error_thrown**: Confirms CONTACT_REQUIRED error is never thrown
- **test_no_contact_blocked_error_thrown**: Confirms CONTACT_BLOCKED error is never thrown
- **test_agent_link_table_not_consulted**: Verifies messaging works without checking agent_links
- **test_auto_contact_if_blocked_parameter_ignored**: Confirms deprecated parameter is ignored
- **test_no_contact_ttl_restrictions**: Verifies no TTL restrictions for messaging
- **test_no_prior_reservation_overlap_required**: Confirms no file reservation overlap needed
- **test_no_thread_participation_required**: Verifies messaging works without thread participation

## TDD Approach

These tests were written following Test-Driven Development principles:

1. **Tests Written First**: All tests were written before verifying the implementation
2. **Behavior-Driven**: Tests describe the expected behavior of direct messaging
3. **Comprehensive Coverage**: Tests cover:
   - Single project messaging
   - Cross-project messaging
   - Edge cases (self-messaging, rapid messages, etc.)
   - Deprecation verification
   - Error conditions

## Running the Tests

```bash
# Run all direct messaging tests
pytest tests/test_direct_messaging.py -v

# Run cross-project tests
pytest tests/test_cross_project_direct_messaging.py -v

# Run deprecation tests
pytest tests/test_contact_deprecation.py -v

# Run all new tests
pytest tests/test_direct_messaging.py tests/test_cross_project_direct_messaging.py tests/test_contact_deprecation.py -v
```

## Implementation Notes

The implementation removes:
- Contact validation logic in `send_message()` (app.py lines 2738-2987)
- Contact-related MCP tools (request_contact, respond_contact, list_contacts, set_contact_policy, macro_contact_handshake)
- All contact policy enforcement

Messages can now be sent directly:
- No contact approval required
- No policy checks
- No AgentLink table consultation
- No TTL restrictions
- No file reservation overlap requirements
- No thread participation requirements

## Expected Behavior

All tests should pass with the current implementation where:
1. Contact validation has been removed from send_message()
2. Contact-related tools are deprecated (not exposed via MCP)
3. Messages flow freely between agents regardless of:
   - Whether they've communicated before
   - Whether they're in the same project
   - Any legacy contact_policy settings in the database

## Legacy Tests

The following existing test files may need updating as they test the old contact approval logic:
- `tests/test_contacts.py` - Tests old contact policy enforcement
- `tests/test_contact_policy.py` - Tests contact policies
- `tests/test_contact_and_routing.py` - Tests contact-based routing

These tests can be:
1. Updated to reflect new direct messaging behavior
2. Removed if they only test deprecated functionality
3. Kept for backward compatibility verification if needed
