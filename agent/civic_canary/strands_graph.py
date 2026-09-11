from __future__ import annotations

import os

from pydantic import BaseModel, Field
from strands import Agent
from strands.models import BedrockModel
from strands.multiagent import GraphBuilder
from strands.multiagent.base import Status
from strands.multiagent.graph import GraphState


class CaptureReview(BaseModel):
    safe_to_analyze: bool
    evidence_scope: list[str] = Field(min_length=1)
    concerns: list[str] = Field(default_factory=list)
    semantic_evidence: list[str] = Field(default_factory=list)
    accessibility_evidence: list[str] = Field(default_factory=list)
    playbook_excerpt: str = ""


class SemanticReview(BaseModel):
    verified_changes: list[str] = Field(default_factory=list)
    needs_review: bool = False


class AccessibilityReview(BaseModel):
    verified_barriers: list[str] = Field(default_factory=list)
    user_impacts: list[str] = Field(default_factory=list)


class PlaybookImpact(BaseModel):
    portal_evidence: str
    playbook_quote: str
    section: str


class ImpactReview(BaseModel):
    mappings: list[PlaybookImpact] = Field(default_factory=list)
    needs_review: bool = False


class ReviewMemo(BaseModel):
    summary: str
    quoted_evidence: list[str] = Field(min_length=1)
    proposed_patches: list[str] = Field(default_factory=list)
    needs_review: bool = False


def _all_complete(required_nodes: list[str]):
    def check(state: GraphState) -> bool:
        return all(
            node_id in state.results and state.results[node_id].status == Status.COMPLETED
            for node_id in required_nodes
        )

    return check


def build_review_graph(model_id: str | None = None, region_name: str | None = None):
    """Build the bounded Strands review graph used for AWS semantic enrichment."""
    resolved_model_id = model_id or os.getenv(
        "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"
    )
    resolved_region = (
        region_name
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    )
    model = BedrockModel(
        model_id=resolved_model_id,
        region_name=resolved_region,
    )
    capture = Agent(
        name="capture_validator",
        model=model,
        system_prompt=(
            "Validate the supplied baseline/current portal evidence. Never browse, submit forms, "
            "infer eligibility, or add facts. Preserve the supplied semantic evidence, "
            "accessibility evidence, and relevant playbook excerpt in the structured result so "
            "parallel reviewers receive the actual evidence. Set safe_to_analyze false if the "
            "packet lacks quoted before/after source evidence."
        ),
        structured_output_model=CaptureReview,
    )
    semantic = Agent(
        name="semantic_diff",
        model=model,
        system_prompt=(
            "Review only the supplied deterministic before/after evidence. Identify material "
            "policy "
            "or document-requirement changes. Quote evidence and do not invent requirements."
        ),
        structured_output_model=SemanticReview,
    )
    accessibility = Agent(
        name="accessibility_audit",
        model=model,
        system_prompt=(
            "Review only the supplied accessibility findings. Explain the user impact without "
            "claiming legal compliance or adding unobserved problems."
        ),
        structured_output_model=AccessibilityReview,
    )
    impact = Agent(
        name="impact_mapper",
        model=model,
        system_prompt=(
            "Map verified changes to quoted nonprofit playbook sections. If evidence is ambiguous, "
            "say NEEDS_REVIEW."
        ),
        structured_output_model=ImpactReview,
    )
    repair = Agent(
        name="repair_drafter",
        model=model,
        system_prompt=(
            "Draft the smallest safe playbook patch supported by quoted evidence. Never publish, "
            "change eligibility, or substitute an unverified URL."
        ),
        structured_output_model=ReviewMemo,
    )

    builder = GraphBuilder()
    builder.add_node(capture, "capture")
    builder.add_node(semantic, "semantic")
    builder.add_node(accessibility, "accessibility")
    builder.add_node(impact, "impact")
    builder.add_node(repair, "repair")
    builder.add_edge("capture", "semantic")
    builder.add_edge("capture", "accessibility")
    join = _all_complete(["semantic", "accessibility"])
    builder.add_edge("semantic", "impact", condition=join)
    builder.add_edge("accessibility", "impact", condition=join)
    builder.add_edge("impact", "repair")
    builder.set_entry_point("capture")
    builder.set_max_node_executions(5)
    builder.set_node_timeout(60)
    builder.set_execution_timeout(240)
    return builder.build()


def validated_review_memo(graph_result) -> ReviewMemo:
    if graph_result.status != Status.COMPLETED:
        raise ValueError(f"Strands graph finished with status {graph_result.status}")
    capture = graph_result.results.get("capture")
    capture_output = (
        getattr(capture.result, "structured_output", None) if capture is not None else None
    )
    validated_capture = CaptureReview.model_validate(capture_output)
    if not validated_capture.safe_to_analyze:
        raise ValueError(
            "Strands capture validator rejected the evidence packet: "
            + "; ".join(validated_capture.concerns)
        )
    repair = graph_result.results.get("repair")
    if repair is None or repair.status != Status.COMPLETED:
        raise ValueError("Strands repair node did not complete")
    structured = getattr(repair.result, "structured_output", None)
    if structured is None:
        raise ValueError("Strands repair node returned no structured output")
    return ReviewMemo.model_validate(structured)
