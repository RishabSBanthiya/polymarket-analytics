"""
Data models for sports portfolio strategy.

Defines structures for:
- Sports games and their associated markets
- Market types and correlations
- Portfolio positions and allocations
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Dict, Optional, Tuple, Any
import numpy as np
import uuid


class MarketType(Enum):
    """Types of markets within a sports game."""
    WINNER = "winner"               # Team A vs Team B winner
    MONEYLINE = "moneyline"         # Same as winner but different naming
    SPREAD = "spread"               # Point spread / handicap
    TOTAL = "total"                 # Over/under total points
    PLAYER_PROP = "player_prop"     # Player performance props
    TEAM_PROP = "team_prop"         # Team-specific props
    GAME_PROP = "game_prop"         # Game events (first score, overtime, etc.)
    QUARTER_HALF = "quarter_half"   # Quarter/half specific markets
    UNKNOWN = "unknown"


class Sport(Enum):
    """Supported sports."""
    NBA = "nba"
    NFL = "nfl"
    NHL = "nhl"
    MLB = "mlb"
    SOCCER = "soccer"
    TENNIS = "tennis"
    MMA = "mma"
    UNKNOWN = "unknown"


@dataclass
class GameMarket:
    """A single market within a sports game."""
    market_id: str
    token_id: str
    question: str
    outcome: str                    # "Yes", team name, player name, etc.
    market_type: MarketType

    # Pricing
    price: float                    # Current mid price (0-1)
    bid: Optional[float] = None     # Best bid
    ask: Optional[float] = None     # Best ask

    # Metadata
    team: Optional[str] = None      # Associated team (if applicable)
    player: Optional[str] = None    # Associated player (if applicable)
    threshold: Optional[float] = None  # For over/under or spread

    # Liquidity
    bid_size: float = 0.0
    ask_size: float = 0.0
    volume_24h: float = 0.0

    @property
    def spread(self) -> Optional[float]:
        """Bid-ask spread in percentage."""
        if self.bid and self.ask:
            return (self.ask - self.bid) / self.ask * 100
        return None

    @property
    def mid_price(self) -> float:
        """Mid price from bid/ask or stored price."""
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.price


@dataclass
class SportsGame:
    """A sports game with all its associated markets."""
    game_id: str
    sport: Sport
    home_team: str
    away_team: str
    game_time: datetime

    # All markets for this game
    markets: List[GameMarket] = field(default_factory=list)

    # Game state
    is_live: bool = False
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    quarter: Optional[int] = None
    time_remaining: Optional[str] = None

    # Metadata
    slug: Optional[str] = None

    @property
    def market_count(self) -> int:
        return len(self.markets)

    @property
    def markets_by_type(self) -> Dict[MarketType, List[GameMarket]]:
        """Group markets by type."""
        result: Dict[MarketType, List[GameMarket]] = {}
        for market in self.markets:
            if market.market_type not in result:
                result[market.market_type] = []
            result[market.market_type].append(market)
        return result

    def get_winner_markets(self) -> List[GameMarket]:
        """Get winner/moneyline markets."""
        return [
            m for m in self.markets
            if m.market_type in (MarketType.WINNER, MarketType.MONEYLINE)
        ]

    def get_player_props(self) -> List[GameMarket]:
        """Get player prop markets."""
        return [m for m in self.markets if m.market_type == MarketType.PLAYER_PROP]

    def get_total_markets(self) -> List[GameMarket]:
        """Get over/under total markets."""
        return [m for m in self.markets if m.market_type == MarketType.TOTAL]


@dataclass
class CorrelationMatrix:
    """Correlation matrix for markets within a game."""
    game_id: str
    market_ids: List[str]           # Ordered list of market IDs
    correlation: np.ndarray         # NxN correlation matrix
    confidence: np.ndarray          # NxN confidence matrix (0-1)

    # Model info
    model_type: str = "unknown"     # e.g., "structural", "ml_predicted", "historical"
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def get_correlation(self, market_a: str, market_b: str) -> Tuple[float, float]:
        """Get correlation and confidence between two markets."""
        try:
            idx_a = self.market_ids.index(market_a)
            idx_b = self.market_ids.index(market_b)
            return float(self.correlation[idx_a, idx_b]), float(self.confidence[idx_a, idx_b])
        except (ValueError, IndexError):
            return 0.0, 0.0

    def get_negatively_correlated_pairs(
        self,
        threshold: float = -0.5,
        min_confidence: float = 0.7
    ) -> List[Tuple[str, str, float, float]]:
        """Get pairs with strong negative correlation."""
        pairs = []
        n = len(self.market_ids)
        for i in range(n):
            for j in range(i + 1, n):
                corr = float(self.correlation[i, j])
                conf = float(self.confidence[i, j])
                if corr <= threshold and conf >= min_confidence:
                    pairs.append((
                        self.market_ids[i],
                        self.market_ids[j],
                        corr,
                        conf
                    ))
        return sorted(pairs, key=lambda x: x[2])  # Sort by correlation (most negative first)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for storage."""
        return {
            "game_id": self.game_id,
            "market_ids": self.market_ids,
            "correlation": self.correlation.tolist(),
            "confidence": self.confidence.tolist(),
            "model_type": self.model_type,
            "computed_at": self.computed_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CorrelationMatrix":
        """Deserialize from storage."""
        return cls(
            game_id=data["game_id"],
            market_ids=data["market_ids"],
            correlation=np.array(data["correlation"]),
            confidence=np.array(data["confidence"]),
            model_type=data.get("model_type", "unknown"),
            computed_at=datetime.fromisoformat(data["computed_at"]),
        )


@dataclass
class PortfolioAllocation:
    """Allocation for a single market in the portfolio."""
    market_id: str
    token_id: str
    outcome: str
    weight: float                   # Portfolio weight (can be negative for shorts)
    shares: float                   # Number of shares
    cost: float                     # Entry cost in USD

    # Entry details
    entry_price: float
    target_price: Optional[float] = None
    stop_price: Optional[float] = None

    # Current state
    current_price: Optional[float] = None
    unrealized_pnl: float = 0.0

    @property
    def current_value(self) -> float:
        """Current value of position."""
        if self.current_price is not None:
            return self.shares * self.current_price
        return self.cost

    @property
    def pnl_pct(self) -> float:
        """P&L as percentage."""
        if self.cost > 0:
            return (self.unrealized_pnl / self.cost) * 100
        return 0.0


@dataclass
class PortfolioPosition:
    """A multi-market portfolio position."""
    position_id: str
    game_id: str
    agent_id: str

    # Allocations
    allocations: List[PortfolioAllocation] = field(default_factory=list)

    # Portfolio metrics
    total_cost: float = 0.0
    expected_return: float = 0.0
    expected_variance: float = 0.0
    sharpe_ratio: float = 0.0

    # Correlation info
    avg_pairwise_correlation: float = 0.0
    hedging_effectiveness: float = 0.0  # 0-1, how well hedged

    # State
    status: str = "pending"         # pending, active, closing, closed
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None

    # P&L
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0

    @classmethod
    def create(
        cls,
        game_id: str,
        agent_id: str,
        allocations: List[PortfolioAllocation],
        correlation_matrix: Optional[CorrelationMatrix] = None,
    ) -> "PortfolioPosition":
        """Create a new portfolio position."""
        position = cls(
            position_id=str(uuid.uuid4()),
            game_id=game_id,
            agent_id=agent_id,
            allocations=allocations,
        )

        # Calculate metrics
        position.total_cost = sum(a.cost for a in allocations)

        if correlation_matrix and len(allocations) > 1:
            # Calculate portfolio variance considering correlations
            weights = np.array([a.weight for a in allocations])
            token_ids = [a.token_id for a in allocations]

            # Build covariance matrix using token_ids (correlations keyed by token_id)
            n = len(token_ids)
            cov = np.zeros((n, n))
            total_corr = 0.0
            count = 0

            for i in range(n):
                for j in range(n):
                    corr, conf = correlation_matrix.get_correlation(
                        token_ids[i], token_ids[j]
                    )
                    # Use binary variance: p * (1 - p) from entry price
                    var_i = allocations[i].entry_price * (1 - allocations[i].entry_price)
                    var_j = allocations[j].entry_price * (1 - allocations[j].entry_price)
                    var_i = max(0.01, var_i)  # Floor at 1%
                    var_j = max(0.01, var_j)
                    # Weight correlation by confidence
                    effective_corr = corr * conf
                    cov[i, j] = effective_corr * np.sqrt(var_i * var_j)
                    if i != j:
                        total_corr += corr
                        count += 1

            # Portfolio variance
            position.expected_variance = float(weights @ cov @ weights.T)

            # Average pairwise correlation
            if count > 0:
                position.avg_pairwise_correlation = total_corr / count

            # Hedging effectiveness: how much variance reduced vs. equal-weighted uncorrelated
            baseline_var = sum(w**2 for w in weights)  # If correlations were 0
            if baseline_var > 0:
                position.hedging_effectiveness = 1 - (position.expected_variance / baseline_var)

        return position

    @property
    def num_positions(self) -> int:
        return len(self.allocations)

    @property
    def long_exposure(self) -> float:
        """Total long exposure."""
        return sum(a.cost for a in self.allocations if a.weight > 0)

    @property
    def short_exposure(self) -> float:
        """Total short exposure."""
        return sum(abs(a.cost) for a in self.allocations if a.weight < 0)

    def update_prices(self, prices: Dict[str, float]) -> None:
        """Update positions with current prices."""
        total_pnl = 0.0
        for alloc in self.allocations:
            if alloc.token_id in prices:
                alloc.current_price = prices[alloc.token_id]
                alloc.unrealized_pnl = (alloc.current_price - alloc.entry_price) * alloc.shares
                if alloc.weight < 0:  # Short position
                    alloc.unrealized_pnl = -alloc.unrealized_pnl
                total_pnl += alloc.unrealized_pnl

        self.unrealized_pnl = total_pnl


@dataclass
class HistoricalGameData:
    """Historical data for ML training."""
    game_id: str
    sport: Sport
    game_time: datetime

    # Teams
    home_team: str
    away_team: str

    # Final result
    home_score: int
    away_score: int
    winner: str  # "home", "away", or "draw"

    # Market resolutions
    market_resolutions: Dict[str, bool] = field(default_factory=dict)  # token_id -> resolved YES

    # Price history (for computing realized correlations)
    price_history: Dict[str, List[Tuple[datetime, float]]] = field(default_factory=dict)

    @property
    def margin(self) -> int:
        """Point margin (positive = home win)."""
        return self.home_score - self.away_score

    @property
    def total_points(self) -> int:
        return self.home_score + self.away_score


@dataclass
class MLFeatures:
    """Features for ML correlation model."""
    # Market pair features
    market_a_type: str
    market_b_type: str
    same_team: bool
    same_player: bool
    structural_correlation: float   # Known logical correlation

    # Game context
    sport: str
    is_playoff: bool
    spread_vegas: Optional[float]   # Vegas spread if available
    total_vegas: Optional[float]    # Vegas total if available

    # Historical features (if available)
    historical_correlation: Optional[float]
    price_volatility_a: float
    price_volatility_b: float

    def to_array(self) -> np.ndarray:
        """Convert to numpy array for model input."""
        return np.array([
            self._encode_market_type(self.market_a_type),
            self._encode_market_type(self.market_b_type),
            1.0 if self.same_team else 0.0,
            1.0 if self.same_player else 0.0,
            self.structural_correlation,
            self._encode_sport(self.sport),
            1.0 if self.is_playoff else 0.0,
            self.spread_vegas or 0.0,
            self.total_vegas or 0.0,
            self.historical_correlation or 0.0,
            self.price_volatility_a,
            self.price_volatility_b,
        ])

    def _encode_market_type(self, market_type: str) -> float:
        """Encode market type as numeric."""
        type_map = {
            "winner": 0.0,
            "moneyline": 0.0,
            "spread": 0.2,
            "total": 0.4,
            "player_prop": 0.6,
            "team_prop": 0.8,
            "game_prop": 1.0,
        }
        return type_map.get(market_type.lower(), 0.5)

    def _encode_sport(self, sport: str) -> float:
        """Encode sport as numeric."""
        sport_map = {
            "nba": 0.0,
            "nfl": 0.2,
            "nhl": 0.4,
            "mlb": 0.6,
            "soccer": 0.8,
        }
        return sport_map.get(sport.lower(), 0.5)
