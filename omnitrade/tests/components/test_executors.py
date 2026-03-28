"""Tests for execution helpers and PaperClient."""

import pytest
from omnitrade.core.enums import Side, OrderStatus, ExchangeId, InstrumentType, SignalDirection
from omnitrade.core.models import OrderbookSnapshot, OrderbookLevel, Instrument
from omnitrade.components.trading import direction_to_side, check_pre_trade_safety, execute_aggressive
from omnitrade.exchanges.base import PaperClient


@pytest.fixture
def orderbook():
    return OrderbookSnapshot(
        instrument_id="test-token",
        bids=[OrderbookLevel(price=0.50, size=100)],
        asks=[OrderbookLevel(price=0.52, size=100)],
    )


class TestPaperClient:
    async def test_buy(self, mock_client, orderbook):
        paper = PaperClient(mock_client)
        from omnitrade.core.models import OrderRequest
        result = await paper.place_order(OrderRequest(
            instrument_id="test-token", side=Side.BUY, size=96.15, price=0.52,
        ))
        assert result.success
        assert result.order_id.startswith("PAPER-")
        assert result.filled_size > 0

    async def test_sell(self, mock_client, orderbook):
        paper = PaperClient(mock_client)
        from omnitrade.core.models import OrderRequest
        result = await paper.place_order(OrderRequest(
            instrument_id="test-token", side=Side.SELL, size=100.0, price=0.50,
        ))
        assert result.success
        assert result.filled_price > 0

    async def test_buy_uses_ask_with_slippage(self, mock_client, orderbook):
        paper = PaperClient(mock_client, slippage_pct=0.01)
        from omnitrade.core.models import OrderRequest
        result = await paper.place_order(OrderRequest(
            instrument_id="test-token", side=Side.BUY, size=100.0, price=0.52,
        ))
        # Should execute at ask (0.52) * (1 + 0.01) = 0.5252
        assert result.success
        assert abs(result.filled_price - 0.5252) < 0.001

    async def test_sell_uses_bid_with_slippage(self, mock_client, orderbook):
        paper = PaperClient(mock_client, slippage_pct=0.01)
        from omnitrade.core.models import OrderRequest
        result = await paper.place_order(OrderRequest(
            instrument_id="test-token", side=Side.SELL, size=100.0, price=0.50,
        ))
        # Should execute at bid (0.50) * (1 - 0.01) = 0.495
        assert result.success
        assert abs(result.filled_price - 0.495) < 0.001

    async def test_sequential_order_ids(self, mock_client, orderbook):
        paper = PaperClient(mock_client)
        from omnitrade.core.models import OrderRequest
        r1 = await paper.place_order(OrderRequest(
            instrument_id="test", side=Side.BUY, size=10.0, price=0.50,
        ))
        r2 = await paper.place_order(OrderRequest(
            instrument_id="test", side=Side.BUY, size=10.0, price=0.50,
        ))
        assert r1.order_id != r2.order_id

    async def test_zero_price_orderbook(self, mock_client):
        mock_client._orderbook = OrderbookSnapshot(
            instrument_id="test",
            bids=[],
            asks=[],
        )
        paper = PaperClient(mock_client)
        from omnitrade.core.models import OrderRequest
        result = await paper.place_order(OrderRequest(
            instrument_id="test", side=Side.BUY, size=50.0, price=0.0,
        ))
        assert not result.success

    async def test_cancel_is_noop(self, mock_client):
        paper = PaperClient(mock_client)
        assert await paper.cancel_order("any-id") is True

    async def test_delegates_market_data(self, mock_client):
        paper = PaperClient(mock_client)
        instruments = await paper.get_instruments()
        assert len(instruments) == len(mock_client._instruments)
        balance = await paper.get_balance()
        assert balance.total_equity == mock_client._balance.total_equity

    async def test_delegates_connect(self, mock_client):
        paper = PaperClient(mock_client)
        assert not paper.is_connected
        await paper.connect()
        assert paper.is_connected


class TestPreTradeChecks:
    def test_spread_too_wide(self):
        wide_book = OrderbookSnapshot(
            instrument_id="test",
            bids=[OrderbookLevel(price=0.30, size=100)],
            asks=[OrderbookLevel(price=0.70, size=100)],
        )
        rejection = check_pre_trade_safety(wide_book, Side.BUY, 0.50, max_spread=0.03)
        assert rejection is not None
        assert rejection.is_rejection
        assert "Spread" in rejection.error_message

    def test_spread_check_only_on_buy(self):
        """Spread check should not block sells (exits should always be allowed)."""
        wide_book = OrderbookSnapshot(
            instrument_id="test",
            bids=[OrderbookLevel(price=0.30, size=100)],
            asks=[OrderbookLevel(price=0.70, size=100)],
        )
        rejection = check_pre_trade_safety(wide_book, Side.SELL, 0.30, max_spread=0.03, max_slippage=1.0)
        assert rejection is None

    def test_slippage_rejected(self):
        book = OrderbookSnapshot(
            instrument_id="test",
            bids=[OrderbookLevel(price=0.50, size=100)],
            asks=[OrderbookLevel(price=0.52, size=100)],
        )
        rejection = check_pre_trade_safety(book, Side.BUY, 0.40, max_slippage=0.001)
        assert rejection is not None
        assert rejection.is_rejection

    def test_passes_when_safe(self):
        book = OrderbookSnapshot(
            instrument_id="test",
            bids=[OrderbookLevel(price=0.50, size=100)],
            asks=[OrderbookLevel(price=0.52, size=100)],
        )
        rejection = check_pre_trade_safety(book, Side.BUY, 0.52, max_spread=0.10, max_slippage=0.10)
        assert rejection is None

    def test_no_ask_available(self):
        empty_ask_book = OrderbookSnapshot(
            instrument_id="test",
            bids=[OrderbookLevel(price=0.50, size=100)],
            asks=[],
        )
        rejection = check_pre_trade_safety(empty_ask_book, Side.BUY, 0.50)
        assert rejection is not None
        assert "No ask" in rejection.error_message

    def test_no_bid_available(self):
        empty_bid_book = OrderbookSnapshot(
            instrument_id="test",
            bids=[],
            asks=[OrderbookLevel(price=0.50, size=100)],
        )
        rejection = check_pre_trade_safety(empty_bid_book, Side.SELL, 0.50)
        assert rejection is not None
        assert "No bid" in rejection.error_message


class TestExecuteAggressive:
    async def test_successful_buy(self, mock_client):
        result = await execute_aggressive(
            mock_client, "test", Side.BUY, 50.0, 0.52,
            max_spread=0.10, max_slippage=0.10,
        )
        assert result.success

    async def test_rejects_wide_spread(self, mock_client):
        mock_client._orderbook = OrderbookSnapshot(
            instrument_id="test",
            bids=[OrderbookLevel(price=0.30, size=100)],
            asks=[OrderbookLevel(price=0.70, size=100)],
        )
        result = await execute_aggressive(
            mock_client, "test", Side.BUY, 50.0, 0.50,
            max_spread=0.03,
        )
        assert not result.success
        assert result.is_rejection


class TestDirectionToSide:
    def test_long(self):
        assert direction_to_side(SignalDirection.LONG) == Side.BUY

    def test_short(self):
        assert direction_to_side(SignalDirection.SHORT) == Side.SELL

    def test_neutral_raises(self):
        with pytest.raises(ValueError):
            direction_to_side(SignalDirection.NEUTRAL)
