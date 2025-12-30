"""
Position sizing components.

Different sizing strategies can be plugged into trading bots
to control how much capital is allocated to each trade.
"""

import math
import logging
from abc import ABC, abstractmethod
from typing import Optional

from ...core.models import Signal
from ...core.config import RiskConfig

logger = logging.getLogger(__name__)


class PositionSizer(ABC):
    """
    Abstract base class for position sizing strategies.
    
    Position sizers determine how much capital to allocate
    to a given trading signal.
    """
    
    @abstractmethod
    def calculate_size(
        self,
        signal: Signal,
        available_capital: float,
        current_price: float
    ) -> float:
        """
        Calculate position size in USD.
        
        Args:
            signal: The trading signal
            available_capital: Capital available for this trade
            current_price: Current price of the asset
        
        Returns:
            Position size in USD (0 if trade should be skipped)
        """
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Name of this sizing strategy"""
        pass


class FixedSizer(PositionSizer):
    """
    Fixed position size.
    
    Always uses the same dollar amount per trade,
    capped by available capital.
    """
    
    def __init__(self, fixed_amount_usd: float = 50.0):
        self.fixed_amount = fixed_amount_usd
    
    @property
    def name(self) -> str:
        return "fixed"
    
    def calculate_size(
        self,
        signal: Signal,
        available_capital: float,
        current_price: float
    ) -> float:
        """Use fixed amount, capped by available capital"""
        return min(self.fixed_amount, available_capital)


class PercentageSizer(PositionSizer):
    """
    Percentage-based position sizing.
    
    Uses a fixed percentage of available capital per trade.
    """
    
    def __init__(self, percentage: float = 0.02):
        """
        Args:
            percentage: Fraction of capital per trade (e.g., 0.02 = 2%)
        """
        if not 0 < percentage <= 1:
            raise ValueError("Percentage must be between 0 and 1")
        self.percentage = percentage
    
    @property
    def name(self) -> str:
        return "percentage"
    
    def calculate_size(
        self,
        signal: Signal,
        available_capital: float,
        current_price: float
    ) -> float:
        """Use percentage of available capital"""
        return available_capital * self.percentage


class KellyPositionSizer(PositionSizer):
    """
    Kelly Criterion-based position sizing.
    
    Uses the Kelly formula to determine optimal position size
    based on expected edge and win probability.
    
    For binary options:
    - If price = p and we believe true probability = q
    - Edge = q - p
    - Kelly fraction = (p*b - (1-p)) / b where b = (1/p) - 1
    
    Uses fractional Kelly (default: half Kelly) for safety.
    """
    
    def __init__(
        self,
        kelly_fraction: float = 0.5,
        min_edge: float = 0.02,
        max_kelly: float = 0.25,
        price_range: tuple = (0.90, 0.99)
    ):
        """
        Args:
            kelly_fraction: Fraction of Kelly to use (0.5 = half Kelly)
            min_edge: Minimum edge required to trade
            max_kelly: Maximum Kelly fraction (cap)
            price_range: (min_price, max_price) for edge calculation
        """
        self.kelly_fraction = kelly_fraction
        self.min_edge = min_edge
        self.max_kelly = max_kelly
        self.price_range = price_range
    
    @property
    def name(self) -> str:
        return "kelly"
    
    def calculate_size(
        self,
        signal: Signal,
        available_capital: float,
        current_price: float
    ) -> float:
        """Calculate Kelly-optimal position size"""
        if current_price <= 0 or current_price >= 1:
            return 0.0
        
        # Calculate Kelly fraction
        kelly = self._calculate_kelly(current_price)
        
        if kelly <= 0:
            return 0.0
        
        # Apply fractional Kelly and cap
        adjusted_kelly = min(kelly * self.kelly_fraction, self.max_kelly)
        
        return available_capital * adjusted_kelly
    
    def _calculate_kelly(self, price: float) -> float:
        """Calculate raw Kelly fraction for a price"""
        min_price, max_price = self.price_range
        
        # Estimate true probability based on price
        # Assumption: prices near max are more likely to be correct
        edge_factor = (price - min_price) / (max_price - min_price)
        edge_factor = max(0, min(1, edge_factor))
        
        # Estimated true probability (higher than market price)
        # This assumes we have an edge on high-probability outcomes
        true_prob = price + (1 - price) * 0.5 * edge_factor
        
        # Binary option payoff
        p = true_prob  # Probability of winning
        q = 1 - p  # Probability of losing
        b = (1.0 / price) - 1  # Odds (payout ratio)
        
        if b <= 0:
            return 0.0
        
        # Kelly formula: (p*b - q) / b
        kelly = (p * b - q) / b
        
        return max(0.0, kelly)


class SignalScaledSizer(PositionSizer):
    """
    Signal-scaled position sizing.
    
    Base position size is scaled by signal strength.
    Stronger signals get larger positions.
    
    Formula:
        size = base_size * (signal_score / reference_score) * scale_factor
    
    Example with defaults:
        - Signal score 50 → 1x base size
        - Signal score 100 → 2x base size
        - Signal score 25 → 0.5x base size
    """
    
    def __init__(
        self,
        base_fraction: float = 0.02,
        reference_score: float = 50.0,
        scale_factor: float = 1.0,
        min_score: float = 20.0,
        max_multiplier: float = 3.0,
    ):
        """
        Args:
            base_fraction: Base position as fraction of capital
            reference_score: Score that results in 1x multiplier
            scale_factor: Overall scaling factor
            min_score: Minimum score to trade
            max_multiplier: Maximum size multiplier
        """
        self.base_fraction = base_fraction
        self.reference_score = reference_score
        self.scale_factor = scale_factor
        self.min_score = min_score
        self.max_multiplier = max_multiplier
    
    @property
    def name(self) -> str:
        return "signal_scaled"
    
    def calculate_size(
        self,
        signal: Signal,
        available_capital: float,
        current_price: float
    ) -> float:
        """Calculate position size scaled by signal strength"""
        if signal.score < self.min_score:
            return 0.0
        
        # Calculate multiplier from signal score
        multiplier = (signal.score / self.reference_score) * self.scale_factor
        multiplier = min(multiplier, self.max_multiplier)
        
        # Base position
        base_size = available_capital * self.base_fraction
        
        # Apply multiplier
        return base_size * multiplier


class CompositePositionSizer(PositionSizer):
    """
    Combines multiple sizing strategies.
    
    Takes the minimum of all component sizers for safety.
    """
    
    def __init__(self, sizers: list[PositionSizer]):
        if not sizers:
            raise ValueError("At least one sizer required")
        self.sizers = sizers
    
    @property
    def name(self) -> str:
        return f"composite({', '.join(s.name for s in self.sizers)})"
    
    def calculate_size(
        self,
        signal: Signal,
        available_capital: float,
        current_price: float
    ) -> float:
        """Take minimum of all sizers"""
        sizes = [
            s.calculate_size(signal, available_capital, current_price)
            for s in self.sizers
        ]
        
        return min(sizes)


