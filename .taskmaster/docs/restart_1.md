# PRD Generation System — Implementation Restart (Checkpoint 1)

## Context

**Completed Design Phase:**
- ✅ Architecture: 3-layer (Data/Wizard/Assembly)
- ✅ Storage: Database-backed in Agent Mail with web UI
- ✅ Generation: Interactive wizard + AI enrichment (hybrid mode)
- ✅ Interface: CLI + slash command
- ✅ Schema: PRD + PRDComponentLink tables (SQLModel)
- ✅ Wizard Flow: 5-minute guided questionnaire (basic info → scope → success metrics → round-specific)
- ✅ Assembly: Jinja2 templates with brutally strict YAML frontmatter (all strings quoted, lowercase bools, ISO-8601 timestamps)
- ✅ AI Enrichment: Claude API prompt for Rounds 1/2/3 with test specifications (reference KellerAI PRD as structural example)

## Next Implementation Tasks

### Phase 1: Database & Models (CURRENT)
1. Create `src/mcp_agent_mail/models/prd.py` with SQLModel classes:
   - `PRD`: id, name, idea, scope_in, scope_out, personas, success_metrics, round1_tokens, round2_components, round3_layouts, created_ts, updated_ts, status, content_md
   - `PRDComponentLink`: id, prd_id (FK), component_name, pr_url, pr_status, synced_ts

2. Add migrations (Alembic) to create prd and prd_component_link tables

3. Create database session/repository functions for CRUD operations

### Phase 2: Wizard Engine
1. Create `src/mcp_agent_mail/wizard/questionnaire.py`:
   - Question definitions (5 basic + 3 round-specific)
   - Answer collection and validation
   - JSON serialization to database

2. Create `src/mcp_agent_mail/wizard/ai_enrichment.py`:
   - Claude API client integration
   - Prompt engineering for Rounds 1/2/3
   - Content enrichment logic (Quick vs Full mode)

### Phase 3: Assembly & Templates
1. Create `src/mcp_agent_mail/templates/prd_template.jinja2`:
   - Strict YAML frontmatter
   - 3-round structure with sections
   - Test specification placeholders
   - Implementation roadmap

2. Create `src/mcp_agent_mail/prd/assembly.py`:
   - Template rendering
   - Markdown generation
   - Content caching in database

### Phase 4: CLI & Slash Command
1. Create `src/cli.py generate-prd` command:
   - Entry point for interactive wizard
   - Mode selection (Quick/Full)
   - Save to database
   - Output markdown file or display

2. Create `.claude/commands/generate-prd.md` slash command:
   - Invoke same wizard flow in chat context
   - Display generated PRD in response

### Phase 5: Web UI (Views)
1. Create routes in app:
   - `GET /mail/{project}/prd/` → List all PRDs
   - `GET /mail/{project}/prd/{prd_id}` → View PRD
   - `GET /mail/{project}/prd/{prd_id}/edit` → Edit PRD
   - `POST /mail/{project}/prd/{prd_id}/delete` → Delete PRD
   - `POST /mail/{project}/prd/{prd_id}/publish` → Change status

2. Create templates:
   - `mail_prd_list.html` → Grid/list of PRDs
   - `mail_prd_view.html` → Full PRD display with metadata
   - `mail_prd_edit.html` → Edit wizard form

### Phase 6: PR Result Linking (Future)
1. Add routes for component linking:
   - `POST /mail/{project}/prd/{prd_id}/component-link` → Link PR
   - Update `PRDComponentLink` with pr_url, pr_status
   - Sync timestamps

2. Add component status tracking in PRD view

## Key Design Decisions

- **Storage**: Agent Mail database (existing) — no new infrastructure
- **Generation**: Interactive wizard (user-guided) + Claude enrichment (optional)
- **Mode**: Quick (2 min) vs Full (30-60 sec + Claude)
- **Frontmatter**: Brutally strict YAML (all strings quoted, lowercase bools, ISO-8601)
- **Interface**: CLI (`uv run src/cli.py generate-prd`) + Slash (`/generate-prd`)
- **Extensibility**: Schema designed for non-KellerAI PRD types in future

## Immediate Next Step

**Begin Phase 1: Create PRD database models in SQLModel.**

Start by reading existing SQLModel patterns in `src/mcp_agent_mail/models/` to match code style, then create `prd.py` with:
- PRD table definition
- PRDComponentLink table definition
- Relationships and constraints
- Default values and validation

Then proceed to Alembic migration for schema creation.

---

**Status**: Design phase complete. Ready for implementation starting with database models.
**Last Updated**: 2025-11-29
**Session**: Brainstorming completion → Implementation checkpoint
