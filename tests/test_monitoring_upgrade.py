import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from agent.civic_canary.browser import (
    AgentCoreBrowserAdapter,
    HttpBrowserAdapter,
    UnsafeTargetError,
    _page_from_html,
    _snapshot_hash,
    page_url_for,
    validate_public_url,
)
from agent.civic_canary.engine import RunExecutionError
from agent.civic_canary.models import FindingStatus, JourneyStep, PortalSnapshot, PortalTarget, Run
from agent.civic_canary.reasoning import (
    DecisionPacket,
    StrandsReasoner,
    evidence_catalog,
    grounded_findings,
)
from services.control_api.app import create_app
from services.monitoring import run_due
from services.notifications import deliver_pending, queue_notifications
from services.runtime import ScanService
from services.storage import AwsStore, InMemoryStore

CASES = json.loads((Path(__file__).parent / "fixtures/monitoring_cases.json").read_text())


def snapshot(html, site="benefits", run_id="r1"):
    page = _page_from_html(
        f'<html lang="en">{html}</html>',
        url=f"https://{site}.example.org/info",
        link_status=lambda _: 200,
    )
    return PortalSnapshot(
        target_id=site,
        run_id=run_id,
        version="live",
        pages=[page],
        content_hash=_snapshot_hash([page]),
    )


def target(site="benefits"):
    return PortalTarget(
        target_id=site,
        start_url=f"https://{site}.example.org/info",
        allowed_hosts=[f"{site}.example.org"],
        journey_steps=[JourneyStep(path="/", label="Page")],
        guidance_context="",
        scan_frequency_minutes=5,
    )


def packet(
    before="Bring proof of identity.", after="Bring proof of identity and a current award letter."
):
    return DecisionPacket.model_validate(
        {
            "summary": "Document change",
            "recommendations": [
                {
                    "title": "Document checklist changed",
                    "category": "REQUIREMENT",
                    "before": {"source_id": "before:p0", "quote": before},
                    "after": {"source_id": "after:p0", "quote": after},
                    "why_it_matters": "Staff should verify the revised checklist.",
                    "affected_people": "People preparing documents for this service",
                    "proposed_patch": "Add the current award letter to the document checklist.",
                    "severity": "HIGH",
                    "confidence": 0.9,
                }
            ],
        }
    )


def finding():
    case = CASES[0]
    catalog = evidence_catalog(snapshot(case["before"]), snapshot(case["after"]))
    return grounded_findings(packet(), catalog, target(), "r1", "")[0]


@pytest.mark.parametrize("case", CASES[:2], ids=lambda row: row["name"])
def test_two_layouts_extract_changed_content(case):
    before, after = snapshot(case["before"]), snapshot(case["after"])
    assert before.pages[0].semantic_blocks != after.pages[0].semantic_blocks
    assert after.pages[0].headings


def test_noise_and_no_change_have_identical_evidence():
    for case in CASES[2:4]:
        catalog = evidence_catalog(snapshot(case["before"]), snapshot(case["after"]))
        assert catalog["before:p0"]["text"] == catalog["after:p0"]["text"]


def test_unsupported_quotes_and_guidance_fail_closed():
    case = CASES[0]
    catalog = evidence_catalog(snapshot(case["before"]), snapshot(case["after"]))
    for invalid in [packet(after="Invented eligibility requirement"), packet()]:
        if invalid.recommendations[0].after.quote != "Invented eligibility requirement":
            invalid.recommendations[0].guidance_quote = "Invented guidance"
        with pytest.raises(ValueError):
            grounded_findings(invalid, catalog, target(), "r1", "")


def test_duplicate_change_identity_independent_of_model_wording_and_run():
    case = CASES[0]
    catalog = evidence_catalog(snapshot(case["before"]), snapshot(case["after"]))
    first = grounded_findings(packet(), catalog, target(), "r1", "")[0]
    changed = packet()
    changed.recommendations[0].title = "Different model wording"
    second = grounded_findings(changed, catalog, target(), "r2", "")[0]
    assert first.finding_id == second.finding_id
    store = InMemoryStore()
    assert store.put_finding(first)
    assert not store.put_finding(second)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org",
        "https://user:pass@example.org",
        "https://127.0.0.1",
        "https://169.254.169.254",
        "https://[::1]",
        "https://example.org:8443",
    ],
)
def test_unsafe_urls_rejected(url, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 443))])
    with pytest.raises(UnsafeTargetError):
        validate_public_url(url)


def test_public_url_preserves_exact_path_and_query(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 443))])
    url = "https://example.org/services.html?program=one"
    assert validate_public_url(url) == url
    configured = target().model_copy(update={"start_url": url})
    assert page_url_for(configured, "/") == url


class FakeBrowser(AgentCoreBrowserAdapter):
    def __init__(self, current=None, error=None):
        self.current, self.error = current, error
        self.calls = 0

    async def capture(self, target, run_id):
        self.calls += 1
        if self.error:
            raise self.error
        return self.current.model_copy(update={"run_id": run_id})


@pytest.mark.asyncio
async def test_production_invokes_reasoner_and_displays_its_patch(monkeypatch):
    monkeypatch.setenv("CIVIC_CANARY_MODE", "aws")
    store = InMemoryStore()
    configured = target()
    store.put_target(configured)
    store.put_baseline(snapshot(CASES[0]["before"]))
    observed = []

    def execute(self, configured, baseline, run_id, guidance, capture):
        current = capture()
        catalog = evidence_catalog(baseline, current)
        observed.append(run_id)
        return (
            current,
            grounded_findings(packet(), catalog, configured, run_id, guidance),
            {
                "run_id": run_id,
                "engine": "strands-bedrock",
                "packet": packet().model_dump(),
            },
            [],
        )

    monkeypatch.setattr(StrandsReasoner, "execute", execute)
    run, findings = await ScanService(store, FakeBrowser(snapshot(CASES[0]["after"]))).run_local(
        configured, "MANUAL", "production-r1"
    )
    assert observed == ["production-r1"]
    assert run.reasoning_source == "strands-bedrock"
    assert findings[0].proposed_patch == packet().recommendations[0].proposed_patch
    assert store.list_json_keys("outbox/")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["browser", "model"])
async def test_failed_initial_capture_or_model_is_recorded(monkeypatch, failure):
    monkeypatch.setenv("CIVIC_CANARY_MODE", "aws")
    store = InMemoryStore()
    configured = target().model_copy(update={"setup_status": "PENDING"})
    browser = FakeBrowser(error=TimeoutError("capture unavailable"))

    def execute(self, configured, baseline, run_id, guidance, capture):
        if failure == "browser":
            capture()
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(StrandsReasoner, "execute", execute)
    with pytest.raises(RunExecutionError):
        await ScanService(store, browser).run_local(configured, "MANUAL", "failed-initial")
    assert store.get_run("failed-initial").status == "FAILED"
    assert store.get_baseline(configured.target_id) is None
    assert not store.list_findings()
    assert not store.list_json_keys("outbox/")


def configure_email(monkeypatch):
    monkeypatch.setenv("CIVIC_CANARY_EMAIL_FROM", "sender@example.org")
    monkeypatch.setenv("CIVIC_CANARY_EMAIL_TO", "reviewer@example.org")
    monkeypatch.setenv("CIVIC_CANARY_PUBLIC_URL", "https://canary.example.org")


def test_notifications_quiet_deduplicated_and_traceable(monkeypatch):
    configure_email(monkeypatch)
    store = InMemoryStore()
    ses = MagicMock()
    ses.send_email.return_value = {"MessageId": "msg1"}
    assert deliver_pending(store, ses)["sent"] == 0
    model = finding()
    store.put_finding(model)
    queue_notifications(store, model)
    queue_notifications(store, model)
    assert deliver_pending(store, ses)["sent"] == 1
    queue_notifications(store, model)
    assert deliver_pending(store, ses)["sent"] == 0
    ses.send_email.assert_called_once()
    assert model.run_id in str(ses.send_email.call_args)
    assert "?finding=" in str(ses.send_email.call_args)


def test_ambiguous_notification_failure_is_not_resent(monkeypatch):
    configure_email(monkeypatch)
    store = InMemoryStore()
    model = finding()
    store.put_finding(model)
    queue_notifications(store, model)
    ses = MagicMock()
    ses.send_email.side_effect = TimeoutError("delivery response lost")
    deliver_pending(store, ses)
    queue_notifications(store, model)
    deliver_pending(store, ses)
    ses.send_email.assert_called_once()
    assert store.get_json(f"notification-claims/{model.finding_id}.json")["status"].startswith(
        "UNKNOWN"
    )


@pytest.mark.asyncio
async def test_background_frequency_and_queue_idempotency(monkeypatch):
    monkeypatch.delenv("CIVIC_CANARY_EMAIL_FROM", raising=False)
    store = InMemoryStore()
    configured = target()
    store.put_target(configured)
    calls = []

    async def run(self, configured, trigger, run_id):
        calls.append(run_id)
        result = Run(
            run_id=run_id,
            target_id=configured.target_id,
            trigger_type=trigger,
            status="SUCCEEDED",
            started_at=datetime.now(UTC),
        )
        store.put_run(result)
        return result, []

    monkeypatch.setattr(ScanService, "run_local", run)
    now = datetime(2026, 9, 12, tzinfo=UTC)
    await run_due(store, now)
    await run_due(store, now + timedelta(minutes=1))
    assert len(calls) == 1
    await run_due(store, now + timedelta(minutes=6))
    assert len(calls) == 2


def test_protected_add_confirm_and_artifact_audit(monkeypatch):
    monkeypatch.setenv("CIVIC_CANARY_MODE", "aws")
    monkeypatch.setenv("REVIEW_TOKEN", "test-review-token")
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 443))])
    store = InMemoryStore()
    api = TestClient(create_app(store))
    headers = {"X-Review-Token": "test-review-token"}
    body = {
        "name": "Library",
        "public_url": "https://library.example.org/service",
        "monitoring_objective": "Delivery contact information",
    }
    assert api.post("/api/targets", json=body).status_code == 401
    added = api.post("/api/targets", headers=headers, json=body)
    assert added.status_code == 200
    configured = store.get_target(added.json()["target_id"])
    assert configured.setup_status == "PENDING"
    assert store.get_run(configured.setup_run_id).status == "QUEUED"
    assert api.get("/api/targets").status_code == 401
    assert (
        api.post(
            f"/api/targets/{configured.target_id}/confirm",
            headers=headers,
            json={"monitored_sections": ["Contact"]},
        ).status_code
        == 409
    )
    model = finding()
    store.put_finding(model)
    result = api.post(
        f"/api/findings/{model.finding_id}/decision",
        headers=headers,
        json={"action": "APPROVE", "note": "Checked source"},
    )
    assert result.status_code == 200
    review = store.reviews[f"review-{model.finding_id}"]
    assert review["reviewer"].startswith("sha256:")
    assert review["action"] == "APPROVED"
    assert review["originalRecommendation"] == model.proposed_patch
    assert review["runId"] == model.run_id
    assert api.get(f"/api/findings/{model.finding_id}/artifact").status_code == 401
    artifact = api.get(f"/api/findings/{model.finding_id}/artifact", headers=headers)
    assert artifact.status_code == 200
    assert model.proposed_patch in artifact.text


def test_local_mode_keeps_demo_and_allows_live_add(monkeypatch):
    monkeypatch.setenv("CIVIC_CANARY_MODE", "local")
    monkeypatch.setenv("REVIEW_TOKEN", "test-review-token")
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 443))])

    async def fake_capture(self, configured, run_id):
        return snapshot("<article><h2>Home delivery</h2><p>Call 555-0101.</p></article>", site=configured.target_id, run_id=run_id)

    monkeypatch.setattr(HttpBrowserAdapter, "capture", fake_capture)
    store = InMemoryStore()
    api = TestClient(create_app(store))
    headers = {"X-Review-Token": "test-review-token"}
    targets = api.get("/api/targets").json()
    assert any(row["target_id"] == "benefits-demo" for row in targets)
    added = api.post(
        "/api/targets",
        headers=headers,
        json={
            "name": "Library",
            "public_url": "https://library.example.org/service",
            "monitoring_objective": "Delivery contact information",
        },
    )
    assert added.status_code == 200
    body = added.json()
    assert body["setup_status"] == "AWAITING_CONFIRMATION"
    assert "Home delivery" in body["recommended_sections"]
    assert any(row["target_id"] == body["target_id"] for row in api.get("/api/targets").json())
    switched = api.post(
        "/api/demo/version",
        headers=headers,
        json={"target_id": "benefits-demo", "version": "v2"},
    )
    assert switched.status_code == 200
    assert switched.json()["active_version"] == "v2"


def test_live_website_confirm_schedules_next_scan(monkeypatch):
    monkeypatch.setenv("CIVIC_CANARY_MODE", "aws")
    monkeypatch.setenv("REVIEW_TOKEN", "test-review-token")
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 443))])
    store = InMemoryStore()
    api = TestClient(create_app(store))
    headers = {"X-Review-Token": "test-review-token"}
    added = api.post(
        "/api/targets",
        headers=headers,
        json={
            "name": "Library",
            "public_url": "https://library.example.org/service",
            "monitoring_objective": "Delivery contact information",
            "scan_frequency_minutes": 60,
        },
    )
    assert added.status_code == 200
    target_id = added.json()["target_id"]
    configured = store.get_target(target_id)
    store.put_baseline(snapshot(CASES[0]["before"], site=target_id))
    configured.setup_status = "AWAITING_CONFIRMATION"
    configured.recommended_sections = ["Home delivery"]
    store.put_target(configured)
    confirmed = api.post(
        f"/api/targets/{target_id}/confirm",
        headers=headers,
        json={"monitored_sections": ["Home delivery"]},
    )
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["setup_status"] == "ACTIVE"
    assert body["monitored_sections"] == ["Home delivery"]
    assert body["next_scan_at"] is not None
    listed = api.get("/api/targets", headers=headers).json()
    assert any(row["target_id"] == target_id for row in listed)


def test_atomic_aws_review_records_identity_and_evidence(monkeypatch):
    monkeypatch.setattr("services.storage.boto3.resource", MagicMock())
    client = MagicMock()
    monkeypatch.setattr("services.storage.boto3.client", lambda *a, **k: client)
    store = AwsStore("sites", "findings", "reviews", "bucket")
    model = finding().model_copy(
        update={"status": FindingStatus.REJECTED, "reviewed_by": "sha256:test"}
    )
    assert store.commit_review(model, FindingStatus.OPEN)
    writes = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(writes) == 2
    review = writes[1]["Put"]["Item"]
    assert review["reviewer"] == {"S": "sha256:test"}
    assert review["action"] == {"S": "REJECTED"}
    assert review["originalRecommendation"]["S"] == model.proposed_patch


def test_real_strands_graph_executes_collection_and_structured_nodes(monkeypatch):
    """Use the real SDK orchestrator/tool loop, replacing only the remote model transport."""
    from strands.models.model import Model

    from agent.civic_canary.reasoning import Classification, GroundingVerdict

    class ScriptedModel(Model):
        def update_config(self, **kwargs):
            pass

        def get_config(self):
            return {"model_id": "offline-test-model"}

        async def structured_output(self, output_model, prompt, **kwargs):
            values = {
                Classification: {"summary": "Changed document"},
                DecisionPacket: packet().model_dump(),
                GroundingVerdict: {"supported": True},
            }
            yield {"output": output_model.model_validate(values[output_model])}

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            # Each agent must call the actual collection tool before finishing.
            called = any(
                "toolResult" in item for message in messages for item in message.get("content", [])
            )
            outputs = {
                "Classification": {"summary": "Changed document"},
                "DecisionPacket": packet().model_dump(),
                "GroundingVerdict": {"supported": True},
            }
            structured = next(
                (spec["name"] for spec in (tool_specs or []) if spec["name"] in outputs), None
            )
            selected_tool = "collect_evidence" if not called else structured
            payload = {"purpose": "full_page"} if not called else outputs.get(structured, {})
            yield {"messageStart": {"role": "assistant"}}
            if selected_tool:
                yield {
                    "contentBlockStart": {
                        "contentBlockIndex": 0,
                        "start": {"toolUse": {"toolUseId": "tool-call", "name": selected_tool}},
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "contentBlockIndex": 0,
                        "delta": {"toolUse": {"input": json.dumps(payload)}},
                    }
                }
            else:
                yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
                yield {
                    "contentBlockDelta": {
                        "contentBlockIndex": 0,
                        "delta": {"text": "Evidence collected"},
                    }
                }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use" if selected_tool else "end_turn"}}
            yield {
                "metadata": {
                    "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                    "metrics": {"latencyMs": 1},
                }
            }

    monkeypatch.setenv("BEDROCK_MODEL_ID", "offline-test-model")
    monkeypatch.setattr(
        "agent.civic_canary.reasoning.BedrockModel", lambda **kwargs: ScriptedModel()
    )
    captures = []

    def capture():
        captures.append("called")
        return snapshot(CASES[0]["after"])

    current, rows, trace, timings = StrandsReasoner().execute(
        target(), snapshot(CASES[0]["before"]), "sdk-test-run", "", capture
    )
    assert captures == ["called"]
    assert rows[0].proposed_patch == packet().recommendations[0].proposed_patch
    assert trace["run_id"] == "sdk-test-run"
    assert [timing.node for timing in timings] == [
        "strands-collect",
        "strands-classify",
        "strands-draft",
        "strands-verify",
    ]


def test_collect_evidence_tool_schema_includes_purpose():
    from strands import tool

    @tool
    def collect_evidence(purpose: str = "full_page") -> dict:
        """Return evidence.

        Args:
            purpose: Short label for the request.
        """
        return {"purpose": purpose}

    schema = collect_evidence.tool_spec["inputSchema"]["json"]
    assert schema["type"] == "object"
    assert "purpose" in schema["properties"]


def test_resolve_setup_sections_keeps_observed_and_falls_back():
    from agent.civic_canary.reasoning import DecisionPacket, resolve_setup_sections

    page = snapshot(CASES[0]["before"]).pages[0]
    current = snapshot(CASES[0]["before"])
    packet = DecisionPacket(
        summary="Setup",
        recommendations=[],
        recommended_sections=["Invented Section", page.headings[0]],
    )
    assert resolve_setup_sections(packet, current) == [page.headings[0]]
    empty = DecisionPacket(summary="Setup", recommendations=[], recommended_sections=[])
    assert resolve_setup_sections(empty, current) == list(
        dict.fromkeys(heading for page in current.pages for heading in page.headings)
    )[:20]


def test_strands_setup_survives_empty_tool_input_json(monkeypatch):
    """Bedrock often streams blank tool input for tools; capture must still succeed."""
    from strands.models.model import Model

    from agent.civic_canary.reasoning import (
        Classification,
        DecisionPacket,
        GroundingVerdict,
        StrandsReasoner,
    )

    class EmptyInputModel(Model):
        def update_config(self, **kwargs):
            pass

        def get_config(self):
            return {"model_id": "offline-test-model"}

        async def structured_output(self, output_model, prompt, **kwargs):
            values = {
                Classification: {"summary": "Initial capture"},
                DecisionPacket: {
                    "summary": "Baseline ready",
                    "recommendations": [],
                    "recommended_sections": ["Invented"],
                },
                GroundingVerdict: {"supported": True},
            }
            yield {"output": output_model.model_validate(values[output_model])}

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            called = any(
                "toolResult" in item for message in messages for item in message.get("content", [])
            )
            outputs = {
                "Classification": {"summary": "Initial capture"},
                "DecisionPacket": {
                    "summary": "Baseline ready",
                    "recommendations": [],
                    "recommended_sections": ["Invented"],
                },
                "GroundingVerdict": {"supported": True},
            }
            structured = next(
                (spec["name"] for spec in (tool_specs or []) if spec["name"] in outputs), None
            )
            selected_tool = "collect_evidence" if not called else structured
            # Empty string reproduces the production warning path in Strands streaming.
            payload = "" if not called else json.dumps(outputs.get(structured, {}))
            yield {"messageStart": {"role": "assistant"}}
            if selected_tool:
                yield {
                    "contentBlockStart": {
                        "contentBlockIndex": 0,
                        "start": {"toolUse": {"toolUseId": "tool-call", "name": selected_tool}},
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "contentBlockIndex": 0,
                        "delta": {"toolUse": {"input": payload}},
                    }
                }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use" if selected_tool else "end_turn"}}
            yield {
                "metadata": {
                    "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                    "metrics": {"latencyMs": 1},
                }
            }

    monkeypatch.setenv("BEDROCK_MODEL_ID", "offline-test-model")
    monkeypatch.setattr(
        "agent.civic_canary.reasoning.BedrockModel", lambda **kwargs: EmptyInputModel()
    )
    captures = []

    def capture():
        captures.append("called")
        return snapshot(CASES[0]["before"])

    current, rows, trace, _timings = StrandsReasoner().execute(
        target().model_copy(update={"setup_status": "PENDING"}),
        None,
        "setup-empty-input",
        "",
        capture,
    )
    assert captures == ["called"]
    assert rows == []
    assert current.pages
    assert trace["packet"]["recommended_sections"]
    assert "Invented" not in trace["packet"]["recommended_sections"]
    assert all(
        section in {heading for page in current.pages for heading in page.headings}
        or section == "Main content"
        for section in trace["packet"]["recommended_sections"]
    )


@pytest.mark.asyncio
async def test_production_setup_run_reaches_awaiting_confirmation(monkeypatch):
    monkeypatch.setenv("CIVIC_CANARY_MODE", "aws")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "offline-test-model")
    store = InMemoryStore()
    configured = target().model_copy(update={"setup_status": "PENDING", "kind": "live"})
    store.put_target(configured)
    captured = snapshot(CASES[0]["before"], site=configured.target_id)

    def execute(self, configured_target, baseline, run_id, guidance, capture):
        assert baseline is None
        snap = capture()
        packet = {
            "summary": "Baseline ready",
            "recommendations": [],
            "recommended_sections": snap.pages[0].headings[:1],
        }
        return (
            snap,
            [],
            {
                "run_id": run_id,
                "engine": "strands-bedrock",
                "model_id": "offline-test-model",
                "packet": packet,
            },
            [],
        )

    monkeypatch.setattr(StrandsReasoner, "execute", execute)
    run, findings = await ScanService(store, FakeBrowser(captured)).run_local(
        configured, "MANUAL", "setup-confirm"
    )
    assert run.status == "SUCCEEDED"
    assert findings == []
    refreshed = store.get_target(configured.target_id)
    assert refreshed.setup_status == "AWAITING_CONFIRMATION"
    assert refreshed.recommended_sections
    assert store.get_baseline(configured.target_id) is not None


@pytest.mark.asyncio
async def test_active_demo_first_baseline_stays_active(monkeypatch):
    """Seeded ACTIVE demos must not be demoted to AWAITING_CONFIRMATION."""
    monkeypatch.setenv("CIVIC_CANARY_MODE", "aws")
    store = InMemoryStore()
    configured = target().model_copy(update={"setup_status": "ACTIVE", "kind": "demo"})
    store.put_target(configured)
    captured = snapshot(CASES[0]["before"], site=configured.target_id)

    def execute(self, configured_target, baseline, run_id, guidance, capture):
        assert baseline is None
        snap = capture()
        return (
            snap,
            [],
            {
                "run_id": run_id,
                "engine": "strands-bedrock",
                "model_id": "offline-test-model",
                "packet": {
                    "summary": "Baseline established",
                    "recommendations": [],
                    "recommended_sections": snap.pages[0].headings[:1],
                },
            },
            [],
        )

    monkeypatch.setattr(StrandsReasoner, "execute", execute)
    run, findings = await ScanService(store, FakeBrowser(captured)).run_local(
        configured, "MANUAL", "demo-baseline"
    )
    assert run.status == "SUCCEEDED"
    assert findings == []
    refreshed = store.get_target(configured.target_id)
    assert refreshed.setup_status == "ACTIVE"
    assert store.get_baseline(configured.target_id) is not None


def test_invalid_public_url_does_not_crash_notification_tick(monkeypatch):
    monkeypatch.setenv("CIVIC_CANARY_EMAIL_FROM", "sender@example.org")
    monkeypatch.setenv("CIVIC_CANARY_EMAIL_TO", "reviewer@example.org")
    monkeypatch.setenv("CIVIC_CANARY_PUBLIC_URL", "not-a-url")
    store = InMemoryStore()
    store.put_finding(finding())
    queue_notifications(store, finding())
    result = deliver_pending(store, MagicMock())
    assert result["status"] == "NOT_CONFIGURED"
    assert result["sent"] == 0
    assert store.list_json_keys("outbox/")


def test_store_factory_fails_fast_when_aws_config_missing(monkeypatch):
    from services.store_factory import default_store

    monkeypatch.setenv("CIVIC_CANARY_MODE", "aws")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    for key in (
        "CIVIC_CANARY_SITES_TABLE",
        "CIVIC_CANARY_FINDINGS_TABLE",
        "CIVIC_CANARY_REVIEWS_TABLE",
        "CIVIC_CANARY_S3_BUCKET",
        "AWS_STORAGE_LAYOUT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "services.store_factory.boto3.client",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ssm unavailable")),
    )
    with pytest.raises(RuntimeError, match="AWS storage config is incomplete"):
        default_store()


@pytest.mark.asyncio
async def test_failed_active_scan_resets_next_scan_for_retry(monkeypatch):
    monkeypatch.setenv("CIVIC_CANARY_MODE", "aws")
    store = InMemoryStore()
    future = datetime.now(UTC) + timedelta(days=1)
    configured = target().model_copy(
        update={"setup_status": "ACTIVE", "next_scan_at": future, "kind": "live"}
    )
    store.put_target(configured)
    store.put_baseline(snapshot(CASES[0]["before"]))

    def execute(self, configured_target, baseline, run_id, guidance, capture):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(StrandsReasoner, "execute", execute)
    with pytest.raises(RunExecutionError):
        await ScanService(store, FakeBrowser(snapshot(CASES[0]["after"]))).run_local(
            configured, "MANUAL", "retry-soon"
        )
    refreshed = store.get_target(configured.target_id)
    assert refreshed.next_scan_at is not None
    assert refreshed.next_scan_at <= datetime.now(UTC) + timedelta(seconds=5)


def test_recover_setup_queue_repairs_missing_job_and_syncs_failed(monkeypatch):
    from agent.civic_canary.models import JourneyStep, PortalTarget, Run, RunStatus, TriggerType
    from services.monitoring import recover_setup_queue

    store = InMemoryStore()
    now = datetime.now(UTC)
    pending = PortalTarget(
        target_id="site-stuck",
        name="Stuck",
        kind="live",
        start_url="https://example.org/",
        allowed_hosts=["example.org"],
        journey_steps=[JourneyStep(path="/", label="Submitted page")],
        setup_status="PENDING",
        setup_run_id="run-missing-job",
        monitoring_objective="Monitor SNAP",
    )
    store.put_target(pending)
    store.put_run(
        Run(
            run_id="run-missing-job",
            target_id="site-stuck",
            trigger_type=TriggerType.MANUAL,
            status=RunStatus.QUEUED,
            started_at=now,
        )
    )
    assert store.get_json("jobs/run-missing-job.json") is None
    assert recover_setup_queue(store, now) >= 1
    assert store.get_json("jobs/run-missing-job.json") == {
        "run_id": "run-missing-job",
        "target_id": "site-stuck",
    }

    desynced = PortalTarget(
        target_id="site-desync",
        name="Desync",
        kind="live",
        start_url="https://example.org/a",
        allowed_hosts=["example.org"],
        journey_steps=[JourneyStep(path="/", label="Submitted page")],
        setup_status="PENDING",
        setup_run_id="run-already-failed",
        monitoring_objective="Monitor SNAP",
    )
    store.put_target(desynced)
    store.put_run(
        Run(
            run_id="run-already-failed",
            target_id="site-desync",
            trigger_type=TriggerType.MANUAL,
            status=RunStatus.FAILED,
            started_at=now,
            finished_at=now,
            summary="page returned HTTP 404",
            error_category="RuntimeError",
        )
    )
    assert recover_setup_queue(store, now) >= 1
    assert store.get_target("site-desync").setup_status == "FAILED"
