"""Tests for exchange registration and client creation."""

import pytest

from omnitrade.core.enums import ExchangeId
from omnitrade.core.config import Config, ExchangeConfig
from omnitrade.exchanges.registry import _registry, create_client, available_exchanges
from omnitrade.exchanges.polymarket.client import PolymarketClient
from omnitrade.exchanges.kalshi.client import KalshiClient
from omnitrade.exchanges.hyperliquid.client import HyperliquidClient


class TestExchangeRegistration:
    """Verify all three exchanges register on import."""

    def test_polymarket_registered(self):
        assert ExchangeId.POLYMARKET in _registry

    def test_kalshi_registered(self):
        assert ExchangeId.KALSHI in _registry

    def test_hyperliquid_registered(self):
        assert ExchangeId.HYPERLIQUID in _registry

    def test_available_exchanges(self):
        available = available_exchanges()
        assert ExchangeId.POLYMARKET in available
        assert ExchangeId.KALSHI in available
        assert ExchangeId.HYPERLIQUID in available

    def test_registry_maps_correct_classes(self):
        assert _registry[ExchangeId.POLYMARKET] is PolymarketClient
        assert _registry[ExchangeId.KALSHI] is KalshiClient
        assert _registry[ExchangeId.HYPERLIQUID] is HyperliquidClient


class TestCreateClient:
    """Verify create_client returns the right client type."""

    def _config(self) -> Config:
        return Config(
            polymarket=ExchangeConfig(exchange=ExchangeId.POLYMARKET, enabled=True),
            kalshi=ExchangeConfig(exchange=ExchangeId.KALSHI, enabled=True),
            hyperliquid=ExchangeConfig(exchange=ExchangeId.HYPERLIQUID, enabled=True),
        )

    def test_create_polymarket_client(self):
        client = create_client(ExchangeId.POLYMARKET, config=self._config())
        assert isinstance(client, PolymarketClient)

    def test_create_kalshi_client(self):
        client = create_client(ExchangeId.KALSHI, config=self._config())
        assert isinstance(client, KalshiClient)

    def test_create_hyperliquid_client(self):
        client = create_client(ExchangeId.HYPERLIQUID, config=self._config())
        assert isinstance(client, HyperliquidClient)


class TestPolymarketPaperBalance:
    """Verify Polymarket client returns usable balance for paper trading."""

    @pytest.mark.asyncio
    async def test_default_paper_balance(self):
        cfg = ExchangeConfig(exchange=ExchangeId.POLYMARKET, enabled=True)
        client = PolymarketClient(cfg)
        balance = await client.get_balance()
        assert balance.total_equity == 10_000.0
        assert balance.available_balance == 10_000.0
        assert balance.currency == "USDC"

    @pytest.mark.asyncio
    async def test_custom_paper_balance(self):
        cfg = ExchangeConfig(exchange=ExchangeId.POLYMARKET, enabled=True)
        client = PolymarketClient(cfg)
        client._paper_balance = 50_000.0
        balance = await client.get_balance()
        assert balance.total_equity == 50_000.0
        assert balance.available_balance == 50_000.0
