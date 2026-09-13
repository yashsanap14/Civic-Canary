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
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        if os.getenv("AWS_STORAGE_LAYOUT") == "existing":
            return ExistingAwsStore(
                sites_table=os.getenv("SITES_TABLE", "CivicCanarySites"),
                findings_table=os.getenv("FINDINGS_TABLE", "CivicCanaryFindings"),
                reviews_table=os.getenv("REVIEWS_TABLE", "CivicCanaryReviews"),
                evidence_bucket=os.getenv("EVIDENCE_BUCKET", "civic-canary"),
                region=region,
            )
        config = {
            "sites_table": os.getenv("CIVIC_CANARY_SITES_TABLE"),
            "findings_table": os.getenv("CIVIC_CANARY_FINDINGS_TABLE"),
            "reviews_table": os.getenv("CIVIC_CANARY_REVIEWS_TABLE"),
            "evidence_bucket": os.getenv("CIVIC_CANARY_S3_BUCKET"),
        }
        if not all(config.values()):
            parameter_name = os.getenv(
                "STORAGE_CONFIG_SSM_PARAMETER", "/civic-canary/storage-config"
            )
            try:
                response = boto3.client("ssm", region_name=region).get_parameter(
                    Name=parameter_name
                )
                ssm_config = json.loads(response["Parameter"]["Value"])
                config["sites_table"] = config["sites_table"] or ssm_config.get("sites_table")
                config["findings_table"] = config["findings_table"] or ssm_config.get(
                    "findings_table"
                )
                config["reviews_table"] = config["reviews_table"] or ssm_config.get("reviews_table")
                config["evidence_bucket"] = config["evidence_bucket"] or ssm_config.get(
                    "evidence_bucket"
                )
                if browser_id := ssm_config.get("browser_id"):
                    os.environ.setdefault("AGENTCORE_BROWSER_ID", str(browser_id))
            except Exception as exc:
                missing = [key for key, value in config.items() if not value]
                raise RuntimeError(
                    "AWS storage config is incomplete "
                    f"(missing {', '.join(missing)}). Set CIVIC_CANARY_* env vars "
                    f"or ensure SSM parameter {parameter_name} is readable. "
                    f"Last SSM error: {type(exc).__name__}: {exc}"
                ) from exc

        missing = [key for key, value in config.items() if not value]
        if missing:
            raise RuntimeError(
                "AWS storage config is incomplete "
                f"(missing {', '.join(missing)}). Set CIVIC_CANARY_SITES_TABLE, "
                "CIVIC_CANARY_FINDINGS_TABLE, CIVIC_CANARY_REVIEWS_TABLE, and "
                "CIVIC_CANARY_S3_BUCKET (or STORAGE_CONFIG_SSM_PARAMETER)."
            )

        return AwsStore(
            sites_table=str(config["sites_table"]),
            findings_table=str(config["findings_table"]),
            reviews_table=str(config["reviews_table"]),
            evidence_bucket=str(config["evidence_bucket"]),
            region=region,
        )
    return InMemoryStore(playbook=PLAYBOOK.read_text(encoding="utf-8"))
