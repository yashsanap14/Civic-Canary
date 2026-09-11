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
            raise ValueError(
                "CIVIC_CANARY_DEMO_BASE_URL is required "
                "when BROWSER_MODE=agentcore"
            )

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
            raise ValueError(
                "journey paths must be absolute "
                "and cannot traverse directories"
            )
        return value


class PortalTarget(BaseModel):
    target_id: str = "benefits-demo"

    name: str = "River County Benefits Portal"

    start_url: str = Field(
        default_factory=default_start_url
    )

    allowed_hosts: list[str] = Field(
        default_factory=default_allowed_hosts
    )

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

    playbook_key: str = (
        "playbooks/benefits-demo/current.md"
    )

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

    headings: list[str] = Field(
        default_factory=list
    )

    visible_text: str

    semantic_blocks: list[str] = Field(
        default_factory=list
    )

    requirements: list[str] = Field(
        default_factory=list
    )

    links: list[LinkFact] = Field(
        default_factory=list
    )

    accessibility_issues: list[
        AccessibilityIssue
    ] = Field(
        default_factory=list
    )

    screenshot_key: str | None = None


class PortalSnapshot(BaseModel):
    target_id: str
    run_id: str
    version: str

    captured_at: datetime = Field(
        default_factory=utc_now
    )

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

    evidence_keys: list[str] = Field(
        default_factory=list
    )

    evidence_urls: list[str] = Field(
        default_factory=list
    )

    screenshot_urls: list[str] = Field(
        default_factory=list
    )

    status: FindingStatus = FindingStatus.OPEN

    created_at: datetime = Field(
        default_factory=utc_now
    )

    decision_note: str | None = None

    decided_at: datetime | None = None

    approved_artifact_key: str | None = None


class NodeTiming(BaseModel):
    node: str

    duration_ms: int = Field(
        ge=0
    )

    status: Literal[
        "SUCCEEDED",
        "FAILED",
    ]


class Run(BaseModel):
    run_id: str

    target_id: str

    trigger_type: TriggerType

    status: RunStatus = RunStatus.QUEUED

    started_at: datetime | None = None

    finished_at: datetime | None = None

    node_timings: list[NodeTiming] = Field(
        default_factory=list
    )

    retry_count: int = 0

    summary: str = ""

    error_category: str | None = None

    review_memo: dict[str, Any] | None = None


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