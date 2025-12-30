"""
Main TradingBot class with composition-based architecture.

The TradingBot is configured with pluggable components:
- SignalSource: Where trading signals come from
- PositionSizer: How to size positions
- ExecutionEngine: How to execute trades
- RiskCoordinator: Multi-agent risk management
"""

import asyncio
import logging
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, timezone

from ..core.models import Signal, Side, ExecutionResult
from ..core.config import Config, get_config
from ..core.api import PolymarketAPI
from .risk_coordinator import RiskCoordinator
from .safety import CircuitBreaker, DrawdownLimit, TradingHalt
from .components.signals import SignalSource
from .components.sizers import PositionSizer
from .components.executors import ExecutionEngine, DryRunExecutor

if TYPE_CHECKING:
    from py_clob_client.client import ClobClient

logger = logging.getLogger(__name__)


class TradingBot:
    """
    Composition-based trading bot.
    
    Instead of inheritance, uses pluggable components for flexibility.
    
    Usage:
        bot = TradingBot(
            agent_id="my-bot",
            signal_source=FlowAlertSignals(),
            position_sizer=SignalScaledSizer(),
            executor=AggressiveExecutor(),
        )
        await bot.start()
        await bot.run()
    """
    
    def __init__(
        self,
        agent_id: str,
        agent_type: str = "generic",
        signal_source: Optional[SignalSource] = None,
        position_sizer: Optional[PositionSizer] = None,
        executor: Optional[ExecutionEngine] = None,
        config: Optional[Config] = None,
        dry_run: bool = False,
    ):
        """
        Initialize trading bot with components.
        
        Args:
            agent_id: Unique identifier for this agent
            agent_type: Type of agent (e.g., "bond", "flow")
            signal_source: Source of trading signals
            position_sizer: Position sizing strategy
            executor: Order execution engine
            config: Configuration (uses default if not provided)
            dry_run: If True, use dry run executor
        """
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.config = config or get_config()
        self.dry_run = dry_run
        
        # Components
        self.signal_source = signal_source
        self.position_sizer = position_sizer
        self.executor = executor or (DryRunExecutor() if dry_run else None)
        
        # Infrastructure
        self.api: Optional[PolymarketAPI] = None
        self.client: Optional["ClobClient"] = None
        self.risk_coordinator: Optional[RiskCoordinator] = None
        
        # Safety components
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=self.config.risk.circuit_breaker_failures,
            reset_timeout_seconds=self.config.risk.circuit_breaker_reset_seconds
        )
        self.drawdown_limit = DrawdownLimit(
            max_daily_drawdown_pct=self.config.risk.max_daily_drawdown_pct,
            max_total_drawdown_pct=self.config.risk.max_total_drawdown_pct
        )
        self.trading_halt = TradingHalt()
        
        # State
        self.running = False
        self._main_task: Optional[asyncio.Task] = None
        
    async def start(self):
        """
        Start the trading bot.
        
        Initializes all components and starts the risk coordinator.
        """
        logger.info(f"{'='*60}")
        logger.info(f"🚀 STARTING TRADING BOT")
        logger.info(f"{'='*60}")
        logger.info(f"  Agent ID:   {self.agent_id}")
        logger.info(f"  Type:       {self.agent_type}")
        logger.info(f"  Mode:       {'🧪 DRY RUN' if self.dry_run else '💸 LIVE'}")
        
        if self.dry_run:
            logger.warning("⚠️  DRY RUN MODE - No real orders will be placed")
        
        # Validate configuration
        self.config.require_credentials()
        logger.info("  ✅ Credentials validated")
        
        # Initialize API
        self.api = PolymarketAPI(self.config)
        await self.api.connect()
        logger.info("  ✅ API connected")
        
        # Initialize CLOB client
        if not self.dry_run:
            from py_clob_client.client import ClobClient
            
            self.client = ClobClient(
                self.config.clob_host,
                key=self.config.private_key,
                chain_id=self.config.chain_id,
                signature_type=2,
                funder=self.config.proxy_address
            )
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            logger.info("  ✅ CLOB client initialized")
        
        # Initialize risk coordinator
        self.risk_coordinator = RiskCoordinator(
            config=self.config,
            api=self.api
        )
        
        if not await self.risk_coordinator.startup(self.agent_id, self.agent_type):
            raise RuntimeError("Failed to start risk coordinator")
        logger.info("  ✅ Risk coordinator started")
        
        # Initialize drawdown tracking
        wallet_state = self.risk_coordinator.get_wallet_state()
        total_equity = wallet_state.usdc_balance + wallet_state.total_positions_value
        self.drawdown_limit.reset(total_equity)
        
        self.running = True
        
        logger.info(f"{'='*60}")
        logger.info(f"💰 WALLET STATE")
        logger.info(f"{'='*60}")
        logger.info(f"  USDC Balance:    ${wallet_state.usdc_balance:,.2f}")
        logger.info(f"  Positions Value: ${wallet_state.total_positions_value:,.2f}")
        logger.info(f"  Total Equity:    ${total_equity:,.2f}")
        logger.info(f"  Available:       ${wallet_state.available_capital:,.2f}")
        logger.info(f"{'='*60}")
        logger.info(f"✅ Bot ready and running!")
    
    async def stop(self):
        """Stop the trading bot gracefully"""
        logger.info(f"{'='*60}")
        logger.info(f"🛑 STOPPING BOT: {self.agent_id}")
        logger.info(f"{'='*60}")
        
        self.running = False
        
        # Cancel main task
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
        
        # Shutdown risk coordinator
        if self.risk_coordinator:
            await self.risk_coordinator.shutdown()
            logger.info("  ✅ Risk coordinator stopped")
        
        # Close API
        if self.api:
            await self.api.close()
            logger.info("  ✅ API disconnected")
        
        logger.info(f"{'='*60}")
        logger.info(f"👋 Bot stopped cleanly")
        logger.info(f"{'='*60}")
    
    async def run(self, interval_seconds: float = 5.0):
        """
        Main trading loop.
        
        Continuously:
        1. Check safety conditions
        2. Get signals from signal source
        3. Process signals and execute trades
        4. Sleep for interval
        """
        if not self.running:
            raise RuntimeError("Bot not started. Call start() first.")
        
        if not self.signal_source:
            raise RuntimeError("No signal source configured")
        
        if not self.position_sizer:
            raise RuntimeError("No position sizer configured")
        
        if not self.executor:
            raise RuntimeError("No executor configured")
        
        logger.info(f"Starting main loop (interval={interval_seconds}s)")
        
        self._main_task = asyncio.current_task()
        
        try:
            while self.running:
                try:
                    await self._trading_iteration()
                except Exception as e:
                    logger.error(f"Error in trading iteration: {e}")
                    self.circuit_breaker.record_failure()
                
                await asyncio.sleep(interval_seconds)
                
        except asyncio.CancelledError:
            logger.info("Trading loop cancelled")
    
    async def _trading_iteration(self):
        """Single iteration of the trading loop"""
        # 1. Check safety conditions
        if not self._check_safety():
            return
        
        # 2. Update equity for drawdown tracking
        await self._update_equity()
        
        # 3. Get signals
        signals = await self.signal_source.get_signals()
        
        if not signals:
            logger.debug("No signals")
            return
        
        logger.info(f"Got {len(signals)} signals")
        
        # 4. Process signals
        for signal in signals:
            if not self.running:
                break
            
            if not self._check_safety():
                break
            
            await self._process_signal(signal)
    
    def _check_safety(self) -> bool:
        """Check all safety conditions"""
        # Trading halt
        if self.trading_halt.is_halted:
            logger.warning(f"Trading halted: {self.trading_halt.reason_summary}")
            return False
        
        # Circuit breaker
        if not self.circuit_breaker.can_execute():
            remaining = self.circuit_breaker.seconds_until_reset
            logger.warning(f"Circuit breaker OPEN ({remaining:.0f}s until reset)")
            return False
        
        # Drawdown limit
        if self.drawdown_limit.is_breached:
            logger.warning(f"Drawdown limit breached: {self.drawdown_limit.breach_reason}")
            return False
        
        return True
    
    async def _update_equity(self):
        """Update equity and check drawdown limits"""
        wallet_state = self.risk_coordinator.get_wallet_state()
        total_equity = wallet_state.usdc_balance + wallet_state.total_positions_value
        
        if not self.drawdown_limit.update(total_equity):
            logger.error("Drawdown limit breached - halting trading")
            self.trading_halt.add_reason("DRAWDOWN", self.drawdown_limit.breach_reason or "")
    
    async def _process_signal(self, signal: Signal):
        """Process a single signal"""
        question = signal.metadata.get('question', signal.token_id[:30])[:40]
        
        try:
            # Check if signal is actionable
            if signal.direction.value == "NEUTRAL":
                logger.debug(f"⏭️  Skip: Neutral signal for {question}...")
                return
            
            # Get available capital
            available = self.risk_coordinator.get_available_capital(self.agent_id)
            if available < self.config.risk.min_trade_value_usd:
                logger.warning(
                    f"⚠️  Skip: Insufficient capital (${available:.2f} < "
                    f"${self.config.risk.min_trade_value_usd:.2f} min)"
                )
                return
            
            # Get current price
            bid, ask, spread = await self.api.get_spread(signal.token_id)
            
            if spread and spread > self.config.risk.max_spread_pct:
                logger.info(f"⏭️  Skip: Spread too wide ({spread:.1%} > {self.config.risk.max_spread_pct:.1%})")
                return
            
            if signal.is_buy:
                current_price = ask or signal.metadata.get("price", 0)
                side = Side.BUY
            else:
                current_price = bid or signal.metadata.get("price", 0)
                side = Side.SELL
            
            if not current_price or current_price <= 0:
                logger.debug(f"⏭️  Skip: No valid price for {question}...")
                return
            
            # Calculate position size
            size_usd = self.position_sizer.calculate_size(
                signal, available, current_price
            )
            
            if size_usd < self.config.risk.min_trade_value_usd:
                logger.debug(f"⏭️  Skip: Position too small (${size_usd:.2f})")
                return
            
            # Reserve capital
            reservation_id = self.risk_coordinator.atomic_reserve(
                agent_id=self.agent_id,
                market_id=signal.market_id,
                token_id=signal.token_id,
                amount_usd=size_usd
            )
            
            if not reservation_id:
                logger.warning(f"⚠️  Skip: Could not reserve ${size_usd:.2f} capital")
                return
            
            try:
                # Log trade attempt
                side_emoji = "📈" if side == Side.BUY else "📉"
                logger.info(f"{'='*60}")
                logger.info(f"{side_emoji} EXECUTING {side.value} ORDER")
                logger.info(f"{'='*60}")
                logger.info(f"  📌 {question}...")
                logger.info(f"  💵 Size:   ${size_usd:.2f}")
                logger.info(f"  💰 Price:  ${current_price:.4f}")
                logger.info(f"  📊 Score:  {signal.score:.1f}")
                logger.info(f"  📈 Spread: {spread:.2%}" if spread else "  📈 Spread: N/A")
                logger.info(f"  💳 Available: ${available:.2f}")
                
                result = await self.executor.execute(
                    client=self.client,
                    token_id=signal.token_id,
                    side=side,
                    size_usd=size_usd,
                    price=current_price,
                    orderbook=None
                )
                
                if result.success and result.filled_shares > 0:
                    # Confirm execution
                    self.risk_coordinator.confirm_execution(
                        reservation_id=reservation_id,
                        filled_shares=result.filled_shares,
                        filled_price=result.filled_price,
                        requested_shares=result.requested_shares
                    )
                    self.circuit_breaker.record_success()
                    
                    # Save execution history
                    try:
                        with self.risk_coordinator.storage.transaction() as txn:
                            txn.save_execution(
                                agent_id=self.agent_id,
                                market_id=signal.market_id,
                                token_id=signal.token_id,
                                side=side,
                                shares=result.filled_shares,
                                price=current_price,
                                filled_price=result.filled_price,
                                signal_score=signal.score,
                                success=True
                            )
                    except Exception as e:
                        logger.warning(f"Failed to save execution history: {e}")
                    
                    total_cost = result.filled_shares * result.filled_price
                    slippage = abs(result.filled_price - current_price) / current_price if current_price > 0 else 0
                    
                    logger.info(f"{'='*60}")
                    logger.info(f"✅ ORDER FILLED")
                    logger.info(f"{'='*60}")
                    logger.info(f"  📦 Shares:   {result.filled_shares:.4f}")
                    logger.info(f"  💰 Price:    ${result.filled_price:.4f}")
                    logger.info(f"  💵 Total:    ${total_cost:.2f}")
                    logger.info(f"  📉 Slippage: {slippage:.2%}")
                    logger.info(f"{'='*60}")
                else:
                    # Release reservation
                    self.risk_coordinator.release_reservation(reservation_id)
                    
                    # Save failed execution
                    try:
                        with self.risk_coordinator.storage.transaction() as txn:
                            txn.save_execution(
                                agent_id=self.agent_id,
                                market_id=signal.market_id,
                                token_id=signal.token_id,
                                side=side,
                                shares=result.requested_shares,
                                price=current_price,
                                filled_price=0.0,
                                signal_score=signal.score,
                                success=False,
                                error_message=result.error_message
                            )
                    except Exception as e:
                        logger.warning(f"Failed to save execution history: {e}")
                    
                    if result.error_message:
                        logger.warning(f"❌ Execution failed: {result.error_message}")
                        self.circuit_breaker.record_failure()
                    else:
                        logger.info("⏳ Order placed but not filled (may fill later)")
                        
            except Exception as e:
                # Release reservation on error
                self.risk_coordinator.release_reservation(reservation_id)
                raise
                
        except Exception as e:
            logger.error(f"❌ Error processing signal for {question}: {e}")
            self.circuit_breaker.record_failure()
    
    def get_status(self) -> dict:
        """Get current bot status"""
        wallet_state = None
        if self.risk_coordinator:
            wallet_state = self.risk_coordinator.get_wallet_state()
        
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "running": self.running,
            "dry_run": self.dry_run,
            "signal_source": self.signal_source.name if self.signal_source else None,
            "position_sizer": self.position_sizer.name if self.position_sizer else None,
            "executor": self.executor.name if self.executor else None,
            "circuit_breaker": {
                "state": self.circuit_breaker.state,
                "failure_count": self.circuit_breaker.failure_count,
            },
            "drawdown": self.drawdown_limit.get_status(),
            "trading_halt": {
                "is_halted": self.trading_halt.is_halted,
                "reasons": self.trading_halt.reasons,
            },
            "wallet": {
                "balance": wallet_state.usdc_balance if wallet_state else 0,
                "positions_value": wallet_state.total_positions_value if wallet_state else 0,
                "reserved": wallet_state.total_reserved if wallet_state else 0,
                "available": wallet_state.available_capital if wallet_state else 0,
            } if wallet_state else None,
        }


