# Chain Sync - On-Chain Transaction Tracking

This document explains the chain-synced transactions system that serves as the single source of truth for all trading activity.

## Overview

The system uses the `transactions` table as the **single source of truth** for all on-chain activity:

1. Syncs all on-chain transactions directly from Polygon
2. Computes positions from transaction history
3. Automatically detects claims, deposits, and withdrawals
4. Preserves agent attribution where possible

## Key Components

### Files

| File | Description |
|------|-------------|
| `polymarket/trading/chain_sync.py` | Chain sync service for fetching on-chain transactions |
| `scripts/migrate_to_chain_sync.py` | Migration script for initial backfill |
| `polymarket/trading/storage/sqlite.py` | `transactions` table and computed position queries |
| `polymarket/trading/risk_coordinator.py` | Uses chain sync for reconciliation |
| `webapp/services/trade_service.py` | Uses transactions table for trade history |

## Configuration

Configure chain sync via environment variables:

```bash
# .env file
CHAIN_SYNC_BATCH_SIZE=2000
CHAIN_SYNC_INITIAL_BLOCK=77000000  # Adjust based on your first trade date
```

### Contract Addresses

The following contracts are monitored:

- **CTF Contract**: `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
- **USDC Contract**: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`

## Initial Setup

### Step 1: Run Migration

```bash
# Fast sync using Polymarket API (recommended)
python scripts/migrate_to_chain_sync.py --wallet YOUR_WALLET_ADDRESS --mode fast

# Or full RPC-based sync
python scripts/migrate_to_chain_sync.py --wallet YOUR_WALLET_ADDRESS --mode full
```

Options:
- `--mode fast`: Uses Polymarket Data API (faster, recommended)
- `--mode full`: Uses direct RPC queries (more thorough)
- `--from-block N`: Start sync from specific block

### Step 2: Verify

```bash
# Run validation tests
pytest tests/test_chain_reconciliation.py -v
```

## How It Works

### Transaction Sync

On bot startup, the `RiskCoordinator` performs chain synchronization:

1. **First start**: Full historical sync from `CHAIN_SYNC_INITIAL_BLOCK`
2. **Subsequent starts**: Incremental sync from last synced block

### Computed Positions

Positions are computed dynamically from transaction history:

```sql
SELECT token_id,
       SUM(CASE WHEN transaction_type = 'buy' THEN shares ELSE 0 END) -
       SUM(CASE WHEN transaction_type = 'sell' THEN shares ELSE 0 END) -
       SUM(CASE WHEN transaction_type = 'claim' THEN shares ELSE 0 END) as net_shares
FROM transactions
GROUP BY token_id
HAVING net_shares > 0
```

## API Reference

### RiskCoordinator Methods

```python
# Get positions computed from transactions
coordinator.get_computed_positions()

# Get exposure from transaction history
coordinator.get_computed_exposure(agent_id="my-agent")

# Get transaction history
coordinator.get_transaction_history(transaction_type="buy", limit=100)

# Manually trigger sync
await coordinator.sync_transactions(full_sync=False)
```

### TradeService Methods

```python
# Get all transactions from chain sync
trade_service.get_all_transactions(wallet_address, transaction_type="buy")

# Get computed positions
trade_service.get_computed_positions(wallet_address)

# Get chain sync status
trade_service.get_chain_sync_status(wallet_address)
```

## Troubleshooting

### Issue: Missing transactions after sync

**Solution**: Run full sync again
```bash
python scripts/migrate_to_chain_sync.py --wallet YOUR_WALLET --mode fast
```

### Issue: Position mismatch between computed and on-chain

**Cause**: Usually a missed claim or unsynced transaction

**Solution**:
1. Check sync state
   ```python
   trade_service.get_chain_sync_status(wallet)
   ```
2. Run incremental sync
   ```python
   await coordinator.sync_transactions(full_sync=False)
   ```

### Issue: Agent attribution missing

**Solution**: Run execution matching
```bash
python scripts/migrate_to_chain_sync.py --wallet YOUR_WALLET --mode fast
```

This will attempt to match existing executions with synced transactions.

## Performance Notes

- **Initial sync**: May take 1-5 minutes with `--mode fast`
- **Incremental sync**: Usually < 1 second
- **Block timestamp caching**: Reduces RPC calls
- **Batch size**: Configurable via `CHAIN_SYNC_BATCH_SIZE`
