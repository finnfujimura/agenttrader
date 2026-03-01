"""Test that PmxtClient.get_markets uses status='all' when market_ids is provided."""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


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
def _fake_market_object(
    *,
    market_id,
    ticker=None,
    title="Resolved market",
    category="politics",
    volume="5000",
    status="closed",
):
    return SimpleNamespace(
        market_id=market_id,
        ticker=ticker,
        title=title,
        category=category,
        tags=[],
        volume=volume,
        resolution_date="2025-01-01T00:00:00Z",
        yes=SimpleNamespace(outcome_id=market_id, price="1", label="yes"),
        no=SimpleNamespace(outcome_id=f"{market_id}-no", price="0", label="no"),
        outcomes=[],
        active=status != "closed",
        closed=status == "closed",
    )


def test_market_ids_query_each_requested_id(_make_client):
    client, poly, kalshi = _make_client()
    poly.fetch_markets.return_value = [_fake_market_object(market_id="0xresolved")]

    results = client.get_markets(
        platform="polymarket",
        market_ids=["0xresolved"],
        limit=1,
    )

    poly.fetch_markets.assert_called_once_with(query="0xresolved", status="all", limit=20)
    assert len(results) == 1
    assert results[0].id == "0xresolved"


def test_market_ids_match_kalshi_ticker_alias(_make_client):
    client, poly, kalshi = _make_client()
    kalshi.fetch_markets.return_value = [
        _fake_market_object(market_id="internal-id", ticker="PRES-2024-DJT", title="Kalshi market")
    ]

    results = client.get_markets(
        platform="kalshi",
        market_ids=["PRES-2024-DJT"],
        limit=1,
    )

    kalshi.fetch_markets.assert_called_once_with(query="PRES-2024-DJT", status="all", limit=20)
    assert len(results) == 1
    assert results[0].id == "internal-id"


def test_market_ids_require_exact_alias_match(_make_client):
    client, poly, kalshi = _make_client()
    poly.fetch_markets.return_value = [_fake_market_object(market_id="0xother")]

    results = client.get_markets(
        platform="polymarket",
        market_ids=["0xresolved"],
        limit=1,
    )

    poly.fetch_markets.assert_called_once_with(query="0xresolved", status="all", limit=20)
    assert results == []


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
