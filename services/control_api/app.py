from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from agent.civic_canary.engine import RunExecutionError
from agent.civic_canary.models import (
    DemoVersionRequest,
    Finding,
    FindingStatus,
    PortalTarget,
    ReviewDecision,
    RunRequest,
    TriggerType,
)
from services.observability import log_event
from services.runtime import ScanService
from services.security import ReviewTokenVerifier
from services.storage import Store
from services.store_factory import default_store


def create_app(store: Store | None = None, verifier: ReviewTokenVerifier | None = None) -> FastAPI:
    app = FastAPI(title="Civic Canary API", version="0.1.0")
    app.state.store = store or default_store()
    app.state.verifier = verifier or ReviewTokenVerifier()
    local_mode = os.getenv("CIVIC_CANARY_MODE", "local") != "aws"
    if local_mode and not app.state.store.get_target("benefits-demo"):
        app.state.store.put_target(PortalTarget())

    origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Review-Token"],
    )

    def require_token(x_review_token: str | None = Header(default=None)) -> None:
        if not app.state.verifier.verify(x_review_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid review token"
            )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "civic-canary"}

    @app.get("/api/targets", response_model=list[PortalTarget])
    def list_targets() -> list[PortalTarget]:
        return app.state.store.list_targets()

    @app.get("/api/runs")
    def list_runs():
        return app.state.store.list_runs()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        run = app.state.store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    @app.get("/api/findings", response_model=list[Finding])
    def list_findings(
        finding_status: Annotated[FindingStatus | None, Query(alias="status")] = None,
    ):
        findings = app.state.store.list_findings()
        return [
            finding
            for finding in findings
            if not finding_status or finding.status == finding_status
        ]

    @app.get("/api/findings/{finding_id}", response_model=Finding)
    def get_finding(finding_id: str):
        finding = app.state.store.get_finding(finding_id)
        if not finding:
            raise HTTPException(status_code=404, detail="finding not found")
        finding.evidence_urls = [
            url
            for key in finding.evidence_keys
            if (url := app.state.store.evidence_url(key)) is not None
        ]
        finding.screenshot_urls = [
            url
            for key in finding.evidence_keys
            if (key.startswith("screenshots/") or "/screenshots/" in key)
            and (url := app.state.store.evidence_url(key)) is not None
        ]
        return finding

    @app.post("/api/runs", dependencies=[Depends(require_token)])
    async def start_run(request: RunRequest, response: Response):
        target = app.state.store.get_target(request.target_id)
        if not target:
            raise HTTPException(status_code=404, detail="target not found")
        if request.idempotency_key:
            run_id = f"run-{request.idempotency_key}"
            existing = app.state.store.get_run(run_id)
            if existing:
                return {"run": existing, "findings": []}
        else:
            run_id = f"run-{uuid.uuid4().hex[:12]}"
        log_event(
            "manual_scan_requested",
            run_id=run_id,
            target_id=target.target_id,
        )
        service = ScanService(app.state.store)
        if service.agentcore_arn():
            try:
                queued = service.queue_agentcore_scan(target, TriggerType.MANUAL, run_id)
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "run_id": run_id,
                        "error_category": type(exc).__name__,
                        "message": str(exc),
                    },
                ) from exc
            response.status_code = status.HTTP_202_ACCEPTED
            return {"run": queued, "findings": []}
        try:
            run, findings = await service.run_local(target, TriggerType.MANUAL, run_id)
        except RunExecutionError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "run_id": exc.run.run_id,
                    "error_category": exc.run.error_category,
                    "message": exc.run.summary,
                },
            ) from exc
        return {"run": run, "findings": findings}

    @app.post("/api/demo/version", dependencies=[Depends(require_token)])
    def set_demo_version(request: DemoVersionRequest):
        target = app.state.store.get_target(request.target_id)
        if not target:
            raise HTTPException(status_code=404, detail="target not found")
        target.active_version = request.version
        app.state.store.put_target(target)
        return target

    @app.post(
        "/api/findings/{finding_id}/decision",
        dependencies=[Depends(require_token)],
    )
    def decide(
        finding_id: str,
        decision: ReviewDecision,
        x_review_token: str | None = Header(default=None),
    ):
        finding = app.state.store.get_finding(finding_id)
        if not finding:
            raise HTTPException(status_code=404, detail="finding not found")
        resuming_approval = (
            finding.status == FindingStatus.APPROVAL_PENDING
            and decision.action == "APPROVE"
        )
        if finding.status != FindingStatus.OPEN and not resuming_approval:
            raise HTTPException(status_code=409, detail="finding already decided")
        decided_at = datetime.now(UTC)
        artifact_key = None
        if decision.action == "REJECT":
            rejected = finding.model_copy(
                update={
                    "status": FindingStatus.REJECTED,
                    "decision_note": decision.note,
                    "decided_at": decided_at,
                }
            )
            if not app.state.store.transition_finding(rejected, FindingStatus.OPEN):
                raise HTTPException(status_code=409, detail="finding already decided")
            finding = rejected
        else:
            if resuming_approval:
                pending = finding
            else:
                pending = finding.model_copy(
                    update={
                        "status": FindingStatus.APPROVAL_PENDING,
                        "decision_note": decision.note,
                        "decided_at": decided_at,
                    }
                )
                if not app.state.store.transition_finding(pending, FindingStatus.OPEN):
                    raise HTTPException(status_code=409, detail="finding already decided")
            try:
                artifact_key = app.state.store.write_approved_artifact(pending)
            except Exception as exc:
                rollback = pending.model_copy(
                    update={
                        "status": FindingStatus.OPEN,
                        "decision_note": None,
                        "decided_at": None,
                    }
                )
                app.state.store.transition_finding(
                    rollback, FindingStatus.APPROVAL_PENDING
                )
                raise HTTPException(
                    status_code=502, detail="approved draft could not be stored"
                ) from exc
            approved = pending.model_copy(
                update={
                    "status": FindingStatus.APPROVED,
                    "approved_artifact_key": artifact_key,
                }
            )
            if app.state.store.transition_finding(
                approved, FindingStatus.APPROVAL_PENDING
            ):
                finding = approved
            else:
                latest = app.state.store.get_finding(finding_id)
                if latest is None or latest.status != FindingStatus.APPROVED:
                    raise HTTPException(status_code=409, detail="finding state changed")
                finding = latest
                artifact_key = latest.approved_artifact_key
        app.state.store.record_review(
            finding_id=finding.finding_id,
            action=finding.status.value,
            note=decision.note,
            reviewer=(
                "sha256:" + hashlib.sha256(x_review_token.encode()).hexdigest()
                if x_review_token else "reviewer"
            ),
        )
        log_event(
            "review_decision_recorded",
            run_id=finding.run_id,
            finding_id=finding.finding_id,
            decision=finding.status,
        )
        return {"finding": finding, "approved_artifact_key": artifact_key}

    static_root = Path(__file__).resolve().parents[2] / "web" / "dist"
    if static_root.is_dir():
        app.mount("/", StaticFiles(directory=static_root, html=True), name="reviewer-ui")

    return app


app = create_app()
