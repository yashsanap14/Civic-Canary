from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from agent.civic_canary.browser import (
    UnsafeTargetError,
    discover_https_redirect_hosts,
    incorporate_final_navigation_host,
    validate_navigation_url,
)


class _FakeResponse:
    def __init__(self, *, status_code: int, location: str | None = None):
        self.status_code = status_code
        self.headers = {"location": location} if location else {}

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400 and "location" in self.headers


def test_discover_https_redirect_hosts_accepts_public_final_host(monkeypatch) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )
    responses = [
        _FakeResponse(status_code=301, location="https://final.example.org/home"),
        _FakeResponse(status_code=200),
    ]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return responses.pop(0)

    monkeypatch.setattr("agent.civic_canary.browser.httpx.Client", FakeClient)
    hosts, chain = discover_https_redirect_hosts(
        "https://start.example.org/",
        ["start.example.org"],
        run_id="run-redirect-1",
        target_id="site-1",
    )
    assert hosts == ["start.example.org", "final.example.org"]
    assert chain == [
        "https://start.example.org/",
        "https://final.example.org/home",
    ]
    validate_navigation_url("https://final.example.org/home", hosts)


def test_discover_https_redirect_hosts_rejects_private_final_host(monkeypatch) -> None:
    def fake_getaddrinfo(host, *args, **kwargs):
        if host == "start.example.org":
            return [(0, 0, 0, "", ("93.184.216.34", 443))]
        return [(0, 0, 0, "", ("10.0.0.8", 443))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    responses = [
        _FakeResponse(status_code=302, location="https://internal.example.org/"),
    ]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return responses.pop(0)

    monkeypatch.setattr("agent.civic_canary.browser.httpx.Client", FakeClient)
    with pytest.raises(UnsafeTargetError, match="safe public HTTPS"):
        discover_https_redirect_hosts(
            "https://start.example.org/",
            ["start.example.org"],
            run_id="run-redirect-2",
            target_id="site-2",
        )


def test_incorporate_final_navigation_host_only_during_onboarding(monkeypatch) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )
    expanded = incorporate_final_navigation_host(
        submitted_url="https://www.vaemergency.gov/",
        final_url="https://www.vdem.virginia.gov/",
        allowed_hosts=["www.vaemergency.gov"],
        onboarding=True,
        run_id="run-3",
        target_id="site-3",
    )
    assert "www.vdem.virginia.gov" in expanded
    assert "www.vaemergency.gov" in expanded

    with pytest.raises(UnsafeTargetError, match="allow-list"):
        incorporate_final_navigation_host(
            submitted_url="https://www.vaemergency.gov/",
            final_url="https://www.vdem.virginia.gov/",
            allowed_hosts=["www.vaemergency.gov"],
            onboarding=False,
            run_id="run-4",
            target_id="site-4",
        )


@pytest.mark.asyncio
async def test_http_capture_expands_allow_list_on_setup_redirect(monkeypatch) -> None:
    from agent.civic_canary.browser import HttpBrowserAdapter
    from agent.civic_canary.models import JourneyStep, PortalTarget

    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )

    class FakeAsyncResponse:
        def __init__(self, status_code, text="", location=None):
            self.status_code = status_code
            self.text = text
            self.headers = {"location": location} if location else {}

        @property
        def is_redirect(self):
            return 300 <= self.status_code < 400 and "location" in self.headers

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())

    sequence = [
        FakeAsyncResponse(301, location="https://final.example.org/"),
        FakeAsyncResponse(
            200,
            text="<html><body><main><h1>Emergency services</h1><p>Guidance</p></main></body></html>",
        ),
    ]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            return sequence.pop(0)

        async def head(self, url):
            return FakeAsyncResponse(200)

    monkeypatch.setattr("agent.civic_canary.browser.httpx.AsyncClient", FakeAsyncClient)
    target = PortalTarget(
        target_id="site-redirect",
        kind="live",
        name="VA Emergency",
        start_url="https://start.example.org/",
        allowed_hosts=["start.example.org"],
        journey_steps=[JourneyStep(path="/", label="Home")],
        setup_status="PENDING",
    )
    # Bypass DNS-heavy validate_target for the start host by stubbing validate_target.
    monkeypatch.setattr("agent.civic_canary.browser.validate_target", lambda *a, **k: None)
    snapshot = await HttpBrowserAdapter().capture(target, "run-http-redirect")
    assert snapshot.pages
    assert "final.example.org" in target.allowed_hosts
    assert snapshot.pages[0].url.startswith("https://final.example.org")
