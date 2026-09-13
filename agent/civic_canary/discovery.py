"""Objective-guided page discovery for live website onboarding.

Finds a small set of relevant pages from a parent/entry URL without crawling
the whole site. Later scans reuse the confirmed journey_steps map.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

from .models import (
    DiscoveredPage,
    DiscoverySummary,
    JourneyStep,
    PageSnapshot,
    PortalTarget,
    SkippedDiscoveryItem,
)

_NAV_ONLY_MARKERS = re.compile(
    r"\b(home|menu|skip to|accessibility|cookie|privacy|login|sign in|contact us)\b",
    re.I,
)
_UNRELATED_MARKERS = re.compile(
    r"\b(careers|job openings|press release|media kit|twitter|facebook|instagram|"
    r"youtube|linkedin|donate now)\b",
    re.I,
)


def discovery_max_pages() -> int:
    return max(1, min(int(os.getenv("CIVIC_CANARY_DISCOVERY_MAX_PAGES", "5")), 12))


def discovery_max_depth() -> int:
    return max(0, min(int(os.getenv("CIVIC_CANARY_DISCOVERY_MAX_DEPTH", "3")), 6))


def discovery_enabled_for(target: PortalTarget) -> bool:
    """Run discovery during live setup when the journey is still the single entry page."""
    if target.kind != "live":
        return False
    if target.setup_status not in {"PENDING", "FAILED"}:
        return False
    if os.getenv("CIVIC_CANARY_DISCOVERY", "1").lower() in {"0", "false", "no", "off"}:
        return False
    if len(target.journey_steps) == 1 and target.journey_steps[0].path == "/":
        return True
    # Explicit re-discovery when journey was never confirmed beyond the entry label.
    return len(target.journey_steps) <= 1


def canonicalize_url(url: str) -> str:
    """Normalize URL identity: drop fragment, default ports, trailing slash (except root)."""
    bare, _fragment = urldefrag(url.strip())
    parsed = urlparse(bare)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = parsed.query
    netloc = host
    if parsed.port and parsed.port not in {80, 443}:
        netloc = f"{host}:{parsed.port}"
    return urlunparse((scheme, netloc, path, "", query, ""))


def url_path_and_query(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path


def content_fingerprint(page: PageSnapshot) -> str:
    text = "\n".join(page.semantic_blocks or [page.visible_text])[:8000]
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _tokenize_objective(objective: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]{3,}", objective.lower())
    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "monitor",
        "monitoring",
        "changes",
        "change",
        "page",
        "website",
        "site",
        "about",
        "into",
        "their",
        "have",
        "will",
        "any",
    }
    return [token for token in tokens if token not in stop]


def score_link_for_objective(*, text: str, href: str, objective: str) -> float:
    """Heuristic relevance score; higher is better. Negative means skip."""
    blob = f"{text} {href}".lower()
    if _UNRELATED_MARKERS.search(blob) and not any(
        token in blob for token in _tokenize_objective(objective)[:4]
    ):
        return -2.0
    tokens = _tokenize_objective(objective)
    if not tokens:
        return 0.1
    hits = sum(1 for token in tokens if token in blob)
    path_bonus = 0.0
    path = urlparse(href).path.lower()
    for token in tokens:
        if token in path:
            path_bonus += 0.75
    # Prefer content-ish paths over pure chrome.
    if _NAV_ONLY_MARKERS.fullmatch(text.strip() or ""):
        return -1.0
    return hits + path_bonus


def is_navigation_only_page(page: PageSnapshot) -> bool:
    blocks = [block for block in (page.semantic_blocks or []) if len(block) > 40]
    if len(blocks) >= 3:
        return False
    link_count = len(page.links)
    text_len = len(page.visible_text or "")
    return link_count >= 8 and text_len < 900


def near_duplicate_fingerprint(left: str, right: str) -> bool:
    return bool(left and right and left == right)


@dataclass
class _Candidate:
    url: str
    text: str
    score: float
    depth: int
    from_url: str


@dataclass
class DiscoveryPlanner:
    objective: str
    allowed_hosts: set[str]
    max_pages: int
    max_depth: int
    skipped: list[SkippedDiscoveryItem] = field(default_factory=list)
    seen_urls: set[str] = field(default_factory=set)
    seen_fingerprints: set[str] = field(default_factory=set)
    selected: list[DiscoveredPage] = field(default_factory=list)

    def accept_host(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host in self.allowed_hosts

    def consider_links(
        self, *, page: PageSnapshot, depth: int
    ) -> list[_Candidate]:
        ranked: list[_Candidate] = []
        for link in page.links:
            href = link.href
            if not href.lower().startswith("https://"):
                continue
            canonical = canonicalize_url(href)
            if canonical in self.seen_urls:
                self.skipped.append(
                    SkippedDiscoveryItem(
                        label=link.text[:200] or canonical,
                        reason="duplicate links",
                        url=canonical,
                    )
                )
                continue
            if not self.accept_host(canonical):
                self.skipped.append(
                    SkippedDiscoveryItem(
                        label=link.text[:200] or canonical,
                        reason="outside trusted domain",
                        url=canonical,
                    )
                )
                continue
            # Same page with only a different anchor already collapsed by canonicalize.
            score = score_link_for_objective(
                text=link.text, href=canonical, objective=self.objective
            )
            if score < 0:
                reason = (
                    "navigation-only pages"
                    if score <= -1 and score > -2
                    else "unrelated programs"
                )
                self.skipped.append(
                    SkippedDiscoveryItem(
                        label=link.text[:200] or canonical,
                        reason=reason,
                        url=canonical,
                    )
                )
                continue
            if score < 0.75 and depth > 0:
                self.skipped.append(
                    SkippedDiscoveryItem(
                        label=link.text[:200] or canonical,
                        reason="unrelated programs",
                        url=canonical,
                    )
                )
                continue
            ranked.append(
                _Candidate(
                    url=canonical,
                    text=link.text[:200] or canonical,
                    score=score,
                    depth=depth + 1,
                    from_url=page.url,
                )
            )
        ranked.sort(key=lambda item: (-item.score, item.url))
        # Bound fan-out from each page so we never crawl the whole site.
        return ranked[: max(3, self.max_pages)]

    def register_page(
        self,
        page: PageSnapshot,
        *,
        label: str,
        reason: str,
        depth: int,
        selected: bool = True,
    ) -> DiscoveredPage | None:
        canonical = canonicalize_url(page.url)
        if canonical in self.seen_urls:
            self.skipped.append(
                SkippedDiscoveryItem(
                    label=label, reason="duplicate links", url=canonical
                )
            )
            return None
        fingerprint = content_fingerprint(page)
        if fingerprint in self.seen_fingerprints:
            self.skipped.append(
                SkippedDiscoveryItem(
                    label=label,
                    reason="duplicate or near-duplicate content",
                    url=canonical,
                )
            )
            self.seen_urls.add(canonical)
            return None
        if selected and is_navigation_only_page(page) and depth > 0:
            self.skipped.append(
                SkippedDiscoveryItem(
                    label=label, reason="navigation-only pages", url=canonical
                )
            )
            self.seen_urls.add(canonical)
            return None
        self.seen_urls.add(canonical)
        self.seen_fingerprints.add(fingerprint)
        discovered = DiscoveredPage(
            url=canonical,
            path=url_path_and_query(canonical),
            label=(label or page.title or "Page")[:200],
            selected=selected,
            reason=reason[:500],
            depth=depth,
            content_fingerprint=fingerprint,
        )
        if selected:
            self.selected.append(discovered)
        return discovered


def compact_skipped(items: list[SkippedDiscoveryItem], limit: int = 40) -> list[SkippedDiscoveryItem]:
    """Dedupe skipped rows by reason+url while preserving order."""
    seen: set[tuple[str, str]] = set()
    out: list[SkippedDiscoveryItem] = []
    for item in items:
        key = (item.reason, item.url or item.label)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def journey_steps_from_discovery(summary: DiscoverySummary) -> list[JourneyStep]:
    steps: list[JourneyStep] = []
    for page in summary.discovered_pages:
        if not page.selected:
            continue
        steps.append(JourneyStep(path=page.path, label=page.label))
    if not steps:
        # Always keep at least the entry page.
        entry = summary.discovered_pages[0] if summary.discovered_pages else None
        if entry:
            return [JourneyStep(path=entry.path, label=entry.label)]
        return [JourneyStep(path="/", label="Submitted page")]
    return steps[: discovery_max_pages()]


def apply_page_edits(
    summary: DiscoverySummary | None,
    edits: list,
    *,
    fallback_entry_url: str,
    objective: str,
    website_name: str,
) -> tuple[DiscoverySummary, list[JourneyStep]]:
    """Merge reviewer page edits into discovery summary + journey_steps."""
    if not edits:
        if summary is None:
            path = url_path_and_query(fallback_entry_url)
            page = DiscoveredPage(
                url=canonicalize_url(fallback_entry_url),
                path=path,
                label="Submitted page",
                selected=True,
                reason="Entry page",
            )
            summary = DiscoverySummary(
                website_name=website_name,
                entry_url=fallback_entry_url,
                monitoring_objective=objective,
                discovered_pages=[page],
                skipped=[],
                pages_visited=1,
                max_pages=discovery_max_pages(),
            )
        return summary, journey_steps_from_discovery(summary)

    pages: list[DiscoveredPage] = []
    for edit in edits:
        selected = bool(getattr(edit, "selected", True))
        path = edit.path
        label = edit.label.strip() or path
        url = edit.url or urljoin(fallback_entry_url, path)
        pages.append(
            DiscoveredPage(
                url=canonicalize_url(url),
                path=path,
                label=label[:200],
                selected=selected,
                reason="Reviewer-edited monitoring page",
            )
        )
    updated = DiscoverySummary(
        website_name=website_name,
        entry_url=(summary.entry_url if summary else fallback_entry_url),
        monitoring_objective=(summary.monitoring_objective if summary else objective),
        discovered_pages=pages,
        skipped=list(summary.skipped) if summary else [],
        pages_visited=summary.pages_visited if summary else len(pages),
        max_pages=summary.max_pages if summary else discovery_max_pages(),
    )
    return updated, journey_steps_from_discovery(updated)


def build_discovery_summary(
    *,
    website_name: str,
    entry_url: str,
    objective: str,
    pages: list[DiscoveredPage],
    skipped: list[SkippedDiscoveryItem],
    pages_visited: int,
    max_pages: int,
) -> DiscoverySummary:
    return DiscoverySummary(
        website_name=website_name,
        entry_url=entry_url,
        monitoring_objective=objective,
        discovered_pages=pages,
        skipped=compact_skipped(skipped),
        pages_visited=pages_visited,
        max_pages=max_pages,
    )


def refine_candidates_with_strands(
    *,
    objective: str,
    entry_title: str,
    candidates: list[tuple[str, str, float]],
    limit: int,
) -> list[str] | None:
    """Optional Strands ranking. Returns ordered canonical URLs or None on failure."""
    if not candidates or os.getenv("CIVIC_CANARY_DISCOVERY_STRANDS", "1").lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return None
    try:
        from strands import Agent
        from strands.models import BedrockModel
    except Exception:
        return None
    lines = "\n".join(
        f"- score={score:.2f} | {text[:80]} | {url}" for url, text, score in candidates[:20]
    )
    prompt = (
        "You help a read-only government website monitor choose a few pages to watch.\n"
        f"Monitoring objective: {objective}\n"
        f"Entry page title: {entry_title}\n"
        "Candidate links (do not invent URLs):\n"
        f"{lines}\n"
        f"Return ONLY a JSON array of up to {limit} absolute https URLs from the list, "
        "most relevant first. Prefer eligibility, requirements, FAQ, apply, and policy pages. "
        "Exclude careers, social, and unrelated benefit programs."
    )
    try:
        model_id = os.getenv(
            "BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        agent = Agent(model=BedrockModel(model_id=model_id))
        result = agent(prompt)
        text = str(result)
        match = re.search(r"\[.*\]", text, re.S)
        if not match:
            return None

        urls = json.loads(match.group(0))
        if not isinstance(urls, list):
            return None
        allowed = {canonicalize_url(url) for url, _text, _score in candidates}
        ordered: list[str] = []
        for item in urls:
            if not isinstance(item, str):
                continue
            canonical = canonicalize_url(item)
            if canonical in allowed and canonical not in ordered:
                ordered.append(canonical)
        return ordered[:limit] or None
    except Exception:
        return None


NavigatePage = Callable[[str], PageSnapshot]


def discover_pages(
    *,
    target: PortalTarget,
    fetch_page: NavigatePage,
    go_back: Callable[[], None] | None = None,
) -> tuple[list[PageSnapshot], DiscoverySummary]:
    """Visit the entry page, then a bounded set of relevant internal pages."""
    max_pages = discovery_max_pages()
    max_depth = discovery_max_depth()
    allowed = {host.lower() for host in target.allowed_hosts}
    planner = DiscoveryPlanner(
        objective=target.monitoring_objective,
        allowed_hosts=allowed,
        max_pages=max_pages,
        max_depth=max_depth,
    )

    snapshots: list[PageSnapshot] = []
    entry = fetch_page(target.start_url)
    entry_page = planner.register_page(
        entry,
        label="Entry page",
        reason="User-provided starting page",
        depth=0,
        selected=True,
    )
    snapshots.append(entry)
    if entry_page is None:
        summary = build_discovery_summary(
            website_name=target.name,
            entry_url=target.start_url,
            objective=target.monitoring_objective,
            pages=[],
            skipped=planner.skipped,
            pages_visited=1,
            max_pages=max_pages,
        )
        return snapshots, summary

    # First-pass candidates from the entry page.
    frontier = planner.consider_links(page=entry, depth=0)
    strands_order = refine_candidates_with_strands(
        objective=target.monitoring_objective,
        entry_title=entry.title,
        candidates=[(item.url, item.text, item.score) for item in frontier],
        limit=max_pages - 1,
    )
    if strands_order:
        by_url = {item.url: item for item in frontier}
        frontier = [
            by_url[url]
            for url in strands_order
            if url in by_url
        ] + [item for item in frontier if item.url not in set(strands_order)]

    queue: list[_Candidate] = list(frontier)
    visited_hub = canonicalize_url(entry.url)

    while queue and len(planner.selected) < max_pages:
        candidate = queue.pop(0)
        if candidate.depth > max_depth:
            planner.skipped.append(
                SkippedDiscoveryItem(
                    label=candidate.text,
                    reason="depth limit",
                    url=candidate.url,
                )
            )
            continue
        if candidate.url in planner.seen_urls:
            continue
        if len(planner.selected) >= max_pages:
            break
        try:
            page = fetch_page(candidate.url)
        except Exception as exc:  # noqa: BLE001 - keep discovery resilient
            planner.skipped.append(
                SkippedDiscoveryItem(
                    label=candidate.text,
                    reason=f"navigation failed: {type(exc).__name__}",
                    url=candidate.url,
                )
            )
            if go_back is not None:
                try:
                    go_back()
                except Exception:
                    pass
            continue

        discovered = planner.register_page(
            page,
            label=candidate.text or page.title,
            reason=f"Relevant to objective (score {candidate.score:.1f})",
            depth=candidate.depth,
            selected=True,
        )
        if discovered is None:
            if go_back is not None:
                try:
                    go_back()
                except Exception:
                    pass
            continue

        snapshots.append(page)
        # Explore one level deeper only from strongly relevant pages.
        if candidate.depth < max_depth and len(planner.selected) < max_pages:
            for child in planner.consider_links(page=page, depth=candidate.depth):
                if child.url not in planner.seen_urls and child.url not in {
                    item.url for item in queue
                }:
                    queue.append(child)
        if go_back is not None and canonicalize_url(page.url) != visited_hub:
            try:
                go_back()
            except Exception:
                # Fall back to absolute navigation on the next fetch_page call.
                pass
        # Keep queue short.
        queue.sort(key=lambda item: (-item.score, item.depth, item.url))
        queue = queue[: max_pages * 2]

    summary = build_discovery_summary(
        website_name=target.name,
        entry_url=target.start_url,
        objective=target.monitoring_objective,
        pages=list(planner.selected),
        skipped=planner.skipped,
        pages_visited=len(snapshots),
        max_pages=max_pages,
    )
    return snapshots, summary
