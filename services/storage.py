from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal
from threading import RLock
from typing import Any
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

from agent.civic_canary.models import Finding, FindingStatus, PortalSnapshot, PortalTarget, Run


def _jsonable(model: Any) -> dict[str, Any]:
    return json.loads(model.model_dump_json())


def _to_dynamo(value: Any) -> Any:
    """Convert JSON-compatible values to DynamoDB-safe values."""
    if isinstance(value, list):
        return [_to_dynamo(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_dynamo(item) for key, item in value.items()}
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def _dynamo_item(model: Any) -> dict[str, Any]:
    return _to_dynamo(_jsonable(model))


def _from_dynamo(value: Any) -> Any:
    if isinstance(value, list):
        return [_from_dynamo(item) for item in value]
    if isinstance(value, dict):
        return {key: _from_dynamo(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return float(value) if value % 1 else int(value)
    return value


class Store(ABC):
    @abstractmethod
    def get_target(self, target_id: str) -> PortalTarget | None: ...

    @abstractmethod
    def list_targets(self) -> list[PortalTarget]: ...

    @abstractmethod
    def put_target(self, target: PortalTarget) -> None: ...

    @abstractmethod
    def get_baseline(self, target_id: str) -> PortalSnapshot | None: ...

    @abstractmethod
    def put_baseline(self, snapshot: PortalSnapshot) -> None: ...

    @abstractmethod
    def put_snapshot(self, snapshot: PortalSnapshot) -> None: ...

    @abstractmethod
    def put_run(self, run: Run) -> None: ...

    @abstractmethod
    def create_run(self, run: Run) -> bool: ...

    @abstractmethod
    def get_run(self, run_id: str) -> Run | None: ...

    @abstractmethod
    def list_runs(self) -> list[Run]: ...

    @abstractmethod
    def put_finding(self, finding: Finding) -> bool: ...

    @abstractmethod
    def get_finding(self, finding_id: str) -> Finding | None: ...

    @abstractmethod
    def transition_finding(
        self,
        finding: Finding,
        expected_status: FindingStatus,
    ) -> bool: ...

    @abstractmethod
    def list_findings(self) -> list[Finding]: ...

    @abstractmethod
    def write_approved_artifact(self, finding: Finding) -> str: ...

    @abstractmethod
    def load_playbook(self, key: str) -> str: ...

    @abstractmethod
    def evidence_url(self, key: str) -> str | None: ...


class InMemoryStore(Store):
    def __init__(self, playbook: str = "") -> None:
        self.targets: dict[str, PortalTarget] = {}
        self.baselines: dict[str, PortalSnapshot] = {}
        self.snapshots: dict[str, PortalSnapshot] = {}
        self.runs: dict[str, Run] = {}
        self.findings: dict[str, Finding] = {}
        self.artifacts: dict[str, str] = {}
        self.playbook = playbook
        self._lock = RLock()

    def get_target(self, target_id: str) -> PortalTarget | None:
        target = self.targets.get(target_id)
        return target.model_copy(deep=True) if target else None

    def list_targets(self) -> list[PortalTarget]:
        return [target.model_copy(deep=True) for target in self.targets.values()]

    def put_target(self, target: PortalTarget) -> None:
        with self._lock:
            self.targets[target.target_id] = target.model_copy(deep=True)

    def get_baseline(self, target_id: str) -> PortalSnapshot | None:
        baseline = self.baselines.get(target_id)
        return baseline.model_copy(deep=True) if baseline else None

    def put_baseline(self, snapshot: PortalSnapshot) -> None:
        with self._lock:
            self.baselines[snapshot.target_id] = snapshot.model_copy(deep=True)

    def put_snapshot(self, snapshot: PortalSnapshot) -> None:
        with self._lock:
            self.snapshots[snapshot.run_id] = snapshot.model_copy(deep=True)

    def put_run(self, run: Run) -> None:
        with self._lock:
            self.runs[run.run_id] = run.model_copy(deep=True)

    def create_run(self, run: Run) -> bool:
        with self._lock:
            if run.run_id in self.runs:
                return False
            self.runs[run.run_id] = run.model_copy(deep=True)
            return True

    def get_run(self, run_id: str) -> Run | None:
        run = self.runs.get(run_id)
        return run.model_copy(deep=True) if run else None

    def list_runs(self) -> list[Run]:
        return sorted(
            (run.model_copy(deep=True) for run in self.runs.values()),
            key=lambda run: run.started_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    def put_finding(self, finding: Finding) -> bool:
        with self._lock:
            existing = self.findings.get(finding.finding_id)
            if existing and finding.status == "OPEN":
                return False
            self.findings[finding.finding_id] = finding.model_copy(deep=True)
            return True

    def get_finding(self, finding_id: str) -> Finding | None:
        finding = self.findings.get(finding_id)
        return finding.model_copy(deep=True) if finding else None

    def transition_finding(
        self,
        finding: Finding,
        expected_status: FindingStatus,
    ) -> bool:
        with self._lock:
            existing = self.findings.get(finding.finding_id)
            if existing is None or existing.status != expected_status:
                return False
            self.findings[finding.finding_id] = finding.model_copy(deep=True)
            return True

    def list_findings(self) -> list[Finding]:
        return sorted(
            (finding.model_copy(deep=True) for finding in self.findings.values()),
            key=lambda finding: finding.created_at,
            reverse=True,
        )

    def write_approved_artifact(self, finding: Finding) -> str:
        key = f"approved/{finding.finding_id}.md"
        content = (
            f"# Approved Civic Canary patch\n\n"
            f"Finding: {finding.title}\n\n"
            f"{finding.proposed_patch}\n\n"
            "This artifact is a reviewed draft. It was not published automatically.\n"
        )
        self.artifacts[key] = content
        return key

    def load_playbook(self, key: str) -> str:
        return self.playbook

    def evidence_url(self, key: str) -> str | None:
        return None


class AwsStore(Store):
    target_key = "target_id"
    finding_key = "finding_id"

    def _target_item(self, target: PortalTarget) -> dict:
        item = _dynamo_item(target)
        item[self.target_key] = item.pop("target_id")
        return item

    def _finding_item(self, finding: Finding) -> dict:
        item = _dynamo_item(finding)
        item[self.finding_key] = item.pop("finding_id")
        return item

    def _target_model(self, item: dict) -> PortalTarget:
        return PortalTarget.model_validate(
            {**_from_dynamo(item), "target_id": item[self.target_key]}
        )

    def _finding_model(self, item: dict) -> Finding:
        return Finding.model_validate(
            {**_from_dynamo(item), "finding_id": item[self.finding_key]}
        )

    def __init__(
        self,
        *,
        targets_table: str,
        runs_table: str,
        findings_table: str,
        evidence_bucket: str,
        region: str = "us-east-1",
    ) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=region)
        self.targets = dynamodb.Table(targets_table)
        self.runs = dynamodb.Table(runs_table)
        self.findings = dynamodb.Table(findings_table)
        self.s3 = boto3.client("s3", region_name=region)
        self.bucket = evidence_bucket

    def get_target(self, target_id: str) -> PortalTarget | None:
        item = self.targets.get_item(
            Key={self.target_key: target_id}, ConsistentRead=True
        ).get("Item")
        return self._target_model(item) if item else None

    @staticmethod
    def _scan_all(table) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        options: dict[str, Any] = {}
        while True:
            response = table.scan(**options)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return items
            options["ExclusiveStartKey"] = last_key

    def list_targets(self) -> list[PortalTarget]:
        return [
            self._target_model(item)
            for item in self._scan_all(self.targets)
        ]

    def put_target(self, target: PortalTarget) -> None:
        self.targets.put_item(Item=self._target_item(target))

    def get_baseline(self, target_id: str) -> PortalSnapshot | None:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=f"baselines/{target_id}.json")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        return PortalSnapshot.model_validate_json(response["Body"].read())

    def put_baseline(self, snapshot: PortalSnapshot) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=f"baselines/{snapshot.target_id}.json",
            Body=snapshot.model_dump_json(indent=2).encode(),
            ContentType="application/json",
        )

    def put_snapshot(self, snapshot: PortalSnapshot) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=f"snapshots/{snapshot.target_id}/{snapshot.run_id}/snapshot.json",
            Body=snapshot.model_dump_json(indent=2).encode(),
            ContentType="application/json",
        )

    def put_run(self, run: Run) -> None:
        self.runs.put_item(Item=_dynamo_item(run))

    def create_run(self, run: Run) -> bool:
        try:
            self.runs.put_item(
                Item=_dynamo_item(run),
                ConditionExpression="attribute_not_exists(run_id)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get_run(self, run_id: str) -> Run | None:
        item = self.runs.get_item(Key={"run_id": run_id}).get("Item")
        return Run.model_validate(_from_dynamo(item)) if item else None

    def list_runs(self) -> list[Run]:
        return sorted(
            [Run.model_validate(_from_dynamo(item)) for item in self._scan_all(self.runs)],
            key=lambda run: run.started_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    def put_finding(self, finding: Finding) -> bool:
        if finding.status != "OPEN":
            self.findings.put_item(Item=self._finding_item(finding))
            return True
        try:
            self.findings.put_item(
                Item=self._finding_item(finding),
                ConditionExpression=f"attribute_not_exists({self.finding_key})",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get_finding(self, finding_id: str) -> Finding | None:
        item = self.findings.get_item(
            Key={self.finding_key: finding_id}, ConsistentRead=True
        ).get("Item")
        return self._finding_model(item) if item else None

    def transition_finding(
        self,
        finding: Finding,
        expected_status: FindingStatus,
    ) -> bool:
        try:
            self.findings.put_item(
                Item=self._finding_item(finding),
                ConditionExpression="#finding_status = :expected",
                ExpressionAttributeNames={"#finding_status": "status"},
                ExpressionAttributeValues={":expected": expected_status.value},
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def list_findings(self) -> list[Finding]:
        return sorted(
            [
                self._finding_model(item)
                for item in self._scan_all(self.findings)
            ],
            key=lambda finding: finding.created_at,
            reverse=True,
        )

    def write_approved_artifact(self, finding: Finding) -> str:
        key = f"approved/{finding.finding_id}.md"
        content = (
            f"# Approved Civic Canary patch\n\n{finding.proposed_patch}\n\n"
            "This reviewed draft was not published automatically.\n"
        )
        self.s3.put_object(
            Bucket=self.bucket, Key=key, Body=content.encode(), ContentType="text/markdown"
        )
        return key

    def load_playbook(self, key: str) -> str:
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read().decode()

    def evidence_url(self, key: str) -> str | None:
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=300,
        )


class ExistingAwsStore(AwsStore):
    """Existing Sites/Findings/Reviews tables; scan records live in S3.

    Domain models stay snake_case. Only DynamoDB partition keys are translated.
    Terminal review decisions and finding transitions commit atomically.
    """

    target_key = "siteId"
    finding_key = "findingId"

    def __init__(
        self,
        *,
        sites_table: str = "CivicCanarySites",
        findings_table: str = "CivicCanaryFindings",
        reviews_table: str = "CivicCanaryReviews",
        evidence_bucket: str = "civic-canary",
        region: str = "us-east-1",
    ) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=region)
        self.targets = dynamodb.Table(sites_table)
        self.findings = dynamodb.Table(findings_table)
        self.reviews = dynamodb.Table(reviews_table)
        # A separate low-level client avoids DynamoDB resource double serialization.
        self.ddb = boto3.client("dynamodb", region_name=region)
        self.s3 = boto3.client("s3", region_name=region)
        self.bucket = evidence_bucket

    @staticmethod
    def _run_key(run_id: str) -> str:
        return f"runs/{quote(run_id, safe='')}.json"

    def put_run(self, run: Run) -> None:
        self.s3.put_object(
            Bucket=self.bucket, Key=self._run_key(run.run_id),
            Body=run.model_dump_json().encode(), ContentType="application/json",
        )

    def create_run(self, run: Run) -> bool:
        try:
            self.s3.put_object(
                Bucket=self.bucket, Key=self._run_key(run.run_id),
                Body=run.model_dump_json().encode(), ContentType="application/json",
                IfNoneMatch="*",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"PreconditionFailed", "412"}:
                return False
            raise

    def get_run(self, run_id: str) -> Run | None:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self._run_key(run_id))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                return None
            raise
        return Run.model_validate_json(response["Body"].read())

    def list_runs(self) -> list[Run]:
        runs = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix="runs/"
        ):
            for item in page.get("Contents", []):
                if not item["Key"].endswith(".json"):
                    continue
                response = self.s3.get_object(Bucket=self.bucket, Key=item["Key"])
                runs.append(Run.model_validate_json(response["Body"].read()))
        return sorted(
            runs, key=lambda run: run.started_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    def transition_finding(self, finding: Finding, expected_status: FindingStatus) -> bool:
        if finding.status not in {FindingStatus.APPROVED, FindingStatus.REJECTED}:
            return super().transition_finding(finding, expected_status)
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()

        def serialize(item: dict) -> dict:
            return {key: serializer.serialize(value) for key, value in item.items()}

        review = _to_dynamo({
            "reviewId": f"review-{finding.finding_id}",
            "findingId": finding.finding_id,
            "siteId": finding.target_id,
            "runId": finding.run_id,
            "action": "APPROVE" if finding.status == FindingStatus.APPROVED else "REJECT",
            "note": finding.decision_note,
            "decidedAt": finding.decided_at.isoformat() if finding.decided_at else None,
            "approvedArtifactKey": finding.approved_artifact_key,
        })
        try:
            self.ddb.transact_write_items(TransactItems=[
                {"Put": {
                    "TableName": self.findings.name,
                    "Item": serialize(self._finding_item(finding)),
                    "ConditionExpression": "#s = :expected",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {
                        ":expected": serializer.serialize(expected_status.value)
                    },
                }},
                {"Put": {
                    "TableName": self.reviews.name,
                    "Item": serialize(review),
                    "ConditionExpression": "attribute_not_exists(reviewId)",
                }},
            ])
            return True
        except ClientError as exc:
            reasons = exc.response.get("CancellationReasons", [])
            if exc.response["Error"]["Code"] == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in reasons
            ):
                return False
            raise
