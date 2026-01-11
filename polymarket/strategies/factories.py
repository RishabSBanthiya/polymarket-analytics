"""
Unified Bot Factory Module.

Single entry point for creating any trading bot type.
All bots use the TradingBot composition pattern.
"""

import logging
from typing import Optional, List, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket.trading.bot import TradingBot

logger = logging.getLogger(__name__)


STRATEGIES = ["bond", "flow", "arb", "stat-arb", "sports"]


def create_bot(
    strategy: str,
    agent_id: Optional[str] = None,
    dry_run: bool = True,
    **kwargs
) -> "TradingBot":
    """
    Create a trading bot for any strategy.

    Args:
        strategy: One of "bond", "flow", "arb", "stat-arb", "sports"
        agent_id: Unique agent identifier (default: "{strategy}-bot")
        dry_run: Whether to simulate trades
        **kwargs: Strategy-specific configuration

    Returns:
        Configured TradingBot ready to start

    Examples:
        # Bond bot
        bot = create_bot("bond", dry_run=True, min_price=0.95)

        # Flow bot
        bot = create_bot("flow", dry_run=True, min_score=40)

        # Arb bot
        bot = create_bot("arb", dry_run=True, min_edge_bps=50)

        # Stat arb bot
        bot = create_bot("stat-arb", dry_run=True, types=["pair_spread"])

        # Sports bot
        bot = create_bot("sports", dry_run=True, sports=["nba", "nfl"])
    """
    # Normalize strategy name
    strategy = strategy.lower().replace("_", "-")

    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy}. Available: {STRATEGIES}")

    # Default agent ID
    if agent_id is None:
        agent_id = f"{strategy}-bot"

    if strategy == "bond":
        return _create_bond_bot(agent_id, dry_run, **kwargs)
    elif strategy == "flow":
        return _create_flow_bot(agent_id, dry_run, **kwargs)
    elif strategy == "arb":
        return _create_arb_bot(agent_id, dry_run, **kwargs)
    elif strategy == "stat-arb":
        return _create_stat_arb_bot(agent_id, dry_run, **kwargs)
    elif strategy == "sports":
        return _create_sports_bot(agent_id, dry_run, **kwargs)
    else:
        raise ValueError(f"Strategy {strategy} not implemented")


def _create_bond_bot(
    agent_id: str,
    dry_run: bool,
    min_price: float = 0.95,
    max_price: float = 0.98,
    **kwargs
) -> "TradingBot":
    """Create bond strategy bot."""
    from polymarket.strategies.bond_strategy import create_bond_bot
    return create_bond_bot(
        agent_id=agent_id,
        dry_run=dry_run,
        min_price=min_price,
        max_price=max_price,
    )


def _create_flow_bot(
    agent_id: str,
    dry_run: bool,
    min_score: float = 30.0,
    min_trade_size: float = 100.0,
    category: Optional[str] = None,
    **kwargs
) -> "TradingBot":
    """Create flow copy strategy bot."""
    from polymarket.strategies.flow_strategy import create_flow_bot
    return create_flow_bot(
        agent_id=agent_id,
        dry_run=dry_run,
        min_score=min_score,
        min_trade_size=min_trade_size,
        category=category,
    )


def _create_arb_bot(
    agent_id: str,
    dry_run: bool,
    min_edge_bps: int = 50,
    order_size_usd: float = 20.0,
    max_positions: int = 5,
    **kwargs
) -> "TradingBot":
    """Create arbitrage strategy bot."""
    from polymarket.strategies.arb_strategy import create_arb_bot
    return create_arb_bot(
        agent_id=agent_id,
        dry_run=dry_run,
        min_edge_bps=min_edge_bps,
        order_size_usd=order_size_usd,
        max_positions=max_positions,
    )


def _create_stat_arb_bot(
    agent_id: str,
    dry_run: bool,
    types: Optional[List[str]] = None,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
    stop_z: float = 3.5,
    min_correlation: float = 0.7,
    max_positions: int = 10,
    position_size_pct: float = 0.10,
    **kwargs
) -> "TradingBot":
    """Create stat arb strategy bot."""
    from polymarket.strategies.stat_arb.signals import create_stat_arb_bot
    return create_stat_arb_bot(
        agent_id=agent_id,
        dry_run=dry_run,
        types=types,
        entry_z=entry_z,
        exit_z=exit_z,
        stop_z=stop_z,
        min_correlation=min_correlation,
        max_positions=max_positions,
        position_size_pct=position_size_pct,
    )


def _create_sports_bot(
    agent_id: str,
    dry_run: bool,
    sports: Optional[List[str]] = None,
    capital: float = 200.0,
    max_portfolios: int = 3,
    min_sharpe: float = 0.5,
    risk_aversion: float = 2.0,
    stop_loss: float = 0.15,
    take_profit: float = 0.30,
    allow_shorts: bool = False,
    **kwargs
) -> "TradingBot":
    """Create sports portfolio strategy bot."""
    from polymarket.strategies.sports_portfolio.scanner import create_sports_bot
    return create_sports_bot(
        agent_id=agent_id,
        dry_run=dry_run,
        sports=sports,
        capital=capital,
        max_portfolios=max_portfolios,
        min_sharpe=min_sharpe,
        risk_aversion=risk_aversion,
        stop_loss=stop_loss,
        take_profit=take_profit,
        allow_shorts=allow_shorts,
    )


def get_strategy_defaults(strategy: str) -> Dict[str, Any]:
    """
    Get default configuration for a strategy.

    Args:
        strategy: Strategy name

    Returns:
        Dict of default parameter values
    """
    defaults = {
        "bond": {
            "min_price": 0.95,
            "max_price": 0.98,
            "interval": 5.0,
        },
        "flow": {
            "min_score": 30.0,
            "min_trade_size": 100.0,
            "interval": 2.0,
        },
        "arb": {
            "min_edge_bps": 50,
            "order_size_usd": 20.0,
            "max_positions": 5,
            "interval": 10.0,
        },
        "stat-arb": {
            "types": ["pair_spread", "multi_outcome", "duplicate"],
            "entry_z": 2.0,
            "exit_z": 0.5,
            "stop_z": 3.5,
            "min_correlation": 0.7,
            "max_positions": 10,
            "interval": 30.0,
        },
        "sports": {
            "sports": ["nba", "nfl", "nhl"],
            "capital": 200.0,
            "max_portfolios": 3,
            "min_sharpe": 0.5,
            "interval": 60.0,
        },
    }

    strategy = strategy.lower().replace("_", "-")
    return defaults.get(strategy, {})


def list_strategies() -> List[str]:
    """Get list of available strategies."""
    return STRATEGIES.copy()
