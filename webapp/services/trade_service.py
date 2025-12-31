"""
Trade service for aggregating and processing trade data.

Uses the transactions table as the source of truth (chain-synced on-chain data).
"""

import logging
from typing import Optional, List, Dict
from datetime import datetime, timedelta

from ..storage.trade_storage import TradeStorage
from polymarket.core.config import get_config
from polymarket.trading.storage.sqlite import SQLiteStorage

logger = logging.getLogger(__name__)


class TradeService:
    """Service for trade history operations using chain-synced data."""
    
    def __init__(self, storage: Optional[TradeStorage] = None):
        self.storage = storage or TradeStorage()
        self.config = get_config()
        self._sqlite_storage = SQLiteStorage(self.config.db_path)
    
    def get_all_transactions(
        self,
        wallet_address: str,
        transaction_type: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[dict]:
        """
        Get all transactions from the chain-synced transactions table.
        
        This is the primary method for accessing complete on-chain history.
        """
        with self._sqlite_storage.transaction() as txn:
            transactions = txn.get_transactions(
                wallet_address=wallet_address,
                transaction_type=transaction_type,
                start_time=start_time,
                end_time=end_time,
                limit=limit
            )
        
        # Convert to trade format for compatibility
        trades = []
        for tx in transactions:
            tx_type = tx.get("transaction_type", "")
            side = tx_type.upper() if tx_type else "UNKNOWN"
            
            trades.append({
                "id": tx.get("id"),
                "tx_hash": tx.get("tx_hash"),
                "agent_id": tx.get("agent_id") or "unattributed",
                "market_id": tx.get("market_id") or "",
                "token_id": tx.get("token_id") or "",
                "side": side,
                "shares": tx.get("shares") or 0.0,
                "price": tx.get("price_per_share") or 0.0,
                "filled_price": tx.get("price_per_share") or 0.0,
                "signal_score": None,
                "success": True,
                "error_message": None,
                "timestamp": tx.get("block_timestamp"),
                "wallet_address": wallet_address,
                "is_orphan": tx.get("agent_id") is None,
                "transaction_type": tx_type,
                "usdc_amount": tx.get("usdc_amount"),
                "block_number": tx.get("block_number"),
            })
        
        return trades
    
    def get_unattributed_trades(self, wallet_address: str) -> List[dict]:
        """
        Get trades without agent attribution.
        
        Returns transactions from the chain sync that don't have an agent_id.
        """
        all_transactions = self.get_all_transactions(wallet_address, limit=10000)
        return [t for t in all_transactions if t.get("agent_id") == "unattributed"]
    
    async def get_trades_async(
        self,
        agent_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        wallet_address: Optional[str] = None,
        limit: Optional[int] = None,
        include_orphans: bool = True
    ) -> List[dict]:
        """Get trades with filters from chain-synced transactions."""
        if limit is None and start_time is None:
            limit = 10000
        
        if wallet_address:
            # Use chain sync data
            trades = self.get_all_transactions(
                wallet_address=wallet_address,
                start_time=start_time,
                end_time=end_time,
                limit=limit
            )
            
            # Filter by agent_id if specified
            if agent_id:
                trades = [t for t in trades if t.get("agent_id") == agent_id]
            
            # Optionally exclude orphans
            if not include_orphans:
                trades = [t for t in trades if not t.get("is_orphan", False)]
        else:
            # Fallback to executions table for backward compatibility
            trades = self.storage.get_executions(
                agent_id=agent_id,
                start_time=start_time,
                end_time=end_time,
                wallet_address=wallet_address,
                limit=limit
            )
            for trade in trades:
                trade["is_orphan"] = False
        
        # Sort by timestamp descending
        trades.sort(key=lambda x: x["timestamp"] if x.get("timestamp") else datetime.min, reverse=True)
        
        if limit:
            return trades[:limit]
        return trades
    
    def get_trades(
        self,
        agent_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        wallet_address: Optional[str] = None,
        limit: Optional[int] = None,
        include_orphans: bool = True
    ) -> List[dict]:
        """Get trades with filters (sync version)."""
        if wallet_address:
            trades = self.get_all_transactions(
                wallet_address=wallet_address,
                start_time=start_time,
                end_time=end_time,
                limit=limit
            )
            
            if agent_id:
                trades = [t for t in trades if t.get("agent_id") == agent_id]
            
            if not include_orphans:
                trades = [t for t in trades if not t.get("is_orphan", False)]
        else:
            trades = self.storage.get_executions(
                agent_id=agent_id,
                start_time=start_time,
                end_time=end_time,
                wallet_address=wallet_address,
                limit=limit
            )
            for trade in trades:
                trade["is_orphan"] = False
        
        trades.sort(key=lambda x: x["timestamp"] if x.get("timestamp") else datetime.min, reverse=True)
        
        if limit:
            return trades[:limit]
        return trades
    
    async def get_trade_stats_async(
        self,
        agent_id: Optional[str] = None,
        wallet_address: Optional[str] = None,
        days: int = 30,
        include_orphans: bool = True
    ) -> Dict:
        """Get trade statistics from chain-synced data."""
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        trades = await self.get_trades_async(
            agent_id=agent_id,
            start_time=start_time,
            end_time=end_time,
            wallet_address=wallet_address,
            include_orphans=include_orphans
        )
        
        if not trades:
            return {
                "total": 0,
                "successful": 0,
                "failed": 0,
                "total_volume": 0.0,
                "avg_trade_size": 0.0,
                "orphan_count": 0
            }
        
        successful = [t for t in trades if t.get("success", True)]
        orphan_trades = [t for t in trades if t.get("is_orphan", False)]
        total_volume = sum(
            (t.get("filled_price") or t.get("price") or 0) * (t.get("shares") or 0) 
            for t in successful
        )
        
        return {
            "total": len(trades),
            "successful": len(successful),
            "failed": len(trades) - len(successful),
            "total_volume": total_volume,
            "avg_trade_size": total_volume / len(successful) if successful else 0.0,
            "orphan_count": len(orphan_trades)
        }
    
    def get_trade_stats(
        self,
        agent_id: Optional[str] = None,
        wallet_address: Optional[str] = None,
        days: int = 30
    ) -> Dict:
        """Get trade statistics (sync version)."""
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        trades = self.get_trades(
            agent_id=agent_id,
            start_time=start_time,
            end_time=end_time,
            wallet_address=wallet_address,
            include_orphans=True
        )
        
        if not trades:
            return {
                "total": 0,
                "successful": 0,
                "failed": 0,
                "total_volume": 0.0,
                "avg_trade_size": 0.0,
                "orphan_count": 0
            }
        
        successful = [t for t in trades if t.get("success", True)]
        orphan_trades = [t for t in trades if t.get("is_orphan", False)]
        total_volume = sum(
            (t.get("filled_price") or t.get("price") or 0) * (t.get("shares") or 0) 
            for t in successful
        )
        
        return {
            "total": len(trades),
            "successful": len(successful),
            "failed": len(trades) - len(successful),
            "total_volume": total_volume,
            "avg_trade_size": total_volume / len(successful) if successful else 0.0,
            "orphan_count": len(orphan_trades)
        }
    
    def get_computed_positions(self, wallet_address: str) -> List[dict]:
        """
        Get current positions computed from transaction history.
        
        This is the source-of-truth for positions.
        """
        with self._sqlite_storage.transaction() as txn:
            return txn.get_computed_positions(wallet_address)
    
    def get_transaction_summary(self, wallet_address: str) -> Dict:
        """
        Get summary of all transactions for a wallet.
        
        Returns counts and totals by transaction type.
        """
        with self._sqlite_storage.transaction() as txn:
            return txn.get_transaction_summary(wallet_address)
    
    def get_chain_sync_status(self, wallet_address: str) -> Optional[Dict]:
        """
        Get the current chain sync status for a wallet.
        """
        with self._sqlite_storage.transaction() as txn:
            return txn.get_chain_sync_state(wallet_address)
