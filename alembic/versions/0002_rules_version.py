"""Версии правил в БД (правка на вкладке «Админка» без пересборки).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rules_version",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(12), nullable=False),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column("author", sa.String(255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_rules_version"),
    )
    op.create_index("ix_rules_version_version", "rules_version", ["version"])


def downgrade() -> None:
    op.drop_index("ix_rules_version_version", table_name="rules_version")
    op.drop_table("rules_version")
