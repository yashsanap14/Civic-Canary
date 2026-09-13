from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:

    return datetime.now(UTC)


def default_start_url() -> str:
    """

    Use local fixture URLs in local/demo mode.



    When running with Amazon Bedrock AgentCore Browser,

    use the publicly reachable Civic Canary demo portal.

    """

    browser_mode = os.getenv("BROWSER_MODE", "local").lower()

    if browser_mode == "agentcore":
        base_url = os.getenv("CIVIC_CANARY_DEMO_BASE_URL")

        if not base_url:
            raise ValueError("CIVIC_CANARY_DEMO_BASE_URL is required when BROWSER_MODE=agentcore")

        return f"{base_url.rstrip('/')}/portal/{{version}}/"

    return "fixture://benefits"


def default_allowed_hosts() -> list[str]:
    """

    Build the browser allowlist from the deployed Civic Canary host.



    Local fixture mode continues to use benefits.demo.local.

    """

    browser_mode = os.getenv("BROWSER_MODE", "local").lower()

    if browser_mode == "agentcore":
        base_url = os.getenv("CIVIC_CANARY_DEMO_BASE_URL", "")

        hostname = urlparse(base_url).hostname

        if not hostname:
            raise ValueError(
                "CIVIC_CANARY_DEMO_BASE_URL must contain "
                "a valid hostname when BROWSER_MODE=agentcore"
            )

        return [hostname]

    return ["benefits.demo.local"]


class RunStatus(StrEnum):
    QUEUED = "QUEUED"

    RUNNING = "RUNNING"

    SUCCEEDED = "SUCCEEDED"

    FAILED = "FAILED"


class FindingStatus(StrEnum):
    OPEN = "OPEN"

    APPROVAL_PENDING = "APPROVAL_PENDING"

    APPROVED = "APPROVED"

    REJECTED = "REJECTED"


class Severity(StrEnum):
    LOW = "LOW"

    MEDIUM = "MEDIUM"

    HIGH = "HIGH"


class Materiality(StrEnum):
    COSMETIC = "COSMETIC"

    MATERIAL = "MATERIAL"

    NEEDS_REVIEW = "NEEDS_REVIEW"


class TriggerType(StrEnum):
    MANUAL = "MANUAL"

    SCHEDULED = "SCHEDULED"


class JourneyStep(BaseModel):
    path: str

    label: str

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:

        if not value.startswith("/") or ".." in value:
            raise ValueError("journey paths must be absolute and cannot traverse directories")

        return value


class PortalTarget(BaseModel):
    description: str = ""

    monitoring_objective: str = "Monitor material public-benefit and accessibility changes"

    scan_frequency_minutes: int = Field(default=1440, ge=5, le=43200)

    guidance_context: str | None = None

    recommended_sections: list[str] = Field(default_factory=list)

    monitored_sections: list[str] = Field(default_factory=list)

    setup_status: Literal["ACTIVE", "PENDING", "AWAITING_CONFIRMATION", "FAILED"] = "ACTIVE"

    setup_run_id: str | None = None

    next_scan_at: datetime | None = None

    last_scan_at: datetime | None = None

    kind: Literal["demo", "live"] = "demo"

    fixture_namespace: str = ""

    change_summary: str = (
        "V2 adds a required award letter, breaks Spanish guidance, and removes a form label."
    )

    target_id: str = "benefits-demo"

    name: str = "River County Benefits Portal"

    start_url: str = Field(default_factory=default_start_url)

    allowed_hosts: list[str] = Field(default_factory=default_allowed_hosts)

    journey_steps: list[JourneyStep] = Field(
        default_factory=lambda: [
            JourneyStep(
                path="/index.html",
                label="Overview",
            ),
            JourneyStep(
                path="/documents.html",
                label="Required documents",
            ),
            JourneyStep(
                path="/apply.html",
                label="Application form",
            ),
        ]
    )

    active_version: Literal["v1", "v2"] = "v1"

    enabled: bool = True

    playbook_key: str = "playbooks/benefits-demo/current.md"

    baseline_version: str = "v1"


class LinkFact(BaseModel):
    text: str

    href: str

    status: int = 200


class AccessibilityIssue(BaseModel):
    rule: str

    description: str

    selector: str

    severity: Severity = Severity.MEDIUM


class PageSnapshot(BaseModel):
    url: str

    title: str

    language: str | None = None

    headings: list[str] = Field(default_factory=list)

    visible_text: str

    semantic_blocks: list[str] = Field(default_factory=list)

    requirements: list[str] = Field(default_factory=list)

    links: list[LinkFact] = Field(default_factory=list)

    accessibility_issues: list[AccessibilityIssue] = Field(default_factory=list)

    screenshot_key: str | None = None


class PortalSnapshot(BaseModel):
    target_id: str

    run_id: str

    version: str

    captured_at: datetime = Field(default_factory=utc_now)

    pages: list[PageSnapshot]

    content_hash: str

    screenshot_key: str | None = None


class DetectedChange(BaseModel):
    category: Literal[
        "REQUIREMENT",
        "BROKEN_LINK",
        "ACCESSIBILITY",
        "CONTENT",
    ]

    previous_value: str | None = None

    current_value: str

    source_url: str

    evidence: str

    confidence: float = Field(
        ge=0,
        le=1,
    )

    materiality: Materiality

    severity: Severity

    fingerprint: str


class Finding(BaseModel):
    before: str = ""

    after: str = ""

    why_it_matters: str = ""

    affected_people: str = ""

    current_guidance: str = ""

    agent_confidence: float | None = Field(default=None, ge=0, le=1)

    reasoning_source: str = "deterministic-fixture"

    reviewed_by: str | None = None

    finding_id: str

    run_id: str

    target_id: str

    title: str

    category: str

    severity: Severity

    materiality: Materiality

    evidence: list[str]

    affected_playbook_sections: list[str]

    proposed_patch: str

    evidence_keys: list[str] = Field(default_factory=list)

    evidence_urls: list[str] = Field(default_factory=list)

    screenshot_urls: list[str] = Field(default_factory=list)

    status: FindingStatus = FindingStatus.OPEN

    created_at: datetime = Field(default_factory=utc_now)

    decision_note: str | None = None

    decided_at: datetime | None = None

    approved_artifact_key: str | None = None


class NodeTiming(BaseModel):
    node: str

    duration_ms: int = Field(ge=0)

    status: Literal[
        "SUCCEEDED",
        "FAILED",
    ]


class BriefStatus(StrEnum):
    BASELINE_ESTABLISHED = "BASELINE_ESTABLISHED"

    NO_MATERIAL_CHANGE = "NO_MATERIAL_CHANGE"

    REVIEW_RECOMMENDED = "REVIEW_RECOMMENDED"

    IMPORTANT_CHANGE = "IMPORTANT_CHANGE"


class BriefChangeItem(BaseModel):
    category: str

    title: str

    severity: Literal["Low", "Medium", "High"]

    previous: str = ""

    current: str = ""

    impact: str = ""

    recommended_action: str = ""


class MonitoringBrief(BaseModel):
    """Human-readable monitoring summary for a completed scan."""

    run_id: str

    target_id: str

    website_name: str

    scanned_at: datetime = Field(default_factory=utc_now)

    status: BriefStatus

    status_label: str

    executive_summary: str

    sections_reviewed: list[str] = Field(default_factory=list)

    changes: list[BriefChangeItem] = Field(default_factory=list)

    overall_severity: Literal["Low", "Medium", "High"] | None = None

    why_it_matters: str = ""

    recommended_action: str = ""

    source_url: str | None = None

    evidence_keys: list[str] = Field(default_factory=list)

    screenshot_key: str | None = None

    source: str = "deterministic"


class Run(BaseModel):
    run_id: str

    target_id: str

    trigger_type: TriggerType

    status: RunStatus = RunStatus.QUEUED

    started_at: datetime | None = None

    finished_at: datetime | None = None

    node_timings: list[NodeTiming] = Field(default_factory=list)

    retry_count: int = 0

    summary: str = ""

    error_category: str | None = None

    review_memo: dict[str, Any] | None = None

    reasoning_source: str = "deterministic-fixture"

    notification_status: str = "NOT_REQUIRED"

    monitoring_brief: MonitoringBrief | None = None


class ReviewDecision(BaseModel):
    action: Literal[
        "APPROVE",
        "REJECT",
    ]

    note: str = Field(
        default="",
        max_length=1000,
    )


class RunRequest(BaseModel):
    target_id: str = "benefits-demo"

    idempotency_key: str | None = Field(
        default=None,
        max_length=100,
    )


class DemoVersionRequest(BaseModel):
    target_id: str = "benefits-demo"

    version: Literal[
        "v1",
        "v2",
    ]


class AgentInvocation(BaseModel):
    run_id: str

    target: PortalTarget

    trigger_type: TriggerType


class ApprovedArtifact(BaseModel):
    finding_id: str

    storage_key: str

    content: str


class AddWebsiteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    public_url: str = Field(min_length=8, max_length=2048)

    description: str = Field(default="", max_length=1000)

    monitoring_objective: str = Field(min_length=3, max_length=2000)

    scan_frequency_minutes: int = Field(default=1440, ge=5, le=43200)

    guidance_context: str = Field(default="", max_length=20000)


class ConfirmMonitoringRequest(BaseModel):
    monitored_sections: list[str] = Field(min_length=1, max_length=20)

    @field_validator("monitored_sections")
    @classmethod
    def validate_sections(cls, values: list[str]) -> list[str]:

        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("Each section must contain 1–200 characters")

        return list(dict.fromkeys(value.strip() for value in values))
