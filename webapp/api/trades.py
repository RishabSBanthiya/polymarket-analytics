"""
Trade history API endpoints.
"""

from fastapi import APIRouter, Query
from typing import Optional, List
from datetime import datetime

from ..models.schemas import TradeResponse
from ..services.trade_service import TradeService

router = APIRouter()
service = TradeService()


@router.get("/", response_model=List[TradeResponse])
async def list_trades(
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    start_time: Optional[datetime] = Query(None, description="Start time filter"),
    end_time: Optional[datetime] = Query(None, description="End time filter"),
    wallet_address: Optional[str] = Query(None, description="Filter by wallet address"),
    limit: Optional[int] = Query(100, description="Limit results")
):
    """List all trades with optional filters"""
    trades = service.get_trades(
        agent_id=agent_id,
        start_time=start_time,
        end_time=end_time,
        wallet_address=wallet_address,
        limit=limit
    )
    return trades


@router.get("/stats")
async def get_trade_stats(
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    wallet_address: Optional[str] = Query(None, description="Filter by wallet address"),
    days: int = Query(30, description="Number of days to look back")
):
    """Get trade statistics"""
    return service.get_trade_stats(
        agent_id=agent_id,
        wallet_address=wallet_address,
        days=days
    )

