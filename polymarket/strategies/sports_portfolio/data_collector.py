"""
Historical Sports Data Collector.

Fetches and processes historical sports market data from Polymarket
for training ML correlation models.
"""

import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field, asdict
import json

from polymarket.core.api import PolymarketAPI
from polymarket.core.models import Market

from .models import (
    Sport,
    MarketType,
    HistoricalGameData,
    GameMarket,
)

logger = logging.getLogger(__name__)


@dataclass
class ResolvedMarket:
    """A resolved sports market."""
    market_id: str
    token_id: str
    question: str
    outcome: str
    resolved_yes: bool          # True if this outcome resolved YES
    final_price: float          # Price at resolution (0 or 1)

    # Classification
    sport: Sport
    market_type: MarketType
    team: Optional[str] = None
    player: Optional[str] = None

    # Timing
    end_date: Optional[datetime] = None

    # Game grouping
    game_key: Optional[str] = None
    home_team: Optional[str] = None
    away_team: Optional[str] = None


@dataclass
class GameResolution:
    """Resolved game with all its markets."""
    game_key: str
    sport: Sport
    home_team: str
    away_team: str
    game_date: datetime

    # Resolved markets
    markets: List[ResolvedMarket] = field(default_factory=list)

    # Computed correlations
    realized_correlations: Dict[Tuple[str, str], float] = field(default_factory=dict)

    @property
    def market_count(self) -> int:
        return len(self.markets)

    def compute_correlations(self) -> Dict[Tuple[str, str], float]:
        """Compute realized correlations between all market pairs."""
        correlations = {}

        for i, m1 in enumerate(self.markets):
            for m2 in self.markets[i+1:]:
                # Realized correlation: both resolve same (+1) or opposite (-1)
                if m1.resolved_yes == m2.resolved_yes:
                    corr = 1.0
                else:
                    corr = -1.0

                key = tuple(sorted([m1.token_id, m2.token_id]))
                correlations[key] = corr

        self.realized_correlations = correlations
        return correlations


class SportsDataCollector:
    """
    Collects historical sports market data for ML training.
    """

    def __init__(
        self,
        api: PolymarketAPI,
        db_path: Path = None,
    ):
        self.api = api
        self.db_path = db_path or Path("data/sports_training_data.db")
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite database for storing training data."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS resolved_markets (
                    token_id TEXT PRIMARY KEY,
                    market_id TEXT,
                    question TEXT,
                    outcome TEXT,
                    resolved_yes INTEGER,
                    final_price REAL,
                    sport TEXT,
                    market_type TEXT,
                    team TEXT,
                    player TEXT,
                    end_date TEXT,
                    game_key TEXT,
                    home_team TEXT,
                    away_team TEXT,
                    collected_at TEXT
                );

                CREATE TABLE IF NOT EXISTS game_resolutions (
                    game_key TEXT PRIMARY KEY,
                    sport TEXT,
                    home_team TEXT,
                    away_team TEXT,
                    game_date TEXT,
                    market_count INTEGER,
                    correlations_json TEXT,
                    collected_at TEXT
                );

                CREATE TABLE IF NOT EXISTS training_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_key TEXT,
                    sport TEXT,
                    token_a TEXT,
                    token_b TEXT,
                    market_type_a TEXT,
                    market_type_b TEXT,
                    same_team INTEGER,
                    same_player INTEGER,
                    realized_correlation REAL,
                    created_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_markets_game ON resolved_markets(game_key);
                CREATE INDEX IF NOT EXISTS idx_markets_sport ON resolved_markets(sport);
                CREATE INDEX IF NOT EXISTS idx_samples_sport ON training_samples(sport);
            """)

    async def collect_historical_data(
        self,
        days_back: int = 90,
        sports: List[str] = None,
    ) -> Dict[str, int]:
        """
        Collect historical resolved sports markets.

        Returns: Stats dict with counts per sport
        """
        sports = sports or ["nba", "nfl", "nhl", "mlb", "soccer"]

        logger.info(f"Collecting historical sports data for past {days_back} days")
        logger.info(f"Sports: {sports}")

        # Fetch closed/resolved markets
        raw_markets = await self.api.fetch_closed_markets(
            days=days_back,
            resolved_only=True,
        )

        logger.info(f"Fetched {len(raw_markets)} closed markets total")

        # Filter to sports markets
        sports_markets = []
        for raw in raw_markets:
            category = (raw.get("category") or "").lower()
            question = (raw.get("question") or "").lower()
            slug = (raw.get("slug") or raw.get("market_slug") or "").lower()

            # Check if sports-related
            is_sports = False
            detected_sport = None

            for sport in sports:
                if sport in category or sport in slug:
                    is_sports = True
                    detected_sport = sport
                    break

            # Also check keywords
            sport_keywords = {
                "nba": ["basketball", "lakers", "celtics", "warriors", "nets", "knicks", "bulls"],
                "nfl": ["football", "chiefs", "eagles", "cowboys", "49ers", "patriots"],
                "nhl": ["hockey", "bruins", "rangers", "penguins", "canadiens"],
                "mlb": ["baseball", "yankees", "dodgers", "red sox", "cubs"],
                "soccer": ["premier league", "la liga", "champions league", "world cup", "manchester", "liverpool"],
            }

            if not is_sports:
                for sport, keywords in sport_keywords.items():
                    if sport not in sports:
                        continue
                    if any(kw in question or kw in slug for kw in keywords):
                        is_sports = True
                        detected_sport = sport
                        break

            if is_sports:
                raw["_detected_sport"] = detected_sport
                sports_markets.append(raw)

        logger.info(f"Found {len(sports_markets)} sports markets")

        # Parse and classify markets
        resolved_markets = []
        for raw in sports_markets:
            parsed = self._parse_resolved_market(raw)
            if parsed:
                resolved_markets.extend(parsed)

        logger.info(f"Parsed {len(resolved_markets)} resolved market outcomes")

        # Group by game
        games = self._group_into_games(resolved_markets)
        logger.info(f"Grouped into {len(games)} games")

        # Store in database
        stats = self._store_data(resolved_markets, games)

        # Generate training samples
        sample_count = self._generate_training_samples(games)
        stats["training_samples"] = sample_count

        logger.info(f"Collection complete: {stats}")
        return stats

    def _parse_resolved_market(self, raw: dict) -> List[ResolvedMarket]:
        """Parse a raw market into resolved market objects."""
        results = []

        market_id = raw.get("conditionId") or raw.get("condition_id") or ""
        question = raw.get("question") or ""
        sport_str = raw.get("_detected_sport", "unknown")

        try:
            sport = Sport(sport_str)
        except ValueError:
            sport = Sport.UNKNOWN

        # Parse tokens and their resolutions
        clob_token_ids = raw.get("clobTokenIds", [])
        outcomes = raw.get("outcomes", [])
        prices = raw.get("outcomePrices", [])

        # Handle JSON strings
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except:
                clob_token_ids = []

        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except:
                outcomes = []

        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except:
                prices = []

        # Parse end date
        end_date = None
        end_str = raw.get("endDate") or raw.get("end_date_iso")
        if end_str:
            try:
                end_str = str(end_str).replace("Z", "+00:00")
                if "T" not in end_str:
                    end_str += "T00:00:00+00:00"
                end_date = datetime.fromisoformat(end_str)
            except:
                pass

        # Extract game info
        game_key, home_team, away_team = self._extract_game_info(question, raw.get("slug", ""))

        # Classify market type
        market_type = self._classify_market_type(question)
        team = self._extract_team_from_question(question, home_team, away_team)
        player = self._extract_player_from_question(question)

        for i, token_id in enumerate(clob_token_ids):
            outcome = outcomes[i] if i < len(outcomes) else f"Outcome {i}"
            price = float(prices[i]) if i < len(prices) and prices[i] else 0.0

            # Determine if resolved YES (price ~= 1.0)
            resolved_yes = price >= 0.99

            results.append(ResolvedMarket(
                market_id=market_id,
                token_id=str(token_id),
                question=question,
                outcome=str(outcome),
                resolved_yes=resolved_yes,
                final_price=price,
                sport=sport,
                market_type=market_type,
                team=team,
                player=player,
                end_date=end_date,
                game_key=game_key,
                home_team=home_team,
                away_team=away_team,
            ))

        return results

    def _extract_game_info(
        self,
        question: str,
        slug: str
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Extract game key, home team, away team from question/slug."""
        question_lower = question.lower()
        slug_lower = slug.lower()

        # Try slug pattern: sport-team1-team2-date
        slug_parts = slug_lower.split("-")
        if len(slug_parts) >= 4:
            # Check if looks like sport-team-team-date
            sport = slug_parts[0]
            if sport in ["nba", "nfl", "nhl", "mlb"]:
                team1 = slug_parts[1]
                team2 = slug_parts[2]
                date_part = slug_parts[3] if len(slug_parts) > 3 else ""

                game_key = f"{sport}_{team1}_{team2}_{date_part}"
                return game_key, team2, team1  # team2 often home in slug format

        # Try question pattern: Team A vs Team B
        vs_match = re.search(
            r"([a-z\s]+)\s+(?:vs\.?|@|versus)\s+([a-z\s]+)",
            question_lower,
        )
        if vs_match:
            team1 = vs_match.group(1).strip()[:20]  # Truncate long names
            team2 = vs_match.group(2).strip()[:20]
            game_key = f"game_{team1}_{team2}"
            return game_key, team2, team1

        return None, None, None

    def _classify_market_type(self, question: str) -> MarketType:
        """Classify market type from question."""
        q = question.lower()

        if any(x in q for x in ["win", "beat", "defeat", "victory"]):
            return MarketType.WINNER
        if any(x in q for x in ["spread", "by ", "+", "-"]) and re.search(r'\d+', q):
            return MarketType.SPREAD
        if any(x in q for x in ["over", "under", "total", "combined"]):
            return MarketType.TOTAL
        if any(x in q for x in ["score", "points", "goals", "touchdown", "assist", "rebound"]):
            return MarketType.PLAYER_PROP
        if any(x in q for x in ["first to", "overtime", "shutout"]):
            return MarketType.GAME_PROP
        if any(x in q for x in ["quarter", "half", "halftime"]):
            return MarketType.QUARTER_HALF

        return MarketType.UNKNOWN

    def _extract_team_from_question(
        self,
        question: str,
        home_team: Optional[str],
        away_team: Optional[str],
    ) -> Optional[str]:
        """Extract team reference from question."""
        q = question.lower()

        if home_team and home_team.lower() in q:
            return home_team
        if away_team and away_team.lower() in q:
            return away_team

        return None

    def _extract_player_from_question(self, question: str) -> Optional[str]:
        """Extract player name from question."""
        # Pattern: "Will [Name] score..."
        match = re.search(r"will\s+([A-Z][a-z]+\s+[A-Z][a-z]+)", question)
        if match:
            return match.group(1)

        return None

    def _group_into_games(
        self,
        markets: List[ResolvedMarket],
    ) -> List[GameResolution]:
        """Group markets into games."""
        games_dict: Dict[str, GameResolution] = {}

        for market in markets:
            if not market.game_key:
                continue

            if market.game_key not in games_dict:
                games_dict[market.game_key] = GameResolution(
                    game_key=market.game_key,
                    sport=market.sport,
                    home_team=market.home_team or "unknown",
                    away_team=market.away_team or "unknown",
                    game_date=market.end_date or datetime.now(timezone.utc),
                )

            games_dict[market.game_key].markets.append(market)

        # Compute correlations for each game
        for game in games_dict.values():
            if game.market_count >= 2:
                game.compute_correlations()

        return list(games_dict.values())

    def _store_data(
        self,
        markets: List[ResolvedMarket],
        games: List[GameResolution],
    ) -> Dict[str, int]:
        """Store data in SQLite database."""
        stats = {"markets": 0, "games": 0}

        with sqlite3.connect(self.db_path) as conn:
            now = datetime.now(timezone.utc).isoformat()

            # Store markets
            for market in markets:
                try:
                    conn.execute("""
                        INSERT OR REPLACE INTO resolved_markets
                        (token_id, market_id, question, outcome, resolved_yes,
                         final_price, sport, market_type, team, player, end_date,
                         game_key, home_team, away_team, collected_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        market.token_id,
                        market.market_id,
                        market.question,
                        market.outcome,
                        1 if market.resolved_yes else 0,
                        market.final_price,
                        market.sport.value,
                        market.market_type.value,
                        market.team,
                        market.player,
                        market.end_date.isoformat() if market.end_date else None,
                        market.game_key,
                        market.home_team,
                        market.away_team,
                        now,
                    ))
                    stats["markets"] += 1
                except Exception as e:
                    logger.debug(f"Failed to store market: {e}")

            # Store games
            for game in games:
                if game.market_count < 2:
                    continue

                try:
                    corr_json = json.dumps({
                        f"{k[0]}_{k[1]}": v
                        for k, v in game.realized_correlations.items()
                    })

                    conn.execute("""
                        INSERT OR REPLACE INTO game_resolutions
                        (game_key, sport, home_team, away_team, game_date,
                         market_count, correlations_json, collected_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        game.game_key,
                        game.sport.value,
                        game.home_team,
                        game.away_team,
                        game.game_date.isoformat(),
                        game.market_count,
                        corr_json,
                        now,
                    ))
                    stats["games"] += 1
                except Exception as e:
                    logger.debug(f"Failed to store game: {e}")

            conn.commit()

        return stats

    def _generate_training_samples(
        self,
        games: List[GameResolution],
    ) -> int:
        """Generate training samples from game correlations."""
        count = 0

        with sqlite3.connect(self.db_path) as conn:
            now = datetime.now(timezone.utc).isoformat()

            for game in games:
                if game.market_count < 2:
                    continue

                # Create market lookup
                market_lookup = {m.token_id: m for m in game.markets}

                for (token_a, token_b), correlation in game.realized_correlations.items():
                    m_a = market_lookup.get(token_a)
                    m_b = market_lookup.get(token_b)

                    if not m_a or not m_b:
                        continue

                    same_team = (m_a.team and m_b.team and m_a.team == m_b.team)
                    same_player = (m_a.player and m_b.player and m_a.player == m_b.player)

                    try:
                        conn.execute("""
                            INSERT INTO training_samples
                            (game_key, sport, token_a, token_b, market_type_a,
                             market_type_b, same_team, same_player, realized_correlation, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            game.game_key,
                            game.sport.value,
                            token_a,
                            token_b,
                            m_a.market_type.value,
                            m_b.market_type.value,
                            1 if same_team else 0,
                            1 if same_player else 0,
                            correlation,
                            now,
                        ))
                        count += 1
                    except Exception as e:
                        logger.debug(f"Failed to store sample: {e}")

            conn.commit()

        return count

    def get_training_data(
        self,
        sport: Optional[str] = None,
    ) -> List[Dict]:
        """Retrieve training samples from database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            if sport:
                rows = conn.execute("""
                    SELECT * FROM training_samples WHERE sport = ?
                """, (sport,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM training_samples
                """).fetchall()

            return [dict(row) for row in rows]

    def get_stats(self) -> Dict[str, any]:
        """Get database statistics."""
        with sqlite3.connect(self.db_path) as conn:
            stats = {}

            # Total counts
            stats["total_markets"] = conn.execute(
                "SELECT COUNT(*) FROM resolved_markets"
            ).fetchone()[0]

            stats["total_games"] = conn.execute(
                "SELECT COUNT(*) FROM game_resolutions"
            ).fetchone()[0]

            stats["total_samples"] = conn.execute(
                "SELECT COUNT(*) FROM training_samples"
            ).fetchone()[0]

            # Per sport
            stats["by_sport"] = {}
            for row in conn.execute("""
                SELECT sport, COUNT(*) as cnt FROM training_samples GROUP BY sport
            """).fetchall():
                stats["by_sport"][row[0]] = row[1]

            # Per market type pair
            stats["by_market_types"] = {}
            for row in conn.execute("""
                SELECT market_type_a, market_type_b, COUNT(*) as cnt, AVG(realized_correlation) as avg_corr
                FROM training_samples
                GROUP BY market_type_a, market_type_b
                ORDER BY cnt DESC
                LIMIT 20
            """).fetchall():
                key = f"{row[0]}_vs_{row[1]}"
                stats["by_market_types"][key] = {
                    "count": row[2],
                    "avg_correlation": round(row[3], 3),
                }

            return stats
