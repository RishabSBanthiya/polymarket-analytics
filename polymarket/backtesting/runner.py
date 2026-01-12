"""
Unified Backtest Runner.

Provides a single interface for running backtests across all strategies.
"""

import logging
from typing import Dict, Any, Optional, List, Type

from .results import BacktestResults

logger = logging.getLogger(__name__)


# Strategy registry mapping strategy names to backtester classes
STRATEGY_REGISTRY: Dict[str, tuple] = {}


def _lazy_load_strategies():
    """Lazily load strategy backtesters to avoid circular imports."""
    global STRATEGY_REGISTRY
    if STRATEGY_REGISTRY:
        return

    try:
        from .strategies.bond_backtest import SimpleBondBacktester
        STRATEGY_REGISTRY["bond"] = SimpleBondBacktester
    except ImportError as e:
        logger.warning(f"Could not load bond backtester: {e}")

    try:
        from .strategies.flow_backtest import SimpleFlowBacktester
        STRATEGY_REGISTRY["flow"] = SimpleFlowBacktester
    except ImportError as e:
        logger.warning(f"Could not load flow backtester: {e}")

    try:
        from .strategies.arb_backtest import ArbBacktester
        STRATEGY_REGISTRY["arb"] = ArbBacktester
    except ImportError as e:
        logger.warning(f"Could not load arb backtester: {e}")

    try:
        from .strategies.stat_arb_backtest import StatArbBacktester
        STRATEGY_REGISTRY["stat-arb"] = StatArbBacktester
    except ImportError as e:
        logger.warning(f"Could not load stat-arb backtester: {e}")

    try:
        from .strategies.sports_portfolio_backtest import SportsPortfolioBacktester
        STRATEGY_REGISTRY["sports"] = SportsPortfolioBacktester
    except ImportError as e:
        logger.warning(f"Could not load sports backtester: {e}")


class BacktestRunner:
    """
    Unified runner for backtesting any strategy.

    Provides consistent interface across all strategy backtests.
    """

    def __init__(
        self,
        initial_capital: float = 1000.0,
        lookback_days: int = 60,
        verbose: bool = False,
    ):
        self.initial_capital = initial_capital
        self.lookback_days = lookback_days
        self.verbose = verbose
        _lazy_load_strategies()

    def list_strategies(self) -> List[str]:
        """Get list of available strategies."""
        return list(STRATEGY_REGISTRY.keys())

    async def run_backtest(
        self,
        strategy: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[BacktestResults]:
        """
        Run backtest for a strategy.

        Args:
            strategy: Strategy name (bond, flow, arb, stat-arb, sports)
            params: Strategy-specific parameters

        Returns:
            BacktestResults if successful, None otherwise
        """
        strategy = strategy.lower().replace("_", "-")

        if strategy not in STRATEGY_REGISTRY:
            logger.error(f"Unknown strategy: {strategy}. Available: {list(STRATEGY_REGISTRY.keys())}")
            return None

        backtester_class = STRATEGY_REGISTRY[strategy]
        params = params or {}

        try:
            # Create backtester with params
            backtester = backtester_class(
                initial_capital=self.initial_capital,
                **params
            )

            # Fetch markets
            from polymarket.core.api import PolymarketAPI
            api = PolymarketAPI()

            try:
                # Get closed markets for backtesting
                raw_markets = await api.fetch_closed_markets(days=self.lookback_days)

                # Parse and filter markets
                markets = []
                for raw in raw_markets:
                    parsed = api.parse_market(raw)
                    if parsed and parsed.resolved:
                        markets.append(parsed)

                if not markets:
                    logger.warning("No resolved markets found for backtest")
                    return None

                logger.info(f"Running {strategy} backtest on {len(markets)} markets...")

                # Fetch price history and set cache
                price_cache = {}
                for market in markets[:100]:  # Limit for performance
                    for token in market.tokens:
                        try:
                            prices = await api.fetch_price_history(token.token_id)
                            if prices:
                                price_cache[token.token_id] = prices
                        except Exception as e:
                            logger.debug(f"Could not fetch prices for {token.token_id}: {e}")

                if hasattr(backtester, 'set_price_cache'):
                    backtester.set_price_cache(price_cache)

                # Run backtest
                results = await backtester.run(markets)
                return results

            finally:
                await api.close()

        except Exception as e:
            logger.error(f"Backtest failed: {e}")
            if self.verbose:
                import traceback
                traceback.print_exc()
            return None

    async def run_optimization(
        self,
        strategy: str,
        n_iterations: int = 50,
    ) -> Optional[Any]:
        """
        Run Bayesian optimization for a strategy.

        Args:
            strategy: Strategy name
            n_iterations: Number of optimization iterations

        Returns:
            OptimizationResult if successful, None otherwise
        """
        strategy = strategy.lower().replace("_", "-")

        if strategy not in STRATEGY_REGISTRY:
            logger.error(f"Unknown strategy: {strategy}")
            return None

        try:
            from .optimization import BayesianOptimizerV3

            # Get parameter space for strategy
            param_space = self._get_param_space(strategy)
            if param_space is None:
                logger.error(f"No parameter space defined for {strategy}")
                return None

            # Create optimizer
            optimizer = BayesianOptimizerV3(
                param_space=param_space,
                n_iterations=n_iterations,
            )

            # Fetch markets
            from polymarket.core.api import PolymarketAPI
            api = PolymarketAPI()

            try:
                raw_markets = await api.fetch_closed_markets(days=self.lookback_days)
                markets = []
                for raw in raw_markets:
                    parsed = api.parse_market(raw)
                    if parsed and parsed.resolved:
                        markets.append(parsed)

                if len(markets) < 20:
                    logger.warning(f"Not enough markets for optimization: {len(markets)}")
                    return None

                logger.info(f"Running {strategy} optimization on {len(markets)} markets...")

                # Create backtester factory
                backtester_class = STRATEGY_REGISTRY[strategy]

                def create_backtester(params):
                    return backtester_class(
                        initial_capital=self.initial_capital,
                        **params
                    )

                # Run optimization
                result = await optimizer.optimize(
                    create_backtester=create_backtester,
                    markets=markets,
                )

                return result

            finally:
                await api.close()

        except Exception as e:
            logger.error(f"Optimization failed: {e}")
            if self.verbose:
                import traceback
                traceback.print_exc()
            return None

    def _get_param_space(self, strategy: str):
        """Get parameter space for a strategy."""
        try:
            from .optimization import SimpleParameterSpace

            spaces = {
                "bond": SimpleParameterSpace(
                    params={
                        "entry_price": (0.92, 0.97, 0.95),
                        "max_spread_pct": (0.01, 0.06, 0.03),
                        "max_position_pct": (0.05, 0.20, 0.10),
                    }
                ),
                "flow": SimpleParameterSpace(
                    params={
                        "min_entry_price": (0.10, 0.90, 0.50),
                        "max_entry_price": (0.50, 0.99, 0.90),
                        "stop_loss_pct": (0.10, 0.30, 0.20),
                    }
                ),
                "arb": SimpleParameterSpace(
                    params={
                        "min_edge_bps": (20, 100, 50),
                        "order_size_pct": (0.05, 0.20, 0.10),
                        "max_positions": (3, 10, 5),
                    }
                ),
                "stat-arb": SimpleParameterSpace(
                    params={
                        "min_edge_bps": (10, 100, 30),
                        "position_size_pct": (0.05, 0.20, 0.10),
                        "min_similarity": (0.70, 0.95, 0.85),
                    }
                ),
                "sports": SimpleParameterSpace(
                    params={
                        "min_negative_corr": (-0.8, -0.3, -0.5),
                        "max_position_pct": (0.05, 0.25, 0.15),
                        "min_edge_pct": (0.01, 0.10, 0.03),
                    }
                ),
            }

            return spaces.get(strategy)

        except ImportError:
            logger.warning("Could not import optimization module")
            return None
