"""
Unified Backtesting CLI.

Run backtests or optimization for any strategy from a single entry point.

Usage:
    python -m polymarket.backtesting run --strategy bond --backtest
    python -m polymarket.backtesting run --strategy flow --optimize -n 50
    python -m polymarket.backtesting run --strategy arb --backtest --days 30
    python -m polymarket.backtesting run --strategy stat-arb --optimize
    python -m polymarket.backtesting run --strategy sports --backtest
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Backtesting CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m polymarket.backtesting run --strategy bond --backtest
  python -m polymarket.backtesting run --strategy flow --optimize -n 50
  python -m polymarket.backtesting run --strategy stat-arb --backtest --days 90
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Command")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run backtest or optimization")
    run_parser.add_argument(
        "--strategy", "-s",
        required=True,
        choices=["bond", "flow", "arb", "stat-arb", "sports"],
        help="Strategy to backtest"
    )
    run_parser.add_argument(
        "--backtest", "-b",
        action="store_true",
        help="Run backtest with default parameters"
    )
    run_parser.add_argument(
        "--optimize", "-o",
        action="store_true",
        help="Run Bayesian optimization"
    )
    run_parser.add_argument(
        "--days", "-d",
        type=int,
        default=60,
        help="Lookback period in days (default: 60)"
    )
    run_parser.add_argument(
        "--capital", "-c",
        type=float,
        default=1000.0,
        help="Starting capital (default: 1000)"
    )
    run_parser.add_argument(
        "--iterations", "-n",
        type=int,
        default=50,
        help="Optimization iterations (default: 50)"
    )
    run_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output"
    )

    # Strategy-specific args
    run_parser.add_argument("--entry-price", type=float, help="Bond: entry price")
    run_parser.add_argument("--max-spread", type=float, help="Bond: max spread pct")
    run_parser.add_argument("--min-edge", type=int, help="Arb/StatArb: min edge bps")
    run_parser.add_argument("--types", type=str, help="StatArb: arb types (comma-separated)")
    run_parser.add_argument("--min-negative-corr", type=float, help="Sports: min negative correlation")
    run_parser.add_argument("--max-position", type=float, help="Sports: max position pct")
    run_parser.add_argument("--min-edge-pct", type=float, help="Sports: min edge pct")
    run_parser.add_argument("--sport", type=str, default="all", help="Sports: sport to backtest (nba, nfl, nhl, all)")

    # List command
    list_parser = subparsers.add_parser("list", help="List available strategies")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "list":
        print("Available strategies:")
        print("  bond     - Expiring market strategy")
        print("  flow     - Flow copy strategy")
        print("  arb      - Delta-neutral arbitrage")
        print("  stat-arb - Statistical arbitrage")
        print("  sports   - Sports portfolio")
        sys.exit(0)

    if args.command == "run":
        if not args.backtest and not args.optimize:
            print("Error: Must specify --backtest or --optimize")
            sys.exit(1)

        asyncio.run(run_backtest(args))


async def run_backtest(args):
    """Run backtest or optimization for the specified strategy."""
    from .runner import BacktestRunner

    runner = BacktestRunner(
        initial_capital=args.capital,
        lookback_days=args.days,
        verbose=args.verbose,
    )

    # Build strategy-specific params
    params = {}

    if args.strategy == "bond":
        if args.entry_price:
            params["entry_price"] = args.entry_price
        if args.max_spread:
            params["max_spread_pct"] = args.max_spread

    elif args.strategy == "arb":
        if args.min_edge:
            params["min_edge_bps"] = args.min_edge

    elif args.strategy == "stat-arb":
        if args.min_edge:
            params["min_edge_bps"] = args.min_edge
        if args.types:
            params["enabled_types"] = args.types.split(",")

    elif args.strategy == "sports":
        if hasattr(args, 'min_negative_corr') and args.min_negative_corr is not None:
            params["min_negative_corr"] = args.min_negative_corr
        if hasattr(args, 'max_position') and args.max_position is not None:
            params["max_position_pct"] = args.max_position
        if hasattr(args, 'min_edge_pct') and args.min_edge_pct is not None:
            params["min_edge_pct"] = args.min_edge_pct
        if hasattr(args, 'sport') and args.sport:
            params["sport"] = args.sport

    if args.backtest:
        print(f"\nRunning {args.strategy} backtest...")
        print(f"  Capital: ${args.capital:.2f}")
        print(f"  Days: {args.days}")
        print()

        results = await runner.run_backtest(args.strategy, params)

        if results:
            results.print_report()
        else:
            print("Backtest failed or no results")

    elif args.optimize:
        print(f"\nRunning {args.strategy} optimization...")
        print(f"  Capital: ${args.capital:.2f}")
        print(f"  Days: {args.days}")
        print(f"  Iterations: {args.iterations}")
        print()

        opt_result = await runner.run_optimization(
            args.strategy,
            n_iterations=args.iterations,
        )

        if opt_result:
            print("\n" + "="*60)
            print("OPTIMIZATION RESULTS")
            print("="*60)
            print(f"Best CV Score: {opt_result.best_cv_score:.3f}")
            print(f"Best Holdout Score: {opt_result.best_holdout_score:.3f}")
            print(f"Overfitting Ratio: {opt_result.overfitting_ratio:.2f}")
            print(f"Verdict: {opt_result.verdict}")
            print("\nBest Parameters:")
            for k, v in opt_result.best_params.items():
                print(f"  {k}: {v}")
        else:
            print("Optimization failed")


if __name__ == "__main__":
    main()
