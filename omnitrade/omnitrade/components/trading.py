"""
Trading components: position sizing, order execution helpers, and exit strategies.

Consolidates sizers, pre-trade safety checks, and exit monitors into a single module.
All components work with any exchange through the unified ExchangeClient interface.

Execution is handled by ExchangeClient.place_order() directly — paper mode is
implemented by wrapping the client with PaperClient (see exchanges/base.py).
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from ..core.enums import ExitReason, OrderStatus, OrderType, Side, SignalDirection
from ..core.models import (
    OrderbookSnapshot,
    OrderRequest,
    OrderResult,
    PositionState,
    Signal,
)
from ..exchanges.base import ExchangeClient

logger = logging.getLogger(__name__)


# === Position Sizing ===


class PositionSizer(ABC):
    """Abstract base for position sizing strategies."""

    name: str = ""

    @abstractmethod
    def calculate_size(self, signal: Signal, available_capital: float, current_price: float) -> float:
        """Calculate position size in USD. Returns 0 to skip."""
        pass


class FixedSizer(PositionSizer):
    """Fixed USD amount per trade."""

    name = "fixed"

    def __init__(self, amount_usd: float = 50.0):
        self.amount = amount_usd

    def calculate_size(self, signal: Signal, available_capital: float, current_price: float) -> float:
        return min(self.amount, available_capital)


class PercentageSizer(PositionSizer):
    """Fixed percentage of capital per trade."""

    name = "percentage"

    def __init__(self, percentage: float = 0.02):
        if not 0 < percentage <= 1:
            raise ValueError("Percentage must be between 0 and 1")
        self.percentage = percentage

    def calculate_size(self, signal: Signal, available_capital: float, current_price: float) -> float:
        return available_capital * self.percentage


class FixedFractionSizer(PositionSizer):
    """Fraction of capital with min/max constraints."""

    name = "fixed_fraction"

    def __init__(self, fraction: float = 0.10, min_usd: float = 10.0, max_usd: float = 100.0):
        if not 0 < fraction <= 1:
            raise ValueError("Fraction must be between 0 and 1")
        self.fraction = fraction
        self.min_usd = min_usd
        self.max_usd = max_usd

    def calculate_size(self, signal: Signal, available_capital: float, current_price: float) -> float:
        base = available_capital * self.fraction
        size = max(self.min_usd, min(self.max_usd, base))
        return min(size, available_capital)


class KellySizer(PositionSizer):
    """
    Kelly Criterion sizing for binary outcomes.

    Uses fractional Kelly (default half) for safety.
    """

    name = "kelly"

    def __init__(self, kelly_fraction: float = 0.5, min_edge: float = 0.02, max_kelly: float = 0.25):
        self.kelly_fraction = kelly_fraction
        self.min_edge = min_edge
        self.max_kelly = max_kelly

    def calculate_size(self, signal: Signal, available_capital: float, current_price: float) -> float:
        if current_price <= 0 or current_price >= 1:
            return 0.0
        edge = min(signal.score / 100, 0.5) * 0.1
        if edge < self.min_edge:
            return 0.0
        p = current_price + edge
        q = 1 - p
        b = (1.0 / current_price) - 1
        if b <= 0:
            return 0.0
        kelly = max(0, (p * b - q) / b)
        adjusted = min(kelly * self.kelly_fraction, self.max_kelly)
        return available_capital * adjusted


class SignalScaledSizer(PositionSizer):
    """Size scaled by signal strength."""

    name = "signal_scaled"

    def __init__(
        self, base_fraction: float = 0.02, reference_score: float = 50.0,
        scale_factor: float = 1.0, min_score: float = 20.0, max_multiplier: float = 3.0,
    ):
        self.base_fraction = base_fraction
        self.reference_score = reference_score
        self.scale_factor = scale_factor
        self.min_score = min_score
        self.max_multiplier = max_multiplier

    def calculate_size(self, signal: Signal, available_capital: float, current_price: float) -> float:
        if signal.score < self.min_score:
            logger.info(
                "Sizer: score %.1f < min %.1f for %s -> $0",
                signal.score, self.min_score, signal.instrument_id,
            )
            return 0.0
        if available_capital <= 0:
            logger.info(
                "Sizer: no capital ($%.2f) for %s -> $0",
                available_capital, signal.instrument_id,
            )
            return 0.0
        multiplier = min((signal.score / self.reference_score) * self.scale_factor, self.max_multiplier)
        size = available_capital * self.base_fraction * multiplier
        logger.debug(
            "Sizer: %s score=%.1f x%.2f -> $%.2f (%.1f%% of $%.2f)",
            signal.instrument_id, signal.score, multiplier, size,
            size / available_capital * 100, available_capital,
        )
        return size


class CompositeSizer(PositionSizer):
    """Takes the minimum of multiple sizers (safety)."""

    name = "composite"

    def __init__(self, sizers: list[PositionSizer]):
        if not sizers:
            raise ValueError("Need at least one sizer")
        self._sizers = sizers

    def calculate_size(self, signal: Signal, available_capital: float, current_price: float) -> float:
        sizes = [s.calculate_size(signal, available_capital, current_price) for s in self._sizers]
        return min(sizes) if sizes else 0.0


# === Execution Helpers ===


def direction_to_side(direction: SignalDirection) -> Side:
    """Map signal direction to order side."""
    if direction == SignalDirection.LONG:
        return Side.BUY
    elif direction == SignalDirection.SHORT:
        return Side.SELL
    raise ValueError(f"Cannot convert {direction} to Side")


def check_pre_trade_safety(
    orderbook: OrderbookSnapshot,
    side: Side,
    price: float,
    max_spread: float = 0.03,
    max_slippage: float = 0.02,
) -> Optional[OrderResult]:
    """
    Pre-trade safety checks (spread + slippage).

    Returns an OrderResult rejection if checks fail, or None if safe to proceed.
    """
    # Spread check (only on buys — exits should always be allowed)
    if side == Side.BUY and orderbook.spread is not None:
        if orderbook.spread > max_spread:
            return OrderResult(
                success=False,
                error_message=f"Spread too wide: {orderbook.spread:.1%} > {max_spread:.1%}",
                is_rejection=True,
            )

    # Get execution price
    if side == Side.BUY:
        exec_price = orderbook.best_ask
    else:
        exec_price = orderbook.best_bid

    if exec_price is None or exec_price <= 0:
        return OrderResult(
            success=False,
            error_message=f"No {'ask' if side == Side.BUY else 'bid'} available",
        )

    # Slippage check
    if price > 0:
        slippage = abs(exec_price - price) / price
        if slippage > max_slippage:
            return OrderResult(
                success=False,
                requested_price=price,
                error_message=f"Slippage {slippage:.1%} > {max_slippage:.1%}",
                is_rejection=True,
            )

    return None


async def execute_aggressive(
    client: ExchangeClient,
    instrument_id: str,
    side: Side,
    size_usd: float,
    price: float,
    max_spread: float = 0.03,
    max_slippage: float = 0.02,
) -> OrderResult:
    """
    Execute aggressively — take best available price with safety checks.

    This is the standard live execution path for directional and cross-exchange bots.
    """
    orderbook = await client.get_orderbook(instrument_id, depth=5)

    rejection = check_pre_trade_safety(orderbook, side, price, max_spread, max_slippage)
    if rejection is not None:
        return rejection

    exec_price = orderbook.best_ask if side == Side.BUY else orderbook.best_bid
    shares = size_usd / exec_price
    if shares <= 0:
        return OrderResult(success=False, error_message="Invalid share calculation")

    request = OrderRequest(
        instrument_id=instrument_id,
        side=side,
        size=shares,
        price=exec_price,
        order_type=OrderType.GTC,
    )
    return await client.place_order(request)


# === Exit Strategies ===


@dataclass
class ExitConfig:
    """Exit strategy configuration. Defaults optimized via Bayesian search."""

    near_resolution_enabled: bool = True
    near_resolution_high: float = 0.99
    near_resolution_low: float = 0.01

    take_profit_enabled: bool = True
    take_profit_pct: float = 0.05

    trailing_stop_enabled: bool = True
    trailing_stop_activation_pct: float = 0.02
    trailing_stop_distance_pct: float = 0.01

    time_exit_enabled: bool = True
    max_hold_minutes: int = 75

    stop_loss_enabled: bool = True
    stop_loss_pct: float = 0.25

    def __post_init__(self):
        if self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be positive")
        if self.trailing_stop_distance_pct <= 0:
            raise ValueError("trailing_stop_distance_pct must be positive")
        if self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be positive")


class ExitMonitor:
    """
    Monitors positions and determines when to exit.

    Priority: near-resolution > take-profit > trailing stop > stop-loss > time limit
    """

    def __init__(self, config: Optional[ExitConfig] = None):
        self.config = config or ExitConfig()
        self._states: dict[str, PositionState] = {}

    def register(self, position_id: str, state: PositionState) -> None:
        self._states[position_id] = state

    def unregister(self, position_id: str) -> None:
        self._states.pop(position_id, None)

    def get_state(self, position_id: str) -> Optional[PositionState]:
        return self._states.get(position_id)

    def check(
        self, state: PositionState, current_price: float, current_time: datetime,
    ) -> Optional[Tuple[ExitReason, float, str]]:
        if state.entry_price <= 0:
            return None
        self._update_state(state, current_price)
        ret = (current_price - state.entry_price) / state.entry_price
        if self.config.near_resolution_enabled:
            if current_price >= self.config.near_resolution_high:
                return (ExitReason.NEAR_RESOLUTION, current_price,
                        f"Price {current_price:.4f} >= {self.config.near_resolution_high}")
            if current_price <= self.config.near_resolution_low:
                return (ExitReason.NEAR_RESOLUTION, current_price,
                        f"Price {current_price:.4f} <= {self.config.near_resolution_low}")
        if self.config.take_profit_enabled and ret >= self.config.take_profit_pct:
            return (ExitReason.TAKE_PROFIT, current_price, f"Return {ret:.1%} >= {self.config.take_profit_pct:.1%}")
        if self.config.trailing_stop_enabled and state.trailing_stop_activated:
            if current_price <= state.trailing_stop_level:
                return (ExitReason.TRAILING_STOP, current_price,
                        f"Price {current_price:.4f} <= stop {state.trailing_stop_level:.4f}")
        if self.config.stop_loss_enabled and ret <= -self.config.stop_loss_pct:
            return (ExitReason.STOP_LOSS, current_price, f"Loss {ret:.1%} <= -{self.config.stop_loss_pct:.1%}")
        if self.config.time_exit_enabled:
            hold_time = (current_time - state.entry_time).total_seconds() / 60
            if hold_time >= self.config.max_hold_minutes:
                return (ExitReason.TIME_LIMIT, current_price, f"Held {hold_time:.0f} min >= {self.config.max_hold_minutes}")
        return None

    def _update_state(self, state: PositionState, price: float) -> None:
        state.peak_price = max(state.peak_price, price)
        state.trough_price = min(state.trough_price, price)
        if self.config.trailing_stop_enabled and not state.trailing_stop_activated:
            gain = (price - state.entry_price) / state.entry_price
            if gain >= self.config.trailing_stop_activation_pct:
                state.trailing_stop_activated = True
                state.trailing_stop_level = price * (1 - self.config.trailing_stop_distance_pct)
        if state.trailing_stop_activated:
            new_stop = price * (1 - self.config.trailing_stop_distance_pct)
            if new_stop > state.trailing_stop_level:
                state.trailing_stop_level = new_stop
