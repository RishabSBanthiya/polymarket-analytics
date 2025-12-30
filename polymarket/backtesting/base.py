"""
Base backtester class with common functionality.

Provides:
- Historical data fetching
- Market preparation
- Kelly criterion sizing
- Liquidity estimation
- Result tracking
"""

import asyncio
import aiohttp
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple

from ..core.models import Market, Token, HistoricalPrice, OrderbookSnapshot
from ..core.config import Config, get_config
from ..core.api import PolymarketAPI
from .results import BacktestResults, SimulatedTrade
from .execution import SimulatedExecution

logger = logging.getLogger(__name__)


class BaseBacktester(ABC):
    """
    Abstract base class for backtesting strategies.
    
    Subclasses implement the specific strategy logic in run_strategy().
    """
    
    def __init__(
        self,
        initial_capital: float = 1000.0,
        days: int = 7,
        config: Optional[Config] = None,
        verbose: bool = False,
    ):
        """
        Initialize backtester.
        
        Args:
            initial_capital: Starting capital in USD
            days: Number of days to backtest
            config: Configuration (uses default if not provided)
            verbose: Enable verbose logging
        """
        self.initial_capital = initial_capital
        self.days = days
        self.config = config or get_config()
        self.verbose = verbose
        
        # State
        self.cash = initial_capital
        self.positions: Dict[str, Tuple[float, float, datetime]] = {}  # token_id -> (shares, price, time)
        
        # Execution simulator
        self.execution = SimulatedExecution()
        
        # API client
        self.api: Optional[PolymarketAPI] = None
        
        # Results
        self.results: Optional[BacktestResults] = None
    
    @property
    @abstractmethod
    def strategy_name(self) -> str:
        """Name of the strategy being backtested"""
        pass
    
    @abstractmethod
    async def run_strategy(self, markets: List[Market]) -> BacktestResults:
        """
        Run the backtest strategy.
        
        Args:
            markets: List of markets to backtest on
        
        Returns:
            BacktestResults with all trade details
        """
        pass
    
    async def run(self) -> BacktestResults:
        """
        Main entry point for running backtest.
        
        Fetches data and runs the strategy.
        """
        logger.info(f"Starting backtest: {self.strategy_name}")
        logger.info(f"Capital: ${self.initial_capital:,.2f}, Days: {self.days}")
        
        # Initialize API
        self.api = PolymarketAPI(self.config)
        await self.api.connect()
        
        try:
            # Fetch closed markets
            logger.info("Fetching closed markets...")
            raw_markets = await self.api.fetch_closed_markets(days=self.days)
            logger.info(f"Found {len(raw_markets)} closed markets")
            
            # Prepare markets
            markets = []
            for raw in raw_markets:
                market = await self.prepare_market(raw)
                if market:
                    markets.append(market)
            
            logger.info(f"Prepared {len(markets)} markets for backtesting")
            
            # Run strategy
            self.results = await self.run_strategy(markets)
            self.results.markets_analyzed = len(markets)
            
            return self.results
            
        finally:
            await self.api.close()
    
    async def prepare_market(self, raw_market: dict) -> Optional[Market]:
        """
        Prepare a market for backtesting.
        
        Fetches price history and determines winner.
        """
        market = self.api.parse_market(raw_market)
        if not market:
            return None
        
        # Determine winning outcome
        winning_outcome = None
        for token in market.tokens:
            # Fetch final price to determine winner
            history = await self.api.fetch_price_history(token.token_id, interval="max")
            if history:
                final_price = history[-1].price if history else 0
                if final_price > 0.9:  # Resolved to YES
                    winning_outcome = token.outcome
                    break
        
        market.winning_outcome = winning_outcome
        market.resolved = True
        
        return market
    
    async def fetch_price_history(
        self,
        token_id: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None
    ) -> List[HistoricalPrice]:
        """Fetch price history for a token"""
        if start_ts and end_ts:
            return await self.api.fetch_price_history_range(token_id, start_ts, end_ts)
        else:
            return await self.api.fetch_price_history(token_id)
    
    async def fetch_orderbook(self, token_id: str) -> Optional[OrderbookSnapshot]:
        """
        Fetch orderbook for a token.
        
        Note: For closed markets, orderbook will be empty.
        """
        return await self.api.fetch_orderbook(token_id)
    
    def calculate_kelly_fraction(self, price: float) -> float:
        """
        Calculate Kelly Criterion fraction.
        
        For expiring market strategy where we believe high prices
        have high probability of resolving to $1.
        """
        if price <= 0 or price >= 1:
            return 0.0
        
        # Estimate edge based on price
        min_price = 0.90
        max_price = 0.99
        
        edge_factor = (price - min_price) / (max_price - min_price)
        edge_factor = max(0, min(1, edge_factor))
        
        # Estimated true probability
        true_prob = price + (1 - price) * 0.5 * edge_factor
        
        p = true_prob
        q = 1 - p
        b = (1.0 / price) - 1  # Odds
        
        if b <= 0:
            return 0.0
        
        # Kelly formula: (p*b - q) / b
        kelly = (p * b - q) / b
        
        # Half Kelly for safety
        kelly = kelly * 0.5
        
        return max(0.0, min(0.25, kelly))
    
    def calculate_position_size(
        self,
        price: float,
        available_cash: float
    ) -> Tuple[float, float]:
        """
        Calculate position size in dollars.
        
        Returns: (position_dollars, kelly_fraction)
        """
        kelly = self.calculate_kelly_fraction(price)
        
        if kelly <= 0:
            return 0.0, 0.0
        
        min_price = self.config.risk.min_trade_value_usd / 100  # Rough estimate
        max_price = 0.98
        
        # Scale by price proximity to max
        price_scale = (price - min_price) / (max_price - min_price)
        price_scale = max(0, min(1, price_scale))
        
        adjusted_fraction = kelly * (0.5 + 0.5 * price_scale)
        
        position_dollars = available_cash * adjusted_fraction
        
        if position_dollars < self.config.risk.min_trade_value_usd:
            return 0.0, 0.0
        
        return position_dollars, adjusted_fraction
    
    def estimate_liquidity(
        self,
        price_history: List[HistoricalPrice],
        target_price: float,
        max_slippage: float = 0.01
    ) -> float:
        """
        Estimate liquidity from price history.
        
        Very rough estimate based on price stability.
        """
        if len(price_history) < 10:
            return 100.0  # Base estimate
        
        # Calculate price volatility as proxy for liquidity
        recent_prices = [p.price for p in price_history[-20:]]
        
        if not recent_prices:
            return 100.0
        
        avg_price = sum(recent_prices) / len(recent_prices)
        volatility = sum(abs(p - avg_price) for p in recent_prices) / len(recent_prices)
        
        # Lower volatility = higher liquidity (rough heuristic)
        if volatility > 0:
            liquidity_estimate = 100.0 / (volatility * 10)
        else:
            liquidity_estimate = 1000.0
        
        return max(10.0, min(10000.0, liquidity_estimate))
    
    def check_spread(
        self,
        orderbook: Optional[OrderbookSnapshot]
    ) -> Tuple[Optional[float], Optional[float], float]:
        """
        Get bid, ask, and spread from orderbook.
        
        Returns: (best_bid, best_ask, spread_pct)
        """
        if not orderbook:
            return None, None, 0.0
        
        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask
        
        if not best_bid or not best_ask:
            return best_bid, best_ask, 0.0
        
        spread_pct = (best_ask - best_bid) / best_bid if best_bid > 0 else 0.0
        
        return best_bid, best_ask, spread_pct
    
    def record_trade(
        self,
        results: BacktestResults,
        market: Market,
        token: Token,
        entry_time: datetime,
        entry_price: float,
        shares: float,
        cost: float,
        exit_time: Optional[datetime],
        exit_price: Optional[float],
        reason: str
    ):
        """Record a trade in results"""
        pnl = None
        pnl_pct = None
        proceeds = None
        resolved_to = None
        held_to_resolution = False
        
        if exit_price is not None and shares > 0:
            proceeds = shares * exit_price
            pnl = proceeds - cost
            pnl_pct = pnl / cost if cost > 0 else 0.0
            
            if exit_price > 0.9:
                resolved_to = 1.0
                held_to_resolution = True
            elif exit_price < 0.1:
                resolved_to = 0.0
                held_to_resolution = True
        
        trade = SimulatedTrade(
            market_question=market.question[:100],
            token_id=token.token_id,
            token_outcome=token.outcome,
            entry_time=entry_time,
            entry_price=entry_price,
            exit_time=exit_time,
            exit_price=exit_price,
            shares=shares,
            cost=cost,
            proceeds=proceeds,
            pnl=pnl,
            pnl_percent=pnl_pct,
            resolved_to=resolved_to,
            held_to_resolution=held_to_resolution,
            reason=reason
        )
        
        results.add_trade(trade)

