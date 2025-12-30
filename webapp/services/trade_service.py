"""
Trade service for aggregating and processing trade data.
"""

import asyncio
from typing import Optional, List, Dict
from datetime import datetime, timedelta

from ..storage.trade_storage import TradeStorage
from polymarket.core.api import PolymarketAPI
from polymarket.core.config import get_config


class TradeService:
    """Service for trade history operations"""
    
    def __init__(self, storage: Optional[TradeStorage] = None):
        self.storage = storage or TradeStorage()
        self.config = get_config()
    
    async def _fetch_orphan_trades(self, wallet_address: str) -> List[dict]:
        """Fetch orphan trades from wallet's complete transaction history"""
        try:
            api = PolymarketAPI(self.config)
            await api.connect()
            
            orphan_trades = []
            
            # Get all stored executions for this wallet to compare
            stored_executions = self.storage.get_executions(wallet_address=wallet_address)
            
            # Create matching sets for stored executions
            # Exact match: (token_id, shares, price, side, timestamp within 5 min)
            stored_matches = set()
            for e in stored_executions:
                # Create multiple keys for flexible matching
                key_exact = (
                    e["token_id"],
                    round(e["shares"], 4),
                    round(e["filled_price"], 4),
                    e["side"]
                )
                stored_matches.add(key_exact)
                
                # Also store by token + approximate price/time
                timestamp_key = e["timestamp"].replace(second=0, microsecond=0)
                stored_matches.add((e["token_id"], timestamp_key, e["side"]))
            
            # Fetch complete transaction history (trades, deposits, withdrawals)
            # Use a large limit to get all-time history
            try:
                transactions = await api.fetch_user_transactions(wallet_address, limit=10000)
                import logging
                logging.getLogger(__name__).info(f"Fetched {len(transactions)} transactions from API")
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Could not fetch user transactions, falling back to positions: {e}")
                transactions = []
            
            # Process transactions
            for tx in transactions:
                tx_type = tx.get("type", "trade")
                side = tx.get("side", "").upper()
                
                # Skip deposits and withdrawals for now (they're not trades)
                # But we could add them as separate transaction types if needed
                if tx_type in ["deposit", "withdrawal", "transfer"]:
                    # Add as a special transaction type
                    orphan_trades.append({
                        "id": None,
                        "agent_id": "orphan",
                        "market_id": "",
                        "token_id": "",
                        "side": side,
                        "shares": 0.0,
                        "price": tx.get("price", 0.0),
                        "filled_price": tx.get("price", 0.0),
                        "signal_score": None,
                        "success": True,
                        "error_message": None,
                        "timestamp": tx["timestamp"],
                        "wallet_address": wallet_address,
                        "is_orphan": True,
                        "transaction_type": tx_type
                    })
                    continue
                
                # For trades, check if they match stored executions
                token_id = tx.get("token_id", "")
                shares = tx.get("shares", 0)
                price = tx.get("price", 0)
                timestamp = tx["timestamp"]
                
                if not token_id or token_id == "":
                    import logging
                    logging.getLogger(__name__).debug(f"Skipping trade with no token_id")
                    continue
                
                if shares <= 0:
                    import logging
                    logging.getLogger(__name__).debug(f"Skipping trade with shares <= 0: {shares}")
                    continue
                
                # Check for matches
                key_exact = (token_id, round(shares, 4), round(price, 4), side)
                timestamp_key = timestamp.replace(second=0, microsecond=0)
                key_time = (token_id, timestamp_key, side)
                
                is_matched = False
                
                # Check exact match
                if key_exact in stored_matches:
                    is_matched = True
                # Check time-based match (same token, same minute, same side)
                elif key_time in stored_matches:
                    is_matched = True
                # Check approximate match (within 5 minutes, similar price/shares)
                else:
                    for stored in stored_executions:
                        if stored["token_id"] == token_id and stored["side"] == side:
                            time_diff = abs((stored["timestamp"] - timestamp).total_seconds())
                            if time_diff < 300:  # 5 minutes
                                price_diff = abs(price - stored["filled_price"]) / stored["filled_price"] if stored["filled_price"] > 0 else 1.0
                                shares_diff = abs(shares - stored["shares"]) / stored["shares"] if stored["shares"] > 0 else 1.0
                                # Match if price within 5% and shares within 20%
                                if price_diff < 0.05 and shares_diff < 0.20:
                                    is_matched = True
                                    break
                
                if not is_matched:
                    # This is an orphan trade
                    orphan_trades.append({
                        "id": None,
                        "agent_id": "orphan",
                        "market_id": tx.get("market_id", ""),
                        "token_id": token_id,
                        "side": side,
                        "shares": shares,
                        "price": price,
                        "filled_price": price,
                        "signal_score": None,
                        "success": True,
                        "error_message": None,
                        "timestamp": timestamp,
                        "wallet_address": wallet_address,
                        "is_orphan": True,
                        "transaction_type": "trade"
                    })
            
            # Fallback: Also check current positions if we didn't get transactions
            if not transactions:
                positions = await api.fetch_positions(wallet_address)
                for pos in positions:
                    # Check if this position matches any stored execution
                    pos_key = (pos.token_id, round(pos.shares, 4), round(pos.entry_price, 4), "BUY")
                    
                    if pos_key not in stored_matches:
                        # Check approximate match
                        is_matched = False
                        for stored in stored_executions:
                            if stored["token_id"] == pos.token_id:
                                price_diff = abs(pos.entry_price - stored["filled_price"]) / stored["filled_price"] if stored["filled_price"] > 0 else 1.0
                                shares_diff = abs(pos.shares - stored["shares"]) / stored["shares"] if stored["shares"] > 0 else 1.0
                                if price_diff < 0.05 and shares_diff < 0.20:
                                    is_matched = True
                                    break
                        
                        if not is_matched:
                            orphan_trades.append({
                                "id": None,
                                "agent_id": "orphan",
                                "market_id": pos.market_id,
                                "token_id": pos.token_id,
                                "side": "BUY",
                                "shares": pos.shares,
                                "price": pos.entry_price,
                                "filled_price": pos.entry_price,
                                "signal_score": None,
                                "success": True,
                                "error_message": None,
                                "timestamp": pos.entry_time if pos.entry_time else datetime.now(),
                                "wallet_address": wallet_address,
                                "is_orphan": True,
                                "transaction_type": "trade"
                            })
            
            await api.close()
            
            # Remove duplicates (same token_id, side, shares, price, and exact timestamp)
            # Use more specific criteria to avoid removing legitimate trades
            seen = set()
            unique_orphans = []
            for trade in orphan_trades:
                # Create a key that uniquely identifies a trade
                # Include shares and price to distinguish between multiple trades in the same minute
                key = (
                    trade["token_id"],
                    trade["side"],
                    round(trade.get("shares", 0), 4),
                    round(trade.get("filled_price", 0), 6),
                    trade["timestamp"].isoformat(),  # Use exact timestamp, not rounded
                    trade.get("transaction_type", "trade")
                )
                if key not in seen:
                    seen.add(key)
                    unique_orphans.append(trade)
            
            return unique_orphans
            
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error fetching orphan trades: {e}")
            return []
    
    async def get_trades_async(
        self,
        agent_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        wallet_address: Optional[str] = None,
        limit: Optional[int] = None,
        include_orphans: bool = True
    ) -> List[dict]:
        """Get trades with filters, optionally including orphan trades (async version)"""
        # For all-time queries, use a very large limit
        if limit is None and start_time is None:
            limit = 10000  # Get all trades
        
        trades = self.storage.get_executions(
            agent_id=agent_id,
            start_time=start_time,
            end_time=end_time,
            wallet_address=wallet_address,
            limit=limit
        )
        
        # Mark non-orphan trades
        for trade in trades:
            trade["is_orphan"] = False
        
        # Add orphan trades if requested and wallet_address is provided
        if include_orphans and wallet_address:
            try:
                orphan_trades = await self._fetch_orphan_trades(wallet_address)
                trades.extend(orphan_trades)
            except Exception as e:
                print(f"Error getting orphan trades: {e}")
        
        # Sort by timestamp descending
        trades.sort(key=lambda x: x["timestamp"], reverse=True)
        
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
        """Get trades with filters (sync version, orphans disabled in sync context)"""
        trades = self.storage.get_executions(
            agent_id=agent_id,
            start_time=start_time,
            end_time=end_time,
            wallet_address=wallet_address,
            limit=limit
        )
        
        # Mark non-orphan trades
        for trade in trades:
            trade["is_orphan"] = False
        
        # Note: Orphan detection requires async, use get_trades_async in async contexts
        # Sort by timestamp descending
        trades.sort(key=lambda x: x["timestamp"], reverse=True)
        
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
        """Get trade statistics including orphan trades (async version)"""
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
        
        successful = [t for t in trades if t["success"]]
        orphan_trades = [t for t in trades if t.get("is_orphan", False)]
        total_volume = sum(t["filled_price"] * t["shares"] for t in successful)
        
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
        """Get trade statistics (sync version, orphans not included)"""
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        trades = self.get_trades(
            agent_id=agent_id,
            start_time=start_time,
            end_time=end_time,
            wallet_address=wallet_address,
            include_orphans=False  # Can't fetch orphans in sync context
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
        
        successful = [t for t in trades if t["success"]]
        total_volume = sum(t["filled_price"] * t["shares"] for t in successful)
        
        return {
            "total": len(trades),
            "successful": len(successful),
            "failed": len(trades) - len(successful),
            "total_volume": total_volume,
            "avg_trade_size": total_volume / len(successful) if successful else 0.0,
            "orphan_count": 0
        }

