# Signal Sources

All signals live in `components/signals.py`. Single-exchange signals extend `SignalSource`; cross-exchange signals extend `CrossExchangeSignalSource`.

## Existing Signals

### MidpointDeviationSignal
**Type:** Single-exchange | **Bot:** Directional | **Name:** `midpoint_deviation`

Mean-reversion signal for binary markets. Goes LONG when midpoint is below a fair value, SHORT when above.

| Param | Default | Description |
|---|---|---|
| `fair_value` | 0.5 | Assumed true probability |
| `min_deviation` | 0.05 | Minimum distance from fair value to fire |

**Score:** `abs(deviation) * 100` (0-50 range for typical binary markets)

**When to use:** Markets that oscillate around a known fair value. Works best on liquid, high-volume binary contracts where price mean-reverts.

**Limitations:** Assumes you know the fair value. Fails on trending markets or during news events.

---

### FavoriteLongshotSignal
**Type:** Single-exchange | **Bot:** Directional | **Name:** `favorite_longshot`

Exploits the well-documented favorite-longshot bias: cheap contracts (longshots) are systematically overpriced, expensive contracts (favorites) are underpriced.

| Param | Default | Description |
|---|---|---|
| `low_threshold` | 0.20 | Below this = overpriced longshot (SHORT) |
| `high_threshold` | 0.80 | Above this = underpriced favorite (LONG) |
| `max_score` | 100.0 | Maximum signal score |
| `max_lookups` | 60 | Cap on midpoint API calls per cycle |

**Score:** Scales linearly with distance from threshold. A contract at 0.05 scores higher than one at 0.15.

**When to use:** Broad market scanning where you don't have a view on individual outcomes. Statistically profitable over large samples.

**Limitations:** Loses on individual positions regularly. Needs diversification across many markets. Slow to realize PnL (must wait for resolution).

---

### OrderbookMicrostructureSignal
**Type:** Single-exchange (stateful) | **Bot:** Directional | **Name:** `orderbook_microstructure`

Predicts short-term price direction from order flow imbalance, depth pressure, and volume-weighted midpoint dynamics. Maintains rolling history per instrument.

| Param | Default | Description |
|---|---|---|
| `window_size` | 20 | Rolling history length |
| `depth_levels` | 5 | Orderbook levels to analyze |
| `min_score` | 0.1 | Minimum composite score to emit signal |
| `imbalance_weight` | 0.30 | Weight for bid/ask volume imbalance |
| `weighted_mid_weight` | 0.25 | Weight for VWAP mid deviation |
| `depth_weight` | 0.25 | Weight for cumulative depth pressure |
| `spread_weight` | 0.20 | Spread dampening factor |

**Features computed per snapshot:**
- **Volume imbalance** — bid volume / total volume (0.5 = balanced)
- **Weighted mid deviation** — VWAP midpoint vs raw midpoint
- **Depth pressure** — cumulative bid vs ask depth at each level
- **Spread signal** — wide spread reduces confidence

**Extras with history (3+ snapshots):**
- Momentum (imbalance slope + weighted-mid drift)
- Flow shift amplifier (sudden imbalance change in signal direction = +30%)

**When to use:** High-frequency directional trading. Best on markets with deep, active orderbooks.

**Limitations:** Needs frequent polling (every few seconds). Noisy on thin books. Stateful — must call `reset()` when switching instruments.

---

### BinaryPerpHedgeSignal
**Type:** Cross-exchange | **Bot:** Hedge | **Name:** `binary_perp_hedge`

Generates hedged two-leg signals: buy a near-resolution binary contract + hedge with a perp short (or vice versa).

| Param | Default | Description |
|---|---|---|
| `binary_exchange` | POLYMARKET | Where to buy the binary |
| `hedge_exchange` | HYPERLIQUID | Where to hedge with perps |
| `min_binary_price` | 0.70 | Only consider binaries priced 0.70-0.95 |
| `max_binary_price` | 0.95 | Upper bound |
| `hedge_ratio` | 0.5 | Hedge leg size as fraction of binary leg |
| `hedge_leverage` | 2.0 | Perp leverage |

**Logic:** Scans binary markets for crypto-related contracts near resolution (high price = high conviction). Matches them to Hyperliquid perps via keyword mapping (`ASSET_KEYWORD_MAP`). Detects directional intent from market name ("Will BTC reach $100k?" = LONG).

**When to use:** Crypto price binary markets approaching resolution. Captures the resolution premium while hedging spot exposure.

---

### CrossExchangeArbSignal
**Type:** Cross-exchange | **Bot:** Cross-arb | **Name:** `cross_exchange_arb`

Scans for the same event priced differently on Polymarket vs Kalshi. If the price gap exceeds a threshold, generates a buy-cheap/sell-expensive signal.

| Param | Default | Description |
|---|---|---|
| `min_edge_bps` | 50.0 | Minimum price difference in basis points |
| `max_price` | 0.95 | Ignore contracts above this price |

**Matching:** Name-based (strips punctuation, lowercases, compares). Production would use fuzzy matching.

**When to use:** When both Polymarket and Kalshi list the same event. Edge decays as markets converge.

---

## Proposed New Signals

### 1. VolumeSpike Signal
**Type:** Single-exchange | **Best for:** Directional bot

Detects sudden volume surges relative to a rolling average. A 3x-5x spike in trade volume often precedes a directional move (informed traders arriving).

```
score = (current_volume / rolling_avg_volume - 1) * weight
direction = inferred from whether volume is hitting bids or asks
```

**Why it works on prediction markets:** News events (poll results, court rulings, earnings) cause informed flow that hits the book before prices fully adjust. Volume spikes before price moves.

**Params:** `window_minutes`, `spike_threshold` (e.g., 3x), `lookback_trades`

---

### 2. ResolutionDecay Signal
**Type:** Single-exchange | **Best for:** Directional bot (time-aware)

As a binary contract approaches its expiry/resolution date, contracts priced above 0.5 tend to drift toward 1.0 and those below 0.5 drift toward 0.0. This signal goes LONG on favorites and SHORT on longshots, with score scaling exponentially as time-to-expiry shrinks.

```
days_left = (expiry - now).days
if days_left < threshold:
    score = base_score * (1 / max(days_left, 1))
    direction = LONG if price > 0.5 else SHORT
```

**Why it works:** Market participants discount time value. As resolution approaches, uncertainty compresses and prices converge to outcomes. The last 48-72 hours see the most aggressive convergence.

**Params:** `max_days_out` (e.g., 7), `min_price_distance` (from 0.5), `decay_curve` (linear/exponential)

---

### 3. SpreadCompression Signal
**Type:** Single-exchange | **Best for:** Market-making bot

Monitors bid-ask spread dynamics. When a historically wide spread suddenly compresses, it signals increased liquidity/competition. When a tight spread suddenly widens, it signals uncertainty or a liquidity vacuum.

```
spread_z = (current_spread - rolling_avg_spread) / rolling_std_spread
if spread_z < -2: # spread much tighter than normal
    direction = NEUTRAL (reduce quote size, competition arrived)
if spread_z > 2: # spread much wider than normal
    direction = widen quotes / increase size (profit opportunity)
```

**Why it works for MM:** Tells you when to be aggressive (wide spreads = more edge per quote) vs defensive (tight spreads = more adverse selection risk).

**Params:** `window_size`, `z_threshold`, `min_observations`

---

### 4. CorrelatedMarket Signal
**Type:** Single-exchange | **Best for:** Directional bot

Exploits price correlations between related markets. Example: if "Will Republicans win the House?" moves sharply but "Will Republicans win the Senate?" hasn't moved yet, the second market is likely mispriced.

```
leader_move = leader_price - leader_price_prev
follower_expected = correlation * leader_move
follower_actual = follower_price - follower_price_prev
gap = follower_expected - follower_actual
if abs(gap) > threshold:
    direction = LONG if gap > 0 else SHORT
```

**Why it works:** Prediction markets are thin. News hits one market before propagating to correlated ones. The delay can be minutes to hours.

**Params:** `market_pairs` (list of correlated market_id tuples), `correlation_estimate`, `min_leader_move`, `max_lag_minutes`

---

### 5. ProbabilitySum Signal
**Type:** Single-exchange | **Best for:** Directional bot (arb-style)

For multi-outcome markets (e.g., "Who will win the election?" with 5 candidates), the YES prices should sum to ~1.0. When they sum to significantly more or less, there's a mispricing.

```
total = sum(yes_price for each outcome in market)
if total > 1.0 + threshold:
    # Overpriced: SHORT the most expensive outcome(s)
if total < 1.0 - threshold:
    # Underpriced: LONG the cheapest outcome(s)
```

**Why it works:** Market makers and arbitrageurs enforce this constraint, but on thin markets it can drift for minutes. Especially common after a single outcome moves sharply and the others haven't adjusted.

**Params:** `sum_threshold` (e.g., 0.03), `min_outcomes` (e.g., 3), `max_single_position_pct`

---

### 6. NewMarketMomentum Signal
**Type:** Single-exchange | **Best for:** Directional bot

Newly listed markets (first 24-48 hours) tend to have mispriced contracts as price discovery is still happening. This signal monitors fresh listings and trades in the direction of early momentum.

```
if market_age < max_age:
    price_trend = (current_price - listing_price) / listing_price
    if abs(price_trend) > min_trend:
        direction = LONG if price_trend > 0 else SHORT
        score = abs(price_trend) * recency_boost
```

**Why it works:** Early liquidity on new markets is thin and driven by the most opinionated participants. Initial price moves tend to continue as more participants discover the market and pile in.

**Params:** `max_age_hours` (e.g., 48), `min_trend_pct` (e.g., 0.05), `recency_boost`

---

### 7. CalendarEventSignal
**Type:** Single-exchange | **Best for:** Directional bot (event-driven)

Fires signals around known calendar events (elections, earnings dates, court rulings, Fed meetings) that are likely to resolve prediction markets. Goes LONG on high-conviction markets (price > 0.8) just before the event, capturing the final convergence premium.

```
hours_to_event = (event_time - now).total_seconds() / 3600
if hours_to_event < window_hours:
    if price > high_conviction_threshold:
        direction = LONG
        score = price * (1 / max(hours_to_event, 0.5))
```

**Why it works:** The last few hours before a known catalyst see the sharpest convergence. A contract at 0.92 with 2 hours to resolution has very asymmetric payoff.

**Params:** `event_calendar` (dict of market_id to event datetime), `window_hours`, `high_conviction_threshold`

---

## How to Add a New Signal

1. Add a class in `components/signals.py` extending `SignalSource` (or `CrossExchangeSignalSource` for multi-exchange)
2. Set `name = "your_signal_name"` as a class variable
3. Implement `async def generate(self, client) -> list[Signal]`
4. Wire it into the bot in `scripts/run_bot.py`
5. Add tests in `tests/components/`
