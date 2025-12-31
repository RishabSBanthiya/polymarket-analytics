"""
Validation tests for chain reconciliation.

These tests verify that the chain-synced transactions table
correctly reflects on-chain state.
"""

import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from typing import List

# Add parent directory to path for imports
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from polymarket.core.config import Config, ChainSyncConfig, RiskConfig
from polymarket.core.models import Position, PositionStatus
from polymarket.trading.storage.sqlite import SQLiteStorage
from polymarket.trading.chain_sync import ChainSyncService


# ==================== FIXTURES ====================

@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary database for testing"""
    db_path = str(tmp_path / "test_risk_state.db")
    storage = SQLiteStorage(db_path)
    return storage


@pytest.fixture
def test_config(tmp_path):
    """Create a test configuration"""
    return Config(
        db_path=str(tmp_path / "test_risk_state.db"),
        proxy_address="0x1234567890abcdef1234567890abcdef12345678",
        polygon_rpc_url="https://polygon-rpc.com",
        risk=RiskConfig(),
        chain_sync=ChainSyncConfig(
            enabled=True,
            batch_size=100,
            initial_sync_block=0,
        )
    )


@pytest.fixture
def mock_api():
    """Create a mock API"""
    api = MagicMock()
    api.connect = AsyncMock()
    api.close = AsyncMock()
    api.get_current_block = AsyncMock(return_value=50000000)
    api.get_block_timestamp = AsyncMock(return_value=datetime.now(timezone.utc))
    api.fetch_positions = AsyncMock(return_value=[])
    api.fetch_usdc_balance = AsyncMock(return_value=1000.0)
    api.fetch_ctf_transfer_events = AsyncMock(return_value=[])
    api.fetch_usdc_transfer_events = AsyncMock(return_value=[])
    return api


# ==================== UNIT TESTS ====================

class TestTransactionStorage:
    """Tests for transaction storage operations"""
    
    def test_upsert_transaction(self, temp_db):
        """Test inserting and updating transactions"""
        with temp_db.transaction() as txn:
            # Insert a new transaction
            tx_id = txn.upsert_transaction(
                tx_hash="0xabc123",
                log_index=0,
                block_number=50000000,
                block_timestamp=datetime.now(timezone.utc),
                transaction_type="buy",
                wallet_address="0x1234",
                token_id="123456789",
                market_id="market1",
                outcome="Yes",
                shares=100.0,
                price_per_share=0.50,
                usdc_amount=50.0,
                agent_id="test-agent"
            )
            
            assert tx_id is not None
            
            # Retrieve the transaction
            tx = txn.get_transaction("0xabc123", 0)
            assert tx is not None
            assert tx["transaction_type"] == "buy"
            assert tx["shares"] == 100.0
            assert tx["agent_id"] == "test-agent"
    
    def test_upsert_preserves_agent_id(self, temp_db):
        """Test that upserting doesn't overwrite existing agent_id with None"""
        with temp_db.transaction() as txn:
            # Insert with agent_id
            txn.upsert_transaction(
                tx_hash="0xdef456",
                log_index=0,
                block_number=50000000,
                block_timestamp=datetime.now(timezone.utc),
                transaction_type="buy",
                wallet_address="0x1234",
                agent_id="original-agent"
            )
            
            # Upsert same transaction without agent_id
            txn.upsert_transaction(
                tx_hash="0xdef456",
                log_index=0,
                block_number=50000000,
                block_timestamp=datetime.now(timezone.utc),
                transaction_type="buy",
                wallet_address="0x1234",
                agent_id=None  # Should not overwrite
            )
            
            # Verify agent_id preserved
            tx = txn.get_transaction("0xdef456", 0)
            assert tx["agent_id"] == "original-agent"
    
    def test_get_computed_positions(self, temp_db):
        """Test computing positions from transaction history"""
        wallet = "0x1234"
        token = "token123"
        now = datetime.now(timezone.utc)
        
        with temp_db.transaction() as txn:
            # Insert buy transaction
            txn.upsert_transaction(
                tx_hash="0xbuy1",
                log_index=0,
                block_number=50000000,
                block_timestamp=now,
                transaction_type="buy",
                wallet_address=wallet,
                token_id=token,
                market_id="market1",
                outcome="Yes",
                shares=100.0,
                price_per_share=0.50,
                usdc_amount=50.0
            )
            
            # Insert another buy
            txn.upsert_transaction(
                tx_hash="0xbuy2",
                log_index=0,
                block_number=50000001,
                block_timestamp=now,
                transaction_type="buy",
                wallet_address=wallet,
                token_id=token,
                market_id="market1",
                outcome="Yes",
                shares=50.0,
                price_per_share=0.60,
                usdc_amount=30.0
            )
            
            # Insert sell
            txn.upsert_transaction(
                tx_hash="0xsell1",
                log_index=0,
                block_number=50000002,
                block_timestamp=now,
                transaction_type="sell",
                wallet_address=wallet,
                token_id=token,
                market_id="market1",
                outcome="Yes",
                shares=30.0,
                price_per_share=0.70,
                usdc_amount=21.0
            )
            
            # Get computed positions
            positions = txn.get_computed_positions(wallet)
            
            assert len(positions) == 1
            pos = positions[0]
            assert pos["token_id"] == token
            # 100 + 50 - 30 = 120 shares remaining
            assert pos["shares"] == 120.0
            assert pos["total_bought"] == 150.0
            assert pos["total_sold"] == 30.0
    
    def test_get_computed_positions_excludes_claimed(self, temp_db):
        """Test that claimed positions are excluded"""
        wallet = "0x1234"
        token = "token123"
        now = datetime.now(timezone.utc)
        
        with temp_db.transaction() as txn:
            # Insert buy
            txn.upsert_transaction(
                tx_hash="0xbuy",
                log_index=0,
                block_number=50000000,
                block_timestamp=now,
                transaction_type="buy",
                wallet_address=wallet,
                token_id=token,
                shares=100.0
            )
            
            # Insert claim (full amount)
            txn.upsert_transaction(
                tx_hash="0xclaim",
                log_index=0,
                block_number=50000001,
                block_timestamp=now,
                transaction_type="claim",
                wallet_address=wallet,
                token_id=token,
                shares=100.0
            )
            
            # Get computed positions - should be empty
            positions = txn.get_computed_positions(wallet)
            assert len(positions) == 0
    
    def test_agent_computed_exposure(self, temp_db):
        """Test computing agent exposure from transactions"""
        wallet = "0x1234"
        agent = "test-agent"
        now = datetime.now(timezone.utc)
        
        with temp_db.transaction() as txn:
            # Insert buy for agent
            txn.upsert_transaction(
                tx_hash="0xbuy",
                log_index=0,
                block_number=50000000,
                block_timestamp=now,
                transaction_type="buy",
                wallet_address=wallet,
                token_id="token1",
                shares=100.0,
                price_per_share=0.50,
                usdc_amount=50.0,
                agent_id=agent
            )
            
            # Get agent exposure (using avg price from transactions)
            exposure = txn.get_agent_computed_exposure(agent)
            
            # 100 shares * 0.50 price = 50.0
            assert exposure == 50.0
    
    def test_link_transaction_to_agent(self, temp_db):
        """Test linking orphan transaction to agent"""
        now = datetime.now(timezone.utc)
        
        with temp_db.transaction() as txn:
            # Insert orphan transaction
            txn.upsert_transaction(
                tx_hash="0xorphan",
                log_index=0,
                block_number=50000000,
                block_timestamp=now,
                transaction_type="buy",
                wallet_address="0x1234",
                agent_id=None
            )
            
            # Link to agent
            success = txn.link_transaction_to_agent("0xorphan", 0, "my-agent")
            assert success
            
            # Verify link
            tx = txn.get_transaction("0xorphan", 0)
            assert tx["agent_id"] == "my-agent"
    
    def test_transaction_summary(self, temp_db):
        """Test transaction summary by type"""
        wallet = "0x1234"
        now = datetime.now(timezone.utc)
        
        with temp_db.transaction() as txn:
            # Insert various transaction types
            txn.upsert_transaction(
                tx_hash="0xbuy1", log_index=0, block_number=1,
                block_timestamp=now, transaction_type="buy",
                wallet_address=wallet, shares=100.0, usdc_amount=50.0
            )
            txn.upsert_transaction(
                tx_hash="0xbuy2", log_index=0, block_number=2,
                block_timestamp=now, transaction_type="buy",
                wallet_address=wallet, shares=50.0, usdc_amount=25.0
            )
            txn.upsert_transaction(
                tx_hash="0xsell1", log_index=0, block_number=3,
                block_timestamp=now, transaction_type="sell",
                wallet_address=wallet, shares=30.0, usdc_amount=20.0
            )
            txn.upsert_transaction(
                tx_hash="0xdeposit", log_index=0, block_number=4,
                block_timestamp=now, transaction_type="deposit",
                wallet_address=wallet, usdc_amount=1000.0
            )
            
            summary = txn.get_transaction_summary(wallet)
            
            assert summary["buy"]["count"] == 2
            assert summary["buy"]["total_usdc"] == 75.0
            assert summary["buy"]["total_shares"] == 150.0
            assert summary["sell"]["count"] == 1
            assert summary["deposit"]["count"] == 1


class TestChainSyncState:
    """Tests for chain sync state tracking"""
    
    def test_update_and_get_sync_state(self, temp_db):
        """Test updating and retrieving sync state"""
        wallet = "0x1234"
        
        with temp_db.transaction() as txn:
            # Initially no state
            state = txn.get_chain_sync_state(wallet)
            assert state is None
            
            # Update state
            txn.update_chain_sync_state(wallet, 50000000, 100)
            
            # Retrieve state
            state = txn.get_chain_sync_state(wallet)
            assert state is not None
            assert state["last_synced_block"] == 50000000
            assert state["total_transactions"] == 100
    
    def test_update_overwrites_state(self, temp_db):
        """Test that updating state overwrites previous values"""
        wallet = "0x1234"
        
        with temp_db.transaction() as txn:
            txn.update_chain_sync_state(wallet, 50000000, 100)
            txn.update_chain_sync_state(wallet, 50001000, 150)
            
            state = txn.get_chain_sync_state(wallet)
            assert state["last_synced_block"] == 50001000
            assert state["total_transactions"] == 150


# ==================== INTEGRATION TESTS ====================

class TestChainSyncService:
    """Integration tests for chain sync service"""
    
    @pytest.mark.asyncio
    async def test_full_sync_empty(self, temp_db, test_config, mock_api):
        """Test full sync with no transactions"""
        sync_service = ChainSyncService(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        result = await sync_service.full_sync("0x1234")
        
        assert result.success
        assert result.transactions_synced == 0
        assert len(result.errors) == 0
    
    @pytest.mark.asyncio
    async def test_full_sync_with_events(self, temp_db, test_config, mock_api):
        """Test full sync with mock events"""
        # Setup mock events
        mock_api.fetch_ctf_transfer_events = AsyncMock(return_value=[
            {
                "tx_hash": "0xtest1",
                "log_index": 0,
                "block_number": 50000000,
                "transaction_type": "buy",
                "wallet_address": "0x1234",
                "token_id": "123456",
                "shares": 100.0,
            }
        ])
        
        sync_service = ChainSyncService(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        result = await sync_service.full_sync("0x1234")
        
        assert result.success
        assert result.transactions_synced >= 1
    
    @pytest.mark.asyncio
    async def test_incremental_sync(self, temp_db, test_config, mock_api):
        """Test incremental sync from last synced block"""
        wallet = "0x1234"
        
        # Set initial sync state
        with temp_db.transaction() as txn:
            txn.update_chain_sync_state(wallet, 50000000, 10)
        
        mock_api.get_current_block = AsyncMock(return_value=50001000)
        
        sync_service = ChainSyncService(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        result = await sync_service.incremental_sync(wallet)
        
        assert result.success
        assert result.from_block == 50000001
        assert result.to_block == 50001000
    
    @pytest.mark.asyncio
    async def test_verify_sync_integrity_valid(self, temp_db, test_config, mock_api):
        """Test sync integrity verification with matching state"""
        wallet = "0x1234"
        now = datetime.now(timezone.utc)
        
        # Insert transaction
        with temp_db.transaction() as txn:
            txn.upsert_transaction(
                tx_hash="0xtest",
                log_index=0,
                block_number=50000000,
                block_timestamp=now,
                transaction_type="buy",
                wallet_address=wallet,
                token_id="token1",
                shares=100.0
            )
        
        # Mock API to return matching position
        mock_api.fetch_positions = AsyncMock(return_value=[
            Position(
                id=1,
                agent_id="",
                market_id="market1",
                token_id="token1",
                outcome="Yes",
                shares=100.0,
                entry_price=0.5,
                status=PositionStatus.OPEN
            )
        ])
        
        sync_service = ChainSyncService(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        is_valid, discrepancies = await sync_service.verify_sync_integrity(wallet)
        
        assert is_valid
        assert len(discrepancies) == 0
    
    @pytest.mark.asyncio
    async def test_verify_sync_integrity_mismatch(self, temp_db, test_config, mock_api):
        """Test sync integrity verification with mismatched shares"""
        wallet = "0x1234"
        now = datetime.now(timezone.utc)
        
        # Insert transaction with 100 shares
        with temp_db.transaction() as txn:
            txn.upsert_transaction(
                tx_hash="0xtest",
                log_index=0,
                block_number=50000000,
                block_timestamp=now,
                transaction_type="buy",
                wallet_address=wallet,
                token_id="token1",
                shares=100.0
            )
        
        # Mock API to return different shares
        mock_api.fetch_positions = AsyncMock(return_value=[
            Position(
                id=1,
                agent_id="",
                market_id="market1",
                token_id="token1",
                outcome="Yes",
                shares=150.0,  # Different from computed
                entry_price=0.5,
                status=PositionStatus.OPEN
            )
        ])
        
        sync_service = ChainSyncService(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        is_valid, discrepancies = await sync_service.verify_sync_integrity(wallet)
        
        assert not is_valid
        assert len(discrepancies) > 0
        assert "Share mismatch" in discrepancies[0]


# ==================== BALANCE VERIFICATION ====================

class TestBalanceVerification:
    """Tests for USDC balance verification"""
    
    def test_compute_expected_balance(self, temp_db):
        """Test computing expected balance from transactions"""
        wallet = "0x1234"
        now = datetime.now(timezone.utc)
        
        with temp_db.transaction() as txn:
            # Initial deposit
            txn.upsert_transaction(
                tx_hash="0xdeposit",
                log_index=0,
                block_number=1,
                block_timestamp=now,
                transaction_type="deposit",
                wallet_address=wallet,
                usdc_amount=1000.0
            )
            
            # Buy (costs USDC)
            txn.upsert_transaction(
                tx_hash="0xbuy",
                log_index=0,
                block_number=2,
                block_timestamp=now,
                transaction_type="buy",
                wallet_address=wallet,
                token_id="token1",
                shares=100.0,
                usdc_amount=50.0
            )
            
            # Sell (receives USDC)
            txn.upsert_transaction(
                tx_hash="0xsell",
                log_index=0,
                block_number=3,
                block_timestamp=now,
                transaction_type="sell",
                wallet_address=wallet,
                token_id="token1",
                shares=50.0,
                usdc_amount=35.0
            )
            
            # Claim (receives USDC)
            txn.upsert_transaction(
                tx_hash="0xclaim",
                log_index=0,
                block_number=4,
                block_timestamp=now,
                transaction_type="claim",
                wallet_address=wallet,
                token_id="token1",
                shares=50.0,
                usdc_amount=50.0
            )
            
            summary = txn.get_transaction_summary(wallet)
            
            # Expected balance = deposit - buy + sell + claim
            # = 1000 - 50 + 35 + 50 = 1035
            expected_balance = (
                summary["deposit"]["total_usdc"]
                - summary["buy"]["total_usdc"]
                + summary["sell"]["total_usdc"]
                + summary["claim"]["total_usdc"]
                - summary["withdrawal"]["total_usdc"]
            )
            
            assert expected_balance == 1035.0


# Run tests if executed directly
if __name__ == "__main__":
    pytest.main([__file__, "-v"])

