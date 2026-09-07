"""Unit coverage for worker policy that does not require a database server."""

from unittest.mock import Mock

from dark_core_lib import ARKPublishResult

from app.workers.publisher import ChainPublisherWorker, ReplicationReconciliationWorker
from app.workers.recovery import derive_recovery_stage
from app.models.processing import ProcessingStage
from app.models.states import ARKState


def test_replication_policy_never_allows_purge_before_publication():
    worker = ReplicationReconciliationWorker(
        Mock(), publish_after_replicas=2, target_replicas=1
    )
    assert worker.publish_after == 2
    assert worker.target_replicas == 2


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


def test_recovery_derives_stage_from_local_evidence_only():
    metadata = Mock(level1_cid="l1", original_cid="l2", level1_json={"title": "x"})
    draft = Mock(state=ARKState.DRAFT.value)
    published = Mock(state=ARKState.PUBLISHED.value)

    assert derive_recovery_stage(draft, metadata)[0] == ProcessingStage.AVAILABILITY
    assert derive_recovery_stage(published, metadata)[0] == ProcessingStage.REPLICATION

    metadata.original_cid = None
    metadata.original_content = "raw"
    metadata.original_media_type = "application/xml"
    assert derive_recovery_stage(draft, metadata)[0] == ProcessingStage.METADATA
