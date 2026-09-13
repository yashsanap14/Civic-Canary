from __future__ import annotations

import json
import uuid
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
    def record_review(
        self,
        finding_id: str,
        action: str,
        note: str = "",
        reviewer: str = "reviewer",
    ) -> str: ...

    @abstractmethod
    def load_playbook(self, key: str) -> str: ...

    @abstractmethod
    def evidence_url(self, key: str) -> str | None: ...

    def commit_review(self, finding: Finding, expected_status: FindingStatus) -> bool:
        raise NotImplementedError

    def put_json_once(self, key: str, value: dict) -> bool:
        raise NotImplementedError

    def put_json(self, key: str, value: dict) -> None:
        raise NotImplementedError

    def get_json(self, key: str) -> dict | None:
        raise NotImplementedError

    def list_json_keys(self, prefix: str, limit: int = 50) -> list[str]:
        raise NotImplementedError

    def delete_json(self, key: str) -> None:
        raise NotImplementedError

    def delete_target(self, target_id: str) -> dict[str, int]:
        """Remove a target and records that belong only to that target_id."""
        raise NotImplementedError

    def read_artifact(self, key: str) -> str:
        raise NotImplementedError

    def target_page(
        self, limit: int = 50, cursor: dict | None = None
    ) -> tuple[list[PortalTarget], dict | None]:
        return self.list_targets()[:limit], None

    def finding_page(
        self, limit: int = 50, cursor: dict | None = None
    ) -> tuple[list[Finding], dict | None]:
        return self.list_findings()[:limit], None

    def recent_runs(self, limit: int = 50) -> list[Run]:
        return self.list_runs()[:limit]


class InMemoryStore(Store):
    def __init__(self, playbook: str = "") -> None:
        self.targets: dict[str, PortalTarget] = {}
        self.baselines: dict[str, PortalSnapshot] = {}
        self.snapshots: dict[str, PortalSnapshot] = {}
        self.runs: dict[str, Run] = {}
        self.findings: dict[str, Finding] = {}
        self.reviews: dict[str, dict[str, Any]] = {}
        self.artifacts: dict[str, str] = {}
        self.playbook = playbook
        self.json_objects: dict[str, dict] = {}
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

    def record_review(
        self,
        finding_id: str,
        action: str,
        note: str = "",
        reviewer: str = "reviewer",
    ) -> str:
        with self._lock:
            review_id = f"rev-{uuid.uuid4().hex[:12]}"
            self.reviews[review_id] = {
                "reviewId": review_id,
                "findingId": finding_id,
                "action": action,
                "reviewedAt": datetime.now(UTC).isoformat(),
                "reviewer": reviewer,
                "notes": note,
            }
            return review_id

    def load_playbook(self, key: str) -> str:
        return self.playbook

    def evidence_url(self, key: str) -> str | None:
        return None

    def commit_review(self, finding: Finding, expected_status: FindingStatus) -> bool:
        with self._lock:
            if not self.transition_finding(finding, expected_status):
                return False
            self.reviews[f"review-{finding.finding_id}"] = review_item(finding)
            return True

    def put_json_once(self, key: str, value: dict) -> bool:
        with self._lock:
            if key in self.json_objects:
                return False
            self.put_json(key, value)
            return True

    def put_json(self, key: str, value: dict) -> None:
        self.json_objects[key] = json.loads(json.dumps(value))

    def get_json(self, key: str) -> dict | None:
        value = self.json_objects.get(key)
        return json.loads(json.dumps(value)) if value is not None else None

    def list_json_keys(self, prefix: str, limit: int = 50) -> list[str]:
        return sorted(key for key in self.json_objects if key.startswith(prefix))[:limit]

    def delete_json(self, key: str) -> None:
        self.json_objects.pop(key, None)

    def delete_target(self, target_id: str) -> dict[str, int]:
        with self._lock:
            target = self.targets.get(target_id)
            if target is None:
                return {"targets": 0}

            runs = [run for run in self.runs.values() if run.target_id == target_id]
            findings = [
                finding for finding in self.findings.values() if finding.target_id == target_id
            ]
            run_ids = {run.run_id for run in runs}
            finding_ids = {finding.finding_id for finding in findings}

            jobs_removed = 0
            for run_id in run_ids:
                job_key = f"jobs/{run_id}.json"
                if job_key in self.json_objects:
                    self.json_objects.pop(job_key, None)
                    jobs_removed += 1

            recent_removed = 0
            for key in list(self.json_objects):
                if not key.startswith("recent-runs/"):
                    continue
                payload = self.json_objects.get(key) or {}
                if payload.get("target_id") == target_id or any(
                    key.endswith(f"-{run_id}.json") for run_id in run_ids
                ):
                    self.json_objects.pop(key, None)
                    recent_removed += 1

            outbox_removed = 0
            notifications_removed = 0
            artifacts_removed = 0
            for finding_id in finding_ids:
                for prefix in ("outbox/", "notification-events/", "notification-claims/"):
                    key = f"{prefix}{finding_id}.json"
                    if key in self.json_objects:
                        self.json_objects.pop(key, None)
                        if prefix == "outbox/":
                            outbox_removed += 1
                        else:
                            notifications_removed += 1
                artifact_key = f"approved/{finding_id}.md"
                if artifact_key in self.artifacts:
                    self.artifacts.pop(artifact_key, None)
                    artifacts_removed += 1
                self.findings.pop(finding_id, None)

            reviews_removed = 0
            for review_id, review in list(self.reviews.items()):
                if (
                    review.get("findingId") in finding_ids
                    or review.get("siteId") == target_id
                    or review_id in {f"review-{finding_id}" for finding_id in finding_ids}
                ):
                    self.reviews.pop(review_id, None)
                    reviews_removed += 1

            for run_id in run_ids:
                self.runs.pop(run_id, None)
                self.snapshots.pop(run_id, None)

            self.baselines.pop(target_id, None)
            self.targets.pop(target_id, None)

            return {
                "targets": 1,
                "runs": len(run_ids),
                "findings": len(finding_ids),
                "reviews": reviews_removed,
                "jobs": jobs_removed,
                "recent_runs": recent_removed,
                "outbox": outbox_removed,
                "notifications": notifications_removed,
                "artifacts": artifacts_removed,
            }

    def read_artifact(self, key: str) -> str:
        return self.artifacts[key]


class AwsStore(Store):
    def __init__(
        self,
        sites_table: str,
        findings_table: str,
        reviews_table: str,
        evidence_bucket: str,
        region: str = "us-east-1",
    ) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=region)
        self.sites = dynamodb.Table(sites_table)
        self.findings = dynamodb.Table(findings_table)
        self.reviews = dynamodb.Table(reviews_table)
        self.s3 = boto3.client("s3", region_name=region)
        self.bucket = evidence_bucket
        self.region = region
        self.ddb = boto3.client("dynamodb", region_name=region)

    def get_target(self, target_id: str) -> PortalTarget | None:
        item = self.sites.get_item(Key={"siteId": target_id}).get("Item")
        if not item:
            return None
        data = _from_dynamo(item)
        if "siteId" in data:
            data["target_id"] = data.pop("siteId")
        return PortalTarget.model_validate(data)

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
        targets: list[PortalTarget] = []
        for item in self._scan_all(self.sites):
            data = _from_dynamo(item)
            if "siteId" in data:
                data["target_id"] = data.pop("siteId")
            targets.append(PortalTarget.model_validate(data))
        return targets

    def put_target(self, target: PortalTarget) -> None:
        item = _dynamo_item(target)
        item["siteId"] = item.pop("target_id")
        self.sites.put_item(Item=item)

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
        self.s3.put_object(
            Bucket=self.bucket,
            Key=f"runs/{run.run_id}.json",
            Body=run.model_dump_json(indent=2).encode(),
            ContentType="application/json",
        )

    def create_run(self, run: Run) -> bool:
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=f"runs/{run.run_id}.json",
                Body=run.model_dump_json(indent=2).encode(),
                ContentType="application/json",
                IfNoneMatch="*",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"PreconditionFailed", "412"}:
                return False
            raise

    def get_run(self, run_id: str) -> Run | None:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=f"runs/{run_id}.json")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        return Run.model_validate_json(response["Body"].read())

    def list_runs(self) -> list[Run]:
        runs: list[Run] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix="runs/"):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                if not key.endswith(".json"):
                    continue
                try:
                    resp = self.s3.get_object(Bucket=self.bucket, Key=key)
                    runs.append(Run.model_validate_json(resp["Body"].read()))
                except ClientError as exc:
                    if exc.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
                        raise
        return sorted(
            runs,
            key=lambda run: run.started_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    def put_finding(self, finding: Finding) -> bool:
        item = _dynamo_item(finding)
        item["findingId"] = item.pop("finding_id")
        if finding.status != "OPEN":
            self.findings.put_item(Item=item)
            return True
        try:
            self.findings.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(findingId)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def get_finding(self, finding_id: str) -> Finding | None:
        item = self.findings.get_item(Key={"findingId": finding_id}).get("Item")
        if not item:
            return None
        data = _from_dynamo(item)
        if "findingId" in data:
            data["finding_id"] = data.pop("findingId")
        return Finding.model_validate(data)

    def transition_finding(
        self,
        finding: Finding,
        expected_status: FindingStatus,
    ) -> bool:
        item = _dynamo_item(finding)
        item["findingId"] = item.pop("finding_id")
        try:
            self.findings.put_item(
                Item=item,
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
        findings: list[Finding] = []
        for item in self._scan_all(self.findings):
            data = _from_dynamo(item)
            if "findingId" in data:
                data["finding_id"] = data.pop("findingId")
            findings.append(Finding.model_validate(data))
        return sorted(
            findings,
            key=lambda finding: finding.created_at,
            reverse=True,
        )

    def write_approved_artifact(self, finding: Finding) -> str:
        key = f"approved/{finding.finding_id}.md"
        content = (
            f"# Approved Civic Canary patch\n\n{finding.proposed_patch}\n\n"
            f"Run: {finding.run_id}\nReviewer: {finding.reviewed_by}\n"
            f"Decision time: {finding.decided_at}\nEvidence: {finding.evidence}\n\n"
            "This reviewed draft was not published automatically.\n"
        )
        self.s3.put_object(
            Bucket=self.bucket, Key=key, Body=content.encode(), ContentType="text/markdown"
        )
        return key

    def record_review(
        self,
        finding_id: str,
        action: str,
        note: str = "",
        reviewer: str = "reviewer",
    ) -> str:
        review_id = f"rev-{uuid.uuid4().hex[:12]}"
        item = {
            "reviewId": review_id,
            "findingId": finding_id,
            "action": action,
            "reviewedAt": datetime.now(UTC).isoformat(),
            "reviewer": reviewer,
            "notes": note,
        }
        self.reviews.put_item(Item=_to_dynamo(item))
        return review_id

    def load_playbook(self, key: str) -> str:
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read().decode()

    def evidence_url(self, key: str) -> str | None:
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=300,
        )

    def commit_review(self, finding: Finding, expected_status: FindingStatus) -> bool:
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()

        def wire(value):
            return {key: serializer.serialize(item) for key, item in _to_dynamo(value).items()}

        item = _dynamo_item(finding)
        item["findingId"] = item.pop("finding_id")
        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.findings.name,
                            "Item": wire(item),
                            "ConditionExpression": "#s = :expected",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {
                                ":expected": {"S": expected_status.value}
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.reviews.name,
                            "Item": wire(review_item(finding)),
                            "ConditionExpression": "attribute_not_exists(reviewId)",
                        }
                    },
                ]
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in exc.response.get("CancellationReasons", [])
            ):
                return False
            raise

    def put_json_once(self, key: str, value: dict) -> bool:
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=json.dumps(value).encode(),
                ContentType="application/json",
                IfNoneMatch="*",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"PreconditionFailed", "412"}:
                return False
            raise

    def put_json(self, key: str, value: dict) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(value).encode(),
            ContentType="application/json",
        )

    def get_json(self, key: str) -> dict | None:
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                return None
            raise

    def list_json_keys(self, prefix: str, limit: int = 50) -> list[str]:
        response = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=limit)
        return [item["Key"] for item in response.get("Contents", [])]

    def delete_json(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def _delete_s3_prefix(self, prefix: str) -> int:
        """Delete objects under an exact prefix. Prefix must already be target-scoped."""
        if not prefix or ".." in prefix:
            raise ValueError("Refusing to delete an unsafe S3 prefix")
        removed = 0
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objects = [{"Key": item["Key"]} for item in page.get("Contents", []) if item.get("Key")]
            if not objects:
                continue
            # delete_objects accepts up to 1000 keys per call
            for index in range(0, len(objects), 1000):
                batch = objects[index : index + 1000]
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})
                removed += len(batch)
        return removed

    def _delete_s3_key_if_exists(self, key: str) -> int:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
            return 1
        except ClientError:
            return 0

    def delete_target(self, target_id: str) -> dict[str, int]:
        target = self.get_target(target_id)
        if target is None:
            return {"targets": 0}

        runs = [run for run in self.list_runs() if run.target_id == target_id]
        findings = [finding for finding in self.list_findings() if finding.target_id == target_id]
        run_ids = {run.run_id for run in runs}
        finding_ids = {finding.finding_id for finding in findings}

        jobs_removed = 0
        for run_id in run_ids:
            job_key = f"jobs/{run_id}.json"
            if self.get_json(job_key) is not None:
                self.delete_json(job_key)
                jobs_removed += 1

        recent_removed = 0
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix="recent-runs/"):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                if not key.endswith(".json"):
                    continue
                payload = self.get_json(key) or {}
                if payload.get("target_id") == target_id or any(
                    key.endswith(f"-{run_id}.json") for run_id in run_ids
                ):
                    self.delete_json(key)
                    recent_removed += 1

        outbox_removed = 0
        notifications_removed = 0
        artifacts_removed = 0
        for finding_id in finding_ids:
            for prefix in ("outbox/", "notification-events/", "notification-claims/"):
                key = f"{prefix}{finding_id}.json"
                if self.get_json(key) is not None:
                    self.delete_json(key)
                    if prefix == "outbox/":
                        outbox_removed += 1
                    else:
                        notifications_removed += 1
            artifacts_removed += self._delete_s3_key_if_exists(f"approved/{finding_id}.md")
            self.findings.delete_item(Key={"findingId": finding_id})
            self.reviews.delete_item(Key={"reviewId": f"review-{finding_id}"})

        runs_removed = 0
        for run in runs:
            run_key = getattr(self, "_run_key", lambda run_id: f"runs/{run_id}.json")(run.run_id)
            runs_removed += self._delete_s3_key_if_exists(run_key)

        evidence_removed = 0
        evidence_removed += self._delete_s3_key_if_exists(f"baselines/{target_id}.json")
        evidence_removed += self._delete_s3_prefix(f"baselines/{target_id}/")
        evidence_removed += self._delete_s3_prefix(f"snapshots/{target_id}/")
        evidence_removed += self._delete_s3_prefix(f"screenshots/{target_id}/")
        if target.playbook_key.startswith(f"playbooks/{target_id}/"):
            evidence_removed += self._delete_s3_prefix(f"playbooks/{target_id}/")

        self.sites.delete_item(Key={"siteId": target_id})

        return {
            "targets": 1,
            "runs": runs_removed,
            "findings": len(finding_ids),
            "jobs": jobs_removed,
            "recent_runs": recent_removed,
            "outbox": outbox_removed,
            "notifications": notifications_removed,
            "artifacts": artifacts_removed,
            "evidence_objects": evidence_removed,
        }

    def read_artifact(self, key: str) -> str:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode()

    def target_page(self, limit: int = 50, cursor: dict | None = None):
        options = {"Limit": limit}
        if cursor:
            options["ExclusiveStartKey"] = cursor
        response = self.sites.scan(**options)
        rows = [
            PortalTarget.model_validate({**_from_dynamo(item), "target_id": item["siteId"]})
            for item in response.get("Items", [])
        ]
        return rows, response.get("LastEvaluatedKey")

    def finding_page(self, limit: int = 50, cursor: dict | None = None):
        options = {"Limit": limit}
        if cursor:
            options["ExclusiveStartKey"] = cursor
        response = self.findings.scan(**options)
        rows = [
            Finding.model_validate({**_from_dynamo(item), "finding_id": item["findingId"]})
            for item in response.get("Items", [])
        ]
        return rows, response.get("LastEvaluatedKey")

    def recent_runs(self, limit: int = 50) -> list[Run]:
        return [
            Run.model_validate(self.get_json(key))
            for key in self.list_json_keys("recent-runs/", limit)
        ]


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
        self.sites = self.targets
        self.findings = dynamodb.Table(findings_table)
        self.reviews = dynamodb.Table(reviews_table)
        # A separate low-level client avoids DynamoDB resource double serialization.
        self.ddb = boto3.client("dynamodb", region_name=region)
        self.s3 = boto3.client("s3", region_name=region)
        self.bucket = evidence_bucket

    def _target_item(self, target: PortalTarget) -> dict:
        item = _dynamo_item(target)
        item["siteId"] = item.pop("target_id")
        return item

    def _finding_item(self, finding: Finding) -> dict:
        item = _dynamo_item(finding)
        item["findingId"] = item.pop("finding_id")
        return item

    def get_target(self, target_id: str) -> PortalTarget | None:
        item = self.sites.get_item(Key={"siteId": target_id}, ConsistentRead=True).get("Item")
        return (
            PortalTarget.model_validate({**_from_dynamo(item), "target_id": item["siteId"]})
            if item
            else None
        )

    def record_review(
        self, finding_id: str, action: str, note: str = "", reviewer: str = "reviewer"
    ) -> str:
        # The API calls this after transition_finding; the transaction already saved it.
        return f"review-{finding_id}"

    @staticmethod
    def _run_key(run_id: str) -> str:
        return f"runs/{quote(run_id, safe='')}.json"

    def put_run(self, run: Run) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self._run_key(run.run_id),
            Body=run.model_dump_json().encode(),
            ContentType="application/json",
        )

    def create_run(self, run: Run) -> bool:
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=self._run_key(run.run_id),
                Body=run.model_dump_json().encode(),
                ContentType="application/json",
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
            runs,
            key=lambda run: run.started_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    def transition_finding(self, finding: Finding, expected_status: FindingStatus) -> bool:
        if finding.status not in {FindingStatus.APPROVED, FindingStatus.REJECTED}:
            return super().transition_finding(finding, expected_status)
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()

        def serialize(item: dict) -> dict:
            return {key: serializer.serialize(value) for key, value in item.items()}

        review = _to_dynamo(
            {
                "reviewId": f"review-{finding.finding_id}",
                "findingId": finding.finding_id,
                "siteId": finding.target_id,
                "runId": finding.run_id,
                "action": "APPROVE" if finding.status == FindingStatus.APPROVED else "REJECT",
                "note": finding.decision_note,
                "decidedAt": finding.decided_at.isoformat() if finding.decided_at else None,
                "approvedArtifactKey": finding.approved_artifact_key,
            }
        )
        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.findings.name,
                            "Item": serialize(self._finding_item(finding)),
                            "ConditionExpression": "#s = :expected",
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {
                                ":expected": serializer.serialize(expected_status.value)
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.reviews.name,
                            "Item": serialize(review),
                            "ConditionExpression": "attribute_not_exists(reviewId)",
                        }
                    },
                ]
            )
            return True
        except ClientError as exc:
            reasons = exc.response.get("CancellationReasons", [])
            if exc.response["Error"]["Code"] == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in reasons
            ):
                return False
            raise


def review_item(finding: Finding) -> dict:
    return {
        "reviewId": f"review-{finding.finding_id}",
        "findingId": finding.finding_id,
        "siteId": finding.target_id,
        "runId": finding.run_id,
        "action": finding.status.value,
        "reviewedAt": finding.decided_at.isoformat()
        if finding.decided_at
        else datetime.now(UTC).isoformat(),
        "reviewer": finding.reviewed_by or "reviewer",
        "notes": finding.decision_note,
        "originalRecommendation": finding.proposed_patch,
        "evidence": finding.evidence,
        "evidenceKeys": finding.evidence_keys,
        "approvedArtifactKey": finding.approved_artifact_key,
    }
