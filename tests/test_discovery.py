"""Tests for objective-guided page discovery."""

from __future__ import annotations

import os

import pytest

from agent.civic_canary.discovery import (
    apply_page_edits,
    canonicalize_url,
    content_fingerprint,
    discover_pages,
    discovery_enabled_for,
    journey_steps_from_discovery,
    score_link_for_objective,
)
from agent.civic_canary.models import (
    DiscoveredPage,
    DiscoverySummary,
    JourneyStep,
    LinkFact,
    MonitoredPageEdit,
    PageSnapshot,
    PortalTarget,
)


def _page(url: str, title: str, links: list[tuple[str, str]], text: str) -> PageSnapshot:
    return PageSnapshot(
        url=url,
        title=title,
        visible_text=text,
        semantic_blocks=[text],
        links=[LinkFact(text=label, href=href, status=200) for label, href in links],
    )


def test_canonicalize_strips_fragments_and_trailing_slash() -> None:
    assert (
        canonicalize_url("https://www.dss.virginia.gov/snap/#requirements")
        == "https://www.dss.virginia.gov/snap"
    )
    assert canonicalize_url("https://WWW.Example.ORG/Path/") == "https://www.example.org/Path"


def test_score_prefers_objective_links() -> None:
    objective = "Monitor SNAP eligibility, requirements, and policy changes."
    snap = score_link_for_objective(
        text="SNAP requirements",
        href="https://www.dss.virginia.gov/relief/food-assistance/snap/",
        objective=objective,
    )
    careers = score_link_for_objective(
        text="Careers",
        href="https://www.dss.virginia.gov/careers/",
        objective=objective,
    )
    assert snap > careers
    assert careers < 0


def test_discovery_finds_relevant_pages_and_skips_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIVIC_CANARY_DISCOVERY_MAX_PAGES", "4")
    monkeypatch.setenv("CIVIC_CANARY_DISCOVERY_MAX_DEPTH", "2")
    monkeypatch.setenv("CIVIC_CANARY_DISCOVERY_STRANDS", "0")

    entry = "https://www.dss.virginia.gov/"
    snap_url = "https://www.dss.virginia.gov/relief/food-assistance/snap/"
    faq_url = "https://www.dss.virginia.gov/relief/food-assistance/snap/faq/"
    faq_anchor = "https://www.dss.virginia.gov/relief/food-assistance/snap/faq/#apply"
    careers = "https://www.dss.virginia.gov/careers/"

    pages = {
        entry: _page(
            entry,
            "Virginia DSS",
            [
                ("SNAP", snap_url),
                ("SNAP again", snap_url),
                ("Careers", careers),
                ("Food Assistance", "https://www.dss.virginia.gov/relief/food-assistance/"),
            ],
            "Virginia Department of Social Services home with programs.",
        ),
        "https://www.dss.virginia.gov/relief/food-assistance": _page(
            "https://www.dss.virginia.gov/relief/food-assistance",
            "Food Assistance",
            [("SNAP main page", snap_url), ("FAQ", faq_url)],
            "Food assistance programs including SNAP eligibility overview.",
        ),
        snap_url.rstrip("/"): _page(
            snap_url.rstrip("/"),
            "SNAP",
            [("Requirements FAQ", faq_url), ("FAQ anchor", faq_anchor)],
            "Supplemental Nutrition Assistance Program eligibility and requirements policy.",
        ),
        faq_url.rstrip("/"): _page(
            faq_url.rstrip("/"),
            "SNAP FAQ",
            [],
            "Frequently asked questions about SNAP requirements and application information.",
        ),
        careers.rstrip("/"): _page(
            careers.rstrip("/"),
            "Careers",
            [],
            "Join our team and apply for open positions.",
        ),
    }

    def fetch(url: str) -> PageSnapshot:
        key = canonicalize_url(url)
        if key not in pages:
            raise AssertionError(f"unexpected fetch {url}")
        return pages[key]

    target = PortalTarget(
        target_id="site-dss",
        name="Virginia DSS",
        kind="live",
        start_url=entry,
        allowed_hosts=["www.dss.virginia.gov"],
        journey_steps=[JourneyStep(path="/", label="Submitted page")],
        monitoring_objective="Monitor SNAP eligibility, requirements, and policy changes.",
        setup_status="PENDING",
    )

    snapshots, summary = discover_pages(target=target, fetch_page=fetch, go_back=None)
    assert summary.website_name == "Virginia DSS"
    assert any("SNAP" in page.label or "snap" in page.path.lower() for page in summary.discovered_pages)
    assert len(summary.discovered_pages) <= 4
    assert len(snapshots) == summary.pages_visited
    reasons = {item.reason for item in summary.skipped}
    assert "duplicate links" in reasons or "unrelated programs" in reasons

    steps = journey_steps_from_discovery(summary)
    assert steps
    assert all(step.path.startswith("/") for step in steps)


def test_near_duplicate_content_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIVIC_CANARY_DISCOVERY_MAX_PAGES", "5")
    monkeypatch.setenv("CIVIC_CANARY_DISCOVERY_STRANDS", "0")
    shared = "Identical SNAP eligibility requirements text for fingerprinting."
    entry = "https://example.org/"
    a = "https://example.org/snap"
    b = "https://example.org/snap-copy"

    catalog = {
        canonicalize_url(entry): _page(
            entry,
            "Home",
            [("SNAP", a), ("SNAP copy", b)],
            "Portal home mentioning SNAP eligibility.",
        ),
        canonicalize_url(a): _page(a, "SNAP A", [], shared),
        canonicalize_url(b): _page(b, "SNAP B", [], shared),
    }

    target = PortalTarget(
        target_id="site-dup",
        name="Example",
        kind="live",
        start_url=entry,
        allowed_hosts=["example.org"],
        journey_steps=[JourneyStep(path="/", label="Submitted page")],
        monitoring_objective="Monitor SNAP eligibility requirements",
        setup_status="PENDING",
    )
    _snapshots, summary = discover_pages(
        target=target, fetch_page=lambda url: catalog[canonicalize_url(url)], go_back=None
    )
    selected_urls = {page.url for page in summary.discovered_pages if page.selected}
    assert canonicalize_url(a) in selected_urls or canonicalize_url(b) in selected_urls
    assert not (
        canonicalize_url(a) in selected_urls and canonicalize_url(b) in selected_urls
    )


def test_apply_page_edits_updates_journey() -> None:
    summary = DiscoverySummary(
        website_name="Virginia DSS",
        entry_url="https://www.dss.virginia.gov/",
        monitoring_objective="SNAP",
        discovered_pages=[
            DiscoveredPage(
                url="https://www.dss.virginia.gov/snap",
                path="/snap",
                label="SNAP main page",
                selected=True,
            ),
            DiscoveredPage(
                url="https://www.dss.virginia.gov/other",
                path="/other",
                label="Other",
                selected=True,
            ),
        ],
        skipped=[],
        pages_visited=2,
        max_pages=5,
    )
    updated, steps = apply_page_edits(
        summary,
        [
            MonitoredPageEdit(path="/snap", label="SNAP eligibility", selected=True),
            MonitoredPageEdit(path="/other", label="Other", selected=False),
            MonitoredPageEdit(
                path="/snap/faq",
                label="SNAP FAQ",
                url="https://www.dss.virginia.gov/snap/faq",
                selected=True,
            ),
        ],
        fallback_entry_url="https://www.dss.virginia.gov/",
        objective="SNAP",
        website_name="Virginia DSS",
    )
    assert [step.path for step in steps] == ["/snap", "/snap/faq"]
    assert steps[0].label == "SNAP eligibility"
    assert sum(1 for page in updated.discovered_pages if page.selected) == 2


def test_discovery_enabled_only_for_live_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CIVIC_CANARY_DISCOVERY", raising=False)
    live = PortalTarget(
        target_id="site-1",
        name="Live",
        kind="live",
        start_url="https://example.org/",
        allowed_hosts=["example.org"],
        journey_steps=[JourneyStep(path="/", label="Submitted page")],
        setup_status="PENDING",
    )
    assert discovery_enabled_for(live) is True
    live.setup_status = "ACTIVE"
    assert discovery_enabled_for(live) is False
    live.setup_status = "PENDING"
    live.journey_steps = [
        JourneyStep(path="/snap", label="SNAP"),
        JourneyStep(path="/faq", label="FAQ"),
    ]
    assert discovery_enabled_for(live) is False


def test_content_fingerprint_stable() -> None:
    page = _page("https://example.org/a", "A", [], "Hello   world")
    assert content_fingerprint(page) == content_fingerprint(
        _page("https://example.org/b", "B", [], "Hello world")
    )


def test_confirm_api_accepts_page_edits(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_TOKEN", "test-token")
    monkeypatch.setenv("CIVIC_CANARY_MODE", "local")
    from fastapi.testclient import TestClient

    from agent.civic_canary.models import PortalSnapshot
    from services.control_api.app import create_app
    from services.storage import InMemoryStore

    store = InMemoryStore(playbook="# guidance")
    target = PortalTarget(
        target_id="site-edit",
        name="Virginia DSS",
        kind="live",
        start_url="https://www.dss.virginia.gov/",
        allowed_hosts=["www.dss.virginia.gov"],
        journey_steps=[JourneyStep(path="/", label="Submitted page")],
        monitoring_objective="SNAP",
        recommended_sections=["Eligibility"],
        setup_status="AWAITING_CONFIRMATION",
        discovery_summary=DiscoverySummary(
            website_name="Virginia DSS",
            entry_url="https://www.dss.virginia.gov/",
            monitoring_objective="SNAP",
            discovered_pages=[
                DiscoveredPage(
                    url="https://www.dss.virginia.gov/snap",
                    path="/snap",
                    label="SNAP",
                    selected=True,
                )
            ],
            skipped=[],
            pages_visited=1,
            max_pages=5,
        ),
    )
    store.put_target(target)
    store.put_baseline(
        PortalSnapshot(
            target_id=target.target_id,
            run_id="run-baseline",
            version="live",
            pages=[
                _page(
                    "https://www.dss.virginia.gov/snap",
                    "SNAP",
                    [],
                    "Eligibility content",
                )
            ],
            content_hash="abc",
        )
    )
    client = TestClient(create_app(store=store))
    response = client.post(
        f"/api/targets/{target.target_id}/confirm",
        headers={"X-Review-Token": "test-token"},
        json={
            "monitored_sections": ["Eligibility"],
            "monitored_pages": [
                {
                    "path": "/snap",
                    "label": "SNAP main page",
                    "url": "https://www.dss.virginia.gov/snap",
                    "selected": True,
                },
                {
                    "path": "/snap/faq",
                    "label": "SNAP FAQ",
                    "url": "https://www.dss.virginia.gov/snap/faq",
                    "selected": True,
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["setup_status"] == "ACTIVE"
    assert [step["path"] for step in body["journey_steps"]] == ["/snap", "/snap/faq"]
    assert body["journey_steps"][0]["label"] == "SNAP main page"
