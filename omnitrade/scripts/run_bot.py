#!/usr/bin/env python3
"""
OmniTrade unified bot CLI.

Usage:
    python scripts/run_bot.py directional --exchange polymarket --paper
    python scripts/run_bot.py mm --exchange kalshi --live
    python scripts/run_bot.py directional --exchange hyperliquid --paper --interval 15
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnitrade.core.config import Config, get_config, set_config
from omnitrade.core.enums import ExchangeId, Environment
from omnitrade.core.models import Instrument
from omnitrade.exchanges.registry import create_client
from omnitrade.storage.sqlite import SQLiteStorage
from omnitrade.risk.coordinator import RiskCoordinator
from omnitrade.utils.logging import setup_logging


class MarketFilteredClient:
    """Wraps an ExchangeClient to fetch only specific instruments.

    For each filter term, makes a targeted API call using the exchange's
    server-side filtering (event_ticker for Kalshi, slug_contains for
    Polymarket) rather than fetching all instruments and filtering locally.
    """

    def __init__(self, client, filters: list[str]):
        self._client = client
        self._filters = filters
        self._exchange_id = client.exchange_id
        self._cache: list[Instrument] | None = None
        self._cache_time: float = 0
        self._logger = logging.getLogger("omnitrade.filter")

    async def get_instruments(self, active_only: bool = True, **kwargs) -> list[Instrument]:
        # Re-fetch every 5 minutes (10 cycles at 30s), cache between
        import time
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_time) < 300:
            return self._cache

        all_matched: list[Instrument] = []
        seen_ids: set[str] = set()

        for term in self._filters:
            instruments = await self._fetch_for_term(term, active_only, **kwargs)
            for inst in instruments:
                if inst.instrument_id not in seen_ids:
                    all_matched.append(inst)
                    seen_ids.add(inst.instrument_id)

        self._logger.info(
            "Market filter %s: found %d instruments",
            self._filters, len(all_matched),
        )
        for m in all_matched[:10]:
            self._logger.info("  -> %s: %s (mid=%.4f)", m.instrument_id, m.name[:50], m.price)
        if len(all_matched) > 10:
            self._logger.info("  ... and %d more", len(all_matched) - 10)

        self._cache = all_matched
        self._cache_time = now
        return all_matched

    async def _fetch_for_term(self, term: str, active_only: bool, **kwargs) -> list[Instrument]:
        """Fetch instruments for a single filter term using server-side filtering."""
        from omnitrade.core.enums import ExchangeId

        if self._exchange_id == ExchangeId.KALSHI:
            # Kalshi: use series_ticker for broad category (e.g. KXBTC),
            # falls back to event_ticker for specific events (e.g. KXBTC-25NOV1800)
            instruments = await self._client.get_instruments(
                active_only=active_only, series_ticker=term, limit=200, **kwargs,
            )
            if not instruments:
                instruments = await self._client.get_instruments(
                    active_only=active_only, event_ticker=term, limit=200, **kwargs,
                )
            if not instruments:
                instruments = await self._client.get_instruments(
                    active_only=active_only, ticker=term, limit=200, **kwargs,
                )
        elif self._exchange_id == ExchangeId.POLYMARKET:
            # Polymarket Gamma API supports slug/tag search
            instruments = await self._client.get_instruments(
                active_only=active_only, slug=term, limit=200, **kwargs,
            )
            if not instruments:
                instruments = await self._client.get_instruments(
                    active_only=active_only, tag=term, limit=200, **kwargs,
                )
        else:
            # Fallback: fetch all and filter locally
            all_inst = await self._client.get_instruments(active_only=active_only, limit=500, **kwargs)
            term_lower = term.lower()
            instruments = [
                i for i in all_inst
                if term_lower in f"{i.instrument_id} {i.name} {i.market_id}".lower()
            ]

        return instruments

    def __getattr__(self, name):
        return getattr(self._client, name)


def parse_args():
    parser = argparse.ArgumentParser(description="OmniTrade Bot Runner")
    parser.add_argument(
        "bot_type",
        choices=["directional", "mm", "market-making", "hedge", "cross-arb"],
        help="Bot type to run",
    )
    parser.add_argument(
        "--exchange", "-e",
        choices=["polymarket", "kalshi", "hyperliquid"],
        default=None,
        help="Exchange to trade on (required for single-exchange bots)",
    )
    parser.add_argument(
        "--hedge-exchange",
        choices=["hyperliquid"],
        default="hyperliquid",
        help="Exchange for hedge leg (default: hyperliquid)",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--paper", "--dry-run", action="store_true", default=True,
        help="Paper trading mode (default)",
    )
    mode_group.add_argument(
        "--live", action="store_true",
        help="Live trading mode (real money!)",
    )
    parser.add_argument(
        "--interval", "-i", type=float, default=30.0,
        help="Trading loop interval in seconds",
    )
    parser.add_argument(
        "--agent-id", type=str, default=None,
        help="Bot agent ID (auto-generated if not set)",
    )
    parser.add_argument(
        "--signal", "-s",
        choices=["midpoint", "orderbook", "longshot-bias"],
        default="midpoint",
        help="Signal source for directional bot (default: midpoint)",
    )
    parser.add_argument(
        "--market", "-m", type=str, action="append", default=[],
        help="Filter to specific market(s) by keyword or ID (can repeat: -m bitcoin -m ethereum)",
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


async def run_directional(exchange_id, config, agent_id, interval, environment, signal_type="midpoint", market_filters=None):
    """Run a directional bot."""
    from omnitrade.components.signals import MidpointDeviationSignal, FavoriteLongshotSignal, OrderbookMicrostructureSignal
    from omnitrade.components.trading import SignalScaledSizer, FixedSizer
    from omnitrade.exchanges.base import PaperClient
    from omnitrade.bots.directional import DirectionalBot

    storage = SQLiteStorage(config.db_path)
    storage.initialize()

    risk_config = config.risk
    client = create_client(exchange_id, config)
    await client.connect()

    if market_filters:
        client = MarketFilteredClient(client, market_filters)

    # Paper mode: wrap client to simulate fills
    if environment == Environment.PAPER:
        client = PaperClient(client)

    # Sync balance into risk storage so atomic_reserve can check it
    balance = await client.get_balance()
    max_per_instrument = balance.total_equity * risk_config.max_per_market_exposure_pct

    # For small accounts, lower min trade and use fixed sizing
    if risk_config.min_trade_value_usd > max_per_instrument and max_per_instrument > 0:
        risk_config.min_trade_value_usd = max(1.0, max_per_instrument * 0.5)

    logger.info(
        "Account: $%.2f balance, max $%.2f/instrument, min trade $%.2f",
        balance.total_equity, max_per_instrument, risk_config.min_trade_value_usd,
    )

    risk = RiskCoordinator(storage, risk_config)
    risk.register_account(exchange_id, agent_id)
    storage.update_balance(exchange_id.value, agent_id, balance.total_equity)

    if signal_type == "orderbook":
        signal_source = OrderbookMicrostructureSignal()
    elif signal_type == "longshot-bias":
        signal_source = FavoriteLongshotSignal()
    else:
        signal_source = MidpointDeviationSignal()

    # Size trades to fit within per-instrument risk limits
    trade_size = max_per_instrument * 0.9  # 90% of per-instrument limit
    if signal_type == "longshot-bias":
        sizer = FixedSizer(max(risk_config.min_trade_value_usd, trade_size))
    elif balance.total_equity < 500:
        # Small account: SignalScaledSizer produces tiny sizes, use fixed instead
        sizer = FixedSizer(max(risk_config.min_trade_value_usd, trade_size))
        logger.info("Small account: using FixedSizer at $%.2f per trade", trade_size)
    else:
        sizer = SignalScaledSizer()

    # Widen price filter for longshot-bias (it trades extremes by design)
    price_bounds = {}
    if signal_type == "longshot-bias":
        price_bounds = {"min_price": 0.01, "max_price": 0.99}

    bot = DirectionalBot(
        agent_id=agent_id,
        client=client,
        signal_source=signal_source,
        sizer=sizer,
        risk=risk,
        **price_bounds,
    )

    try:
        await bot.run(interval_seconds=interval)
    except KeyboardInterrupt:
        await bot.stop()
    finally:
        storage.close()


async def run_market_making(exchange_id, config, agent_id, interval, environment, market_filters=None):
    """Run a market making bot."""
    from omnitrade.bots.market_making import MarketMakingBot, AdaptiveQuoter, ActiveMarketSelector
    from omnitrade.exchanges.base import PaperClient

    storage = SQLiteStorage(config.db_path)
    storage.initialize()

    risk = RiskCoordinator(storage, config.risk)
    client = create_client(exchange_id, config)

    if market_filters:
        client = MarketFilteredClient(client, market_filters)

    if environment == Environment.PAPER:
        client = PaperClient(client)

    bot = MarketMakingBot(
        agent_id=agent_id,
        client=client,
        quote_engine=AdaptiveQuoter(),
        market_selector=ActiveMarketSelector(),
        risk=risk,
        environment=environment,
    )

    try:
        await bot.run(interval_seconds=interval)
    except KeyboardInterrupt:
        await bot.stop()
    finally:
        storage.close()


async def run_hedge(binary_exchange_id, hedge_exchange_id, config, agent_id, interval, environment):
    """Run a cross-exchange hedge bot (binary + perp)."""
    from omnitrade.components.signals import BinaryPerpHedgeSignal
    from omnitrade.exchanges.base import PaperClient
    from omnitrade.bots.cross_exchange import CrossExchangeBot

    storage = SQLiteStorage(config.db_path)
    storage.initialize()

    risk = RiskCoordinator(storage, config.risk)

    # Create clients for both exchanges
    clients = {
        binary_exchange_id: create_client(binary_exchange_id, config),
        hedge_exchange_id: create_client(hedge_exchange_id, config),
    }

    # Paper mode: wrap all clients
    if environment == Environment.PAPER:
        clients = {ex: PaperClient(c) for ex, c in clients.items()}

    signal_source = BinaryPerpHedgeSignal(
        binary_exchange=binary_exchange_id,
        hedge_exchange=hedge_exchange_id,
    )

    bot = CrossExchangeBot(
        agent_id=agent_id,
        clients=clients,
        signal_source=signal_source,
        risk=risk,
    )

    try:
        await bot.run(interval_seconds=interval)
    except KeyboardInterrupt:
        await bot.stop()
    finally:
        storage.close()


async def run_cross_arb(config, agent_id, interval, environment):
    """Run cross-exchange arb bot (Polymarket vs Kalshi)."""
    from omnitrade.components.signals import CrossExchangeArbSignal
    from omnitrade.exchanges.base import PaperClient
    from omnitrade.bots.cross_exchange import CrossExchangeBot

    storage = SQLiteStorage(config.db_path)
    storage.initialize()

    risk = RiskCoordinator(storage, config.risk)

    clients = {
        ExchangeId.POLYMARKET: create_client(ExchangeId.POLYMARKET, config),
        ExchangeId.KALSHI: create_client(ExchangeId.KALSHI, config),
    }

    if environment == Environment.PAPER:
        clients = {ex: PaperClient(c) for ex, c in clients.items()}

    bot = CrossExchangeBot(
        agent_id=agent_id,
        clients=clients,
        signal_source=CrossExchangeArbSignal(),
        risk=risk,
    )

    try:
        await bot.run(interval_seconds=interval)
    except KeyboardInterrupt:
        await bot.stop()
    finally:
        storage.close()


def main():
    args = parse_args()
    setup_logging(args.log_level)

    environment = Environment.LIVE if args.live else Environment.PAPER

    config = Config.from_env()
    config.environment = environment
    set_config(config)

    mode = "LIVE" if environment == Environment.LIVE else "PAPER"

    if environment == Environment.LIVE:
        print("WARNING: LIVE TRADING MODE - Real money at risk!")
        confirm = input("Type 'yes' to confirm: ")
        if confirm.lower() != "yes":
            print("Aborted.")
            return

    # Cross-exchange bots don't need --exchange
    if args.bot_type == "hedge":
        exchange = args.exchange or "polymarket"
        exchange_id = ExchangeId(exchange)
        hedge_id = ExchangeId(args.hedge_exchange)
        agent_id = args.agent_id or f"hedge-{exchange}-{args.hedge_exchange}"

        print(f"OmniTrade hedge bot | {exchange} + {args.hedge_exchange} | {mode} mode")
        print(f"Agent: {agent_id} | Interval: {args.interval}s")
        print("-" * 50)

        asyncio.run(run_hedge(exchange_id, hedge_id, config, agent_id, args.interval, environment))

    elif args.bot_type == "cross-arb":
        agent_id = args.agent_id or "cross-arb-poly-kalshi"

        print(f"OmniTrade cross-arb bot | polymarket + kalshi | {mode} mode")
        print(f"Agent: {agent_id} | Interval: {args.interval}s")
        print("-" * 50)

        asyncio.run(run_cross_arb(config, agent_id, args.interval, environment))

    else:
        # Single-exchange bots require --exchange
        if not args.exchange:
            print("Error: --exchange is required for directional and mm bots")
            sys.exit(1)

        exchange_id = ExchangeId(args.exchange)
        agent_id = args.agent_id or f"{args.bot_type}-{args.exchange}"

        print(f"OmniTrade {args.bot_type} bot | {args.exchange} | {mode} mode")
        print(f"Agent: {agent_id} | Interval: {args.interval}s")
        print("-" * 50)

        market_filters = args.market if args.market else None
        if market_filters:
            print(f"Market filter: {', '.join(market_filters)}")

        if args.bot_type == "directional":
            asyncio.run(run_directional(exchange_id, config, agent_id, args.interval, environment, args.signal, market_filters))
        else:
            asyncio.run(run_market_making(exchange_id, config, agent_id, args.interval, environment, market_filters))


if __name__ == "__main__":
    main()
