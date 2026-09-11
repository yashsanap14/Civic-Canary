import io
from datetime import UTC, datetime

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.stub import ANY, Stubber

from agent.civic_canary.models import Finding, FindingStatus, PortalTarget, Run
from services.storage import ExistingAwsStore
from services.store_factory import default_store


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("CIVIC_CANARY_MODE", "aws")
    monkeypatch.setenv("AWS_STORAGE_LAYOUT", "existing")
    for name in ("SITES_TABLE", "FINDINGS_TABLE", "REVIEWS_TABLE", "EVIDENCE_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    return default_store()


def wire(item):
    return {key: TypeSerializer().serialize(value) for key, value in item.items()}


def finding():
    return Finding(
        finding_id="finding-1", run_id="run-1", target_id="benefits-demo",
        title="Changed requirement", category="REQUIREMENT", severity="HIGH",
        materiality="MATERIAL", evidence=["New document"], affected_playbook_sections=["Documents"],
        proposed_patch="Add document.",
    )


def test_existing_factory_and_site_roundtrip(store):
    assert isinstance(store, ExistingAwsStore)
    assert store.bucket == "civic-canary"
    assert store.targets.name == "CivicCanarySites"
    assert store.reviews.name == "CivicCanaryReviews"
    target = PortalTarget()
    item = store._target_item(target)
    assert item["siteId"] == target.target_id
    assert "target_id" not in item
    with Stubber(store.targets.meta.client) as stub:
        stub.add_response("put_item", {}, {
            "TableName": "CivicCanarySites", "Item": item,
        })
        stub.add_response("get_item", {"Item": wire(item)}, {
            "TableName": "CivicCanarySites", "Key": {"siteId": target.target_id},
            "ConsistentRead": True,
        })
        store.put_target(target)
        assert store.get_target(target.target_id) == target
        stub.assert_no_pending_responses()


def test_finding_keys_dedup_and_paginated_scan(store):
    model = finding()
    item = store._finding_item(model)
    assert "finding_id" not in item
    assert item["findingId"] == model.finding_id
    with Stubber(store.findings.meta.client) as stub:
        params = {"TableName": "CivicCanaryFindings", "Item": item,
                  "ConditionExpression": "attribute_not_exists(findingId)"}
        stub.add_response("put_item", {}, params)
        stub.add_client_error("put_item", "ConditionalCheckFailedException",
                              expected_params=params)
        stub.add_response("scan", {
            "Items": [wire(item)], "LastEvaluatedKey": wire({"findingId": model.finding_id}),
        }, {"TableName": "CivicCanaryFindings"})
        stub.add_response("scan", {"Items": []}, {
            "TableName": "CivicCanaryFindings",
            "ExclusiveStartKey": {"findingId": model.finding_id},
        })
        assert store.put_finding(model)
        assert not store.put_finding(model)
        assert store.list_findings() == [model]
        stub.assert_no_pending_responses()


def test_runs_use_s3_and_conditional_reservation(store):
    run = Run(run_id="run/a", target_id="benefits-demo", trigger_type="MANUAL")
    params = {"Bucket": "civic-canary", "Key": "runs/run%2Fa.json",
              "Body": run.model_dump_json().encode(), "ContentType": "application/json"}
    with Stubber(store.s3) as stub:
        stub.add_response("put_object", {}, {**params, "IfNoneMatch": "*"})
        stub.add_client_error("put_object", "PreconditionFailed",
                              expected_params={**params, "IfNoneMatch": "*"})
        stub.add_response("put_object", {}, params)
        stub.add_response("get_object", {"Body": io.BytesIO(run.model_dump_json().encode())},
                          {"Bucket": "civic-canary", "Key": "runs/run%2Fa.json"})
        assert store.create_run(run)
        assert not store.create_run(run)
        store.put_run(run)
        assert store.get_run(run.run_id) == run
        stub.assert_no_pending_responses()


def test_run_listing_paginates_and_preserves_failed_runs(store):
    run = Run(run_id="run-failed", target_id="benefits-demo", trigger_type="MANUAL",
              status="FAILED", error_category="TimeoutError")
    with Stubber(store.s3) as stub:
        stub.add_response("list_objects_v2", {
            "Contents": [{"Key": "runs/run-failed.json"}],
            "IsTruncated": True, "NextContinuationToken": "next",
        }, {"Bucket": "civic-canary", "Prefix": "runs/"})
        stub.add_response("get_object", {"Body": io.BytesIO(run.model_dump_json().encode())},
                          {"Bucket": "civic-canary", "Key": "runs/run-failed.json"})
        stub.add_response("list_objects_v2", {"IsTruncated": False},
                          {"Bucket": "civic-canary", "Prefix": "runs/",
                           "ContinuationToken": "next"})
        assert store.list_runs() == [run]
        stub.assert_no_pending_responses()


def test_approved_draft_uses_existing_bucket(store):
    with Stubber(store.s3) as stub:
        stub.add_response("put_object", {}, {
            "Bucket": "civic-canary", "Key": "approved/finding-1.md",
            "Body": ANY, "ContentType": "text/markdown",
        })
        assert store.write_approved_artifact(finding()) == "approved/finding-1.md"
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("state,expected,action", [
    (FindingStatus.APPROVED, FindingStatus.APPROVAL_PENDING, "APPROVE"),
    (FindingStatus.REJECTED, FindingStatus.OPEN, "REJECT"),
])
def test_review_and_finding_commit_in_one_transaction(store, monkeypatch, state, expected, action):
    model = finding().model_copy(update={
        "status": state, "decision_note": "Verified", "decided_at": datetime.now(UTC),
    })
    captured = []
    monkeypatch.setattr(store.ddb, "transact_write_items",
                        lambda **kwargs: captured.append(kwargs))
    assert store.transition_finding(model, expected)
    updates = captured[0]["TransactItems"]
    assert len(updates) == 2
    assert updates[0]["Put"]["TableName"] == "CivicCanaryFindings"
    assert updates[0]["Put"]["Item"]["findingId"] == {"S": "finding-1"}
    assert updates[0]["Put"]["ExpressionAttributeValues"][":expected"] == {"S": expected.value}
    review = updates[1]["Put"]
    assert review["TableName"] == "CivicCanaryReviews"
    decoded = {k: TypeDeserializer().deserialize(v) for k, v in review["Item"].items()}
    assert decoded["reviewId"] == "review-finding-1"
    assert decoded["findingId"] == "finding-1"
    assert decoded["action"] == action


def test_transaction_failures_are_not_silently_accepted(store):
    model = finding().model_copy(update={"status": FindingStatus.REJECTED})
    with Stubber(store.ddb) as stub:
        stub.add_client_error(
            "transact_write_items", "TransactionCanceledException",
            expected_params={"TransactItems": ANY},
            modeled_fields={"CancellationReasons": [{"Code": "ConditionalCheckFailed"}]},
        )
        assert not store.transition_finding(model, FindingStatus.OPEN)
        stub.add_client_error("transact_write_items", "AccessDeniedException",
                              expected_params={"TransactItems": ANY})
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError):
            store.transition_finding(model, FindingStatus.OPEN)
