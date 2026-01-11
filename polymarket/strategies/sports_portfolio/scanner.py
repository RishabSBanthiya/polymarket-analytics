"""
Sports Portfolio Scanner.

Scans for portfolio opportunities across sports games
and generates signals for the trading bot.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional, AsyncIterator

from polymarket.core.api import PolymarketAPI
from polymarket.core.models import Signal, SignalDirection
from polymarket.trading.components.signals import SignalSource

from .models import (
    SportsGame,
    CorrelationMatrix,
    PortfolioPosition,
    PortfolioAllocation,
)
from .config import SportsPortfolioConfig
from .game_aggregator import GameMarketAggregator
from .correlation_model import MLCorrelationModel
from .portfolio_optimizer import PortfolioOptimizer

logger = logging.getLogger(__name__)


class SportsPortfolioScanner:
    """
    Scans sports games for portfolio opportunities.

    Integrates:
    - Game market aggregation
    - ML correlation prediction
    - Portfolio optimization
    - Signal generation
    """

    def __init__(
        self,
        api: PolymarketAPI,
        config: Optional[SportsPortfolioConfig] = None,
    ):
        self.api = api
        self.config = config or SportsPortfolioConfig()

        # Components
        self.game_aggregator = GameMarketAggregator(api, self.config)
        self.correlation_model = MLCorrelationModel(self.config.ml_model)
        self.portfolio_optimizer = PortfolioOptimizer(self.config.portfolio_opt)

        # State
        self._active_positions: Dict[str, PortfolioPosition] = {}
        self._last_scan: Optional[datetime] = None
        self._opportunities: List[PortfolioPosition] = []

    async def scan(self) -> List[PortfolioPosition]:
        """
        Scan all games for portfolio opportunities.

        Returns list of optimized portfolios meeting criteria.
        """
        # Get available games
        games = await self.game_aggregator.get_games(
            sports=self.config.get_enabled_sports(),
            hours_ahead=int(self.config.risk.max_time_to_resolution_hours),
        )

        if not games:
            logger.debug("No games found")
            return []

        logger.info(f"Scanning {len(games)} games for portfolio opportunities")
        opportunities = []

        stats = {"total": len(games), "active": 0, "time_early": 0, "time_late": 0, "no_neg_corr": 0, "opt_fail": 0, "low_sharpe": 0, "low_hedge": 0}

        for game in games:
            # Skip if already have position in this game
            if game.game_id in self._active_positions:
                stats["active"] += 1
                continue

            # Check time constraints
            time_to_start = (game.game_time - datetime.now(timezone.utc)).total_seconds() / 3600
            if time_to_start < self.config.risk.min_time_to_resolution_hours:
                stats["time_early"] += 1
                logger.debug(f"Game {game.game_id}: too soon ({time_to_start:.1f}h < {self.config.risk.min_time_to_resolution_hours}h)")
                continue
            if time_to_start > self.config.risk.max_time_to_resolution_hours:
                stats["time_late"] += 1
                logger.debug(f"Game {game.game_id}: too far ({time_to_start:.1f}h > {self.config.risk.max_time_to_resolution_hours}h)")
                continue

            # Predict correlations
            correlation_matrix = self.correlation_model.predict_correlation_matrix(game)

            # Check for sufficient negative correlations
            neg_pairs = correlation_matrix.get_negatively_correlated_pairs(
                threshold=self.config.min_negative_correlation,
                min_confidence=self.config.min_correlation_confidence,
            )

            if not neg_pairs:
                stats["no_neg_corr"] += 1
                logger.debug(f"Game {game.game_id}: no negative correlation pairs (threshold={self.config.min_negative_correlation}, conf={self.config.min_correlation_confidence})")
                continue

            logger.debug(f"Game {game.game_id}: {len(neg_pairs)} negative correlation pairs found")

            # Optimize portfolio
            position = self.portfolio_optimizer.optimize(
                game=game,
                correlation_matrix=correlation_matrix,
                capital=self.config.risk.max_portfolio_cost,
            )

            if position is None:
                stats["opt_fail"] += 1
                logger.debug(f"Game {game.game_id}: optimization failed")
                continue

            # Additional checks
            if position.sharpe_ratio < self.config.portfolio_opt.min_sharpe_ratio:
                stats["low_sharpe"] += 1
                logger.debug(f"Game {game.game_id}: Sharpe {position.sharpe_ratio:.3f} < {self.config.portfolio_opt.min_sharpe_ratio}")
                continue

            if position.hedging_effectiveness < self.config.portfolio_opt.min_hedging_effectiveness:
                stats["low_hedge"] += 1
                logger.debug(f"Game {game.game_id}: Hedge eff {position.hedging_effectiveness:.2f} < {self.config.portfolio_opt.min_hedging_effectiveness}")
                continue

            opportunities.append(position)

            if self.config.log_opportunities:
                logger.info(
                    f"Portfolio opportunity: {game.home_team} vs {game.away_team} | "
                    f"Positions: {position.num_positions} | "
                    f"Sharpe: {position.sharpe_ratio:.2f} | "
                    f"Hedge: {position.hedging_effectiveness:.1%}"
                )

        self._opportunities = opportunities
        self._last_scan = datetime.now(timezone.utc)

        # Log filtering summary
        logger.info(
            f"Scan summary: {stats['total']} games | "
            f"active={stats['active']}, time_early={stats['time_early']}, time_late={stats['time_late']}, "
            f"no_neg_corr={stats['no_neg_corr']}, opt_fail={stats['opt_fail']}, "
            f"low_sharpe={stats['low_sharpe']}, low_hedge={stats['low_hedge']} | "
            f"Found {len(opportunities)} opportunities"
        )

        return opportunities

    async def generate_signals(self) -> AsyncIterator[Signal]:
        """
        Generate trading signals from portfolio opportunities.

        Yields individual market signals for execution.
        """
        if not self._opportunities:
            await self.scan()

        for position in self._opportunities:
            for alloc in position.allocations:
                # Create signal for each allocation
                direction = SignalDirection.BUY if alloc.weight > 0 else SignalDirection.SELL

                signal = Signal(
                    market_id=alloc.market_id,
                    token_id=alloc.token_id,
                    direction=direction,
                    score=position.sharpe_ratio,  # Use Sharpe as signal strength
                    source="sports_portfolio",
                    metadata={
                        "portfolio_id": position.position_id,
                        "game_id": position.game_id,
                        "weight": alloc.weight,
                        "target_shares": alloc.shares,
                        "entry_price": alloc.entry_price,
                        "portfolio_sharpe": position.sharpe_ratio,
                        "hedging_effectiveness": position.hedging_effectiveness,
                        "avg_correlation": position.avg_pairwise_correlation,
                    },
                )
                yield signal

    def add_position(self, position: PortfolioPosition) -> None:
        """Track active position."""
        self._active_positions[position.game_id] = position

    def remove_position(self, game_id: str) -> None:
        """Remove tracked position."""
        self._active_positions.pop(game_id, None)

    def get_position(self, game_id: str) -> Optional[PortfolioPosition]:
        """Get active position for a game."""
        return self._active_positions.get(game_id)

    async def monitor_positions(self) -> List[Dict]:
        """
        Monitor active positions and check for exit conditions.

        Returns list of positions needing action.
        """
        actions = []

        for game_id, position in list(self._active_positions.items()):
            # Fetch current prices
            prices = {}
            for alloc in position.allocations:
                ob = await self.api.fetch_orderbook(alloc.token_id)
                if ob and ob.best_bid and ob.best_ask:
                    prices[alloc.token_id] = (ob.best_bid + ob.best_ask) / 2

            # Update P&L
            position.update_prices(prices)

            # Check exit conditions
            action = None

            # Stop loss
            if position.unrealized_pnl < 0:
                loss_pct = abs(position.unrealized_pnl) / position.total_cost
                if loss_pct >= self.config.risk.portfolio_stop_loss_pct:
                    action = {
                        "type": "stop_loss",
                        "game_id": game_id,
                        "position": position,
                        "reason": f"Loss {loss_pct:.1%} exceeds stop {self.config.risk.portfolio_stop_loss_pct:.1%}",
                    }

            # Take profit
            if position.unrealized_pnl > 0:
                profit_pct = position.unrealized_pnl / position.total_cost
                if profit_pct >= self.config.risk.portfolio_take_profit_pct:
                    action = {
                        "type": "take_profit",
                        "game_id": game_id,
                        "position": position,
                        "reason": f"Profit {profit_pct:.1%} exceeds target {self.config.risk.portfolio_take_profit_pct:.1%}",
                    }

            # Game started (if not wanting live exposure)
            game = await self.game_aggregator.get_game(game_id)
            if game and game.is_live:
                action = {
                    "type": "game_live",
                    "game_id": game_id,
                    "position": position,
                    "reason": "Game has started",
                }

            if action:
                actions.append(action)

        return actions

    def get_portfolio_summary(self) -> Dict:
        """Get summary of all active portfolios."""
        total_cost = 0
        total_pnl = 0
        positions_count = 0

        for position in self._active_positions.values():
            total_cost += position.total_cost
            total_pnl += position.unrealized_pnl
            positions_count += position.num_positions

        return {
            "active_portfolios": len(self._active_positions),
            "total_positions": positions_count,
            "total_cost": total_cost,
            "unrealized_pnl": total_pnl,
            "pnl_pct": (total_pnl / total_cost * 100) if total_cost > 0 else 0,
            "last_scan": self._last_scan.isoformat() if self._last_scan else None,
            "pending_opportunities": len(self._opportunities),
        }

    async def close(self) -> None:
        """Cleanup resources."""
        if self.api:
            await self.api.close()


class SportsPortfolioSignalSource(SignalSource):
    """
    SignalSource implementation for integration with TradingBot.

    Wraps SportsPortfolioScanner for use with the standard bot framework.
    """

    def __init__(
        self,
        api: PolymarketAPI,
        config: Optional[SportsPortfolioConfig] = None,
    ):
        self.scanner = SportsPortfolioScanner(api, config)

    @property
    def name(self) -> str:
        return "sports_portfolio"

    async def get_signals(self) -> List[Signal]:
        """Get signals for trading bot."""
        signals = []
        async for signal in self.scanner.generate_signals():
            signals.append(signal)
        return signals

    async def close(self) -> None:
        await self.scanner.close()


def create_sports_bot(
    agent_id: str = "sports-bot",
    dry_run: bool = True,
    sports: Optional[List[str]] = None,
    capital: float = 200.0,
    max_portfolios: int = 3,
    min_sharpe: float = 0.5,
    risk_aversion: float = 2.0,
    stop_loss: float = 0.15,
    take_profit: float = 0.30,
    allow_shorts: bool = False,
) -> "TradingBot":
    """
    Factory function to create a sports portfolio trading bot.

    Args:
        agent_id: Unique identifier for this bot
        dry_run: If True, simulate trades without executing
        sports: List of sports to monitor (default: nba, nfl, nhl)
        capital: Max capital per portfolio in USD
        max_portfolios: Maximum concurrent portfolios
        min_sharpe: Minimum Sharpe ratio for trades
        risk_aversion: Risk aversion coefficient
        stop_loss: Portfolio stop loss percentage
        take_profit: Portfolio take profit percentage
        allow_shorts: Whether to allow short positions

    Returns:
        TradingBot configured for sports portfolio
    """
    from polymarket.trading.bot import TradingBot
    from polymarket.trading.components.executors import (
        MultiLegExecutor, DryRunMultiLegExecutor
    )
    from polymarket.trading.components.sizers import FixedFractionSizer

    # Build config
    enabled_sports = sports if sports else ["nba", "nfl", "nhl"]
    config = SportsPortfolioConfig(
        enabled_sports=enabled_sports,
    )
    config.risk.max_portfolio_cost = capital
    config.risk.portfolio_stop_loss_pct = stop_loss
    config.risk.portfolio_take_profit_pct = take_profit
    config.portfolio_opt.min_sharpe_ratio = min_sharpe
    config.portfolio_opt.risk_aversion = risk_aversion
    config.portfolio_opt.allow_shorts = allow_shorts

    api = PolymarketAPI()

    signal_source = SportsPortfolioSignalSource(api, config)

    if dry_run:
        executor = DryRunMultiLegExecutor()
    else:
        executor = MultiLegExecutor()

    sizer = FixedFractionSizer(
        fraction=capital / 1000,  # Relative to $1000 base
        min_trade_usd=10.0,
        max_trade_usd=capital,
    )

    return TradingBot(
        agent_id=agent_id,
        signal_source=signal_source,
        position_sizer=sizer,
        executor=executor,
        dry_run=dry_run,
    )
