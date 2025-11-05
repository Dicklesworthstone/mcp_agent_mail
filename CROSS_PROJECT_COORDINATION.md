# Cross-Project Coordination Patterns

This guide explains how to coordinate agents working across different parts of a codebase (e.g., frontend and backend, multiple microservices, etc.).

## About This Guide

**Example Format**: Code examples use simplified pseudo-JSON for clarity. Your MCP client library handles the actual JSON-RPC protocol - focus on understanding the tool calls and architectural patterns shown here.

## Current State: Direct Cross-Project Messaging

Projects are independent namespaces, but agents can now **send messages directly across projects** without requiring prior approval or contact requests.

**Key capabilities:**

- Agents in different projects can discover each other via resources
- Agents can send messages to agents in other projects using `send_message()`
- File reservations remain scoped to individual projects
- Each project maintains its own inbox/outbox

**Example that DOES work:**

```
Project A: /data/projects/frontend
  - Agent: "FrontendDev"

Project B: /data/projects/backend
  - Agent: "BackendDev"

Result: FrontendDev and BackendDev CAN communicate directly via send_message().
```

## Recommended Approach: Single Shared Project

For most use cases, especially monorepos or tightly coupled frontend/backend codebases, **use a single shared project** for all agents.

### Pattern 1: Monorepo (Recommended)

Use when frontend and backend are in the same repository:

```
Project: /data/projects/my-app
Repository Structure:
  /data/projects/my-app/
    ├── frontend/          (React, Vue, etc.)
    ├── backend/           (FastAPI, Express, etc.)
    ├── database/          (migrations, schemas)
    └── docs/

Agents:
  - FrontendDev     → Works on frontend/
  - BackendDev      → Works on backend/
  - DatabaseAdmin   → Works on database/
  - DevOpsEngineer  → Works on CI/CD across all directories
```

**Setup:**

```json
// All agents use the SAME project_key

// FrontendDev registers:
{
  "tool": "ensure_project",
  "arguments": {"human_key": "/data/projects/my-app"}
}
{
  "tool": "register_agent",
  "arguments": {
    "project_key": "/data/projects/my-app",
    "name": "FrontendDev",
    "task_description": "React components and UI"
  }
}

// BackendDev registers:
{
  "tool": "ensure_project",
  "arguments": {"human_key": "/data/projects/my-app"}
}
{
  "tool": "register_agent",
  "arguments": {
    "project_key": "/data/projects/my-app",
    "name": "BackendDev",
    "task_description": "API endpoints and business logic"
  }
}

// Now they can communicate!
```

**Benefits:**
- ✅ Agents can discover each other via `resource://agents/my-app`
- ✅ Agents can send messages to coordinate
- ✅ Thread discussions can include all stakeholders
- ✅ File reservations work across the entire codebase
- ✅ Shared project archive for all communication history

**Considerations:**
- All agents share the same agent namespace (agent names must be unique within the project)
- All agents CAN message all other agents in the project (unless contact policies restrict this)
- Agents only see messages addressed to them or sent by them (privacy is maintained)
- File reservations need careful path specification to avoid conflicts

### Pattern 2: Polyrepo with Shared Project

Use when frontend and backend are separate repositories but need coordination:

```
Repositories:
  /data/projects/frontend-repo/     (separate git repo)
  /data/projects/backend-repo/      (separate git repo)

MCP Agent Mail Project:
  /data/projects/my-app-coordination/

Agents:
  - FrontendDev     → Works on /data/projects/frontend-repo/
  - BackendDev      → Works on /data/projects/backend-repo/
  - Both registered in project: /data/projects/my-app-coordination/
```

**Setup:**

```json
// Create a coordination project separate from code repos

// FrontendDev:
{
  "tool": "ensure_project",
  "arguments": {"human_key": "/data/projects/my-app-coordination"}
}
{
  "tool": "register_agent",
  "arguments": {
    "project_key": "/data/projects/my-app-coordination",
    "name": "FrontendDev",
    "task_description": "Frontend development"
  }
}

// BackendDev:
{
  "tool": "register_agent",
  "arguments": {
    "project_key": "/data/projects/my-app-coordination",
    "name": "BackendDev",
    "task_description": "Backend development"
  }
}
```

**Benefits:**
- ✅ Coordination possible despite separate code repositories
- ✅ Message archive is in a dedicated location
- ✅ Clear separation between coordination and code

**Considerations:**
- File reservations won't prevent conflicts across different repos (use messages to coordinate instead)
- Need to manage multiple git repositories

## When to Use Separate Projects

Use separate, isolated projects when:

1. **Completely independent teams** - Teams that rarely or never coordinate
2. **Different organizations** - External contractors, vendors, etc.
3. **Security/privacy requirements** - Need hard isolation between teams
4. **Long-term projects with rare interaction** - Separate concerns that don't need real-time coordination

**Example: Agency with Multiple Clients**

```
Project A: /data/projects/client-acme
  Agents: AcmeFrontend, AcmeBackend, AcmeDevOps

Project B: /data/projects/client-globex
  Agents: GlobexFrontend, GlobexBackend, GlobexDevOps

Result: Complete isolation between client projects.
```

**Important:** Agents in separate projects can now communicate directly without requiring prior approval.

## Cross-Project Messaging

Agents can send messages across project boundaries:

```json
{
  "tool": "send_message",
  "arguments": {
    "project_key": "/data/projects/frontend",
    "sender_name": "FrontendDev",
    "to": ["BackendDev"],  // Agent in another project
    "subject": "API contract discussion",
    "body_md": "Let's coordinate on the new endpoints"
  }
}
```

**Note:** While cross-project messaging is supported, using a single shared project (as recommended above) is simpler for most use cases.

### 3. Federated Agent Directory

Future capability to discover agents across multiple projects:

```json
// FUTURE - NOT YET AVAILABLE
{
  "resource": "resource://agents?scope=all"
}

// Would return agents from all projects the current agent has access to
```

## Migration Guide

If you currently have agents in separate projects that need to coordinate, here's how to migrate:

### Scenario: Frontend and Backend in Separate Projects

**Current state (NOT working):**

```
Project A: /data/projects/frontend
  - Agent: FrontendDev

Project B: /data/projects/backend
  - Agent: BackendDev

Problem: Can't communicate!
```

**Solution: Merge into single project**

```json
// Step 1: Create new shared project
{
  "tool": "ensure_project",
  "arguments": {"human_key": "/data/projects/my-app"}
}

// Step 2: Re-register FrontendDev in new project
{
  "tool": "register_agent",
  "arguments": {
    "project_key": "/data/projects/my-app",
    "name": "FrontendDev",
    "task_description": "Frontend development"
  }
}

// Step 3: Re-register BackendDev in new project
{
  "tool": "register_agent",
  "arguments": {
    "project_key": "/data/projects/my-app",
    "name": "BackendDev",
    "task_description": "Backend development"
  }
}

// Step 4: Both agents now use project_key="/data/projects/my-app" for all operations
```

**Note:** Historical messages from the old projects won't automatically migrate. If you need message history:

1. Archive old project messages using `resource://inbox/{old-project}/{agent}`
2. Reference them in new messages if needed
3. Old messages remain in Git history under the old project archives

## Best Practices

### DO: Use Shared Projects for Coordinating Teams

✅ **Good:** Single project for frontend, backend, database, DevOps teams working on the same product

```
Project: /data/projects/product-abc
Agents: FrontendTeam, BackendTeam, DatabaseTeam, DevOpsTeam
```

### DON'T: Use Separate Projects for Teams That Need to Coordinate

❌ **Bad:** Separate projects for tightly coupled components

```
Project A: /data/projects/frontend
Project B: /data/projects/backend
Result: Teams can't coordinate!
```

### DO: Use Descriptive Agent Names

✅ **Good:** Names that indicate role and domain

```
Agents:
  - FrontendUIComponents
  - BackendAPIServices
  - DatabaseMigrations
  - DevOpsCICD
```

❌ **Bad:** Generic or ambiguous names

```
Agents:
  - Claude
  - Agent1
  - Helper
```

### DO: Use File Reservations to Signal Editing Intent

When multiple agents work on the same codebase, use file reservations to avoid conflicts:

```json
// FrontendDev reserves UI files
{
  "tool": "file_reservation_paths",
  "arguments": {
    "paths": ["src/components/*.tsx", "src/styles/*.css"],
    "exclusive": true,
    "reason": "Redesigning user profile page"
  }
}

// BackendDev reserves API files
{
  "tool": "file_reservation_paths",
  "arguments": {
    "paths": ["api/users/*.py", "api/auth/*.py"],
    "exclusive": true,
    "reason": "Adding OAuth support"
  }
}
```

### DO: Use Threads for Related Discussions

Keep related messages in the same thread:

```json
// Initial message creates thread
{"tool": "send_message", "arguments": {
  "subject": "API contract for user service",
  // Returns: {"thread_id": "msg-123", ...}
}}

// Reply keeps same thread
{"tool": "reply_message", "arguments": {
  "message_id": 123,
  // Automatically preserves thread_id
}}

// Later: Summarize the entire discussion
{"tool": "summarize_thread", "arguments": {
  "thread_id": "msg-123"
}}
```

## Examples

### Example 1: Fullstack Web Application

**Architecture:**
- Monorepo with frontend (React) and backend (FastAPI)
- Shared database
- CI/CD pipeline

**Setup:**

```json
{
  "tool": "ensure_project",
  "arguments": {"human_key": "/data/projects/webapp"}
}

// Register 4 agents in the same project
["FrontendDev", "BackendDev", "DatabaseAdmin", "DevOpsEngineer"].forEach(name => {
  {"tool": "register_agent", "arguments": {
    "project_key": "/data/projects/webapp",
    "name": name
  }}
})
```

**Coordination Example:**

```json
// FrontendDev sends message to BackendDev
{"tool": "send_message", "arguments": {
  "project_key": "/data/projects/webapp",
  "sender_name": "FrontendDev",
  "to": ["BackendDev"],
  "subject": "New feature: user avatars",
  "body_md": "Adding avatar upload. Need API endpoint: POST /api/users/{id}/avatar"
}}

// BackendDev reserves API files
{"tool": "file_reservation_paths", "arguments": {
  "project_key": "/data/projects/webapp",
  "agent_name": "BackendDev",
  "paths": ["backend/api/users.py"],
  "reason": "Adding avatar upload endpoint"
}}

// BackendDev replies when done
{"tool": "reply_message", "arguments": {
  "project_key": "/data/projects/webapp",
  "sender_name": "BackendDev",
  "body_md": "Done! Endpoint accepts multipart/form-data. See backend/api/users.py:145"
}}

// FrontendDev releases backend reservation, reserves frontend
{"tool": "file_reservation_paths", "arguments": {
  "project_key": "/data/projects/webapp",
  "agent_name": "FrontendDev",
  "paths": ["frontend/src/components/UserProfile.tsx"],
  "reason": "Integrating avatar upload UI"
}}
```

### Example 2: Microservices (Separate Repos, Shared Coordination)

**Architecture:**
- 3 microservices in separate repositories
- Each service can be deployed independently
- Need to coordinate API contracts

**Setup:**

```json
// Create coordination project (not a code repo)
{"tool": "ensure_project", "arguments": {
  "human_key": "/data/projects/microservices-platform-coordination"
}}

// Register agents for each service
{"tool": "register_agent", "arguments": {
  "project_key": "/data/projects/microservices-platform-coordination",
  "name": "UserServiceDev",
  "task_description": "User authentication service"
}}

{"tool": "register_agent", "arguments": {
  "project_key": "/data/projects/microservices-platform-coordination",
  "name": "PaymentServiceDev",
  "task_description": "Payment processing service"
}}

{"tool": "register_agent", "arguments": {
  "project_key": "/data/projects/microservices-platform-coordination",
  "name": "NotificationServiceDev",
  "task_description": "Email/SMS notification service"
}}
```

**Coordination Example:**

```json
// UserServiceDev announces API change
{"tool": "send_message", "arguments": {
  "project_key": "/data/projects/microservices-platform-coordination",
  "sender_name": "UserServiceDev",
  "to": ["PaymentServiceDev", "NotificationServiceDev"],
  "cc": [],
  "subject": "BREAKING: User API v2 migration",
  "body_md": "Upgrading to v2 API. Old /users/{id} → /v2/users/{uuid}. Migration by Dec 1.",
  "importance": "high"
}}

// PaymentServiceDev acknowledges
{"tool": "reply_message", "arguments": {
  "sender_name": "PaymentServiceDev",
  "body_md": "Acknowledged. Will update payment service by Nov 25."
}}

// Later: Check who hasn't acknowledged
{"tool": "fetch_inbox", "arguments": {
  "agent_name": "UserServiceDev",
  // Check for ack_required messages
}}
```

## Troubleshooting

### Problem: Agents can't see each other

**Symptoms:**
```
Error: Agent 'FrontendDev' not registered for project '/data/projects/backend'
```

**Diagnosis:**
```json
// Check which project each agent is in
{"resource": "resource://projects"}
// Returns list of all projects

{"resource": "resource://agents/backend-abc"}
// Check agents in backend project

{"resource": "resource://agents/frontend-xyz"}
// Check agents in frontend project
```

**Solution:**
Re-register all agents in a single shared project (see Migration Guide above).

### Problem: Message not received

**Symptoms:**
- Sent message but recipient's inbox is empty

**Diagnosis:**
```json
// Verify both agents are in same project
{"resource": "resource://agents/{project}"}

// Check sender's outbox
{"resource": "resource://outbox/{project}/{sender}"}

// Check recipient's inbox
{"resource": "resource://inbox/{project}/{recipient}"}
```

**Common causes:**
1. Agents in different projects
2. Wrong recipient name (typo)
3. Message filtered by contact policy

## Summary

**Current Best Practice:**
- ✅ Use a **single shared project** for all agents that need to coordinate
- ✅ Choose a descriptive project name (e.g., `/data/projects/my-product`)
- ✅ Register all agents in the same project
- ✅ Use file reservations to signal editing intent and avoid conflicts
- ✅ Use threads to organize related discussions

**Future:**
- ⏳ Cross-project messaging (planned)
- ⏳ Federated agent directory (planned)
- ⏳ Cross-project file reservations (planned)

**When in doubt:**
- If agents need to coordinate → Same project
- If agents should be isolated → Separate projects

For more information:
- [AGENT_ONBOARDING.md](./AGENT_ONBOARDING.md) - Step-by-step agent workflows
- [ROOT_CAUSE_ANALYSIS.md](./ROOT_CAUSE_ANALYSIS.md) - Understanding coordination failures
- [README.md](./README.md) - Full system documentation
