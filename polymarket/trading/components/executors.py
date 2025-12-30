"""
Execution engine components.

Execution engines handle the actual placement of orders on the exchange.
Different engines use different strategies (market, limit, aggressive, etc.)
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING
from datetime import datetime, timezone

from ...core.models import Signal, ExecutionResult, OrderbookSnapshot, Side
from ...core.config import RiskConfig

if TYPE_CHECKING:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType

logger = logging.getLogger(__name__)


class ExecutionEngine(ABC):
    """
    Abstract base class for execution engines.
    
    Execution engines handle order placement and fill tracking.
    """
    
    @abstractmethod
    async def execute(
        self,
        client: "ClobClient",
        token_id: str,
        side: Side,
        size_usd: float,
        price: float,
        orderbook: Optional[OrderbookSnapshot] = None
    ) -> ExecutionResult:
        """
        Execute a trade.
        
        Args:
            client: CLOB client for placing orders
            token_id: Token to trade
            side: BUY or SELL
            size_usd: Target size in USD
            price: Target price
            orderbook: Current orderbook state (optional)
        
        Returns:
            ExecutionResult with fill details
        """
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Name of this execution engine"""
        pass


class AggressiveExecutor(ExecutionEngine):
    """
    Aggressive execution - takes best available price.
    
    For BUY: Takes best ask (immediate fill)
    For SELL: Takes best bid (immediate fill)
    
    Prioritizes fill over price.
    """
    
    def __init__(self, max_slippage: float = 0.02):
        self.max_slippage = max_slippage
    
    @property
    def name(self) -> str:
        return "aggressive"
    
    async def execute(
        self,
        client: "ClobClient",
        token_id: str,
        side: Side,
        size_usd: float,
        price: float,
        orderbook: Optional[OrderbookSnapshot] = None
    ) -> ExecutionResult:
        """Execute aggressively at best available price"""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        
        try:
            # Get current orderbook if not provided
            if orderbook is None:
                book = client.get_orderbook(token_id)
                if book:
                    asks = book.get("asks", [])
                    bids = book.get("bids", [])
                    best_ask = float(asks[0]["price"]) if asks else None
                    best_bid = float(bids[0]["price"]) if bids else None
                else:
                    best_ask = None
                    best_bid = None
            else:
                best_ask = orderbook.best_ask
                best_bid = orderbook.best_bid
            
            # Determine execution price
            if side == Side.BUY:
                if best_ask is None:
                    return ExecutionResult(
                        success=False,
                        error_message="No ask available"
                    )
                exec_price = best_ask
                clob_side = BUY
            else:
                if best_bid is None:
                    return ExecutionResult(
                        success=False,
                        error_message="No bid available"
                    )
                exec_price = best_bid
                clob_side = SELL
            
            # Check slippage
            if price > 0:
                slippage = abs(exec_price - price) / price
                if slippage > self.max_slippage:
                    return ExecutionResult(
                        success=False,
                        requested_price=price,
                        error_message=f"Slippage too high: {slippage:.1%}"
                    )
            
            # Calculate shares
            shares = size_usd / exec_price if exec_price > 0 else 0
            
            if shares <= 0:
                return ExecutionResult(
                    success=False,
                    error_message="Invalid share calculation"
                )
            
            # Create and place order
            order_args = OrderArgs(
                price=exec_price,
                size=shares,
                side=clob_side,
                token_id=token_id
            )
            
            signed_order = client.create_order(order_args)
            response = client.post_order(signed_order, OrderType.GTC)
            
            if not response.get("success"):
                return ExecutionResult(
                    success=False,
                    requested_shares=shares,
                    requested_price=exec_price,
                    error_message=response.get("errorMsg", "Order failed")
                )
            
            # Determine fill
            order_status = response.get("status", "").lower()
            order_id = response.get("orderID", "")
            
            if order_status in ["matched", "filled"]:
                # Immediate fill
                filled_shares = shares
                try:
                    taking_amount = response.get("takingAmount")
                    if taking_amount:
                        filled_shares = float(taking_amount)
                except:
                    pass
                
                return ExecutionResult(
                    success=True,
                    order_id=order_id,
                    filled_shares=filled_shares,
                    filled_price=exec_price,
                    requested_shares=shares,
                    requested_price=exec_price
                )
            else:
                # Order placed but not filled yet
                return ExecutionResult(
                    success=True,
                    order_id=order_id,
                    filled_shares=0.0,
                    filled_price=exec_price,
                    requested_shares=shares,
                    requested_price=exec_price
                )
                
        except Exception as e:
            logger.error(f"Execution error: {e}")
            return ExecutionResult(
                success=False,
                error_message=str(e)
            )


class LimitOrderExecutor(ExecutionEngine):
    """
    Limit order execution - places order at specified price.
    
    May not fill immediately; order sits on book until matched.
    """
    
    def __init__(self, price_offset: float = 0.001):
        """
        Args:
            price_offset: Offset from best price to place limit order
                         Positive = more aggressive (higher bid, lower ask)
        """
        self.price_offset = price_offset
    
    @property
    def name(self) -> str:
        return "limit"
    
    async def execute(
        self,
        client: "ClobClient",
        token_id: str,
        side: Side,
        size_usd: float,
        price: float,
        orderbook: Optional[OrderbookSnapshot] = None
    ) -> ExecutionResult:
        """Execute with limit order at or near target price"""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        
        try:
            # Get current orderbook if not provided
            if orderbook is None:
                book = client.get_orderbook(token_id)
                if book:
                    asks = book.get("asks", [])
                    bids = book.get("bids", [])
                    best_ask = float(asks[0]["price"]) if asks else None
                    best_bid = float(bids[0]["price"]) if bids else None
                else:
                    best_ask = None
                    best_bid = None
            else:
                best_ask = orderbook.best_ask
                best_bid = orderbook.best_bid
            
            # Determine limit price
            if side == Side.BUY:
                if best_bid:
                    # Place slightly above best bid
                    exec_price = min(price, best_bid + self.price_offset)
                else:
                    exec_price = price
                clob_side = BUY
            else:
                if best_ask:
                    # Place slightly below best ask
                    exec_price = max(price, best_ask - self.price_offset)
                else:
                    exec_price = price
                clob_side = SELL
            
            # Calculate shares
            shares = size_usd / exec_price if exec_price > 0 else 0
            
            if shares <= 0:
                return ExecutionResult(
                    success=False,
                    error_message="Invalid share calculation"
                )
            
            # Create and place order
            order_args = OrderArgs(
                price=exec_price,
                size=shares,
                side=clob_side,
                token_id=token_id
            )
            
            signed_order = client.create_order(order_args)
            response = client.post_order(signed_order, OrderType.GTC)
            
            if not response.get("success"):
                return ExecutionResult(
                    success=False,
                    requested_shares=shares,
                    requested_price=exec_price,
                    error_message=response.get("errorMsg", "Order failed")
                )
            
            order_id = response.get("orderID", "")
            order_status = response.get("status", "").lower()
            
            # Check for immediate fill
            if order_status in ["matched", "filled"]:
                filled_shares = shares
                try:
                    taking_amount = response.get("takingAmount")
                    if taking_amount:
                        filled_shares = float(taking_amount)
                except:
                    pass
                
                return ExecutionResult(
                    success=True,
                    order_id=order_id,
                    filled_shares=filled_shares,
                    filled_price=exec_price,
                    requested_shares=shares,
                    requested_price=exec_price
                )
            else:
                # Limit order on book
                return ExecutionResult(
                    success=True,
                    order_id=order_id,
                    filled_shares=0.0,
                    filled_price=exec_price,
                    requested_shares=shares,
                    requested_price=exec_price
                )
                
        except Exception as e:
            logger.error(f"Execution error: {e}")
            return ExecutionResult(
                success=False,
                error_message=str(e)
            )


class DryRunExecutor(ExecutionEngine):
    """
    Dry run executor - simulates execution without placing orders.
    
    Useful for testing and paper trading.
    """
    
    def __init__(self, fill_probability: float = 0.95):
        self.fill_probability = fill_probability
    
    @property
    def name(self) -> str:
        return "dry_run"
    
    async def execute(
        self,
        client: "ClobClient",
        token_id: str,
        side: Side,
        size_usd: float,
        price: float,
        orderbook: Optional[OrderbookSnapshot] = None
    ) -> ExecutionResult:
        """Simulate execution without placing real orders"""
        import random
        
        shares = size_usd / price if price > 0 else 0
        
        # Simulate fill with some probability
        if random.random() < self.fill_probability:
            # Simulate some slippage
            slippage = random.uniform(0, 0.01)
            if side == Side.BUY:
                filled_price = price * (1 + slippage)
            else:
                filled_price = price * (1 - slippage)
            
            logger.info(
                f"[DRY RUN] {side.value} {shares:.2f} shares @ ${filled_price:.4f}"
            )
            
            return ExecutionResult(
                success=True,
                order_id=f"dry_run_{datetime.now().timestamp()}",
                filled_shares=shares,
                filled_price=filled_price,
                requested_shares=shares,
                requested_price=price
            )
        else:
            logger.info(f"[DRY RUN] Order not filled (simulated)")
            return ExecutionResult(
                success=False,
                requested_shares=shares,
                requested_price=price,
                error_message="Simulated: order not filled"
            )


