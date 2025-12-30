"""
Performance service for calculating metrics.
"""

from typing import Optional, List, Dict
from datetime import datetime, timedelta

from ..storage.trade_storage import TradeStorage
from polymarket.trading.storage.sqlite import SQLiteStorage
from polymarket.core.config import get_config


class PerformanceService:
    """Service for performance calculations"""
    
    def __init__(self):
        self.trade_storage = TradeStorage()
        config = get_config()
        self.storage = SQLiteStorage(config.db_path)
    
    def get_agent_performance(self, agent_id: str, days: int = 30) -> Dict:
        """Get performance metrics for an agent"""
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        trades = self.trade_storage.get_executions(
            agent_id=agent_id,
            start_time=start_time,
            end_time=end_time
        )
        
        successful = [t for t in trades if t["success"]]
        total_volume = sum(t["filled_price"] * t["shares"] for t in successful)
        
        # Get positions for P&L calculation
        with self.storage.transaction() as txn:
            positions = txn.get_agent_positions(agent_id)
        
        total_pnl = sum(p.unrealized_pnl for p in positions)
        
        return {
            "agent_id": agent_id,
            "total_trades": len(trades),
            "successful_trades": len(successful),
            "failed_trades": len(trades) - len(successful),
            "win_rate": len(successful) / len(trades) if trades else 0.0,
            "total_pnl": total_pnl,
            "avg_trade_size": total_volume / len(successful) if successful else 0.0,
            "total_volume": total_volume
        }
    
    def get_all_agents_performance(self, days: int = 30) -> List[Dict]:
        """Get performance for all agents"""
        with self.storage.transaction() as txn:
            agents = txn.get_all_agents()
        
        return [self.get_agent_performance(agent.agent_id, days) for agent in agents]
    
    def get_overview(self, days: int = 30) -> Dict:
        """Get overall performance overview"""
        agents_perf = self.get_all_agents_performance(days)
        
        return {
            "total_agents": len(agents_perf),
            "total_trades": sum(a["total_trades"] for a in agents_perf),
            "total_pnl": sum(a["total_pnl"] for a in agents_perf),
            "total_volume": sum(a["total_volume"] for a in agents_perf),
            "agents": agents_perf
        }
    
    def get_timeseries(self, days: int = 30, interval: str = "day") -> List[Dict]:
        """Get performance time series data"""
        # This would require more complex aggregation
        # For now, return simplified version
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        trades = self.trade_storage.get_executions(
            start_time=start_time,
            end_time=end_time
        )
        
        # Group by day
        by_day = {}
        for trade in trades:
            day = trade["timestamp"].date()
            if day not in by_day:
                by_day[day] = {"date": day, "trades": 0, "volume": 0.0}
            by_day[day]["trades"] += 1
            if trade["success"]:
                by_day[day]["volume"] += trade["filled_price"] * trade["shares"]
        
        return sorted(by_day.values(), key=lambda x: x["date"])

