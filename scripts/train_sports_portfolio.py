#!/usr/bin/env python3
"""
Sports Portfolio Training and Backtesting Script.

Collects historical data, trains sport-specific ML models,
and backtests with parameter optimization.

Usage:
    # Collect data and train
    python scripts/train_sports_portfolio.py --collect --train

    # Backtest with optimization
    python scripts/train_sports_portfolio.py --backtest --optimize

    # Full pipeline
    python scripts/train_sports_portfolio.py --full
"""

import asyncio
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from polymarket.core.api import PolymarketAPI
from polymarket.strategies.sports_portfolio.config import SportsPortfolioConfig, MLModelConfig
from polymarket.strategies.sports_portfolio.data_collector import SportsDataCollector
from polymarket.strategies.sports_portfolio.trainer import SportSpecificTrainer
from polymarket.strategies.sports_portfolio.backtest import (
    SportsPortfolioBacktester,
    BacktestResult,
    generate_backtest_report,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def collect_data(
    sports: list,
    days_back: int,
) -> dict:
    """Collect historical sports market data."""
    logger.info(f"Collecting data for: {sports}")
    logger.info(f"Looking back {days_back} days")

    async with PolymarketAPI() as api:
        collector = SportsDataCollector(api)
        stats = await collector.collect_historical_data(
            days_back=days_back,
            sports=sports,
        )

        # Print stats
        logger.info("Collection complete!")
        logger.info(f"  Markets: {stats.get('markets', 0)}")
        logger.info(f"  Games: {stats.get('games', 0)}")
        logger.info(f"  Training samples: {stats.get('training_samples', 0)}")

        # Detailed stats
        detailed = collector.get_stats()
        logger.info("\nBy sport:")
        for sport, count in detailed.get("by_sport", {}).items():
            logger.info(f"  {sport}: {count} samples")

        logger.info("\nBy market type pairs:")
        for pair, info in list(detailed.get("by_market_types", {}).items())[:10]:
            logger.info(f"  {pair}: count={info['count']}, avg_corr={info['avg_correlation']:.2f}")

        return stats


def train_models(sports: list) -> dict:
    """Train sport-specific ML models."""
    logger.info("Training sport-specific models")

    # Load training data
    collector = SportsDataCollector(PolymarketAPI())  # Just for db access
    training_data = collector.get_training_data()

    if not training_data:
        logger.error("No training data found. Run with --collect first.")
        return {}

    logger.info(f"Loaded {len(training_data)} training samples")

    # Train models
    config = MLModelConfig()
    trainer = SportSpecificTrainer(config)

    results = trainer.train_all_sports(
        training_data,
        min_samples=30,  # Lower threshold for initial testing
    )

    # Print results
    logger.info("\nTraining results:")
    for sport, metrics in results.items():
        if "error" in metrics:
            logger.warning(f"  {sport}: {metrics['error']}")
        else:
            logger.info(
                f"  {sport}: R2={metrics.get('test_r2', 0):.3f}, "
                f"MAE={metrics.get('test_mae', 0):.3f}, "
                f"samples={metrics.get('samples', 0)}"
            )

    return results


def run_backtest(
    sports: list,
    optimize: bool = False,
) -> dict:
    """Run backtest with optional optimization."""
    logger.info("Running backtest")

    # Load trained models
    config = MLModelConfig()
    trainer = SportSpecificTrainer(config)
    loaded = trainer.load_models()

    logger.info(f"Loaded models: {list(loaded.keys())}")

    # Create backtester
    backtester = SportsPortfolioBacktester(trainer=trainer)

    results = {}

    for sport in sports:
        logger.info(f"\nBacktesting {sport}...")

        if optimize:
            # Optimize parameters
            best_params, result = backtester.optimize_parameters(
                sport,
                metric="sharpe_ratio",
            )

            if result:
                results[sport] = result
                logger.info(f"  Best Sharpe: {result.sharpe_ratio:.2f}")
                logger.info(f"  Best params: {best_params}")
        else:
            # Run with default config
            base_config = SportsPortfolioConfig()
            result = backtester.backtest(sport, base_config)
            results[sport] = result

    return results


def save_results(results: dict, output_dir: Path) -> None:
    """Save backtest results to files."""
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Generate and save report
    report = generate_backtest_report(results)
    report_path = output_dir / f"sports_portfolio_report_{timestamp}.txt"

    with open(report_path, "w") as f:
        f.write(report)

    logger.info(f"Report saved to: {report_path}")

    # Save detailed results as JSON
    import json

    results_dict = {}
    for sport, result in results.items():
        results_dict[sport] = {
            "params": result.params,
            "total_pnl": result.total_pnl,
            "total_trades": result.total_trades,
            "win_rate": result.win_rate,
            "sharpe_ratio": result.sharpe_ratio,
            "sortino_ratio": result.sortino_ratio,
            "max_drawdown": result.max_drawdown,
            "profit_factor": result.profit_factor,
            "avg_positions": result.avg_positions_per_portfolio,
            "avg_hedging": result.avg_hedging_effectiveness,
        }

    json_path = output_dir / f"sports_portfolio_results_{timestamp}.json"
    with open(json_path, "w") as f:
        json.dump(results_dict, f, indent=2)

    logger.info(f"Results saved to: {json_path}")


def print_summary(results: dict) -> None:
    """Print summary of results."""
    print("\n" + "=" * 70)
    print("BACKTEST SUMMARY")
    print("=" * 70)

    for sport, result in results.items():
        print(f"\n{sport.upper()}:")
        print(f"  Total P&L:     ${result.total_pnl:,.2f}")
        print(f"  Total Trades:  {result.total_trades}")
        print(f"  Win Rate:      {result.win_rate:.1%}")
        print(f"  Sharpe Ratio:  {result.sharpe_ratio:.2f}")
        print(f"  Max Drawdown:  {result.max_drawdown:.1%}")
        print(f"  Profit Factor: {result.profit_factor:.2f}")

        if result.params:
            print("  Best Parameters:")
            for k, v in result.params.items():
                print(f"    {k}: {v}")

    # Overall
    total_pnl = sum(r.total_pnl for r in results.values())
    total_trades = sum(r.total_trades for r in results.values())

    print("\n" + "=" * 70)
    print(f"TOTAL P&L: ${total_pnl:,.2f} across {total_trades} trades")
    print("=" * 70)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Sports Portfolio Training and Backtesting"
    )

    parser.add_argument(
        "--collect",
        action="store_true",
        help="Collect historical data from Polymarket",
    )

    parser.add_argument(
        "--train",
        action="store_true",
        help="Train sport-specific ML models",
    )

    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run backtest",
    )

    parser.add_argument(
        "--optimize",
        action="store_true",
        help="Optimize parameters during backtest",
    )

    parser.add_argument(
        "--full",
        action="store_true",
        help="Run full pipeline: collect, train, backtest with optimization",
    )

    parser.add_argument(
        "--sports",
        type=str,
        default="nba,nfl,nhl",
        help="Comma-separated list of sports (default: nba,nfl,nhl)",
    )

    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Days of historical data to collect (default: 90)",
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default="reports/sports_portfolio",
        help="Output directory for reports (default: reports/sports_portfolio)",
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    return parser.parse_args()


async def main():
    """Main entry point."""
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    sports = [s.strip().lower() for s in args.sports.split(",")]
    output_dir = Path(args.output_dir)

    # Determine what to run
    do_collect = args.collect or args.full
    do_train = args.train or args.full
    do_backtest = args.backtest or args.full
    do_optimize = args.optimize or args.full

    if not any([do_collect, do_train, do_backtest]):
        logger.error("Specify at least one action: --collect, --train, --backtest, or --full")
        return

    # Run pipeline
    if do_collect:
        logger.info("\n" + "=" * 50)
        logger.info("STEP 1: DATA COLLECTION")
        logger.info("=" * 50)
        await collect_data(sports, args.days)

    if do_train:
        logger.info("\n" + "=" * 50)
        logger.info("STEP 2: MODEL TRAINING")
        logger.info("=" * 50)
        train_models(sports)

    if do_backtest:
        logger.info("\n" + "=" * 50)
        logger.info("STEP 3: BACKTESTING" + (" WITH OPTIMIZATION" if do_optimize else ""))
        logger.info("=" * 50)
        results = run_backtest(sports, optimize=do_optimize)

        if results:
            print_summary(results)
            save_results(results, output_dir)


if __name__ == "__main__":
    asyncio.run(main())
