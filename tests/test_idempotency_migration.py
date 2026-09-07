"""The consolidated initial schema owns client-item idempotency."""

from pathlib import Path


def test_initial_schema_contains_active_client_item_partial_index():
    migration = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0001_initial_schema.py"
    ).read_text(encoding="utf-8")

    assert "uq_ark_records_active_client_item" in migration
    assert "client_item_id IS NOT NULL AND state != 'T'" in migration


def test_new_installation_has_one_consolidated_schema_migration():
    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    assert [path.name for path in versions.glob("*.py")] == ["0001_initial_schema.py"]
