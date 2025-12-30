"""
Trade history API endpoints.
"""

from fastapi import APIRouter, Query
from typing import Optional, List
from datetime import datetime

from ..models.schemas import TradeResponse
from ..services.trade_service import TradeService
from polymarket.core.config import get_config

router = APIRouter()
service = TradeService()
config = get_config()


@router.get("/", response_model=List[TradeResponse])
async def list_trades(
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    start_time: Optional[datetime] = Query(None, description="Start time filter"),
    end_time: Optional[datetime] = Query(None, description="End time filter"),
    wallet_address: Optional[str] = Query(None, description="Filter by wallet address"),
    limit: Optional[int] = Query(100, description="Limit results"),
    include_orphans: bool = Query(True, description="Include orphan trades")
):
    """List all trades with optional filters, including orphan trades from wallet history"""
    # Use wallet_address from config if not provided
    if not wallet_address and config.proxy_address:
        wallet_address = config.proxy_address
    
    trades = await service.get_trades_async(
        agent_id=agent_id,
        start_time=start_time,
        end_time=end_time,
        wallet_address=wallet_address,
        limit=limit,
        include_orphans=include_orphans
    )
    return trades


@router.get("/stats")
async def get_trade_stats(
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    wallet_address: Optional[str] = Query(None, description="Filter by wallet address"),
    days: int = Query(30, description="Number of days to look back"),
    include_orphans: bool = Query(True, description="Include orphan trades in stats")
):
    """Get trade statistics including orphan trades"""
    # Use wallet_address from config if not provided
    if not wallet_address and config.proxy_address:
        wallet_address = config.proxy_address
    
    return await service.get_trade_stats_async(
        agent_id=agent_id,
        wallet_address=wallet_address,
        days=days,
        include_orphans=include_orphans
    )

