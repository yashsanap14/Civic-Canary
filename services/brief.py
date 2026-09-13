"""Build human-readable monitoring briefs from scan evidence and findings."""

from __future__ import annotations

from datetime import UTC, datetime

from agent.civic_canary.models import (
    BriefChangeItem,
    BriefStatus,
    Finding,
    Materiality,
    MonitoringBrief,
    PortalSnapshot,
    PortalTarget,
    Run,
    Severity,
)

STATUS_LABELS = {
    BriefStatus.BASELINE_ESTABLISHED: "Baseline established",
    BriefStatus.NO_MATERIAL_CHANGE: "No material change",
    BriefStatus.REVIEW_RECOMMENDED: "Review recommended",
    BriefStatus.IMPORTANT_CHANGE: "Important change detected",
}

SEVERITY_LABELS = {
    Severity.LOW: "Low",
    Severity.MEDIUM: "Medium",
    Severity.HIGH: "High",
    "LOW": "Low",
    "MEDIUM": "Medium",
    "HIGH": "High",
}


def _category_label(finding: Finding) -> str:
    """Map finding metadata to a readable section without site-specific hardcoding."""
    if finding.affected_playbook_sections:
        return finding.affected_playbook_sections[0]
    raw = (finding.category or "CONTENT").replace("_", " ").strip()
    return raw.title() if raw else "Other"


def _severity_rank(value: str | Severity | None) -> int:
    label = SEVERITY_LABELS.get(value, value) if value is not None else "Low"
    return {"Low": 1, "Medium": 2, "High": 3}.get(str(label), 1)


def _overall_severity(findings: list[Finding]) -> str | None:
    if not findings:
        return None
    top = max(findings, key=lambda item: _severity_rank(item.severity))
    return SEVERITY_LABELS.get(top.severity, "Medium")


def _packet_summary(run: Run) -> str | None:
    memo = run.review_memo or {}
    packet = memo.get("packet") if isinstance(memo, dict) else None
    if isinstance(packet, dict):
        summary = packet.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return None


def _sections_for(target: PortalTarget, snapshot: PortalSnapshot | None, run: Run) -> list[str]:
    if target.monitored_sections:
        return list(target.monitored_sections)[:20]
    if target.recommended_sections:
        return list(target.recommended_sections)[:20]
    memo = run.review_memo or {}
    packet = memo.get("packet") if isinstance(memo, dict) else None
    if isinstance(packet, dict):
        sections = packet.get("recommended_sections")
        if isinstance(sections, list) and sections:
            return [str(item) for item in sections[:20]]
    if snapshot is not None:
        headings = list(
            dict.fromkeys(heading for page in snapshot.pages for heading in page.headings)
        )
        if headings:
            return headings[:20]
    return ["Monitored page content"]


def _change_items(findings: list[Finding]) -> list[BriefChangeItem]:
    items: list[BriefChangeItem] = []
    for finding in findings:
        severity = SEVERITY_LABELS.get(finding.severity, "Medium")
        action = (
            "Review the proposed guidance and approve or reject before publishing updates."
            if finding.status == "OPEN"
            else "Decision already recorded for this finding."
        )
        if finding.proposed_patch.strip():
            action = (
                f"Review and approve the proposed guidance update: "
                f"{finding.proposed_patch.strip()[:180]}"
            )
        items.append(
            BriefChangeItem(
                category=_category_label(finding),
                title=finding.title,
                severity=severity,  # type: ignore[arg-type]
                previous=finding.before or "Not stated on the previous page.",
                current=finding.after or finding.title,
                impact=finding.why_it_matters
                or "This change may affect people who rely on the published guidance.",
                recommended_action=action,
            )
        )
    return items


def build_monitoring_brief(
    *,
    run: Run,
    target: PortalTarget,
    findings: list[Finding],
    snapshot: PortalSnapshot | None = None,
    baseline_established: bool = False,
) -> MonitoringBrief:
    """Create a concise monitoring brief for UI and history."""
    scanned_at = run.finished_at or run.started_at or datetime.now(UTC)
    source_url = target.start_url
    if snapshot and snapshot.pages:
        source_url = snapshot.pages[0].url or source_url
    screenshot_key = None
    if snapshot is not None:
        screenshot_key = snapshot.screenshot_key or next(
            (page.screenshot_key for page in snapshot.pages if page.screenshot_key),
            None,
        )
    evidence_keys: list[str] = []
    if snapshot is not None:
        evidence_keys.append(f"snapshots/{target.target_id}/{run.run_id}/snapshot.json")
    if screenshot_key:
        evidence_keys.append(screenshot_key)
    for finding in findings:
        evidence_keys.extend(finding.evidence_keys)

    sections = _sections_for(target, snapshot, run)
    packet_summary = _packet_summary(run)
    source = (
        "strands-bedrock"
        if run.reasoning_source == "strands-bedrock" and packet_summary
        else ("setup" if baseline_established else "deterministic")
    )

    if baseline_established:
        status = BriefStatus.BASELINE_ESTABLISHED
        # Prefer a baseline-specific narrative; packet text may sound like a comparison scan.
        executive = (
            f"Civic Canary captured the first trusted baseline for {target.name}. "
            f"Monitored sections were identified from the live page and are ready for confirmation. "
            f"Future scans will compare against this baseline and only surface meaningful changes."
        )
        if packet_summary:
            executive = (
                f"{executive} Agent notes from inspection: {packet_summary}"
            )
        why = "A stable baseline is required before material-change detection can begin."
        action = "Confirm the sections to monitor, or adjust them before scheduled scanning continues."
        changes: list[BriefChangeItem] = []
        overall = None
        source = "setup" if source != "strands-bedrock" else "strands-bedrock"
    elif not findings:
        status = BriefStatus.NO_MATERIAL_CHANGE
        executive = packet_summary or (
            f"Civic Canary reviewed the monitored sections on {target.name} and found no "
            f"meaningful policy or eligibility changes since the previous baseline. "
            f"Published guidance remains consistent with the last trusted version."
        )
        why = "No material drift was detected against the approved baseline."
        action = "No action required. Continue scheduled monitoring."
        changes = []
        overall = None
    else:
        has_material = any(finding.materiality == Materiality.MATERIAL for finding in findings)
        has_review = any(finding.materiality == Materiality.NEEDS_REVIEW for finding in findings)
        if has_material and not has_review:
            status = BriefStatus.IMPORTANT_CHANGE
        elif has_review and not has_material:
            status = BriefStatus.REVIEW_RECOMMENDED
        elif has_material:
            status = BriefStatus.IMPORTANT_CHANGE
        else:
            status = BriefStatus.REVIEW_RECOMMENDED
        changes = _change_items(findings)
        overall = _overall_severity(findings)
        categories = sorted({item.category for item in changes})
        category_text = ", ".join(categories[:5]) if categories else "monitored content"
        executive = packet_summary or (
            f"Civic Canary detected {len(findings)} change(s) on {target.name} affecting "
            f"{category_text}. Review each item below before updating community guidance."
        )
        why = changes[0].impact if changes else "Detected changes may affect public guidance."
        action = (
            "Review each change, then approve or reject the proposed guidance updates."
            if status == BriefStatus.IMPORTANT_CHANGE
            else "Human review is recommended before treating these changes as final."
        )

    return MonitoringBrief(
        run_id=run.run_id,
        target_id=target.target_id,
        website_name=target.name,
        scanned_at=scanned_at,
        status=status,
        status_label=STATUS_LABELS[status],
        executive_summary=executive,
        sections_reviewed=sections,
        changes=changes,
        overall_severity=overall,  # type: ignore[arg-type]
        why_it_matters=why,
        recommended_action=action,
        source_url=source_url,
        evidence_keys=list(dict.fromkeys(evidence_keys)),
        screenshot_key=screenshot_key,
        source=source,
    )
