"""
Signal sources for trading bots.

Unified module containing all signal source implementations:
- Single-exchange signals (midpoint deviation, favorite-longshot, orderbook microstructure)
- Cross-exchange signals (binary-perp hedge, cross-exchange arbitrage)

Each signal source implements either SignalSource (single-exchange) or
CrossExchangeSignalSource (multi-exchange) and produces Signal or
MultiLegSignal objects that bots act on.
"""

import logging
import random
import re
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..core.enums import ExchangeId, InstrumentType, SignalDirection
from ..core.models import Instrument, MultiLegSignal, OrderbookSnapshot, Signal, SignalLeg
from ..exchanges.base import ExchangeClient

logger = logging.getLogger(__name__)


# === Single-Exchange Signals ===


class SignalSource(ABC):
    """Abstract base for signal generation."""

    name: str = ""

    @abstractmethod
    async def generate(self, client: ExchangeClient) -> list[Signal]:
        """Generate trading signals. Called each bot iteration."""
        pass


class MidpointDeviationSignal(SignalSource):
    """
    Simple signal: go long when price is below fair value,
    short when above. Useful for mean-reversion on binary markets.
    """

    name = "midpoint_deviation"

    def __init__(self, fair_value: float = 0.5, min_deviation: float = 0.05):
        self.fair_value = fair_value
        self.min_deviation = min_deviation

    async def generate(self, client: ExchangeClient) -> list[Signal]:
        signals = []
        instruments = await client.get_instruments(active_only=True)
        for inst in instruments:
            mid = await client.get_midpoint(inst.instrument_id)
            if mid is None:
                continue
            deviation = mid - self.fair_value
            if abs(deviation) < self.min_deviation:
                continue
            direction = SignalDirection.SHORT if deviation > 0 else SignalDirection.LONG
            score = abs(deviation) * 100  # Scale to 0-50 range
            signals.append(Signal(
                instrument_id=inst.instrument_id,
                direction=direction,
                score=score,
                source=self.name,
                price=mid,
                market_id=inst.market_id,
                exchange=client.exchange_id,
            ))
        return signals


class FavoriteLongshotSignal(SignalSource):
    """
    Exploits the favorite-longshot bias in prediction markets.

    Empirical observation: contracts priced below a low threshold
    underperform their implied odds (overpriced longshots), while
    contracts above a high threshold outperform (underpriced favorites).

    Strategy:
      - SHORT contracts priced below `low_threshold` (default 0.20)
      - LONG  contracts priced above `high_threshold` (default 0.80)

    Score scales with distance from threshold — more extreme prices
    produce stronger signals.
    """

    name = "favorite_longshot"

    def __init__(
        self,
        low_threshold: float = 0.20,
        high_threshold: float = 0.80,
        max_score: float = 100.0,
        max_lookups: int = 60,
    ):
        self.low_threshold = low_threshold
        self.high_threshold = high_threshold
        self.max_score = max_score
        self._max_lookups = max_lookups

    async def generate(self, client: ExchangeClient) -> list[Signal]:
        signals = []
        instruments = await client.get_instruments(active_only=True)

        def _sort_key(inst):
            p = inst.price if inst.price and inst.price > 0 else 0.5
            return min(abs(p - self.low_threshold), abs(p - self.high_threshold))

        sorted_insts = sorted(instruments, key=_sort_key)
        if len(sorted_insts) > self._max_lookups:
            prices = {inst.price for inst in sorted_insts if inst.price and inst.price > 0}
            if len(prices) <= 1:
                random.shuffle(sorted_insts)
        candidates = sorted_insts[:self._max_lookups]

        for inst in candidates:
            mid = await client.get_midpoint(inst.instrument_id)
            if mid is None or mid <= 0:
                continue

            if mid < self.low_threshold:
                edge = self.low_threshold - mid
                score = (edge / self.low_threshold) * self.max_score
                signals.append(Signal(
                    instrument_id=inst.instrument_id,
                    direction=SignalDirection.SHORT,
                    score=score,
                    source=self.name,
                    price=mid,
                    market_id=inst.market_id,
                    exchange=client.exchange_id,
                ))
            elif mid > self.high_threshold:
                edge = mid - self.high_threshold
                score = (edge / (1.0 - self.high_threshold)) * self.max_score
                signals.append(Signal(
                    instrument_id=inst.instrument_id,
                    direction=SignalDirection.LONG,
                    score=score,
                    source=self.name,
                    price=mid,
                    market_id=inst.market_id,
                    exchange=client.exchange_id,
                ))

        logger.info(
            f"FavoriteLongshot: {len(signals)} signals "
            f"({len(candidates)}/{len(instruments)} checked)"
        )
        return signals


# === Orderbook Microstructure ===


@dataclass
class MicrostructureFeatures:
    """Computed from a single orderbook snapshot."""
    volume_imbalance: float
    weighted_mid: float
    weighted_mid_deviation: float
    depth_pressure: float
    spread_signal: float
    raw_mid: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class OrderbookMicrostructureSignal(SignalSource):
    """
    Stateful signal source that analyzes orderbook microstructure
    to predict short-term price direction.

    Tracks rolling history of features per instrument to detect
    momentum and flow shifts.
    """

    name = "orderbook_microstructure"

    def __init__(
        self,
        window_size: int = 20,
        depth_levels: int = 5,
        min_score: float = 0.1,
        imbalance_weight: float = 0.30,
        weighted_mid_weight: float = 0.25,
        depth_weight: float = 0.25,
        spread_weight: float = 0.20,
    ):
        self.window_size = window_size
        self.depth_levels = depth_levels
        self.min_score = min_score
        self.imbalance_weight = imbalance_weight
        self.weighted_mid_weight = weighted_mid_weight
        self.depth_weight = depth_weight
        self.spread_weight = spread_weight
        self._history: dict[str, deque[MicrostructureFeatures]] = {}

    def _compute_features(self, snapshot: OrderbookSnapshot) -> Optional[MicrostructureFeatures]:
        if not snapshot.bids or not snapshot.asks:
            return None
        bids = snapshot.bids[:self.depth_levels]
        asks = snapshot.asks[:self.depth_levels]
        bid_vol = sum(level.size for level in bids)
        ask_vol = sum(level.size for level in asks)
        total_vol = bid_vol + ask_vol
        if total_vol == 0:
            return None
        volume_imbalance = bid_vol / total_vol
        raw_mid = snapshot.midpoint
        if raw_mid is None or raw_mid == 0:
            return None
        weighted_num = 0.0
        weighted_den = 0.0
        for level in bids:
            weighted_num += level.price * ask_vol
            weighted_den += ask_vol
        for level in asks:
            weighted_num += level.price * bid_vol
            weighted_den += bid_vol
        if weighted_den == 0:
            return None
        weighted_mid = weighted_num / weighted_den
        weighted_mid_deviation = weighted_mid - raw_mid
        max_levels = min(len(bids), len(asks))
        if max_levels == 0:
            return None
        depth_scores = []
        cum_bid = 0.0
        cum_ask = 0.0
        for i in range(max_levels):
            cum_bid += bids[i].size
            cum_ask += asks[i].size
            total = cum_bid + cum_ask
            if total > 0:
                depth_scores.append((cum_bid - cum_ask) / total)
        depth_pressure = sum(depth_scores) / len(depth_scores) if depth_scores else 0.0
        relative_spread = snapshot.spread
        if relative_spread is None:
            spread_signal = 1.0
        else:
            spread_signal = min(relative_spread / 0.20, 1.0)
        return MicrostructureFeatures(
            volume_imbalance=volume_imbalance,
            weighted_mid=weighted_mid,
            weighted_mid_deviation=weighted_mid_deviation,
            depth_pressure=depth_pressure,
            spread_signal=spread_signal,
            raw_mid=raw_mid,
            timestamp=snapshot.timestamp,
        )

    def _compute_composite(
        self,
        features: MicrostructureFeatures,
        history: deque[MicrostructureFeatures],
    ) -> tuple[float, SignalDirection, dict]:
        imbalance_centered = (features.volume_imbalance - 0.5) * 2
        depth_component = features.depth_pressure
        if features.raw_mid > 0:
            wm_normalized = features.weighted_mid_deviation / features.raw_mid
            wm_component = max(-1.0, min(1.0, wm_normalized * 10))
        else:
            wm_component = 0.0
        raw_composite = (
            self.imbalance_weight * imbalance_centered
            + self.weighted_mid_weight * wm_component
            + self.depth_weight * depth_component
        )
        spread_dampener = 1.0 - features.spread_signal * self.spread_weight
        composite = raw_composite * spread_dampener
        metadata = {
            "volume_imbalance": features.volume_imbalance,
            "weighted_mid_deviation": features.weighted_mid_deviation,
            "depth_pressure": features.depth_pressure,
            "spread_signal": features.spread_signal,
        }
        if len(history) >= 3:
            recent = list(history)[-3:]
            imbalance_values = [f.volume_imbalance for f in recent] + [features.volume_imbalance]
            slope = _simple_slope(imbalance_values)
            wm_drifts = [f.weighted_mid_deviation for f in recent] + [features.weighted_mid_deviation]
            avg_drift = sum(wm_drifts) / len(wm_drifts)
            drift_component = max(-1.0, min(1.0, avg_drift * 10)) if features.raw_mid > 0 else 0.0
            momentum = (slope + drift_component) / 2
            composite += momentum * 0.2
            metadata["momentum_slope"] = slope
            metadata["momentum_drift"] = drift_component
            prev_imbalance = history[-1].volume_imbalance
            imbalance_shift = features.volume_imbalance - prev_imbalance
            if abs(imbalance_shift) > 0.1:
                if (imbalance_shift > 0 and composite > 0) or (imbalance_shift < 0 and composite < 0):
                    composite *= 1.3
                    metadata["flow_shift_amplified"] = True
        if composite > 0:
            direction = SignalDirection.LONG
        elif composite < 0:
            direction = SignalDirection.SHORT
        else:
            direction = SignalDirection.NEUTRAL
        score = abs(composite) * 100
        return score, direction, metadata

    async def generate(self, client: ExchangeClient) -> list[Signal]:
        signals = []
        instruments = await client.get_instruments(active_only=True)
        for inst in instruments:
            try:
                snapshot = await client.get_orderbook(inst.instrument_id, depth=self.depth_levels)
            except Exception:
                logger.debug("Skipping %s: orderbook unavailable", inst.instrument_id)
                continue
            features = self._compute_features(snapshot)
            if features is None:
                continue
            if inst.instrument_id not in self._history:
                self._history[inst.instrument_id] = deque(maxlen=self.window_size)
            history = self._history[inst.instrument_id]
            score, direction, metadata = self._compute_composite(features, history)
            history.append(features)
            if score < self.min_score or direction == SignalDirection.NEUTRAL:
                continue
            signals.append(Signal(
                instrument_id=inst.instrument_id,
                direction=direction,
                score=score,
                source=self.name,
                price=features.raw_mid,
                market_id=inst.market_id,
                exchange=client.exchange_id,
                metadata=metadata,
            ))
        return signals

    def reset(self, instrument_id: Optional[str] = None) -> None:
        if instrument_id is not None:
            self._history.pop(instrument_id, None)
        else:
            self._history.clear()


def _simple_slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    if denominator == 0:
        return 0.0
    slope = numerator / denominator
    return max(-1.0, min(1.0, slope))


# === Cross-Exchange Signals ===


class CrossExchangeSignalSource:
    """Base for cross-exchange signal sources."""

    name = "cross_exchange"

    async def generate(self, clients: dict[ExchangeId, ExchangeClient]) -> list[MultiLegSignal]:
        raise NotImplementedError


ASSET_KEYWORD_MAP = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"],
    "DOGE": ["dogecoin", "doge"],
    "XRP": ["xrp", "ripple"],
    "AVAX": ["avalanche", "avax"],
    "MATIC": ["polygon", "matic"],
    "ARB": ["arbitrum", "arb"],
    "OP": ["optimism"],
    "LINK": ["chainlink", "link"],
}


def match_perp_symbol(market_name: str) -> Optional[str]:
    name_lower = market_name.lower()
    for symbol, keywords in ASSET_KEYWORD_MAP.items():
        for kw in keywords:
            if kw in name_lower:
                return symbol
    return None


def detect_direction(market_name: str) -> Optional[SignalDirection]:
    name_lower = market_name.lower()
    up_keywords = ["above", "reach", "exceed", "hit", "ath", "high", "rise", "up",
                   "over", "surpass", "break"]
    down_keywords = ["below", "drop", "fall", "crash", "low", "under", "decline",
                     "down", "sink"]
    up_count = sum(1 for kw in up_keywords if kw in name_lower)
    down_count = sum(1 for kw in down_keywords if kw in name_lower)
    if up_count > down_count:
        return SignalDirection.LONG
    elif down_count > up_count:
        return SignalDirection.SHORT
    return None


class BinaryPerpHedgeSignal(CrossExchangeSignalSource):
    """
    Generates hedged signals: binary outcome + perp hedge.
    """

    name = "binary_perp_hedge"

    def __init__(
        self,
        binary_exchange: ExchangeId = ExchangeId.POLYMARKET,
        hedge_exchange: ExchangeId = ExchangeId.HYPERLIQUID,
        min_binary_price: float = 0.70,
        max_binary_price: float = 0.95,
        min_score: float = 30.0,
        hedge_ratio: float = 0.5,
        hedge_leverage: float = 2.0,
    ):
        self.binary_exchange = binary_exchange
        self.hedge_exchange = hedge_exchange
        self.min_binary_price = min_binary_price
        self.max_binary_price = max_binary_price
        self.min_score = min_score
        self.hedge_ratio = hedge_ratio
        self.hedge_leverage = hedge_leverage

    async def generate(
        self, clients: dict[ExchangeId, ExchangeClient]
    ) -> list[MultiLegSignal]:
        binary_client = clients.get(self.binary_exchange)
        hedge_client = clients.get(self.hedge_exchange)
        if not binary_client or not hedge_client:
            return []
        signals: list[MultiLegSignal] = []
        try:
            instruments = await binary_client.get_instruments(active_only=True)
        except Exception as e:
            logger.warning(f"Failed to fetch binary instruments: {e}")
            return []
        try:
            perps = await hedge_client.get_instruments(active_only=True)
            perp_symbols = {p.instrument_id for p in perps}
        except Exception as e:
            logger.warning(f"Failed to fetch perp instruments: {e}")
            return []
        for inst in instruments:
            if inst.instrument_type not in (
                InstrumentType.BINARY_OUTCOME,
                InstrumentType.EVENT_CONTRACT,
            ):
                continue
            if not (self.min_binary_price <= inst.price <= self.max_binary_price):
                continue
            perp_symbol = match_perp_symbol(inst.name)
            if perp_symbol is None or perp_symbol not in perp_symbols:
                continue
            direction = detect_direction(inst.name)
            if direction is None:
                continue
            score = inst.price * 100
            if score < self.min_score:
                continue
            edge_bps = (1.0 - inst.price) * 10000 * 0.5
            if direction == SignalDirection.LONG:
                hedge_direction = SignalDirection.SHORT
            else:
                hedge_direction = SignalDirection.LONG
            try:
                perp_mid = await hedge_client.get_midpoint(perp_symbol)
            except Exception:
                perp_mid = 0.0
            signal = MultiLegSignal(
                legs=[
                    SignalLeg(
                        exchange=self.binary_exchange,
                        instrument_id=inst.instrument_id,
                        direction=SignalDirection.LONG,
                        weight=1.0,
                        price=inst.price,
                        metadata={"market_name": inst.name, "outcome": inst.outcome},
                    ),
                    SignalLeg(
                        exchange=self.hedge_exchange,
                        instrument_id=perp_symbol,
                        direction=hedge_direction,
                        weight=self.hedge_ratio,
                        price=perp_mid or 0.0,
                        leverage=self.hedge_leverage,
                        metadata={"hedge_type": "delta"},
                    ),
                ],
                strategy_type="binary_perp_hedge",
                score=score,
                source=self.name,
                edge_bps=edge_bps,
                metadata={
                    "binary_price": inst.price,
                    "perp_symbol": perp_symbol,
                    "direction": direction.value,
                },
            )
            signals.append(signal)
        signals.sort(key=lambda s: s.score, reverse=True)
        logger.info(f"BinaryPerpHedge: found {len(signals)} opportunities")
        return signals


class CrossExchangeArbSignal(CrossExchangeSignalSource):
    """
    Cross-exchange arbitrage signal.
    """

    name = "cross_exchange_arb"

    def __init__(
        self,
        min_edge_bps: float = 50.0,
        max_price: float = 0.95,
    ):
        self.min_edge_bps = min_edge_bps
        self.max_price = max_price

    async def generate(
        self, clients: dict[ExchangeId, ExchangeClient]
    ) -> list[MultiLegSignal]:
        poly_client = clients.get(ExchangeId.POLYMARKET)
        kalshi_client = clients.get(ExchangeId.KALSHI)
        if not poly_client or not kalshi_client:
            return []
        signals: list[MultiLegSignal] = []
        try:
            poly_instruments = await poly_client.get_instruments(active_only=True)
            kalshi_instruments = await kalshi_client.get_instruments(active_only=True)
        except Exception as e:
            logger.warning(f"Failed to fetch instruments for arb scan: {e}")
            return []
        kalshi_by_name: dict[str, Instrument] = {}
        for ki in kalshi_instruments:
            clean = re.sub(r"[^a-z0-9 ]", "", ki.name.lower().split(" - ")[0])
            kalshi_by_name[clean] = ki
        for pi in poly_instruments:
            clean = re.sub(r"[^a-z0-9 ]", "", pi.name.lower().split(" - ")[0])
            ki = kalshi_by_name.get(clean)
            if ki is None:
                continue
            if pi.outcome != ki.outcome:
                continue
            price_diff = abs(pi.price - ki.price)
            edge_bps = price_diff * 10000
            if edge_bps < self.min_edge_bps:
                continue
            if pi.price < ki.price:
                buy_exchange, buy_inst = ExchangeId.POLYMARKET, pi
                sell_exchange, sell_inst = ExchangeId.KALSHI, ki
            else:
                buy_exchange, buy_inst = ExchangeId.KALSHI, ki
                sell_exchange, sell_inst = ExchangeId.POLYMARKET, pi
            signal = MultiLegSignal(
                legs=[
                    SignalLeg(
                        exchange=buy_exchange,
                        instrument_id=buy_inst.instrument_id,
                        direction=SignalDirection.LONG,
                        weight=1.0,
                        price=buy_inst.price,
                    ),
                    SignalLeg(
                        exchange=sell_exchange,
                        instrument_id=sell_inst.instrument_id,
                        direction=SignalDirection.SHORT,
                        weight=1.0,
                        price=sell_inst.price,
                    ),
                ],
                strategy_type="cross_exchange_arb",
                score=edge_bps,
                source=self.name,
                edge_bps=edge_bps,
                metadata={
                    "buy_price": buy_inst.price,
                    "sell_price": sell_inst.price,
                    "market_name": pi.name,
                },
            )
            signals.append(signal)
        signals.sort(key=lambda s: s.score, reverse=True)
        logger.info(f"CrossExchangeArb: found {len(signals)} opportunities")
        return signals
