"""
Trade storage wrapper for web app.
"""

from typing import Optional, List
from datetime import datetime

from polymarket.trading.storage.sqlite import SQLiteStorage
from polymarket.core.config import get_config


class TradeStorage:
    """Wrapper for trade execution history storage"""
    
    def __init__(self, db_path: Optional[str] = None):
        config = get_config()
        self.storage = SQLiteStorage(db_path or config.db_path)
    
    def get_executions(
        self,
        agent_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        wallet_address: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[dict]:
        """Get execution history"""
        with self.storage.transaction() as txn:
            executions = txn.get_executions(
                agent_id=agent_id,
                start_time=start_time,
                end_time=end_time,
                wallet_address=wallet_address
            )
        
        if limit:
            return executions[:limit]
        return executions
    
    def get_execution(self, execution_id: int) -> Optional[dict]:
        """Get single execution by ID"""
        # Note: This would require adding a method to storage
        # For now, we'll get all and filter
        executions = self.get_executions()
        for exec in executions:
            if exec["id"] == execution_id:
                return exec
        return None

