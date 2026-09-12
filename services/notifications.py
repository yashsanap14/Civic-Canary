"""Durable decision outbox. SES ambiguous outcomes are never automatically resent."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

import boto3

from services.observability import log_event


def queue_notifications(store, finding):
    finding = store.get_finding(finding.finding_id) or finding
    record = {
        "finding_id": finding.finding_id,
        "run_id": finding.run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "PENDING",
    }
    # One immutable event per finding; retries cannot create a second alert.
    if store.put_json_once(f"notification-events/{finding.finding_id}.json", record):
        store.put_json(f"outbox/{finding.finding_id}.json", record)
    elif not store.get_json(f"notification-claims/{finding.finding_id}.json"):
        # Repair event persisted just before an interrupted outbox write.
        store.put_json(f"outbox/{finding.finding_id}.json", record)


def deliver_pending(store, client=None) -> dict:
    sender = os.getenv("CIVIC_CANARY_EMAIL_FROM")
    recipient = os.getenv("CIVIC_CANARY_EMAIL_TO")
    public_url = os.getenv("CIVIC_CANARY_PUBLIC_URL", "")
    if not sender or not recipient:
        return {"status": "NOT_CONFIGURED", "sent": 0}
    if urlparse(public_url).scheme != "https" or not urlparse(public_url).hostname:
        raise ValueError("CIVIC_CANARY_PUBLIC_URL must be an HTTPS URL")
    ses = client or boto3.client("sesv2", region_name=os.getenv("AWS_REGION", "us-east-1"))
    sent = 0
    for key in store.list_json_keys("outbox/", 50):
        record = store.get_json(key)
        if not record:
            continue
        finding = store.get_finding(record["finding_id"])
        if finding is None:
            continue
        if finding.status != "OPEN":
            store.put_json(
                f"notification-claims/{finding.finding_id}.json",
                {**record, "status": "SUPPRESSED_ALREADY_REVIEWED"},
            )
            store.delete_json(key)
            continue
        claim_key = f"notification-claims/{finding.finding_id}.json"
        if not store.put_json_once(claim_key, {**record, "status": "SENDING"}):
            # Another attempt may have sent successfully before losing its response.
            existing = store.get_json(claim_key) or record
            if existing.get("status") == "SENDING":
                store.put_json(claim_key, {**existing, "status": "UNKNOWN_REQUIRES_RECONCILIATION"})
            store.delete_json(key)
            continue
        link = f"{public_url.rstrip('/')}/?finding={quote(finding.finding_id, safe='')}"
        try:
            response = ses.send_email(
                FromEmailAddress=sender,
                Destination={"ToAddresses": [recipient]},
                Content={
                    "Simple": {
                        "Subject": {"Data": "Civic Canary: a website change needs your decision"},
                        "Body": {
                            "Text": {
                                "Data": (
                                    "A change requires review. Sign in with your reviewer token.\n"
                                    f"Decision packet: {link}\nRun: {finding.run_id}\n"
                                    "No guidance has been published automatically."
                                )
                            }
                        },
                    }
                },
                EmailTags=[{"Name": "run_id", "Value": finding.run_id}],
            )
            status = "SENT"
            result = {**record, "status": status, "message_id": response["MessageId"]}
            sent += 1
        except Exception as exc:
            status = "UNKNOWN_REQUIRES_RECONCILIATION"
            result = {**record, "status": status, "error_category": type(exc).__name__}
        store.put_json(claim_key, result)
        store.delete_json(key)
        run = store.get_run(finding.run_id)
        if run:
            run.notification_status = status
            store.put_run(run)
            from services.monitoring import index_run

            index_run(store, run)
        log_event(
            "decision_notification",
            run_id=finding.run_id,
            finding_id=finding.finding_id,
            status=status,
        )
    return {"status": "COMPLETE", "sent": sent}
