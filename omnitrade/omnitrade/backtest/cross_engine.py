"""
Cross-exchange backtest engine.

Runs CrossExchangeBot strategies (hedge + arb) against real market data
from multiple exchanges.
"""

import logging
import tempfile
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..core.enums import ExchangeId, InstrumentType, Environment
from ..core.models import Instrument, OrderbookSnapshot
from ..core.config import RiskConfig
from ..components.trading import ExitConfig
from ..exchanges.base import PaperClient
from ..risk.coordinator import RiskCoordinator
from ..storage.sqlite import SQLiteStorage
from ..bots.cross_exchange import CrossExchangeBot
from ..components.signals import CrossExchangeSignalSource

from .engine import (
    BacktestExchangeClient,
    BacktestResult,
    compute_sharpe,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Multi-instrument backtest client
# ---------------------------------------------------------------------------

class MultiInstrumentBacktestClient(BacktestExchangeClient):
    """
    BacktestExchangeClient supporting multiple instruments.

    Each instrument has its own snapshot timeline. Used by cross-exchange
    runners where a single exchange may trade several instruments (e.g.,
    Hyperliquid with multiple perps, or a Polymarket + Kalshi scanner).
    """

    def __init__(
        self,
        instruments: list[Instrument],
        snapshots_by_id: dict[str, list[OrderbookSnapshot]],
        initial_balance: float = 10_000,
        exchange_id: ExchangeId = ExchangeId.POLYMARKET,
    ):
        # Pick first instrument + its snapshots for the base class
        first_id = instruments[0].instrument_id
        first_snaps = snapshots_by_id[first_id]
        super().__init__(first_snaps, instruments[0], initial_balance, exchange_id)

        self._instruments = list(instruments)
        self._snapshots_by_id = dict(snapshots_by_id)
        self._steps_by_id: dict[str, int] = {iid: 0 for iid in snapshots_by_id}
        self._exchange_id = exchange_id

    @property
    def exchange_id(self) -> ExchangeId:
        return self._exchange_id

    async def get_instruments(self, active_only: bool = True, **filters) -> list[Instrument]:
        # Update prices from current snapshots
        for inst in self._instruments:
            snaps = self._snapshots_by_id.get(inst.instrument_id)
            if snaps:
                step = self._steps_by_id.get(inst.instrument_id, 0)
                snap = snaps[min(step, len(snaps) - 1)]
                mid = snap.midpoint
                if mid is not None:
                    inst.price = mid
                    inst.bid = snap.best_bid or mid
                    inst.ask = snap.best_ask or mid
        return list(self._instruments)

    async def get_instrument(self, instrument_id: str) -> Optional[Instrument]:
        for inst in self._instruments:
            if inst.instrument_id == instrument_id:
                snaps = self._snapshots_by_id.get(instrument_id)
                if snaps:
                    step = self._steps_by_id.get(instrument_id, 0)
                    snap = snaps[min(step, len(snaps) - 1)]
                    mid = snap.midpoint
                    if mid is not None:
                        inst.price = mid
                        inst.bid = snap.best_bid or mid
                        inst.ask = snap.best_ask or mid
                return inst
        return None

    async def get_orderbook(self, instrument_id: str, depth: int = 10) -> OrderbookSnapshot:
        snaps = self._snapshots_by_id.get(instrument_id)
        if snaps:
            step = self._steps_by_id.get(instrument_id, 0)
            return snaps[min(step, len(snaps) - 1)]
        return self.current_snapshot

    def advance(self) -> None:
        """Advance all per-instrument step counters."""
        super().advance()
        for iid in self._steps_by_id:
            max_step = len(self._snapshots_by_id[iid]) - 1
            self._steps_by_id[iid] = min(self._steps_by_id[iid] + 1, max_step)


# ---------------------------------------------------------------------------
# Cross-exchange result
# ---------------------------------------------------------------------------

@dataclass
class CrossExchangeBacktestResult(BacktestResult):
    """BacktestResult extended with cross-exchange strategy metrics."""
    strategies_opened: int = 0
    strategies_closed: int = 0
    rollbacks: int = 0
    avg_edge_bps: float = 0.0
    per_leg_pnl: dict = field(default_factory=dict)  # exchange_name -> pnl


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class CrossExchangeBacktestRunner:
    """
    Runs CrossExchangeBot against real market data from multiple exchanges.

    Accepts pre-built exchange data: a dict mapping ExchangeId to
    (instruments, snapshots) tuples.
    """

    def __init__(
        self,
        signal_source: CrossExchangeSignalSource,
        exchange_data: dict[ExchangeId, tuple[list[Instrument], list[OrderbookSnapshot]]],
        initial_balance: float = 10_000,
        strategy_type: str = "hedge",
        scenario_name: str = "real_data",
    ):
        self.signal_source = signal_source
        self.exchange_data = exchange_data
        self.initial_balance = initial_balance
        self.strategy_type = strategy_type
        self._scenario_name = scenario_name

    async def run(self) -> CrossExchangeBacktestResult:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = tmp.name
        tmp.close()

        try:
            return await self._execute(db_path, self.exchange_data)
        finally:
            try:
                os.unlink(db_path)
            except OSError:
                pass

    async def _execute(
        self,
        db_path: str,
        exchange_data: dict[ExchangeId, tuple[list[Instrument], list[OrderbookSnapshot]]],
    ) -> CrossExchangeBacktestResult:
        storage = SQLiteStorage(db_path)
        storage.initialize()

        # Create per-exchange clients
        clients: dict[ExchangeId, MultiInstrumentBacktestClient] = {}
        num_steps = 0
        for exchange_id, (instruments, snapshots) in exchange_data.items():
            snaps_by_id = {}
            for inst in instruments:
                # Filter snapshots for this instrument
                inst_snaps = [s for s in snapshots if s.instrument_id == inst.instrument_id]
                if not inst_snaps:
                    inst_snaps = snapshots  # fallback
                snaps_by_id[inst.instrument_id] = inst_snaps
                num_steps = max(num_steps, len(inst_snaps))

            client = MultiInstrumentBacktestClient(
                instruments=instruments,
                snapshots_by_id=snaps_by_id,
                initial_balance=self.initial_balance,
                exchange_id=exchange_id,
            )
            clients[exchange_id] = client

            # Seed balance per exchange
            storage.update_balance(exchange_id.value, "", self.initial_balance)

        # Risk coordinator
        risk_config = RiskConfig(
            max_wallet_exposure_pct=0.80,
            max_per_agent_exposure_pct=0.60,
            max_per_market_exposure_pct=0.60,
            min_trade_value_usd=1.0,
            max_trade_value_usd=5000.0,
            max_daily_drawdown_pct=0.30,
            max_total_drawdown_pct=0.50,
        )

        risk = RiskCoordinator(storage, risk_config)
        for exchange_id in clients:
            risk.register_account(exchange_id, "")

        exit_config = ExitConfig(
            take_profit_pct=0.10,
            stop_loss_pct=0.30,
            max_hold_minutes=240,
            trailing_stop_activation_pct=0.05,
            trailing_stop_distance_pct=0.02,
        )

        agent_id = f"cross-bt-{self.strategy_type}-{self._scenario_name}"

        # Wrap clients with PaperClient for simulated fills
        paper_clients = {ex: PaperClient(c, slippage_pct=0.001) for ex, c in clients.items()}

        bot = CrossExchangeBot(
            agent_id=agent_id,
            clients=paper_clients,
            signal_source=self.signal_source,
            risk=risk,
            exit_config=exit_config,
            base_size_usd=50.0,
            max_strategies=5,
        )

        await bot.start()
        equity_curve = [self.initial_balance]

        for step in range(num_steps - 1):
            try:
                await bot._iteration()
            except Exception as e:
                logger.debug("Cross iteration %d error: %s", step, e)

            # Advance all raw clients (not the PaperClient wrappers)
            for client in clients.values():
                client.advance()

            # Mark-to-market from all sub-agents
            total_equity = 0.0
            for exchange_id, client in clients.items():
                sub_agent = f"{agent_id}-{exchange_id.value}"
                open_pos = storage.get_agent_positions(sub_agent, "open")
                closed_pos = storage.get_agent_positions(sub_agent, "closed")
                closed_pnl = sum(p.get("pnl", 0) or 0 for p in closed_pos)

                unrealized = 0.0
                for p in open_pos:
                    inst_id = p["instrument_id"]
                    mid_snap = await client.get_orderbook(inst_id, depth=1)
                    mid = mid_snap.midpoint or p["entry_price"]
                    if p["side"] == "BUY":
                        unrealized += (mid - p["entry_price"]) * p["size"]
                    else:
                        unrealized += (p["entry_price"] - mid) * p["size"]

                ex_equity = self.initial_balance + closed_pnl + unrealized
                client._equity = ex_equity
                storage.update_balance(exchange_id.value, "", ex_equity)
                total_equity += ex_equity

            equity_curve.append(total_equity / len(clients) if clients else self.initial_balance)

        await bot.stop()

        # Compute data duration for Sharpe — use the longest snapshot timeline
        total_duration_secs = 0.0
        for client in clients.values():
            snaps = client._snapshots
            if len(snaps) >= 2:
                dur = (snaps[-1].timestamp - snaps[0].timestamp).total_seconds()
                total_duration_secs = max(total_duration_secs, dur)

        result = self._compute_cross_metrics(
            storage, agent_id, equity_curve, clients, bot, total_duration_secs,
        )
        storage.close()
        return result

    def _compute_cross_metrics(
        self,
        storage: SQLiteStorage,
        agent_id: str,
        equity_curve: list[float],
        clients: dict[ExchangeId, MultiInstrumentBacktestClient],
        bot: CrossExchangeBot,
        total_duration_secs: float = 0.0,
    ) -> CrossExchangeBacktestResult:

        # Gather positions across all sub-agents
        all_closed = []
        per_leg_pnl: dict[str, float] = {}

        for exchange_id in clients:
            sub_agent = f"{agent_id}-{exchange_id.value}"
            closed = storage.get_agent_positions(sub_agent, "closed")
            all_closed.extend(closed)
            leg_pnl = sum(p.get("pnl", 0) or 0 for p in closed)
            per_leg_pnl[exchange_id.value] = leg_pnl

        total_trades = len(all_closed)
        winning = sum(1 for p in all_closed if (p.get("pnl") or 0) > 0)
        losing = sum(1 for p in all_closed if (p.get("pnl") or 0) < 0)
        total_pnl = sum(p.get("pnl", 0) or 0 for p in all_closed)

        win_rate = winning / total_trades if total_trades > 0 else 0.0
        avg_trade_pnl = total_pnl / total_trades if total_trades > 0 else 0.0

        # Max drawdown
        peak = equity_curve[0]
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        sharpe = compute_sharpe(equity_curve, total_duration_secs)

        final_equity = equity_curve[-1] if equity_curve else self.initial_balance

        # Strategy-level metrics
        strategies_opened = len(bot._active_strategies)
        # Closed strategies = total unique strategy keys seen minus currently active
        # Approximate from closed positions: each strategy has 2 legs
        strategies_closed = total_trades // 2 if total_trades >= 2 else 0

        return CrossExchangeBacktestResult(
            signal_name=self.signal_source.name,
            scenario_name=self._scenario_name,
            total_pnl=final_equity - self.initial_balance,
            total_trades=total_trades,
            winning_trades=winning,
            losing_trades=losing,
            win_rate=win_rate,
            max_drawdown_pct=max_dd,
            sharpe_ratio=sharpe,
            avg_trade_pnl=avg_trade_pnl,
            equity_curve=equity_curve,
            final_equity=final_equity,
            # Cross-exchange specific
            strategies_opened=strategies_opened,
            strategies_closed=strategies_closed,
            rollbacks=0,
            avg_edge_bps=0.0,
            per_leg_pnl=per_leg_pnl,
        )
