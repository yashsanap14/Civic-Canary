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
