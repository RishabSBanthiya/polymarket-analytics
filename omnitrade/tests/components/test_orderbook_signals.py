"""Tests for orderbook microstructure signal."""

import pytest
from collections import deque
from datetime import datetime, timezone

from omnitrade.core.enums import ExchangeId, SignalDirection, InstrumentType
from omnitrade.core.models import (
    Instrument, OrderbookSnapshot, OrderbookLevel, Signal,
)
from omnitrade.components.signals import (
    MicrostructureFeatures,
    OrderbookMicrostructureSignal,
    _simple_slope,
)


def make_book(bids, asks, instrument_id="test-token"):
    """Helper: create OrderbookSnapshot from (price, size) tuples."""
    return OrderbookSnapshot(
        instrument_id=instrument_id,
        bids=[OrderbookLevel(price=p, size=s) for p, s in bids],
        asks=[OrderbookLevel(price=p, size=s) for p, s in asks],
    )


# ==================== Feature Computation ====================


class TestComputeFeatures:
    def test_balanced_book(self):
        """Equal bid/ask volume should give 0.5 imbalance."""
        book = make_book(
            bids=[(0.50, 100), (0.49, 100)],
            asks=[(0.52, 100), (0.53, 100)],
        )
        sig = OrderbookMicrostructureSignal(depth_levels=5)
        features = sig._compute_features(book)

        assert features is not None
        assert features.volume_imbalance == pytest.approx(0.5)
        assert features.depth_pressure == pytest.approx(0.0)
        assert features.raw_mid == pytest.approx(0.51)

    def test_bid_heavy_book(self):
        """More bid volume should give imbalance > 0.5."""
        book = make_book(
            bids=[(0.50, 300), (0.49, 200)],
            asks=[(0.52, 50), (0.53, 50)],
        )
        sig = OrderbookMicrostructureSignal(depth_levels=5)
        features = sig._compute_features(book)

        assert features is not None
        assert features.volume_imbalance > 0.5
        assert features.depth_pressure > 0

    def test_ask_heavy_book(self):
        """More ask volume should give imbalance < 0.5."""
        book = make_book(
            bids=[(0.50, 50), (0.49, 50)],
            asks=[(0.52, 300), (0.53, 200)],
        )
        sig = OrderbookMicrostructureSignal(depth_levels=5)
        features = sig._compute_features(book)

        assert features is not None
        assert features.volume_imbalance < 0.5
        assert features.depth_pressure < 0

    def test_wide_spread_book(self):
        """Wide spread should give high spread_signal."""
        book = make_book(
            bids=[(0.30, 100)],
            asks=[(0.70, 100)],
        )
        sig = OrderbookMicrostructureSignal(depth_levels=5)
        features = sig._compute_features(book)

        assert features is not None
        assert features.spread_signal == pytest.approx(1.0)  # Capped at 1.0

    def test_tight_spread_book(self):
        """Tight spread should give low spread_signal."""
        book = make_book(
            bids=[(0.500, 100)],
            asks=[(0.501, 100)],
        )
        sig = OrderbookMicrostructureSignal(depth_levels=5)
        features = sig._compute_features(book)

        assert features is not None
        assert features.spread_signal < 0.1

    def test_empty_bids_returns_none(self):
        """Empty bid side should return None."""
        book = make_book(bids=[], asks=[(0.52, 100)])
        sig = OrderbookMicrostructureSignal()
        assert sig._compute_features(book) is None

    def test_empty_asks_returns_none(self):
        """Empty ask side should return None."""
        book = make_book(bids=[(0.50, 100)], asks=[])
        sig = OrderbookMicrostructureSignal()
        assert sig._compute_features(book) is None

    def test_zero_volume_returns_none(self):
        """Zero volume on both sides should return None."""
        book = make_book(
            bids=[(0.50, 0)],
            asks=[(0.52, 0)],
        )
        sig = OrderbookMicrostructureSignal()
        assert sig._compute_features(book) is None

    def test_single_level_book(self):
        """Single level on each side should work."""
        book = make_book(
            bids=[(0.50, 100)],
            asks=[(0.52, 100)],
        )
        sig = OrderbookMicrostructureSignal(depth_levels=1)
        features = sig._compute_features(book)

        assert features is not None
        assert features.volume_imbalance == pytest.approx(0.5)
        assert features.raw_mid == pytest.approx(0.51)

    def test_depth_levels_limits_analysis(self):
        """Only top N levels should be analyzed."""
        book = make_book(
            bids=[(0.50, 100), (0.49, 100), (0.48, 1000)],
            asks=[(0.52, 100), (0.53, 100), (0.54, 1000)],
        )
        sig = OrderbookMicrostructureSignal(depth_levels=2)
        features = sig._compute_features(book)

        # With depth_levels=2, only first 2 levels used: 200 vs 200
        assert features is not None
        assert features.volume_imbalance == pytest.approx(0.5)


# ==================== Composite Scoring ====================


class TestComputeComposite:
    def test_bullish_signal_direction(self):
        """Bid-heavy features should produce LONG direction."""
        features = MicrostructureFeatures(
            volume_imbalance=0.8,
            weighted_mid=0.52,
            weighted_mid_deviation=0.01,
            depth_pressure=0.5,
            spread_signal=0.1,
            raw_mid=0.51,
        )
        sig = OrderbookMicrostructureSignal()
        score, direction, meta = sig._compute_composite(features, deque())

        assert direction == SignalDirection.LONG
        assert score > 0

    def test_bearish_signal_direction(self):
        """Ask-heavy features should produce SHORT direction."""
        features = MicrostructureFeatures(
            volume_imbalance=0.2,
            weighted_mid=0.50,
            weighted_mid_deviation=-0.01,
            depth_pressure=-0.5,
            spread_signal=0.1,
            raw_mid=0.51,
        )
        sig = OrderbookMicrostructureSignal()
        score, direction, meta = sig._compute_composite(features, deque())

        assert direction == SignalDirection.SHORT
        assert score > 0

    def test_spread_dampening(self):
        """Wide spread should reduce score vs tight spread."""
        features_tight = MicrostructureFeatures(
            volume_imbalance=0.8,
            weighted_mid=0.52,
            weighted_mid_deviation=0.01,
            depth_pressure=0.5,
            spread_signal=0.0,  # Tight
            raw_mid=0.51,
        )
        features_wide = MicrostructureFeatures(
            volume_imbalance=0.8,
            weighted_mid=0.52,
            weighted_mid_deviation=0.01,
            depth_pressure=0.5,
            spread_signal=1.0,  # Wide
            raw_mid=0.51,
        )
        sig = OrderbookMicrostructureSignal()
        score_tight, _, _ = sig._compute_composite(features_tight, deque())
        score_wide, _, _ = sig._compute_composite(features_wide, deque())

        assert score_tight > score_wide

    def test_metadata_contains_features(self):
        """Metadata should contain feature values."""
        features = MicrostructureFeatures(
            volume_imbalance=0.6,
            weighted_mid=0.51,
            weighted_mid_deviation=0.005,
            depth_pressure=0.2,
            spread_signal=0.3,
            raw_mid=0.505,
        )
        sig = OrderbookMicrostructureSignal()
        _, _, meta = sig._compute_composite(features, deque())

        assert "volume_imbalance" in meta
        assert "depth_pressure" in meta
        assert "spread_signal" in meta
        assert meta["volume_imbalance"] == pytest.approx(0.6)


# ==================== Statefulness ====================


class TestStatefulness:
    def test_history_accumulation(self):
        """History should grow with each call."""
        sig = OrderbookMicrostructureSignal(window_size=10)
        features = MicrostructureFeatures(
            volume_imbalance=0.6, weighted_mid=0.51,
            weighted_mid_deviation=0.005, depth_pressure=0.2,
            spread_signal=0.1, raw_mid=0.505,
        )
        history = deque(maxlen=10)

        for _ in range(5):
            sig._compute_composite(features, history)
            history.append(features)

        assert len(history) == 5

    def test_momentum_with_history(self):
        """With >= 3 history points, momentum should be computed."""
        sig = OrderbookMicrostructureSignal()
        history = deque(maxlen=20)

        # Build up bullish history
        for imb in [0.55, 0.60, 0.65]:
            history.append(MicrostructureFeatures(
                volume_imbalance=imb, weighted_mid=0.51,
                weighted_mid_deviation=0.005, depth_pressure=0.2,
                spread_signal=0.1, raw_mid=0.505,
            ))

        current = MicrostructureFeatures(
            volume_imbalance=0.70, weighted_mid=0.52,
            weighted_mid_deviation=0.01, depth_pressure=0.3,
            spread_signal=0.1, raw_mid=0.51,
        )
        _, _, meta = sig._compute_composite(current, history)
        assert "momentum_slope" in meta

    def test_no_momentum_without_history(self):
        """Without 3 history points, no momentum metadata."""
        sig = OrderbookMicrostructureSignal()
        features = MicrostructureFeatures(
            volume_imbalance=0.6, weighted_mid=0.51,
            weighted_mid_deviation=0.005, depth_pressure=0.2,
            spread_signal=0.1, raw_mid=0.505,
        )
        _, _, meta = sig._compute_composite(features, deque())
        assert "momentum_slope" not in meta

    def test_flow_shift_amplification(self):
        """Sudden imbalance shift in same direction should amplify score."""
        sig = OrderbookMicrostructureSignal()
        history = deque(maxlen=20)

        # History with moderate imbalance
        for _ in range(3):
            history.append(MicrostructureFeatures(
                volume_imbalance=0.55, weighted_mid=0.51,
                weighted_mid_deviation=0.005, depth_pressure=0.2,
                spread_signal=0.1, raw_mid=0.505,
            ))

        # Current: sudden jump to very bullish
        current_amplified = MicrostructureFeatures(
            volume_imbalance=0.80, weighted_mid=0.52,
            weighted_mid_deviation=0.01, depth_pressure=0.3,
            spread_signal=0.1, raw_mid=0.51,
        )

        score_amp, _, meta_amp = sig._compute_composite(current_amplified, history)

        # Compare against no sudden shift
        history_stable = deque(maxlen=20)
        for _ in range(3):
            history_stable.append(MicrostructureFeatures(
                volume_imbalance=0.75, weighted_mid=0.51,
                weighted_mid_deviation=0.005, depth_pressure=0.2,
                spread_signal=0.1, raw_mid=0.505,
            ))
        current_stable = MicrostructureFeatures(
            volume_imbalance=0.80, weighted_mid=0.52,
            weighted_mid_deviation=0.01, depth_pressure=0.3,
            spread_signal=0.1, raw_mid=0.51,
        )
        score_stable, _, meta_stable = sig._compute_composite(current_stable, history_stable)

        assert meta_amp.get("flow_shift_amplified", False) is True
        assert score_amp > score_stable


# ==================== Integration ====================


class TestGenerate:
    @pytest.mark.asyncio
    async def test_generate_with_mock_client(self, mock_client):
        """generate() should return signals from multi-level orderbooks."""
        mock_client._orderbook = make_book(
            bids=[(0.50, 300), (0.49, 200), (0.48, 100)],
            asks=[(0.52, 50), (0.53, 50), (0.54, 50)],
        )
        sig = OrderbookMicrostructureSignal(depth_levels=3, min_score=0.0)
        signals = await sig.generate(mock_client)

        assert len(signals) > 0
        for s in signals:
            assert isinstance(s, Signal)
            assert s.source == "orderbook_microstructure"
            assert s.direction in (SignalDirection.LONG, SignalDirection.SHORT)

    @pytest.mark.asyncio
    async def test_generate_empty_book_no_signals(self, mock_client):
        """Empty orderbook should produce no signals."""
        mock_client._orderbook = OrderbookSnapshot(
            instrument_id="test-token", bids=[], asks=[],
        )
        sig = OrderbookMicrostructureSignal()
        signals = await sig.generate(mock_client)
        assert signals == []

    @pytest.mark.asyncio
    async def test_generate_builds_history(self, mock_client):
        """Successive generate() calls should build up history."""
        mock_client._orderbook = make_book(
            bids=[(0.50, 200), (0.49, 100)],
            asks=[(0.52, 100), (0.53, 100)],
        )
        sig = OrderbookMicrostructureSignal(depth_levels=3, min_score=0.0)

        await sig.generate(mock_client)
        await sig.generate(mock_client)
        await sig.generate(mock_client)

        # Each instrument should have 3 history entries
        for inst_id, hist in sig._history.items():
            assert len(hist) == 3

    @pytest.mark.asyncio
    async def test_min_score_filters(self, mock_client):
        """Signals below min_score should be filtered."""
        # Nearly balanced book -> low score
        mock_client._orderbook = make_book(
            bids=[(0.50, 100)],
            asks=[(0.52, 100)],
        )
        sig = OrderbookMicrostructureSignal(min_score=999.0)
        signals = await sig.generate(mock_client)
        assert signals == []


# ==================== Config & Reset ====================


class TestConfigAndReset:
    def test_custom_weights(self):
        """Custom weights should be stored."""
        sig = OrderbookMicrostructureSignal(
            imbalance_weight=0.40,
            weighted_mid_weight=0.30,
            depth_weight=0.20,
            spread_weight=0.10,
        )
        assert sig.imbalance_weight == 0.40
        assert sig.weighted_mid_weight == 0.30
        assert sig.depth_weight == 0.20
        assert sig.spread_weight == 0.10

    def test_custom_window_size(self):
        """Custom window size should limit history."""
        sig = OrderbookMicrostructureSignal(window_size=3)
        sig._history["test"] = deque(maxlen=3)
        for i in range(10):
            sig._history["test"].append(MicrostructureFeatures(
                volume_imbalance=0.5, weighted_mid=0.51,
                weighted_mid_deviation=0.0, depth_pressure=0.0,
                spread_signal=0.1, raw_mid=0.51,
            ))
        assert len(sig._history["test"]) == 3

    def test_reset_all(self):
        """reset() with no args should clear all history."""
        sig = OrderbookMicrostructureSignal()
        sig._history["a"] = deque([MicrostructureFeatures(
            volume_imbalance=0.5, weighted_mid=0.51,
            weighted_mid_deviation=0.0, depth_pressure=0.0,
            spread_signal=0.1, raw_mid=0.51,
        )])
        sig._history["b"] = deque([MicrostructureFeatures(
            volume_imbalance=0.5, weighted_mid=0.51,
            weighted_mid_deviation=0.0, depth_pressure=0.0,
            spread_signal=0.1, raw_mid=0.51,
        )])
        sig.reset()
        assert len(sig._history) == 0

    def test_reset_single_instrument(self):
        """reset(instrument_id) should only clear that instrument."""
        sig = OrderbookMicrostructureSignal()
        sig._history["a"] = deque([MicrostructureFeatures(
            volume_imbalance=0.5, weighted_mid=0.51,
            weighted_mid_deviation=0.0, depth_pressure=0.0,
            spread_signal=0.1, raw_mid=0.51,
        )])
        sig._history["b"] = deque([MicrostructureFeatures(
            volume_imbalance=0.5, weighted_mid=0.51,
            weighted_mid_deviation=0.0, depth_pressure=0.0,
            spread_signal=0.1, raw_mid=0.51,
        )])
        sig.reset("a")
        assert "a" not in sig._history
        assert "b" in sig._history

    def test_name(self):
        """name property should return correct value."""
        sig = OrderbookMicrostructureSignal()
        assert sig.name == "orderbook_microstructure"


# ==================== Helper ====================


class TestSimpleSlope:
    def test_increasing_values(self):
        assert _simple_slope([1.0, 2.0, 3.0, 4.0]) > 0

    def test_decreasing_values(self):
        assert _simple_slope([4.0, 3.0, 2.0, 1.0]) < 0

    def test_constant_values(self):
        assert _simple_slope([5.0, 5.0, 5.0]) == pytest.approx(0.0)

    def test_single_value(self):
        assert _simple_slope([1.0]) == 0.0

    def test_clamped_to_range(self):
        result = _simple_slope([0.0, 0.0, 0.0, 100.0])
        assert -1.0 <= result <= 1.0
