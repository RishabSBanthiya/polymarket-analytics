"""
Bond strategy backtester.

Tests the expiring market strategy on historical data.

Uses realistic execution with:
- No fees (Polymarket has no fees)
- Spread checks
- Slippage based on liquidity
- Position size limits
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
from collections import defaultdict

from ...core.models import Market, Token, HistoricalPrice
from ..base import BaseBacktester
from ..results import BacktestResults
from ..execution import RealisticExecution

logger = logging.getLogger(__name__)


class BondBacktester(BaseBacktester):
    """
    Backtester for the bond (expiring market) strategy.
    
    Uses realistic execution:
    - No transaction fees
    - Slippage modeling based on liquidity
    - Spread checks before trading
    """
    
    def __init__(
        self,
        initial_capital: float = 1000.0,
        days: int = 7,
        min_price: float = 0.95,
        max_price: float = 0.98,
        min_seconds_left: int = 60,
        max_seconds_left: int = 1800,
        max_spread_pct: float = 0.02,  # 2% max spread for bonds
        **kwargs
    ):
        super().__init__(initial_capital, days, **kwargs)
        self.min_price = min_price
        self.max_price = max_price
        self.min_seconds_left = min_seconds_left
        self.max_seconds_left = max_seconds_left
        self.max_spread_pct = max_spread_pct
        
        # Use realistic execution (no fees, slippage/spread checks)
        self.execution = RealisticExecution(
            max_spread_pct=max_spread_pct,
            buy_slippage_pct=0.003,  # Lower slippage for near-expiry markets
            sell_slippage_pct=0.003,
        )
        
        # Track liquidity estimates
        self._liquidity_cache: Dict[str, float] = {}
    
    @property
    def strategy_name(self) -> str:
        return f"Bond Strategy ({self.min_price:.0%}-{self.max_price:.0%})"
    
    async def run_strategy(self, markets: List[Market]) -> BacktestResults:
        """Run the bond strategy backtest"""
        start_date = datetime.now(timezone.utc) - timedelta(days=self.days)
        end_date = datetime.now(timezone.utc)
        
        results = BacktestResults(
            strategy_name=self.strategy_name,
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
        )
        
        # Track equity over time
        equity_history = [(start_date, self.initial_capital)]
        
        markets_traded = 0
        
        for market in markets:
            if self.verbose:
                logger.info(f"Analyzing: {market.question[:50]}...")
            
            for token in market.tokens:
                # Fetch price history
                history = await self.fetch_price_history(token.token_id)
                
                if not history:
                    continue
                
                # Find entry opportunities
                trade = await self._find_trade_opportunity(
                    market, token, history, results
                )
                
                if trade:
                    markets_traded += 1
                    equity_history.append((trade.entry_time, self.cash))
        
        results.markets_traded = markets_traded
        results.equity_curve = equity_history
        results.finalize()
        
        return results
    
    async def _find_trade_opportunity(
        self,
        market: Market,
        token: Token,
        history: List[HistoricalPrice],
        results: BacktestResults
    ) -> Optional[object]:
        """Find and simulate a trade opportunity with realistic execution"""
        if len(history) < 10:
            return None
        
        # Estimate liquidity from price stability
        liquidity_estimate = self.estimate_liquidity(history, self.min_price)
        self._liquidity_cache[token.token_id] = liquidity_estimate
        
        # Look for entry points
        for i, point in enumerate(history):
            # Check price range
            if not (self.min_price <= point.price <= self.max_price):
                continue
            
            # Estimate time to expiry at this point
            # (rough estimate based on position in history)
            position_ratio = i / len(history)
            if position_ratio < 0.8:  # Only trade in last 20% of market life
                continue
            
            # Estimate spread from price volatility
            recent_prices = [p.price for p in history[max(0, i-10):i+1]]
            if len(recent_prices) >= 2:
                price_range = max(recent_prices) - min(recent_prices)
                estimated_spread = price_range / point.price if point.price > 0 else 0.10
            else:
                estimated_spread = 0.02  # Default 2%
            
            # Check spread acceptability
            if estimated_spread > self.max_spread_pct:
                if self.verbose:
                    logger.debug(f"  Skipped: spread {estimated_spread:.1%} > {self.max_spread_pct:.1%}")
                continue
            
            # Check if we have capital
            if self.cash < self.config.risk.min_trade_value_usd:
                break
            
            # Calculate position size
            position_dollars, kelly = self.calculate_position_size(point.price, self.cash)
            
            if position_dollars <= 0:
                continue
            
            # Cap position by estimated liquidity (max 10% of available)
            max_position = liquidity_estimate * 0.10
            position_dollars = min(position_dollars, max_position)
            
            if position_dollars < self.config.risk.min_trade_value_usd:
                continue
            
            # Simulate execution with liquidity-based slippage
            exec_price, filled_shares, fee = self.execution.execute_buy(
                point.price,
                position_dollars / point.price,
                None,
                liquidity_usd=liquidity_estimate
            )
            
            if filled_shares <= 0:
                continue
            
            cost = filled_shares * exec_price  # No fee
            
            if cost > self.cash:
                continue
            
            # Execute trade
            self.cash -= cost
            
            # Find exit (end of history = resolution)
            exit_price = history[-1].price
            exit_time = history[-1].datetime
            entry_time = point.datetime
            
            # Exit simulation (resolved markets have no slippage at $1.00)
            if exit_price > 0.99:
                # Resolved to YES - get $1.00 per share
                actual_exit_price = 1.0
                exit_shares = filled_shares
            elif exit_price < 0.01:
                # Resolved to NO - get $0.00 per share
                actual_exit_price = 0.0
                exit_shares = filled_shares
            else:
                # Still trading - apply exit slippage
                actual_exit_price, exit_shares, _ = self.execution.execute_sell(
                    exit_price,
                    filled_shares,
                    None,
                    liquidity_usd=liquidity_estimate
                )
            
            proceeds = exit_shares * actual_exit_price
            self.cash += proceeds
            
            # Record trade
            self.record_trade(
                results=results,
                market=market,
                token=token,
                entry_time=entry_time,
                entry_price=exec_price,
                shares=filled_shares,
                cost=cost,
                exit_time=exit_time,
                exit_price=actual_exit_price,
                reason=f"Bond @ {point.price:.2%} (spread: {estimated_spread:.1%})"
            )
            
            if self.verbose:
                pnl = proceeds - cost
                logger.info(
                    f"  Trade: {token.outcome} {filled_shares:.2f} @ ${exec_price:.4f} -> "
                    f"${actual_exit_price:.4f} P&L: ${pnl:.2f} (liq: ${liquidity_estimate:.0f})"
                )
            
            return results.trades[-1]
        
        return None


async def run_bond_backtest(
    initial_capital: float = 1000.0,
    days: int = 7,
    verbose: bool = False,
) -> BacktestResults:
    """Run bond strategy backtest"""
    backtester = BondBacktester(
        initial_capital=initial_capital,
        days=days,
        verbose=verbose,
    )
    
    results = await backtester.run()
    results.print_report()
    
    return results


if __name__ == "__main__":
    import asyncio
    import argparse
    
    parser = argparse.ArgumentParser(description="Bond Strategy Backtester")
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--verbose", action="store_true")
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    
    asyncio.run(run_bond_backtest(
        initial_capital=args.capital,
        days=args.days,
        verbose=args.verbose,
    ))

