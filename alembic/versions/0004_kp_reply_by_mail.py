"""КП ответом на письмо по сделке: Message-ID и тема письма, канал email_kp, канал журнала «почта».

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None


def _check(table: str, column: str, values: tuple[str, ...]) -> None:
    name = f"ck_{table}_{column}"
    op.drop_constraint(name, table, type_="check")
    listed = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(op.f(name), table, f"{column} IN ({listed})")


def upgrade() -> None:
    op.add_column("notification", sa.Column("message_id", sa.String(255), nullable=True))
    op.add_column("notification", sa.Column("subject", sa.Text(), nullable=True))
    _check("notification", "channel", ("email_deal", "email_daily", "extension", "email_kp"))
    _check("deal_status_log", "channel", ("stand", "extension", "worker", "email"))


def downgrade() -> None:
    op.execute("DELETE FROM notification WHERE channel = 'email_kp'")
    op.execute("UPDATE deal_status_log SET channel = 'stand' WHERE channel = 'email'")
    _check("deal_status_log", "channel", ("stand", "extension", "worker"))
    _check("notification", "channel", ("email_deal", "email_daily", "extension"))
    op.drop_column("notification", "subject")
    op.drop_column("notification", "message_id")
