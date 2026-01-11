"""Test correlation logic directly."""
import sys
sys.path.insert(0, "/Users/rishabbanthiya/polymarket-analytics")

from polymarket.strategies.sports_portfolio.correlation_model import MLCorrelationModel
from polymarket.strategies.sports_portfolio.config import MLModelConfig, SportsPortfolioConfig
from polymarket.strategies.sports_portfolio.models import (
    SportsGame, GameMarket, MarketType, Sport, CorrelationMatrix
)
from datetime import datetime, timezone

# Create mock game with known market types
def create_test_game():
    """Create a test game with various market types."""
    game_time = datetime.now(timezone.utc)

    markets = [
        # Winner markets (Team A vs Team B)
        GameMarket(
            market_id="m1",
            token_id="t1",
            question="Lakers to beat Celtics",
            outcome="Yes",
            price=0.55,
            market_type=MarketType.WINNER,
            team="Lakers",
        ),
        GameMarket(
            market_id="m2",
            token_id="t2",
            question="Celtics to beat Lakers",
            outcome="Yes",
            price=0.45,
            market_type=MarketType.WINNER,
            team="Celtics",
        ),
        # Total markets
        GameMarket(
            market_id="m3",
            token_id="t3",
            question="Total points over 220.5",
            outcome="Yes",
            price=0.50,
            market_type=MarketType.TOTAL,
            threshold=220.5,
        ),
        GameMarket(
            market_id="m3",  # Same market ID for over/under
            token_id="t3u",
            question="Total points under 220.5",
            outcome="Yes",
            price=0.50,
            market_type=MarketType.TOTAL,
            threshold=220.5,
        ),
        # Player props (same team)
        GameMarket(
            market_id="m4",
            token_id="t4",
            question="LeBron James points over 25.5",
            outcome="Yes",
            price=0.50,
            market_type=MarketType.PLAYER_PROP,
            team="Lakers",
            player="LeBron James",
            threshold=25.5,
        ),
        GameMarket(
            market_id="m5",
            token_id="t5",
            question="Anthony Davis points over 24.5",
            outcome="Yes",
            price=0.50,
            market_type=MarketType.PLAYER_PROP,
            team="Lakers",
            player="Anthony Davis",
            threshold=24.5,
        ),
        # Player props (opposite team)
        GameMarket(
            market_id="m6",
            token_id="t6",
            question="Jayson Tatum points over 26.5",
            outcome="Yes",
            price=0.50,
            market_type=MarketType.PLAYER_PROP,
            team="Celtics",
            player="Jayson Tatum",
            threshold=26.5,
        ),
        # Spread markets
        GameMarket(
            market_id="m7",
            token_id="t7",
            question="Lakers -3.5",
            outcome="Yes",
            price=0.50,
            market_type=MarketType.SPREAD,
            team="Lakers",
            threshold=-3.5,
        ),
        GameMarket(
            market_id="m8",
            token_id="t8",
            question="Celtics +3.5",
            outcome="Yes",
            price=0.50,
            market_type=MarketType.SPREAD,
            team="Celtics",
            threshold=3.5,
        ),
    ]

    return SportsGame(
        game_id="test-game",
        sport=Sport.NBA,
        home_team="Lakers",
        away_team="Celtics",
        game_time=game_time,
        markets=markets,
        slug="lakers-celtics-test",
    )


def test_correlations():
    """Test correlation matrix generation."""
    config = MLModelConfig()
    model = MLCorrelationModel(config)
    sports_config = SportsPortfolioConfig()

    game = create_test_game()
    print(f"\nGame: {game.home_team} vs {game.away_team}")
    print(f"Markets: {len(game.markets)}")
    print(f"\nConfig thresholds:")
    print(f"  min_negative_correlation: {sports_config.min_negative_correlation}")
    print(f"  min_correlation_confidence: {sports_config.min_correlation_confidence}")

    # Show market details
    print("\nMarket details:")
    for i, m in enumerate(game.markets):
        print(f"  {i}. {m.token_id}: Type={m.market_type.value}, Team={m.team}, Player={m.player}")

    # Get correlation matrix
    corr_matrix = model.predict_correlation_matrix(game)

    print(f"\nCorrelation matrix (model_type={corr_matrix.model_type}):")

    # Find all correlations
    n = len(game.markets)
    correlations = []
    for i in range(n):
        for j in range(i+1, n):
            corr, conf = corr_matrix.get_correlation(
                game.markets[i].token_id,
                game.markets[j].token_id
            )
            mi = game.markets[i]
            mj = game.markets[j]
            correlations.append((corr, conf, i, j, mi, mj))

    # Sort by correlation (most negative first)
    correlations.sort(key=lambda x: x[0])

    print("\nAll correlations (sorted by value):")
    for corr, conf, i, j, mi, mj in correlations:
        meets_threshold = (corr <= sports_config.min_negative_correlation and
                         conf >= sports_config.min_correlation_confidence)
        marker = " ** HEDGING PAIR **" if meets_threshold else ""
        print(f"  {corr:+.2f} (conf={conf:.2f}): {mi.market_type.value}[{mi.team}] vs {mj.market_type.value}[{mj.team}]{marker}")

    # Count hedging pairs
    hedging_pairs = [(c, cf, i, j, mi, mj) for c, cf, i, j, mi, mj in correlations
                    if c <= sports_config.min_negative_correlation and cf >= sports_config.min_correlation_confidence]

    print(f"\n=== SUMMARY ===")
    print(f"Total pairs: {len(correlations)}")
    print(f"Hedging pairs found: {len(hedging_pairs)}")

    if hedging_pairs:
        print("\nHedging pairs:")
        for corr, conf, i, j, mi, mj in hedging_pairs:
            print(f"  - {mi.question[:40]}... vs {mj.question[:40]}... (corr={corr:.2f})")
    else:
        print("\n*** NO HEDGING PAIRS FOUND ***")
        print("This means the bot won't find any opportunities!")

        # Show what thresholds would be needed
        if correlations:
            most_negative = correlations[0]
            print(f"\nMost negative correlation: {most_negative[0]:.2f} (conf={most_negative[1]:.2f})")
            print(f"Current threshold: corr <= {sports_config.min_negative_correlation}")
            if most_negative[0] > sports_config.min_negative_correlation:
                print(f"Need to relax threshold to: corr <= {most_negative[0]:.2f}")


if __name__ == "__main__":
    test_correlations()
