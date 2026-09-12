"""Durable EC2 work queue and per-site schedules using existing S3/DynamoDB storage."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from agent.civic_canary.models import Run, RunStatus, TriggerType
from services.runtime import ScanService


def index_run(store, run):
    timestamp = run.started_at or datetime.now(UTC)
    reverse = 9999999999999 - int(timestamp.timestamp() * 1000)
    store.put_json(f"recent-runs/{reverse:013d}-{run.run_id}.json", run.model_dump(mode="json"))


def enqueue_scan(store, target, trigger=TriggerType.MANUAL, run_id=None):
    run = Run(
        run_id=run_id or f"run-{uuid.uuid4().hex[:12]}",
        target_id=target.target_id,
        trigger_type=trigger,
        started_at=datetime.now(UTC),
    )
    if not store.create_run(run):
        existing = store.get_run(run.run_id)
        if existing is None or existing.target_id != target.target_id:
            raise ValueError("Run ID is already reserved for another website")
        if existing.status == RunStatus.QUEUED:
            store.put_json(
                f"jobs/{run.run_id}.json", {"run_id": run.run_id, "target_id": target.target_id}
            )
        return existing
    store.put_json(f"jobs/{run.run_id}.json", {"run_id": run.run_id, "target_id": target.target_id})
    index_run(store, run)
    return run


async def run_due(store, now=None):
    """Run under the supplied single-worker flock/systemd unit, never inside the web process."""
    now = now or datetime.now(UTC)
    cursor = None
    while True:
        targets, cursor = store.target_page(50, cursor)
        for target in targets:
            if not target.enabled or target.setup_status != "ACTIVE":
                continue
            if target.next_scan_at and target.next_scan_at > now:
                continue
            slot = int(now.timestamp()) // (target.scan_frequency_minutes * 60)
            digest = hashlib.sha256(f"{target.target_id}:{slot}".encode()).hexdigest()[:24]
            enqueue_scan(store, target, TriggerType.SCHEDULED, f"scheduled-{digest}")
            target.next_scan_at = now + timedelta(minutes=target.scan_frequency_minutes)
            store.put_target(target)
        if not cursor:
            break
    processed = 0
    for key in store.list_json_keys("jobs/", 4):
        job = store.get_json(key)
        if not job:
            continue
        run = store.get_run(job["run_id"])
        if not run:
            continue
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
            index_run(store, run)
            store.delete_json(key)
            continue
        if run.status == RunStatus.RUNNING:
            # Single worker lock means this is recovery after process termination.
            run.status = RunStatus.FAILED
            run.error_category = "InterruptedWorker"
            run.summary = "Previous worker stopped during this scan; schedule a fresh scan."
            run.finished_at = now
            store.put_run(run)
            index_run(store, run)
            store.delete_json(key)
            continue
        target = store.get_target(run.target_id)
        try:
            if target is None:
                raise ValueError("Website configuration is missing")
            run, _ = await ScanService(store).run_local(target, run.trigger_type, run.run_id)
        except Exception as exc:
            from agent.civic_canary.engine import RunExecutionError

            run = exc.run if isinstance(exc, RunExecutionError) else run
            run.status = RunStatus.FAILED
            run.finished_at = datetime.now(UTC)
            run.error_category = run.error_category or type(exc).__name__
            run.summary = str(exc)
            store.put_run(run)
            if target and target.setup_status in {"PENDING", "FAILED"}:
                target.setup_status = "FAILED"
                store.put_target(target)
        index_run(store, run)
        store.delete_json(key)
        processed += 1
    from services.notifications import deliver_pending

    return {"processed": processed, "notifications": deliver_pending(store)}
