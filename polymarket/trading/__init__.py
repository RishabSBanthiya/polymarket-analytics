"""
Trading package - Live trading infrastructure.

Contains:
- storage: Pluggable storage backends (SQLite, Redis)
- risk_coordinator: Multi-agent risk management
- components: Composable trading components (signals, sizers, executors)
- bot: Main TradingBot class
- safety: Circuit breakers, drawdown limits
"""

from .risk_coordinator import RiskCoordinator
from .safety import CircuitBreaker, DrawdownLimit, TradingHalt
from .bot import TradingBot

__all__ = [
    "RiskCoordinator",
    "CircuitBreaker",
    "DrawdownLimit",
    "TradingHalt",
    "TradingBot",
]


