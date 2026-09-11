from __future__ import annotations

import io
from unittest.mock import MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError

from agent.civic_canary.models import (
    Finding,
    PortalTarget,
    Run,
    RunStatus,
    Severity,
    TriggerType,
)
from services.storage import AwsStore, InMemoryStore


@pytest.fixture
def mock_dynamodb_tables():
    with patch("services.storage.boto3.resource") as mock_resource:
        mock_dynamo = MagicMock()
        mock_resource.return_value = mock_dynamo
        mock_sites_table = MagicMock()
        mock_sites_table.name = "CivicCanarySites"
        mock_findings_table = MagicMock()
        mock_findings_table.name = "CivicCanaryFindings"
        mock_reviews_table = MagicMock()
        mock_reviews_table.name = "CivicCanaryReviews"

        def get_table(name: str):
            if name == "CivicCanarySites":
                return mock_sites_table
            if name == "CivicCanaryFindings":
                return mock_findings_table
            if name == "CivicCanaryReviews":
                return mock_reviews_table
            mock = MagicMock()
            mock.name = name
            return mock

        mock_dynamo.Table.side_effect = get_table
        yield {
            "dynamo": mock_dynamo,
            "sites": mock_sites_table,
            "findings": mock_findings_table,
            "reviews": mock_reviews_table,
        }


@pytest.fixture
def mock_s3_client():
    with patch("services.storage.boto3.client") as mock_client:
        mock_s3 = MagicMock()
        mock_client.return_value = mock_s3
        yield mock_s3


def test_aws_store_constructor_accepts_new_table_names(
    mock_dynamodb_tables, mock_s3_client
) -> None:
    store = AwsStore(
        sites_table="CivicCanarySites",
        findings_table="CivicCanaryFindings",
        reviews_table="CivicCanaryReviews",
        evidence_bucket="civic-canary",
        region="us-east-1",
    )
    assert store.sites.name == "CivicCanarySites"
    assert store.findings.name == "CivicCanaryFindings"
    assert store.reviews.name == "CivicCanaryReviews"
    assert store.bucket == "civic-canary"
    assert store.region == "us-east-1"


def test_site_saved_to_dynamodb_using_site_id(
    mock_dynamodb_tables, mock_s3_client
) -> None:
    store = AwsStore(
        sites_table="CivicCanarySites",
        findings_table="CivicCanaryFindings",
        reviews_table="CivicCanaryReviews",
        evidence_bucket="civic-canary",
    )
    target = PortalTarget(target_id="benefits-portal-1", name="State Benefits")
    store.put_target(target)

    mock_dynamodb_tables["sites"].put_item.assert_called_once()
    call_item = mock_dynamodb_tables["sites"].put_item.call_args[1]["Item"]
    assert call_item["siteId"] == "benefits-portal-1"
    assert "target_id" not in call_item


def test_site_loaded_correctly_from_site_id(
    mock_dynamodb_tables, mock_s3_client
) -> None:
    store = AwsStore(
        sites_table="CivicCanarySites",
        findings_table="CivicCanaryFindings",
        reviews_table="CivicCanaryReviews",
        evidence_bucket="civic-canary",
    )
    mock_dynamodb_tables["sites"].get_item.return_value = {
        "Item": {
            "siteId": "benefits-portal-1",
            "name": "State Benefits",
            "start_url": "https://benefits.state.gov",
            "allowed_hosts": ["benefits.state.gov"],
            "journey_steps": [{"path": "/apply", "label": "Apply"}],
            "active_version": "v1",
            "enabled": True,
            "playbook_key": "playbooks/benefits/playbook.md",
            "baseline_version": "v1",
        }
    }

    loaded = store.get_target("benefits-portal-1")
    assert loaded is not None
    assert loaded.target_id == "benefits-portal-1"
    assert loaded.name == "State Benefits"
    mock_dynamodb_tables["sites"].get_item.assert_called_once_with(
        Key={"siteId": "benefits-portal-1"}
    )


def test_finding_saved_using_finding_id(
    mock_dynamodb_tables, mock_s3_client
) -> None:
    store = AwsStore(
        sites_table="CivicCanarySites",
        findings_table="CivicCanaryFindings",
        reviews_table="CivicCanaryReviews",
        evidence_bucket="civic-canary",
    )
    finding = Finding(
        finding_id="finding-abc-123",
        run_id="run-1",
        target_id="benefits-demo",
        title="Broken link on documents page",
        category="BROKEN_LINK",
        severity=Severity.HIGH,
        materiality="MATERIAL",
        evidence=["404 observed"],
        affected_playbook_sections=["Overview"],
        proposed_patch="Fix link",
    )
    created = store.put_finding(finding)
    assert created is True

    mock_dynamodb_tables["findings"].put_item.assert_called_once()
    call_args = mock_dynamodb_tables["findings"].put_item.call_args[1]
    assert call_args["Item"]["findingId"] == "finding-abc-123"
    assert "finding_id" not in call_args["Item"]
    assert call_args["ConditionExpression"] == "attribute_not_exists(findingId)"


def test_finding_loaded_from_finding_id(
    mock_dynamodb_tables, mock_s3_client
) -> None:
    store = AwsStore(
        sites_table="CivicCanarySites",
        findings_table="CivicCanaryFindings",
        reviews_table="CivicCanaryReviews",
        evidence_bucket="civic-canary",
    )
    mock_dynamodb_tables["findings"].get_item.return_value = {
        "Item": {
            "findingId": "finding-abc-123",
            "run_id": "run-1",
            "target_id": "benefits-demo",
            "title": "Broken link",
            "category": "BROKEN_LINK",
            "severity": "HIGH",
            "materiality": "MATERIAL",
            "evidence": ["404 observed"],
            "affected_playbook_sections": ["Overview"],
            "proposed_patch": "Fix link",
            "status": "OPEN",
            "created_at": "2026-09-10T12:00:00Z",
        }
    }

    finding = store.get_finding("finding-abc-123")
    assert finding is not None
    assert finding.finding_id == "finding-abc-123"
    mock_dynamodb_tables["findings"].get_item.assert_called_once_with(
        Key={"findingId": "finding-abc-123"}
    )


@pytest.mark.parametrize("action", ["APPROVED", "REJECTED"])
def test_review_saved_using_review_id(
    mock_dynamodb_tables, mock_s3_client, action
) -> None:
    store = AwsStore(
        sites_table="CivicCanarySites",
        findings_table="CivicCanaryFindings",
        reviews_table="CivicCanaryReviews",
        evidence_bucket="civic-canary",
    )
    review_id = store.record_review(
        finding_id="finding-abc-123",
        action=action,
        note="Approved after checking source evidence.",
        reviewer="auditor@civiccanary.gov",
    )
    assert review_id.startswith("rev-")
    mock_dynamodb_tables["reviews"].put_item.assert_called_once()
    call_item = mock_dynamodb_tables["reviews"].put_item.call_args[1]["Item"]
    assert call_item["reviewId"] == review_id
    assert call_item["findingId"] == "finding-abc-123"
    assert call_item["action"] == action
    assert call_item["reviewedAt"]
    assert call_item["reviewer"] == "auditor@civiccanary.gov"
    assert call_item["notes"] == "Approved after checking source evidence."


def test_runs_stored_and_retrieved_via_s3_without_runs_table(
    mock_dynamodb_tables, mock_s3_client
) -> None:
    store = AwsStore(
        sites_table="CivicCanarySites",
        findings_table="CivicCanaryFindings",
        reviews_table="CivicCanaryReviews",
        evidence_bucket="civic-canary",
    )
    run = Run(
        run_id="run-test-s3-100",
        target_id="benefits-demo",
        trigger_type=TriggerType.MANUAL,
        status=RunStatus.SUCCEEDED,
    )
    store.put_run(run)

    # Verify S3 put_object called for runs/
    mock_s3_client.put_object.assert_called_once()
    put_args = mock_s3_client.put_object.call_args[1]
    assert put_args["Bucket"] == "civic-canary"
    assert put_args["Key"] == "runs/run-test-s3-100.json"
    assert put_args["ContentType"] == "application/json"

    # Verify get_run reads from S3
    mock_s3_client.get_object.return_value = {
        "Body": io.BytesIO(run.model_dump_json().encode())
    }
    loaded = store.get_run("run-test-s3-100")
    assert loaded is not None
    assert loaded.run_id == "run-test-s3-100"
    mock_s3_client.get_object.assert_called_with(
        Bucket="civic-canary", Key="runs/run-test-s3-100.json"
    )


def test_in_memory_store_full_workflow() -> None:
    store = InMemoryStore(playbook="# Playbook")
    target = PortalTarget(target_id="test-site")
    store.put_target(target)
    assert store.get_target("test-site") is not None

    finding = Finding(
        finding_id="f-1",
        run_id="r-1",
        target_id="test-site",
        title="Title",
        category="REQUIREMENT",
        severity=Severity.LOW,
        materiality="COSMETIC",
        evidence=["Evidence"],
        affected_playbook_sections=["Section"],
        proposed_patch="Patch",
    )
    store.put_finding(finding)
    assert store.get_finding("f-1") is not None

    review_id = store.record_review("f-1", "APPROVED", note="Looks good")
    assert review_id in store.reviews
    assert store.reviews[review_id]["action"] == "APPROVED"


@pytest.fixture
def aws_store(mock_dynamodb_tables, mock_s3_client):
    return AwsStore("CivicCanarySites", "CivicCanaryFindings", "CivicCanaryReviews", "civic-canary")


def test_only_existing_tables_opened(aws_store, mock_dynamodb_tables):
    assert mock_dynamodb_tables["dynamo"].Table.call_args_list == [
        call("CivicCanarySites"), call("CivicCanaryFindings"), call("CivicCanaryReviews")
    ]


def test_run_creation_is_conditional(aws_store, mock_s3_client):
    run = Run(run_id="r1", target_id="demo", trigger_type=TriggerType.MANUAL)
    assert aws_store.create_run(run)
    assert mock_s3_client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    mock_s3_client.get_object.assert_not_called()
    mock_s3_client.put_object.side_effect = ClientError(
        {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
    )
    assert not aws_store.create_run(run)
    mock_s3_client.put_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "PutObject"
    )
    with pytest.raises(ClientError):
        aws_store.create_run(run)


def test_recent_runs_paginated_and_sorted(aws_store, mock_s3_client):
    runs = [Run(run_id=f"r{i}", target_id="demo", trigger_type=TriggerType.MANUAL,
                started_at=f"2026-09-10T0{i}:00:00Z") for i in (1, 2)]
    mock_s3_client.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "runs/r1.json"}, {"Key": "runs/"}]},
        {"Contents": [{"Key": "runs/r2.json"}]},
    ]
    mock_s3_client.get_object.side_effect = [
        {"Body": io.BytesIO(run.model_dump_json().encode())} for run in runs
    ]
    assert [run.run_id for run in aws_store.list_runs()] == ["r2", "r1"]


def test_site_scan_maps_keys_across_pages(aws_store, mock_dynamodb_tables):
    table = mock_dynamodb_tables["sites"]
    table.scan.side_effect = [
        {"Items": [{"siteId": "one"}], "LastEvaluatedKey": {"siteId": "one"}},
        {"Items": [{"siteId": "two"}]},
    ]
    assert [target.target_id for target in aws_store.list_targets()] == ["one", "two"]
    assert table.scan.call_args.kwargs == {"ExclusiveStartKey": {"siteId": "one"}}
