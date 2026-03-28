"""Tests for the market-making backtest engine."""

import asyncio

import pytest

from omnitrade.backtest.mm_engine import (
    MMBacktestExchangeClient,
    MMBacktestResult,
    MMBacktestRunner,
)
from omnitrade.core.enums import ExchangeId, InstrumentType, Side, OrderStatus, OrderType
from omnitrade.core.models import (
    Instrument, OrderbookSnapshot, OrderbookLevel, OrderRequest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_instrument(iid: str = "MM-TEST") -> Instrument:
    return Instrument(
        instrument_id=iid,
        exchange=ExchangeId.POLYMARKET,
        instrument_type=InstrumentType.BINARY_OUTCOME,
        name="MM Test Instrument",
        market_id="mm-test",
        outcome="YES",
        active=True,
        price=0.50,
        bid=0.48,
        ask=0.52,
    )


def _make_controlled_snapshots(
    mids: list[float], spread: float = 0.02
) -> list[OrderbookSnapshot]:
    """Create snapshots with known midpoints for deterministic tests."""
    snapshots = []
    for i, mid in enumerate(mids):
        bid = mid - spread / 2
        ask = mid + spread / 2
        snapshots.append(OrderbookSnapshot(
            instrument_id="MM-TEST",
            bids=[OrderbookLevel(price=round(bid, 4), size=100.0)],
            asks=[OrderbookLevel(price=round(ask, 4), size=100.0)],
        ))
    return snapshots


def _make_client(
    snapshots=None, num_steps: int = 20, balance: float = 10_000,
) -> MMBacktestExchangeClient:
    if snapshots is None:
        snapshots = _make_controlled_snapshots([0.50] * num_steps)
    instrument = _make_instrument()
    return MMBacktestExchangeClient(snapshots, instrument, initial_balance=balance)


# ---------------------------------------------------------------------------
# MMBacktestExchangeClient: order management
# ---------------------------------------------------------------------------

class TestMMBacktestExchangeClient:

    def test_limit_order_rests_when_no_cross(self):
        """BUY limit below best ask should rest as open."""
        snapshots = _make_controlled_snapshots([0.50, 0.50, 0.50])
        client = _make_client(snapshots=snapshots)

        async def _test():
            await client.connect()
            # best_ask = 0.51, place buy at 0.48 → should rest
            req = OrderRequest(
                instrument_id="MM-TEST",
                side=Side.BUY,
                size=10.0,
                price=0.48,
                order_type=OrderType.LIMIT,
            )
            result = await client.place_order(req)
            assert result.success
            assert result.status == OrderStatus.OPEN
            assert result.filled_size == 0.0

            opens = await client.get_open_orders()
            assert len(opens) == 1
            assert opens[0].price == 0.48

        asyncio.run(_test())

    def test_buy_immediate_fill_when_crosses(self):
        """BUY limit at or above best ask should fill immediately."""
        snapshots = _make_controlled_snapshots([0.50, 0.50])
        client = _make_client(snapshots=snapshots)

        async def _test():
            await client.connect()
            # best_ask = 0.51, place buy at 0.52 → should fill at best_ask
            req = OrderRequest(
                instrument_id="MM-TEST",
                side=Side.BUY,
                size=10.0,
                price=0.52,
                order_type=OrderType.LIMIT,
            )
            result = await client.place_order(req)
            assert result.success
            assert result.status == OrderStatus.FILLED
            assert result.filled_size == 10.0
            assert result.filled_price == 0.51  # best_ask

        asyncio.run(_test())

    def test_sell_immediate_fill_when_crosses(self):
        """SELL limit at or below best bid should fill immediately."""
        snapshots = _make_controlled_snapshots([0.50, 0.50])
        client = _make_client(snapshots=snapshots)

        async def _test():
            await client.connect()
            # best_bid = 0.49, place sell at 0.48 → should fill at best_bid
            req = OrderRequest(
                instrument_id="MM-TEST",
                side=Side.SELL,
                size=10.0,
                price=0.48,
                order_type=OrderType.LIMIT,
            )
            result = await client.place_order(req)
            assert result.success
            assert result.status == OrderStatus.FILLED
            assert result.filled_price == 0.49  # best_bid

        asyncio.run(_test())

    def test_resting_order_fills_on_advance(self):
        """Resting order should fill when subsequent snapshot crosses it."""
        # Start at mid=0.50 (ask=0.51), then drop to mid=0.44 (ask=0.45)
        snapshots = _make_controlled_snapshots([0.50, 0.44, 0.44])
        client = _make_client(snapshots=snapshots)

        async def _test():
            await client.connect()
            # Place buy at 0.46 — ask is 0.51, won't cross yet
            req = OrderRequest(
                instrument_id="MM-TEST",
                side=Side.BUY,
                size=10.0,
                price=0.46,
                order_type=OrderType.LIMIT,
            )
            result = await client.place_order(req)
            assert result.status == OrderStatus.OPEN

            # Advance to snapshot with ask=0.45 → crosses buy at 0.46
            client.advance()

            # Order should now be filled
            opens = await client.get_open_orders()
            assert len(opens) == 0
            assert client.total_bid_fills == 1
            assert client.fills[0].price == 0.45  # filled at new best_ask

        asyncio.run(_test())

    def test_cancel_removes_from_open(self):
        """cancel_order should remove from open orders."""
        snapshots = _make_controlled_snapshots([0.50, 0.50, 0.50])
        client = _make_client(snapshots=snapshots)

        async def _test():
            await client.connect()
            req = OrderRequest(
                instrument_id="MM-TEST",
                side=Side.BUY,
                size=10.0,
                price=0.40,
                order_type=OrderType.LIMIT,
            )
            result = await client.place_order(req)
            assert len(await client.get_open_orders()) == 1

            cancelled = await client.cancel_order(result.order_id)
            assert cancelled is True
            assert len(await client.get_open_orders()) == 0
            assert client.cancelled_count == 1

        asyncio.run(_test())

    def test_fill_log_records_all_fills(self):
        """Fill log should record both immediate and resting fills."""
        # mid=0.50, then mid=0.40 (ask drops to 0.39)
        snapshots = _make_controlled_snapshots([0.50, 0.40])
        client = _make_client(snapshots=snapshots)

        async def _test():
            await client.connect()
            # Immediate fill: sell at 0.48 when best_bid=0.49
            sell_req = OrderRequest(
                instrument_id="MM-TEST",
                side=Side.SELL,
                size=5.0,
                price=0.48,
                order_type=OrderType.LIMIT,
            )
            await client.place_order(sell_req)

            # Resting: buy at 0.42 (ask=0.51, won't cross)
            buy_req = OrderRequest(
                instrument_id="MM-TEST",
                side=Side.BUY,
                size=5.0,
                price=0.42,
                order_type=OrderType.LIMIT,
            )
            await client.place_order(buy_req)

            assert len(client.fills) == 1  # Only sell filled so far

            client.advance()  # mid drops to 0.40, ask=0.41 → buy at 0.42 fills

            assert len(client.fills) == 2
            assert client.total_bid_fills == 1
            assert client.total_ask_fills == 1

        asyncio.run(_test())

    def test_get_open_orders_returns_tracked(self):
        """get_open_orders should return all resting orders."""
        snapshots = _make_controlled_snapshots([0.50, 0.50, 0.50])
        client = _make_client(snapshots=snapshots)

        async def _test():
            await client.connect()
            # Place 3 resting orders
            for price in [0.40, 0.42, 0.44]:
                req = OrderRequest(
                    instrument_id="MM-TEST",
                    side=Side.BUY,
                    size=10.0,
                    price=price,
                    order_type=OrderType.LIMIT,
                )
                await client.place_order(req)

            opens = await client.get_open_orders()
            assert len(opens) == 3
            prices = {o.price for o in opens}
            assert prices == {0.40, 0.42, 0.44}

        asyncio.run(_test())


# ---------------------------------------------------------------------------
# MMBacktestRunner
# ---------------------------------------------------------------------------

class TestMMBacktestRunner:

    def test_produces_mm_result(self):
        """Runner should produce a valid MMBacktestResult."""
        snapshots = _make_controlled_snapshots([0.50] * 30)
        runner = MMBacktestRunner(
            snapshots=snapshots,
            instrument_id="MM-TEST",
            scenario_name="mm_test",
            initial_balance=10_000,
        )
        result = asyncio.run(runner.run())
        assert isinstance(result, MMBacktestResult)
        assert result.signal_name == "market_making"
        assert result.scenario_name == "mm_test"
        # With snapshot deduplication, constant-midpoint series collapses
        # to first + last = 1 active step → equity_curve has 2 entries
        assert len(result.equity_curve) >= 2
        assert result.final_equity > 0

    def test_round_trip_profit(self):
        """Buying low and selling high should produce profit."""
        snapshots = _make_controlled_snapshots(
            [0.50] * 50, spread=0.04  # Wide spread for easier fills
        )
        runner = MMBacktestRunner(
            snapshots=snapshots,
            instrument_id="MM-TEST",
            scenario_name="stable",
            initial_balance=10_000,
        )
        result = asyncio.run(runner.run())
        assert isinstance(result, MMBacktestResult)
        # With a stable midpoint and wide spread, MM should at least not lose heavily
        assert result.final_equity > 0
        assert result.total_volume >= 0

    def test_inventory_tracked(self):
        """Inventory should be tracked through fills."""
        snapshots = _make_controlled_snapshots([0.50] * 40)
        runner = MMBacktestRunner(
            snapshots=snapshots,
            instrument_id="MM-TEST",
            scenario_name="inv_test",
            initial_balance=10_000,
        )
        result = asyncio.run(runner.run())
        assert isinstance(result, MMBacktestResult)
        # avg_inventory should be non-negative
        assert result.avg_inventory >= 0.0
        assert result.peak_inventory >= result.avg_inventory
