#!/usr/bin/env python3
"""
Unified Trading Bot Runner.

Run any trading strategy with a single command.

Usage:
    # Bond strategy (expiring markets)
    python run_bot.py bond --dry-run

    # Flow copy strategy
    python run_bot.py flow --dry-run --min-score 40

    # Arbitrage strategy
    python run_bot.py arb --dry-run --min-edge 50

    # Statistical arbitrage
    python run_bot.py stat-arb --dry-run --types pair_spread,multi_outcome

    # Sports portfolio
    python run_bot.py sports --dry-run --sports nba,nfl

    # Multiple agents
    python run_bot.py bond --agent-id bond-1 &
    python run_bot.py flow --agent-id flow-1 &
"""

import asyncio
import argparse
import logging
import logging.handlers
import signal
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from polymarket.core.config import Config, get_config


# Global for signal handling
_bot = None
logger = None


def setup_logging(agent_id: str, log_level: str, log_dir: str = "logs") -> logging.Logger:
    """
    Setup logging with both console and file output.
    
    Creates per-bot log files with rotation.
    """
    # Create log directory
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    # Create logger for this bot
    bot_logger = logging.getLogger()
    bot_logger.setLevel(getattr(logging, log_level))
    
    # Clear any existing handlers
    bot_logger.handlers = []
    
    # Format
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level))
    console_handler.setFormatter(formatter)
    bot_logger.addHandler(console_handler)
    
    # File handler (rotating, 10MB max, keep 5 backups)
    log_file = log_path / f"{agent_id}.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)  # Always log DEBUG to file
    file_handler.setFormatter(formatter)
    bot_logger.addHandler(file_handler)
    
    # Also create a trades-only log for this bot
    trades_file = log_path / f"{agent_id}_trades.log"
    trades_handler = logging.handlers.RotatingFileHandler(
        trades_file,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=10,  # Keep more trade history
        encoding='utf-8'
    )
    trades_handler.setLevel(logging.INFO)
    trades_handler.setFormatter(formatter)
    # Only log from trading module to this file
    trades_handler.addFilter(lambda record: 'trading' in record.name or 'strategies' in record.name)
    bot_logger.addHandler(trades_handler)
    
    return logging.getLogger(__name__)


def setup_async_signal_handlers(bot):
    """Setup graceful shutdown handlers using asyncio-friendly approach"""
    def handler():
        logger.info("Received shutdown signal, stopping bot...")
        bot.running = False

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handler)


async def run_bond(args):
    """Run bond strategy"""
    from polymarket.strategies.bond_strategy import create_bond_bot

    global _bot

    _bot = create_bond_bot(
        agent_id=args.agent_id,
        dry_run=args.dry_run,
        min_price=args.min_price,
        max_price=args.max_price,
    )

    # Setup signal handlers after bot is created
    setup_async_signal_handlers(_bot)

    try:
        await _bot.start()
        await _bot.run(interval_seconds=args.interval)
    finally:
        await _bot.stop()


async def run_flow(args):
    """Run flow copy strategy"""
    from polymarket.strategies.flow_strategy import create_flow_bot, FlowCopySignalSource

    global _bot

    _bot = create_flow_bot(
        agent_id=args.agent_id,
        dry_run=args.dry_run,
        min_score=args.min_score,
        min_trade_size=args.min_trade_size,
        category=args.category,
    )

    # Setup signal handlers after bot is created
    setup_async_signal_handlers(_bot)

    try:
        await _bot.start()

        # Start flow detector
        if isinstance(_bot.signal_source, FlowCopySignalSource):
            await _bot.signal_source.start_detector()

        await _bot.run(interval_seconds=args.interval)

    finally:
        # Stop detector
        if isinstance(_bot.signal_source, FlowCopySignalSource):
            await _bot.signal_source.stop_detector()

        await _bot.stop()


async def run_arb(args):
    """Run arbitrage strategy"""
    from polymarket.strategies.arb_strategy import create_arb_bot, ArbSignals

    global _bot

    _bot = create_arb_bot(
        agent_id=args.agent_id,
        dry_run=args.dry_run,
        min_edge_bps=args.min_edge,
        order_size_usd=args.order_size,
        max_positions=args.max_positions,
    )

    setup_async_signal_handlers(_bot)

    try:
        await _bot.start()

        # Refresh markets before running
        if isinstance(_bot.signal_source, ArbSignals):
            await _bot.signal_source.refresh_markets()

        await _bot.run(interval_seconds=args.interval)

    finally:
        await _bot.stop()


async def run_stat_arb(args):
    """Run statistical arbitrage strategy"""
    from polymarket.strategies.stat_arb.signals import create_stat_arb_bot

    global _bot

    # Parse types
    types = args.types.split(",") if args.types else None

    _bot = create_stat_arb_bot(
        agent_id=args.agent_id,
        dry_run=args.dry_run,
        types=types,
        entry_z=args.entry_z,
        exit_z=args.exit_z,
        stop_z=args.stop_z,
        min_correlation=args.min_correlation,
        max_positions=args.max_positions,
        position_size_pct=args.position_size,
    )

    setup_async_signal_handlers(_bot)

    try:
        await _bot.start()
        await _bot.run(interval_seconds=args.interval)
    finally:
        await _bot.stop()


async def run_sports(args):
    """Run sports portfolio strategy"""
    from polymarket.strategies.sports_portfolio.scanner import create_sports_bot

    global _bot

    # Parse sports
    sports = args.sports.split(",") if args.sports else None

    _bot = create_sports_bot(
        agent_id=args.agent_id,
        dry_run=args.dry_run,
        sports=sports,
        capital=args.capital,
        max_portfolios=args.max_portfolios,
        min_sharpe=args.min_sharpe,
        risk_aversion=args.risk_aversion,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit,
        allow_shorts=args.allow_shorts,
    )

    setup_async_signal_handlers(_bot)

    try:
        await _bot.start()
        await _bot.run(interval_seconds=args.interval)
    finally:
        await _bot.stop()


def main():
    parser = argparse.ArgumentParser(
        description="Unified Trading Bot Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_bot.py bond --dry-run
  python run_bot.py flow --dry-run --min-score 40
  python run_bot.py arb --dry-run --min-edge 50
  python run_bot.py stat-arb --dry-run --types pair_spread
  python run_bot.py sports --dry-run --sports nba,nfl
        """
    )

    subparsers = parser.add_subparsers(dest="strategy", help="Trading strategy")

    # Bond strategy
    bond_parser = subparsers.add_parser("bond", help="Expiring market strategy")
    bond_parser.add_argument("--agent-id", default="bond-bot", help="Agent ID")
    bond_parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    bond_parser.add_argument("--interval", type=float, default=5.0, help="Scan interval (seconds)")
    bond_parser.add_argument("--min-price", type=float, default=0.95, help="Minimum price")
    bond_parser.add_argument("--max-price", type=float, default=0.98, help="Maximum price")

    # Flow strategy
    flow_parser = subparsers.add_parser("flow", help="Flow copy strategy")
    flow_parser.add_argument("--agent-id", default="flow-bot", help="Agent ID")
    flow_parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    flow_parser.add_argument("--interval", type=float, default=2.0, help="Scan interval (seconds)")
    flow_parser.add_argument("--min-score", type=float, default=30.0, help="Minimum signal score")
    flow_parser.add_argument("--min-trade-size", type=float, default=100.0, help="Min trade size to track")
    flow_parser.add_argument("--category", type=str, default=None, help="Market category filter")

    # Arb strategy
    arb_parser = subparsers.add_parser("arb", help="Delta-neutral arbitrage")
    arb_parser.add_argument("--agent-id", default="arb-bot", help="Agent ID")
    arb_parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    arb_parser.add_argument("--interval", type=float, default=10.0, help="Scan interval (seconds)")
    arb_parser.add_argument("--min-edge", type=int, default=50, help="Minimum edge (bps)")
    arb_parser.add_argument("--order-size", type=float, default=20.0, help="Order size (USD)")
    arb_parser.add_argument("--max-positions", type=int, default=5, help="Max concurrent positions")

    # Stat-arb strategy
    stat_arb_parser = subparsers.add_parser("stat-arb", help="Statistical arbitrage")
    stat_arb_parser.add_argument("--agent-id", default="stat-arb-bot", help="Agent ID")
    stat_arb_parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    stat_arb_parser.add_argument("--interval", type=float, default=30.0, help="Scan interval (seconds)")
    stat_arb_parser.add_argument("--types", type=str, default=None,
                                  help="Arb types: pair_spread,multi_outcome,duplicate,conditional")
    stat_arb_parser.add_argument("--entry-z", type=float, default=2.0, help="Entry z-score")
    stat_arb_parser.add_argument("--exit-z", type=float, default=0.5, help="Exit z-score")
    stat_arb_parser.add_argument("--stop-z", type=float, default=3.5, help="Stop loss z-score")
    stat_arb_parser.add_argument("--min-correlation", type=float, default=0.7, help="Min correlation")
    stat_arb_parser.add_argument("--max-positions", type=int, default=10, help="Max positions")
    stat_arb_parser.add_argument("--position-size", type=float, default=0.10, help="Position size (fraction)")

    # Sports strategy
    sports_parser = subparsers.add_parser("sports", help="Sports portfolio strategy")
    sports_parser.add_argument("--agent-id", default="sports-bot", help="Agent ID")
    sports_parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    sports_parser.add_argument("--interval", type=float, default=60.0, help="Scan interval (seconds)")
    sports_parser.add_argument("--sports", type=str, default="nba,nfl,nhl", help="Sports to track")
    sports_parser.add_argument("--capital", type=float, default=200.0, help="Capital per portfolio")
    sports_parser.add_argument("--max-portfolios", type=int, default=3, help="Max concurrent portfolios")
    sports_parser.add_argument("--min-sharpe", type=float, default=0.5, help="Minimum Sharpe ratio")
    sports_parser.add_argument("--risk-aversion", type=float, default=2.0, help="Risk aversion")
    sports_parser.add_argument("--stop-loss", type=float, default=0.15, help="Stop loss pct")
    sports_parser.add_argument("--take-profit", type=float, default=0.30, help="Take profit pct")
    sports_parser.add_argument("--allow-shorts", action="store_true", help="Allow short positions")

    # Global args
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log level")
    parser.add_argument("--log-dir", default="logs",
                        help="Directory for log files (default: logs)")

    args = parser.parse_args()

    if not args.strategy:
        parser.print_help()
        sys.exit(1)

    # Get agent_id from subparser args
    agent_id = getattr(args, 'agent_id', f'{args.strategy}-bot')

    # Setup logging with file output
    global logger
    logger = setup_logging(agent_id, args.log_level, args.log_dir)

    logger.info(f"{'='*60}")
    logger.info(f"Starting {args.strategy} bot: {agent_id}")
    logger.info(f"Log files: {args.log_dir}/{agent_id}.log")
    logger.info(f"{'='*60}")

    # Run appropriate strategy
    if args.strategy == "bond":
        asyncio.run(run_bond(args))
    elif args.strategy == "flow":
        asyncio.run(run_flow(args))
    elif args.strategy == "arb":
        asyncio.run(run_arb(args))
    elif args.strategy == "stat-arb":
        asyncio.run(run_stat_arb(args))
    elif args.strategy == "sports":
        asyncio.run(run_sports(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

