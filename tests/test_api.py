from pathlib import Path

from fastapi.testclient import TestClient

from services.control_api.app import create_app
from services.security import ReviewTokenVerifier
from services.storage import InMemoryStore

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = (ROOT / "agent" / "fixtures" / "benefits-playbook.md").read_text()


def client(monkeypatch) -> tuple[TestClient, InMemoryStore]:
    monkeypatch.setenv("REVIEW_TOKEN", "review-demo")
    store = InMemoryStore(playbook=PLAYBOOK)
    return TestClient(create_app(store, ReviewTokenVerifier())), store


def test_public_reads_and_protected_actions(monkeypatch) -> None:
    api, _ = client(monkeypatch)
    assert api.get("/api/health").status_code == 200
    assert api.get("/api/targets").status_code == 200
    assert api.post("/api/runs", json={"target_id": "benefits-demo"}).status_code == 401


def test_demo_scan_and_approval(monkeypatch) -> None:
    api, store = client(monkeypatch)
    headers = {"X-Review-Token": "review-demo"}
    switched = api.post(
        "/api/demo/version",
        headers=headers,
        json={"target_id": "benefits-demo", "version": "v2"},
    )
    assert switched.status_code == 200
    result = api.post(
        "/api/runs",
        headers=headers,
        json={"target_id": "benefits-demo", "idempotency_key": "acceptance"},
    )
    assert result.status_code == 200
    payload = result.json()
    assert payload["run"]["status"] == "SUCCEEDED"
    assert len(payload["findings"]) == 3

    finding_id = payload["findings"][0]["finding_id"]
    approved = api.post(
        f"/api/findings/{finding_id}/decision",
        headers=headers,
        json={"action": "APPROVE", "note": "Evidence checked."},
    )
    assert approved.status_code == 200
    artifact_key = approved.json()["approved_artifact_key"]
    assert artifact_key in store.artifacts
    assert "not published automatically" in store.artifacts[artifact_key]
    assert store.get_finding(finding_id).approved_artifact_key == artifact_key

    second_decision = api.post(
        f"/api/findings/{finding_id}/decision",
        headers=headers,
        json={"action": "REJECT", "note": "Too late."},
    )
    assert second_decision.status_code == 409

    repeated = api.post(
        "/api/runs",
        headers=headers,
        json={"target_id": "benefits-demo", "idempotency_key": "acceptance-repeat"},
    )
    assert repeated.status_code == 200
    assert len(repeated.json()["findings"]) == 0
    assert store.get_finding(finding_id).status == "APPROVED"


def test_idempotency_does_not_duplicate_runs(monkeypatch) -> None:
    api, _ = client(monkeypatch)
    headers = {"X-Review-Token": "review-demo"}
    body = {"target_id": "benefits-demo", "idempotency_key": "same-request"}
    first = api.post("/api/runs", headers=headers, json=body)
    second = api.post("/api/runs", headers=headers, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["run"]["run_id"] == second.json()["run"]["run_id"]


def test_approval_storage_failure_rolls_back_to_open(monkeypatch) -> None:
    api, store = client(monkeypatch)
    headers = {"X-Review-Token": "review-demo"}
    api.post(
        "/api/demo/version",
        headers=headers,
        json={"target_id": "benefits-demo", "version": "v2"},
    )
    result = api.post("/api/runs", headers=headers, json={"target_id": "benefits-demo"})
    finding_id = result.json()["findings"][0]["finding_id"]

    def fail_write(_finding):
        raise OSError("simulated S3 outage")

    monkeypatch.setattr(store, "write_approved_artifact", fail_write)
    response = api.post(
        f"/api/findings/{finding_id}/decision",
        headers=headers,
        json={"action": "APPROVE", "note": "Checked."},
    )
    assert response.status_code == 502
    assert store.get_finding(finding_id).status == "OPEN"


def test_delete_live_website_removes_target_scoped_records(monkeypatch) -> None:
    from agent.civic_canary.models import (
        Finding,
        FindingStatus,
        JourneyStep,
        PortalSnapshot,
        PortalTarget,
        Run,
        RunStatus,
        TriggerType,
    )

    api, store = client(monkeypatch)
    headers = {"X-Review-Token": "review-demo"}
    live = PortalTarget(
        target_id="site-deleteme01",
        kind="live",
        name="Deletable site",
        start_url="https://example.org/services",
        allowed_hosts=["example.org"],
        journey_steps=[JourneyStep(path="/", label="Home")],
        setup_status="ACTIVE",
        enabled=True,
    )
    keep = PortalTarget(
        target_id="site-keepme0001",
        kind="live",
        name="Keep site",
        start_url="https://example.org/other",
        allowed_hosts=["example.org"],
        journey_steps=[JourneyStep(path="/", label="Home")],
        setup_status="ACTIVE",
        enabled=True,
    )
    store.put_target(live)
    store.put_target(keep)
    run = Run(
        run_id="run-delete-1",
        target_id=live.target_id,
        trigger_type=TriggerType.MANUAL,
        status=RunStatus.SUCCEEDED,
    )
    keep_run = Run(
        run_id="run-keep-1",
        target_id=keep.target_id,
        trigger_type=TriggerType.MANUAL,
        status=RunStatus.SUCCEEDED,
    )
    store.put_run(run)
    store.put_run(keep_run)
    store.put_json(f"jobs/{run.run_id}.json", {"run_id": run.run_id})
    store.put_json(f"recent-runs/0001-{run.run_id}.json", run.model_dump(mode="json"))
    store.put_baseline(
        PortalSnapshot(
            target_id=live.target_id,
            run_id=run.run_id,
            version="live",
            pages=[],
            content_hash="abc123",
        )
    )
    finding = Finding(
        finding_id="finding-delete-1",
        run_id=run.run_id,
        target_id=live.target_id,
        title="Hours changed",
        category="CONTENT",
        severity="MEDIUM",
        materiality="MATERIAL",
        evidence=["before", "after"],
        affected_playbook_sections=["Hours"],
        proposed_patch="Update hours",
        status=FindingStatus.OPEN,
    )
    store.put_finding(finding)
    store.put_json(f"outbox/{finding.finding_id}.json", {"finding_id": finding.finding_id})
    keep_finding = Finding(
        finding_id="finding-keep-1",
        run_id=keep_run.run_id,
        target_id=keep.target_id,
        title="Keep me",
        category="CONTENT",
        severity="LOW",
        materiality="COSMETIC",
        evidence=["ok"],
        affected_playbook_sections=[],
        proposed_patch="",
        status=FindingStatus.OPEN,
    )
    store.put_finding(keep_finding)

    denied = api.delete("/api/targets/benefits-demo", headers=headers)
    assert denied.status_code == 403
    assert store.get_target("benefits-demo") is not None

    missing = api.delete("/api/targets/site-missing", headers=headers)
    assert missing.status_code == 404

    deleted = api.delete(f"/api/targets/{live.target_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
    assert store.get_target(live.target_id) is None
    assert store.get_run(run.run_id) is None
    assert store.get_finding(finding.finding_id) is None
    assert store.get_baseline(live.target_id) is None
    assert store.get_json(f"jobs/{run.run_id}.json") is None
    assert store.get_json(f"outbox/{finding.finding_id}.json") is None

    assert store.get_target(keep.target_id) is not None
    assert store.get_run(keep_run.run_id) is not None
    assert store.get_finding(keep_finding.finding_id) is not None


def test_delete_rejects_running_scan(monkeypatch) -> None:
    from agent.civic_canary.models import JourneyStep, PortalTarget, Run, RunStatus, TriggerType

    api, store = client(monkeypatch)
    headers = {"X-Review-Token": "review-demo"}
    live = PortalTarget(
        target_id="site-running001",
        kind="live",
        name="Busy site",
        start_url="https://example.org/busy",
        allowed_hosts=["example.org"],
        journey_steps=[JourneyStep(path="/", label="Home")],
        setup_status="ACTIVE",
    )
    store.put_target(live)
    store.put_run(
        Run(
            run_id="run-busy",
            target_id=live.target_id,
            trigger_type=TriggerType.MANUAL,
            status=RunStatus.RUNNING,
        )
    )
    response = api.delete(f"/api/targets/{live.target_id}", headers=headers)
    assert response.status_code == 409
    assert store.get_target(live.target_id) is not None
