"""
Backtest results and simulated trade dataclasses.

These classes track the results of backtesting runs and provide
comprehensive reporting with bias warnings.
"""

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict


@dataclass
class SimulatedTrade:
    """A simulated trade from backtesting"""
    market_question: str
    token_id: str
    token_outcome: str
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime]
    exit_price: Optional[float]
    shares: float
    cost: float
    proceeds: Optional[float]
    pnl: Optional[float]
    pnl_percent: Optional[float]
    resolved_to: Optional[float]  # 1.0 or 0.0
    held_to_resolution: bool
    reason: str  # Why trade was made
    
    @property
    def is_winner(self) -> bool:
        """Check if trade was profitable"""
        return self.pnl is not None and self.pnl > 0
    
    @property
    def is_complete(self) -> bool:
        """Check if trade has been exited"""
        return self.exit_time is not None


@dataclass
class BacktestResults:
    """Comprehensive backtest results with bias warnings"""
    
    # Configuration
    strategy_name: str
    start_date: datetime
    end_date: datetime
    initial_capital: float
    
    # Results
    final_capital: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    
    # Financial metrics
    total_pnl: float = 0.0
    total_fees: float = 0.0
    gross_pnl: float = 0.0
    
    # Risk metrics
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    peak_equity: float = 0.0
    
    # Trade list
    trades: List[SimulatedTrade] = field(default_factory=list)
    
    # Equity curve
    equity_curve: List[tuple] = field(default_factory=list)  # [(timestamp, equity), ...]
    
    # Bias warnings
    survivorship_bias_warning: bool = True  # Always true for closed markets
    look_ahead_bias_warning: bool = True    # Usually true without live orderbook
    execution_assumption_warning: bool = True  # Always true for simulated execution
    
    # Additional metadata
    markets_analyzed: int = 0
    markets_traded: int = 0
    
    @property
    def win_rate(self) -> float:
        """Win rate as percentage"""
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades
    
    @property
    def loss_rate(self) -> float:
        """Loss rate as percentage"""
        if self.total_trades == 0:
            return 0.0
        return self.losing_trades / self.total_trades
    
    @property
    def return_pct(self) -> float:
        """Total return as percentage"""
        if self.initial_capital <= 0:
            return 0.0
        return (self.final_capital - self.initial_capital) / self.initial_capital
    
    @property
    def avg_trade_pnl(self) -> float:
        """Average P&L per trade"""
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl / self.total_trades
    
    @property
    def avg_winner(self) -> float:
        """Average winning trade P&L"""
        winners = [t.pnl for t in self.trades if t.pnl and t.pnl > 0]
        return statistics.mean(winners) if winners else 0.0
    
    @property
    def avg_loser(self) -> float:
        """Average losing trade P&L"""
        losers = [t.pnl for t in self.trades if t.pnl and t.pnl < 0]
        return statistics.mean(losers) if losers else 0.0
    
    @property
    def profit_factor(self) -> float:
        """Gross profit / gross loss"""
        gross_profit = sum(t.pnl for t in self.trades if t.pnl and t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl and t.pnl < 0))
        
        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0.0
        return gross_profit / gross_loss
    
    @property
    def sharpe_ratio(self) -> Optional[float]:
        """Sharpe ratio (assuming 0% risk-free rate)"""
        if len(self.trades) < 10:
            return None
        
        returns = [t.pnl_percent for t in self.trades if t.pnl_percent is not None]
        if len(returns) < 2:
            return None
        
        try:
            mean_return = statistics.mean(returns)
            std_return = statistics.stdev(returns)
            
            if std_return == 0:
                return None
            
            # Annualize (assuming ~250 trading days)
            return (mean_return * 250) / (std_return * (250 ** 0.5))
        except statistics.StatisticsError:
            return None
    
    def add_trade(self, trade: SimulatedTrade):
        """Add a trade and update statistics"""
        self.trades.append(trade)
        self.total_trades += 1
        
        if trade.pnl:
            self.total_pnl += trade.pnl
            if trade.pnl > 0:
                self.winning_trades += 1
            else:
                self.losing_trades += 1
    
    def finalize(self):
        """Calculate final metrics after all trades added"""
        self.final_capital = self.initial_capital + self.total_pnl - self.total_fees
        
        # Calculate max drawdown from equity curve
        if self.equity_curve:
            peak = self.initial_capital
            max_dd = 0.0
            
            for _, equity in self.equity_curve:
                if equity > peak:
                    peak = equity
                dd = peak - equity
                if dd > max_dd:
                    max_dd = dd
            
            self.max_drawdown = max_dd
            self.peak_equity = peak
            
            if peak > 0:
                self.max_drawdown_pct = max_dd / peak
    
    def print_report(self):
        """Print comprehensive results report"""
        print("\n" + "="*60)
        print(f"BACKTEST RESULTS: {self.strategy_name}")
        print("="*60)
        
        print(f"\nPeriod: {self.start_date.date()} to {self.end_date.date()}")
        print(f"Markets Analyzed: {self.markets_analyzed}")
        print(f"Markets Traded: {self.markets_traded}")
        
        print("\n--- PERFORMANCE ---")
        print(f"Initial Capital:  ${self.initial_capital:,.2f}")
        print(f"Final Capital:    ${self.final_capital:,.2f}")
        print(f"Total P&L:        ${self.total_pnl:,.2f} ({self.return_pct:.1%})")
        print(f"Fees Paid:        ${self.total_fees:,.2f}")
        
        print("\n--- TRADES ---")
        print(f"Total Trades:     {self.total_trades}")
        print(f"Winning Trades:   {self.winning_trades} ({self.win_rate:.1%})")
        print(f"Losing Trades:    {self.losing_trades} ({self.loss_rate:.1%})")
        print(f"Avg Trade P&L:    ${self.avg_trade_pnl:.2f}")
        print(f"Avg Winner:       ${self.avg_winner:.2f}")
        print(f"Avg Loser:        ${self.avg_loser:.2f}")
        print(f"Profit Factor:    {self.profit_factor:.2f}")
        
        print("\n--- RISK ---")
        print(f"Max Drawdown:     ${self.max_drawdown:,.2f} ({self.max_drawdown_pct:.1%})")
        print(f"Peak Equity:      ${self.peak_equity:,.2f}")
        
        sharpe = self.sharpe_ratio
        if sharpe is not None:
            print(f"Sharpe Ratio:     {sharpe:.2f}")
        else:
            print("Sharpe Ratio:     N/A (insufficient data)")
        
        # BIAS WARNINGS
        print("\n" + "!"*60)
        print("IMPORTANT WARNINGS")
        print("!"*60)
        
        if self.survivorship_bias_warning:
            print("\n⚠️  SURVIVORSHIP BIAS:")
            print("   Only resolved markets were analyzed.")
            print("   Cancelled/disputed markets not included.")
        
        if self.look_ahead_bias_warning:
            print("\n⚠️  LOOK-AHEAD BIAS:")
            print("   Historical orderbook data not available.")
            print("   Execution prices are estimates only.")
        
        if self.execution_assumption_warning:
            print("\n⚠️  EXECUTION OPTIMISM:")
            print("   Assumes fills at quoted/estimated prices.")
            print("   Real slippage may be significantly higher.")
            print("   Large orders may move the market.")
        
        print("\n" + "="*60)
        print("⚠️  ACTUAL RESULTS MAY VARY SIGNIFICANTLY")
        print("="*60 + "\n")
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON export"""
        return {
            "strategy_name": self.strategy_name,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "initial_capital": self.initial_capital,
            "final_capital": self.final_capital,
            "total_pnl": self.total_pnl,
            "return_pct": self.return_pct,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": self.win_rate,
            "avg_trade_pnl": self.avg_trade_pnl,
            "profit_factor": self.profit_factor,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_pct": self.max_drawdown_pct,
            "sharpe_ratio": self.sharpe_ratio,
            "total_fees": self.total_fees,
            "markets_analyzed": self.markets_analyzed,
            "markets_traded": self.markets_traded,
            "warnings": {
                "survivorship_bias": self.survivorship_bias_warning,
                "look_ahead_bias": self.look_ahead_bias_warning,
                "execution_assumption": self.execution_assumption_warning,
            }
        }

