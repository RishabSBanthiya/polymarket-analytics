"""
Multi-Agent Risk Coordinator.

Centralized risk management for multiple trading agents sharing the same wallet.
Provides:
- Atomic capital reservation (no race conditions)
- State reconciliation on startup (using chain sync for source of truth)
- Agent heartbeat monitoring
- Exposure limit enforcement

The coordinator now uses the transactions table as the source of truth,
syncing on-chain data via the ChainSyncService.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple, Dict, TYPE_CHECKING

from ..core.models import Position, WalletState, PositionStatus, ReservationStatus
from ..core.config import RiskConfig, Config, get_config
from ..core.api import PolymarketAPI
from .storage.base import StorageBackend
from .storage.sqlite import SQLiteStorage
from .chain_sync import ChainSyncService

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class RiskCoordinator:
    """
    Centralized risk management for multi-agent trading.
    
    All agents MUST use the same RiskCoordinator instance (or at least
    the same storage backend) to ensure proper coordination.
    
    Usage:
        coordinator = RiskCoordinator(config=Config.from_env())
        await coordinator.startup("my-agent", "bond")
        
        # Before trading:
        reservation_id = coordinator.atomic_reserve(
            "my-agent", market_id, token_id, amount_usd
        )
        if reservation_id:
            try:
                result = await execute_trade(...)
                coordinator.confirm_execution(reservation_id, result.shares, result.price)
            except:
                coordinator.release_reservation(reservation_id)
    """
    
    def __init__(
        self,
        config: Optional[Config] = None,
        storage: Optional[StorageBackend] = None,
        api: Optional[PolymarketAPI] = None
    ):
        self.config = config or get_config()
        self.storage = storage or SQLiteStorage(self.config.db_path)
        self.api = api
        self.wallet_address = self.config.proxy_address or ""
        self._reconciled = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._current_agent_id: Optional[str] = None
        self._chain_sync: Optional[ChainSyncService] = None
        self._current_prices: Dict[str, float] = {}  # Cache for current prices
    
    async def startup(self, agent_id: str, agent_type: str) -> bool:
        """
        Initialize coordinator for an agent.
        
        MUST be called before any trading operations.
        
        Steps:
        1. Register agent
        2. Reconcile DB state with on-chain reality
        3. Start heartbeat loop
        
        Returns True if startup successful.
        """
        if not self.wallet_address:
            logger.error("No wallet address configured")
            return False
        
        # Initialize API if not provided
        if self.api is None:
            self.api = PolymarketAPI(self.config)
            await self.api.connect()
        
        # Register agent (or restart if already exists)
        try:
            with self.storage.transaction() as txn:
                if not txn.register_agent(agent_id, agent_type, self.wallet_address):
                    logger.error(f"Failed to register agent {agent_id}")
                    return False
        except Exception as e:
            logger.error(f"Error registering agent {agent_id}: {e}")
            return False
        
        self._current_agent_id = agent_id
        
        # Reconcile state
        await self._reconcile_state()
        self._reconciled = True
        
        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(agent_id)
        )
        
        logger.info(f"RiskCoordinator started for agent {agent_id} ({agent_type})")
        return True
    
    async def shutdown(self):
        """Shutdown coordinator gracefully"""
        # Cancel heartbeat
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        
        # Mark agent as stopped
        if self._current_agent_id:
            from ..core.models import AgentStatus
            with self.storage.transaction() as txn:
                txn.update_agent_status(self._current_agent_id, AgentStatus.STOPPED)
        
        # Close chain sync service
        if self._chain_sync:
            await self._chain_sync.close()
        
        # Close API
        if self.api:
            await self.api.close()
        
        logger.info("RiskCoordinator shutdown complete")
    
    async def _reconcile_state(self):
        """
        CRITICAL: Sync DB state with on-chain reality via chain sync.
        
        This method:
        1. Syncs all on-chain transactions to the transactions table
        2. Computes positions from transaction history
        3. Verifies computed positions match on-chain state
        4. Updates USDC balance and price cache
        """
        logger.info(f"{'='*60}")
        logger.info("🔄 RECONCILING STATE WITH CHAIN SYNC...")
        logger.info(f"{'='*60}")
        logger.info(f"  Wallet: {self.wallet_address}")
        
        # Initialize chain sync service
        if self._chain_sync is None:
            self._chain_sync = ChainSyncService(
                config=self.config,
                storage=self.storage,
                api=self.api
            )
        
        # Check if we need full sync or incremental
        with self.storage.transaction() as txn:
            sync_state = txn.get_chain_sync_state(self.wallet_address)
        
        if sync_state is None:
            logger.info("  📡 First sync - performing full historical sync...")
            result = await self._chain_sync.full_sync(
                self.wallet_address,
                match_existing_executions=True
            )
        else:
            logger.info(f"  📡 Incremental sync from block {sync_state['last_synced_block']:,}...")
            result = await self._chain_sync.incremental_sync(self.wallet_address)
        
        if result.success:
            logger.info(f"  ✅ Synced {result.transactions_synced} transactions")
        else:
            logger.warning(f"  ⚠️  Sync had errors: {result.errors}")
        
        # Fetch USDC balance from chain
        actual_balance = await self.api.fetch_usdc_balance(self.wallet_address)
        
        # Get computed positions from transactions
        with self.storage.transaction() as txn:
            txn.update_usdc_balance(self.wallet_address, actual_balance)
            computed_positions = txn.get_computed_positions(self.wallet_address)
            tx_summary = txn.get_transaction_summary(self.wallet_address)
        
        # Update current prices cache
        actual_positions = await self.api.fetch_positions(self.wallet_address)
        self._current_prices = {
            p.token_id: p.current_price or p.entry_price or 0
            for p in actual_positions
        }
        
        # Calculate total positions value
        total_positions_value = sum(
            p["shares"] * self._current_prices.get(p["token_id"], p["avg_entry_price"])
            for p in computed_positions
        )
        
        # Log summary
        logger.info(f"{'='*60}")
        logger.info(f"✅ CHAIN SYNC RECONCILIATION COMPLETE")
        logger.info(f"  Transactions: {tx_summary.get('buy', {}).get('count', 0)} buys, "
                   f"{tx_summary.get('sell', {}).get('count', 0)} sells, "
                   f"{tx_summary.get('claim', {}).get('count', 0)} claims")
        logger.info(f"  Computed Positions: {len(computed_positions)}")
        logger.info(f"  USDC Balance: ${actual_balance:.2f}")
        logger.info(f"  Positions Value: ${total_positions_value:.2f}")
        logger.info(f"  Total Equity: ${actual_balance + total_positions_value:.2f}")
        logger.info(f"{'='*60}")
        
        # Verify integrity and fix any discrepancies
        is_valid, discrepancies = await self._chain_sync.verify_sync_integrity(self.wallet_address)
        if not is_valid:
            logger.warning(f"  ⚠️  Sync integrity issues: {len(discrepancies)} discrepancies")
            # Fix the discrepancies
            fixes = await self._chain_sync.fix_discrepancies(self.wallet_address)
            if fixes > 0:
                logger.info(f"  ✅ Applied {fixes} fixes to match on-chain state")
        
        # Still cleanup stale reservations and agents
        with self.storage.transaction() as txn:
            released_count = txn.release_all_reservations()
            if released_count > 0:
                logger.info(f"  🔓 Released {released_count} stale reservations")
            
            crashed_count = txn.cleanup_stale_agents(
                self.config.risk.stale_agent_threshold_seconds
            )
            if crashed_count > 0:
                logger.warning(f"  ⚠️  Marked {crashed_count} stale agents as crashed")
    
    async def _heartbeat_loop(self, agent_id: str):
        """Background task to update heartbeat"""
        while True:
            try:
                self.storage.update_heartbeat(agent_id)
                await asyncio.sleep(self.config.risk.heartbeat_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                await asyncio.sleep(5)
    
    def atomic_reserve(
        self,
        agent_id: str,
        market_id: str,
        token_id: str,
        amount_usd: float,
        ttl_seconds: Optional[int] = None
    ) -> Optional[str]:
        """
        Atomically check limits AND reserve capital.
        
        This is a SINGLE TRANSACTION - no race conditions possible.
        
        Returns reservation_id if successful, None if denied.
        """
        if not self._reconciled:
            raise RuntimeError("Must call startup() before trading!")
        
        if amount_usd < self.config.risk.min_trade_value_usd:
            logger.warning(f"Trade value ${amount_usd:.2f} below minimum")
            return None
        
        if amount_usd > self.config.risk.max_trade_value_usd:
            logger.warning(f"Trade value ${amount_usd:.2f} above maximum")
            return None
        
        ttl = ttl_seconds or self.config.risk.reservation_ttl_seconds
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        
        with self.storage.transaction() as txn:
            # Get current wallet state
            wallet_state = txn.get_wallet_state(self.wallet_address)
            total_equity = wallet_state.usdc_balance + wallet_state.total_positions_value
            
            if total_equity <= 0:
                logger.warning("No equity available")
                return None
            
            # Check global wallet limit
            max_wallet_exposure = total_equity * self.config.risk.max_wallet_exposure_pct
            if wallet_state.total_exposure + amount_usd > max_wallet_exposure:
                logger.warning(
                    f"Wallet exposure limit: {wallet_state.total_exposure + amount_usd:.2f} > "
                    f"{max_wallet_exposure:.2f}"
                )
                return None
            
            # Check per-agent limit
            max_agent_exposure = total_equity * self.config.risk.max_per_agent_exposure_pct
            agent_exposure = txn.get_agent_exposure(agent_id)
            if agent_exposure + amount_usd > max_agent_exposure:
                logger.warning(
                    f"Agent exposure limit: {agent_exposure + amount_usd:.2f} > "
                    f"{max_agent_exposure:.2f}"
                )
                return None
            
            # Check per-market limit
            max_market_exposure = total_equity * self.config.risk.max_per_market_exposure_pct
            market_exposure = txn.get_market_exposure(market_id, self.wallet_address)
            if market_exposure + amount_usd > max_market_exposure:
                logger.warning(
                    f"Market exposure limit: {market_exposure + amount_usd:.2f} > "
                    f"{max_market_exposure:.2f}"
                )
                return None
            
            # Check available capital
            if amount_usd > wallet_state.available_capital:
                logger.warning(
                    f"Insufficient capital: need ${amount_usd:.2f}, "
                    f"available ${wallet_state.available_capital:.2f}"
                )
                return None
            
            # All checks passed - create reservation
            reservation_id = txn.create_reservation(
                agent_id=agent_id,
                market_id=market_id,
                token_id=token_id,
                amount_usd=amount_usd,
                expires_at=expires_at
            )
            
            logger.info(
                f"Reserved ${amount_usd:.2f} for {agent_id} "
                f"(reservation: {reservation_id[:8]}...)"
            )
            return reservation_id
    
    def confirm_execution(
        self,
        reservation_id: str,
        filled_shares: float,
        filled_price: float,
        requested_shares: Optional[float] = None
    ) -> bool:
        """
        Confirm trade executed, converting reservation to position.
        
        Handles partial fills correctly - releases unfilled portion back
        to available capital.
        
        Returns True if successful.
        """
        with self.storage.transaction() as txn:
            reservation = txn.get_reservation(reservation_id)
            
            if not reservation:
                logger.error(f"Reservation not found: {reservation_id}")
                return False
            
            if reservation.status != ReservationStatus.PENDING:
                logger.error(f"Reservation not pending: {reservation.status}")
                return False
            
            filled_amount = filled_shares * filled_price
            reserved_amount = reservation.amount_usd
            
            # Create position for filled portion
            txn.create_position(
                agent_id=reservation.agent_id,
                market_id=reservation.market_id,
                token_id=reservation.token_id,
                outcome="",  # Will be updated later
                shares=filled_shares,
                entry_price=filled_price
            )
            
            # Release unfilled portion back to available capital
            unfilled_amount = reserved_amount - filled_amount
            if unfilled_amount > 0.01:  # More than 1 cent unfilled
                logger.info(f"Partial fill: releasing ${unfilled_amount:.2f} back to pool")
            
            # Mark reservation as executed
            txn.mark_reservation_executed(reservation_id, filled_amount)
            
            logger.info(
                f"Confirmed execution: {filled_shares:.2f} shares @ ${filled_price:.4f} "
                f"(${filled_amount:.2f})"
            )
            return True
    
    def release_reservation(self, reservation_id: str) -> bool:
        """
        Release a reservation (trade failed or cancelled).
        
        Returns True if successful.
        """
        try:
            self.storage.release_reservation(reservation_id)
            logger.info(f"Released reservation {reservation_id[:8]}...")
            return True
        except Exception as e:
            logger.error(f"Error releasing reservation: {e}")
            return False
    
    def can_trade(
        self,
        agent_id: str,
        amount_usd: float
    ) -> Tuple[bool, str]:
        """
        Check if agent can make a trade of given size.
        
        Returns (can_trade, reason).
        
        Note: This is a READ-ONLY check. Use atomic_reserve() for actual
        reservation to avoid race conditions.
        """
        if not self._reconciled:
            return False, "Coordinator not initialized"
        
        if amount_usd < self.config.risk.min_trade_value_usd:
            return False, f"Below minimum (${self.config.risk.min_trade_value_usd})"
        
        if amount_usd > self.config.risk.max_trade_value_usd:
            return False, f"Above maximum (${self.config.risk.max_trade_value_usd})"
        
        with self.storage.transaction() as txn:
            wallet_state = txn.get_wallet_state(self.wallet_address)
            total_equity = wallet_state.usdc_balance + wallet_state.total_positions_value
            
            if total_equity <= 0:
                return False, "No equity"
            
            # Check wallet limit
            max_wallet = total_equity * self.config.risk.max_wallet_exposure_pct
            if wallet_state.total_exposure + amount_usd > max_wallet:
                return False, f"Wallet limit (${max_wallet:.2f})"
            
            # Check agent limit
            max_agent = total_equity * self.config.risk.max_per_agent_exposure_pct
            agent_exposure = txn.get_agent_exposure(agent_id)
            if agent_exposure + amount_usd > max_agent:
                return False, f"Agent limit (${max_agent:.2f})"
            
            # Check available capital
            if amount_usd > wallet_state.available_capital:
                return False, f"Insufficient capital (${wallet_state.available_capital:.2f})"
        
        return True, "OK"
    
    def get_available_capital(self, agent_id: str) -> float:
        """
        Get capital available for an agent.
        
        Takes into account:
        - USDC balance
        - Active reservations
        - Agent-specific limits
        """
        with self.storage.transaction() as txn:
            wallet_state = txn.get_wallet_state(self.wallet_address)
            total_equity = wallet_state.usdc_balance + wallet_state.total_positions_value
            
            if total_equity <= 0:
                return 0.0
            
            # Agent's remaining allocation
            max_agent = total_equity * self.config.risk.max_per_agent_exposure_pct
            agent_exposure = txn.get_agent_exposure(agent_id)
            agent_remaining = max(0, max_agent - agent_exposure)
            
            # Wallet's remaining capital
            wallet_remaining = wallet_state.available_capital
            
            # Return the minimum
            return min(agent_remaining, wallet_remaining)
    
    def get_wallet_state(self) -> WalletState:
        """Get current wallet state"""
        return self.storage.get_wallet_state(self.wallet_address)
    
    async def refresh_balance(self) -> float:
        """Refresh USDC balance from chain"""
        if not self.api:
            return 0.0
        
        balance = await self.api.fetch_usdc_balance(self.wallet_address)
        
        with self.storage.transaction() as txn:
            txn.update_usdc_balance(self.wallet_address, balance)
        
        return balance
    
    async def fetch_actual_position(self, token_id: str) -> Optional[float]:
        """
        Fetch actual share balance for a token from the API.
        
        Returns actual shares held, or None if position doesn't exist or API unavailable.
        This queries the real on-chain balance, not our SQL tracking.
        """
        if not self.api:
            return None
        
        try:
            actual_positions = await self.api.fetch_positions(self.wallet_address)
            for pos in actual_positions:
                if pos.token_id == token_id:
                    return pos.shares
            # Position not found on-chain
            return 0.0
        except Exception as e:
            logger.warning(f"Failed to fetch actual position for {token_id[:20]}...: {e}")
            return None
    
    def mark_position_closed_by_token(self, token_id: str) -> int:
        """
        Mark position as closed by token_id.
        
        Returns number of positions closed.
        """
        with self.storage.transaction() as txn:
            return txn.mark_position_closed_by_token(self.wallet_address, token_id)
    
    def cleanup_stale(self) -> Tuple[int, int]:
        """
        Cleanup stale reservations and agents.
        
        Returns (reservations_cleaned, agents_cleaned).
        """
        with self.storage.transaction() as txn:
            reservations = txn.cleanup_expired_reservations()
            agents = txn.cleanup_stale_agents(
                self.config.risk.stale_agent_threshold_seconds
            )
        
        if reservations > 0:
            logger.info(f"Cleaned up {reservations} expired reservations")
        if agents > 0:
            logger.warning(f"Marked {agents} stale agents as crashed")
        
        return reservations, agents
    
    async def reconcile_positions(self) -> Tuple[int, int]:
        """
        Lightweight reconciliation of SQL positions with on-chain state.
        
        Performs incremental chain sync to update the transactions table.
        
        Returns (transactions_synced, actual_position_count).
        """
        if not self.api:
            return 0, 0
        
        try:
            if self._chain_sync is None:
                self._chain_sync = ChainSyncService(
                    config=self.config,
                    storage=self.storage,
                    api=self.api
                )
            
            result = await self._chain_sync.incremental_sync(self.wallet_address)
            
            # Update current prices cache
            actual_positions = await self.api.fetch_positions(self.wallet_address)
            self._current_prices = {
                p.token_id: p.current_price or p.entry_price or 0
                for p in actual_positions
            }
            
            # Also refresh USDC balance
            actual_balance = await self.api.fetch_usdc_balance(self.wallet_address)
            with self.storage.transaction() as txn:
                txn.update_usdc_balance(self.wallet_address, actual_balance)
            
            return result.transactions_synced, len(actual_positions)
            
        except Exception as e:
            logger.warning(f"Chain sync reconciliation failed: {e}")
            return 0, 0
    
    def get_computed_positions(self) -> List[dict]:
        """
        Get current positions computed from transaction history.
        
        This is the source-of-truth method when chain sync is enabled.
        """
        with self.storage.transaction() as txn:
            return txn.get_computed_positions(self.wallet_address)
    
    def get_computed_exposure(self, agent_id: Optional[str] = None) -> float:
        """
        Get exposure computed from transaction history.
        
        Args:
            agent_id: Optional agent ID to filter by. If None, returns total wallet exposure.
        
        Returns total position value based on transactions.
        """
        with self.storage.transaction() as txn:
            if agent_id:
                return txn.get_agent_computed_exposure(agent_id, self._current_prices)
            else:
                return txn.get_total_computed_exposure(self.wallet_address, self._current_prices)
    
    def get_transaction_history(
        self,
        transaction_type: Optional[str] = None,
        token_id: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[dict]:
        """
        Get transaction history from the transactions table.
        
        Args:
            transaction_type: Optional filter ('buy', 'sell', 'claim', 'deposit', 'withdrawal')
            token_id: Optional filter by token
            limit: Optional limit on results
        
        Returns list of transactions.
        """
        with self.storage.transaction() as txn:
            return txn.get_transactions(
                wallet_address=self.wallet_address,
                transaction_type=transaction_type,
                token_id=token_id,
                limit=limit
            )
    
    async def sync_transactions(self, full_sync: bool = False) -> int:
        """
        Manually trigger transaction sync.
        
        Args:
            full_sync: If True, performs full historical sync. Otherwise incremental.
        
        Returns number of transactions synced.
        """
        if self._chain_sync is None:
            self._chain_sync = ChainSyncService(
                config=self.config,
                storage=self.storage,
                api=self.api
            )
        
        if full_sync:
            result = await self._chain_sync.full_sync(self.wallet_address)
        else:
            result = await self._chain_sync.incremental_sync(self.wallet_address)
        
        return result.transactions_synced


