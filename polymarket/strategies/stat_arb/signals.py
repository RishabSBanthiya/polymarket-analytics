"""
Statistical Arbitrage Signal Source.

Implements the SignalSource interface for integration with TradingBot.
Aggregates opportunities from all scanners and converts to signals.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Set

from polymarket.core.api import PolymarketAPI
from polymarket.core.models import Market, Signal, SignalDirection
from polymarket.trading.components.signals import SignalSource

from .models import ArbType, StatArbOpportunity
from .config import StatArbConfig
from .correlation_engine import CorrelationEngine
from .scanners import (
    MultiOutcomeScanner,
    DuplicateMarketScanner,
    PairSpreadScanner,
    ConditionalProbScanner,
)

logger = logging.getLogger(__name__)


class StatArbSignals(SignalSource):
    """
    Signal source for statistical arbitrage opportunities.

    Combines multiple scanners:
    - Multi-outcome sum arbitrage
    - Duplicate market arbitrage
    - Pair spread (mean reversion)
    - Conditional probability arbitrage

    Converts opportunities to standard Signal objects for TradingBot.
    """

    def __init__(
        self,
        api: PolymarketAPI,
        config: StatArbConfig,
        correlation_engine: Optional[CorrelationEngine] = None,
    ):
        self.api = api
        self.config = config

        # Initialize correlation engine if not provided
        self.correlation_engine = correlation_engine or CorrelationEngine(
            api, config.correlation
        )

        # Initialize scanners
        self._scanners: Dict[ArbType, object] = {}
        self._init_scanners()

        # Deduplication
        self._seen_opportunities: Dict[str, datetime] = {}
        self._dedup_window = timedelta(minutes=5)

        # State
        self._markets: List[Market] = []
        self._last_market_refresh: Optional[datetime] = None
        self._initialized = False

    def _init_scanners(self) -> None:
        """Initialize enabled scanners."""
        enabled = self.config.get_enabled_types()

        if ArbType.MULTI_OUTCOME_SUM in enabled:
            self._scanners[ArbType.MULTI_OUTCOME_SUM] = MultiOutcomeScanner(
                self.api, self.config.multi_outcome
            )

        if ArbType.DUPLICATE_MARKET in enabled:
            self._scanners[ArbType.DUPLICATE_MARKET] = DuplicateMarketScanner(
                self.api, self.config.duplicate, self.correlation_engine
            )

        if ArbType.PAIR_SPREAD in enabled:
            self._scanners[ArbType.PAIR_SPREAD] = PairSpreadScanner(
                self.api, self.config.pair_trading, self.correlation_engine
            )

        if ArbType.CONDITIONAL_PROB in enabled:
            self._scanners[ArbType.CONDITIONAL_PROB] = ConditionalProbScanner(
                self.api, self.config.conditional, self.correlation_engine
            )

        logger.info(f"Initialized {len(self._scanners)} scanners: {list(self._scanners.keys())}")

    @property
    def name(self) -> str:
        """Signal source name."""
        return "stat_arb"

    async def initialize(self) -> None:
        """Initialize signal source with market data."""
        if self._initialized:
            return

        logger.info("Initializing stat arb signal source...")

        # Fetch all markets
        await self._refresh_markets()

        # Initialize correlation engine
        await self.correlation_engine.initialize(self._markets)

        self._initialized = True
        logger.info("Stat arb signal source initialized")

    async def get_signals(self) -> List[Signal]:
        """
        Get current trading signals from all scanners.

        Returns:
            List of Signal objects for TradingBot consumption.
        """
        if not self._initialized:
            await self.initialize()

        # Refresh markets if stale
        await self._maybe_refresh_markets()

        # Update correlation engine if needed
        await self.correlation_engine.update_if_stale(self._markets)

        # Run all scanners in parallel
        opportunities = await self._scan_all()

        # Convert to signals
        signals = []
        for opp in opportunities:
            if self._is_duplicate(opp):
                continue

            signal = self._opportunity_to_signal(opp)
            signals.append(signal)
            self._seen_opportunities[opp.opportunity_id] = opp.detected_at

        # Clean old entries from dedup cache
        self._clean_dedup_cache()

        logger.debug(f"Generated {len(signals)} signals from {len(opportunities)} opportunities")
        return signals

    async def _scan_all(self) -> List[StatArbOpportunity]:
        """Run all enabled scanners and aggregate results."""
        all_opportunities: List[StatArbOpportunity] = []

        # Prepare scan tasks
        tasks = []
        scanner_types = []

        for arb_type, scanner in self._scanners.items():
            if arb_type == ArbType.MULTI_OUTCOME_SUM:
                tasks.append(scanner.scan(self._markets))
            elif arb_type == ArbType.DUPLICATE_MARKET:
                tasks.append(scanner.scan(self._markets))
            elif arb_type == ArbType.PAIR_SPREAD:
                tasks.append(scanner.scan())  # Uses internal pairs
            elif arb_type == ArbType.CONDITIONAL_PROB:
                tasks.append(scanner.scan(self._markets))

            scanner_types.append(arb_type)

        # Run all scans in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Aggregate results
        for arb_type, result in zip(scanner_types, results):
            if isinstance(result, Exception):
                logger.warning(f"{arb_type.value} scanner failed: {result}")
                continue

            if result:
                all_opportunities.extend(result)
                logger.debug(f"{arb_type.value}: found {len(result)} opportunities")

        # Sort by edge (highest first)
        all_opportunities.sort(key=lambda x: x.edge_bps, reverse=True)

        return all_opportunities

    async def _refresh_markets(self) -> None:
        """Refresh market list from API."""
        try:
            raw_markets = await self.api.fetch_all_markets_including_restricted()

            # Parse raw dicts into Market objects
            self._markets = []
            for raw in raw_markets:
                if isinstance(raw, dict):
                    parsed = self.api.parse_market(raw)
                    if parsed:
                        self._markets.append(parsed)
                else:
                    # Already a Market object
                    self._markets.append(raw)

            self._last_market_refresh = datetime.now(timezone.utc)
            logger.info(f"Refreshed {len(self._markets)} markets")
        except Exception as e:
            logger.error(f"Failed to refresh markets: {e}")

    async def _maybe_refresh_markets(self) -> None:
        """Refresh markets if they're stale."""
        if self._last_market_refresh is None:
            await self._refresh_markets()
            return

        age_seconds = (datetime.now(timezone.utc) - self._last_market_refresh).total_seconds()
        if age_seconds >= self.config.market_refresh_seconds:
            await self._refresh_markets()

    def _opportunity_to_signal(self, opp: StatArbOpportunity) -> Signal:
        """Convert a StatArbOpportunity to a Signal."""
        # Use first leg as primary for Signal
        primary_leg = opp.legs[0] if opp.legs else None

        return Signal(
            market_id=opp.market_ids[0],
            token_id=opp.token_ids[0] if opp.token_ids else "",
            direction=SignalDirection.BUY if primary_leg and primary_leg.side == "BUY" else SignalDirection.SELL,
            score=opp.confidence * 100,  # Convert to 0-100 scale
            source=f"stat_arb_{opp.arb_type.value}",
            metadata={
                "opportunity_id": opp.opportunity_id,
                "arb_type": opp.arb_type.value,
                "edge_bps": opp.edge_bps,
                "z_score": opp.z_score,
                "legs": [
                    {
                        "token_id": leg.token_id,
                        "market_id": leg.market_id,
                        "outcome": leg.outcome,
                        "side": leg.side,
                        "target_price": leg.target_price,
                        "target_shares": leg.target_shares,
                    }
                    for leg in opp.legs
                ],
                "all_market_ids": opp.market_ids,
                "all_token_ids": opp.token_ids,
                "questions": opp.questions,
                "total_cost": opp.total_cost,
                "expected_profit": opp.expected_profit,
                **opp.metadata,
            },
        )

    def _is_duplicate(self, opp: StatArbOpportunity) -> bool:
        """Check if opportunity was recently seen."""
        if opp.opportunity_id in self._seen_opportunities:
            seen_at = self._seen_opportunities[opp.opportunity_id]
            if datetime.now(timezone.utc) - seen_at < self._dedup_window:
                return True
        return False

    def _clean_dedup_cache(self) -> None:
        """Remove old entries from deduplication cache."""
        cutoff = datetime.now(timezone.utc) - self._dedup_window
        to_remove = [
            opp_id for opp_id, seen_at in self._seen_opportunities.items()
            if seen_at < cutoff
        ]
        for opp_id in to_remove:
            del self._seen_opportunities[opp_id]

    def get_scanner_stats(self) -> Dict[str, int]:
        """Get stats about scanner state."""
        return {
            "total_markets": len(self._markets),
            "enabled_scanners": len(self._scanners),
            "correlated_pairs": len(self.correlation_engine.get_correlated_pairs()),
            "market_clusters": len(self.correlation_engine.get_clusters()),
            "dedup_cache_size": len(self._seen_opportunities),
        }


def create_stat_arb_signals(
    api: PolymarketAPI,
    types: Optional[List[str]] = None,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
    stop_z: float = 3.5,
    min_correlation: float = 0.7,
    multi_min_edge: int = 30,
    dup_min_edge: int = 30,
    dup_similarity: float = 0.85,
    cond_min_edge: int = 50,
) -> StatArbSignals:
    """
    Factory function to create StatArbSignals with custom configuration.

    Args:
        api: PolymarketAPI instance
        types: List of arb types to enable (default: all)
        entry_z: Z-score threshold for pair trade entry
        exit_z: Z-score threshold for pair trade exit
        stop_z: Z-score threshold for pair trade stop loss
        min_correlation: Minimum correlation for pairs
        multi_min_edge: Minimum edge for multi-outcome arb (bps)
        dup_min_edge: Minimum edge for duplicate arb (bps)
        dup_similarity: Minimum similarity for duplicate detection
        cond_min_edge: Minimum edge for conditional arb (bps)

    Returns:
        Configured StatArbSignals instance
    """
    from .config import (
        StatArbConfig, PairTradingConfig, MultiOutcomeConfig,
        DuplicateConfig, ConditionalConfig, CorrelationConfig
    )

    # Determine enabled types
    enabled_types = types if types else ["pair_spread", "multi_outcome", "duplicate", "conditional"]
    type_set = set(enabled_types)

    config = StatArbConfig(
        enabled_types=list(type_set),
        pair_trading=PairTradingConfig(
            entry_z_threshold=entry_z,
            exit_z_threshold=exit_z,
            stop_loss_z=stop_z,
            min_correlation=min_correlation,
        ),
        multi_outcome=MultiOutcomeConfig(
            min_edge_bps=multi_min_edge,
        ),
        duplicate=DuplicateConfig(
            min_edge_bps=dup_min_edge,
            min_similarity=dup_similarity,
        ),
        conditional=ConditionalConfig(
            min_edge_bps=cond_min_edge,
        ),
        correlation=CorrelationConfig(
            min_price_correlation=min_correlation,
        ),
    )

    return StatArbSignals(api, config)


def create_stat_arb_bot(
    agent_id: str = "stat-arb-bot",
    dry_run: bool = True,
    types: Optional[List[str]] = None,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
    stop_z: float = 3.5,
    min_correlation: float = 0.7,
    max_positions: int = 10,
    position_size_pct: float = 0.10,
) -> "TradingBot":
    """
    Factory function to create a stat arb trading bot.

    Args:
        agent_id: Unique identifier for this bot
        dry_run: If True, simulate trades without executing
        types: List of arb types to enable
        entry_z: Z-score for pair trade entry
        exit_z: Z-score for pair trade exit
        stop_z: Z-score for stop loss
        min_correlation: Minimum correlation threshold
        max_positions: Maximum concurrent positions
        position_size_pct: Position size as fraction of capital

    Returns:
        TradingBot configured for stat arb
    """
    from polymarket.trading.bot import TradingBot
    from polymarket.trading.components.executors import (
        MultiLegExecutor, DryRunMultiLegExecutor
    )
    from polymarket.trading.components.sizers import FixedFractionSizer

    api = PolymarketAPI()

    signal_source = create_stat_arb_signals(
        api, types=types, entry_z=entry_z, exit_z=exit_z,
        stop_z=stop_z, min_correlation=min_correlation,
    )

    if dry_run:
        executor = DryRunMultiLegExecutor()
    else:
        executor = MultiLegExecutor()

    sizer = FixedFractionSizer(
        fraction=position_size_pct,
        min_trade_usd=10.0,
        max_trade_usd=100.0,
    )

    return TradingBot(
        agent_id=agent_id,
        signal_source=signal_source,
        position_sizer=sizer,
        executor=executor,
        dry_run=dry_run,
    )
