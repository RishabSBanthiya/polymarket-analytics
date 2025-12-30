"""
Trade service for aggregating and processing trade data.
"""

from typing import Optional, List, Dict
from datetime import datetime, timedelta

from ..storage.trade_storage import TradeStorage


class TradeService:
    """Service for trade history operations"""
    
    def __init__(self, storage: Optional[TradeStorage] = None):
        self.storage = storage or TradeStorage()
    
    def get_trades(
        self,
        agent_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        wallet_address: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[dict]:
        """Get trades with filters"""
        return self.storage.get_executions(
            agent_id=agent_id,
            start_time=start_time,
            end_time=end_time,
            wallet_address=wallet_address,
            limit=limit
        )
    
    def get_trade_stats(
        self,
        agent_id: Optional[str] = None,
        wallet_address: Optional[str] = None,
        days: int = 30
    ) -> Dict:
        """Get trade statistics"""
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        trades = self.get_trades(
            agent_id=agent_id,
            start_time=start_time,
            end_time=end_time,
            wallet_address=wallet_address
        )
        
        if not trades:
            return {
                "total": 0,
                "successful": 0,
                "failed": 0,
                "total_volume": 0.0,
                "avg_trade_size": 0.0
            }
        
        successful = [t for t in trades if t["success"]]
        total_volume = sum(t["filled_price"] * t["shares"] for t in successful)
        
        return {
            "total": len(trades),
            "successful": len(successful),
            "failed": len(trades) - len(successful),
            "total_volume": total_volume,
            "avg_trade_size": total_volume / len(successful) if successful else 0.0
        }

