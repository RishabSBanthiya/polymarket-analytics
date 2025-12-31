# Polymarket Analytics

A bulletproof multi-agent trading infrastructure for Polymarket, featuring:
- **Multi-agent risk coordination** with atomic capital reservation
- **Composable trading bots** with pluggable components
- **Real-time flow detection** via WebSocket
- **Comprehensive backtesting** with bias warnings

> **Technical Deep-Dive**: See [AGENTS.md](AGENTS.md) for architecture details, API documentation, and how to implement new strategies.

---

## How It Works

```mermaid
flowchart TB
    subgraph External[External APIs]
        RTDS[RTDS WebSocket<br/>Real-time Trades]
        GAMMA[Gamma API<br/>Market Data]
        CLOB[CLOB API<br/>Orderbook & Orders]
        DATA[Data API<br/>Positions]
        POLY[Polygon RPC<br/>USDC Balance]
    end

    subgraph Agents[Trading Agents]
        BOND[Bond Bot<br/>Expiring Markets]
        FLOW[Flow Bot<br/>Copy Trading]
    end

    subgraph Core[Core Infrastructure]
        RISK[Risk Coordinator<br/>Capital Management]
        DETECT[Flow Detector<br/>Signal Generation]
        STORE[(SQLite<br/>State & History)]
    end

    RTDS --> DETECT
    GAMMA --> BOND
    GAMMA --> FLOW
    CLOB --> BOND
    CLOB --> FLOW
    DATA --> RISK
    POLY --> RISK

    DETECT --> FLOW
    BOND --> RISK
    FLOW --> RISK
    RISK --> STORE
    RISK --> CLOB
```

**Trading Flow:**
1. **Flow Detector** monitors real-time trades via WebSocket, detecting unusual activity
2. **Trading Agents** (Bond/Flow) generate signals and request capital from Risk Coordinator
3. **Risk Coordinator** atomically reserves capital, enforces limits, and tracks positions
4. **Execution** happens via CLOB API with slippage protection and exit strategies

### Trading Lifecycle

```mermaid
sequenceDiagram
    participant Signal as Signal Source
    participant Bot as Trading Bot
    participant Risk as Risk Coordinator
    participant Exec as Execution Engine
    participant CLOB as CLOB API
    participant Exit as Exit Monitor

    Signal->>Bot: Generate Signal
    Bot->>Bot: Calculate Position Size
    Bot->>Risk: Request Capital Reservation
    Risk->>Risk: Check Limits (Wallet/Agent/Market)
    alt Capital Available
        Risk->>Bot: Reservation ID
        Bot->>Exec: Execute Trade
        Exec->>CLOB: Place Order
        CLOB->>Exec: Order Filled
        Exec->>Bot: Execution Result
        Bot->>Risk: Confirm Execution
        Risk->>Risk: Update Position
        Bot->>Exit: Register Position
        loop Monitor Position
            Exit->>Exit: Check Exit Conditions
            alt Exit Triggered
                Exit->>Exec: Exit Trade
                Exec->>CLOB: Place Exit Order
                Exit->>Risk: Close Position
            end
        end
    else Capital Unavailable
        Risk->>Bot: Reject (Limit Exceeded)
    end
```

---

## Quick Start

### Prerequisites

- Python 3.10+
- Polymarket account with trading enabled
- Polygon wallet with USDC

### Installation

```bash
# Clone and setup
git clone <repo-url>
cd polymarket-analytics

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env  # Edit with your keys
```

### Running Bots

```bash
# Dry run (no real trades)
python run_bot.py bond --dry-run
python run_bot.py flow --dry-run

# Live trading
python run_bot.py bond --agent-id bond-1 --interval 10
python run_bot.py flow --agent-id flow-1 --interval 5

# Monitor agents
python scripts/risk_monitor.py status
```

---

## Trading Strategies

### Bond Strategy (Expiring Markets)

Trades markets near expiration priced 95-98¢, betting they resolve to $1. Behaves like short-term bonds with high probability of small gain.

```mermaid
flowchart TB
    subgraph Scan[Market Scanning]
        SCAN[Scan Markets<br/>Expiring 60s-30min]
        FILTER[Filter by Price<br/>95-98¢ Range]
        SCORE[Score Opportunities<br/>Time + Expected Return]
    end
    
    subgraph Execution[Trade Execution]
        SIZE[Kelly Position Sizing<br/>Half-Kelly, 25% Max]
        RESERVE[Reserve Capital<br/>Atomic Transaction]
        EXEC[Execute Trade<br/>IOC Order]
    end
    
    subgraph Monitoring[Risk Monitoring]
        HEDGE[Hedge Monitor<br/>Adverse Price Moves]
        DIVERSIFY[Time-Bucketed<br/>Diversification]
        ORPHAN[Orphan Handler<br/>Crashed Trades]
    end
    
    SCAN --> FILTER
    FILTER --> SCORE
    SCORE --> SIZE
    SIZE --> RESERVE
    RESERVE --> EXEC
    EXEC --> HEDGE
    HEDGE --> DIVERSIFY
    DIVERSIFY --> ORPHAN
```

```bash
# Dry run
python run_bot.py bond --dry-run --interval 10

# Live trading
python run_bot.py bond --agent-id bond-1 --interval 10

# Custom price range
python run_bot.py bond --min-price 0.94 --max-price 0.99
```

### Flow Copy Strategy

Copies unusual flow signals from smart money, oversized bets, and coordinated wallets.

```mermaid
flowchart TB
    subgraph Signal[Signal Processing]
        ALERT[Flow Alert<br/>From Detector]
        SCORE[Composite Score<br/>Weighted Signals]
        DECAY[Time Decay<br/>Signal Freshness]
        DEDUP[Deduplication<br/>Prevent Duplicates]
    end
    
    subgraph Trade[Trade Execution]
        SIZE[Signal-Scaled Sizing<br/>Higher Score = Larger]
        EXEC[Execute Trade<br/>Aggressive IOC]
    end
    
    subgraph Exit[Exit Strategy]
        TP[Take Profit<br/>5% Gain]
        TS[Trailing Stop<br/>2% Activate, 1% Trail]
        SL[Stop Loss<br/>25% Loss]
        MAX[Max Hold<br/>75 Minutes]
    end
    
    ALERT --> SCORE
    SCORE --> DECAY
    DECAY --> DEDUP
    DEDUP --> SIZE
    SIZE --> EXEC
    EXEC --> TP
    EXEC --> TS
    EXEC --> SL
    EXEC --> MAX
```

```bash
# Dry run
python run_bot.py flow --dry-run --interval 5

# With minimum signal score
python run_bot.py flow --min-score 40 --min-trade-size 500

# Filter by category
python run_bot.py flow --category crypto
```

### Running Multiple Agents

Agents coordinate via shared SQLite database - no race conditions.

```bash
# Start multiple agents
python run_bot.py bond --agent-id bond-1 &
python run_bot.py bond --agent-id bond-2 &
python run_bot.py flow --agent-id flow-1 &

# Monitor all agents
python scripts/risk_monitor.py agents

# Emergency stop
python scripts/risk_monitor.py stop-all --yes
```

---

## Risk Management

The `RiskCoordinator` provides bulletproof multi-agent risk management:

```mermaid
flowchart TB
    subgraph Request[Trade Request]
        REQ[Agent Requests Capital]
    end
    
    subgraph Checks[Risk Checks]
        WALLET{Wallet Limit<br/>80% equity}
        AGENT{Agent Limit<br/>40% equity}
        MARKET{Market Limit<br/>15% equity}
        AVAIL{Available<br/>Capital?}
        CIRCUIT{Circuit Breaker<br/>OK?}
        DRAWDOWN{Drawdown<br/>OK?}
    end
    
    subgraph Action[Action]
        RESERVE[Atomic Reservation<br/>SQLite Transaction]
        REJECT[Reject Request]
    end
    
    REQ --> CIRCUIT
    CIRCUIT -->|Fail| REJECT
    CIRCUIT -->|Pass| DRAWDOWN
    DRAWDOWN -->|Fail| REJECT
    DRAWDOWN -->|Pass| WALLET
    WALLET -->|Fail| REJECT
    WALLET -->|Pass| AGENT
    AGENT -->|Fail| REJECT
    AGENT -->|Pass| MARKET
    MARKET -->|Fail| REJECT
    MARKET -->|Pass| AVAIL
    AVAIL -->|Fail| REJECT
    AVAIL -->|Pass| RESERVE
    
    style REJECT fill:#ffcccc
    style RESERVE fill:#ccffcc
```

| Feature | Description |
|---------|-------------|
| **Atomic Reservation** | No race conditions between agents |
| **State Reconciliation** | Syncs DB with on-chain state on startup |
| **Exposure Limits** | Per-wallet, per-agent, per-market limits |
| **Circuit Breaker** | Stops trading after consecutive failures |
| **Drawdown Limits** | Stops trading on excessive losses |
| **Agent Heartbeats** | Detects crashed agents |

### Monitoring

```bash
python scripts/risk_monitor.py status       # Overall status
python scripts/risk_monitor.py agents       # List agents
python scripts/risk_monitor.py positions    # View positions
python scripts/risk_monitor.py drawdown     # Drawdown status
python scripts/risk_monitor.py cleanup      # Cleanup stale data
```

---

## Flow Detection

Real-time unusual flow detection via Polymarket WebSocket.

```mermaid
flowchart LR
    subgraph Input[Data Sources]
        WS[RTDS WebSocket<br/>Real-time Trades]
        POLY[Polygon RPC<br/>On-chain Data]
    end
    
    subgraph Tracking[State Tracking]
        MARKET[Market State<br/>Price/Volume History]
        WALLET[Wallet Profile<br/>History & Stats]
    end
    
    subgraph Detection[Detection Algorithms]
        SMART[Smart Money<br/>High Win Rate]
        OVERSIZED[Oversized Bet<br/>10x+ Median]
        COORD[Coordinated<br/>Connected Wallets]
        VOLUME[Volume Spike<br/>3x Baseline]
        PRICE[Price Movement<br/>Z-score > 2.5]
        ACCEL[Acceleration<br/>Momentum]
        FRESH[Fresh Wallet<br/><7 Days Old]
    end
    
    subgraph Output[Alert Generation]
        SCORE[Composite Score<br/>Weighted Sum]
        DEDUP[Deduplication<br/>Cooldown]
        ALERT[Flow Alert]
    end
    
    WS --> MARKET
    WS --> WALLET
    POLY --> WALLET
    
    MARKET --> OVERSIZED
    MARKET --> VOLUME
    MARKET --> PRICE
    MARKET --> ACCEL
    WALLET --> SMART
    WALLET --> COORD
    WALLET --> FRESH
    
    OVERSIZED --> SCORE
    SMART --> SCORE
    COORD --> SCORE
    VOLUME --> SCORE
    PRICE --> SCORE
    ACCEL --> SCORE
    FRESH --> SCORE
    
    SCORE --> DEDUP
    DEDUP --> ALERT
    
    style ALERT fill:#ffeb3b
```

| Signal | Description | Weight |
|--------|-------------|--------|
| SMART_MONEY_ACTIVITY | Wallets with >65% win rate | 30 |
| OVERSIZED_BET | Trades 10x+ avg or >$10k | 25 |
| COORDINATED_WALLETS | On-chain connected wallets trading together | 25 |
| VOLUME_SPIKE | Volume 3x+ baseline | 10 |
| PRICE_ACCELERATION | Momentum building | 10 |
| SUDDEN_PRICE_MOVEMENT | Rapid price changes | 8 |
| FRESH_WALLET_ACTIVITY | New wallets (<7 days on-chain) | 5 |

```bash
# Run flow detector standalone
python polymarket/flow_detector.py --verbose --min-trade-size 100
```

---

## Backtesting

```bash
# Bond strategy
python scripts/run_backtest.py bond --capital 1000 --days 7

# Flow signals
python scripts/run_backtest.py flow --capital 1000 --days 7

# Save results
python scripts/run_backtest.py bond --output results.json
```

**Bias Warnings** (included in all results):
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
```

### Risk Limits

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
```

---

## API Reference

| API | Base URL | Purpose | Rate Limit |
|-----|----------|---------|------------|
| **RTDS WebSocket** | `wss://ws-live-data.polymarket.com` | Real-time trades | N/A |
| **Gamma API** | `https://gamma-api.polymarket.com` | Market metadata | 4,000/10s |
| **CLOB API** | `https://clob.polymarket.com` | Orderbook, prices, orders | 9,000/10s |
| **Data API** | `https://data-api.polymarket.com` | Positions, activity | 1,000/10s |
| **Polygon RPC** | Various | USDC balance | Varies |

---

## Component Architecture

The system uses a composition-based architecture where trading bots are assembled from pluggable components:

```mermaid
graph TB
    subgraph Bot[TradingBot]
        SIGNAL[SignalSource<br/>Where signals come from]
        SIZER[PositionSizer<br/>How to size positions]
        EXEC[ExecutionEngine<br/>How to execute trades]
        EXIT[ExitMonitor<br/>When to exit]
    end
    
    subgraph Signals[Signal Sources]
        EXPIRING[ExpiringMarketSignals<br/>Markets near expiration]
        FLOW_ALERT[FlowAlertSignals<br/>Flow detector alerts]
    end
    
    subgraph Sizers[Position Sizers]
        FIXED[FixedFractionSizer<br/>Fixed % of capital]
        KELLY[KellyPositionSizer<br/>Kelly criterion]
        SCALED[SignalScaledSizer<br/>Based on signal score]
    end
    
    subgraph Executors[Execution Engines]
        DRY[DryRunExecutor<br/>Simulation only]
        AGGR[AggressiveExecutor<br/>IOC with slippage protection]
    end
    
    SIGNAL -.->|implements| EXPIRING
    SIGNAL -.->|implements| FLOW_ALERT
    SIZER -.->|implements| FIXED
    SIZER -.->|implements| KELLY
    SIZER -.->|implements| SCALED
    EXEC -.->|implements| DRY
    EXEC -.->|implements| AGGR
    
    style Bot fill:#e1f5ff
    style Signals fill:#fff4e1
    style Sizers fill:#e8f5e9
    style Executors fill:#fce4ec
```

## Project Structure

```
polymarket-analytics/
├── polymarket/
│   ├── core/                 # Shared infrastructure
│   │   ├── models.py         # Dataclasses (Market, Position, Signal)
│   │   ├── api.py            # Async Polymarket API client
│   │   ├── config.py         # Validated configuration
│   │   └── rate_limiter.py   # Shared rate limiting
│   │
│   ├── trading/              # Live trading infrastructure
│   │   ├── bot.py            # Composition-based TradingBot
│   │   ├── risk_coordinator.py
│   │   ├── safety.py         # Circuit breakers, drawdown limits
│   │   ├── storage/          # SQLite persistence
│   │   └── components/       # Pluggable components
│   │       ├── signals.py    # Signal sources
│   │       ├── sizers.py     # Position sizers
│   │       ├── executors.py  # Execution engines
│   │       └── exit_strategies.py
│   │
│   ├── strategies/           # Strategy implementations
│   │   ├── bond_strategy.py  # Expiring market strategy
│   │   └── flow_strategy.py  # Flow copy strategy
│   │
│   ├── backtesting/          # Backtesting framework
│   └── flow_detector.py      # Real-time flow detection
│
├── webapp/                   # Dashboard (FastAPI)
├── scripts/                  # CLI utilities
├── run_bot.py               # Unified bot runner
└── requirements.txt
```

---

## Troubleshooting

### Bots not starting
- Check `.env` file has required credentials
- Verify `PRIVATE_KEY` and `POLYMARKET_PROXY_ADDRESS` are set

### Rate limit errors
- Reduce polling interval: `--interval 30`
- Check `API_RATE_LIMIT_PER_10S` setting (default: 9000 for CLOB API)

### No signals detected
- Flow detector needs time to build market state
- Try lowering `--min-score` or `--min-trade-size`

### Circuit breaker triggered
```bash
python scripts/risk_monitor.py status   # Check status
python scripts/risk_monitor.py cleanup  # Reset
```

---

## Roadmap & TODOs

### Planned Features

- [ ] **Additional Strategies**: Mean reversion, news-based trading, arbitrage detection
- [ ] **Dashboard Improvements**: Real-time P&L charts, position visualization
- [ ] **Alert Integrations**: Telegram/Discord notifications for signals and fills
- [ ] **Multi-Wallet Support**: Coordinate trading across multiple wallets
- [ ] **Enhanced Backtesting**: Realistic slippage modeling, market impact simulation
- [ ] **Strategy Optimizer**: Bayesian parameter optimization for new strategies

### Known Issues / Technical Debt

- [ ] API rate limiting edge cases under sustained high load
- [ ] Orphan position cleanup could be more aggressive on market resolution
- [ ] WebSocket reconnection occasionally drops first few trades after reconnect
- [ ] CLOB API timeout handling could be more graceful
- [ ] Exit monitor position registration has 30s grace period (API sync delay)

---

## License

MIT License
