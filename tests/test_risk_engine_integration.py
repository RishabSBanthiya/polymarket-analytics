"""
Integration tests for the Risk Engine with chain sync.

These tests verify that the RiskCoordinator correctly uses
the transactions table for position and exposure calculations.
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from typing import List

# Add parent directory to path for imports
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from polymarket.core.config import Config, ChainSyncConfig, RiskConfig
from polymarket.core.models import Position, PositionStatus, ReservationStatus
from polymarket.trading.storage.sqlite import SQLiteStorage
from polymarket.trading.risk_coordinator import RiskCoordinator


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
        risk=RiskConfig(
            max_wallet_exposure_pct=0.80,
            max_per_agent_exposure_pct=0.40,
            max_per_market_exposure_pct=0.15,
            min_trade_value_usd=5.0,
            max_trade_value_usd=1000.0,
        ),
        chain_sync=ChainSyncConfig(
            enabled=True,
            batch_size=100,
        )
    )


@pytest.fixture
def test_config_legacy(tmp_path):
    """Create a test configuration with chain sync disabled"""
    return Config(
        db_path=str(tmp_path / "test_risk_state.db"),
        proxy_address="0x1234567890abcdef1234567890abcdef12345678",
        polygon_rpc_url="https://polygon-rpc.com",
        risk=RiskConfig(),
        chain_sync=ChainSyncConfig(
            enabled=False,
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


# ==================== COORDINATOR TESTS ====================

class TestRiskCoordinatorWithChainSync:
    """Test RiskCoordinator with chain sync enabled"""
    
    @pytest.mark.asyncio
    async def test_startup_with_chain_sync(self, temp_db, test_config, mock_api):
        """Test coordinator startup with chain sync"""
        coordinator = RiskCoordinator(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        success = await coordinator.startup("test-agent", "test")
        
        assert success
        assert coordinator._reconciled
        
        await coordinator.shutdown()
    
    @pytest.mark.asyncio
    async def test_get_computed_positions(self, temp_db, test_config, mock_api):
        """Test getting positions computed from transactions"""
        wallet = test_config.proxy_address
        now = datetime.now(timezone.utc)
        
        # Insert transactions
        with temp_db.transaction() as txn:
            txn.upsert_transaction(
                tx_hash="0xbuy",
                log_index=0,
                block_number=50000000,
                block_timestamp=now,
                transaction_type="buy",
                wallet_address=wallet,
                token_id="token1",
                market_id="market1",
                shares=100.0,
                price_per_share=0.50,
                agent_id="test-agent"
            )
        
        coordinator = RiskCoordinator(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        # Mock positions to match
        mock_api.fetch_positions = AsyncMock(return_value=[
            Position(
                id=1,
                agent_id="test-agent",
                market_id="market1",
                token_id="token1",
                outcome="Yes",
                shares=100.0,
                entry_price=0.50,
                status=PositionStatus.OPEN
            )
        ])
        
        await coordinator.startup("test-agent", "test")
        
        positions = coordinator.get_computed_positions()
        
        assert len(positions) == 1
        assert positions[0]["shares"] == 100.0
        assert positions[0]["token_id"] == "token1"
        
        await coordinator.shutdown()
    
    @pytest.mark.asyncio
    async def test_get_computed_exposure(self, temp_db, test_config, mock_api):
        """Test getting exposure computed from transactions"""
        wallet = test_config.proxy_address
        now = datetime.now(timezone.utc)
        
        # Insert transactions for agent
        with temp_db.transaction() as txn:
            txn.upsert_transaction(
                tx_hash="0xbuy1",
                log_index=0,
                block_number=50000000,
                block_timestamp=now,
                transaction_type="buy",
                wallet_address=wallet,
                token_id="token1",
                shares=100.0,
                price_per_share=0.50,
                agent_id="agent1"
            )
            txn.upsert_transaction(
                tx_hash="0xbuy2",
                log_index=0,
                block_number=50000001,
                block_timestamp=now,
                transaction_type="buy",
                wallet_address=wallet,
                token_id="token2",
                shares=50.0,
                price_per_share=0.40,
                agent_id="agent2"
            )
        
        coordinator = RiskCoordinator(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        # Mock positions
        mock_api.fetch_positions = AsyncMock(return_value=[
            Position(id=1, agent_id="agent1", market_id="m1", token_id="token1",
                    outcome="Yes", shares=100.0, entry_price=0.50, current_price=0.55,
                    status=PositionStatus.OPEN),
            Position(id=2, agent_id="agent2", market_id="m2", token_id="token2",
                    outcome="Yes", shares=50.0, entry_price=0.40, current_price=0.45,
                    status=PositionStatus.OPEN),
        ])
        
        await coordinator.startup("agent1", "test")
        
        # Get total exposure
        total_exposure = coordinator.get_computed_exposure()
        assert total_exposure > 0
        
        # Get agent-specific exposure
        agent1_exposure = coordinator.get_computed_exposure(agent_id="agent1")
        agent2_exposure = coordinator.get_computed_exposure(agent_id="agent2")
        
        # agent1: 100 shares * 0.50 = 50
        # agent2: 50 shares * 0.40 = 20
        assert agent1_exposure == 50.0
        assert agent2_exposure == 20.0
        
        await coordinator.shutdown()
    
    @pytest.mark.asyncio
    async def test_sync_transactions(self, temp_db, test_config, mock_api):
        """Test manual transaction sync"""
        coordinator = RiskCoordinator(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        await coordinator.startup("test-agent", "test")
        
        # Trigger manual sync
        synced = await coordinator.sync_transactions(full_sync=False)
        
        assert isinstance(synced, int)
        
        await coordinator.shutdown()


class TestReservationFlow:
    """Test the reservation -> execution -> confirm flow"""
    
    @pytest.mark.asyncio
    async def test_reserve_confirm_flow(self, temp_db, test_config, mock_api):
        """Test complete reservation flow"""
        wallet = test_config.proxy_address
        
        # Mock initial state with balance
        mock_api.fetch_usdc_balance = AsyncMock(return_value=1000.0)
        mock_api.fetch_positions = AsyncMock(return_value=[])
        
        coordinator = RiskCoordinator(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        await coordinator.startup("test-agent", "test")
        
        # Create reservation
        reservation_id = coordinator.atomic_reserve(
            agent_id="test-agent",
            market_id="market1",
            token_id="token1",
            amount_usd=50.0
        )
        
        assert reservation_id is not None
        
        # Confirm execution
        success = coordinator.confirm_execution(
            reservation_id=reservation_id,
            filled_shares=100.0,
            filled_price=0.50
        )
        
        assert success
        
        await coordinator.shutdown()
    
    @pytest.mark.asyncio
    async def test_reserve_release_flow(self, temp_db, test_config, mock_api):
        """Test reservation release on failure"""
        mock_api.fetch_usdc_balance = AsyncMock(return_value=1000.0)
        mock_api.fetch_positions = AsyncMock(return_value=[])
        
        coordinator = RiskCoordinator(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        await coordinator.startup("test-agent", "test")
        
        # Create reservation
        reservation_id = coordinator.atomic_reserve(
            agent_id="test-agent",
            market_id="market1",
            token_id="token1",
            amount_usd=50.0
        )
        
        assert reservation_id is not None
        
        # Release (trade failed)
        success = coordinator.release_reservation(reservation_id)
        
        assert success
        
        # Verify reservation is released
        with temp_db.transaction() as txn:
            reservation = txn.get_reservation(reservation_id)
            assert reservation.status == ReservationStatus.RELEASED
        
        await coordinator.shutdown()
    
    @pytest.mark.asyncio
    async def test_exposure_limits_enforced(self, temp_db, test_config, mock_api):
        """Test that exposure limits are enforced"""
        wallet = test_config.proxy_address
        
        # Set up with limited balance
        mock_api.fetch_usdc_balance = AsyncMock(return_value=100.0)
        mock_api.fetch_positions = AsyncMock(return_value=[])
        
        coordinator = RiskCoordinator(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        await coordinator.startup("test-agent", "test")
        
        # Try to reserve more than allowed
        # With 100 USDC and 80% max wallet exposure, max is 80
        reservation_id = coordinator.atomic_reserve(
            agent_id="test-agent",
            market_id="market1",
            token_id="token1",
            amount_usd=90.0  # Over the limit
        )
        
        assert reservation_id is None  # Should be denied
        
        await coordinator.shutdown()


class TestTransactionHistory:
    """Test transaction history access"""
    
    @pytest.mark.asyncio
    async def test_get_transaction_history(self, temp_db, test_config, mock_api):
        """Test retrieving transaction history"""
        wallet = test_config.proxy_address
        now = datetime.now(timezone.utc)
        
        # Insert various transactions
        with temp_db.transaction() as txn:
            for i in range(5):
                txn.upsert_transaction(
                    tx_hash=f"0xtx{i}",
                    log_index=0,
                    block_number=50000000 + i,
                    block_timestamp=now,
                    transaction_type="buy" if i % 2 == 0 else "sell",
                    wallet_address=wallet,
                    token_id="token1",
                    shares=10.0 * (i + 1),
                    agent_id="test-agent"
                )
        
        coordinator = RiskCoordinator(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        await coordinator.startup("test-agent", "test")
        
        # Get all history
        history = coordinator.get_transaction_history()
        assert len(history) == 5
        
        # Get filtered history
        buys = coordinator.get_transaction_history(transaction_type="buy")
        assert len(buys) == 3
        
        sells = coordinator.get_transaction_history(transaction_type="sell")
        assert len(sells) == 2
        
        await coordinator.shutdown()


class TestReconciliationIdempotency:
    """Test that reconciliation is idempotent"""
    
    @pytest.mark.asyncio
    async def test_reconciliation_idempotent(self, temp_db, test_config, mock_api):
        """Test that running reconciliation twice produces same state"""
        wallet = test_config.proxy_address
        
        mock_api.fetch_usdc_balance = AsyncMock(return_value=1000.0)
        mock_api.fetch_positions = AsyncMock(return_value=[
            Position(
                id=1,
                agent_id="test-agent",
                market_id="market1",
                token_id="token1",
                outcome="Yes",
                shares=100.0,
                entry_price=0.50,
                status=PositionStatus.OPEN
            )
        ])
        
        coordinator = RiskCoordinator(
            config=test_config,
            storage=temp_db,
            api=mock_api
        )
        
        # First reconciliation
        await coordinator.startup("test-agent", "test")
        state1 = coordinator.get_wallet_state()
        
        # Second reconciliation
        await coordinator._reconcile_state()
        state2 = coordinator.get_wallet_state()
        
        # States should be equivalent
        assert state1.usdc_balance == state2.usdc_balance
        
        await coordinator.shutdown()


class TestClaimDetection:
    """Test automatic claim detection from chain"""
    
    @pytest.mark.asyncio
    async def test_claims_detected_from_chain(self, temp_db, test_config, mock_api):
        """Test that claims are auto-detected from chain sync"""
        wallet = test_config.proxy_address
        now = datetime.now(timezone.utc)
        
        # Insert buy and claim transactions
        with temp_db.transaction() as txn:
            txn.upsert_transaction(
                tx_hash="0xbuy",
                log_index=0,
                block_number=50000000,
                block_timestamp=now - timedelta(days=1),
                transaction_type="buy",
                wallet_address=wallet,
                token_id="token1",
                shares=100.0,
                price_per_share=0.50,
                usdc_amount=50.0
            )
            txn.upsert_transaction(
                tx_hash="0xclaim",
                log_index=0,
                block_number=50001000,
                block_timestamp=now,
                transaction_type="claim",
                wallet_address=wallet,
                token_id="token1",
                shares=100.0,
                usdc_amount=100.0
            )
        
        # Position should now be zero
        with temp_db.transaction() as txn:
            positions = txn.get_computed_positions(wallet)
        
        # All shares claimed, no open position
        assert len(positions) == 0


# ==================== LEGACY MODE TESTS ====================

class TestLegacyMode:
    """Test coordinator in legacy mode (chain sync disabled)"""
    
    @pytest.mark.asyncio
    async def test_legacy_reconciliation(self, temp_db, test_config_legacy, mock_api):
        """Test legacy reconciliation when chain sync is disabled"""
        mock_api.fetch_usdc_balance = AsyncMock(return_value=1000.0)
        mock_api.fetch_positions = AsyncMock(return_value=[])
        
        coordinator = RiskCoordinator(
            config=test_config_legacy,
            storage=temp_db,
            api=mock_api
        )
        
        # Should use legacy reconciliation
        await coordinator.startup("test-agent", "test")
        
        assert coordinator._reconciled
        
        await coordinator.shutdown()


# Run tests if executed directly
if __name__ == "__main__":
    pytest.main([__file__, "-v"])

