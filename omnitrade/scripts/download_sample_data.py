#!/usr/bin/env python3
"""
Download sample exchange data for backtesting.

Data source: https://github.com/Jon-Becker/prediction-market-analysis

Data layout (36GB compressed, downloaded via `make setup`):
  data/polymarket/{blocks,markets,trades}   - Polymarket parquet files
  data/kalshi/{markets,trades}              - Kalshi parquet files

After downloading, run the backtest with:
    python scripts/backtest.py --data-dir ./data/polymarket --list-markets
    python scripts/backtest.py --data-dir ./data/polymarket --market "presidential"
    python scripts/backtest.py --data-dir ./data/kalshi --exchange kalshi --list-markets
"""

import os
import sys


def check_data(data_dir: str) -> bool:
    """Check if data directory exists and has the expected structure."""
    trades_dir = os.path.join(data_dir, "trades")
    markets_dir = os.path.join(data_dir, "markets")

    if not os.path.isdir(trades_dir):
        return False
    if not os.path.isdir(markets_dir):
        return False

    trade_files = [f for f in os.listdir(trades_dir) if f.endswith(".parquet")]
    market_files = [f for f in os.listdir(markets_dir) if f.endswith(".parquet")]

    return len(trade_files) > 0 and len(market_files) > 0


def main() -> None:
    default_dir = os.path.join(os.path.dirname(__file__), "..", "data", "polymarket")
    data_dir = sys.argv[1] if len(sys.argv) > 1 else default_dir

    if check_data(data_dir):
        abs_path = os.path.abspath(data_dir)
        trade_count = len([f for f in os.listdir(os.path.join(data_dir, "trades")) if f.endswith(".parquet")])
        market_count = len([f for f in os.listdir(os.path.join(data_dir, "markets")) if f.endswith(".parquet")])
        print(f"Data already exists at: {abs_path}")
        print(f"  Trade files: {trade_count}")
        print(f"  Market files: {market_count}")
        blocks_dir = os.path.join(data_dir, "blocks")
        if os.path.isdir(blocks_dir):
            block_count = len([f for f in os.listdir(blocks_dir) if f.endswith(".parquet")])
            print(f"  Block files: {block_count}")
        print()
        print("Ready to backtest:")
        print(f"  python scripts/backtest.py --data-dir {abs_path} --list-markets")
        return

    print("Exchange trade data not found.")
    print()
    print("Dataset is ~36GB compressed. Download via `make setup` or manually:")
    print()
    print("  Data layout:")
    print("    data/polymarket/{blocks,markets,trades}")
    print("    data/kalshi/{markets,trades}")
    print()
    print("  Option 1: make setup (recommended)")
    print("  ------------------------------------")
    print("  make setup")
    print()
    print("  Option 2: Git sparse clone")
    print("  ---------------------------")
    print("  git clone --filter=blob:none --sparse \\")
    print("    https://github.com/Jon-Becker/prediction-market-analysis.git")
    print("  cd prediction-market-analysis")
    print("  git sparse-checkout set data/polymarket data/kalshi")
    print()
    print("  Then symlink or copy:")
    print(f"  ln -s $(pwd)/data/polymarket {os.path.abspath(data_dir)}")
    print()


if __name__ == "__main__":
    main()
