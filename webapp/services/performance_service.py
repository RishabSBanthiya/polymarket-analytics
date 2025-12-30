"""
Performance service for calculating metrics.
"""

from typing import Optional, List, Dict
from datetime import datetime, timedelta
from collections import defaultdict

from ..storage.trade_storage import TradeStorage
from ..services.trade_service import TradeService
from polymarket.trading.storage.sqlite import SQLiteStorage
from polymarket.core.config import get_config


class PerformanceService:
    """Service for performance calculations"""
    
    def __init__(self):
        self.trade_storage = TradeStorage()
        self.trade_service = TradeService()
        config = get_config()
        self.storage = SQLiteStorage(config.db_path)
        self.config = config
    
    def _calculate_realized_pnl(self, trades: List[dict]) -> float:
        """
        Calculate realized P&L from closed trades.
        Matches buy and sell trades for the same token to calculate realized P&L.
        Uses FIFO (First In First Out) method.
        """
        if not trades:
            return 0.0
        
        # Group trades by token_id
        token_trades = defaultdict(list)
        for trade in trades:
            # Include successful trades and trades with token_id (skip deposits/withdrawals)
            if trade.get("token_id") and trade.get("token_id") != "":
                # Only include trades that succeeded, or orphan trades (which are always successful)
                if trade.get("success", False) or trade.get("is_orphan", False):
                    token_trades[trade["token_id"]].append(trade)
        
        total_realized_pnl = 0.0
        
        for token_id, token_trade_list in token_trades.items():
            # Sort by timestamp, but prioritize BUYs when timestamps are very close (within 1 second)
            # This handles cases where API timestamps might be slightly out of order
            def sort_key(trade):
                ts = trade["timestamp"]
                side = trade.get("side", "").upper()
                # Use timestamp as primary key, but add a small offset for BUYs to prioritize them
                # when timestamps are very close
                offset = 0 if side == "BUY" else 0.5
                return (ts.timestamp() + offset, 0 if side == "BUY" else 1)
            
            token_trade_list.sort(key=sort_key)
            
            # Track open positions (buys not yet sold)
            open_buys = []  # List of dicts with shares, price, timestamp
            
            for trade in token_trade_list:
                side = trade.get("side", "").upper()
                shares = float(trade.get("shares", 0))
                price = float(trade.get("filled_price", 0) or trade.get("price", 0))
                
                if shares <= 0 or price <= 0:
                    continue
                
                if side == "BUY":
                    # Add to open buys
                    open_buys.append({
                        "shares": shares,
                        "price": price,
                        "timestamp": trade["timestamp"]
                    })
                elif side == "SELL":
                    # Match against open buys (FIFO)
                    remaining_sell_shares = shares
                    
                    while remaining_sell_shares > 0 and open_buys:
                        buy = open_buys[0]
                        buy_shares = float(buy["shares"])
                        buy_price = float(buy["price"])
                        
                        if buy_shares <= remaining_sell_shares:
                            # This buy is fully closed
                            shares_closed = buy_shares
                            remaining_sell_shares -= buy_shares
                            open_buys.pop(0)
                        else:
                            # Partial close
                            shares_closed = remaining_sell_shares
                            buy["shares"] -= remaining_sell_shares
                            remaining_sell_shares = 0
                        
                        # Calculate realized P&L for this closed position
                        # P&L = (sell_price - buy_price) * shares_closed
                        realized_pnl = (price - buy_price) * shares_closed
                        total_realized_pnl += realized_pnl
        
        return total_realized_pnl
    
    async def get_agent_performance_async(
        self, 
        agent_id: str, 
        days: int = 30,
        include_orphans: bool = True
    ) -> Dict:
        """Get performance metrics for an agent including orphan trades (async)"""
        end_time = datetime.now()
        # If days is very large (all-time), don't filter by start_time
        if days >= 36500:  # ~100 years = all-time
            start_time = None
        else:
            start_time = end_time - timedelta(days=days)
        
        # Get wallet address for orphan detection
        wallet_address = None
        if include_orphans:
            wallet_address = self.config.proxy_address
        
        trades = await self.trade_service.get_trades_async(
            agent_id=agent_id,
            start_time=start_time,
            end_time=end_time,
            wallet_address=wallet_address,
            include_orphans=include_orphans and agent_id != "orphan",  # Don't double-count if querying orphan agent
            limit=None  # Get all trades for all-time P&L
        )
        
        successful = [t for t in trades if t["success"]]
        total_volume = sum(t["filled_price"] * t["shares"] for t in successful)
        
        # Calculate P&L: realized from closed trades + unrealized from open positions
        realized_pnl = self._calculate_realized_pnl(trades)
        
        # Get positions for unrealized P&L calculation
        with self.storage.transaction() as txn:
            if agent_id == "orphan":
                # For orphan agent, get all positions not attributed to any agent
                if wallet_address:
                    all_positions = txn.get_all_positions(wallet_address)
                else:
                    all_positions = []
                
                # Get all positions that are attributed to agents
                agents = txn.get_all_agents()
                agent_token_ids = set()
                for agent in agents:
                    agent_pos = txn.get_agent_positions(agent.agent_id)
                    agent_token_ids.update({p.token_id for p in agent_pos})
                
                # Orphan positions are those not in any agent's positions
                agent_positions = [p for p in all_positions if p.token_id not in agent_token_ids]
            else:
                agent_positions = txn.get_agent_positions(agent_id)
        
        # Unrealized P&L from open positions (including orphan positions)
        open_positions = [p for p in agent_positions if p.status.value in ["open", "orphan"]]
        unrealized_pnl = sum(p.unrealized_pnl for p in open_positions)
        
        total_pnl = realized_pnl + unrealized_pnl
        
        return {
            "agent_id": agent_id,
            "total_trades": len(trades),
            "successful_trades": len(successful),
            "failed_trades": len(trades) - len(successful),
            "win_rate": len(successful) / len(trades) if trades else 0.0,
            "total_pnl": total_pnl,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "avg_trade_size": total_volume / len(successful) if successful else 0.0,
            "total_volume": total_volume
        }
    
    def get_agent_performance(self, agent_id: str, days: int = 30) -> Dict:
        """Get performance metrics for an agent (sync version)"""
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        trades = self.trade_storage.get_executions(
            agent_id=agent_id,
            start_time=start_time,
            end_time=end_time
        )
        
        successful = [t for t in trades if t["success"]]
        total_volume = sum(t["filled_price"] * t["shares"] for t in successful)
        
        # Calculate realized P&L from closed trades
        realized_pnl = self._calculate_realized_pnl(trades)
        
        # Get positions for unrealized P&L calculation
        with self.storage.transaction() as txn:
            positions = txn.get_agent_positions(agent_id)
        
        # Unrealized P&L from open positions (including orphan positions)
        open_positions = [p for p in positions if p.status.value in ["open", "orphan"]]
        unrealized_pnl = sum(p.unrealized_pnl for p in open_positions)
        
        total_pnl = realized_pnl + unrealized_pnl
        
        return {
            "agent_id": agent_id,
            "total_trades": len(trades),
            "successful_trades": len(successful),
            "failed_trades": len(trades) - len(successful),
            "win_rate": len(successful) / len(trades) if trades else 0.0,
            "total_pnl": total_pnl,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "avg_trade_size": total_volume / len(successful) if successful else 0.0,
            "total_volume": total_volume
        }
    
    async def get_all_agents_performance_async(
        self, 
        days: int = 30,
        include_orphans: bool = True
    ) -> List[Dict]:
        """Get performance for all agents including orphan agent (async)"""
        with self.storage.transaction() as txn:
            agents = txn.get_all_agents()
        
        results = []
        for agent in agents:
            perf = await self.get_agent_performance_async(
                agent.agent_id, 
                days=days,
                include_orphans=False  # Don't include orphans in individual agent stats
            )
            results.append(perf)
        
        # Add orphan agent performance if requested
        if include_orphans:
            orphan_perf = await self.get_agent_performance_async(
                "orphan",
                days=days,
                include_orphans=True
            )
            results.append(orphan_perf)
        
        return results
    
    def get_all_agents_performance(self, days: int = 30) -> List[Dict]:
        """Get performance for all agents (sync version)"""
        with self.storage.transaction() as txn:
            agents = txn.get_all_agents()
        
        return [self.get_agent_performance(agent.agent_id, days) for agent in agents]
    
    async def get_overview_async(
        self, 
        days: int = 30,
        include_orphans: bool = True
    ) -> Dict:
        """Get overall performance overview including orphan trades (async)"""
        # For all-time P&L, calculate directly from all trades to ensure accuracy
        if days >= 36500:  # All-time
            end_time = datetime.now()
            start_time = None
            wallet_address = self.config.proxy_address if include_orphans else None
            
            # Get all trades across all agents (no limit for all-time)
            all_trades = await self.trade_service.get_trades_async(
                agent_id=None,
                start_time=start_time,
                end_time=end_time,
                wallet_address=wallet_address,
                include_orphans=include_orphans,
                limit=None
            )
            
            # Filter to only actual trades (exclude deposits/withdrawals)
            actual_trades = [t for t in all_trades if t.get("token_id") and t.get("token_id") != ""]
            
            # Calculate realized P&L from all trades
            realized_pnl = self._calculate_realized_pnl(actual_trades)
            
            # Get all open positions for unrealized P&L
            # Fetch fresh positions from API to get current prices
            from polymarket.core.api import PolymarketAPI
            api = PolymarketAPI(self.config)
            await api.connect()
            try:
                api_positions = await api.fetch_positions(wallet_address) if wallet_address else []
                unrealized_pnl = 0.0
                for pos in api_positions:
                    if pos.current_price and pos.entry_price:
                        unrealized_pnl += (pos.current_price - pos.entry_price) * pos.shares
            finally:
                await api.close()
            total_pnl = realized_pnl + unrealized_pnl
            
            successful = [t for t in actual_trades if t.get("success", False) or t.get("is_orphan", False)]
            total_volume = sum(t["filled_price"] * t["shares"] for t in successful)
            
            # Also get agent breakdowns for display
            agents_perf = await self.get_all_agents_performance_async(
                days=days,
                include_orphans=include_orphans
            )
            
            return {
                "total_agents": len(agents_perf),
                "total_trades": len(actual_trades),
                "total_pnl": total_pnl,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "total_volume": total_volume,
                "agents": agents_perf
            }
        else:
            # For time-limited queries, use agent aggregation
            agents_perf = await self.get_all_agents_performance_async(
                days=days,
                include_orphans=include_orphans
            )
            
            # Calculate totals including realized and unrealized breakdown
            total_realized = sum(a.get("realized_pnl", 0) for a in agents_perf)
            total_unrealized = sum(a.get("unrealized_pnl", 0) for a in agents_perf)
            
            return {
                "total_agents": len(agents_perf),
                "total_trades": sum(a["total_trades"] for a in agents_perf),
                "total_pnl": sum(a["total_pnl"] for a in agents_perf),
                "realized_pnl": total_realized,
                "unrealized_pnl": total_unrealized,
                "total_volume": sum(a["total_volume"] for a in agents_perf),
                "agents": agents_perf
            }
    
    def get_overview(self, days: int = 30) -> Dict:
        """Get overall performance overview (sync version)"""
        agents_perf = self.get_all_agents_performance(days)
        
        total_realized = sum(a.get("realized_pnl", 0) for a in agents_perf)
        total_unrealized = sum(a.get("unrealized_pnl", 0) for a in agents_perf)
        
        return {
            "total_agents": len(agents_perf),
            "total_trades": sum(a["total_trades"] for a in agents_perf),
            "total_pnl": sum(a["total_pnl"] for a in agents_perf),
            "realized_pnl": total_realized,
            "unrealized_pnl": total_unrealized,
            "total_volume": sum(a["total_volume"] for a in agents_perf),
            "agents": agents_perf
        }
    
    async def get_timeseries_async(
        self, 
        days: int = 30, 
        interval: str = "day",
        include_orphans: bool = True
    ) -> List[Dict]:
        """Get performance time series data including orphan trades (async)"""
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        wallet_address = self.config.proxy_address if include_orphans else None
        trades = await self.trade_service.get_trades_async(
            start_time=start_time,
            end_time=end_time,
            wallet_address=wallet_address,
            include_orphans=include_orphans
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
    
    def get_timeseries(self, days: int = 30, interval: str = "day") -> List[Dict]:
        """Get performance time series data (sync version)"""
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

