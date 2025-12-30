"""
Composable trading components.

These are pluggable components that can be mixed and matched
to create different trading strategies:

- SignalSource: Where trading signals come from
- PositionSizer: How to size positions
- ExecutionEngine: How to execute trades
"""

from .signals import SignalSource, ExpiringMarketSignals, FlowAlertSignals
from .sizers import PositionSizer, KellyPositionSizer, SignalScaledSizer, FixedSizer
from .executors import ExecutionEngine, AggressiveExecutor, LimitOrderExecutor

__all__ = [
    # Signal sources
    "SignalSource",
    "ExpiringMarketSignals",
    "FlowAlertSignals",
    # Position sizers
    "PositionSizer",
    "KellyPositionSizer",
    "SignalScaledSizer",
    "FixedSizer",
    # Execution engines
    "ExecutionEngine",
    "AggressiveExecutor",
    "LimitOrderExecutor",
]


