from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from agent.civic_canary.browser import create_browser_adapter
from agent.civic_canary.engine import CivicCanaryEngine, RunExecutionError
from agent.civic_canary.models import (
    AgentInvocation,
    Materiality,
    NodeTiming,
    RunStatus,
    TriggerType,
)
from agent.civic_canary.strands_graph import build_review_graph, validated_review_memo
from services.observability import log_event
from services.store_factory import default_store

app = BedrockAgentCoreApp()


def _snapshot_evidence(snapshot) -> list[dict]:
    """Create a bounded, inspectable packet for the semantic review graph."""
    return [
        {
            "url": page.url,
            "title": page.title,
            "requirements": page.requirements,
            "semantic_blocks": [block[:1000] for block in page.semantic_blocks[:40]],
            "broken_links": [
                link.model_dump(mode="json") for link in page.links if link.status >= 400
            ],
            "accessibility_issues": [
                issue.model_dump(mode="json") for issue in page.accessibility_issues
            ],
        }
        for page in snapshot.pages[:20]
    ]


@app.entrypoint
def invoke(payload: dict) -> dict:
    invocation = AgentInvocation.model_validate(payload)
    log_event(
        "agentcore_run_started",
        run_id=invocation.run_id,
        target_id=invocation.target.target_id,
    )
    store = default_store()
    target = invocation.target
    browser_mode = os.getenv("BROWSER_MODE", "agentcore")
    browser = create_browser_adapter(browser_mode)
    baseline = store.get_baseline(target.target_id)
    if baseline is None:
        baseline_target = target.model_copy(
            update={"active_version": target.baseline_version}
        )
        baseline = asyncio.run(browser.capture(baseline_target, "baseline-v1"))
        store.put_baseline(baseline)
    try:
        run, snapshot, findings = asyncio.run(
            CivicCanaryEngine(browser).execute(
                target=target,
                baseline=baseline,
                playbook=store.load_playbook(target.playbook_key),
                trigger_type=TriggerType(invocation.trigger_type),
                run_id=invocation.run_id,
            )
        )
    except RunExecutionError as exc:
        store.put_run(exc.run)
        log_event(
            "agentcore_run_failed",
            run_id=exc.run.run_id,
            error_category=exc.run.error_category,
        )
        return {"run": exc.run.model_dump(mode="json"), "findings": []}
    review_summary = ""
    if findings and os.getenv("ENABLE_STRANDS_REVIEW_GRAPH", "true").lower() == "true":
        review_input = json.dumps(
            {
                "instruction": (
                    "Validate these deterministic Civic Canary findings and draft a safe "
                    "reviewer memo."
                ),
                "findings": [finding.model_dump(mode="json") for finding in findings],
                "baseline_snapshot": _snapshot_evidence(baseline),
                "current_snapshot": _snapshot_evidence(snapshot),
                "playbook": store.load_playbook(target.playbook_key)[:20000],
            }
        )
        error: Exception | None = None
        for attempt in range(2):
            try:
                result = build_review_graph()(
                    review_input, invocation_state={"run_id": run.run_id}
                )
                memo = validated_review_memo(result)
                review_summary = memo.model_dump_json()
                run.review_memo = memo.model_dump(mode="json")
                run.retry_count += attempt
                for node in result.execution_order:
                    node_result = result.results.get(node.node_id)
                    run.node_timings.append(
                        NodeTiming(
                            node=f"strands-{node.node_id}",
                            duration_ms=max(0, node.execution_time),
                            status=(
                                "SUCCEEDED"
                                if node_result and node_result.status.value == "completed"
                                else "FAILED"
                            ),
                        )
                    )
                if memo.needs_review:
                    for finding in findings:
                        finding.materiality = Materiality.NEEDS_REVIEW
                error = None
                break
            except Exception as exc:
                error = exc
        if error:
            run.status = RunStatus.FAILED
            run.finished_at = datetime.now(UTC)
            run.error_category = type(error).__name__
            run.summary = f"Strands review graph failed after one repair attempt: {error}"
            store.put_run(run)
            log_event(
                "agentcore_run_failed",
                run_id=run.run_id,
                error_category=run.error_category,
            )
            return {"run": run.model_dump(mode="json"), "findings": []}
    store.put_snapshot(snapshot)
    persisted = [finding for finding in findings if store.put_finding(finding)]
    run.summary = (
        f"Created {len(persisted)} new finding(s) requiring review. "
        f"Strands graph completed: {bool(review_summary)}."
    )
    run.finished_at = datetime.now(UTC)
    store.put_run(run)
    log_event(
        "agentcore_run_completed",
        run_id=run.run_id,
        status=run.status,
        new_findings=len(persisted),
    )
    return {
        "run": run.model_dump(mode="json"),
        "findings": [finding.model_dump(mode="json") for finding in persisted],
    }


if __name__ == "__main__":
    app.run()
