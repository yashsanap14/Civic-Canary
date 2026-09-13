from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from agent.civic_canary.engine import RunExecutionError
from agent.civic_canary.models import (
    AddWebsiteRequest,
    ConfirmMonitoringRequest,
    DemoVersionRequest,
    Finding,
    FindingStatus,
    JourneyStep,
    PortalTarget,
    ReviewDecision,
    RunRequest,
    TriggerType,
)
from agent.civic_canary.scenarios import seed_targets
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
    if local_mode:
        for target in seed_targets():
            if not app.state.store.get_target(target.target_id):
                app.state.store.put_target(target)

    origins = [
        origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "X-Review-Token"],
    )

    @app.middleware("http")
    async def prevent_api_caching(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require_token(x_review_token: str | None = Header(default=None)) -> None:
        if not app.state.verifier.verify(x_review_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid review token"
            )

    def require_read(x_review_token: str | None = Header(default=None)) -> None:
        if not local_mode:
            require_token(x_review_token)

    @app.post("/api/targets", dependencies=[Depends(require_token)])
    async def add_website(request: AddWebsiteRequest):
        from agent.civic_canary.browser import HttpBrowserAdapter, validate_public_url
        from services.monitoring import enqueue_scan

        try:
            validate_public_url(request.public_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        host = urlparse(request.public_url).hostname
        if not host:
            raise HTTPException(422, "Public URL must include a hostname")
        target = PortalTarget(
            target_id=f"site-{uuid.uuid4().hex[:12]}",
            name=request.name,
            kind="live",
            start_url=request.public_url,
            allowed_hosts=[host],
            journey_steps=[JourneyStep(path="/", label="Submitted page")],
            description=request.description,
            monitoring_objective=request.monitoring_objective,
            scan_frequency_minutes=request.scan_frequency_minutes,
            guidance_context=request.guidance_context,
            setup_status="PENDING",
            change_summary="No scripted change—future scans compare against the captured live baseline.",
        )
        app.state.store.put_target(target)
        if local_mode:
            # Local/hackathon path: inspect immediately over HTTPS (no AgentCore worker).
            service = ScanService(app.state.store, HttpBrowserAdapter())
            try:
                run, _ = await service.run_local(target, TriggerType.MANUAL)
            except RunExecutionError as exc:
                failed = app.state.store.get_target(target.target_id) or target
                failed.setup_status = "FAILED"
                failed.setup_run_id = exc.run.run_id
                app.state.store.put_target(failed)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "run_id": exc.run.run_id,
                        "error_category": exc.run.error_category,
                        "message": exc.run.summary or "Live website inspection failed",
                    },
                ) from exc
            return app.state.store.get_target(target.target_id) or target
        run = enqueue_scan(app.state.store, target)
        target.setup_run_id = run.run_id
        app.state.store.put_target(target)
        return target

    @app.get("/api/targets/{target_id}", dependencies=[Depends(require_read)])
    def get_target(target_id: str):
        target = app.state.store.get_target(target_id)
        if not target:
            raise HTTPException(404, "Website not found")
        return target

    def _is_demo_target(target: PortalTarget) -> bool:
        return (
            target.kind == "demo"
            or target.target_id.endswith("-demo")
            or target.target_id == "benefits-demo"
        )

    @app.delete("/api/targets/{target_id}", dependencies=[Depends(require_token)])
    def delete_website(target_id: str):
        target = app.state.store.get_target(target_id)
        if not target:
            raise HTTPException(404, "Website not found")
        if _is_demo_target(target) or target.kind != "live":
            raise HTTPException(403, "Demo scenarios cannot be deleted from the live dashboard")

        active_runs = [
            run
            for run in app.state.store.list_runs()
            if run.target_id == target_id and run.status in {"QUEUED", "RUNNING"}
        ]
        if any(run.status == "RUNNING" for run in active_runs):
            raise HTTPException(
                409,
                "A scan is currently running for this website. Try again after it finishes.",
            )

        # Drop queued jobs for this target before removing the site row so workers
        # cannot pick up orphaned work after deletion.
        for run in active_runs:
            try:
                app.state.store.delete_json(f"jobs/{run.run_id}.json")
            except Exception:
                pass

        try:
            summary = app.state.store.delete_target(target_id)
        except Exception as exc:
            log_event(
                "target_delete_failed",
                run_id=f"delete-{target_id}",
                target_id=target_id,
                error_category=type(exc).__name__,
                summary=str(exc),
            )
            raise HTTPException(
                502,
                "The website could not be deleted completely. Check storage permissions and try again.",
            ) from exc

        log_event(
            "target_deleted",
            run_id=f"delete-{target_id}",
            target_id=target_id,
            name=target.name,
            **{key: value for key, value in summary.items() if isinstance(value, int)},
        )
        return {"ok": True, "target_id": target_id, "deleted": summary}

    @app.post("/api/targets/{target_id}/inspect", dependencies=[Depends(require_token)])
    async def inspect_website(target_id: str):
        from agent.civic_canary.browser import HttpBrowserAdapter
        from services.monitoring import enqueue_scan

        target = get_target(target_id)
        if target.setup_status not in {"PENDING", "FAILED"}:
            raise HTTPException(409, "Website inspection is already complete")
        if target.setup_run_id:
            existing = app.state.store.get_run(target.setup_run_id)
            if existing and existing.status == "RUNNING":
                return {"run": existing}
            if existing and existing.status == "QUEUED":
                # Repair a missing jobs/*.json object so the worker can pick it up.
                run = enqueue_scan(
                    app.state.store, target, TriggerType.MANUAL, existing.run_id
                )
                return {"run": run}
        target.setup_status = "PENDING"
        app.state.store.put_target(target)
        if local_mode:
            service = ScanService(app.state.store, HttpBrowserAdapter())
            try:
                run, _ = await service.run_local(target, TriggerType.MANUAL)
            except RunExecutionError as exc:
                failed = app.state.store.get_target(target_id) or target
                failed.setup_status = "FAILED"
                failed.setup_run_id = exc.run.run_id
                app.state.store.put_target(failed)
                raise HTTPException(
                    status_code=502,
                    detail={
                        "run_id": exc.run.run_id,
                        "error_category": exc.run.error_category,
                        "message": exc.run.summary or "Live website inspection failed",
                    },
                ) from exc
            return {"run": run}
        run = enqueue_scan(app.state.store, target)
        target.setup_run_id = run.run_id
        app.state.store.put_target(target)
        return {"run": run}

    @app.post("/api/targets/{target_id}/confirm", dependencies=[Depends(require_token)])
    def confirm_website(target_id: str, request: ConfirmMonitoringRequest):
        target = get_target(target_id)
        if target.setup_status not in {"AWAITING_CONFIRMATION", "ACTIVE"}:
            raise HTTPException(409, "Successful baseline inspection is required first")
        if app.state.store.get_baseline(target_id) is None:
            raise HTTPException(409, "Baseline evidence is missing")
        target.monitored_sections = request.monitored_sections
        target.setup_status = "ACTIVE"
        target.next_scan_at = datetime.now(UTC) + timedelta(minutes=target.scan_frequency_minutes)
        app.state.store.put_target(target)
        return target

    @app.get("/api/findings-page", dependencies=[Depends(require_read)])
    def finding_page(limit: int = Query(50, ge=1, le=100), cursor: str | None = None):
        try:
            key = json.loads(base64.urlsafe_b64decode(cursor)) if cursor else None
            if key is not None and (
                set(key) != {"findingId"} or not isinstance(key["findingId"], str)
            ):
                raise ValueError("Invalid cursor")
        except Exception as exc:
            raise HTTPException(422, "Invalid cursor") from exc
        rows, next_key = app.state.store.finding_page(limit, key)
        next_cursor = (
            base64.urlsafe_b64encode(json.dumps(next_key).encode()).decode() if next_key else None
        )
        return {"items": rows, "next_cursor": next_cursor}

    @app.get("/api/findings/{finding_id}/artifact", dependencies=[Depends(require_token)])
    def approved_artifact(finding_id: str):
        finding = app.state.store.get_finding(finding_id)
        if (
            not finding
            or finding.status != FindingStatus.APPROVED
            or not finding.approved_artifact_key
        ):
            raise HTTPException(404, "Approved artifact is unavailable")
        return Response(
            app.state.store.read_artifact(finding.approved_artifact_key),
            media_type="text/markdown",
            headers={
                "Content-Disposition": 'attachment; filename="approved-guidance.md"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/health")
    def health() -> dict[str, str | bool]:
        payload: dict[str, str | bool] = {
            "status": "ok",
            "service": "civic-canary",
            "mode": "local" if local_mode else "aws",
        }
        if not local_mode:
            try:
                app.state.store.target_page(1)
                payload["storage"] = "ok"
            except Exception as exc:
                payload["status"] = "degraded"
                payload["storage"] = type(exc).__name__
        return payload

    @app.get(
        "/api/targets", response_model=list[PortalTarget], dependencies=[Depends(require_read)]
    )
    def list_targets() -> list[PortalTarget]:
        return app.state.store.target_page(100)[0]

    @app.get("/api/runs", dependencies=[Depends(require_read)])
    def list_runs():
        return app.state.store.recent_runs(50)

    @app.get("/api/runs/{run_id}", dependencies=[Depends(require_read)])
    def get_run(run_id: str):
        run = app.state.store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    @app.get("/api/findings", response_model=list[Finding], dependencies=[Depends(require_read)])
    def list_findings(
        finding_status: Annotated[FindingStatus | None, Query(alias="status")] = None,
    ):
        findings = app.state.store.finding_page(100)[0]
        return [
            finding
            for finding in findings
            if not finding_status or finding.status == finding_status
        ]

    @app.get(
        "/api/findings/{finding_id}", response_model=Finding, dependencies=[Depends(require_read)]
    )
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
                if existing.target_id != target.target_id:
                    raise HTTPException(409, "Idempotency key belongs to another website")
                if not local_mode and existing.status == "QUEUED":
                    from services.monitoring import enqueue_scan

                    existing = enqueue_scan(app.state.store, target, TriggerType.MANUAL, run_id)
                return {"run": existing, "findings": []}
        else:
            run_id = f"run-{uuid.uuid4().hex[:12]}"
        log_event(
            "manual_scan_requested",
            run_id=run_id,
            target_id=target.target_id,
        )
        if not local_mode:
            from services.monitoring import enqueue_scan

            if target.setup_status != "ACTIVE":
                raise HTTPException(409, "Confirm monitoring sections before scanning")
            response.status_code = status.HTTP_202_ACCEPTED
            return {
                "run": enqueue_scan(app.state.store, target, TriggerType.MANUAL, run_id),
                "findings": [],
            }
        if target.target_id != "benefits-demo" and target.kind != "demo" and target.setup_status != "ACTIVE":
            raise HTTPException(409, "Confirm monitoring sections before scanning")
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
            finding.status == FindingStatus.APPROVAL_PENDING and decision.action == "APPROVE"
        )
        if finding.status != FindingStatus.OPEN and not resuming_approval:
            raise HTTPException(status_code=409, detail="finding already decided")
        reviewer = "sha256:" + hashlib.sha256((x_review_token or "reviewer").encode()).hexdigest()
        decided_at = datetime.now(UTC)
        artifact_key = None
        if decision.action == "REJECT":
            rejected = finding.model_copy(
                update={
                    "status": FindingStatus.REJECTED,
                    "reviewed_by": reviewer,
                    "decision_note": decision.note,
                    "decided_at": decided_at,
                }
            )
            if not app.state.store.commit_review(rejected, FindingStatus.OPEN):
                raise HTTPException(status_code=409, detail="finding already decided")
            finding = rejected
        else:
            if resuming_approval:
                pending = finding
            else:
                pending = finding.model_copy(
                    update={
                        "status": FindingStatus.APPROVAL_PENDING,
                        "reviewed_by": reviewer,
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
                app.state.store.transition_finding(rollback, FindingStatus.APPROVAL_PENDING)
                raise HTTPException(
                    status_code=502, detail="approved draft could not be stored"
                ) from exc
            approved = pending.model_copy(
                update={
                    "status": FindingStatus.APPROVED,
                    "approved_artifact_key": artifact_key,
                    "reviewed_by": reviewer,
                }
            )
            if app.state.store.commit_review(approved, FindingStatus.APPROVAL_PENDING):
                finding = approved
            else:
                latest = app.state.store.get_finding(finding_id)
                if latest is None or latest.status != FindingStatus.APPROVED:
                    raise HTTPException(status_code=409, detail="finding state changed")
                finding = latest
                artifact_key = latest.approved_artifact_key
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
