"""Initial schema - consolidated baseline.

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-02-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ark_records table
    op.create_table(
        "ark_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("naan", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=1), nullable=False),
        sa.Column("authority_id", sa.String(length=255), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("tombstoned_at", sa.DateTime(), nullable=True),
        sa.Column("client_item_id", sa.String(length=100), nullable=True),
        sa.Column("publish_retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("publish_last_error", sa.Text(), nullable=True),
        sa.Column("publish_last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("publish_permanently_failed", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("naan", "name", name="uq_naan_name"),
    )
    op.create_index(op.f("ix_ark_records_authority_id"), "ark_records", ["authority_id"], unique=False)
    op.create_index(op.f("ix_ark_records_created_at"), "ark_records", ["created_at"], unique=False)
    op.create_index(op.f("ix_ark_records_state"), "ark_records", ["state"], unique=False)
    op.create_index("ix_naan_name", "ark_records", ["naan", "name"], unique=False)
    op.create_index("ix_state_authority", "ark_records", ["state", "authority_id"], unique=False)
    op.create_index(
        "ix_state_permanently_failed_created",
        "ark_records",
        ["state", "publish_permanently_failed", "created_at"],
        unique=False,
    )

    # noid_counters table
    op.create_table(
        "noid_counters",
        sa.Column("namespace_key", sa.String(length=160), nullable=False),
        sa.Column("next_value", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("namespace_key"),
    )

    # worker_runtime_status table
    op.create_table(
        "worker_runtime_status",
        sa.Column("worker_name", sa.String(length=100), nullable=False),
        sa.Column("instance_id", sa.String(length=100), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("last_cycle_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("total_processed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_succeeded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_permanent_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("worker_name"),
    )
    op.create_index(
        op.f("ix_worker_runtime_status_last_heartbeat_at"),
        "worker_runtime_status",
        ["last_heartbeat_at"],
        unique=False,
    )

    # ark_metadata table
    op.create_table(
        "ark_metadata",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ark_record_id", sa.Integer(), nullable=False),
        sa.Column("level1_json", sa.JSON(), nullable=False),
        sa.Column("level1_cid", sa.String(length=100), nullable=True),
        sa.Column("original_content", sa.Text(), nullable=False),
        sa.Column("original_schema", sa.String(length=50), nullable=False),
        sa.Column("original_cid", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["ark_record_id"], ["ark_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ark_record_id"),
    )
    op.create_index(op.f("ix_ark_metadata_ark_record_id"), "ark_metadata", ["ark_record_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ark_metadata_ark_record_id"), table_name="ark_metadata")
    op.drop_table("ark_metadata")
    op.drop_index(op.f("ix_worker_runtime_status_last_heartbeat_at"), table_name="worker_runtime_status")
    op.drop_table("worker_runtime_status")
    op.drop_table("noid_counters")
    op.drop_index("ix_state_permanently_failed_created", table_name="ark_records")
    op.drop_index("ix_state_authority", table_name="ark_records")
    op.drop_index("ix_naan_name", table_name="ark_records")
    op.drop_index(op.f("ix_ark_records_state"), table_name="ark_records")
    op.drop_index(op.f("ix_ark_records_created_at"), table_name="ark_records")
    op.drop_index(op.f("ix_ark_records_authority_id"), table_name="ark_records")
    op.drop_table("ark_records")
