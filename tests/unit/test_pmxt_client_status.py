"""Test that PmxtClient.get_markets uses status='all' when market_ids is provided."""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

models = importlib.import_module("agenttrader.data.models")


@pytest.fixture
def _make_client(monkeypatch):
    """Create a PmxtClient with mocked pmxt backends, returning (client, poly_mock, kalshi_mock)."""
    pmxt_client_mod = importlib.import_module("agenttrader.data.pmxt_client")

    def factory():
        client = object.__new__(pmxt_client_mod.PmxtClient)
        poly = MagicMock()
        kalshi = MagicMock()
        kalshi.fetch_markets.return_value = []
        client._poly = poly
        client._kalshi = kalshi
        return client, poly, kalshi

    return factory


def _fake_market_item(market_id, status="closed"):
    """Minimal dict that _to_market can convert."""
    return {
        "id": market_id,
        "conditionId": market_id,
        "question": "Resolved?",
        "category": "politics",
        "tags": [],
        "outcomes": ["Yes", "No"],
        "volume": "5000",
        "endDate": "2025-01-01T00:00:00Z",
        "active": status != "closed",
        "closed": status == "closed",
        "resolvedBy": "yes" if status == "closed" else None,
    }


def test_market_ids_uses_status_all(_make_client):
    client, poly, kalshi = _make_client()

    resolved_market = SimpleNamespace(
        id="0xresolved",
        condition_id="0xresolved",
        platform=models.Platform.POLYMARKET,
        title="Resolved market",
        category="politics",
        tags=[],
        market_type=models.MarketType.BINARY,
        volume=5000.0,
        close_time=0,
        resolved=True,
        resolution="yes",
        scalar_low=None,
        scalar_high=None,
    )

    # When status="all", the API returns the resolved market.
    # When status="active", it wouldn't.
    def fake_fetch(status="active", limit=100):
        if status == "all":
            return [_fake_market_item("0xresolved", status="closed")]
        return []  # active filter excludes resolved markets

    poly.fetch_markets.side_effect = fake_fetch

    # Patch _to_market to return our resolved market directly
    original_to_market = type(client)._to_market
    def patched_to_market(self, item, platform, status_hint="active"):
        return resolved_market
    type(client)._to_market = patched_to_market

    try:
        results = client.get_markets(
            platform="polymarket",
            market_ids=["0xresolved"],
        )

        # Verify status="all" was passed to the API
        poly.fetch_markets.assert_called_once_with(status="all", limit=100)
        assert len(results) == 1
        assert results[0].id == "0xresolved"
    finally:
        type(client)._to_market = original_to_market


def test_no_market_ids_active_uses_status_active(_make_client):
    client, poly, kalshi = _make_client()
    poly.fetch_markets.return_value = []

    client.get_markets(platform="polymarket")

    poly.fetch_markets.assert_called_once_with(status="active", limit=100)


def test_no_market_ids_resolved_uses_status_closed(_make_client):
    client, poly, kalshi = _make_client()
    poly.fetch_markets.return_value = []

    client.get_markets(platform="polymarket", resolved=True)

    poly.fetch_markets.assert_called_once_with(status="closed", limit=100)
