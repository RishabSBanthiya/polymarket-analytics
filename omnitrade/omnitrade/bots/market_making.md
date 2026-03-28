# Market Making Bot

## Overview

Two-sided quoting bot that provides liquidity by placing bid and ask orders. Designed for latency-disadvantaged environments where speed is not the primary edge. Instead, it uses adaptive quoting, fair value estimation, and toxicity awareness to maintain profitability. Works with any exchange through the `ExchangeClient` interface.

## How It Works

The bot runs a continuous loop at a configurable interval (default 3s):

1. **Update equity** — fetch balance, check drawdown limits (halts if breached)
2. **Select markets** — ask the market selector for the best instruments to quote
3. **Detect pairs** — group instruments by `market_id` for YES/NO inventory netting
4. **Cancel stale orders** — cancel existing orders, track failed cancels as filled
5. **Generate quotes** — the quote engine produces bid/ask prices and sizes
6. **Place orders** — submit limit orders, skipping sides at inventory limit
7. **Track fills** — update inventory and toxicity tracker

## AdaptiveQuoter

Dynamic quoting that adapts to market conditions:

- **Volatility-scaled spreads** — `VolatilityTracker` computes rolling stdev of log-returns from recent mid-prices. Spread widens automatically in volatile markets, tightens in calm ones. Controlled by `vol_scale` (default 2x), clamped between `min_half_spread` and `max_half_spread`.

- **Fair value model** — `FairValueEstimator` replaces the raw midpoint with an estimated fair value that accounts for:
  - *Orderbook imbalance*: heavy bid depth shifts fair value above mid (and vice versa)
  - *Price drift*: trending market shifts fair value in the trend direction

- **Asymmetric sizing** — reduces quote size on the "toxic" side based on recent price drift. If price is rising, the ask side is toxic (informed buyers lifting offers), so ask size is reduced. Controlled by `toxic_size_scale` and `max_toxic_reduction`.

- **Toxicity awareness** — `FillToxicityTracker` monitors fill timing. Orders that fill within `toxic_threshold_seconds` of placement are classified as toxic (informed flow). High toxic ratios add a spread penalty via `spread_penalty_scale`.

- **Quadratic inventory skew** — skew is proportional to `ratio * |ratio|`, meaning it grows quadratically at high inventory levels. Much more aggressive at reducing imbalance than linear skew.

## Components

All components are pluggable (composition over inheritance):

| Component | Class | Role |
|-----------|-------|------|
| Quote Engine | `AdaptiveQuoter` | Generates `Quote` objects with bid/ask price and size |
| Market Selector | `ActiveMarketSelector` | Chooses which instruments to quote |
| Inventory Manager | `InventoryManager` | Tracks net exposure per instrument with YES/NO pair netting |
| Risk Coordinator | `RiskCoordinator` | Drawdown limits, failure tracking, heartbeat |
| Volatility Tracker | `VolatilityTracker` | Rolling mid-price volatility and drift estimation |
| Fair Value Estimator | `FairValueEstimator` | Orderbook imbalance-based fair value |
| Toxicity Tracker | `FillToxicityTracker` | Detects toxic vs passive fills |

## Configuration

### MarketMakingBot

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_id` | `str` | required | Unique identifier for this bot instance |
| `client` | `ExchangeClient` | required | Exchange connection |
| `quote_engine` | `QuoteEngine` | required | Quote generation logic |
| `market_selector` | `ActiveMarketSelector` | required | Instrument selection logic |
| `risk` | `RiskCoordinator` | required | Shared risk coordinator |
| `inventory` | `InventoryManager` | `None` | Inventory tracker (default created if omitted) |
| `toxicity_tracker` | `FillToxicityTracker` | `None` | Fill toxicity monitoring |
| `environment` | `Environment` | `PAPER` | Paper or live trading |
| `max_instruments` | `int` | `5` | Maximum instruments to quote simultaneously |
| `refresh_interval` | `float` | `3.0` | Seconds between quote refresh cycles |

### AdaptiveQuoter

| Parameter | Default | Description |
|-----------|---------|-------------|
| `base_half_spread` | `0.015` | Base spread before volatility/toxicity adjustments |
| `vol_scale` | `2.0` | Multiplier on realized volatility |
| `min_half_spread` | `0.005` | Floor on effective spread |
| `max_half_spread` | `0.08` | Ceiling on effective spread |
| `size_usd` | `25.0` | Dollar amount per side per quote |
| `max_contracts` | `200.0` | Maximum contracts per order |
| `max_inventory` | `500.0` | Max inventory for skew calculation |
| `inventory_skew` | `0.5` | Quadratic skew sensitivity |
| `toxic_size_scale` | `10.0` | How much drift reduces toxic-side size |
| `max_toxic_reduction` | `0.5` | Max size reduction on toxic side (50%) |

## Inventory Netting

When the bot quotes both YES and NO on the same event (same `market_id`), inventory is netted across the pair. If you're long $100 YES and long $40 NO, the net YES inventory is $60 and net NO is -$60. This prevents the skew engine from treating each side independently, which would compound exposure.

## Safety Features

- **Drawdown circuit breaker** — equity check every iteration; breaches cancel all orders and halt the bot
- **Inventory limits** — skips buy orders at max long, sell orders at max short
- **Max contracts cap** — prevents runaway position sizes at low prices
- **Stale order cleanup** — cancel failures (404/409) remove orders from tracking
- **Toxicity-based spread widening** — automatic defense against informed flow

## CLI Usage

```bash
# Paper trading (default)
python scripts/run_bot.py mm --exchange polymarket

# Live trading
python scripts/run_bot.py mm --exchange kalshi --live
```

## Market Selection Guidance

For latency-disadvantaged setups, prefer:

- **Slow-information markets** — political outcomes, weather, long-dated sports futures where the edge is analytical, not speed-based
- **Wide natural spreads** — markets with 8-10c spreads where your 4c spread captures real value
- **Far from resolution** — avoid near-expiry binary options with high gamma

Avoid fast-moving price-action markets (e.g., BTC 15-minute contracts) where faster participants will consistently pick off stale quotes.
