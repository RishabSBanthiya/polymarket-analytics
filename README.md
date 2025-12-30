# Polymarket Analytics

A bulletproof multi-agent trading infrastructure for Polymarket, featuring:
- **Multi-agent risk coordination** with atomic capital reservation
- **Composable trading bots** with pluggable components
- **Real-time flow detection** via WebSocket
- **Comprehensive backtesting** with bias warnings

## Quick Start

```bash
# Install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure credentials
cp .env.example .env  # Edit with your keys

# Run bots in dry-run mode
python run_bot.py bond --dry-run
python run_bot.py flow --dry-run

# Monitor agents
python risk_monitor.py status
```

---

## Architecture

```
polymarket-analytics/
├── core/                      # Shared infrastructure
│   ├── models.py              # Dataclasses (Market, Position, Signal, etc.)
│   ├── api.py                 # Async Polymarket API client
│   ├── config.py              # Validated configuration
│   └── rate_limiter.py        # Shared rate limiting
│
├── trading/                   # Live trading infrastructure
│   ├── bot.py                 # Composition-based TradingBot
│   ├── risk_coordinator.py    # Multi-agent risk management
│   ├── safety.py              # Circuit breakers, drawdown limits
│   ├── storage/               # Pluggable storage backends
│   │   ├── base.py            # Abstract interface
│   │   └── sqlite.py          # SQLite implementation
│   └── components/            # Pluggable trading components
│       ├── signals.py         # Signal sources
│       ├── sizers.py          # Position sizers
│       └── executors.py       # Execution engines
│
├── strategies/                # Strategy configurations
│   ├── bond_strategy.py       # Expiring market strategy
│   └── flow_strategy.py       # Flow copy strategy
│
├── backtesting/               # Backtesting framework
│   ├── base.py                # BaseBacktester
│   ├── results.py             # Results with bias warnings
│   ├── execution.py           # Simulated execution
│   └── strategies/            # Backtest implementations
│
├── flow_detector.py           # Real-time unusual flow detection
├── run_bot.py                 # Unified bot runner CLI
├── run_backtest.py            # Unified backtest runner CLI
├── risk_monitor.py            # Multi-agent monitoring CLI
└── check_portfolio.py         # Portfolio utility
```

---

## Trading Bots

### Bond Strategy (Expiring Markets)

Trades markets near expiration priced 95-98¢, betting they resolve to $1.

```bash
# Dry run
python run_bot.py bond --dry-run --interval 10

# Live trading
python run_bot.py bond --agent-id bond-1 --interval 10

# Custom price range
python run_bot.py bond --min-price 0.94 --max-price 0.99
```

### Flow Copy Strategy

Copies unusual flow signals (smart money, oversized bets, coordinated wallets).

```bash
# Dry run
python run_bot.py flow --dry-run --interval 5

# With minimum signal score
python run_bot.py flow --min-score 40 --min-trade-size 500

# Filter by category
python run_bot.py flow --category crypto
```

### Running Multiple Agents

```bash
# Start multiple agents (they coordinate via shared SQLite)
python run_bot.py bond --agent-id bond-1 &
python run_bot.py bond --agent-id bond-2 &
python run_bot.py flow --agent-id flow-1 &

# Monitor all agents
python risk_monitor.py agents

# Emergency stop
python risk_monitor.py stop-all
```

---

## Risk Management

The `RiskCoordinator` provides bulletproof multi-agent risk management:

### Features

- **Atomic Capital Reservation**: No race conditions between agents
- **State Reconciliation**: Syncs DB with on-chain state on startup
- **Exposure Limits**: Per-wallet, per-agent, per-market limits
- **Circuit Breaker**: Stops trading after consecutive failures
- **Drawdown Limits**: Stops trading on excessive losses
- **Agent Heartbeats**: Detects crashed agents

### Configuration

Set via environment variables or `.env`:

```bash
# Exposure limits (as fraction of total equity)
MAX_WALLET_EXPOSURE_PCT=0.80      # 80% max exposure
MAX_PER_AGENT_EXPOSURE_PCT=0.40   # 40% per agent
MAX_PER_MARKET_EXPOSURE_PCT=0.15  # 15% per market

# Trade limits
MIN_TRADE_VALUE_USD=5.0
MAX_TRADE_VALUE_USD=1000.0
MAX_SPREAD_PCT=0.03
MAX_SLIPPAGE_PCT=0.01

# Safety limits
MAX_DAILY_DRAWDOWN_PCT=0.10       # 10% daily stop
MAX_TOTAL_DRAWDOWN_PCT=0.25       # 25% total stop
CIRCUIT_BREAKER_FAILURES=5        # Stop after 5 failures

# Timing
RESERVATION_TTL_SECONDS=60
HEARTBEAT_INTERVAL_SECONDS=30
```

### Monitoring

```bash
# Overall status
python risk_monitor.py status

# List agents
python risk_monitor.py agents

# View positions
python risk_monitor.py positions

# View reservations
python risk_monitor.py reservations

# Drawdown status
python risk_monitor.py drawdown

# Cleanup stale data
python risk_monitor.py cleanup

# Emergency stop all
python risk_monitor.py stop-all --yes
```

---

## Flow Detection

Real-time unusual flow detection via Polymarket WebSocket.

### Detection Signals

| Signal | Description | Weight |
|--------|-------------|--------|
| SMART_MONEY_ACTIVITY | Wallets with >65% win rate | 30 |
| OVERSIZED_BET | Trades 10x+ avg or >$10k | 25 |
| COORDINATED_WALLETS | Related wallets trading together | 25 |
| VOLUME_SPIKE | Volume 3x+ baseline | 10 |
| PRICE_ACCELERATION | Momentum building | 10 |
| SUDDEN_PRICE_MOVEMENT | Rapid price changes | 8 |
| FRESH_WALLET_ACTIVITY | New wallets | 5 |

### Standalone Usage

```bash
# Run flow detector directly
python flow_detector.py --verbose --min-trade-size 100

# Filter by category
python flow_detector.py --category crypto --verbose
```

---

## Backtesting

### Run Backtests

```bash
# Bond strategy
python run_backtest.py bond --capital 1000 --days 7

# Flow signals
python run_backtest.py flow --capital 1000 --days 7

# Save results
python run_backtest.py bond --output results.json
```

### Bias Warnings

All backtest results include important warnings:

- **Survivorship Bias**: Only resolved markets analyzed
- **Look-Ahead Bias**: Historical orderbooks not available
- **Execution Optimism**: Assumes fills at quoted prices

---

## Configuration

### Environment Variables

Create a `.env` file:

```bash
# Required for live trading
PRIVATE_KEY=0x...
POLYMARKET_PROXY_ADDRESS=0x...

# Optional
CHAIN_ID=137
POLYGON_RPC_URL=https://polygon-rpc.com
RISK_DB_PATH=data/risk_state.db
LOG_LEVEL=INFO

# API rate limiting (Polymarket limits: CLOB=9000, Data=1000, Gamma=4000 per 10s)
API_RATE_LIMIT_PER_10S=9000
API_RATE_LIMIT_WINDOW_SECONDS=10

# Risk limits (see Risk Management section)
```

---

## Utilities

### Portfolio Checker

```bash
python check_portfolio.py
```

---

## API Reference

### Polymarket APIs Used

- **RTDS WebSocket**: `wss://ws-live-data.polymarket.com` - Real-time trades
- **Gamma API**: `https://gamma-api.polymarket.com` - Market data
- **CLOB API**: `https://clob.polymarket.com` - Orderbook, prices, orders
- **Data API**: `https://data-api.polymarket.com` - Positions, activity

---

## Troubleshooting

### Bots not starting
- Check `.env` file has required credentials
- Verify `PRIVATE_KEY` and `POLYMARKET_PROXY_ADDRESS` are set

### Rate limit errors
- Reduce polling interval: `--interval 30`
- Check `API_RATE_LIMIT_PER_10S` setting (default: 9000 for CLOB API)
- For Data API endpoints, use 1000; for Gamma API, use 4000

### No signals detected
- Flow detector needs time to build market state
- Try lowering `--min-score` or `--min-trade-size`

### Circuit breaker triggered
```bash
# Check status
python risk_monitor.py status

# Reset by cleaning up
python risk_monitor.py cleanup
```

---

## License

MIT License
