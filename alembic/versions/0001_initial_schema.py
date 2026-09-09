"""Initial schema - consolidated baseline (includes all migrations).

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
    op.create_table(
        "processing_stages",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "processing_statuses",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "processing_error_codes",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "processing_wait_reasons",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.bulk_insert(
        sa.table("processing_stages", sa.column("id"), sa.column("code"), sa.column("description")),
        [
            {"id": 0, "code": "none", "description": "No active workflow"},
            {"id": 1, "code": "metadata", "description": "Persist Level 1 and Level 2 metadata"},
            {"id": 2, "code": "availability", "description": "Verify publication replica threshold"},
            {"id": 3, "code": "chain", "description": "Publish metadata reference to blockchain"},
            {"id": 4, "code": "replication", "description": "Reach retention replica threshold"},
            {"id": 5, "code": "complete", "description": "Workflow completed"},
        ],
    )
    op.bulk_insert(
        sa.table("processing_statuses", sa.column("id"), sa.column("code"), sa.column("description")),
        [
            {"id": 1, "code": "ready", "description": "Ready for its owning worker"},
            {"id": 2, "code": "waiting", "description": "Scheduled for a later normal-worker action"},
            {"id": 3, "code": "failed", "description": "Manual intervention required"},
            {"id": 4, "code": "done", "description": "Stage completed"},
            {"id": 5, "code": "cancelled", "description": "Cancelled by tombstone"},
        ],
    )
    op.bulk_insert(
        sa.table("processing_wait_reasons", sa.column("id"), sa.column("code"), sa.column("description")),
        [
            {"id": 0, "code": "none", "description": "No scheduled wait"},
            {"id": 1, "code": "cluster_pinning", "description": "Cluster accepted content and is pinning it"},
            {"id": 2, "code": "initial_visibility", "description": "Waiting for the first replica to become visible"},
            {"id": 3, "code": "replica_target", "description": "Waiting for the configured replication target"},
            {"id": 4, "code": "storage_backoff", "description": "Retrying after a temporary storage failure"},
            {"id": 5, "code": "rpc_backoff", "description": "Retrying after a temporary RPC failure"},
            {"id": 6, "code": "chain_confirmation", "description": "Waiting for chain transaction confirmation"},
            {"id": 7, "code": "cluster_queued", "description": "Cluster has queued a pin assignment"},
        ],
    )
    op.bulk_insert(
        sa.table("processing_error_codes", sa.column("id"), sa.column("code"), sa.column("retryable"), sa.column("description")),
        [
            {"id": 100, "code": "storage_unavailable", "retryable": True, "description": "Metadata storage is unavailable"},
            {"id": 101, "code": "storage_invalid_response", "retryable": True, "description": "Metadata storage returned an unrecognised response; retry after endpoint recovery"},
            {"id": 200, "code": "replication_unavailable", "retryable": True, "description": "Replication status cannot be observed"},
            {"id": 201, "code": "cid_mismatch", "retryable": False, "description": "Repaired content generated a different CID"},
            {"id": 300, "code": "chain_rpc_unavailable", "retryable": True, "description": "Blockchain RPC is unavailable"},
            {"id": 301, "code": "chain_reverted", "retryable": False, "description": "Blockchain transaction reverted"},
            {"id": 302, "code": "authority_not_found", "retryable": False, "description": "Authority was not found"},
            {"id": 303, "code": "authorization_failed", "retryable": False, "description": "Authority authorization failed"},
            {"id": 304, "code": "chain_state_conflict", "retryable": False, "description": "On-chain state differs from expected state"},
            {"id": 900, "code": "unexpected", "retryable": True, "description": "Unexpected worker error"},
        ],
    )

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
        sa.Column("processing_stage", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("processing_status", sa.SmallInteger(), server_default="4", nullable=False),
        sa.Column("processing_attempt_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("next_action_at", sa.DateTime(), nullable=True),
        sa.Column("processing_wait_reason", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("processing_error_code", sa.SmallInteger(), nullable=True),
        sa.Column("processing_error_detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["processing_stage"], ["processing_stages.id"]),
        sa.ForeignKeyConstraint(["processing_status"], ["processing_statuses.id"]),
        sa.ForeignKeyConstraint(["processing_wait_reason"], ["processing_wait_reasons.id"]),
        sa.ForeignKeyConstraint(["processing_error_code"], ["processing_error_codes.id"]),
        sa.UniqueConstraint("naan", "name", name="uq_naan_name"),
    )
    op.create_index(op.f("ix_ark_records_authority_id"), "ark_records", ["authority_id"], unique=False)
    op.create_index(op.f("ix_ark_records_created_at"), "ark_records", ["created_at"], unique=False)
    op.create_index(op.f("ix_ark_records_state"), "ark_records", ["state"], unique=False)
    op.create_index("ix_naan_name", "ark_records", ["naan", "name"], unique=False)
    op.create_index("ix_state_authority", "ark_records", ["state", "authority_id"], unique=False)
    op.create_index("ix_ark_processing_queue", "ark_records", ["processing_stage", "processing_status", "next_action_at"], unique=False)
    op.create_index(
        "ix_ark_processing_active_due",
        "ark_records",
        ["processing_stage", "next_action_at", "id"],
        unique=False,
        postgresql_where=sa.text("processing_status IN (1, 2)"),
    )
    op.create_index(
        "ix_ark_detail_active_rollup",
        "ark_records",
        ["processing_stage", "processing_status", "processing_wait_reason", "next_action_at"],
        unique=False,
        postgresql_where=sa.text("state IN ('D', 'U', 'P')"),
        sqlite_where=sa.text("state IN ('D', 'U', 'P')"),
    )
    op.create_index(
        "ix_ark_failed_summary",
        "ark_records",
        ["processing_stage", "processing_error_code"],
        unique=False,
        postgresql_where=sa.text("processing_status = 3"),
        sqlite_where=sa.text("processing_status = 3"),
    )
    op.create_index(
        "ix_ark_failed_recent",
        "ark_records",
        ["updated_at", "id"],
        unique=False,
        postgresql_where=sa.text("processing_status = 3"),
        sqlite_where=sa.text("processing_status = 3"),
    )
    # Unique partial index for active client_item_id idempotency (from 0003)
    predicate = sa.text("client_item_id IS NOT NULL AND state != 'T'")
    op.create_index(
        "uq_ark_records_active_client_item",
        "ark_records",
        ["authority_id", "naan", "client_item_id"],
        unique=True,
        postgresql_where=predicate,
        sqlite_where=predicate,
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
        sa.Column("next_wake_at", sa.DateTime(), nullable=True),
        sa.Column("last_cycle_duration_seconds", sa.Float(), nullable=True),
        sa.Column("last_cycle_processed", sa.Integer(), nullable=True),
        sa.Column("last_cycle_succeeded", sa.Integer(), nullable=True),
        sa.Column("last_cycle_failed", sa.Integer(), nullable=True),
        sa.Column("last_reconciliation_at", sa.DateTime(), nullable=True),
        sa.Column("last_reconciliation_checked", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_reconciliation_advanced", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_reconciliation_waiting", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_reconciliation_repaired", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_reconciliation_purged", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_reconciliation_failed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_replication_promotions_accepted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_replication_confirmed_cids", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_replication_pending_cids", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_replication_batch_latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_no_progress_cycles", sa.SmallInteger(), server_default="0", nullable=False),
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

    # ark_metadata table (includes original_media_type from 0002)
    op.create_table(
        "ark_metadata",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ark_record_id", sa.Integer(), nullable=False),
        sa.Column("level1_json", sa.JSON(), nullable=True),
        sa.Column("level1_cid", sa.String(length=100), nullable=True),
        sa.Column("original_content", sa.Text(), nullable=True),
        sa.Column("original_schema", sa.String(length=50), nullable=False),
        sa.Column("original_media_type", sa.String(length=255), nullable=True),
        sa.Column("original_cid", sa.String(length=100), nullable=True),
        sa.Column("level1_replica_count", sa.SmallInteger(), nullable=True),
        sa.Column("level2_replica_count", sa.SmallInteger(), nullable=True),
        sa.Column("replication_checked_at", sa.DateTime(), nullable=True),
        sa.Column("replication_observation_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("replication_error_code", sa.SmallInteger(), nullable=True),
        sa.Column("replication_last_error", sa.Text(), nullable=True),
        sa.Column("last_repair_at", sa.DateTime(), nullable=True),
        sa.Column("payload_purged_at", sa.DateTime(), nullable=True),
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
        sa.ForeignKeyConstraint(["replication_error_code"], ["processing_error_codes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ark_record_id"),
    )
    op.create_index(op.f("ix_ark_metadata_ark_record_id"), "ark_metadata", ["ark_record_id"], unique=False)
    op.create_index(
        op.f("ix_ark_metadata_replication_checked_at"),
        "ark_metadata",
        ["replication_checked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ark_metadata_replication_checked_at"), table_name="ark_metadata")
    op.drop_index(op.f("ix_ark_metadata_ark_record_id"), table_name="ark_metadata")
    op.drop_table("ark_metadata")
    op.drop_index(op.f("ix_worker_runtime_status_last_heartbeat_at"), table_name="worker_runtime_status")
    op.drop_table("worker_runtime_status")
    op.drop_table("noid_counters")
    op.drop_index("uq_ark_records_active_client_item", table_name="ark_records")
    op.drop_index("ix_ark_failed_recent", table_name="ark_records")
    op.drop_index("ix_ark_failed_summary", table_name="ark_records")
    op.drop_index("ix_ark_detail_active_rollup", table_name="ark_records")
    op.drop_index("ix_ark_processing_queue", table_name="ark_records")
    op.drop_index("ix_state_authority", table_name="ark_records")
    op.drop_index("ix_naan_name", table_name="ark_records")
    op.drop_index(op.f("ix_ark_records_state"), table_name="ark_records")
    op.drop_index(op.f("ix_ark_records_created_at"), table_name="ark_records")
    op.drop_index(op.f("ix_ark_records_authority_id"), table_name="ark_records")
    op.drop_table("ark_records")
    op.drop_table("processing_error_codes")
    op.drop_table("processing_wait_reasons")
    op.drop_table("processing_statuses")
    op.drop_table("processing_stages")
