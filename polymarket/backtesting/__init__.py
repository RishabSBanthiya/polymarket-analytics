"""
Backtesting package - Historical strategy testing infrastructure.

Contains:
- base: BaseBacktester class with common functionality
- results: BacktestResults and SimulatedTrade dataclasses
- execution: Simulated execution with fees and slippage
"""

from .base import BaseBacktester
from .results import BacktestResults, SimulatedTrade
from .execution import SimulatedExecution

__all__ = [
    "BaseBacktester",
    "BacktestResults",
    "SimulatedTrade",
    "SimulatedExecution",
]

