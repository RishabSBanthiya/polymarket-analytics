"""Tests for the backtest engine."""

import asyncio

import pytest

from omnitrade.backtest.engine import (
    BacktestExchangeClient,
    BacktestRunner,
    BacktestResult,
)
from omnitrade.core.enums import ExchangeId, InstrumentType
from omnitrade.core.models import Instrument, OrderbookSnapshot, OrderbookLevel
from omnitrade.components.signals import MidpointDeviationSignal, OrderbookMicrostructureSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_instrument(iid: str = "BT-TEST") -> Instrument:
    return Instrument(
        instrument_id=iid,
        exchange=ExchangeId.POLYMARKET,
        instrument_type=InstrumentType.BINARY_OUTCOME,
        name="Test Instrument",
        market_id="bt-test",
        outcome="YES",
        active=True,
    )


def _make_snapshots(
    num_steps: int = 50,
    base_mid: float = 0.50,
    spread: float = 0.02,
    drift: float = 0.002,
    instrument_id: str = "BT-SYN-001",
) -> list[OrderbookSnapshot]:
    """Create controlled orderbook snapshots with predictable drift."""
    snapshots = []
    mid = base_mid
    for i in range(num_steps):
        mid = max(0.05, min(0.95, mid + drift * (1 if i % 3 != 2 else -0.5)))
        bid = mid - spread / 2
        ask = mid + spread / 2
        snapshots.append(OrderbookSnapshot(
            instrument_id=instrument_id,
            bids=[
                OrderbookLevel(price=round(bid, 4), size=100.0),
                OrderbookLevel(price=round(bid - 0.01, 4), size=80.0),
                OrderbookLevel(price=round(bid - 0.02, 4), size=60.0),
            ],
            asks=[
                OrderbookLevel(price=round(ask, 4), size=100.0),
                OrderbookLevel(price=round(ask + 0.01, 4), size=80.0),
                OrderbookLevel(price=round(ask + 0.02, 4), size=60.0),
            ],
        ))
    return snapshots


# ---------------------------------------------------------------------------
# BacktestExchangeClient
# ---------------------------------------------------------------------------

class TestBacktestExchangeClient:
    def _make_client(self, num_steps: int = 10, balance: float = 5000.0):
        snapshots = _make_snapshots(num_steps)
        instrument = _make_instrument()
        return BacktestExchangeClient(snapshots, instrument, initial_balance=balance)

    def test_initial_balance(self):
        client = self._make_client()
        balance = asyncio.run(client.get_balance())
        assert balance.total_equity == 5000
        assert balance.available_balance == 5000

    def test_advance_changes_orderbook(self):
        client = self._make_client(num_steps=10)

        async def _test():
            snap0 = await client.get_orderbook("BT-TEST")
            client.advance()
            snap1 = await client.get_orderbook("BT-TEST")
            # Snapshots should differ due to drift
            assert snap0.best_bid != snap1.best_bid or snap0.best_ask != snap1.best_ask

        asyncio.run(_test())

    def test_get_instruments(self):
        client = self._make_client()

        async def _test():
            instruments = await client.get_instruments()
            assert len(instruments) == 1
            assert instruments[0].instrument_id == "BT-TEST"

        asyncio.run(_test())

    def test_connect_disconnect(self):
        client = self._make_client()

        async def _test():
            assert not client.is_connected
            await client.connect()
            assert client.is_connected
            await client.close()
            assert not client.is_connected

        asyncio.run(_test())

    def test_advance_clamps_at_end(self):
        client = self._make_client(num_steps=3)
        client.advance()
        client.advance()
        client.advance()  # Beyond end
        client.advance()  # Still beyond
        assert client._step == 2  # Clamped to last index


# ---------------------------------------------------------------------------
# BacktestRunner (integration)
# ---------------------------------------------------------------------------

class TestBacktestRunner:
    def test_produces_result_midpoint(self):
        signal = MidpointDeviationSignal(fair_value=0.5, min_deviation=0.03)
        snapshots = _make_snapshots(num_steps=30)
        runner = BacktestRunner(
            signal_source=signal,
            snapshots=snapshots,
            instrument_id="BT-TEST",
            scenario_name="test_run",
            initial_balance=10_000,
        )
        result = asyncio.run(runner.run())
        assert isinstance(result, BacktestResult)
        assert result.signal_name == "midpoint_deviation"
        assert result.scenario_name == "test_run"
        assert len(result.equity_curve) == 30  # num_steps entries
        assert result.final_equity > 0

    def test_produces_result_orderbook(self):
        signal = OrderbookMicrostructureSignal()
        snapshots = _make_snapshots(num_steps=30, drift=0.003)
        runner = BacktestRunner(
            signal_source=signal,
            snapshots=snapshots,
            instrument_id="BT-TEST",
            scenario_name="trending_up",
            initial_balance=10_000,
        )
        result = asyncio.run(runner.run())
        assert isinstance(result, BacktestResult)
        assert result.signal_name == "orderbook_microstructure"
        assert result.final_equity > 0

    def test_metrics_are_consistent(self):
        signal = MidpointDeviationSignal(fair_value=0.5, min_deviation=0.03)
        snapshots = _make_snapshots(num_steps=50, drift=0.003)
        runner = BacktestRunner(
            signal_source=signal,
            snapshots=snapshots,
            instrument_id="BT-TEST",
            scenario_name="trending_up",
            initial_balance=10_000,
        )
        result = asyncio.run(runner.run())
        assert result.winning_trades + result.losing_trades <= result.total_trades
        assert result.max_drawdown_pct >= 0.0
        if result.total_trades > 0:
            assert 0.0 <= result.win_rate <= 1.0
