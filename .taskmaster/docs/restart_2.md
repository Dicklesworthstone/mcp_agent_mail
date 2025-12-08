# PRD Generation System — Implementation Checkpoint 2

## Completed Components

### Phase 1: Database & Models ✅
- **File**: `src/mcp_agent_mail/models.py`
- **Changes**: Added `PRD` and `PRDComponentLink` SQLModel classes
- **Details**:
  - PRD table: id, project_id (FK), name, idea, scope_in, scope_out, personas, success_metrics
  - JSON fields: round1_tokens, round2_components, round3_layouts (for wizard answers)
  - Metadata: status (draft/published/archived), generation_mode (quick/full), ai_enriched (bool)
  - Timestamps: created_ts, updated_ts (timezone-aware)
  - PRDComponentLink table: id, prd_id (FK), component_name, pr_url, pr_status, synced_ts
- **Schema**: Auto-creates on app startup via SQLModel.metadata.create_all()
- **Validation**: ✅ No lint/type errors

### Phase 2: Wizard Engine ✅
- **File**: `src/mcp_agent_mail/prd_wizard.py`
- **Classes**:
  - `PRDQuestion`: Single question with key, prompt, help_text, multiline support
  - `PRDWizard`: Interactive questionnaire runner with 9 questions
- **Questionnaire Flow** (9 questions):
  1. PRD Name
  2. Core Idea / Problem Statement (multiline)
  3. In Scope (multiline)
  4. Out of Scope (multiline)
  5. Target Personas (multiline)
  6. Success Metrics (multiline)
  7. Round 1 Focus: Tokens & Structure (multiline)
  8. Round 2 Focus: Components (multiline)
  9. Round 3 Focus: Layouts & Responsive (multiline)
- **Methods**:
  - `run()`: Execute full wizard, return collected answers
  - `structure_answers()`: Convert answers to {basic_fields, round_answers} dict
- **Validation**: ✅ Linting fixed, no type errors

### Phase 3: Template Engine ✅
- **File**: `src/mcp_agent_mail/prd_template.py`
- **Functions**:
  - `get_jinja_environment()`: Configured Jinja2 env with custom filters (iso_timestamp, format_date)
  - `generate_prd_content(prd, ai_enriched_rounds)`: Render markdown from PRD + optional AI content
  - `build_claude_enrichment_prompt(prd, reference_prd_excerpt)`: Build Claude API prompt for Rounds 1-3
- **Template**: `src/mcp_agent_mail/templates/prd/prd_base.jinja2`
  - Strict YAML frontmatter with all fields quoted
  - Three-round structure (Round 1/2/3) with optional AI-enriched content fallback
  - Skeleton mode when AI content not available
  - Includes test execution sections and implementation roadmap
- **Validation**: ✅ All linting and type checks pass

## Files Created

```
src/mcp_agent_mail/
├── models.py                                    (modified: added PRD + PRDComponentLink)
├── prd_wizard.py                               (new: questionnaire engine)
├── prd_template.py                             (new: template rendering)
└── templates/prd/
    └── prd_base.jinja2                         (new: PRD template)
```

## Next Phases

### Phase 4: CLI & Slash Command (NEXT)
1. Create `src/cli.py` update with `generate-prd` command:
   - Entry point: `generate_prd(project_key, mode="quick")`
   - Wizard flow integration
   - AI enrichment (if mode="full")
   - Database save
   - Output markdown file or display

2. Create `.claude/commands/generate-prd.md` slash command:
   - Invoke same wizard in chat context
   - Display generated PRD in response

### Phase 5: Web UI Views (AFTER CLI)
1. Routes needed:
   - `GET /mail/{project}/prd/` → List PRDs
   - `GET /mail/{project}/prd/{prd_id}` → View PRD
   - `GET /mail/{project}/prd/{prd_id}/edit` → Edit PRD
   - `POST /mail/{project}/prd/{prd_id}/delete` → Delete
   - `POST /mail/{project}/prd/{prd_id}/publish` → Change status

2. Templates needed:
   - `mail_prd_list.html` - Grid/list view
   - `mail_prd_view.html` - Full PRD display
   - `mail_prd_edit.html` - Edit form

### Phase 6: PR Result Linking (FUTURE)
1. Routes for component linking:
   - `POST /mail/{project}/prd/{prd_id}/component-link`
   - Update PRDComponentLink with pr_url, pr_status

2. Sync timestamps and status tracking

## Design Decisions

✅ **Storage**: Agent Mail database (existing infrastructure)
✅ **Generation**: Interactive wizard (user-guided) + Claude enrichment (optional)
✅ **Modes**: Quick (direct assembly) vs Full (AI enrichment + assembly)
✅ **Frontmatter**: Brutally strict YAML (all strings quoted, lowercase bools, ISO-8601 timestamps)
✅ **Interface**: Both CLI and slash command
✅ **Templates**: Jinja2 with proper escaping disabled for markdown

## Key Implementation Details

### PRD Model Structure
```python
prd = PRD(
    project_id=1,
    name="Design System v2",
    idea="Create unified design system...",
    scope_in="Colors, components, layouts",
    scope_out="Animation library",
    personas="Designers, developers",
    success_metrics="100% component coverage",
    round1_tokens={"focus": "..."},
    round2_components={"focus": "..."},
    round3_layouts={"focus": "..."},
    generation_mode="quick",  # or "full"
    ai_enriched=False,  # true if AI-enriched
    status="draft"  # draft, published, archived
)
```

### Wizard Answer Structuring
```python
wizard = PRDWizard()
answers = wizard.run()  # Dict with question keys
basic_fields, round_answers = wizard.structure_answers()

# Result structure:
{
    "round1_tokens": {"focus": "..."},
    "round2_components": {"focus": "..."},
    "round3_layouts": {"focus": "..."}
}
```

### PRD Content Generation
```python
# Quick mode (direct)
content = generate_prd_content(prd)

# Full mode (with AI)
content = generate_prd_content(prd, ai_enriched_rounds={
    "round1": "...(AI generated)...",
    "round2": "...(AI generated)...",
    "round3": "...(AI generated)..."
})
```

## Immediate Next Step

**Begin Phase 4: CLI Implementation**

Update `src/cli.py` to add `generate-prd` command:
1. Parse `--project-key` or detect from context
2. Run PRDWizard().run()
3. Save wizard answers to database (PRD record)
4. If mode="full": call Claude API with enrichment prompt
5. Generate markdown via template engine
6. Save content_md to PRD.content_md
7. Output result (file or display)

---

**Status**: All core infrastructure built. Ready for CLI integration.
**Last Updated**: 2025-11-29
**Token Estimate**: ~70K used, significant work complete
