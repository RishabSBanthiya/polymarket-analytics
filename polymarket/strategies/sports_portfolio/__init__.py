"""
Sports Portfolio Strategy - ML-based negative correlation hedging.

Builds optimized portfolios of binary options within sports games
using ML-predicted correlations for hedging and variance reduction.
"""

from .models import (
    SportsGame,
    GameMarket,
    MarketType,
    CorrelationMatrix,
    PortfolioPosition,
    PortfolioAllocation,
)
from .config import SportsPortfolioConfig
from .game_aggregator import GameMarketAggregator
from .correlation_model import MLCorrelationModel
from .portfolio_optimizer import PortfolioOptimizer
from .scanner import SportsPortfolioScanner
from .data_collector import SportsDataCollector
from .trainer import SportSpecificTrainer
from .backtest import SportsPortfolioBacktester, BacktestResult

__all__ = [
    "SportsGame",
    "GameMarket",
    "MarketType",
    "CorrelationMatrix",
    "PortfolioPosition",
    "PortfolioAllocation",
    "SportsPortfolioConfig",
    "GameMarketAggregator",
    "MLCorrelationModel",
    "PortfolioOptimizer",
    "SportsPortfolioScanner",
    "SportsDataCollector",
    "SportSpecificTrainer",
    "SportsPortfolioBacktester",
    "BacktestResult",
]
