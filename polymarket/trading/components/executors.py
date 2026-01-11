"""
Execution engine components.

Execution engines handle the actual placement of orders on the exchange.
Different engines use different strategies (market, limit, aggressive, etc.)
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, timezone

from ...core.models import (
    Signal, ExecutionResult, OrderbookSnapshot, Side,
    calculate_time_based_slippage_threshold,
    SignalLeg, MultiLegSignal, LegExecutionResult, MultiLegExecutionResult
)
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
        orderbook: Optional[OrderbookSnapshot] = None,
        original_signal_price: Optional[float] = None,
        market_lifetime_hours: Optional[float] = None
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
            original_signal_price: Original price from signal/alert (for drift check)
            market_lifetime_hours: Total market duration in hours (for time-based slippage)
        
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
    
    Includes safety checks:
    - Max spread check (default 3%): Reject if bid-ask spread is too wide
    - Max price slippage from signal (default 10%): Reject if price moved too much
    """
    
    def __init__(
        self, 
        max_slippage: float = 0.02,
        max_spread: float = 0.03,  # 3% max spread
        max_price_drift: float = 0.10  # 10% max price drift from original signal
    ):
        self.max_slippage = max_slippage
        self.max_spread = max_spread
        self.max_price_drift = max_price_drift
    
    @property
    def name(self) -> str:
        return "aggressive"
    
    def _calculate_spread(self, best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
        """Calculate bid-ask spread as a percentage"""
        if best_bid is None or best_ask is None:
            return None
        if best_bid <= 0:
            return None
        
        # Spread = (ask - bid) / midpoint
        midpoint = (best_ask + best_bid) / 2
        if midpoint <= 0:
            return None
        
        spread = (best_ask - best_bid) / midpoint
        return spread
    
    async def execute(
        self,
        client: "ClobClient",
        token_id: str,
        side: Side,
        size_usd: float,
        price: float,
        orderbook: Optional[OrderbookSnapshot] = None,
        original_signal_price: Optional[float] = None,  # Price from original flow alert
        market_lifetime_hours: Optional[float] = None  # For time-based slippage threshold
    ) -> ExecutionResult:
        """Execute aggressively at best available price"""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        
        try:
            # Get current orderbook if not provided
            if orderbook is None:
                book = client.get_order_book(token_id)
                if book:
                    # OrderBookSummary has .asks and .bids as lists of OrderSummary objects
                    # OrderSummary has .price and .size as string attributes
                    asks = book.asks if book.asks else []
                    bids = book.bids if book.bids else []
                    
                    # Parse and sort properly:
                    # - Bids: highest first (best bid = highest price buyer)
                    # - Asks: lowest first (best ask = lowest price seller)
                    bid_prices = sorted([float(b.price) for b in bids], reverse=True)
                    ask_prices = sorted([float(a.price) for a in asks], reverse=False)
                    
                    best_bid = bid_prices[0] if bid_prices else None
                    best_ask = ask_prices[0] if ask_prices else None
                else:
                    best_ask = None
                    best_bid = None
            else:
                best_ask = orderbook.best_ask
                best_bid = orderbook.best_bid
            
            # ============ SPREAD CHECK ============
            # Reject if bid-ask spread is too wide (poor liquidity)
            # NOTE: Only check spread for BUY orders - SELLs should always be able to exit
            # to reduce exposure, even with wide spreads (limit orders will be placed)
            spread = self._calculate_spread(best_bid, best_ask)
            if side == Side.BUY and spread is not None and spread > self.max_spread:
                logger.warning(
                    f"Spread too wide: {spread:.1%} > {self.max_spread:.1%} max. "
                    f"Bid: ${best_bid:.4f}, Ask: ${best_ask:.4f}"
                )
                return ExecutionResult(
                    success=False,
                    error_message=f"Spread too wide: {spread:.1%} (max {self.max_spread:.1%})",
                    is_rejection=True  # Protective rejection, not a system error
                )
            elif side == Side.SELL and spread is not None and spread > self.max_spread:
                # Log but allow SELLs to proceed - reducing exposure is priority
                bid_str = f"${best_bid:.4f}" if best_bid else "N/A"
                logger.info(
                    f"Wide spread ({spread:.1%}) but allowing SELL to reduce exposure. "
                    f"Bid: {bid_str}"
                )
            
            # ============ TIME-BASED SLIPPAGE CHECK ============
            # Use dynamic slippage threshold based on market lifetime
            # Short markets (< 30 min) need tight slippage, longer markets can tolerate more
            if original_signal_price is not None and original_signal_price > 0:
                current_price = best_ask if side == Side.BUY else best_bid
                if current_price:
                    price_drift = abs(current_price - original_signal_price) / original_signal_price
                    
                    # Calculate time-based slippage threshold
                    # Falls back to self.max_price_drift if market_lifetime_hours is None
                    if market_lifetime_hours is not None:
                        slippage_threshold = calculate_time_based_slippage_threshold(market_lifetime_hours)
                        logger.debug(
                            f"Time-based slippage: market lifetime {market_lifetime_hours:.2f}h -> "
                            f"threshold {slippage_threshold:.1%}"
                        )
                    else:
                        slippage_threshold = self.max_price_drift
                    
                    if price_drift > slippage_threshold:
                        logger.warning(
                            f"Price drifted too much from signal: {price_drift:.1%} > {slippage_threshold:.1%} max. "
                            f"Original: ${original_signal_price:.4f}, Current: ${current_price:.4f}"
                            + (f" (market lifetime: {market_lifetime_hours:.1f}h)" if market_lifetime_hours else "")
                        )
                        return ExecutionResult(
                            success=False,
                            requested_price=original_signal_price,
                            error_message=f"Price drifted {price_drift:.1%} from signal (max {slippage_threshold:.1%})",
                            is_rejection=True  # Protective rejection, not a system error
                        )
            
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
            
            # Check slippage from target price
            if price > 0:
                slippage = abs(exec_price - price) / price
                if slippage > self.max_slippage:
                    return ExecutionResult(
                        success=False,
                        requested_price=price,
                        error_message=f"Slippage too high: {slippage:.1%}",
                        is_rejection=True  # Protective rejection, not a system error
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
            
            # Handle response - can be dict or object
            def get_response_field(field: str, default=None):
                """Get field from response, handling both dict and object types."""
                if isinstance(response, dict):
                    return response.get(field, default)
                return getattr(response, field, default)
            
            success = get_response_field("success")
            if not success:
                error_msg = get_response_field("errorMsg") or get_response_field("error_msg") or "Order failed"
                return ExecutionResult(
                    success=False,
                    requested_shares=shares,
                    requested_price=exec_price,
                    error_message=error_msg
                )
            
            # Determine fill
            order_status = str(get_response_field("status", "") or "").lower()
            order_id = get_response_field("orderID") or get_response_field("order_id") or ""
            
            if order_status in ["matched", "filled"]:
                # Immediate fill
                filled_shares = shares
                try:
                    taking_amount = get_response_field("takingAmount") or get_response_field("taking_amount")
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
        orderbook: Optional[OrderbookSnapshot] = None,
        original_signal_price: Optional[float] = None,
        market_lifetime_hours: Optional[float] = None
    ) -> ExecutionResult:
        """Execute with limit order at or near target price"""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        
        try:
            # Get current orderbook if not provided
            if orderbook is None:
                book = client.get_order_book(token_id)
                if book:
                    # OrderBookSummary has .asks and .bids as lists of OrderSummary objects
                    # OrderSummary has .price and .size as string attributes
                    asks = book.asks if book.asks else []
                    bids = book.bids if book.bids else []
                    
                    # Parse and sort properly:
                    # - Bids: highest first (best bid = highest price buyer)
                    # - Asks: lowest first (best ask = lowest price seller)
                    bid_prices = sorted([float(b.price) for b in bids], reverse=True)
                    ask_prices = sorted([float(a.price) for a in asks], reverse=False)
                    
                    best_bid = bid_prices[0] if bid_prices else None
                    best_ask = ask_prices[0] if ask_prices else None
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
            
            # Handle response - can be dict or object
            def get_response_field(field: str, default=None):
                """Get field from response, handling both dict and object types."""
                if isinstance(response, dict):
                    return response.get(field, default)
                return getattr(response, field, default)
            
            success = get_response_field("success")
            if not success:
                error_msg = get_response_field("errorMsg") or get_response_field("error_msg") or "Order failed"
                return ExecutionResult(
                    success=False,
                    requested_shares=shares,
                    requested_price=exec_price,
                    error_message=error_msg
                )
            
            order_id = get_response_field("orderID") or get_response_field("order_id") or ""
            order_status = str(get_response_field("status", "") or "").lower()
            
            # Check for immediate fill
            if order_status in ["matched", "filled"]:
                filled_shares = shares
                try:
                    taking_amount = get_response_field("takingAmount") or get_response_field("taking_amount")
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
        orderbook: Optional[OrderbookSnapshot] = None,
        original_signal_price: Optional[float] = None,
        market_lifetime_hours: Optional[float] = None
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


class MultiLegExecutor(ExecutionEngine):
    """
    Executor for atomic multi-leg trades.

    Executes all legs sequentially with optional rollback on failure.
    Used by arb, stat-arb, and sports portfolio strategies.
    """

    def __init__(
        self,
        base_executor: Optional[ExecutionEngine] = None,
        rollback_on_failure: bool = True,
        max_leg_delay_seconds: float = 5.0
    ):
        """
        Args:
            base_executor: Executor to use for individual legs (default: AggressiveExecutor)
            rollback_on_failure: Whether to unwind filled legs if later legs fail
            max_leg_delay_seconds: Max time allowed between leg executions
        """
        self.base_executor = base_executor or AggressiveExecutor()
        self.rollback_on_failure = rollback_on_failure
        self.max_leg_delay_seconds = max_leg_delay_seconds

    @property
    def name(self) -> str:
        return f"multi_leg_{self.base_executor.name}"

    async def execute(
        self,
        client: "ClobClient",
        token_id: str,
        side: Side,
        size_usd: float,
        price: float,
        orderbook: Optional[OrderbookSnapshot] = None,
        original_signal_price: Optional[float] = None,
        market_lifetime_hours: Optional[float] = None
    ) -> ExecutionResult:
        """Single-leg execution - delegates to base executor"""
        return await self.base_executor.execute(
            client, token_id, side, size_usd, price,
            orderbook, original_signal_price, market_lifetime_hours
        )

    async def execute_multi_leg(
        self,
        client: "ClobClient",
        signal: MultiLegSignal,
        total_size_usd: float
    ) -> MultiLegExecutionResult:
        """
        Execute multiple legs atomically.

        Args:
            client: CLOB client for placing orders
            signal: MultiLegSignal containing all legs to execute
            total_size_usd: Total position size in USD (divided among legs)

        Returns:
            MultiLegExecutionResult with per-leg results
        """
        leg_results: List[LegExecutionResult] = []
        total_cost = 0.0
        total_shares = 0.0
        start_time = datetime.now(timezone.utc)

        for i, leg in enumerate(signal.legs):
            # Check for timeout between legs
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            if i > 0 and elapsed > self.max_leg_delay_seconds * i:
                logger.warning(
                    f"Multi-leg execution taking too long: {elapsed:.1f}s for {i} legs"
                )

            # Calculate size for this leg
            leg_size_usd = total_size_usd * leg.size_pct

            # Execute leg
            result = await self.base_executor.execute(
                client=client,
                token_id=leg.token_id,
                side=leg.side,
                size_usd=leg_size_usd,
                price=leg.target_price
            )

            leg_results.append(LegExecutionResult(
                leg_index=i,
                token_id=leg.token_id,
                result=result
            ))

            if result.success:
                total_cost += result.filled_shares * result.filled_price
                total_shares += result.filled_shares
                logger.info(
                    f"Leg {i+1}/{signal.num_legs} filled: "
                    f"{leg.side.value} {result.filled_shares:.2f} @ ${result.filled_price:.4f}"
                )
            else:
                logger.warning(
                    f"Leg {i+1}/{signal.num_legs} failed: {result.error_message}"
                )

                # Check if we need to rollback
                if self.rollback_on_failure and i > 0:
                    # Some legs already filled - need to unwind
                    return MultiLegExecutionResult(
                        success=False,
                        leg_results=leg_results,
                        total_cost=total_cost,
                        total_shares=total_shares,
                        error_message=f"Leg {i+1} failed: {result.error_message}",
                        needs_rollback=True
                    )
                else:
                    return MultiLegExecutionResult(
                        success=False,
                        leg_results=leg_results,
                        total_cost=total_cost,
                        total_shares=total_shares,
                        error_message=f"Leg {i+1} failed: {result.error_message}",
                        needs_rollback=False
                    )

        # All legs succeeded
        return MultiLegExecutionResult(
            success=True,
            leg_results=leg_results,
            total_cost=total_cost,
            total_shares=total_shares
        )

    async def rollback_position(
        self,
        client: "ClobClient",
        leg_results: List[LegExecutionResult]
    ) -> List[ExecutionResult]:
        """
        Attempt to unwind filled legs after a partial failure.

        Args:
            client: CLOB client
            leg_results: Results from the failed multi-leg execution

        Returns:
            List of rollback execution results
        """
        rollback_results = []

        for leg_result in leg_results:
            if not leg_result.success:
                continue

            # Reverse the trade
            original = leg_result.result
            reverse_side = Side.SELL if original.filled_shares > 0 else Side.BUY

            # Try to unwind at market
            rollback = await self.base_executor.execute(
                client=client,
                token_id=leg_result.token_id,
                side=reverse_side,
                size_usd=original.filled_shares * original.filled_price,
                price=original.filled_price
            )

            rollback_results.append(rollback)

            if rollback.success:
                logger.info(
                    f"Rolled back leg {leg_result.leg_index}: "
                    f"{reverse_side.value} {rollback.filled_shares:.2f}"
                )
            else:
                logger.error(
                    f"Failed to rollback leg {leg_result.leg_index}: "
                    f"{rollback.error_message}"
                )

        return rollback_results


class DryRunMultiLegExecutor(MultiLegExecutor):
    """Dry run version of multi-leg executor for testing"""

    def __init__(self, fill_probability: float = 0.95):
        super().__init__(
            base_executor=DryRunExecutor(fill_probability=fill_probability),
            rollback_on_failure=False  # No real rollback needed in dry run
        )

    @property
    def name(self) -> str:
        return "dry_run_multi_leg"


