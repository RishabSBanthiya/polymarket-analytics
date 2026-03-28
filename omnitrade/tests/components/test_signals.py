"""Tests for signal sources."""

import pytest

from omnitrade.core.enums import SignalDirection, ExchangeId, InstrumentType
from omnitrade.core.models import Instrument
from omnitrade.components.signals import FavoriteLongshotSignal, MidpointDeviationSignal


def _patch_midpoints(mock_client, midpoints: dict):
    """Patch mock_client.get_midpoint to return per-instrument prices."""
    async def _get_midpoint(instrument_id):
        return midpoints.get(instrument_id)
    mock_client.get_midpoint = _get_midpoint


class TestFavoriteLongshotSignal:
    """Tests for the favorite-longshot bias signal."""

    async def test_short_signal_for_cheap_contract(self, mock_client):
        """Contracts below low_threshold should produce SHORT signals."""
        mock_client._instruments = [
            Instrument(
                instrument_id="longshot",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="Longshot Market",
                price=0.10,
                market_id="mkt-1",
            ),
        ]
        _patch_midpoints(mock_client, {"longshot": 0.10})

        signal_source = FavoriteLongshotSignal()
        signals = await signal_source.generate(mock_client)

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SHORT
        assert signals[0].instrument_id == "longshot"
        assert signals[0].source == "favorite_longshot"

    async def test_long_signal_for_expensive_contract(self, mock_client):
        """Contracts above high_threshold should produce LONG signals."""
        mock_client._instruments = [
            Instrument(
                instrument_id="favorite",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="Favorite Market",
                price=0.90,
                market_id="mkt-2",
            ),
        ]
        _patch_midpoints(mock_client, {"favorite": 0.90})

        signal_source = FavoriteLongshotSignal()
        signals = await signal_source.generate(mock_client)

        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.LONG
        assert signals[0].instrument_id == "favorite"

    async def test_no_signal_in_middle_range(self, mock_client):
        """Contracts between thresholds should produce no signals."""
        mock_client._instruments = [
            Instrument(
                instrument_id="midrange",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="Midrange Market",
                price=0.50,
                market_id="mkt-3",
            ),
        ]
        _patch_midpoints(mock_client, {"midrange": 0.50})

        signal_source = FavoriteLongshotSignal()
        signals = await signal_source.generate(mock_client)

        assert len(signals) == 0

    async def test_score_scales_with_extremity(self, mock_client):
        """More extreme prices should produce higher scores."""
        mock_client._instruments = [
            Instrument(
                instrument_id="very-cheap",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="Very Cheap",
                price=0.05,
                market_id="mkt-4",
            ),
            Instrument(
                instrument_id="somewhat-cheap",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="Somewhat Cheap",
                price=0.15,
                market_id="mkt-5",
            ),
        ]
        _patch_midpoints(mock_client, {"very-cheap": 0.05, "somewhat-cheap": 0.15})

        signal_source = FavoriteLongshotSignal()
        signals = await signal_source.generate(mock_client)

        assert len(signals) == 2
        by_id = {s.instrument_id: s for s in signals}
        assert by_id["very-cheap"].score > by_id["somewhat-cheap"].score

    async def test_custom_thresholds(self, mock_client):
        """Custom thresholds should be respected."""
        mock_client._instruments = [
            Instrument(
                instrument_id="edge-case",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="Edge Case",
                price=0.25,
                market_id="mkt-6",
            ),
        ]
        _patch_midpoints(mock_client, {"edge-case": 0.25})

        # Default thresholds: no signal at 0.25
        default_source = FavoriteLongshotSignal()
        assert len(await default_source.generate(mock_client)) == 0

        # Custom threshold at 0.30: should produce SHORT signal
        custom_source = FavoriteLongshotSignal(low_threshold=0.30)
        signals = await custom_source.generate(mock_client)
        assert len(signals) == 1
        assert signals[0].direction == SignalDirection.SHORT

    async def test_empty_instruments(self, mock_client):
        """No instruments should produce no signals."""
        mock_client._instruments = []

        signal_source = FavoriteLongshotSignal()
        signals = await signal_source.generate(mock_client)

        assert signals == []

    async def test_boundary_prices_excluded(self, mock_client):
        """Prices exactly at thresholds should not generate signals."""
        mock_client._instruments = [
            Instrument(
                instrument_id="at-low",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="At Low Threshold",
                price=0.20,
                market_id="mkt-7",
            ),
            Instrument(
                instrument_id="at-high",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="At High Threshold",
                price=0.80,
                market_id="mkt-8",
            ),
        ]
        _patch_midpoints(mock_client, {"at-low": 0.20, "at-high": 0.80})

        signal_source = FavoriteLongshotSignal()
        signals = await signal_source.generate(mock_client)

        assert len(signals) == 0

    async def test_name_property(self):
        signal_source = FavoriteLongshotSignal()
        assert signal_source.name == "favorite_longshot"

    async def test_mixed_instruments(self, mock_client):
        """Mix of cheap, expensive, and midrange should only signal extremes."""
        mock_client._instruments = [
            Instrument(
                instrument_id="cheap",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="Cheap",
                price=0.08,
                market_id="mkt-a",
            ),
            Instrument(
                instrument_id="mid",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="Mid",
                price=0.50,
                market_id="mkt-b",
            ),
            Instrument(
                instrument_id="expensive",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="Expensive",
                price=0.92,
                market_id="mkt-c",
            ),
        ]
        _patch_midpoints(mock_client, {"cheap": 0.08, "mid": 0.50, "expensive": 0.92})

        signal_source = FavoriteLongshotSignal()
        signals = await signal_source.generate(mock_client)

        assert len(signals) == 2
        directions = {s.instrument_id: s.direction for s in signals}
        assert directions["cheap"] == SignalDirection.SHORT
        assert directions["expensive"] == SignalDirection.LONG

    async def test_zero_price_skipped(self, mock_client):
        """Instruments with zero midpoint should be skipped."""
        mock_client._instruments = [
            Instrument(
                instrument_id="no-price",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name="No Price",
                price=0.0,
                market_id="mkt-9",
            ),
        ]
        _patch_midpoints(mock_client, {"no-price": 0.0})

        signal_source = FavoriteLongshotSignal()
        signals = await signal_source.generate(mock_client)

        assert len(signals) == 0

    async def test_max_lookups_caps_api_calls(self, mock_client):
        """Only max_lookups instruments should be checked."""
        mock_client._instruments = [
            Instrument(
                instrument_id=f"inst-{i}",
                exchange=ExchangeId.POLYMARKET,
                instrument_type=InstrumentType.BINARY_OUTCOME,
                name=f"Inst {i}",
                price=0.10,
                market_id=f"mkt-{i}",
            )
            for i in range(50)
        ]
        call_count = 0
        async def _counting_midpoint(instrument_id):
            nonlocal call_count
            call_count += 1
            return 0.10
        mock_client.get_midpoint = _counting_midpoint

        signal_source = FavoriteLongshotSignal(max_lookups=5)
        signals = await signal_source.generate(mock_client)

        assert call_count == 5
        assert len(signals) == 5
