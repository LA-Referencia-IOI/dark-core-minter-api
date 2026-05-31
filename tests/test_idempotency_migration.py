"""
Tests for the active client item idempotency Alembic migration.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError


def _load_migration():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0003_add_active_client_item_idempotency.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0003", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_ark_records_table(conn):
    metadata = sa.MetaData()
    sa.Table(
        "ark_records",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("authority_id", sa.String(255), nullable=False),
        sa.Column("naan", sa.String(50), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("state", sa.String(1), nullable=False),
        sa.Column("client_item_id", sa.String(100), nullable=True),
    )
    metadata.create_all(conn)


def _run_upgrade(conn, migration, monkeypatch):
    context = MigrationContext.configure(conn)
    operations = Operations(context)
    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(
            get_bind=lambda: conn,
            create_index=operations.create_index,
            drop_index=operations.drop_index,
        ),
    )
    migration.upgrade()


def test_idempotency_migration_fails_when_active_duplicates_exist(monkeypatch):
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        _create_ark_records_table(conn)
        conn.execute(
            sa.text(
                """
                INSERT INTO ark_records (authority_id, naan, name, state, client_item_id)
                VALUES
                  ('authority-1', '12345', 'one', 'R', 'oai:repo:1'),
                  ('authority-1', '12345', 'two', 'P', 'oai:repo:1')
                """
            )
        )

        with pytest.raises(RuntimeError, match="duplicate active ARK reservations"):
            _run_upgrade(conn, migration, monkeypatch)


def test_idempotency_migration_allows_tombstoned_duplicates_and_creates_partial_index(monkeypatch):
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        _create_ark_records_table(conn)
        conn.execute(
            sa.text(
                """
                INSERT INTO ark_records (authority_id, naan, name, state, client_item_id)
                VALUES
                  ('authority-1', '12345', 'old', 'T', 'oai:repo:1'),
                  ('authority-1', '12345', 'active', 'R', 'oai:repo:1')
                """
            )
        )

        _run_upgrade(conn, migration, monkeypatch)

        conn.execute(
            sa.text(
                """
                INSERT INTO ark_records (authority_id, naan, name, state, client_item_id)
                VALUES ('authority-1', '12345', 'second-tombstone', 'T', 'oai:repo:1')
                """
            )
        )

        with pytest.raises(IntegrityError):
            conn.execute(
                sa.text(
                    """
                    INSERT INTO ark_records (authority_id, naan, name, state, client_item_id)
                    VALUES ('authority-1', '12345', 'duplicate-active', 'D', 'oai:repo:1')
                    """
                )
            )
