"""Журнал смены статусов сделки и ключи плагина по устройствам.

feedback → deal_status_log (решения менеджера переносятся, таблица удаляется);
manager.ext_key_hash → ext_key («первый ключ»), колонка удаляется.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None

STATUSES = ("new", "filtered_out", "analyzed", "hot", "taken", "submitted", "skipped", "expired")


def _status(name: str, column: str, nullable: bool) -> sa.Column[str]:
    return sa.Column(
        column,
        sa.Enum(
            *STATUSES, name=op.f(f"ck_{name}_{column}"), native_enum=False, create_constraint=True, length=32
        ),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "deal_status_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("deal_id", sa.BigInteger(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        _status("deal_status_log", "from_status", True),
        _status("deal_status_log", "to_status", False),
        sa.Column(
            "actor",
            sa.Enum(
                "manager",
                "robot",
                name=op.f("ck_deal_status_log_actor"),
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("actor_name", sa.String(255), nullable=False),
        sa.Column(
            "channel",
            sa.Enum(
                "stand",
                "extension",
                "worker",
                name=op.f("ck_deal_status_log_channel"),
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("device", sa.String(80), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("conditions_met", sa.Integer(), nullable=True),
        sa.Column("rules_version", sa.String(64), nullable=True),
        sa.Column("proposal_id", sa.BigInteger(), nullable=True),
        sa.Column("proposal_version", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(
            ["deal_id"], ["deal.id"], name=op.f("fk_deal_status_log_deal_id_deal"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["proposal.id"],
            name=op.f("fk_deal_status_log_proposal_id_proposal"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deal_status_log")),
    )
    op.create_index(op.f("ix_deal_status_log_deal_id"), "deal_status_log", ["deal_id"])
    op.create_index("ix_deal_status_log_at", "deal_status_log", ["at"])
    op.execute(
        """
        INSERT INTO deal_status_log (deal_id, at, to_status, actor, actor_name, channel, reason, details)
        SELECT f.deal_id, f.created_at, f.action, 'manager', COALESCE(m.name, 'менеджер'),
               CASE WHEN f.manager_id IS NULL THEN 'stand' ELSE 'extension' END, f.reason,
               '{"from": "feedback"}'::jsonb
        FROM feedback f LEFT JOIN manager m ON m.id = f.manager_id
        ORDER BY f.id
        """
    )
    op.drop_index(op.f("ix_feedback_deal_id"), table_name="feedback")
    op.drop_table("feedback")

    op.create_table(
        "ext_key",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["manager_id"], ["manager.id"], name=op.f("fk_ext_key_manager_id_manager"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ext_key")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_ext_key_key_hash")),
    )
    op.create_index(op.f("ix_ext_key_manager_id"), "ext_key", ["manager_id"])
    op.execute(
        """
        INSERT INTO ext_key (manager_id, key_hash, label, created_at)
        SELECT id, ext_key_hash, 'первый ключ', updated_at FROM manager WHERE ext_key_hash IS NOT NULL
        """
    )
    op.drop_constraint(op.f("uq_manager_ext_key_hash"), "manager", type_="unique")
    op.drop_column("manager", "ext_key_hash")


def downgrade() -> None:
    op.add_column("manager", sa.Column("ext_key_hash", sa.String(128), nullable=True))
    op.create_unique_constraint(op.f("uq_manager_ext_key_hash"), "manager", ["ext_key_hash"])
    op.execute(
        """
        UPDATE manager m SET ext_key_hash = k.key_hash
        FROM (SELECT DISTINCT ON (manager_id) manager_id, key_hash FROM ext_key
              WHERE revoked_at IS NULL ORDER BY manager_id, id DESC) k
        WHERE k.manager_id = m.id
        """
    )
    op.drop_index(op.f("ix_ext_key_manager_id"), table_name="ext_key")
    op.drop_table("ext_key")

    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("deal_id", sa.BigInteger(), nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=True),
        _status("feedback", "action", False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["deal_id"], ["deal.id"], name=op.f("fk_feedback_deal_id_deal"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"], ["manager.id"], name=op.f("fk_feedback_manager_id_manager"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
    )
    op.create_index(op.f("ix_feedback_deal_id"), "feedback", ["deal_id"])
    op.execute(
        """
        INSERT INTO feedback (deal_id, action, reason, created_at, updated_at)
        SELECT deal_id, to_status, reason, at, at FROM deal_status_log WHERE actor = 'manager' ORDER BY id
        """
    )
    op.drop_index("ix_deal_status_log_at", table_name="deal_status_log")
    op.drop_index(op.f("ix_deal_status_log_deal_id"), table_name="deal_status_log")
    op.drop_table("deal_status_log")
