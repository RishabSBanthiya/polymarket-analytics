"""Tests for the cross-exchange backtest engine."""

import asyncio

import pytest

from omnitrade.backtest.cross_engine import (
    MultiInstrumentBacktestClient,
    CrossExchangeBacktestRunner,
    CrossExchangeBacktestResult,
)
from omnitrade.core.enums import ExchangeId, InstrumentType
from omnitrade.core.models import (
    Instrument, OrderbookSnapshot, OrderbookLevel,
)
from omnitrade.components.signals import (
    BinaryPerpHedgeSignal,
    CrossExchangeArbSignal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hedge_data(
    num_steps: int = 30,
) -> dict[ExchangeId, tuple[list[Instrument], list[OrderbookSnapshot]]]:
    """Create controlled hedge data: binary + perp with correlated movement."""
    binary_inst = Instrument(
        instrument_id="BIN-BTC-YES",
        exchange=ExchangeId.POLYMARKET,
        instrument_type=InstrumentType.BINARY_OUTCOME,
        name="Bitcoin reach 100k - YES",
        market_id="btc-100k",
        outcome="YES",
        active=True,
        price=0.50,
    )
    perp_inst = Instrument(
        instrument_id="BTC",
        exchange=ExchangeId.HYPERLIQUID,
        instrument_type=InstrumentType.PERPETUAL,
        name="BTC",
        market_id="BTC-PERP",
        active=True,
        price=50_000.0,
        max_leverage=10.0,
    )

    binary_snaps = []
    perp_snaps = []
    binary_mid = 0.50
    perp_price = 50_000.0

    for step in range(num_steps):
        # Correlated drift
        binary_mid = max(0.05, min(0.95, binary_mid + 0.002))
        perp_price += 200

        binary_snaps.append(OrderbookSnapshot(
            instrument_id="BIN-BTC-YES",
            bids=[OrderbookLevel(price=round(max(0.01, binary_mid - 0.01), 4), size=100.0)],
            asks=[OrderbookLevel(price=round(min(0.99, binary_mid + 0.01), 4), size=100.0)],
        ))
        perp_snaps.append(OrderbookSnapshot(
            instrument_id="BTC",
            bids=[OrderbookLevel(price=round(perp_price - 10, 2), size=0.5)],
            asks=[OrderbookLevel(price=round(perp_price + 10, 2), size=0.5)],
        ))

    return {
        ExchangeId.POLYMARKET: ([binary_inst], binary_snaps),
        ExchangeId.HYPERLIQUID: ([perp_inst], perp_snaps),
    }


def _make_arb_data(
    num_steps: int = 30,
) -> dict[ExchangeId, tuple[list[Instrument], list[OrderbookSnapshot]]]:
    """Create controlled arb data: same event on two exchanges with price edge."""
    poly_inst = Instrument(
        instrument_id="POLY-ELECT-YES",
        exchange=ExchangeId.POLYMARKET,
        instrument_type=InstrumentType.BINARY_OUTCOME,
        name="Election Winner - YES",
        market_id="election-winner",
        outcome="YES",
        active=True,
        price=0.50,
    )
    kalshi_inst = Instrument(
        instrument_id="KALSHI-ELECT-YES",
        exchange=ExchangeId.KALSHI,
        instrument_type=InstrumentType.EVENT_CONTRACT,
        name="Election Winner - YES",
        market_id="election-winner",
        outcome="YES",
        active=True,
        price=0.52,
    )

    poly_snaps = []
    kalshi_snaps = []
    poly_mid = 0.50
    kalshi_mid = 0.52

    for step in range(num_steps):
        poly_mid = max(0.05, min(0.95, poly_mid + 0.001))
        kalshi_mid = max(0.05, min(0.95, kalshi_mid + 0.001))

        poly_snaps.append(OrderbookSnapshot(
            instrument_id="POLY-ELECT-YES",
            bids=[OrderbookLevel(price=round(max(0.01, poly_mid - 0.01), 4), size=100.0)],
            asks=[OrderbookLevel(price=round(min(0.99, poly_mid + 0.01), 4), size=100.0)],
        ))
        kalshi_snaps.append(OrderbookSnapshot(
            instrument_id="KALSHI-ELECT-YES",
            bids=[OrderbookLevel(price=round(max(0.01, kalshi_mid - 0.01), 4), size=100.0)],
            asks=[OrderbookLevel(price=round(min(0.99, kalshi_mid + 0.01), 4), size=100.0)],
        ))

    return {
        ExchangeId.POLYMARKET: ([poly_inst], poly_snaps),
        ExchangeId.KALSHI: ([kalshi_inst], kalshi_snaps),
    }


# ---------------------------------------------------------------------------
# MultiInstrumentBacktestClient
# ---------------------------------------------------------------------------

class TestMultiInstrumentBacktestClient:

    def _make_client(self):
        inst_a = Instrument(
            instrument_id="A",
            exchange=ExchangeId.POLYMARKET,
            instrument_type=InstrumentType.BINARY_OUTCOME,
            name="Test A",
            active=True,
            price=0.50,
        )
        inst_b = Instrument(
            instrument_id="B",
            exchange=ExchangeId.POLYMARKET,
            instrument_type=InstrumentType.BINARY_OUTCOME,
            name="Test B",
            active=True,
            price=0.60,
        )
        snaps_a = [
            OrderbookSnapshot(
                instrument_id="A",
                bids=[OrderbookLevel(price=0.49, size=100)],
                asks=[OrderbookLevel(price=0.51, size=100)],
            ),
            OrderbookSnapshot(
                instrument_id="A",
                bids=[OrderbookLevel(price=0.55, size=100)],
                asks=[OrderbookLevel(price=0.57, size=100)],
            ),
        ]
        snaps_b = [
            OrderbookSnapshot(
                instrument_id="B",
                bids=[OrderbookLevel(price=0.59, size=100)],
                asks=[OrderbookLevel(price=0.61, size=100)],
            ),
            OrderbookSnapshot(
                instrument_id="B",
                bids=[OrderbookLevel(price=0.65, size=100)],
                asks=[OrderbookLevel(price=0.67, size=100)],
            ),
        ]
        return MultiInstrumentBacktestClient(
            instruments=[inst_a, inst_b],
            snapshots_by_id={"A": snaps_a, "B": snaps_b},
            exchange_id=ExchangeId.POLYMARKET,
        )

    def test_get_instruments_returns_all(self):
        client = self._make_client()

        async def _test():
            await client.connect()
            instruments = await client.get_instruments()
            assert len(instruments) == 2
            ids = {i.instrument_id for i in instruments}
            assert ids == {"A", "B"}

        asyncio.run(_test())

    def test_get_orderbook_per_instrument(self):
        client = self._make_client()

        async def _test():
            await client.connect()
            book_a = await client.get_orderbook("A")
            book_b = await client.get_orderbook("B")

            assert book_a.instrument_id == "A"
            assert book_b.instrument_id == "B"
            assert book_a.midpoint != book_b.midpoint

        asyncio.run(_test())

    def test_advance_updates_all(self):
        client = self._make_client()

        async def _test():
            await client.connect()
            book_a_0 = await client.get_orderbook("A")
            book_b_0 = await client.get_orderbook("B")

            client.advance()

            book_a_1 = await client.get_orderbook("A")
            book_b_1 = await client.get_orderbook("B")

            # Both should have advanced
            assert book_a_1.midpoint != book_a_0.midpoint
            assert book_b_1.midpoint != book_b_0.midpoint

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# CrossExchangeBacktestRunner
# ---------------------------------------------------------------------------

class TestCrossExchangeBacktestRunner:

    def test_hedge_produces_valid_result(self):
        """Hedge strategy should produce a valid result."""
        exchange_data = _make_hedge_data(num_steps=30)
        signal = BinaryPerpHedgeSignal()
        runner = CrossExchangeBacktestRunner(
            signal_source=signal,
            exchange_data=exchange_data,
            initial_balance=10_000,
            strategy_type="hedge",
            scenario_name="hedge_test",
        )
        result = asyncio.run(runner.run())
        assert isinstance(result, CrossExchangeBacktestResult)
        assert result.signal_name == "binary_perp_hedge"
        assert result.scenario_name == "hedge_test"
        assert result.final_equity > 0

    def test_arb_produces_valid_result(self):
        """Arb strategy should produce a valid result."""
        exchange_data = _make_arb_data(num_steps=30)
        signal = CrossExchangeArbSignal(min_edge_bps=10.0)
        runner = CrossExchangeBacktestRunner(
            signal_source=signal,
            exchange_data=exchange_data,
            initial_balance=10_000,
            strategy_type="arb",
            scenario_name="arb_test",
        )
        result = asyncio.run(runner.run())
        assert isinstance(result, CrossExchangeBacktestResult)
        assert result.signal_name == "cross_exchange_arb"
        assert result.final_equity > 0

    def test_equity_curve_length(self):
        """Equity curve should have num_steps entries."""
        exchange_data = _make_hedge_data(num_steps=25)
        signal = BinaryPerpHedgeSignal()
        runner = CrossExchangeBacktestRunner(
            signal_source=signal,
            exchange_data=exchange_data,
            initial_balance=10_000,
            strategy_type="hedge",
            scenario_name="curve_test",
        )
        result = asyncio.run(runner.run())
        assert len(result.equity_curve) == 25  # num_steps

    def test_per_leg_pnl_tracked(self):
        """per_leg_pnl should have entries for each exchange."""
        exchange_data = _make_hedge_data(num_steps=30)
        signal = BinaryPerpHedgeSignal()
        runner = CrossExchangeBacktestRunner(
            signal_source=signal,
            exchange_data=exchange_data,
            initial_balance=10_000,
            strategy_type="hedge",
            scenario_name="leg_test",
        )
        result = asyncio.run(runner.run())
        assert isinstance(result.per_leg_pnl, dict)
        # Should have entries for the exchanges involved
        assert "polymarket" in result.per_leg_pnl
        assert "hyperliquid" in result.per_leg_pnl
