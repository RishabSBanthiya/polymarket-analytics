"""
Portfolio Optimizer for Sports Markets.

Implements mean-variance optimization with correlation constraints
to build hedged portfolios of binary options.

Optimization methods:
- Mean-Variance (Markowitz)
- Risk Parity
- Maximum Sharpe Ratio
"""

import logging
from typing import List, Dict, Optional, Tuple
import numpy as np

from .models import (
    SportsGame,
    GameMarket,
    CorrelationMatrix,
    PortfolioAllocation,
    PortfolioPosition,
)
from .config import PortfolioOptConfig, SportsPortfolioConfig

logger = logging.getLogger(__name__)

# Optional optimization imports
try:
    from scipy.optimize import minimize, LinearConstraint, Bounds
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    logger.warning("scipy not installed. Optimization limited.")

try:
    import cvxpy as cp
    CVXPY_AVAILABLE = True
except ImportError:
    CVXPY_AVAILABLE = False


class PortfolioOptimizer:
    """
    Optimizes portfolio allocation across sports markets.

    Uses correlation matrix to minimize variance while
    achieving target returns through diversification.
    """

    def __init__(
        self,
        config: PortfolioOptConfig,
    ):
        self.config = config

    def optimize(
        self,
        game: SportsGame,
        correlation_matrix: CorrelationMatrix,
        expected_returns: Optional[Dict[str, float]] = None,
        capital: float = 100.0,
    ) -> Optional[PortfolioPosition]:
        """
        Optimize portfolio for a game.

        Args:
            game: Sports game with markets
            correlation_matrix: Predicted correlations
            expected_returns: Expected return per market (default: use prices)
            capital: Total capital to allocate

        Returns:
            Optimized portfolio position or None if infeasible
        """
        markets = game.markets
        n = len(markets)

        logger.info(f"Optimize: {n} markets, min_positions={self.config.min_positions}")

        if n < self.config.min_positions:
            logger.debug(f"Too few markets: {n} < {self.config.min_positions}")
            return None

        # Get expected returns (default: edge from mid price vs fair value)
        if expected_returns is None:
            expected_returns = self._estimate_returns(markets)

        # For hedged portfolios, allow all markets (not just positive edge)
        # The edge comes from variance reduction, not individual returns
        tradeable = [
            m for m in markets
            if m.token_id in expected_returns
        ]
        logger.info(f"Tradeable markets: {len(tradeable)}")

        if len(tradeable) < self.config.min_positions:
            logger.debug(f"Too few tradeable markets: {len(tradeable)}")
            return None

        # Build covariance matrix from correlations
        # Assume variance = price * (1 - price) for binary outcomes
        variances = []
        for m in tradeable:
            p = m.mid_price
            var = p * (1 - p)  # Binary outcome variance
            variances.append(max(0.01, var))  # Floor at 1%

        token_ids = [m.token_id for m in tradeable]
        n_tradeable = len(tradeable)

        # Build covariance matrix
        cov_matrix = np.zeros((n_tradeable, n_tradeable))
        for i in range(n_tradeable):
            for j in range(n_tradeable):
                corr, conf = correlation_matrix.get_correlation(
                    token_ids[i], token_ids[j]
                )
                # Weight by confidence
                effective_corr = corr * conf
                cov_matrix[i, j] = effective_corr * np.sqrt(variances[i] * variances[j])

        # Expected returns vector
        mu = np.array([expected_returns[m.token_id] for m in tradeable])

        # Run optimization
        if self.config.method == "mean_variance":
            weights = self._mean_variance_optimize(mu, cov_matrix)
        elif self.config.method == "risk_parity":
            weights = self._risk_parity_optimize(cov_matrix)
        elif self.config.method == "max_sharpe":
            weights = self._max_sharpe_optimize(mu, cov_matrix)
        else:
            weights = self._mean_variance_optimize(mu, cov_matrix)

        if weights is None:
            logger.info("Optimization returned None weights")
            return None

        logger.info(f"Optimization succeeded, weights: {weights[:3]}...")

        # Check hedging requirement
        if self.config.require_hedge:
            has_hedge = self._has_hedging_pairs(weights, correlation_matrix, token_ids)
            logger.info(f"Has hedging pairs: {has_hedge}")
            if not has_hedge:
                logger.debug("No hedging pairs found")
                return None

        # Build allocations
        allocations = []
        for i, market in enumerate(tradeable):
            weight = weights[i]
            if abs(weight) < self.config.min_weight_per_position:
                continue  # Skip tiny positions

            # Calculate shares and cost
            price = market.ask if weight > 0 else market.bid
            if price is None:
                price = market.mid_price

            alloc_capital = abs(weight) * capital
            shares = alloc_capital / price

            allocations.append(PortfolioAllocation(
                market_id=market.market_id,
                token_id=market.token_id,
                outcome=market.outcome,
                weight=weight,
                shares=shares,
                cost=alloc_capital,
                entry_price=price,
            ))

        if len(allocations) < self.config.min_positions:
            logger.info(f"Too few allocations: {len(allocations)} < {self.config.min_positions}")
            return None

        logger.info(f"Created {len(allocations)} allocations")

        # Create portfolio position
        position = PortfolioPosition.create(
            game_id=game.game_id,
            agent_id="sports-portfolio",
            allocations=allocations,
            correlation_matrix=correlation_matrix,
        )

        # Calculate expected return and Sharpe
        position.expected_return = sum(
            alloc.weight * expected_returns.get(alloc.token_id, 0)
            for alloc in allocations
        )

        if position.expected_variance > 0:
            position.sharpe_ratio = position.expected_return / np.sqrt(position.expected_variance)
        else:
            position.sharpe_ratio = 0.0

        # Check minimum Sharpe
        logger.info(f"Portfolio Sharpe: {position.sharpe_ratio:.4f}, variance: {position.expected_variance:.6f}")
        if position.sharpe_ratio < self.config.min_sharpe_ratio:
            logger.info(f"Sharpe too low: {position.sharpe_ratio:.4f} < {self.config.min_sharpe_ratio}")
            return None

        return position

    def _mean_variance_optimize(
        self,
        mu: np.ndarray,
        cov: np.ndarray,
    ) -> Optional[np.ndarray]:
        """
        Mean-variance optimization.

        Maximize: mu'w - (lambda/2) * w'Cov*w
        Subject to: sum(w) = 1, bounds on weights
        """
        n = len(mu)

        if not SCIPY_AVAILABLE:
            # Simple equal-weight fallback
            return np.ones(n) / n

        def objective(w):
            ret = mu @ w
            var = w @ cov @ w
            return -(ret - self.config.risk_aversion / 2 * var)

        # Constraints: fully invested (sum = 1 for long-only)
        constraints = [
            {'type': 'eq', 'fun': lambda w: np.sum(w) - 1},
        ]

        # Bounds: long-only with reasonable concentration
        # Use lower bound of 0 (positions can be 0 or min_weight+)
        bounds = [(0, self.config.max_weight_per_position)] * n

        # Initial guess: equal-weight
        x0 = np.ones(n) / n

        try:
            result = minimize(
                objective,
                x0,
                method='SLSQP',
                bounds=bounds,
                constraints=constraints,
                options={'maxiter': 1000},
            )
            if result.success:
                return result.x
            else:
                logger.debug(f"Optimization failed: {result.message}")
                return None
        except Exception as e:
            logger.warning(f"Optimization error: {e}")
            return None

    def _risk_parity_optimize(
        self,
        cov: np.ndarray,
    ) -> Optional[np.ndarray]:
        """
        Risk parity optimization.

        Each asset contributes equally to portfolio risk.
        """
        n = cov.shape[0]

        if not SCIPY_AVAILABLE:
            return np.ones(n) / n

        def risk_contribution(w):
            portfolio_var = w @ cov @ w
            if portfolio_var <= 0:
                return np.ones(n) / n
            marginal_risk = cov @ w
            risk_contrib = w * marginal_risk / np.sqrt(portfolio_var)
            return risk_contrib

        def objective(w):
            rc = risk_contribution(w)
            target_rc = 1.0 / n
            return np.sum((rc - target_rc) ** 2)

        # Constraints: weights sum to 1
        constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]

        # Bounds: all positive for risk parity
        bounds = [(0.01, self.config.max_weight_per_position)] * n

        x0 = np.ones(n) / n

        try:
            result = minimize(
                objective,
                x0,
                method='SLSQP',
                bounds=bounds,
                constraints=constraints,
            )
            if result.success:
                return result.x
            return None
        except Exception as e:
            logger.warning(f"Risk parity optimization error: {e}")
            return None

    def _max_sharpe_optimize(
        self,
        mu: np.ndarray,
        cov: np.ndarray,
        rf: float = 0.0,
    ) -> Optional[np.ndarray]:
        """
        Maximum Sharpe ratio optimization.
        """
        n = len(mu)

        if not SCIPY_AVAILABLE:
            return np.ones(n) / n

        def neg_sharpe(w):
            ret = mu @ w
            var = w @ cov @ w
            if var <= 0:
                return 0
            return -(ret - rf) / np.sqrt(var)

        constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]

        # Long-only bounds for stable optimization
        bounds = [(0, self.config.max_weight_per_position)] * n

        x0 = np.ones(n) / n

        try:
            result = minimize(
                neg_sharpe,
                x0,
                method='SLSQP',
                bounds=bounds,
                constraints=constraints,
            )
            if result.success:
                return result.x
            return None
        except Exception as e:
            logger.warning(f"Max Sharpe optimization error: {e}")
            return None

    def _estimate_returns(self, markets: List[GameMarket]) -> Dict[str, float]:
        """
        Estimate expected returns from market prices.

        For binary options in hedged portfolios, the edge comes primarily from:
        1. Variance reduction through correlation-based hedging
        2. Spread capture from trading
        3. Mean-reversion of prices

        We use a baseline edge that reflects realistic trading edge.
        """
        returns = {}

        for market in markets:
            if market.bid and market.ask:
                spread = market.ask - market.bid
                mid = (market.bid + market.ask) / 2

                # Base edge: half the spread (market making edge)
                # Plus additional edge from price deviation from 50%
                base_edge = spread / 2  # Spread capture

                # Favorite/underdog adjustment
                if mid > 0.5:
                    # Favorites slightly overpriced: small negative adjustment
                    deviation_edge = -0.005 * (mid - 0.5) * 2
                else:
                    # Underdogs slightly underpriced: small positive adjustment
                    deviation_edge = 0.005 * (0.5 - mid) * 2

                # Total edge: base + deviation, minimum 0.5%
                edge = max(0.005, base_edge + deviation_edge)
                returns[market.token_id] = edge
            else:
                # No orderbook data: use conservative baseline
                returns[market.token_id] = 0.005

        return returns

    def _has_hedging_pairs(
        self,
        weights: np.ndarray,
        correlation_matrix: CorrelationMatrix,
        token_ids: List[str],
    ) -> bool:
        """Check if portfolio has sufficient hedging."""
        # Find pairs with significant weight and negative correlation
        n = len(weights)

        for i in range(n):
            if abs(weights[i]) < self.config.min_weight_per_position:
                continue

            for j in range(i + 1, n):
                if abs(weights[j]) < self.config.min_weight_per_position:
                    continue

                corr, conf = correlation_matrix.get_correlation(
                    token_ids[i], token_ids[j]
                )

                # Check for hedging pair
                if corr <= self.config.min_negative_correlation and conf > 0.5:
                    return True

        return False

    def rebalance(
        self,
        position: PortfolioPosition,
        game: SportsGame,
        correlation_matrix: CorrelationMatrix,
        current_prices: Dict[str, float],
    ) -> Optional[List[Tuple[str, float]]]:
        """
        Calculate rebalancing trades for existing position.

        Returns: List of (token_id, shares_delta) tuples
        """
        # Update current values
        position.update_prices(current_prices)

        # Get target allocation
        target_position = self.optimize(
            game,
            correlation_matrix,
            capital=position.total_cost + position.unrealized_pnl,
        )

        if target_position is None:
            return None

        # Calculate deltas
        current_shares = {a.token_id: a.shares for a in position.allocations}
        target_shares = {a.token_id: a.shares for a in target_position.allocations}

        trades = []
        all_tokens = set(current_shares.keys()) | set(target_shares.keys())

        for token_id in all_tokens:
            current = current_shares.get(token_id, 0)
            target = target_shares.get(token_id, 0)
            delta = target - current

            if abs(delta) > 0.1:  # Minimum trade size
                trades.append((token_id, delta))

        return trades if trades else None


class HedgeCalculator:
    """
    Calculate optimal hedge ratios for positions.

    Used for dynamic hedging during live games.
    """

    @staticmethod
    def calculate_hedge_ratio(
        position_token_id: str,
        hedge_token_id: str,
        correlation: float,
        position_variance: float,
        hedge_variance: float,
    ) -> float:
        """
        Calculate optimal hedge ratio using minimum variance hedge.

        h* = -corr * sqrt(var_position / var_hedge)
        """
        if hedge_variance <= 0:
            return 0.0

        h = -correlation * np.sqrt(position_variance / hedge_variance)
        return float(h)

    @staticmethod
    def calculate_hedged_variance(
        position_variance: float,
        hedge_variance: float,
        correlation: float,
        hedge_ratio: float,
    ) -> float:
        """Calculate variance of hedged position."""
        return (
            position_variance
            + hedge_ratio**2 * hedge_variance
            + 2 * hedge_ratio * correlation * np.sqrt(position_variance * hedge_variance)
        )

    @staticmethod
    def variance_reduction(
        original_variance: float,
        hedged_variance: float,
    ) -> float:
        """Calculate percentage variance reduction from hedge."""
        if original_variance <= 0:
            return 0.0
        return (original_variance - hedged_variance) / original_variance
