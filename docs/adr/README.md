# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) documenting significant technical decisions made for the MCP Agent Mail project.

## Index

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [ADR-002](002-rust-optimization-analysis.md) | Rust/PyO3 Optimization Analysis | Decided | January 2026 |

## What is an ADR?

An Architecture Decision Record (ADR) is a document that captures an important architectural decision made along with its context and consequences. ADRs help future developers understand:

- **Why** a decision was made
- **What** alternatives were considered
- **When** to reconsider the decision

## Template

When adding a new ADR, use this structure:

```markdown
# ADR-XXX: Title

## Status
[Proposed | Decided | Superseded by ADR-YYY | Deprecated]

## Context
What is the issue that we're seeing that is motivating this decision?

## Decision
What is the change that we're proposing and/or doing?

## Consequences
What becomes easier or more difficult to do because of this change?
```

## References

- [Michael Nygard's ADR template](https://github.com/joelparkerhenderson/architecture-decision-record/blob/main/templates/decision-record-template-by-michael-nygard/index.md)
- [ADR GitHub organization](https://adr.github.io/)
