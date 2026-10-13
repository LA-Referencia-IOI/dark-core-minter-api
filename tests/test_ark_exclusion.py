"""Real PostgreSQL concurrency tests; use an isolated test database URL."""

import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api import arks, worker as worker_api
from app.database import ark_locks
from app.database.models import Base, ARKRecord, ARKMetadata
from app.dependencies import get_corelib_client, get_metadata_storage, get_db
from app.middleware.auth import require_authority_identity
from app.models.processing import ProcessingStage, ProcessingStatus, ProcessingErrorCode, ProcessingWaitReason
from app.repositories.ark_repository import ARKRepository
from app.workers.publisher import MetadataPersistenceWorker, ReplicationReconciliationWorker, ChainPublisherWorker

ARK = "ark:12345/exclusion-test"


@pytest.fixture
def engine(monkeypatch):
    url = os.getenv("ARK_LOCK_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ARK_LOCK_TEST_DATABASE_URL is required for real PostgreSQL locks")
    admin = create_engine(url)
    schema = "ark_lock_test_" + uuid4().hex
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"}, pool_size=4)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for name, enum in (("processing_stages", ProcessingStage), ("processing_statuses", ProcessingStatus), ("processing_wait_reasons", ProcessingWaitReason), ("processing_error_codes", ProcessingErrorCode)):
            table = Base.metadata.tables[name]
            values = [{"id": int(value), "code": value.name.lower(), "description": value.name} for value in enum]
            if name == "processing_error_codes":
                for row in values:
                    row["retryable"] = row["id"] in (100, 101, 200, 201, 300, 900)
            conn.execute(table.insert(), values)
    monkeypatch.setattr(ark_locks, "get_engine", lambda: engine)
    yield engine
    engine.dispose()
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def seed(engine, state="P", stage=ProcessingStage.REPLICATION, status=ProcessingStatus.READY):
    with Session(engine) as db:
        record = ARKRecord(naan="12345", name="exclusion-test", state=state, authority_id="authority", target="https://example.org", processing_stage=int(stage), processing_status=int(status))
        db.add(record)
        db.flush()
        db.add(ARKMetadata(ark_record_id=record.id, level1_json={"title": "old"}, original_content="old", original_schema="dc", original_media_type="text/plain", level1_cid="l1", original_cid="l2"))
        db.commit()


@pytest.fixture
def client(engine, monkeypatch):
    app = FastAPI()
    app.include_router(arks.router, prefix="/arks")
    app.include_router(worker_api.router, prefix="/worker")
    app.dependency_overrides[require_authority_identity] = lambda: {"authority_id": "authority"}
    core = Mock()
    core.config.default_gas_limit = 100000
    app.dependency_overrides[get_corelib_client] = lambda: core
    app.dependency_overrides[get_metadata_storage] = lambda: Mock()
    def database():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(arks, "_validate_ark_checkdigit_if_enabled", lambda *a: None)
    with TestClient(app) as client:
        yield client


def payload():
    return {"authority_id": "authority", "target": "https://example.org/new", "minimal_metadata": {"title": "new", "authors": ["Author"], "year": 2026}, "original_metadata": "new", "metadata_schema": "dc", "metadata_media_type": "text/plain"}


def test_lock_survives_commit_and_rollback_and_is_released(engine):
    with ark_locks.acquire_ark_lock(ARK) as db:
        pid = db.scalar(text("SELECT pg_backend_pid()"))
        for finish in (db.commit, db.rollback):
            finish()
            assert db.scalar(text("SELECT pg_backend_pid()")) == pid
            with ark_locks.acquire_ark_lock(ARK) as other:
                assert other is None
        with ark_locks.acquire_ark_lock(ARK + "-other") as other:
            assert other is not None
    with ark_locks.acquire_ark_lock(ARK) as db:
        assert db is not None
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid IN (SELECT pid FROM pg_stat_activity WHERE datname=current_database())")) == 0


def test_uncommitted_changes_rollback_on_exit(engine):
    seed(engine)
    with pytest.raises(ValueError):
        with ark_locks.acquire_ark_lock(ARK) as db:
            db.execute(text("UPDATE ark_records SET target='uncommitted'"))
            raise ValueError("worker failed")
    with Session(engine) as db:
        assert ARKRepository(db).get_by_ark(ARK).target == "https://example.org"
    with ark_locks.acquire_ark_lock(ARK) as db:
        assert db is not None


def test_connection_loss_releases_lock_and_prevents_reconnect(engine):
    with ark_locks.acquire_ark_lock(ARK) as db:
        pid = db.scalar(text("SELECT pg_backend_pid()"))
        db.commit()
        with engine.begin() as admin:
            admin.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        with pytest.raises(DBAPIError):
            db.execute(text("SELECT 1"))
        db.rollback()
        with pytest.raises(RuntimeError, match="connection was lost"):
            db.execute(text("SELECT 1"))
        with ark_locks.acquire_ark_lock(ARK) as replacement:
            assert replacement is not None


def test_api_conflicts_with_worker_then_update_succeeds(engine, client):
    seed(engine)
    with ark_locks.acquire_ark_lock(ARK):
        assert client.put(f"/arks/{ARK}", json=payload()).status_code == 409
        assert client.delete(f"/arks/{ARK}").status_code == 409
    response = client.put(f"/arks/{ARK}", json=payload())
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "U"
    assert client.put(f"/arks/{ARK}", json=payload()).status_code == 409
    with Session(engine) as db:
        record = ARKRepository(db).get_by_ark(ARK)
        assert record.processing_stage == int(ProcessingStage.METADATA)
        assert ARKRepository(db).get_metadata_by_ark_id(record.id).original_content == "new"


@pytest.mark.parametrize("state", ["D", "U", "T"])
def test_update_rejects_nonpublished(engine, client, state):
    seed(engine, state=state)
    assert client.put(f"/arks/{ARK}", json=payload()).status_code == 409


def test_reserved_accepts_first_metadata(engine, client):
    seed(engine, state="R", stage=ProcessingStage.NONE)
    response = client.put(f"/arks/{ARK}", json=payload())
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "D"


def test_workers_recheck_stage_and_retry_after_lock(engine):
    seed(engine, state="U", stage=ProcessingStage.METADATA)
    storage = Mock()
    core = Mock()
    assert not ChainPublisherWorker(core).publish_single_ark(ARK)
    assert ReplicationReconciliationWorker(storage)._reconcile(ARK)["checked"] == 0
    core.update_ark.assert_not_called()
    storage.get_replication_status.assert_not_called()
    with Session(engine) as db:
        record = ARKRepository(db).get_by_ark(ARK)
        record.processing_status = int(ProcessingStatus.WAITING)
        record.next_action_at = datetime.utcnow() + timedelta(minutes=10)
        record.processing_wait_reason = int(ProcessingWaitReason.STORAGE_BACKOFF)
        db.commit()
    assert not MetadataPersistenceWorker(storage)._persist(ARK)
    storage.store_document.assert_not_called()


def test_reconciler_holds_lock_through_external_io(engine, client):
    seed(engine)
    storage = Mock()
    def observe(cid):
        assert client.put(f"/arks/{ARK}", json=payload()).status_code == 409
        return SimpleNamespace(total_replicas=2, status="pinned")
    storage.get_replication_statuses.side_effect = lambda cids: {cid: observe(cid) for cid in cids}
    result = ReplicationReconciliationWorker(storage)._reconcile(ARK)
    assert result["purged"] == 1
    assert client.put(f"/arks/{ARK}", json=payload()).status_code == 200


def test_async_cluster_pin_wait_is_a_normal_wait(engine):
    seed(engine, state="D", stage=ProcessingStage.AVAILABILITY)
    storage = Mock()
    storage.get_replication_statuses.side_effect = lambda cids: {cid: SimpleNamespace(total_replicas=1, status="pinned") for cid in cids}

    result = ReplicationReconciliationWorker(
        storage, first_pin_recheck_seconds=10, publish_after_replicas=2
    )._reconcile(ARK)

    assert result["failed"] == 0
    with Session(engine) as db:
        record = ARKRepository(db).get_by_ark(ARK)
        assert record.processing_stage == int(ProcessingStage.AVAILABILITY)
        assert record.processing_status == int(ProcessingStatus.WAITING)
        assert record.next_action_at is not None
        assert record.processing_wait_reason == int(ProcessingWaitReason.INITIAL_VISIBILITY)
        assert record.processing_error_code is None
    assert record.processing_error_detail is None


def test_cluster_pinning_never_repairs_payload_before_it_settles(engine):
    seed(engine, state="D", stage=ProcessingStage.AVAILABILITY)
    storage = Mock()
    storage.get_replication_statuses.side_effect = lambda cids: {cid: SimpleNamespace(total_replicas=0, status="pinning") for cid in cids}

    result = ReplicationReconciliationWorker(
        storage, first_pin_recheck_seconds=10, repair_grace_seconds=0
    )._reconcile(ARK)

    assert result["failed"] == 0
    storage.store_document.assert_not_called()
    with Session(engine) as db:
        record = ARKRepository(db).get_by_ark(ARK)
        assert record.processing_status == int(ProcessingStatus.WAITING)
        assert record.next_action_at is not None
        assert record.processing_wait_reason == int(ProcessingWaitReason.CLUSTER_PINNING)


def test_legacy_rescue_endpoint_is_removed(engine, client):
    seed(engine, state="D", stage=ProcessingStage.CHAIN, status=ProcessingStatus.FAILED)
    response = client.post("/worker/errors/rescue", json={"authority_id": "authority"})
    assert response.status_code == 404, response.text


def test_schema_has_no_revision():
    assert "content_revision" not in ARKRecord.__table__.columns


def test_classic_and_compact_ark_share_the_same_lock(engine):
    with ark_locks.acquire_ark_lock(ARK) as db:
        assert db is not None
        with ark_locks.acquire_ark_lock(ARK.replace('ark:', 'ark:/')) as other:
            assert other is None


def test_new_owner_archives_old_payload_transactionally(engine):
    # Exercise the real JSON insert and FK ordering on PostgreSQL.
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE superseded_ark_jobs (id SERIAL PRIMARY KEY, ark VARCHAR(255) NOT NULL, original_authority_id VARCHAR(255) NOT NULL, replacement_authority_id VARCHAR(255) NOT NULL, snapshot JSON NOT NULL, created_at TIMESTAMP DEFAULT now())'))
    seed(engine, state='U', stage=ProcessingStage.CHAIN)
    core = Mock()
    core.get_authority_by_uuid.return_value = SimpleNamespace(active=True, wallet_address='0xbbb')
    core.get_ark.return_value = SimpleNamespace(owner='0xbbb', url='https://example.org/on-chain', cid='chain-cid')
    core.is_authorized_for_naan.return_value = True
    for commit in (False, True):
        with ark_locks.acquire_ark_lock(ARK) as db:
            repo = ARKRepository(db)
            record = repo.get_by_ark(ARK)
            arks._reconcile_local_owner(core, db, repo, record, 'replacement', '12345', 'exclusion-test', ARK)
            assert record.authority_id == 'replacement'
            assert record.state == 'P'
            assert record.target == 'https://example.org/on-chain'
            assert repo.get_metadata_by_ark_id(record.id) is None
            audit = db.execute(text('SELECT original_authority_id, replacement_authority_id, snapshot FROM superseded_ark_jobs')).one()
            assert audit.original_authority_id == 'authority'
            assert audit.snapshot['metadata']['original_content'] == 'old'
            assert audit.snapshot['record']['authority_id'] == 'authority'
            if commit:
                db.commit()
            else:
                db.rollback()
                assert repo.get_by_ark(ARK).authority_id == 'authority'
                assert repo.get_metadata_by_ark_id(record.id).original_content == 'old'
                assert db.scalar(text('SELECT count(*) FROM superseded_ark_jobs')) == 0
    with Session(engine) as db:
        assert db.scalar(text('SELECT count(*) FROM superseded_ark_jobs')) == 1



def test_replication_unassigned_authority_returns_cycle_counters(engine, monkeypatch):
    seed(engine)
    monkeypatch.setattr('app.workers.publisher.get_settings', lambda: SimpleNamespace(authority_is_assigned=lambda uuid: False))
    result = ReplicationReconciliationWorker(Mock())._reconcile(ARK)
    assert result['failed'] == 1
    with Session(engine) as db:
        assert ARKRepository(db).get_by_ark(ARK).processing_status == int(ProcessingStatus.FAILED)
