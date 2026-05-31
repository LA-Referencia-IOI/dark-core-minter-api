"""Add active client item idempotency for batch ARK reservations.

Revision ID: 0003_add_active_client_item_idempotency
Revises: 0002_add_original_media_type
Create Date: 2026-05-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_add_active_client_item_idempotency"
down_revision: Union[str, None] = "0002_add_original_media_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "uq_ark_records_active_client_item"
TOMBSTONE_STATE = "T"


def _find_active_client_item_duplicates(bind, limit: int = 20):
    return (
        bind.execute(
            sa.text(
                """
                SELECT authority_id, naan, client_item_id, COUNT(*) AS duplicate_count
                FROM ark_records
                WHERE client_item_id IS NOT NULL
                  AND state != :tombstone_state
                GROUP BY authority_id, naan, client_item_id
                HAVING COUNT(*) > 1
                ORDER BY duplicate_count DESC, authority_id, naan, client_item_id
                LIMIT :limit
                """
            ),
            {"tombstone_state": TOMBSTONE_STATE, "limit": limit},
        )
        .mappings()
        .all()
    )


def _format_duplicate_error(duplicates) -> str:
    examples = "; ".join(
        (
            f"authority_id={row['authority_id']}, naan={row['naan']}, "
            f"client_item_id={row['client_item_id']}, count={row['duplicate_count']}"
        )
        for row in duplicates
    )
    return (
        "Cannot create active client_item_id idempotency index because duplicate "
        "active ARK reservations already exist. Resolve duplicates manually first. "
        f"Examples: {examples}"
    )


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = _find_active_client_item_duplicates(bind)
    if duplicates:
        raise RuntimeError(_format_duplicate_error(duplicates))

    predicate = sa.text("client_item_id IS NOT NULL AND state != 'T'")
    op.create_index(
        INDEX_NAME,
        "ark_records",
        ["authority_id", "naan", "client_item_id"],
        unique=True,
        postgresql_where=predicate,
        sqlite_where=predicate,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="ark_records")
