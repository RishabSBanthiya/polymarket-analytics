"""
Bond strategy backtester.

Tests the expiring market strategy on historical data.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from ...core.models import Market, Token, HistoricalPrice
from ..base import BaseBacktester
from ..results import BacktestResults

logger = logging.getLogger(__name__)


class BondBacktester(BaseBacktester):
    """
    Backtester for the bond (expiring market) strategy.
    """
    
    def __init__(
        self,
        initial_capital: float = 1000.0,
        days: int = 7,
        min_price: float = 0.95,
        max_price: float = 0.98,
        min_seconds_left: int = 60,
        max_seconds_left: int = 1800,
        **kwargs
    ):
        super().__init__(initial_capital, days, **kwargs)
        self.min_price = min_price
        self.max_price = max_price
        self.min_seconds_left = min_seconds_left
        self.max_seconds_left = max_seconds_left
    
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
        """Find and simulate a trade opportunity"""
        if len(history) < 10:
            return None
        
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
            
            # Check if we have capital
            if self.cash < self.config.risk.min_trade_value_usd:
                break
            
            # Calculate position size
            position_dollars, kelly = self.calculate_position_size(point.price, self.cash)
            
            if position_dollars <= 0:
                continue
            
            # Simulate execution
            exec_price, filled_shares, fee = self.execution.execute_buy(
                point.price,
                position_dollars / point.price,
                None
            )
            
            if filled_shares <= 0:
                continue
            
            cost = filled_shares * exec_price + fee
            
            if cost > self.cash:
                continue
            
            # Execute trade
            self.cash -= cost
            results.total_fees += fee
            
            # Find exit (end of history = resolution)
            exit_price = history[-1].price
            exit_time = history[-1].datetime
            entry_time = point.datetime
            
            # Exit simulation
            _, exit_shares, exit_fee = self.execution.execute_sell(
                exit_price,
                filled_shares,
                None
            )
            
            results.total_fees += exit_fee
            proceeds = exit_shares * exit_price - exit_fee
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
                exit_price=exit_price,
                reason=f"Bond trade @ {point.price:.2%}"
            )
            
            if self.verbose:
                pnl = proceeds - cost
                logger.info(
                    f"  Trade: {token.outcome} {filled_shares:.2f} @ ${exec_price:.4f} -> "
                    f"${exit_price:.4f} P&L: ${pnl:.2f}"
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

