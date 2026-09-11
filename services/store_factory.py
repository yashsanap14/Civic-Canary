from __future__ import annotations

import json
import os
from pathlib import Path

import boto3

from services.storage import AwsStore, ExistingAwsStore, InMemoryStore, Store

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = PROJECT_ROOT / "agent" / "fixtures" / "benefits-playbook.md"


def default_store() -> Store:
    if os.getenv("CIVIC_CANARY_MODE", "local") == "aws":
        region = os.getenv("AWS_REGION", "us-east-1")
        if os.getenv("AWS_STORAGE_LAYOUT") == "existing":
            return ExistingAwsStore(
                sites_table=os.getenv("SITES_TABLE", "CivicCanarySites"),
                findings_table=os.getenv("FINDINGS_TABLE", "CivicCanaryFindings"),
                reviews_table=os.getenv("REVIEWS_TABLE", "CivicCanaryReviews"),
                evidence_bucket=os.getenv("EVIDENCE_BUCKET", "civic-canary"),
                region=region,
            )
        config = {
            "targets_table": os.getenv("TARGETS_TABLE"),
            "runs_table": os.getenv("RUNS_TABLE"),
            "findings_table": os.getenv("FINDINGS_TABLE"),
            "evidence_bucket": os.getenv("EVIDENCE_BUCKET"),
        }
        if not all(config.values()):
            parameter_name = os.getenv(
                "STORAGE_CONFIG_SSM_PARAMETER", "/civic-canary/storage-config"
            )
            response = boto3.client("ssm", region_name=region).get_parameter(
                Name=parameter_name
            )
            config = json.loads(response["Parameter"]["Value"])
        for environment_name, config_name in {
            "TARGETS_TABLE": "targets_table",
            "RUNS_TABLE": "runs_table",
            "FINDINGS_TABLE": "findings_table",
            "EVIDENCE_BUCKET": "evidence_bucket",
            "AGENTCORE_BROWSER_ID": "browser_id",
        }.items():
            if value := config.get(config_name):
                os.environ.setdefault(environment_name, str(value))
        return AwsStore(
            targets_table=str(config["targets_table"]),
            runs_table=str(config["runs_table"]),
            findings_table=str(config["findings_table"]),
            evidence_bucket=str(config["evidence_bucket"]),
            region=region,
        )
    return InMemoryStore(playbook=PLAYBOOK.read_text(encoding="utf-8"))
