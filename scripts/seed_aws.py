from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import boto3

from agent.civic_canary.models import PortalTarget
from services.store_factory import default_store

ROOT = Path(__file__).resolve().parents[1]


def stack_outputs(stack_name: str, region: str) -> dict[str, str]:
    stacks = boto3.client("cloudformation", region_name=region).describe_stacks(
        StackName=stack_name
    )["Stacks"]
    return {item["OutputKey"]: item["OutputValue"] for item in stacks[0]["Outputs"]}


def seed(stack_name: str, region: str) -> None:
    outputs = stack_outputs(stack_name, region)
    frontend_url = outputs["FrontendUrl"].rstrip("/")
    domain = urlparse(frontend_url).hostname
    if not domain:
        raise RuntimeError("FrontendUrl stack output does not contain a hostname")
    target = PortalTarget(
        start_url=f"{frontend_url}/portal/{{version}}/",
        allowed_hosts=[domain],
        active_version="v1",
    )
    dynamodb = boto3.resource("dynamodb", region_name=region)
    dynamodb.Table(outputs["TargetsTable"]).put_item(
        Item=json.loads(target.model_dump_json())
    )

    s3 = boto3.client("s3", region_name=region)
    s3.put_object(
        Bucket=outputs["EvidenceBucket"],
        Key=target.playbook_key,
        Body=(ROOT / "agent" / "fixtures" / "benefits-playbook.md").read_bytes(),
        ContentType="text/markdown",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", default="CivicCanaryMvp")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--existing", action="store_true",
                        help="Seed the configured existing tables and bucket without CDK")
    args = parser.parse_args()
    if args.existing:
        import os

        os.environ["CIVIC_CANARY_MODE"] = "aws"
        os.environ["AWS_STORAGE_LAYOUT"] = "existing"
        os.environ["AWS_REGION"] = args.region
        store = default_store()
        target = PortalTarget()
        if store.get_target(target.target_id) is not None:
            raise SystemExit(
                "Target already exists; refusing to overwrite its baseline/configuration."
            )
        # Store the playbook before making the new target visible.
        store.s3.put_object(
            Bucket=store.bucket, Key=target.playbook_key,
            Body=(ROOT / "agent" / "fixtures" / "benefits-playbook.md").read_bytes(),
            ContentType="text/markdown",
        )
        store.put_target(target)
        print("Seeded existing storage with the synthetic V1 target and playbook.")
        return
    seed(args.stack, args.region)
    print("Seeded the synthetic target and playbook. The first V1 scan establishes its baseline.")


if __name__ == "__main__":
    main()
