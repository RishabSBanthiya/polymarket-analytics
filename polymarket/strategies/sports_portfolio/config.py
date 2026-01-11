"""
Configuration for sports portfolio strategy.

Provides settings for:
- ML correlation model
- Portfolio optimization
- Risk management
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MLModelConfig:
    """Configuration for ML correlation model."""
    # Model type
    model_type: str = "gradient_boosting"  # "gradient_boosting", "neural_network", "random_forest"

    # Training parameters
    min_training_samples: int = 100
    validation_split: float = 0.2
    use_cross_validation: bool = True
    cv_folds: int = 5

    # Feature engineering
    use_structural_features: bool = True
    use_historical_features: bool = True
    use_price_features: bool = True

    # Model hyperparameters (gradient boosting defaults)
    n_estimators: int = 100
    max_depth: int = 5
    learning_rate: float = 0.1
    min_samples_split: int = 10

    # Confidence calibration
    calibrate_confidence: bool = True
    min_confidence_threshold: float = 0.6

    # Update frequency
    retrain_interval_hours: int = 24


@dataclass
class PortfolioOptConfig:
    """Configuration for portfolio optimization."""
    # Optimization method
    method: str = "mean_variance"  # "mean_variance", "risk_parity", "max_sharpe"

    # Risk parameters
    risk_aversion: float = 2.0          # Lambda in mean-variance
    target_volatility: float = 0.15     # For risk-parity
    min_sharpe_ratio: float = 0.1       # Minimum to trade (relaxed)

    # Position constraints
    max_weight_per_position: float = 0.30   # Max 30% in single position
    min_weight_per_position: float = 0.05   # Min 5% if included
    allow_short_positions: bool = True      # Allow negative weights
    max_short_exposure: float = 0.30        # Max 30% short exposure

    # Diversification
    min_positions: int = 3                  # Minimum positions in portfolio
    max_positions: int = 10                 # Maximum positions
    min_negative_correlation: float = -0.25  # At least one pair below this

    # Hedging requirements
    require_hedge: bool = True              # Must have at least one hedging pair
    min_hedging_effectiveness: float = 0.2  # Variance reduction vs uncorrelated

    # Transaction costs
    trading_cost_bps: int = 20              # Round-trip trading cost estimate


@dataclass
class RiskConfig:
    """Risk management configuration."""
    # Position limits
    max_portfolio_cost: float = 500.0       # Max USD per portfolio
    max_game_exposure: float = 1000.0       # Max exposure to single game
    max_concurrent_portfolios: int = 5      # Max active portfolios

    # Stop loss / take profit
    portfolio_stop_loss_pct: float = 0.15   # 15% portfolio stop
    portfolio_take_profit_pct: float = 0.30 # 30% take profit
    position_stop_loss_pct: float = 0.25    # 25% individual position stop

    # Correlation breakdown protection
    max_correlation_deviation: float = 0.3  # Exit if correlation deviates too much
    correlation_check_interval: int = 300   # Check every 5 minutes

    # Time-based
    min_time_to_resolution_hours: float = 1.0   # Don't enter < 1 hour to game
    max_time_to_resolution_hours: float = 48.0  # Don't enter > 48 hours ahead


@dataclass
class SportsPortfolioConfig:
    """Main configuration for sports portfolio strategy."""
    # Sub-configs
    ml_model: MLModelConfig = field(default_factory=MLModelConfig)
    portfolio_opt: PortfolioOptConfig = field(default_factory=PortfolioOptConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    # Enabled sports
    enabled_sports: Optional[List[str]] = None  # None = all sports

    # Market filtering
    min_market_liquidity: float = 100.0     # Min USD liquidity per market
    max_spread_bps: int = 300               # Max 3% spread
    min_markets_per_game: int = 4           # Need at least 4 markets

    # Correlation thresholds (relaxed for opportunity detection)
    min_negative_correlation: float = -0.25  # Consider as hedging pair
    min_correlation_confidence: float = 0.5  # Minimum model confidence

    # Scanning settings
    scan_interval_seconds: int = 60         # How often to scan
    market_refresh_seconds: int = 120       # How often to refresh markets

    # Execution
    use_limit_orders: bool = True
    limit_order_offset_bps: int = 10        # Place limits 10bps better than market
    order_timeout_seconds: int = 30

    # Logging
    log_opportunities: bool = True
    log_portfolio_construction: bool = True

    def get_enabled_sports(self) -> List[str]:
        """Get list of enabled sports."""
        if self.enabled_sports is None:
            return ["nba", "nfl", "nhl", "mlb", "soccer"]
        return self.enabled_sports

    @classmethod
    def conservative(cls) -> "SportsPortfolioConfig":
        """Conservative configuration with stricter thresholds."""
        return cls(
            portfolio_opt=PortfolioOptConfig(
                risk_aversion=3.0,
                max_weight_per_position=0.20,
                allow_short_positions=False,
                min_negative_correlation=-0.5,
            ),
            risk=RiskConfig(
                max_portfolio_cost=200.0,
                portfolio_stop_loss_pct=0.10,
                max_concurrent_portfolios=3,
            ),
            min_market_liquidity=200.0,
            max_spread_bps=200,
            min_negative_correlation=-0.6,
            min_correlation_confidence=0.8,
        )

    @classmethod
    def aggressive(cls) -> "SportsPortfolioConfig":
        """Aggressive configuration with looser thresholds."""
        return cls(
            portfolio_opt=PortfolioOptConfig(
                risk_aversion=1.0,
                max_weight_per_position=0.40,
                allow_short_positions=True,
                max_short_exposure=0.40,
                min_negative_correlation=-0.2,
            ),
            risk=RiskConfig(
                max_portfolio_cost=1000.0,
                portfolio_stop_loss_pct=0.20,
                max_concurrent_portfolios=10,
            ),
            min_market_liquidity=50.0,
            max_spread_bps=400,
            min_negative_correlation=-0.3,
            min_correlation_confidence=0.6,
        )
