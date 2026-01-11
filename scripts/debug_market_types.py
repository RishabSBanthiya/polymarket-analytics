"""Debug market type classification from real data."""
import asyncio
import sys
sys.path.insert(0, "/Users/rishabbanthiya/polymarket-analytics")

from polymarket.core.api import PolymarketAPI
from polymarket.strategies.sports_portfolio.game_aggregator import GameMarketAggregator
from polymarket.strategies.sports_portfolio.config import SportsPortfolioConfig


async def debug():
    """Debug market classification."""
    config = SportsPortfolioConfig()
    api = PolymarketAPI()
    await api.connect()

    try:
        aggregator = GameMarketAggregator(api, config)
        games = await aggregator.get_games(sports=["nba"], hours_ahead=48)

        print(f"Found {len(games)} games\n")

        for game in games[:2]:  # Check first 2 games
            print(f"{'='*60}")
            print(f"Game: {game.home_team} vs {game.away_team}")
            print(f"Sport: {game.sport.value}")
            print(f"Markets: {len(game.markets)}")

            # Count market types
            type_counts = {}
            for m in game.markets:
                t = m.market_type.value
                type_counts[t] = type_counts.get(t, 0) + 1

            print(f"\nMarket type distribution: {type_counts}")

            # Show sample of each type
            print("\nSample markets by type:")
            shown_types = set()
            for m in game.markets:
                if m.market_type.value not in shown_types:
                    shown_types.add(m.market_type.value)
                    print(f"  [{m.market_type.value}] team={m.team}, player={m.player}")
                    print(f"    Q: {m.question[:80]}...")

            # Find potential hedging pairs
            print("\n--- Checking for potential hedging pairs ---")
            winner_markets = [m for m in game.markets if m.market_type.value == "winner"]
            spread_markets = [m for m in game.markets if m.market_type.value == "spread"]
            total_markets = [m for m in game.markets if m.market_type.value == "total"]
            player_prop_markets = [m for m in game.markets if m.market_type.value == "player_prop"]

            print(f"Winner markets: {len(winner_markets)}")
            if len(winner_markets) >= 2:
                # Check if we have opposite team winners
                teams = set(m.team for m in winner_markets if m.team)
                print(f"  Teams: {teams}")
                if len(teams) >= 2:
                    print("  -> CAN CREATE HEDGING PAIR (opposite team winners)")

            print(f"Spread markets: {len(spread_markets)}")
            if len(spread_markets) >= 2:
                teams = set(m.team for m in spread_markets if m.team)
                print(f"  Teams: {teams}")
                if len(teams) >= 2:
                    print("  -> CAN CREATE HEDGING PAIR (opposite team spreads)")

            print(f"Total markets: {len(total_markets)}")
            if len(total_markets) >= 2:
                # Check for over/under pairs
                has_over = any("over" in m.question.lower() for m in total_markets)
                has_under = any("under" in m.question.lower() for m in total_markets)
                print(f"  Has over: {has_over}, Has under: {has_under}")
                if has_over and has_under:
                    print("  -> CAN CREATE HEDGING PAIR (over/under)")

            print(f"Player prop markets: {len(player_prop_markets)}")
            if len(player_prop_markets) >= 2:
                # Check for opposite team props
                teams = set(m.team for m in player_prop_markets if m.team)
                print(f"  Teams: {teams}")
                if len(teams) >= 2:
                    print("  -> CAN CREATE HEDGING PAIR (opposite team props)")

            print()

    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(debug())
