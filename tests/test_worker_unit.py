"""Unit coverage for worker policy that does not require a database server."""

from unittest.mock import Mock
from datetime import datetime, timezone

from dark_core_lib import ARKPublishResult

from app.workers.publisher import ChainPublisherWorker, ReplicationReconciliationWorker


def test_replication_policy_never_allows_purge_before_publication():
    worker = ReplicationReconciliationWorker(
        Mock(), publish_after_replicas=2, target_replicas=1
    )
    assert worker.publish_after == 2
    assert worker.target_replicas == 2


def test_cluster_wait_uses_the_explicit_first_pin_cadence():
    worker = ReplicationReconciliationWorker(
        Mock(),
        first_pin_recheck_seconds=15,
        first_pin_second_recheck_seconds=60,
        first_pin_max_recheck_seconds=300,
        durability_recheck_seconds=300,
        durability_second_recheck_seconds=900,
        durability_max_recheck_seconds=3600,
    )
    first = worker._first_pin_wait_at(1)
    second = worker._first_pin_wait_at(2)
    later = worker._first_pin_wait_at(3)
    durability_first = worker._durability_wait_at(1)
    durability_second = worker._durability_wait_at(2)
    durability_later = worker._durability_wait_at(3)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert 14 <= (first - now).total_seconds() <= 16
    assert 59 <= (second - now).total_seconds() <= 61
    assert 299 <= (later - now).total_seconds() <= 301
    assert 299 <= (durability_first - now).total_seconds() <= 301
    assert 899 <= (durability_second - now).total_seconds() <= 901
    assert 3599 <= (durability_later - now).total_seconds() <= 3601


def test_chain_reconciliation_requires_exact_target_and_level1_cid():
    core = Mock()
    core.get_ark.return_value = Mock(url="https://example.org/object", cid="bafy-l1")
    worker = ChainPublisherWorker(core)

    assert worker._chain_matches("12345", "2000000x", "https://example.org/object", "bafy-l1")
    assert not worker._chain_matches("12345", "2000000x", "https://example.org/other", "bafy-l1")


def test_uncertain_pipeline_result_is_deferred_without_consuming_retry_budget():
    core = Mock()
    # The ARK was not created, so it is safe to retry it later.
    from dark_core_lib.exceptions import ARKNotFoundError

    core.get_ark.side_effect = ARKNotFoundError("not found")
    worker = ChainPublisherWorker(core)
    repo = Mock()
    item = {
        "ark": "ark:12345/2000000x",
        "target": "https://example.org/object",
        "cid": "bafy-l1",
    }

    published = worker._handle_pipeline_result(
        repo,
        item,
        ARKPublishResult(
            ref=item["ark"], action="create", status="not_sent", error="nonce gap"
        ),
    )

    assert not published
    repo.defer_processing_retry.assert_called_once()
    repo.mark_processing_failed.assert_not_called()


def test_reverted_pipeline_result_remains_permanent_after_exact_chain_check():
    core = Mock()
    from dark_core_lib.exceptions import ARKNotFoundError

    core.get_ark.side_effect = ARKNotFoundError("not found")
    worker = ChainPublisherWorker(core)
    repo = Mock()
    item = {
        "ark": "ark:12345/2000000x",
        "target": "https://example.org/object",
        "cid": "bafy-l1",
    }

    published = worker._handle_pipeline_result(
        repo,
        item,
        ARKPublishResult(ref=item["ark"], action="create", status="reverted", error="revert"),
    )

    assert not published
    repo.mark_processing_failed.assert_called_once()
    repo.defer_processing_retry.assert_not_called()
