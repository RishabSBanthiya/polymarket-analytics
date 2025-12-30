"""
Core package - Shared infrastructure for Polymarket trading system.

Contains:
- models: Dataclasses for Market, Token, Position, Signal, etc.
- api: Async Polymarket API client
- config: Centralized configuration with validation
- rate_limiter: Shared rate limiter across all agents
"""

from .models import (
    Market,
    Token,
    Position,
    Signal,
    Trade,
    HistoricalPrice,
    OrderbookSnapshot,
    WalletState,
    AgentInfo,
)
from .config import Config, RiskConfig
from .rate_limiter import SharedRateLimiter

__all__ = [
    # Models
    "Market",
    "Token",
    "Position",
    "Signal",
    "Trade",
    "HistoricalPrice",
    "OrderbookSnapshot",
    "WalletState",
    "AgentInfo",
    # Config
    "Config",
    "RiskConfig",
    # Rate limiter
    "SharedRateLimiter",
]


