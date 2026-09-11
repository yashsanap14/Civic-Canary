from __future__ import annotations

import json
import os
import uuid
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
        log_event("local_scan_started", run_id=resolved_run_id, target_id=target.target_id)
        try:
            baseline = self.store.get_baseline(target.target_id)
            if baseline is None:
                baseline_target = target.model_copy(
                    update={"active_version": target.baseline_version}
                )
                baseline = await self.browser.capture(baseline_target, "baseline-v1")
                self.store.put_baseline(baseline)
            playbook = self.store.load_playbook(target.playbook_key)
            engine = CivicCanaryEngine(self.browser)
            run, snapshot, findings = await engine.execute(
                target=target,
                baseline=baseline,
                playbook=playbook,
                trigger_type=trigger,
                run_id=resolved_run_id,
            )
            self.store.put_snapshot(snapshot)
            persisted = [finding for finding in findings if self.store.put_finding(finding)]
            run.summary = (
                "No new material findings."
                if not persisted
                else f"Created {len(persisted)} new finding(s) requiring review."
            )
            self.store.put_run(run)
            log_event(
                "local_scan_completed",
                run_id=run.run_id,
                status=run.status,
                new_findings=len(persisted),
            )
            return run, persisted
        except RunExecutionError as exc:
            self.store.put_run(exc.run)
            log_event(
                "local_scan_failed",
                run_id=exc.run.run_id,
                error_category=exc.run.error_category,
            )
            raise

    def agentcore_arn(self) -> str | None:
        arn = os.getenv("AGENT_RUNTIME_ARN")
        if arn:
            return arn
        parameter = os.getenv("AGENT_RUNTIME_SSM_PARAMETER")
        if not parameter:
            return None
        value = boto3.client(
            "ssm", region_name=_aws_region()
        ).get_parameter(Name=parameter)["Parameter"]["Value"]
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

    def queue_agentcore_scan(
        self, target: PortalTarget, trigger: TriggerType, run_id: str
    ) -> Run:
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
            response = boto3.client(
                "lambda", region_name=_aws_region()
            ).invoke(
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

