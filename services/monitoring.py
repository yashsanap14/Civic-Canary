"""Durable EC2 work queue and per-site schedules using existing S3/DynamoDB storage."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

from agent.civic_canary.engine import RunExecutionError
from agent.civic_canary.models import Run, RunStatus, TriggerType
from services.observability import log_event
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
        # QUEUED/RUNNING without jobs/*.json is invisible to the timer worker and
        # leaves setup stuck on PENDING forever. Always re-materialize the job object.
        if existing.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
            store.put_json(
                f"jobs/{run.run_id}.json", {"run_id": run.run_id, "target_id": target.target_id}
            )
            log_event(
                "scan_job_requeued",
                run_id=run.run_id,
                target_id=target.target_id,
                trigger_type=trigger.value,
                status=existing.status,
            )
        return existing
    store.put_json(f"jobs/{run.run_id}.json", {"run_id": run.run_id, "target_id": target.target_id})
    index_run(store, run)
    log_event(
        "scan_job_enqueued",
        run_id=run.run_id,
        target_id=target.target_id,
        trigger_type=trigger.value,
        setup_status=target.setup_status,
    )
    return run


def _scan_service(store):
    """Use AgentCore Browser in AWS mode; otherwise the default adapter."""
    if os.getenv("CIVIC_CANARY_MODE", "local") == "aws":
        from agent.civic_canary.browser import AgentCoreBrowserAdapter

        return ScanService(store, AgentCoreBrowserAdapter())
    return ScanService(store)


async def process_queued_run(store, run_id: str) -> Run:
    """Consume one queued job end-to-end. Never leaves the run QUEUED/RUNNING."""
    log_event("job_dispatch_started", run_id=run_id)
    job_key = f"jobs/{run_id}.json"
    run = store.get_run(run_id)
    if run is None:
        store.delete_json(job_key)
        raise ValueError(f"Run {run_id} is missing")
    if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
        log_event("job_already_terminal", run_id=run_id, status=run.status)
        store.delete_json(job_key)
        index_run(store, run)
        return run

    target = store.get_target(run.target_id)
    log_event(
        "job_target_loaded",
        run_id=run_id,
        target_id=run.target_id,
        setup_status=getattr(target, "setup_status", None),
        has_target=target is not None,
    )
    run.status = RunStatus.RUNNING
    store.put_run(run)
    index_run(store, run)
    log_event("run_status_running", run_id=run_id, target_id=run.target_id)

    try:
        if target is None:
            raise ValueError("Website configuration is missing")
        service = _scan_service(store)
        log_event(
            "scan_service_ready",
            run_id=run_id,
            target_id=target.target_id,
            browser=type(service.browser).__name__,
            mode=os.getenv("CIVIC_CANARY_MODE", "local"),
        )
        run, findings = await service.run_local(target, run.trigger_type, run.run_id)
        refreshed = store.get_target(target.target_id) or target
        baseline = store.get_baseline(target.target_id)
        log_event(
            "job_dispatch_completed",
            run_id=run_id,
            target_id=target.target_id,
            status=run.status,
            setup_status=refreshed.setup_status,
            baseline_saved=baseline is not None,
            findings=len(findings),
        )
    except Exception as exc:
        run = exc.run if isinstance(exc, RunExecutionError) else store.get_run(run_id) or run
        run.status = RunStatus.FAILED
        run.finished_at = datetime.now(UTC)
        run.error_category = run.error_category or type(exc).__name__
        run.summary = str(getattr(exc, "cause", exc) or exc)
        store.put_run(run)
        if target and target.setup_status in {"PENDING", "FAILED"}:
            target.setup_status = "FAILED"
            target.setup_run_id = run.run_id
            store.put_target(target)
        log_event(
            "job_dispatch_failed",
            run_id=run_id,
            target_id=run.target_id,
            error_category=run.error_category,
            summary=run.summary,
            setup_status=getattr(target, "setup_status", None),
        )
        index_run(store, run)
        store.delete_json(job_key)
        if isinstance(exc, RunExecutionError):
            raise
        raise RunExecutionError(run, exc) from exc

    index_run(store, run)
    store.delete_json(job_key)
    return run


def _run_age_seconds(run: Run, now: datetime) -> float:
    started = run.started_at or now
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, (now - started).total_seconds())


def recover_setup_queue(store, now: datetime | None = None) -> int:
    """Keep live onboarding from sitting on PENDING/QUEUED forever.

    - Re-materialize missing jobs/*.json for QUEUED setup runs.
    - Fail stale QUEUED/RUNNING setup runs and mirror FAILED onto the target.
    - Sync target.setup_status when the setup run already failed.
    """
    now = now or datetime.now(UTC)
    stale_queued = int(os.getenv("CIVIC_CANARY_STALE_QUEUED_SECONDS", "900"))
    stale_running = int(os.getenv("CIVIC_CANARY_STALE_RUNNING_SECONDS", "1800"))
    repaired = 0
    cursor = None
    while True:
        targets, cursor = store.target_page(50, cursor)
        for target in targets:
            if target.setup_status not in {"PENDING", "FAILED"}:
                continue
            if not target.setup_run_id:
                continue
            run = store.get_run(target.setup_run_id)
            if run is None:
                if target.setup_status == "PENDING":
                    target.setup_status = "FAILED"
                    store.put_target(target)
                    log_event(
                        "setup_missing_run_failed",
                        run_id=target.setup_run_id,
                        target_id=target.target_id,
                        summary="Setup run record was missing; marked FAILED",
                    )
                    repaired += 1
                continue

            if run.status == RunStatus.FAILED and target.setup_status == "PENDING":
                target.setup_status = "FAILED"
                store.put_target(target)
                log_event(
                    "setup_status_synced_failed",
                    run_id=run.run_id,
                    target_id=target.target_id,
                    error_category=run.error_category,
                    summary=run.summary or "Synced FAILED setup status from run",
                )
                repaired += 1
                continue

            if run.status == RunStatus.QUEUED:
                job_key = f"jobs/{run.run_id}.json"
                age = _run_age_seconds(run, now)
                if store.get_json(job_key) is None:
                    store.put_json(
                        job_key, {"run_id": run.run_id, "target_id": target.target_id}
                    )
                    log_event(
                        "setup_job_repaired",
                        run_id=run.run_id,
                        target_id=target.target_id,
                        age_seconds=int(age),
                        summary="Requeued missing jobs/*.json for PENDING setup",
                    )
                    repaired += 1
                elif age >= stale_queued:
                    run.status = RunStatus.FAILED
                    run.finished_at = now
                    run.error_category = "StaleQueue"
                    run.summary = (
                        f"Inspection stayed QUEUED for {int(age)}s without completing; "
                        "retry inspection."
                    )
                    store.put_run(run)
                    target.setup_status = "FAILED"
                    store.put_target(target)
                    store.delete_json(job_key)
                    index_run(store, run)
                    log_event(
                        "setup_stale_queued_failed",
                        run_id=run.run_id,
                        target_id=target.target_id,
                        age_seconds=int(age),
                        summary=run.summary,
                    )
                    repaired += 1
                continue

            if run.status == RunStatus.RUNNING:
                age = _run_age_seconds(run, now)
                if age < stale_running:
                    continue
                run.status = RunStatus.FAILED
                run.finished_at = now
                run.error_category = "StaleRunning"
                run.summary = (
                    f"Inspection stayed RUNNING for {int(age)}s without finishing; "
                    "retry inspection."
                )
                store.put_run(run)
                target.setup_status = "FAILED"
                store.put_target(target)
                store.delete_json(f"jobs/{run.run_id}.json")
                index_run(store, run)
                log_event(
                    "setup_stale_running_failed",
                    run_id=run.run_id,
                    target_id=target.target_id,
                    age_seconds=int(age),
                    summary=run.summary,
                )
                repaired += 1
        if not cursor:
            break
    return repaired


async def run_due(store, now=None):
    """Run under the supplied single-worker flock/systemd unit, never inside the web process."""
    now = now or datetime.now(UTC)
    repaired = recover_setup_queue(store, now)
    if repaired:
        log_event(
            "setup_queue_recovery",
            run_id="worker",
            repaired=repaired,
            summary="Repaired or failed stale live-setup runs",
        )
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
            # Idempotent scheduled run id. next_scan_at advances only after SUCCEEDED
            # (or is reset to now on FAILED) so failures retry on the next worker tick.
            enqueue_scan(store, target, TriggerType.SCHEDULED, f"scheduled-{digest}")
        if not cursor:
            break
    processed = 0
    job_budget = int(os.getenv("CIVIC_CANARY_JOB_BUDGET", "8"))
    for key in store.list_json_keys("jobs/", max(1, job_budget)):
        job = store.get_json(key)
        if not job:
            continue
        run_id = job["run_id"]
        log_event("worker_claiming_job", run_id=run_id, job_key=key)
        run = store.get_run(run_id)
        if not run:
            store.delete_json(key)
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
            target = store.get_target(run.target_id)
            if target:
                if target.setup_status in {"PENDING", "FAILED"}:
                    target.setup_status = "FAILED"
                if target.setup_status == "ACTIVE":
                    target.next_scan_at = now
                store.put_target(target)
            log_event("worker_interrupted_run_failed", run_id=run_id)
            index_run(store, run)
            store.delete_json(key)
            processed += 1
            continue
        try:
            completed = await process_queued_run(store, run_id)
            if completed.status == RunStatus.SUCCEEDED:
                refreshed = store.get_target(completed.target_id)
                if refreshed and refreshed.setup_status == "ACTIVE":
                    refreshed.next_scan_at = now + timedelta(
                        minutes=refreshed.scan_frequency_minutes
                    )
                    store.put_target(refreshed)
        except Exception as exc:
            failed_target = store.get_target(run.target_id)
            if failed_target and failed_target.setup_status == "ACTIVE":
                failed_target.next_scan_at = now
                store.put_target(failed_target)
            log_event(
                "worker_job_error",
                run_id=run_id,
                error_category=type(exc).__name__,
                summary=str(exc),
            )
        processed += 1
    from services.notifications import deliver_pending

    try:
        notifications = deliver_pending(store)
    except Exception as exc:
        notifications = {
            "status": "DELIVERY_ERROR",
            "sent": 0,
            "error_category": type(exc).__name__,
        }
        log_event(
            "notification_tick_failed",
            run_id="worker",
            error_category=type(exc).__name__,
            summary=str(exc),
        )
    result = {"processed": processed, "notifications": notifications}
    log_event("worker_tick_completed", run_id="worker", **result)
    return result
