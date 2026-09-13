from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3

from agent.civic_canary.browser import BrowserAdapter, create_browser_adapter
from agent.civic_canary.engine import CivicCanaryEngine, RunExecutionError
from agent.civic_canary.models import Finding, PortalTarget, Run, RunStatus, TriggerType
from services.observability import log_event
from services.storage import Store

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "web" / "public" / "portal"


def _aws_region() -> str:
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")


class ScanService:
    def __init__(self, store: Store, browser: BrowserAdapter | None = None) -> None:
        self.store = store
        self.browser = browser or create_browser_adapter(fixture_root=FIXTURES)

    async def run_local(
        self,
        target: PortalTarget,
        trigger: TriggerType,
        run_id: str | None = None,
    ) -> tuple[Run, list[Finding]]:
        resolved_run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        production = os.getenv("CIVIC_CANARY_MODE", "local") == "aws"
        run = Run(
            run_id=resolved_run_id,
            target_id=target.target_id,
            trigger_type=trigger,
            status=RunStatus.RUNNING,
            started_at=datetime.now(UTC),
        )
        existing = self.store.get_run(resolved_run_id)
        if existing and existing.started_at:
            run.started_at = existing.started_at
        if production:
            run.reasoning_source = "strands-bedrock"
        self.store.put_run(run)
        log_event(
            "scan_started",
            run_id=resolved_run_id,
            target_id=target.target_id,
            trigger_type=trigger.value if hasattr(trigger, "value") else str(trigger),
            setup_status=target.setup_status,
            production=production,
            has_baseline=self.store.get_baseline(target.target_id) is not None,
        )
        try:
            baseline = self.store.get_baseline(target.target_id)
            if production:
                needs_setup = target.setup_status in {"PENDING", "FAILED"}
                if needs_setup:
                    baseline = None
                from agent.civic_canary.browser import AgentCoreBrowserAdapter
                from agent.civic_canary.reasoning import StrandsReasoner

                if not isinstance(self.browser, AgentCoreBrowserAdapter):
                    raise RuntimeError("Production scans require AgentCore Browser")
                guidance = target.guidance_context
                if guidance is None:
                    guidance = self.store.load_playbook(target.playbook_key)

                def capture():
                    for attempt in range(2):
                        try:
                            return asyncio.run(self.browser.capture(target, resolved_run_id))
                        except Exception as first_error:
                            if attempt:
                                raise
                            run.retry_count += 1
                            log_event(
                                "browser_capture_retry",
                                run_id=resolved_run_id,
                                target_id=target.target_id,
                                error_category=type(first_error).__name__,
                                summary=str(first_error),
                            )

                snapshot, findings, trace, timings = await asyncio.to_thread(
                    StrandsReasoner().execute, target, baseline, resolved_run_id, guidance, capture
                )
                run.review_memo = trace
                run.reasoning_source = "strands-bedrock"
                run.node_timings = timings
                # Preserve the complete graph output before publishing findings.
                self.store.put_run(run)
                if baseline is None:
                    self.store.put_baseline(snapshot)
                    log_event(
                        "baseline_saved",
                        run_id=resolved_run_id,
                        target_id=target.target_id,
                        setup=needs_setup,
                        pages=len(snapshot.pages),
                    )
                    # Only live/setup onboarding enters AWAITING_CONFIRMATION. ACTIVE demos
                    # keep ACTIVE after their first baseline so V1→V2 runs stay unblocked.
                    if needs_setup:
                        latest = self.store.get_target(target.target_id) or target
                        latest.recommended_sections = trace["packet"]["recommended_sections"]
                        latest.setup_status = "AWAITING_CONFIRMATION"
                        latest.setup_run_id = resolved_run_id
                        self.store.put_target(latest)
                        target = latest
                        run.status = RunStatus.SUCCEEDED
                        run.summary = "Baseline captured. Confirm the sections to monitor."
                        run.finished_at = datetime.now(UTC)
                        run.notification_status = "NOT_REQUIRED"
                        self.store.put_snapshot(snapshot)
                        self.store.put_run(run)
                        log_event(
                            "setup_awaiting_confirmation",
                            run_id=resolved_run_id,
                            target_id=target.target_id,
                            sections=len(target.recommended_sections),
                        )
                        return run, []
            else:
                from agent.civic_canary.browser import HttpBrowserAdapter

                live_site = target.kind == "live" or (
                    target.target_id != "benefits-demo"
                    and "{version}" not in (target.start_url or "")
                    and not (target.start_url or "").startswith("fixture:")
                )
                browser = (
                    self.browser
                    if isinstance(self.browser, HttpBrowserAdapter) or not live_site
                    else HttpBrowserAdapter()
                )
                if live_site and target.setup_status in {"PENDING", "FAILED"}:
                    snapshot = await browser.capture(target, resolved_run_id)
                    self.store.put_baseline(snapshot)
                    self.store.put_snapshot(snapshot)
                    headings = list(
                        dict.fromkeys(
                            heading for page in snapshot.pages for heading in page.headings
                        )
                    )[:20]
                    target.recommended_sections = headings or ["Main content"]
                    target.setup_status = "AWAITING_CONFIRMATION"
                    target.setup_run_id = resolved_run_id
                    self.store.put_target(target)
                    run.status = RunStatus.SUCCEEDED
                    run.summary = "Baseline captured. Confirm the sections to monitor."
                    run.finished_at = datetime.now(UTC)
                    run.reasoning_source = "http-baseline"
                    self.store.put_run(run)
                    return run, []
                if baseline is None:
                    baseline_target = target.model_copy(
                        update={"active_version": target.baseline_version}
                    )
                    baseline = await browser.capture(
                        baseline_target, f"baseline-{resolved_run_id}"
                    )
                    self.store.put_baseline(baseline)
                engine = CivicCanaryEngine(browser)
                run, snapshot, findings = await engine.execute(
                    target=target,
                    baseline=baseline,
                    playbook=target.guidance_context
                    if target.guidance_context is not None
                    else self.store.load_playbook(target.playbook_key),
                    trigger_type=trigger,
                    run_id=resolved_run_id,
                )
            self.store.put_snapshot(snapshot)
            for finding in findings:
                finding.evidence_keys = list(
                    dict.fromkeys(
                        finding.evidence_keys
                        + [
                            f"snapshots/{target.target_id}/{resolved_run_id}/snapshot.json",
                            f"baselines/{target.target_id}.json",
                        ]
                        + [page.screenshot_key for page in snapshot.pages if page.screenshot_key]
                    )
                )
                if baseline:
                    finding.evidence_keys.extend(
                        page.screenshot_key for page in baseline.pages if page.screenshot_key
                    )
            persisted = [finding for finding in findings if self.store.put_finding(finding)]
            run.status = RunStatus.SUCCEEDED
            run.summary = (
                f"Created {len(persisted)} new finding(s) requiring review."
                if persisted
                else "No new material findings; no notification required."
            )
            run.finished_at = datetime.now(UTC)
            if production:
                from services.notifications import queue_notifications

                # Queue all resulting findings: retries repair a failure after finding persistence.
                for finding in findings:
                    queue_notifications(self.store, finding)
                run.notification_status = "PENDING" if persisted else "NOT_REQUIRED"
                latest = self.store.get_target(target.target_id) or target
                latest.last_scan_at = run.finished_at
                latest.next_scan_at = run.finished_at + timedelta(
                    minutes=latest.scan_frequency_minutes
                )
                self.store.put_target(latest)
                target = latest
            self.store.put_run(run)
            log_event(
                "scan_completed",
                run_id=run.run_id,
                target_id=target.target_id,
                engine=run.reasoning_source,
                new_findings=len(persisted),
                status=run.status,
                setup_status=target.setup_status,
            )
            return run, persisted
        except Exception as exc:
            if isinstance(exc, RunExecutionError):
                run = exc.run
            run.status = RunStatus.FAILED
            run.finished_at = datetime.now(UTC)
            run.error_category = (
                type(exc.cause).__name__
                if isinstance(exc, RunExecutionError)
                else type(exc).__name__
            )
            run.summary = str(exc)
            self.store.put_run(run)
            # Allow a failed ACTIVE site to be retried on the next worker tick.
            latest = self.store.get_target(target.target_id)
            if latest and latest.setup_status == "ACTIVE":
                latest.next_scan_at = datetime.now(UTC)
                self.store.put_target(latest)
            log_event(
                "scan_failed",
                run_id=run.run_id,
                target_id=target.target_id,
                error_category=run.error_category,
                setup_status=getattr(latest or target, "setup_status", None),
            )
            if isinstance(exc, RunExecutionError):
                raise
            raise RunExecutionError(run, exc) from exc

    def agentcore_arn(self) -> str | None:
        arn = os.getenv("AGENT_RUNTIME_ARN")
        if arn:
            return arn
        parameter = os.getenv("AGENT_RUNTIME_SSM_PARAMETER")
        if not parameter:
            return None
        value = boto3.client("ssm", region_name=_aws_region()).get_parameter(Name=parameter)[
            "Parameter"
        ]["Value"]
        return None if value == "UNCONFIGURED" else value

    def invoke_agentcore(self, target: PortalTarget, trigger: TriggerType, run_id: str) -> dict:
        arn = self.agentcore_arn()
        if not arn:
            raise RuntimeError("AgentCore runtime ARN is not configured")
        client = boto3.client("bedrock-agentcore", region_name=_aws_region())
        runtime_session_id = f"civic-canary-{run_id}-{uuid.uuid4().hex[:8]}"
        log_event(
            "agentcore_invocation_started",
            run_id=run_id,
            target_id=target.target_id,
            runtime_session_id=runtime_session_id,
        )
        response = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=runtime_session_id,
            payload=json.dumps(
                {
                    "run_id": run_id,
                    "target": target.model_dump(mode="json"),
                    "trigger_type": trigger.value,
                }
            ),
            qualifier="DEFAULT",
        )
        result = json.loads(response["response"].read())
        log_event(
            "agentcore_invocation_completed",
            run_id=run_id,
            runtime_session_id=runtime_session_id,
        )
        return result

    def queue_agentcore_scan(self, target: PortalTarget, trigger: TriggerType, run_id: str) -> Run:
        """Persist a durable queue record and asynchronously dispatch the scan worker."""
        worker = os.getenv("SCAN_WORKER_FUNCTION_NAME")
        if not worker:
            raise RuntimeError("scan worker Lambda is not configured")
        run = Run(
            run_id=run_id,
            target_id=target.target_id,
            trigger_type=trigger,
            status=RunStatus.QUEUED,
        )
        if not self.store.create_run(run):
            existing = self.store.get_run(run_id)
            if existing is None:
                raise RuntimeError("run id was reserved but could not be loaded")
            return existing
        try:
            response = boto3.client("lambda", region_name=_aws_region()).invoke(
                FunctionName=worker,
                InvocationType="Event",
                Payload=json.dumps(
                    {
                        "run_id": run_id,
                        "target_id": target.target_id,
                        "trigger_type": trigger.value,
                    }
                ).encode(),
            )
            if response.get("StatusCode") != 202:
                raise RuntimeError("scan worker did not accept the asynchronous invocation")
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error_category = type(exc).__name__
            run.summary = f"Could not dispatch scan worker: {exc}"
            self.store.put_run(run)
            raise
        log_event(
            "scan_queued",
            run_id=run_id,
            target_id=target.target_id,
            trigger_type=trigger.value,
        )
        return run
