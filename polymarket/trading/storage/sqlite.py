"""
SQLite storage backend implementation.

Uses SQLite with WAL mode for better concurrent access.
All operations are performed within transactions for atomicity.
"""

import sqlite3
import uuid
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional, List, Generator

from ...core.models import (
    Position,
    Reservation,
    AgentInfo,
    WalletState,
    ReservationStatus,
    PositionStatus,
    AgentStatus,
)
from .base import StorageBackend, StorageTransaction

logger = logging.getLogger(__name__)


class SQLiteTransaction(StorageTransaction):
    """SQLite transaction implementation"""
    
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
    
    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute SQL with parameters"""
        return self.conn.execute(sql, params)
    
    def _fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """Execute and fetch one row"""
        cursor = self._execute(sql, params)
        return cursor.fetchone()
    
    def _fetchall(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        """Execute and fetch all rows"""
        cursor = self._execute(sql, params)
        return cursor.fetchall()
    
    # ==================== WALLET STATE ====================
    
    def get_wallet_state(self, wallet_address: str) -> WalletState:
        """Get current wallet state using computed positions from transactions table"""
        # Get cached USDC balance
        balance = self.get_usdc_balance(wallet_address)
        
        # Get computed positions from transactions table (source of truth)
        computed_positions = self.get_computed_positions(wallet_address)
        
        # Convert computed positions to Position objects for compatibility
        positions = []
        for cp in computed_positions:
            if cp['shares'] > 0.001:  # Only include positions with shares
                positions.append(Position(
                    id=None,
                    agent_id=cp.get('agent_id') or 'unattributed',
                    market_id=cp.get('market_id') or '',
                    token_id=cp['token_id'],
                    outcome=cp.get('outcome') or '',
                    shares=cp['shares'],
                    entry_price=cp.get('avg_entry_price', 0),
                    entry_time=None,
                    current_price=cp.get('avg_entry_price', 0),  # Will be updated with live prices
                    status=PositionStatus.OPEN,
                ))
        
        # Get active reservations
        reservations = self.get_all_reservations(wallet_address, ReservationStatus.PENDING)
        
        # Get agents
        agents = self.get_all_agents(wallet_address)
        
        # Calculate totals - use shares * avg_entry_price for positions
        total_positions_value = sum(p.shares * (p.current_price or p.entry_price) for p in positions)
        total_reserved = sum(r.amount_usd for r in reservations if r.is_active)
        
        return WalletState(
            wallet_address=wallet_address,
            usdc_balance=balance,
            total_positions_value=total_positions_value,
            total_reserved=total_reserved,
            positions=positions,
            reservations=reservations,
            agents=agents,
        )
    
    def get_usdc_balance(self, wallet_address: str) -> float:
        """Get cached USDC balance"""
        row = self._fetchone(
            "SELECT balance FROM wallet_balances WHERE wallet_address = ?",
            (wallet_address.lower(),)
        )
        return float(row["balance"]) if row else 0.0
    
    def update_usdc_balance(self, wallet_address: str, balance: float) -> None:
        """Update cached USDC balance"""
        self._execute(
            """
            INSERT INTO wallet_balances (wallet_address, balance, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
                balance = excluded.balance,
                updated_at = excluded.updated_at
            """,
            (wallet_address.lower(), balance, datetime.now(timezone.utc).isoformat())
        )
    
    # ==================== AGENTS ====================
    
    def register_agent(self, agent_id: str, agent_type: str, wallet_address: str) -> bool:
        """Register a new agent or restart an existing one"""
        # Check if agent already exists
        existing = self.get_agent(agent_id)
        if existing:
            if existing.status == AgentStatus.ACTIVE:
                # Agent is being restarted - this is okay
                logger.info(f"Agent {agent_id} was already active, restarting...")
            else:
                logger.info(f"Agent {agent_id} was {existing.status.value}, restarting...")
        
        now = datetime.now(timezone.utc).isoformat()
        
        self._execute(
            """
            INSERT INTO agents (agent_id, agent_type, wallet_address, started_at, last_heartbeat, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                agent_type = excluded.agent_type,
                wallet_address = excluded.wallet_address,
                started_at = excluded.started_at,
                last_heartbeat = excluded.last_heartbeat,
                status = excluded.status
            """,
            (agent_id, agent_type, wallet_address.lower(), now, now, AgentStatus.ACTIVE.value)
        )
        
        logger.info(f"Registered agent {agent_id} ({agent_type})")
        return True
    
    def update_heartbeat(self, agent_id: str) -> None:
        """Update agent heartbeat"""
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            "UPDATE agents SET last_heartbeat = ? WHERE agent_id = ?",
            (now, agent_id)
        )
    
    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """Get agent by ID"""
        row = self._fetchone(
            "SELECT * FROM agents WHERE agent_id = ?",
            (agent_id,)
        )
        return self._row_to_agent(row) if row else None
    
    def get_all_agents(self, wallet_address: Optional[str] = None) -> List[AgentInfo]:
        """Get all agents"""
        if wallet_address:
            rows = self._fetchall(
                "SELECT * FROM agents WHERE wallet_address = ?",
                (wallet_address.lower(),)
            )
        else:
            rows = self._fetchall("SELECT * FROM agents")
        
        return [self._row_to_agent(row) for row in rows]
    
    def update_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        """Update agent status"""
        self._execute(
            "UPDATE agents SET status = ? WHERE agent_id = ?",
            (status.value, agent_id)
        )
    
    def cleanup_stale_agents(self, stale_threshold_seconds: int) -> int:
        """Mark stale agents as crashed"""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_threshold_seconds)).isoformat()
        
        cursor = self._execute(
            """
            UPDATE agents 
            SET status = ? 
            WHERE status = ? AND last_heartbeat < ?
            """,
            (AgentStatus.CRASHED.value, AgentStatus.ACTIVE.value, cutoff)
        )
        
        count = cursor.rowcount
        if count > 0:
            logger.warning(f"Marked {count} stale agents as crashed")
        
        return count
    
    def _row_to_agent(self, row: sqlite3.Row) -> AgentInfo:
        """Convert database row to AgentInfo"""
        return AgentInfo(
            agent_id=row["agent_id"],
            agent_type=row["agent_type"],
            started_at=datetime.fromisoformat(row["started_at"]),
            last_heartbeat=datetime.fromisoformat(row["last_heartbeat"]),
            status=AgentStatus(row["status"]),
        )
    
    # ==================== RESERVATIONS ====================
    
    def create_reservation(
        self,
        agent_id: str,
        market_id: str,
        token_id: str,
        amount_usd: float,
        expires_at: datetime
    ) -> str:
        """Create a capital reservation"""
        reservation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        
        self._execute(
            """
            INSERT INTO reservations 
            (id, agent_id, market_id, token_id, amount_usd, reserved_at, expires_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reservation_id,
                agent_id,
                market_id,
                token_id,
                amount_usd,
                now,
                expires_at.isoformat(),
                ReservationStatus.PENDING.value
            )
        )
        
        logger.debug(f"Created reservation {reservation_id}: ${amount_usd:.2f}")
        return reservation_id
    
    def get_reservation(self, reservation_id: str) -> Optional[Reservation]:
        """Get reservation by ID"""
        row = self._fetchone(
            "SELECT * FROM reservations WHERE id = ?",
            (reservation_id,)
        )
        return self._row_to_reservation(row) if row else None
    
    def get_agent_reservations(self, agent_id: str) -> List[Reservation]:
        """Get active reservations for an agent"""
        rows = self._fetchall(
            "SELECT * FROM reservations WHERE agent_id = ? AND status = ?",
            (agent_id, ReservationStatus.PENDING.value)
        )
        return [self._row_to_reservation(row) for row in rows]
    
    def get_all_reservations(
        self,
        wallet_address: Optional[str] = None,
        status: Optional[ReservationStatus] = None
    ) -> List[Reservation]:
        """Get all reservations"""
        sql = "SELECT r.* FROM reservations r"
        params = []
        
        if wallet_address:
            sql += " JOIN agents a ON r.agent_id = a.agent_id WHERE a.wallet_address = ?"
            params.append(wallet_address.lower())
            if status:
                sql += " AND r.status = ?"
                params.append(status.value)
        elif status:
            sql += " WHERE status = ?"
            params.append(status.value)
        
        rows = self._fetchall(sql, tuple(params))
        return [self._row_to_reservation(row) for row in rows]
    
    def mark_reservation_executed(self, reservation_id: str, filled_amount: float) -> None:
        """Mark reservation as executed"""
        self._execute(
            "UPDATE reservations SET status = ?, filled_amount = ? WHERE id = ?",
            (ReservationStatus.EXECUTED.value, filled_amount, reservation_id)
        )
        logger.debug(f"Marked reservation {reservation_id} as executed (${filled_amount:.2f})")
    
    def release_reservation(self, reservation_id: str) -> None:
        """Release a reservation"""
        self._execute(
            "UPDATE reservations SET status = ? WHERE id = ?",
            (ReservationStatus.RELEASED.value, reservation_id)
        )
        logger.debug(f"Released reservation {reservation_id}")
    
    def release_all_reservations(self, agent_id: Optional[str] = None) -> int:
        """Release all reservations"""
        if agent_id:
            cursor = self._execute(
                "UPDATE reservations SET status = ? WHERE agent_id = ? AND status = ?",
                (ReservationStatus.RELEASED.value, agent_id, ReservationStatus.PENDING.value)
            )
        else:
            cursor = self._execute(
                "UPDATE reservations SET status = ? WHERE status = ?",
                (ReservationStatus.RELEASED.value, ReservationStatus.PENDING.value)
            )
        
        count = cursor.rowcount
        if count > 0:
            logger.info(f"Released {count} reservations")
        return count
    
    def cleanup_expired_reservations(self) -> int:
        """Mark expired reservations"""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self._execute(
            "UPDATE reservations SET status = ? WHERE status = ? AND expires_at < ?",
            (ReservationStatus.EXPIRED.value, ReservationStatus.PENDING.value, now)
        )
        return cursor.rowcount
    
    def _row_to_reservation(self, row: sqlite3.Row) -> Reservation:
        """Convert database row to Reservation"""
        return Reservation(
            id=row["id"],
            agent_id=row["agent_id"],
            market_id=row["market_id"],
            token_id=row["token_id"],
            amount_usd=float(row["amount_usd"]),
            reserved_at=datetime.fromisoformat(row["reserved_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            status=ReservationStatus(row["status"]),
        )
    
    # ==================== POSITIONS ====================
    
    def create_position(
        self,
        agent_id: str,
        market_id: str,
        token_id: str,
        outcome: str,
        shares: float,
        entry_price: float
    ) -> int:
        """Create a new position"""
        now = datetime.now(timezone.utc).isoformat()
        
        cursor = self._execute(
            """
            INSERT INTO positions 
            (agent_id, market_id, token_id, outcome, shares, entry_price, entry_time, current_price, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                market_id,
                token_id,
                outcome,
                shares,
                entry_price,
                now,
                entry_price,  # current_price starts as entry_price
                PositionStatus.OPEN.value
            )
        )
        
        position_id = cursor.lastrowid
        logger.info(f"Created position {position_id}: {shares:.2f} shares @ ${entry_price:.4f}")
        return position_id
    
    def get_position(self, position_id: int) -> Optional[Position]:
        """Get position by ID"""
        row = self._fetchone(
            "SELECT * FROM positions WHERE id = ?",
            (position_id,)
        )
        return self._row_to_position(row) if row else None
    
    def get_agent_positions(
        self,
        agent_id: str,
        status: Optional[PositionStatus] = None
    ) -> List[Position]:
        """Get positions for an agent"""
        if status:
            rows = self._fetchall(
                "SELECT * FROM positions WHERE agent_id = ? AND status = ?",
                (agent_id, status.value)
            )
        else:
            rows = self._fetchall(
                "SELECT * FROM positions WHERE agent_id = ?",
                (agent_id,)
            )
        return [self._row_to_position(row) for row in rows]
    
    def get_all_positions(
        self,
        wallet_address: Optional[str] = None,
        status: Optional[PositionStatus] = None
    ) -> List[Position]:
        """Get all positions"""
        sql = "SELECT p.* FROM positions p"
        params = []
        conditions = []
        
        if wallet_address:
            # Join with agents table OR match orphan agent pattern
            # Orphan positions have agent_id like 'orphan_{wallet[:8]}'
            wallet_lower = wallet_address.lower()
            orphan_prefix = f"orphan_{wallet_lower[:8]}"
            sql += " LEFT JOIN agents a ON p.agent_id = a.agent_id"
            conditions.append("(a.wallet_address = ? OR p.agent_id LIKE ?)")
            params.extend([wallet_lower, f"{orphan_prefix}%"])
        
        if status:
            conditions.append("p.status = ?")
            params.append(status.value)
        
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        
        rows = self._fetchall(sql, tuple(params))
        return [self._row_to_position(row) for row in rows]
    
    def update_position_price(self, position_id: int, current_price: float) -> None:
        """Update position current price"""
        self._execute(
            "UPDATE positions SET current_price = ? WHERE id = ?",
            (current_price, position_id)
        )
    
    def mark_position_closed(self, position_id: int) -> None:
        """Mark position as closed"""
        self._execute(
            "UPDATE positions SET status = ? WHERE id = ?",
            (PositionStatus.CLOSED.value, position_id)
        )
        logger.info(f"Marked position {position_id} as closed")
    
    def mark_position_closed_by_token(self, wallet_address: str, token_id: str) -> int:
        """
        Mark all positions with the given token_id as closed.
        
        Returns number of positions closed.
        """
        result = self._execute(
            """
            UPDATE positions 
            SET status = ? 
            WHERE token_id = ? AND status != ?
            """,
            (PositionStatus.CLOSED.value, token_id, PositionStatus.CLOSED.value)
        )
        count = result.rowcount if result else 0
        if count > 0:
            logger.info(f"Marked {count} position(s) for token {token_id[:20]}... as closed")
        return count
    
    
    def _row_to_position(self, row: sqlite3.Row) -> Position:
        """Convert database row to Position"""
        return Position(
            id=row["id"],
            agent_id=row["agent_id"],
            market_id=row["market_id"],
            token_id=row["token_id"],
            outcome=row["outcome"],
            shares=float(row["shares"]),
            entry_price=float(row["entry_price"]),
            entry_time=datetime.fromisoformat(row["entry_time"]) if row["entry_time"] else None,
            current_price=float(row["current_price"]) if row["current_price"] else None,
            status=PositionStatus(row["status"]),
        )
    
    # ==================== EXPOSURE CALCULATIONS ====================
    
    def get_total_exposure(self, wallet_address: str) -> float:
        """Get total exposure for wallet"""
        # Positions value
        positions = self.get_all_positions(wallet_address, PositionStatus.OPEN)
        positions_value = sum(p.current_value for p in positions)
        
        # Active reservations
        reservations = self.get_all_reservations(wallet_address, ReservationStatus.PENDING)
        reserved = sum(r.amount_usd for r in reservations if r.is_active)
        
        return positions_value + reserved
    
    def get_agent_exposure(self, agent_id: str) -> float:
        """Get exposure for an agent"""
        positions = self.get_agent_positions(agent_id, PositionStatus.OPEN)
        positions_value = sum(p.current_value for p in positions)
        
        reservations = self.get_agent_reservations(agent_id)
        reserved = sum(r.amount_usd for r in reservations if r.is_active)
        
        return positions_value + reserved
    
    def get_market_exposure(self, market_id: str, wallet_address: str) -> float:
        """Get exposure in a specific market"""
        # Get positions in this market
        row = self._fetchone(
            """
            SELECT COALESCE(SUM(p.shares * p.current_price), 0) as exposure
            FROM positions p
            JOIN agents a ON p.agent_id = a.agent_id
            WHERE p.market_id = ? AND a.wallet_address = ? AND p.status = ?
            """,
            (market_id, wallet_address.lower(), PositionStatus.OPEN.value)
        )
        positions_exposure = float(row["exposure"]) if row else 0.0
        
        # Get reservations in this market
        row = self._fetchone(
            """
            SELECT COALESCE(SUM(r.amount_usd), 0) as reserved
            FROM reservations r
            JOIN agents a ON r.agent_id = a.agent_id
            WHERE r.market_id = ? AND a.wallet_address = ? AND r.status = ?
            """,
            (market_id, wallet_address.lower(), ReservationStatus.PENDING.value)
        )
        reserved = float(row["reserved"]) if row else 0.0
        
        return positions_exposure + reserved
    
    # ==================== RATE LIMITING ====================
    
    def log_request(self, agent_id: str, endpoint: str, timestamp: datetime) -> None:
        """Log an API request"""
        self._execute(
            "INSERT INTO request_log (agent_id, endpoint, timestamp) VALUES (?, ?, ?)",
            (agent_id, endpoint, timestamp.isoformat())
        )
    
    def count_requests_since(self, since: datetime) -> int:
        """Count requests since timestamp"""
        row = self._fetchone(
            "SELECT COUNT(*) as count FROM request_log WHERE timestamp > ?",
            (since.isoformat(),)
        )
        return int(row["count"]) if row else 0
    
    def cleanup_old_requests(self, before: datetime) -> int:
        """Remove old request logs"""
        cursor = self._execute(
            "DELETE FROM request_log WHERE timestamp < ?",
            (before.isoformat(),)
        )
        return cursor.rowcount
    
    # ==================== EXECUTION HISTORY ====================
    
    def save_execution(
        self,
        agent_id: str,
        market_id: str,
        token_id: str,
        side: "Side",
        shares: float,
        price: float,
        filled_price: float,
        signal_score: float,
        success: bool,
        error_message: Optional[str] = None
    ) -> int:
        """Save execution result"""
        from ...core.models import Side
        
        cursor = self._execute(
            """
            INSERT INTO executions (
                agent_id, market_id, token_id, side, shares, price,
                filled_price, signal_score, success, error_message, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id, market_id, token_id, side.value, shares, price,
                filled_price, signal_score, 1 if success else 0,
                error_message, datetime.now(timezone.utc).isoformat()
            )
        )
        return cursor.lastrowid
    
    def get_executions(
        self,
        agent_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        wallet_address: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[dict]:
        """Get execution history"""
        from ...core.models import Side
        
        # Build query - always join agents to get wallet_address
        query = "SELECT e.*, a.wallet_address FROM executions e JOIN agents a ON e.agent_id = a.agent_id"
        params = []
        conditions = []
        
        if wallet_address:
            conditions.append("a.wallet_address = ?")
            params.append(wallet_address.lower())
        
        if agent_id:
            conditions.append("e.agent_id = ?")
            params.append(agent_id)
        
        if start_time:
            conditions.append("e.timestamp >= ?")
            params.append(start_time.isoformat())
        
        if end_time:
            conditions.append("e.timestamp <= ?")
            params.append(end_time.isoformat())

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY e.timestamp DESC"
        if limit:
            query += f" LIMIT {limit}"
        
        rows = self._fetchall(query, tuple(params))
        return [
            {
                "id": row["id"],
                "agent_id": row["agent_id"],
                "market_id": row["market_id"],
                "token_id": row["token_id"],
                "side": row["side"],
                "shares": row["shares"],
                "price": row["price"],
                "filled_price": row["filled_price"],
                "signal_score": row["signal_score"],
                "success": bool(row["success"]),
                "error_message": row["error_message"],
                "timestamp": datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                "wallet_address": row["wallet_address"]
            }
            for row in rows
        ]
    
    # Claims are now tracked in the transactions table via chain sync.
    # Use get_transactions(transaction_type='claim') for claim data.
    
    # ==================== ON-CHAIN TRANSACTIONS (SOURCE OF TRUTH) ====================
    
    def upsert_transaction(
        self,
        tx_hash: str,
        log_index: int,
        block_number: int,
        block_timestamp: datetime,
        transaction_type: str,
        wallet_address: str,
        token_id: Optional[str] = None,
        market_id: Optional[str] = None,
        outcome: Optional[str] = None,
        shares: Optional[float] = None,
        price_per_share: Optional[float] = None,
        usdc_amount: Optional[float] = None,
        agent_id: Optional[str] = None,
        raw_event: Optional[str] = None
    ) -> int:
        """
        Insert or update an on-chain transaction.
        
        This is the source of truth for all wallet activity.
        Uses UPSERT to handle duplicate inserts gracefully.
        
        Returns the transaction ID.
        """
        import json
        now = datetime.now(timezone.utc).isoformat()
        
        cursor = self._execute(
            """
            INSERT INTO transactions (
                tx_hash, log_index, block_number, block_timestamp,
                transaction_type, wallet_address, token_id, market_id, outcome,
                shares, price_per_share, usdc_amount, agent_id, raw_event, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tx_hash, log_index) DO UPDATE SET
                block_number = excluded.block_number,
                block_timestamp = excluded.block_timestamp,
                transaction_type = excluded.transaction_type,
                wallet_address = excluded.wallet_address,
                token_id = excluded.token_id,
                market_id = excluded.market_id,
                outcome = excluded.outcome,
                shares = excluded.shares,
                price_per_share = excluded.price_per_share,
                usdc_amount = excluded.usdc_amount,
                agent_id = COALESCE(transactions.agent_id, excluded.agent_id),
                raw_event = excluded.raw_event,
                synced_at = excluded.synced_at
            """,
            (
                tx_hash,
                log_index,
                block_number,
                block_timestamp.isoformat(),
                transaction_type,
                wallet_address.lower(),
                token_id,
                market_id,
                outcome,
                shares,
                price_per_share,
                usdc_amount,
                agent_id,
                raw_event,
                now
            )
        )
        
        return cursor.lastrowid or 0
    
    def get_transaction(self, tx_hash: str, log_index: int = 0) -> Optional[dict]:
        """Get a transaction by hash and log index"""
        row = self._fetchone(
            "SELECT * FROM transactions WHERE tx_hash = ? AND log_index = ?",
            (tx_hash, log_index)
        )
        return self._row_to_transaction(row) if row else None
    
    def get_transactions(
        self,
        wallet_address: Optional[str] = None,
        transaction_type: Optional[str] = None,
        token_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        start_block: Optional[int] = None,
        end_block: Optional[int] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[dict]:
        """Get transactions with filters"""
        query = "SELECT * FROM transactions WHERE 1=1"
        params = []
        
        if wallet_address:
            query += " AND wallet_address = ?"
            params.append(wallet_address.lower())
        
        if transaction_type:
            query += " AND transaction_type = ?"
            params.append(transaction_type)
        
        if token_id:
            query += " AND token_id = ?"
            params.append(token_id)
        
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        
        if start_block is not None:
            query += " AND block_number >= ?"
            params.append(start_block)
        
        if end_block is not None:
            query += " AND block_number <= ?"
            params.append(end_block)
        
        if start_time:
            query += " AND block_timestamp >= ?"
            params.append(start_time.isoformat())
        
        if end_time:
            query += " AND block_timestamp <= ?"
            params.append(end_time.isoformat())
        
        query += " ORDER BY block_number DESC, log_index DESC"
        
        if limit:
            query += f" LIMIT {limit}"
        
        rows = self._fetchall(query, tuple(params))
        return [self._row_to_transaction(row) for row in rows]
    
    def get_computed_positions(
        self,
        wallet_address: str,
        include_zero_balances: bool = False
    ) -> List[dict]:
        """
        Compute current positions from transaction history.
        
        This aggregates all buys, sells, and claims to derive net share holdings.
        This is the NEW way to get positions - derived from on-chain source of truth.
        """
        query = """
            SELECT 
                token_id,
                market_id,
                outcome,
                agent_id,
                SUM(CASE WHEN transaction_type = 'buy' THEN shares ELSE 0 END) as total_bought,
                SUM(CASE WHEN transaction_type = 'sell' THEN shares ELSE 0 END) as total_sold,
                SUM(CASE WHEN transaction_type = 'claim' THEN shares ELSE 0 END) as total_claimed,
                SUM(CASE WHEN transaction_type = 'buy' THEN shares ELSE 0 END) -
                SUM(CASE WHEN transaction_type = 'sell' THEN shares ELSE 0 END) -
                SUM(CASE WHEN transaction_type = 'claim' THEN shares ELSE 0 END) as net_shares,
                AVG(CASE WHEN transaction_type = 'buy' THEN price_per_share END) as avg_entry_price,
                MIN(CASE WHEN transaction_type = 'buy' THEN block_timestamp END) as first_buy_time,
                MAX(block_timestamp) as last_activity_time,
                SUM(CASE WHEN transaction_type = 'buy' THEN usdc_amount ELSE 0 END) as total_cost,
                SUM(CASE WHEN transaction_type = 'sell' THEN usdc_amount ELSE 0 END) as total_proceeds,
                SUM(CASE WHEN transaction_type = 'claim' THEN usdc_amount ELSE 0 END) as total_claimed_value
            FROM transactions
            WHERE wallet_address = ? AND token_id IS NOT NULL
            GROUP BY token_id, market_id
        """
        
        if not include_zero_balances:
            query += " HAVING net_shares > 0.0001"
        
        query += " ORDER BY last_activity_time DESC"
        
        rows = self._fetchall(query, (wallet_address.lower(),))
        
        positions = []
        for row in rows:
            positions.append({
                "token_id": row["token_id"],
                "market_id": row["market_id"],
                "outcome": row["outcome"],
                "agent_id": row["agent_id"],
                "shares": float(row["net_shares"]) if row["net_shares"] else 0.0,
                "total_bought": float(row["total_bought"]) if row["total_bought"] else 0.0,
                "total_sold": float(row["total_sold"]) if row["total_sold"] else 0.0,
                "total_claimed": float(row["total_claimed"]) if row["total_claimed"] else 0.0,
                "avg_entry_price": float(row["avg_entry_price"]) if row["avg_entry_price"] else 0.0,
                "first_buy_time": datetime.fromisoformat(row["first_buy_time"].replace("Z", "+00:00")) if row["first_buy_time"] else None,
                "last_activity_time": datetime.fromisoformat(row["last_activity_time"].replace("Z", "+00:00")) if row["last_activity_time"] else None,
                "total_cost": float(row["total_cost"]) if row["total_cost"] else 0.0,
                "total_proceeds": float(row["total_proceeds"]) if row["total_proceeds"] else 0.0,
                "total_claimed_value": float(row["total_claimed_value"]) if row["total_claimed_value"] else 0.0,
            })
        
        return positions
    
    def get_agent_computed_exposure(self, agent_id: str, current_prices: Optional[dict] = None) -> float:
        """
        Get exposure for an agent from transaction history.
        
        Args:
            agent_id: The agent ID to calculate exposure for
            current_prices: Optional dict of token_id -> current_price for valuation
        
        Returns total position value (shares * price) for the agent.
        """
        query = """
            SELECT 
                token_id,
                SUM(CASE WHEN transaction_type = 'buy' THEN shares ELSE 0 END) -
                SUM(CASE WHEN transaction_type = 'sell' THEN shares ELSE 0 END) -
                SUM(CASE WHEN transaction_type = 'claim' THEN shares ELSE 0 END) as net_shares,
                AVG(CASE WHEN transaction_type = 'buy' THEN price_per_share END) as avg_price
            FROM transactions
            WHERE agent_id = ? AND token_id IS NOT NULL
            GROUP BY token_id
            HAVING net_shares > 0.0001
        """
        
        rows = self._fetchall(query, (agent_id,))
        
        total_exposure = 0.0
        for row in rows:
            shares = float(row["net_shares"]) if row["net_shares"] else 0.0
            # Use current price if provided, otherwise use avg entry price
            if current_prices and row["token_id"] in current_prices:
                price = current_prices[row["token_id"]]
            else:
                price = float(row["avg_price"]) if row["avg_price"] else 0.0
            total_exposure += shares * price
        
        return total_exposure
    
    def get_market_computed_exposure(
        self,
        market_id: str,
        wallet_address: str,
        current_prices: Optional[dict] = None
    ) -> float:
        """
        Get exposure in a specific market from transaction history.
        """
        query = """
            SELECT 
                token_id,
                SUM(CASE WHEN transaction_type = 'buy' THEN shares ELSE 0 END) -
                SUM(CASE WHEN transaction_type = 'sell' THEN shares ELSE 0 END) -
                SUM(CASE WHEN transaction_type = 'claim' THEN shares ELSE 0 END) as net_shares,
                AVG(CASE WHEN transaction_type = 'buy' THEN price_per_share END) as avg_price
            FROM transactions
            WHERE wallet_address = ? AND market_id = ? AND token_id IS NOT NULL
            GROUP BY token_id
            HAVING net_shares > 0.0001
        """
        
        rows = self._fetchall(query, (wallet_address.lower(), market_id))
        
        total_exposure = 0.0
        for row in rows:
            shares = float(row["net_shares"]) if row["net_shares"] else 0.0
            if current_prices and row["token_id"] in current_prices:
                price = current_prices[row["token_id"]]
            else:
                price = float(row["avg_price"]) if row["avg_price"] else 0.0
            total_exposure += shares * price
        
        return total_exposure
    
    def get_total_computed_exposure(
        self,
        wallet_address: str,
        current_prices: Optional[dict] = None
    ) -> float:
        """
        Get total exposure for wallet from transaction history.
        """
        query = """
            SELECT 
                token_id,
                SUM(CASE WHEN transaction_type = 'buy' THEN shares ELSE 0 END) -
                SUM(CASE WHEN transaction_type = 'sell' THEN shares ELSE 0 END) -
                SUM(CASE WHEN transaction_type = 'claim' THEN shares ELSE 0 END) as net_shares,
                AVG(CASE WHEN transaction_type = 'buy' THEN price_per_share END) as avg_price
            FROM transactions
            WHERE wallet_address = ? AND token_id IS NOT NULL
            GROUP BY token_id
            HAVING net_shares > 0.0001
        """
        
        rows = self._fetchall(query, (wallet_address.lower(),))
        
        total_exposure = 0.0
        for row in rows:
            shares = float(row["net_shares"]) if row["net_shares"] else 0.0
            if current_prices and row["token_id"] in current_prices:
                price = current_prices[row["token_id"]]
            else:
                price = float(row["avg_price"]) if row["avg_price"] else 0.0
            total_exposure += shares * price
        
        return total_exposure
    
    def link_transaction_to_agent(self, tx_hash: str, log_index: int, agent_id: str) -> bool:
        """Link an existing transaction to an agent (for attribution)"""
        cursor = self._execute(
            "UPDATE transactions SET agent_id = ? WHERE tx_hash = ? AND log_index = ?",
            (agent_id, tx_hash, log_index)
        )
        return cursor.rowcount > 0
    
    def count_transactions(self, wallet_address: Optional[str] = None) -> int:
        """Count total transactions"""
        if wallet_address:
            row = self._fetchone(
                "SELECT COUNT(*) as count FROM transactions WHERE wallet_address = ?",
                (wallet_address.lower(),)
            )
        else:
            row = self._fetchone("SELECT COUNT(*) as count FROM transactions")
        return int(row["count"]) if row else 0
    
    def get_transaction_summary(self, wallet_address: str) -> dict:
        """Get summary of all transactions for a wallet"""
        query = """
            SELECT 
                transaction_type,
                COUNT(*) as count,
                SUM(COALESCE(usdc_amount, 0)) as total_usdc,
                SUM(COALESCE(shares, 0)) as total_shares
            FROM transactions
            WHERE wallet_address = ?
            GROUP BY transaction_type
        """
        rows = self._fetchall(query, (wallet_address.lower(),))
        
        summary = {
            "buy": {"count": 0, "total_usdc": 0.0, "total_shares": 0.0},
            "sell": {"count": 0, "total_usdc": 0.0, "total_shares": 0.0},
            "claim": {"count": 0, "total_usdc": 0.0, "total_shares": 0.0},
            "deposit": {"count": 0, "total_usdc": 0.0, "total_shares": 0.0},
            "withdrawal": {"count": 0, "total_usdc": 0.0, "total_shares": 0.0},
        }
        
        for row in rows:
            tx_type = row["transaction_type"]
            if tx_type in summary:
                summary[tx_type] = {
                    "count": int(row["count"]),
                    "total_usdc": float(row["total_usdc"]) if row["total_usdc"] else 0.0,
                    "total_shares": float(row["total_shares"]) if row["total_shares"] else 0.0,
                }
        
        return summary
    
    def _row_to_transaction(self, row: sqlite3.Row) -> dict:
        """Convert database row to transaction dict"""
        import json
        return {
            "id": row["id"],
            "tx_hash": row["tx_hash"],
            "log_index": row["log_index"],
            "block_number": row["block_number"],
            "block_timestamp": datetime.fromisoformat(row["block_timestamp"].replace("Z", "+00:00")),
            "transaction_type": row["transaction_type"],
            "wallet_address": row["wallet_address"],
            "token_id": row["token_id"],
            "market_id": row["market_id"],
            "outcome": row["outcome"],
            "shares": float(row["shares"]) if row["shares"] else None,
            "price_per_share": float(row["price_per_share"]) if row["price_per_share"] else None,
            "usdc_amount": float(row["usdc_amount"]) if row["usdc_amount"] else None,
            "agent_id": row["agent_id"],
            "raw_event": json.loads(row["raw_event"]) if row["raw_event"] else None,
            "synced_at": datetime.fromisoformat(row["synced_at"].replace("Z", "+00:00")),
        }
    
    # ==================== CHAIN SYNC STATE ====================
    
    def get_chain_sync_state(self, wallet_address: str) -> Optional[dict]:
        """Get chain sync state for a wallet"""
        row = self._fetchone(
            "SELECT * FROM chain_sync_state WHERE wallet_address = ?",
            (wallet_address.lower(),)
        )
        if not row:
            return None
        return {
            "wallet_address": row["wallet_address"],
            "last_synced_block": row["last_synced_block"],
            "last_sync_time": datetime.fromisoformat(row["last_sync_time"].replace("Z", "+00:00")),
            "total_transactions": row["total_transactions"],
        }
    
    def update_chain_sync_state(
        self,
        wallet_address: str,
        last_synced_block: int,
        total_transactions: int
    ) -> None:
        """Update chain sync state for a wallet"""
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            """
            INSERT INTO chain_sync_state (id, wallet_address, last_synced_block, last_sync_time, total_transactions)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                wallet_address = excluded.wallet_address,
                last_synced_block = excluded.last_synced_block,
                last_sync_time = excluded.last_sync_time,
                total_transactions = excluded.total_transactions
            """,
            (wallet_address.lower(), last_synced_block, now, total_transactions)
        )
    
    # ==================== DRAWDOWN STATE ====================
    
    def get_drawdown_state(self, wallet_address: str) -> Optional[dict]:
        """Get persisted drawdown state for a wallet"""
        row = self._fetchone(
            "SELECT * FROM drawdown_state WHERE wallet_address = ?",
            (wallet_address.lower(),)
        )
        if not row:
            return None
        
        daily_start_date = None
        if row["daily_start_date"]:
            try:
                daily_start_date = datetime.fromisoformat(row["daily_start_date"].replace("Z", "+00:00"))
            except:
                pass
        
        return {
            "wallet_address": row["wallet_address"],
            "peak_equity": row["peak_equity"],
            "daily_start_equity": row["daily_start_equity"],
            "daily_start_date": daily_start_date,
            "is_breached": bool(row["is_breached"]),
            "breach_reason": row["breach_reason"],
            "updated_at": datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")) if row["updated_at"] else None,
        }
    
    def update_drawdown_state(
        self,
        wallet_address: str,
        peak_equity: float,
        daily_start_equity: float,
        daily_start_date: datetime,
        is_breached: bool = False,
        breach_reason: Optional[str] = None
    ) -> None:
        """Update persisted drawdown state for a wallet"""
        now = datetime.now(timezone.utc).isoformat()
        self._execute(
            """
            INSERT INTO drawdown_state (id, wallet_address, peak_equity, daily_start_equity, daily_start_date, is_breached, breach_reason, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                wallet_address = excluded.wallet_address,
                peak_equity = excluded.peak_equity,
                daily_start_equity = excluded.daily_start_equity,
                daily_start_date = excluded.daily_start_date,
                is_breached = excluded.is_breached,
                breach_reason = excluded.breach_reason,
                updated_at = excluded.updated_at
            """,
            (
                wallet_address.lower(),
                peak_equity,
                daily_start_equity,
                daily_start_date.isoformat(),
                1 if is_breached else 0,
                breach_reason,
                now
            )
        )
    
    # ==================== FLOW ALERTS ====================
    
    def save_alert(
        self,
        alert_type: str,
        market_id: str,
        token_id: str,
        question: str,
        timestamp: datetime,
        severity: str,
        reason: str,
        details: dict,
        category: str,
        score: Optional[float] = None
    ) -> int:
        """Save flow alert"""
        import json
        
        cursor = self._execute(
            """
            INSERT INTO flow_alerts (
                alert_type, market_id, token_id, question, timestamp,
                severity, reason, details, category, score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_type, market_id, token_id, question, timestamp.isoformat(),
                severity, reason, json.dumps(details), category, score
            )
        )
        return cursor.lastrowid
    
    def get_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        alert_type: Optional[str] = None,
        severity: Optional[str] = None,
        category: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[dict]:
        """Get alerts with filters"""
        import json
        
        query = "SELECT * FROM flow_alerts WHERE 1=1"
        params = []
        
        if start_time:
            query += " AND timestamp >= ?"
            params.append(start_time.isoformat())
        
        if end_time:
            query += " AND timestamp <= ?"
            params.append(end_time.isoformat())
        
        if alert_type:
            query += " AND alert_type = ?"
            params.append(alert_type)
        
        if severity:
            query += " AND severity = ?"
            params.append(severity)
        
        if category:
            query += " AND category = ?"
            params.append(category)
        
        query += " ORDER BY timestamp DESC"
        
        if limit:
            query += f" LIMIT {limit}"
        
        rows = self._fetchall(query, tuple(params))
        return [
            {
                "id": row["id"],
                "alert_type": row["alert_type"],
                "market_id": row["market_id"],
                "token_id": row["token_id"],
                "question": row["question"],
                "timestamp": datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                "severity": row["severity"],
                "reason": row["reason"],
                "details": json.loads(row["details"]) if row["details"] else {},
                "category": row["category"],
                "score": row["score"]
            }
            for row in rows
        ]
    
    def get_alert_stats(self) -> dict:
        """Get alert statistics"""
        total = self._fetchone("SELECT COUNT(*) as count FROM flow_alerts")
        by_type = self._fetchall(
            "SELECT alert_type, COUNT(*) as count FROM flow_alerts GROUP BY alert_type"
        )
        by_severity = self._fetchall(
            "SELECT severity, COUNT(*) as count FROM flow_alerts GROUP BY severity"
        )
        
        return {
            "total": int(total["count"]) if total else 0,
            "by_type": {row["alert_type"]: int(row["count"]) for row in by_type},
            "by_severity": {row["severity"]: int(row["count"]) for row in by_severity}
        }


class SQLiteStorage(StorageBackend):
    """
    SQLite storage backend.
    
    Uses WAL mode for better concurrent access.
    Creates database and tables on initialization.
    """
    
    def __init__(self, db_path: str = "data/risk_state.db"):
        self.db_path = db_path
        self._ensure_directory()
        self.initialize()
    
    def _ensure_directory(self):
        """Ensure database directory exists"""
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
    
    def initialize(self) -> None:
        """Create database tables"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        
        # Create tables
        conn.executescript("""
            -- Wallet balances cache
            CREATE TABLE IF NOT EXISTS wallet_balances (
                wallet_address TEXT PRIMARY KEY,
                balance REAL NOT NULL,
                updated_at TEXT NOT NULL
            );
            
            -- Active agents
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                agent_type TEXT NOT NULL,
                wallet_address TEXT NOT NULL,
                started_at TEXT NOT NULL,
                last_heartbeat TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE INDEX IF NOT EXISTS idx_agents_wallet ON agents(wallet_address);
            CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
            
            -- Capital reservations
            CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                reserved_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                filled_amount REAL,
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reservations_agent ON reservations(agent_id);
            CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);
            CREATE INDEX IF NOT EXISTS idx_reservations_market ON reservations(market_id);
            
            -- Positions
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                shares REAL NOT NULL,
                entry_price REAL NOT NULL,
                entry_time TEXT NOT NULL,
                current_price REAL,
                status TEXT NOT NULL DEFAULT 'open',
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_positions_agent ON positions(agent_id);
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
            CREATE INDEX IF NOT EXISTS idx_positions_token ON positions(token_id);
            
            -- Request log for rate limiting
            CREATE TABLE IF NOT EXISTS request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_request_log_timestamp ON request_log(timestamp);
            
            -- Execution history
            CREATE TABLE IF NOT EXISTS executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                shares REAL NOT NULL,
                price REAL NOT NULL,
                filled_price REAL NOT NULL,
                signal_score REAL,
                success INTEGER NOT NULL DEFAULT 1,
                error_message TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_executions_agent ON executions(agent_id);
            CREATE INDEX IF NOT EXISTS idx_executions_timestamp ON executions(timestamp);
            CREATE INDEX IF NOT EXISTS idx_executions_market ON executions(market_id);
            
            -- Flow alerts
            CREATE TABLE IF NOT EXISTS flow_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                question TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                severity TEXT NOT NULL,
                reason TEXT NOT NULL,
                details TEXT NOT NULL,
                category TEXT NOT NULL,
                score REAL
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON flow_alerts(timestamp);
            CREATE INDEX IF NOT EXISTS idx_alerts_type ON flow_alerts(alert_type);
            CREATE INDEX IF NOT EXISTS idx_alerts_severity ON flow_alerts(severity);
            CREATE INDEX IF NOT EXISTS idx_alerts_market ON flow_alerts(market_id);
            
            -- Claims/redemptions from resolved markets
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                shares REAL NOT NULL,
                entry_price REAL NOT NULL,
                claim_price REAL NOT NULL DEFAULT 1.0,
                claim_time TEXT NOT NULL,
                realized_pnl REAL NOT NULL,
                notes TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_claims_agent ON claims(agent_id);
            CREATE INDEX IF NOT EXISTS idx_claims_market ON claims(market_id);
            
            -- On-chain transactions (SOURCE OF TRUTH)
            -- All buys, sells, claims, deposits, withdrawals with on-chain timestamps
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_hash TEXT NOT NULL,
                log_index INTEGER NOT NULL DEFAULT 0,
                block_number INTEGER NOT NULL,
                block_timestamp TEXT NOT NULL,
                
                transaction_type TEXT NOT NULL,  -- 'buy', 'sell', 'claim', 'deposit', 'withdrawal'
                wallet_address TEXT NOT NULL,
                token_id TEXT,
                market_id TEXT,
                outcome TEXT,
                
                shares REAL,
                price_per_share REAL,
                usdc_amount REAL,
                
                agent_id TEXT,  -- Linked from executions, NULL for orphans
                
                raw_event TEXT,  -- JSON of full event data
                synced_at TEXT NOT NULL,
                
                UNIQUE(tx_hash, log_index)
            );
            CREATE INDEX IF NOT EXISTS idx_transactions_wallet ON transactions(wallet_address);
            CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions(transaction_type);
            CREATE INDEX IF NOT EXISTS idx_transactions_token ON transactions(token_id);
            CREATE INDEX IF NOT EXISTS idx_transactions_block ON transactions(block_number);
            CREATE INDEX IF NOT EXISTS idx_transactions_timestamp ON transactions(block_timestamp);
            CREATE INDEX IF NOT EXISTS idx_transactions_agent ON transactions(agent_id);
            
            -- Chain sync state
            CREATE TABLE IF NOT EXISTS chain_sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                wallet_address TEXT NOT NULL,
                last_synced_block INTEGER NOT NULL DEFAULT 0,
                last_sync_time TEXT NOT NULL,
                total_transactions INTEGER NOT NULL DEFAULT 0
            );
            
            -- Drawdown state (persists across bot restarts)
            CREATE TABLE IF NOT EXISTS drawdown_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                wallet_address TEXT NOT NULL,
                peak_equity REAL NOT NULL DEFAULT 0,
                daily_start_equity REAL NOT NULL DEFAULT 0,
                daily_start_date TEXT NOT NULL,
                is_breached INTEGER NOT NULL DEFAULT 0,
                breach_reason TEXT,
                updated_at TEXT NOT NULL
            );
        """)
        
        conn.commit()
        conn.close()
        
        logger.info(f"SQLite storage initialized: {self.db_path}")
    
    @contextmanager
    def transaction(self) -> Generator[SQLiteTransaction, None, None]:
        """Create a transaction context"""
        conn = sqlite3.connect(self.db_path, isolation_level='IMMEDIATE')
        conn.execute("PRAGMA journal_mode=WAL")
        
        try:
            txn = SQLiteTransaction(conn)
            yield txn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def close(self) -> None:
        """Close storage (no-op for SQLite, connections are per-transaction)"""
        pass


