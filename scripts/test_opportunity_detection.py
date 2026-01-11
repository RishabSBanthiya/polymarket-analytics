"""Quick test for opportunity detection with mock data."""
import sys
sys.path.insert(0, "/Users/rishabbanthiya/polymarket-analytics")

from polymarket.strategies.sports_portfolio.correlation_model import MLCorrelationModel
from polymarket.strategies.sports_portfolio.portfolio_optimizer import PortfolioOptimizer
from polymarket.strategies.sports_portfolio.config import (
    SportsPortfolioConfig, MLModelConfig, PortfolioOptConfig
)
from polymarket.strategies.sports_portfolio.models import (
    SportsGame, GameMarket, MarketType, Sport
)
from datetime import datetime, timezone, timedelta


def create_realistic_game():
    """Create a realistic game with multiple market types."""
    game_time = datetime.now(timezone.utc) + timedelta(hours=3)

    markets = [
        # Winner markets - opposite teams (should hedge!)
        # Lakers is favorite at 55c, bid/ask spread creates edge
        GameMarket(
            market_id="m-winner",
            token_id="t-lakers-win",
            question="Lakers vs Celtics",
            outcome="Lakers",
            price=0.55,
            bid=0.53,  # Can sell at 53c
            ask=0.57,  # Can buy at 57c
            market_type=MarketType.WINNER,
            team="Lakers",
        ),
        # Celtics is underdog at 45c (more edge buying underdogs)
        GameMarket(
            market_id="m-winner",
            token_id="t-celtics-win",
            question="Lakers vs Celtics",
            outcome="Celtics",
            price=0.45,
            bid=0.43,
            ask=0.47,
            market_type=MarketType.WINNER,
            team="Celtics",
        ),
        # Spread markets - opposite teams
        GameMarket(
            market_id="m-spread",
            token_id="t-lakers-spread",
            question="Spread: Lakers (-3.5)",
            outcome="Yes",
            price=0.52,
            bid=0.50,
            ask=0.54,
            market_type=MarketType.SPREAD,
            team="Lakers",
            threshold=-3.5,
        ),
        GameMarket(
            market_id="m-spread",
            token_id="t-celtics-spread",
            question="Spread: Celtics (+3.5)",
            outcome="Yes",
            price=0.48,
            bid=0.46,
            ask=0.50,
            market_type=MarketType.SPREAD,
            team="Celtics",
            threshold=3.5,
        ),
        # Player props - opposing players
        GameMarket(
            market_id="m-lebron",
            token_id="t-lebron-pts",
            question="LeBron James: Points Over 25.5",
            outcome="Yes",
            price=0.52,
            bid=0.50,
            ask=0.54,
            market_type=MarketType.PLAYER_PROP,
            team="Lakers",
            player="LeBron James",
            threshold=25.5,
        ),
        GameMarket(
            market_id="m-tatum",
            token_id="t-tatum-pts",
            question="Jayson Tatum: Points Over 26.5",
            outcome="Yes",
            price=0.48,
            bid=0.46,
            ask=0.50,
            market_type=MarketType.PLAYER_PROP,
            team="Celtics",
            player="Jayson Tatum",
            threshold=26.5,
        ),
        # Total markets
        GameMarket(
            market_id="m-total",
            token_id="t-over",
            question="Lakers vs Celtics: O/U 220.5",
            outcome="Over",
            price=0.52,
            bid=0.50,
            ask=0.54,
            market_type=MarketType.TOTAL,
            threshold=220.5,
        ),
        GameMarket(
            market_id="m-total",
            token_id="t-under",
            question="Lakers vs Celtics: O/U 220.5",
            outcome="Under",
            price=0.48,
            bid=0.46,
            ask=0.50,
            market_type=MarketType.TOTAL,
            threshold=220.5,
        ),
    ]

    return SportsGame(
        game_id="lakers-celtics-test",
        sport=Sport.NBA,
        home_team="Lakers",
        away_team="Celtics",
        game_time=game_time,
        markets=markets,
        slug="lakers-celtics-test",
    )


def test_opportunity_detection():
    """Test if the portfolio optimizer can find opportunities."""
    config = SportsPortfolioConfig()
    ml_config = MLModelConfig()
    opt_config = PortfolioOptConfig()

    correlation_model = MLCorrelationModel(ml_config)
    optimizer = PortfolioOptimizer(opt_config)

    game = create_realistic_game()

    print(f"Game: {game.home_team} vs {game.away_team}")
    print(f"Markets: {len(game.markets)}")
    print(f"\nConfig:")
    print(f"  min_negative_correlation: {config.min_negative_correlation}")
    print(f"  min_correlation_confidence: {config.min_correlation_confidence}")
    print(f"  min_sharpe_ratio: {opt_config.min_sharpe_ratio}")

    # Generate correlation matrix
    corr_matrix = correlation_model.predict_correlation_matrix(game)
    print(f"\nCorrelation matrix type: {corr_matrix.model_type}")

    # Check for negative correlations
    neg_pairs = corr_matrix.get_negatively_correlated_pairs(
        threshold=config.min_negative_correlation,
        min_confidence=config.min_correlation_confidence,
    )
    print(f"\nNegatively correlated pairs: {len(neg_pairs)}")
    for pair in neg_pairs[:5]:
        print(f"  {pair}")

    # Calculate expected returns (showing the problem)
    print("\n--- Expected returns (from optimizer) ---")
    returns = {}
    for market in game.markets:
        if market.bid and market.ask:
            spread = market.ask - market.bid
            mid = (market.bid + market.ask) / 2
            if mid > 0.5:
                edge = -spread / 2 * (mid - 0.5) * 2
            else:
                edge = spread / 2 * (0.5 - mid) * 2
            returns[market.token_id] = edge
            print(f"  {market.token_id}: mid={mid:.2f}, edge={edge:.4f} ({'positive' if edge > 0 else 'NEGATIVE'})")

    # Override with positive returns for ALL markets (hedging works regardless)
    # In real hedged portfolios, the edge comes from variance reduction, not individual returns
    expected_returns = {m.token_id: 0.01 for m in game.markets}  # 1% base edge
    print("\n--- Using flat 1% expected returns for hedging test ---")

    # Try to optimize
    print("\n--- Running portfolio optimization ---")
    portfolio = optimizer.optimize(
        game=game,
        correlation_matrix=corr_matrix,
        capital=200.0,
        expected_returns=expected_returns,
    )

    if portfolio:
        print(f"\n*** PORTFOLIO FOUND ***")
        print(f"Game: {portfolio.game_id}")
        print(f"Expected return: {portfolio.expected_return:.4f}")
        print(f"Expected variance: {portfolio.expected_variance:.4f}")
        print(f"Sharpe ratio: {portfolio.sharpe_ratio:.2f}")
        print(f"\nAllocations:")
        for alloc in portfolio.allocations:
            print(f"  {alloc.token_id}: {alloc.shares:.2f} shares @ ${alloc.entry_price:.2f}")
    else:
        print("\n*** NO PORTFOLIO FOUND ***")
        print("The optimizer could not find a valid portfolio.")


if __name__ == "__main__":
    test_opportunity_detection()
