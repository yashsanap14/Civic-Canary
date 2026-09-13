from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
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


def validate_public_url(url: str) -> str:
    """Reject credentials, unusual ports, non-public addresses and private DNS answers."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        raise UnsafeTargetError(
            "Use a public HTTPS URL without credentials, fragments or custom ports"
        )
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeTargetError("Private hosts are not supported")
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise UnsafeTargetError("Website hostname could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeTargetError("Website resolves to a private or reserved address")
    return url


def page_url_for(target: PortalTarget, path: str) -> str:
    base = target.start_url.replace("{version}", target.active_version)
    # New websites preserve the exact submitted page, including its query string.
    if path == "/" and "{version}" not in target.start_url:
        return base
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def validate_target(target: PortalTarget, *, allow_local: bool = False) -> None:
    if not target.enabled:
        raise UnsafeTargetError("target is disabled")
    parsed = urlparse(target.start_url)
    if parsed.scheme == "fixture":
        if not allow_local:
            raise UnsafeTargetError("fixture targets are only allowed in local/demo mode")
        forbidden = ("login", "log-in", "signin", "sign-in", "captcha", "upload")
        if any(term in step.path.lower() for step in target.journey_steps for term in forbidden):
            raise UnsafeTargetError("login, CAPTCHA, and upload paths are not supported")
        return
    if parsed.scheme not in ({"https", "http"} if allow_local else {"https"}):
        raise UnsafeTargetError("target must use HTTPS")
    if not parsed.hostname or parsed.hostname not in target.allowed_hosts:
        raise UnsafeTargetError("target hostname is not allow-listed")
    validate_navigation_url(target.start_url, target.allowed_hosts, allow_http=allow_local)
    for step in target.journey_steps:
        candidate = page_url_for(target, step.path)
        validate_navigation_url(candidate, target.allowed_hosts, allow_http=allow_local)


def validate_navigation_url(
    url: str, allowed_hosts: list[str], *, allow_http: bool = False
) -> None:
    """Enforce the same exact-host, read-only navigation policy at every boundary."""
    parsed = urlparse(url)
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if parsed.scheme not in allowed_schemes:
        raise UnsafeTargetError("navigation must use an allowed web scheme")
    if parsed.username or parsed.password or parsed.port not in {None, 443, 80}:
        raise UnsafeTargetError("Credentials and custom ports are not supported")
    if parsed.hostname:
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise UnsafeTargetError("Private addresses are not supported")
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


def agentcore_request_should_continue(method: str, url: str, resource_type: str, allowed_hosts: list[str]) -> bool:
    """Allow page allow-list traffic plus AgentCore Session Replay internals.

    Session Replay extensions upload DOM/CDP batches with non-GET methods to AWS
    endpoints. Aborting those requests (or chrome-extension:// / websocket traffic)
    leaves a terminated session with Pages (0) even when page.goto() succeeded.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in {"chrome-extension", "chrome", "data", "blob", "about"}:
        return True
    if resource_type in {"websocket", "eventsource", "manifest", "serviceworker"}:
        return True
    host = (parsed.hostname or "").lower().rstrip(".")
    if host and (
        host == "amazonaws.com"
        or host.endswith(".amazonaws.com")
        or host == "amazon.com"
        or host.endswith(".amazon.com")
        or "bedrock-agentcore" in host
    ):
        return True
    return request_is_allowed(method, url, allowed_hosts)


def _browser_journal(event: str, *, run_id: str, **details: object) -> None:
    """Emit browser lifecycle logs to both structured logging and stdout (journalctl)."""
    from services.observability import log_event

    payload = {"event": event, "run_id": run_id, **details}
    line = json.dumps(payload, separators=(",", ":"), default=str)
    # systemd/journalctl captures process stdout from the EC2 worker.
    print(f"[civic_canary] {line}", flush=True)
    log_event(event, run_id=run_id, **details)


def _wait_for_default_replay_page(context, *, run_id: str, target_id: str, session_id: str | None):
    """Return the instrumented default page; never create an unrecorded page.

    AgentCore Session Replay attaches only to the pre-existing default context/page.
    Official guidance: ``page = browser.contexts[0].pages[0]``. Calling
    ``context.new_page()`` creates a tab the replay extension does not instrument,
    which yields Pages (0) in the AWS console even when capture succeeds.
    """
    wait_seconds = float(os.getenv("AGENTCORE_DEFAULT_PAGE_WAIT_SECONDS", "15"))
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        pages = list(context.pages)
        _browser_journal(
            "browser_default_page_poll",
            run_id=run_id,
            target_id=target_id,
            browser_session_id=session_id,
            page_count=len(pages),
            wait_seconds=wait_seconds,
        )
        if pages:
            return pages[0]
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    raise RuntimeError(
        "AgentCore default page was not available after "
        f"{wait_seconds:.0f}s; Session Replay requires browser.contexts[0].pages[0]. "
        "Refusing context.new_page() because it bypasses replay instrumentation (Pages 0)."
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _page_from_html(
    html: str,
    *,
    url: str,
    link_status: Callable[[str], int],
) -> PageSnapshot:
    soup = BeautifulSoup(html, "html.parser")
    link_soup = BeautifulSoup(html, "html.parser")
    for element in soup.select(
        "script,style,noscript,nav,footer,time,[role=navigation],"
        "[id*=cookie],[class*=cookie],[aria-label*=cookie]"
    ):
        element.decompose()

    headings = [_normalize_text(node.get_text(" ", strip=True)) for node in soup.select("h1,h2,h3")]
    requirements = [
        _normalize_text(node.get_text(" ", strip=True))
        for node in soup.select("[data-requirement]")
    ]
    semantic_blocks = []
    seen_blocks: set[str] = set()
    content_root = soup.select_one("main,article,[role=main]") or soup
    for node in content_root.select("h1,h2,h3,p,li,td,dt,dd"):
        block = _normalize_text(node.get_text(" ", strip=True))
        if re.fullmatch(r"(?:Last updated[: ]*)?\d{4}[-/]\d{2}[-/]\d{2}(?:[ T].*)?", block, re.I):
            continue
        if block and block not in seen_blocks:
            semantic_blocks.append(block[:2000])
            seen_blocks.add(block)
    links = [
        LinkFact(
            text=_normalize_text(node.get_text(" ", strip=True))
            or node.get("aria-label", "Unlabelled link"),
            href=urljoin(url, node.get("href", "")),
            status=link_status(node.get("href", "")),
        )
        for node in link_soup.select("a[href]")[:100]
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
        semantic_blocks=semantic_blocks[:100],
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

    def _version_root(self, target: PortalTarget) -> Path:
        version = target.active_version
        if version not in {"v1", "v2"}:
            raise UnsafeTargetError("invalid fixture version")
        namespace = (target.fixture_namespace or "").strip("/")
        if namespace:
            if not re.fullmatch(r"[a-z0-9-]+", namespace):
                raise UnsafeTargetError("invalid fixture namespace")
            version_root = (self.fixture_root / namespace / version).resolve()
        else:
            version_root = (self.fixture_root / version).resolve()
        if self.fixture_root not in version_root.parents:
            raise UnsafeTargetError("invalid fixture version")
        return version_root

    async def capture(self, target: PortalTarget, run_id: str) -> PortalSnapshot:
        validate_target(target, allow_local=True)
        version_root = self._version_root(target)
        host = (target.allowed_hosts[0] if target.allowed_hosts else "benefits.demo.local").lower()

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
            url = f"https://{host}/{step.path.lstrip('/')}"
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
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, follow_redirects=False
        ) as client:
            for step in target.journey_steps:
                page_url = page_url_for(target, step.path)
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
            region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        )
        self.identifier = identifier or os.getenv("AGENTCORE_BROWSER_ID")
        if not self.identifier:
            raise ValueError("AGENTCORE_BROWSER_ID is required when BROWSER_MODE=agentcore")

    async def capture(self, target: PortalTarget, run_id: str) -> PortalSnapshot:
        validate_target(target)
        await asyncio.to_thread(
            validate_public_url, target.start_url.replace("{version}", target.active_version)
        )
        return await asyncio.to_thread(self._capture_sync, target, run_id)

    def _capture_sync(self, target: PortalTarget, run_id: str) -> PortalSnapshot:
        import boto3
        from bedrock_agentcore.tools.browser_client import browser_session
        from playwright.sync_api import sync_playwright

        pages: list[PageSnapshot] = []
        screenshot_key: str | None = None
        axe_path = Path(__file__).resolve().parents[1] / "fixtures" / "axe.min.js"
        axe_source = axe_path.read_text(encoding="utf-8") if axe_path.is_file() else None
        if axe_source is None:
            _browser_journal(
                "browser_axe_missing",
                run_id=run_id,
                target_id=target.target_id,
                summary="axe.min.js missing; continuing with heuristic accessibility checks only",
            )

        session_id: str | None = None
        # Keep the AgentCore session alive after Playwright disconnects so recording
        # batches can flush before StopBrowserSession (nested withs, not combined).
        with browser_session(self.region, identifier=self.identifier) as client:
            session_id = getattr(client, "session_id", None)
            _browser_journal(
                "browser_session_created",
                run_id=run_id,
                target_id=target.target_id,
                browser_session_id=session_id,
                browser_id=self.identifier,
            )
            try:
                browser_info = client.get_browser(self.identifier)
                recording = browser_info.get("recording") or {}
                s3_location = recording.get("s3Location") or {}
                recording_enabled = bool(recording.get("enabled"))
                _browser_journal(
                    "browser_recording_config",
                    run_id=run_id,
                    target_id=target.target_id,
                    browser_session_id=session_id,
                    browser_id=self.identifier,
                    recording_enabled=recording_enabled,
                    recording_bucket=s3_location.get("bucket") or s3_location.get("Bucket"),
                    recording_prefix=(
                        s3_location.get("prefix")
                        or s3_location.get("keyPrefix")
                        or s3_location.get("Prefix")
                    ),
                    browser_status=browser_info.get("status"),
                )
                if not recording_enabled:
                    _browser_journal(
                        "browser_recording_disabled",
                        run_id=run_id,
                        target_id=target.target_id,
                        browser_session_id=session_id,
                        browser_id=self.identifier,
                        summary=(
                            "AGENTCORE_BROWSER_ID browser does not have session recording enabled; "
                            "AWS Session Replay will show Pages (0)"
                        ),
                    )
            except Exception as recording_error:
                _browser_journal(
                    "browser_recording_config_unavailable",
                    run_id=run_id,
                    target_id=target.target_id,
                    browser_session_id=session_id,
                    browser_id=self.identifier,
                    error_category=type(recording_error).__name__,
                    summary=str(recording_error),
                )

            with sync_playwright() as playwright:
                browser = None
                page = None
                context = None
                page_reused = False
                ws_url, headers = client.generate_ws_headers()
                browser = playwright.chromium.connect_over_cdp(ws_url, headers=headers)
                # AgentCore Session Replay only attaches to the default CDP context/page.
                if not browser.contexts:
                    raise RuntimeError(
                        "AgentCore Browser connected without a default context; cannot record navigation"
                    )
                context = browser.contexts[0]
                _browser_journal(
                    "browser_connected",
                    run_id=run_id,
                    target_id=target.target_id,
                    browser_session_id=session_id,
                    context_count=len(browser.contexts),
                    page_count_before=len(context.pages),
                    summary="CDP connect_over_cdp succeeded",
                )

                def guard_route(route) -> None:
                    request = route.request
                    if not agentcore_request_should_continue(
                        request.method,
                        request.url,
                        request.resource_type,
                        target.allowed_hosts,
                    ):
                        route.abort("blockedbyclient")
                        return
                    if request_is_allowed(request.method, request.url, target.allowed_hosts):
                        try:
                            if urlparse(request.url).scheme == "https":
                                validate_public_url(request.url.split("#")[0])
                        except (ValueError, OSError):
                            route.abort("blockedbyclient")
                            return
                    route.continue_()

                context.set_default_timeout(20000)
                context.set_default_navigation_timeout(45000)
                # Resolve the instrumented default page BEFORE installing routes so we
                # never fall back to context.new_page() (unrecorded tab → Pages 0).
                page = _wait_for_default_replay_page(
                    context,
                    run_id=run_id,
                    target_id=target.target_id,
                    session_id=session_id,
                )
                page_reused = True
                context.route("**/*", guard_route)
                kept_pages = {id(page)}
                url_before = ""
                try:
                    url_before = page.url
                except Exception:
                    url_before = ""

                def close_unexpected_popup(popup) -> None:
                    if id(popup) in kept_pages:
                        return
                    try:
                        popup.close()
                    except Exception:
                        return

                context.on("page", close_unexpected_popup)
                _browser_journal(
                    "browser_page_ready",
                    run_id=run_id,
                    target_id=target.target_id,
                    browser_session_id=session_id,
                    context_count=len(browser.contexts),
                    page_count_before=len(context.pages),
                    page_reused=True,
                    page_created=False,
                    page_url_before=url_before,
                    summary="Reusing AgentCore default instrumented page",
                )
                try:
                    for index, step in enumerate(target.journey_steps):
                        page_url = page_url_for(target, step.path)
                        validate_navigation_url(page_url, target.allowed_hosts)
                        try:
                            current_url = page.url
                        except Exception:
                            current_url = url_before
                        _browser_journal(
                            "browser_navigation_started",
                            run_id=run_id,
                            target_id=target.target_id,
                            browser_session_id=session_id,
                            step=step.label,
                            target_url=page_url,
                            page_url_before=current_url,
                            page_reused=page_reused,
                            context_count=len(browser.contexts),
                            page_count_before=len(context.pages),
                        )
                        try:
                            response = page.goto(page_url, wait_until="domcontentloaded")
                            try:
                                page.wait_for_load_state("load", timeout=15000)
                            except Exception as load_error:
                                _browser_journal(
                                    "browser_navigation_load_wait_timeout",
                                    run_id=run_id,
                                    target_id=target.target_id,
                                    browser_session_id=session_id,
                                    error_category=type(load_error).__name__,
                                    summary=str(load_error),
                                )
                        except Exception as navigation_error:
                            _browser_journal(
                                "browser_navigation_failed",
                                run_id=run_id,
                                target_id=target.target_id,
                                browser_session_id=session_id,
                                target_url=page_url,
                                error_category=type(navigation_error).__name__,
                                summary=str(navigation_error),
                            )
                            raise
                        final_url = page.url
                        title = page.title()
                        status = response.status if response is not None else None
                        _browser_journal(
                            "browser_navigation_completed",
                            run_id=run_id,
                            target_id=target.target_id,
                            browser_session_id=session_id,
                            status=status,
                            target_url=page_url,
                            page_url_before=current_url,
                            page_url_after=final_url,
                            final_url=final_url,
                            title=title,
                            page_reused=page_reused,
                            context_count=len(browser.contexts),
                            page_count_after=len(context.pages),
                        )
                        validate_navigation_url(final_url, target.allowed_hosts)
                        if response is not None and response.status >= 400:
                            raise RuntimeError(
                                f"page returned HTTP {response.status}: {page_url}"
                            )
                        axe_result: dict = {"violations": []}
                        if axe_source:
                            page.add_script_tag(content=axe_source)
                            axe_result = page.evaluate(
                                "async () => await window.axe.run(document, "
                                "{resultTypes: ['violations']})"
                            )
                        html = page.content()
                        if not html or len(html) < 32:
                            raise RuntimeError(
                                f"AgentCore page content was empty after navigation to {final_url}"
                            )
                        soup = BeautifulSoup(html, "html.parser")
                        statuses: dict[str, int] = {}
                        for anchor in soup.select("a[href]")[:20]:
                            href = anchor.get("href", "")
                            linked = urljoin(page.url, href)
                            if not href or href.startswith(("#", "mailto:", "tel:")):
                                statuses[href] = 200
                            elif not request_is_allowed("HEAD", linked, target.allowed_hosts):
                                statuses[href] = 403
                            else:
                                try:
                                    validate_public_url(linked.split("#")[0])
                                    linked_response = context.request.head(
                                        linked, max_redirects=0, timeout=1500
                                    )
                                    if linked_response.status in {405, 501}:
                                        linked_response = context.request.get(
                                            linked,
                                            headers={"Range": "bytes=0-0"},
                                            max_redirects=0,
                                            timeout=1500,
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
                            f"baselines/{target.target_id}/{run_id}/screenshots"
                            if run_id.startswith("baseline")
                            else f"screenshots/{target.target_id}/{run_id}"
                        )
                        page_screenshot_key = f"{screenshot_prefix}/{index + 1}-{slug}.png"
                        bucket = os.getenv("EVIDENCE_BUCKET") or os.getenv(
                            "CIVIC_CANARY_S3_BUCKET"
                        )
                        if not bucket:
                            raise RuntimeError(
                                "CIVIC_CANARY_S3_BUCKET is required for browser evidence"
                            )
                        screenshot_bytes = page.screenshot(full_page=True)
                        boto3.client("s3", region_name=self.region).put_object(
                            Bucket=bucket,
                            Key=page_screenshot_key,
                            Body=screenshot_bytes,
                            ContentType="image/png",
                        )
                        captured_page.screenshot_key = page_screenshot_key
                        screenshot_key = screenshot_key or page_screenshot_key
                        pages.append(captured_page)
                        _browser_journal(
                            "browser_evidence_captured",
                            run_id=run_id,
                            target_id=target.target_id,
                            browser_session_id=session_id,
                            step=step.label,
                            final_url=final_url,
                            title=title,
                            html_bytes=len(html),
                            screenshot_key=page_screenshot_key,
                            headings=len(captured_page.headings),
                            page_reused=page_reused,
                            page_count_after=len(context.pages),
                            summary="Evidence capture complete; replay page should be recorded",
                        )
                    if not pages:
                        raise RuntimeError(
                            "AgentCore capture finished with zero pages; navigation did not produce evidence"
                        )
                finally:
                    # Keep the instrumented page open while CDP is connected so replay
                    # can upload DOM/navigation batches (short sessions → Pages 0).
                    flush_seconds = float(os.getenv("AGENTCORE_REPLAY_FLUSH_SECONDS", "5"))
                    shutdown_started = time.monotonic()
                    _browser_journal(
                        "browser_session_closing",
                        run_id=run_id,
                        target_id=target.target_id,
                        browser_session_id=session_id,
                        pages_captured=len(pages),
                        page_reused=page_reused,
                        flush_seconds=flush_seconds,
                        context_count=len(browser.contexts) if browser is not None else 0,
                        page_count=len(context.pages) if context is not None else 0,
                        summary="Waiting for Session Replay flush before CDP disconnect",
                    )
                    if flush_seconds > 0:
                        time.sleep(flush_seconds)
                    _browser_journal(
                        "browser_replay_flush_waited",
                        run_id=run_id,
                        target_id=target.target_id,
                        browser_session_id=session_id,
                        flush_seconds=flush_seconds,
                        elapsed_ms=int((time.monotonic() - shutdown_started) * 1000),
                    )
                    # Do not page.close() the default instrumented tab before session stop.
                    try:
                        if browser is not None:
                            browser.close()
                    except Exception:
                        pass
                    _browser_journal(
                        "browser_cdp_disconnected",
                        run_id=run_id,
                        target_id=target.target_id,
                        browser_session_id=session_id,
                        pages_captured=len(pages),
                        page_reused=page_reused,
                        shutdown_ms=int((time.monotonic() - shutdown_started) * 1000),
                    )

            settle_seconds = float(os.getenv("AGENTCORE_REPLAY_SETTLE_SECONDS", "5"))
            settle_started = time.monotonic()
            _browser_journal(
                "browser_session_settle_before_stop",
                run_id=run_id,
                target_id=target.target_id,
                browser_session_id=session_id,
                settle_seconds=settle_seconds,
                pages_captured=len(pages),
                summary="AgentCore session still open; allowing final recording upload before StopBrowserSession",
            )
            if settle_seconds > 0:
                time.sleep(settle_seconds)
            _browser_journal(
                "browser_session_closed",
                run_id=run_id,
                target_id=target.target_id,
                browser_session_id=session_id,
                pages_captured=len(pages),
                settle_ms=int((time.monotonic() - settle_started) * 1000),
                summary="Exiting browser_session context; StopBrowserSession will finalize recording",
            )

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
            region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        )
        return AgentCoreBrowserAdapter(region=resolved_region, identifier=resolved_identifier)
    if selected_mode == "local":
        root = fixture_root or (Path(__file__).resolve().parents[2] / "web" / "public" / "portal")
        return FixtureBrowserAdapter(root)
    if selected_mode == "http":
        return HttpBrowserAdapter()
    raise ValueError(
        f"Unsupported BROWSER_MODE: {selected_mode!r}. Expected 'local' or 'agentcore'."
    )
