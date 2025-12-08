"""PRD template engine with Jinja2 for generating markdown from wizard data."""

from __future__ import annotations

from jinja2 import Environment, PackageLoader

from .models import PRD


def get_jinja_environment() -> Environment:
    """Get configured Jinja2 environment for template rendering."""
    env = Environment(
        loader=PackageLoader("mcp_agent_mail", "templates/prd"),
        autoescape=False,  # Don't escape markdown
    )
    env.filters["iso_timestamp"] = lambda dt: dt.isoformat() if dt else ""
    env.filters["format_date"] = lambda dt: dt.strftime("%Y-%m-%d") if dt else ""
    return env


def generate_prd_content(
    prd: PRD,
    ai_enriched_rounds: dict[str, str] | None = None,
) -> str:
    """Generate markdown PRD from PRD object and optional AI-enriched content.

    Args:
        prd: PRD database model with wizard answers
        ai_enriched_rounds: Optional dict with keys 'round1', 'round2', 'round3'
                           containing AI-enriched content for each round

    Returns:
        Rendered markdown PRD as string
    """
    env = get_jinja_environment()
    template = env.get_template("prd_base.jinja2")

    # Prepare context
    context = {
        "prd_id": prd.id,
        "name": prd.name,
        "idea": prd.idea,
        "scope_in": prd.scope_in,
        "scope_out": prd.scope_out,
        "personas": prd.personas,
        "success_metrics": prd.success_metrics,
        "created_ts": prd.created_ts.isoformat(),
        "updated_ts": prd.updated_ts.isoformat(),
        "status": prd.status,
        "generation_mode": prd.generation_mode,
        "ai_enriched": prd.ai_enriched,
        # Round-specific data
        "round1_focus": prd.round1_tokens.get("focus", ""),
        "round2_focus": prd.round2_components.get("focus", ""),
        "round3_focus": prd.round3_layouts.get("focus", ""),
        # AI-enriched content (if available)
        "round1_content": ai_enriched_rounds.get("round1", "") if ai_enriched_rounds else "",
        "round2_content": ai_enriched_rounds.get("round2", "") if ai_enriched_rounds else "",
        "round3_content": ai_enriched_rounds.get("round3", "") if ai_enriched_rounds else "",
    }

    return template.render(context)


def build_claude_enrichment_prompt(prd: PRD, reference_prd_excerpt: str) -> str:
    """Build Claude API prompt for AI enrichment of PRD content.

    Args:
        prd: PRD database model with wizard answers
        reference_prd_excerpt: Example PRD structure (first 200 lines) for reference

    Returns:
        Prompt string ready for Claude API
    """
    prompt = f"""You are a Product Requirements Document expert specializing in design systems
and TDD-driven product specification.

Given the following PRD concept, generate comprehensive TDD-driven content for Rounds 1, 2, and 3.
Use the reference PRD structure as a guide for organization and test specification format.

---

**PRD Metadata:**
- Name: {prd.name}
- Core Idea: {prd.idea}
- Scope (In): {prd.scope_in}
- Scope (Out): {prd.scope_out}
- Target Personas: {prd.personas}
- Success Metrics: {prd.success_metrics}

---

**Round 1 Focus (Foundational Tokens & Structure):**
{prd.round1_tokens.get('focus', 'Not specified')}

**Round 2 Focus (Core Components):**
{prd.round2_components.get('focus', 'Not specified')}

**Round 3 Focus (Layouts & Responsive Design):**
{prd.round3_layouts.get('focus', 'Not specified')}

---

**Reference PRD Structure:**

{reference_prd_excerpt}

---

**Generate:**

1. **Round 1: Foundational Tokens & Structure** with:
   - Design token categories (colors, spacing, typography, shadows, borders, etc.)
   - 15+ test specifications for token validation
   - Concrete examples of token definitions
   - Mathematical scales and consistency rules

2. **Round 2: Core Components** with:
   - 3-5 semantic component definitions
   - Base CSS structure for each component
   - Variant system (light/dark, success/warning/error, etc.)
   - Interactive states (hover, focus, active, disabled)
   - 15+ test specifications per component
   - Accessibility requirements

3. **Round 3: Layouts & Responsive Design** with:
   - Responsive breakpoint definitions (mobile, tablet, desktop, ultra-wide)
   - Layout templates with grid structures
   - Mobile-first approach and progressive enhancement
   - 15+ test specifications for responsive behavior
   - Dark mode layout verification
   - Accessibility layout tests

**Output format:**
- Use Markdown with clear section headers (##, ###, ####)
- Include code blocks with ```css``` or ```html``` for examples
- Format test specifications as:
  ```
  TEST: Test_Name
  - GIVEN: condition
  - WHEN: action
  - THEN: expected result
  ```
- Include concrete values (colors, dimensions, etc.)
- Be specific and measurable, not vague
- Assume a design system methodology (Sentinel Research style)

**Important:**
- Make content specific to the PRD concept, not generic
- Test specifications must be testable and measurable
- Include both visual and accessibility requirements
- Organize content in three clear rounds
- Maintain consistency with reference PRD structure

---

Generate the complete three-round PRD content below:
"""
    return prompt
