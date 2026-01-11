"""
Game Market Aggregator.

Fetches and organizes all markets for a sports game,
classifying them by type and extracting relevant features.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple, Set

from polymarket.core.api import PolymarketAPI
from polymarket.core.models import Market

from .models import (
    SportsGame,
    GameMarket,
    MarketType,
    Sport,
)
from .config import SportsPortfolioConfig

logger = logging.getLogger(__name__)


# Patterns for market type classification
# Order matters - more specific patterns should come first
MARKET_TYPE_PATTERNS = {
    # Player props - check BEFORE totals since they often have "over" keyword
    MarketType.PLAYER_PROP: [
        r"[A-Z][a-z]+\s+[A-Z][a-z]+.*(?:over|under)\s+\d+",  # "Player Name: Points Over X"
        r"[A-Z][a-z]+\s+[A-Z][a-z]+.*:\s*(?:points|assists|rebounds|yards)",  # "Player: Points"
        r"(?:points|assists|rebounds|yards|touchdowns|receptions|completions)\s+over\s+\d+",
        r"score\s*\d+\+?\s*(?:points|goals|runs|touchdowns)",
        r"pass(?:es|ing)?\s*for\s*\d+",
        r"rush(?:es|ing)?\s*for\s*\d+",
        r"receiv(?:e|ing)\s*\d+",
        r"strikeouts?\s*over",
        r"home\s*runs?",
    ],
    MarketType.SPREAD: [
        r"spread:\s*\w+\s*\([+-]?\d+",  # "Spread: Team (-3.5)"
        r"(?:by|win by)\s*\d+",
        r"spread",
        r"handicap",
        r"^\s*\w+\s+[+-]\d+\.?\d*$",  # "Team -3.5"
    ],
    MarketType.TOTAL: [
        r"o/u\s*\d+",  # "O/U 220.5"
        r"over\s*/\s*under",
        r"total\s*(?:points|goals|runs)",
        r"combined\s*(?:points|score)",
    ],
    # Winner/Moneyline - only for "Team vs Team" format without over/under/spread keywords
    MarketType.WINNER: [
        r"^[A-Za-z]+\s+vs\.?\s+[A-Za-z]+$",  # "Hawks vs. Warriors" (simple)
        r"^[A-Za-z\s]+\s+vs\.?\s+[A-Za-z\s]+:\s*(?:moneyline|winner)?",  # With moneyline
        r"win(?:s|ning)?",
        r"beat(?:s)?",
        r"defeat(?:s)?",
        r"victor(?:y|ious)?",
        r"moneyline",
    ],
    MarketType.GAME_PROP: [
        r"first\s*(?:team\s*)?(?:to\s*)?score",
        r"overtime",
        r"ot\b",
        r"double\s*overtime",
        r"shutout",
        r"no\s*score",
    ],
    MarketType.QUARTER_HALF: [
        r"(?:1st|2nd|3rd|4th)\s*quarter",
        r"(?:first|second|third|fourth)\s*quarter",
        r"(?:1st|2nd)\s*half",
        r"1h\s+",  # "1H Spread"
        r"halftime",
        r"at\s*(?:the\s*)?half",
    ],
}

# Team name variations
TEAM_ALIASES = {
    # NBA
    "lakers": ["la lakers", "los angeles lakers", "lal", "lakers"],
    "celtics": ["boston celtics", "bos", "celtics"],
    "warriors": ["golden state warriors", "gsw", "golden state", "warriors"],
    "nets": ["brooklyn nets", "bkn", "nets"],
    "knicks": ["new york knicks", "nyk", "knicks"],
    "hawks": ["atlanta hawks", "atl", "hawks"],
    "heat": ["miami heat", "mia", "heat"],
    "bulls": ["chicago bulls", "chi", "bulls"],
    "cavaliers": ["cleveland cavaliers", "cle", "cavs", "cavaliers"],
    "mavericks": ["dallas mavericks", "dal", "mavs", "mavericks"],
    "nuggets": ["denver nuggets", "den", "nuggets"],
    "pistons": ["detroit pistons", "det", "pistons"],
    "rockets": ["houston rockets", "hou", "rockets"],
    "pacers": ["indiana pacers", "ind", "pacers"],
    "clippers": ["la clippers", "los angeles clippers", "lac", "clippers"],
    "grizzlies": ["memphis grizzlies", "mem", "grizzlies"],
    "bucks": ["milwaukee bucks", "mil", "bucks"],
    "timberwolves": ["minnesota timberwolves", "min", "wolves", "timberwolves"],
    "pelicans": ["new orleans pelicans", "nop", "pelicans"],
    "thunder": ["oklahoma city thunder", "okc", "thunder"],
    "magic": ["orlando magic", "orl", "magic"],
    "76ers": ["philadelphia 76ers", "phi", "sixers", "76ers"],
    "suns": ["phoenix suns", "phx", "suns"],
    "trail blazers": ["portland trail blazers", "por", "blazers", "trail blazers"],
    "kings": ["sacramento kings", "sac", "kings"],
    "spurs": ["san antonio spurs", "sas", "spurs"],
    "raptors": ["toronto raptors", "tor", "raptors"],
    "jazz": ["utah jazz", "uta", "jazz"],
    "wizards": ["washington wizards", "was", "wizards"],
    "hornets": ["charlotte hornets", "cha", "hornets"],
    # NFL
    "chiefs": ["kansas city chiefs", "kc", "chiefs"],
    "eagles": ["philadelphia eagles", "phi", "eagles"],
    "49ers": ["san francisco 49ers", "sf", "niners", "49ers"],
    "cowboys": ["dallas cowboys", "dal", "cowboys"],
    "bills": ["buffalo bills", "buf", "bills"],
    "bengals": ["cincinnati bengals", "cin", "bengals"],
    "ravens": ["baltimore ravens", "bal", "ravens"],
    "steelers": ["pittsburgh steelers", "pit", "steelers"],
    "browns": ["cleveland browns", "cle", "browns"],
    "chargers": ["los angeles chargers", "lac", "chargers"],
    "raiders": ["las vegas raiders", "lv", "raiders"],
    "broncos": ["denver broncos", "den", "broncos"],
    "texans": ["houston texans", "hou", "texans"],
    "colts": ["indianapolis colts", "ind", "colts"],
    "jaguars": ["jacksonville jaguars", "jax", "jaguars"],
    "titans": ["tennessee titans", "ten", "titans"],
    "patriots": ["new england patriots", "ne", "pats", "patriots"],
    "dolphins": ["miami dolphins", "mia", "dolphins"],
    "jets": ["new york jets", "nyj", "jets"],
    "giants": ["new york giants", "nyg", "giants"],
    "commanders": ["washington commanders", "was", "commanders"],
    "panthers": ["carolina panthers", "car", "panthers"],
    "falcons": ["atlanta falcons", "atl", "falcons"],
    "saints": ["new orleans saints", "no", "saints"],
    "buccaneers": ["tampa bay buccaneers", "tb", "bucs", "buccaneers"],
    "packers": ["green bay packers", "gb", "packers"],
    "bears": ["chicago bears", "chi", "bears"],
    "lions": ["detroit lions", "det", "lions"],
    "vikings": ["minnesota vikings", "min", "vikings"],
    "cardinals": ["arizona cardinals", "ari", "cards", "cardinals"],
    "rams": ["los angeles rams", "lar", "rams"],
    "seahawks": ["seattle seahawks", "sea", "hawks_nfl", "seahawks"],
    # NHL
    "bruins": ["boston bruins", "bos", "bruins"],
    "penguins": ["pittsburgh penguins", "pit", "pens", "penguins"],
    "predators": ["nashville predators", "nsh", "preds", "predators"],
    "capitals": ["washington capitals", "was", "caps", "capitals"],
    "blackhawks": ["chicago blackhawks", "chi", "hawks_nhl", "blackhawks"],
    "red wings": ["detroit red wings", "det", "wings", "red wings"],
}


class GameMarketAggregator:
    """
    Aggregates and classifies markets for sports games.

    Responsibilities:
    - Fetch sports game markets from Polymarket
    - Group markets by game
    - Classify market types
    - Extract teams, players, and thresholds
    """

    def __init__(
        self,
        api: PolymarketAPI,
        config: SportsPortfolioConfig,
    ):
        self.api = api
        self.config = config
        self._games_cache: Dict[str, SportsGame] = {}
        self._last_refresh: Optional[datetime] = None

    async def get_games(
        self,
        sports: Optional[List[str]] = None,
        hours_ahead: int = 48,
        refresh: bool = False,
    ) -> List[SportsGame]:
        """
        Get all sports games with their markets.

        Args:
            sports: List of sports to include (None = all enabled)
            hours_ahead: How far ahead to look for games
            refresh: Force refresh even if cache is fresh
        """
        # Check cache freshness
        if (
            not refresh
            and self._last_refresh
            and (datetime.now(timezone.utc) - self._last_refresh).seconds < self.config.market_refresh_seconds
        ):
            return list(self._games_cache.values())

        sports = sports or self.config.get_enabled_sports()
        logger.info(f"Fetching sports games for: {sports}")

        # Fetch sports markets from API
        all_markets = await self._fetch_sports_markets(sports)
        logger.info(f"Fetched {len(all_markets)} raw sports markets")

        # Group by game
        games = self._group_markets_by_game(all_markets)
        logger.info(f"Grouped into {len(games)} games")

        # Filter by time and market count
        cutoff = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
        filtered_games = []

        for game in games:
            if game.game_time > cutoff:
                continue
            if game.market_count < self.config.min_markets_per_game:
                continue
            filtered_games.append(game)

        # Fetch orderbook data for markets
        await self._enrich_with_orderbooks(filtered_games)

        # Update cache
        self._games_cache = {g.game_id: g for g in filtered_games}
        self._last_refresh = datetime.now(timezone.utc)

        logger.info(f"Returning {len(filtered_games)} games with sufficient markets")
        return filtered_games

    async def get_game(self, game_id: str) -> Optional[SportsGame]:
        """Get a specific game by ID."""
        if game_id in self._games_cache:
            return self._games_cache[game_id]

        # Try to fetch fresh
        games = await self.get_games(refresh=True)
        return self._games_cache.get(game_id)

    async def _fetch_sports_markets(self, sports: List[str]) -> List[Market]:
        """Fetch sports markets from API."""
        markets = []

        # Fetch from restricted sports endpoint
        for sport in sports:
            try:
                raw_markets = await self.api.fetch_sports_games(
                    days_ahead=3,
                    sports=[sport],
                )
                for raw in raw_markets:
                    parsed = self.api.parse_market(raw)
                    if parsed:
                        markets.append(parsed)
            except Exception as e:
                logger.warning(f"Failed to fetch {sport} markets: {e}")

        # Also check regular markets for sports category
        try:
            all_markets = await self.api.fetch_all_markets(active=True, max_markets=5000)
            for raw in all_markets:
                category = raw.get("category", "").lower()
                if category in ["sports", "nba", "nfl", "nhl", "mlb", "soccer", "football"]:
                    parsed = self.api.parse_market(raw)
                    if parsed:
                        markets.append(parsed)
        except Exception as e:
            logger.warning(f"Failed to fetch regular sports markets: {e}")

        # Deduplicate by condition_id
        seen = set()
        unique = []
        for m in markets:
            if m.condition_id not in seen:
                seen.add(m.condition_id)
                unique.append(m)

        return unique

    def _group_markets_by_game(self, markets: List[Market]) -> List[SportsGame]:
        """Group markets into games based on teams and timing."""
        games: Dict[str, SportsGame] = {}

        for market in markets:
            # Try to extract game info from question
            game_info = self._extract_game_info(market)
            if not game_info:
                continue

            game_key, sport, home_team, away_team, game_time = game_info

            # Get or create game
            if game_key not in games:
                games[game_key] = SportsGame(
                    game_id=game_key,
                    sport=sport,
                    home_team=home_team,
                    away_team=away_team,
                    game_time=game_time,
                    slug=market.slug,
                )

            game = games[game_key]

            # Classify and add market
            for token in market.tokens:
                market_type = self._classify_market_type(market.question)
                player = self._extract_player(market.question)
                threshold = self._extract_threshold(market.question)

                # For winner/moneyline markets, use token.outcome as team
                # For spread markets, extract from the question
                if market_type in (MarketType.WINNER, MarketType.MONEYLINE):
                    # The token outcome is the team name
                    team = self._match_team_name(token.outcome, home_team, away_team)
                else:
                    team = self._extract_team(market.question, home_team, away_team)

                # For player props, try to match player to team
                if market_type == MarketType.PLAYER_PROP and not team and player:
                    # TODO: Look up player -> team mapping
                    # For now, use the game's teams from question context
                    team = self._extract_team(market.question, home_team, away_team)

                game_market = GameMarket(
                    market_id=market.condition_id,
                    token_id=token.token_id,
                    question=market.question,
                    outcome=token.outcome,
                    market_type=market_type,
                    price=token.price,
                    team=team,
                    player=player,
                    threshold=threshold,
                )
                game.markets.append(game_market)

        return list(games.values())

    def _extract_game_info(
        self,
        market: Market,
    ) -> Optional[Tuple[str, Sport, str, str, datetime]]:
        """Extract game information from a market."""
        question = market.question.lower()

        # Try to extract teams from question
        # Pattern: "Team A vs Team B" or "Team A @ Team B"
        vs_match = re.search(
            r"([a-z\s]+)\s+(?:vs\.?|@|at|versus)\s+([a-z\s]+)",
            question,
        )

        if vs_match:
            away_team = vs_match.group(1).strip()
            home_team = vs_match.group(2).strip()
        else:
            # Try slug-based extraction
            if market.slug:
                slug_parts = market.slug.split("-")
                if len(slug_parts) >= 3:
                    # Pattern: sport-team1-team2-date
                    sport_str = slug_parts[0]
                    away_team = slug_parts[1]
                    home_team = slug_parts[2]
                else:
                    return None
            else:
                return None

        # Normalize team names
        home_team = self._normalize_team_name(home_team)
        away_team = self._normalize_team_name(away_team)

        if not home_team or not away_team:
            return None

        # Determine sport
        sport = self._detect_sport(market.question, market.slug, market.category)

        # Game time from market end date
        game_time = market.end_date

        # Create unique game key
        game_key = f"{sport.value}_{away_team}_{home_team}_{game_time.strftime('%Y%m%d')}"

        return game_key, sport, home_team, away_team, game_time

    def _detect_sport(
        self,
        question: str,
        slug: Optional[str],
        category: Optional[str],
    ) -> Sport:
        """Detect sport from market data."""
        text = f"{question} {slug or ''} {category or ''}".lower()

        if any(x in text for x in ["nba", "basketball", "lakers", "celtics", "warriors"]):
            return Sport.NBA
        if any(x in text for x in ["nfl", "football", "chiefs", "eagles", "cowboys"]):
            return Sport.NFL
        if any(x in text for x in ["nhl", "hockey", "bruins", "rangers"]):
            return Sport.NHL
        if any(x in text for x in ["mlb", "baseball", "yankees", "dodgers"]):
            return Sport.MLB
        if any(x in text for x in ["soccer", "premier league", "champions league", "la liga"]):
            return Sport.SOCCER

        return Sport.UNKNOWN

    def _normalize_team_name(self, name: str) -> str:
        """Normalize team name to canonical form."""
        name = name.lower().strip()

        # Check aliases
        for canonical, aliases in TEAM_ALIASES.items():
            if name == canonical or name in aliases:
                return canonical

        # Remove common suffixes
        name = re.sub(r"\s*(fc|united|city)$", "", name)

        return name

    def _classify_market_type(self, question: str) -> MarketType:
        """Classify market type from question text."""
        # Keep original case for some patterns, use both for matching
        question_lower = question.lower()

        for market_type, patterns in MARKET_TYPE_PATTERNS.items():
            for pattern in patterns:
                # Try matching with original case first (for patterns with [A-Z])
                # then with lowercase
                if re.search(pattern, question, re.IGNORECASE):
                    return market_type
                if re.search(pattern, question_lower):
                    return market_type

        return MarketType.UNKNOWN

    def _match_team_name(
        self,
        outcome: str,
        home_team: str,
        away_team: str,
    ) -> Optional[str]:
        """Match an outcome/team name to home or away team."""
        outcome_lower = outcome.lower().strip()

        # Direct match
        if outcome_lower == home_team.lower():
            return home_team
        if outcome_lower == away_team.lower():
            return away_team

        # Check aliases for both teams
        for team in [home_team, away_team]:
            team_lower = team.lower()
            if team_lower in TEAM_ALIASES:
                aliases = TEAM_ALIASES[team_lower]
                if outcome_lower in [a.lower() for a in aliases]:
                    return team

            # Also check if outcome contains the team name
            if team_lower in outcome_lower or outcome_lower in team_lower:
                return team

        return None

    def _extract_team(
        self,
        question: str,
        home_team: str,
        away_team: str,
    ) -> Optional[str]:
        """Extract which team a market relates to."""
        question_lower = question.lower()

        # Check for team mentions
        if home_team.lower() in question_lower:
            return home_team
        if away_team.lower() in question_lower:
            return away_team

        # Check aliases
        for team in [home_team, away_team]:
            aliases = TEAM_ALIASES.get(team.lower(), [])
            for alias in aliases:
                if alias in question_lower:
                    return team

        return None

    def _extract_player(self, question: str) -> Optional[str]:
        """Extract player name from question."""
        # Pattern: "Will [Player Name] score..."
        match = re.search(
            r"will\s+([A-Z][a-z]+\s+[A-Z][a-z]+)",
            question,
        )
        if match:
            return match.group(1)

        # Pattern: "[Player Name] to score..."
        match = re.search(
            r"([A-Z][a-z]+\s+[A-Z][a-z]+)\s+(?:to\s+)?(?:score|pass|rush|receive)",
            question,
        )
        if match:
            return match.group(1)

        return None

    def _extract_threshold(self, question: str) -> Optional[float]:
        """Extract numeric threshold from question (for over/under, spreads)."""
        # Pattern: "over 220.5" or "under 45"
        match = re.search(r"(?:over|under|o/u)\s*(\d+\.?\d*)", question.lower())
        if match:
            return float(match.group(1))

        # Pattern: "+5.5" or "-3.5"
        match = re.search(r"([+-]\d+\.?\d*)", question)
        if match:
            return float(match.group(1))

        return None

    async def _enrich_with_orderbooks(self, games: List[SportsGame]) -> None:
        """Fetch orderbook data for all markets in games."""
        all_token_ids = []
        token_to_market: Dict[str, GameMarket] = {}

        for game in games:
            for market in game.markets:
                all_token_ids.append(market.token_id)
                token_to_market[market.token_id] = market

        # Fetch orderbooks in batches
        batch_size = 20
        for i in range(0, len(all_token_ids), batch_size):
            batch = all_token_ids[i:i + batch_size]
            tasks = [self.api.fetch_orderbook(tid) for tid in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for token_id, result in zip(batch, results):
                if isinstance(result, Exception):
                    continue
                if result and token_id in token_to_market:
                    market = token_to_market[token_id]
                    market.bid = result.best_bid
                    market.ask = result.best_ask
                    market.bid_size = result.bid_size
                    market.ask_size = result.ask_size

        logger.debug(f"Enriched {len(all_token_ids)} markets with orderbook data")

    def get_structural_correlation(
        self,
        market_a: GameMarket,
        market_b: GameMarket,
        game: SportsGame,
    ) -> Tuple[float, float]:
        """
        Get structural (logical) correlation between two markets.

        Returns: (correlation, confidence)
        """
        # Same outcome in winner market: perfect negative correlation
        if (
            market_a.market_type in (MarketType.WINNER, MarketType.MONEYLINE)
            and market_b.market_type in (MarketType.WINNER, MarketType.MONEYLINE)
        ):
            if market_a.team and market_b.team and market_a.team != market_b.team:
                return -1.0, 1.0  # Perfect negative, high confidence

        # Player props for different players: moderate negative
        if (
            market_a.market_type == MarketType.PLAYER_PROP
            and market_b.market_type == MarketType.PLAYER_PROP
        ):
            if market_a.player and market_b.player and market_a.player != market_b.player:
                # Same team players: weak negative (compete for ball)
                if market_a.team == market_b.team:
                    return -0.3, 0.7
                # Different team players: near zero
                return 0.0, 0.6

        # Winner and team total: positive correlation
        if market_a.market_type == MarketType.WINNER and market_b.market_type == MarketType.TOTAL:
            if market_a.team and "over" in market_b.question.lower():
                return 0.4, 0.6
            if market_a.team and "under" in market_b.question.lower():
                return -0.2, 0.5

        # Over/under on same total: perfect negative
        if market_a.market_type == MarketType.TOTAL and market_b.market_type == MarketType.TOTAL:
            if (
                market_a.threshold == market_b.threshold
                and market_a.market_id == market_b.market_id
            ):
                return -1.0, 1.0

        # Default: weak correlation, low confidence
        return 0.0, 0.3
