"""Catalog of demo scenarios and example live public websites."""

from __future__ import annotations

from pathlib import Path

from agent.civic_canary.models import JourneyStep, PortalTarget

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "agent" / "fixtures"


def _playbook(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def demo_scenarios() -> list[PortalTarget]:
    """Synthetic V1/V2 portals used for hackathon demos."""
    return [
        PortalTarget(
            target_id="benefits-demo",
            name="River County Benefits Portal",
            kind="demo",
            fixture_namespace="",
            change_summary="V2 adds a required award letter, breaks Spanish guidance, and removes a form label.",
            description="Original civic benefits demo used for controlled regressions.",
            monitoring_objective="Document requirements, language access, and application accessibility",
            playbook_key="playbooks/benefits-demo/current.md",
            guidance_context=None,
        ),
        PortalTarget(
            target_id="transit-demo",
            name="Community Transit Portal",
            kind="demo",
            fixture_namespace="transit",
            start_url="fixture://transit",
            allowed_hosts=["transit.demo.local"],
            journey_steps=[JourneyStep(path="/index.html", label="Service board")],
            change_summary="V2 changes Route 12 weekday evening hours and the last downtown trip.",
            description="Bus route and service-hour updates for riders.",
            monitoring_objective="Route changes and service hours that affect riders",
            playbook_key="playbooks/transit-demo/current.md",
            guidance_context=_playbook("transit-playbook.md"),
        ),
        PortalTarget(
            target_id="library-demo",
            name="Community Library Portal",
            kind="demo",
            fixture_namespace="library",
            start_url="fixture://library",
            allowed_hosts=["library.demo.local"],
            journey_steps=[JourneyStep(path="/index.html", label="Branch updates")],
            change_summary="V2 announces Riverside Branch temporary closure and relocation of holds pickup.",
            description="Branch closures and relocation notices for library patrons.",
            monitoring_objective="Branch closures, relocations, and holds pickup changes",
            playbook_key="playbooks/library-demo/current.md",
            guidance_context=_playbook("library-playbook.md"),
        ),
        PortalTarget(
            target_id="housing-demo",
            name="Housing Assistance Portal",
            kind="demo",
            fixture_namespace="housing",
            start_url="fixture://housing",
            allowed_hosts=["housing.demo.local"],
            journey_steps=[JourneyStep(path="/index.html", label="Application checklist")],
            change_summary="V2 moves the rent-aid deadline earlier and adds a required income worksheet.",
            description="Housing aid deadlines and document requirements.",
            monitoring_objective="Application deadlines and required housing documents",
            playbook_key="playbooks/housing-demo/current.md",
            guidance_context=_playbook("housing-playbook.md"),
        ),
        PortalTarget(
            target_id="health-demo",
            name="Community Health Clinic",
            kind="demo",
            fixture_namespace="health",
            start_url="fixture://health",
            allowed_hosts=["health.demo.local"],
            journey_steps=[JourneyStep(path="/index.html", label="Clinic hours")],
            change_summary="V2 shortens evening clinic hours and requires appointments for vaccine visits.",
            description="Clinic hours and appointment policy changes.",
            monitoring_objective="Clinic hours and appointment requirements",
            playbook_key="playbooks/health-demo/current.md",
            guidance_context=_playbook("health-playbook.md"),
        ),
        PortalTarget(
            target_id="food-demo",
            name="Food Assistance Portal",
            kind="demo",
            fixture_namespace="food",
            start_url="fixture://food",
            allowed_hosts=["food.demo.local"],
            journey_steps=[JourneyStep(path="/index.html", label="Distribution schedule")],
            change_summary="V2 moves Saturday pantry distribution to a new location and later start time.",
            description="Food pantry distribution location and time changes.",
            monitoring_objective="Distribution location and schedule changes",
            playbook_key="playbooks/food-demo/current.md",
            guidance_context=_playbook("food-playbook.md"),
        ),
    ]


def live_website_examples() -> list[PortalTarget]:
    """Real public pages. Baseline is captured from the live site—no synthetic V2."""
    return [
        PortalTarget(
            target_id="fairfax-connector",
            name="Fairfax Connector",
            kind="live",
            start_url="https://www.fairfaxcounty.gov/connector/",
            allowed_hosts=["www.fairfaxcounty.gov"],
            journey_steps=[JourneyStep(path="/", label="Connector home")],
            active_version="v1",
            setup_status="ACTIVE",
            scan_frequency_minutes=1440,
            monitoring_objective="Service alerts, route notices, and rider news that affect transit users",
            description="Official Fairfax Connector public site. Baseline is the live page at first scan.",
            change_summary="No scripted change—future scans compare against the captured live baseline.",
            playbook_key="playbooks/fairfax-connector/current.md",
            guidance_context=_playbook("fairfax-connector-playbook.md"),
        ),
        PortalTarget(
            target_id="fairfax-library",
            name="Fairfax County Public Library",
            kind="live",
            start_url="https://www.fairfaxcounty.gov/library/",
            allowed_hosts=["www.fairfaxcounty.gov"],
            journey_steps=[JourneyStep(path="/", label="Library home")],
            active_version="v1",
            setup_status="ACTIVE",
            scan_frequency_minutes=1440,
            monitoring_objective="Library location, hours, and service updates for patrons",
            description="Official FCPL public site. Baseline is the live page at first scan.",
            change_summary="No scripted change—future scans compare against the captured live baseline.",
            playbook_key="playbooks/fairfax-library/current.md",
            guidance_context=_playbook("fairfax-library-playbook.md"),
        ),
    ]


def seed_targets() -> list[PortalTarget]:
    return [*demo_scenarios(), *live_website_examples()]
