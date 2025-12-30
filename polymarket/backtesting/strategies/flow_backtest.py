"""
Flow signal backtester.

Tests the predictive value of flow detection signals.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict
from collections import defaultdict

from ...core.models import Market, Token, Trade
from ..base import BaseBacktester
from ..results import BacktestResults, SimulatedTrade

logger = logging.getLogger(__name__)


# Signal weights (same as live trading)
SIGNAL_WEIGHTS = {
    "SMART_MONEY_ACTIVITY": 30,
    "OVERSIZED_BET": 25,
    "COORDINATED_WALLETS": 25,
    "VOLUME_SPIKE": 10,
    "PRICE_ACCELERATION": 10,
}


class FlowBacktester(BaseBacktester):
    """
    Backtester for flow detection signals.
    
    Tests how predictive various signal types are of future price moves.
    """
    
    def __init__(
        self,
        initial_capital: float = 1000.0,
        days: int = 7,
        min_trade_size: float = 100.0,
        evaluation_windows: List[int] = None,
        **kwargs
    ):
        super().__init__(initial_capital, days, **kwargs)
        self.min_trade_size = min_trade_size
        self.evaluation_windows = evaluation_windows or [1, 5, 15, 30]  # minutes
        
        # Signal tracking
        self.signal_results: Dict[str, List[dict]] = defaultdict(list)
    
    @property
    def strategy_name(self) -> str:
        return "Flow Detection Signals"
    
    async def run_strategy(self, markets: List[Market]) -> BacktestResults:
        """Run the flow signal backtest"""
        start_date = datetime.now(timezone.utc) - timedelta(days=self.days)
        end_date = datetime.now(timezone.utc)
        
        results = BacktestResults(
            strategy_name=self.strategy_name,
            start_date=start_date,
            end_date=end_date,
            initial_capital=self.initial_capital,
        )
        
        for market in markets:
            if self.verbose:
                logger.info(f"Analyzing: {market.question[:50]}...")
            
            for token in market.tokens:
                await self._analyze_token_signals(market, token, results)
        
        # Finalize
        results.finalize()
        
        # Print signal analysis
        self._print_signal_analysis()
        
        return results
    
    async def _analyze_token_signals(
        self,
        market: Market,
        token: Token,
        results: BacktestResults
    ):
        """Analyze signals for a token"""
        # Fetch price history
        history = await self.fetch_price_history(token.token_id)
        
        if len(history) < 20:
            return
        
        # Fetch trades
        trades = await self.api.fetch_trades(token.token_id, limit=500)
        
        if len(trades) < 20:
            return
        
        # Detect signals from trade history
        signals = self._detect_signals_from_trades(trades, history)
        
        # Evaluate each signal
        for signal in signals:
            evaluation = self._evaluate_signal(signal, history)
            if evaluation:
                self.signal_results[signal["type"]].append(evaluation)
    
    def _detect_signals_from_trades(
        self,
        trades: List[Trade],
        history: List
    ) -> List[dict]:
        """Detect flow signals from trade history"""
        signals = []
        
        # Calculate baseline metrics
        if not trades:
            return signals
        
        avg_size = sum(t.value_usd for t in trades) / len(trades)
        
        # Track recent trades for pattern detection
        window_trades: Dict[str, List[Trade]] = defaultdict(list)
        
        for i, trade in enumerate(trades):
            # Oversized bet detection
            if trade.value_usd >= self.min_trade_size * 10:
                if trade.value_usd >= avg_size * 10:
                    signals.append({
                        "type": "OVERSIZED_BET",
                        "timestamp": trade.timestamp,
                        "price": trade.price,
                        "value": trade.value_usd,
                        "direction": "BUY" if trade.side.value == "BUY" else "SELL",
                    })
            
            # Volume spike detection (simple version)
            window_key = trade.timestamp.strftime("%Y-%m-%d-%H-%M")
            window_trades[window_key].append(trade)
            
            if len(window_trades[window_key]) >= 5:
                window_value = sum(t.value_usd for t in window_trades[window_key])
                if window_value >= avg_size * 20:
                    signals.append({
                        "type": "VOLUME_SPIKE",
                        "timestamp": trade.timestamp,
                        "price": trade.price,
                        "value": window_value,
                        "direction": "NEUTRAL",
                    })
            
            # Price acceleration detection
            if i >= 5:
                recent_prices = [trades[j].price for j in range(i-5, i+1)]
                if len(recent_prices) >= 5:
                    early_change = abs(recent_prices[2] - recent_prices[0])
                    late_change = abs(recent_prices[-1] - recent_prices[2])
                    
                    if early_change > 0 and late_change > early_change * 2:
                        direction = "BUY" if recent_prices[-1] > recent_prices[0] else "SELL"
                        signals.append({
                            "type": "PRICE_ACCELERATION",
                            "timestamp": trade.timestamp,
                            "price": trade.price,
                            "value": late_change,
                            "direction": direction,
                        })
        
        return signals
    
    def _evaluate_signal(self, signal: dict, history: List) -> Optional[dict]:
        """Evaluate a signal's predictive value"""
        if not history:
            return None
        
        # Find price at signal time
        signal_ts = signal["timestamp"].timestamp()
        signal_price = signal["price"]
        
        # Find future prices
        future_prices = {}
        for window in self.evaluation_windows:
            target_ts = signal_ts + (window * 60)
            
            # Find closest price point
            closest = None
            closest_diff = float('inf')
            
            for point in history:
                diff = abs(point.timestamp - target_ts)
                if diff < closest_diff:
                    closest_diff = diff
                    closest = point.price
            
            if closest and closest_diff < 300:  # Within 5 min of target
                future_prices[window] = closest
        
        if not future_prices:
            return None
        
        # Calculate returns
        returns = {}
        for window, price in future_prices.items():
            if signal_price > 0:
                returns[window] = (price - signal_price) / signal_price
        
        # Determine if predictive
        direction = signal.get("direction", "NEUTRAL")
        was_predictive = False
        
        if direction == "BUY":
            was_predictive = any(r > 0.01 for r in returns.values())
        elif direction == "SELL":
            was_predictive = any(r < -0.01 for r in returns.values())
        
        return {
            "type": signal["type"],
            "direction": direction,
            "price_at_signal": signal_price,
            "returns": returns,
            "was_predictive": was_predictive,
        }
    
    def _print_signal_analysis(self):
        """Print analysis of signal effectiveness"""
        print("\n" + "="*60)
        print("FLOW SIGNAL ANALYSIS")
        print("="*60)
        
        for signal_type, evaluations in self.signal_results.items():
            if not evaluations:
                continue
            
            print(f"\n--- {signal_type} ---")
            print(f"Total signals: {len(evaluations)}")
            
            predictive = [e for e in evaluations if e["was_predictive"]]
            print(f"Predictive: {len(predictive)} ({len(predictive)/len(evaluations):.1%})")
            
            # Average returns by window
            for window in self.evaluation_windows:
                returns = [e["returns"].get(window, 0) for e in evaluations if window in e["returns"]]
                if returns:
                    avg_return = sum(returns) / len(returns)
                    print(f"  {window}min avg return: {avg_return:.2%}")
        
        print("\n" + "="*60)


async def run_flow_backtest(
    initial_capital: float = 1000.0,
    days: int = 7,
    verbose: bool = False,
) -> BacktestResults:
    """Run flow signal backtest"""
    backtester = FlowBacktester(
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
    
    parser = argparse.ArgumentParser(description="Flow Signal Backtester")
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--verbose", action="store_true")
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    
    asyncio.run(run_flow_backtest(
        initial_capital=args.capital,
        days=args.days,
        verbose=args.verbose,
    ))

