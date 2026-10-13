"""The consolidated initial schema owns client-item idempotency."""

from pathlib import Path


def test_initial_schema_contains_active_client_item_partial_index():
    migration = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0001_initial_schema.py"
    ).read_text(encoding="utf-8")

    assert "uq_ark_records_active_client_item" in migration
    assert "client_item_id IS NOT NULL AND state != 'T'" in migration


def test_schema_migrations_form_one_upgrade_path():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0004_remove_admin_coordination"]
    assert [revision.revision for revision in scripts.walk_revisions()] == [
        "0004_remove_admin_coordination", "0003_authority_retirement", "0002_authority_request_nonces", "0001_initial_schema"
    ]
