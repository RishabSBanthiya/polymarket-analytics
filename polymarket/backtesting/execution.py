"""
Simulated execution with realistic assumptions.

Provides execution simulation with:
- Transaction fees
- Slippage modeling
- Liquidity constraints
"""

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from ..core.models import OrderbookSnapshot, Side

logger = logging.getLogger(__name__)


@dataclass
class SimulatedExecution:
    """
    Simulates order execution with realistic assumptions.
    
    Features:
    - Transaction fees (Polymarket: 0.01%)
    - Buy/sell slippage
    - Liquidity constraints from orderbook
    """
    
    transaction_fee_pct: float = 0.0001  # 0.01%
    buy_slippage_pct: float = 0.01       # 1%
    sell_slippage_pct: float = 0.01      # 1%
    
    def execute_buy(
        self,
        price: float,
        size_shares: float,
        orderbook: Optional[OrderbookSnapshot] = None
    ) -> Tuple[float, float, float]:
        """
        Simulate buy execution.
        
        Args:
            price: Target/market price
            size_shares: Number of shares to buy
            orderbook: Current orderbook state (optional)
        
        Returns:
            Tuple of (execution_price, filled_shares, fee)
        """
        # Apply slippage
        exec_price = price * (1 + self.buy_slippage_pct)
        
        # Check liquidity if orderbook provided
        filled_shares = size_shares
        if orderbook and orderbook.ask_depth:
            available = self._get_available_liquidity(orderbook.ask_depth, exec_price)
            filled_shares = min(size_shares, available)
        
        # Calculate fee
        trade_value = filled_shares * exec_price
        fee = trade_value * self.transaction_fee_pct
        
        return exec_price, filled_shares, fee
    
    def execute_sell(
        self,
        price: float,
        size_shares: float,
        orderbook: Optional[OrderbookSnapshot] = None
    ) -> Tuple[float, float, float]:
        """
        Simulate sell execution.
        
        Args:
            price: Target/market price
            size_shares: Number of shares to sell
            orderbook: Current orderbook state (optional)
        
        Returns:
            Tuple of (execution_price, filled_shares, fee)
        """
        # Apply slippage
        exec_price = price * (1 - self.sell_slippage_pct)
        
        # Check liquidity if orderbook provided
        filled_shares = size_shares
        if orderbook and orderbook.bid_depth:
            available = self._get_available_liquidity(orderbook.bid_depth, exec_price)
            filled_shares = min(size_shares, available)
        
        # Calculate fee
        trade_value = filled_shares * exec_price
        fee = trade_value * self.transaction_fee_pct
        
        return exec_price, filled_shares, fee
    
    def _get_available_liquidity(
        self,
        depth: list,
        max_price: float
    ) -> float:
        """
        Calculate available liquidity up to max price.
        
        Args:
            depth: List of (price, size) tuples
            max_price: Maximum price willing to pay/accept
        
        Returns:
            Total available shares
        """
        total = 0.0
        for price, size in depth:
            if price <= max_price:
                total += size
            else:
                break
        return total
    
    def estimate_impact(
        self,
        side: Side,
        size_shares: float,
        orderbook: OrderbookSnapshot
    ) -> Optional[float]:
        """
        Estimate price impact of an order.
        
        Args:
            side: BUY or SELL
            size_shares: Order size
            orderbook: Current orderbook
        
        Returns:
            Estimated execution price, or None if insufficient liquidity
        """
        if side == Side.BUY:
            depth = orderbook.ask_depth
        else:
            depth = orderbook.bid_depth
        
        if not depth:
            return None
        
        remaining = size_shares
        total_cost = 0.0
        
        for price, size in depth:
            if remaining <= 0:
                break
            
            fill = min(remaining, size)
            total_cost += fill * price
            remaining -= fill
        
        if remaining > 0:
            # Insufficient liquidity
            return None
        
        return total_cost / size_shares


class RealisticExecution(SimulatedExecution):
    """
    More realistic execution simulation.
    
    Adds:
    - Execution delay modeling
    - Market impact estimation
    - Partial fill simulation
    """
    
    def __init__(
        self,
        transaction_fee_pct: float = 0.0001,
        buy_slippage_pct: float = 0.01,
        sell_slippage_pct: float = 0.01,
        fill_probability: float = 0.95,
        partial_fill_probability: float = 0.10,
    ):
        super().__init__(transaction_fee_pct, buy_slippage_pct, sell_slippage_pct)
        self.fill_probability = fill_probability
        self.partial_fill_probability = partial_fill_probability
    
    def execute_buy(
        self,
        price: float,
        size_shares: float,
        orderbook: Optional[OrderbookSnapshot] = None
    ) -> Tuple[float, float, float]:
        """Execute buy with realistic fill simulation"""
        import random
        
        # Check if order fills at all
        if random.random() > self.fill_probability:
            return price, 0.0, 0.0
        
        exec_price, filled_shares, fee = super().execute_buy(price, size_shares, orderbook)
        
        # Check for partial fill
        if random.random() < self.partial_fill_probability:
            fill_ratio = random.uniform(0.3, 0.9)
            filled_shares *= fill_ratio
            fee *= fill_ratio
        
        return exec_price, filled_shares, fee
    
    def execute_sell(
        self,
        price: float,
        size_shares: float,
        orderbook: Optional[OrderbookSnapshot] = None
    ) -> Tuple[float, float, float]:
        """Execute sell with realistic fill simulation"""
        import random
        
        # Check if order fills at all
        if random.random() > self.fill_probability:
            return price, 0.0, 0.0
        
        exec_price, filled_shares, fee = super().execute_sell(price, size_shares, orderbook)
        
        # Check for partial fill
        if random.random() < self.partial_fill_probability:
            fill_ratio = random.uniform(0.3, 0.9)
            filled_shares *= fill_ratio
            fee *= fill_ratio
        
        return exec_price, filled_shares, fee

