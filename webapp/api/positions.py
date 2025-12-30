"""
Position API endpoints.
"""

from fastapi import APIRouter, Query
from typing import Optional, List
from datetime import datetime

from ..models.schemas import PositionResponse
from polymarket.trading.storage.sqlite import SQLiteStorage
from polymarket.core.config import get_config

router = APIRouter()
config = get_config()
storage = SQLiteStorage(config.db_path)


@router.get("/", response_model=List[PositionResponse])
async def list_positions(
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    status: Optional[str] = Query(None, description="Filter by status (open/closed/orphan)"),
    wallet_address: Optional[str] = Query(None, description="Filter by wallet address")
):
    """List all positions. When status='open', includes both OPEN and ORPHAN positions."""
    from polymarket.core.models import PositionStatus
    
    with storage.transaction() as txn:
        if wallet_address:
            # get_all_positions already includes orphan positions for the wallet
            positions = txn.get_all_positions(wallet_address)
        elif agent_id:
            # Orphan positions are not associated with specific agents, so only get agent's positions
            status_enum = PositionStatus(status) if status else None
            positions = txn.get_agent_positions(agent_id, status_enum)
        else:
            # Get all positions (will include orphans)
            positions = txn.get_all_positions()
        
        # Filter by status - when status is "open", include both OPEN and ORPHAN
        if status:
            if status.lower() == "open":
                # Include both OPEN and ORPHAN positions
                positions = [p for p in positions if p.status in (PositionStatus.OPEN, PositionStatus.ORPHAN)]
            else:
                # For other statuses, filter normally
                try:
                    status_enum = PositionStatus(status)
                    positions = [p for p in positions if p.status == status_enum]
                except ValueError:
                    # Invalid status, return empty list
                    positions = []
    
    return [
        {
            "id": p.id,
            "agent_id": p.agent_id,
            "market_id": p.market_id,
            "token_id": p.token_id,
            "outcome": p.outcome,
            "shares": p.shares,
            "entry_price": p.entry_price,
            "entry_time": p.entry_time,
            "current_price": p.current_price,
            "status": p.status.value,
            "cost_basis": p.cost_basis,
            "current_value": p.current_value,
            "unrealized_pnl": p.unrealized_pnl,
            "unrealized_pnl_pct": p.unrealized_pnl_pct
        }
        for p in positions
    ]


@router.get("/stats")
async def get_position_stats(
    wallet_address: Optional[str] = Query(None, description="Filter by wallet address")
):
    """Get position statistics. Includes orphan positions in open_positions count."""
    from polymarket.core.models import PositionStatus
    
    with storage.transaction() as txn:
        if wallet_address:
            positions = txn.get_all_positions(wallet_address)
        else:
            positions = txn.get_all_positions()
    
    # Include both OPEN and ORPHAN positions as "open"
    open_positions = [p for p in positions if p.status in (PositionStatus.OPEN, PositionStatus.ORPHAN)]
    total_value = sum(p.current_value for p in open_positions)
    total_pnl = sum(p.unrealized_pnl for p in open_positions)
    
    return {
        "total_positions": len(positions),
        "open_positions": len(open_positions),
        "closed_positions": len(positions) - len(open_positions),
        "total_value": total_value,
        "total_pnl": total_pnl
    }

