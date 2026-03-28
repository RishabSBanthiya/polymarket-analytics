"""
Load real prediction-market trade data and reconstruct orderbook snapshots.

Supports data from Jon-Becker/prediction-market-analysis (Parquet format)
for both Polymarket (on-chain CTF Exchange fills) and Kalshi (REST API trades).

Trade data is converted into approximate orderbook snapshots via time-windowed
aggregation of trade flow.

Requires optional dependencies: pandas, pyarrow
Install with: pip install omnitrade[backtest-data]
"""

from __future__ import annotations

import json
import logging
import os
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from ..core.models import OrderbookSnapshot, OrderbookLevel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class NormalizedTrade:
    """A single trade normalized from exchange data."""

    asset_id: str
    side: str  # "BUY" or "SELL"
    price: float  # 0-1 normalized
    size: float
    timestamp: datetime
    condition_id: str = ""
    exchange: str = ""  # "polymarket" or "kalshi"
    trade_id: str = ""
    fee: float = 0.0


@dataclass
class MarketInfo:
    """Metadata about a prediction market."""

    condition_id: str
    question: str
    token_ids: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    volume: float = 0.0
    active: bool = True
    closed: bool = False
    exchange: str = ""
    slug: str = ""
    liquidity: float = 0.0
    outcome_prices: list[float] = field(default_factory=list)
    end_date: Optional[datetime] = None
    created_at: Optional[datetime] = None
    # Kalshi-specific fields
    event_ticker: str = ""
    market_type: str = ""
    status: str = ""
    yes_bid: Optional[int] = None
    yes_ask: Optional[int] = None
    last_price: Optional[int] = None
    open_interest: int = 0
    result: str = ""


# ---------------------------------------------------------------------------
# Block timestamp lookup (Polymarket on-chain data)
# ---------------------------------------------------------------------------

# Fallback: Ethereum genesis-ish epoch + ~2s block time
_FALLBACK_EPOCH = 1672531200
_FALLBACK_BLOCK_TIME = 2


class BlockTimestampLookup:
    """Maps Ethereum block numbers to UTC timestamps.

    Reads ``blocks/*.parquet`` files containing ``block_number`` (int) and
    ``timestamp`` (ISO-8601 string) columns.  Provides exact lookup for
    blocks in the table and linear interpolation for blocks between known
    entries.
    """

    def __init__(self, blocks_dir: str) -> None:
        self._blocks: list[int] = []
        self._timestamps: list[float] = []  # unix epoch seconds
        self._loaded = False
        self._blocks_dir = blocks_dir

    # -- lazy loading -------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True

        if not os.path.isdir(self._blocks_dir):
            logger.debug("Blocks directory not found: %s", self._blocks_dir)
            return

        try:
            import pyarrow.parquet as pq
        except ImportError:
            logger.warning("pyarrow not installed — block lookup disabled")
            return

        t0 = time.monotonic()
        block_files = sorted(f for f in os.listdir(self._blocks_dir) if f.endswith(".parquet"))
        logger.info("  Loading block timestamps from %d files...", len(block_files))

        block_ts_pairs: list[tuple[int, float]] = []
        for fidx, fname in enumerate(block_files):
            fpath = os.path.join(self._blocks_dir, fname)
            try:
                table = pq.read_table(fpath)
                blocks = table.column("block_number").to_pylist()
                timestamps = table.column("timestamp").to_pylist()
                for blk, ts_raw in zip(blocks, timestamps):
                    if isinstance(ts_raw, str):
                        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        epoch = dt.timestamp()
                    elif isinstance(ts_raw, (int, float)):
                        epoch = float(ts_raw)
                    else:
                        continue
                    block_ts_pairs.append((int(blk), epoch))
            except Exception:
                logger.debug("Failed to read block file: %s", fpath, exc_info=True)
                continue
            if (fidx + 1) % 50 == 0:
                logger.info("    block files: %d/%d read (%d mappings so far)",
                            fidx + 1, len(block_files), len(block_ts_pairs))

        if not block_ts_pairs:
            logger.info("  No block timestamp data found")
            return

        block_ts_pairs.sort(key=lambda p: p[0])
        # Deduplicate by block number (keep first occurrence)
        seen: set[int] = set()
        deduped: list[tuple[int, float]] = []
        for blk, ts in block_ts_pairs:
            if blk not in seen:
                seen.add(blk)
                deduped.append((blk, ts))

        self._blocks = [p[0] for p in deduped]
        self._timestamps = [p[1] for p in deduped]
        elapsed = time.monotonic() - t0
        logger.info("  Loaded %d block timestamps in %.1fs", len(self._blocks), elapsed)

    # -- public API ---------------------------------------------------------

    @property
    def has_data(self) -> bool:
        """Return True if block-timestamp data has been loaded."""
        self._ensure_loaded()
        return len(self._blocks) > 0

    def lookup(self, block_number: int) -> Optional[float]:
        """Exact lookup.  Returns unix epoch seconds or None."""
        self._ensure_loaded()
        if not self._blocks:
            return None
        idx = bisect_left(self._blocks, block_number)
        if idx < len(self._blocks) and self._blocks[idx] == block_number:
            return self._timestamps[idx]
        return None

    def interpolate(self, block_number: int) -> float:
        """Return a timestamp for *block_number*, interpolating if necessary.

        Falls back to ``_FALLBACK_EPOCH + block * _FALLBACK_BLOCK_TIME``
        only when no block data is available at all.
        """
        self._ensure_loaded()
        if not self._blocks:
            return _FALLBACK_EPOCH + block_number * _FALLBACK_BLOCK_TIME

        # Exact hit
        exact = self.lookup(block_number)
        if exact is not None:
            return exact

        # Clamp to bounds
        if block_number <= self._blocks[0]:
            return self._timestamps[0]
        if block_number >= self._blocks[-1]:
            return self._timestamps[-1]

        # Linear interpolation between the two surrounding known blocks
        right = bisect_right(self._blocks, block_number)
        left = right - 1
        blk_lo, blk_hi = self._blocks[left], self._blocks[right]
        ts_lo, ts_hi = self._timestamps[left], self._timestamps[right]
        frac = (block_number - blk_lo) / (blk_hi - blk_lo)
        return ts_lo + frac * (ts_hi - ts_lo)


# ---------------------------------------------------------------------------
# CTF fill parser (Polymarket on-chain events)
# ---------------------------------------------------------------------------

def parse_ctf_fill(
    maker_amount: int,
    taker_amount: int,
    maker_asset_id: str,
    taker_asset_id: str,
    fee: int = 0,
) -> Optional[tuple[str, str, float, float, float]]:
    """Parse a single CTF Exchange OrderFilled event.

    Returns ``(asset_id, side, price, size, fee_usd)`` or ``None`` if the
    fill is invalid (zero amounts, nonsensical price, etc.).

    Logic:
        - ``maker_asset_id == "0"`` means maker gave USDC, taker gave
          outcome tokens -> taker is SELLING tokens (aggressive seller).
        - ``taker_asset_id == "0"`` means taker gave USDC, maker gave
          outcome tokens -> taker is BUYING tokens (aggressive buyer).
        - Both amounts have 6 decimal places (divide by 1e6).
        - ``price = usdc_amount / token_amount``, clamped to (0.001, 0.999).
    """
    if maker_amount <= 0 or taker_amount <= 0:
        return None

    maker_usd = maker_amount / 1e6
    taker_usd = taker_amount / 1e6
    fee_usd = fee / 1e6

    if str(maker_asset_id) == "0":
        # Maker gave USDC -> taker gave tokens -> taker is SELLING
        usdc_amount = maker_usd
        token_amount = taker_usd
        asset_id = str(taker_asset_id)
        side = "SELL"
    elif str(taker_asset_id) == "0":
        # Taker gave USDC -> maker gave tokens -> taker is BUYING
        usdc_amount = taker_usd
        token_amount = maker_usd
        asset_id = str(maker_asset_id)
        side = "BUY"
    else:
        # Neither side is USDC — skip
        return None

    if token_amount == 0:
        return None

    price = usdc_amount / token_amount
    price = max(0.001, min(0.999, price))

    return (asset_id, side, price, token_amount, fee_usd)


# ---------------------------------------------------------------------------
# Helper: import pandas with a friendly error message
# ---------------------------------------------------------------------------

def _import_pandas():
    """Import and return the pandas module, or raise ImportError."""
    try:
        import pandas as pd
        return pd
    except ImportError:
        raise ImportError(
            "pandas and pyarrow are required for real data loading. "
            "Install with: pip install 'omnitrade[backtest-data]'"
        )


# ---------------------------------------------------------------------------
# Helper: parse a JSON-encoded field from a DataFrame row
# ---------------------------------------------------------------------------

def _parse_json_field(value, fallback=None):
    """Parse a JSON string or pass through a list.  Returns *fallback* on failure."""
    if fallback is None:
        fallback = []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return list(fallback)


def _safe_parse_datetime(value) -> Optional[datetime]:
    """Best-effort parse of a datetime-ish value to an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    try:
        s = str(value).strip()
        if not s:
            return None
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Polymarket data loader
# ---------------------------------------------------------------------------

class PolymarketDataLoader:
    """Reads Parquet trade/market files from a local Polymarket data directory.

    Expected directory layout (Jon-Becker/prediction-market-analysis)::

        data_dir/
            trades/   *.parquet  (raw CTF Exchange OrderFilled events)
            markets/  *.parquet  (Polymarket market metadata)
            blocks/   *.parquet  (block_number -> timestamp mapping, optional)
    """

    def __init__(self, data_dir: str) -> None:
        self._data_dir = data_dir
        self._trades_dir = os.path.join(data_dir, "trades")
        self._markets_dir = os.path.join(data_dir, "markets")
        self._blocks_dir = os.path.join(data_dir, "blocks")

        if not os.path.isdir(self._trades_dir):
            raise FileNotFoundError(f"Trades directory not found: {self._trades_dir}")
        if not os.path.isdir(self._markets_dir):
            raise FileNotFoundError(f"Markets directory not found: {self._markets_dir}")

        self._markets_cache: Optional[dict[str, MarketInfo]] = None
        self._token_to_condition: dict[str, str] = {}
        self._block_lookup: Optional[BlockTimestampLookup] = None

    # -- internal helpers ---------------------------------------------------

    def _get_block_lookup(self) -> BlockTimestampLookup:
        """Lazy-create the block timestamp lookup."""
        if self._block_lookup is None:
            self._block_lookup = BlockTimestampLookup(self._blocks_dir)
        return self._block_lookup

    # -- markets ------------------------------------------------------------

    @property
    def _cache_path(self) -> str:
        """Path to the JSON market metadata cache file."""
        return os.path.join(self._data_dir, ".market_cache.json")

    def _is_cache_fresh(self) -> bool:
        """Return True if the disk cache exists and is newer than all market parquet files."""
        cache = self._cache_path
        if not os.path.isfile(cache):
            return False
        cache_mtime = os.path.getmtime(cache)
        for fname in os.listdir(self._markets_dir):
            if fname.endswith(".parquet"):
                fpath = os.path.join(self._markets_dir, fname)
                if os.path.getmtime(fpath) > cache_mtime:
                    return False
        return True

    def _load_markets_from_cache(self) -> Optional[dict[str, MarketInfo]]:
        """Try to load market metadata from the JSON disk cache."""
        try:
            with open(self._cache_path, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError):
            return None

        markets: dict[str, MarketInfo] = {}
        for cid, entry in data.items():
            token_ids = entry.get("token_ids", [])
            info = MarketInfo(
                condition_id=cid,
                question=entry.get("question", ""),
                outcomes=entry.get("outcomes", []),
                outcome_prices=entry.get("outcome_prices", []),
                volume=entry.get("volume", 0.0),
                liquidity=entry.get("liquidity", 0.0),
                active=entry.get("active", True),
                closed=entry.get("closed", False),
                exchange="polymarket",
                slug=entry.get("slug", ""),
                token_ids=token_ids,
                end_date=_safe_parse_datetime(entry.get("end_date")),
                created_at=_safe_parse_datetime(entry.get("created_at")),
            )
            markets[cid] = info
            # Rebuild token -> condition_id mapping
            for tid in token_ids:
                self._token_to_condition[tid] = cid
        return markets

    def _save_markets_to_cache(self, markets: dict[str, MarketInfo]) -> None:
        """Persist market metadata to a lightweight JSON file."""
        data: dict[str, dict] = {}
        for cid, info in markets.items():
            entry: dict = {
                "question": info.question,
                "outcomes": info.outcomes,
                "outcome_prices": info.outcome_prices,
                "volume": info.volume,
                "liquidity": info.liquidity,
                "active": info.active,
                "closed": info.closed,
                "slug": info.slug,
                "token_ids": info.token_ids,
            }
            if info.end_date is not None:
                entry["end_date"] = info.end_date.isoformat()
            if info.created_at is not None:
                entry["created_at"] = info.created_at.isoformat()
            data[cid] = entry
        try:
            with open(self._cache_path, "w") as f:
                json.dump(data, f)
        except OSError:
            logger.debug("Failed to write market cache: %s", self._cache_path, exc_info=True)

    def load_markets(self) -> dict[str, MarketInfo]:
        """Load all markets from Parquet files.  Returns dict keyed by condition_id."""
        if self._markets_cache is not None:
            return self._markets_cache

        t0 = time.monotonic()

        # Try disk cache first
        if self._is_cache_fresh():
            logger.info("Loading markets from disk cache: %s", self._cache_path)
            cached = self._load_markets_from_cache()
            if cached is not None:
                self._markets_cache = cached
                logger.info("  Loaded %d markets from cache in %.1fs",
                            len(cached), time.monotonic() - t0)
                return cached
            logger.info("  Cache load failed, falling back to parquet files")

        pd = _import_pandas()
        markets: dict[str, MarketInfo] = {}

        parquet_files = sorted(f for f in os.listdir(self._markets_dir) if f.endswith(".parquet"))
        logger.info("Parsing %d market parquet files...", len(parquet_files))

        for fidx, fname in enumerate(parquet_files):
            fpath = os.path.join(self._markets_dir, fname)
            try:
                df = pd.read_parquet(fpath)
            except Exception:
                logger.debug("Failed to read market file: %s", fpath, exc_info=True)
                continue

            records = df.to_dict("records")
            for row in records:
                cid = str(row.get("condition_id", row.get("id", "")))
                if not cid:
                    continue

                outcomes = _parse_json_field(row.get("outcomes"))
                outcome_prices_raw = _parse_json_field(row.get("outcome_prices"))
                outcome_prices: list[float] = []
                for p in outcome_prices_raw:
                    try:
                        outcome_prices.append(float(p))
                    except (ValueError, TypeError):
                        pass

                slug = str(row.get("slug", ""))

                info = MarketInfo(
                    condition_id=cid,
                    question=str(row.get("question", "")),
                    outcomes=outcomes,
                    outcome_prices=outcome_prices,
                    volume=float(row.get("volume", 0) or 0),
                    liquidity=float(row.get("liquidity", 0) or 0),
                    active=bool(row.get("active", True)),
                    closed=bool(row.get("closed", False)),
                    exchange="polymarket",
                    slug=slug,
                    end_date=_safe_parse_datetime(row.get("end_date")),
                    created_at=_safe_parse_datetime(row.get("created_at")),
                )
                markets[cid] = info

            if (fidx + 1) % 20 == 0 or fidx + 1 == len(parquet_files):
                logger.info("  market files: %d/%d parsed (%d markets so far)",
                            fidx + 1, len(parquet_files), len(markets))

        elapsed = time.monotonic() - t0
        logger.info("  Loaded %d markets in %.1fs", len(markets), elapsed)

        self._markets_cache = markets
        self._save_markets_to_cache(markets)
        logger.info("  Saved market cache to %s", self._cache_path)
        return markets

    # -- trades -------------------------------------------------------------

    def load_trades(
        self,
        condition_id: Optional[str] = None,
        market_slug: Optional[str] = None,
        max_trades: Optional[int] = None,
    ) -> list[NormalizedTrade]:
        """Load trades, optionally filtered by market.  Returns sorted by timestamp."""
        t0 = time.monotonic()

        # If filtering by slug, resolve to condition_id
        target_cid = condition_id
        if market_slug and not target_cid:
            market = self.find_market(market_slug)
            if market:
                target_cid = market.condition_id
            else:
                raise ValueError(f"No market found matching: {market_slug}")

        # Load markets for token lookup when filtering
        if target_cid:
            logger.info("  Resolving token IDs for condition_id=%s...", target_cid[:30])
            self.load_markets()

        # Build set of target token IDs for fast filtering
        target_token_ids: set[str] = set()
        if target_cid:
            for tid, cid in self._token_to_condition.items():
                if cid == target_cid:
                    target_token_ids.add(tid)
            logger.info("  Found %d token IDs for filtering", len(target_token_ids))
            if target_token_ids:
                logger.info("    token_ids: %s", ", ".join(list(target_token_ids)[:5])
                             + ("..." if len(target_token_ids) > 5 else ""))

        logger.info("  Loading block timestamp index...")
        block_lookup = self._get_block_lookup()

        all_trades: list[NormalizedTrade] = []
        files = sorted(
            f for f in os.listdir(self._trades_dir) if f.endswith(".parquet")
        )
        total_files = len(files)
        logger.info("  Scanning %d trade files%s...",
                     total_files,
                     f" (max_trades={max_trades})" if max_trades else "")

        # Use adaptive logging interval: every 10% of files, min every 100, max every 2000
        log_interval = max(100, min(2000, total_files // 10))
        files_with_matches = 0

        for file_idx, fname in enumerate(files):
            fpath = os.path.join(self._trades_dir, fname)
            try:
                trades = self._load_trades_from_file(
                    fpath, target_cid, target_token_ids, block_lookup,
                )
            except Exception:
                logger.debug("Failed to load trades from %s", fpath, exc_info=True)
                continue

            if trades:
                files_with_matches += 1
            all_trades.extend(trades)

            if (file_idx + 1) % log_interval == 0 or file_idx + 1 == total_files:
                elapsed = time.monotonic() - t0
                rate = (file_idx + 1) / elapsed if elapsed > 0 else 0
                remaining = (total_files - file_idx - 1) / rate if rate > 0 else 0
                logger.info(
                    "  Scanning trades: %d/%d files (%.0f files/s, ~%.0fs remaining) "
                    "— %d matches in %d files",
                    file_idx + 1, total_files, rate, remaining,
                    len(all_trades), files_with_matches,
                )

            if max_trades and len(all_trades) >= max_trades:
                all_trades = all_trades[:max_trades]
                logger.info("  Reached max_trades=%d, stopping early at file %d/%d",
                            max_trades, file_idx + 1, total_files)
                break

        logger.info("  Sorting %d trades by timestamp...", len(all_trades))
        all_trades.sort(key=lambda t: t.timestamp)
        if max_trades:
            all_trades = all_trades[:max_trades]

        elapsed = time.monotonic() - t0
        logger.info("  Trade loading complete: %d trades in %.1fs", len(all_trades), elapsed)
        return all_trades

    def _load_trades_from_file(
        self,
        fpath: str,
        target_cid: Optional[str],
        target_token_ids: set[str],
        block_lookup: BlockTimestampLookup,
    ) -> list[NormalizedTrade]:
        """Load and parse CTF fills from a single parquet file."""
        import pyarrow.parquet as pq

        # Column projection: only read what we need
        needed_cols = [
            "maker_amount", "taker_amount", "block_number",
            "maker_asset_id", "taker_asset_id",
            "transaction_hash", "log_index", "fee",
        ]
        available_cols = set(pq.read_schema(fpath).names)
        read_cols = [c for c in needed_cols if c in available_cols]

        # Predicate pushdown: filter by token IDs at the PyArrow level
        pq_filters = None
        if target_token_ids:
            token_list = list(target_token_ids)
            # Filter rows where either maker_asset_id or taker_asset_id matches
            if "maker_asset_id" in available_cols and "taker_asset_id" in available_cols:
                import pyarrow.compute  # noqa: F401  (ensures availability)
                pq_filters = [
                    [("maker_asset_id", "in", token_list)],
                    [("taker_asset_id", "in", token_list)],
                ]

        table = pq.read_table(fpath, columns=read_cols, filters=pq_filters)
        columns = set(table.column_names)

        maker_amounts = table.column("maker_amount").to_pylist()
        taker_amounts = table.column("taker_amount").to_pylist()
        block_numbers = table.column("block_number").to_pylist()

        maker_asset_ids = (
            table.column("maker_asset_id").to_pylist()
            if "maker_asset_id" in columns
            else [""] * len(maker_amounts)
        )
        taker_asset_ids = (
            table.column("taker_asset_id").to_pylist()
            if "taker_asset_id" in columns
            else [""] * len(maker_amounts)
        )
        tx_hashes = (
            table.column("transaction_hash").to_pylist()
            if "transaction_hash" in columns
            else [""] * len(maker_amounts)
        )
        log_indices = (
            table.column("log_index").to_pylist()
            if "log_index" in columns
            else [""] * len(maker_amounts)
        )
        fees = (
            table.column("fee").to_pylist()
            if "fee" in columns
            else [0] * len(maker_amounts)
        )

        trades: list[NormalizedTrade] = []
        for i in range(len(maker_amounts)):
            parsed = parse_ctf_fill(
                maker_amount=int(maker_amounts[i]),
                taker_amount=int(taker_amounts[i]),
                maker_asset_id=str(maker_asset_ids[i]),
                taker_asset_id=str(taker_asset_ids[i]),
                fee=int(fees[i]),
            )
            if parsed is None:
                continue

            asset_id, side, price, size, fee_usd = parsed

            # Filter by token if needed
            if target_token_ids and asset_id not in target_token_ids:
                continue

            block = int(block_numbers[i])
            ts_epoch = block_lookup.interpolate(block)
            ts = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)

            cid = self._token_to_condition.get(asset_id, "")
            if target_cid and cid and cid != target_cid:
                continue

            tx_hash = str(tx_hashes[i]) if tx_hashes[i] else ""
            log_idx = str(log_indices[i]) if log_indices[i] else ""
            trade_id = f"{tx_hash}-{log_idx}" if tx_hash else ""

            trades.append(NormalizedTrade(
                asset_id=asset_id,
                side=side,
                price=price,
                size=size,
                timestamp=ts,
                condition_id=cid or (target_cid or ""),
                exchange="polymarket",
                trade_id=trade_id,
                fee=fee_usd,
            ))

        return trades

    # -- search helpers -----------------------------------------------------

    def find_market(self, query: str) -> Optional[MarketInfo]:
        """Find a market by condition_id, slug, or question substring."""
        t0 = time.monotonic()
        logger.info("Searching for market: '%s'", query)
        markets = self.load_markets()
        logger.info("  Searching %d markets...", len(markets))

        # Exact condition_id match
        if query in markets:
            logger.info("  Found by exact condition_id match in %.1fs", time.monotonic() - t0)
            return markets[query]

        # Slug match
        query_lower = query.lower()
        for info in markets.values():
            if info.slug and info.slug.lower() == query_lower:
                logger.info("  Found by slug match: '%s' in %.1fs",
                            info.question[:60], time.monotonic() - t0)
                return info

        # Substring search in question (case-insensitive)
        for info in markets.values():
            if query_lower in info.question.lower():
                logger.info("  Found by question substring: '%s' in %.1fs",
                            info.question[:60], time.monotonic() - t0)
                return info

        logger.info("  No market found for '%s' (searched %d markets in %.1fs)",
                     query, len(markets), time.monotonic() - t0)
        return None

    def find_markets(self, query: str) -> list[MarketInfo]:
        """Find ALL markets matching *query* by condition_id, slug, or question substring.

        Returns matches sorted by volume (descending).  An exact condition_id
        hit returns only that single market.
        """
        t0 = time.monotonic()
        logger.info("Searching for all markets matching: '%s'", query)
        markets = self.load_markets()

        # Exact condition_id match — unambiguous, return just that one
        if query in markets:
            logger.info("  Exact condition_id match in %.1fs", time.monotonic() - t0)
            return [markets[query]]

        query_lower = query.lower()
        results: list[MarketInfo] = []

        for info in markets.values():
            # Slug match
            if info.slug and info.slug.lower() == query_lower:
                results.append(info)
                continue
            # Substring match in question
            if query_lower in info.question.lower():
                results.append(info)

        results.sort(key=lambda m: m.volume, reverse=True)
        logger.info("  Found %d markets matching '%s' in %.1fs",
                     len(results), query, time.monotonic() - t0)
        return results

    def list_markets(
        self,
        min_volume: float = 0,
        active_only: bool = False,
    ) -> list[MarketInfo]:
        """List available markets, optionally filtered."""
        markets = self.load_markets()
        result = []
        for info in markets.values():
            if active_only and not info.active:
                continue
            if info.volume < min_volume:
                continue
            result.append(info)
        result.sort(key=lambda m: m.volume, reverse=True)
        return result


# ---------------------------------------------------------------------------
# Kalshi data loader
# ---------------------------------------------------------------------------

class KalshiDataLoader:
    """Reads Parquet trade/market files from a local Kalshi data directory.

    Expected directory layout::

        data_dir/
            trades/   *.parquet
            markets/  *.parquet
    """

    def __init__(self, data_dir: str) -> None:
        self._data_dir = data_dir
        self._trades_dir = os.path.join(data_dir, "trades")
        self._markets_dir = os.path.join(data_dir, "markets")

        if not os.path.isdir(self._trades_dir):
            raise FileNotFoundError(f"Trades directory not found: {self._trades_dir}")
        if not os.path.isdir(self._markets_dir):
            raise FileNotFoundError(f"Markets directory not found: {self._markets_dir}")

        self._markets_cache: Optional[dict[str, MarketInfo]] = None

    # -- cache helpers ------------------------------------------------------

    @property
    def _cache_path(self) -> str:
        """Path to the JSON market metadata cache file."""
        return os.path.join(self._data_dir, ".market_cache.json")

    def _is_cache_fresh(self) -> bool:
        """Return True if the disk cache exists and is newer than all market parquet files."""
        cache = self._cache_path
        if not os.path.isfile(cache):
            return False
        cache_mtime = os.path.getmtime(cache)
        for fname in os.listdir(self._markets_dir):
            if fname.endswith(".parquet"):
                fpath = os.path.join(self._markets_dir, fname)
                if os.path.getmtime(fpath) > cache_mtime:
                    return False
        return True

    def _load_markets_from_cache(self) -> Optional[dict[str, MarketInfo]]:
        """Try to load market metadata from the JSON disk cache."""
        try:
            with open(self._cache_path, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError):
            return None

        markets: dict[str, MarketInfo] = {}
        for ticker, entry in data.items():
            status_val = entry.get("status", "")
            info = MarketInfo(
                condition_id=ticker,
                question=entry.get("question", ""),
                exchange="kalshi",
                event_ticker=entry.get("event_ticker", ""),
                market_type=entry.get("market_type", ""),
                status=status_val,
                yes_bid=_safe_int(entry.get("yes_bid")),
                yes_ask=_safe_int(entry.get("yes_ask")),
                last_price=_safe_int(entry.get("last_price")),
                volume=entry.get("volume", 0.0),
                open_interest=int(entry.get("open_interest", 0)),
                result=entry.get("result", ""),
                active=(status_val == "open"),
                closed=(status_val in ("closed", "finalized")),
                created_at=_safe_parse_datetime(entry.get("created_at")),
                end_date=_safe_parse_datetime(entry.get("end_date")),
            )
            markets[ticker] = info
        return markets

    def _save_markets_to_cache(self, markets: dict[str, MarketInfo]) -> None:
        """Persist market metadata to a lightweight JSON file."""
        data: dict[str, dict] = {}
        for ticker, info in markets.items():
            entry: dict = {
                "question": info.question,
                "event_ticker": info.event_ticker,
                "market_type": info.market_type,
                "status": info.status,
                "yes_bid": info.yes_bid,
                "yes_ask": info.yes_ask,
                "last_price": info.last_price,
                "volume": info.volume,
                "open_interest": info.open_interest,
                "result": info.result,
            }
            if info.created_at is not None:
                entry["created_at"] = info.created_at.isoformat()
            if info.end_date is not None:
                entry["end_date"] = info.end_date.isoformat()
            data[ticker] = entry
        try:
            with open(self._cache_path, "w") as f:
                json.dump(data, f)
        except OSError:
            logger.debug("Failed to write market cache: %s", self._cache_path, exc_info=True)

    # -- markets ------------------------------------------------------------

    def load_markets(self) -> dict[str, MarketInfo]:
        """Load all Kalshi markets.  Returns dict keyed by ticker (-> condition_id)."""
        if self._markets_cache is not None:
            return self._markets_cache

        t0 = time.monotonic()

        # Try disk cache first
        if self._is_cache_fresh():
            logger.info("Loading Kalshi markets from disk cache: %s", self._cache_path)
            cached = self._load_markets_from_cache()
            if cached is not None:
                self._markets_cache = cached
                logger.info("  Loaded %d markets from cache in %.1fs",
                            len(cached), time.monotonic() - t0)
                return cached
            logger.info("  Cache load failed, falling back to parquet files")

        pd = _import_pandas()
        markets: dict[str, MarketInfo] = {}

        parquet_files = sorted(f for f in os.listdir(self._markets_dir) if f.endswith(".parquet"))
        logger.info("Parsing %d Kalshi market parquet files...", len(parquet_files))

        for fidx, fname in enumerate(parquet_files):
            fpath = os.path.join(self._markets_dir, fname)
            try:
                df = pd.read_parquet(fpath)
            except Exception:
                logger.debug("Failed to read market file: %s", fpath, exc_info=True)
                continue

            records = df.to_dict("records")
            for row in records:
                ticker = str(row.get("ticker", ""))
                if not ticker:
                    continue

                status_val = str(row.get("status", "")).lower()

                info = MarketInfo(
                    condition_id=ticker,
                    question=str(row.get("title", "")),
                    exchange="kalshi",
                    event_ticker=str(row.get("event_ticker", "")),
                    market_type=str(row.get("market_type", "")),
                    status=status_val,
                    yes_bid=_safe_int(row.get("yes_bid")),
                    yes_ask=_safe_int(row.get("yes_ask")),
                    last_price=_safe_int(row.get("last_price")),
                    volume=float(row.get("volume", 0) or 0),
                    open_interest=int(row.get("open_interest", 0) or 0),
                    result=str(row.get("result", "")),
                    active=(status_val == "open"),
                    closed=(status_val in ("closed", "finalized")),
                    created_at=_safe_parse_datetime(row.get("created_time")),
                    end_date=_safe_parse_datetime(row.get("close_time")),
                )
                markets[ticker] = info

            if (fidx + 1) % 20 == 0 or fidx + 1 == len(parquet_files):
                logger.info("  market files: %d/%d parsed (%d markets so far)",
                            fidx + 1, len(parquet_files), len(markets))

        elapsed = time.monotonic() - t0
        logger.info("  Loaded %d Kalshi markets in %.1fs", len(markets), elapsed)

        self._markets_cache = markets
        self._save_markets_to_cache(markets)
        logger.info("  Saved market cache to %s", self._cache_path)
        return markets

    # -- trades -------------------------------------------------------------

    def load_trades(
        self,
        condition_id: Optional[str] = None,
        market_slug: Optional[str] = None,
        max_trades: Optional[int] = None,
    ) -> list[NormalizedTrade]:
        """Load Kalshi trades, optionally filtered.  Returns sorted by timestamp."""
        t0 = time.monotonic()

        target_ticker = condition_id
        if market_slug and not target_ticker:
            market = self.find_market(market_slug)
            if market:
                target_ticker = market.condition_id
            else:
                raise ValueError(f"No market found matching: {market_slug}")

        pd = _import_pandas()
        all_trades: list[NormalizedTrade] = []

        files = sorted(
            f for f in os.listdir(self._trades_dir) if f.endswith(".parquet")
        )
        total_files = len(files)
        logger.info("  Scanning %d Kalshi trade files%s...",
                     total_files,
                     f" (ticker={target_ticker})" if target_ticker else "")
        log_interval = max(50, min(500, total_files // 10))
        files_with_matches = 0

        for file_idx, fname in enumerate(files):
            fpath = os.path.join(self._trades_dir, fname)
            try:
                df = pd.read_parquet(fpath)
            except Exception:
                logger.debug("Failed to read trades file: %s", fpath, exc_info=True)
                continue

            # Vectorized filtering by ticker before iterating rows
            if target_ticker and "ticker" in df.columns:
                df = df[df["ticker"] == target_ticker]
                if df.empty:
                    continue

            # Filter to valid taker_side values
            if "taker_side" in df.columns:
                df = df[df["taker_side"].astype(str).str.lower().isin(["yes", "no"])]
                if df.empty:
                    continue

            # Filter out rows without yes_price
            if "yes_price" in df.columns:
                df = df[df["yes_price"].notna()]
                if df.empty:
                    continue

            files_with_matches += 1
            records = df.to_dict("records")
            for row in records:
                ticker = str(row.get("ticker", ""))

                taker_side_raw = str(row.get("taker_side", "")).lower()
                if taker_side_raw == "yes":
                    side = "BUY"
                elif taker_side_raw == "no":
                    side = "SELL"
                else:
                    continue

                yes_price_cents = row.get("yes_price")
                if yes_price_cents is None:
                    continue
                try:
                    price = float(yes_price_cents) / 100.0
                except (ValueError, TypeError):
                    continue
                price = max(0.001, min(0.999, price))

                ts = _safe_parse_datetime(row.get("created_time"))
                if ts is None:
                    continue

                try:
                    size = float(row.get("count", 0))
                except (ValueError, TypeError):
                    continue

                trade_id = str(row.get("trade_id", ""))

                all_trades.append(NormalizedTrade(
                    asset_id=ticker,
                    side=side,
                    price=price,
                    size=size,
                    timestamp=ts,
                    condition_id=ticker,
                    exchange="kalshi",
                    trade_id=trade_id,
                ))

            if (file_idx + 1) % log_interval == 0 or file_idx + 1 == total_files:
                elapsed = time.monotonic() - t0
                rate = (file_idx + 1) / elapsed if elapsed > 0 else 0
                remaining = (total_files - file_idx - 1) / rate if rate > 0 else 0
                logger.info(
                    "  Scanning trades: %d/%d files (%.0f files/s, ~%.0fs remaining) "
                    "— %d matches in %d files",
                    file_idx + 1, total_files, rate, remaining,
                    len(all_trades), files_with_matches,
                )

            if max_trades and len(all_trades) >= max_trades:
                all_trades = all_trades[:max_trades]
                logger.info("  Reached max_trades=%d, stopping early at file %d/%d",
                            max_trades, file_idx + 1, total_files)
                break

        logger.info("  Sorting %d trades by timestamp...", len(all_trades))
        all_trades.sort(key=lambda t: t.timestamp)
        if max_trades:
            all_trades = all_trades[:max_trades]

        elapsed = time.monotonic() - t0
        logger.info("  Trade loading complete: %d trades in %.1fs", len(all_trades), elapsed)
        return all_trades

    # -- search helpers -----------------------------------------------------

    def find_market(self, query: str) -> Optional[MarketInfo]:
        """Find a Kalshi market by ticker or title substring."""
        t0 = time.monotonic()
        logger.info("Searching for Kalshi market: '%s'", query)
        markets = self.load_markets()
        logger.info("  Searching %d markets...", len(markets))

        # Exact ticker match
        if query in markets:
            logger.info("  Found by exact ticker match in %.1fs", time.monotonic() - t0)
            return markets[query]

        # Substring search in title/question (case-insensitive)
        query_lower = query.lower()
        for info in markets.values():
            if query_lower in info.question.lower():
                logger.info("  Found by title substring: '%s' in %.1fs",
                            info.question[:60], time.monotonic() - t0)
                return info

        logger.info("  No market found for '%s' (searched %d markets in %.1fs)",
                     query, len(markets), time.monotonic() - t0)
        return None

    def find_markets(self, query: str) -> list[MarketInfo]:
        """Find ALL Kalshi markets matching *query* by ticker or title substring.

        Returns matches sorted by volume (descending).  An exact ticker
        hit returns only that single market.
        """
        t0 = time.monotonic()
        logger.info("Searching for all Kalshi markets matching: '%s'", query)
        markets = self.load_markets()

        # Exact ticker match — unambiguous
        if query in markets:
            logger.info("  Exact ticker match in %.1fs", time.monotonic() - t0)
            return [markets[query]]

        query_lower = query.lower()
        results: list[MarketInfo] = []
        for info in markets.values():
            if query_lower in info.question.lower():
                results.append(info)

        results.sort(key=lambda m: m.volume, reverse=True)
        logger.info("  Found %d Kalshi markets matching '%s' in %.1fs",
                     len(results), query, time.monotonic() - t0)
        return results

    def list_markets(
        self,
        min_volume: float = 0,
        active_only: bool = False,
    ) -> list[MarketInfo]:
        """List available Kalshi markets, optionally filtered."""
        markets = self.load_markets()
        result = []
        for info in markets.values():
            if active_only and not info.active:
                continue
            if info.volume < min_volume:
                continue
            result.append(info)
        result.sort(key=lambda m: m.volume, reverse=True)
        return result


# ---------------------------------------------------------------------------
# Helper: safe int conversion
# ---------------------------------------------------------------------------

def _safe_int(value) -> Optional[int]:
    """Convert to int or return None."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Orderbook reconstruction from trades
# ---------------------------------------------------------------------------

class OrderbookReconstructor:
    """Converts a stream of trades into approximate OrderbookSnapshots."""

    def __init__(
        self,
        window_seconds: int = 30,
        tick_size: float = 0.01,
        depth_levels: int = 5,
        level_spacing: float = 0.01,
        synthetic_base_size: float = 50.0,
    ):
        self.window_seconds = window_seconds
        self.tick_size = tick_size
        self.depth_levels = depth_levels
        self.level_spacing = level_spacing
        self.synthetic_base_size = synthetic_base_size

    def reconstruct(
        self,
        trades: list[NormalizedTrade],
        instrument_id: str = "REAL-001",
    ) -> list[OrderbookSnapshot]:
        """Convert trades into a list of OrderbookSnapshots."""
        if not trades:
            return []

        t0 = time.monotonic()
        snapshots: list[OrderbookSnapshot] = []
        prev_snapshot: Optional[OrderbookSnapshot] = None

        # Bucket trades into time windows
        logger.info("  Bucketing %d trades into %ds windows...",
                     len(trades), self.window_seconds)
        windows = self._bucket_by_window(trades)
        logger.info("  Building %d orderbook snapshots...", len(windows))

        for window_start, window_trades in windows:
            snapshot = self._build_snapshot(
                window_trades, window_start, instrument_id, prev_snapshot
            )
            snapshots.append(snapshot)
            prev_snapshot = snapshot

        elapsed = time.monotonic() - t0
        non_empty = sum(1 for s in snapshots if s.midpoint is not None)
        logger.info("  Reconstruction complete: %d snapshots (%d with data) in %.1fs",
                     len(snapshots), non_empty, elapsed)
        return snapshots

    def _bucket_by_window(
        self, trades: list[NormalizedTrade]
    ) -> list[tuple[datetime, list[NormalizedTrade]]]:
        """Group trades into time windows."""
        if not trades:
            return []

        window_delta = timedelta(seconds=self.window_seconds)
        start = trades[0].timestamp
        end = trades[-1].timestamp

        windows: list[tuple[datetime, list[NormalizedTrade]]] = []
        current_start = start
        trade_idx = 0

        while current_start <= end:
            current_end = current_start + window_delta
            window_trades: list[NormalizedTrade] = []

            while trade_idx < len(trades) and trades[trade_idx].timestamp < current_end:
                window_trades.append(trades[trade_idx])
                trade_idx += 1

            windows.append((current_start, window_trades))
            current_start = current_end

        return windows

    def _build_snapshot(
        self,
        trades: list[NormalizedTrade],
        timestamp: datetime,
        instrument_id: str,
        prev_snapshot: Optional[OrderbookSnapshot],
    ) -> OrderbookSnapshot:
        """Build a single snapshot from a window of trades."""
        if not trades:
            # Carry forward previous snapshot with updated timestamp
            if prev_snapshot:
                return OrderbookSnapshot(
                    instrument_id=instrument_id,
                    bids=[OrderbookLevel(price=b.price, size=b.size) for b in prev_snapshot.bids],
                    asks=[OrderbookLevel(price=a.price, size=a.size) for a in prev_snapshot.asks],
                    timestamp=timestamp,
                )
            # No previous snapshot — synthesize around 0.5
            return self._synthesize_snapshot(0.5, instrument_id, timestamp)

        # Separate by side: buys hit the ask, sells hit the bid
        buy_trades = [t for t in trades if t.side == "BUY"]
        sell_trades = [t for t in trades if t.side == "SELL"]

        # Aggregate by price level
        ask_levels = self._aggregate_levels(buy_trades)  # Buys hit asks
        bid_levels = self._aggregate_levels(sell_trades)  # Sells hit bids

        # Estimate mid price from all trades
        all_sizes = sum(t.size for t in trades)
        if all_sizes > 0:
            vwap = sum(t.price * t.size for t in trades) / all_sizes
        else:
            vwap = trades[-1].price

        spread_estimate = 0.02

        # If one side is missing, synthesize from the other
        if not ask_levels and bid_levels:
            best_bid = max(l.price for l in bid_levels)
            ask_levels = [OrderbookLevel(
                price=self._clamp(best_bid + spread_estimate),
                size=self.synthetic_base_size,
            )]
        elif not bid_levels and ask_levels:
            best_ask = min(l.price for l in ask_levels)
            bid_levels = [OrderbookLevel(
                price=self._clamp(best_ask - spread_estimate),
                size=self.synthetic_base_size,
            )]
        elif not bid_levels and not ask_levels:
            return self._synthesize_snapshot(vwap, instrument_id, timestamp)

        # Sort: bids descending, asks ascending
        bid_levels.sort(key=lambda l: l.price, reverse=True)
        ask_levels.sort(key=lambda l: l.price)

        # Ensure bid < ask
        if bid_levels and ask_levels and bid_levels[0].price >= ask_levels[0].price:
            mid = (bid_levels[0].price + ask_levels[0].price) / 2
            bid_levels[0] = OrderbookLevel(
                price=self._clamp(mid - spread_estimate / 2),
                size=bid_levels[0].size,
            )
            ask_levels[0] = OrderbookLevel(
                price=self._clamp(mid + spread_estimate / 2),
                size=ask_levels[0].size,
            )

        # Pad to depth_levels
        bid_levels = self._pad_levels(bid_levels, "bid")
        ask_levels = self._pad_levels(ask_levels, "ask")

        return OrderbookSnapshot(
            instrument_id=instrument_id,
            bids=bid_levels[:self.depth_levels],
            asks=ask_levels[:self.depth_levels],
            timestamp=timestamp,
        )

    def _aggregate_levels(self, trades: list[NormalizedTrade]) -> list[OrderbookLevel]:
        """Aggregate trades into price levels using VWAP and summed size."""
        if not trades:
            return []

        buckets: dict[float, list[NormalizedTrade]] = {}
        for t in trades:
            rounded = round(t.price / self.tick_size) * self.tick_size
            rounded = round(rounded, 4)
            buckets.setdefault(rounded, []).append(t)

        levels = []
        for price, bucket_trades in buckets.items():
            total_size = sum(t.size for t in bucket_trades)
            total_value = sum(t.price * t.size for t in bucket_trades)
            vwap = total_value / total_size if total_size > 0 else price
            levels.append(OrderbookLevel(
                price=self._clamp(round(vwap, 4)),
                size=round(total_size, 2),
            ))

        return levels

    def _pad_levels(
        self, levels: list[OrderbookLevel], side: str
    ) -> list[OrderbookLevel]:
        """Pad levels to depth_levels with decaying synthetic levels."""
        if len(levels) >= self.depth_levels:
            return levels

        if not levels:
            return levels

        result = list(levels)
        if side == "bid":
            base_price = result[-1].price
            for i in range(self.depth_levels - len(result)):
                decay = 1.0 / (2 + i)
                price = self._clamp(base_price - (i + 1) * self.level_spacing)
                result.append(OrderbookLevel(
                    price=price,
                    size=round(self.synthetic_base_size * decay, 2),
                ))
        else:  # ask
            base_price = result[-1].price
            for i in range(self.depth_levels - len(result)):
                decay = 1.0 / (2 + i)
                price = self._clamp(base_price + (i + 1) * self.level_spacing)
                result.append(OrderbookLevel(
                    price=price,
                    size=round(self.synthetic_base_size * decay, 2),
                ))

        return result

    def _synthesize_snapshot(
        self,
        mid: float,
        instrument_id: str,
        timestamp: datetime,
    ) -> OrderbookSnapshot:
        """Create a fully synthetic snapshot around a mid price."""
        spread = 0.02
        bids = []
        asks = []
        for i in range(self.depth_levels):
            decay = 1.0 / (1 + i * 0.3)
            bids.append(OrderbookLevel(
                price=self._clamp(mid - spread / 2 - i * self.level_spacing),
                size=round(self.synthetic_base_size * decay, 2),
            ))
            asks.append(OrderbookLevel(
                price=self._clamp(mid + spread / 2 + i * self.level_spacing),
                size=round(self.synthetic_base_size * decay, 2),
            ))
        return OrderbookSnapshot(
            instrument_id=instrument_id,
            bids=bids,
            asks=asks,
            timestamp=timestamp,
        )

    @staticmethod
    def _clamp(price: float) -> float:
        """Clamp price to valid range (0.001, 0.999)."""
        return round(max(0.001, min(0.999, price)), 4)


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def load_real_snapshots(
    data_dir: str,
    condition_id: Optional[str] = None,
    market_slug: Optional[str] = None,
    window_seconds: int = 30,
    max_trades: Optional[int] = None,
    exchange: str = "polymarket",
) -> tuple[list[OrderbookSnapshot], Optional[MarketInfo]]:
    """Load real prediction-market data and reconstruct orderbook snapshots.

    Args:
        data_dir: Path to the data directory.
        condition_id: Filter to a specific market by condition_id / ticker.
        market_slug: Filter to a specific market by slug or title substring.
        window_seconds: Time window size for orderbook reconstruction.
        max_trades: Maximum number of trades to load.
        exchange: Which exchange loader to use (``"polymarket"`` or ``"kalshi"``).

    Returns:
        ``(snapshots, market_info)`` tuple.
    """
    if exchange == "kalshi":
        loader = KalshiDataLoader(data_dir)
    else:
        loader = PolymarketDataLoader(data_dir)

    market_info = None
    if condition_id:
        market_info = loader.find_market(condition_id)
    elif market_slug:
        market_info = loader.find_market(market_slug)

    trades = loader.load_trades(
        condition_id=condition_id,
        market_slug=market_slug,
        max_trades=max_trades,
    )

    if not trades:
        return [], market_info

    reconstructor = OrderbookReconstructor(window_seconds=window_seconds)
    instrument_id = condition_id or market_slug or "REAL-001"
    snapshots = reconstructor.reconstruct(trades, instrument_id)

    return snapshots, market_info
