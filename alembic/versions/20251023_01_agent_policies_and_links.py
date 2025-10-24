"""Add agent attachments/contact policy and agent_links table

Revision ID: 20251023_01
Revises:
Create Date: 2025-10-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20251023_01"
down_revision = None
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = [c["name"] for c in insp.get_columns(table)]
    return column in cols


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return name in insp.get_table_names()


def upgrade() -> None:
    # agents.attachments_policy
    if _has_table("agents") and not _has_column("agents", "attachments_policy"):
        op.add_column(
            "agents",
            sa.Column("attachments_policy", sa.String(length=16), nullable=False, server_default="auto"),
        )
        op.alter_column("agents", "attachments_policy", server_default=None)

    # agents.contact_policy
    if _has_table("agents") and not _has_column("agents", "contact_policy"):
        op.add_column(
            "agents",
            sa.Column("contact_policy", sa.String(length=16), nullable=False, server_default="auto"),
        )
        op.alter_column("agents", "contact_policy", server_default=None)

    # agent_links table
    if not _has_table("agent_links"):
        op.create_table(
            "agent_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("a_project_id", sa.Integer(), nullable=False),
            sa.Column("a_agent_id", sa.Integer(), nullable=False),
            sa.Column("b_project_id", sa.Integer(), nullable=False),
            sa.Column("b_agent_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("reason", sa.String(length=512), nullable=False, server_default=""),
            sa.Column("created_ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("expires_ts", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["a_project_id"], ["projects.id"], name="fk_agent_links_a_project"),
            sa.ForeignKeyConstraint(["a_agent_id"], ["agents.id"], name="fk_agent_links_a_agent"),
            sa.ForeignKeyConstraint(["b_project_id"], ["projects.id"], name="fk_agent_links_b_project"),
            sa.ForeignKeyConstraint(["b_agent_id"], ["agents.id"], name="fk_agent_links_b_agent"),
        )
    # Create indexes (safe if table just created)
    if _has_table("agent_links"):
        bind = op.get_bind()
        insp = sa.inspect(bind)
        existing = {ix["name"] for ix in insp.get_indexes("agent_links")}
        def _mk(name: str, cols: list[str]):
            if name not in existing:
                op.create_index(name, "agent_links", cols)
        _mk("ix_agent_links_a_project_id", ["a_project_id"])
        _mk("ix_agent_links_a_agent_id", ["a_agent_id"])
        _mk("ix_agent_links_b_project_id", ["b_project_id"])
        _mk("ix_agent_links_b_agent_id", ["b_agent_id"])


def downgrade() -> None:
    # Conservative: drop agent_links if present; keep columns (non-destructive)
    if _has_table("agent_links"):
        op.drop_table("agent_links")
    # Columns left intact to avoid destructive downgrades by default


