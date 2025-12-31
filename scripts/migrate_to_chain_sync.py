#!/usr/bin/env python3
"""
Migration Script: Migrate existing data to chain-synced transactions table.

This script:
1. Backs up existing database
2. Runs full chain sync to populate transactions table
3. Matches existing executions to transactions for agent attribution
4. Validates computed positions match on-chain state
5. Generates migration report

Usage:
    python scripts/migrate_to_chain_sync.py --wallet 0x... [--dry-run] [--from-block N]

Options:
    --wallet        Wallet address to migrate (required)
    --dry-run       Don't write to database, just report what would happen
    --from-block    Starting block for chain sync (default: auto-detect)
    --backup-only   Only create backup, don't run migration
    --skip-backup   Skip backup step (use with caution)
"""

import asyncio
import argparse
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple, Dict

# Add parent directory to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from polymarket.core.config import Config, get_config
from polymarket.core.api import PolymarketAPI
from polymarket.trading.storage.sqlite import SQLiteStorage
from polymarket.trading.chain_sync import ChainSyncService, fast_sync_from_api

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger(__name__)


class MigrationReport:
    """Tracks migration progress and results"""
    
    def __init__(self):
        self.start_time = datetime.now(timezone.utc)
        self.end_time: Optional[datetime] = None
        self.backup_path: Optional[str] = None
        self.transactions_synced = 0
        self.executions_matched = 0
        self.executions_unmatched = 0
        self.claims_migrated = 0
        self.positions_computed = 0
        self.positions_on_chain = 0
        self.discrepancies: List[str] = []
        self.errors: List[str] = []
    
    def finalize(self):
        self.end_time = datetime.now(timezone.utc)
    
    def print_report(self):
        duration = (self.end_time - self.start_time).total_seconds() if self.end_time else 0
        
        print("\n" + "=" * 60)
        print("📋 MIGRATION REPORT")
        print("=" * 60)
        print(f"  Start Time:       {self.start_time.isoformat()}")
        print(f"  End Time:         {self.end_time.isoformat() if self.end_time else 'N/A'}")
        print(f"  Duration:         {duration:.1f}s")
        print(f"  Backup Path:      {self.backup_path or 'N/A'}")
        print()
        print("📊 SYNC RESULTS:")
        print(f"  Transactions:     {self.transactions_synced}")
        print(f"  Executions:       {self.executions_matched} matched, {self.executions_unmatched} unmatched")
        print(f"  Claims Migrated:  {self.claims_migrated}")
        print()
        print("📦 POSITIONS:")
        print(f"  Computed:         {self.positions_computed}")
        print(f"  On-Chain:         {self.positions_on_chain}")
        print()
        
        if self.discrepancies:
            print("⚠️  DISCREPANCIES:")
            for d in self.discrepancies[:10]:
                print(f"    - {d}")
            if len(self.discrepancies) > 10:
                print(f"    ... and {len(self.discrepancies) - 10} more")
        else:
            print("✅ No discrepancies found")
        
        if self.errors:
            print("\n❌ ERRORS:")
            for e in self.errors:
                print(f"    - {e}")
        
        print("=" * 60)


def backup_database(db_path: str) -> str:
    """Create timestamped backup of database"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.backup_{timestamp}"
    
    shutil.copy2(db_path, backup_path)
    logger.info(f"Created backup: {backup_path}")
    
    return backup_path


async def match_executions_to_transactions(
    storage: SQLiteStorage,
    wallet_address: str,
    dry_run: bool = False
) -> Tuple[int, int]:
    """
    Match existing executions to synced transactions for agent attribution.
    
    Returns (matched, unmatched) counts.
    """
    matched = 0
    unmatched = 0
    
    with storage.transaction() as txn:
        # Get all executions
        executions = txn.get_executions(wallet_address=wallet_address)
        logger.info(f"Found {len(executions)} executions to match")
        
        # Get all transactions that don't have agent_id
        transactions = txn.get_transactions(
            wallet_address=wallet_address,
            transaction_type=None
        )
        
        # Filter to unattributed transactions
        unattributed = [t for t in transactions if t.get("agent_id") is None]
        logger.info(f"Found {len(unattributed)} unattributed transactions")
        
        for execution in executions:
            exec_token = execution.get("token_id", "")
            exec_shares = execution.get("shares", 0) or 0
            exec_time = execution.get("timestamp")
            exec_side = execution.get("side", "")
            exec_agent = execution.get("agent_id", "")
            
            # Map execution side to transaction type
            if exec_side == "BUY":
                expected_type = "buy"
            elif exec_side == "SELL":
                expected_type = "sell"
            else:
                unmatched += 1
                continue
            
            # Find matching transaction
            best_match = None
            best_time_diff = float('inf')
            
            for tx in unattributed:
                if tx.get("token_id") != exec_token:
                    continue
                if tx.get("transaction_type") != expected_type:
                    continue
                
                # Check time proximity (within 5 minutes)
                tx_time = tx.get("block_timestamp")
                if exec_time and tx_time:
                    time_diff = abs((tx_time - exec_time).total_seconds())
                    if time_diff > 300:  # 5 minutes
                        continue
                    
                    if time_diff < best_time_diff:
                        best_time_diff = time_diff
                        best_match = tx
                
                # Check shares proximity (within 5%)
                tx_shares = tx.get("shares", 0) or 0
                if exec_shares > 0 and tx_shares > 0:
                    shares_diff = abs(tx_shares - exec_shares) / exec_shares
                    if shares_diff > 0.05:
                        continue
            
            if best_match:
                if not dry_run:
                    txn.link_transaction_to_agent(
                        best_match["tx_hash"],
                        best_match["log_index"],
                        exec_agent
                    )
                matched += 1
                # Remove from unattributed list to avoid double-matching
                unattributed = [t for t in unattributed if t["id"] != best_match["id"]]
            else:
                unmatched += 1
    
    return matched, unmatched


async def migrate_claims_to_transactions(
    storage: SQLiteStorage,
    wallet_address: str,
    dry_run: bool = False
) -> int:
    """
    Migrate existing claims to transactions table.
    
    Note: Claims should already be detected via chain sync, but this ensures
    agent attribution is preserved from manual claims entries.
    """
    migrated = 0
    
    with storage.transaction() as txn:
        # Get existing claims (if method still exists - was deprecated)
        if not hasattr(txn, 'get_claims'):
            logger.info("Claims table deprecated - skipping migration")
            return 0
        claims = txn.get_claims()
        logger.info(f"Found {len(claims)} claims to migrate")
        
        for claim in claims:
            # Check if there's already a transaction for this claim
            # by matching token_id and approximate timing
            existing = txn.get_transactions(
                wallet_address=wallet_address,
                transaction_type="claim",
                token_id=claim.get("token_id"),
                limit=10
            )
            
            # Look for matching transaction
            claim_time = claim.get("claim_time")
            matched = False
            
            for tx in existing:
                tx_time = tx.get("block_timestamp")
                if claim_time and tx_time:
                    time_diff = abs((tx_time - claim_time).total_seconds())
                    if time_diff < 300:  # Within 5 minutes
                        # Link agent to this transaction
                        if claim.get("agent_id") and not tx.get("agent_id") and not dry_run:
                            txn.link_transaction_to_agent(
                                tx["tx_hash"],
                                tx["log_index"],
                                claim["agent_id"]
                            )
                        matched = True
                        migrated += 1
                        break
            
            if not matched:
                logger.warning(f"No matching transaction found for claim {claim.get('id')}")
    
    return migrated


async def validate_migration(
    storage: SQLiteStorage,
    api: PolymarketAPI,
    wallet_address: str
) -> Tuple[bool, List[str]]:
    """
    Validate that computed positions match on-chain state.
    """
    discrepancies = []
    
    # Get computed positions from transactions
    with storage.transaction() as txn:
        computed_positions = txn.get_computed_positions(wallet_address)
    
    # Get actual positions from API
    actual_positions = await api.fetch_positions(wallet_address)
    
    # Build lookups
    computed_by_token = {p["token_id"]: p for p in computed_positions}
    actual_by_token = {p.token_id: p for p in actual_positions}
    
    # Check each actual position
    for token_id, actual in actual_by_token.items():
        if token_id not in computed_by_token:
            discrepancies.append(
                f"Missing position {token_id[:20]}... - {actual.shares:.6f} shares on-chain but not in computed"
            )
            continue
        
        computed = computed_by_token[token_id]
        actual_shares = actual.shares
        computed_shares = computed["shares"]
        
        if abs(actual_shares - computed_shares) > 0.0001:
            discrepancies.append(
                f"Share mismatch {token_id[:20]}... - "
                f"on-chain: {actual_shares:.6f}, computed: {computed_shares:.6f}"
            )
    
    # Check for ghost computed positions
    for token_id, computed in computed_by_token.items():
        if token_id not in actual_by_token and computed["shares"] > 0.0001:
            discrepancies.append(
                f"Ghost position {token_id[:20]}... - "
                f"{computed['shares']:.6f} shares in computed but not on-chain"
            )
    
    return len(discrepancies) == 0, discrepancies


async def run_migration(
    wallet_address: str,
    dry_run: bool = False,
    from_block: Optional[int] = None,
    skip_backup: bool = False,
    use_fast_sync: bool = True  # Use API-based sync by default
) -> MigrationReport:
    """Run the full migration process."""
    report = MigrationReport()
    config = get_config()
    storage = SQLiteStorage(config.db_path)
    api = None
    chain_sync = None
    
    try:
        # Step 1: Backup database
        if not skip_backup and not dry_run:
            try:
                report.backup_path = backup_database(config.db_path)
            except Exception as e:
                report.errors.append(f"Backup failed: {e}")
                logger.error(f"Backup failed: {e}")
                return report
        else:
            logger.info("Skipping backup (dry-run or --skip-backup)")
        
        # Step 2: Initialize API
        api = PolymarketAPI(config)
        await api.connect()
        
        # Step 3: Run sync (fast API-based or full chain scan)
        if use_fast_sync:
            logger.info("Starting fast sync from Polymarket API...")
            if not dry_run:
                result = await fast_sync_from_api(wallet_address, config)
                report.transactions_synced = result.transactions_synced
                
                if not result.success:
                    for error in result.errors:
                        report.errors.append(f"Fast sync error: {error}")
            else:
                logger.info("Dry-run: Would sync transactions from API")
                with storage.transaction() as txn:
                    report.transactions_synced = txn.count_transactions(wallet_address)
        else:
            logger.info("Starting full chain sync (this may take a while)...")
            chain_sync = ChainSyncService(config, storage, api)
            
            if not dry_run:
                result = await chain_sync.full_sync(
                    wallet_address,
                    from_block=from_block,
                    match_existing_executions=True
                )
                report.transactions_synced = result.transactions_synced
                
                if not result.success:
                    for error in result.errors:
                        report.errors.append(f"Chain sync error: {error}")
            else:
                logger.info("Dry-run: Would sync transactions from chain")
                with storage.transaction() as txn:
                    report.transactions_synced = txn.count_transactions(wallet_address)
        
        # Step 4: Match executions to transactions
        logger.info("Matching executions to transactions...")
        matched, unmatched = await match_executions_to_transactions(
            storage, wallet_address, dry_run
        )
        report.executions_matched = matched
        report.executions_unmatched = unmatched
        
        # Step 5: Migrate claims
        logger.info("Migrating claims...")
        report.claims_migrated = await migrate_claims_to_transactions(
            storage, wallet_address, dry_run
        )
        
        # Step 6: Validate
        logger.info("Validating migration...")
        is_valid, discrepancies = await validate_migration(storage, api, wallet_address)
        report.discrepancies = discrepancies
        
        # Get position counts
        with storage.transaction() as txn:
            computed = txn.get_computed_positions(wallet_address)
            report.positions_computed = len(computed)
        
        actual = await api.fetch_positions(wallet_address)
        report.positions_on_chain = len(actual)
        
        logger.info("Migration complete!")
        
    except Exception as e:
        report.errors.append(f"Migration error: {e}")
        logger.exception(f"Migration failed: {e}")
    finally:
        if api:
            await api.close()
        if chain_sync:
            await chain_sync.close()
        report.finalize()
    
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Migrate existing data to chain-synced transactions table"
    )
    parser.add_argument(
        "--wallet",
        required=True,
        help="Wallet address to migrate"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write to database, just report what would happen"
    )
    parser.add_argument(
        "--from-block",
        type=int,
        default=None,
        help="Starting block for chain sync"
    )
    parser.add_argument(
        "--backup-only",
        action="store_true",
        help="Only create backup, don't run migration"
    )
    parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="Skip backup step"
    )
    parser.add_argument(
        "--full-chain-scan",
        action="store_true",
        help="Use full blockchain scan instead of fast API sync (slower, but more complete)"
    )
    
    args = parser.parse_args()
    
    if args.backup_only:
        config = get_config()
        backup_path = backup_database(config.db_path)
        print(f"Backup created: {backup_path}")
        return
    
    use_fast = not args.full_chain_scan
    
    print(f"\n{'=' * 60}")
    print("🔄 CHAIN SYNC MIGRATION")
    print(f"{'=' * 60}")
    print(f"  Wallet:    {args.wallet}")
    print(f"  Dry Run:   {args.dry_run}")
    print(f"  Mode:      {'Fast (API-based)' if use_fast else 'Full chain scan'}")
    if not use_fast:
        print(f"  From Block: {args.from_block or 'auto'}")
    print(f"{'=' * 60}\n")
    
    if not args.dry_run:
        confirm = input("This will modify your database. Continue? [y/N] ")
        if confirm.lower() != 'y':
            print("Aborted.")
            return
    
    report = asyncio.run(run_migration(
        wallet_address=args.wallet,
        dry_run=args.dry_run,
        from_block=args.from_block,
        skip_backup=args.skip_backup,
        use_fast_sync=use_fast
    ))
    
    report.print_report()
    
    if report.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

