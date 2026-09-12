from __future__ import annotations

import hashlib
import re

from .models import (
    DetectedChange,
    Finding,
    Materiality,
    PortalSnapshot,
    Severity,
)


def _fingerprint(*parts: str) -> str:
    normalized = "|".join(re.sub(r"\s+", " ", part).strip().lower() for part in parts)
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


def detect_changes(baseline: PortalSnapshot, current: PortalSnapshot) -> list[DetectedChange]:
    changes: list[DetectedChange] = []
    baseline_requirements = {
        requirement for page in baseline.pages for requirement in page.requirements
    }
    current_requirements = {
        requirement for page in current.pages for requirement in page.requirements
    }
    for added in sorted(current_requirements - baseline_requirements):
        page = next(page for page in current.pages if added in page.requirements)
        changes.append(
            DetectedChange(
                category="REQUIREMENT",
                previous_value=None,
                current_value=added,
                source_url=page.url,
                evidence=f'New required document: "{added}"',
                confidence=1.0,
                materiality=Materiality.MATERIAL,
                severity=Severity.HIGH,
                fingerprint=_fingerprint(current.target_id, "requirement", added),
            )
        )

    baseline_broken = {
        link.href for page in baseline.pages for link in page.links if link.status >= 400
    }
    for page in current.pages:
        for link in page.links:
            if link.status >= 400 and link.href not in baseline_broken:
                changes.append(
                    DetectedChange(
                        category="BROKEN_LINK",
                        previous_value=f"{link.text}: available",
                        current_value=f"{link.text}: {link.href} returned {link.status}",
                        source_url=page.url,
                        evidence=f'Link "{link.text}" is unavailable (HTTP {link.status}).',
                        confidence=1.0,
                        materiality=Materiality.MATERIAL,
                        severity=Severity.MEDIUM,
                        fingerprint=_fingerprint(current.target_id, "broken-link", link.href),
                    )
                )

    baseline_issues = {
        (issue.rule, issue.selector)
        for page in baseline.pages
        for issue in page.accessibility_issues
    }
    for page in current.pages:
        for issue in page.accessibility_issues:
            if (issue.rule, issue.selector) not in baseline_issues:
                changes.append(
                    DetectedChange(
                        category="ACCESSIBILITY",
                        previous_value="Control previously met the accessibility check",
                        current_value=issue.description,
                        source_url=page.url,
                        evidence=f"{issue.rule} at {issue.selector}: {issue.description}",
                        confidence=1.0,
                        materiality=Materiality.MATERIAL,
                        severity=issue.severity,
                        fingerprint=_fingerprint(
                            current.target_id, "accessibility", issue.rule, issue.selector
                        ),
                    )
                )

    # Monitor ordinary policy copy as well as explicit data-requirement markers. This
    # is deliberately conservative: only high-signal public-service language becomes
    # actionable, while general layout/navigation copy remains cosmetic.
    high_signal = re.compile(
        r"\b(eligible|eligibility|income|deadline|apply by|must|required|benefit amount|"
        r"proof of|document|residen(?:t|cy)|citizen(?:ship)?|"
        r"hours?|route|service|closed?|closure|renovation|relocat(?:e|ed|ion)|"
        r"appointment|walk-in|vaccine|distribution|pickup|branch|clinic|schedule|"
        r"operat(?:e|es|ed|ing)|departs?|trip)\b",
        re.IGNORECASE,
    )
    for page_index, page in enumerate(current.pages):
        if page_index >= len(baseline.pages):
            continue
        previous = baseline.pages[page_index]
        excluded = set(previous.requirements) | set(page.requirements)
        before = {block for block in previous.semantic_blocks if block not in excluded}
        after = {block for block in page.semantic_blocks if block not in excluded}
        removed = sorted(block for block in before - after if high_signal.search(block))
        added = sorted(block for block in after - before if high_signal.search(block))
        if not removed and not added:
            continue
        previous_value = " | ".join(removed) or None
        current_value = " | ".join(added) or "Relevant policy text was removed."
        changes.append(
            DetectedChange(
                category="CONTENT",
                previous_value=previous_value,
                current_value=current_value,
                source_url=page.url,
                evidence=(
                    f'Previous policy text: "{previous_value or "none"}"; '
                    f'current policy text: "{current_value}".'
                ),
                confidence=0.85,
                materiality=Materiality.NEEDS_REVIEW,
                severity=Severity.MEDIUM,
                fingerprint=_fingerprint(
                    current.target_id,
                    "content",
                    previous_value or "",
                    current_value,
                    page.url,
                ),
            )
        )
    return changes


def map_playbook_section(change: DetectedChange, playbook: str) -> tuple[list[str], str]:
    preferred = {
        "REQUIREMENT": "Required documents",
        "BROKEN_LINK": "Language assistance",
        "ACCESSIBILITY": "Application support",
        "CONTENT": "Service updates",
    }
    section = preferred[change.category]
    if change.category == "REQUIREMENT":
        patch = (
            "Add the following verified item to the Required documents checklist:\n\n"
            f"- {change.current_value}\n\n"
            f"Source evidence: {change.evidence}"
        )
    elif change.category == "BROKEN_LINK":
        patch = (
            "Temporarily mark the affected public link as unavailable and direct clients to "
            "staff-assisted support. Do not substitute an unverified URL.\n\n"
            f"Source evidence: {change.evidence}"
        )
    elif change.category == "ACCESSIBILITY":
        patch = (
            "Flag this form control for accessibility remediation and advise staff to offer "
            "assisted completion until the public page is fixed.\n\n"
            f"Source evidence: {change.evidence}"
        )
    else:
        patch = (
            "Do not change client-facing guidance automatically. Ask the playbook owner to "
            "compare this service-language change with the official source and approve exact "
            "wording. Mark the draft NEEDS_REVIEW.\n\n"
            f"Source evidence: {change.evidence}"
        )
    if f"## {section}" not in playbook:
        # Fall back for older playbooks that still use eligibility wording.
        if change.category == "CONTENT" and "## Eligibility and deadlines" in playbook:
            section = "Eligibility and deadlines"
        else:
            return ["Needs playbook owner review"], patch
    return [section], patch


def _guidance_excerpt(playbook: str, section: str) -> str:
    marker = f"## {section}"
    if marker not in playbook:
        return ""
    body = playbook.split(marker, 1)[1]
    next_heading = re.search(r"\n## ", body)
    excerpt = body[: next_heading.start()] if next_heading else body
    return re.sub(r"\s+", " ", excerpt).strip()


def findings_from_changes(
    changes: list[DetectedChange], *, run_id: str, target_id: str, playbook: str
) -> list[Finding]:
    findings: list[Finding] = []
    titles = {
        "REQUIREMENT": "New required document or checklist item",
        "BROKEN_LINK": "Important public link is unavailable",
        "ACCESSIBILITY": "New accessibility barrier on a public form",
        "CONTENT": "Material public-service content changed",
    }
    why = {
        "REQUIREMENT": "Clients may be turned away or delayed if staff guidance omits the new requirement.",
        "BROKEN_LINK": "People who rely on this link may lose access to language or service information.",
        "ACCESSIBILITY": "People using assistive technology may be unable to complete the public form.",
        "CONTENT": "Neighbors may miss a deadline, location, hour, or service rule if guidance is stale.",
    }
    people = {
        "REQUIREMENT": "People preparing documents for this public service",
        "BROKEN_LINK": "People who need the linked public information",
        "ACCESSIBILITY": "People who use assistive technology on the public form",
        "CONTENT": "Community members who rely on this public-service guidance",
    }
    for change in changes:
        sections, patch = map_playbook_section(change, playbook)
        section = sections[0] if sections else "Needs playbook owner review"
        findings.append(
            Finding(
                finding_id=f"finding-{change.fingerprint}",
                run_id=run_id,
                target_id=target_id,
                title=titles[change.category],
                category=change.category,
                severity=change.severity,
                materiality=change.materiality,
                before=change.previous_value or "Not stated on the previous page",
                after=change.current_value,
                why_it_matters=why[change.category],
                affected_people=people[change.category],
                current_guidance=_guidance_excerpt(playbook, section),
                agent_confidence=change.confidence,
                evidence=[change.evidence, f"Observed at {change.source_url}"],
                affected_playbook_sections=sections,
                proposed_patch=patch,
            )
        )
    return findings
