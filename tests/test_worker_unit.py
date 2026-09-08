"""Unit coverage for worker policy that does not require a database server."""

from unittest.mock import Mock
from datetime import datetime, timezone

from dark_core_lib import ARKPublishResult

from app.workers.publisher import ChainPublisherWorker, ReplicationReconciliationWorker
from app.models.processing import ProcessingWaitReason


def test_replication_policy_never_allows_purge_before_publication():
    worker = ReplicationReconciliationWorker(
        Mock(), publish_after_replicas=2, target_replicas=1
    )
    assert worker.publish_after == 2
    assert worker.target_replicas == 2


def test_cluster_wait_is_scheduled_with_bounded_deterministic_backoff():
    worker = ReplicationReconciliationWorker(
        Mock(), pinning_recheck_seconds=2, max_recheck_seconds=10
    )
    first = worker._wait_schedule(
        "ark:12345/2000000x", ProcessingWaitReason.CLUSTER_PINNING,
        int(ProcessingWaitReason.NONE), 0, 0, 1,
    )
    repeated = worker._wait_schedule(
        "ark:12345/2000000x", ProcessingWaitReason.CLUSTER_PINNING,
        int(ProcessingWaitReason.CLUSTER_PINNING), 0, 0, 4,
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert 1 <= (first - now).total_seconds() <= 3
    assert 7 <= (repeated - now).total_seconds() <= 11


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
