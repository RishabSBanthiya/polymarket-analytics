"""
Performance metrics API endpoints.
"""

from fastapi import APIRouter, Query
from typing import Optional, List

from ..models.schemas import PerformanceMetrics
from ..services.performance_service import PerformanceService

router = APIRouter()
service = PerformanceService()


@router.get("/agents", response_model=List[PerformanceMetrics])
async def get_agents_performance(
    days: int = Query(30, description="Number of days to look back"),
    include_orphans: bool = Query(True, description="Include orphan trades in performance")
):
    """Get performance metrics for all agents including orphan agent"""
    return await service.get_all_agents_performance_async(
        days=days,
        include_orphans=include_orphans
    )


@router.get("/agents/{agent_id}", response_model=PerformanceMetrics)
async def get_agent_performance(
    agent_id: str,
    days: int = Query(30, description="Number of days to look back"),
    include_orphans: bool = Query(True, description="Include orphan trades for orphan agent")
):
    """Get performance metrics for a specific agent"""
    return await service.get_agent_performance_async(
        agent_id, 
        days=days,
        include_orphans=include_orphans
    )


@router.get("/overview")
async def get_overview(
    days: Optional[int] = Query(None, description="Number of days to look back (None for all-time)"),
    include_orphans: bool = Query(True, description="Include orphan trades in overview")
):
    """Get overall performance overview including orphan trades"""
    # If days is None or 0, use a very large number to get all-time data
    if days is None or days <= 0:
        days = 36500  # ~100 years, effectively all-time
    return await service.get_overview_async(
        days=days,
        include_orphans=include_orphans
    )


@router.get("/timeseries")
async def get_timeseries(
    days: int = Query(30, description="Number of days to look back"),
    interval: str = Query("day", description="Time interval (day/hour)"),
    include_orphans: bool = Query(True, description="Include orphan trades in timeseries")
):
    """Get performance time series data including orphan trades"""
    return await service.get_timeseries_async(
        days=days, 
        interval=interval,
        include_orphans=include_orphans
    )


@router.get("/pnl-chart")
async def get_pnl_chart_data():
    """
    Get cumulative P&L chart data from blockchain transactions.
    Returns data points suitable for TradingView-style charts.
    """
    from polymarket.core.api import PolymarketAPI
    from polymarket.core.config import get_config
    from collections import defaultdict
    from datetime import datetime, timezone
    
    config = get_config()
    wallet_address = config.proxy_address
    
    if not wallet_address:
        return {"error": "No wallet configured", "data": []}
    
    api = PolymarketAPI(config)
    await api.connect()
    
    try:
        # Get all transactions from API
        transactions = await api.fetch_user_transactions(wallet_address, limit=10000)
        
        if not transactions:
            return {"data": [], "current_pnl": 0, "total_trades": 0}
        
        # Sort by timestamp ascending
        transactions.sort(key=lambda x: x['timestamp'])
        
        # Track cumulative P&L over time using FIFO
        token_buys = defaultdict(list)  # token_id -> list of {shares, price, timestamp}
        pnl_data_points = []
        cumulative_pnl = 0.0
        total_cost_basis = 0.0
        total_proceeds = 0.0
        
        for tx in transactions:
            token_id = tx.get('token_id', '')
            if not token_id:
                continue
                
            side = tx.get('side', '').upper()
            shares = float(tx.get('shares', 0))
            price = float(tx.get('price', 0))
            timestamp = tx['timestamp']
            
            if shares <= 0:
                continue
            
            if side == 'BUY':
                token_buys[token_id].append({
                    'shares': shares,
                    'price': price,
                    'timestamp': timestamp
                })
                total_cost_basis += shares * price
                
                # Record data point (P&L doesn't change on buy, but we track the event)
                pnl_data_points.append({
                    'timestamp': timestamp.isoformat(),
                    'time': int(timestamp.timestamp() * 1000),
                    'pnl': cumulative_pnl,
                    'type': 'buy',
                    'value': shares * price
                })
                
            elif side == 'SELL':
                remaining = shares
                sell_pnl = 0.0
                
                # FIFO matching
                while remaining > 0 and token_buys[token_id]:
                    buy = token_buys[token_id][0]
                    matched = min(remaining, buy['shares'])
                    
                    pnl = (price - buy['price']) * matched
                    sell_pnl += pnl
                    
                    buy['shares'] -= matched
                    remaining -= matched
                    
                    if buy['shares'] <= 0:
                        token_buys[token_id].pop(0)
                
                cumulative_pnl += sell_pnl
                total_proceeds += shares * price
                
                # Record data point with updated P&L
                pnl_data_points.append({
                    'timestamp': timestamp.isoformat(),
                    'time': int(timestamp.timestamp() * 1000),
                    'pnl': cumulative_pnl,
                    'type': 'sell',
                    'value': shares * price,
                    'realized_pnl': sell_pnl
                })
        
        # Get current positions for unrealized P&L
        positions = await api.fetch_positions(wallet_address)
        unrealized_pnl = 0.0
        for pos in positions:
            if pos.current_price and pos.entry_price:
                unrealized_pnl += (pos.current_price - pos.entry_price) * pos.shares
        
        # Add final data point with current time and total P&L
        now = datetime.now(timezone.utc)
        total_pnl = cumulative_pnl + unrealized_pnl
        
        if pnl_data_points:
            pnl_data_points.append({
                'timestamp': now.isoformat(),
                'time': int(now.timestamp() * 1000),
                'pnl': total_pnl,
                'type': 'current',
                'unrealized': unrealized_pnl
            })
        
        return {
            'data': pnl_data_points,
            'current_pnl': total_pnl,
            'realized_pnl': cumulative_pnl,
            'unrealized_pnl': unrealized_pnl,
            'total_trades': len([d for d in pnl_data_points if d['type'] in ['buy', 'sell']]),
            'total_cost_basis': total_cost_basis,
            'total_proceeds': total_proceeds
        }
        
    finally:
        await api.close()

