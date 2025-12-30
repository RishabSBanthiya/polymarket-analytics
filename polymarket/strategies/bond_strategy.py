"""
Bond Strategy - Expiring Market Trading Bot.

This strategy trades on markets near expiration where prices
are in the 95c-98c range, betting they'll resolve to $1.

Named "bond" because these trades behave like short-term bonds -
high probability of small gain, low probability of total loss.
"""

import asyncio
import logging
from typing import Optional, List
from datetime import datetime, timezone

from ..core.config import Config, get_config
from ..core.api import PolymarketAPI
from ..core.models import Market, Signal
from ..trading.bot import TradingBot
from ..trading.components.signals import ExpiringMarketSignals
from ..trading.components.sizers import KellyPositionSizer
from ..trading.components.executors import AggressiveExecutor, DryRunExecutor

logger = logging.getLogger(__name__)


def format_time_remaining(seconds: float) -> str:
    """Format seconds into human-readable time"""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}m"
    else:
        return f"{seconds / 3600:.1f}h"


# Time bucket configuration for diversification
# Format: (max_seconds, weight, max_positions)
TIME_BUCKETS = [
    (120, 4.0, 8),      # <2 min: 4x weight, up to 8 positions
    (300, 3.0, 6),      # 2-5 min: 3x weight, up to 6 positions
    (900, 2.0, 4),      # 5-15 min: 2x weight, up to 4 positions
    (1800, 1.5, 3),     # 15-30 min: 1.5x weight, up to 3 positions
    (float('inf'), 1.0, 2),  # 30+ min: 1x weight, up to 2 positions
]


class BondSignalSource(ExpiringMarketSignals):
    """
    Extended expiring market signal source with market fetching.
    """
    
    def __init__(
        self,
        api: PolymarketAPI,
        min_price: float = 0.95,
        max_price: float = 0.98,
        min_seconds_left: int = 60,
        max_seconds_left: int = 1800,
        refresh_interval: int = 30,
    ):
        super().__init__(min_price, max_price, min_seconds_left, max_seconds_left)
        self.api = api
        self.refresh_interval = refresh_interval
        self._last_refresh: Optional[datetime] = None
        self._scan_count = 0
        self._last_opportunity_count = 0
    
    async def get_signals(self) -> List[Signal]:
        """Get signals, refreshing markets if needed"""
        now = datetime.now(timezone.utc)
        
        # Refresh markets periodically
        if (self._last_refresh is None or 
            (now - self._last_refresh).total_seconds() > self.refresh_interval):
            await self._refresh_markets()
            self._last_refresh = now
        
        signals = await super().get_signals()
        
        self._scan_count += 1
        
        # Log scan summary periodically or when opportunities change
        if signals or self._scan_count % 12 == 0:  # Every ~minute at 5s interval
            self._log_scan_summary(signals)
        
        return signals
    
    def _log_scan_summary(self, signals: List[Signal]):
        """Log detailed scan summary"""
        expiring_count = len([m for m in self._markets if self._is_expiring_soon(m)])
        
        if not signals:
            if expiring_count > 0:
                logger.info(
                    f"📊 Scan #{self._scan_count}: {expiring_count} expiring markets, "
                    f"0 in price range ${self.min_price:.2f}-${self.max_price:.2f}"
                )
            else:
                logger.debug(f"📊 Scan #{self._scan_count}: No expiring markets found")
            return
        
        logger.info(f"{'='*60}")
        logger.info(f"🎯 BOND OPPORTUNITIES FOUND: {len(signals)}")
        logger.info(f"{'='*60}")
        
        for i, signal in enumerate(signals, 1):
            time_left = signal.metadata.get('seconds_left', 0)
            price = signal.metadata.get('price', 0)
            expected_return = ((1.0 / price) - 1.0) * 100 if price > 0 else 0
            question = signal.metadata.get('question', 'Unknown')[:50]
            
            # Determine time bucket
            bucket = "30m+"
            for max_sec, weight, _ in TIME_BUCKETS:
                if time_left <= max_sec:
                    if max_sec <= 120:
                        bucket = "<2m ⚡"
                    elif max_sec <= 300:
                        bucket = "2-5m 🔥"
                    elif max_sec <= 900:
                        bucket = "5-15m"
                    elif max_sec <= 1800:
                        bucket = "15-30m"
                    break
            
            logger.info(
                f"  [{i}] {question}..."
            )
            logger.info(
                f"      💰 Price: ${price:.4f} | "
                f"⏱️  Time: {format_time_remaining(time_left)} ({bucket}) | "
                f"📈 Expected: +{expected_return:.1f}%"
            )
        
        logger.info(f"{'='*60}")
        self._last_opportunity_count = len(signals)
    
    def _is_expiring_soon(self, market: Market) -> bool:
        """Check if market is expiring within our window"""
        if not market.end_date:
            return False
        now = datetime.now(timezone.utc)
        time_left = (market.end_date - now).total_seconds()
        return self.min_seconds_left <= time_left <= self.max_seconds_left
    
    async def _refresh_markets(self):
        """Fetch and parse active markets"""
        logger.info("🔄 Refreshing markets from Polymarket API...")
        
        raw_markets = await self.api.fetch_all_markets()
        
        markets = []
        expired_count = 0
        closed_count = 0
        
        for raw in raw_markets:
            market = self.api.parse_market(raw)
            if market:
                if market.is_expired:
                    expired_count += 1
                elif market.closed:
                    closed_count += 1
                else:
                    markets.append(market)
        
        self.update_markets(markets)
        
        # Count expiring markets in our window
        expiring = [m for m in markets if self._is_expiring_soon(m)]
        
        logger.info(
            f"📥 Loaded {len(markets)} active markets "
            f"(skipped: {expired_count} expired, {closed_count} closed)"
        )
        if expiring:
            logger.info(
                f"⏰ {len(expiring)} markets expiring in {self.min_seconds_left}-{self.max_seconds_left}s window"
            )


def create_bond_bot(
    agent_id: str = "bond-bot",
    config: Optional[Config] = None,
    dry_run: bool = False,
    min_price: float = 0.95,
    max_price: float = 0.98,
) -> TradingBot:
    """
    Create a bond strategy trading bot.
    
    Args:
        agent_id: Unique identifier for this agent
        config: Configuration (uses default if not provided)
        dry_run: If True, simulate trades without execution
        min_price: Minimum price to consider (default 0.95)
        max_price: Maximum price to consider (default 0.98)
    
    Returns:
        Configured TradingBot ready to start
    """
    config = config or get_config()
    
    # Log strategy configuration
    logger.info(f"{'='*60}")
    logger.info(f"🏦 BOND STRATEGY CONFIGURATION")
    logger.info(f"{'='*60}")
    logger.info(f"  Agent ID:      {agent_id}")
    logger.info(f"  Mode:          {'🧪 DRY RUN' if dry_run else '💸 LIVE TRADING'}")
    logger.info(f"  Price Range:   ${min_price:.2f} - ${max_price:.2f}")
    logger.info(f"  Time Window:   60s - 1800s (30 min)")
    logger.info(f"  Position Size: Half-Kelly (max 25%)")
    expected_return_min = ((1.0 / max_price) - 1.0) * 100
    expected_return_max = ((1.0 / min_price) - 1.0) * 100
    logger.info(f"  Expected Returns: +{expected_return_min:.1f}% to +{expected_return_max:.1f}%")
    logger.info(f"{'='*60}")
    
    # Create API for signal source
    api = PolymarketAPI(config)
    
    # Create components
    signal_source = BondSignalSource(
        api=api,
        min_price=min_price,
        max_price=max_price,
        min_seconds_left=60,
        max_seconds_left=1800,
    )
    
    position_sizer = KellyPositionSizer(
        kelly_fraction=0.5,  # Half Kelly for safety
        min_edge=0.02,
        max_kelly=0.25,
        price_range=(min_price, max_price)
    )
    
    executor = DryRunExecutor() if dry_run else AggressiveExecutor(max_slippage=0.02)
    
    # Create bot
    bot = TradingBot(
        agent_id=agent_id,
        agent_type="bond",
        signal_source=signal_source,
        position_sizer=position_sizer,
        executor=executor,
        config=config,
        dry_run=dry_run,
    )
    
    return bot


async def run_bond_bot(
    agent_id: str = "bond-bot",
    dry_run: bool = False,
    interval: float = 5.0,
):
    """
    Run the bond strategy bot.
    
    This is a convenience function for running the bot directly.
    """
    bot = create_bond_bot(agent_id=agent_id, dry_run=dry_run)
    
    try:
        await bot.start()
        await bot.run(interval_seconds=interval)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        await bot.stop()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Bond Strategy Trading Bot")
    parser.add_argument("--agent-id", default="bond-bot", help="Agent ID")
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    parser.add_argument("--interval", type=float, default=5.0, help="Scan interval")
    
    args = parser.parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    asyncio.run(run_bond_bot(
        agent_id=args.agent_id,
        dry_run=args.dry_run,
        interval=args.interval
    ))


