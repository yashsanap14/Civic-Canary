from __future__ import annotations

import asyncio
import hashlib
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .models import (
    AccessibilityIssue,
    LinkFact,
    PageSnapshot,
    PortalSnapshot,
    PortalTarget,
    Severity,
)


class UnsafeTargetError(ValueError):
    """Raised before browsing when a target violates the read-only policy."""


class BrowserAdapter(ABC):
    @abstractmethod
    async def capture(self, target: PortalTarget, run_id: str) -> PortalSnapshot:
        raise NotImplementedError


def validate_target(target: PortalTarget, *, allow_local: bool = False) -> None:
    if not target.enabled:
        raise UnsafeTargetError("target is disabled")
    parsed = urlparse(target.start_url)
    if parsed.scheme == "fixture":
        if not allow_local:
            raise UnsafeTargetError("fixture targets are only allowed in local/demo mode")
        forbidden = ("login", "log-in", "signin", "sign-in", "captcha", "upload")
        if any(
            term in step.path.lower()
            for step in target.journey_steps
            for term in forbidden
        ):
            raise UnsafeTargetError("login, CAPTCHA, and upload paths are not supported")
        return
    if parsed.scheme not in ({"https", "http"} if allow_local else {"https"}):
        raise UnsafeTargetError("target must use HTTPS")
    if not parsed.hostname or parsed.hostname not in target.allowed_hosts:
        raise UnsafeTargetError("target hostname is not allow-listed")
    validate_navigation_url(target.start_url, target.allowed_hosts, allow_http=allow_local)
    for step in target.journey_steps:
        candidate = urljoin(target.start_url.rstrip("/") + "/", step.path.lstrip("/"))
        validate_navigation_url(candidate, target.allowed_hosts, allow_http=allow_local)


def validate_navigation_url(
    url: str, allowed_hosts: list[str], *, allow_http: bool = False
) -> None:
    """Enforce the same exact-host, read-only navigation policy at every boundary."""
    parsed = urlparse(url)
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme not in allowed_schemes:
        raise UnsafeTargetError("navigation must use an allowed web scheme")
    allowed = {host.lower() for host in allowed_hosts}
    if not parsed.hostname or parsed.hostname.lower() not in allowed:
        raise UnsafeTargetError("navigation escaped the target allow-list")
    forbidden = ("login", "log-in", "signin", "sign-in", "captcha", "upload")
    lowered = f"{parsed.path}?{parsed.query}".lower()
    if any(term in lowered for term in forbidden):
        raise UnsafeTargetError("login, CAPTCHA, and upload paths are not supported")


def request_is_allowed(method: str, url: str, allowed_hosts: list[str]) -> bool:
    """Return whether a browser subrequest is safe to send from a capture session."""
    parsed = urlparse(url)
    if parsed.scheme in {"data", "blob", "about"}:
        return method.upper() in {"GET", "HEAD"}
    if method.upper() not in {"GET", "HEAD"}:
        return False
    try:
        validate_navigation_url(url, allowed_hosts)
    except UnsafeTargetError:
        return False
    return True


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _page_from_html(
    html: str,
    *,
    url: str,
    link_status: Callable[[str], int],
) -> PageSnapshot:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()

    headings = [_normalize_text(node.get_text(" ", strip=True)) for node in soup.select("h1,h2,h3")]
    requirements = [
        _normalize_text(node.get_text(" ", strip=True))
        for node in soup.select("[data-requirement]")
    ]
    semantic_blocks = []
    seen_blocks: set[str] = set()
    for node in soup.select("main h1,main h2,main h3,main p,main li"):
        block = _normalize_text(node.get_text(" ", strip=True))
        if block and block not in seen_blocks:
            semantic_blocks.append(block)
            seen_blocks.add(block)
    links = [
        LinkFact(
            text=_normalize_text(node.get_text(" ", strip=True))
            or node.get("aria-label", "Unlabelled link"),
            href=urljoin(url, node.get("href", "")),
            status=link_status(node.get("href", "")),
        )
        for node in soup.select("a[href]")
    ]

    issues: list[AccessibilityIssue] = []
    html_node = soup.find("html")
    language = html_node.get("lang") if html_node else None
    if not language:
        issues.append(
            AccessibilityIssue(
                rule="html-has-lang",
                description="The page does not declare a language.",
                selector="html",
            )
        )
    for input_node in soup.select("input:not([type='hidden']), select, textarea"):
        input_id = input_node.get("id")
        has_label = bool(input_id and soup.select_one(f"label[for='{input_id}']"))
        has_accessible_name = bool(
            input_node.get("aria-label") or input_node.get("aria-labelledby")
        )
        if not has_label and not has_accessible_name:
            selector = f"#{input_id}" if input_id else input_node.name
            issues.append(
                AccessibilityIssue(
                    rule="label",
                    description="Form control has no accessible label.",
                    selector=selector,
                    severity=Severity.MEDIUM,
                )
            )

    return PageSnapshot(
        url=url,
        title=_normalize_text(soup.title.get_text()) if soup.title else "Untitled page",
        language=language,
        headings=headings,
        visible_text=_normalize_text(soup.get_text(" ", strip=True)),
        semantic_blocks=semantic_blocks,
        requirements=requirements,
        links=links,
        accessibility_issues=issues,
    )


def _snapshot_hash(pages: list[PageSnapshot]) -> str:
    normalized = "\n".join(
        f"{page.url}|{page.title}|{page.visible_text}|"
        f"{','.join(sorted(page.requirements))}|"
        f"{','.join(sorted(f'{link.href}:{link.status}' for link in page.links))}|"
        f"{','.join(sorted(issue.rule + issue.selector for issue in page.accessibility_issues))}"
        for page in pages
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


class FixtureBrowserAdapter(BrowserAdapter):
    def __init__(self, fixture_root: Path) -> None:
        self.fixture_root = fixture_root.resolve()

    async def capture(self, target: PortalTarget, run_id: str) -> PortalSnapshot:
        validate_target(target, allow_local=True)
        version_root = (self.fixture_root / target.active_version).resolve()
        if version_root.parent != self.fixture_root:
            raise UnsafeTargetError("invalid fixture version")

        def status_for(href: str) -> int:
            if not href or href.startswith(("#", "mailto:", "tel:")):
                return 200
            parsed = urlparse(href)
            if parsed.scheme in {"http", "https"}:
                return 200 if parsed.hostname in target.allowed_hosts else 403
            candidate = (version_root / parsed.path.lstrip("/")).resolve()
            return 200 if candidate.is_file() and candidate.parent == version_root else 404

        pages: list[PageSnapshot] = []
        for step in target.journey_steps:
            page_path = (version_root / step.path.lstrip("/")).resolve()
            if page_path.parent != version_root or not page_path.is_file():
                raise FileNotFoundError(f"fixture page not found: {step.path}")
            html = page_path.read_text(encoding="utf-8")
            url = f"https://benefits.demo.local/{step.path.lstrip('/')}"
            pages.append(_page_from_html(html, url=url, link_status=status_for))
        return PortalSnapshot(
            target_id=target.target_id,
            run_id=run_id,
            version=target.active_version,
            pages=pages,
            content_hash=_snapshot_hash(pages),
        )


class HttpBrowserAdapter(BrowserAdapter):
    """Read-only fallback for allow-listed static public pages."""

    def __init__(self, timeout_seconds: float = 15) -> None:
        self.timeout_seconds = timeout_seconds

    async def capture(self, target: PortalTarget, run_id: str) -> PortalSnapshot:
        validate_target(target)
        pages: list[PageSnapshot] = []
        base_url = target.start_url.replace("{version}", target.active_version)
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            for step in target.journey_steps:
                page_url = urljoin(base_url.rstrip("/") + "/", step.path.lstrip("/"))
                validate_navigation_url(page_url, target.allowed_hosts)
                response = None
                for _ in range(6):
                    response = await client.get(
                        page_url, headers={"User-Agent": "CivicCanary/0.1 read-only"}
                    )
                    if not response.is_redirect:
                        break
                    page_url = urljoin(page_url, response.headers.get("location", ""))
                    validate_navigation_url(page_url, target.allowed_hosts)
                else:
                    raise RuntimeError("too many redirects while capturing target")
                if response is None:
                    raise RuntimeError("target returned no response")
                response.raise_for_status()

                async def check_link(href: str, base_page_url: str = page_url) -> int:
                    if not href or href.startswith(("#", "mailto:", "tel:")):
                        return 200
                    linked = urljoin(base_page_url, href)
                    parsed = urlparse(linked)
                    if parsed.hostname not in target.allowed_hosts:
                        return 403
                    try:
                        result = await client.head(linked)
                        if result.status_code in {405, 501}:
                            result = await client.get(
                                linked,
                                headers={"Range": "bytes=0-0"},
                            )
                        return result.status_code
                    except httpx.HTTPError:
                        return 599

                soup = BeautifulSoup(response.text, "html.parser")
                statuses = {
                    node.get("href", ""): await check_link(node.get("href", ""))
                    for node in soup.select("a[href]")
                }
                pages.append(
                    _page_from_html(
                        response.text,
                        url=page_url,
                        link_status=lambda href, checked=statuses: checked.get(href, 599),
                    )
                )
        return PortalSnapshot(
            target_id=target.target_id,
            run_id=run_id,
            version="live",
            pages=pages,
            content_hash=_snapshot_hash(pages),
        )


class AgentCoreBrowserAdapter(BrowserAdapter):
    """Managed-browser implementation used inside AgentCore/AWS mode."""

    def __init__(
        self,
        region: str | None = None,
        identifier: str | None = None,
    ) -> None:
        self.region = (
            region
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        )
        self.identifier = identifier or os.getenv("AGENTCORE_BROWSER_ID")
        if not self.identifier:
            raise ValueError("AGENTCORE_BROWSER_ID is required when BROWSER_MODE=agentcore")

    async def capture(self, target: PortalTarget, run_id: str) -> PortalSnapshot:
        validate_target(target)
        return await asyncio.to_thread(self._capture_sync, target, run_id)

    def _capture_sync(self, target: PortalTarget, run_id: str) -> PortalSnapshot:
        import boto3
        from bedrock_agentcore.tools.browser_client import browser_session
        from playwright.sync_api import sync_playwright

        pages: list[PageSnapshot] = []
        screenshot_key: str | None = None
        axe_path = Path(__file__).resolve().parents[1] / "fixtures" / "axe.min.js"
        if not axe_path.is_file():
            raise RuntimeError("bundled axe-core script is missing from the runtime")
        axe_source = axe_path.read_text(encoding="utf-8")
        with browser_session(
            self.region, identifier=self.identifier
        ) as client, sync_playwright() as playwright:
            ws_url, headers = client.generate_ws_headers()
            browser = playwright.chromium.connect_over_cdp(ws_url, headers=headers)
            context = browser.new_context(service_workers="block")
            def guard_route(route) -> None:
                request = route.request
                unsafe_resource = request.resource_type in {"websocket", "serviceworker"}
                unsafe_request = not request_is_allowed(
                    request.method, request.url, target.allowed_hosts
                )
                if unsafe_resource or unsafe_request:
                    route.abort("blockedbyclient")
                    return
                route.continue_()

            context.route("**/*", guard_route)
            page = context.pages[0] if context.pages else context.new_page()
            context.on("page", lambda popup: popup.close())
            try:
                base_url = target.start_url.replace("{version}", target.active_version)
                for index, step in enumerate(target.journey_steps):
                    page_url = urljoin(base_url.rstrip("/") + "/", step.path.lstrip("/"))
                    validate_navigation_url(page_url, target.allowed_hosts)
                    response = page.goto(page_url, wait_until="networkidle")
                    validate_navigation_url(page.url, target.allowed_hosts)
                    if response and response.status >= 400:
                        raise RuntimeError(f"page returned HTTP {response.status}: {page_url}")
                    page.add_script_tag(content=axe_source)
                    axe_result = page.evaluate(
                        "async () => await window.axe.run(document, {resultTypes: ['violations']})"
                    )
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    statuses: dict[str, int] = {}
                    for anchor in soup.select("a[href]"):
                        href = anchor.get("href", "")
                        linked = urljoin(page.url, href)
                        if not href or href.startswith(("#", "mailto:", "tel:")):
                            statuses[href] = 200
                        elif not request_is_allowed("HEAD", linked, target.allowed_hosts):
                            statuses[href] = 403
                        else:
                            try:
                                linked_response = context.request.head(
                                    linked, max_redirects=0
                                )
                                if linked_response.status in {405, 501}:
                                    linked_response = context.request.get(
                                        linked,
                                        headers={"Range": "bytes=0-0"},
                                        max_redirects=0,
                                    )
                                statuses[href] = linked_response.status
                            except Exception:
                                statuses[href] = 599
                    captured_page = _page_from_html(
                        html,
                        url=page.url,
                        link_status=lambda href, checked=statuses: checked.get(href, 599),
                    )
                    known_issues = {
                        (issue.rule, issue.selector)
                        for issue in captured_page.accessibility_issues
                    }
                    severity_map = {
                        "minor": Severity.LOW,
                        "moderate": Severity.MEDIUM,
                        "serious": Severity.HIGH,
                        "critical": Severity.HIGH,
                    }
                    for violation in axe_result.get("violations", []):
                        for node in violation.get("nodes", []):
                            targets = node.get("target", [])
                            selector = str(targets[0]) if targets else "document"
                            issue_key = (f"axe:{violation['id']}", selector)
                            canonical_key = (violation["id"], selector)
                            if issue_key in known_issues or canonical_key in known_issues:
                                continue
                            captured_page.accessibility_issues.append(
                                AccessibilityIssue(
                                    rule=issue_key[0],
                                    description=violation.get(
                                        "help", "Automated accessibility check failed."
                                    ),
                                    selector=selector,
                                    severity=severity_map.get(
                                        violation.get("impact"), Severity.MEDIUM
                                    ),
                                )
                            )
                    slug = re.sub(r"[^a-z0-9]+", "-", step.label.lower()).strip("-")
                    screenshot_prefix = (
                        f"baselines/{target.target_id}/screenshots"
                        if run_id.startswith("baseline")
                        else f"screenshots/{target.target_id}/{run_id}"
                    )
                    page_screenshot_key = f"{screenshot_prefix}/{index + 1}-{slug}.png"
                    bucket = os.getenv("EVIDENCE_BUCKET") or os.getenv("CIVIC_CANARY_S3_BUCKET")
                    if bucket:
                        boto3.client("s3", region_name=self.region).put_object(
                            Bucket=bucket,
                            Key=page_screenshot_key,
                            Body=page.screenshot(full_page=True),
                            ContentType="image/png",
                        )
                        captured_page.screenshot_key = page_screenshot_key
                        screenshot_key = screenshot_key or page_screenshot_key
                    pages.append(captured_page)
            finally:
                page.close()
                browser.close()
        return PortalSnapshot(
            target_id=target.target_id,
            run_id=run_id,
            version="live",
            pages=pages,
            content_hash=_snapshot_hash(pages),
            screenshot_key=screenshot_key,
        )


def create_browser_adapter(
    mode: str | None = None,
    *,
    fixture_root: Path | None = None,
    region: str | None = None,
    identifier: str | None = None,
) -> BrowserAdapter:
    """Create a browser adapter based on mode (or BROWSER_MODE env var)."""
    selected_mode = (mode or os.getenv("BROWSER_MODE", "local")).lower()
    if selected_mode == "agentcore":
        resolved_identifier = identifier or os.getenv("AGENTCORE_BROWSER_ID")
        if not resolved_identifier:
            raise ValueError("AGENTCORE_BROWSER_ID is required when BROWSER_MODE=agentcore")
        resolved_region = (
            region
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        )
        return AgentCoreBrowserAdapter(
            region=resolved_region, identifier=resolved_identifier
        )
    if selected_mode == "local":
        root = (
            fixture_root
            or (Path(__file__).resolve().parents[2] / "web" / "public" / "portal")
        )
        return FixtureBrowserAdapter(root)
    if selected_mode == "http":
        return HttpBrowserAdapter()
    raise ValueError(
        f"Unsupported BROWSER_MODE: {selected_mode!r}. Expected 'local' or 'agentcore'."
    )
