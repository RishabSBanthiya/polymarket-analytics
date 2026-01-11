"""
Position manager components for multi-leg strategies.

Position managers handle the lifecycle of complex positions that span
multiple tokens or markets (arb, stat-arb, sports portfolio).
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from enum import Enum

from ...core.models import (
    MultiLegSignal, MultiLegExecutionResult, SignalLeg,
    Position, PositionStatus, Side
)

logger = logging.getLogger(__name__)


class MultiLegPositionStatus(Enum):
    """Status of a multi-leg position"""
    PENDING = "pending"  # Signal received, not yet executed
    PARTIAL = "partial"  # Some legs filled, awaiting others
    OPEN = "open"  # All legs filled, position active
    CLOSING = "closing"  # Exit in progress
    CLOSED = "closed"  # All legs closed
    FAILED = "failed"  # Execution failed, may need rollback
    EXPIRED = "expired"  # Market resolved


@dataclass
class MultiLegPosition:
    """
    A position spanning multiple legs (tokens/markets).

    Used by arb, stat-arb, and sports portfolio strategies.
    """
    position_id: str
    signal: MultiLegSignal
    status: MultiLegPositionStatus = MultiLegPositionStatus.PENDING
    legs: List[Position] = field(default_factory=list)
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    total_cost: float = 0.0
    total_proceeds: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status == MultiLegPositionStatus.OPEN

    @property
    def is_closed(self) -> bool:
        return self.status in (MultiLegPositionStatus.CLOSED, MultiLegPositionStatus.EXPIRED)

    @property
    def pnl(self) -> float:
        """Realized P&L if closed, unrealized if open"""
        if self.is_closed:
            return self.total_proceeds - self.total_cost
        # For open positions, sum up unrealized P&L of each leg
        return sum(leg.unrealized_pnl for leg in self.legs)

    @property
    def pnl_pct(self) -> float:
        """P&L as percentage of cost"""
        if self.total_cost > 0:
            return self.pnl / self.total_cost
        return 0.0

    @property
    def hold_duration_seconds(self) -> float:
        """Duration position has been held"""
        if self.entry_time is None:
            return 0.0
        end = self.exit_time or datetime.now(timezone.utc)
        return (end - self.entry_time).total_seconds()


class PositionManager(ABC):
    """
    Abstract base class for multi-leg position managers.

    Handles opening, monitoring, and closing multi-leg positions.
    """

    @abstractmethod
    async def open_position(
        self,
        signal: MultiLegSignal,
        capital: float
    ) -> Optional[MultiLegPosition]:
        """
        Open a new multi-leg position.

        Args:
            signal: MultiLegSignal with all legs to execute
            capital: Total capital to allocate to this position

        Returns:
            MultiLegPosition if successful, None if failed
        """
        pass

    @abstractmethod
    async def close_position(
        self,
        position_id: str,
        reason: str = "manual"
    ) -> Optional[float]:
        """
        Close an open position.

        Args:
            position_id: ID of position to close
            reason: Reason for closing (e.g., "take_profit", "stop_loss", "manual")

        Returns:
            Realized P&L if closed, None if failed
        """
        pass

    @abstractmethod
    async def update_positions(self) -> Dict[str, MultiLegPosition]:
        """
        Update status and prices of all open positions.

        Returns:
            Dict of position_id -> updated position
        """
        pass

    @abstractmethod
    def get_open_positions(self) -> List[MultiLegPosition]:
        """Get all currently open positions"""
        pass

    @abstractmethod
    def get_position(self, position_id: str) -> Optional[MultiLegPosition]:
        """Get a specific position by ID"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of this position manager"""
        pass


class SimpleMultiLegPositionManager(PositionManager):
    """
    Simple in-memory position manager for multi-leg strategies.

    Provides basic position tracking without persistence.
    """

    def __init__(self, executor=None, max_positions: int = 10):
        """
        Args:
            executor: MultiLegExecutor for executing trades
            max_positions: Maximum concurrent positions allowed
        """
        from .executors import MultiLegExecutor, DryRunMultiLegExecutor
        self.executor = executor or DryRunMultiLegExecutor()
        self.max_positions = max_positions
        self._positions: Dict[str, MultiLegPosition] = {}
        self._client = None  # Set via set_client()

    @property
    def name(self) -> str:
        return "simple_multi_leg"

    def set_client(self, client):
        """Set the CLOB client for execution"""
        self._client = client

    async def open_position(
        self,
        signal: MultiLegSignal,
        capital: float
    ) -> Optional[MultiLegPosition]:
        """Open a new multi-leg position"""
        # Check position limit
        open_count = len([p for p in self._positions.values() if p.is_open])
        if open_count >= self.max_positions:
            logger.warning(f"Max positions ({self.max_positions}) reached")
            return None

        # Create position
        position = MultiLegPosition(
            position_id=signal.signal_id,
            signal=signal,
            status=MultiLegPositionStatus.PENDING
        )

        # Execute if we have a client
        if self._client is not None:
            from .executors import MultiLegExecutor
            if isinstance(self.executor, MultiLegExecutor):
                result = await self.executor.execute_multi_leg(
                    self._client, signal, capital
                )

                if result.success:
                    position.status = MultiLegPositionStatus.OPEN
                    position.entry_time = datetime.now(timezone.utc)
                    position.total_cost = result.total_cost

                    # Create leg positions
                    for leg_result in result.leg_results:
                        if leg_result.success:
                            leg = signal.legs[leg_result.leg_index]
                            position.legs.append(Position(
                                market_id=leg.market_id,
                                token_id=leg.token_id,
                                shares=leg_result.result.filled_shares,
                                entry_price=leg_result.result.filled_price,
                                entry_time=datetime.now(timezone.utc),
                                status=PositionStatus.OPEN
                            ))
                else:
                    position.status = MultiLegPositionStatus.FAILED
                    logger.error(f"Failed to open position: {result.error_message}")

                    # Handle rollback if needed
                    if result.needs_rollback:
                        await self.executor.rollback_position(
                            self._client, result.leg_results
                        )
                    return None
        else:
            # Dry run mode - simulate success
            position.status = MultiLegPositionStatus.OPEN
            position.entry_time = datetime.now(timezone.utc)
            position.total_cost = capital

            for leg in signal.legs:
                shares = (capital * leg.size_pct) / leg.target_price
                position.legs.append(Position(
                    market_id=leg.market_id,
                    token_id=leg.token_id,
                    shares=shares,
                    entry_price=leg.target_price,
                    entry_time=datetime.now(timezone.utc),
                    status=PositionStatus.OPEN
                ))

        self._positions[position.position_id] = position
        logger.info(
            f"Opened position {position.position_id}: "
            f"{signal.num_legs} legs, ${position.total_cost:.2f} cost"
        )
        return position

    async def close_position(
        self,
        position_id: str,
        reason: str = "manual"
    ) -> Optional[float]:
        """Close an open position"""
        position = self._positions.get(position_id)
        if position is None:
            logger.warning(f"Position {position_id} not found")
            return None

        if not position.is_open:
            logger.warning(f"Position {position_id} is not open")
            return None

        position.status = MultiLegPositionStatus.CLOSING

        # Execute close trades
        if self._client is not None:
            total_proceeds = 0.0
            for leg_pos in position.legs:
                if leg_pos.status != PositionStatus.OPEN:
                    continue

                # Sell the position
                result = await self.executor.execute(
                    client=self._client,
                    token_id=leg_pos.token_id,
                    side=Side.SELL,
                    size_usd=leg_pos.shares * (leg_pos.current_price or leg_pos.entry_price),
                    price=leg_pos.current_price or leg_pos.entry_price
                )

                if result.success:
                    total_proceeds += result.filled_shares * result.filled_price
                    leg_pos.status = PositionStatus.CLOSED

            position.total_proceeds = total_proceeds
        else:
            # Dry run - simulate exit at current prices
            total_proceeds = sum(
                leg.shares * (leg.current_price or leg.entry_price)
                for leg in position.legs
            )
            position.total_proceeds = total_proceeds
            for leg in position.legs:
                leg.status = PositionStatus.CLOSED

        position.status = MultiLegPositionStatus.CLOSED
        position.exit_time = datetime.now(timezone.utc)
        position.metadata["close_reason"] = reason

        logger.info(
            f"Closed position {position_id}: "
            f"P&L ${position.pnl:.2f} ({position.pnl_pct:.1%}), reason={reason}"
        )
        return position.pnl

    async def update_positions(self) -> Dict[str, MultiLegPosition]:
        """Update all open positions with current prices"""
        updated = {}
        for position_id, position in self._positions.items():
            if not position.is_open:
                continue

            # Update leg prices (would fetch from API in real implementation)
            # For now, just mark as updated
            updated[position_id] = position

        return updated

    def get_open_positions(self) -> List[MultiLegPosition]:
        """Get all open positions"""
        return [p for p in self._positions.values() if p.is_open]

    def get_position(self, position_id: str) -> Optional[MultiLegPosition]:
        """Get position by ID"""
        return self._positions.get(position_id)

    def get_stats(self) -> Dict[str, Any]:
        """Get position manager statistics"""
        positions = list(self._positions.values())
        open_positions = [p for p in positions if p.is_open]
        closed_positions = [p for p in positions if p.is_closed]

        total_pnl = sum(p.pnl for p in closed_positions)
        winning = [p for p in closed_positions if p.pnl > 0]
        losing = [p for p in closed_positions if p.pnl <= 0]

        return {
            "total_positions": len(positions),
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "total_pnl": total_pnl,
            "win_rate": len(winning) / len(closed_positions) if closed_positions else 0.0,
            "avg_win": sum(p.pnl for p in winning) / len(winning) if winning else 0.0,
            "avg_loss": sum(p.pnl for p in losing) / len(losing) if losing else 0.0,
        }
