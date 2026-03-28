"""Tests for data_loader: OrderbookReconstructor, NormalizedTrade, parse_ctf_fill, BlockTimestampLookup."""

import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from omnitrade.backtest.data_loader import (
    NormalizedTrade,
    MarketInfo,
    OrderbookReconstructor,
    parse_ctf_fill,
    BlockTimestampLookup,
)
from omnitrade.backtest.engine import BacktestRunner, BacktestResult
from omnitrade.core.models import OrderbookSnapshot
from omnitrade.components.signals import MidpointDeviationSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trades(
    n: int = 20,
    base_price: float = 0.55,
    spread: float = 0.02,
    start: datetime | None = None,
    interval_seconds: int = 5,
) -> list[NormalizedTrade]:
    """Create a list of synthetic NormalizedTrades for testing."""
    if start is None:
        start = datetime(2024, 6, 1, tzinfo=timezone.utc)

    trades = []
    for i in range(n):
        side = "BUY" if i % 2 == 0 else "SELL"
        offset = spread / 2 if side == "BUY" else -spread / 2
        trades.append(NormalizedTrade(
            asset_id="TOKEN-001",
            side=side,
            price=round(base_price + offset, 4),
            size=round(10.0 + i * 0.5, 2),
            timestamp=start + timedelta(seconds=i * interval_seconds),
            condition_id="CID-001",
        ))
    return trades


# ---------------------------------------------------------------------------
# NormalizedTrade
# ---------------------------------------------------------------------------

class TestNormalizedTrade:
    def test_basic_construction(self):
        t = NormalizedTrade(
            asset_id="abc",
            side="BUY",
            price=0.65,
            size=100.0,
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            condition_id="cid-1",
        )
        assert t.asset_id == "abc"
        assert t.side == "BUY"
        assert t.price == 0.65
        assert t.size == 100.0

    def test_defaults(self):
        t = NormalizedTrade(
            asset_id="x",
            side="SELL",
            price=0.5,
            size=1.0,
            timestamp=datetime.now(timezone.utc),
        )
        assert t.condition_id == ""


# ---------------------------------------------------------------------------
# OrderbookReconstructor
# ---------------------------------------------------------------------------

class TestOrderbookReconstructor:
    def test_empty_trades_returns_empty(self):
        recon = OrderbookReconstructor()
        result = recon.reconstruct([])
        assert result == []

    def test_basic_reconstruction(self):
        trades = _make_trades(n=10)
        recon = OrderbookReconstructor(window_seconds=30)
        snapshots = recon.reconstruct(trades, "TEST-001")

        assert len(snapshots) > 0
        for snap in snapshots:
            assert snap.instrument_id == "TEST-001"
            assert len(snap.bids) > 0
            assert len(snap.asks) > 0

    def test_bid_less_than_ask(self):
        """Best bid must always be below best ask."""
        trades = _make_trades(n=40, base_price=0.6, spread=0.04)
        recon = OrderbookReconstructor(window_seconds=30)
        snapshots = recon.reconstruct(trades)

        for snap in snapshots:
            if snap.best_bid is not None and snap.best_ask is not None:
                assert snap.best_bid < snap.best_ask, (
                    f"bid={snap.best_bid} >= ask={snap.best_ask}"
                )

    def test_monotonic_timestamps(self):
        """Snapshot timestamps must be non-decreasing."""
        trades = _make_trades(n=30)
        recon = OrderbookReconstructor(window_seconds=15)
        snapshots = recon.reconstruct(trades)

        for i in range(1, len(snapshots)):
            assert snapshots[i].timestamp >= snapshots[i - 1].timestamp

    def test_carry_forward_for_gaps(self):
        """Empty windows should carry forward previous snapshot."""
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # Trades at t=0s and t=120s, gap in between
        trades = [
            NormalizedTrade("T1", "BUY", 0.60, 50, start, "C1"),
            NormalizedTrade("T1", "SELL", 0.58, 50, start, "C1"),
            NormalizedTrade("T1", "BUY", 0.62, 50, start + timedelta(seconds=120), "C1"),
            NormalizedTrade("T1", "SELL", 0.59, 50, start + timedelta(seconds=120), "C1"),
        ]
        recon = OrderbookReconstructor(window_seconds=30)
        snapshots = recon.reconstruct(trades)

        # Should have >2 snapshots due to the gap being filled
        assert len(snapshots) >= 3
        # Middle snapshots should have valid bids/asks (carried forward)
        for snap in snapshots:
            assert len(snap.bids) > 0
            assert len(snap.asks) > 0

    def test_single_sided_trades_get_synthetic_opposite(self):
        """If only buys or only sells, the other side is synthesized."""
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        # Only buy trades
        trades = [
            NormalizedTrade("T1", "BUY", 0.55, 100, start + timedelta(seconds=i), "C1")
            for i in range(5)
        ]
        recon = OrderbookReconstructor(window_seconds=30)
        snapshots = recon.reconstruct(trades)

        assert len(snapshots) >= 1
        snap = snapshots[0]
        assert len(snap.bids) > 0, "Bids should be synthesized"
        assert len(snap.asks) > 0, "Asks should come from buy trades"

    def test_configurable_depth(self):
        """Snapshots should respect depth_levels setting."""
        trades = _make_trades(n=20)
        for depth in [3, 5, 8]:
            recon = OrderbookReconstructor(depth_levels=depth)
            snapshots = recon.reconstruct(trades)
            for snap in snapshots:
                assert len(snap.bids) <= depth
                assert len(snap.asks) <= depth

    def test_prices_in_valid_range(self):
        """All prices must be in (0, 1)."""
        # Include extreme prices
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        trades = [
            NormalizedTrade("T1", "BUY", 0.99, 50, start, "C1"),
            NormalizedTrade("T1", "SELL", 0.01, 50, start + timedelta(seconds=1), "C1"),
            NormalizedTrade("T1", "BUY", 0.50, 50, start + timedelta(seconds=2), "C1"),
        ]
        recon = OrderbookReconstructor(window_seconds=30)
        snapshots = recon.reconstruct(trades)

        for snap in snapshots:
            for level in snap.bids + snap.asks:
                assert 0 < level.price < 1, f"Price {level.price} out of range"

    def test_window_size_affects_snapshot_count(self):
        """Larger windows should produce fewer snapshots."""
        trades = _make_trades(n=60, interval_seconds=5)  # 300s of trades
        snap_small = OrderbookReconstructor(window_seconds=15).reconstruct(trades)
        snap_large = OrderbookReconstructor(window_seconds=60).reconstruct(trades)
        assert len(snap_small) > len(snap_large)


# ---------------------------------------------------------------------------
# Integration: BacktestRunner with external snapshots
# ---------------------------------------------------------------------------

class TestExternalSnapshots:
    def test_runner_accepts_external_snapshots(self):
        """BacktestRunner should work with pre-built snapshots."""
        trades = _make_trades(n=40, base_price=0.55)
        recon = OrderbookReconstructor(window_seconds=30)
        snapshots = recon.reconstruct(trades, "EXT-001")

        signal = MidpointDeviationSignal(fair_value=0.5, min_deviation=0.03)
        runner = BacktestRunner(
            signal_source=signal,
            snapshots=snapshots,
            instrument_id="EXT-001",
            scenario_name="real_data_test",
        )
        result = asyncio.run(runner.run())

        assert isinstance(result, BacktestResult)
        assert result.scenario_name == "real_data_test"
        assert result.signal_name == "midpoint_deviation"
        assert result.final_equity > 0
        assert len(result.equity_curve) > 0


# ---------------------------------------------------------------------------
# CTF fill parser
# ---------------------------------------------------------------------------

class TestParseCTFFill:
    def test_taker_buys_tokens(self):
        """taker_asset_id=0 means taker gave USDC -> BUY."""
        result = parse_ctf_fill(
            maker_amount=500_000,   # maker gives 0.5 token (6 decimals)
            taker_amount=300_000,   # taker gives 0.3 USDC (6 decimals)
            maker_asset_id="12345",
            taker_asset_id="0",
        )
        assert result is not None
        asset_id, side, price, size, fee = result
        assert side == "BUY"
        assert asset_id == "12345"
        assert abs(price - 0.6) < 0.001  # 0.3 USDC / 0.5 tokens
        assert abs(size - 0.5) < 0.001

    def test_taker_sells_tokens(self):
        """maker_asset_id=0 means maker gave USDC -> taker SELL."""
        result = parse_ctf_fill(
            maker_amount=400_000,   # maker gives 0.4 USDC
            taker_amount=800_000,   # taker gives 0.8 tokens
            maker_asset_id="0",
            taker_asset_id="99999",
        )
        assert result is not None
        asset_id, side, price, size, fee = result
        assert side == "SELL"
        assert asset_id == "99999"
        assert abs(price - 0.5) < 0.001  # 0.4 / 0.8

    def test_neither_side_usdc_returns_none(self):
        """Token-for-token swap should be skipped."""
        result = parse_ctf_fill(
            maker_amount=100_000,
            taker_amount=200_000,
            maker_asset_id="111",
            taker_asset_id="222",
        )
        assert result is None

    def test_zero_amounts_returns_none(self):
        result = parse_ctf_fill(0, 0, "0", "123")
        assert result is None

    def test_zero_token_amount_returns_none(self):
        """Avoid division by zero."""
        result = parse_ctf_fill(
            maker_amount=100_000,
            taker_amount=0,
            maker_asset_id="0",
            taker_asset_id="123",
        )
        assert result is None

    def test_price_clamped_high(self):
        """Price > 0.999 should be clamped."""
        result = parse_ctf_fill(
            maker_amount=1_000_000,   # 1.0 tokens
            taker_amount=999_000,     # 0.999 USDC
            maker_asset_id="123",
            taker_asset_id="0",
        )
        assert result is not None
        _, _, price, _, _ = result
        assert price <= 0.999

    def test_price_clamped_low(self):
        """Very low price should be clamped to 0.001."""
        result = parse_ctf_fill(
            maker_amount=1_000_000,   # 1.0 USDC
            taker_amount=2_000_000_000,  # huge tokens
            maker_asset_id="0",
            taker_asset_id="123",
        )
        assert result is not None
        _, _, price, _, _ = result
        assert price >= 0.001

    def test_fee_parsed(self):
        result = parse_ctf_fill(
            maker_amount=500_000,
            taker_amount=250_000,
            maker_asset_id="0",
            taker_asset_id="777",
            fee=5_000,
        )
        assert result is not None
        _, _, _, _, fee = result
        assert abs(fee - 0.005) < 0.0001


# ---------------------------------------------------------------------------
# Block timestamp lookup
# ---------------------------------------------------------------------------

class TestBlockTimestampLookup:
    def test_no_data_uses_fallback(self, tmp_path):
        """Empty blocks dir -> interpolate returns fallback."""
        blocks_dir = tmp_path / "blocks"
        blocks_dir.mkdir()
        lookup = BlockTimestampLookup(str(blocks_dir))
        ts = lookup.interpolate(1_000_000)
        # Fallback: 1672531200 + 1_000_000 * 2
        assert abs(ts - (1672531200 + 1_000_000 * 2)) < 1.0

    def test_nonexistent_dir_uses_fallback(self, tmp_path):
        lookup = BlockTimestampLookup(str(tmp_path / "nope"))
        assert not lookup.has_data
        ts = lookup.interpolate(100)
        assert abs(ts - (1672531200 + 100 * 2)) < 1.0


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_normalized_trade_new_fields_default(self):
        """New fields should have sensible defaults."""
        t = NormalizedTrade(
            asset_id="x",
            side="BUY",
            price=0.5,
            size=10.0,
            timestamp=datetime.now(timezone.utc),
        )
        assert t.exchange == ""
        assert t.trade_id == ""
        assert t.fee == 0.0
        assert t.condition_id == ""

    def test_market_info_new_fields_default(self):
        """New MarketInfo fields should have sensible defaults."""
        m = MarketInfo(condition_id="cid", question="Will X happen?")
        assert m.exchange == ""
        assert m.slug == ""
        assert m.liquidity == 0.0
        assert m.outcome_prices == []
        assert m.end_date is None
        assert m.created_at is None
        assert m.event_ticker == ""
        assert m.yes_bid is None
        assert m.result == ""

    def test_normalized_trade_positional_args(self):
        """Positional construction with original args still works."""
        t = NormalizedTrade("abc", "BUY", 0.65, 100.0, datetime.now(timezone.utc), "cid-1")
        assert t.asset_id == "abc"
        assert t.condition_id == "cid-1"
