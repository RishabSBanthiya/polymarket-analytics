"""Diagnostic script to understand why sports bot isn't finding opportunities."""
import asyncio
import logging
import sys
sys.path.insert(0, "/Users/rishabbanthiya/polymarket-analytics")

from polymarket.strategies.sports_portfolio.scanner import SportsPortfolioScanner
from polymarket.strategies.sports_portfolio.correlation_model import MLCorrelationModel
from polymarket.strategies.sports_portfolio.config import SportsPortfolioConfig, MLModelConfig
from polymarket.strategies.sports_portfolio.models import MarketType
from polymarket.core.api import PolymarketAPI

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


async def diagnose_with_api(api: PolymarketAPI, config: SportsPortfolioConfig):
    """Run diagnostic to understand market classification."""
    scanner = SportsPortfolioScanner(api, config)
    correlation_model = MLCorrelationModel(MLModelConfig())

    # Fetch games via scanner's aggregator
    logger.info("Fetching sports games...")
    games = await scanner.game_aggregator.get_games(
        sports=["nba", "nfl", "nhl"],
        hours_ahead=48,
    )

    logger.info(f"Found {len(games)} games")

    for game in games[:3]:  # Check first 3 games
        logger.info(f"\n{'='*60}")
        logger.info(f"Game: {game.home_team} vs {game.away_team}")
        logger.info(f"Sport: {game.sport}, Markets: {len(game.markets)}")

        # Classify market types
        type_counts = {}
        for market in game.markets:
            mt = market.market_type.value
            type_counts[mt] = type_counts.get(mt, 0) + 1

        logger.info(f"Market types: {type_counts}")

        # Show first few markets with details
        logger.info("\nSample markets:")
        for i, m in enumerate(game.markets[:10]):
            logger.info(f"  {i+1}. Type={m.market_type.value}, Team={m.team}, Q={m.question[:60]}...")

        # Check correlations
        logger.info("\nCorrelation matrix:")
        corr_matrix = correlation_model.predict_correlation_matrix(game)

        # Find negative correlations
        hedging_pairs = []
        n = len(game.markets)
        for i in range(n):
            for j in range(i+1, n):
                corr, conf = corr_matrix.get_correlation(
                    game.markets[i].token_id,
                    game.markets[j].token_id
                )
                if corr <= config.min_negative_correlation and conf >= config.min_correlation_confidence:
                    hedging_pairs.append((
                        game.markets[i].question[:40],
                        game.markets[j].question[:40],
                        corr,
                        conf
                    ))

        logger.info(f"Found {len(hedging_pairs)} hedging pairs (threshold corr<={config.min_negative_correlation}, conf>={config.min_correlation_confidence})")

        for p in hedging_pairs[:5]:
            logger.info(f"  - '{p[0]}...' vs '{p[1]}...' corr={p[2]:.2f} conf={p[3]:.2f}")

        # Show all unique negative correlations
        all_neg_corr = []
        for i in range(n):
            for j in range(i+1, n):
                corr, conf = corr_matrix.get_correlation(
                    game.markets[i].token_id,
                    game.markets[j].token_id
                )
                if corr < 0:
                    all_neg_corr.append((corr, conf, i, j))

        if all_neg_corr:
            all_neg_corr.sort(key=lambda x: x[0])
            logger.info(f"\nTop 5 most negative correlations (any confidence):")
            for corr, conf, i, j in all_neg_corr[:5]:
                mi = game.markets[i]
                mj = game.markets[j]
                logger.info(f"  corr={corr:.2f} conf={conf:.2f}")
                logger.info(f"    Market A: type={mi.market_type.value} team={mi.team} Q={mi.question[:50]}...")
                logger.info(f"    Market B: type={mj.market_type.value} team={mj.team} Q={mj.question[:50]}...")


async def main():
    api = None
    try:
        config = SportsPortfolioConfig()
        api = PolymarketAPI()
        await api.connect()
        await diagnose_with_api(api, config)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
    finally:
        if api:
            await api.close()


if __name__ == "__main__":
    asyncio.run(main())
