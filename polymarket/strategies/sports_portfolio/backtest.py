"""
Sports Portfolio Backtester.

Backtests the sports portfolio strategy using historical data
with parameter optimization per sport.
"""

import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
import json
import sqlite3
import numpy as np

from .models import Sport, MarketType, CorrelationMatrix, PortfolioPosition, PortfolioAllocation
from .config import SportsPortfolioConfig, PortfolioOptConfig, RiskConfig
from .trainer import SportSpecificTrainer

logger = logging.getLogger(__name__)

# Optional imports
try:
    from scipy.optimize import minimize
    from sklearn.model_selection import ParameterGrid
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


@dataclass
class BacktestTrade:
    """A simulated trade in backtest."""
    game_key: str
    sport: str
    token_id: str
    side: str               # "BUY" or "SELL"
    shares: float
    entry_price: float
    exit_price: float       # Resolution price (0 or 1)
    pnl: float
    timestamp: datetime


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    sport: str
    params: Dict

    # Performance metrics
    total_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0

    # Risk metrics
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0

    # Portfolio metrics
    avg_positions_per_portfolio: float = 0.0
    avg_hedging_effectiveness: float = 0.0
    avg_pairwise_correlation: float = 0.0

    # Trade details
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    @property
    def profit_factor(self) -> float:
        """Gross profit / gross loss."""
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float('inf')

    @property
    def avg_trade_pnl(self) -> float:
        return self.total_pnl / self.total_trades if self.total_trades > 0 else 0


class SportsPortfolioBacktester:
    """
    Backtester for sports portfolio strategy.

    Simulates portfolio construction and resolution
    using historical market data.
    """

    def __init__(
        self,
        db_path: Path = None,
        trainer: SportSpecificTrainer = None,
    ):
        self.db_path = db_path or Path("data/sports_training_data.db")
        self.trainer = trainer

    def backtest(
        self,
        sport: str,
        config: SportsPortfolioConfig,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> BacktestResult:
        """
        Run backtest for a specific sport.

        Args:
            sport: Sport to backtest
            config: Strategy configuration
            start_date: Start of backtest period
            end_date: End of backtest period

        Returns:
            BacktestResult with performance metrics
        """
        # Load historical games
        games = self._load_games(sport, start_date, end_date)

        if not games:
            logger.warning(f"No games found for {sport}")
            return BacktestResult(sport=sport, params=self._config_to_params(config))

        logger.info(f"Backtesting {sport} with {len(games)} games")

        # Initialize result
        result = BacktestResult(
            sport=sport,
            params=self._config_to_params(config),
        )

        equity = 10000.0  # Starting capital
        equity_curve = [equity]
        peak_equity = equity
        max_drawdown = 0.0
        returns = []

        for game in games:
            # Build correlation matrix for this game
            corr_matrix = self._build_correlation_matrix(game, sport)

            if corr_matrix is None:
                continue

            # Check for negative correlation opportunities
            neg_pairs = self._find_negative_pairs(
                corr_matrix,
                threshold=config.min_negative_correlation,
            )

            if len(neg_pairs) < 1:
                continue

            # Simulate portfolio construction
            portfolio = self._construct_portfolio(
                game,
                corr_matrix,
                config,
            )

            if portfolio is None:
                continue

            # Simulate resolution
            trades, portfolio_pnl = self._simulate_resolution(
                portfolio,
                game,
            )

            # Update metrics
            result.trades.extend(trades)
            result.total_pnl += portfolio_pnl
            result.total_trades += len(trades)

            for trade in trades:
                if trade.pnl > 0:
                    result.winning_trades += 1
                elif trade.pnl < 0:
                    result.losing_trades += 1

            # Track portfolio metrics
            if portfolio.num_positions > 0:
                result.avg_positions_per_portfolio += portfolio.num_positions
                result.avg_hedging_effectiveness += portfolio.hedging_effectiveness
                result.avg_pairwise_correlation += portfolio.avg_pairwise_correlation

            # Update equity
            equity += portfolio_pnl
            equity_curve.append(equity)
            returns.append(portfolio_pnl / (equity - portfolio_pnl) if equity > portfolio_pnl else 0)

            # Track drawdown
            peak_equity = max(peak_equity, equity)
            drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            max_drawdown = max(max_drawdown, drawdown)

        # Finalize metrics
        result.equity_curve = equity_curve
        result.max_drawdown = max_drawdown

        if result.total_trades > 0:
            result.win_rate = result.winning_trades / result.total_trades

        num_portfolios = len([t for t in result.trades]) // max(1, int(result.avg_positions_per_portfolio) or 3)
        if num_portfolios > 0:
            result.avg_positions_per_portfolio /= num_portfolios
            result.avg_hedging_effectiveness /= num_portfolios
            result.avg_pairwise_correlation /= num_portfolios

        # Calculate Sharpe and Sortino
        if returns:
            returns_array = np.array(returns)
            mean_return = np.mean(returns_array)
            std_return = np.std(returns_array)

            if std_return > 0:
                result.sharpe_ratio = mean_return / std_return * np.sqrt(252)  # Annualized

            downside_returns = returns_array[returns_array < 0]
            if len(downside_returns) > 0:
                downside_std = np.std(downside_returns)
                if downside_std > 0:
                    result.sortino_ratio = mean_return / downside_std * np.sqrt(252)

        logger.info(
            f"{sport} backtest: PnL=${result.total_pnl:.2f}, "
            f"Trades={result.total_trades}, WinRate={result.win_rate:.1%}, "
            f"Sharpe={result.sharpe_ratio:.2f}"
        )

        return result

    def optimize_parameters(
        self,
        sport: str,
        param_grid: Dict[str, List] = None,
        metric: str = "sharpe_ratio",
    ) -> Tuple[Dict, BacktestResult]:
        """
        Optimize strategy parameters for a sport.

        Args:
            sport: Sport to optimize
            param_grid: Parameter grid to search
            metric: Metric to optimize ("sharpe_ratio", "total_pnl", "win_rate")

        Returns:
            (best_params, best_result)
        """
        if param_grid is None:
            param_grid = self._default_param_grid()

        logger.info(f"Optimizing {sport} parameters")

        best_result = None
        best_params = None
        best_metric = float('-inf')

        all_results = []

        # Grid search
        for params in ParameterGrid(param_grid):
            config = self._params_to_config(params)
            result = self.backtest(sport, config)
            result.params = params
            all_results.append(result)

            # Get metric value
            metric_value = getattr(result, metric, 0)

            if metric_value > best_metric:
                best_metric = metric_value
                best_result = result
                best_params = params

        if best_params:
            logger.info(f"Best params for {sport}: {best_params}")
            logger.info(f"Best {metric}: {best_metric:.3f}")

        return best_params, best_result

    def _load_games(
        self,
        sport: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
    ) -> List[Dict]:
        """Load games from database."""
        games = []

        if not self.db_path.exists():
            logger.warning(f"Database not found: {self.db_path}")
            return []

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            query = """
                SELECT g.*,
                       GROUP_CONCAT(m.token_id) as token_ids,
                       GROUP_CONCAT(m.outcome) as outcomes,
                       GROUP_CONCAT(m.resolved_yes) as resolutions,
                       GROUP_CONCAT(m.market_type) as market_types,
                       GROUP_CONCAT(m.team) as teams,
                       GROUP_CONCAT(m.final_price) as prices
                FROM game_resolutions g
                JOIN resolved_markets m ON g.game_key = m.game_key
                WHERE g.sport = ?
            """
            params = [sport]

            if start_date:
                query += " AND g.game_date >= ?"
                params.append(start_date.isoformat())

            if end_date:
                query += " AND g.game_date <= ?"
                params.append(end_date.isoformat())

            query += " GROUP BY g.game_key HAVING COUNT(m.token_id) >= 2"

            for row in conn.execute(query, params).fetchall():
                game = dict(row)

                # Parse concatenated fields
                game["token_ids"] = game["token_ids"].split(",") if game["token_ids"] else []
                game["outcomes"] = game["outcomes"].split(",") if game["outcomes"] else []
                game["resolutions"] = [int(x) for x in game["resolutions"].split(",")] if game["resolutions"] else []
                game["market_types"] = game["market_types"].split(",") if game["market_types"] else []
                game["teams"] = game["teams"].split(",") if game["teams"] else []
                game["prices"] = [float(x) for x in game["prices"].split(",")] if game["prices"] else []

                # Parse correlations
                if game.get("correlations_json"):
                    game["correlations"] = json.loads(game["correlations_json"])
                else:
                    game["correlations"] = {}

                games.append(game)

        return games

    def _build_correlation_matrix(
        self,
        game: Dict,
        sport: str,
    ) -> Optional[CorrelationMatrix]:
        """Build correlation matrix from game data."""
        token_ids = game.get("token_ids", [])
        n = len(token_ids)

        if n < 2:
            return None

        # Use trainer predictions if available
        if self.trainer and sport in self.trainer._models:
            correlation = np.zeros((n, n))
            confidence = np.zeros((n, n))

            market_types = game.get("market_types", [])
            teams = game.get("teams", [])

            # Pad lists to match token count
            while len(market_types) < n:
                market_types.append("unknown")
            while len(teams) < n:
                teams.append(None)

            for i in range(n):
                correlation[i, i] = 1.0
                confidence[i, i] = 1.0

                for j in range(i + 1, n):
                    team_i = teams[i] if i < len(teams) else None
                    team_j = teams[j] if j < len(teams) else None
                    same_team = team_i and team_j and team_i == team_j

                    type_i = market_types[i] if i < len(market_types) else "unknown"
                    type_j = market_types[j] if j < len(market_types) else "unknown"

                    corr, conf = self.trainer.predict(
                        sport,
                        type_i,
                        type_j,
                        same_team,
                        False,  # same_player not tracked
                    )

                    correlation[i, j] = corr
                    correlation[j, i] = corr
                    confidence[i, j] = conf
                    confidence[j, i] = conf
        else:
            # Use stored correlations
            stored = game.get("correlations", {})
            correlation = np.eye(n)
            confidence = np.ones((n, n)) * 0.5

            for i, tid_i in enumerate(token_ids):
                for j, tid_j in enumerate(token_ids[i+1:], i+1):
                    key = f"{min(tid_i, tid_j)}_{max(tid_i, tid_j)}"
                    if key in stored:
                        correlation[i, j] = stored[key]
                        correlation[j, i] = stored[key]
                        confidence[i, j] = 0.9
                        confidence[j, i] = 0.9

        return CorrelationMatrix(
            game_id=game["game_key"],
            market_ids=token_ids,
            correlation=correlation,
            confidence=confidence,
            model_type="trainer" if self.trainer else "stored",
        )

    def _find_negative_pairs(
        self,
        corr_matrix: CorrelationMatrix,
        threshold: float,
    ) -> List[Tuple[str, str, float]]:
        """Find negatively correlated pairs."""
        pairs = []
        n = len(corr_matrix.market_ids)

        for i in range(n):
            for j in range(i + 1, n):
                corr = corr_matrix.correlation[i, j]
                if corr <= threshold:
                    pairs.append((
                        corr_matrix.market_ids[i],
                        corr_matrix.market_ids[j],
                        corr,
                    ))

        return pairs

    def _construct_portfolio(
        self,
        game: Dict,
        corr_matrix: CorrelationMatrix,
        config: SportsPortfolioConfig,
    ) -> Optional[PortfolioPosition]:
        """Construct simulated portfolio."""
        token_ids = game.get("token_ids", [])
        prices = game.get("prices", [])
        outcomes = game.get("outcomes", [])

        if len(token_ids) < config.portfolio_opt.min_positions:
            return None

        # Simple portfolio construction: use negative pairs
        neg_pairs = self._find_negative_pairs(
            corr_matrix,
            config.min_negative_correlation,
        )

        if not neg_pairs:
            return None

        # Select markets from negative pairs
        selected_tokens = set()
        for t1, t2, _ in neg_pairs[:3]:  # Top 3 negative pairs
            selected_tokens.add(t1)
            selected_tokens.add(t2)

        allocations = []
        capital_per_position = config.risk.max_portfolio_cost / len(selected_tokens)

        for tid in selected_tokens:
            if tid not in token_ids:
                continue

            idx = token_ids.index(tid)
            # Simulate entry price (between 0.3 and 0.7)
            entry_price = max(0.3, min(0.7, np.random.uniform(0.35, 0.65)))
            shares = capital_per_position / entry_price

            allocations.append(PortfolioAllocation(
                market_id=game["game_key"],
                token_id=tid,
                outcome=outcomes[idx] if idx < len(outcomes) else "unknown",
                weight=1.0 / len(selected_tokens),
                shares=shares,
                cost=capital_per_position,
                entry_price=entry_price,
            ))

        if len(allocations) < config.portfolio_opt.min_positions:
            return None

        return PortfolioPosition.create(
            game_id=game["game_key"],
            agent_id="backtest",
            allocations=allocations,
            correlation_matrix=corr_matrix,
        )

    def _simulate_resolution(
        self,
        portfolio: PortfolioPosition,
        game: Dict,
    ) -> Tuple[List[BacktestTrade], float]:
        """Simulate portfolio resolution."""
        trades = []
        total_pnl = 0.0

        token_ids = game.get("token_ids", [])
        resolutions = game.get("resolutions", [])
        prices = game.get("prices", [])

        for alloc in portfolio.allocations:
            if alloc.token_id not in token_ids:
                continue

            idx = token_ids.index(alloc.token_id)
            resolved_yes = resolutions[idx] if idx < len(resolutions) else 0
            exit_price = 1.0 if resolved_yes else 0.0

            pnl = (exit_price - alloc.entry_price) * alloc.shares
            total_pnl += pnl

            trades.append(BacktestTrade(
                game_key=game["game_key"],
                sport=game.get("sport", "unknown"),
                token_id=alloc.token_id,
                side="BUY",
                shares=alloc.shares,
                entry_price=alloc.entry_price,
                exit_price=exit_price,
                pnl=pnl,
                timestamp=datetime.now(timezone.utc),
            ))

        return trades, total_pnl

    def _default_param_grid(self) -> Dict[str, List]:
        """Default parameter grid for optimization."""
        return {
            "risk_aversion": [1.0, 2.0, 3.0],
            "min_negative_correlation": [-0.3, -0.5, -0.7],
            "max_weight_per_position": [0.2, 0.3, 0.4],
            "min_positions": [2, 3, 4],
            "portfolio_stop_loss": [0.10, 0.15, 0.20],
        }

    def _params_to_config(self, params: Dict) -> SportsPortfolioConfig:
        """Convert params dict to config."""
        config = SportsPortfolioConfig()

        if "risk_aversion" in params:
            config.portfolio_opt.risk_aversion = params["risk_aversion"]
        if "min_negative_correlation" in params:
            config.min_negative_correlation = params["min_negative_correlation"]
        if "max_weight_per_position" in params:
            config.portfolio_opt.max_weight_per_position = params["max_weight_per_position"]
        if "min_positions" in params:
            config.portfolio_opt.min_positions = params["min_positions"]
        if "portfolio_stop_loss" in params:
            config.risk.portfolio_stop_loss_pct = params["portfolio_stop_loss"]

        return config

    def _config_to_params(self, config: SportsPortfolioConfig) -> Dict:
        """Convert config to params dict."""
        return {
            "risk_aversion": config.portfolio_opt.risk_aversion,
            "min_negative_correlation": config.min_negative_correlation,
            "max_weight_per_position": config.portfolio_opt.max_weight_per_position,
            "min_positions": config.portfolio_opt.min_positions,
            "portfolio_stop_loss": config.risk.portfolio_stop_loss_pct,
        }


def generate_backtest_report(results: Dict[str, BacktestResult]) -> str:
    """Generate a formatted backtest report."""
    lines = [
        "=" * 70,
        "SPORTS PORTFOLIO BACKTEST REPORT",
        "=" * 70,
        "",
    ]

    for sport, result in results.items():
        lines.extend([
            f"Sport: {sport.upper()}",
            "-" * 40,
            f"  Total P&L:        ${result.total_pnl:,.2f}",
            f"  Total Trades:     {result.total_trades}",
            f"  Win Rate:         {result.win_rate:.1%}",
            f"  Profit Factor:    {result.profit_factor:.2f}",
            f"  Sharpe Ratio:     {result.sharpe_ratio:.2f}",
            f"  Sortino Ratio:    {result.sortino_ratio:.2f}",
            f"  Max Drawdown:     {result.max_drawdown:.1%}",
            f"  Avg Positions:    {result.avg_positions_per_portfolio:.1f}",
            f"  Avg Hedging:      {result.avg_hedging_effectiveness:.1%}",
            "",
            "  Optimized Parameters:",
        ])

        for param, value in result.params.items():
            lines.append(f"    {param}: {value}")

        lines.append("")

    # Summary
    total_pnl = sum(r.total_pnl for r in results.values())
    total_trades = sum(r.total_trades for r in results.values())
    avg_sharpe = np.mean([r.sharpe_ratio for r in results.values()])

    lines.extend([
        "=" * 70,
        "OVERALL SUMMARY",
        "=" * 70,
        f"  Total P&L (all sports):  ${total_pnl:,.2f}",
        f"  Total Trades:            {total_trades}",
        f"  Average Sharpe:          {avg_sharpe:.2f}",
        "=" * 70,
    ])

    return "\n".join(lines)
