from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.civic_canary.browser import (
    AgentCoreBrowserAdapter,
    FixtureBrowserAdapter,
    create_browser_adapter,
)
from agent.civic_canary.strands_graph import build_review_graph
from services.runtime import ScanService, _aws_region
from services.storage import AwsStore, InMemoryStore
from services.store_factory import default_store


def test_default_browser_mode_is_local(monkeypatch) -> None:
    monkeypatch.delenv("BROWSER_MODE", raising=False)
    adapter = create_browser_adapter()
    assert isinstance(adapter, FixtureBrowserAdapter)


def test_explicit_local_browser_mode() -> None:
    adapter = create_browser_adapter("local")
    assert isinstance(adapter, FixtureBrowserAdapter)


def test_agentcore_mode_selects_agentcore_adapter(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_MODE", "agentcore")
    monkeypatch.setenv("AGENTCORE_BROWSER_ID", "mock-browser-12345")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    adapter = create_browser_adapter()
    assert isinstance(adapter, AgentCoreBrowserAdapter)
    assert adapter.identifier == "mock-browser-12345"
    assert adapter.region == "us-east-1"


def test_agentcore_adapter_direct_instantiation(monkeypatch) -> None:
    monkeypatch.setenv("AGENTCORE_BROWSER_ID", "mock-browser-direct")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    adapter = AgentCoreBrowserAdapter()
    assert adapter.identifier == "mock-browser-direct"
    assert adapter.region == "us-east-1"


def test_missing_agentcore_browser_id_raises_clear_error(monkeypatch) -> None:
    monkeypatch.delenv("AGENTCORE_BROWSER_ID", raising=False)
    with pytest.raises(
        ValueError, match="AGENTCORE_BROWSER_ID is required when BROWSER_MODE=agentcore"
    ):
        create_browser_adapter("agentcore")

    with pytest.raises(
        ValueError, match="AGENTCORE_BROWSER_ID is required when BROWSER_MODE=agentcore"
    ):
        AgentCoreBrowserAdapter()


def test_unsupported_browser_mode_raises_error() -> None:
    with pytest.raises(ValueError, match="Unsupported BROWSER_MODE"):
        create_browser_adapter("selenium")


def test_aws_region_resolution_precedence(monkeypatch) -> None:
    # 1. AWS_REGION takes precedence
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    assert _aws_region() == "eu-central-1"

    # 2. AWS_DEFAULT_REGION fallback when AWS_REGION is unset
    monkeypatch.delenv("AWS_REGION", raising=False)
    assert _aws_region() == "us-west-2"

    # 3. Default fallback to us-east-1
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert _aws_region() == "us-east-1"


def test_agentcore_adapter_respects_region_resolution(monkeypatch) -> None:
    monkeypatch.setenv("AGENTCORE_BROWSER_ID", "mock-browser-abc")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    adapter = create_browser_adapter("agentcore")
    assert adapter.region == "us-west-2"


def test_bedrock_model_id_and_region_from_env(monkeypatch) -> None:
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    with patch("agent.civic_canary.strands_graph.BedrockModel") as mock_bedrock_model:
        mock_instance = MagicMock()
        mock_bedrock_model.return_value = mock_instance
        build_review_graph()
        mock_bedrock_model.assert_called_once_with(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            region_name="us-east-1",
        )


def test_scan_service_selects_browser_mode_from_env(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_MODE", "agentcore")
    monkeypatch.setenv("AGENTCORE_BROWSER_ID", "scan-browser-id")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    service = ScanService(InMemoryStore())
    assert isinstance(service.browser, AgentCoreBrowserAdapter)
    assert service.browser.identifier == "scan-browser-id"


def test_agentcore_capture_uses_default_context_and_produces_page(monkeypatch) -> None:
    """Session replay requires contexts[0]; new_context() yields empty AgentCore recordings."""
    from agent.civic_canary.models import JourneyStep, PortalTarget

    monkeypatch.setenv("AGENTCORE_BROWSER_ID", "mock-browser")
    monkeypatch.setenv("CIVIC_CANARY_S3_BUCKET", "civic-canary-evidence")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))]
    )

    html = (
        "<html lang='en'><head><title>Civic Monitor Sample</title></head>"
        "<body><main><h1>Service updates</h1>"
        "<p>Hours of operation are posted here.</p></main></body></html>"
    )

    class FakeResponse:
        status = 200

    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self._title = ""
            self.goto_calls: list[tuple[str, str]] = []

        def goto(self, url, wait_until="load"):
            self.goto_calls.append((url, wait_until))
            self.url = url
            self._title = "Civic Monitor Sample"
            return FakeResponse()

        def wait_for_load_state(self, state, timeout=15000):
            return None

        def title(self):
            return self._title

        def add_script_tag(self, content=""):
            return None

        def evaluate(self, script):
            return {"violations": []}

        def content(self):
            return html

        def screenshot(self, full_page=False):
            return b"png-bytes"

        def close(self):
            return None

    class FakeRequest:
        def head(self, *args, **kwargs):
            return FakeResponse()

        def get(self, *args, **kwargs):
            return FakeResponse()

    class FakeContext:
        def __init__(self, page):
            self.pages = [page]
            self.request = FakeRequest()
            self.routes = []
            self.handlers = []
            self.new_context_called = False

        def set_default_timeout(self, value):
            return None

        def set_default_navigation_timeout(self, value):
            return None

        def route(self, pattern, handler):
            self.routes.append(pattern)

        def on(self, event, handler):
            self.handlers.append((event, handler))

        def new_page(self):
            raise AssertionError("default context already has a page; new_page should not be required")

    class FakeBrowser:
        def __init__(self, context):
            self.contexts = [context]
            self.closed = False

        def new_context(self, **kwargs):
            raise AssertionError(
                "AgentCore Session Replay requires browser.contexts[0], not new_context()"
            )

        def close(self):
            self.closed = True

    class FakeChromium:
        def __init__(self, browser):
            self._browser = browser

        def connect_over_cdp(self, ws_url, headers=None):
            assert ws_url.startswith("wss://")
            assert headers
            return self._browser

    class FakePlaywright:
        def __init__(self, browser):
            self.chromium = FakeChromium(browser)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeClient:
        session_id = "session-abc"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def generate_ws_headers(self):
            return "wss://example.agentcore/session", {"Authorization": "sigv4"}

    page = FakePage()
    context = FakeContext(page)
    browser = FakeBrowser(context)
    put_calls: list[dict] = []

    monkeypatch.setattr(
        "bedrock_agentcore.tools.browser_client.browser_session",
        lambda *a, **k: FakeClient(),
    )
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FakePlaywright(browser),
    )
    monkeypatch.setattr(
        "boto3.client",
        lambda *a, **k: MagicMock(put_object=lambda **kwargs: put_calls.append(kwargs)),
    )

    adapter = AgentCoreBrowserAdapter(identifier="mock-browser", region="us-east-1")
    target = PortalTarget(
        target_id="live-sample",
        kind="live",
        name="Sample public page",
        start_url="https://example.org/services",
        allowed_hosts=["example.org"],
        journey_steps=[JourneyStep(path="/", label="Home")],
        setup_status="PENDING",
    )
    result = adapter._capture_sync(target, "run-live-1")

    assert page.goto_calls, "page.goto must run against the default AgentCore page"
    assert page.goto_calls[0][0] == "https://example.org/services"
    assert page.goto_calls[0][1] == "domcontentloaded"
    assert len(result.pages) == 1
    assert result.pages[0].title == "Civic Monitor Sample"
    assert result.pages[0].headings
    assert result.pages[0].screenshot_key
    assert put_calls and put_calls[0]["Body"] == b"png-bytes"
    assert browser.closed


def test_agentcore_capture_rejects_empty_html(monkeypatch) -> None:
    from agent.civic_canary.models import JourneyStep, PortalTarget

    monkeypatch.setenv("AGENTCORE_BROWSER_ID", "mock-browser")
    monkeypatch.setenv("CIVIC_CANARY_S3_BUCKET", "civic-canary-evidence")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))]
    )

    class FakeResponse:
        status = 200

    class FakePage:
        url = "https://example.org/services"

        def goto(self, url, wait_until="load"):
            self.url = url
            return FakeResponse()

        def wait_for_load_state(self, *args, **kwargs):
            return None

        def title(self):
            return ""

        def add_script_tag(self, content=""):
            return None

        def evaluate(self, script):
            return {"violations": []}

        def content(self):
            return "<html></html>"

        def screenshot(self, full_page=False):
            return b"png"

        def close(self):
            return None

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage()]
            self.request = MagicMock()

        def set_default_timeout(self, value):
            return None

        def set_default_navigation_timeout(self, value):
            return None

        def route(self, *args, **kwargs):
            return None

        def on(self, *args, **kwargs):
            return None

    class FakeBrowser:
        def __init__(self):
            self.contexts = [FakeContext()]

        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        @property
        def chromium(self):
            return MagicMock(connect_over_cdp=lambda *a, **k: FakeBrowser())

    class FakeClient:
        session_id = "session-empty"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def generate_ws_headers(self):
            return "wss://example.agentcore/session", {"Authorization": "sigv4"}

    monkeypatch.setattr(
        "bedrock_agentcore.tools.browser_client.browser_session",
        lambda *a, **k: FakeClient(),
    )
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakePlaywright())
    adapter = AgentCoreBrowserAdapter(identifier="mock-browser", region="us-east-1")
    target = PortalTarget(
        target_id="live-empty",
        kind="live",
        start_url="https://example.org/services",
        allowed_hosts=["example.org"],
        journey_steps=[JourneyStep(path="/", label="Home")],
    )
    with pytest.raises(RuntimeError, match="empty"):
        adapter._capture_sync(target, "run-empty")


def test_store_factory_supports_civic_canary_table_names(monkeypatch) -> None:
    monkeypatch.setenv("CIVIC_CANARY_MODE", "aws")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("CIVIC_CANARY_SITES_TABLE", "CivicCanarySites")
    monkeypatch.setenv("CIVIC_CANARY_REVIEWS_TABLE", "CivicCanaryReviews")
    monkeypatch.setenv("CIVIC_CANARY_FINDINGS_TABLE", "CivicCanaryFindings")
    monkeypatch.setenv("CIVIC_CANARY_S3_BUCKET", "civic-canary")

    with patch("services.storage.boto3.resource"), patch(
        "services.storage.boto3.client"
    ):
        store = default_store()
        assert isinstance(store, AwsStore)
        assert store.bucket == "civic-canary"
